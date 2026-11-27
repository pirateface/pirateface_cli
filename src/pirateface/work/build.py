"""Builds one job's hybrid torrent by streaming its files, and checks the result against the job."""

import time

from pirateface import ui
from pirateface.seed import hybrid
from pirateface.work import source, state
from pirateface.work.errors import WorkError
from pirateface.work.renewal import Lease
from pirateface.work.resumable_hash import ResumableSHA256


def build(job, key, trackers, name=None, blocks=None, max_download_rate=None, *, saved=None):
    """Stream a job with piece checkpoints or file-boundary checkpoints."""
    repo, revision = job["repoId"], job["revision"]
    files = state.sorted_files(job["files"])
    total = sum(f["size"] for f in files)
    plen, have_proofs = hybrid.piece_length(total), blocks is not None
    done, partial = saved if saved is not None else state.load_progress(job, plen, have_proofs, name)
    try:
        ResumableSHA256()
        piece_checkpoints = True
    except RuntimeError:
        piece_checkpoints = False
        print("    Using file-boundary checkpoints (resumable hashing unavailable).")
    if partial and not piece_checkpoints:
        print("    Discarding unfinished piece progress; the interrupted file will be read again.")
        partial = None
        state.save_files(job, plen, have_proofs, done, name)
    if blocks is not None:
        if not done and partial is None and state.blocks_path(name).exists() and state.blocks_path(name).stat().st_size:
            print("    Discarding incomplete block hashes; no finished file was checkpointed.")
        kept = [(f["path"], -(-f["size"] // hybrid.BLOCK)) for f in done]
        if partial:
            kept.append((partial["path"], partial["offset"] // hybrid.BLOCK))
        blocks.keep(kept)
        if partial:
            blocks.verify_partial(partial["path"], bytes.fromhex(partial["roots"]), plen)
    lease = Lease(key, job["leaseId"], total, name)
    lease.renew()  # confirm we hold the lease before streaming
    lease.start()
    progress = ui.work_progress(total)
    rate_limiter = source.RateLimiter(max_download_rate) if max_download_rate is not None else None
    sums = {f["path"]: (f["sha256"], f["size"]) for f in done}

    def on_read(n):
        lease.note(n)
        progress.update(n)

    def on_reuse(n):
        lease.note(n)
        ui.work_reuse(progress, n)

    def on_end(path, digest, size):
        sums[path] = (digest, size)

    def on_file(path, hashes):
        nonlocal partial
        size, *rest = hashes
        done.append({"path": path, "size": size, "sha256": sums[path][0], "hashes": [h.hex() for h in rest]})
        partial = None
        if blocks is not None:
            blocks.flush()
        state.save_files(job, plen, have_proofs, done, name)

    sizes = {f["path"]: f["size"] for f in files}

    last_piece_save = 0.0  # seconds since monotonic; piece boundaries save at most every few seconds

    def on_piece(path, offset, v1, roots, stream):
        nonlocal partial, last_piece_save
        if offset >= sizes[path]:
            return  # the finished-file checkpoint follows; avoid an EOF Range request
        partial = {
            "path": path,
            "offset": offset,
            "v1": v1.hex(),
            "roots": roots.hex(),
            "sha256": stream.sha.checkpoint().hex(),
        }
        if blocks is not None:
            blocks.flush()
        now = time.monotonic()
        if now - last_piece_save >= 5.0:
            last_piece_save = now
            state.save_files(job, plen, have_proofs, done, name, partial=partial)

    def opener(path):
        if not piece_checkpoints:
            return source.open_file(repo, revision, path, on_read, on_end, rate_limiter)
        if partial and partial["path"] == path:
            offset = partial["offset"]
            sha = ResumableSHA256.restore(bytes.fromhex(partial["sha256"]), offset)
        else:
            offset, sha = 0, ResumableSHA256()
        return source.open_file(repo, revision, path, on_read, on_end, rate_limiter, offset, sha)

    try:
        if done or partial:
            print(f"    resuming: {len(done)} of {len(files)} files already hashed")
            on_reuse(sum(f["size"] for f in done) + (partial["offset"] if partial else 0))
        finished = {f["path"]: (f["size"], *map(bytes.fromhex, f["hashes"])) for f in done}
        sources = [(f["path"].split("/"), lambda f=f: opener(f["path"])) for f in files]
        data, v1, _v2 = hybrid.build_from(
            repo.split("/")[1],
            sources,
            trackers,
            plen,
            blocks=blocks,
            done=finished,
            on_file=on_file,
            partial=partial,
            on_piece=on_piece if piece_checkpoints else None,
        )
    finally:
        lease.stop()
        progress.close()
    lease.renew()  # the last renewal before the result is submitted
    for f in files:
        digest, size = sums.get(f["path"], (None, None))
        if size != f["size"]:
            raise WorkError(f"{f['path']} is {size} bytes from Hugging Face, not {f['size']}.")
        if f.get("sha256") and digest != f["sha256"]:
            raise WorkError(f"{f['path']} does not match its LFS SHA-256 at {revision[:12]}.")
    manifest = [{"path": f["path"], "sha256": sums[f["path"]][0]} for f in files]
    return data, manifest, v1
