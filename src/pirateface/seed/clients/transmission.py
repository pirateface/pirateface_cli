"""Transmission (RPC) torrent client."""

import base64
import json
import os
import urllib.error
import urllib.request

from pirateface._http import request as _http_request
from pirateface.seed.clients.common import KEYED, LoginRefused, tracker_update_loop


class Transmission:
    name = "Transmission"

    def __init__(self):
        host = os.environ.get("TR_HOST") or "localhost:9091"
        url = host if host.startswith("http") else f"http://{host}"
        self.url = url.rstrip("/") + ("" if url.endswith("/rpc") else "/transmission/rpc")
        self.headers = {"Content-Type": "application/json"}
        if os.environ.get("TR_AUTH"):
            self.headers["Authorization"] = "Basic " + base64.b64encode(os.environ["TR_AUTH"].encode()).decode()
        self.version = self.rpc("session-get", {"fields": ["rpc-version"]}).get("rpc-version", 0)

    def rpc(self, method, args):
        body = json.dumps({"method": method, "arguments": args}).encode()
        for _ in range(2):
            try:
                req = _http_request(self.url, headers=self.headers, data=body)
                with urllib.request.urlopen(req, timeout=30) as r:
                    out = json.load(r)
                if out.get("result") != "success":
                    raise RuntimeError(out.get("result"))
                return out["arguments"]
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    raise LoginRefused("Transmission refused the login: check TR_AUTH (user:pass)") from e
                if e.code != 409:
                    raise
                self.headers["X-Transmission-Session-Id"] = e.headers["X-Transmission-Session-Id"]
        raise RuntimeError("no Transmission session id")

    def has(self, v1):
        return bool(self.rpc("torrent-get", {"ids": [v1], "fields": ["id"]})["torrents"])

    def add(self, data, save_dir):
        self.rpc("torrent-add", {"metainfo": base64.b64encode(data).decode(), "download-dir": str(save_dir)})

    def update_trackers(self, mine):
        def mutate(t, _urls):
            trackers = t["trackers"]
            keep = [x for x in trackers if not KEYED.match(x["announce"])]  # a key replaced at /account
            if self.version >= 17:  # Transmission 4: trackerList, one tier per blank-line block
                tiers = {}
                for x in keep:
                    tiers.setdefault(x["tier"], []).append(x["announce"])
                lines = "\n\n".join("\n".join(v) for _, v in sorted(tiers.items()))
                self.rpc("torrent-set", {"ids": [t["id"]], "trackerList": lines + "\n\n" + mine})
            else:
                old = [x["id"] for x in trackers if KEYED.match(x["announce"])]
                if old:
                    self.rpc("torrent-set", {"ids": [t["id"]], "trackerRemove": old})
                self.rpc("torrent-set", {"ids": [t["id"]], "trackerAdd": [mine]})

        added, current, skipped, changed = tracker_update_loop(
            self.rpc("torrent-get", {"fields": ["id", "trackers"]})["torrents"],
            mine,
            lambda t: [x["announce"] for x in t["trackers"]],
            mutate,
        )
        if changed:
            self.rpc("torrent-reannounce", {"ids": [t["id"] for t in changed]})
        return added, current, skipped, ""

    def torrents_named(self, name):
        """(save folder, tracker URLs, file paths inside the torrent) of each torrent called name."""
        fields = ["name", "downloadDir", "trackers", "files"]
        return [
            (
                t["downloadDir"],
                [x["announce"] for x in t["trackers"]],
                [f["name"].removeprefix(name + "/") for f in t["files"]],
            )
            for t in self.rpc("torrent-get", {"fields": fields})["torrents"]
            if t["name"] == name
        ]
