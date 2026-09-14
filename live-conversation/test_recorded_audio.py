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

import numpy as np
from pipecat.services.whisper.stt import WhisperSTTService
from pipecat.transcriptions.language import Language

from server import (
    LiveConversationService,
    SAMPLE_RATE,
    SPOKEN_BACKCHANNELS_ENABLED,
    is_gateway_status_question,
)


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
        cls.all_manifests = list(reversed(phone_manifests))
        cls.manifests = cls.all_manifests[-20:]
        if not cls.all_manifests:
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

    def test_quiet_android_sentence_ending_is_preserved(self) -> None:
        """Replay the debug capture that originally lost its requested subject."""
        capture_id = "20260910T190707.340433-4-6f85dc"
        manifest_path = next(
            (path for path in self.all_manifests if path.stem == capture_id), None
        )
        if manifest_path is None:
            self.skipTest(f"captured Android regression audio {capture_id} is unavailable")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        wav_path = manifest_path.parent / manifest["user_audio"]

        async def replay() -> str:
            service = LiveConversationService("agent:main:quiet-ending-regression", 1.15)
            service.stt = self.stt
            with wave.open(str(wav_path), "rb") as recording:
                pcm = recording.readframes(recording.getnframes())
            return await service.transcribe(pcm, purpose="final")

        actual = asyncio.run(replay())
        words = _words(actual)
        self.assertTrue(
            {"live", "conversation", "agent"}.issubset(words),
            f"quiet requested subject was clipped from captured phone audio: {actual!r}",
        )

    def test_status_indicator_delegation_is_not_a_gateway_status_question(self) -> None:
        """Replay the capture whose UI wording triggered the status shortcut."""
        capture_id = "20260910T193957.715784-2-95bafe"
        manifest_path = next(
            (path for path in self.all_manifests if path.stem == capture_id), None
        )
        if manifest_path is None:
            self.skipTest(f"captured Android regression audio {capture_id} is unavailable")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        wav_path = manifest_path.parent / manifest["user_audio"]

        async def replay() -> str:
            service = LiveConversationService("agent:main:status-indicator-regression", 1.15)
            service.stt = self.stt
            with wave.open(str(wav_path), "rb") as recording:
                pcm = recording.readframes(recording.getnframes())
            return await service.transcribe(pcm, purpose="final")

        actual = asyncio.run(replay())
        words = _words(actual)
        self.assertTrue(
            {"agent", "status", "indicator", "acknowledge", "correction", "install"}
            .issubset(words),
            f"captured delegation lost required terms: {actual!r}",
        )
        self.assertFalse(is_gateway_status_question(actual), actual)

    def test_unwanted_backchannel_complaint_disables_spoken_hums(self) -> None:
        """Replay the phone report that the assistant was still saying mm-hmm."""
        capture_id = "20260911T153034.848723-4-b9e7f5"
        manifest_path = next(
            (path for path in self.all_manifests if path.stem == capture_id), None
        )
        if manifest_path is None:
            self.skipTest(f"captured Android regression audio {capture_id} is unavailable")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        wav_path = manifest_path.parent / manifest["user_audio"]

        async def replay() -> str:
            service = LiveConversationService("agent:main:backchannel-regression", 1.15)
            service.stt = self.stt
            with wave.open(str(wav_path), "rb") as recording:
                pcm = recording.readframes(recording.getnframes())
            return await service.transcribe(pcm, purpose="final")

        actual = asyncio.run(replay())
        self.assertTrue(
            {"still", "saying", "mmhmm"}.issubset(_words(actual)),
            f"captured complaint no longer reproduces clearly: {actual!r}",
        )
        self.assertFalse(SPOKEN_BACKCHANNELS_ENABLED)

    def test_normal_phone_statement_survives_partial_and_final_asr(self) -> None:
        """A casual statement must not vanish or change its central meaning."""
        capture_id = "20260911T153118.746660-2-09d0d1"
        manifest_path = next(
            (path for path in self.all_manifests if path.stem == capture_id), None
        )
        if manifest_path is None:
            self.skipTest(f"captured Android regression audio {capture_id} is unavailable")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        wav_path = manifest_path.parent / manifest["user_audio"]

        async def replay() -> dict[str, str]:
            service = LiveConversationService("agent:main:ordinary-speech-regression", 1.15)
            service.stt = self.stt
            with wave.open(str(wav_path), "rb") as recording:
                pcm = recording.readframes(recording.getnframes())
            return {
                purpose: await service.transcribe(pcm, purpose=purpose)
                for purpose in ("partial", "final")
            }

        actual = asyncio.run(replay())
        for purpose, transcript in actual.items():
            with self.subTest(purpose=purpose):
                self.assertTrue(
                    {"arby's", "wrong", "choice"}.issubset(_words(transcript)),
                    f"{purpose} ASR misunderstood the casual phone statement: {transcript!r}",
                )

    def test_quiet_normal_phone_speech_crosses_the_client_start_gate(self) -> None:
        """Prove the S25 onset detector still hears a substantially quieter turn."""
        capture_id = "20260911T152955.906576-2-1ef3d4"
        manifest_path = next(
            (path for path in self.all_manifests if path.stem == capture_id), None
        )
        if manifest_path is None:
            self.skipTest(f"captured Android regression audio {capture_id} is unavailable")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        wav_path = manifest_path.parent / manifest["user_audio"]
        with wave.open(str(wav_path), "rb") as recording:
            pcm = recording.readframes(recording.getnframes())

        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) * 0.30
        frame_samples = SAMPLE_RATE // 50
        consecutive_ms = 0
        longest_ms = 0
        for offset in range(0, len(samples), frame_samples):
            frame = samples[offset:offset + frame_samples]
            level = float(np.sqrt(np.mean(np.square(frame / 32768.0))))
            consecutive_ms = consecutive_ms + 20 if level >= 0.006 else 0
            longest_ms = max(longest_ms, consecutive_ms)
        self.assertGreaterEqual(
            longest_ms, 300,
            "quiet replay never crossed the production 300 ms speech-start gate",
        )


if __name__ == "__main__":
    unittest.main()
