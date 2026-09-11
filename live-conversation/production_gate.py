#!/usr/bin/env python3
"""Non-destructive production smoke gate for a deployed Live Conversation service."""

from __future__ import annotations

import argparse
import asyncio
import json
from urllib.parse import urljoin, urlsplit, urlunsplit

import aiohttp


def websocket_url(base_url: str, path: str = "ws") -> str:
    parsed = urlsplit(urljoin(base_url.rstrip("/") + "/", path))
    return urlunsplit(("wss" if parsed.scheme == "https" else "ws", parsed.netloc, parsed.path, "", ""))


async def run(base_url: str) -> None:
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(urljoin(base_url.rstrip("/") + "/", "health")) as response:
            response.raise_for_status()
            health = await response.json()
        if not health.get("ok"):
            raise RuntimeError(f"service is not ready: {health.get('status')}")
        serialized = json.dumps(health)
        for forbidden in ("/home/", "session_key", "debug_status", "bundle_path"):
            if forbidden in serialized:
                raise RuntimeError(f"health leaked private field: {forbidden}")

        async with session.get(urljoin(base_url.rstrip("/") + "/", "metrics")) as response:
            response.raise_for_status()
            metric_text = await response.text()
        if "openclaw_live_conversation_uptime_seconds" not in metric_text:
            raise RuntimeError("metrics endpoint is missing uptime")

        async with session.ws_connect(websocket_url(base_url), heartbeat=10) as socket:
            initial = [await socket.receive_json(), await socket.receive_json()]
            if {item.get("type") for item in initial} != {"history", "settings"}:
                raise RuntimeError(f"unexpected initial websocket messages: {initial}")
            await socket.send_str("not-json")
            invalid = await socket.receive_json()
            if invalid.get("type") != "error" or "Invalid JSON" not in invalid.get("message", ""):
                raise RuntimeError(f"malformed message was not safely rejected: {invalid}")
    print(f"production gate passed: {base_url}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url", nargs="?", default="http://127.0.0.1:8790/")
    args = parser.parse_args()
    asyncio.run(run(args.base_url))


if __name__ == "__main__":
    main()
