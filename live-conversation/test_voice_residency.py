"""Opt-in deployed runner-loss acceptance test; run only between conversations.

LIVE_CONVERSATION_TEST_RECOVERY=1 python -m unittest test_voice_residency.py
Unloads only the dedicated voice model, then verifies automatic GPU recovery.
"""

import asyncio
import os
import time
import unittest

import aiohttp

from server import SPEECH_MODEL, SPEECH_MODEL_URL, is_model_fully_accelerated


@unittest.skipUnless(os.environ.get("LIVE_CONVERSATION_TEST_RECOVERY") == "1",
                     "opt in to the deployed voice-runner loss test")
class VoiceResidencyAcceptance(unittest.IsolatedAsyncioTestCase):
    async def test_unloaded_model_recovers_without_audio_service_restart(self):
        model_base = SPEECH_MODEL_URL.rsplit("/api/chat", 1)[0]
        health_url = "http://127.0.0.1:8790/health"
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15)
        ) as session:
            async def get_json(url):
                async with session.get(url) as response:
                    response.raise_for_status()
                    return await response.json()

            before = await get_json(health_url)
            self.assertTrue(before["ok"], "start with a ready voice service")
            started = time.monotonic()
            async with session.post(model_base + "/api/generate", json={
                "model": SPEECH_MODEL, "keep_alive": 0,
            }) as response:
                response.raise_for_status()
                await response.read()
            # Ollama acknowledges unload before the runner has exited.
            while time.monotonic() - started < 10:
                unloaded = await get_json(model_base + "/api/ps")
                if not any(m.get("name") == SPEECH_MODEL
                           for m in unloaded.get("models", [])):
                    break
                await asyncio.sleep(0.1)
            else:
                self.fail("Ollama did not unload the voice runner")
            while time.monotonic() - started < 30:
                models = await get_json(model_base + "/api/ps")
                current = await get_json(health_url)
                resident = next((m for m in models.get("models", [])
                                 if m.get("name") == SPEECH_MODEL), {})
                if is_model_fully_accelerated(resident) and current["ok"]:
                    self.assertGreaterEqual(current["uptime_seconds"],
                                            before["uptime_seconds"])
                    self.assertEqual(current["speech_router"]["mode"], "gpu")
                    # Ollama represents an indefinite lifetime centuries ahead.
                    self.assertGreater(int(resident["expires_at"][:4]), 2100)
                    print(f"Voice GPU recovery: {time.monotonic() - started:.2f}s")
                    return
                await asyncio.sleep(0.5)
            self.fail("dedicated voice model did not recover on GPU within 30s")
