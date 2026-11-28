"""pf seed --hf-cache: class every model in the Hugging Face cache as seed, submit or skip, then do it, asking per model."""

import enum
import sys
from dataclasses import dataclass
from pathlib import Path

from pirateface._folders import folder_repo
from pirateface._regex import EXTRA, SHARD, WEIGHT
from pirateface.client import api
from pirateface.config import API
from pirateface.seed import command, hub, submit, user

HOURLY_LIMIT = 200  # submissions per account per hour, enforced by the site


class PlanStatus(enum.Enum):
    """The one verdict plan_cache() draws per model: seed, submit, or skip."""

    SKIP = "skip"
    SEED = "seed"
    SUBMIT = "submit"


@dataclass(frozen=True)
class Plan:
    """One model's verdict: its status, and what the run needs to turn it into action."""

    status: PlanStatus
    repo: str
    rev: str
    snap: Path
    size: str
    why: str


def plan_cache():
    """One Plan per model in the Hugging Face cache: seed it, submit it, or skip it.
    The cache names large files by their SHA-256, so matching them against Hugging Face needs no hashing."""
    from huggingface_hub import constants

    cache = Path(constants.HF_HUB_CACHE)
    if not cache.is_dir():
        raise user.SeedError(f"no Hugging Face cache at {cache} (set HF_HUB_CACHE)")
    print(f"==> Checking models in {cache}")
    licenses = hub.licenses_or_fail()
    ids = hub.license_ids(licenses)
    for snap in sorted(cache.glob("models--*/snapshots/*")):
        repo = folder_repo(snap.parent.parent.name)
        rev = snap.name

        def row(status, why, size="-", repo=repo, rev=rev, snap=snap):
            return Plan(status, repo, rev, snap, size, why)

        try:
            _, card, remote = hub.model(repo, rev)
        except Exception:
            yield row(PlanStatus.SKIP, "not readable on Hugging Face (gated? set HF_TOKEN)")
            continue
        lic = hub.license_id(card, licenses)
        size = sum((snap / p).stat().st_size for p in remote if (snap / p).exists())
        size = f"{size / 1e9:.1f} GB" if size >= 1e8 else f"{size / 1e6:.0f} MB"
        if lic not in ids and not hub.approved_license(repo, licenses):
            yield row(PlanStatus.SKIP, f"license {lic}", size)
            continue
        # A GGUF repo holds many quants: one quant here is complete when every shard of it is.
        gguf = [p for p in remote if p.endswith(".gguf") and (snap / p).exists()]
        # Each shard once: every present shard of a quant names the same siblings.
        shards = {
            SHARD.sub(f"-{i:05d}-of-{m[1]}.gguf", p)
            for p in gguf
            if (m := SHARD.search(p))
            for i in range(1, int(m[1]) + 1)
        }
        missing = sum(not (snap / shard).exists() for shard in shards)
        bad = 0
        for p, (want, is_lfs) in remote.items():
            local = snap / p
            if not local.exists():
                # Weights must be here (other GGUF quants need not be); a skipped README is fine.
                missing += is_lfs and not (gguf and p.endswith(".gguf"))
                continue
            # The snapshot link names the blob by its hash; huggingface_hub 2.x links that blob on to a
            # shared store under another name, so follow one level only.
            blob = local.readlink() if local.is_symlink() else local.resolve()
            bad += blob.name != want
        need = hub.required_file(lic, licenses)
        if bad:
            yield row(PlanStatus.SKIP, f"{bad} files differ from Hugging Face", size)
            continue
        if missing:
            yield row(PlanStatus.SKIP, f"partial download: {missing} weight files missing", size)
            continue
        if need and not (snap / need).exists():
            yield row(PlanStatus.SKIP, f"{lic} copies must include {need}; download it too", size)
            continue
        answer = api.lookup_soft(repo)
        if answer is None:
            yield row(PlanStatus.SKIP, "could not reach Pirate Face", size)
        elif answer.license_required:
            yield row(PlanStatus.SKIP, f"on Pirate Face; accept the license terms at {API}/{repo}", size)
        elif answer.not_listed:
            yield row(PlanStatus.SUBMIT, "complete, not on Pirate Face yet", size)
        elif any(
            remote[p][0] not in {f["sha256"] for q in answer.entry.get("quants") or [answer.entry] for f in q["files"]}
            for p in remote
            if WEIGHT.search(p) and not EXTRA.search(p) and (snap / p).exists()
        ):
            yield row(PlanStatus.SUBMIT, "a quant Pirate Face doesn't list yet", size)
        elif answer.entry.get("revision") != rev:
            yield row(PlanStatus.SKIP, "on Pirate Face at another revision", size)
        elif not answer.entry.get("torrent"):
            yield row(PlanStatus.SKIP, f"on Pirate Face; accept the license terms at {API}/{repo}", size)
        else:
            yield row(PlanStatus.SEED, "on Pirate Face; seed it", size)


def seed_cache(per_file=False):
    """Seed or submit every complete model in the Hugging Face cache, asking per model."""
    plan = []
    for r in plan_cache():
        print(f"    {r.status.value:<7} {r.repo:<48} {r.size:>9}  {r.why}")
        plan.append(r)
    todo = [r for r in plan if r.status in (PlanStatus.SEED, PlanStatus.SUBMIT)]
    if not todo:
        print("\n    Nothing to seed or submit.")
        return
    key = user.load_key()
    needs, failed, later = [], [], []
    tries = 0
    for r in todo:
        status = r.status
        repo, snap, rev, size = r.repo, r.snap, r.rev, r.size
        if status is PlanStatus.SUBMIT and tries >= HOURLY_LIMIT:
            later.append(repo)
            continue
        print()
        verb = "Seed" if status is PlanStatus.SEED else "Share"
        if not user.YES and not user.ask(f"==> {verb} {repo} ({size})? [y/N] ").lower().startswith("y"):
            continue
        try:
            if status is PlanStatus.SEED:
                command.seed_listed(repo, snap, key, per_file)
            else:
                tries += 1
                submit.submit_new(repo, snap, rev, key, per_file=per_file)
        except user.RateLimited:
            later.append(repo)
            tries = HOURLY_LIMIT
        except user.NeedsYou as e:
            needs.append(e)
        except Exception as e:
            print(f"error: {e}\n    {repo} skipped; continuing", file=sys.stderr)
            failed.append(repo)
    if needs:
        print("\n==> Needs you: no torrent client could be reached for these. Nothing was submitted.")
        print("    Open each .torrent in your torrent client and save to the folder shown. Then run")
        print("    this again without -y and press Enter when each one says Seeding.")
        for e in needs:
            print(f"    {e.repo}\n        open:    {e.torrent_path}\n        save to: {e.save_dir}")
    if failed:
        print(f"\n==> Not done (see the error above each one): {' '.join(failed)}")
    if later:
        print(
            f"\n==> Next hour: Pirate Face takes {HOURLY_LIMIT} submissions an hour. Run this again then for: {' '.join(later)}"
        )
