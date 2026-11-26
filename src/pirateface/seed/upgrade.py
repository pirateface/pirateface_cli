"""pf seed --upgrade: move v1-only Pirate Face torrents to hybrid (v1 + v2) ones, whose files get per-file swarms.

With a repo (its uploader): rebuild your v1 torrent of it as a hybrid from the copy qBittorrent seeds, same name and
paths, and have Pirate Face list it in place of the old one. Then seed it at once from the same files, drop the old
torrent (files stay) and add the per-file swarms. With --mine (its uploader too): the same for every listed v1
torrent you submitted that has a complete copy in qBittorrent, in one command, reporting the ones it could not.
Without a repo (any seeder): switch every torrent in qBittorrent that Pirate Face replaced this way to its
replacement, so you keep seeding the swarm that is listed."""

import base64
import http.client
import time
from pathlib import Path

from pirateface import ui
from pirateface.client import api, torrent
from pirateface.config import API
from pirateface.seed import clients, hybrid, package, user
from pirateface.seed.clients.common import LoginRefused
from pirateface.seed.per_file.command import NEEDS_QBITTORRENT, qbittorrent, seed_model
from pirateface.seed.user import SeedError

# The nudge is a note, not a wait: a slow or down site/client must not hold up `pf seed`. Every call gets a
# tight per-call timeout and the whole scan a total wall-clock budget, reusing qbt.trackers()/api.torrent_status()
# per torrent exactly as the rest of this module does.
NUDGE_CALL_TIMEOUT = 5.0
NUDGE_TOTAL_BUDGET = 10.0


def _qbittorrent():
    qbt = qbittorrent()
    if not qbt:
        raise SeedError(NEEDS_QBITTORRENT)
    return qbt


def listed_v1(repo, infohash=None):
    """(.torrent bytes, name, v1 infohash) of the repo's listed v1-only torrent (or the one with that infohash, which
    the site redirects to its hybrid once upgraded), or None if it is already hybrid."""
    try:
        old = api.download(f"/api/torrents/{infohash}.torrent") if infohash else api.torrent_file(repo)
    except api.Unreachable as e:
        raise SeedError(f"could not reach {API}") from e
    if not old:
        raise SeedError(f"Pirate Face has no .torrent for {repo} you can use. See {API}/{repo}")
    name, v1, v2, single = package.info(old)
    if v2:
        print(f"==> {repo} is already hybrid. If you still seed its v1 torrent, switch with: pf seed --upgrade")
        return None
    if single:
        raise SeedError("this is a single-file torrent: its file already has one swarm")
    return old, name, v1


def complete_copy(qbt, repo, old, v1, name):
    """(qBittorrent entry, folder, files) of the torrent qBittorrent holds complete, every file there at its size."""
    entry = qbt.find(v1)
    if not entry or entry.get("progress") != 1:
        raise SeedError(f"qBittorrent must hold {repo}'s torrent complete. Run pf seed {repo}, let it finish, rerun")
    folder = Path(entry["save_path"], name)
    try:
        files = torrent.files_in(old)
    except torrent.Unsafe as e:
        raise SeedError(f"{repo}'s listed .torrent lists paths pf won't write: {e}") from e
    for path, size, _ in files:
        if not folder.joinpath(path).is_file() or folder.joinpath(path).stat().st_size != size:
            raise SeedError(f"{folder / path} is missing or has the wrong size")
    return entry, folder, files


def build_hybrid(name, folder, files, trackers):
    """(hybrid .torrent bytes, its v1 infohash, SHA-256 manifest) of files, same name and paths as the v1 torrent."""
    sums = {}  # each file's SHA-256, from the same read; must match the site's manifest
    sources = hybrid.from_folder(folder, [p.split("/") for p, _, _ in files])
    plen = hybrid.piece_length(sum(size for _, size, _ in files))
    data, new_v1, _ = hybrid.build_from(name, sources, trackers, plen, sums)
    return data, new_v1, [{"path": p, "sha256": h} for p, h in sorted(sums.items())]


def send_upgrade(key, v1, data, manifest):
    """Have Pirate Face list the hybrid in place of the v1 torrent."""
    body = {"from": v1, "torrent": base64.b64encode(data).decode(), "manifest": manifest}
    code, answer = api.upgrade(key, body)
    if code != 200:
        raise SeedError(f"Pirate Face did not take the upgrade (HTTP {code}): {answer.get('error') or answer}")


def rebuild(repo):
    key = user.load_key()
    if upgrade_one(_qbittorrent(), key, repo) is None:
        ui.layer_block([("model", "hybrid v1+v2")], "pf seed --upgrade")


def upgrade_one(qbt, key, repo, infohash=None, report=True):
    """Upgrade repo's listed v1 torrent (or the one with that infohash). Returns its per-file counts
    (seed_model's), or None when its .torrent is hybrid already; raises SeedError when it cannot be
    upgraded. report: seed_model prints its layer block; a caller summing several models prints one."""
    listed = listed_v1(repo, infohash)
    if not listed:
        return None
    old, name, v1 = listed
    entry, folder, files = complete_copy(qbt, repo, old, v1, name)
    print(f"==> Rebuilding {repo} as a hybrid torrent from {folder}")
    data, new_v1, manifest = build_hybrid(name, folder, files, api.trackers(key))
    send_upgrade(key, v1, data, manifest)
    print(f"    Listed in place of the v1 torrent: {API}/{repo}")
    if not qbt.has(new_v1):  # a rerun after the site took it
        qbt.add(data, entry["save_path"], skip_checking=True)  # every byte was just hashed
    qbt.remove(entry["hash"])
    print(f"    Seeding the hybrid ({new_v1}) from the same files; the v1 torrent is out of qBittorrent.")
    print("    Seeders of the old torrent get told to switch: pf seed --upgrade")
    return seed_model(data, entry["save_path"], new_v1, verified=True, label=repo, report=report)


# How many problems --upgrade --mine prints in full before collapsing the rest.
MAX_PROBLEMS = 20


def _upgrade_one_safe(qbt, key, row):
    """(result, None) or (None, problem line). One torrent's qBittorrent OSError or client request error
    must not stop the batch, so it is recorded as a problem and the loop moves on."""
    try:
        return upgrade_one(qbt, key, row["repo"], row["infohash"], report=False), None
    except Exception as e:  # one client error must not stop the rest of the batch
        return None, f"{row['repo']}: {e}"


def _report_mine(done, already, upgraded, problems):
    """The one layer block plus the capped problems list: the first MAX_PROBLEMS, then a count of the rest."""
    rows = [
        ("model", f"{done} upgraded, {already} already hybrid"),
        ("per-file", f"{sum(r['added'] for r in upgraded)} added, {sum(r['skipped'] for r in upgraded)} skipped"),
    ]
    if problems:
        rows.append(("could not", str(len(problems))))
    nxt = "pf seed --upgrade" if done == 0 and already and not problems else None
    ui.layer_block(rows, nxt)
    if not problems:
        return
    print("    Not upgraded:")
    for line in problems[:MAX_PROBLEMS]:
        print(f"      {line}")
    if len(problems) > MAX_PROBLEMS:
        print(f"      ... and {len(problems) - MAX_PROBLEMS} more")


def upgrade_mine():
    """Relist every v1 torrent this account submitted that has a complete copy in qBittorrent, in one
    command. It keeps going on the ones that fail and prints the first MAX_PROBLEMS as a list at the end.
    Safe to rerun: a torrent whose .torrent is hybrid already is skipped."""
    key = user.load_key()
    qbt = _qbittorrent()
    mine = api.my_torrents(key)
    if mine is None:
        raise SeedError(f"could not list your torrents from {API} (key rejected or site down)")
    if not mine:
        print("    You have no listed v1-only torrents to upgrade.")
        return
    print(f"==> Upgrading your {len(mine)} listed v1 torrents")
    results, problems = [], []  # results: one upgrade_one() answer per model it did not fail on
    for row in mine:
        result, problem = _upgrade_one_safe(qbt, key, row)
        if problem:
            problems.append(problem)
        else:
            results.append(result)
    upgraded = [r for r in results if r is not None]
    done, already = len(upgraded), len(results) - len(upgraded)
    _report_mine(done, already, upgraded, problems)


def replacement(qbt, t):
    """The site's record of a v1-only Pirate Face torrent in qBittorrent if it was replaced, else None."""
    if t.get("infohash_v2") or not clients.is_pirateface(qbt.trackers(t["hash"])):
        return None
    status = api.torrent_status(t.get("infohash_v1") or t["hash"])
    return status if status and status.get("status") == "superseded" and status.get("supersededBy") else None


def superseded_in_client(only_repo=None):
    """(entry, status) pairs of the v1-only Pirate Face torrents in qBittorrent that Pirate Face replaced,
    optionally only those for a repo. Best-effort: client or site failures yield an empty list; the whole
    scan is bounded by a total wall-clock budget so a slow or down site or client never hangs `pf seed`.
    The defensive parsing lives here, strictly in this helper: the shared trackers()/replacement() keep
    their exact behavior for their other callers."""
    try:
        qbt = _qbittorrent()
        deadline = time.monotonic() + NUDGE_TOTAL_BUDGET
        remaining = deadline - time.monotonic()
        torrents = qbt.torrents(timeout=min(NUDGE_CALL_TIMEOUT, remaining))
    except (SeedError, LoginRefused, OSError, http.client.HTTPException, ValueError, KeyError):
        return []
    if not isinstance(torrents, list):
        return []
    found = []
    for t in torrents:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if not isinstance(t, dict):
            continue
        try:
            hash_ = t["hash"]
            if t.get("infohash_v2"):
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            trackers = qbt.trackers(hash_, timeout=min(NUDGE_CALL_TIMEOUT, remaining))
            if not clients.is_pirateface(trackers):
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            status = api.torrent_status(t.get("infohash_v1") or hash_, timeout=min(NUDGE_CALL_TIMEOUT, remaining))
        except (SeedError, LoginRefused, KeyError, OSError, http.client.HTTPException, ValueError, TypeError):
            continue
        if not isinstance(status, dict):
            continue
        if status.get("status") != "superseded" or not status.get("supersededBy"):
            continue
        if only_repo is None or status.get("repo") == only_repo:
            found.append((t, status))
    return found


def nudge(repo=None):
    """Best-effort note, never fatal: the client holds superseded torrents and how to switch them."""
    entries = superseded_in_client(repo)
    if not entries:
        return
    repos = sorted({s.get("repo") for _, s in entries if isinstance(s.get("repo"), str) and s.get("repo")})
    names = ", ".join(repos) if repos else "your torrents"
    print(f"    {len(entries)} superseded torrent(s) in qBittorrent ({names}): switch with: pf seed --upgrade")


def switch(qbt, t, status, trackers):
    """Add the replacement on t's files (unless there already), then drop t. False if it can't be done safely."""
    new = status["supersededBy"]
    if not qbt.has(new):
        data = api.download(f"/api/torrents/{new}.torrent")
        if not data or package.info(data)[0] != t["name"]:  # another name would download everything again
            print(f"    Skipped {t['name']}: get its replacement at {API}/{status.get('repo')}")
            return False
        qbt.add(package.with_trackers(data, trackers), t["save_path"])
    qbt.remove(t["hash"])
    return True


def switch_all():
    """Every v1-only Pirate Face torrent in qBittorrent that Pirate Face replaced: add the replacement on the same
    files, then drop the old one. qBittorrent checks the files against the new torrent; nothing is downloaded."""
    print("==> Switching replaced Pirate Face torrents in qBittorrent")
    qbt = _qbittorrent()
    trackers = api.trackers(user.load_key())
    switched = skipped = 0
    for t in qbt.torrents():
        status = replacement(qbt, t)
        if status and switch(qbt, t, status, trackers):
            switched += 1
        elif status:
            skipped += 1
    nxt = "pf seed --update-per-file" if switched else None
    ui.layer_block([("model", f"{switched} switched, {skipped} skipped")], nxt)
