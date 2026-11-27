"""Parsing of a cached magnet-index shard: magnet -> Entry, with build order and removals applied.

The sync half of the index (downloading shards, digests, state) lives in ``pirateface.index.mirror``;
this module answers every lookup from files that are already on disk.
"""

import base64
import contextlib
import functools
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from pirateface import config
from pirateface._regex import COMMIT_ANY as _HASH
from pirateface._regex import REPO_STRICT as _REPO
from pirateface.client import api

# The web seed in an index magnet is a URL libtorrent may fetch pieces from, so only the site's own (or
# the configured mirror's) host is accepted: a shard cannot point a lookup at somebody else's server.
WS_HOSTS = {"pirateface.co"}
_BTMH = re.compile(r"1220[0-9a-fA-F]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")  # lowercase-only 40-char commit ids, from the web seed path

DIR = Path(config.CACHE_DIR) / "index"
SHARDS = DIR / "shards"

_SHARD = re.compile(r"^(\d{4}-\d{2}-\d{2})\.txt$")
_REMOVED = re.compile(r"^removed-(\d{4}-\d{2}-\d{2})\.txt$")


@dataclass(frozen=True)
class Entry:
    magnet: str
    infohash: str
    build: str  # the shard date this magnet was last written in
    commit: str | None  # from the web seed's path, when it names a full commit


def _query(magnet):
    """The magnet's parsed query dict, or None when the line cannot be parsed."""
    try:
        return urllib.parse.parse_qs(urllib.parse.urlparse(magnet).query)
    except ValueError:
        return None


def _web_seed_from(query):
    """(repo id, commit or None) from a magnet's web seed, or None when the line cannot be trusted.

    A shard holds a bare magnet: the web seed's path is the only place the repo id appears, so the
    host must be the site's own (or PF_API's) and the name a plain owner/name -- a shard must not
    point a lookup at another server, at `..`, or at a truncated path.
    """
    ws = query.get("ws") or []
    if len(ws) != 1:
        return None  # libtorrent honours every ws= in a magnet; a second one would be an unchecked server
    try:
        url = urllib.parse.urlparse(ws[0])
    except ValueError:
        return None  # a line urllib cannot parse (an unmatched "[" in the host, say) is skipped like any other
    try:
        hosts = WS_HOSTS | {urllib.parse.urlsplit(config.API).hostname or ""}
    except ValueError:
        hosts = WS_HOSTS  # a malformed PF_API must not take the index down with it
    if url.scheme not in ("http", "https") or url.hostname not in hosts:
        return None
    parts = [part for part in url.path.split("/") if part]
    if len(parts) < 4 or parts[0] != "api" or parts[1] != "ws":
        return None
    repo = f"{parts[2]}/{parts[3]}"
    commit = parts[4] if len(parts) > 4 and _COMMIT.fullmatch(parts[4]) else None
    return (repo, commit, ws[0]) if _REPO.fullmatch(repo) else None


def _magnet_for(infohash, query, ws_url):
    """A magnet built only from what was validated: the v1 infohash (plus a hex-validated v2 one), one web
    seed, and the site's own trackers. A shard's tracker URLs and any other decoration are never carried
    into a lookup: libtorrent would be talking to a server the shard can name."""
    # The xt values are written out literally: urlencode percent-encodes the ":" and "/" in them, which
    # breaks the magnet's standard form and the tools that parse it. Only the ws= and tr= values are quoted
    # -- they are URLs that must survive inside the query string.
    parts = [f"xt=urn:btih:{infohash}"]
    for xt in query.get("xt", ()):
        digest = xt.removeprefix("urn:btmh:")
        if digest == xt:
            continue
        if _BTMH.fullmatch(digest):
            parts.append(f"xt=urn:btmh:{digest.lower()}")
            break
    if ws_url:
        parts.append(f"ws={urllib.parse.quote(ws_url, safe='')}")
    for tracker in api.TRACKERS:
        parts.append(f"tr={urllib.parse.quote(tracker, safe='')}")
    return "magnet:?" + "&".join(parts)


def infohash_of(magnet):
    """The magnet's v1 infohash as lowercase hex (btih is hex or base32), or None."""
    query = _query(magnet)
    if query is None:
        return None
    return _infohash_from(query)


def _infohash_from(query):
    for xt in query.get("xt", ()):
        digest = xt.removeprefix("urn:btih:")
        if digest == xt:
            continue
        if len(digest) == 32:
            with contextlib.suppress(ValueError):
                return base64.b32decode(digest.upper()).hex()
        elif len(digest) == 40 and _HASH.fullmatch(digest):
            return digest.lower()
    return None


def dates():
    """Every build date in the cache, oldest first.

    A build that only drops models ships a removals file and no shard, so both names count: taking dates
    from the shards alone would ignore exactly the builds that publish removals and nothing else.
    """
    if not SHARDS.is_dir():
        return []
    found = set()
    try:
        entries = list(SHARDS.iterdir())
    except OSError:
        return []  # unreadable: answer nothing rather than raise out of every lookup
    for path in entries:
        match = _SHARD.match(path.name) or _REMOVED.match(path.name)
        if match:
            found.add(match[1])
    return sorted(found)


def build():
    """The newest build date in the cache: the build this copy of the index is. None before any sync."""
    known = dates()
    return known[-1] if known else None


@functools.cache
def _parsed():
    """({repo_id: Entry}, removals_lost): every build in date order, each date's removals applied right
    after that shard.

    A removal names an infohash, not a repo: it retires that torrent. Order matters: a removal deletes
    what is known at that date, so a model dropped and re-added later stays listed; applying every
    removal at the end would delete the re-add. Within one shard the last line wins, which is what the
    index page documents for a re-upload.

    A removals list is never traded away for anything. If one cannot be read, this copy of the index
    cannot say what is still allowed and answers nothing at all, so the files come from Hugging Face
    until a sync replaces the file -- the alternative is resurrecting a model the site removed. A shard
    that cannot be read only costs that build's additions, which is the safe direction, and its removals
    still apply.
    """
    found = {}
    for date in dates():
        gone = _removed_hashes(SHARDS / f"removed-{date}.txt")
        if gone is None:
            return {}, True
        for magnet in (line.strip() for line in _lines(SHARDS / f"{date}.txt") or []):
            query = _query(magnet) if magnet else None
            if query is None:
                continue
            seed = _web_seed_from(query)
            infohash = _infohash_from(query) if seed else None
            if seed and infohash:
                repo_id, commit, ws_url = seed
                found[repo_id] = Entry(_magnet_for(infohash, query, ws_url), infohash, date, commit)
        for repo_id, entry in list(found.items()):
            if entry.infohash in gone:
                del found[repo_id]
    return found, False


def broken():
    """True when a removals list could not be read: this copy answers nothing and Hugging Face gets every
    file, until a sync replaces the unreadable file."""
    return _parsed()[1]


def _removed_hashes(path):
    """The infohashes a removals file names (empty when there is none), or None when it cannot be trusted.

    Every non-blank line must be exactly 40 hex characters. A stray character makes the whole file
    unreadable instead of yielding a different hash: searching each line for a hex run shifts the window,
    so `9` followed by a hash reads as a hash that names nothing and the removal is quietly missed.
    """
    lines = _lines(path)
    if lines is None:
        return None
    found = set()
    for line in lines:
        text = line.strip()
        if not text:
            continue
        if not _HASH.fullmatch(text):
            return None
        found.add(text.lower())
    return found


def _lines(path):
    """A shard's lines, [] when there is no such file, None when it exists but cannot be read.

    One unreadable or half-written file must not take the whole index down -- a missing entry only costs a
    fallback to Hugging Face, while a raised exception would cost every entry -- but it must not be read as
    an empty file either: that would apply a build without its removals.
    """
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return []
    except OSError:
        return None


def count():
    """How many repos the cached index lists."""
    return len(_parsed()[0])


def magnets():
    """(magnet, infohash) for every repo the cached index lists, in build order with removals applied.

    The same walk the lookup uses: each build's shard in date order, then that build's removals -- so a
    model dropped and re-added later still comes back, and a removal deletes only what was known at its
    date. `pf index --add` adds these to a torrent client."""
    return [(entry.magnet, entry.infohash) for entry in _parsed()[0].values()]


def resolve(repo_id):
    """The cached index's Entry for the repo, or None when this copy of the index does not list it."""
    return _parsed()[0].get(repo_id)
