"""Push notifications via ntfy. No-op unless CCR_NTFY_URL and CCR_NTFY_TOPIC are set."""
from __future__ import annotations

import logging

import httpx

from . import config

log = logging.getLogger("ccremote.notify")


async def push(title: str, body: str, priority: str = "default", tags: str = "") -> None:
    if not (config.NTFY_URL and config.NTFY_TOPIC):
        return
    headers = {"Title": title, "Priority": priority}
    if tags:
        headers["Tags"] = tags
    if config.NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {config.NTFY_TOKEN}"
    if config.PUBLIC_URL:
        headers["Click"] = config.PUBLIC_URL
    url = f"{config.NTFY_URL}/{config.NTFY_TOPIC}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, content=body.encode("utf-8"), headers=headers)
    except Exception as exc:  # notifications must never break a run
        log.warning("ntfy push failed: %s", exc)
