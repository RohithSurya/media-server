#!/usr/bin/env python3
"""
media-bridge entrypoint.

Runs three formerly-separate custom services as supervised daemon threads inside
one container (all off-VPN, on the default bridge network):
  * seedr    -- Seedr <-> Sonarr/Radarr blackhole bridge   (30s poll)
  * reaper   -- Jellyfin "watched movie" -> Radarr reaper   (6h)
  * movierulz-- 5movierulz crawler + Torznab feed for Prowlarr (crawl ~1h, HTTP server)

Each unit runs in its own thread wrapped so a crash logs + ntfy-alerts + restarts
without taking down the others (threads are I/O-bound; requests releases the GIL,
so seedr's large blocking downloads don't stall the Torznab server or crawler).
"""
import threading
import time

import seedr
import reaper
import movierulz
from common import clog, ntfy


def supervise(name, target):
    def wrap():
        while True:
            try:
                target()
                # run_forever() loops internally; returning means it stopped cleanly
                clog("main", f"{name} returned unexpectedly; restarting in 30s")
            except Exception as e:
                clog("main", f"{name} crashed: {e!r}; restarting in 30s")
                ntfy(f"media-bridge: {name} crashed", repr(e), priority="high", tags="warning")
            time.sleep(30)

    t = threading.Thread(target=wrap, name=name, daemon=True)
    t.start()
    return t


def main():
    clog("main", "media-bridge starting: seedr + reaper + movierulz")
    supervise("seedr", seedr.run_forever)
    supervise("reaper", reaper.run_forever)
    supervise("movierulz-crawler", movierulz.run_crawler)
    supervise("movierulz-torznab", movierulz.serve_torznab)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
