#!/usr/bin/env python3
"""Shared helpers for the media-bridge components (seedr, reaper, movierulz)."""
import os
import re
import time
import requests

NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")


def clog(tag, msg):
    """Timestamped, component-tagged log line (e.g. '[12:00:00] [seedr] ...')."""
    print(f"[{time.strftime('%H:%M:%S')}] [{tag}] {msg}", flush=True)


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def ntfy(title, body, priority="default", tags="information"):
    """Best-effort ntfy push; silently no-ops if NTFY_TOPIC is unset."""
    if not NTFY_TOPIC:
        return
    try:
        requests.post(f"{NTFY_SERVER}/{NTFY_TOPIC}", data=body.encode(),
                      headers={"Title": title, "Priority": priority, "Tags": tags}, timeout=15)
    except Exception as e:
        clog("ntfy", f"notify failed: {e}")
