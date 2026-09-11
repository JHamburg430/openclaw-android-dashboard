"""Real Qwen3-TTS vLLM-Omni acceptance tests.

Run against an already-started server with:

    LIVE_CONVERSATION_QWEN_TTS_TEST_URL=http://127.0.0.1:8792/v1/audio/speech \
      python -m unittest test_qwen_tts.py

The suite uses actual generated PCM and the production Faster-Whisper model;
neither synthesis nor recognition is mocked.
"""

from __future__ import annotations

import os
import time
import unittest

from pipecat.services.whisper.stt import WhisperSTTService
from pipecat.transcriptions.language import Language

from server import OUTPUT_SAMPLE_RATE, QwenVllmTtsClient


TEST_URL = os.environ.get("LIVE_CONVERSATION_QWEN_TTS_TEST_URL", "")


class QwenTtsAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not TEST_URL:
            raise unittest.SkipTest(
                "set LIVE_CONVERSATION_QWEN_TTS_TEST_URL to run real Qwen TTS tests"
            )
        cls.stt = WhisperSTTService(
            settings=WhisperSTTService.Settings(
                model="small.en", language=Language.EN, no_speech_prob=0.6
            ),
            device="cpu",
            compute_type="int8",
        )

    async def asyncSetUp(self) -> None:
        self.tts = QwenVllmTtsClient(url=TEST_URL, initial_chunk_frames=10)

    async def asyncTearDown(self) -> None:
        await self.tts.stop()

    async def synthesize_measured(self, text: str) -> tuple[bytes, float, float]:
        started = time.perf_counter()
        first_audio = 0.0
        pcm = bytearray()
        async for chunk in self.tts.stream_synthesize(text):
            if not first_audio:
                first_audio = time.perf_counter() - started
            pcm.extend(chunk)
        elapsed = time.perf_counter() - started
        return bytes(pcm), first_audio, elapsed

    async def test_real_audio_is_realtime_intelligible_and_low_latency(self) -> None:
        samples = (
            ("Can you hear my first question clearly?", ("first", "question")),
            ("Here is my second question about the weather.", ("second", "weather")),
            ("Jarvis, stop talking.", ("jarvis", "stop", "talking")),
        )
        for text, expected_words in samples:
            with self.subTest(text=text):
                pcm, first_audio, elapsed = await self.synthesize_measured(text)
                duration = len(pcm) / (OUTPUT_SAMPLE_RATE * 2)
                self.assertGreater(duration, 0.5)
                self.assertLess(first_audio, 1.25)
                self.assertLess(elapsed / duration, 0.85)
                transcript = await self._transcribe(pcm)
                normalized = {
                    word.strip(".,?!").lower() for word in transcript.split()
                }
                self.assertTrue(set(expected_words).issubset(normalized), transcript)

    async def _transcribe(self, pcm: bytes) -> str:
        def run() -> str:
            assert self.stt._model
            import numpy as np

            samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
            segments, _ = self.stt._model.transcribe(
                samples,
                language="en",
                beam_size=5,
                vad_filter=True,
                condition_on_previous_text=False,
            )
            return " ".join(segment.text.strip() for segment in segments).strip()

        import asyncio

        return await asyncio.to_thread(run)


if __name__ == "__main__":
    unittest.main()
