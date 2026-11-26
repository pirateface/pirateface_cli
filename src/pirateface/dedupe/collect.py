"""Blob collection and hashing: what the cache holds, and which blobs could be duplicates."""

import hashlib
import os
import re
from collections import defaultdict
from pathlib import Path

DEFAULT_CACHE = Path.home() / ".cache" / "huggingface" / "hub"

_BLOB_NAME = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _cache_root():
    raw = os.environ.get("HF_HUB_CACHE") or os.environ.get("HUGGINGFACE_HUB_CACHE")
    return Path(raw) if raw else DEFAULT_CACHE


def _sha256_file(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _is_regular_file(path):
    try:
        return path.lstat().st_mode & 0o170000 == 0o100000
    except OSError:
        return False


def _collect_blobs(root, errors):
    """Collect (path, size, dev, ino, kind, repo_name) for every real blob.

    Sidecars (.lock/.refs/.tmp), symlinks and non-regular files are ignored, as
    are names that do not look like a hash (shared store only).
    """
    entries = []
    store = root / "blobs"
    if store.is_dir():
        try:
            sidedirs = sorted(store.iterdir())
        except OSError as e:
            errors.append(f"shared store: {e}")
            sidedirs = []
        for sidedir in sidedirs:
            if not sidedir.is_dir():
                continue
            try:
                side_entries = sorted(sidedir.iterdir())
            except OSError as e:
                errors.append(f"shared store/{sidedir.name}: {e}")
                continue
            for blob in side_entries:
                if not _BLOB_NAME.fullmatch(blob.name) or not _is_regular_file(blob):
                    continue
                try:
                    st = blob.stat()
                except OSError as e:
                    errors.append(f"shared store/{sidedir.name}/{blob.name}: {e}")
                    continue
                entries.append((blob, st.st_size, st.st_dev, st.st_ino, "shared", None, st.st_nlink))
    try:
        repos = sorted(root.iterdir())
    except OSError as e:
        errors.append(f"cache root: {e}")
        repos = []
    for repo in repos:
        if not repo.is_dir() or repo.name in ("blobs", ".locks"):
            continue
        blobs = repo / "blobs"
        if not blobs.is_dir():
            continue
        try:
            repo_entries = sorted(blobs.iterdir())
        except OSError as e:
            errors.append(f"{repo.name}/blobs: {e}")
            continue
        for blob in repo_entries:
            # Symlinks (new-layout shared-store links) and anything with a
            # sidecar suffix (.incomplete/.metadata/.lock/.refs/.tmp) are not
            # cache blobs and are never touched.
            if blob.is_symlink() or "." in blob.name:
                continue
            if not _is_regular_file(blob):
                continue
            try:
                st = blob.stat()
            except OSError as e:
                errors.append(f"{repo.name}: {e}")
                continue
            entries.append((blob, st.st_size, st.st_dev, st.st_ino, "per_repo", repo.name, st.st_nlink))
    return entries


def _hash_candidates(entries):
    """Hash only entries whose size appears in >= 2 distinct inodes.

    Two blobs can only be duplicates of one another when their sizes match,
    so a size seen in a single inode can never match anything; hashing it
    would read hundreds of GB of unique-sized weights for nothing.
    """
    sizes = defaultdict(set)  # size -> set of (dev, ino)
    for _path, size, dev, ino, _kind, _repo, _nlink in entries:
        sizes[size].add((dev, ino))
    return [(p, s, d, i, k, r, n) for p, s, d, i, k, r, n in entries if len(sizes[s]) >= 2]


def _same_file(a, b):
    try:
        sa, sb = a.stat(), b.stat()
        return sa.st_ino == sb.st_ino and sa.st_dev == sb.st_dev
    except OSError:
        return False


def _summarize(entries):
    """Aggregate per-repo/shared totals over collected entries."""
    total_readable = sum(size for _p, size, _d, _i, kind, _r, _n in entries if kind == "per_repo")
    total_store_bytes = sum(size for _p, size, _d, _i, kind, _r, _n in entries if kind == "shared")
    store_file_count = sum(1 for _p, _s, _d, _i, kind, _r, _n in entries if kind == "shared")
    per_repo_count = sum(1 for _p, _s, _d, _i, kind, _r, _n in entries if kind == "per_repo")
    return total_readable, total_store_bytes, store_file_count, per_repo_count


def _hash_entries(entries, errors):
    """Hash pre-filtered entries; return (shared, by_digest).

    shared maps content sha256 -> (store path, size); by_digest maps content
    sha256 -> [(repo_name, path, size), ...] for per-repo blobs.
    """
    by_digest = defaultdict(list)
    shared = {}
    for path, size, _dev, _ino, kind, repo_name, nlink in _hash_candidates(entries):
        try:
            digest = _sha256_file(path)
        except OSError as e:
            errors.append(f"{repo_name or 'shared store'}: {e}")
            continue
        if kind == "shared":
            shared[digest] = (path, size, nlink)
        else:
            by_digest[digest].append((repo_name, path, size, nlink))
    return shared, by_digest


def _find_duplicates(by_digest, shared):
    """Find shared-store duplicates and cross-repo / per-repo duplicate groups.

    Returns (dups, per_repo_dups): dups is a list of (repo, blob, size, sha256, nlink);
    per_repo_dups maps digest -> [(repo, keep_blob, size, nlink), (repo, blob, size, nlink), ...]
    with the keep file first. Blobs already hardlinked to the keep file are excluded.
    """
    # Shared-store duplicates: per-repo blobs whose content is already in the store.
    # Blobs already hardlinked to the shared blob are not reclaimable.
    dups = []
    for digest, files in by_digest.items():
        target = shared.get(digest)
        if target is None:
            continue
        target_path, _target_size, _target_nlink = target
        for repo_name, blob, size, nlink in files:
            if _same_file(target_path, blob):
                continue
            dups.append((repo_name, blob, size, digest, nlink))

    # Cross-repo / per-repo duplicates: same content across per-repo blobs, not
    # already covered by the shared store. Skip blobs already hardlinked to the keep file.
    per_repo_dups = {}
    for digest, files in by_digest.items():
        if digest in shared or len(files) < 2:
            continue
        files = sorted(files, key=lambda f: (f[0], str(f[1])))
        keep_blob = files[0][1]
        dup_files = [(r, b, s, n) for r, b, s, n in files[1:] if not _same_file(keep_blob, b)]
        if dup_files:
            per_repo_dups[digest] = [files[0], *dup_files]
    return dups, per_repo_dups
