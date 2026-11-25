"""Shared pieces for the user's torrent clients: the refusal error, the Pirate Face tracker regexes and HELP."""

import re

HELP = (
    "Transmission: its remote access (RPC) must be on (TR_HOST, TR_AUTH=user:pass). "
    "qBittorrent: the Web UI must be on (QBT_URL, QBT_USER, QBT_PASS)."
)


class LoginRefused(Exception):
    """The client answered but wouldn't let us in: worth telling the user, unlike a client that isn't running."""


PIRATEFACE = re.compile(r"^(udp|https?)://tracker\.pirateface\.co[:/]")
KEYED = re.compile(r"^https?://tracker\.pirateface\.co:6970/[0-9a-f]+/announce$")


def is_pirateface(urls):
    return any(PIRATEFACE.match(u) for u in urls)
