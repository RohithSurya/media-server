#!/usr/bin/env python3
"""
5movierulz crawler -> Torznab feed for Prowlarr.

Why this exists: the tamilmv/tamilblasters crawlers are compiled Rust spiders inside
the MediaFusion image and can't be taught a new site. movierulz is a WordPress-style
site, so we crawl it ourselves (through the existing FlareSolverr, since it's
Cloudflare-protected) and expose a minimal Torznab feed that Prowlarr consumes as a
generic Torznab indexer -- exactly how MediaFusion is wired in.

Two threads (started from main.py):
  * run_crawler()   -- periodically crawls all-language category pages via FlareSolverr,
                       extracts magnets, parses/classifies, upserts into sqlite.
  * serve_torznab() -- stdlib HTTP server answering t=caps / search / movie / tvsearch.

Scope: all languages; MOVIES + full-season packs only. Single-episode and "Part"
releases are skipped (no clean Sonarr mapping).

Network: this runs OFF the VPN (default bridge net). All site fetching is proxied
through FlareSolverr (which is inside the VPN), reached at FLARESOLVERR_URL
(default http://gluetun:8191) -- the same gluetun-hostname pattern seedr uses for *arr.
"""
import datetime
import email.utils
import os
import re
import sqlite3
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

import requests

from common import clog, norm

# ---- config ----
BASE = os.environ.get("MOVIERULZ_BASE", "https://www.5movierulz.house").rstrip("/")
FLARESOLVERR_URL = os.environ.get("FLARESOLVERR_URL", "http://gluetun:8191").rstrip("/")
APIKEY = os.environ.get("MOVIERULZ_APIKEY", "")
PORT = int(os.environ.get("TORZNAB_PORT", "8002"))
CRAWL_INTERVAL = int(os.environ.get("CRAWL_INTERVAL", "3600"))
# Crawl the master /movies listing page-by-page (newest first) all the way to the last page, so
# EVERY language and type (incl. *-dubbed) is covered from one source -- nothing is left out.
# Stop at the first empty page (the true end) or, on incremental re-crawls, once a whole page is
# posts already seen before this pass (MIN_PAGES floor guards the curated first page from hiding
# new posts that sit deeper).
MOVIES_PATH = os.environ.get("MOVIERULZ_MOVIES_PATH", "/movies")
MIN_PAGES = int(os.environ.get("MIN_PAGES", "3"))     # always scan at least this many pages per pass
MAX_PAGES = int(os.environ.get("MAX_PAGES", "1000"))  # safety cap; real stop is the last/empty page
FAKE_SEEDERS = int(os.environ.get("FAKE_SEEDERS", "50"))
REQUEST_DELAY = float(os.environ.get("REQUEST_DELAY", "3"))
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "0"))   # 0 = keep forever
DB_PATH = os.environ.get("MOVIERULZ_DB", "/config/movierulz.db")

# Best-effort language label parsed from the post slug (e.g. '...-telugu-7008.html' or
# '...-telugu-dubbed-7112.html'). Functionally optional -- the feed matches by title, not language.
LANG_RE = re.compile(
    r'-(telugu|tamil|malayalam|kannada|hindi|bengali|punjabi|english)(?:-dubbed)?-\d+\.html$', re.I)


def lang_from_slug(url):
    m = LANG_RE.search(url)
    return m.group(1).lower() if m else "unknown"


def log(msg):
    clog("movierulz", msg)


# ---- regexes ----
# Any internal post link, keyed on the trailing numeric id. Captures both URL schemes:
#   Form A: /peddi-2026-dvdscr-telugu-7017.html
#   Form B: /save-the-tigers-season-3-2026-telugu/movie-watch-online-free-7090.html
_host = re.escape(urllib.parse.urlparse(BASE).netloc)
POST_RE = re.compile(r'https?://' + _host + r'/[A-Za-z0-9/_-]+?-(\d+)\.html')
MAGNET_RE = re.compile(r'magnet:\?[^"\'<>\s]+')

# Full-season pack, e.g. "S02 EP (01-08)" -> indexed as a season pack.
SEASON_PACK = re.compile(r'\bS(\d{1,2})\s*EP\s*\(\s*\d+\s*-\s*\d+\s*\)', re.I)
# Other series markers (single ep "S04 EP09"/"S04E09", "S1P2"/"S1P-1-2", spelled-out
# "Season 3") -> NOT mappable to Sonarr cleanly, so these releases are skipped.
SERIES_ANY = re.compile(r'\bS\d{1,2}\s*(?:EP?\s*\d+|E\d+|P[\s\-\d])', re.I)
SEASON_WORD = re.compile(r'\bSeason\s*\d+\b', re.I)

QUALITY_RE = re.compile(r'\b(2160p|1080p|720p|480p)\b', re.I)
YEAR_RE = re.compile(r'\b(19|20)\d{2}\b')
SITE_PREFIX_RE = re.compile(r'^\s*www\.\S+\s+-\s+', re.I)  # strip leading 'www.<domain> - ' (any spelling)
EXT_RE = re.compile(r'\.(mkv|mp4|avi)$', re.I)
SIZE_RE = re.compile(r'(\d+(?:\.\d+)?)\s*(GB|MB)\b', re.I)


# ---- sqlite ----
DB_LOCK = threading.Lock()
_conn = None


def db():
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS posts (
                post_id INTEGER PRIMARY KEY,
                url TEXT,
                scraped_at REAL
            );
            CREATE TABLE IF NOT EXISTS releases (
                btih TEXT PRIMARY KEY,
                post_id INTEGER,
                title TEXT,
                kind TEXT,
                season INTEGER,
                language TEXT,
                year INTEGER,
                quality TEXT,
                size INTEGER,
                magnet TEXT,
                added_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_releases_added ON releases(added_at);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            """
        )
        _conn.commit()
    return _conn


def get_meta(key):
    with DB_LOCK:
        row = db().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None


def set_meta(key, value):
    with DB_LOCK:
        db().execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (key, str(value)))
        db().commit()


def post_seen(post_id):
    with DB_LOCK:
        cur = db().execute("SELECT 1 FROM posts WHERE post_id=?", (post_id,))
        return cur.fetchone() is not None


def mark_post(post_id, url):
    with DB_LOCK:
        db().execute("INSERT OR REPLACE INTO posts(post_id, url, scraped_at) VALUES (?,?,?)",
                     (post_id, url, time.time()))
        db().commit()


def upsert_release(rec):
    with DB_LOCK:
        # OR IGNORE keeps the original added_at -> stable pubDate for Prowlarr.
        db().execute(
            """INSERT OR IGNORE INTO releases
               (btih, post_id, title, kind, season, language, year, quality, size, magnet, added_at)
               VALUES (:btih,:post_id,:title,:kind,:season,:language,:year,:quality,:size,:magnet,:added_at)""",
            rec)
        db().commit()


# ---- FlareSolverr fetch ----
def fetch(url):
    """Return (http_status, html) for url via FlareSolverr, or (None, '') on failure."""
    try:
        r = requests.post(f"{FLARESOLVERR_URL}/v1",
                          json={"cmd": "request.get", "url": url, "maxTimeout": 60000},
                          timeout=90)
        r.raise_for_status()
        sol = r.json().get("solution", {})
        return sol.get("status"), sol.get("response", "") or ""
    except Exception as e:
        log(f"  fetch failed for {url}: {e}")
        return None, ""


# ---- parsing ----
def size_from_name(name):
    m = SIZE_RE.search(name)
    if not m:
        return 0
    val = float(m.group(1))
    return int(val * (1e9 if m.group(2).upper() == "GB" else 1e6))


def classify(name):
    """Return (kind, season) where kind in {'movie','series','skip'}."""
    mp = SEASON_PACK.search(name)
    if mp:
        return "series", int(mp.group(1))
    if SERIES_ANY.search(name) or SEASON_WORD.search(name):
        return "skip", None
    return "movie", None


def normalize_title(name, kind):
    """Clean the dn= name into a release title Sonarr/Radarr can parse.
    For season packs, collapse 'S02 EP (01-08)' -> 'S02' so Sonarr sees a season pack."""
    title = SITE_PREFIX_RE.sub("", name)
    title = EXT_RE.sub("", title)
    if kind == "series":
        title = SEASON_PACK.sub(lambda m: f"S{int(m.group(1)):02d}", title)
    return title.strip()


def parse_magnet(m, post_id, language):
    """Parse one magnet into a release rec, or None if it should be skipped."""
    m = m.replace("&amp;", "&")
    bt = re.search(r'btih:([0-9a-fA-F]{40}|[0-9a-zA-Z]{32})', m)
    if not bt:
        return None
    btih = bt.group(1).lower()
    dn = re.search(r'[?&]dn=([^&]+)', m)
    raw_name = urllib.parse.unquote(dn.group(1)) if dn else ""
    xl = re.search(r'[?&]xl=(\d+)', m)

    kind, season = classify(raw_name)
    if kind == "skip":
        return None

    title = normalize_title(raw_name, kind)
    if not title:
        return None
    size = int(xl.group(1)) if xl else size_from_name(raw_name)
    qm = QUALITY_RE.search(raw_name)
    quality = qm.group(1).lower() if qm else ""
    ym = YEAR_RE.search(raw_name)
    year = int(ym.group(0)) if ym else None

    return {"btih": btih, "post_id": post_id, "title": title, "kind": kind,
            "season": season, "language": language, "year": year, "quality": quality,
            "size": size, "magnet": m, "added_at": time.time()}


def process_post(post_id, url, language):
    status, html = fetch(url)
    time.sleep(REQUEST_DELAY)
    if status != 200 or not html:
        return 0
    n = 0
    for m in MAGNET_RE.findall(html):
        rec = parse_magnet(m, post_id, language)
        if rec:
            upsert_release(rec)
            n += 1
    mark_post(post_id, url)   # mark seen even if 0 magnets, to avoid re-fetching
    return n


def crawl_once():
    """Walk /movies/page/1 .. last page (newest first).
    Until the catalogue has been fully backfilled once (meta 'backfill_done'), keep paginating to the
    real last page every pass -- this makes the long first backfill RESUMABLE across restarts (already
    seen posts are skipped cheaply, so re-scanning the early pages is fast). After the backfill has
    reached the end once, switch to incremental: stop at the first page that is entirely posts known
    before this pass (MIN_PAGES floor guards the curated first page)."""
    total_posts = total_releases = 0
    backfilling = get_meta("backfill_done") != "1"
    with DB_LOCK:
        known_before = {r[0] for r in db().execute("SELECT post_id FROM posts").fetchall()}
    page, reached_end = 1, False
    while page <= MAX_PAGES:
        url = f"{BASE}{MOVIES_PATH}/page/{page}"
        status, html = fetch(url)
        time.sleep(REQUEST_DELAY)
        if status != 200 or not html:
            reached_end = True
            break
        page_ids, seen_on_page = [], set()
        for mobj in POST_RE.finditer(html):
            pid = int(mobj.group(1))
            if pid in seen_on_page:
                continue
            seen_on_page.add(pid)
            page_ids.append((pid, mobj.group(0)))
        if not page_ids:
            reached_end = True   # past the last real page
            break
        for pid, purl in page_ids:
            if not post_seen(pid):
                total_releases += process_post(pid, purl, lang_from_slug(purl))
                total_posts += 1
        # incremental mode only: once a whole page is posts we already had before this pass, the rest
        # downstream is older and already indexed -> stop.
        if not backfilling and page >= MIN_PAGES and all(pid in known_before for pid, _ in page_ids):
            break
        page += 1
    if backfilling and reached_end:
        set_meta("backfill_done", "1")
        log("full catalogue backfill complete -> switching to incremental crawls")
    if RETENTION_DAYS > 0:
        cutoff = time.time() - RETENTION_DAYS * 86400
        with DB_LOCK:
            db().execute("DELETE FROM releases WHERE added_at < ?", (cutoff,))
            db().commit()
    log(f"crawl done: scanned {page} page(s), {total_posts} new posts, {total_releases} releases indexed")


def run_crawler():
    db()  # init schema
    log(f"crawler starting; master /movies crawl every {CRAWL_INTERVAL}s, via {FLARESOLVERR_URL}")
    while True:
        try:
            crawl_once()
        except Exception as e:
            log(f"crawl error: {e}")
        time.sleep(CRAWL_INTERVAL)


# ---- Torznab ----
def torznab_category(rec):
    hd = rec["quality"] in ("720p", "1080p", "2160p")
    if rec["kind"] == "series":
        return 5040 if hd else 5030
    return 2040 if hd else 2030


def caps_xml():
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<caps>\n'
        '  <server title="movierulz"/>\n'
        '  <limits max="100" default="100"/>\n'
        '  <searching>\n'
        '    <search available="yes" supportedParams="q"/>\n'
        '    <movie-search available="yes" supportedParams="q,year"/>\n'
        '    <tv-search available="yes" supportedParams="q,season,ep"/>\n'
        '  </searching>\n'
        '  <categories>\n'
        '    <category id="2000" name="Movies">\n'
        '      <subcat id="2030" name="Movies/SD"/>\n'
        '      <subcat id="2040" name="Movies/HD"/>\n'
        '    </category>\n'
        '    <category id="5000" name="TV">\n'
        '      <subcat id="5030" name="TV/SD"/>\n'
        '      <subcat id="5040" name="TV/HD"/>\n'
        '    </category>\n'
        '  </categories>\n'
        '</caps>\n'
    )


def error_xml(code, desc):
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<error code="{code}" description="{escape(desc)}"/>\n'


def _collapse(s):
    """norm() + collapse runs of the same char, so transliteration variants match:
    'Bangaaram' and 'Bangaram' both -> 'bangaram', 'Pedda'/'Peddha' -> 'peda'/'pedha'."""
    return re.sub(r"(.)\1+", r"\1", norm(s))


def title_matches(q, title):
    """True if every word of the query (collapsed) appears in the collapsed title.
    Token-based + dup-collapse tolerates word order, spacing, and doubled letters --
    the *arr still re-parse and filter results, so we can afford to be generous."""
    ct = _collapse(title)
    toks = [_collapse(w) for w in q.split()]
    toks = [w for w in toks if w]
    return all(w in ct for w in toks)


def query_releases(t, q, season, cats):
    """Select releases matching the search. Returns list of sqlite rows."""
    with DB_LOCK:
        # scan the whole catalogue (no LIMIT) so old back-catalog stays searchable; the token
        # matcher below is cheap and results are capped to 100.
        rows = db().execute("SELECT * FROM releases ORDER BY added_at DESC").fetchall()
    out = []
    for r in rows:
        # kind filter by search type
        if t == "movie" and r["kind"] != "movie":
            continue
        if t == "tvsearch" and r["kind"] != "series":
            continue
        # category filter (e.g. cat=2000 or 5000 families)
        if cats:
            fam = 5000 if r["kind"] == "series" else 2000
            sub = torznab_category(r)
            if not any(c == fam or c == sub for c in cats):
                continue
        # text filter (dup-collapsing, token-based -> matches transliteration variants)
        if q and not title_matches(q, r["title"]):
            continue
        if season and r["kind"] == "series" and r["season"] and int(season) != r["season"]:
            continue
        out.append(r)
    return out[:100]


def results_xml(rows):
    items = []
    for r in rows:
        magnet = r["magnet"]
        size = r["size"] or 0
        cat = torznab_category(r)
        pub = email.utils.formatdate(r["added_at"], usegmt=True)
        items.append(
            "    <item>\n"
            f"      <title>{escape(r['title'])}</title>\n"
            f'      <guid isPermaLink="false">{r["btih"]}</guid>\n'
            f"      <pubDate>{pub}</pubDate>\n"
            f"      <size>{size}</size>\n"
            f"      <link>{escape(magnet)}</link>\n"
            f'      <enclosure url="{escape(magnet)}" length="{size}" type="application/x-bittorrent"/>\n'
            f'      <torznab:attr name="category" value="{cat}"/>\n'
            f'      <torznab:attr name="magneturl" value="{escape(magnet)}"/>\n'
            f'      <torznab:attr name="seeders" value="{FAKE_SEEDERS}"/>\n'
            f'      <torznab:attr name="peers" value="{FAKE_SEEDERS}"/>\n'
            f'      <torznab:attr name="size" value="{size}"/>\n'
            "    </item>\n"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed">\n'
        '  <channel>\n'
        '    <title>movierulz</title>\n'
        '    <description>5movierulz torznab feed</description>\n'
        f'    <link>{escape(BASE)}</link>\n'
        + "".join(items) +
        '  </channel>\n'
        '</rss>\n'
    )


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # silence default per-request stderr logging

    def _send(self, body, code=200):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if not parsed.path.rstrip("/").endswith("/api") and "/torznab" not in parsed.path:
            self._send(error_xml(200, "not found"), 404)
            return
        qs = urllib.parse.parse_qs(parsed.query)
        t = (qs.get("t", ["caps"])[0]).lower()

        if t == "caps":
            self._send(caps_xml())
            return

        if APIKEY and qs.get("apikey", [""])[0] != APIKEY:
            self._send(error_xml(100, "Incorrect user credentials"))
            return

        if t not in ("search", "movie", "tvsearch"):
            self._send(error_xml(202, f"No such function ({t})"))
            return

        q = qs.get("q", [""])[0]
        season = qs.get("season", [""])[0]
        cats = []
        for c in qs.get("cat", []):
            cats += [int(x) for x in c.split(",") if x.strip().isdigit()]
        try:
            rows = query_releases(t, q, season, cats)
            self._send(results_xml(rows))
        except Exception as e:
            log(f"torznab query error: {e}")
            self._send(error_xml(900, "internal error"))


def serve_torznab():
    db()  # ensure schema exists before serving
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log(f"torznab server listening on :{PORT} (apikey {'set' if APIKEY else 'DISABLED'})")
    httpd.serve_forever()


if __name__ == "__main__":
    # manual run: crawl in a thread, serve in foreground
    threading.Thread(target=run_crawler, daemon=True).start()
    serve_torznab()
