"""One way to ask whether a path is a regular file of a given size."""

import sys


def size_matches(path, size):
    """True when path is a regular file whose size equals size. An unreadable or missing path is never a match."""
    if path is None:
        return False
    try:
        return path.is_file() and path.stat().st_size == size
    except OSError:
        return False


def skip_line(source):
    """The "Skipping ...: unreadable or wrong size" line for a source that did not match the expected size."""
    return f"Skipping {source}: unreadable or wrong size"


def warn_skip(source):
    """Print the skip line for a source that exists but does not match the expected size.

    Missing sources stay silent: in from_cache a blob that is not in the cache is an expected skip,
    not a warning."""
    if source is not None and (source.is_file() or source.exists()):
        print(skip_line(source), file=sys.stderr)
