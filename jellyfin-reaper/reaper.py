#!/usr/bin/env python3
"""
Jellyfin -> Radarr "watched movie" reaper.

Reclaims disk by deleting a movie once EVERY Jellyfin user has finished it and a
grace window has elapsed. Deletion goes through Radarr's API (deleteFiles=true) so
the file, its folder (incl. Bazarr .srt sidecars) and the Radarr DB entry are all
removed together -- the clean, hardlink-aware path. We never `rm` /data/media.

Policy (env-tunable):
  * eligible        = movie has Played=true for ALL quorum users
  * grace           = delete only GRACE_DAYS after the MOST RECENT watch across users
                      (a re-watch updates LastPlayedDate, so it resets the clock)
  * match           = Jellyfin item ProviderIds.Tmdb  <->  Radarr movie tmdbId
  * scope           = all movies in the library (no opt-in tag); TV untouched

Safety guards:
  * DRY_RUN (default true)        -> only logs/notifies "WOULD delete", changes nothing
  * abort-on-uncertainty         -> if Jellyfin/users unreachable or zero users, skip
                                    the whole run (never assume "watched")
  * MAX_DELETES_PER_RUN          -> caps blast radius if something mis-flags many movies
  * hasFile gate                 -> ignore file-less / unmonitored Radarr entries

Jellyfin auth: header  X-Emby-Token: <JELLYFIN_API_KEY>
Radarr auth:   header  X-Api-Key: <RADARR_API_KEY>
"""
import os
import time
import datetime
import requests

# ---- config (mirrors seedr-bridge env-loading style) ----
JELLYFIN_URL = os.environ["JELLYFIN_URL"].rstrip("/")
JELLYFIN_API_KEY = os.environ["JELLYFIN_API_KEY"]
RADARR_URL = os.environ["RADARR_URL"].rstrip("/")
RADARR_API_KEY = os.environ["RADARR_API_KEY"]

GRACE_DAYS = int(os.environ.get("GRACE_DAYS", "15"))
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "21600"))   # 6 h
MAX_DELETES_PER_RUN = int(os.environ.get("MAX_DELETES_PER_RUN", "5"))
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() not in ("false", "0", "no")
# optional comma-sep list of user NAMES to form the "all users" quorum; default = all enabled users
USER_FILTER = [u.strip() for u in os.environ.get("JELLYFIN_USERS", "").split(",") if u.strip()]

NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")

JF = {"X-Emby-Token": JELLYFIN_API_KEY}
RA = {"X-Api-Key": RADARR_API_KEY}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def notify(title, body, tags="wastebasket"):
    if not NTFY_TOPIC:
        return
    try:
        requests.post(f"{NTFY_SERVER}/{NTFY_TOPIC}", data=body.encode(),
                      headers={"Title": title, "Priority": "default", "Tags": tags}, timeout=15)
    except Exception as e:
        log(f"    (warn) ntfy notify failed: {e}")


def parse_jf_date(s):
    """Jellyfin LastPlayedDate -> aware datetime, or None. Handles varying fractional digits/Z."""
    if not s:
        return None
    try:
        s = s.strip().replace("Z", "+00:00")
        # datetime.fromisoformat dislikes >6 fractional digits -> trim
        if "." in s:
            head, frac = s.split(".", 1)
            tz = ""
            for sep in ("+", "-"):
                if sep in frac:
                    frac, tzpart = frac.split(sep, 1)
                    tz = sep + tzpart
                    break
            frac = frac[:6]
            s = f"{head}.{frac}{tz}"
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt
    except Exception:
        return None


# ---- Jellyfin ----
def jf_users():
    """Enabled users forming the watch quorum. Raises on transport/HTTP error."""
    r = requests.get(f"{JELLYFIN_URL}/Users", headers=JF, timeout=30)
    r.raise_for_status()
    users = []
    for u in r.json():
        if u.get("Policy", {}).get("IsDisabled"):
            continue
        if USER_FILTER and u.get("Name") not in USER_FILTER:
            continue
        users.append((u["Id"], u.get("Name", u["Id"])))
    return users


def jf_played_movies(user_id):
    """{tmdb_id(int): last_played(datetime|None)} of movies this user has Played. Raises on error."""
    r = requests.get(
        f"{JELLYFIN_URL}/Users/{user_id}/Items",
        headers=JF,
        params={
            "IncludeItemTypes": "Movie",
            "Recursive": "true",
            "IsPlayed": "true",
            "Fields": "ProviderIds",
        },
        timeout=60,
    )
    r.raise_for_status()
    out = {}
    for it in r.json().get("Items", []):
        tmdb = it.get("ProviderIds", {}).get("Tmdb")
        if not tmdb:
            continue
        try:
            tmdb = int(tmdb)
        except (TypeError, ValueError):
            continue
        out[tmdb] = parse_jf_date(it.get("UserData", {}).get("LastPlayedDate"))
    return out


# ---- Radarr ----
def radarr_movies_by_tmdb():
    """{tmdb_id(int): movie} for Radarr movies that currently have a file."""
    r = requests.get(f"{RADARR_URL}/api/v3/movie", headers=RA, timeout=60)
    r.raise_for_status()
    return {m["tmdbId"]: m for m in r.json() if m.get("hasFile") and m.get("tmdbId")}


def delete_movie(movie_id):
    r = requests.delete(
        f"{RADARR_URL}/api/v3/movie/{movie_id}",
        headers=RA,
        params={"deleteFiles": "true", "addImportExclusion": "false"},
        timeout=60,
    )
    r.raise_for_status()


# ---- main logic ----
def reap():
    users = jf_users()
    if not users:
        log("no enabled Jellyfin users found -> skipping run (guard)")
        return
    log(f"watch quorum = {len(users)} user(s): {', '.join(n for _, n in users)}")

    # per-user played map; ANY failure aborts the whole run (never assume 'watched')
    per_user = {}
    for uid, name in users:
        per_user[uid] = jf_played_movies(uid)
        log(f"  {name}: {len(per_user[uid])} played movies")

    # movies played by EVERY user
    common = set.intersection(*(set(m.keys()) for m in per_user.values())) if per_user else set()
    radarr = radarr_movies_by_tmdb()

    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = datetime.timedelta(days=GRACE_DAYS)
    candidates = []  # (last_played, movie)
    for tmdb in common:
        movie = radarr.get(tmdb)
        if not movie:
            continue  # watched but not a current Radarr movie-with-file
        dates = [per_user[uid].get(tmdb) for uid in per_user]
        if any(d is None for d in dates):
            log(f"  skip '{movie.get('title')}' -> missing LastPlayedDate for a user")
            continue
        last_played = max(dates)
        age = now - last_played
        if age >= cutoff:
            candidates.append((last_played, movie))
        else:
            days_left = (cutoff - age).days
            log(f"  '{movie.get('title')}' watched by all, {days_left}d left in grace")

    candidates.sort(key=lambda c: c[0])  # oldest-watched first
    if not candidates:
        log("nothing eligible for deletion this run")
        return

    action = "WOULD delete" if DRY_RUN else "deleting"
    deleted = []
    for last_played, movie in candidates[:MAX_DELETES_PER_RUN]:
        title = f"{movie.get('title')} ({movie.get('year')})"
        log(f"  {action}: {title}  [watched all users by {last_played:%Y-%m-%d}]")
        if not DRY_RUN:
            try:
                delete_movie(movie["id"])
            except Exception as e:
                log(f"    (error) Radarr delete failed for {title}: {e}")
                continue
        deleted.append(title)

    capped = len(candidates) > MAX_DELETES_PER_RUN
    if deleted:
        verb = "WOULD delete" if DRY_RUN else "Deleted"
        body = "\n".join(f"- {t}" for t in deleted)
        if capped:
            body += f"\n(+{len(candidates) - MAX_DELETES_PER_RUN} more, capped this run)"
        notify(f"{verb} {len(deleted)} watched movie(s)", body)


def main():
    mode = "DRY-RUN (no deletions)" if DRY_RUN else "LIVE"
    log(f"jellyfin-reaper starting [{mode}]; grace={GRACE_DAYS}d, "
        f"interval={CHECK_INTERVAL}s, max/run={MAX_DELETES_PER_RUN}")
    while True:
        try:
            reap()
        except requests.RequestException as e:
            log(f"run aborted -- API unreachable, deleting nothing: {e}")
        except Exception as e:
            log(f"run error: {e}")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
