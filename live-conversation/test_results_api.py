"""Allowlisted Live Conversation test runner and same-origin results UI."""
from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Callable
import uuid

from aiohttp import web


ROOT = Path(__file__).resolve().parent
REPOSITORY = ROOT.parent
DEFAULT_RESULTS_PATH = Path.home() / ".openclaw/state/live-conversation-test-results.json"
MAX_LOG_LINES = 300
NODE = shutil.which("node") or str(Path.home() / "nodejs/bin/node")


def _python(*args: str, env: dict[str, str] | None = None) -> dict:
    return {"argv": [sys.executable, *args], "env": env or {}}


SUITES = {
    "smoke": {
        "label": "Service smoke",
        "description": "Checks deployed health, metrics, WebSocket safety, settings validation, and settings transport.",
        "kind": "fast",
        "steps": [
            {"label": "Deployed service gate", **_python(str(ROOT / "production_gate.py"), "http://127.0.0.1:8790/")},
            {"label": "Settings tests", **_python("-m", "unittest", "discover", "-s", str(ROOT), "-p", "test_settings*.py", "-v")},
        ],
    },
    "browser": {
        "label": "Browser UI",
        "description": "Exercises the Live Conversation and settings pages in headless Chromium.",
        "kind": "fast",
        "steps": [
            {"label": "Conversation page", "argv": [NODE, str(REPOSITORY / "scripts/test-live-conversation-app.mjs")], "env": {}},
            {"label": "Conversation turn UI", "argv": [NODE, str(REPOSITORY / "scripts/test-live-conversation-turns.mjs")], "env": {}},
            {"label": "Test results page", "argv": [NODE, str(REPOSITORY / "scripts/test-live-conversation-tests.mjs")], "env": {}},
        ],
    },
    "unit": {
        "label": "Complete automated suite",
        "description": "Runs every Python Live Conversation regression, including audio fixtures when local assets are available.",
        "kind": "thorough",
        "steps": [
            {"label": "Python regression suite", **_python("-m", "unittest", "discover", "-s", str(ROOT), "-p", "test_*.py", "-v")},
        ],
    },
    "qwen": {
        "label": "Live Qwen GPU acceptance",
        "description": "Generates real Vivian audio, checks latency and cadence, then verifies intelligibility with Whisper.",
        "kind": "live",
        "steps": [
            {"label": "Qwen synthesis and Whisper acceptance", **_python(
                "-m", "unittest", str(ROOT / "test_qwen_tts.py"), "-v",
                env={"LIVE_CONVERSATION_QWEN_TTS_TEST_URL": "http://127.0.0.1:8792/v1/audio/speech"},
            )},
        ],
    },
    "conversation": {
        "label": "Live conversation matrix",
        "description": "Sends synthesized speech through the deployed WebSocket and validates ASR, routing, replies, audio, pauses, and barge-in.",
        "kind": "live",
        "steps": [
            {"label": "End-to-end audio matrix", **_python(str(ROOT / "e2e_conversation_matrix.py"), "--url", "http://127.0.0.1:8790/ws")},
        ],
    },
}

SUITES["release"] = {
    "label": "Full release validation",
    "description": "Runs all automated, browser, real-Qwen, and deployed conversation gates in release order.",
    "kind": "release",
    "steps": [step for suite_id in ("smoke", "browser", "unit", "qwen", "conversation") for step in SUITES[suite_id]["steps"]],
}


def catalog() -> list[dict]:
    return [
        {"id": suite_id, "label": suite["label"], "description": suite["description"],
         "kind": suite["kind"], "steps": [step["label"] for step in suite["steps"]]}
        for suite_id, suite in SUITES.items()
    ]


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class TestRunner:
    def __init__(self, results_path: str | Path = DEFAULT_RESULTS_PATH):
        self.results_path = Path(results_path)
        self.current: dict | None = None
        self.history: deque[dict] = deque(maxlen=20)
        self.task: asyncio.Task | None = None
        self.process: asyncio.subprocess.Process | None = None
        self._load()

    def _load(self) -> None:
        try:
            saved = json.loads(self.results_path.read_text())
            if isinstance(saved.get("history"), list):
                self.history.extend(saved["history"][-20:])
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def snapshot(self) -> dict:
        history = [{key: value for key, value in run.items() if key != "log"}
                   for run in reversed(self.history)]
        return {"suites": catalog(), "current": self.current, "history": history}

    async def start(self, suite_id: str) -> dict:
        if suite_id not in SUITES:
            raise ValueError("Unknown test suite.")
        if self.task and not self.task.done():
            raise RuntimeError("A test run is already in progress.")
        suite = SUITES[suite_id]
        self.current = {
            "id": uuid.uuid4().hex, "suite": suite_id, "label": suite["label"], "status": "running",
            "started_at": _timestamp(), "finished_at": None, "step_index": 0,
            "step_count": len(suite["steps"]), "step": suite["steps"][0]["label"], "steps": [], "log": [],
        }
        self._persist()
        self.task = asyncio.create_task(self._run(suite), name="live-conversation-test-run")
        return self.current

    async def _run(self, suite: dict) -> None:
        assert self.current is not None
        for index, step in enumerate(suite["steps"], start=1):
            self.current["step_index"] = index
            self.current["step"] = step["label"]
            result = {"label": step["label"], "status": "running", "started_at": _timestamp(), "finished_at": None, "exit_code": None}
            self.current["steps"].append(result)
            self._append_log(f"\n▶ {step['label']}")
            self._persist()
            # Unit/default tests must not inherit deployment overrides from the
            # long-running service process. Live suites add back only the
            # allowlisted variables they require.
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(("LIVE_CONVERSATION_", "QWEN_TTS_"))}
            env.update(step.get("env", {}))
            env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            env["PYTHONUNBUFFERED"] = "1"
            try:
                self.process = await asyncio.create_subprocess_exec(
                    *step["argv"], cwd=REPOSITORY, env=env,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                )
                assert self.process.stdout is not None
                while line := await self.process.stdout.readline():
                    self._append_log(line.decode("utf-8", "replace").rstrip())
                code = await self.process.wait()
            except asyncio.CancelledError:
                if self.process and self.process.returncode is None:
                    self.process.terminate()
                    await self.process.wait()
                result.update(status="cancelled", finished_at=_timestamp())
                self.current["status"] = "cancelled"
                self.current["finished_at"] = _timestamp()
                self._finish()
                raise
            except Exception as error:
                code = 127
                self._append_log(f"Runner error: {type(error).__name__}: {error}")
            finally:
                self.process = None
            result.update(status="passed" if code == 0 else "failed", finished_at=_timestamp(), exit_code=code)
            self._persist()
            if code:
                self.current["status"] = "failed"
                self.current["finished_at"] = _timestamp()
                self._finish()
                return
        self.current["status"] = "passed"
        self.current["finished_at"] = _timestamp()
        self._finish()

    def _append_log(self, line: str) -> None:
        assert self.current is not None
        self.current["log"].append(line[:1200])
        self.current["log"] = self.current["log"][-MAX_LOG_LINES:]

    def _finish(self) -> None:
        assert self.current is not None
        self.history.append(dict(self.current))
        self._persist()

    def _persist(self) -> None:
        try:
            self.results_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = self.results_path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"history": list(self.history)}, indent=2) + "\n")
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.results_path)
        except OSError:
            pass

    async def close(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass


def install(app: web.Application, origin_matches: Callable[[web.Request], bool], results_path: str | Path = DEFAULT_RESULTS_PATH) -> TestRunner:
    runner = TestRunner(results_path)
    root = Path(__file__).parent

    async def page(request: web.Request) -> web.StreamResponse:
        name = "tests.js" if request.path.endswith(".js") else "tests.html"
        return web.FileResponse(root / name, headers={"Cache-Control": "no-store"})

    async def results(request: web.Request) -> web.Response:
        if not origin_matches(request):
            return web.json_response({"error": "Test results require the same origin."}, status=403)
        return web.json_response(runner.snapshot(), headers={"Cache-Control": "no-store"})

    async def start(request: web.Request) -> web.Response:
        if not origin_matches(request) or request.content_type != "application/json":
            return web.json_response({"error": "Same-origin JSON request required."}, status=403)
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {"suite"} or not isinstance(body["suite"], str):
                raise ValueError("Expected one suite identifier.")
            run = await runner.start(body["suite"])
            return web.json_response(run, status=202)
        except (ValueError, json.JSONDecodeError) as error:
            return web.json_response({"error": str(error)}, status=400)
        except RuntimeError as error:
            return web.json_response({"error": str(error)}, status=409)

    app.router.add_get("/tests", page)
    app.router.add_get("/tests.js", page)
    app.router.add_get("/api/tests", results)
    app.router.add_post("/api/tests/runs", start)
    return runner
