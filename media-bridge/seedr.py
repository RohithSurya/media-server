#!/usr/bin/env python3
"""
Seedr <-> Sonarr/Radarr blackhole bridge.

Flow (Torrent Blackhole pattern):
  1. Sonarr/Radarr write <release>.magnet into /data/blackhole/{sonarr,radarr}/
  2. We add the magnet to Seedr      -> POST /rest/transfer/magnet  (field: magnet)
  3. We poll the root listing until the transfer finishes
     (an in-progress entry lives in root["torrents"]; when done it becomes a
      top-level folder in root["folders"] whose "path" == the transfer title).
  4. We download the finished files   -> GET /rest/file/{id} (302 -> CDN bytes)
     into /data/downloads/{sonarr,radarr}/<title>/  (the *arr "watch" folder).
  5. We delete the item from Seedr     -> DELETE /rest/folder/{id}   to free space.

API contract verified live against the account (Seedr does not publish a schema):
  root/subfolder listing -> {folders:[{id, path, size}], files:[{id, name, size}],
                             torrents:[{id, name, progress, ...}]}
  add magnet response    -> {user_torrent_id, title, torrent_hash, success}
Auth: HTTP Basic. Password is provided base64-encoded (SEEDR_PASSWORD_B64) to
survive Docker/shell '$' interpolation; plain SEEDR_PASSWORD also accepted.
"""
import base64
import json
import os
import re
import shutil
import time
import pathlib
import requests

from common import clog, norm, ntfy

FAIL_COUNTS = {}  # magnet filename -> consecutive 413 attempts (in-memory)
TOMBSTONE_GRACE = 600  # secs to keep reaping a superseded release's late-appearing Seedr folder


def log(msg):
    clog("seedr", msg)


def movie_key(release_name):
    """Normalized 'title + year' identity for a Radarr release, or None if undecidable.
    'Real Steel 2011 1080p PROPER BluRay...'  -> 'realsteel2011'
    'Real Steel (2011) 1080p BRRip x264 -YTS' -> 'realsteel2011'
    Used to recognise that a new release supersedes an old one of the SAME movie."""
    m = re.search(r"\b(19|20)\d{2}\b", release_name)
    if m:
        return norm(release_name[:m.end()])
    q = re.search(r"\b(2160p|1080p|720p|480p|bluray|brrip|bdrip|webrip|web|hdtv|x264|x265|hevc)\b",
                  release_name, re.I)
    return norm(release_name[:q.start()]) if q else None   # None -> skip supersede (too risky)

EMAIL = os.environ["SEEDR_EMAIL"]
_pw_b64 = os.environ.get("SEEDR_PASSWORD_B64")
PASSWORD = base64.b64decode(_pw_b64).decode() if _pw_b64 else os.environ["SEEDR_PASSWORD"]
POLL = int(os.environ.get("POLL_INTERVAL", "30"))

BASE = "https://www.seedr.cc/rest"
DATA = pathlib.Path("/data")
STATE_DIR = DATA / "blackhole" / ".bridge-state"
CATEGORIES = ["sonarr", "radarr"]

# Stalled-download handling: if a transfer's progress hasn't advanced for this long,
# fail it in Radarr/Sonarr (-> blocklist + auto re-grab a different release) and notify.
STALL_TIMEOUT = int(os.environ.get("STALL_TIMEOUT", "2700"))   # 45 min
FETCH_SKIP_MAX = int(os.environ.get("FETCH_SKIP_MAX", str(1 << 20)))  # <1 MB: skip an unfetchable junk sidecar instead of failing the whole folder
ARR = {"radarr": (os.environ.get("RADARR_URL"), os.environ.get("RADARR_API_KEY")),
       "sonarr": (os.environ.get("SONARR_URL"), os.environ.get("SONARR_API_KEY"))}

SESSION = requests.Session()
SESSION.auth = (EMAIL, PASSWORD)
SESSION.headers["User-Agent"] = "seedr-bridge/1.0"


def api(method, path, **kw):
    r = SESSION.request(method, f"{BASE}{path}", timeout=kw.pop("timeout", 60), **kw)
    r.raise_for_status()
    return r


def list_folder(folder_id=""):
    return api("GET", f"/folder/{folder_id}" if folder_id != "" else "/folder").json()


def add_magnet(magnet):
    return api("POST", "/transfer/magnet", data={"magnet": magnet}).json()


def download_file(file_id, dest: pathlib.Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with SESSION.get(f"{BASE}/file/{file_id}", stream=True, timeout=900,
                     allow_redirects=True) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                if chunk:
                    f.write(chunk)
    tmp.rename(dest)
    log(f"    downloaded {dest.name} ({dest.stat().st_size:,} bytes)")


def fetch_folder_recursive(folder_id, dest_root: pathlib.Path):
    listing = list_folder(folder_id)
    for f in listing.get("files", []):
        dest = dest_root / f["name"]
        # Resume support: if a previous pass already pulled this file in full,
        # skip it. Without this, one dropped connection on a big 4K file made
        # the whole folder re-download from scratch (thrashing 20+ GB packs).
        size = f.get("size")
        if dest.exists() and size and dest.stat().st_size == size:
            continue
        try:
            download_file(f["id"], dest)
        except Exception as e:
            # Junk sidecars (YTSProxies.com.txt, "Torrent Downloaded From*.txt",
            # RARBG.nfo, ...) sometimes 404 on Seedr's CDN. They're not media and
            # the *arr import ignores them, so a small file we can't fetch must
            # NOT poison the whole folder (which would block delete_folder + the
            # import forever and re-thrash the big mkv). Skip it; re-raise on any
            # substantial file so real media still benefits from retry/resume.
            if size and size < FETCH_SKIP_MAX:
                log(f"    skipping unfetchable sidecar {f['name']} ({size:,} B): {e}")
                continue
            raise
    for sub in listing.get("folders", []):
        fetch_folder_recursive(sub["id"], dest_root / sub["path"])


def delete_folder(folder_id):
    try:
        api("DELETE", f"/folder/{folder_id}")
    except Exception as e:
        log(f"    (warning) could not delete Seedr folder {folder_id}: {e}")


def delete_file(file_id):
    try:
        api("DELETE", f"/file/{file_id}")
    except Exception as e:
        log(f"    (warning) could not delete Seedr file {file_id}: {e}")


def delete_transfer(tid):
    # in-progress torrents are removed via /transfer/{user_torrent_id}
    # (a finished one becomes a folder, removed via delete_folder instead).
    try:
        api("DELETE", f"/transfer/{tid}")
    except Exception as e:
        log(f"    (warning) could not delete Seedr transfer {tid}: {e}")


def _name_matches(seedr_name, title):
    # True when seedr_name and title are (almost certainly) the SAME release.
    # norm() drops punctuation, so Seedr's sanitization (dashes/spaces/appended group
    # tags) mostly reduces to one name being a prefix of the other. We compare the FULL
    # shorter string -- NOT a fixed-length prefix -- so two DIFFERENT releases of the
    # same movie (which share only the 'title+year' head, e.g. ...DTS vs ...BRRip) do
    # NOT match. This distinction is what the supersede reaper's safety guard relies on.
    a, b = norm(seedr_name), norm(title)
    if not a or not b:
        return False
    shorter, longer = sorted((a, b), key=len)
    return len(shorter) >= 18 and longer.startswith(shorter)


def cleanup_local_partial(cat, old_title, new_title=None):
    # remove a superseded release's partially-fetched download dir. The dir is named
    # after the Seedr folder (often sanitized), not the state title, so match fuzzily.
    base = DATA / "downloads" / cat
    if not base.exists():
        return
    for d in base.iterdir():
        if not d.is_dir():
            continue
        if new_title and _name_matches(d.name, new_title):   # never the replacement's dir
            continue
        if _name_matches(d.name, old_title):
            shutil.rmtree(d, ignore_errors=True)
            log(f"    [override] removed local partial '{d.name}'")


def purge_superseded(cat, new_key, new_name, root):
    """Mark every in-progress Radarr release of the SAME movie as `new_key` (but a
    DIFFERENT release than `new_name`) for deferred cleanup: delete its transfer now
    (frees in-progress space), drop any local partial, and rewrite its state file as a
    tombstone so check_completions reaps the (often late-appearing) Seedr folder.
    Returns the number of releases superseded."""
    if not new_key or not STATE_DIR.exists():
        return 0
    n = 0
    for sp in STATE_DIR.glob("*.json"):
        try:
            rec = json.loads(sp.read_text())
        except Exception:
            continue
        if rec.get("category") != cat or rec.get("superseded"):
            continue
        if norm(rec.get("title", "")) == norm(new_name):     # same release -> leave it
            continue
        if movie_key(rec.get("title", "")) != new_key:        # different movie -> leave it
            continue
        tid = rec.get("user_torrent_id")
        if tid is not None:
            delete_transfer(tid)
        cleanup_local_partial(cat, rec["title"], new_name)
        rec.update(superseded=True, new_title=new_name, superseded_at=time.time())
        sp.write_text(json.dumps(rec))                        # tombstone -> reaped by check_completions
        n += 1
        log(f"    [override] superseded '{rec['title']}' (transfer {tid}); deferring folder cleanup")
    return n


def notify(title, body):
    ntfy(title, body, priority="high", tags="warning")


def arr_mark_failed(cat, release_name):
    """Find the most recent 'grabbed' history record for release_name and mark it failed
    -> Radarr/Sonarr blocklists it and (autoRedownloadFailed) grabs a different release.
    Uses plain requests, NOT SESSION (which carries Seedr Basic-auth we must not leak)."""
    url, key = ARR.get(cat, (None, None))
    if not url or not key:
        return False
    try:
        h = requests.get(f"{url}/api/v3/history",
                         params={"eventType": 1, "pageSize": 100,
                                 "sortKey": "date", "sortDirection": "descending"},
                         headers={"X-Api-Key": key}, timeout=30)
        h.raise_for_status()
        nrel = norm(release_name)
        hid = next((r["id"] for r in h.json().get("records", [])
                    if norm(r.get("sourceTitle", "")) == nrel), None)
        if hid is None:
            log(f"    (warn) no grabbed history match for '{release_name}' in {cat}")
            return False
        requests.post(f"{url}/api/v3/history/failed/{hid}",
                      headers={"X-Api-Key": key}, timeout=30).raise_for_status()
        log(f"    marked {cat} history {hid} failed -> blocklist + auto re-search")
        return True
    except Exception as e:
        log(f"    (warn) {cat} mark-failed failed: {e}")
        return False


def handle_stalled(cat, rec, sp, prog):
    title = rec["title"]
    release = rec.get("release_name", title)
    log(f"[{cat}] '{title}' STALLED on Seedr ({prog}% for >{STALL_TIMEOUT // 60}min) -> failing & retrying")
    tid = rec.get("user_torrent_id")
    if tid is not None:
        delete_transfer(tid)                       # free the dead transfer
    retried = arr_mark_failed(cat, release)
    notify(f"Stalled: {release}",
           f"Stuck at {prog}% for {STALL_TIMEOUT // 60} min on Seedr. "
           + ("Asked Radarr/Sonarr to grab a different release."
              if retried else "Could not reach Radarr/Sonarr -- override it manually."))
    # tombstone -> existing reaper cleans any partial folder/local dir and we stop re-failing
    rec.update(superseded=True, new_title=None, superseded_at=time.time(), stalled=True)
    sp.write_text(json.dumps(rec))
    cleanup_local_partial(cat, title)


def state_file(category, title):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    safe = base64.urlsafe_b64encode(f"{category}|{title}".encode()).decode()
    return STATE_DIR / f"{safe}.json"


def process_new_magnets():
    for cat in CATEGORIES:
        bh = DATA / "blackhole" / cat
        bh.mkdir(parents=True, exist_ok=True)
        for mfile in sorted(bh.glob("*.magnet")) + sorted(bh.glob("*.txt")):
            magnet = mfile.read_text().strip()
            if not magnet.startswith("magnet:"):
                continue
            # movie identity (Radarr only) -> lets a new release supersede an old
            # in-progress 'zombie' transfer of the SAME movie.
            nkey = movie_key(mfile.stem) if cat == "radarr" else None
            log(f"[{cat}] adding {mfile.name}")
            try:
                resp = add_magnet(magnet)
            except requests.HTTPError as e:
                if getattr(e, "response", None) is not None and e.response.status_code == 413:
                    try:
                        r = list_folder()
                        free = r.get("space_max", 0) - r.get("space_used", 0)
                    except Exception:
                        r, free = {}, 0
                    # override: a same-movie zombie is hogging Seedr -> free it and
                    # retry the add next loop instead of dropping this one as too large.
                    if nkey and purge_superseded(cat, nkey, mfile.stem, r) > 0:
                        FAIL_COUNTS.pop(mfile.name, None)
                        log(f"    [override] freed Seedr space from old release; will retry add for {mfile.name}")
                        continue
                    n = FAIL_COUNTS.get(mfile.name, 0) + 1
                    FAIL_COUNTS[mfile.name] = n
                    # plenty of free space yet still 413 => the torrent itself is
                    # bigger than the account; it will never fit -> drop it.
                    if free > 20e9 or n >= 5:
                        log(f"    '{mfile.name}' does not fit Seedr ({free/1e9:.0f}GB free) -> dropping (too large)")
                        mfile.unlink()
                        FAIL_COUNTS.pop(mfile.name, None)
                    else:
                        log(f"    add 413 (Seedr full, {free/1e9:.0f}GB free); attempt {n}/5, will retry")
                    continue
                log(f"    add failed ({e}); will retry next loop")
                continue
            except Exception as e:
                log(f"    add failed ({e}); will retry next loop")
                continue
            FAIL_COUNTS.pop(mfile.name, None)
            if not resp.get("success", True):
                log(f"    Seedr rejected magnet: {resp}; dropping {mfile.name}")
                mfile.unlink()
                continue
            title = resp.get("title") or mfile.stem
            # override: drop any old in-progress release of the same movie.
            if nkey:
                purge_superseded(cat, nkey, mfile.stem, list_folder())
            rec = {"category": cat, "title": title, "release_name": mfile.stem,
                   "user_torrent_id": resp.get("user_torrent_id"),
                   "torrent_hash": resp.get("torrent_hash"), "added": time.time()}
            state_file(cat, title).write_text(json.dumps(rec))
            mfile.unlink()
            log(f"    queued on Seedr as '{title}'")


def check_completions():
    if not STATE_DIR.exists() or not any(STATE_DIR.glob("*.json")):
        return
    root = list_folder()
    folders = {f["path"]: f["id"] for f in root.get("folders", [])}
    # In-progress transfers. Seedr keeps a torrent here (and surfaces its
    # destination folder early, populating files as they finish) until it
    # reaches 100%, at which point it disappears from this list. So the ONLY
    # reliable "done" signal is the torrent leaving root["torrents"] -- NOT the
    # mere existence of the folder (that bug grabbed a half-finished folder).
    torrents_by_id = {t.get("id"): t for t in root.get("torrents", [])}
    torrents_by_name = {t.get("name"): t for t in root.get("torrents", [])}
    for sp in sorted(STATE_DIR.glob("*.json")):
        rec = json.loads(sp.read_text())
        title, cat = rec["title"], rec["category"]
        # superseded release -> reap its (often late-appearing) Seedr footprint, never
        # the replacement's, until the grace window closes.
        if rec.get("superseded"):
            new_title = rec.get("new_title")
            for name, fid in list(folders.items()):
                if _name_matches(name, title) and not (new_title and _name_matches(name, new_title)):
                    log(f"    [override] cleaning leftover Seedr folder '{name}'")
                    delete_folder(fid)
            for f in root.get("files", []):
                nm = f.get("name", "")
                if _name_matches(nm, title) and not (new_title and _name_matches(nm, new_title)):
                    delete_file(f["id"])
            cleanup_local_partial(cat, title, new_title)
            if time.time() - rec.get("superseded_at", 0) > TOMBSTONE_GRACE:
                sp.unlink()
                log(f"    [override] cleanup window closed for '{title}'")
            continue
        tid = rec.get("user_torrent_id")
        t = torrents_by_id.get(tid) or torrents_by_name.get(title)
        if t is not None:
            prog, now = round(t.get("progress", 0), 1), time.time()
            log(f"[{cat}] '{title}' downloading on Seedr ({prog}%)")
            if rec.get("last_progress") is None or prog > rec["last_progress"] + 0.05:
                rec.update(last_progress=prog, last_progress_at=now)   # advanced -> reset stall clock
                sp.write_text(json.dumps(rec))
            elif now - rec.get("last_progress_at", rec.get("added", now)) > STALL_TIMEOUT:
                handle_stalled(cat, rec, sp, prog)
            continue  # still in progress -> do NOT fetch yet
        # torrent finished (gone from in-progress) -> locate its result.
        ntitle = norm(title)
        # (a) multi-file / wrapped torrents land as a top-level FOLDER
        folder_id = folders.get(title)
        if folder_id is None:  # tolerate Seedr sanitizing the name
            for name, fid in folders.items():
                if norm(name)[:20] == ntitle[:20]:
                    folder_id, title = fid, name
                    break
        if folder_id is not None:
            dest = DATA / "downloads" / cat / title
            log(f"[{cat}] '{title}' complete -> fetching folder to {dest}")
            try:
                fetch_folder_recursive(folder_id, dest)
                delete_folder(folder_id)
                sp.unlink()
                log(f"[{cat}] '{title}' delivered.")
            except Exception as e:
                log(f"    fetch failed for '{title}': {e}; will retry")
            continue
        # (b) bare single-file torrents land as a ROOT FILE (no folder)
        match = None
        for f in root.get("files", []):
            stem = norm(os.path.splitext(f.get("name", ""))[0])
            if stem and (stem[:18] == ntitle[:18] or stem.startswith(ntitle[:16]) or ntitle.startswith(stem[:16])):
                match = f
                break
        if match is not None:
            dest = DATA / "downloads" / cat / title / match["name"]
            log(f"[{cat}] '{title}' complete (single file) -> fetching {match['name']}")
            try:
                download_file(match["id"], dest)
                delete_file(match["id"])
                sp.unlink()
                log(f"[{cat}] '{title}' delivered.")
            except Exception as e:
                log(f"    fetch failed for '{title}': {e}; will retry")
            continue
        log(f"[{cat}] '{title}' finished but result not visible yet; will retry")


def run_forever():
    log(f"seedr-bridge starting; polling every {POLL}s")
    try:
        root = list_folder()
        used = root.get("space_used", 0) / 1e9
        cap = root.get("space_max", 0) / 1e9
        log(f"Seedr auth OK; storage {used:.1f}/{cap:.1f} GB used")
    except Exception as e:
        log(f"FATAL: cannot reach Seedr REST API: {e}")
    while True:
        try:
            process_new_magnets()
            check_completions()
        except Exception as e:
            log(f"loop error: {e}")
        time.sleep(POLL)


if __name__ == "__main__":
    run_forever()
