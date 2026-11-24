"""Model folder -> links in the seed folder; a .torrent's info, trackers and magnet."""

import contextlib
import os
import re
import urllib.parse
from pathlib import Path

import libtorrent as lt

from pirateface.config import SEEDS
from pirateface.listing import listing_problems, unsafe_path

# Hidden paths (.git, .cache, .env), keys and partial downloads stay out of the torrent.
SKIP = re.compile(r"(^|/)(\.|__pycache__/|id_rsa)|\.(pem|key|incomplete|lock|part)$")

HF_HUB = ("models--", "datasets--", "spaces--")


def _is_junction(p):
    """Windows directory junction or mount point, true on Windows. Other OSes have no junction concept."""
    return getattr(os.path, "isjunction", None) is not None and os.path.isjunction(p)


def _allowed_link(target, src):
    """True when a link inside src may be read through: its target stays inside src, or is a Hugging Face
    hub cache blob (<cache>/models--<author>--<name>/blobs/<hash>) of the repo src sits under
    (<cache>/models--<author>--<name>/snapshots/<revision>). Anything else is refused."""
    if target.is_relative_to(src):
        return True
    if not target.exists() and not target.is_symlink():
        return False
    parent = target.parent
    if parent.name != "blobs":
        return False
    model_dir = parent.parent
    if not model_dir.name.startswith(HF_HUB):
        return False
    if not (model_dir / "snapshots").is_dir():
        return False
    try:
        src.relative_to(model_dir / "snapshots")
    except ValueError:
        return False
    return True


def seeds_folder(repo, v1):
    """The seed folder for a repo: author--model, as the Hugging Face cache names it, so two authors can
    publish models of the same name. An old <model> folder that already holds this same torrent keeps its
    place (its .torrent has this v1 infohash): nothing is renamed or moved, so a client seeding it is fine."""
    old, new = Path(repo.split("/")[1]), Path(repo.replace("/", "--"))
    for tor in Path(SEEDS, old).glob("*.torrent"):
        with contextlib.suppress(Exception):  # unreadable or not a torrent: not this one
            if str(lt.torrent_info(tor.read_bytes()).info_hashes().v1) == v1:
                return old
    return new


class LinkError(Exception):
    pass


def link(target, dest):
    """Symlink dest to target; a hard link where symlinks need rights (Windows without Developer Mode).
    A real file already at dest is kept: it is the file itself, or one the torrent client downloaded."""
    if dest.exists() and not dest.is_symlink():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        dest.symlink_to(target)
    except FileExistsError:
        # dest already holds an entry (a symlink, or a real file). Not a rights problem.
        raise LinkError(f"{dest} already exists; remove it and rerun") from None
    except OSError:
        try:
            dest.hardlink_to(target)
        except OSError as e:
            raise LinkError(
                f"could not link {dest.name} ({e}). On Windows, turn on Developer Mode "
                "(Settings > System > For developers), or use an --out folder on the same drive as the model"
            ) from e


def check_listing(data, paths):
    """Raise LinkError if a listed torrent (paths: torrent.files_in) has a path this OS won't write or a symlink
    entry. Seeding links these paths into the seed folder and the torrent client writes them, so this runs before
    either. The checks themselves are in pirateface/listing.py, where the download path asks the same ones."""
    problems = listing_problems(data, paths)
    if problems:
        raise LinkError("Pirate Face's .torrent lists paths pf won't write:\n    " + "\n    ".join(problems[:20]))


def unportable(paths):
    """Why these torrent paths ("/"-separated) can't be downloaded as-is everywhere, or [].
    Names that differ only in case (file or folder) would overwrite each other on Windows and macOS,
    and names Windows can't store fail there. Both are refused, not renamed: renaming changes the files."""
    problems, seen = [], {}
    for rel in paths:
        if why := unsafe_path(rel):
            problems.append(why)
        parts = rel.split("/")
        for i in range(len(parts)):
            prefix = "/".join(parts[: i + 1])
            if seen.setdefault(prefix.casefold(), prefix) != prefix:
                problems.append(f"{prefix} and {seen[prefix.casefold()]}: names differ only in case")
    return list(dict.fromkeys(problems))


def clear_links(dest):
    """Remove the links an earlier run left under dest, and the folders that empties. Real files stay:
    dest can hold the user's files, or files their torrent client downloaded. A symlinked dir, junction
    or other link to a folder outside dest is never walked and never removed: it is left alone."""
    if not dest.is_dir() or dest.is_symlink() or _is_junction(dest):
        return
    dirs_seen = []
    for d, dirs, names in os.walk(dest, topdown=True, followlinks=False):
        dirs[:] = [x for x in dirs if not (Path(d, x).is_symlink() or _is_junction(Path(d, x)))]
        for n in names:
            p = Path(d, n)
            if p.is_symlink() or _is_junction(p):
                p.unlink()
        if Path(d) != dest:
            dirs_seen.append(Path(d))
    for d in reversed(dirs_seen):
        with contextlib.suppress(OSError):
            d.rmdir()


def stage(src, dest):
    """Link the model files in src into dest. Returns (included paths, skipped paths)."""
    clear_links(dest)
    src_root = Path(src).resolve()
    included, skipped, seen = [], [], set()
    for d, dirs, names in os.walk(src, topdown=True, followlinks=True):
        # Links are followed (the HF cache is links), but a folder reached twice, as through a link back up
        # the tree, is listed once: a loop would otherwise pack the model into the torrent many times.
        real = Path(d).resolve()
        if real in seen:
            dirs.clear()
            continue
        seen.add(real)
        kept = []
        for n in dirs:
            p = Path(d, n)
            if p.is_symlink() or _is_junction(p):
                if _allowed_link(p.resolve(), src_root):
                    kept.append(n)
                    continue
                dir_rel = p.relative_to(src).as_posix()
                if SKIP.search(dir_rel + "/"):
                    continue  # a skipped tree: left out, not refused
                raise LinkError(f"{p} links outside the model folder: {p.resolve()}")
            kept.append(n)
        dirs[:] = kept
        for n in names:
            full = Path(d, n)
            rel = full.relative_to(src).as_posix()
            if SKIP.search(rel):
                skipped.append(rel)
                continue  # a skipped file: left out, not refused
            if (full.is_symlink() or _is_junction(full)) and not _allowed_link(full.resolve(), src_root):
                raise LinkError(f"{full} links outside the model folder: {full.resolve()}")
            included.append(rel)
    included.sort()
    problems = unportable(included)
    if problems:
        raise LinkError("rename or remove these files first:\n    " + "\n    ".join(problems[:20]))
    if Path(src).resolve() != Path(dest).resolve():
        # src is dest: the files are already where the torrent is built from; linking would be self-links.
        for rel in included:
            link(Path(src, rel).absolute(), dest / rel)
    return included, sorted(skipped)


def link_existing(src, dest, paths):
    """Link the files of a listed torrent that src already has. Returns how many."""
    if Path(src).resolve() == Path(dest).resolve():
        # src is dest: the files (or the links to them) are already where the client seeds from.
        return sum(1 for p in paths if Path(src, p).is_file())
    clear_links(dest)
    found = [p for p in paths if Path(src, p).is_file()]
    for p in found:
        link(Path(src, p).absolute(), dest / p)
    return len(found)


def with_trackers(data, trackers):
    """The same torrent (same infohash) announcing to these trackers."""
    t = lt.bdecode(data)
    t[b"announce"] = trackers[0].encode()
    t[b"announce-list"] = [[url.encode()] for url in trackers]
    return lt.bencode(t)


def info(data):
    """(name, v1 infohash, v2 infohash or None, single file?)"""
    ti = lt.torrent_info(data)
    v2 = ti.info_hashes().v2
    return ti.name(), str(ti.info_hashes().v1), None if v2.is_all_zeros() else str(v2), ti.num_files() == 1


def magnet(v1, v2, name=None, trackers=()):
    q = urllib.parse.quote
    m = f"magnet:?xt=urn:btih:{v1}" + (f"&xt=urn:btmh:1220{v2}" if v2 else "")
    if name:
        m += "&dn=" + q(name, safe="")
    return m + "".join("&tr=" + q(t, safe="") for t in trackers)
