"""Private local lease and file-boundary checkpoints for a work lane."""

import contextlib
import hashlib
import json
import os

from pirateface.seed import user
from pirateface.seed.hybrid import BLOCK, torrent_order

STATE_FILE = user.KEY_FILE.parent / "work-lease.json"


class CheckpointLocked(Exception):
    """Another process currently owns this worker's checkpoint."""


def _path(name, kind="lease", suffix=".json"):
    return STATE_FILE.with_name(f"work-{kind}{'' if name is None else '-' + name}{suffix}")


def lease_path(name=None):
    """Path to this worker's saved lease checkpoint, for user-facing recovery messages."""
    return _path(name)


def _write(path, data):
    """Write JSON to path atomically (temp file + rename), readable by this user only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
            f.write("\n")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _problem(state):
    """What is wrong with this saved lease, or None when it is the shape the worker indexes. Every consumer
    of a loaded lease reads `leaseId`, `job["repoId"]` (as an owner/name pair), `job["revision"]` and
    `job["files"]` (each entry a dict with a `path`) straight off, so a checkpoint missing or mistyping one
    used to raise KeyError, TypeError, IndexError or AttributeError out of the worker loop as a traceback.
    A file's size may be absent or null, because _job_size() reads both as "this lease does not say how big
    its files are", which is a work error it clears and pauses on, not corruption. Any other size is refused
    unless it is a whole number of bytes: a string reaches sum() as a TypeError, and a bool or a float is
    not a byte count even though both sum cleanly."""
    if not isinstance(state, dict):
        return f"the body is {type(state).__name__}, not an object"
    lease = state.get("leaseId")
    if not isinstance(lease, str) or not lease:
        return "it has no string leaseId"
    job = state.get("job")
    if not isinstance(job, dict):
        return "job is missing or not an object"
    for field in ("leaseId", "revision"):
        if not isinstance(job.get(field), str):
            return f"job.{field} is missing or not a string"
    if job.get("leaseId") != lease:
        return "job.leaseId does not match the saved leaseId"
    repo = job.get("repoId")
    if not isinstance(repo, str):
        return "job.repoId is missing or not a string"
    owner, _, name = repo.partition("/")
    if not owner or not name:
        return "job.repoId is not an owner/name pair"
    files = job.get("files")
    if not isinstance(files, list):
        return "job.files is missing or not a list"
    for index, item in enumerate(files):
        if not isinstance(item, dict):
            return f"job.files[{index}] is {type(item).__name__}, not an object"
        if not isinstance(item.get("path"), str):
            return f"job.files[{index}].path is missing or not a string"
        size = item.get("size")
        if size is not None and (isinstance(size, bool) or not isinstance(size, int)):
            return f"job.files[{index}].size is {type(size).__name__}, not a whole number of bytes"
    return None


def load(name=None):
    path = _path(name)
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise OSError(f"could not read saved work lease at {path}: {e}") from e
    problem = _problem(state)
    if problem:
        raise OSError(f"saved work lease at {path} is invalid ({problem}); remove it to start fresh")
    return state


@contextlib.contextmanager
def lock(name=None):
    """Exclusively claim a worker checkpoint until this process exits the context.

    Keep the lock file on disk: unlinking a locked file can let another process lock a new inode at the same path.
    """
    path = _path(name, "lock", ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise CheckpointLocked(f"could not acquire checkpoint lock at {path}") from error
        else:
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise CheckpointLocked(f"could not acquire checkpoint lock at {path}") from error
        locked = True
        yield
    finally:
        if locked:
            with contextlib.suppress(OSError):
                if os.name == "nt":
                    import msvcrt

                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def save(job, name=None):
    _write(_path(name), {"leaseId": job["leaseId"], "job": job})


def clear(name=None, files=False):
    """Delete the saved lease and, when requested, its file-boundary hashes."""
    _path(name).unlink(missing_ok=True)
    if files:
        clear_files(name)


def blocks_path(name=None):
    """Path of a lane's file-boundary block hashes."""
    return _path(name, "files", ".blocks")


def clear_files(name=None):
    """Remove the saved piece checkpoint (the progress file and the block-hash sidecar). The lease is left alone."""
    _path(name, "files").unlink(missing_ok=True)
    blocks_path(name).unlink(missing_ok=True)


def _job_key(job, plen, proofs):
    files = sorted(
        ([f["path"], f["size"], f.get("sha256")] for f in job["files"]),
        key=lambda f: torrent_order(f[0].split("/")),
    )
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
        need = 32 * (sum(-(-f["size"] // BLOCK) for f in done) + (partial["offset"] // BLOCK if partial else 0))
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
