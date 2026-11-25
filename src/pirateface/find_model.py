"""Client-side sha256 -> model lookup: what listed model does this local file belong to?

There is no reverse-hash endpoint on the site, so this walks the repos in the Hugging Face cache, asks the
site what each listing holds, and matches the hash of the file the user named. It answers nothing about a
file that is not in the cache or that the site does not list -- the prototype's honest limit, documented in
the PR.
"""

import hashlib
import sys
from pathlib import Path

from pirateface._regex import REPO_STRICT
from pirateface.client import api

MIN_FILE_SIZE = 1 << 20  # 1 MiB: tiny configs and tokenizers are too easy to mis-match by size alone


def hash_file(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _cache_repos():
    from huggingface_hub import constants

    cache = Path(constants.HF_HUB_CACHE)
    if not cache.is_dir():
        return Path(constants.HF_HUB_CACHE), []
    repos = []
    for repo_dir in sorted(cache.glob("models--*")):
        repo = repo_dir.name.removeprefix("models--").replace("--", "/", 1)
        if REPO_STRICT.fullmatch(repo):
            repos.append(repo)
    return cache, repos


def find(path, out=sys.stdout):
    """Print every listed model whose sha256 the file matches. Exit code: 0 if any found, 1 otherwise."""
    path = Path(path)
    if not path.is_file():
        print(f"error: {path} is not a file", file=sys.stderr)
        return 1
    if path.stat().st_size < MIN_FILE_SIZE:
        print(f"error: {path} is under {MIN_FILE_SIZE // (1 << 20)} MiB; too easy to mis-match", file=sys.stderr)
        return 1
    digest = hash_file(path)
    print(f"hash: {digest}", file=sys.stderr)

    cache, repos = _cache_repos()
    if not cache.is_dir():
        print(f"note: no Hugging Face cache at {cache}; checking nothing", file=sys.stderr)
    if not repos:
        print("note: no models in the Hugging Face cache to check", file=sys.stderr)

    matches = []
    failures = []
    for repo in repos:
        try:
            found = api.listed(repo)
        except Exception as e:
            failures.append((repo, e))
            continue
        if not found:
            continue
        for p, (size, sha, _url) in found["files"].items():
            if sha == digest:
                matches.append((repo, p, found["commit"], size))
                break

    if not matches:
        print("no listed model holds this file", file=sys.stderr)
        if failures:
            print(f"({len(failures)} lookups failed, e.g. {failures[0][0]}: {failures[0][1]})", file=sys.stderr)
        return 1
    for repo, p, commit, size in matches:
        print(f"{repo}  {p}  ({size / 1e9:.2f} GB, {commit[:12]})", file=out)
    return 0
