"""Test-results page and allowlisted runner behavior without loading models."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import test_results_api


def same_origin(request):
    return request.headers.get("Origin") in (None, "http://127.0.0.1")


class TestResultsApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.results = Path(self.temp.name) / "results.json"
        self.app = web.Application()
        self.runner = test_results_api.install(self.app, same_origin, self.results)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.runner.close()
        await self.client.close()
        self.temp.cleanup()

    async def test_page_catalog_and_empty_results_load(self):
        page = await self.client.get("/tests")
        self.assertEqual(page.status, 200)
        self.assertIn("Run validation", await page.text())
        script = await self.client.get("/tests.js")
        self.assertEqual(script.status, 200)
        self.assertIn("/api/tests/runs", await script.text())
        response = await self.client.get("/api/tests")
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertEqual({item["id"] for item in payload["suites"]},
                         {"smoke", "browser", "unit", "qwen", "conversation", "release"})
        self.assertEqual(payload["history"], [])

    async def test_same_origin_json_and_allowlist_are_enforced(self):
        denied = await self.client.get("/api/tests", headers={"Origin": "https://untrusted.example"})
        self.assertEqual(denied.status, 403)
        denied = await self.client.post("/api/tests/runs", json={"suite": "smoke"},
                                        headers={"Origin": "https://untrusted.example"})
        self.assertEqual(denied.status, 403)
        bad_type = await self.client.post("/api/tests/runs", data="{}")
        self.assertEqual(bad_type.status, 403)
        unknown = await self.client.post("/api/tests/runs", json={"suite": "../../bin/sh"})
        self.assertEqual(unknown.status, 400)
        extra = await self.client.post("/api/tests/runs", json={"suite": "smoke", "command": "id"})
        self.assertEqual(extra.status, 400)

    async def test_run_streams_output_persists_result_and_rejects_overlap(self):
        suite = {
            "label": "Fixture", "description": "fixture", "kind": "fast",
            "steps": [{"label": "First", "argv": [sys.executable, "-c", "import time; print('started', flush=True); time.sleep(.15); print('done')"], "env": {}}],
        }
        with patch.dict(test_results_api.SUITES, {"fixture": suite}, clear=False):
            response = await self.client.post("/api/tests/runs", json={"suite": "fixture"})
            self.assertEqual(response.status, 202)
            overlap = await self.client.post("/api/tests/runs", json={"suite": "fixture"})
            self.assertEqual(overlap.status, 409)
            for _ in range(30):
                await asyncio.sleep(.03)
                payload = await (await self.client.get("/api/tests")).json()
                if payload["current"]["status"] != "running":
                    break
            self.assertEqual(payload["current"]["status"], "passed")
            self.assertIn("started", payload["current"]["log"])
            self.assertIn("done", payload["current"]["log"])
            self.assertEqual(payload["history"][0]["status"], "passed")
            self.assertNotIn("log", payload["history"][0])
            saved = json.loads(self.results.read_text())
            self.assertEqual(saved["history"][0]["steps"][0]["exit_code"], 0)
            self.assertEqual(self.results.stat().st_mode & 0o777, 0o600)

    async def test_subprocess_does_not_inherit_deployment_overrides(self):
        suite = {
            "label": "Environment", "description": "fixture", "kind": "fast",
            "steps": [{
                "label": "Inspect",
                "argv": [sys.executable, "-c", "import os; print(os.getenv('LIVE_CONVERSATION_TTS_BACKEND', 'clean'))"],
                "env": {},
            }],
        }
        with patch.dict(test_results_api.SUITES, {"environment": suite}, clear=False), \
             patch.dict("os.environ", {"LIVE_CONVERSATION_TTS_BACKEND": "qwen"}, clear=False):
            await self.runner.start("environment")
            await self.runner.task
        self.assertEqual(self.runner.current["status"], "passed")
        self.assertIn("clean", self.runner.current["log"])
        self.assertNotIn("qwen", self.runner.current["log"])


if __name__ == "__main__":
    unittest.main()
