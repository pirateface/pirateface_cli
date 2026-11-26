"""sha256 -> listed model lookup: what does this file hash to, and which listed models hold it?

The site indexes listed uploads by SHA-256 (>= 1 MiB, Hugging Face backed) behind a batched
POST /api/files/by-sha256 endpoint, so the whole lookup is one request. A file the site does not
list (or is too small to identify honestly by SHA-256) reports no match -- that is the honest limit,
and never a guess.
"""

import hashlib
import json
import sys
import urllib.error
from pathlib import Path

from pirateface._http import NO_REDIRECTS, request_json
from pirateface.config import API

MIN_FILE_SIZE = 1 << 20  # 1 MiB: tiny configs and tokenizers are too small to identify honestly by sha256
BATCH_SIZE = 100  # the endpoint refuses batches larger than this
TIMEOUT = 30


def hash_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _batches(items, size):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _lookup(digests):
    """The listed rows for each sha256 in digests (at most BATCH_SIZE per request).

    Returns ({digest: [rows]}, error_message). Rows are {repoId, infohash, path, length, root,
    sha256} dicts. On a transport failure or an unreadable answer, the partial map is returned
    with error_message set -- a failed batch is not a match.
    """
    rows_by_digest = {d: [] for d in digests}
    error = None
    for batch in _batches(digests, BATCH_SIZE):
        payload = json.dumps({"hashes": batch}).encode()
        try:
            status, body = request_json(
                f"{API}/api/files/by-sha256",
                TIMEOUT,
                policy=NO_REDIRECTS,
                method="POST",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
        except urllib.error.HTTPError as e:
            error = f"the site refused the lookup (HTTP {e.code})"
            return rows_by_digest, error
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            error = f"could not reach {API} ({e})"
            return rows_by_digest, error
        except json.JSONDecodeError as e:
            error = f"the site's answer could not be read ({e})"
            return rows_by_digest, error
        if status != 200:
            error = f"the site answered HTTP {status}"
            return rows_by_digest, error
        if not isinstance(body, dict):
            error = "the site's answer was not a {hash: [rows]} map"
            return rows_by_digest, error
        for d in batch:
            rows = body.get(d)
            if isinstance(rows, list):
                rows_by_digest[d] = [
                    row for row in rows if isinstance(row, dict) and row.get("repoId") and row.get("path")
                ]
            else:
                error = f"the site's answer was missing or malformed for {d}"
                return rows_by_digest, error
    return rows_by_digest, error


def find(path, out=sys.stdout):
    """Print every listed model whose sha256 the file matches. Exit code: 0 if any found, 1 otherwise."""
    path = Path(path)
    if not path.is_file():
        print(f"error: {path} is not a file", file=sys.stderr)
        return 1
    if path.stat().st_size < MIN_FILE_SIZE:
        print(
            f"error: {path} is under {MIN_FILE_SIZE // (1 << 20)} MiB; too small to identify by sha256", file=sys.stderr
        )
        return 1
    digest = hash_file(path)
    print(f"hash: {digest}", file=sys.stderr)

    found, error = _lookup([digest])
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    rows = found[digest]
    if not rows:
        print("no listed model holds this file", file=sys.stderr)
        return 1
    for row in rows:
        size = row.get("length")
        size_s = f"{size / 1e9:.2f} GB" if isinstance(size, int) and size >= 0 else "?"
        info = str(row.get("infohash") or "")
        info_s = info[:12] if info else "?"
        print(f"{row['repoId']}  {row['path']}  ({size_s}, {info_s})", file=out)
    return 0
