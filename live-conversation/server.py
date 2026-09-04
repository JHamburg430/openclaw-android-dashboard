#!/usr/bin/env python3
"""Dedicated low-latency voice service for the Android Dashboard."""

from __future__ import annotations

import argparse
import asyncio
import base64
from dataclasses import asdict, dataclass
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import re
import struct
import time
from typing import Any
from zoneinfo import ZoneInfo

LOGGER = logging.getLogger("live_conversation")

import aiohttp
from aiohttp import WSMsgType, web
from pipecat.frames.frames import TranscriptionFrame
from pipecat.services.whisper.stt import WhisperSTTService
from pipecat.transcriptions.language import Language


SAMPLE_RATE = 16_000
OUTPUT_SAMPLE_RATE = 24_000
DEFAULT_PORT = 8790
DEFAULT_SESSION_KEY = "agent:main:live-conversation"
DEFAULT_NODE_COMMAND = "/home/john/nodejs/bin/node"
DEFAULT_OPENCLAW_MODULE = "/home/john/nodejs/lib/node_modules/openclaw/openclaw.mjs"
DEFAULT_TTS_WORKER = "/home/john/.openclaw/plugins/local-realtime-voice/bin/openclaw-local-tts-worker"
DEFAULT_TTS_RUNTIME = "/home/john/.openclaw/tools/sherpa-onnx-tts/runtime"
DEFAULT_TTS_MODEL_DIR = "/home/john/.openclaw/tools/sherpa-onnx-tts/models/vits-piper-en_GB-jarvis-high"
SPEECH_MODEL_URL = "http://127.0.0.1:11434/api/chat"
SPEECH_MODEL = "qwen3.5:0.8b"
AGENT_SENTINEL = "[[OPENCLAW_AGENT]]"
SAY_SENTINEL = "[[SAY]]"


@dataclass
class TurnMetrics:
    asr_ms: int = 0
    routing_ms: int = 0
    response_ms: int = 0
    tts_ms: int = 0
    total_ms: int = 0
    route: str = ""


def speech_model_prompt(now: datetime | None = None, agent_pending: bool = False) -> str:
    clock = now or datetime.now(ZoneInfo("America/Detroit"))
    if agent_pending:
        return f"""You are the conversational voice supervisor while an OpenClaw agent works in the background.
Always output {SAY_SENTINEL} followed by one short natural spoken response.
Only respond because the user has spoken to you. If asked for status, truthfully say the agent is still working. Answer unrelated casual or timeless questions yourself. If asked for another task needing tools or private context, explain briefly that the current agent is still busy; do not claim you started another agent.
Never volunteer a timer-based or generic progress message. Agent progress may be relayed only when the agent actually emits it.
Never fabricate private, project, or agent progress. The current local time is {clock.strftime('%-I:%M %p')} America/Detroit.
Examples:
User: Are you still working on it?
Assistant: {SAY_SENTINEL} Yes, the agent is still working in the background.
User: Tell me a joke while you work.
Assistant: {SAY_SENTINEL} Why did the robot take a vacation? It needed to recharge.
User: Also check my calendar.
Assistant: {SAY_SENTINEL} The agent is still busy with the first request, so I haven’t started the calendar check."""
    return f"""You are the conversational voice supervisor. No OpenClaw agent is currently working.
Output exactly one of these forms:
- For casual conversation or timeless general knowledge answerable confidently without tools: {SAY_SENTINEL} followed by one short natural spoken response.
- If no agent is working and the request needs apps, projects, private context, files, logs, calendar, messages, memory, current external facts, tools, judgment, or an action: {AGENT_SENTINEL} followed by a short natural acknowledgment telling the user what you are handing off. Write the acknowledgment yourself for this request.
- If an agent is already working, keep conversing. For a status question, use {SAY_SENTINEL} and truthfully say the agent is still working. For unrelated simple conversation, answer normally. Do not start a second agent.
The current local date and time is {clock.strftime('%A, %B %-d, %Y, %-I:%M %p')} America/Detroit; time and date questions can be answered directly.
Never fabricate private, project, or agent progress. The sentinel is machine-readable and must not be spoken.
Examples:
User: How is the RAG app improvement going?
Assistant: {AGENT_SENTINEL} I’ll ask the agent to check the RAG app and report back.
User: Fix the routing bug.
Assistant: {AGENT_SENTINEL} I’ll have the agent inspect and fix the routing bug.
User: Who wrote The Hobbit?
Assistant: {SAY_SENTINEL} J. R. R. Tolkien wrote The Hobbit.
User: What time is it?
Assistant: {SAY_SENTINEL} It is {clock.strftime('%-I:%M %p')}."""


def parse_speech_model_output(text: str) -> tuple[str, str]:
    cleaned = text.strip()
    if AGENT_SENTINEL in cleaned:
        acknowledgment = cleaned.split(AGENT_SENTINEL, 1)[1].strip()
        return "agent", acknowledgment
    if SAY_SENTINEL in cleaned:
        cleaned = cleaned.split(SAY_SENTINEL, 1)[1].strip()
    return "direct", cleaned


def extract_agent_text(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("text", "reply", "answer", "output"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key in ("payloads", "messages", "result", "data"):
            if key in payload:
                found = extract_agent_text(payload[key])
                if found:
                    return found
    if isinstance(payload, list):
        for value in reversed(payload):
            found = extract_agent_text(value)
            if found:
                return found
    return ""


_CARDINAL_UNDER_20 = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen",
)
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")
_ORDINAL_UNDER_20 = (
    "zeroth", "first", "second", "third", "fourth", "fifth", "sixth", "seventh",
    "eighth", "ninth", "tenth", "eleventh", "twelfth", "thirteenth", "fourteenth",
    "fifteenth", "sixteenth", "seventeenth", "eighteenth", "nineteenth",
)
_MONTHS = {
    name.lower(): index
    for index, names in enumerate(
        (
            (), ("January", "Jan"), ("February", "Feb"), ("March", "Mar"),
            ("April", "Apr"), ("May",), ("June", "Jun"), ("July", "Jul"),
            ("August", "Aug"), ("September", "Sep", "Sept"), ("October", "Oct"),
            ("November", "Nov"), ("December", "Dec"),
        )
    )
    for name in names
}


def _cardinal_under_100(value: int) -> str:
    if value < 20:
        return _CARDINAL_UNDER_20[value]
    tens, ones = divmod(value, 10)
    return _TENS[tens] if not ones else f"{_TENS[tens]}-{_CARDINAL_UNDER_20[ones]}"


def _ordinal_day(value: int) -> str:
    if value < 20:
        return _ORDINAL_UNDER_20[value]
    tens, ones = divmod(value, 10)
    if not ones:
        return {2: "twentieth", 3: "thirtieth"}[tens]
    return f"{_TENS[tens]}-{_ORDINAL_UNDER_20[ones]}"


def _spoken_year(value: int) -> str:
    if 2000 <= value <= 2009:
        return "two thousand" if value == 2000 else f"two thousand {_CARDINAL_UNDER_20[value - 2000]}"
    if 2010 <= value <= 2099:
        return f"twenty {_cardinal_under_100(value - 2000)}"
    if 1000 <= value <= 1999:
        century, remainder = divmod(value, 100)
        if not remainder:
            return f"{_cardinal_under_100(century)} hundred"
        return f"{_cardinal_under_100(century)} {_cardinal_under_100(remainder)}"
    return str(value)


def _spoken_date(year: int, month: int, day: int) -> str | None:
    try:
        parsed = datetime(year, month, day)
    except ValueError:
        return None
    return f"{parsed.strftime('%B')} {_ordinal_day(day)}, {_spoken_year(year)}"


def _spoken_time(hour: int, minute: int, second: int | None = None) -> str | None:
    if not 0 <= hour <= 23 or not 0 <= minute <= 59 or (second is not None and not 0 <= second <= 59):
        return None
    period = "A M" if hour < 12 else "P M"
    display_hour = hour % 12 or 12
    if minute == 0:
        rendered = f"{display_hour} {period}"
    elif minute < 10:
        rendered = f"{display_hour} oh {_CARDINAL_UNDER_20[minute]} {period}"
    else:
        rendered = f"{display_hour} {_cardinal_under_100(minute)} {period}"
    if second:
        rendered += f" and {_cardinal_under_100(second)} seconds"
    return rendered


def normalize_spoken_text(text: str) -> str:
    """Convert display-oriented Markdown and compact metrics into natural speech."""
    spoken = text
    # Preserve link labels while dropping destinations that are awkward to read aloud.
    spoken = re.sub(r"!?\[([^\]]+)\]\([^\)]+\)", r"\1", spoken)
    # Source blocks are useful on screen but should not be dictated symbol by symbol.
    spoken = re.sub(r"```[^\n]*\n.*?```", " ", spoken, flags=re.DOTALL)
    spoken = re.sub(r"https?://\S+", " ", spoken)
    spoken = re.sub(r"(?<!\w)[*_~`]+|[*_~`]+(?!\w)", "", spoken)
    # Dates should sound like dates, not arithmetic or digit sequences.
    def replace_iso_date(match: re.Match[str]) -> str:
        rendered = _spoken_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        return rendered or match.group(0)

    def replace_us_date(match: re.Match[str]) -> str:
        year = int(match.group(3))
        if year < 100:
            year += 2000 if year < 70 else 1900
        rendered = _spoken_date(year, int(match.group(1)), int(match.group(2)))
        return rendered or match.group(0)

    def replace_time(match: re.Match[str]) -> str:
        rendered = _spoken_time(
            int(match.group(1)), int(match.group(2)), int(match.group(3)) if match.group(3) else None
        )
        return rendered or match.group(0)

    def replace_named_date(match: re.Match[str]) -> str:
        month = _MONTHS[match.group(1).lower()]
        year = int(match.group(3))
        rendered = _spoken_date(year, month, int(match.group(2)))
        return rendered or match.group(0)

    def replace_day_first_date(match: re.Match[str]) -> str:
        month = _MONTHS[match.group(2).lower()]
        year = int(match.group(3))
        rendered = _spoken_date(year, month, int(match.group(1)))
        return rendered or match.group(0)

    spoken = re.sub(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)", replace_iso_date, spoken)
    spoken = re.sub(r"(?<!\d)(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})(?!\d)", replace_us_date, spoken)
    month_names = "|".join(sorted((re.escape(name) for name in _MONTHS), key=len, reverse=True))
    spoken = re.sub(
        rf"\b({month_names})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b",
        replace_named_date,
        spoken,
        flags=re.IGNORECASE,
    )
    spoken = re.sub(
        rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({month_names})\.?[,]?\s+(\d{{4}})\b",
        replace_day_first_date,
        spoken,
        flags=re.IGNORECASE,
    )
    spoken = re.sub(
        r"(?:T|\s+at\s+)([01]?\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?(?:Z|[+-]\d{2}:?\d{2})?",
        lambda match: " at " + replace_time(match),
        spoken,
    )
    spoken = re.sub(r"(?<!\w)(\d[\d,]*(?:\.\d+)?)\s*/\s*(\d[\d,]*(?:\.\d+)?)(?!\w)", r"\1 out of \2", spoken)
    spoken = re.sub(r"(\d(?:[\d,]*\d)?(?:\.\d+)?)\s*%", r"\1 percent", spoken)
    spoken = re.sub(r"\bv(?=\d+(?:\.\d+)+)", "version ", spoken, flags=re.IGNORECASE)
    spoken = re.sub(r"(?<=\d)\.(?=\d)", " point ", spoken)
    spoken = re.sub(r"\b(\d[\d,.]*)\s*ms\b", r"\1 milliseconds", spoken, flags=re.IGNORECASE)
    spoken = re.sub(
        r"\b(\d[\d,.]*)\s*(kb|mb|gb|tb)\b",
        lambda match: f"{match.group(1)} {match.group(2).upper()}",
        spoken,
        flags=re.IGNORECASE,
    )
    spoken = spoken.replace("->", " to ").replace("=>", " results in ")
    spoken = re.sub(r">=", " at least ", spoken)
    spoken = re.sub(r"<=", " at most ", spoken)
    spoken = re.sub(r"(?m)^\s*[-+•]\s+", ". ", spoken)
    spoken = re.sub(r"(?m)^\s*\d+[.)]\s+", ". ", spoken)
    spoken = re.sub(r"(?m)^\s*#{1,6}\s+", "", spoken)
    spoken = re.sub(r"\s*\|\s*", ". ", spoken)
    spoken = spoken.replace(" — ", ". ").replace(" – ", ". ")
    spoken = re.sub(r"\s+", " ", spoken).strip()
    spoken = re.sub(r"\s+([,.;:!?])", r"\1", spoken)
    spoken = re.sub(r"(?:\.\s*){2,}", ". ", spoken)
    return spoken.removeprefix(". ")


def split_spoken_text(text: str, max_chars: int = 180) -> list[str]:
    """Split long replies into natural TTS units so playback can begin promptly."""
    normalized = normalize_spoken_text(text)
    if not normalized:
        return [""]
    sentences = re.split(r"(?<=[.!?])\s+", normalized)
    parts: list[str] = []
    for sentence in sentences:
        while len(sentence) > max_chars:
            split_at = max(sentence.rfind(mark, 0, max_chars + 1) for mark in (", ", "; ", ": ", " "))
            if split_at <= 0:
                split_at = max_chars
            else:
                split_at += 1
            parts.append(sentence[:split_at].strip())
            sentence = sentence[split_at:].strip()
        if sentence:
            parts.append(sentence)
    return parts or [normalized]


class PersistentTtsWorker:
    def __init__(self, command: str, runtime_dir: str, model_dir: str, speed: float = 1.0):
        self.command = command
        self.runtime_dir = runtime_dir
        self.model_dir = model_dir
        self.speed = speed
        self.process: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()

    async def start(self) -> None:
        if self.process and self.process.returncode is None:
            return
        models = list(Path(self.model_dir).glob("*.onnx"))
        if len(models) != 1:
            raise RuntimeError(f"Expected one TTS model in {self.model_dir}")
        environment = os.environ.copy()
        library_path = str(Path(self.runtime_dir) / "lib")
        environment["LD_LIBRARY_PATH"] = ":".join(filter(None, [library_path, environment.get("LD_LIBRARY_PATH", "")]))
        self.process = await asyncio.create_subprocess_exec(
            self.command,
            "--model", str(models[0]),
            "--tokens", str(Path(self.model_dir) / "tokens.txt"),
            "--data-dir", str(Path(self.model_dir) / "espeak-ng-data"),
            "--threads", "4",
            "--speed", str(self.speed),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
        assert self.process.stdout
        handshake = await self.process.stdout.readexactly(8)
        if handshake[:4] != b"RTV1" or struct.unpack("<I", handshake[4:])[0] != OUTPUT_SAMPLE_RATE:
            raise RuntimeError("Invalid TTS worker handshake")

    async def synthesize(self, text: str) -> bytes:
        async with self.lock:
            await self.start()
            assert self.process and self.process.stdin and self.process.stdout
            encoded = text.encode("utf-8")
            self.process.stdin.write(struct.pack("<I", len(encoded)) + encoded)
            await self.process.stdin.drain()
            header = await self.process.stdout.readexactly(8)
            status, length = struct.unpack("<II", header)
            payload = await self.process.stdout.readexactly(length)
            if status != 0:
                raise RuntimeError(payload.decode("utf-8", errors="replace"))
            return payload

    async def stop(self) -> None:
        if not self.process:
            return
        self.process.terminate()
        await self.process.wait()
        self.process = None


class LiveConversationService:
    def __init__(self, session_key: str, tts_speed: float):
        self.session_key = session_key
        self.stt: WhisperSTTService | None = None
        self.tts = PersistentTtsWorker(DEFAULT_TTS_WORKER, DEFAULT_TTS_RUNTIME, DEFAULT_TTS_MODEL_DIR, tts_speed)
        self.agent_lock = asyncio.Lock()
        self.speech_lock = asyncio.Lock()

    async def start(self) -> None:
        self.stt = await asyncio.to_thread(
            WhisperSTTService,
            settings=WhisperSTTService.Settings(model="tiny.en", language=Language.EN),
            device="cpu",
            compute_type="int8",
        )
        await self.tts.start()

    async def stop(self) -> None:
        await self.tts.stop()

    async def transcribe(self, audio: bytes) -> str:
        if not self.stt:
            raise RuntimeError("Speech recognizer is not ready")
        text = ""
        async for frame in self.stt.run_stt(audio):
            if isinstance(frame, TranscriptionFrame):
                text = frame.text.strip()
        return text

    async def agent_reply(self, text: str) -> str:
        async with self.agent_lock:
            process = await asyncio.create_subprocess_exec(
                DEFAULT_NODE_COMMAND, DEFAULT_OPENCLAW_MODULE, "agent",
                "--session-key", self.session_key,
                "--message", text,
                "--timeout", "600",
                "--json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=610)
        if process.returncode != 0:
            raise RuntimeError(stderr.decode("utf-8", errors="replace").strip() or "Agent request failed")
        payload = json.loads(stdout)
        reply = extract_agent_text(payload)
        if not reply:
            raise RuntimeError("Agent returned no speakable text")
        return reply

    async def speech_reply(self, text: str, agent_pending: bool = False) -> tuple[str, str]:
        payload = {
            "model": SPEECH_MODEL,
            "stream": False,
            "think": False,
            "messages": [
                {"role": "system", "content": speech_model_prompt(agent_pending=agent_pending)},
                {"role": "user", "content": text},
            ],
            "options": {"temperature": 0, "num_predict": 80},
        }
        timeout = aiohttp.ClientTimeout(total=15)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(SPEECH_MODEL_URL, json=payload) as response:
                    response.raise_for_status()
                    result = await response.json()
            route, reply = parse_speech_model_output(result.get("message", {}).get("content", ""))
            if agent_pending:
                route = "direct"
            if not reply:
                raise ValueError("speech model returned no speakable text")
            return route, reply
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
            raise RuntimeError(f"Speech supervisor unavailable: {error}") from error

    async def send_spoken_response(
        self,
        socket: web.WebSocketResponse,
        text: str,
        route: str,
        response_id: str,
        message_type: str = "reply",
    ) -> int:
        """Synthesize and stream one complete spoken response."""
        async with self.speech_lock:
            await socket.send_json({
                "type": message_type,
                "text": text,
                "route": route,
                "responseId": response_id,
            })
            await socket.send_json({"type": "state", "state": "speaking", "route": route})
            speech_parts = split_spoken_text(text)
            tts_started = time.perf_counter()
            pcm = await self.tts.synthesize(speech_parts[0])
            tts_ms = round((time.perf_counter() - tts_started) * 1000)
            LOGGER.info(
                "response_stream_start id=%s route=%s chars=%d parts=%d first_pcm_bytes=%d first_tts_ms=%d",
                response_id, route, len(text), len(speech_parts), len(pcm), tts_ms,
            )
            await socket.send_json({"type": "output_audio_buffer.started", "responseId": response_id})
            chunk_bytes = OUTPUT_SAMPLE_RATE * 2 // 10
            total_pcm_bytes = 0
            for index, _ in enumerate(speech_parts):
                next_synthesis: asyncio.Task[bytes] | None = None
                if index + 1 < len(speech_parts):
                    next_synthesis = asyncio.create_task(self.tts.synthesize(speech_parts[index + 1]))
                for offset in range(0, len(pcm), chunk_bytes):
                    chunk = pcm[offset:offset + chunk_bytes]
                    await socket.send_json({
                        "type": "response.output_audio.delta",
                        "responseId": response_id,
                        "sampleRate": OUTPUT_SAMPLE_RATE,
                        "audioBase64": base64.b64encode(chunk).decode("ascii"),
                    })
                    total_pcm_bytes += len(chunk)
                    await asyncio.sleep(len(chunk) / (OUTPUT_SAMPLE_RATE * 2))
                if next_synthesis is not None:
                    wait_started = time.perf_counter()
                    pcm = await next_synthesis
                    tts_ms += round((time.perf_counter() - wait_started) * 1000)
            await socket.send_json({"type": "response.output_audio.done", "responseId": response_id})
            LOGGER.info(
                "response_stream_done id=%s route=%s pcm_bytes=%d tts_wait_ms=%d",
                response_id, route, total_pcm_bytes, tts_ms,
            )
        return tts_ms

    async def process_turn(self, socket: web.WebSocketResponse, audio: bytes, agent_pending: bool = False) -> tuple[str, str] | None:
        started = time.perf_counter()
        metrics = TurnMetrics()
        try:
            await socket.send_json({"type": "state", "state": "transcribing"})
            stage = time.perf_counter()
            transcript = await self.transcribe(audio)
            metrics.asr_ms = round((time.perf_counter() - stage) * 1000)
            if not transcript:
                await socket.send_json({"type": "state", "state": "listening", "detail": "No clear speech detected."})
                return
            await socket.send_json({"type": "transcript", "text": transcript})

            stage = time.perf_counter()
            route, reply = await self.speech_reply(transcript, agent_pending=agent_pending)
            metrics.routing_ms = round((time.perf_counter() - stage) * 1000)
            metrics.route = route

            response_id = f"response-{time.monotonic_ns()}"
            metrics.tts_ms = await self.send_spoken_response(
                socket, reply, metrics.route, response_id
            )
            metrics.total_ms = round((time.perf_counter() - started) * 1000)
            await socket.send_json({"type": "metrics", **asdict(metrics)})
            await socket.send_json({"type": "state", "state": "listening"})
            if route == "agent" and not agent_pending:
                return transcript, reply
        except Exception as error:
            await socket.send_json({"type": "error", "message": str(error)})
        return None

def render_page() -> str:
    return """<!doctype html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Live Conversation</title><style>
html,body{margin:0;min-height:100%;background:#060a10;color:#f4f8fc;font-family:system-ui,sans-serif}main{padding:18px 14px 28px;max-width:760px;margin:auto}h1{font-size:23px;margin:0 0 5px}.sub{color:#9aa9b8;margin:0 0 16px}.state{font-size:18px;color:#54e0b4;margin:12px 0}.meter{height:14px;background:#101820;border:1px solid #304050;border-radius:8px;overflow:hidden}.meter div{height:100%;width:0;background:#00ab7e;transition:width 60ms}.buttons{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin:14px 0}button{min-height:48px;border:1px solid #00ab7e;border-radius:8px;background:#1e2630;color:#fff;font-size:15px}.card{background:#0a0e14;border-radius:8px;padding:12px;margin-top:10px;min-height:48px}.label{color:#8797a8;font-size:11px;text-transform:uppercase;letter-spacing:.08em}.metrics{font-size:12px;color:#aebdca;margin-top:12px}.route{display:inline-block;border:1px solid #36556a;border-radius:10px;padding:2px 7px;font-size:11px;margin-left:6px}
</style></head><body><main><h1>Live Conversation</h1><p class="sub">Fast local speech recognition. Simple requests answer here; complex work escalates to OpenClaw.</p><div id="state" class="state">Ready</div><div class="meter"><div id="bar"></div></div><div class="buttons"><button id="start">Start conversation</button><button id="stop">Stop</button></div><div class="card"><div class="label">You</div><div id="user">—</div></div><div class="card"><div class="label">Assistant <span id="route" class="route">waiting</span></div><div id="assistant">—</div></div><div id="metrics" class="metrics"></div></main><script>
const state=document.getElementById('state'),bar=document.getElementById('bar'),user=document.getElementById('user'),assistant=document.getElementById('assistant'),route=document.getElementById('route'),metrics=document.getElementById('metrics');let ws,timer,recording=false,awaitingResponse=false,responseActive=false,speechMs=0,silenceMs=0,candidateSpeechMs=0,pre=[];
function rms(b64){const s=atob(b64||'');let sum=0,n=0;for(let i=0;i+1<s.length;i+=2){let v=(s.charCodeAt(i)&255)|((s.charCodeAt(i+1)&255)<<8);if(v&32768)v-=65536;const f=v/32768;sum+=f*f;n++}return n?Math.sqrt(sum/n):0}
function send(x){if(ws&&ws.readyState===1)ws.send(JSON.stringify(x))}
function report(event,detail){send({type:'client_event',event:event,detail:String(detail||'')})}
function begin(){if(recording||awaitingResponse)return;recording=true;candidateSpeechMs=0;speechMs=0;silenceMs=0;if(responseActive){responseActive=false;try{OpenClawNativeAudio.interruptAgentResponsePlayback()}catch(e){}send({type:'input_audio_buffer.speech_started'})}send({type:'start'});for(const audioBase64 of pre)send({type:'audio',audioBase64});pre=[];state.textContent='Listening…'}
function tick(){let chunk='';try{chunk=OpenClawNativeAudio.readChunkBase64()||''}catch(e){state.textContent='Microphone error';return}if(!chunk)return;const level=rms(chunk);bar.style.width=Math.min(100,Math.round(level*850))+'%';if(!recording){pre.push(chunk);while(pre.length>20)pre.shift();if(awaitingResponse){candidateSpeechMs=0;return}candidateSpeechMs=level>=.006?candidateSpeechMs+20:0;if(candidateSpeechMs>=200)begin();return}send({type:'audio',audioBase64:chunk});if(level>=.006){speechMs+=20;silenceMs=0}else silenceMs+=20;if(speechMs>=200&&silenceMs>=600){recording=false;awaitingResponse=true;candidateSpeechMs=0;try{OpenClawNativeAudio.prepareAgentResponsePlayback()}catch(e){}send({type:'commit'});state.textContent='Transcribing…'}}
function start(){if(ws&&ws.readyState===1)return;ws=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/ws');let audioChunks=0,audioChars=0;ws.onopen=()=>{OpenClawNativeAudio.startCapture(16000,20);timer=setInterval(tick,20);state.textContent='Listening…'};ws.onmessage=e=>{const m=JSON.parse(e.data);if(m.type==='state')state.textContent=m.state[0].toUpperCase()+m.state.slice(1)+'…';if(m.type==='transcript')user.textContent=m.text;if(m.type==='reply'){assistant.textContent=m.text;route.textContent=m.route;report('reply',{route:m.route})}if(m.type==='output_audio_buffer.started'){awaitingResponse=false;responseActive=true;candidateSpeechMs=0;pre=[];audioChunks=0;audioChars=0;try{OpenClawNativeAudio.prepareAgentResponsePlayback();report('playback_prepared',m.responseId)}catch(err){report('playback_prepare_error',err)}}if(m.type==='response.output_audio.delta'){audioChunks++;audioChars+=(m.audioBase64||'').length;try{OpenClawNativeAudio.playAgentResponsePcm16Base64(m.audioBase64,m.sampleRate);if(audioChunks===1)report('first_pcm_enqueued','rate='+m.sampleRate+' chars='+(m.audioBase64||'').length)}catch(err){report('pcm_enqueue_error',err)}}if(m.type==='response.output_audio.done'){responseActive=false;candidateSpeechMs=0;pre=[];report('pcm_delivery_done','chunks='+audioChunks+' chars='+audioChars)}if(m.type==='metrics')metrics.textContent=`ASR ${m.asr_ms} ms · Agent ${m.response_ms} ms · TTS ${m.tts_ms} ms · Ready ${m.total_ms} ms`;if(m.type==='error'){awaitingResponse=false;responseActive=false;state.textContent='Error';assistant.textContent=m.message;report('server_error',m.message)}};ws.onerror=()=>state.textContent='Connection error';ws.onclose=()=>state.textContent='Stopped'}
function stop(){if(timer)clearInterval(timer);timer=null;try{OpenClawNativeAudio.stopCapture()}catch(e){}if(ws)ws.close();ws=null;recording=false;awaitingResponse=false;responseActive=false;candidateSpeechMs=0;bar.style.width='0%';state.textContent='Stopped'}
document.getElementById('start').onclick=start;document.getElementById('stop').onclick=stop;window.addEventListener('pagehide',stop);
</script></body></html>"""


async def index(_: web.Request) -> web.Response:
    return web.Response(text=render_page(), content_type="text/html", headers={"Cache-Control": "no-store"})


async def health(request: web.Request) -> web.Response:
    service: LiveConversationService = request.app["service"]
    return web.json_response({"ok": service.stt is not None, "pipecat": "1.8.1", "stt": "faster-whisper tiny.en cpu-int8"})


async def websocket(request: web.Request) -> web.WebSocketResponse:
    service: LiveConversationService = request.app["service"]
    socket = web.WebSocketResponse(heartbeat=20)
    await socket.prepare(request)
    audio = bytearray()
    turn_task: asyncio.Task[None] | None = None
    agent_task: asyncio.Task[None] | None = None

    async def run_agent(transcript: str) -> None:
        nonlocal agent_task
        work = asyncio.create_task(service.agent_reply(transcript))
        started = time.monotonic()
        try:
            await socket.send_json({"type": "agent_status", "state": "working"})
            reply = await work
            await service.send_spoken_response(socket, reply, "agent", f"agent-{time.monotonic_ns()}")
            await socket.send_json({"type": "agent_status", "state": "complete", "elapsed_ms": round((time.monotonic() - started) * 1000)})
            await socket.send_json({"type": "state", "state": "listening"})
        except asyncio.CancelledError:
            work.cancel()
            raise
        except Exception as error:
            await service.send_spoken_response(socket, f"The agent ran into a problem: {error}", "agent", f"agent-error-{time.monotonic_ns()}")
            await socket.send_json({"type": "agent_status", "state": "error"})
        finally:
            agent_task = None

    async def run_turn(turn: bytes) -> None:
        nonlocal agent_task
        handoff = await service.process_turn(socket, turn, agent_pending=agent_task is not None)
        if handoff and agent_task is None:
            transcript, _ = handoff
            agent_task = asyncio.create_task(run_agent(transcript))
    async for message in socket:
        if message.type != WSMsgType.TEXT:
            continue
        payload = json.loads(message.data)
        kind = payload.get("type")
        if kind == "input_audio_buffer.speech_started":
            await socket.send_json({"type": "output_audio_buffer.cleared"})
        elif kind == "start":
            audio.clear()
        elif kind == "audio" and len(audio) < SAMPLE_RATE * 2 * 30:
            audio.extend(base64.b64decode(payload.get("audioBase64", ""), validate=True))
        elif kind == "commit" and audio:
            if turn_task and not turn_task.done():
                turn_task.cancel()
            turn = bytes(audio)
            audio.clear()
            turn_task = asyncio.create_task(run_turn(turn))
        elif kind == "client_event":
            LOGGER.info("client_event event=%s detail=%s", payload.get("event", ""), payload.get("detail", ""))
    if turn_task and not turn_task.done():
        turn_task.cancel()
    if agent_task and not agent_task.done():
        agent_task.cancel()
    return socket


async def build_app(args: argparse.Namespace) -> web.Application:
    service = LiveConversationService(args.session_key, args.tts_speed)
    await service.start()
    app = web.Application()
    app["service"] = service
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/ws", websocket)
    async def cleanup(_: web.Application) -> None:
        await service.stop()

    app.on_cleanup.append(cleanup)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--session-key", default=DEFAULT_SESSION_KEY)
    parser.add_argument("--tts-speed", type=float, default=1.0)
    args = parser.parse_args()
    web.run_app(build_app(args), host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
