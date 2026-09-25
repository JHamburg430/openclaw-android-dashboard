"""Nemotron-only Live Conversation operations server.

This replaces the legacy Pipecat/Qwen process as the service behind port 8790.
The actual realtime Talk provider remains the Gateway plugin on port 8793;
this small server is its private monitoring, control, and validation console.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from aiohttp import web

import nemotron_control


ROOT = Path(__file__).resolve().parent


def origin_matches(request: web.Request) -> bool:
    origin = request.headers.get("Origin")
    if not origin:
        return True
    host = request.headers.get("Host", "")
    return origin in {f"http://{host}", f"https://{host}"}


async def health(request: web.Request) -> web.Response:
    status = await nemotron_control.snapshot()
    provider = status["provider"]
    provider_health = status["provider_health"]
    gateway = status["gateway"]
    ok = (provider.get("ActiveState") == "active" and provider_health.get("status") == "ok"
          and gateway.get("ok") is True and gateway.get("nemotron_loaded") is True)
    return web.json_response({"ok": ok, "status": "healthy" if ok else "degraded", "mode": "nemotron", "provider": provider_health, "gateway": gateway})


async def index(_: web.Request) -> web.StreamResponse:
    return web.FileResponse(ROOT / "nemotron.html", headers={"Cache-Control": "no-store"})


@web.middleware
async def security_headers(request: web.Request, handler):
    response = await handler(request)
    response.headers.update({
        "Content-Security-Policy": "default-src 'self'; connect-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Permissions-Policy": "camera=(), geolocation=(), payment=(), usb=()",
        "Cache-Control": response.headers.get("Cache-Control", "no-store"),
        "Server": "OpenClaw Nemotron Control",
    })
    return response


async def build_app() -> web.Application:
    app = web.Application(middlewares=[security_headers])
    jobs = nemotron_control.install(app, origin_matches)
    app.router.add_get("/", index)
    app.router.add_get("/health", health)

    async def cleanup(_: web.Application) -> None:
        await jobs.close()

    app.on_cleanup.append(cleanup)
    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args()
    web.run_app(build_app(), host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
