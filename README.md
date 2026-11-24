# Pirate Face CLI

Downloads HF models over BitTorrent from [Pirate Face](https://pirateface.co) seeders. Your code stays the same.

## Install

```sh
pip install pirateface
```

Or without installing: `uv run -p 3.13 --with pirateface python your_script.py`. Needs Python 3.12 or 3.13. With `uvx`, pass `-p 3.13`.

Anything that downloads through HF (transformers, diffusers, `hf download`) now pulls from the swarm. Files are checked against Hugging Face's SHA-256 before they are trusted; offline, with the cached index, the same bytes are checked against the torrent's own piece hashes. If a model isn't on Pirate Face or a peer stalls, that file comes from Hugging Face as usual. Set `PF_HF_FIRST=1` to reverse the order: Hugging Face first, swarm only when it fails.

## Commands

```sh
pf download org/model      # download into the Hugging Face cache (--local-dir DIR for a folder)
pf seed org/model [FOLDER] # seed a model, submitting it first if Pirate Face doesn't list it
pf submit --repo org/model FOLDER  # package and submit a folder (--dry-run to preview)
pf work                    # build torrents for queued model requests, streamed from Hugging Face
pf restore PATH            # upload .torrent files to fix a listing whose magnet stalls
pf index [--sync]          # the cached magnet index: what it holds, and mirror the newest build
```

`pf <command> --help` covers every flag. `pf seed` stores each model in `~/pirateface-seeds/<author>--<model>/` so same-named models from different authors do not collide, and leaves existing files in place.

`pf work --max-size 100` takes only models up to 100 GiB; `pf work --once` takes one job and exits; `--max-download-rate 10` caps the Hugging Face stream at 10 MB/s. If interrupted, it saves the lease and file metadata privately under the Pirate Face config directory and resumes the job on the next run while the lease is active, restarting at the file that was cut off. Bytes are never saved to disk. Use a distinct `--name` per concurrent worker.

`pf seed` variants: `--hf-cache` (every complete model in your HF cache, asking per model), `--from-cache` (seed listed torrents matching HF/Ollama cache files), `--update-trackers`, `--update-per-file`, `--no-per-file`, `--upgrade` (relist a v1-only torrent as hybrid; `--upgrade --mine` does it for every v1 torrent you submitted with a complete qBittorrent copy).

`pf restore` takes `.torrent` files or a client folder, such as qBittorrent's `BT_backup` or Transmission's `torrents/`.

`pf submit --direct --repo your-org/model FOLDER` publishes your own model that isn't on Hugging Face.

`pf seed`, `submit`, `work` and `restore` ask once for your key from [pirateface.co/settings](https://pirateface.co/settings). `pf seed --from-cache` asks only after you choose a match.

## When Pirate Face is down

The site's magnet index lists every model's magnet as a weekly torrent, mirrored into `$PF_CACHE/index/shards/`. When Pirate Face cannot be reached, a listed model in that copy still downloads: checked against the Hugging Face fingerprint first, and when Hugging Face cannot answer, against the torrent's own piece hashes via the infohash the shard commits to. A model not listed, a torrent that carries only some files, an unreadable removals list, or a download with no fingerprint falls back to Hugging Face, with the reason printed.

```sh
pf index          # the build this copy holds, how many models, whether the site has a newer one
pf index --sync   # mirror the build the site names, replacing copies the build has changed
pf index --add    # add every magnet in the index to qBittorrent, your seeding tracker on new ones
```

## Settings

| Variable | |
|---|---|
| `PIRATEFACE_KEY` | Account key (otherwise asked once and saved) |
| `QBT_URL`, `QBT_USER`, `QBT_PASS` | qBittorrent Web UI (default `http://localhost:8080`) |
| `OLLAMA_MODELS` | Ollama model store root (default: the detected Ollama model directory) |
| `TR_HOST`, `TR_AUTH` | Transmission RPC (default `localhost:9091`), `user:pass` |
| `HF_TOKEN` | For gated repos |
| `PF_DISABLE` | Set to `1` to turn the download hook off |
| `PF_HF_FIRST` | `1` = download from Hugging Face first, swarm only when it fails |
| `PF_NO_BLOCK_PROOFS` | `1` = `pf work`, `submit` and `seed` submit without block proofs (default: they prove random 16 KiB blocks against the torrent's v2 hashes) |
| `PF_LISTEN` | libtorrent listen interfaces (default `0.0.0.0:0,[::]:0`) |

Downloads use libtorrent's defaults: listen on `0.0.0.0:0,[::]:0` (change via `PF_LISTEN`), UPnP and NAT-PMP port mapping, DHT and LSD peer discovery, and uncapped upload while downloading.

## Development

```sh
uv run --group dev ruff check
```

## Contributing

- One change per PR, small enough to read in one sitting.
- Please don't add tests for now. We're still settling how we test, so check your change by hand and say how in the PR.
- `uv run --group dev ruff check` and `ruff format --check` should pass with nothing new.
- When you replace code, delete the old code. No disabled flags or leftover branches.
- Fix problems where they start rather than adding special cases around them.
- If Pirate Face can't be reached, say so and carry on. Never crash, and never write outside the model folder.
- Add your Pirate Face username to the PR so we can credit you.
