"""The libtorrent alert pump and the disk-flush/resume-data waits.

One process-wide pump thread is the only reader of libtorrent's alerts; the waiters pair a call with the
alert it proves. Split from torrent.py so the alert stream stays apart from the handle bookkeeping."""

import sys
import threading

import libtorrent as lt

# libtorrent reports pieces done before they're written, so files can read as zeros until flushed.
# cache_flushed_alert doesn't say which flush it answers, so only one runs at a time.
_flush = threading.Condition()
_flush_one_at_a_time = threading.Lock()
_flush_done = {}

_resume = {}  # infohash -> bencoded resume data captured from the alert pump
_resume_cv = threading.Condition()

_NOISY = ("PIECE", "HAVE", "REQUEST", "picker", "ALLOWED_FAST", "dht", "portmap", "stats", "block")


def start_pump(session, debug):
    threading.Thread(target=_pump, args=(session, debug), daemon=True).start()


def _pump(session, debug):
    """The only reader of libtorrent's alerts."""
    while True:
        session.wait_for_alert(1000)
        for a in session.pop_alerts():
            if isinstance(a, lt.cache_flushed_alert):
                with _flush:
                    key = str(a.handle.info_hashes().v1)
                    _flush_done[key] = _flush_done.get(key, 0) + 1
                    _flush.notify_all()
            if isinstance(a, lt.save_resume_data_alert):
                with _resume_cv:
                    _resume[str(a.handle.info_hashes().v1)] = lt.bencode(a.resume_data)
                    _resume_cv.notify_all()
            if debug:
                msg = a.message()
                if not any(k in msg or k in type(a).__name__ for k in _NOISY):
                    print(f"[lt] {msg[:220]}", file=sys.stderr, flush=True)


def flush_to_disk(h, timeout=30):
    key = str(h.info_hashes().v1)
    with _flush_one_at_a_time, _flush:
        before = _flush_done.get(key, 0)
        h.flush_cache()
        if not _flush.wait_for(lambda: _flush_done.get(key, 0) > before, timeout):
            raise TimeoutError("libtorrent didn't confirm the file was written to disk")


def resume_data(h, timeout=10):
    """Bencoded resume data, so a re-added handle trusts what is already on disk instead of re-hashing it."""
    key = str(h.info_hashes().v1)
    with _resume_cv:
        _resume.pop(key, None)
        h.save_resume_data()
        if not _resume_cv.wait_for(lambda: key in _resume, timeout):
            return None
        return _resume.pop(key)
