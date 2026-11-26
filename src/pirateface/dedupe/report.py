"""Human-readable report of what a scan found: pure printing, no I/O beyond stdout."""


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
