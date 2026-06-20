# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A self-hosted media automation stack defined by a single `docker-compose.yaml`, running on a
**Jetson Nano Super (aarch64)**. Pipeline: **search** (Prowlarr) → **automate** (Sonarr TV / Radarr
movies) → **cloud download** (Seedr) → **stream** (Jellyfin). Two services are custom Python and live
in this repo (`seedr-bridge/`, `jellyfin-reaper/`); everything else is an off-the-shelf image. Service
config/state directories (`prowlarr/`, `radarr/`, `sonarr/`, `bazarr/`, `jellyfin/`, `data/`) are
runtime data and are gitignored — only the compose file and the two custom services are version-controlled.

## Commands

```bash
docker compose up -d                              # bring up / apply compose changes
docker compose up -d --build seedr-bridge         # rebuild a custom service after editing its .py
docker compose logs -f seedr-bridge               # follow a service's logs
docker restart prowlarr flaresolverr sonarr radarr  # REQUIRED after any gluetun restart (see below)
```

There is no build/lint/test tooling — the Python services are single-file scripts run directly
(`CMD ["python", "-u", "<script>.py"]`). To exercise a change, rebuild the service and watch its logs.
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
  (or `docker compose down && up -d`). `seedr-bridge` and `jellyfin` are unaffected.
- **Cross-service URLs inside the netns**: containers sharing gluetun's namespace talk to the *arr
  apps via the hostname **`gluetun`** (e.g. `http://gluetun:7878`), NOT `radarr`/`sonarr` (which don't
  resolve). `seedr-bridge` reaches them this way; `jellyfin-reaper` reaches host-net Jellyfin via
  `host.docker.internal` (`extra_hosts: host-gateway`).
- **FlareSolverr must share gluetun's exit IP** (it's in the same netns) because `cf_clearance`
  cookies are IP-bound. CF-protected indexers are wired with a `flaresolverr` tag in Prowlarr.

What runs **outside** the VPN: `seedr-bridge` (Seedr does the torrenting in its cloud, so the Jetson
never joins a swarm), `bazarr` and `jellyfin` (both `network_mode: host` for LAN/DLNA/Tailscale reach;
they hit the *arr apps on `127.0.0.1` because gluetun publishes those ports on the host).

## The `/data` single-root convention

`./data` is mounted into sonarr, radarr, seedr-bridge, and bazarr at the same path so imports are
hardlink-friendly and paths map 1:1 across services. Layout:
- `data/blackhole/{sonarr,radarr}/` — Sonarr/Radarr (configured with a **Torrent Blackhole** download
  client, since Seedr has no native *arr connector) drop `<release>.magnet` files here.
- `data/downloads/{sonarr,radarr}/` — seedr-bridge writes finished downloads here; the *arr
  watch-folder scan imports them.
- `data/media/{tv,movies}` — the Jellyfin library, mounted read-only into jellyfin.
- `data/blackhole/.bridge-state/` — seedr-bridge per-transfer state JSON (one file per transfer).

## seedr-bridge (`seedr-bridge/bridge.py`)

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

## jellyfin-reaper (`jellyfin-reaper/reaper.py`)

Polling loop (default every 6h) that reclaims disk: deletes a movie once **every** Jellyfin user has
Played it AND `GRACE_DAYS` (default 15) have elapsed since the most recent watch. Matches Jellyfin
items to Radarr movies by TMDB id, and deletes **through Radarr's API** (`deleteFiles=true`) so the
file, folder, Bazarr `.srt` sidecars, and DB entry go together — never `rm` on `/data/media`.

Safety guards (keep intact): `DRY_RUN` defaults **true** (flip to false in compose only after verifying
the dry-run list); any Jellyfin/user API failure or zero users **aborts the whole run** (never assume
"watched"); `MAX_DELETES_PER_RUN` caps blast radius; only movies with `hasFile` are considered.
