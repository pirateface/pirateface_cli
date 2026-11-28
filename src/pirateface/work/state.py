"""Private local lease for a work lane: the saved lease checkpoint, its exclusive lock and the shapes it
has to be in to be trusted. The file/piece checkpoint machinery (paths, progress, block hashes) lives in
work/checkpoint.py; importers use that module directly."""

import contextlib
import json
import os

from pirateface.work.checkpoint import _path, _write, clear_files

__all__ = [
    "CheckpointLocked",
    "clear",
    "lease_path",
    "load",
    "lock",
    "save",
]


class CheckpointLocked(Exception):
    """Another process currently owns this worker's checkpoint."""


def lease_path(name=None):
    """Path to this worker's saved lease checkpoint, for user-facing recovery messages."""
    return _path(name)


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
