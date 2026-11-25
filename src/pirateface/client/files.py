"""A .torrent's file list: the paths it names, its size, and BEP 19 web seeds.

Pure listing logic shared by the download side (client/torrent.py) and the seeding side
(client/api.py, seed/upgrade.py, seed/from_cache.py, seed/per_file/files.py). A caller that has
decoded the info dict does not re-encode it.
"""

import os
import urllib.parse

import libtorrent as lt

from pirateface.listing import info_problems, unsafe_path

PAD_DIR = ".pad"  # pad bytes a creator wrote out by hand, without attr=p to say so


class Unavailable(Exception):
    pass


class Unsafe(Unavailable):
    """The torrent lists a path this client will not write: one that leaves the torrent's own folder, or a
    name this OS cannot store."""


class NotInTorrent(Unavailable):
    """The torrent's file list does not include this file: the swarm carries only part of the repo."""


def web_seed_url(torrent, path_in_torrent):
    """BEP 19: a base ending in "/" gets the file's path appended; otherwise it is the file itself."""
    seeds = torrent.get(b"url-list") or []
    seed = (seeds if isinstance(seeds, bytes) else seeds[0] if seeds else b"").decode()
    if not seed:
        return None
    return seed + urllib.parse.quote(path_in_torrent) if seed.endswith("/") else seed


def real_files(fs):
    """The indices of fs's files, BEP 47 pad files left out."""
    return [i for i in range(fs.num_files()) if not fs.file_flags(i) & fs.flag_pad_file]


def _relative(ti, i):
    """The entry's path inside the torrent's folder (libtorrent prefixes the torrent name, and on Windows
    returns backslashes). The single place a torrent path becomes a relative one."""
    fs = ti.files()
    path = fs.file_path(i).replace(os.sep, "/")
    return path.split("/", 1)[1] if fs.num_files() > 1 and "/" in path else path


def _file_index(ti, filename):
    fs = ti.files()
    for i in range(fs.num_files()):
        if _relative(ti, i) == filename:
            return i
    return None


def files_in(data):
    """(path, size, web seed URL or None) for each file in a .torrent, BEP 47 pad files left out.

    Refuses with Unsafe -- naming the offending path -- a listing this client cannot write as given. Every
    torrent path becomes a path on disk here, so a caller never has to check one again.

    `data` is a .torrent's bytes or its decoded info dict: a caller that already decoded the info (the
    download gate does, from the handle) needs no round trip. An info dict carries no BEP 19 web seed, so
    the third element is None.
    """
    if isinstance(data, dict):
        torrent, info = {}, data
    else:
        torrent = lt.bdecode(data)
        info = torrent[b"info"]
    try:
        name = info[b"name"].decode()
    except UnicodeDecodeError as e:
        raise Unsafe(f"the torrent's name is not UTF-8: {info[b'name'][:80]!r}") from e
    single = b"files" not in info
    if single:
        if b"length" not in info:
            raise Unsafe(f"{name}: the torrent has no file list this client can read")  # BEP 52 v2 only
        entries = [(name, info[b"length"])]
    else:
        entries = []
        for f in info[b"files"]:
            if b"p" in f.get(b"attr", b""):
                continue
            try:
                parts = [part.decode() for part in f[b"path"]]
            except UnicodeDecodeError as e:
                raise Unsafe(f"{name}: refusing {f[b'path']!r}: a path that is not UTF-8") from e
            if not parts:
                raise Unsafe(f"{name}: a file in the torrent has no path")
            if parts[0] == PAD_DIR:
                continue  # a pad folder written out by hand, without attr=p to say so
            entries.append(("/".join(parts), f[b"length"]))
    why = unsafe_path(name) or next(iter(info_problems(info, [p for p, _ in entries])), None)
    if why:
        raise Unsafe(f"{name}: refusing {why}")
    return [(p, size, web_seed_url(torrent, name if single else f"{name}/{p}")) for p, size in entries]
