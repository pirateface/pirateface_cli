"""qBittorrent (Web UI) torrent client."""

import contextlib
import http.cookiejar
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from pirateface._http import request as _http_request
from pirateface.seed.clients.common import KEYED, LoginRefused, tracker_update_loop


class QBittorrent:
    name = "qBittorrent"

    def __init__(self):
        self.base = (os.environ.get("QBT_URL") or "http://localhost:8080").rstrip("/")
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.op.addheaders = [("Referer", self.base)]
        # No login needed when "Bypass authentication for clients on localhost" is on.
        if os.environ.get("QBT_USER"):
            self.call("auth/login", {"username": os.environ["QBT_USER"], "password": os.environ.get("QBT_PASS", "")})
        try:
            self.call("app/version")
        except urllib.error.HTTPError as e:
            # Whatever auth/login answers, a 403 on any API call means we are not authorized.
            if e.code == 403:
                raise LoginRefused("qBittorrent refused the login: check QBT_USER and QBT_PASS") from e
            raise

    def call(self, path, form=None, body=None, content_type=None, text=True):
        if form is not None:
            body = urllib.parse.urlencode(form).encode()
        req = _http_request(self.base + "/api/v2/" + path, data=body)
        if content_type:
            req.add_header("Content-Type", content_type)
        with self.op.open(req, timeout=30) as r:
            data = r.read()
        return data.decode() if text else data

    def torrents(self):
        return json.loads(self.call("torrents/info"))

    def trackers(self, h):
        return [x["url"] for x in json.loads(self.call("torrents/trackers?hash=" + h)) if "://" in x["url"]]

    def torrents_named(self, name):
        """(save folder, tracker URLs, file paths inside the torrent) of each torrent called name."""
        found = []
        for t in self.torrents():
            if t["name"] == name:
                files = json.loads(self.call("torrents/files?hash=" + t["hash"]))
                paths = [f["name"].removeprefix(name + "/") for f in files]
                found.append((t["save_path"], self.trackers(t["hash"]), paths))
        return found

    def export(self, h):
        """The .torrent file of a torrent in the client (qBittorrent 4.5 or later)."""
        return self.call("torrents/export?hash=" + h, text=False)

    def find(self, *infohashes):
        """The client's entry matching any infohash or torrent ID, or None."""
        # qBittorrent ids a hybrid or v2 torrent by its v2 hash cut to 40 digits; full hashes are in infohash_v1/_v2.
        ids = ("hash", "infohash_v1", "infohash_v2")
        wanted = {str(infohash).casefold() for infohash in infohashes}
        return next(
            (t for t in self.torrents() if wanted.intersection(str(t.get(k, "")).casefold() for k in ids)), None
        )

    def has(self, infohash):
        return self.find(infohash) is not None

    def all_infohashes(self):
        """Every infohash the client holds, casefolded (v1 and v2, and each torrent's display hash).

        One torrents/info call for the whole client, so `pf index --add` can skip what is already there
        without an HTTP round-trip per magnet."""
        found = set()
        for t in self.torrents():
            for key in ("hash", "infohash_v1", "infohash_v2"):
                if value := str(t.get(key, "")):
                    found.add(value.casefold())
        return found

    def add_magnets(self, urls):
        """Add magnet links with qBittorrent's default save path. qBittorrent fetches each one's metadata from
        the swarm, then downloads and seeds it; trackers named in each magnet's `tr` are announced to."""
        b = uuid.uuid4().hex
        body = (
            f'--{b}\r\nContent-Disposition: form-data; name="urls"\r\n\r\n{"\r\n".join(urls)}\r\n--{b}--\r\n'
        ).encode()
        answer = self.call("torrents/add", body=body, content_type=f"multipart/form-data; boundary={b}").strip()
        if answer == "Fails.":
            raise RuntimeError("qBittorrent did not take the magnets")

    def add(self, data, save_dir, skip_checking=False, paused=None, stopped=None):
        b = uuid.uuid4().hex
        fields = [f'--{b}\r\nContent-Disposition: form-data; name="savepath"\r\n\r\n{save_dir}\r\n']
        if paused is not None:
            value = "true" if paused else "false"
            fields.append(f'--{b}\r\nContent-Disposition: form-data; name="paused"\r\n\r\n{value}\r\n')
        if stopped is not None:
            value = "true" if stopped else "false"
            fields.append(f'--{b}\r\nContent-Disposition: form-data; name="stopped"\r\n\r\n{value}\r\n')
        if skip_checking:
            fields.append(f'--{b}\r\nContent-Disposition: form-data; name="skip_checking"\r\n\r\ntrue\r\n')
        fields.append(
            f'--{b}\r\nContent-Disposition: form-data; name="torrents"; filename="model.torrent"\r\n'
            "Content-Type: application/x-bittorrent\r\n\r\n"
        )
        body = "".join(fields).encode() + data + f"\r\n--{b}--\r\n".encode()
        answer = self.call("torrents/add", body=body, content_type=f"multipart/form-data; boundary={b}").strip()
        if answer == "Fails.":
            raise RuntimeError("qBittorrent did not take the torrent")
        try:
            return json.loads(answer).get("added_torrent_ids", [])
        except ValueError:
            return []

    def add_paused_and_recheck(self, data, save_dir, v1, timeout=1800):
        """Add the torrent stopped for rechecking; resume only a complete verified match.

        An incomplete or timed out add is removed without deleting staged links or source files.
        """
        ids = [v1]
        try:
            ids.extend(self.add(data, save_dir, paused=True, stopped=True))
            deadline = time.monotonic() + 30
            added = self.find(*ids)
            while not added and time.monotonic() < deadline:
                time.sleep(0.5)
                added = self.find(*ids)
            if not added:
                raise RuntimeError("qBittorrent did not report the added torrent")
            torrent_hash = added["hash"]
            self.call("torrents/recheck", {"hashes": torrent_hash})
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                current = self.find(*ids, torrent_hash)
                if not current:
                    raise RuntimeError("qBittorrent removed the torrent during its recheck")
                if "check" not in current.get("state", "").casefold():
                    complete = current.get("amount_left") == 0 and current.get("progress", 0) >= 1
                    if complete:
                        try:
                            self.call("torrents/start", {"hashes": torrent_hash})
                        except urllib.error.HTTPError as error:
                            if error.code != 404:
                                raise
                            self.call("torrents/resume", {"hashes": torrent_hash})
                        return "seeding"
                    self.remove(torrent_hash)
                    return "incomplete"
                time.sleep(2)
            self.remove(torrent_hash)
            return "incomplete"
        except Exception:
            with contextlib.suppress(Exception):
                current = self.find(*ids)
                if current:
                    self.remove(current["hash"])
            raise

    def remove(self, torrent_hash):
        """Remove a torrent but keep its files on disk."""
        self.call("torrents/delete", {"hashes": torrent_hash, "deleteFiles": "false"})

    def force_start(self, v2):
        """Seed a v2 torrent even when the queue is full (qBittorrent queues past 3 active uploads by default).
        Its id here is the v2 infohash cut to 40 digits; the full one is ignored."""
        self.call("torrents/setForceStart", {"hashes": v2[:40], "value": "true"})

    def ensure_announce_all(self):
        """Announce to every tracker in every tier. qBittorrent's default stops at the first tracker that
        answers, so a keyed tracker added after the site ones would never hear from it. This changes a
        qBittorrent setting for every torrent, so it asks first (-y answers yes). True if it changed."""
        from pirateface.seed import user  # user imports this module

        prefs = json.loads(self.call("app/preferences"))
        if not (prefs.get("announce_to_all_trackers") and prefs.get("announce_to_all_tiers")):
            print(
                "    qBittorrent announces to the first tracker that answers, so your seeding tracker would not\n"
                "    hear from it. pf can turn on Options > Advanced > Always announce to all trackers in a tier /\n"
                "    to all tiers. This applies to every torrent in qBittorrent."
            )
            if not user.YES and user.ask("    Type yes to change it: ") != "yes":
                raise user.SeedError("qBittorrent settings left unchanged")
            self.call(
                "app/setPreferences",
                {"json": json.dumps({"announce_to_all_trackers": True, "announce_to_all_tiers": True})},
            )
            return True
        return False

    def update_trackers(self, mine):
        extra = (
            " Turned on Options > Advanced > Always announce to all trackers in a tier / to all tiers."
            if self.ensure_announce_all()
            else ""
        )

        def mutate(t, urls):
            h = t["hash"]
            old = [u for u in urls if KEYED.match(u)]
            if old:
                self.call("torrents/removeTrackers", {"hash": h, "urls": "|".join(old)})
            self.call("torrents/addTrackers", {"hash": h, "urls": mine})

        added, current, skipped, changed = tracker_update_loop(
            self.torrents(), mine, lambda t: self.trackers(t["hash"]), mutate
        )
        if changed:
            self.call("torrents/reannounce", {"hashes": "|".join(t["hash"] for t in changed)})
        return added, current, skipped, extra
