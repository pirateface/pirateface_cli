"""Local file-boundary and piece checkpoints for a work lane: the shared path/write helpers and the
checkpoint load/save machinery that rebuilds a job from what a previous run already hashed."""

import contextlib
import hashlib
import json

from pirateface._atomic import write_bytes
from pirateface.seed import user
from pirateface.seed.hybrid import BLOCK, ceil_blocks, torrent_order

STATE_FILE = user.KEY_FILE.parent / "work-lease.json"


def _path(name, kind="lease", suffix=".json"):
    return STATE_FILE.with_name(f"work-{kind}{'' if name is None else '-' + name}{suffix}")


def _write(path, data):
    """Write JSON to path atomically (temp file + rename), readable by this user only."""
    write_bytes(path, (json.dumps(data) + "\n").encode("utf-8"), mode=0o600)


def blocks_path(name=None):
    """Path of a lane's file-boundary block hashes."""
    return _path(name, "files", ".blocks")


def clear_files(name=None):
    """Remove the saved piece checkpoint (the progress file and the block-hash sidecar). The lease is left alone."""
    _path(name, "files").unlink(missing_ok=True)
    blocks_path(name).unlink(missing_ok=True)


def sorted_files(files):
    """A job's files in torrent order, the order the torrent lists them."""
    return sorted(files, key=lambda f: torrent_order(f["path"].split("/")))


def _job_key(job, plen, proofs):
    files = [[f["path"], f["size"], f.get("sha256")] for f in sorted_files(job["files"])]
    return [job["repoId"], job["revision"], plen, proofs, files]


def _partial_checksum(partial):
    values = [partial[k] for k in ("path", "offset", "v1", "roots", "sha256")]
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


def load_progress(job, plen, proofs, name=None):
    """Load completed files and the partial file; discard anything that does not check out.

    A checkpoint that cannot be trusted — wrong shape, a different job, a corrupt partial, missing block
    hashes — is thrown away and the job rebuilds from scratch. Nothing here raises."""
    path = _path(name, "files")
    if not path.exists():
        return [], None
    try:
        saved = json.loads(path.read_text())
        done = saved["files"]
        expected = _job_key(job, plen, proofs)
        saved_key = saved["job"]
        if (
            isinstance(saved_key, list)
            and len(saved_key) == 4
            and isinstance(saved_key[0], str)
            and isinstance(saved_key[1], str)
            and isinstance(saved_key[2], int)
            and isinstance(saved_key[3], bool)
            and isinstance(done, list)
        ):
            clear_files(name)
            print("    Discarded the older file-only checkpoint; progress will be saved in the new format.")
            return [], None
        if not isinstance(saved_key, list) or len(saved_key) != 5:
            raise ValueError("checkpoint version cannot be validated")
        if (saved_key[0], saved_key[1], saved_key[4]) != (expected[0], expected[1], expected[4]):
            clear_files(name)
            print("    Discarded progress for a different model, revision or file list.")
            return [], None
        if saved_key != expected or not isinstance(done, list):
            raise ValueError("checkpoint uses a different piece length or proof configuration")
        entries = expected[-1]
        if len(done) > len(entries) or any(
            f["path"] != entry[0] or f["size"] != entry[1] for f, entry in zip(done, entries, strict=False)
        ):
            raise ValueError("saved files do not match the job")
        partial = saved.get("partial")
        if partial is not None:
            if len(done) >= len(entries) or not isinstance(partial, dict):
                raise ValueError("invalid partial-file checkpoint")
            if partial.get("checksum") != _partial_checksum(partial):
                raise ValueError("partial-file checkpoint checksum does not match")
            size = partial["offset"]
            if partial["path"] != entries[len(done)][0] or not isinstance(size, int):
                raise ValueError("partial file does not match the job")
            if size <= 0 or size >= entries[len(done)][1] or size % plen:
                raise ValueError("invalid partial-file offset")
            if len(bytes.fromhex(partial["v1"])) != (size // plen) * 20:
                raise ValueError("invalid partial v1 hashes")
            if len(bytes.fromhex(partial["roots"])) != (size // plen) * 32:
                raise ValueError("invalid partial v2 roots")
            if len(bytes.fromhex(partial["sha256"])) != 40:
                raise ValueError("invalid partial SHA-256 state")
        need = 32 * (sum(ceil_blocks(f["size"]) for f in done) + (partial["offset"] // BLOCK if partial else 0))
        if proofs and need and blocks_path(name).stat().st_size < need:
            raise ValueError("saved block hashes are incomplete")
        return done, partial
    except (OSError, KeyError, TypeError, ValueError) as e:
        with contextlib.suppress(OSError):
            clear_files(name)
        print(f"    Discarded unusable file checkpoint ({e}); rebuilding from scratch.")
        return [], None


def save_files(job, plen, proofs, done, name=None, partial=None):
    if partial is not None:
        partial = {**partial, "checksum": _partial_checksum(partial)}
    _write(_path(name, "files"), {"job": _job_key(job, plen, proofs), "files": done, "partial": partial})
