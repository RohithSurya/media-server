# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A self-hosted media automation stack defined by a single `docker-compose.yaml`, running on a
**Jetson Nano Super (aarch64)**. Pipeline: **search** (Prowlarr) → **automate** (Sonarr TV / Radarr
movies) → **cloud download** (Seedr) → **stream** (Jellyfin). The custom Python code is one service,
`media-bridge/` (three responsibilities in one container: seedr bridge + jellyfin-reaper + movierulz
scraper/Torznab feed); everything else is an off-the-shelf image. Service config/state directories
(`prowlarr/`, `radarr/`, `sonarr/`, `bazarr/`, `jellyfin/`, `data/`, `media-bridge/config/`) are
runtime data and are gitignored — only the compose file and the custom service code are version-controlled.

## Commands

```bash
docker compose up -d                              # bring up / apply compose changes
docker compose up -d --build media-bridge         # rebuild the custom service after editing its .py
docker compose logs -f media-bridge               # follow its logs ([seedr]/[reaper]/[movierulz] tags)
docker restart prowlarr flaresolverr sonarr radarr  # REQUIRED after any gluetun restart (see below)
```

There is no build/lint/test tooling — `media-bridge` is a small Python package run directly
(`CMD ["python", "-u", "main.py"]`, which starts its three components as supervised threads). To
exercise a change, rebuild the service and watch its logs.
Secrets come from `.env` (gitignored); see `.env.example` for the required keys.

## Architecture: the VPN namespace

`gluetun` (Proton VPN, WireGuard, kill switch) is the network anchor. The **indexer-facing** apps —
`prowlarr` (9696), `flaresolverr` (8191), `sonarr` (8989), `radarr` (7878) — run with
`network_mode: "service:gluetun"`, so they share gluetun's network namespace and exit IP, and their
ports are published *on the gluetun service*, not their own. They also `depends_on` gluetun being
`service_healthy`.

Consequences that drive most edits here:
- **Adding a VPN-routed container**: give it `network_mode: "service:gluetun"`,
  `depends_on: gluetun (service_healthy)`, and publish its ports on the gluetun service block.
- **Restart gotcha**: restarting gluetun recreates its netns and silently breaks every dependent
  container. After any gluetun restart you MUST `docker restart prowlarr flaresolverr sonarr radarr`
  (or `docker compose down && up -d`). `media-bridge` and `jellyfin` are unaffected — and the movierulz
  Torznab feed (host port 8002) keeps serving, since it is published on the host, not on gluetun.
- **Cross-service URLs inside the netns**: containers sharing gluetun's namespace talk to the *arr
  apps via the hostname **`gluetun`** (e.g. `http://gluetun:7878`), NOT `radarr`/`sonarr` (which don't
  resolve). `media-bridge` (on the default bridge net) reaches the *arr apps AND FlareSolverr the same
  way — `http://gluetun:7878/8989/8191`, since those ports are published on gluetun — and reaches
  host-net Jellyfin via `host.docker.internal` (`extra_hosts: host-gateway`).
- **FlareSolverr must share gluetun's exit IP** (it's in the same netns) because `cf_clearance`
  cookies are IP-bound. CF-protected indexers are wired with a `flaresolverr` tag in Prowlarr. The
  movierulz crawler also bypasses CF through this same FlareSolverr (it never fetches the site directly).

What runs **outside** the VPN: `media-bridge` (seedr does the torrenting in its cloud, so the Jetson
never joins a swarm; keeping it off-VPN also keeps Seedr CDN downloads direct/fast), `bazarr` and
`jellyfin` (both `network_mode: host` for LAN/DLNA/Tailscale reach; they hit the *arr apps on
`127.0.0.1` because gluetun publishes those ports on the host).

## The `/data` single-root convention

`./data` is mounted into sonarr, radarr, media-bridge, and bazarr at the same path so imports are
hardlink-friendly and paths map 1:1 across services. Layout:
- `data/blackhole/{sonarr,radarr}/` — Sonarr/Radarr (configured with a **Torrent Blackhole** download
  client, since Seedr has no native *arr connector) drop `<release>.magnet` files here.
- `data/downloads/{sonarr,radarr}/` — media-bridge's seedr component writes finished downloads here; the
  *arr watch-folder scan imports them.
- `data/media/{tv,movies}` — the Jellyfin library, mounted read-only into jellyfin.
- `data/blackhole/.bridge-state/` — media-bridge seedr per-transfer state JSON (one file per transfer).

## media-bridge (`media-bridge/`)

One container running three components as supervised daemon threads (`main.py` → each in a try/except
wrapper that logs + ntfy-alerts + restarts, so one crash can't take the others down). It is **off the
VPN** on the default bridge net; it reaches the *arr apps and FlareSolverr via `http://gluetun:<port>`
and host-net Jellyfin via `host.docker.internal`. Shared helpers live in `common.py` (`clog`, `norm`,
`ntfy`). Logs are tagged `[seedr]` / `[reaper]` / `[movierulz]`. Rebuilding/restarting this container to
ship a movierulz change also restarts the seedr loop mid-download — safe, since seedr resumes from its
`.bridge-state` JSON.

### seedr component (`media-bridge/seedr.py`)

Polling loop (`process_new_magnets` → `check_completions`, default every 30s) bridging the *arr
blackhole to Seedr's REST API (HTTP Basic auth; password is base64 in `SEEDR_PASSWORD_B64` because a
literal `$` breaks Docker/shell interpolation). Per magnet: add to Seedr → poll → download finished
files into the watch folder → delete from Seedr to free space.

Non-obvious invariants — preserve these when editing:
- **Completion is signalled by the torrent leaving `root["torrents"]`**, NOT by the destination folder
  existing. Seedr surfaces the folder early and fills it as files finish; fetching on folder-existence
  grabs a half-finished download.
- Results land two ways: multi-file torrents as a top-level **folder**, single-file torrents as a bare
  **root file** — both are handled, matched by normalized name prefix.
- **Override/supersede (Radarr only)**: a new release whose `movie_key` (normalized "title+year")
  matches an in-progress *different* release of the same movie triggers `purge_superseded`, which
  deletes the old Seedr transfer immediately and writes a **tombstone** state file; `check_completions`
  reaps the old release's late-appearing folder/partial each loop until `TOMBSTONE_GRACE` expires.
- `_name_matches` MUST compare the full shorter normalized name (prefix relationship, `len>=18`), never
  a fixed-length prefix — a truncated prefix wrongly matches two *different* releases of the same movie
  and defeats the supersede safety guard.
- **Stall handling**: if Seedr `progress` doesn't advance for `STALL_TIMEOUT` (default 2700s), the
  transfer is failed in Radarr/Sonarr via `arr_mark_failed` (history `eventType=1` match by normalized
  `sourceTitle` == stored `release_name`, then `POST /history/failed/{id}` → blocklist + auto re-grab)
  and an ntfy notification is sent. `arr_mark_failed` uses plain `requests`, NOT the module-level
  `SESSION` (which carries Seedr Basic-auth that must not leak to the *arr).
- **413 on add** = Seedr full or release too big. If a same-movie zombie is hogging space, free it and
  retry; if lots of space free yet still 413 (or after 5 tries), the release can't fit the account and
  the magnet is dropped. Seedr account is only ~34 GB — Quality Definition max sizes are capped at
  130 MB/min in both Sonarr and Radarr to keep grabs within budget.

### reaper component (`media-bridge/reaper.py`)

Polling loop (default every 6h) that reclaims disk: deletes a movie once **every** Jellyfin user has
Played it AND `GRACE_DAYS` (default 15) have elapsed since the most recent watch. Matches Jellyfin
items to Radarr movies by TMDB id, and deletes **through Radarr's API** (`deleteFiles=true`) so the
file, folder, Bazarr `.srt` sidecars, and DB entry go together — never `rm` on `/data/media`.

Safety guards (keep intact): `DRY_RUN` defaults **true** (flip to false in compose only after verifying
the dry-run list); any Jellyfin/user API failure or zero users **aborts the whole run** (never assume
"watched"); `MAX_DELETES_PER_RUN` caps blast radius; only movies with `hasFile` are considered.

### movierulz component (`media-bridge/movierulz.py`)

A crawler thread + a stdlib Torznab HTTP server (`:8002`, host-published) that makes 5movierulz a
generic Torznab indexer for Prowlarr — the tamilmv/tamilblasters spiders are compiled into the
MediaFusion image and can't be taught a new site, so we scrape this one ourselves.

- **Crawler** (`run_crawler`, default hourly): walks `/category/<lang>-featured` + `-movies-<year>` for
  all `LANGUAGES`, paginating `/page/N` until a page is all-seen (incremental). Fetches every page
  **through FlareSolverr** (`FLARESOLVERR_URL`, CF-protected site), never directly. Post discovery keys
  on the trailing numeric id, covering both URL schemes (`…-<id>.html` and the legacy
  `…/movie-watch-online-free-<id>.html`). Metadata is parsed from the magnet **`dn=` name** (authoritative,
  uniform across URL forms), size from `xl=` with a name-regex fallback. Stored in
  `media-bridge/config/movierulz.db` (sqlite), keyed by btih (`INSERT OR IGNORE` → stable `pubDate`).
- **Scope = movies + full-season packs only.** `S0X EP (a-b)` → `kind=series` with the title normalized
  to a Sonarr season-pack (`… S0X …`, EP-range dropped). Single episodes (`S04 EP09`) and "Part" forms
  (`S1P2`, `S1P-1-2`) are **skipped** — the season/part regexes also guard against mis-filing them as
  movies.
- **Torznab server** (`serve_torznab`): `t=caps` advertises movie + tv search; `t=search/movie/tvsearch`
  validate `MOVIERULZ_APIKEY` (Torznab error 100 on mismatch) and substring-match stored titles. Every
  `<item>` MUST carry a `<pubDate>` (Prowlarr drops items without it — the same bug the custom
  `mediafusion-api` binary patches) and emits a configurable `FAKE_SEEDERS` (movierulz exposes no
  seeders) so Radarr/Sonarr minimum-seeders doesn't filter everything.
- **Prowlarr wiring**: add as a generic Torznab indexer, `apiPath=/api`, apiKey `= MOVIERULZ_APIKEY`,
  baseUrl `http://media-bridge:8002/torznab` (Prowlarr is in gluetun's netns, which is on the default
  bridge, so it resolves the container name; fall back to the host LAN IP `:8002` if not).
