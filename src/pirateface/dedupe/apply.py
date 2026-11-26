"""Hardlinking duplicates onto their canonical copy: the --apply half of the dedupe pass."""

import contextlib
import os
import sys

from pirateface.dedupe.collect import _same_file


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
