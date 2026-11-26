"""pf dedupe: find Hugging Face cache blobs duplicated across repos.

Since huggingface_hub v1.32 (PR #4498), new downloads go through a cross-repo
shared blob store (`hub/blobs/<2-char>/<sha256>`). Older downloads (or plain-HTTP
fetches) keep per-repo `blobs/` directories keyed by etag. The same file can
then be stored twice. This module reports that duplication and, with --apply,
replaces each duplicated per-repo blob with a hardlink to the shared blob of the
same sha256, reclaiming the duplicate bytes. Snapshot symlinks keep working:
they point at per-repo blob paths, which stay valid as hardlinks.
"""

import contextlib
import hashlib
import os
import sys
from collections import defaultdict
from pathlib import Path

DEFAULT_CACHE = Path.home() / ".cache" / "huggingface" / "hub"


def _cache_root():
    raw = os.environ.get("HF_HUB_CACHE") or os.environ.get("HUGGINGFACE_HUB_CACHE")
    return Path(raw) if raw else DEFAULT_CACHE


def _sha256_file(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _shared_blobs(root):
    store = root / "blobs"
    if not store.is_dir():
        return {}
    found = {}
    for sidedir in store.iterdir():
        if not sidedir.is_dir():
            continue
        for blob in sidedir.iterdir():
            if blob.suffix != "" or len(blob.name) != 64:  # .lock/.refs sidecars, non-sha256 names
                continue
            with contextlib.suppress(OSError):
                found[blob.name] = blob.stat().st_size
    return found


def _per_repo_blobs(root):
    seen = []
    for repo in root.iterdir():
        if not repo.is_dir() or repo.name in ("blobs", ".locks"):
            continue
        blobs = repo / "blobs"
        if not blobs.is_dir():
            continue
        for blob in blobs.iterdir():
            if blob.is_symlink() or not blob.is_file():
                continue
            seen.append((repo.name, blob))
    return seen


def run(args):
    root = Path(args.cache) if args.cache else _cache_root()
    if not root.is_dir():
        print(f"error: Hugging Face cache not found at {root}", file=sys.stderr)
        return 1

    shared = _shared_blobs(root)
    if not shared:
        print("no shared blob store found (empty cache or a huggingface_hub older than v1.32); nothing to dedupe")
        return 0

    per_repo = _per_repo_blobs(root)
    dups = []  # (repo, blob path, size, sha256)
    errors = []
    by_digest = defaultdict(list)  # per-repo blobs sharing content
    for repo_name, blob in per_repo:
        try:
            digest = _sha256_file(blob)
            size = blob.stat().st_size
        except OSError as e:
            errors.append(f"{repo_name}: {e}")
            continue
        by_digest[digest].append((repo_name, blob, size))
        if digest in shared:
            dups.append((repo_name, blob, size, digest))

    intra = {d: files for d, files in by_digest.items() if len(files) > 1}
    intra_reclaim = sum(size for files in intra.values() for _, _, size in files[1:])
    reclaim = sum(size for _, _, size, _ in dups)
    print(f"cache: {root}")
    print(f"shared store: {len(shared)} blob(s), {sum(shared.values()):,} bytes")
    total = sum(b.stat().st_size for _, b in per_repo)
    print(f"per-repo blobs (pre-v1.32 layout): {len(per_repo)} files, {total:,} bytes")
    print(f"duplicates of shared blobs: {len(dups)} file(s), {reclaim:,} bytes reclaimable")
    for repo_name, blob, size, digest in dups[:10]:
        print(f"  {size:>12,} B  {repo_name}/{blob.name}  sha256={digest[:16]}…")
    if len(dups) > 10:
        print(f"  … {len(dups) - 10} more")
    if intra:
        print(f"same content in {len(intra)} per-repo group(s) (pre-v1.32 duplication, not shared-store overlap): ~{intra_reclaim:,} bytes reclaimable")
        for digest, files in list(intra.items())[:10]:
            total = sum(size for _, _, size in files)
            print(f"  {total:>12,} B  sha256={digest[:16]}…  in {len(files)} repo caches")
    if errors:
        print(f"unreadable: {len(errors)}")

    if not dups and not intra:
        return 0
    if not args.apply:
        print("dry run: pass --apply to hardlink duplicates onto the shared store (and the first copy in each intra-repo group)")
        return 0

    # Shared-store duplicates first, then intra-repo duplicates.
    def _same_file(a, b):
        try:
            sa, sb = a.stat(), b.stat()
            return sa.st_ino == sb.st_ino and sa.st_dev == sb.st_dev
        except OSError:
            return False

    def _link_to(target, blob):
        if _same_file(target, blob):
            return
        tmp = blob.with_name(blob.name + ".dedupe-tmp")
        try:
            with contextlib.suppress(OSError):
                tmp.unlink()
            os.link(target, tmp)
            tmp.replace(blob)
            if _same_file(target, blob):
                return
            # Rename onto an existing hardlink to the same inode can no-op on some
            # filesystems: fall back to unlink+link, which is atomic enough here.
            blob.unlink()
            os.link(target, blob)
        except OSError as e:
            print(f"skip {blob}: {e}", file=sys.stderr)
            with contextlib.suppress(OSError):
                tmp.unlink()

    for _repo_name, blob, _size, digest in dups:
        _link_to(root / "blobs" / digest[:2] / digest, blob)

    for files in intra.values():
        keep = files[0][1]
        for _repo_name, blob, _size in files[1:]:
            if blob == keep:
                continue
            _link_to(keep, blob)

    print(f"hardlinked {len(dups)} shared-store duplicate(s) and {sum(len(f) - 1 for f in intra.values())} intra-repo duplicate(s); ~{reclaim + intra_reclaim:,} bytes reclaimable")
    return 0


def register(subparsers):
    p = subparsers.add_parser(
        "dedupe",
        help="find duplicate Hugging Face cache blobs (shared store vs old per-repo blobs)",
        description="Reports per-repo cache blobs whose sha256 already exists in the cross-repo shared blob store, "
        "and with --apply hardlinks them onto the shared blobs. Dry run by default.",
    )
    p.add_argument("--apply", action="store_true", help="hardlink duplicates onto the shared store (default: dry run)")
    p.add_argument("--cache", help="Hugging Face cache root (default: $HF_HUB_CACHE or ~/.cache/huggingface/hub)")
    return p
