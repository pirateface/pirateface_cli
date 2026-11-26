"""Ask Pirate Face to rescue a model it doesn't have yet: one background POST per repo, joined at exit."""

import atexit
import contextlib
import json
import threading

from pirateface._http import NO_REDIRECTS
from pirateface._http import open_request as _http_open_request
from pirateface.config import API, flag

_rescues = []


def _send_rescue(repo_id):
    with contextlib.suppress(Exception):
        _http_open_request(
            f"{API}/api/rescue",
            10,
            method="POST",
            headers={"Content-Type": "application/json"},
            data=json.dumps({"repoId": repo_id}).encode(),
            policy=NO_REDIRECTS,
        ).close()


def request_rescue(repo_id):
    """Ask Pirate Face to add a model it doesn't have yet, in the background."""
    if flag("PF_NO_RESCUE"):
        return False
    t = threading.Thread(target=_send_rescue, args=(repo_id,), daemon=True)
    t.start()
    _rescues.append(t)
    return True


@atexit.register
def _wait_for_rescues():
    for t in _rescues:
        t.join(timeout=2)
