#!/usr/bin/env python3
"""Dedicated low-latency voice service for the Android Dashboard."""

from __future__ import annotations

import argparse
import ast
import asyncio
import base64
from dataclasses import asdict, dataclass
from datetime import datetime
import json
import logging
import operator
import os
from pathlib import Path
import re
import struct
import time
from typing import Any
from zoneinfo import ZoneInfo

LOGGER = logging.getLogger("live_conversation")

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


@dataclass
class TurnMetrics:
    asr_ms: int = 0
    routing_ms: int = 0
    response_ms: int = 0
    tts_ms: int = 0
    total_ms: int = 0
    route: str = ""


def _safe_arithmetic(expression: str) -> float | int:
    operations = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
        ast.USub: operator.neg,
        ast.UAdd: operator.pos,
    }

    def evaluate(node: ast.AST) -> float | int:
        if isinstance(node, ast.Expression):
            return evaluate(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.UnaryOp) and type(node.op) in operations:
            return operations[type(node.op)](evaluate(node.operand))
        if isinstance(node, ast.BinOp) and type(node.op) in operations:
            left = evaluate(node.left)
            right = evaluate(node.right)
            if isinstance(node.op, ast.Pow) and abs(float(right)) > 8:
                raise ValueError("exponent too large")
            return operations[type(node.op)](left, right)
        raise ValueError("unsupported arithmetic")

    if len(expression) > 80:
        raise ValueError("expression too long")
    return evaluate(ast.parse(expression, mode="eval"))


def route_simple(text: str, now: datetime | None = None) -> str | None:
    """Answer only deterministic, context-free requests locally."""
    normalized = re.sub(r"[^a-z0-9+*/().%^-]+", " ", text.lower()).strip()
    clock = now or datetime.now(ZoneInfo("America/Detroit"))

    if re.fullmatch(r"(?:hello|hi|hey|good morning|good afternoon|good evening)(?: there)?", normalized):
        return "Hello. I'm listening."
    if re.fullmatch(r"(?:thanks|thank you|cheers|okay|ok|got it|sounds good)", normalized):
        return "You're welcome."
    if re.search(r"\b(?:can you hear me|are you there|are you listening)\b", normalized):
        return "Yes. I can hear you clearly."
    if re.search(r"\b(?:who are you|what are you)\b", normalized):
        return "I'm your OpenClaw voice assistant."
    if re.search(r"\b(?:what(?:'s| is) the time|what time is it|current time)\b", text.lower()):
        return f"It is {clock.strftime('%-I:%M %p')}."
    if re.search(r"\b(?:what(?:'s| is) the date|what day is it|today(?:'s| is) date|current date)\b", text.lower()):
        return f"Today is {clock.strftime('%A, %B %-d')}."

    arithmetic = normalized
    replacements = {
        "what is ": "",
        "what s ": "",
        "calculate ": "",
        "plus": "+",
        "minus": "-",
        "times": "*",
        "multiplied by": "*",
        "divided by": "/",
    }
    lowered = text.lower().strip().rstrip("?.!")
    for source, target in replacements.items():
        lowered = lowered.replace(source, target)
    candidate = re.sub(r"\s+", "", lowered)
    if re.fullmatch(r"[0-9+*/().%^-]+", candidate) and re.search(r"[+*/%^]|\d-\d", candidate):
        try:
            value = _safe_arithmetic(candidate.replace("^", "**"))
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            return f"The answer is {value}."
        except (ArithmeticError, SyntaxError, ValueError):
            return None
    return None


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


class PersistentTtsWorker:
    def __init__(self, command: str, runtime_dir: str, model_dir: str, speed: float = 0.85):
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

    async def send_spoken_response(
        self,
        socket: web.WebSocketResponse,
        text: str,
        route: str,
        response_id: str,
        message_type: str = "reply",
    ) -> int:
        """Synthesize and stream one complete spoken response."""
        await socket.send_json({
            "type": message_type,
            "text": text,
            "route": route,
            "responseId": response_id,
        })
        await socket.send_json({"type": "state", "state": "speaking", "route": route})
        tts_started = time.perf_counter()
        pcm = await self.tts.synthesize(text)
        tts_ms = round((time.perf_counter() - tts_started) * 1000)
        LOGGER.info(
            "response_ready id=%s route=%s chars=%d pcm_bytes=%d tts_ms=%d",
            response_id, route, len(text), len(pcm), tts_ms,
        )
        await socket.send_json({"type": "output_audio_buffer.started", "responseId": response_id})
        chunk_bytes = OUTPUT_SAMPLE_RATE * 2 // 10
        for offset in range(0, len(pcm), chunk_bytes):
            await socket.send_json({
                "type": "response.output_audio.delta",
                "responseId": response_id,
                "sampleRate": OUTPUT_SAMPLE_RATE,
                "audioBase64": base64.b64encode(pcm[offset:offset + chunk_bytes]).decode("ascii"),
            })
        await socket.send_json({"type": "response.output_audio.done", "responseId": response_id})
        return tts_ms

    async def process_turn(self, socket: web.WebSocketResponse, audio: bytes) -> None:
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
            reply = route_simple(transcript)
            metrics.routing_ms = round((time.perf_counter() - stage) * 1000)
            if reply is not None:
                metrics.route = "direct"
            else:
                metrics.route = "agent"
                acknowledgment_id = f"ack-{time.monotonic_ns()}"
                await self.send_spoken_response(
                    socket,
                    "I'll check and let you know.",
                    "agent",
                    acknowledgment_id,
                    message_type="acknowledgment",
                )
                await socket.send_json({"type": "state", "state": "thinking", "route": "agent"})
                agent_started = time.perf_counter()
                reply = await self.agent_reply(transcript)
                metrics.response_ms = round((time.perf_counter() - agent_started) * 1000)

            response_id = f"response-{time.monotonic_ns()}"
            metrics.tts_ms = await self.send_spoken_response(
                socket, reply, metrics.route, response_id
            )
            metrics.total_ms = round((time.perf_counter() - started) * 1000)
            await socket.send_json({"type": "metrics", **asdict(metrics)})
            await socket.send_json({"type": "state", "state": "listening"})
        except Exception as error:
            await socket.send_json({"type": "error", "message": str(error)})


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
    active_task: asyncio.Task[None] | None = None
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
            if active_task and not active_task.done():
                active_task.cancel()
            turn = bytes(audio)
            audio.clear()
            active_task = asyncio.create_task(service.process_turn(socket, turn))
        elif kind == "client_event":
            LOGGER.info("client_event event=%s detail=%s", payload.get("event", ""), payload.get("detail", ""))
    if active_task and not active_task.done():
        active_task.cancel()
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
    parser.add_argument("--tts-speed", type=float, default=0.85)
    args = parser.parse_args()
    web.run_app(build_app(args), host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
