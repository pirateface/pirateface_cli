"""The lease renewal thread for a work lane: it polls the site so a long stream never lets the lease lapse."""

import threading
import time
from dataclasses import dataclass

from pirateface.work import site
from pirateface.work.errors import LeaseLost, WorkError

RENEW_SECONDS = 10  # the rescue board reads this; the site drops a lease after 10 minutes without one
RENEW_UNREACHABLE_SECONDS = 300  # half the lease lapse: beyond this a lost lease can't be renewed


@dataclass(frozen=True)
class LeaseRunning:
    """The renewal thread is alive: the lease is held, and renewals are being attempted."""


@dataclass(frozen=True)
class LeaseFailed:
    """The renewal thread caught a WorkError and exited; the next note() re-raises it."""

    error: WorkError


LeaseSnapshot = LeaseRunning | LeaseFailed


@dataclass(frozen=True)
class GraceWindow:
    """The RENEW_UNREACHABLE_SECONDS grace window around an unanswered renewal. Opens on the first
    unanswered answer, is not moved by later ones, and closes on a 200."""

    since: float | None = None

    def unanswered(self, now: float) -> "GraceWindow":
        if self.since is None:
            return GraceWindow(now)
        return self

    def expired(self, now: float) -> bool:
        return self.since is not None and now - self.since > RENEW_UNREACHABLE_SECONDS

    def reset(self) -> "GraceWindow":
        return GraceWindow()


class Lease:
    """Renews the lease from a daemon thread every RENEW_SECONDS, so the stream's reads never block
    on the HTTPS call, and reports bytes so the rescue board can draw the lane. A lost lease stops the
    job: the thread records the error and the next note() raises it. A site that can't be reached is
    not a lost lease: renewals keep trying until the outage passes RENEW_UNREACHABLE_SECONDS."""

    def __init__(self, key, lease_id, total, name=None):
        self.key, self.id, self.total, self.done, self.name = key, lease_id, total, 0, name
        self._snapshot = LeaseRunning()
        self._grace = GraceWindow()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._renew_loop, daemon=True)

    @property
    def state(self) -> LeaseSnapshot:
        return self._snapshot

    def _renew_loop(self):
        while not self._stop.wait(RENEW_SECONDS):
            try:
                self.renew(strict=False)
            except WorkError as e:
                self._snapshot = LeaseFailed(e)
                return

    def renew(self, strict=True):
        """Renew the lease now: the thread on its timer, build once at each end of the job. An unanswered
        renewal (status 0) rides out RENEW_UNREACHABLE_SECONDS on either path, as it always has. Any other
        answer that is not 200 ends a direct call at once, so nothing streams or submits on a lease the
        site never confirmed; the timer, which asks non-strictly, also rides anything site.transient counts."""
        status, body = site.renew(self.key, self.id, self.done, self.total, self.name)
        if status == 200:
            self._grace = self._grace.reset()
            return
        # status 0 first, for both paths: a network error is not a lost lease and never was. `not strict`
        # then adds the answers only the timer may tolerate, so a direct call keeps its old contract.
        if status == 0 or (not strict and site.transient(status)):
            self._grace = self._grace.unanswered(time.monotonic())
            if self._grace.expired(time.monotonic()):
                minutes = RENEW_UNREACHABLE_SECONDS // 60
                if status == 0:
                    raise LeaseLost(f"the site has been unreachable for {minutes} minutes")
                raise LeaseLost(f"the lease could not be renewed for {minutes} minutes (last answer: {status})")
            return  # within the grace window: keep renewing on the timer
        raise LeaseLost(f"lease lost ({status}): {(body or {}).get('error', 'request failed')}")

    def note(self, n):
        self.done += n
        if isinstance(self._snapshot, LeaseFailed):
            raise self._snapshot.error

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2)
