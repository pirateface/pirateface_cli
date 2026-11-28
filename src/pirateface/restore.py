"""pf restore: upload .torrent files to restore missing metadata on Pirate Face."""

import http.client
import sys
from pathlib import Path

import libtorrent as lt

from pirateface.client import api
from pirateface.config import API, TORRENT_OVERSIZE_MESSAGE
from pirateface.seed import user


def _scrub(data):
    """A .torrent's info dict re-encoded on its own, or None when the bytes are not a .torrent this command
    can re-encode. The private tracker, comments and any other decoration is dropped before the file is
    uploaded: only the info that names the content goes to the site."""
    try:
        decoded = lt.bdecode(data)
    except Exception:
        return None
    if not isinstance(decoded, dict):
        return None
    info = decoded.get(b"info")
    if not isinstance(info, dict):
        return None
    try:
        return lt.bencode({b"info": info})
    except Exception:
        return None


def _collect(paths):
    seen, files = set(), []
    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_dir():
            candidates = sorted(p.rglob("*.torrent"))
        elif p.is_file():
            candidates = [p]
        else:
            print(f"not found: {raw}", file=sys.stderr)
            continue
        for c in candidates:
            rp = c.resolve()
            if rp not in seen:
                seen.add(rp)
                files.append(c)
    return files


def run(args):
    user.YES = args.yes
    files = _collect(args.paths)
    if not files:
        sys.exit("error: no .torrent files found")
    try:
        key = user.load_key()
    except user.SeedError as e:
        sys.exit(f"error: {e}")

    failures = 0
    for path in files:
        try:
            if user.torrent_oversize(path.stat().st_size):
                print(f"{TORRENT_OVERSIZE_MESSAGE}: {path}")
                failures += 1
                continue
            data = path.read_bytes()
        except OSError as e:
            print(f"could not read {path}: {e.strerror or e}")
            failures += 1
            continue
        data = _scrub(data)
        if data is None:
            print(f"skipped (not a valid .torrent): {path.name}")
            failures += 1
            continue
        try:
            status, body = api.restore(key, data)
        except (OSError, http.client.HTTPException) as e:
            sys.exit(f"error: could not reach {API} ({getattr(e, 'reason', e)}); nothing more was sent")
        except ValueError as e:
            # The site answered with something the client cannot read (an empty or non-JSON body): a
            # per-file failure, not a crash, so the rest of the batch still runs.
            print(f"error (response unreadable): {path.name} - {e}")
            failures += 1
            continue
        if not isinstance(body, dict):
            # The site answered with a body of the wrong shape: treat it as unreadable rather than
            # crashing on body.get() below.
            print(f"error (unexpected response): {path.name}")
            failures += 1
            continue
        if status == 200:
            print(f"restored: {path.name} -> {body.get('repoId', '')} ({body.get('infohash', '')})")
        elif status == 404:
            print(f"not listed: {path.name}")
        elif status == 409:
            print(f"already has metadata: {path.name}")
        elif status in (401, 403):
            sys.exit(f"error: {body.get('error', 'authentication failed')}")
        else:
            print(f"error ({status}): {path.name} - {body.get('error', 'request failed')}")
            failures += 1
    if failures:
        sys.exit(f"error: {failures} of {len(files)} .torrent file(s) could not be restored")
