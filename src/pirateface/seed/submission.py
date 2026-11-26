"""What a Pirate Face submission sends: the terms version, the prompts that fill the body, and the body itself.

Split out of submit.py (behavior-preserving). Importable on its own; submit.py re-exports the same names.
"""

import base64
import contextlib
import json

from pirateface.config import API, MAX_UPLOAD_BYTES
from pirateface.seed import package, user
from pirateface.seed.user import SeedError

TERMS_VERSION = "2026-09-18.2"


class Answers:
    """Prompts during a submission. A dry run never asks: it records what a real run would ask for."""

    def __init__(self, dry_run):
        self.dry_run, self.skipped = dry_run, []

    def ask(self, prompt, what):
        if self.dry_run:
            self.skipped.append(what)
            print(f"    (dry run: a real run asks for the {what} here)")
            return None
        return user.ask(prompt)


def evidence_for(repo, revision, lic, match, direct, command, answers):
    if match:
        return (
            f"Source: https://huggingface.co/{repo} at revision {revision}. License per model card: {lic}. "
            f"{match} Hugging Face LFS files at that revision are in this torrent, and their SHA-256 hashes match "
            f"the local copy (checked by {command})."
        )
    if direct:
        return f"Direct listing: submitted directly to Pirate Face (not on Hugging Face). License: {lic}."
    print("    Where did this copy come from? Links, license proof, and why it's gone from Hugging Face if it is.")
    evidence = answers.ask("    Evidence: ", "evidence")
    if evidence is not None and len(evidence) < 30:
        raise SeedError("evidence needs at least 30 characters")
    return evidence


def request_body(repo, revision, lic, evidence, sums, data, v1, v2, readme, web_seed):
    """The POST /api/community-torrents body."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise SeedError("the .torrent is over the 3 MB upload limit")
    body = {
        "repoId": repo,
        "revision": revision,
        "magnet": package.magnet(v1, v2),  # the site adds the trackers
        "license": lic,
        "evidence": evidence,
        "manifest": [{"path": p, "sha256": h} for p, h in sorted(sums.items())],
        "torrent": base64.b64encode(data).decode(),  # lets the magnet work with no peer online
        "submissionTermsVersion": TERMS_VERSION,
        "redistributionConfirmed": True,
        "accuracyConfirmed": True,
        "reviewAcknowledged": True,
        "privacyAcknowledged": True,
        "termsAccepted": True,
    }
    # README.md is the model card. Pirate Face shows it for models that aren't on Hugging Face.
    if readme.is_file() and readme.stat().st_size <= 200_000:
        with contextlib.suppress(UnicodeDecodeError):
            body["readme"] = readme.read_bytes().decode("utf-8")  # as is: the site checks it against the manifest
    if web_seed:
        body["webSeed"] = web_seed
    return body


def print_dry_run(body, data, v1, v2, torrent_path, skipped):
    shown = dict(body, torrent=f"<{len(data)} bytes, base64>")
    if "readme" in shown:
        shown["readme"] = f"<README.md, {len(body['readme'])} characters>"
    print("\n==> Dry run: nothing added to a torrent client, nothing submitted")
    print(f"    v1 infohash  {v1}")
    print(f"    v2 infohash  {v2}")
    print(f"    torrent      {torrent_path}")
    print(f"    POST {API}/api/community-torrents")
    print(json.dumps(shown, indent=2))
    if skipped:
        print(f"    A real run asks for: {', '.join(skipped)}")
