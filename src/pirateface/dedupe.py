"""pf dedupe: find Hugging Face cache blobs duplicated across repos.

Since huggingface_hub v1.32 (PR #4498), new downloads go through a cache-wide
shared blob store (`hub/blobs/<2-char>/<xet-hash>`), keyed by Xet hash — not by
the sha256 of the content. Older downloads (or plain-HTTP fetches) keep per-repo
`blobs/` directories keyed by etag, which for LFS files is the sha256 of the
content. The same file can then be stored twice. This module reports that
duplication and, with --apply, replaces each duplicated per-repo blob with a
hardlink to the shared blob of the same content, reclaiming the duplicate bytes.
Snapshot symlinks keep working: they point at per-repo blob paths, which stay
valid as hardlinks.
"""

import contextlib
import os
import sys
from collections import defaultdict
from pathlib import Path

from pirateface._hash import sha256_file
from pirateface._regex import DIGEST

DEFAULT_CACHE = Path.home() / ".cache" / "huggingface" / "hub"


def _cache_root():
    raw = os.environ.get("HF_HUB_CACHE") or os.environ.get("HUGGINGFACE_HUB_CACHE")
    return Path(raw) if raw else DEFAULT_CACHE


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
                if not DIGEST.fullmatch(blob.name) or not _is_regular_file(blob):
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
            digest = sha256_file(path)
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


def _print_report(
    root,
    store_file_count,
    total_store_bytes,
    per_repo_count,
    total_readable,
    dups,
    per_repo_dups,
    reclaim,
    per_repo_reclaim,
    pinned_dup_files,
    pinned_dup_bytes,
    pinned_per_repo_files,
    pinned_per_repo_bytes,
    errors,
):
    print(f"cache: {root}")
    if store_file_count:
        print(f"shared store: {store_file_count} blob(s), {total_store_bytes:,} bytes")
    else:
        print("no shared blob store found (empty cache or a huggingface_hub older than v1.32)")
    print(f"per-repo blobs (pre-v1.32 layout): {per_repo_count} files, {total_readable:,} bytes")
    print(f"duplicates of shared blobs: {len(dups)} file(s), {reclaim:,} bytes reclaimable")
    for repo_name, blob, size, digest, nlink in dups[:10]:
        pin = "  (pinned)" if nlink > 1 else ""
        print(f"  {size:>12,} B  {repo_name}/{blob.name}  sha256={digest[:16]}…{pin}")
    if len(dups) > 10:
        print(f"  … {len(dups) - 10} more")
    if pinned_dup_files:
        print(
            f"  pinned by other hardlinks: {pinned_dup_files} file(s), {pinned_dup_bytes:,} bytes — left as-is, 0 bytes reclaimable"
        )
    if per_repo_dups:
        print(
            f"cross-repo / per-repo duplicates in {len(per_repo_dups)} group(s) (not shared-store overlap): ~{per_repo_reclaim:,} bytes reclaimable"
        )
        for digest, files in list(per_repo_dups.items())[:10]:
            reclaimable = sum(size for _, _, size, nlink in files[1:] if nlink <= 1)
            print(f"  {reclaimable:>12,} B  sha256={digest[:16]}…  in {len(files)} per-repo blob(s)")
        if pinned_per_repo_files:
            print(
                f"  pinned by other hardlinks: {pinned_per_repo_files} file(s), {pinned_per_repo_bytes:,} bytes — left as-is, 0 bytes reclaimable"
            )
    if errors:
        print(f"unreadable: {len(errors)}")
        for e in errors[:10]:
            print(f"  {e}")


def _link_to(target_path, blob):
    if _same_file(target_path, blob):
        return True
    try:
        before = blob.stat()
    except OSError as e:
        print(f"skip {blob}: {e}", file=sys.stderr)
        return False
    tmp = blob.with_name(blob.name + ".dedupe-tmp")
    with contextlib.suppress(OSError):
        tmp.unlink()
    try:
        os.link(target_path, tmp)
    except OSError as e:
        print(f"skip {blob}: {e}", file=sys.stderr)
        return False
    try:
        after = blob.stat()
    except OSError as e:
        print(f"skip {blob}: {e}", file=sys.stderr)
        with contextlib.suppress(OSError):
            tmp.unlink()
        return False
    # The file changed between our check and the link: keep the original.
    if after.st_ino != before.st_ino or after.st_size != before.st_size or after.st_mtime_ns != before.st_mtime_ns:
        print(f"skip {blob}: file changed during dedupe", file=sys.stderr)
        with contextlib.suppress(OSError):
            tmp.unlink()
        return False
    try:
        tmp.replace(blob)
    except OSError as e:
        print(f"skip {blob}: {e}", file=sys.stderr)
        with contextlib.suppress(OSError):
            tmp.unlink()
        return False
    if not _same_file(target_path, blob):
        print(f"skip {blob}: replacement did not take effect", file=sys.stderr)
        return False
    return True


def _apply_dedupe(dups, per_repo_dups, shared, reclaim, per_repo_reclaim, pinned_dup_files, pinned_per_repo_files):
    applied_dups = 0
    applied_per_repo = 0

    # Shared-store duplicates first, then cross-repo / per-repo duplicates.
    # Pinning blobs (another hardlink keeps the old inode alive) reclaim 0 bytes: leave them.
    unpinned_dups = [d for d in dups if d[4] <= 1]
    for _repo_name, blob, _size, digest, _nlink in unpinned_dups:
        target_path, _, _ = shared[digest]
        applied_dups += _link_to(target_path, blob)

    unpinned_groups = []
    for digest, files in per_repo_dups.items():
        keep = files[0][1]
        dup_files = [(r, b, s, n) for r, b, s, n in files[1:] if n <= 1]
        unpinned_groups.append((digest, files[0], dup_files))
        for _repo_name, blob, _size, _nlink in dup_files:
            applied_per_repo += _link_to(keep, blob)

    total_unpinned_per_repo = sum(len(dups_) for _d, _k, dups_ in unpinned_groups)
    print(
        f"hardlinked {applied_dups}/{len(unpinned_dups)} shared-store duplicate(s) and {applied_per_repo}/{total_unpinned_per_repo} cross-repo / per-repo duplicate(s); ~{reclaim + per_repo_reclaim:,} bytes reported reclaimable"
    )
    if pinned_dup_files or pinned_per_repo_files:
        print(
            f"left {pinned_dup_files + pinned_per_repo_files} pinned file(s) as-is (other hardlinks keep their inodes alive)"
        )
    return applied_dups, applied_per_repo


def run(args):
    root = Path(args.cache) if args.cache else _cache_root()
    if not root.is_dir():
        print(f"error: Hugging Face cache not found at {root}", file=sys.stderr)
        return 1

    errors = []
    entries = _collect_blobs(root, errors)
    total_readable, total_store_bytes, store_file_count, per_repo_count = _summarize(entries)
    shared, by_digest = _hash_entries(entries, errors)
    dups, per_repo_dups = _find_duplicates(by_digest, shared)

    # Blobs with st_nlink > 1 are pinned by another hardlink (e.g. SEEDS/): the
    # inode survives the replacement, so the reclaim is honestly 0. Report them,
    # leave them alone, and never count them as reclaimable.
    pinned_dups = [d for d in dups if d[4] > 1]
    reclaim = sum(size for _r, _b, size, _dg, _nl in dups if _nl <= 1)
    pinned_per_repo = [
        (digest, blob, size)
        for digest, files in per_repo_dups.items()
        for _r, blob, size, nlink in files[1:]
        if nlink > 1
    ]
    per_repo_reclaim = sum(size for files in per_repo_dups.values() for _r, _b, size, nlink in files[1:] if nlink <= 1)
    pinned_dup_files = len(pinned_dups)
    pinned_dup_bytes = sum(size for _r, _b, size, _dg, _nl in pinned_dups)
    pinned_per_repo_files = len(pinned_per_repo)
    pinned_per_repo_bytes = sum(size for _dg, _b, size in pinned_per_repo)

    _print_report(
        root,
        store_file_count,
        total_store_bytes,
        per_repo_count,
        total_readable,
        dups,
        per_repo_dups,
        reclaim,
        per_repo_reclaim,
        pinned_dup_files,
        pinned_dup_bytes,
        pinned_per_repo_files,
        pinned_per_repo_bytes,
        errors,
    )
    print("tip: pf seed --from-cache seeds cached models straight from the cache with 0 extra disk")

    if not dups and not per_repo_dups:
        return 0
    if not args.apply:
        print(
            "dry run: pass --apply to hardlink duplicates onto the shared store (and the first copy in each cross-repo / per-repo group)"
        )
        return 0

    _apply_dedupe(
        dups,
        per_repo_dups,
        shared,
        reclaim,
        per_repo_reclaim,
        pinned_dup_files,
        pinned_per_repo_files,
    )
    return 0


def register(subparsers):
    p = subparsers.add_parser(
        "dedupe",
        help="find duplicate Hugging Face cache blobs (shared store vs old per-repo blobs)",
        description="Reports per-repo cache blobs whose content is already in the cross-repo shared blob store, "
        "and with --apply hardlinks them onto the shared blobs. Dry run by default.",
    )
    p.add_argument("--apply", action="store_true", help="hardlink duplicates onto the shared store (default: dry run)")
    p.add_argument("--cache", help="Hugging Face cache root (default: $HF_HUB_CACHE or ~/.cache/huggingface/hub)")
    return p
