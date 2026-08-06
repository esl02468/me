"""Free phone push via ntfy.sh — no account, no keys, no AI.

Setup (one minute):
  1. Pick a long random topic name, e.g. nq-esl-x7k2m9q4 (it IS the
     password — anyone who knows it can read/send).
  2. On your phone, install the ntfy app and subscribe to that topic.
  3. On the VPS: setx NQ_NTFY_TOPIC nq-esl-x7k2m9q4   (then restart the task)

Pushes are best-effort and silent on failure — trading never blocks on a
notification.
"""

from __future__ import annotations

import os
import urllib.request

TOPIC = os.environ.get("NQ_NTFY_TOPIC", "").strip()
SERVER = os.environ.get("NQ_NTFY_SERVER", "https://ntfy.sh").rstrip("/")


def enabled() -> bool:
    return bool(TOPIC)


def push(message: str, title: str = "NQ toolkit", priority: str = "default") -> bool:
    if not TOPIC:
        return False
    try:
        req = urllib.request.Request(
            f"{SERVER}/{TOPIC}",
            data=message.encode(),
            headers={"Title": title, "Priority": priority},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8):
            return True
    except OSError:
        return False
