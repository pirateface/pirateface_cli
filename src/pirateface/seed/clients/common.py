"""Shared pieces for the user's torrent clients: the refusal error, the Pirate Face tracker regexes and HELP."""

import re

HELP = (
    "Transmission: its remote access (RPC) must be on (TR_HOST, TR_AUTH=user:pass). "
    "qBittorrent: the Web UI must be on (QBT_URL, QBT_USER, QBT_PASS)."
)


class LoginRefused(Exception):
    """The client answered but wouldn't let us in: worth telling the user, unlike a client that isn't running."""


_PIRATEFACE = re.compile(r"^(udp|https?)://tracker\.pirateface\.co[:/]")
KEYED = re.compile(r"^https?://tracker\.pirateface\.co:6970/[0-9a-f]+/announce$")


def is_pirateface(urls):
    return any(_PIRATEFACE.match(u) for u in urls)


def tracker_update_loop(torrents, mine, get_urls, mutate):
    """The shared skeleton of the two clients' update_trackers: walk the torrents, and for each Pirate Face
    torrent that lacks `mine`, let mutate(entry, urls) change its trackers; return (added, current, skipped,
    changed) where changed holds the entries whose trackers were updated."""
    added = current = skipped = 0
    changed = []
    for entry in torrents:
        urls = get_urls(entry)
        if not is_pirateface(urls):
            skipped += 1
            continue
        if mine in urls:
            current += 1
            continue
        mutate(entry, urls)
        added += 1
        changed.append(entry)
    return added, current, skipped, changed
