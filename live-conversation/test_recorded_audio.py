"""Optional regression gate over consented real Live Conversation captures.

Set ``LIVE_CONVERSATION_TEST_RECORDINGS`` to the capture root. The production
release suite can then replay recent phone microphone WAVs through Whisper and
compare them with the transcript stored when each turn was captured.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import unittest
import wave

from pipecat.services.whisper.stt import WhisperSTTService
from pipecat.transcriptions.language import Language

from server import LiveConversationService, SAMPLE_RATE


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9']+", text.casefold()))


class RecordedPhoneAudioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        configured = os.environ.get("LIVE_CONVERSATION_TEST_RECORDINGS", "").strip()
        if not configured:
            raise unittest.SkipTest(
                "set LIVE_CONVERSATION_TEST_RECORDINGS to replay consented phone captures"
            )
        cls.root = Path(configured)
        phone_manifests = []
        for manifest_path in sorted(cls.root.glob("*/*.json"), reverse=True):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            source = str(manifest.get("capture_source") or "")
            user_agent = str((manifest.get("client_context") or {}).get("user_agent") or "")
            if source == "android_live_microphone" or "android" in user_agent.casefold():
                phone_manifests.append(manifest_path)
            if len(phone_manifests) >= 20:
                break
        cls.manifests = list(reversed(phone_manifests))
        if not cls.manifests:
            raise unittest.SkipTest(
                f"no Android microphone Live Conversation captures in {cls.root}"
            )
        cls.stt = WhisperSTTService(
            settings=WhisperSTTService.Settings(
                model="small.en", language=Language.EN, no_speech_prob=0.6
            ),
            device="cpu",
            compute_type="int8",
        )

    def test_captured_phone_audio_remains_decodable(self) -> None:
        service = LiveConversationService("agent:main:recorded-phone-audio", 1.15)
        service.stt = self.stt
        service.stt_description = "faster-whisper small.en recorded-phone regression"

        async def replay() -> None:
            for manifest_path in self.manifests:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                wav_path = manifest_path.parent / manifest["user_audio"]
                with self.subTest(capture=manifest["capture_id"]):
                    with wave.open(str(wav_path), "rb") as recording:
                        self.assertEqual(recording.getnchannels(), 1)
                        self.assertEqual(recording.getsampwidth(), 2)
                        self.assertEqual(recording.getframerate(), SAMPLE_RATE)
                        pcm = recording.readframes(recording.getnframes())
                    expected = str(manifest.get("transcript", "")).strip()
                    actual = await service.transcribe(pcm, purpose="recorded-regression")
                    if manifest.get("status") in {"no_clear_speech", "ignored_ambient"}:
                        self.assertLessEqual(
                            len(_words(actual)), 2,
                            f"background-only phone capture hallucinated speech: {actual!r}",
                        )
                        continue
                    self.assertTrue(actual, "real phone capture became undecodable")
                    if expected:
                        expected_words = _words(expected)
                        overlap = len(expected_words & _words(actual)) / max(1, len(expected_words))
                        self.assertGreaterEqual(
                            overlap, 0.70,
                            f"transcript drifted: expected={expected!r} actual={actual!r}",
                        )

        asyncio.run(replay())


if __name__ == "__main__":
    unittest.main()
