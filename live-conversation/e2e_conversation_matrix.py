#!/usr/bin/env python3
"""Required real-audio Live Conversation release matrix.

This intentionally uses no mocked model, ASR, TTS, endpoint, or WebSocket
responses. It synthesizes user speech with the production Kokoro voice, sends
PCM through the deployed WebSocket service, and validates real Whisper
transcripts, semantic endpoint decisions, local-model replies, response PCM,
noise handling, pause handling, and barge-in.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import time
from dataclasses import dataclass

from aiohttp import ClientSession, WSMsgType
import numpy as np

from server import (
    DEFAULT_TTS_MODEL_DIR,
    DEFAULT_TTS_RUNTIME,
    DEFAULT_TTS_WORKER,
    OUTPUT_SAMPLE_RATE,
    PersistentTtsWorker,
    SAMPLE_RATE,
)


FRAME_BYTES = SAMPLE_RATE * 2 // 50


def silence(milliseconds: int) -> bytes:
    return bytes(round(SAMPLE_RATE * milliseconds / 1000) * 2)


def resample(pcm: bytes, source_rate: int, target_rate: int) -> bytes:
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    if source_rate == target_rate or not len(samples):
        return pcm
    count = round(len(samples) * target_rate / source_rate)
    output = np.interp(
        np.linspace(0, len(samples) - 1, count),
        np.arange(len(samples)), samples,
    )
    return np.clip(np.rint(output), -32768, 32767).astype("<i2").tobytes()


def mix_noise(pcm: bytes, level: float, seed: int = 430) -> bytes:
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    noise = np.random.default_rng(seed).normal(0.0, level, len(samples))
    return np.rint(np.clip(samples + noise, -1.0, 1.0) * 32767).astype("<i2").tobytes()


def rms(pcm: bytes) -> float:
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    return math.sqrt(float(np.mean(samples * samples))) if len(samples) else 0.0


def contains_words(text: str, words: tuple[str, ...]) -> bool:
    aliases = {
        "0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
        "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine",
    }
    heard = {
        aliases.get(token, token)
        for token in "".join(
            character.lower() if character.isalnum() else " " for character in text
        ).split()
    }
    return all(aliases.get(word.lower(), word.lower()) in heard for word in words)


@dataclass
class TurnResult:
    transcript: str
    reply: str
    route: str
    response_pcm: bytes
    metrics: dict


class LiveSocket:
    def __init__(self, socket):
        self.socket = socket
        self.events: asyncio.Queue[dict] = asyncio.Queue()
        self.all_events: list[dict] = []
        self.reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        async for message in self.socket:
            if message.type != WSMsgType.TEXT:
                continue
            event = json.loads(message.data)
            self.all_events.append(event)
            await self.events.put(event)

    async def send(self, event: dict) -> None:
        await self.socket.send_json(event)

    async def wait(self, predicate, timeout: float = 45.0) -> dict:
        async with asyncio.timeout(timeout):
            while True:
                event = await self.events.get()
                if event.get("type") == "error":
                    raise AssertionError(event.get("message", "Live Conversation error"))
                if predicate(event):
                    return event

    async def close(self) -> None:
        await self.socket.close()
        self.reader.cancel()


async def synthesize(text: str, speed: float) -> bytes:
    worker = PersistentTtsWorker(
        DEFAULT_TTS_WORKER, DEFAULT_TTS_RUNTIME, DEFAULT_TTS_MODEL_DIR, speed=speed
    )
    try:
        pcm = await worker.synthesize(text, speed)
    finally:
        await worker.stop()
    return silence(300) + resample(pcm, OUTPUT_SAMPLE_RATE, SAMPLE_RATE) + silence(400)


async def send_pcm(live: LiveSocket, pcm: bytes) -> None:
    started = time.monotonic()
    for offset in range(0, len(pcm), FRAME_BYTES):
        chunk = pcm[offset:offset + FRAME_BYTES]
        await live.send({
            "type": "audio",
            "audioBase64": base64.b64encode(chunk).decode("ascii"),
        })
        # Android sends 20 ms microphone frames in real time. Pacing is part of
        # this release gate: without it, speech speed and pause/interruption
        # durations are merely byte patterns and do not validate endpointing.
        expected_elapsed = (offset + len(chunk)) / (SAMPLE_RATE * 2)
        delay = expected_elapsed - (time.monotonic() - started)
        if delay > 0:
            await asyncio.sleep(delay)


async def begin_turn(live: LiveSocket) -> None:
    await live.send({"type": "input_audio_buffer.speech_started"})
    await live.send({"type": "start"})


async def finish_turn(live: LiveSocket, expected: tuple[str, ...]) -> TurnResult:
    start_index = len(live.all_events)
    await live.send({"type": "commit"})
    transcript_event = await live.wait(
        lambda e: e.get("type") == "transcript"
        and contains_words(e.get("text", ""), expected)
    )
    reply_event = await live.wait(lambda e: e.get("type") == "reply")
    response_id = reply_event["responseId"]
    await live.wait(
        lambda e: e.get("type") == "response.output_audio.done"
        and e.get("responseId") == response_id
    )
    metrics = await live.wait(lambda e: e.get("type") == "metrics")
    relevant = live.all_events[start_index:]
    pcm = b"".join(
        base64.b64decode(event["audioBase64"])
        for event in relevant
        if event.get("type") == "response.output_audio.delta"
        and event.get("responseId") == response_id
    )
    if reply_event.get("route") != "direct":
        raise AssertionError(f"expected direct model response, got {reply_event}")
    if not reply_event.get("text", "").strip() or len(pcm) < OUTPUT_SAMPLE_RATE:
        raise AssertionError("real model response or response audio was empty")
    if rms(pcm) < 0.005:
        raise AssertionError("response PCM did not contain an audible signal")
    return TurnResult(
        transcript_event["text"], reply_event["text"], reply_event["route"], pcm, metrics
    )


async def complete_turn(
    live: LiveSocket, pcm: bytes, expected: tuple[str, ...]
) -> TurnResult:
    await begin_turn(live)
    await send_pcm(live, pcm)
    return await finish_turn(live, expected)


async def run_matrix(url: str) -> dict:
    slow, natural, fast = await asyncio.gather(
        synthesize("Please answer briefly. What color is a clear daytime sky?", 0.82),
        synthesize("Please answer briefly. What is two plus two?", 1.10),
        synthesize("Please answer briefly. Name the season after winter.", 1.35),
    )
    pause_cases = await asyncio.gather(
        asyncio.gather(
            synthesize("What is the result when you add", 1.02),
            synthesize("three and three?", 1.02),
        ),
        asyncio.gather(
            synthesize("Which season comes immediately after", 1.02),
            synthesize("spring?", 1.02),
        ),
        asyncio.gather(
            synthesize("What is the usual colour of", 1.02),
            synthesize("healthy grass?", 1.02),
        ),
    )
    long_request, interruption = await asyncio.gather(
        synthesize("Please explain photosynthesis in five sentences.", 1.05),
        synthesize("Actually, just tell me what two plus two is.", 1.15),
    )
    broken_first, broken_second = await asyncio.gather(
        synthesize("Please answer only after both parts. What is the capital of", 0.95),
        synthesize("the nation called Canada?", 1.0),
    )
    unfinished = await synthesize(
        "Could you tell me whether", 0.88
    )

    report: dict[str, object] = {
        "speed_noise": [], "pause_gaps_ms": [], "broken_sentence": {},
        "barge_in": {},
    }
    async with ClientSession() as client:
        socket = await client.ws_connect(url, heartbeat=20)
        live = LiveSocket(socket)
        try:
            for label, pcm, expected in (
                ("slow", slow, ("daytime", "sky")),
                ("natural_noise", mix_noise(natural, 0.006), ("two", "plus")),
                ("fast", fast, ("season", "winter")),
            ):
                result = await complete_turn(live, pcm, expected)
                report["speed_noise"].append({
                    "case": label, "transcript": result.transcript,
                    "reply": result.reply, "response_pcm_bytes": len(result.response_pcm),
                })

            for gap_ms, (pause_a, pause_b), expected in zip(
                (250, 800, 1800), pause_cases,
                (("three",), ("spring",), ("grass",)),
            ):
                trim_a = pause_a[:-round(SAMPLE_RATE * 0.4) * 2]
                trim_b = pause_b[round(SAMPLE_RATE * 0.3) * 2:]
                await begin_turn(live)
                await send_pcm(live, trim_a + silence(gap_ms))
                endpoint = None
                if gap_ms >= 750:
                    await live.send({"type": "endpoint_candidate"})
                    endpoint = await live.wait(lambda e: e.get("type") == "endpoint_decision")
                    if endpoint.get("source", "").endswith("semantic-error"):
                        raise AssertionError(
                            f"semantic endpoint controller failed after {gap_ms} ms: {endpoint}"
                        )
                    if endpoint.get("complete"):
                        raise AssertionError(
                            f"incomplete thought was ended after {gap_ms} ms: {endpoint}"
                        )
                await send_pcm(live, trim_b)
                result = await finish_turn(live, expected)
                report["pause_gaps_ms"].append({
                    "gap": gap_ms, "endpoint": endpoint,
                    "transcript": result.transcript, "reply": result.reply,
                })

            # An incomplete long thought should yield a blank-text PCM hum, not
            # a text token that Kokoro can spell aloud.
            await begin_turn(live)
            # Ensure this conversational hold is long enough to exercise the
            # production >=4 s backchannel threshold at real-time pacing.
            await send_pcm(live, silence(2200) + unfinished)
            # The phone continues sending silent microphone frames while the
            # speaker pauses; do the same so rolling ASR can stabilize.
            await send_pcm(live, silence(1400))
            await live.send({"type": "endpoint_candidate"})
            endpoint = await live.wait(lambda e: e.get("type") == "endpoint_decision")
            if endpoint.get("complete"):
                raise AssertionError(f"unfinished backchannel probe ended early: {endpoint}")
            backchannel = await live.wait(lambda e: e.get("type") == "backchannel")
            if backchannel.get("text") != "":
                raise AssertionError(f"backchannel exposed spelling text: {backchannel}")
            backchannel_id = backchannel["responseId"]
            await live.wait(
                lambda e: e.get("type") == "response.output_audio.done"
                and e.get("responseId") == backchannel_id
            )
            hum_pcm = b"".join(
                base64.b64decode(event["audioBase64"])
                for event in live.all_events
                if event.get("type") == "response.output_audio.delta"
                and event.get("responseId") == backchannel_id
            )
            if len(hum_pcm) < 20_000 or rms(hum_pcm) < 0.02:
                raise AssertionError("nonverbal backchannel audio was missing")
            await send_pcm(live, await synthesize("autumn comes after summer?", 1.0))
            await finish_turn(live, ("autumn", "summer"))
            report["backchannel"] = {
                "text": backchannel.get("text"), "pcm_bytes": len(hum_pcm),
                "endpoint": endpoint,
            }

            # Two separately committed ASR turns must remain one semantic
            # sentence: the first waits silently and the second gets one real
            # model/TTS response using the assembled meaning.
            broken_start = len(live.all_events)
            await begin_turn(live)
            await send_pcm(live, broken_first)
            await live.send({"type": "commit"})
            first_fragment = await live.wait(
                lambda e: e.get("type") == "transcript"
                and contains_words(e.get("text", ""), ("capital",))
            )
            await live.wait(
                lambda e: e.get("type") == "state"
                and "Waiting for you to finish" in e.get("detail", "")
            )
            premature = [
                event for event in live.all_events[broken_start:]
                if event.get("type") in {"reply", "backchannel"}
            ]
            if premature:
                raise AssertionError(
                    f"first broken-sentence fragment produced output: {premature}"
                )
            second_fragment = await complete_turn(
                live, broken_second, ("canada",)
            )
            if "ottawa" not in second_fragment.reply.lower():
                raise AssertionError(
                    "broken sentence was not answered from its assembled meaning: "
                    f"{second_fragment.reply!r}"
                )
            report["broken_sentence"] = {
                "first_transcript": first_fragment["text"],
                "second_transcript": second_fragment.transcript,
                "assembled_reply": second_fragment.reply,
                "response_pcm_bytes": len(second_fragment.response_pcm),
            }

            # Interrupt actual response PCM, then verify a second real utterance,
            # model response, and audio response survive the barge-in.
            await begin_turn(live)
            await send_pcm(live, long_request)
            await live.send({"type": "commit"})
            first_transcript = await live.wait(
                lambda e: e.get("type") == "transcript"
                and contains_words(e.get("text", ""), ("photosynthesis",))
            )
            first_reply = await live.wait(lambda e: e.get("type") == "reply")
            if first_reply.get("route") != "direct":
                raise AssertionError(
                    f"timeless explanation unexpectedly delegated: {first_reply}"
                )
            await live.wait(
                lambda e: e.get("type") == "response.output_audio.delta"
                and e.get("responseId") == first_reply.get("responseId")
            )
            await begin_turn(live)
            await send_pcm(live, interruption)
            second = await finish_turn(live, ("two", "plus"))
            report["barge_in"] = {
                "first_transcript": first_transcript["text"],
                "interrupted_response_id": first_reply["responseId"],
                "second_transcript": second.transcript,
                "second_reply": second.reply,
                "second_pcm_bytes": len(second.response_pcm),
            }
        finally:
            await live.close()
    return report


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8790/ws")
    args = parser.parse_args()
    report = await run_matrix(args.url)
    encoded = json.dumps(report, indent=2)
    print(encoded)


if __name__ == "__main__":
    asyncio.run(main())
