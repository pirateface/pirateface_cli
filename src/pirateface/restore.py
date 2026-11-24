"""pf restore: upload .torrent files to restore missing metadata on Pirate Face."""

import http.client
import sys
from pathlib import Path

import libtorrent as lt

from pirateface.client import api
from pirateface.config import API, MAX_UPLOAD_BYTES
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

    for path in files:
        try:
            if path.stat().st_size > MAX_UPLOAD_BYTES:
                print(f"too large (>3 MB): {path}")
                continue
            data = path.read_bytes()
        except OSError as e:
            print(f"could not read {path}: {e.strerror or e}")
            continue
        data = _scrub(data)
        if data is None:
            print(f"skipped (not a valid .torrent): {path.name}")
            continue
        try:
            status, body = api.restore(key, data)
        except (OSError, http.client.HTTPException) as e:
            sys.exit(f"error: could not reach {API} ({getattr(e, 'reason', e)}); nothing more was sent")
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
