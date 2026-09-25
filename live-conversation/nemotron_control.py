"""Nemotron operations API for the Live Conversation control surface.

Only fixed, local commands are exposed.  The browser can inspect the provider,
Gateway, effective Talk routing, and recent service logs, and can operate the
dedicated Nemotron user service without gaining arbitrary shell access.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import time
from typing import Callable

from aiohttp import web


ROOT = Path(__file__).resolve().parent
PLUGIN_ROOT = Path.home() / ".openclaw/plugins/nemotron-realtime-voice"
CONFIG_PATH = Path.home() / ".openclaw/openclaw.json"
PROVIDER_UNIT = "openclaw-nemo-voicechat.service"
LEGACY_UNIT = "openclaw-live-conversation.service"
NODE = str(Path.home() / "nodejs/bin/node")


async def _run(*argv: str, cwd: Path | None = None, timeout: float = 8.0) -> tuple[int, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            *argv, cwd=str(cwd) if cwd else None,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env=os.environ.copy(),
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return 124, "Command timed out."
        return process.returncode or 0, output.decode("utf-8", "replace")[-100000:]
    except OSError as error:
        return 127, f"{type(error).__name__}: {error}"


async def _json_command(*argv: str, timeout: float = 8.0) -> dict:
    code, output = await _run(*argv, timeout=timeout)
    try:
        # Some CLI builds emit a short diagnostic on stderr before the JSON
        # document.  Decode the first complete object instead of treating that
        # harmless prefix as a Gateway failure.
        start = output.find("{")
        value, _ = json.JSONDecoder().raw_decode(output[start:]) if start >= 0 else (None, 0)
        return value if isinstance(value, dict) else {"value": value}
    except (ValueError, TypeError):
        return {"ok": code == 0, "error": output.strip()[-2000:] or f"exit {code}"}


def _config_snapshot() -> dict:
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        realtime = config.get("talk", {}).get("realtime", {})
        provider = realtime.get("providers", {}).get("nemotron-realtime-voice", {})
        return {
            "mode": realtime.get("mode"),
            "brain": realtime.get("brain"),
            "transport": realtime.get("transport"),
            "provider": realtime.get("provider"),
            "consultRouting": realtime.get("consultRouting"),
            "serverUrl": provider.get("serverUrl"),
            "ackMessages": realtime.get("ackMessages", []),
            "emitUserTranscript": realtime.get("emitUserTranscript"),
            "emitAssistantTranscript": realtime.get("emitAssistantTranscript"),
        }
    except (OSError, ValueError, TypeError) as error:
        return {"error": f"Could not read OpenClaw Talk configuration: {error}"}


async def _unit(unit: str) -> dict:
    code, output = await _run("systemctl", "--user", "show", unit, "--no-pager",
                              "--property=ActiveState,SubState,MainPID,ExecMainStartTimestamp,UnitFileState")
    values = {}
    for line in output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return {"unit": unit, "available": code == 0, **values, "error": None if code == 0 else output.strip()}


async def snapshot() -> dict:
    provider, legacy, health, gateway = await asyncio.gather(
        _unit(PROVIDER_UNIT), _unit(LEGACY_UNIT),
        _json_command("curl", "-fsS", "--max-time", "3", "http://127.0.0.1:8793/health"),
        _json_command(str(Path.home() / "nodejs/bin/openclaw"), "health", "--json", timeout=10),
    )
    plugin_code, plugin_output = await _run("git", "-C", str(PLUGIN_ROOT), "log", "-1", "--format=%h %s")
    return {
        "generated_at": time.time(),
        "provider": provider,
        "legacy": legacy,
        "provider_health": health,
        "gateway": {
            "ok": gateway.get("ok") is True,
            "error": gateway.get("error"),
            "event_loop": gateway.get("eventLoop", {}),
            "nemotron_loaded": "nemotron-realtime-voice" in gateway.get("plugins", {}).get("loaded", []),
            "plugin_count": len(gateway.get("plugins", {}).get("loaded", [])),
            "plugin_errors": gateway.get("plugins", {}).get("errors", []),
        },
        "talk": _config_snapshot(),
        "plugin_revision": plugin_output.strip() if plugin_code == 0 else "unknown",
        "migration": {
            "target": "Nemotron realtime voice",
            "provider_unit": PROVIDER_UNIT,
            "legacy_unit": LEGACY_UNIT,
            "legacy_should_be_stopped": True,
        },
    }


COMMANDS = {
    "provider-start": ("systemctl", "--user", "start", PROVIDER_UNIT),
    "provider-stop": ("systemctl", "--user", "stop", PROVIDER_UNIT),
    "provider-restart": ("systemctl", "--user", "restart", PROVIDER_UNIT),
    "legacy-stop": ("systemctl", "--user", "stop", LEGACY_UNIT),
}

TESTS = {
    "provider-health": ("curl", "-fsS", "--max-time", "5", "http://127.0.0.1:8793/health"),
    "gateway-health": (str(Path.home() / "nodejs/bin/openclaw"), "health", "--json"),
    "plugin-tests": ("npm", "test", "--", "--run"),
    "relay-tests": (NODE, "scripts/test-talk-relay-patch.mjs"),
    "native-input-tests": (NODE, "scripts/test-native-input-compat.mjs"),
}


class NemotronJobs:
    def __init__(self) -> None:
        self.current: dict | None = None
        self.task: asyncio.Task | None = None

    def status(self) -> dict:
        return self.current or {"state": "idle"}

    async def start(self, kind: str, name: str) -> dict:
        commands = COMMANDS if kind == "action" else TESTS
        if name not in commands:
            raise ValueError("Unknown Nemotron operation.")
        if self.task and not self.task.done():
            raise RuntimeError("Another Nemotron operation is already running.")
        self.current = {"state": "running", "kind": kind, "name": name, "started_at": time.time(), "output": ""}
        self.task = asyncio.create_task(self._run(commands[name]), name="nemotron-operation")
        return self.current

    async def _run(self, command: tuple[str, ...]) -> None:
        cwd = PLUGIN_ROOT if command[0] == "npm" else ROOT.parent
        code, output = await _run(*command, cwd=cwd, timeout=120 if command[0] in ("npm", NODE) else 15)
        if self.current:
            self.current.update(state="passed" if code == 0 else "failed", exit_code=code,
                                finished_at=time.time(), output=output[-12000:])

    async def close(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass


def install(app: web.Application, origin_matches: Callable[[web.Request], bool]) -> NemotronJobs:
    jobs = NemotronJobs()
    root = Path(__file__).parent

    async def page(request: web.Request) -> web.StreamResponse:
        name = ("nemotron.js" if request.path.endswith(".js") else
                "nemotron.css" if request.path.endswith(".css") else "nemotron.html")
        return web.FileResponse(root / name, headers={"Cache-Control": "no-store"})

    async def status(request: web.Request) -> web.Response:
        if not origin_matches(request):
            return web.json_response({"error": "Nemotron control requires the same origin."}, status=403)
        value = await snapshot()
        value["job"] = jobs.status()
        return web.json_response(value, headers={"Cache-Control": "no-store"})

    async def logs(request: web.Request) -> web.Response:
        if not origin_matches(request):
            return web.json_response({"error": "Nemotron control requires the same origin."}, status=403)
        code, output = await _run("journalctl", "--user", "-u", PROVIDER_UNIT, "-n", "100", "--no-pager", "-o", "short-iso")
        return web.json_response({"ok": code == 0, "text": output})

    async def operation(request: web.Request) -> web.Response:
        if not origin_matches(request) or request.content_type != "application/json":
            return web.json_response({"error": "Same-origin JSON request required."}, status=403)
        try:
            body = await request.json()
            if not isinstance(body, dict) or body.keys() != {"kind", "name"} or body["kind"] not in ("action", "test"):
                raise ValueError("Expected kind and name.")
            result = await jobs.start(body["kind"], body["name"])
            return web.json_response(result, status=202)
        except (ValueError, json.JSONDecodeError) as error:
            return web.json_response({"error": str(error)}, status=400)
        except RuntimeError as error:
            return web.json_response({"error": str(error)}, status=409)

    app.router.add_get("/nemotron", page)
    app.router.add_get("/nemotron.js", page)
    app.router.add_get("/nemotron.css", page)
    app.router.add_get("/api/nemotron/status", status)
    app.router.add_get("/api/nemotron/logs", logs)
    app.router.add_post("/api/nemotron/operations", operation)
    app["nemotron_jobs"] = jobs
    return jobs
