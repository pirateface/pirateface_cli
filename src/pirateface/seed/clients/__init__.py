"""The user's torrent client: Transmission (RPC) or qBittorrent (Web UI).

The two implementations live in transmission.py and qbittorrent.py; the shared refusal error, tracker
regexes and the help line in common.py. This module is the facade every caller imports.
"""

from pirateface.seed.clients.common import HELP, LoginRefused, is_pirateface
from pirateface.seed.clients.qbittorrent import QBittorrent
from pirateface.seed.clients.transmission import Transmission

__all__ = [
    "HELP",
    "LoginRefused",
    "QBittorrent",
    "add",
    "is_pirateface",
    "qbittorrent_unreachable",
    "running",
    "update_trackers",
]


def qbittorrent_unreachable():
    """The one SeedError for a qBittorrent that did not answer (not running, remote access off). Raised by
    the two callers that add a torrent; a login refusal keeps its own message, which names the cause."""
    from pirateface.seed import user  # user imports this module

    return user.SeedError(f"qBittorrent is not reachable; configure its Web UI ({HELP})")


def running():
    """The clients that answer."""
    return [c for c in map(_connect, (Transmission, QBittorrent)) if c]


def _connect(client):
    """The client, or None if it isn't running. A client that refuses our login says so."""
    try:
        return client()
    except LoginRefused as e:
        print(f"    {e}")
        return None
    except Exception:  # not running, or remote access off
        return None


def add(data, save_dir, v1, prefer=None):
    """Add the torrent to the first client that answers, trying a running client of class prefer first.
    Returns what happened, or None if none could. A client that answered but refused names it and why."""
    errors = []
    for client in sorted(running(), key=lambda c: not (prefer and isinstance(c, prefer))):
        try:
            if client.has(v1):
                return f"Already in {client.name}."
            client.add(data, save_dir)
            return f"Added to {client.name} with the files in {save_dir}. It checks them, then seeds."
        except Exception as e:
            errors.append(f"{client.name} refused it: {e}")
    if errors:
        from pirateface.seed import user  # user imports this module

        raise user.SeedError("; ".join(errors))
    return None


def update_trackers(mine):
    clients = running()
    if not clients:
        raise RuntimeError("no torrent client answered. " + HELP)
    for client in clients:
        added, current, skipped, extra = client.update_trackers(mine)
        print(f"    {client.name}: added to {added}, already had it {current}, not Pirate Face {skipped}.{extra}")
