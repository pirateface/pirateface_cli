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

import sys
from pathlib import Path

from pirateface.dedupe.apply import _apply_dedupe
from pirateface.dedupe.collect import (
    _cache_root,
    _collect_blobs,
    _find_duplicates,
    _hash_entries,
    _summarize,
)
from pirateface.dedupe.report import _print_report


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
