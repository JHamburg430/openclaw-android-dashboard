"""Model-backed audio integration tests for Live Conversation.

Unlike the fast unit tests in test_server.py, this module generates its input
with the production Kokoro voice and decodes it with the production
Faster-Whisper configuration.  Fixtures are generated in memory so the suite
never relies on prerecorded transcripts or brittle binary files.
"""

from __future__ import annotations

import asyncio
import math
from pathlib import Path
import unittest
from unittest.mock import AsyncMock

import numpy as np
from pipecat.services.whisper.stt import WhisperSTTService
from pipecat.transcriptions.language import Language

from server import (
    DEFAULT_TTS_MODEL_DIR,
    DEFAULT_TTS_RUNTIME,
    DEFAULT_TTS_WORKER,
    LiveConversationService,
    OUTPUT_SAMPLE_RATE,
    PersistentTtsWorker,
    SAMPLE_RATE,
)


PHRASES = {
    "first": "Can you hear my first question clearly?",
    "second": "Here is my second question about the weather.",
    "third": "And this is my third question about tomorrow.",
    "stop": "Jarvis, stop talking.",
    "confirm": "Go ahead.",
    "pause_a": "Please remember the first part",
    "pause_b": "and also remember the second part.",
}


def _resample_pcm16(pcm: bytes, source_rate: int, target_rate: int) -> bytes:
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    if not len(samples) or source_rate == target_rate:
        return pcm
    output_length = round(len(samples) * target_rate / source_rate)
    source_positions = np.arange(len(samples), dtype=np.float64)
    target_positions = np.linspace(0, len(samples) - 1, output_length)
    output = np.interp(target_positions, source_positions, samples)
    return np.clip(np.rint(output), -32768, 32767).astype("<i2").tobytes()


def _silence(milliseconds: int) -> bytes:
    return bytes(round(SAMPLE_RATE * milliseconds / 1000) * 2)


def _mix_noise(pcm: bytes, rms: float, seed: int = 430) -> bytes:
    """Mix deterministic broadband room noise into PCM without clipping."""
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    noise = np.random.default_rng(seed).normal(0.0, rms, len(samples))
    mixed = np.clip(samples + noise, -1.0, 1.0)
    return np.rint(mixed * 32767.0).astype("<i2").tobytes()


def _rms(pcm: bytes) -> float:
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    return float(math.sqrt(float(np.mean(samples * samples)))) if len(samples) else 0.0


class VoiceModelAudioIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Exercise real speech waveforms through the same ASR used in production."""

    audio: dict[str, bytes] = {}
    stt: WhisperSTTService
    stt_description = ""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        required = [
            Path(DEFAULT_TTS_WORKER),
            Path(DEFAULT_TTS_RUNTIME) / "lib",
            Path(DEFAULT_TTS_MODEL_DIR) / "model.onnx",
            Path(DEFAULT_TTS_MODEL_DIR) / "tokens.txt",
            Path(DEFAULT_TTS_MODEL_DIR) / "voices.bin",
        ]
        missing = [str(path) for path in required if not path.exists()]
        if missing:
            raise unittest.SkipTest(
                "production voice assets are unavailable: " + ", ".join(missing)
            )

        async def synthesize_fixtures() -> dict[str, bytes]:
            worker = PersistentTtsWorker(
                DEFAULT_TTS_WORKER,
                DEFAULT_TTS_RUNTIME,
                DEFAULT_TTS_MODEL_DIR,
                speed=1.15,
            )
            try:
                generated = {}
                for name, text in PHRASES.items():
                    pcm = await worker.synthesize(text)
                    generated[name] = (
                        _silence(250)
                        + _resample_pcm16(pcm, OUTPUT_SAMPLE_RATE, SAMPLE_RATE)
                        + _silence(350)
                    )
                return generated
            finally:
                await worker.stop()

        cls.audio = asyncio.run(synthesize_fixtures())
        # CTranslate2 initializes CUDA lazily on the first segment iteration,
        # so constructor-based fallback can falsely pass when a shell/CI runner
        # lacks the systemd service's CUDA library path. Use the same model and
        # decoding configuration on CPU for a portable, deterministic suite.
        cls.stt = WhisperSTTService(
            settings=WhisperSTTService.Settings(
                model="small.en", language=Language.EN, no_speech_prob=0.6
            ),
            device="cpu",
            compute_type="int8",
        )
        cls.stt_description = "faster-whisper small.en cpu-int8 integration test"

    def service(self) -> LiveConversationService:
        service = LiveConversationService("agent:main:voice-audio-test", 1.15)
        service.stt = self.stt
        service.stt_description = self.stt_description
        return service

    def assert_words_heard(self, transcript: str, *words: str) -> None:
        normalized = "".join(
            character.lower() if character.isalnum() else " "
            for character in transcript
        ).split()
        heard = set(normalized)
        self.assertTrue(transcript.strip(), "real speech produced an empty transcript")
        for word in words:
            self.assertIn(word.lower(), heard, f"{word!r} was not heard in {transcript!r}")

    async def test_each_synthesized_user_turn_is_heard(self) -> None:
        service = self.service()
        expected = {
            "first": ("first", "question"),
            "second": ("second", "weather"),
            "third": ("third", "tomorrow"),
        }
        for name, words in expected.items():
            with self.subTest(turn=name):
                transcript = await service.transcribe(self.audio[name])
                self.assert_words_heard(transcript, *words)

    async def test_real_audio_survives_background_noise(self) -> None:
        service = self.service()
        noisy_speech = _mix_noise(self.audio["second"], rms=0.008)
        transcript = await service.transcribe(noisy_speech)
        self.assert_words_heard(transcript, "second", "weather")

        noise_only = _mix_noise(_silence(2600), rms=0.008)
        self.assertEqual(await service.transcribe(noise_only), "")

    async def test_natural_thinking_pause_remains_one_complete_input(self) -> None:
        service = self.service()
        combined = (
            self.audio["pause_a"][:-round(SAMPLE_RATE * 0.35) * 2]
            + _silence(500)
            + self.audio["pause_b"][round(SAMPLE_RATE * 0.25) * 2:]
        )
        transcript = await service.transcribe(combined)
        self.assert_words_heard(transcript, "first", "second")

    async def test_multiple_real_audio_turns_preserve_conversation_flow(self) -> None:
        service = self.service()
        service.speech_reply = AsyncMock(side_effect=[
            ("direct", "I heard your first question.", None),
            ("direct", "I heard your second question.", None),
            ("direct", "I heard your third question.", None),
        ])
        service.tts.synthesize = AsyncMock(return_value=b"")
        socket = AsyncMock()

        for name in ("first", "second", "third"):
            self.assertIsNone(await service.process_turn(socket, self.audio[name]))

        user_turns = [
            message["content"] for message in service.recent_history()
            if message["role"] == "user"
        ]
        self.assertEqual(len(user_turns), 3)
        self.assert_words_heard(user_turns[0], "first", "question")
        self.assert_words_heard(user_turns[1], "second", "weather")
        self.assert_words_heard(user_turns[2], "third", "tomorrow")
        self.assertEqual(service.speech_reply.await_count, 3)

    async def test_synthesized_stop_interrupts_silently(self) -> None:
        service = self.service()
        service.speech_reply = AsyncMock()
        service.tts.synthesize = AsyncMock()
        service.speech_generation = 7
        socket = AsyncMock()

        self.assertIsNone(await service.process_turn(socket, self.audio["stop"]))
        self.assertEqual(service.speech_generation, 8)
        service.speech_reply.assert_not_awaited()
        service.tts.synthesize.assert_not_awaited()
        self.assertFalse(any(
            call.args[0].get("type") == "reply"
            for call in socket.send_json.await_args_list
        ))

    async def test_real_second_input_supersedes_slow_reply_without_being_lost(self) -> None:
        service = self.service()
        first_routing_started = asyncio.Event()
        release_first_routing = asyncio.Event()

        async def delayed_reply(transcript: str, agent_pending: bool = False):
            if "first" in transcript.lower():
                first_routing_started.set()
                await release_first_routing.wait()
                return "direct", "This first response is obsolete.", None
            return "direct", "I heard the newer weather question.", None

        service.speech_reply = AsyncMock(side_effect=delayed_reply)
        service.tts.synthesize = AsyncMock(return_value=b"")
        socket = AsyncMock()

        first = asyncio.create_task(service.process_turn(socket, self.audio["first"]))
        await first_routing_started.wait()
        # This is the same generation change emitted by confirmed browser
        # barge-in before the newer committed audio reaches the FIFO worker.
        service.interrupt_speech()
        release_first_routing.set()
        self.assertIsNone(await first)
        self.assertIsNone(await service.process_turn(socket, self.audio["second"]))

        history = service.recent_history()
        user_turns = [item["content"] for item in history if item["role"] == "user"]
        assistant_turns = [
            item["content"] for item in history if item["role"] == "assistant"
        ]
        self.assertEqual(len(user_turns), 2)
        self.assert_words_heard(user_turns[0], "first", "question")
        self.assert_words_heard(user_turns[1], "second", "weather")
        self.assertEqual(assistant_turns, ["I heard the newer weather question."])

    async def test_synthesized_confirmation_completes_pending_action_flow(self) -> None:
        service = self.service()
        service.confirmation_required = True
        service.pending_confirmation = (
            "Check the live audio flow.",
            "agent:main:voice-audio-confirmation-test",
            "session",
        )
        service.speech_reply = AsyncMock()
        service.tts.synthesize = AsyncMock(return_value=b"")
        socket = AsyncMock()

        handoff = await service.process_turn(socket, self.audio["confirm"])

        self.assertEqual(handoff, (
            "Check the live audio flow.",
            "Okay, proceeding.",
            "agent:main:voice-audio-confirmation-test",
        ))
        self.assertIsNone(service.pending_confirmation)
        service.speech_reply.assert_not_awaited()

    async def test_generated_voice_fixture_has_realistic_signal_level(self) -> None:
        # Guards against accidentally testing silence or a broken model response.
        for name, pcm in self.audio.items():
            with self.subTest(phrase=name):
                self.assertGreater(len(pcm), SAMPLE_RATE)
                self.assertGreater(_rms(pcm), 0.012)


if __name__ == "__main__":
    unittest.main()
