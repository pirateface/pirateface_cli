"""pf verify: hash local files (file or directory, recursive) and report who published them.

Two modes: live against POST {base}/api/trust/by-sha256 (the Pirate Face trust API; base configurable via
PF_API_BASE, falling back to the site's own PF_API config), or --index with a trust-index.jsonl.gz (fully
offline, zero network calls). Output is Level-1 origin reporting: who published the file (HF, ModelScope,
Ollama, lab), never a safety claim. Site down is reported, never crashed on.
"""

import gzip
import http.client
import json
import os
import re
import sys
import zlib
from pathlib import Path

from pirateface._hash import sha256_file
from pirateface._http import NO_REDIRECTS, request_json
from pirateface.config import API, MIN_FILE_BYTES

SOURCE_LABELS = {"hf": "HF", "modelscope": "ModelScope", "ollama": "Ollama", "lab": "Lab"}
SOURCE_ORDER = ("hf", "modelscope", "ollama", "lab")
MAX_HASHES_PER_CALL = 200  # the site's documented cap
_SHA256 = re.compile(r"[0-9a-fA-F]{64}")


def _base():
    return (os.environ.get("PF_API_BASE") or API).rstrip("/")


def _collect(paths, all_files):
    """Files under each path, sorted. Tiny files found via directory recursion are noise unless all_files;
    explicitly named files are always included. Returns (files, errors)."""
    entries = []  # (path, explicit)
    errors = 0
    for raw in paths:
        path = Path(raw)
        try:
            if path.is_file():
                entries.append((path, True))
            elif path.is_dir():
                entries.extend((f, False) for f in path.rglob("*") if f.is_file())
            else:
                print(f"error: {path} is not a file or directory", file=sys.stderr)
                errors += 1
        except OSError as e:
            print(f"error: {path}: {e}", file=sys.stderr)
            errors += 1
    seen = {}
    for path, explicit in entries:
        # explicit paths win over recursion duplicates
        seen[path] = explicit or seen.get(path, False)
    files = sorted(seen, key=lambda f: str(f))
    if not all_files:
        kept, skipped = [], 0
        for f in files:
            if seen[f]:
                kept.append(f)
                continue
            try:
                size = f.stat().st_size
            except OSError:
                kept.append(f)
                continue
            if size < MIN_FILE_BYTES:
                skipped += 1
                continue
            kept.append(f)
        if skipped:
            print(
                f"skipped {skipped} file(s) under {MIN_FILE_BYTES // (1 << 20)} MiB (--all to include)", file=sys.stderr
            )
        files = kept
    return files, errors


def _load_index(path):
    """{sha256: [row, ...]} from a trust-index.jsonl.gz; unreadable lines are skipped with a note."""
    rows = {}
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError as e:
                print(f"warning: {path} line {lineno}: {e}", file=sys.stderr)
                continue
            if not isinstance(obj, dict):
                continue
            digest = obj.get("sha256")
            if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                continue
            rows.setdefault(digest.lower(), []).append(obj)
    return rows


def _lookup_live(base, hashes):
    """{sha256: [rows]} for hashes from the site, tolerating omitted keys and empty results."""
    out = {}
    for i in range(0, len(hashes), MAX_HASHES_PER_CALL):
        chunk = hashes[i : i + MAX_HASHES_PER_CALL]
        status, body = request_json(
            f"{base}/api/trust/by-sha256",
            30,
            headers={"Content-Type": "application/json"},
            policy=NO_REDIRECTS,
            method="POST",
            data=json.dumps({"hashes": chunk}).encode(),
        )
        if status != 200:
            raise ValueError(f"the site answered HTTP {status}")
        if not isinstance(body, dict):
            raise TypeError("the site's answer was not a JSON object")
        results = body.get("results")
        if results is None:
            results = {}
        if not isinstance(results, dict):
            raise TypeError("the site's answer had a malformed results map")
        for digest in chunk:
            rows = results.get(digest) or results.get(digest.lower()) or []
            if not isinstance(rows, list):
                raise TypeError(f"the site's answer was malformed for {digest}")
            out[digest] = [r for r in rows if isinstance(r, dict)]
    return out


def _origin_label(rows):
    """One origin line per (source, repo_or_tag): e.g. 'HF Qwen/Qwen3-8B + ModelScope Qwen/Qwen3-8B'."""
    seen = set()
    labeled = []
    for source in SOURCE_ORDER:
        for row in rows:
            if str(row.get("source") or "").lower() != source:
                continue
            repo = row.get("repo_or_tag")
            if not isinstance(repo, str) or not repo:
                continue
            if (source, repo) not in seen:
                seen.add((source, repo))
                labeled.append(f"{SOURCE_LABELS.get(source, source)} {repo}")
    return " + ".join(labeled) if labeled else "No origin match"


def register(subparsers):
    p = subparsers.add_parser(
        "verify",
        help="report where local files came from (sha256, Level-1 origin)",
        description="Hashes each file (file or directory, recursive) to sha256 and reports who published it: "
        "HF, ModelScope, Ollama or lab. Default: the site's trust API. --index: fully offline, no network.",
    )
    p.add_argument("paths", nargs="+", help="file(s) and folder(s) (folders searched recursively)")
    p.add_argument("--index", metavar="PATH", help="offline: a trust-index.jsonl.gz (no network calls)")
    p.add_argument("--json", action="store_true", help="print JSON instead of one line per file")
    p.add_argument("--all", action="store_true", help="include files under 1 MiB (default: skipped)")
    return p


def run(args):
    files, collect_errors = _collect(args.paths, args.all)
    if not files:
        return 1
    hashed, errors = [], collect_errors
    for path in files:
        try:
            digest = sha256_file(path)
            size = path.stat().st_size
        except OSError as e:
            print(f"error: {path} could not be read ({e})", file=sys.stderr)
            errors += 1
            continue
        hashed.append((path, digest, size))
    if not hashed:
        return 1
    hashes = sorted({d for _, d, _ in hashed})

    origins = {}  # sha256 -> [rows]
    if args.index:
        try:
            loaded = _load_index(args.index)
        except (OSError, EOFError, zlib.error, ValueError) as e:
            print(f"error: could not read {args.index} ({e})", file=sys.stderr)
            return 1
        for digest in hashes:
            origins[digest] = loaded.get(digest, [])
    else:
        try:
            origins = _lookup_live(_base(), hashes)
        except (
            OSError,
            http.client.HTTPException,
            ValueError,
            TypeError,
        ) as e:  # site down, refused, unreadable: report, never crash
            print(
                f"error: could not reach the trust API ({e}). Use --index with a trust-index.jsonl.gz for offline.",
                file=sys.stderr,
            )
            return 1

    findings = []
    for path, digest, size in hashed:
        rows = origins.get(digest, [])
        findings.append(
            {
                "path": str(path),
                "sha256": digest,
                "size_bytes": size,
                "origins": rows,
                "origin_label": _origin_label(rows),
            }
        )

    if args.json:
        print(json.dumps(findings, indent=2))
    else:
        for f in findings:
            print(f"{f['path']}  {f['origin_label']}")
    unverified = {f["path"] for f in findings if not f["origins"]}
    if not unverified and not errors:
        return 0
    return 1
