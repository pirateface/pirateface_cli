"""A cached copy of Pirate Face's magnet index: model id -> magnet, readable while the site is down.

The index is a torrent of weekly shards (``shards/YYYY-MM-DD.txt``, one bare magnet per line) plus, from
the first build that drops something, ``shards/removed-YYYY-MM-DD.txt`` with one v1 infohash per line.
pf mirrors those shards into PF_CACHE/index/shards/ and answers lookups from that copy when pirateface.co
cannot be reached, so a model can still be downloaded instead of falling back to Hugging Face.
"""

import json
import threading
import time
from pathlib import Path

from pirateface._atomic import copy_file, write_bytes
from pirateface._hash import sha256_file
from pirateface.client import api, torrent
from pirateface.index import FILE, INDEX_REPO
from pirateface.index.shards import (
    _REMOVED,
    _SHARD,
    DIR,
    SHARDS,
    _parsed,
    broken,
    build,
    count,
    infohash_of,
    magnets,
    resolve,
)

# Carried in the package as the fallback for a copy that has never synced: the site names the current build,
# and this is the 2026-11-09 build for a client that has not asked yet.
BOOTSTRAP_MAGNET = (
    "magnet:?xt=urn:btih:5f47f5c8edd0ffde0142229bb1fbe111f33d773f"
    "&xt=urn:btmh:12207d3e6dfe08b37a6ea047fb9f4541b183fa95e18cd5660eb007f8ea47e4b4d663"
    "&tr=udp%3A%2F%2Ftracker.pirateface.co%3A6969%2Fannounce"
    "&tr=http%3A%2F%2Ftracker.pirateface.co%3A6969%2Fannounce"
)

STATE = DIR / "state.json"

_sync_lock = threading.Lock()

__all__ = [
    "broken",
    "build",
    "count",
    "infohash_of",
    "listing",
    "magnets",
    "resolve",
    "state",
    "sync",
]


def listing(repo_id):
    """The repo as the cached index names it, in api.listed()'s shape, or None.

    No sha256 per file (None): the magnet's infohash commits to every piece, and libtorrent counts a piece
    downloaded only once its hash matches, so the torrent's own hashes are the check.
    """
    entry = resolve(repo_id)
    if entry is None or entry.commit is None:
        return None
    try:
        files = torrent.swarm_files(repo_id, magnet=entry.magnet, where="the cached index")
    except (torrent.Unavailable, TimeoutError):
        return None
    return {
        "repo_id": repo_id,
        "commit": entry.commit,
        "files": {path: (size, None, None) for path, size in files},
        "trust": f"the cached index (build {entry.build}): infohash {entry.infohash}",
    }


def state():
    """What the last sync recorded: build date, magnet, shard names and when it ran. {} if unknown."""
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_state(**fields):
    write_bytes(STATE, json.dumps(fields, indent=1, sort_keys=True).encode("utf-8"))


def _digest(path):
    """sha256 of a cached file, or None if it cannot be read."""
    try:
        return sha256_file(path)
    except OSError:
        return None


def _unchanged(path, size, recorded):
    """Whether the cached file is the copy this build names.

    The size alone misses a rewrite at the same size, so a sync records each file's sha256 and compares it
    too. A copy mirrored before digests existed is judged by size and gains a recorded digest on the next
    sync. A file that cannot be read is never the copy this build names: otherwise `pf index` tells the
    user to run a sync that would keep the very file it is complaining about.
    """
    try:
        if not path.is_file() or path.stat().st_size != size:
            return False
    except OSError:
        return False
    digest = _digest(path)
    if digest is None:
        return False  # unreadable: refetch it rather than trust the size
    return recorded is None or digest == recorded


def published():
    """The build the site says is current, as (magnet, infohash), or None when it does not answer.

    /api/torrents answers for the index now, so the site is the first place to look: a client asks once
    per sync and gets everything else over the torrent. The magnet carried in the package is only the
    starting point for a copy that has never synced.
    """
    try:
        answer = api.lookup(INDEX_REPO)
    except api.Unreachable:
        return None
    if not (answer.found and answer.entry.get("magnet")):
        return None
    return answer.entry["magnet"], answer.entry.get("infohash") or infohash_of(answer.entry["magnet"])


def _remote_magnet():
    """The build to join: the site's, else the magnet from the last sync, else the one in the package."""
    found = published()
    if found:
        return found[0]
    return state().get("magnet") or BOOTSTRAP_MAGNET


def sync():
    """Mirror the index torrent's shards into PF_CACHE/index/shards/ and return the build date.

    Removals sort before their shard so a half-finished mirror leaves every shard next to its removals,
    not the models a build dropped listed -- and downloadable -- until someone noticed. A cached copy is
    replaced when it is not the copy this build names: missing, wrong in size, or (once a digest is
    recorded) different bytes at that size. The temp file carries the pid, so two processes syncing at
    once cannot share one.
    """
    with _sync_lock:
        magnet = _remote_magnet()
        files = []
        for path, size in torrent.swarm_files(INDEX_REPO, magnet=magnet, where="Pirate Face"):
            if not FILE.fullmatch(path):  # LICENSE and anything else a build carries are not lookups
                continue
            name = Path(path).name
            if match := _REMOVED.match(name):
                files.append((match[1], 0, path, size))  # removals first, for that date
            elif match := _SHARD.match(name):
                files.append((match[1], 1, path, size))
        SHARDS.mkdir(parents=True, exist_ok=True)
        digests = dict(state().get("digests") or {})
        try:
            for _, _, path, size in sorted(files):
                dest = SHARDS / Path(path).name
                name = dest.name
                if _unchanged(dest, size, digests.get(name)):
                    # Judged by size alone: record a digest now, so a rewrite at that size is caught instead
                    # of kept for the life of the copy. Guarded rather than setdefault, which would hash the
                    # file again on every sync.
                    if name not in digests:
                        digests[name] = _digest(dest)
                    continue
                src = torrent.fetch_file(INDEX_REPO, path, magnet=magnet)
                copy_file(src, dest)
                digests[name] = _digest(dest)
        finally:
            # However far it got, what resolve() reads must be what is on disk now.
            _parsed.cache_clear()
        date = build()
        shards = [path.name for path in SHARDS.iterdir() if _SHARD.match(path.name) or _REMOVED.match(path.name)]
        _write_state(
            build=date,
            magnet=magnet,
            shards=shards,
            digests={name: digests[name] for name in shards if digests.get(name)},
            synced=time.time(),
        )
        return date
