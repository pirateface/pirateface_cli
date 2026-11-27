import atexit
import contextlib
import enum
import shutil
import threading
import time
from pathlib import Path

import libtorrent as lt

from pirateface import config, ui
from pirateface._atomic import write_bytes
from pirateface._http import user_agent as _user_agent
from pirateface.client import alerts, api, rescue
from pirateface.client.files import (
    NotInTorrent,
    Unavailable,
    Unsafe,
    _file_index,
    _relative,
    files_in,
    real_files,
)

CACHE_DIR = Path(config.CACHE_DIR)
_lock = threading.Lock()
_session = None
_handles = {}  # repo_id -> its state once asked: a torrent handle, RepoState.NO_SWARM, or the Unsafe that refused it
_wanted = {}
_active = {}  # repo_id -> fetch_file calls currently downloading
_setup = {}  # repo_id -> lock serialising handle setup
_idle_cv = threading.Condition(_lock)  # signalled when a repo's in-flight fetch count drops


class RepoState(enum.Enum):
    """A repo's _handles state other than its torrent handle: no usable swarm answer."""

    NO_SWARM = "not listed, no usable answer, or no file list in time: Hugging Face serves it"


def _get_session():
    global _session
    if _session is None:
        debug = config.flag("PF_DEBUG")
        _session = lt.session(
            {
                "listen_interfaces": config.LISTEN,
                "user_agent": _user_agent(),
                "alert_mask": lt.alert.category_t.all_categories if debug else lt.alert.category_t.storage_notification,
                # The torrent goes "finished" between files and drops the web seed; reconnect in 2s, not 30s.
                # Keep close_redundant_connections on: idle web seeds otherwise never request the next file.
                "urlseed_wait_retry": 2,
                # The tracker hands out a 60s interval to peers that are moving data; libtorrent's default floor is 300s.
                "min_announce_interval": 60,
            }
        )
        alerts.start_pump(_session, debug)
    return _session


@atexit.register
def _cleanup():
    if _session is None:
        return
    # Never block interpreter exit behind a fetch thread holding _lock: give up on the snapshot
    # and let the OS reclaim files.
    if not _lock.acquire(timeout=1):
        return
    try:
        handles = [h for h in _handles.values() if isinstance(h, lt.torrent_handle)]
    finally:
        _lock.release()
    for h in handles:
        with contextlib.suppress(Exception):
            path = Path(h.status().save_path) / h.torrent_file().name()
            _session.remove_torrent(h)
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)


def _setup_lock(repo_id):
    """Serialises handle creation/re-add per repo so parallel fetches share one handle."""
    with _lock:
        return _setup.setdefault(repo_id, threading.Lock())


def _remember(repo_id, state):
    """One map says what happened to a repo, so nothing has to be kept in step with it."""
    with _lock:
        _handles[repo_id] = state


def _forget(repo_id, handle):
    """Forget a repo: remove its handle (best effort) and drop its state."""
    with _lock:
        with contextlib.suppress(Exception):
            _get_session().remove_torrent(handle)
        _handles.pop(repo_id, None)


def _lookup_magnet(repo_id):
    """The repo's magnet, or None to fall back on Hugging Face. NO_SWARM is remembered so the site is
    asked once per process; an unreachable site raises instead, so it is retried next time."""
    try:
        answer = api.lookup(repo_id)
    except api.Unreachable as e:
        raise Unavailable("Pirate Face unreachable") from e
    if answer.license_required:
        raise Unavailable(f"accept the license terms at {config.API}/{repo_id}")
    magnet = answer.entry.get("magnet") if answer.found else None
    if not magnet:
        ui.not_on_pirateface(repo_id, answer.not_listed and rescue.request_rescue(repo_id))
        _remember(repo_id, RepoState.NO_SWARM)
    return magnet


def _torrent_params(repo_id, magnet, resume, cache_list=True):
    """(params, saved): add_torrent params with the cached file list and any resume data folded in, and
    the .torrent path the file list is cached to (a magnet alone needs a live peer to get it).

    cache_list is False when the caller supplied the magnet: the site it would ask is exactly what may be
    unreachable on that path, and the magnet is authoritative.
    """
    atp = lt.parse_magnet_uri(magnet)
    atp.save_path = str(CACHE_DIR)
    atp.flags |= lt.torrent_flags.default_dont_download
    saved = CACHE_DIR / "torrents" / f"{atp.info_hashes.v1}.torrent"
    if not saved.exists() and cache_list:
        _save_torrent_file(repo_id, saved, str(atp.info_hashes.v1))
    if saved.exists():
        try:
            atp.ti = lt.torrent_info(str(saved))
        except RuntimeError:
            # A cache file a crash or a failed write left behind is never the copy this build names:
            # drop it and let the file list come from a peer, as if there were no cache.
            saved.unlink(missing_ok=True)
    if resume:
        ra = lt.read_resume_data(resume)
        atp.have_pieces = ra.have_pieces
        atp.verified_pieces = ra.verified_pieces
        atp.unfinished_pieces = ra.unfinished_pieces
    return atp, saved


def _wait_for_metadata(repo_id, h):
    """Block until the torrent has its file list, or forget the repo and time out."""
    deadline = time.monotonic() + config.METADATA_TIMEOUT
    while not h.status().has_metadata:
        if time.monotonic() > deadline:
            _get_session().remove_torrent(h)
            _remember(repo_id, RepoState.NO_SWARM)
            raise TimeoutError("no peer sent the file list in time")
        time.sleep(0.2)


def _check_listing(repo_id, h, saved):
    """Refuse a listing this client won't write, before the handle is remembered or its .torrent cached.

    libtorrent writes the torrent's own paths under save_path, so this is the one place the download can be
    stopped: every file is still skipped (default_dont_download), so nothing has been prioritised or created."""
    info = lt.bdecode(h.torrent_file().info_section())
    try:
        files_in(info)  # the decoded info dict: no re-encode, and the result is only used to refuse
    except Unsafe as e:
        _get_session().remove_torrent(h)
        saved.unlink(missing_ok=True)  # the .torrent we cached from the site, kept for nothing
        _remember(repo_id, e)  # the reason, not None: every later file of this repo says the same
        raise


def _cache_file_list(saved, h):
    """Save the file list a peer sent, so a magnet alone needs a live peer only the first time."""
    if not saved.exists():
        write_bytes(saved, lt.bencode({"info": lt.bdecode(h.torrent_file().info_section())}))


def _handle(repo_id, resume=None, magnet=None, where=None):
    """The repo's handle, adding the torrent the first time. Callers hold the repo's _setup_lock, so only the
    shared dicts need _lock: the site lookup and the metadata wait run without it, so one repo waiting for its
    file list doesn't hold up every other repo's downloads and status lines.

    `magnet` joins that swarm instead of asking the site which one belongs to the repo: that is the
    cached-index path, where the site is what cannot be reached. One repo id is not one swarm -- revisions
    and quants are separate torrents -- so a handle already open is reused only if it is that swarm.
    `where` is the provenance appended to the swarm line -- "the cached index" when the file list came
    from the cache instead of the site.
    """
    from_index = magnet is not None
    with _lock:
        state = _handles.get(repo_id)
    match state:
        case Unsafe() as refused:
            raise Unavailable(str(refused))  # a listing this client refused: same answer for every file
        case RepoState.NO_SWARM if magnet is None:
            raise Unavailable()  # an answer about the API, not the index: a magnet still joins its swarm
        case lt.torrent_handle() as h:
            if magnet is None or str(h.info_hashes().v1) == str(lt.parse_magnet_uri(magnet).info_hashes.v1):
                return h
            raise Unavailable("a different torrent for this repo is already open")
    if magnet is None:
        magnet = _lookup_magnet(repo_id)
        if magnet is None:
            raise Unavailable()
    atp, saved = _torrent_params(repo_id, magnet, resume, cache_list=not from_index)
    h = _get_session().add_torrent(atp)
    _wait_for_metadata(repo_id, h)
    _check_listing(repo_id, h, saved)
    _cache_file_list(saved, h)
    _choose_by_piece(h)
    _remember(repo_id, h)
    threading.Thread(target=_show_status, args=(repo_id, h), daemon=True).start()
    fs = h.torrent_file().files()
    real = real_files(fs)
    if where is None:
        where = "the cached index" if from_index else "Pirate Face"
    ui.banner(repo_id, len(real), sum(fs.file_size(i) for i in real), where)
    return h


def _choose_by_piece(h):
    """Turn every file on, then choose what to download per piece. A file at priority 0 has its share of a
    piece parked in libtorrent's part file, and switching that file on while the piece is half-downloaded
    fails with "file too short" and stops the torrent. Nothing is downloaded yet, so this switch is safe."""
    h.prioritize_files([4] * h.torrent_file().num_files())
    # Applied all at once by a disk job, which then resets piece priorities; pad files stay at 0.
    deadline = time.monotonic() + config.METADATA_TIMEOUT
    while 4 not in h.get_file_priorities():
        if time.monotonic() > deadline:
            raise TimeoutError("libtorrent didn't apply the file priorities")
        time.sleep(0.05)
    h.prioritize_pieces([0] * h.torrent_file().num_pieces())


def _pieces_for(ti, files):
    """Piece priorities that download exactly the pieces these files touch."""
    fs, length = ti.files(), ti.piece_length()
    prio = [0] * ti.num_pieces()
    for f in files:
        start, size = fs.file_offset(f), fs.file_size(f)
        for p in range(start // length, (start + size - 1) // length + 1 if size else 0):
            prio[p] = 4
    return prio


def _save_torrent_file(repo_id, saved, infohash):
    try:
        data = api.torrent_file(repo_id)
    except api.Unreachable:
        return  # the magnet still works; file list can come from peers
    if not data:
        return
    try:
        ti = lt.torrent_info(data)
    except RuntimeError:
        # A .torrent the site sent that libtorrent can't parse shouldn't take the repo off the
        # swarm: the magnet still works and the file list can come from peers.
        return
    if str(ti.info_hashes().v1) == infohash:
        write_bytes(saved, data)


def _show_status(repo_id, h, every=0.5, recent=3.0):
    """Sources = who sent bytes recently, not who is connected: the web seed reconnects per file and flickers."""
    sent = {}
    last_seen = {}
    bar = None
    while True:
        time.sleep(every)
        try:
            with _lock:
                wanted = list(_wanted.get(repo_id, ()))
                current = _handles.get(repo_id)
            # Stop if this handle was replaced (e.g. re-added after going finished).
            if current is not h:
                if bar is not None:
                    bar.close()
                return
            fs = h.torrent_file().files()
            progress = h.file_progress(lt.torrent_handle.piece_granularity)
            total = sum(fs.file_size(i) for i in wanted)
            done = sum(min(progress[i], fs.file_size(i)) for i in wanted)
            files_done = sum(progress[i] >= fs.file_size(i) for i in wanted)
            if not wanted or files_done == len(wanted):
                if bar is not None:
                    bar.close()
                    bar = None
                continue
            now = time.monotonic()
            for p in h.get_peer_info():
                conn = tuple(p.ip)
                if p.total_download > sent.get(conn, 0):
                    last_seen[(conn[0], p.connection_type != lt.peer_info.standard_bittorrent)] = now
                sent[conn] = p.total_download
            active = [web for (_, web), t in last_seen.items() if now - t < recent]
            label = ui.sources_label(any(active), active.count(False))
            if bar is None:
                bar = ui.repo_progress(repo_id)
            bar.total, bar.n = total, done
            ui.set_status(
                bar,
                f"{files_done}/{len(wanted)} files · {done / 1e6:.1f}/{total / 1e6:.1f} MB · {label} · {h.status().download_rate / 1e6:.1f} MB/s",
            )
        except Exception:
            # The handle can disappear while this thread is reading it; the download
            # thread reports real failures, so stop quietly here.
            if bar is not None:
                bar.close()
            return


def swarm_files(repo_id, magnet=None, where=None):
    """(path, size) for each real file in the repo's torrent, BEP 47 pad files left out.

    Opens (or reuses) the handle, so this is how a caller finds out what a swarm actually carries before
    promising a download from it: a repo can be in the index while its torrent holds only some files.
    """
    with _setup_lock(repo_id):
        h = _handle(repo_id, magnet=magnet, where=where)
        ti = h.torrent_file()
        fs = ti.files()
        return [(_relative(ti, i), fs.file_size(i)) for i in real_files(fs)]


def _revive_if_finished(repo_id, h, magnet):
    """Re-add a finished handle (with resume data, so nothing is re-hashed), only while no other fetch
    is using it; otherwise return h as-is. A "finished" torrent drops its web seed, so the next file
    never gets bytes."""
    # Wait for other fetches to drain their last file -- each fetch decrements _active in a finally, so
    # this drains; the wait can take as long as its flush plus any wait behind the global flush lock.
    with _idle_cv:
        while _active.get(repo_id) and h.status().state == lt.torrent_status.finished:
            _idle_cv.wait(5)
    if h.status().state != lt.torrent_status.finished:  # still: no other fetch revived it meanwhile
        return h
    resume = alerts.resume_data(h)
    _forget(repo_id, h)
    return _handle(repo_id, resume, magnet=magnet)


def _wait_for_download(h, i, size, stall_timeout):
    """Block until file i has size bytes; fail on a torrent error or on no bytes arriving (a stall -- not
    on no piece finishing, which a slow but live swarm can exceed)."""
    last_received, last_change = -1, time.monotonic()
    while h.file_progress(lt.torrent_handle.piece_granularity)[i] < size:
        st = h.status()
        if st.errc.value():
            raise RuntimeError(f"torrent error: {st.errc.message()}")
        if st.total_payload_download != last_received:
            last_received, last_change = st.total_payload_download, time.monotonic()
        elif time.monotonic() - last_change > stall_timeout:
            raise TimeoutError(f"stalled for {stall_timeout:.0f}s")
        time.sleep(0.25)


def fetch_file(repo_id, filename, stall_timeout=config.STALL_TIMEOUT, magnet=None):
    with _setup_lock(repo_id):
        h = _handle(repo_id, magnet=magnet)
        h = _revive_if_finished(repo_id, h, magnet)

        ti = h.torrent_file()
        i = _file_index(ti, filename)
        if i is None:
            raise NotInTorrent("not in the torrent")
        fs = ti.files()
        size = fs.file_size(i)
        path = Path(h.status().save_path) / fs.file_path(i)
        # Set all priorities in one call (all-skipped for a moment drops the web seed), from our own set:
        # libtorrent applies them lazily, so reading them back across threads loses updates.
        # Keep accumulating wanted files: from_pretrained fetches several files in parallel.
        with _lock:
            wanted = _wanted.setdefault(repo_id, set())
            wanted.add(i)
            h.prioritize_pieces(_pieces_for(ti, wanted))
            # Counted only after the priorities are in place: an exception above
            # must not leave a phantom in-flight fetch behind.
            _active[repo_id] = _active.get(repo_id, 0) + 1

    try:
        _wait_for_download(h, i, size, stall_timeout)
        alerts.flush_to_disk(h)
        return path
    except BaseException:
        # A failed fetch must not leave its file wanted forever: at priority 4 the
        # torrent never reaches "finished" again, which would disable the re-add path.
        with _idle_cv:
            wanted = _wanted[repo_id]
            if i in wanted and _active.get(repo_id, 0) <= 1:
                wanted.discard(i)
                with contextlib.suppress(Exception):
                    h.prioritize_pieces(_pieces_for(ti, wanted))
        raise
    finally:
        with _idle_cv:
            _active[repo_id] -= 1
            _idle_cv.notify_all()
