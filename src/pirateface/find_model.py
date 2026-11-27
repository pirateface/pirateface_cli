"""sha256 -> listed model lookup: what does this file hash to, and which listed models hold it?

The site indexes listed uploads by SHA-256 (>= 1 MiB, Hugging Face backed) behind a POST
/api/files/by-sha256 endpoint, so the whole lookup is one request. A file the site does not
list (or is too small to identify honestly by SHA-256) reports no match -- that is the honest limit,
and never a guess.
"""

import sys
from pathlib import Path

from pirateface import ui
from pirateface._hash import sha256_file
from pirateface.client.api import Unreachable, Unreadable, rows_by_sha256

MIN_FILE_SIZE = 1 << 20  # 1 MiB: tiny configs and tokenizers are too small to identify honestly by sha256


def find(path, out=sys.stdout):
    """Print every listed model whose sha256 the file matches. Exit code: 0 if any found, 1 otherwise."""
    path = Path(path)
    if not path.is_file():
        print(f"error: {path} is not a file", file=sys.stderr)
        return 1
    try:
        file_size = path.stat().st_size
    except OSError as e:
        print(f"error: {path} is not a file ({e})", file=sys.stderr)
        return 1
    if file_size < MIN_FILE_SIZE:
        print(
            f"error: {path} is under {MIN_FILE_SIZE // (1 << 20)} MiB; too small to identify by sha256",
            file=sys.stderr,
        )
        return 1
    try:
        digest = sha256_file(path)
    except OSError as e:
        print(f"error: {path} could not be read ({e})", file=sys.stderr)
        return 1
    print(f"hash: {digest}", file=sys.stderr)

    try:
        rows = rows_by_sha256(digest)
    except (Unreachable, Unreadable) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if not rows:
        print("no listed model holds this file", file=sys.stderr)
        return 1
    for row in rows:
        row_size = row.get("length")
        size_s = ui.gb(row_size) if isinstance(row_size, int) and row_size >= 0 else "?"
        info = str(row.get("infohash") or "")
        info_s = info[:12] if info else "?"
        print(f"{row['repoId']}  {row['path']}  ({size_s}, {info_s})", file=out)
    return 0
