"""What pf seed and pf submit ask of the user: the -y flag, prompts, the account key, and the errors they stop with."""

import getpass
import os
import re
import sys
from pathlib import Path

from pirateface.client import api
from pirateface.config import API, MAX_UPLOAD_BYTES, TORRENT_OVERSIZE_MESSAGE
from pirateface.seed import clients

# %APPDATA% on Windows, else $XDG_CONFIG_HOME or ~/.config (where package.sh kept it).
CONFIG = os.environ.get("APPDATA") if os.name == "nt" else os.environ.get("XDG_CONFIG_HOME")
KEY_FILE = Path(CONFIG or Path.home() / ".config") / "pirateface" / "key"
YES = False  # -y: never prompt. Set by command.run and restore.run.


class SeedError(Exception):
    pass


class RateLimited(SeedError):
    pass


class NeedsYou(SeedError):
    """-y and no torrent client: nothing was submitted; the user has to open the .torrent."""

    def __init__(self, repo, torrent_path, save_dir):
        super().__init__(f"no torrent client answered. {clients.HELP}")
        self.repo, self.torrent_path, self.save_dir = repo, torrent_path, save_dir


def ask(prompt, hidden=False):
    if YES:
        raise SeedError(f"needs an answer to {prompt.strip()!r}; rerun without -y")
    if not sys.stdin.isatty():
        raise SeedError("no terminal for prompts (agents: pass -y with the user's OK)")
    return (getpass.getpass if hidden else input)(prompt).strip()


def confirm(prompt):
    """The one y/N prompt: True when the user answers y. Under -y the prompt is not asked, so a seeded
    run with -y never blocks on one; it is also the single place the [y/N] idiom lives."""
    if YES:
        return True
    return ask(prompt).lower().startswith("y")


def load_key():
    """Account key from /settings, saved for next time."""
    key = read_key()
    if key:
        return key
    if YES:
        raise SeedError(f"set PIRATEFACE_KEY (from {API}/settings)")
    print(f"\n==> Your Pirate Face key: {API}/settings -> Community torrent key")
    key = ask("    Paste it (hidden): ", hidden=True)
    if not key.startswith("pfct_"):
        raise SeedError("that doesn't look like a Pirate Face key (starts with pfct_)")
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEY_FILE.touch(mode=0o600)
    KEY_FILE.write_text(key + "\n")
    print(f"    Saved to {KEY_FILE}")
    return key


def read_key():
    """The saved account key, or None. Never prompts."""
    return os.environ.get("PIRATEFACE_KEY") or (KEY_FILE.read_text().strip() if KEY_FILE.exists() else "") or None


SEEDING_TRACKER = re.compile(r"^https?://[^ ]+:6970/[^ ]+/announce$")


def seeding_tracker():
    """This account's keyed seeding tracker (the :6970 URL /api/trackers returns last), or None when
    there is no saved key or the site did not answer. Trackers are outside the info dict, so adding one
    never changes an infohash or re-downloads anything."""
    key = read_key()
    if not key:
        return None
    try:
        mine = api.trackers(key)[-1]
    except Exception:
        return None
    return mine if SEEDING_TRACKER.fullmatch(mine) else None


def seeding_tracker_or_fail():
    """seeding_tracker(), or the one SeedError that names where it comes from. The single place that turns a
    missing tracker into a fatal error, so pf seed --update-trackers and pf index --add cannot drift apart."""
    mine = seeding_tracker()
    if not mine:
        raise SeedError(f"could not get your seeding tracker from {API}/api/trackers (key rejected or site down)")
    return mine


def torrent_oversize(size):
    """Whether a .torrent of this many bytes is over the upload limit. The guard itself, shared by the
    fatal callers (work, submit) and the per-file skip in pf restore."""
    return size > MAX_UPLOAD_BYTES


def torrent_size_or_fail(data):
    """The one place that turns an oversized torrent into a fatal SeedError. pf work and pf submit both
    guard the torrent they are about to upload; this keeps the two in step."""
    if torrent_oversize(len(data)):
        raise SeedError(TORRENT_OVERSIZE_MESSAGE)
