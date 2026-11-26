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
import hashlib
import os
import re
import sys
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


def _shared_blobs(root, errors):
    """Map content sha256 -> (store path, size) for the shared blob store.

    Store entries are keyed by Xet hash, not by content sha256, so the only
    reliable match is hashing the entry's content. Sidecars (.lock/.refs/.tmp),
    symlinks and other non-regular files are ignored, as are names that do not
    look like a hash.
    """
    store = root / "blobs"
    if not store.is_dir():
        return {}
    found = {}
    try:
        sidedirs = sorted(store.iterdir())
    except OSError as e:
        errors.append(f"shared store: {e}")
        return {}
    for sidedir in sidedirs:
        if not sidedir.is_dir():
            continue
        try:
            entries = sorted(sidedir.iterdir())
        except OSError as e:
            errors.append(f"shared store/{sidedir.name}: {e}")
            continue
        for blob in entries:
            if not _BLOB_NAME.fullmatch(blob.name) or not _is_regular_file(blob):
                continue
            try:
                size = blob.stat().st_size
                digest = _sha256_file(blob)
            except OSError as e:
                errors.append(f"shared store/{sidedir.name}/{blob.name}: {e}")
                continue
            found[digest] = (blob, size)
    return found


def _per_repo_blobs(root, errors):
    seen = []
    try:
        repos = sorted(root.iterdir())
    except OSError as e:
        errors.append(f"cache root: {e}")
        return seen
    for repo in repos:
        if not repo.is_dir() or repo.name in ("blobs", ".locks"):
            continue
        blobs = repo / "blobs"
        if not blobs.is_dir():
            continue
        try:
            entries = sorted(blobs.iterdir())
        except OSError as e:
            errors.append(f"{repo.name}/blobs: {e}")
            continue
        for blob in entries:
            # Symlinks (new-layout shared-store links) and anything with a
            # sidecar suffix (.incomplete/.metadata/.lock/.refs/.tmp) are not
            # cache blobs and are never touched.
            if blob.is_symlink() or "." in blob.name:
                continue
            if not _is_regular_file(blob):
                continue
            seen.append((repo.name, blob))
    return seen


def _same_file(a, b):
    try:
        sa, sb = a.stat(), b.stat()
        return sa.st_ino == sb.st_ino and sa.st_dev == sb.st_dev
    except OSError:
        return False


def run(args):
    root = Path(args.cache) if args.cache else _cache_root()
    if not root.is_dir():
        print(f"error: Hugging Face cache not found at {root}", file=sys.stderr)
        return 1

    errors = []
    shared = _shared_blobs(root, errors)
    per_repo = _per_repo_blobs(root, errors)
    by_digest = defaultdict(list)  # per-repo blobs sharing content
    total_readable = 0
    for repo_name, blob in per_repo:
        try:
            digest = _sha256_file(blob)
            size = blob.stat().st_size
        except OSError as e:
            errors.append(f"{repo_name}: {e}")
            continue
        total_readable += size
        by_digest[digest].append((repo_name, blob, size))

    # Shared-store duplicates: per-repo blobs whose content is already in the store.
    # Blobs already hardlinked to the shared blob are not reclaimable.
    dups = []  # (repo, blob path, size, sha256)
    for digest, files in by_digest.items():
        target = shared.get(digest)
        if target is None:
            continue
        target_path, _target_size = target
        for repo_name, blob, size in files:
            if _same_file(target_path, blob):
                continue
            dups.append((repo_name, blob, size, digest))

    # Intra-repo duplicates: content shared across repo caches, not already
    # covered by the shared store. Skip blobs already hardlinked to the keep file.
    intra = {}
    for digest, files in by_digest.items():
        if digest in shared or len(files) < 2:
            continue
        files = sorted(files, key=lambda f: (f[0], str(f[1])))
        keep_blob = files[0][1]
        dup_files = [(r, b, s) for r, b, s in files[1:] if not _same_file(keep_blob, b)]
        if dup_files:
            intra[digest] = [files[0], *dup_files]

    intra_reclaim = sum(size for files in intra.values() for _, _, size in files[1:])
    reclaim = sum(size for _, _, size, _ in dups)

    print(f"cache: {root}")
    if shared:
        print(f"shared store: {len(shared)} blob(s), {sum(size for _, size in shared.values()):,} bytes")
    else:
        print("no shared blob store found (empty cache or a huggingface_hub older than v1.32)")
    print(f"per-repo blobs (pre-v1.32 layout): {len(per_repo)} files, {total_readable:,} bytes")
    print(f"duplicates of shared blobs: {len(dups)} file(s), {reclaim:,} bytes reclaimable")
    for repo_name, blob, size, digest in dups[:10]:
        print(f"  {size:>12,} B  {repo_name}/{blob.name}  sha256={digest[:16]}…")
    if len(dups) > 10:
        print(f"  … {len(dups) - 10} more")
    if intra:
        print(
            f"same content in {len(intra)} per-repo group(s) (pre-v1.32 duplication, not shared-store overlap): ~{intra_reclaim:,} bytes reclaimable"
        )
        for digest, files in list(intra.items())[:10]:
            total = sum(size for _, _, size in files)
            print(f"  {total:>12,} B  sha256={digest[:16]}…  in {len(files)} repo caches")
    if errors:
        print(f"unreadable: {len(errors)}")
        for e in errors[:10]:
            print(f"  {e}")

    if not dups and not intra:
        return 0
    if not args.apply:
        print(
            "dry run: pass --apply to hardlink duplicates onto the shared store (and the first copy in each intra-repo group)"
        )
        return 0

    applied_dups = 0
    applied_intra = 0

    # Shared-store duplicates first, then intra-repo duplicates.
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

    for _repo_name, blob, _size, digest in dups:
        target_path, _ = shared[digest]
        applied_dups += _link_to(target_path, blob)

    for files in intra.values():
        keep = files[0][1]
        for _repo_name, blob, _size in files[1:]:
            if blob == keep:
                continue
            applied_intra += _link_to(keep, blob)

    print(
        f"hardlinked {applied_dups}/{len(dups)} shared-store duplicate(s) and {applied_intra}/{sum(len(f) - 1 for f in intra.values())} intra-repo duplicate(s); ~{reclaim + intra_reclaim:,} bytes reported reclaimable"
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
