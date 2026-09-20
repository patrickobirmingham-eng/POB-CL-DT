"""
Tiny wrapper around ntfy.sh push notifications. Best-effort: if this fails for
any reason (no internet blip, ntfy.sh hiccup), it logs and moves on rather than
crashing the trading bot — a missed notification should never take down a live
session or block order management.
"""
import requests

import config


def send(title: str, message: str, priority: str = "default", tags: str = ""):
    if not getattr(config, "NTFY_ENABLED", False):
        return
    topic = getattr(config, "NTFY_TOPIC", "")
    if not topic:
        return
    try:
        requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": priority,  # "min","low","default","high","urgent"
                "Tags": tags,           # e.g. "chart_with_upwards_trend", "warning"
            },
            timeout=10,
        )
    except Exception as e:
        print(f"[notify] Failed to send push notification: {e}")
