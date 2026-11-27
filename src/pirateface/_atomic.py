"""One way to write a file atomically: a temp file in the same folder, then a rename."""

import os
import shutil
from pathlib import Path


def write_bytes(path, data, mode=None):
    """Write bytes to path atomically, so a crash or a full disk leaves no half-written file behind.

    mode: the temp file's permission bits (e.g. 0o600 for a file readable by this user only); None keeps the
    default umask.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.part")
    try:
        if mode is None:
            tmp.write_bytes(data)
        else:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def copy_file(src, dest):
    """Copy a file into place atomically: a temp file in dest's folder, then a rename.

    The temp name carries the pid, so concurrent copies cannot share one; a failure removes the temp file,
    so a crash or a full disk leaves no half-written file behind.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"{dest.name}.{os.getpid()}.part")
    try:
        shutil.copyfile(src, tmp)
        tmp.replace(dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
