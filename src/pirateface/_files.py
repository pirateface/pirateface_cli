"""One way to ask whether a path is a regular file of a given size."""


def size_matches(path, size):
    """True when path is a regular file whose size equals size. An unreadable or missing path is never a match."""
    if path is None:
        return False
    try:
        return path.is_file() and path.stat().st_size == size
    except OSError:
        return False
