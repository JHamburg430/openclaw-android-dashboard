#!/usr/bin/env python3
"""Dedicated low-latency voice service for the Android Dashboard."""

from __future__ import annotations

import argparse
import asyncio
import base64
from collections import Counter, deque
from dataclasses import asdict, dataclass, replace
from datetime import datetime
import json
import inspect
import logging
import os
from pathlib import Path
import re
import secrets
import struct
import time
from typing import Any, AsyncIterator, Awaitable, Callable
from urllib.parse import urlsplit
import wave
from zoneinfo import ZoneInfo

LOGGER = logging.getLogger("live_conversation")

import aiohttp
from aiohttp import WSMsgType, web
import numpy as np
from pipecat.services.whisper.stt import WhisperSTTService
from pipecat.transcriptions.language import Language
from semantic_turn import SemanticTurnDetector, TurnDecision


SAMPLE_RATE = 16_000
OUTPUT_SAMPLE_RATE = 24_000
PLAYBACK_PREFILL_SECONDS = 0.3
DEFAULT_PORT = 8790
DEFAULT_SESSION_KEY = "agent:main:live-conversation"
DEFAULT_NODE_COMMAND = "/home/john/nodejs/bin/node"
DEFAULT_OPENCLAW_MODULE = "/home/john/nodejs/lib/node_modules/openclaw/openclaw.mjs"
DEFAULT_TTS_WORKER = str(Path(__file__).with_name("openclaw-kokoro-tts-worker"))
DEFAULT_TTS_RUNTIME = "/home/john/.openclaw/tools/sherpa-onnx-tts/runtime"
DEFAULT_TTS_MODEL_DIR = "/home/john/.openclaw/tools/sherpa-onnx-tts/models/kokoro-en-v0_19"
DEFAULT_TTS_SPEAKER_ID = 9  # bm_george, a British male voice
DEFAULT_TTS_BACKEND = "kokoro"
DEFAULT_QWEN_TTS_URL = "http://127.0.0.1:8792/v1/audio/speech"
DEFAULT_QWEN_TTS_MODEL = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
DEFAULT_QWEN_TTS_VOICE = "aiden"
DEFAULT_QWEN_TTS_INSTRUCTIONS = (
    "Speak naturally as a calm, concise personal assistant. Use conversational "
    "pacing, clear phrasing, and subtle warmth without sounding theatrical."
)
DEFAULT_QWEN_INITIAL_CHUNK_FRAMES = 10
SPEECH_MODEL_URL = "http://127.0.0.1:11439/api/chat"
# Large enough for materially better natural-language supervision while staying
# within the sub-second warm-response budget on the local Ollama GPUs.
# Use a dedicated Ollama model identity so unrelated qwen3.5 requests with a
# different context size cannot replace Live Conversation's warm runner.
SPEECH_MODEL = "openclaw-live-conversation:4b"
SPEECH_MODEL_KEEP_ALIVE = "30m"
SPEECH_MODEL_CONTEXT = 8_192
SPEECH_NUM_PREDICT = 384
SPEECH_RETRY_NUM_PREDICT = 640
DEGRADED_SPEECH_TIMEOUT_SECONDS = 8
AGENT_SENTINEL = "[[OPENCLAW_AGENT]]"
NEW_AGENT_SENTINEL = "[[OPENCLAW_NEW]]"
SAY_SENTINEL = "[[SAY]]"
IGNORE_SENTINEL = "[[IGNORE]]"
SPEECH_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "route": {
            "type": "string",
            "enum": ["direct", "agent", "new_agent", "session", "ignore"],
        },
        "reply": {"type": "string"},
        "session_key": {"type": ["string", "null"]},
    },
    "required": ["route", "reply", "session_key"],
    "additionalProperties": False,
}
TURN_UNDERSTANDING_SCHEMA = {
    "type": "object",
    "properties": {
        "complete": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "speech_act": {
            "type": "string",
            "enum": [
                "request", "question", "answer", "statement", "correction",
                "continuation", "meta", "control", "ambient",
            ],
        },
        "relation": {
            "type": "string",
            "enum": ["new", "continuation", "correction", "meta"],
        },
        "actionable": {"type": "boolean"},
        "requires_grounding": {"type": "boolean"},
        "supersedes_previous": {"type": "boolean"},
        "clarification_needed": {"type": "boolean"},
        "assembled_text": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": [
        "complete", "confidence", "speech_act", "relation", "actionable",
        "requires_grounding", "supersedes_previous", "clarification_needed",
        "assembled_text", "reason",
    ],
    "additionalProperties": False,
}
SEMANTIC_ENDPOINT_SCHEMA = {
    "type": "object",
    "properties": {
        "complete": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["complete", "confidence"],
    "additionalProperties": False,
}
FRAGMENT_RESOLUTION_SCHEMA = {
    "type": "object",
    "properties": {
        "relation": {
            "type": "string",
            "enum": ["new", "continuation", "correction"],
        },
        "complete": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "assembled_text": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["relation", "complete", "confidence", "assembled_text", "reason"],
    "additionalProperties": False,
}
SPEECH_OUTPUT_SCHEMA["properties"].update(TURN_UNDERSTANDING_SCHEMA["properties"])
SPEECH_OUTPUT_SCHEMA["required"].extend(TURN_UNDERSTANDING_SCHEMA["required"])
CAPABILITY_CACHE_SECONDS = 60
SESSION_CACHE_SECONDS = 3
SESSION_POLL_SECONDS = 10
PARTIAL_TRANSCRIPT_INTERVAL_SECONDS = 1.0
ONLINE_ASR_WINDOW_SECONDS = 14.0
ONLINE_ASR_OVERLAP_SECONDS = 2.0
BACKCHANNEL_DISPLAY_TEXT = ""
SMART_TURN_MODEL_PATH = os.environ.get(
    "SMART_TURN_MODEL_PATH",
    "/home/john/.openclaw/tools/pipecat-live-conversation/models/smart-turn-v3.2-cpu.onnx",
)
AGENT_RESULT_POLL_SECONDS = 3.0
AGENT_RESULT_WAIT_SECONDS = 600.0
DEFAULT_HISTORY_PATH = "/home/john/.openclaw/state/live-conversation-history.json"
DEFAULT_SETTINGS_PATH = "/home/john/.openclaw/state/live-conversation-settings.json"
DEFAULT_RECORDINGS_PATH = "/home/john/.openclaw/state/live-conversation-recordings"
DEFAULT_DEBUG_PATH = "/home/john/.openclaw/state/live-conversation-debug"
DEFAULT_DASHBOARD_REPO = "/home/john/openclaw-android-dashboard"
MAX_HISTORY_MESSAGES = 120
MAX_HISTORY_CHARS = 72_000
MAX_CONVERSATION_EVENTS = 500
DEFAULT_RECORDING_RETENTION_DAYS = 30
DEFAULT_DEBUG_RETENTION_DAYS = 14
DEFAULT_RECORDING_MAX_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_DEBUG_MAX_BYTES = 512 * 1024 * 1024
MAX_HTTP_BODY_BYTES = 64 * 1024
MAX_WS_MESSAGE_BYTES = 512 * 1024
MAX_TURN_AUDIO_BYTES = SAMPLE_RATE * 2 * 10 * 60
MAX_PENDING_TURNS = 4
MAX_LIVE_CONNECTIONS = 4
MAX_WAKE_CONNECTIONS = 2
# Routing only needs the immediate conversational neighborhood. A larger window
# repeatedly filled the entire 8k model context, adding seconds of prompt
# evaluation and duplicating history already supplied to the controller.
PROMPT_HISTORY_MESSAGES = 8
PROMPT_HISTORY_CHARS = 4_000
WAKE_WORD = "jarvis"
WAKE_WORD_ALIASES = frozenset(("jarvis", "jervis"))
WAKE_WINDOW_SECONDS = 2.5
WAKE_COOLDOWN_SECONDS = 5.0


def _secure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def _secure_atomic_write(path: Path, content: str) -> None:
    _secure_directory(path.parent)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)
    path.chmod(0o600)


def _origin_matches_request(request: web.Request) -> bool:
    """Accept native clients without Origin and same-origin browser clients only."""
    origin = request.headers.get("Origin")
    if not origin:
        return True
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and parsed.netloc == request.host


def _prometheus_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_:]", "_", value)


def _increment_metric(service: Any, name: str, value: int = 1) -> None:
    counters = getattr(service, "metric_counters", None)
    if counters is not None:
        counters[name] += value


def _words(text: str) -> list[str]:
    return re.findall(r"\S+", re.sub(r"\s+", " ", text).strip())


@dataclass
class OnlineTranscript:
    """LocalAgreement transcript with immutable and revisable prefixes."""

    stable_words: list[str]
    previous_words: list[str]
    unstable_words: list[str]
    decode_from_sample: int = 0

    @classmethod
    def empty(cls) -> "OnlineTranscript":
        return cls([], [], [])

    def update(self, hypothesis: str, audio_samples: int) -> tuple[str, str]:
        current = self._without_stable_overlap(_words(hypothesis))
        common = 0
        for old, new in zip(self.previous_words, current):
            if old.casefold().strip(".,?!") != new.casefold().strip(".,?!"):
                break
            common += 1
        # Keep two words revisable; Whisper commonly repairs the newest phrase.
        commit = max(0, common - 2)
        if commit:
            self.stable_words.extend(current[:commit])
            current = current[commit:]
            self.previous_words = self.previous_words[commit:]
            self.decode_from_sample = max(
                0,
                audio_samples - round(ONLINE_ASR_OVERLAP_SECONDS * SAMPLE_RATE),
            )
        self.previous_words = current
        self.unstable_words = current
        return " ".join(self.stable_words), " ".join(self.unstable_words)

    def display(self) -> str:
        return " ".join((*self.stable_words, *self.unstable_words)).strip()

    def final(self, hypothesis: str) -> str:
        tail = self._without_stable_overlap(_words(hypothesis))
        return " ".join((*self.stable_words, *tail)).strip()

    def _without_stable_overlap(self, tail: list[str]) -> list[str]:
        maximum = min(len(self.stable_words), len(tail), 12)
        for size in range(maximum, 0, -1):
            left = [word.casefold().strip(".,?!") for word in self.stable_words[-size:]]
            right = [word.casefold().strip(".,?!") for word in tail[:size]]
            if left == right:
                return tail[size:]
        return tail


def tts_speed_for(text: str, route: str = "direct", base: float = 1.08) -> float:
    """Choose restrained conversational cadence supported by Kokoro."""
    normalized = text.strip()
    if route == "backchannel":
        return min(1.28, base + 0.10)
    if re.search(r"\b(?:sorry|unfortunately|careful|warning|failed|can't|cannot)\b", normalized, re.I):
        return max(0.96, base - 0.10)
    if "?" in normalized or len(normalized) < 45:
        return min(1.24, base + 0.04)
    if len(normalized) > 260:
        return min(1.20, base + 0.02)
    return base


def affirmative_hum_pcm() -> bytes:
    """Generate a short nonverbal two-part affirmative hum as PCM16.

    Kokoro does not implement eSpeak's ``[[phoneme]]`` syntax and pronounced
    that marker as letters. This signal never passes through a text model.
    """
    parts: list[np.ndarray] = []
    for duration, start_hz, end_hz, amplitude in (
        (0.20, 138.0, 148.0, 0.18),
        (0.24, 158.0, 172.0, 0.21),
    ):
        count = round(OUTPUT_SAMPLE_RATE * duration)
        frequency = np.linspace(start_hz, end_hz, count, dtype=np.float64)
        phase = 2.0 * np.pi * np.cumsum(frequency) / OUTPUT_SAMPLE_RATE
        voiced = (
            np.sin(phase)
            + 0.34 * np.sin(2.0 * phase + 0.15)
            + 0.12 * np.sin(3.0 * phase + 0.35)
        )
        envelope = np.sin(np.linspace(0.0, np.pi, count, dtype=np.float64)) ** 1.4
        parts.append(voiced * envelope * amplitude)
        parts.append(np.zeros(round(OUTPUT_SAMPLE_RATE * 0.055), dtype=np.float64))
    samples = np.concatenate(parts[:-1])
    return np.clip(np.rint(samples * 32767.0), -32768, 32767).astype("<i2").tobytes()


def conversation_entities(text: str) -> dict[str, list[str]]:
    """Extract compact topic/entity keys for durable referent resolution."""
    normalized = re.sub(r"\s+", " ", text).strip()
    stop = {
        "about", "agent", "another", "could", "have", "please", "session",
        "start", "that", "this", "with", "would", "your", "into", "from",
    }
    terms = [
        word for word in re.findall(r"[a-z0-9][a-z0-9._-]+", normalized.lower())
        if len(word) > 2 and word not in stop
    ]
    proper = re.findall(r"\b[A-Z][A-Za-z0-9._-]{2,}\b", normalized)
    return {
        "topics": list(dict.fromkeys(terms))[:16],
        "named_entities": list(dict.fromkeys(proper))[:8],
    }


def is_wake_word(transcript: str) -> bool:
    normalized = re.sub(r"[^a-z]+", " ", transcript.lower()).strip()
    return bool(WAKE_WORD_ALIASES.intersection(normalized.split()))


def is_silent_stop_command(transcript: str) -> bool:
    """Recognize spoken playback-stop commands that must never get a reply."""
    normalized = re.sub(r"[^a-z']+", " ", transcript.lower()).strip()
    return bool(re.fullmatch(
        r"(?:(?:hey )?jarvis )?(?:(?:can|could|would|will) you )?(?:please )?"
        r"(?:stop(?: (?:talking|speaking))?|be quiet|quiet|shut up|"
        r"that's enough|that is enough)(?: please| now)?(?: jarvis)?",
        normalized,
    ))


def repair_known_transcription_errors(transcript: str) -> str:
    """Repair recurring, context-specific ASR substitutions before agent handoff."""
    return re.sub(
        r"\bfive\s+(agent|conversation\s+model)\b",
        lambda match: "live " + match.group(1),
        transcript,
        flags=re.IGNORECASE,
    )


def direct_voice_surface_reply(transcript: str) -> str | None:
    """Answer voice-presence and identity checks without model interpretation."""
    normalized = re.sub(r"[^a-z0-9']+", " ", transcript.lower()).strip()
    hearing_check = re.search(
        r"\b(?:can|could|do|did|are) you (?:actually )?(?:able to )?hear(?:ing)? me\b",
        normalized,
    )
    environmental_state = re.search(
        r"\b(?:closed|minimized|minimised|background|locked|screen off|app off)\b",
        normalized,
    )
    if hearing_check and not environmental_state:
        return "Yes, I can hear you clearly."
    if normalized in {
        "who are you",
        "what are you",
        "are you jarvis",
        "are you the live conversation model",
        "are you the live conversation agent",
    }:
        return (
            "I'm Jarvis, the model operating Live Conversation. "
            "I answer here directly and use gateway agents when tool-backed work is needed."
        )
    return None


def confirmation_policy_command(transcript: str) -> str | None:
    """Recognize voice commands that configure action confirmation."""
    normalized = re.sub(r"[^a-z0-9']+", " ", transcript.lower()).strip()
    if re.search(r"\b(?:what is|what's|tell me)\b.*\bconfirmation (?:mode|setting|policy)\b", normalized):
        return "status"
    confirmation = re.search(r"\b(?:confirm|confirmation|ask me|permission|approval)\b", normalized)
    action = re.search(r"\b(?:action|actions|act|acting|anything|proceed|doing|do it)\b", normalized)
    if not confirmation:
        return None
    if re.search(r"\b(?:turn|turning|switch|set|enable|activate)\b.*\b(?:on|required|always)\b", normalized):
        return "confirm"
    if re.search(r"\b(?:turn|turning|switch|set|disable|deactivate)\b.*\b(?:off|automatic|never)\b", normalized):
        return "automatic"
    if not action:
        return None
    if re.search(
        r"\b(?:don't|do not|never) (?:ask|require|need)\b|"
        r"\b(?:without|no) (?:asking|confirmation|permission|approval)\b|"
        r"\b(?:disable|turn off|stop asking)\b",
        normalized,
    ):
        return "automatic"
    if re.search(
        r"\b(?:always|require|enable|turn on)\b|"
        r"\bask me\b.*\b(?:before|first)\b|"
        r"\bget (?:my )?(?:confirmation|permission|approval)\b",
        normalized,
    ):
        return "confirm"
    return None


def confirmation_answer(transcript: str) -> bool | None:
    """Resolve a short answer to a pending action-confirmation question."""
    normalized = re.sub(r"[^a-z0-9']+", " ", transcript.lower()).strip()
    if len(normalized) > 80:
        return None
    if re.fullmatch(r"(?:yes|yeah|yep|sure|okay|ok|go ahead|proceed|do it|please do|confirm)(?: please)?", normalized):
        return True
    if re.fullmatch(
        r"(?:no|nope)(?: cancel(?: it| that)?)?|"
        r"(?:cancel|stop|don't|do not|never mind|nevermind)(?: it| that)?",
        normalized,
    ):
        return False
    return None


def has_stale_identity_confusion(text: str) -> bool:
    """Detect the known false claim that Jarvis and Live Conversation are separate."""
    normalized = re.sub(r"\s+", " ", text.lower())
    return bool(
        re.search(r"\bnot (?:the )?live conversation model\b", normalized)
        or re.search(
            r"\blive conversation (?:model|agent) is (?:running|separate|within)",
            normalized,
        )
    )


def is_operational_acknowledgment(text: str) -> bool:
    """Exclude prior routing acknowledgments from future routing examples."""
    normalized = re.sub(r"\s+", " ", text.lower()).strip()
    return bool(
        re.search(
            r"\b(?:i'll|i will|let me)\b.*\b(?:agent|monitor(?:ing)?|verif(?:y|ying)|"
            r"investigat(?:e|ing)|inspect(?:ing)?|fix(?:ing)?|updat(?:e|ing)|"
            r"check(?:ing)?|spawn(?:ing)?|launch(?:ing)?|adjust(?:ing)?|tun(?:e|ing)|"
            r"configur(?:e|ing)|improv(?:e|ing)|refin(?:e|ing)|keep .*sessions? active)\b",
            normalized,
        )
        or re.search(r"\b(?:spawned|started|launched) (?:a|an|the|another) agent\b", normalized)
    )


def remove_unrequested_action_promises(text: str) -> str:
    """Keep conversational content while removing invented future operations."""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    kept = [sentence for sentence in sentences if not is_operational_acknowledgment(sentence)]
    return " ".join(kept).strip() or "I understand."


def is_gateway_status_question(transcript: str) -> bool:
    """Recognize broad requests for currently running gateway work."""
    normalized = re.sub(r"[^a-z0-9]+", " ", transcript.lower()).strip()
    # An action can mention both a task/agent and a UI "status" or "update"
    # without asking for gateway-session state. Keep explicit delegation out of
    # this deterministic shortcut so the semantic router receives the request.
    delegation = (
        re.search(r"^(?:please )?(?:task|assign)\b.*\b(?:agent|subagent)\b", normalized)
        or re.search(
            r"\b(?:have|ask|tell)\b.{0,80}\b(?:agent|subagent)\b.{0,40}"
            r"(?:(?:to|with)\b.{0,40})?\b(?:fix|change|update|review|inspect|check|"
            r"investigate|add|remove|build|deploy)\w*\b",
            normalized,
        )
    )
    if delegation:
        return False
    subject = re.search(r"\b(?:sessions?|agents?|subagents?|tasks?|runs?)\b", normalized)
    status = re.search(r"\b(?:status|running|active|progress|update|working)\b", normalized)
    return bool(subject and status and not re.search(r"\b(?:start|launch|create)\b", normalized))


def is_referential_agent_question(transcript: str) -> bool:
    """Recognize questions or follow-ups aimed at the most recently launched agent."""
    normalized = re.sub(r"[^a-z0-9']+", " ", transcript.lower()).strip()
    return bool(re.search(
        r"\b(?:this|that|the|it)\s+(?:sub\s*)?agent\b|\b(?:it|that one)\b",
        normalized,
    ))


def summarize_tracked_agent(
    tracked: dict[str, Any], gateway: dict[str, Any] | None = None
) -> str:
    """Answer an anaphoric status question about one exact voice-launched session."""
    request = re.sub(r"\s+", " ", str(tracked.get("request") or "the requested task")).strip()
    request = request[:180].rstrip(" .?!")
    state = str(tracked.get("state") or "running")
    if gateway:
        if gateway.get("hasActiveRun"):
            state = "running"
        elif gateway.get("status") in {"failed", "error"}:
            state = "failed"
        elif gateway.get("status") in {"done", "completed"}:
            state = "complete"
    if state == "running":
        return f"The agent assigned to this task is still running: {request}."
    if state == "failed":
        return f"The agent assigned to this task stopped with an error: {request}."
    return f"The agent assigned to this task has completed: {request}."


def has_explicit_action_request(transcript: str) -> bool:
    """Require affirmative request language before any tool-backed route."""
    normalized = re.sub(r"[^a-z0-9']+", " ", transcript.lower()).strip()
    return bool(
        re.search(r"\b(?:can|could|would|will) you\b|\bplease\b", normalized)
        or re.search(r"\b(?:i need|i want) you to\b", normalized)
        or re.search(r"\b(?:have|ask|tell|task|assign|get)\b.*\bagent\b", normalized)
        or re.match(
            r"^(?:also )?(?:fix|change|update|monitor|verify|review|inspect|check|"
            r"start|spawn|launch|create|assign|send|set|add|remove|stop|run|build|deploy)\b",
            normalized,
        )
        or re.search(
            r"\bneeds? (?:to be )?(?:fixed|changed|updated|reviewed|checked|investigated)\b",
            normalized,
        )
    )


def requires_tool_backed_action(transcript: str) -> bool:
    """Identify explicit operations for the deterministic degraded router.

    ``has_explicit_action_request`` intentionally treats polite request language
    as authorization once the semantic model has classified a tool route.  The
    degraded router has no semantic classification, so it must not interpret a
    conversational request such as "please answer briefly" as permission to
    launch an agent.
    """
    normalized = re.sub(r"[^a-z0-9']+", " ", transcript.lower()).strip()
    if is_explicit_new_agent_request(normalized):
        return True
    if re.search(r"\b(?:have|ask|tell|task|assign|get)\b.*\b(?:agent|subagent)\b", normalized):
        return True
    operational = (
        r"fix|change|update|monitor|verify|review|inspect|check|investigate|"
        r"start|spawn|launch|create|assign|send|set|add|remove|stop|run|build|"
        r"deploy|install|configure|search|find|look up"
    )
    if re.match(rf"^(?:also |please )?(?:{operational})\b", normalized):
        return True
    if re.search(
        rf"\b(?:can|could|would|will) you (?:please )?(?:{operational})\b",
        normalized,
    ):
        return True
    return bool(re.search(
        r"\bneeds? (?:to be )?(?:fixed|changed|updated|reviewed|checked|investigated)\b",
        normalized,
    ))


def requires_authoritative_lookup(transcript: str) -> bool:
    """Route changeable facts and ambiguous live locations to grounded tools."""
    normalized = re.sub(r"\s+", " ", transcript).strip()
    lowered = normalized.lower()
    if re.match(r"^(?:i am|i'm) (?:testing|trying|using|reviewing|observing)\b", lowered):
        return False
    if re.search(
        r"\b(?:latest|currently|right now|today|tonight|recent|news|weather|"
        r"forecast|price|score|schedule|availability|verify|source)\b|"
        r"\bfacts? (?:about|on)\b",
        lowered,
    ):
        return True
    if re.search(
        r"\b(?:hear|listen|microphone|wake word)\b.*\b(?:app|screen|phone)\b.*"
        r"\b(?:closed|minimi[sz]ed|background|locked|off)\b|"
        r"\b(?:app|screen|phone)\b.*\b(?:closed|minimi[sz]ed|background|locked|off)\b.*"
        r"\b(?:hear|listen|microphone|wake word)\b",
        lowered,
    ):
        return True
    return bool(re.match(
        r"^where (?:is|are) (?:the )?[A-Z][\w'-]+(?: [A-Z][\w'-]+)+[?.!]*$",
        normalized,
        re.IGNORECASE,
    ))


def summarize_gateway_status(sessions: str, agent_pending: bool = False) -> str:
    """Build a short factual answer from the authoritative session catalog."""
    active: list[str] = []
    for line in sessions.splitlines():
        if "hasActiveRun=yes" not in line:
            continue
        title_match = re.search(r"(?:^| \| )title=([^|]+)", line)
        title = title_match.group(1).strip() if title_match else "Untitled session"
        if title not in active:
            active.append(title)
    if not active:
        if agent_pending:
            return "One agent request from this Live Conversation is still running."
        return "No gateway sessions are currently running."
    displayed = active[:4]
    names = "; ".join(displayed)
    remainder = len(active) - len(displayed)
    suffix = f"; plus {remainder} more" if remainder else ""
    noun = "session is" if len(active) == 1 else "sessions are"
    return f"{len(active)} gateway {noun} currently running: {names}{suffix}."


def is_explicit_new_agent_request(transcript: str) -> bool:
    """Recognize complete spoken commands that must create a separate session."""
    normalized = re.sub(r"[^a-z0-9]+", " ", transcript.lower()).strip()
    request = re.search(
        r"\b(?:start|spawn|launch|create)\b.*\b(?:new|another|separate|additional)?\s*"
        r"(?:sub\s*)?agent\b",
        normalized,
    )
    unfinished = re.search(r"\b(?:that|to|for|so)\s*$", normalized)
    return bool(request and not unfinished)


@dataclass
class TurnMetrics:
    asr_ms: int = 0
    routing_ms: int = 0
    response_ms: int = 0
    tts_ms: int = 0
    total_ms: int = 0
    route: str = ""


@dataclass(frozen=True)
class TurnUnderstanding:
    """One semantic contract shared by endpointing, routing, and recovery."""

    complete: bool
    confidence: float
    speech_act: str
    relation: str
    actionable: bool
    requires_grounding: bool
    supersedes_previous: bool
    assembled_text: str
    reason: str
    clarification_needed: bool = False


VALID_SPEECH_ACTS = frozenset({
    "request", "question", "answer", "statement", "correction",
    "continuation", "meta", "control", "ambient",
})
SPEECH_ACT_ALIASES = {
    "acknowledgement": "answer",
    "acknowledgment": "answer",
    "command": "request",
    "instruction": "request",
    "clarification": "correction",
    "repair": "correction",
    "observation": "statement",
    "feedback": "statement",
    "response": "answer",
}


def normalize_speech_act(value: Any, payload: dict[str, Any]) -> tuple[str, str | None]:
    """Canonicalize descriptive controller labels without changing its route.

    Speech-act taxonomy is diagnostic metadata. An otherwise valid semantic
    decision must not fail merely because the model uses a synonymous label.
    Unknown labels are derived from the controller's structured semantics,
    never from transcript keywords.
    """
    raw = re.sub(r"[\s-]+", "_", str(value or "").strip().lower())
    canonical = SPEECH_ACT_ALIASES.get(raw, raw)
    if canonical in VALID_SPEECH_ACTS:
        return canonical, raw if canonical != raw else None

    relation = str(payload.get("relation") or "").strip().lower()
    route = str(payload.get("route") or "").strip().lower()
    if relation == "meta":
        canonical = "meta"
    elif relation == "correction":
        canonical = "correction"
    elif relation == "continuation":
        canonical = "continuation"
    elif payload.get("actionable") is True or route in {"agent", "new_agent", "session"}:
        canonical = "request"
    elif payload.get("requires_grounding") is True:
        canonical = "question"
    elif route == "ignore":
        canonical = "ambient"
    else:
        canonical = "statement"
    return canonical, raw or "missing"


def turn_understanding_prompt(
    transcript: str,
    pending_fragment: str = "",
    recent_context: list[dict[str, str]] | None = None,
) -> str:
    context = json.dumps((recent_context or [])[-6:], ensure_ascii=False)
    return f"""You are the semantic turn controller for a realtime voice assistant.
Classify meaning, not isolated words. Your output controls endpointing and whether tools may run.

Current transcript: {json.dumps(transcript, ensure_ascii=False)}
Unresolved earlier fragment: {json.dumps(pending_fragment, ensure_ascii=False)}
Recent dialogue: {context}

Return exactly one JSON object containing every one of these keys, even when a value is empty:
`route`, `reply`, `session_key`, `complete`, `confidence`, `speech_act`, `relation`,
`actionable`, `requires_grounding`, `supersedes_previous`, `clarification_needed`,
`assembled_text`, and `reason`.
Never omit a key. `confidence` must be a JSON number from 0 through 1. `relation` must be exactly
one of `new`, `continuation`, `correction`, or `meta`; incompleteness belongs only in `complete`.
- `speech_act` must be exactly one of `request`, `question`, `answer`, `statement`, `correction`,
  `continuation`, `meta`, `control`, or `ambient`. Do not invent a synonym or a new category.
- `complete` evaluates whether the USER has finished expressing the current communicative act. It never requires the user to supply the answer to their own question. A self-contained question such as "What colour is a clear daytime sky?" is complete even if earlier dialogue contained unfinished tests. A fluent-sounding clause can still be incomplete: "What are the latest updates for the" is incomplete because its object is missing. Do not let punctuation, silence, acoustic endpoint confidence, or unrelated earlier turn patterns override the current meaning.
- `speech_act` describes the whole utterance. `meta` means the speaker is asking about or correcting the assistant's immediately preceding behavior, plan, wording, or claim.
- `relation` describes how this transcript relates to the unresolved fragment or recent dialogue. Use `continuation` when it completes the unresolved thought, `correction` when it replaces/repairs it, and `meta` for questions such as "What are you going to verify?" A self-contained question with no unresolved reference is `new`; do not force it into an earlier test pattern merely because topics or wording repeat.
- `actionable` is true only for an actual request, command, or unmistakable instruction to do work. Mentioning words such as latest, verify, agent, update, test, or fix is not authorization by itself.
- Requests to answer, explain, define, describe, calculate, or name timeless knowledge directly are not tool actions; set `actionable` false unless they also require current, private, or tool-backed evidence.
- `requires_grounding` is true only when answering the complete communicative act requires current/private/tool-backed evidence. A meta-question about what the assistant just said does not require grounding merely because it repeats "verify" or "latest".
- `supersedes_previous` is true when this turn corrects, retracts, or replaces the prior turn or its planned response.
- `clarification_needed` is true when the committed speech is too garbled, contradictory, or semantically incoherent to recover confidently. This is different from an intelligible unfinished thought: unfinished speech waits for a continuation, while unclear speech gets one brief request to repeat or rephrase it. Never use a backchannel as the answer to unclear speech.
- `assembled_text` is the exact complete meaning to route. Combine the unresolved fragment with this transcript only when they form one coherent thought. If the current transcript is incomplete, preserve it verbatim. If it is a new unrelated turn, use only the current transcript.
- When an unresolved fragment is nonempty, first test whether the current transcript can supply its missing subject, object, predicate, complement, condition, or proposition. Classify the current words alone only after rejecting that coherent assembly. A short noun phrase or clause may be a complete continuation even when it would be incomplete in isolation.
- `reason` is a short semantic explanation, never a keyword citation.

Complete examples (the reply wording may vary, but all keys are mandatory):
Current: "What are the latest updates for the"
{{"route":"direct","reply":"","session_key":null,"complete":false,"confidence":0.98,"speech_act":"question","relation":"new","actionable":false,"requires_grounding":false,"supersedes_previous":false,"clarification_needed":false,"assembled_text":"What are the latest updates for the","reason":"The requested subject is missing, so the thought is unfinished."}}

Pending: "What are the latest updates for the"; current: "Live Conversation project?"
{{"route":"agent","reply":"I'll check the latest Live Conversation project updates.","session_key":null,"complete":true,"confidence":0.97,"speech_act":"question","relation":"continuation","actionable":true,"requires_grounding":true,"supersedes_previous":false,"clarification_needed":false,"assembled_text":"What are the latest updates for the Live Conversation project?","reason":"The second fragment supplies the missing subject and asks for current information."}}

Pending: "Please answer only after both parts. What is the capital of?"; current: "The nation called Canada?"
{{"route":"direct","reply":"Ottawa is the capital of Canada.","session_key":null,"complete":true,"confidence":0.99,"speech_act":"question","relation":"continuation","actionable":false,"requires_grounding":false,"supersedes_previous":false,"clarification_needed":false,"assembled_text":"What is the capital of the nation called Canada?","reason":"The second transcription supplies the missing object of the pending question, so the two fragments form one complete turn."}}

Recent assistant: "I'll verify that"; current: "What are you going to verify?"
{{"route":"direct","reply":"I was referring to the subject of your previous request, but that request was cut off before you named it.","session_key":null,"complete":true,"confidence":0.99,"speech_act":"meta","relation":"meta","actionable":false,"requires_grounding":false,"supersedes_previous":true,"clarification_needed":false,"assembled_text":"What are you going to verify?","reason":"This asks about the assistant's stated plan rather than requesting a new verification."}}

Current: "I'm testing the latest update"
{{"route":"direct","reply":"Understood.","session_key":null,"complete":true,"confidence":0.99,"speech_act":"statement","relation":"new","actionable":false,"requires_grounding":false,"supersedes_previous":false,"clarification_needed":false,"assembled_text":"I'm testing the latest update","reason":"This reports the speaker's activity and does not request work."}}

Current: "Please verify the latest release"
{{"route":"agent","reply":"I'll verify the latest release.","session_key":null,"complete":true,"confidence":0.99,"speech_act":"request","relation":"new","actionable":true,"requires_grounding":true,"supersedes_previous":false,"clarification_needed":false,"assembled_text":"Please verify the latest release","reason":"This explicitly requests a current, tool-backed verification."}}

Current: "to fix the thing where instead of saying the same thing say the same thing"
{{"route":"direct","reply":"I didn't catch that clearly. Could you say it again?","session_key":null,"complete":false,"confidence":0.35,"speech_act":"correction","relation":"correction","actionable":false,"requires_grounding":false,"supersedes_previous":false,"clarification_needed":true,"assembled_text":"to fix the thing where instead of saying the same thing say the same thing","reason":"The recognized wording is contradictory and does not preserve a recoverable intended correction."}}
"""


def speech_model_prompt(
    now: datetime | None = None,
    agent_pending: bool = False,
    capabilities: str = "",
    sessions: str = "",
    voice_sessions: str = "",
    event_context: str = "",
    confirmation_required: bool = False,
) -> str:
    clock = now or datetime.now(ZoneInfo("America/Detroit"))
    pending_note = (
        "One or more agent requests launched by this voice connection are still awaiting a final response. "
        if agent_pending else
        "No agent request launched by this voice connection is currently awaiting a final response. "
    )
    confirmation_note = (
        "Action confirmation is ON. The bridge will ask before executing an action. "
        if confirmation_required else
        "Action confirmation is OFF. Explicit action requests execute after your acknowledgment. "
    )
    return f"""You are Jarvis, the model operating John's Live Conversation voice interface right now.
Jarvis and the Live Conversation model are the same speaker: both refer to you. You receive John's locally transcribed microphone input, choose how each turn is handled, and speak the response. A gateway agent is only a tool-backed work session that you may use; it is not a separate Live Conversation model. Never claim that you are merely a supervisor outside Live Conversation, and never ask an agent to verify whether you can hear John when the current utterance already arrived. Receiving one transcript proves only that the current capture path worked; it does not prove that listening continues while the app is closed, minimized, backgrounded, or the screen is locked. Questions about those runtime states require an authoritative refresh through an agent.
You are not a general chatbot pretending to lack system access. You can see the live gateway-session summary and capability catalog below, answer questions about them directly, route a follow-up into an existing session, or launch a separate agent session. {pending_note}
{confirmation_note}
Make each decision in this order: (1) determine whether John addressed you, (2) resolve references using the newest relevant conversation and session context, (3) distinguish conversation from an explicit request, (4) decide whether current evidence or a tool-backed refresh is required, (5) choose the least powerful route that can satisfy the request, and only then (6) write the spoken reply. Newer instructions override older ones.
Conversation history is memory for continuity, preferences, names, and referents—not evidence that changeable information is still true. Never answer a request for current status, availability, progress, configuration, messages, schedules, files, logs, or other time-sensitive facts by repeating history. Refresh the authoritative source through an agent or the live session catalog first. If a refresh is unavailable, say that the current state could not be verified. A completed historical action never prevents John from requesting the same action again.
For timeless factual questions, answer directly only when the fact is well established and you are highly confident. Otherwise choose `agent` to verify it. Never fill uncertainty with a plausible-sounding claim.
First decide whether John actually requested something. Statements describing what he is currently doing, testing, observing, or noticing are conversation—not authorization to start work. Words such as “test,” “updates,” “performance,” or an agent name do not make a statement actionable. Launch or message an agent only when the transcript contains a request, command, or unmistakable instruction. Never invent work such as monitoring, verifying, checking, or updating when John merely says he is testing something.
Return one JSON object matching the required schema. Choose `route` before writing `reply`:
- `direct`: conversation, observations, acknowledgments, timeless knowledge, or answers already available in this prompt. The reply must answer or acknowledge naturally and must not promise agent work.
- `agent`: an explicit request requiring apps, projects, private context, files, logs, calendar, messages, memory, current facts, tools, judgment, or a system action. The reply briefly acknowledges the work.
- `new_agent`: only when John explicitly requests another, new, separate, or additional agent. Multiple agents may work concurrently.
- `session`: only for a follow-up clearly aimed at one listed session. Copy its exact key into `session_key`; never invent one.
- `ignore`: only speech clearly not addressed to you and having no plausible conversational meaning. Use an empty reply.
For every action route (`agent`, `new_agent`, or `session`), `reply` is a brief, natural acknowledgment. Say what you are about to do; never imply the action already happened or include an unverified result. Once routing and authorization are final, the bridge dispatches the action while the acknowledgment is spoken. A silent stop command is the sole exception because its purpose is to stop speech immediately.
Use an empty `session_key` for every route except `session`. A question about status or currently running work is `direct`; answer it from the session summary. If a transcript is semantically unfinished, set `complete` false, use `direct` with an empty reply, and take no action; the bridge will keep listening for the continuation. Imperfect grammar alone is not grounds to ignore a turn.

Choose the answer shape before writing `reply`, based on John's request and the
amount of explanation genuinely needed:
- MICRO: use one word or one short clause for yes/no answers, acknowledgments,
  names, simple arithmetic, and other atomic facts. Do not pad it with a preamble.
- BRIEF: this is the default. Give the answer first, then at most one useful
  qualifying sentence. Prefer about 10–35 spoken words.
- SUMMARY: for status, comparisons, or multi-part results, lead with the outcome,
  then give two or three short sentences containing only the decisive points.
- DETAILED: use only when John asks for detail or the subject needs careful
  explanation. Start with a one-sentence orientation, develop one idea per short
  sentence, and use clear transitions. Do not compress several ideas into one breath.
Never announce the mode or say "here is a summary." Do not repeat the question.
Avoid filler, throat-clearing, Markdown, headings, parenthetical chains, semicolon
chains, and dense spoken lists. For up to three list items, use natural ordinal
transitions and a full sentence for each item. For a longer list, state the count,
say only the most important items, and offer the remainder if John wants it.
Write punctuation for speech: commas mark a light pause; periods separate complete
thoughts; a new sentence should sound intentional rather than rushed. Vary sentence
length naturally, but keep every sentence easy to say aloud in one breath.
The current local date and time is {clock.strftime('%A, %B %-d, %Y, %-I:%M %p')} America/Detroit; time and date questions can be answered directly.
Available OpenClaw capabilities (cached and refreshed automatically):
{capabilities or 'Capability catalog is temporarily unavailable; delegate capability questions to the agent.'}
Live and recent gateway sessions (refreshed automatically; hasActiveRun is authoritative):
{sessions or 'No active or recent gateway sessions were returned.'}
Sessions launched from this Live Conversation, newest first:
{voice_sessions or 'No session has been launched from this Live Conversation yet.'}
Recent structured conversation events (newest last):
{event_context or 'No corrections, interruptions, or tool events need additional context.'}
Resolve “this agent,” “that agent,” “it,” and similar follow-ups to the newest relevant Live Conversation session above. Preserve the subject and intent established by recent user turns. Do not replace a specific referent with a generic list of all sessions.
Use this catalog to answer capability questions quickly. If the user explicitly asks to use, configure, expand, or change a capability, choose `agent`. Do not claim an unavailable capability exists. Newly added agents and skills appear after the catalog refreshes.
Interpret likely recognition mistakes using the conversation and session context. In this voice app, “five agent” or “five conversation model” means “live agent” or “live conversation model” unless John explicitly discusses the number five or five distinct agents; correct that known ASR error without asking and use the corrected word “live” in the acknowledgment. Do not silently replace any other uncertain proper noun; ask a short clarification instead.
When John asks to make the live agent or Live Conversation model more capable, that is a complete actionable request: choose `agent` so the agent can review the conversation and current implementation. Do not ask which capabilities he means unless he explicitly presents alternatives requiring a choice.
Never fabricate private, project, or agent progress. Keep `reply` short and natural for speech. Complete every reply as a grammatical sentence; never end mid-sentence.
Examples:
User: Testing out the latest Live Conversation updates.
Assistant: {{"route":"direct","reply":"I hear you. Go ahead with the test.","session_key":""}}
User: I'm trying the new audio behavior.
Assistant: {{"route":"direct","reply":"I'm listening.","session_key":""}}
User: I'm testing the update; have an agent monitor the logs.
Assistant: {{"route":"agent","reply":"I’ll have the agent monitor the logs during your test.","session_key":""}}
User: How is the RAG app improvement going?
Assistant: {{"route":"direct","reply":"The Manuals RAG session is still active and working on retrieval accuracy.","session_key":""}}
User: Fix the routing bug.
Assistant: {{"route":"agent","reply":"I’ll have the agent inspect and fix the routing bug.","session_key":""}}
User: Also tell that active routing agent to check the reconnect path.
Assistant: {{"route":"session","reply":"I’ll add the reconnect check to that session.","session_key":"agent:main:dashboard:example"}}
User: Start another agent to inspect the audio cutoff.
Assistant: {{"route":"new_agent","reply":"I’ll start a separate agent to inspect the audio cutoff.","session_key":""}}
User: Make the five agent more capable.
Assistant: {{"route":"agent","reply":"I’ll have the agent review this conversation and improve the live agent’s capabilities.","session_key":""}}
User: Who wrote The Hobbit?
Assistant: {{"route":"direct","reply":"J. R. R. Tolkien wrote The Hobbit.","session_key":""}}
User: What time is it?
Assistant: {{"route":"direct","reply":"It is {clock.strftime('%-I:%M %p')}.","session_key":""}}
User: Are you able to hear me?
Assistant: {{"route":"direct","reply":"Yes, I can hear you clearly.","session_key":""}}
User: Are you the Live Conversation model?
Assistant: {{"route":"direct","reply":"Yes. I'm Jarvis, the model operating Live Conversation.","session_key":""}}"""


def parse_speech_model_output(text: str) -> tuple[str, str, str | None]:
    cleaned = text.strip()
    try:
        structured = json.loads(cleaned)
    except (ValueError, TypeError):
        structured = None
    if isinstance(structured, dict):
        route = structured.get("route")
        reply = structured.get("reply")
        session_key = structured.get("session_key")
        if route in {"direct", "agent", "new_agent", "session", "ignore"} and isinstance(reply, str):
            return route, reply.strip(), session_key.strip() if isinstance(session_key, str) and session_key.strip() else None
    if IGNORE_SENTINEL in cleaned:
        return "ignore", "", None
    session_match = re.search(r"\[\[OPENCLAW_SESSION:([^\]]+)\]\]", cleaned)
    if session_match:
        session_key = session_match.group(1).strip()
        acknowledgment = cleaned[session_match.end():].strip()
        return "session", acknowledgment, session_key
    if NEW_AGENT_SENTINEL in cleaned:
        acknowledgment = cleaned.split(NEW_AGENT_SENTINEL, 1)[1].strip()
        return "new_agent", acknowledgment, None
    if AGENT_SENTINEL in cleaned:
        acknowledgment = cleaned.split(AGENT_SENTINEL, 1)[1].strip()
        return "agent", acknowledgment, None
    if SAY_SENTINEL in cleaned:
        cleaned = cleaned.split(SAY_SENTINEL, 1)[1].strip()
    return "direct", cleaned, None


def parse_turn_understanding(text: str, transcript: str) -> TurnUnderstanding:
    """Parse safety fields strictly while normalizing descriptive taxonomy."""
    cleaned = text.strip()
    try:
        payload = json.loads(cleaned)
    except (TypeError, ValueError) as error:
        # Some local-model builds occasionally wrap schema-constrained output in
        # a Markdown fence or a sentence. Accept one complete JSON object while
        # keeping every field-level safety check below strict.
        payload = None
        decoder = json.JSONDecoder()
        for start in (index for index, character in enumerate(cleaned) if character == "{"):
            try:
                candidate, _ = decoder.raw_decode(cleaned[start:])
            except ValueError:
                continue
            if isinstance(candidate, dict):
                payload = candidate
                LOGGER.warning("semantic_controller_unwrapped_json")
                break
        if payload is not None:
            error = None
        else:
            # Preserve the legacy sentinel protocol during rolling upgrades. The
            # current JSON-schema path always supplies the semantic fields.
            route, _, _ = parse_speech_model_output(text)
            if not any(marker in text for marker in (
                AGENT_SENTINEL, NEW_AGENT_SENTINEL, SAY_SENTINEL, IGNORE_SENTINEL,
                "[[OPENCLAW_SESSION:",
            )):
                raise ValueError("semantic controller returned invalid JSON") from error
            action = route in {"agent", "new_agent", "session"}
            return TurnUnderstanding(
                True, 1.0, "request" if action else "statement", "new",
                action, False, False, transcript, "legacy sentinel decision",
            )
    if not isinstance(payload, dict):
        raise ValueError("semantic controller returned a non-object")
    if "speech_act" not in payload and {"route", "reply", "session_key"} <= payload.keys():
        route = payload.get("route")
        action = route in {"agent", "new_agent", "session"}
        return TurnUnderstanding(
            True, 1.0, "request" if action else "statement", "new",
            action, False, False, transcript, "legacy structured decision",
        )
    speech_act, normalized_from = normalize_speech_act(payload.get("speech_act"), payload)
    relation = payload.get("relation")
    if normalized_from:
        LOGGER.warning(
            "semantic_controller_normalized_speech_act raw=%r canonical=%s",
            normalized_from, speech_act,
        )
    if relation not in {"new", "continuation", "correction", "meta"}:
        raise ValueError("semantic controller returned an invalid relation")
    confidence = payload.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        raise ValueError("semantic controller returned invalid confidence")
    assembled = payload.get("assembled_text")
    if not isinstance(assembled, str) or not assembled.strip():
        assembled = transcript
    reason = str(payload.get("reason") or "semantic classification").strip()[:240]
    clarification_needed = payload.get("clarification_needed") is True
    # Enforce internal agreement in the model's structured decision. This does
    # not inspect transcript words: it repairs the case where the controller's
    # own semantic diagnosis says the input is unrecoverable/unclear but its
    # boolean contradicts that diagnosis.
    if not clarification_needed and payload.get("complete") is not True:
        normalized_reason = reason.casefold()
        diagnosed_unclear = any(phrase in normalized_reason for phrase in (
            "semantically contradictory", "semantically incoherent",
            "recognition error", "meaning is unclear", "warrant clarification",
            "cannot be interpreted", "too garbled", "unrecoverable",
        ))
        if diagnosed_unclear:
            clarification_needed = True
            LOGGER.warning(
                "semantic_controller_repaired_clarification_flag reason=%r", reason
            )
    return TurnUnderstanding(
        complete=payload.get("complete") is True,
        confidence=max(0.0, min(1.0, float(confidence))),
        speech_act=speech_act,
        relation=relation,
        actionable=payload.get("actionable") is True,
        requires_grounding=payload.get("requires_grounding") is True,
        supersedes_previous=payload.get("supersedes_previous") is True,
        assembled_text=re.sub(r"\s+", " ", assembled).strip(),
        reason=reason,
        clarification_needed=clarification_needed,
    )


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


def extract_agent_acknowledgment(payload: Any) -> str:
    """Extract a tool-owned acknowledgment without speaking raw tool JSON."""
    if isinstance(payload, str):
        try:
            return extract_agent_acknowledgment(json.loads(payload))
        except (ValueError, TypeError):
            return ""
    if isinstance(payload, dict):
        acknowledgment = payload.get("acknowledgment")
        if isinstance(acknowledgment, str) and acknowledgment.strip():
            return acknowledgment.strip()
        for key in ("arguments", "input", "content", "messages", "payloads", "result", "data"):
            if key in payload:
                found = extract_agent_acknowledgment(payload[key])
                if found:
                    return found
    if isinstance(payload, list):
        for value in reversed(payload):
            found = extract_agent_acknowledgment(value)
            if found:
                return found
    return ""


def latest_assistant_text(payload: Any) -> str:
    """Return visible assistant text produced after the newest delegated handoff."""
    messages = payload.get("messages", []) if isinstance(payload, dict) else []
    if not isinstance(messages, list):
        return ""
    delegation_index = -1
    for index, message in enumerate(messages):
        if extract_agent_acknowledgment(message):
            delegation_index = index
    for message in reversed(messages[delegation_index + 1:]):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts = [
                block.get("text", "").strip()
                for block in content
                if isinstance(block, dict)
                and block.get("type") in {"text", "output_text"}
                and isinstance(block.get("text"), str)
                and block.get("text", "").strip()
            ]
            if parts:
                return "\n".join(parts)
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
    # Session keys, UUIDs, epoch values, and nanosecond counters are useful on
    # screen but make local TTS recite long, meaningless digit sequences. Keep
    # the original display text and replace only the speech rendering.
    spoken = re.sub(
        r"\bagent:[a-z0-9._-]+(?::[a-z0-9._-]+)+",
        "the agent session",
        spoken,
        flags=re.IGNORECASE,
    )
    spoken = re.sub(
        r"(?<![a-f0-9])[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}(?![a-f0-9])",
        "the identifier",
        spoken,
        flags=re.IGNORECASE,
    )

    def replace_long_number(match: re.Match[str]) -> str:
        return "the numeric identifier" if len(match.group(0).replace(",", "")) >= 10 else match.group(0)

    spoken = re.sub(r"(?<![\w.])\d[\d,]*\d(?![\w.])", replace_long_number, spoken)
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


def split_spoken_text(text: str, max_chars: int = 90) -> list[str]:
    """Split long replies into buffered TTS units without sentence-boundary stalls."""
    normalized = normalize_spoken_text(text)
    if not normalized:
        return [""]
    sentences = re.split(r"(?<=[.!?])\s+", normalized)
    fragments: list[str] = []
    for sentence in sentences:
        while len(sentence) > max_chars:
            split_at = max(sentence.rfind(mark, 0, max_chars + 1) for mark in (", ", "; ", ": ", " "))
            if split_at <= 0:
                split_at = max_chars
            else:
                split_at += 1
            fragments.append(sentence[:split_at].strip())
            sentence = sentence[split_at:].strip()
        if sentence:
            fragments.append(sentence)

    # Keep the first synthesis unit short so playback starts promptly. Packing
    # fragments up to the cap still gives the one-part lookahead enough audio
    # to synthesize the next unit without an audible boundary gap.
    parts: list[str] = []
    current = ""
    for fragment in fragments:
        candidate = f"{current} {fragment}".strip()
        if current and len(candidate) > max_chars:
            parts.append(current)
            current = fragment
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts or [normalized]


def partial_structured_reply(text: str) -> tuple[str | None, str]:
    """Extract the route and currently decoded reply from partial JSON."""
    route_match = re.search(r'"route"\s*:\s*"([^"\\]*)"', text)
    # Never synthesize a provisional reply before the same semantic decision
    # has affirmatively classified the user's thought as complete.
    if not re.search(r'"complete"\s*:\s*true\b', text):
        return route_match.group(1) if route_match else None, ""
    reply_match = re.search(r'"reply"\s*:\s*"', text)
    if not reply_match:
        return route_match.group(1) if route_match else None, ""
    encoded = text[reply_match.end():]
    decoded: list[str] = []
    escaped = False
    unicode_digits: str | None = None
    for character in encoded:
        if unicode_digits is not None:
            if character.lower() not in "0123456789abcdef":
                break
            unicode_digits += character
            if len(unicode_digits) == 4:
                decoded.append(chr(int(unicode_digits, 16)))
                unicode_digits = None
                escaped = False
            continue
        if escaped:
            if character == "u":
                unicode_digits = ""
                continue
            decoded.append({
                "n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f",
                '"': '"', "\\": "\\", "/": "/",
            }.get(character, character))
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if character == '"':
            break
        decoded.append(character)
    return route_match.group(1) if route_match else None, "".join(decoded)


class IncrementalSpeechStream:
    """Speak stable direct-reply clauses while Ollama is still generating."""

    def __init__(
        self, service: "LiveConversationService", socket: web.WebSocketResponse,
        response_id: str, generation: int,
    ):
        self.service = service
        self.socket = socket
        self.response_id = response_id
        self.generation = generation
        self.spoken = ""
        self.started = False
        self.tts_ms = 0
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.worker: asyncio.Task[None] | None = None

    async def feed(self, structured_text: str) -> None:
        route, reply = partial_structured_reply(structured_text)
        if route != "direct" or not reply.startswith(self.spoken):
            return
        remainder = reply[len(self.spoken):]
        boundaries = list(re.finditer(r"[.!?](?:\s+|$)|[,;:](?:\s+)", remainder))
        if not boundaries:
            return
        end = boundaries[-1].end()
        piece = remainder[:end].strip()
        if len(piece) < 24 or is_operational_acknowledgment(piece):
            return
        self.spoken = reply[:len(self.spoken) + end]
        self._queue(piece)

    def _queue(self, text: str) -> None:
        if not text:
            return
        self.queue.put_nowait(text)
        if self.worker is None:
            self.worker = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            text = await self.queue.get()
            if text is None:
                return
            await self._speak(text)

    async def cancel(self) -> None:
        if self.worker is not None and not self.worker.done():
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                pass

    async def _speak(self, text: str) -> None:
        if not text or self.generation != self.service.speech_generation:
            return
        async with self.service.speech_lock:
            if self.generation != self.service.speech_generation:
                return
            async def begin_audio() -> None:
                if self.started:
                    return
                await self.socket.send_json({
                    "type": "state", "state": "speaking", "route": "direct"
                })
                await self.socket.send_json({
                    "type": "output_audio_buffer.started", "responseId": self.response_id
                })
                self.started = True

            async def send_chunk(chunk: bytes, total_bytes: int) -> bool:
                if self.generation != self.service.speech_generation:
                    return False
                await self.socket.send_json({
                    "type": "response.output_audio.delta",
                    "responseId": self.response_id,
                    "sampleRate": OUTPUT_SAMPLE_RATE,
                    "audioBase64": base64.b64encode(chunk).decode("ascii"),
                })
                if total_bytes > round(
                    OUTPUT_SAMPLE_RATE * 2 * PLAYBACK_PREFILL_SECONDS
                ):
                    await asyncio.sleep(len(chunk) / (OUTPUT_SAMPLE_RATE * 2))
                return True

            tts_started = time.perf_counter()
            chunk_bytes = OUTPUT_SAMPLE_RATE * 2 // 10
            total_bytes = 0
            if hasattr(self.service.tts, "stream_synthesize"):
                pending_pcm = bytearray()
                stream = self.service.tts.stream_synthesize(
                    text, tts_speed_for(text, "direct", self.service.tts.speed)
                )
                first_audio = True
                try:
                    async for raw_chunk in stream:
                        if first_audio:
                            first_audio = False
                            first_ms = round((time.perf_counter() - tts_started) * 1000)
                            self.tts_ms += first_ms
                            await begin_audio()
                            LOGGER.info(
                                "incremental_stream_start id=%s chars=%d backend=qwen first_tts_ms=%d",
                                self.response_id, len(text), first_ms,
                            )
                        pending_pcm.extend(raw_chunk)
                        while len(pending_pcm) >= chunk_bytes:
                            chunk = bytes(pending_pcm[:chunk_bytes])
                            del pending_pcm[:chunk_bytes]
                            total_bytes += len(chunk)
                            if not await send_chunk(chunk, total_bytes):
                                return
                    if first_audio:
                        raise RuntimeError("Qwen TTS returned no audio")
                    if pending_pcm:
                        total_bytes += len(pending_pcm)
                        await send_chunk(bytes(pending_pcm), total_bytes)
                finally:
                    close = getattr(stream, "aclose", None)
                    if close is not None:
                        await close()
            else:
                pcm = await self.service.tts.synthesize(
                    text, tts_speed_for(text, "direct", self.service.tts.speed)
                )
                self.tts_ms += round((time.perf_counter() - tts_started) * 1000)
                await begin_audio()
                for offset in range(0, len(pcm), chunk_bytes):
                    chunk = pcm[offset:offset + chunk_bytes]
                    total_bytes += len(chunk)
                    if not await send_chunk(chunk, total_bytes):
                        return

    async def finish(self, reply: str, route: str) -> int:
        await self.socket.send_json({
            "type": "reply", "text": reply, "route": route,
            "responseId": self.response_id,
        })
        if route != "direct" or not reply.startswith(self.spoken):
            if self.worker is not None:
                self.queue.put_nowait(None)
                await self.worker
            return -1
        remainder = reply[len(self.spoken):].strip()
        if remainder:
            self._queue(remainder)
        if self.worker is not None:
            self.queue.put_nowait(None)
            await self.worker
        if self.started:
            await self.socket.send_json({
                "type": "response.output_audio.done", "responseId": self.response_id
            })
        return self.tts_ms


class PersistentTtsWorker:
    def __init__(self, command: str, runtime_dir: str, model_dir: str, speed: float = 1.08):
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
            "--voices", str(Path(self.model_dir) / "voices.bin"),
            "--sid", str(DEFAULT_TTS_SPEAKER_ID),
            "--threads", "8",
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

    async def synthesize(self, text: str, speed: float | None = None) -> bytes:
        async with self.lock:
            try:
                await self.start()
                assert self.process and self.process.stdin and self.process.stdout
                request_speed = self.speed if speed is None else speed
                encoded = f"\x1e{request_speed:.3f}\n{text}".encode("utf-8")
                self.process.stdin.write(struct.pack("<I", len(encoded)) + encoded)
                await self.process.stdin.drain()
                header = await self.process.stdout.readexactly(8)
                status, length = struct.unpack("<II", header)
                payload = await self.process.stdout.readexactly(length)
                if status != 0:
                    raise RuntimeError(payload.decode("utf-8", errors="replace"))
                return payload
            except asyncio.CancelledError:
                # A canceled read would leave that response at the head of the
                # persistent protocol and poison the next utterance. Discard
                # the worker so the next request starts with a clean handshake.
                await self.stop()
                raise

    async def stop(self) -> None:
        if not self.process:
            return
        try:
            self.process.terminate()
        except ProcessLookupError:
            pass
        try:
            await self.process.wait()
        except ProcessLookupError:
            pass
        self.process = None


class QwenVllmTtsClient:
    """Streaming PCM client for vLLM-Omni's OpenAI-compatible speech API."""

    def __init__(
        self,
        url: str = DEFAULT_QWEN_TTS_URL,
        model: str = DEFAULT_QWEN_TTS_MODEL,
        voice: str = DEFAULT_QWEN_TTS_VOICE,
        instructions: str = DEFAULT_QWEN_TTS_INSTRUCTIONS,
        speed: float = 1.0,
        initial_chunk_frames: int = DEFAULT_QWEN_INITIAL_CHUNK_FRAMES,
        startup_wait_seconds: float = 0.0,
    ):
        self.url = url
        self.model = model
        self.voice = voice
        self.instructions = instructions
        self.speed = speed
        self.initial_chunk_frames = initial_chunk_frames
        self.startup_wait_seconds = startup_wait_seconds
        self.session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self.session and not self.session.closed:
            return
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=3, sock_read=30)
        self.session = aiohttp.ClientSession(timeout=timeout)
        health_url = self.url.rsplit("/v1/audio/speech", 1)[0] + "/health"
        deadline = time.monotonic() + self.startup_wait_seconds
        while True:
            try:
                async with self.session.get(health_url) as response:
                    response.raise_for_status()
                return
            except asyncio.CancelledError:
                await self.stop()
                raise
            except Exception:
                if time.monotonic() >= deadline:
                    await self.stop()
                    raise
                await asyncio.sleep(1)

    def request_payload(self, text: str) -> dict[str, Any]:
        return {
            "model": self.model,
            "input": text,
            "voice": self.voice,
            "instructions": self.instructions,
            "language": "English",
            "response_format": "pcm",
            "stream": True,
            "stream_format": "audio",
            "initial_codec_chunk_frames": self.initial_chunk_frames,
        }

    async def stream_synthesize(
        self, text: str, speed: float | None = None
    ) -> AsyncIterator[bytes]:
        # Qwen3-TTS does not currently expose reliable native rate control. The
        # argument is accepted for parity with Kokoro; prosody comes from the
        # per-request voice instruction instead.
        del speed
        await self.start()
        assert self.session
        async with self.session.post(self.url, json=self.request_payload(text)) as response:
            response.raise_for_status()
            async for chunk in response.content.iter_chunked(64 * 1024):
                if chunk:
                    yield bytes(chunk)

    async def synthesize(self, text: str, speed: float | None = None) -> bytes:
        pcm = bytearray()
        async for chunk in self.stream_synthesize(text, speed):
            pcm.extend(chunk)
        return bytes(pcm)

    async def stop(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None


def configured_tts_backend(speed: float) -> PersistentTtsWorker | QwenVllmTtsClient:
    backend = os.environ.get("LIVE_CONVERSATION_TTS_BACKEND", DEFAULT_TTS_BACKEND).strip().lower()
    if backend == "qwen":
        return QwenVllmTtsClient(
            url=os.environ.get("QWEN_TTS_URL", DEFAULT_QWEN_TTS_URL),
            model=os.environ.get("QWEN_TTS_MODEL", DEFAULT_QWEN_TTS_MODEL),
            voice=os.environ.get("QWEN_TTS_VOICE", DEFAULT_QWEN_TTS_VOICE),
            instructions=os.environ.get(
                "QWEN_TTS_INSTRUCTIONS", DEFAULT_QWEN_TTS_INSTRUCTIONS
            ),
            speed=1.0,
            initial_chunk_frames=int(os.environ.get(
                "QWEN_TTS_INITIAL_CHUNK_FRAMES", DEFAULT_QWEN_INITIAL_CHUNK_FRAMES
            )),
            startup_wait_seconds=float(os.environ.get(
                "QWEN_TTS_STARTUP_WAIT_SECONDS", "0"
            )),
        )
    if backend != "kokoro":
        raise ValueError(f"Unsupported LIVE_CONVERSATION_TTS_BACKEND: {backend}")
    return PersistentTtsWorker(
        DEFAULT_TTS_WORKER, DEFAULT_TTS_RUNTIME, DEFAULT_TTS_MODEL_DIR, speed
    )


class LiveConversationService:
    def __init__(
        self,
        session_key: str,
        tts_speed: float,
        history_path: str | None = None,
        settings_path: str | None = None,
        recordings_path: str | None = None,
        debug_path: str | None = None,
        recording_retention_days: int = DEFAULT_RECORDING_RETENTION_DAYS,
        debug_retention_days: int = DEFAULT_DEBUG_RETENTION_DAYS,
        recording_max_bytes: int = DEFAULT_RECORDING_MAX_BYTES,
        debug_max_bytes: int = DEFAULT_DEBUG_MAX_BYTES,
    ):
        self.session_key = session_key
        self.stt: WhisperSTTService | None = None
        self.stt_description = "not loaded"
        self.semantic_turn: SemanticTurnDetector | None = None
        self.semantic_turn_description = "fallback"
        self.tts = configured_tts_backend(tts_speed)
        self.tts_backend_description = type(self.tts).__name__
        self.speech_lock = asyncio.Lock()
        self.transcribe_lock = asyncio.Lock()
        self.capabilities = ""
        self.capabilities_updated_at = 0.0
        self.capability_refresh_task: asyncio.Task[str] | None = None
        self.sessions = ""
        self.session_keys: set[str] = set()
        self.session_records: dict[str, dict[str, Any]] = {}
        self.sessions_updated_at = 0.0
        self.sessions_last_success_wall = 0.0
        self.sessions_error: str | None = None
        self.session_refresh_task: asyncio.Task[str] | None = None
        self.session_poll_task: asyncio.Task[None] | None = None
        self.speech_generation = 0
        self.pending_spoken_replies: deque[str] = deque(maxlen=10)
        self.confirmation_required = False
        self.audio_capture_enabled = False
        self.debug_status: dict[str, Any] = {"state": "idle"}
        self.pending_confirmation: tuple[str, str | None, str] | None = None
        self.pending_fragment = ""
        self.pending_fragment_turn_id: str | None = None
        self.last_turn_understanding: TurnUnderstanding | None = None
        self.speech_model_accelerated: bool | None = None
        self.speech_model_acceleration_checked_at = 0.0
        self.superseded_turn_ids: set[str] = set()
        self.recent_agent_sessions: deque[dict[str, Any]] = deque(maxlen=12)
        self.history_path = Path(history_path) if history_path else None
        self.settings_path = Path(settings_path) if settings_path else None
        self.recordings_path = Path(recordings_path) if recordings_path else None
        self.debug_path = Path(debug_path) if debug_path else None
        self.recording_retention_days = max(1, recording_retention_days)
        self.debug_retention_days = max(1, debug_retention_days)
        self.recording_max_bytes = max(1, recording_max_bytes)
        self.debug_max_bytes = max(1, debug_max_bytes)
        self.metric_counters: Counter[str] = Counter()
        self.metric_samples: dict[str, deque[float]] = {
            name: deque(maxlen=1000)
            for name in ("asr_ms", "response_ms", "routing_ms", "tts_ms", "total_ms")
        }
        self.started_at = time.time()
        self.history: deque[dict[str, str]] = deque(maxlen=MAX_HISTORY_MESSAGES)
        self.events: deque[dict[str, Any]] = deque(maxlen=MAX_CONVERSATION_EVENTS)
        for private_root in (self.recordings_path, self.debug_path):
            if private_root and private_root.exists():
                _secure_directory(private_root)
        for private_file in (self.settings_path, self.history_path):
            if private_file and private_file.exists():
                try:
                    private_file.chmod(0o600)
                except OSError as error:
                    LOGGER.warning("private_data_permissions_failed path=%s error=%s", private_file, error)
        self._load_settings()
        self._load_history()
        self.prune_private_data()

    def _load_settings(self) -> None:
        if not self.settings_path or not self.settings_path.exists():
            return
        interrupted_debug = False
        try:
            payload = json.loads(self.settings_path.read_text(encoding="utf-8"))
            self.confirmation_required = payload.get("action_confirmation") == "confirm"
            self.audio_capture_enabled = payload.get("audio_capture") is True
            debug_status = payload.get("debug_status")
            if isinstance(debug_status, dict) and isinstance(debug_status.get("state"), str):
                self.debug_status = dict(debug_status)
                if self.debug_status.get("state") in {"collecting", "working"}:
                    self.debug_status.update({
                        "state": "failed",
                        "completed_at": int(time.time()),
                        "message": (
                            "The correction session was interrupted before completion. "
                            "The diagnostic bundle was retained."
                        ),
                        "error": "Correction session interrupted by service restart.",
                    })
                    interrupted_debug = True
            tracked = payload.get("recent_agent_sessions", [])
            if isinstance(tracked, list):
                for item in tracked[-12:]:
                    if (
                        isinstance(item, dict)
                        and isinstance(item.get("session_key"), str)
                        and isinstance(item.get("request"), str)
                    ):
                        self.recent_agent_sessions.append(dict(item))
            if interrupted_debug:
                self._save_settings()
        except (OSError, ValueError, TypeError) as error:
            LOGGER.warning("conversation_settings_load_failed error=%s", error)

    def _save_settings(self) -> None:
        if not self.settings_path:
            return
        try:
            _secure_atomic_write(
                self.settings_path,
                json.dumps({
                    "action_confirmation": (
                        "confirm" if self.confirmation_required else "automatic"
                    ),
                    "recent_agent_sessions": list(self.recent_agent_sessions),
                    "audio_capture": self.audio_capture_enabled,
                    "debug_status": self.debug_status,
                }, indent=2),
            )
        except OSError as error:
            LOGGER.warning("conversation_settings_save_failed error=%s", error)

    def set_confirmation_required(self, required: bool) -> None:
        self.confirmation_required = required
        if not required:
            self.pending_confirmation = None
        self._save_settings()

    def set_audio_capture_enabled(self, enabled: bool) -> None:
        self.audio_capture_enabled = enabled
        self._save_settings()

    def settings_payload(self) -> dict[str, Any]:
        return {
            "type": "settings",
            "action_confirmation": (
                "confirm" if self.confirmation_required else "automatic"
            ),
            "audio_capture": self.audio_capture_enabled,
            "audio_capture_retention_days": self.recording_retention_days,
            "debug_status": dict(self.debug_status),
        }

    def observe_turn_metrics(self, metrics: TurnMetrics) -> None:
        self.metric_counters["turns_total"] += 1
        if metrics.route:
            self.metric_counters[f"route_{_prometheus_name(metrics.route)}_total"] += 1
        for name, samples in self.metric_samples.items():
            value = getattr(metrics, name, None)
            if isinstance(value, (int, float)):
                samples.append(float(value))

    @staticmethod
    def _prune_tree(root: Path | None, retention_days: int, max_bytes: int) -> tuple[int, int]:
        if not root or not root.exists():
            return 0, 0
        for directory in (root, *(path for path in root.rglob("*") if path.is_dir())):
            try:
                directory.chmod(0o700)
            except OSError as error:
                LOGGER.warning("private_data_permissions_failed path=%s error=%s", directory, error)
        cutoff = time.time() - retention_days * 86400
        files = [path for path in root.rglob("*") if path.is_file() and not path.is_symlink()]
        removed_files = 0
        removed_bytes = 0
        for path in files:
            try:
                stat = path.stat()
                if stat.st_mtime < cutoff:
                    path.unlink()
                    removed_files += 1
                    removed_bytes += stat.st_size
                else:
                    path.chmod(0o600)
            except OSError as error:
                LOGGER.warning("private_data_retention_failed path=%s error=%s", path, error)
        remaining = []
        for path in root.rglob("*"):
            try:
                if path.is_file() and not path.is_symlink():
                    stat = path.stat()
                    remaining.append((stat.st_mtime, stat.st_size, path))
            except OSError:
                continue
        total = sum(size for _, size, _ in remaining)
        for _, size, path in sorted(remaining):
            if total <= max_bytes:
                break
            try:
                path.unlink()
                total -= size
                removed_files += 1
                removed_bytes += size
            except OSError as error:
                LOGGER.warning("private_data_quota_failed path=%s error=%s", path, error)
        for directory in sorted(
            (path for path in root.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts), reverse=True,
        ):
            try:
                directory.rmdir()
            except OSError:
                pass
        return removed_files, removed_bytes

    def prune_private_data(self) -> dict[str, int]:
        recordings, recording_bytes = self._prune_tree(
            self.recordings_path, self.recording_retention_days, self.recording_max_bytes
        )
        debug, debug_bytes = self._prune_tree(
            self.debug_path, self.debug_retention_days, self.debug_max_bytes
        )
        removed = recordings + debug
        if removed:
            LOGGER.info(
                "private_data_pruned files=%s bytes=%s recordings=%s debug=%s",
                removed, recording_bytes + debug_bytes, recordings, debug,
            )
        self.metric_counters["retention_files_deleted_total"] += removed
        self.metric_counters["retention_bytes_deleted_total"] += recording_bytes + debug_bytes
        return {"recordings": recordings, "debug": debug, "bytes": recording_bytes + debug_bytes}

    def delete_audio_captures(self) -> dict[str, int]:
        if not self.recordings_path or not self.recordings_path.exists():
            return {"files": 0, "bytes": 0}
        files = [
            path for path in self.recordings_path.rglob("*")
            if path.is_file() and not path.is_symlink()
        ]
        removed_files = 0
        removed_bytes = 0
        for path in files:
            try:
                size = path.stat().st_size
                path.unlink()
                removed_files += 1
                removed_bytes += size
            except OSError as error:
                LOGGER.warning("audio_capture_delete_failed path=%s error=%s", path, error)
        for directory in sorted(
            (path for path in self.recordings_path.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts), reverse=True,
        ):
            try:
                directory.rmdir()
            except OSError:
                pass
        self.metric_counters["recording_files_deleted_total"] += removed_files
        self.metric_counters["recording_bytes_deleted_total"] += removed_bytes
        return {"files": removed_files, "bytes": removed_bytes}

    def set_debug_status(self, state: str, **fields: Any) -> dict[str, Any]:
        allowed = {"idle", "collecting", "working", "fixed", "release_available", "gateway_updated", "failed"}
        if state not in allowed:
            raise ValueError(f"invalid debug state: {state}")
        self.debug_status = {"state": state, **fields}
        self._save_settings()
        return dict(self.debug_status)

    @staticmethod
    def redact_diagnostics(value: str) -> str:
        """Remove credential-shaped values before diagnostic text is persisted."""
        patterns = (
            r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s\"']+",
            r"(?i)((?:token|secret|password|api[_-]?key|device[_-]?token)\s*[:=]\s*)[^\s,;\"']+",
            r"(?i)(\"(?:token|secret|password|api[_-]?key|device[_-]?token)\"\s*:\s*\")[^\"]+",
        )
        redacted = value
        for pattern in patterns:
            redacted = re.sub(pattern, r"\1[REDACTED]", redacted)
        return redacted

    async def _diagnostic_command(self, *arguments: str, timeout: int = 15) -> str:
        try:
            process = await asyncio.create_subprocess_exec(
                *arguments,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
            text = stdout.decode("utf-8", errors="replace")
            return self.redact_diagnostics(text[-80_000:])
        except Exception as error:
            return f"unavailable: {error.__class__.__name__}: {error}"

    async def system_update_snapshot(self) -> dict[str, Any]:
        repo = DEFAULT_DASHBOARD_REPO
        head, tag, gateway_version, gateway_state, gateway_config_stamp = await asyncio.gather(
            self._diagnostic_command("git", "-C", repo, "rev-parse", "HEAD"),
            self._diagnostic_command("git", "-C", repo, "describe", "--tags", "--abbrev=0"),
            self._diagnostic_command(DEFAULT_NODE_COMMAND, DEFAULT_OPENCLAW_MODULE, "--version"),
            self._diagnostic_command(
                "systemctl", "--user", "show", "openclaw-gateway.service",
                "--property=ActiveState,SubState,ActiveEnterTimestampMonotonic",
            ),
            self._diagnostic_command(
                "stat", "--format=%Y:%s", "/home/john/.openclaw/openclaw.json"
            ),
        )
        return {
            "dashboard_head": head.strip(),
            "dashboard_tag": tag.strip(),
            "gateway_version": gateway_version.strip(),
            "gateway_service": gateway_state.strip(),
            "gateway_config_stamp": gateway_config_stamp.strip(),
        }

    async def create_debug_bundle(
        self, request_id: str, client_context: dict[str, Any] | None = None
    ) -> tuple[Path, dict[str, Any]]:
        """Collect a local, redacted diagnosis package without exposing it over HTTP."""
        if not self.debug_path:
            raise RuntimeError("Live Conversation debug storage is not configured")
        now = datetime.now(ZoneInfo("America/Detroit"))
        directory = self.debug_path / now.strftime("%Y%m%d")
        self.prune_private_data()
        _secure_directory(directory)
        baseline, dashboard_status, voice_logs, gateway_logs = await asyncio.gather(
            self.system_update_snapshot(),
            self._diagnostic_command(
                "git", "-C", DEFAULT_DASHBOARD_REPO, "status", "--short", "--branch"
            ),
            self._diagnostic_command(
                "journalctl", "--user", "-u", "openclaw-live-conversation.service",
                "-n", "350", "--no-pager", "-o", "short-iso"
            ),
            self._diagnostic_command(
                "journalctl", "--user", "-u", "openclaw-gateway.service",
                "-n", "180", "--no-pager", "-o", "short-iso"
            ),
        )
        recordings: list[dict[str, Any]] = []
        if self.recordings_path and self.recordings_path.exists():
            for manifest in sorted(self.recordings_path.glob("*/*.json"), reverse=True)[:20]:
                try:
                    item = json.loads(manifest.read_text(encoding="utf-8"))
                    item["manifest_path"] = str(manifest)
                    recordings.append(item)
                except (OSError, ValueError, TypeError):
                    continue
        bundle = {
            "schema_version": 1,
            "request_id": request_id,
            "requested_at": now.isoformat(),
            "purpose": "live_conversation_diagnosis_and_correction",
            "authorization": {
                "diagnose": True,
                "correct": True,
                "publish_release_if_needed": True,
                "update_gateway_if_needed": True,
            },
            "privacy": "Local host only; credentials redacted; recordings referenced by local path.",
            "client_context": dict(client_context or {}),
            "settings": {
                "action_confirmation": "confirm" if self.confirmation_required else "automatic",
                "audio_capture": self.audio_capture_enabled,
                "stt": self.stt_description,
                "semantic_turn": self.semantic_turn_description,
            },
            "conversation": {
                "messages": self.recent_history()[-120:],
                "events": self.recent_events()[-500:],
            },
            "recent_audio_captures": recordings,
            "system_before": baseline,
            "dashboard_git_status": dashboard_status,
            "live_conversation_logs": voice_logs,
            "gateway_logs": gateway_logs,
        }
        path = directory / f"{request_id}.json"
        _secure_atomic_write(
            path,
            self.redact_diagnostics(json.dumps(bundle, ensure_ascii=False, indent=2)),
        )
        return path, baseline

    def begin_audio_capture(
        self, pcm: bytes, sequence: int, client_context: dict[str, Any] | None = None
    ) -> str | None:
        """Persist one consented microphone turn as PCM WAV plus a sidecar.

        Capture is deliberately performed before ASR so recognition failures,
        clipped onsets, room noise, and rejected speech remain available for
        regression work. It is disabled by default and never records wake-word
        audio.
        """
        if not self.audio_capture_enabled or not self.recordings_path or not pcm:
            return None
        now = datetime.now(ZoneInfo("America/Detroit"))
        capture_id = f"{now:%Y%m%dT%H%M%S.%f}-{sequence}-{secrets.token_hex(3)}"
        day = self.recordings_path / now.strftime("%Y%m%d")
        try:
            self.prune_private_data()
            _secure_directory(day)
            wav_path = day / f"{capture_id}-user.wav"
            with wave.open(str(wav_path), "wb") as recording:
                recording.setnchannels(1)
                recording.setsampwidth(2)
                recording.setframerate(SAMPLE_RATE)
                recording.writeframes(pcm)
            wav_path.chmod(0o600)
            samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
            rms = float(np.sqrt(np.mean(np.square(samples / 32768.0)))) if len(samples) else 0.0
            user_agent = str((client_context or {}).get("user_agent") or "")
            capture_source = (
                "android_live_microphone"
                if "android" in user_agent.casefold() else
                "automated_or_non_android_client"
            )
            self._write_capture_manifest(capture_id, {
                "schema_version": 1,
                "capture_id": capture_id,
                "captured_at": now.isoformat(),
                "consent": "explicit_in_app_opt_in",
                "capture_source": capture_source,
                "status": "captured",
                "user_audio": wav_path.name,
                "sample_rate": SAMPLE_RATE,
                "channels": 1,
                "sample_width_bytes": 2,
                "duration_ms": round(len(pcm) / (SAMPLE_RATE * 2) * 1000),
                "rms": round(rms, 6),
                "sequence": sequence,
                "client_context": dict(client_context or {}),
            })
            LOGGER.info("audio_capture_saved id=%s path=%s", capture_id, wav_path)
            return capture_id
        except (OSError, ValueError) as error:
            LOGGER.warning("audio_capture_failed error=%s", error)
            return None

    def update_audio_capture(self, capture_id: str | None, **fields: Any) -> None:
        if not capture_id or not self.recordings_path:
            return
        manifest = self._capture_manifest_path(capture_id)
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload.update(fields)
            self._write_capture_manifest(capture_id, payload)
        except (OSError, ValueError, TypeError) as error:
            LOGGER.warning("audio_capture_metadata_failed id=%s error=%s", capture_id, error)

    def _capture_manifest_path(self, capture_id: str) -> Path:
        assert self.recordings_path is not None
        return self.recordings_path / capture_id[:8] / f"{capture_id}.json"

    def _write_capture_manifest(self, capture_id: str, payload: dict[str, Any]) -> None:
        manifest = self._capture_manifest_path(capture_id)
        _secure_atomic_write(manifest, json.dumps(payload, indent=2, sort_keys=True))

    def _load_history(self) -> None:
        if not self.history_path or not self.history_path.exists():
            return
        try:
            payload = json.loads(self.history_path.read_text(encoding="utf-8"))
            events = payload.get("events", []) if isinstance(payload, dict) else []
            if isinstance(events, list):
                for event in events[-MAX_CONVERSATION_EVENTS:]:
                    if isinstance(event, dict) and isinstance(event.get("type"), str):
                        self.events.append(dict(event))
            messages = payload.get("messages", []) if isinstance(payload, dict) else []
            for message in messages[-MAX_HISTORY_MESSAGES:]:
                role = message.get("role")
                content = message.get("content")
                if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                    self.history.append({"role": role, "content": content.strip()})
            if not self.events:
                now_ms = int(time.time() * 1000)
                for index, message in enumerate(self.history):
                    self.events.append({
                        "id": f"legacy-{index}", "type": "message",
                        "timestamp_ms": now_ms + index, "role": message["role"],
                        "content": message["content"], "status": "final",
                        "provenance": "legacy",
                    })
        except (OSError, ValueError, TypeError) as error:
            LOGGER.warning("conversation_history_load_failed error=%s", error)

    def _save_history(self) -> None:
        if not self.history_path:
            return
        try:
            _secure_atomic_write(
                self.history_path,
                json.dumps({
                    "schema_version": 2,
                    "messages": list(self.history),
                    "events": list(self.events),
                }, ensure_ascii=False, indent=2),
            )
        except OSError as error:
            LOGGER.warning("conversation_history_save_failed error=%s", error)

    def record_event(self, event_type: str, **fields: Any) -> dict[str, Any]:
        event = {
            "id": fields.pop("id", f"evt-{time.time_ns()}-{secrets.token_hex(3)}"),
            "type": event_type,
            "timestamp_ms": fields.pop("timestamp_ms", int(time.time() * 1000)),
            **fields,
        }
        self.events.append(event)
        return event

    def remember(
        self, role: str, content: str, *, turn_id: str | None = None,
        status: str = "final", provenance: str = "live",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        content = content.strip()
        if role not in ("user", "assistant") or not content:
            return
        # WebView reconnects and agent callback recovery can legitimately replay
        # the same event. Do not let transport retries become conversational
        # memory that the supervisor interprets as repeated user intent/results.
        if self.history and self.history[-1]["role"] == role:
            previous = re.sub(r"\s+", " ", self.history[-1]["content"]).strip()
            current = re.sub(r"\s+", " ", content).strip()
            if previous == current:
                return
        self.history.append({"role": role, "content": content})
        self.record_event(
            "message", role=role, content=content, turn_id=turn_id,
            status=status, provenance=provenance, metadata=metadata or {},
        )
        while sum(len(item["content"]) for item in self.history) > MAX_HISTORY_CHARS:
            self.history.popleft()
        self._save_history()

    def recent_history(self) -> list[dict[str, str]]:
        return [dict(message) for message in self.history]

    def recent_events(self) -> list[dict[str, Any]]:
        return [dict(event) for event in self.events]

    def event_context_summary(self) -> str:
        lines: deque[str] = deque(maxlen=12)
        for event in self.events:
            if event.get("type") == "message":
                metadata = event.get("metadata") or {}
                assembled = str(metadata.get("assembled_text") or "")
                content = str(event.get("content") or "")
                if assembled and assembled != content:
                    lines.append(
                        f"turn={event.get('turn_id') or 'unknown'} continuation: {assembled[:300]}"
                    )
            elif event.get("type") == "tool":
                lines.append(
                    f"tool={event.get('tool')} session={event.get('session_key')} "
                    f"status={event.get('status')} request={str(event.get('request') or '')[:220]}"
                )
            elif event.get("type") == "interruption":
                lines.append(
                    f"turn={event.get('turn_id') or 'unknown'} interrupted: {event.get('status')}"
                )
        return "\n".join(lines)

    def queue_pending_reply(self, reply: str) -> None:
        if reply.strip():
            self.pending_spoken_replies.append(reply.strip())

    def allocate_agent_session_key(self, request: str) -> str:
        words = re.findall(r"[a-z0-9]+", request.lower())
        stop_words = {
            "a", "an", "the", "to", "for", "of", "and", "agent", "subagent",
            "spawn", "start", "launch", "create",
        }
        slug = "-".join(word for word in words if word not in stop_words)[:48].strip("-")
        # A monotonic nanosecond suffix was a 15-to-19 digit number which could
        # leak into status speech and be dictated in full. A 48-bit opaque token
        # remains collision-resistant without looking like a giant cardinal.
        suffix = secrets.token_hex(6)
        if suffix.isdigit():
            suffix = "a" + suffix[1:]
        return f"agent:main:live-conversation-{slug or 'task'}-{suffix}"

    def register_agent_session(
        self, session_key: str, request: str, origin_turn_id: str | None = None
    ) -> None:
        self.session_keys.add(session_key)
        self.recent_agent_sessions = deque(
            (item for item in self.recent_agent_sessions if item.get("session_key") != session_key),
            maxlen=12,
        )
        entity_state = conversation_entities(request)
        self.recent_agent_sessions.append({
            "session_key": session_key,
            "request": re.sub(r"\s+", " ", request).strip(),
            "state": "running",
            "started_at": int(time.time()),
            "origin_turn_id": origin_turn_id,
            **entity_state,
            "result": "",
        })
        self.sessions_updated_at = 0.0
        self.record_event(
            "tool", tool="gateway_agent", action="start", status="running",
            session_key=session_key, request=re.sub(r"\s+", " ", request).strip(),
            turn_id=origin_turn_id,
        )
        self._save_settings()
        self._save_history()

    def update_agent_session(
        self, session_key: str, state: str, result: str = ""
    ) -> None:
        for item in self.recent_agent_sessions:
            if item.get("session_key") == session_key:
                item["state"] = state
                if result:
                    item["result"] = re.sub(r"\s+", " ", result).strip()[:500]
                break
        self.sessions_updated_at = 0.0
        self.record_event(
            "tool", tool="gateway_agent", action="status", status=state,
            session_key=session_key,
        )
        self._save_settings()
        self._save_history()

    def latest_agent_session(self) -> dict[str, Any] | None:
        return dict(self.recent_agent_sessions[-1]) if self.recent_agent_sessions else None

    def supersede_previous_agent_turn(self) -> None:
        """Suppress only the callback owned by the immediately corrected plan."""
        for item in reversed(self.recent_agent_sessions):
            origin = item.get("origin_turn_id")
            if origin and item.get("state") == "running":
                self.superseded_turn_ids.add(str(origin))
                self.record_event(
                    "interruption", turn_id=origin,
                    status="agent_callback_superseded_by_correction",
                )
                self._save_history()
                return

    def resolve_agent_session(self, transcript: str) -> dict[str, Any] | None:
        """Resolve a referenced voice session by topic, falling back to recency."""
        if not self.recent_agent_sessions:
            return None
        stop_words = {
            "a", "an", "and", "agent", "session", "that", "this", "the", "it",
            "tell", "ask", "please", "status", "how", "is", "was", "to", "of",
        }
        query_terms = set(re.findall(r"[a-z0-9]+", transcript.lower())) - stop_words
        best: dict[str, Any] | None = None
        best_score = 0
        for recency, item in enumerate(reversed(self.recent_agent_sessions)):
            key = str(item.get("session_key") or "")
            gateway = self.session_records.get(key, {})
            candidate = " ".join(str(value or "") for value in (
                item.get("request"), gateway.get("displayName"),
                gateway.get("derivedTitle"), gateway.get("label"),
                " ".join(item.get("topics") or []),
                " ".join(item.get("named_entities") or []), item.get("result"),
            ))
            candidate_terms = set(re.findall(r"[a-z0-9]+", candidate.lower())) - stop_words
            overlap = len(query_terms & candidate_terms)
            phrase_bonus = 12 if query_terms and " ".join(query_terms) in candidate.lower() else 0
            score = overlap * 10 + phrase_bonus + max(0, 5 - recency)
            if score > best_score:
                best, best_score = dict(item), score
        # Pure pronouns have no topical terms and intentionally resolve by recency.
        return best or self.latest_agent_session()

    def voice_session_summary(self) -> str:
        lines = []
        for item in reversed(self.recent_agent_sessions):
            key = str(item.get("session_key") or "")
            gateway = self.session_records.get(key, {})
            active = gateway.get("hasActiveRun", item.get("state") == "running")
            state = "running" if active else str(item.get("state") or gateway.get("status") or "unknown")
            lines.append(
                f"key={key} | state={state} | request={str(item.get('request') or '')[:220]}"
            )
        return "\n".join(lines)

    def recover_recent_agent_session(self) -> None:
        """Migrate the newest pre-tracking voice session from history and the gateway."""
        if self.recent_agent_sessions:
            return
        candidates = [
            item for key, item in self.session_records.items()
            if key.startswith("agent:main:live-conversation-")
        ]
        if not candidates:
            return
        request = ""
        for index in range(len(self.history) - 1, 0, -1):
            message = self.history[index]
            previous = self.history[index - 1]
            if (
                message["role"] == "assistant"
                and is_operational_acknowledgment(message["content"])
                and previous["role"] == "user"
            ):
                request = previous["content"]
                break
        if not request:
            return
        newest = max(candidates, key=lambda item: item.get("updatedAt") or 0)
        key = str(newest["key"])
        state = "running" if newest.get("hasActiveRun") else (
            "failed" if newest.get("status") in {"failed", "error"} else "complete"
        )
        self.recent_agent_sessions.append({
            "session_key": key,
            "request": request,
            "state": state,
            "started_at": int(time.time()),
        })
        self._save_settings()

    def prompt_history(self) -> list[dict[str, str]]:
        filtered: list[dict[str, str]] = []
        for message in self.history:
            if message["role"] == "assistant" and is_operational_acknowledgment(message["content"]):
                continue
            filtered.append(message)
        selected: deque[dict[str, str]] = deque()
        used_chars = 0
        for message in reversed(filtered):
            if message["role"] == "assistant" and has_stale_identity_confusion(message["content"]):
                continue
            content = message["content"]
            # A pasted diagnostic or long agent result must not consume more
            # than the complete prompt-history budget. Preserve its newest end,
            # which normally contains the conclusion and latest state.
            if len(content) > PROMPT_HISTORY_CHARS:
                content = "…" + content[-(PROMPT_HISTORY_CHARS - 1):]
            size = len(content)
            if selected and (
                len(selected) >= PROMPT_HISTORY_MESSAGES
                or used_chars + size > PROMPT_HISTORY_CHARS
            ):
                break
            selected.appendleft({"role": message["role"], "content": content})
            used_chars += size
        # Do not begin the model context with a detached assistant answer when
        # the corresponding user turn fell just outside the history budget.
        while selected and selected[0]["role"] == "assistant":
            selected.popleft()
        return list(selected)

    def assemble_turn_text(self, transcript: str) -> str:
        """Join a semantic continuation before routing or authorization."""
        previous_user = next(
            (item["content"] for item in reversed(self.history) if item["role"] == "user"),
            "",
        )
        unfinished_agent_request = re.search(
            r"\b(?:start|have|ask|tell|task|send)\b.*\b(?:agent|sub-?agent|session)\b.*(?:\bthat|\bto|\bfor|\bwith|\bso|\.{2,})\s*$",
            previous_user,
            flags=re.IGNORECASE,
        )
        unfinished_clause = re.search(
            r"\b(?:and|or|but|because|with|to|for|about|that|which)\s*$",
            previous_user,
            flags=re.IGNORECASE,
        )
        continuation_start = re.match(
            r"\s*(?:and|also|then|but|because|with|to|for|about|checking|reviewing|"
            r"fixing|updating|making|turning|looking|testing)\b",
            transcript,
            flags=re.IGNORECASE,
        )
        if (
            unfinished_agent_request
            or unfinished_clause and continuation_start
        ) and len(previous_user) <= 500:
            return f"{previous_user.rstrip(' .')} {transcript.lstrip()}"
        return transcript

    def contextualize_agent_request(self, transcript: str) -> str:
        """Backward-compatible alias for assembled-turn action handoffs."""
        return self.assemble_turn_text(transcript)

    async def start(self) -> None:
        try:
            self.stt = await asyncio.to_thread(
                WhisperSTTService,
                settings=WhisperSTTService.Settings(
                    model="small.en", language=Language.EN, no_speech_prob=0.6
                ),
                device="cuda",
                compute_type="float16",
            )
            self.stt_description = "faster-whisper small.en cuda-float16"
            await self.transcribe(bytes(SAMPLE_RATE), purpose="warmup")
        except Exception as error:
            LOGGER.warning("cuda_stt_unavailable_falling_back_to_cpu error=%s", error)
            self.stt = await asyncio.to_thread(
                WhisperSTTService,
                settings=WhisperSTTService.Settings(
                    model="small.en", language=Language.EN, no_speech_prob=0.6
                ),
                device="cpu",
                compute_type="int8",
            )
            self.stt_description = "faster-whisper small.en cpu-int8 fallback"
        try:
            await self.tts.start()
        except Exception as error:
            if not isinstance(self.tts, QwenVllmTtsClient):
                raise
            LOGGER.error("qwen_tts_unavailable_falling_back_to_kokoro error=%s", error)
            self.tts = PersistentTtsWorker(
                DEFAULT_TTS_WORKER, DEFAULT_TTS_RUNTIME, DEFAULT_TTS_MODEL_DIR, 1.08
            )
            self.tts_backend_description = "PersistentTtsWorker (Qwen fallback)"
            await self.tts.start()
        try:
            self.semantic_turn = await asyncio.to_thread(
                SemanticTurnDetector, SMART_TURN_MODEL_PATH
            )
            self.semantic_turn_description = "smart-turn-v3.2-cpu"
        except Exception as error:
            LOGGER.warning("semantic_turn_unavailable error=%s", error)
        await asyncio.gather(self.refresh_capabilities(), self.refresh_sessions())
        self.recover_recent_agent_session()
        await self.warm_speech_model()
        self.session_poll_task = asyncio.create_task(self.poll_sessions())

    async def warm_speech_model(self) -> None:
        payload = {
            "model": SPEECH_MODEL,
            "keep_alive": SPEECH_MODEL_KEEP_ALIVE,
            "stream": False,
            "think": False,
            "messages": [{"role": "user", "content": "Reply only: ready"}],
            "options": {
                "temperature": 0,
                "num_predict": 3,
                "num_ctx": SPEECH_MODEL_CONTEXT,
            },
        }
        try:
            timeout = aiohttp.ClientTimeout(total=60)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(SPEECH_MODEL_URL, json=payload) as response:
                    response.raise_for_status()
                    await response.read()
            LOGGER.info("speech_model_warm model=%s", SPEECH_MODEL)
            await self.refresh_speech_model_acceleration(force=True)
        except Exception as error:
            LOGGER.warning("speech_model_warm_failed error=%s", error)

    async def refresh_speech_model_acceleration(self, force: bool = False) -> bool | None:
        """Report whether Ollama actually placed the speech router in VRAM.

        Ollama can accept a CUDA device, fail to initialize it, and silently
        reload the model on CPU. Its chat endpoint remains healthy in that
        state, but the full conversation prompt misses every latency deadline.
        """
        now = time.monotonic()
        if not force and now - self.speech_model_acceleration_checked_at < 2:
            return self.speech_model_accelerated
        ps_url = SPEECH_MODEL_URL.rsplit("/api/chat", 1)[0] + "/api/ps"
        try:
            timeout = aiohttp.ClientTimeout(total=0.75)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(ps_url) as response:
                    response.raise_for_status()
                    payload = await response.json()
            matching = [
                item for item in payload.get("models", [])
                if item.get("name") == SPEECH_MODEL or item.get("model") == SPEECH_MODEL
            ]
            accelerated = bool(matching and int(matching[0].get("size_vram") or 0) > 0)
            if accelerated != self.speech_model_accelerated:
                LOGGER.warning(
                    "speech_model_acceleration_changed accelerated=%s size_vram=%s",
                    accelerated,
                    matching[0].get("size_vram") if matching else "model-not-loaded",
                )
            self.speech_model_accelerated = accelerated
            self.speech_model_acceleration_checked_at = now
            return accelerated
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError) as error:
            LOGGER.warning("speech_model_acceleration_check_failed error=%s", error)
            return self.speech_model_accelerated

    async def degraded_speech_reply(self, text: str) -> tuple[str, str, str | None]:
        """Keep voice routing useful when the dedicated model loses GPU use."""
        normalized = re.sub(r"\s+", " ", text).strip()
        lowered = normalized.casefold()
        if re.match(r"^(?:i am|i'm|just )?(?:testing|trying out|testing out)\b", lowered):
            return "direct", "I hear you. Go ahead with the test.", None
        if requires_tool_backed_action(normalized):
            route = "new_agent" if is_explicit_new_agent_request(normalized) else "agent"
            return route, "I'll have the agent handle that.", None
        if requires_authoritative_lookup(normalized):
            return "agent", "I'll verify that against a current source.", None

        payload = {
            "model": SPEECH_MODEL,
            "keep_alive": SPEECH_MODEL_KEEP_ALIVE,
            "stream": False,
            "think": False,
            "messages": [
                {"role": "system", "content": (
                    "Answer the user directly in one concise, natural spoken sentence. "
                    "Never claim to perform an action or know changing current facts."
                )},
                {"role": "user", "content": normalized},
            ],
            "options": {
                "temperature": 0,
                "num_predict": 96,
                "num_ctx": SPEECH_MODEL_CONTEXT,
            },
        }
        try:
            timeout = aiohttp.ClientTimeout(total=DEGRADED_SPEECH_TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(SPEECH_MODEL_URL, json=payload) as response:
                    response.raise_for_status()
                    result = await response.json()
            reply = str(result.get("message", {}).get("content", "")).strip()
            if reply:
                reply = remove_unrequested_action_promises(reply)
                LOGGER.warning("speech_supervisor_degraded_direct_reply chars=%d", len(reply))
                return "direct", reply, None
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError) as error:
            LOGGER.warning("speech_supervisor_degraded_reply_failed error=%s", error)
        return "direct", "I couldn't process that quickly. Please try again.", None

    async def poll_sessions(self) -> None:
        while True:
            await asyncio.sleep(SESSION_POLL_SECONDS)
            try:
                await asyncio.gather(
                    self.refresh_sessions(),
                    self.refresh_speech_model_acceleration(force=True),
                )
            except Exception as error:
                LOGGER.warning("session_poll_failed error=%s", error)

    async def stop(self) -> None:
        if self.capability_refresh_task and not self.capability_refresh_task.done():
            self.capability_refresh_task.cancel()
        if self.session_refresh_task and not self.session_refresh_task.done():
            self.session_refresh_task.cancel()
        if self.session_poll_task and not self.session_poll_task.done():
            self.session_poll_task.cancel()
        await self.tts.stop()

    def interrupt_speech(self) -> None:
        """Stop paced delivery promptly when confirmed new speech begins."""
        self.speech_generation += 1

    async def transcribe(
        self, audio: bytes, purpose: str = "final", initial_prompt: str = ""
    ) -> str:
        if not self.stt:
            raise RuntimeError("Speech recognizer is not ready")

        def run_to_completion() -> str:
            assert self.stt and self.stt._model
            audio_float = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0
            # Android's VOICE_COMMUNICATION processing can reduce the level of
            # sentence endings substantially.  Keep partial and wake decoding
            # conservative, but let the authoritative full-turn pass retain a
            # quiet final phrase instead of converting a complete request into
            # an unresolved fragment.
            vad_threshold = 0.35 if purpose == "final" else (
                0.5 if purpose == "wake" else 0.6
            )
            segments, _ = self.stt._model.transcribe(
                audio_float,
                language="en",
                beam_size=5,
                best_of=5,
                temperature=0.0,
                condition_on_previous_text=False,
                without_timestamps=True,
                no_speech_threshold=0.6,
                hotwords=(
                    "John, Jarvis, OpenClaw, Live Conversation, live agent, "
                    "subagent, gateway, sessions"
                ),
                initial_prompt=initial_prompt or None,
                vad_filter=True,
                vad_parameters={
                    "threshold": vad_threshold,
                    "min_speech_duration_ms": 250,
                    "min_silence_duration_ms": 300,
                    "speech_pad_ms": 300 if purpose == "final" else 200,
                },
            )
            text = "".join(
                f"{segment.text} "
                for segment in segments
                if segment.no_speech_prob < 0.6
            ).strip()
            LOGGER.info(
                "speech_transcription_complete purpose=%s audio_seconds=%.2f chars=%d",
                purpose, len(audio) / (SAMPLE_RATE * 2), len(text),
            )
            return text

        async with self.transcribe_lock:
            decode = asyncio.create_task(asyncio.to_thread(run_to_completion))
            return await decode

    async def transcribe_online(
        self, audio: bytes, state: OnlineTranscript, purpose: str = "partial"
    ) -> tuple[str, str, str]:
        """Decode only the active tail and expose stable/revisable prefixes."""
        sample_count = len(audio) // 2
        max_window_samples = round(ONLINE_ASR_WINDOW_SECONDS * SAMPLE_RATE)
        start_sample = max(state.decode_from_sample, sample_count - max_window_samples)
        tail = audio[start_sample * 2:]
        stable_prompt = " ".join(state.stable_words[-48:])
        hypothesis = await self.transcribe(
            tail, purpose=purpose, initial_prompt=stable_prompt
        )
        stable, unstable = state.update(hypothesis, sample_count)
        return stable, unstable, state.display()

    async def finalize_online_transcript(
        self, audio: bytes, state: OnlineTranscript
    ) -> str:
        # Online hypotheses remain windowed for latency, but final recognition
        # must see the entire utterance. Reusing the rolling 14-second tail here
        # discarded sentence beginnings whenever John spoke slowly or paused.
        hypothesis = await self.transcribe(audio, purpose="final")
        return hypothesis or state.display()

    async def endpoint_decision(self, audio: bytes, transcript: str = "") -> TurnDecision:
        if self.semantic_turn is not None:
            acoustic = await asyncio.to_thread(self.semantic_turn.predict, audio)
            if not acoustic.complete or not transcript.strip():
                return acoustic
            semantic = await self.semantic_endpoint_decision(transcript)
            if semantic is None:
                # Failure to establish semantic completeness must not cut the
                # speaker off. The browser's hard endpoint remains a bounded
                # fallback if no more speech arrives.
                return TurnDecision(False, 0.0, f"{acoustic.source}+semantic-error")
            complete, confidence = semantic
            return TurnDecision(
                complete and confidence >= 0.7,
                min(acoustic.probability, confidence),
                f"{acoustic.source}+semantic",
            )
        # Conservative fallback is explicit and observable rather than being
        # misrepresented as model-based semantic endpointing.
        unfinished = bool(re.search(
            r"(?:,|\b(?:and|or|but|because|with|to|for|about|that|which))\s*$",
            transcript.strip(), re.I,
        ))
        return TurnDecision(not unfinished, 0.5 if not unfinished else 0.0, "text-fallback")

    async def semantic_endpoint_decision(
        self, transcript: str
    ) -> tuple[bool, float] | None:
        """Veto premature acoustic endpoints with a meaning-level judgment."""
        if self.speech_model_accelerated is False:
            LOGGER.warning("semantic_endpoint_skipped_degraded_speech_model")
            return None
        payload = {
            "model": SPEECH_MODEL,
            "keep_alive": SPEECH_MODEL_KEEP_ALIVE,
            "stream": False,
            "think": False,
            "format": SEMANTIC_ENDPOINT_SCHEMA,
            "messages": [
                {"role": "system", "content": (
                    "Judge whether the speaker has expressed a semantically complete "
                    "turn that can be answered now. Meaning and discourse intent matter, "
                    "not punctuation or a fixed word list. A grammatical clause is still "
                    "incomplete when it promises, introduces, or explicitly reserves a "
                    "question, condition, explanation, contrast, or continuation that has "
                    "not yet been spoken. Ignore punctuation inserted by ASR and ask whether "
                    "all required semantic arguments are present. For example, 'Which season "
                    "comes immediately after?' is incomplete because what it comes after is "
                    "missing; 'Which season comes immediately after spring?' is complete. "
                    "'What is the usual colour of?' and 'Could you tell me whether?' are "
                    "incomplete because their objects or propositions are missing. Return only "
                    "the required compact JSON object with complete and confidence."
                )},
                {"role": "user", "content": transcript},
            ],
            # Keep the same context allocation as the main speech supervisor.
            # Asking Ollama for 1024 here evicted the warm 8192-token runner,
            # forcing a multi-second model reload during natural pauses.
            "options": {
                "temperature": 0, "num_predict": 32,
                "num_ctx": SPEECH_MODEL_CONTEXT,
            },
        }
        timeout = aiohttp.ClientTimeout(total=4)
        last_error: Exception | None = None
        for attempt in range(2):
            request_payload = payload
            if attempt:
                request_payload = {
                    **payload,
                    "messages": [
                        *payload["messages"],
                        {"role": "system", "content": (
                            "The prior output did not match the schema. Return exactly "
                            "one JSON object with boolean complete and numeric confidence. "
                            "No other keys or text."
                        )},
                    ],
                }
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(
                        SPEECH_MODEL_URL, json=request_payload
                    ) as response:
                        response.raise_for_status()
                        result = await response.json()
                content = str(result.get("message", {}).get("content", "")).strip()
                parsed = json.loads(content)
                complete = parsed.get("complete")
                confidence = parsed.get("confidence")
                if not isinstance(complete, bool):
                    raise ValueError("missing complete")
                if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
                    raise ValueError("missing confidence")
                LOGGER.info(
                    "semantic_endpoint_complete complete=%s confidence=%.3f "
                    "transcript=%r attempt=%d",
                    complete, float(confidence), transcript, attempt + 1,
                )
                return complete, float(confidence)
            except Exception as error:
                last_error = error
                LOGGER.warning(
                    "semantic_endpoint_controller_retry attempt=%d error=%s",
                    attempt + 1, error,
                )
        LOGGER.warning("semantic_endpoint_controller_failed error=%s", last_error)
        return None

    async def prefetch_for_partial(self, stable_text: str) -> str | None:
        """Warm read-only authoritative context from immutable ASR text."""
        normalized = stable_text.lower()
        if re.search(r"\b(?:session|agent|task|run)s?\b", normalized) and re.search(
            r"\b(?:status|active|running|progress|latest|recent)\b", normalized
        ):
            await self.refresh_sessions(force=True)
            return "sessions"
        if re.search(r"\b(?:can you|capabilit|skill|available agent)\b", normalized):
            await self.refresh_capabilities()
            return "capabilities"
        return None

    async def openclaw_json(self, *arguments: str, timeout: int = 30) -> Any:
        process = await asyncio.create_subprocess_exec(
            DEFAULT_NODE_COMMAND, DEFAULT_OPENCLAW_MODULE, *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        if process.returncode != 0:
            raise RuntimeError(stderr.decode("utf-8", errors="replace").strip() or "OpenClaw command failed")
        return json.loads(stdout)

    async def refresh_capabilities(self) -> str:
        if self.capabilities and time.monotonic() - self.capabilities_updated_at < CAPABILITY_CACHE_SECONDS:
            return self.capabilities
        try:
            agents, skills = await asyncio.gather(
                self.openclaw_json("agents", "list", "--json"),
                self.openclaw_json("skills", "list", "--agent", "main", "--json"),
            )
            agent_lines = [f"agent {item['id']}: {item.get('name', item['id'])}" for item in agents]
            skill_lines = [
                f"skill {item['name']}: {item.get('description', '')}"
                for item in skills.get("skills", [])
                if item.get("eligible") and item.get("modelVisible")
            ]
            self.capabilities = "\n".join(agent_lines + skill_lines)
            self.capabilities_updated_at = time.monotonic()
        except Exception as error:
            LOGGER.warning("capability_refresh_failed error=%s", error)
        return self.capabilities

    async def refresh_sessions(self, force: bool = False) -> str:
        if not force and self.sessions and time.monotonic() - self.sessions_updated_at < SESSION_CACHE_SECONDS:
            return self.sessions
        try:
            payload = await self.openclaw_json(
                "gateway", "call", "sessions.list", "--json", "--params",
                json.dumps({
                    "limit": 100,
                    "activeMinutes": 10_080,
                    "includeLastMessage": True,
                    "includeDerivedTitles": True,
                }),
            )
            items = payload.get("sessions", []) if isinstance(payload, dict) else []
            lines: list[str] = []
            keys: set[str] = set()
            records: dict[str, dict[str, Any]] = {}
            for item in items:
                key = item.get("key")
                if not isinstance(key, str) or not key:
                    continue
                keys.add(key)
                records[key] = dict(item)
                title = (
                    item.get("displayName") or item.get("derivedTitle")
                    or item.get("label") or "Untitled"
                )
                active = "yes" if item.get("hasActiveRun") else "no"
                updated = item.get("updatedAt")
                if isinstance(updated, (int, float)):
                    updated_text = datetime.fromtimestamp(
                        updated / 1000, ZoneInfo("America/Detroit")
                    ).strftime("%b %-d %-I:%M %p")
                else:
                    updated_text = "unknown"
                preview = re.sub(
                    r"\s+", " ", str(item.get("lastMessagePreview") or "")
                ).strip()[:180]
                lines.append(
                    f"key={key} | title={title} | hasActiveRun={active} | updated={updated_text}"
                    + (f" | latest={preview}" if preview else "")
                )
            self.session_keys = keys
            self.session_records = records
            self.sessions = "\n".join(lines) or "No active or recent gateway sessions were returned."
            self.sessions_updated_at = time.monotonic()
            self.sessions_last_success_wall = time.time()
            self.sessions_error = None
            tracking_changed = False
            for tracked in self.recent_agent_sessions:
                gateway = records.get(str(tracked.get("session_key") or ""))
                if not gateway:
                    continue
                state = "running" if gateway.get("hasActiveRun") else (
                    "failed" if gateway.get("status") in {"failed", "error"} else "complete"
                )
                if tracked.get("state") != state:
                    tracked["state"] = state
                    tracking_changed = True
            if tracking_changed:
                self._save_settings()
        except Exception as error:
            self.sessions_error = str(error) or error.__class__.__name__
            LOGGER.warning("session_refresh_failed error=%s", error)
        return self.sessions

    def session_freshness_reply(self) -> str | None:
        if not self.sessions_error:
            return None
        if self.sessions_last_success_wall:
            age_seconds = max(0, round(time.time() - self.sessions_last_success_wall))
            if age_seconds < 60:
                age = f"{age_seconds} seconds"
            else:
                age = f"{max(1, round(age_seconds / 60))} minutes"
            return (
                "I couldn't refresh Gateway sessions just now. "
                f"The last successful catalog is {age} old, so I won't present it as current."
            )
        return "I couldn't refresh Gateway sessions, so I can't verify their current status."

    @staticmethod
    def gateway_is_transient(error: Exception) -> bool:
        message = str(error).lower()
        return any(marker in message for marker in (
            "connection refused", "connection closed", "socket closed", "websocket",
            "gateway", "econnrefused", "econnreset", "didn't receive pong",
        ))

    async def agent_reply(self, text: str, session_key: str | None = None) -> str:
        target_key = session_key or self.session_key
        for attempt, delay in enumerate((1, 2, 4, 8), start=1):
            try:
                payload = await self.openclaw_json(
                    "agent", "--session-key", target_key, "--message", text,
                    "--timeout", "600", "--json", timeout=610,
                )
                break
            except Exception as error:
                if attempt == 4 or not self.gateway_is_transient(error):
                    raise
                LOGGER.info(
                    "gateway_reconnecting attempt=%d target=%s error=%s",
                    attempt, target_key, error,
                )
                await asyncio.sleep(delay)
        reply = extract_agent_text(payload)
        if reply:
            return reply

        history = await self.openclaw_json(
            "gateway", "call", "chat.history", "--json", "--params",
            json.dumps({"sessionKey": target_key, "limit": 40}),
        )
        acknowledgment = extract_agent_acknowledgment(history)
        if not acknowledgment:
            raise RuntimeError("Agent returned no speakable text")
        LOGGER.info("agent_delegated_waiting_for_final target=%s", target_key)
        deadline = time.monotonic() + AGENT_RESULT_WAIT_SECONDS
        while time.monotonic() < deadline:
            await asyncio.sleep(AGENT_RESULT_POLL_SECONDS)
            history = await self.openclaw_json(
                "gateway", "call", "chat.history", "--json", "--params",
                json.dumps({"sessionKey": target_key, "limit": 40}),
            )
            final_text = latest_assistant_text(history)
            if final_text:
                return final_text
        return "The delegated agent is still working in its session."

    async def speech_reply(
        self, text: str, agent_pending: bool = False,
        stream_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> tuple[str, str, str | None]:
        # Safety/presence controls remain deterministic. They do not authorize
        # work and cannot be confused with the semantic routing problem.
        direct_reply = direct_voice_surface_reply(text)
        if direct_reply:
            return "direct", direct_reply, None
        status_question = is_gateway_status_question(text)
        if status_question:
            await self.refresh_sessions(force=True)
            freshness_reply = self.session_freshness_reply()
            if freshness_reply:
                return "direct", freshness_reply, None
            if is_referential_agent_question(text):
                tracked = self.resolve_agent_session(text)
                if tracked:
                    key = str(tracked.get("session_key") or "")
                    return "direct", summarize_tracked_agent(
                        tracked, self.session_records.get(key)
                    ), None
            return "direct", summarize_gateway_status(self.sessions, agent_pending), None

        if self.speech_model_accelerated is False:
            LOGGER.warning("speech_supervisor_using_degraded_route")
            return await self.degraded_speech_reply(text)

        if time.monotonic() - self.capabilities_updated_at >= CAPABILITY_CACHE_SECONDS:
            if not self.capability_refresh_task or self.capability_refresh_task.done():
                self.capability_refresh_task = asyncio.create_task(self.refresh_capabilities())
        if time.monotonic() - self.sessions_updated_at >= SESSION_CACHE_SECONDS:
            if not self.session_refresh_task or self.session_refresh_task.done():
                self.session_refresh_task = asyncio.create_task(self.refresh_sessions())
            if not self.sessions:
                try:
                    await asyncio.wait_for(asyncio.shield(self.session_refresh_task), timeout=2.5)
                except asyncio.TimeoutError:
                    pass
        capabilities = self.capabilities
        controller_text = text
        pending_for_prompt = self.pending_fragment
        fragment_resolution: tuple[str, bool, float, str, str] | None = None
        if self.pending_fragment:
            fragment_resolution = await self.semantic_fragment_resolution(
                self.pending_fragment, text
            )
            if fragment_resolution is not None:
                relation, fragment_complete, confidence, assembled, reason = (
                    fragment_resolution
                )
                LOGGER.info(
                    "semantic_fragment_resolution relation=%s complete=%s "
                    "confidence=%.3f pending=%r current=%r assembled=%r reason=%r",
                    relation, fragment_complete, confidence, self.pending_fragment,
                    text, assembled, reason,
                )
                if relation in {"continuation", "correction"} and confidence >= 0.7:
                    controller_text = assembled
                    pending_for_prompt = ""
        payload = {
            "model": SPEECH_MODEL,
            "keep_alive": SPEECH_MODEL_KEEP_ALIVE,
            "stream": stream_callback is not None,
            "think": False,
            "format": SPEECH_OUTPUT_SCHEMA,
            "messages": [
                {"role": "system", "content": speech_model_prompt(
                    agent_pending=agent_pending,
                    capabilities=capabilities,
                    sessions=self.sessions,
                    voice_sessions=self.voice_session_summary(),
                    event_context=self.event_context_summary(),
                    confirmation_required=self.confirmation_required,
                )},
                {"role": "system", "content": turn_understanding_prompt(
                    controller_text,
                    pending_fragment=pending_for_prompt,
                    # The same history is supplied as chat messages immediately
                    # below; embedding it here again wastes routing latency.
                    recent_context=None,
                )},
                *self.prompt_history(),
                {"role": "user", "content": controller_text},
            ],
            "options": {
                "temperature": 0,
                "num_predict": SPEECH_NUM_PREDICT,
                "num_ctx": SPEECH_MODEL_CONTEXT,
            },
        }
        timeout = aiohttp.ClientTimeout(total=30)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                result: dict[str, Any] = {}
                prediction_limits = (
                    (SPEECH_RETRY_NUM_PREDICT,) if stream_callback is not None
                    else (SPEECH_NUM_PREDICT, SPEECH_RETRY_NUM_PREDICT)
                )
                for attempt, prediction_limit in enumerate(prediction_limits, start=1):
                    request_payload = {
                        **payload,
                        "options": {
                            **payload["options"],
                            "num_predict": prediction_limit,
                        },
                    }
                    async with session.post(
                        SPEECH_MODEL_URL, json=request_payload
                    ) as response:
                        response.raise_for_status()
                        if stream_callback is None:
                            result = await response.json()
                        else:
                            structured_text = ""
                            async for raw_line in response.content:
                                if not raw_line.strip():
                                    continue
                                update = json.loads(raw_line)
                                if update.get("error"):
                                    raise ValueError(str(update["error"]))
                                structured_text += str(
                                    update.get("message", {}).get("content", "")
                                )
                                await stream_callback(structured_text)
                                if update.get("done"):
                                    result = dict(update)
                            result["message"] = {"content": structured_text}
                    eval_count = result.get("eval_count")
                    output_limited = (
                        result.get("done_reason") == "length"
                        or isinstance(eval_count, int) and eval_count >= prediction_limit
                    )
                    if not output_limited:
                        break
                    LOGGER.warning(
                        "speech_supervisor_output_limited attempt=%d output_tokens=%s "
                        "num_predict=%d done_reason=%s",
                        attempt, eval_count, prediction_limit,
                        result.get("done_reason", "unknown"),
                    )
                else:
                    raise ValueError("speech supervisor exceeded its output limit")
            LOGGER.info(
                "speech_supervisor_complete prompt_tokens=%s output_tokens=%s "
                "done_reason=%s load_ms=%.0f prompt_ms=%.0f generation_ms=%.0f",
                result.get("prompt_eval_count", "unknown"),
                result.get("eval_count", "unknown"),
                result.get("done_reason", "unknown"),
                result.get("load_duration", 0) / 1_000_000,
                result.get("prompt_eval_duration", 0) / 1_000_000,
                result.get("eval_duration", 0) / 1_000_000,
            )
            raw_decision = result.get("message", {}).get("content", "")
            try:
                understanding = parse_turn_understanding(raw_decision, controller_text)
            except ValueError as first_error:
                if "invalid JSON" not in str(first_error):
                    raise
                LOGGER.warning(
                    "semantic_controller_invalid_json_retry output_chars=%d",
                    len(raw_decision),
                )
                repair_payload = {
                    **payload,
                    "stream": False,
                    "messages": [
                        *payload["messages"],
                        {"role": "system", "content": (
                            "Your previous response was not valid JSON. Return exactly one "
                            "complete JSON object matching the supplied schema. Include every "
                            "required field. For a complete direct question or statement, reply "
                            "must contain the concise natural-language response to speak. Do not "
                            "use Markdown or explanatory text."
                        )},
                    ],
                    "options": {
                        **payload["options"],
                        "num_predict": SPEECH_RETRY_NUM_PREDICT,
                    },
                }
                async with aiohttp.ClientSession(timeout=timeout) as repair_session:
                    async with repair_session.post(
                        SPEECH_MODEL_URL, json=repair_payload
                    ) as repair_response:
                        repair_response.raise_for_status()
                        repair_result = await repair_response.json()
                raw_decision = repair_result.get("message", {}).get("content", "")
                understanding = parse_turn_understanding(raw_decision, controller_text)
                result = repair_result
                LOGGER.info("semantic_controller_json_retry_succeeded")
            if fragment_resolution is not None:
                relation, fragment_complete, confidence, assembled, reason = (
                    fragment_resolution
                )
                if relation in {"continuation", "correction"} and confidence >= 0.7:
                    understanding = replace(
                        understanding,
                        complete=fragment_complete and understanding.complete,
                        confidence=min(confidence, understanding.confidence),
                        relation=relation,
                        assembled_text=assembled,
                        reason=f"{reason}; {understanding.reason}",
                    )
            self.last_turn_understanding = understanding
            LOGGER.info(
                "turn_understanding complete=%s confidence=%.3f act=%s relation=%s "
                "actionable=%s grounding=%s supersedes=%s reason=%r transcript=%r",
                understanding.complete, understanding.confidence,
                understanding.speech_act, understanding.relation,
                understanding.actionable, understanding.requires_grounding,
                understanding.supersedes_previous, understanding.reason, text,
            )
            if understanding.supersedes_previous:
                self.supersede_previous_agent_turn()
            if understanding.clarification_needed or understanding.confidence < 0.65:
                route, reply, _ = parse_speech_model_output(raw_decision)
                if route != "direct" or not reply:
                    reply = "I didn't catch that clearly. Could you say it again?"
                LOGGER.info(
                    "semantic_clarification_requested confidence=%.3f reason=%r",
                    understanding.confidence, understanding.reason,
                )
                return "direct", reply, None
            if not understanding.complete:
                self.pending_fragment = (
                    understanding.assembled_text or controller_text
                ).strip()
                return "wait", "", None
            effective_text = understanding.assembled_text or text
            if self.pending_fragment:
                if understanding.relation in {"continuation", "correction"}:
                    LOGGER.info(
                        "semantic_fragment_resolved relation=%s pending=%r assembled=%r",
                        understanding.relation, self.pending_fragment, effective_text,
                    )
                else:
                    LOGGER.info(
                        "semantic_fragment_superseded pending=%r new=%r",
                        self.pending_fragment, effective_text,
                    )
                    if self.pending_fragment_turn_id:
                        self.superseded_turn_ids.add(self.pending_fragment_turn_id)
                self.pending_fragment = ""
                self.pending_fragment_turn_id = None

            route, reply, target_session = parse_speech_model_output(raw_decision)
            if (
                route in {"agent", "new_agent", "session"}
                and understanding.actionable
                and not understanding.requires_grounding
                and not requires_tool_backed_action(effective_text)
            ):
                LOGGER.warning("speech_supervisor_repaired_direct_knowledge_request")
                route, target_session = "direct", None
                reply = await self.generate_direct_answer(effective_text)
            if is_referential_agent_question(effective_text) and understanding.actionable:
                tracked = self.resolve_agent_session(effective_text)
                if tracked:
                    route, target_session = "session", str(tracked["session_key"])
                    reply = reply or "I'll add that to the same agent."
            elif understanding.requires_grounding:
                route, target_session = "agent", None
                reply = reply or "I'll verify that against a current source."
            reply = repair_known_transcription_errors(reply)
            if has_stale_identity_confusion(reply):
                LOGGER.warning("speech_supervisor_repaired_false_identity")
                route, reply, target_session = (
                    "direct",
                    "I'm Jarvis, the model operating Live Conversation. "
                    "I answer here directly and use gateway agents when tool-backed work is needed.",
                    None,
                )
            if route in {"agent", "new_agent", "session"} and not (
                understanding.actionable or understanding.requires_grounding
            ):
                LOGGER.warning("speech_supervisor_blocked_unrequested_action route=%s", route)
                route, target_session = "direct", None
                reply = remove_unrequested_action_promises(reply)
            elif route == "direct" and understanding.actionable and re.fullmatch(
                r"(?i)(?:i understand|understood|okay|ok|all right|got it)[.!]?",
                reply.strip(),
            ):
                LOGGER.warning("speech_supervisor_repaired_nonresponsive_action_ack")
                route, reply, target_session = (
                    "agent",
                    "I'll have the agent handle that.",
                    None,
                )
            elif route == "direct" and is_operational_acknowledgment(reply):
                if understanding.actionable or understanding.requires_grounding:
                    LOGGER.warning("speech_supervisor_repaired_missing_agent_route")
                    route, target_session = "agent", None
                else:
                    LOGGER.warning("speech_supervisor_removed_invented_action")
                    reply = remove_unrequested_action_promises(reply)
            if route == "session" and target_session not in self.session_keys:
                LOGGER.warning("speech_supervisor_invalid_session target=%s", target_session)
                route, target_session = "agent", None
            if route == "direct" and not reply:
                LOGGER.warning("speech_supervisor_missing_direct_reply_generating_answer")
                reply = await self.generate_direct_answer(effective_text)
            if route != "ignore" and not reply:
                raise ValueError("speech model returned no speakable text")
            return route, reply, target_session
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
            if await self.refresh_speech_model_acceleration(force=True) is False:
                LOGGER.warning(
                    "speech_supervisor_failed_over_to_degraded_route error=%s",
                    error or error.__class__.__name__,
                )
                return await self.degraded_speech_reply(text)
            raise RuntimeError(f"Speech supervisor unavailable: {error}") from error

    async def semantic_fragment_resolution(
        self, pending: str, current: str
    ) -> tuple[str, bool, float, str, str] | None:
        """Resolve separately committed speech fragments by meaning, not tokens."""
        payload = {
            "model": SPEECH_MODEL,
            "keep_alive": SPEECH_MODEL_KEEP_ALIVE,
            "stream": False,
            "think": False,
            # Ollama's schema-constrained decoder can prematurely stop after
            # the first enum property on this small focused contract. JSON mode
            # plus strict parsing below reliably returns and validates all five
            # fields while preserving the same safety boundary.
            "format": "json",
            "messages": [
                {"role": "system", "content": (
                    "You resolve two consecutive speech transcriptions. Decide whether "
                    "the newer transcription completes the unresolved earlier thought, "
                    "corrects/replaces it, or starts an unrelated new turn. Judge their "
                    "semantic roles and missing arguments; do not use a fixed connector-word "
                    "list and do not trust ASR punctuation. Use continuation when the newer "
                    "words supply a missing subject, object, predicate, complement, condition, "
                    "or proposition. For continuation, assembled_text must express the full "
                    "combined meaning naturally without the speaker's staging instructions. "
                    "For correction, assembled_text must contain the corrected meaning. For "
                    "new, assembled_text must contain only the newer turn. complete means the "
                    "assembled user turn is now answerable or actionable. Return exactly one "
                    "compact JSON object with the five keys relation, complete, confidence, "
                    "assembled_text, and reason. Example shape: "
                    "{\"relation\":\"continuation\",\"complete\":true,"
                    "\"confidence\":0.99,\"assembled_text\":\"What is the capital of "
                    "the nation called Canada?\",\"reason\":\"The newer phrase supplies "
                    "the missing object.\"}"
                )},
                {"role": "user", "content": json.dumps({
                    "unresolved_earlier_transcription": pending,
                    "newer_transcription": current,
                }, ensure_ascii=False)},
            ],
            "options": {
                "temperature": 0, "num_predict": 160,
                "num_ctx": SPEECH_MODEL_CONTEXT,
            },
        }
        timeout = aiohttp.ClientTimeout(total=8)
        last_error: Exception | None = None
        for attempt in range(2):
            request_payload = payload
            if attempt:
                request_payload = {
                    **payload,
                    "messages": [
                        *payload["messages"],
                        {"role": "system", "content": (
                            "The prior output was invalid. Return exactly one schema-matching "
                            "JSON object and no other text."
                        )},
                    ],
                }
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(
                        SPEECH_MODEL_URL, json=request_payload
                    ) as response:
                        response.raise_for_status()
                        result = await response.json()
                content = str(result.get("message", {}).get("content", "")).strip()
                parsed = json.loads(content)
                relation = parsed.get("relation")
                complete = parsed.get("complete")
                confidence = parsed.get("confidence")
                assembled = parsed.get("assembled_text")
                reason = parsed.get("reason")
                if relation not in {"new", "continuation", "correction"}:
                    raise ValueError("invalid fragment relation")
                if not isinstance(complete, bool):
                    raise ValueError("invalid fragment completeness")
                if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
                    raise ValueError("invalid fragment confidence")
                if not isinstance(assembled, str) or not assembled.strip():
                    raise ValueError("missing assembled fragment text")
                return (
                    relation, complete, float(confidence), assembled.strip(),
                    str(reason or "semantic fragment resolution")[:240],
                )
            except Exception as error:
                last_error = error
                LOGGER.warning(
                    "semantic_fragment_controller_retry attempt=%d error=%s",
                    attempt + 1, error,
                )
        LOGGER.warning("semantic_fragment_controller_failed error=%s", last_error)
        return None

    async def generate_direct_answer(self, text: str) -> str:
        """Recover a missing direct reply using the real local conversation model."""
        payload = {
            "model": SPEECH_MODEL,
            "keep_alive": SPEECH_MODEL_KEEP_ALIVE,
            "stream": False,
            "think": False,
            "messages": [
                {"role": "system", "content": (
                    "You are Jarvis in a live spoken conversation. Respond directly and "
                    "naturally to the user's complete turn in one or two concise spoken "
                    "sentences. Do not mention routing, tools, agents, or this instruction."
                )},
                {"role": "user", "content": text},
            ],
            "options": {
                "temperature": 0.2, "num_predict": 128,
                "num_ctx": SPEECH_MODEL_CONTEXT,
            },
        }
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(SPEECH_MODEL_URL, json=payload) as response:
                response.raise_for_status()
                result = await response.json()
        reply = str(result.get("message", {}).get("content", "")).strip()
        if not reply:
            raise ValueError("speech model returned no direct answer")
        return reply

    async def send_spoken_response(
        self,
        socket: web.WebSocketResponse,
        text: str,
        route: str,
        response_id: str,
        message_type: str = "reply",
        expected_generation: int | None = None,
        display_text: str | None = None,
        pcm_override: bytes | None = None,
    ) -> int:
        """Synthesize and stream one complete spoken response."""
        generation = (
            self.speech_generation if expected_generation is None else expected_generation
        )
        if generation != self.speech_generation:
            LOGGER.info(
                "response_skipped_for_newer_speech id=%s route=%s", response_id, route
            )
            return -1
        await socket.send_json({
            "type": message_type,
            "text": text if display_text is None else display_text,
            "route": route,
            "responseId": response_id,
        })
        lock_started = time.perf_counter()
        async with self.speech_lock:
            lock_wait_ms = round((time.perf_counter() - lock_started) * 1000)
            if generation != self.speech_generation:
                LOGGER.info(
                    "response_stream_superseded id=%s route=%s lock_wait_ms=%d",
                    response_id, route, lock_wait_ms,
                )
                return -1
            await socket.send_json({"type": "state", "state": "speaking", "route": route})
            tts_started = time.perf_counter()
            chunk_bytes = OUTPUT_SAMPLE_RATE * 2 // 10
            prefill_bytes = round(
                OUTPUT_SAMPLE_RATE * 2 * PLAYBACK_PREFILL_SECONDS
            )
            total_pcm_bytes = 0
            interrupted = False
            tts_ms = 0

            async def send_chunk(chunk: bytes) -> bool:
                nonlocal total_pcm_bytes
                if generation != self.speech_generation:
                    return False
                await socket.send_json({
                    "type": "response.output_audio.delta",
                    "responseId": response_id,
                    "sampleRate": OUTPUT_SAMPLE_RATE,
                    "audioBase64": base64.b64encode(chunk).decode("ascii"),
                })
                total_pcm_bytes += len(chunk)
                # Keep a small PCM lead ahead of Android's AudioTrack. Sending
                # exactly one 100 ms delta every 100 ms left no jitter margin.
                if total_pcm_bytes > prefill_bytes:
                    await asyncio.sleep(len(chunk) / (OUTPUT_SAMPLE_RATE * 2))
                return True

            if pcm_override is None and hasattr(self.tts, "stream_synthesize"):
                # Qwen arrives as arbitrary HTTP chunks. Reframe it into the
                # same 100 ms deltas used by Android, keeping enough initial
                # audio queued to bridge the model's next codec chunk.
                pending_pcm = bytearray()
                stream = self.tts.stream_synthesize(
                    text, tts_speed_for(text, route, self.tts.speed)
                )
                audio_started = False
                try:
                    async for raw_chunk in stream:
                        if not audio_started:
                            tts_ms = round((time.perf_counter() - tts_started) * 1000)
                            LOGGER.info(
                                "response_stream_start id=%s route=%s chars=%d backend=qwen first_pcm_bytes=%d first_tts_ms=%d",
                                response_id, route, len(text), len(raw_chunk), tts_ms,
                            )
                            await socket.send_json({
                                "type": "output_audio_buffer.started",
                                "responseId": response_id,
                            })
                            audio_started = True
                        pending_pcm.extend(raw_chunk)
                        while len(pending_pcm) >= chunk_bytes:
                            chunk = bytes(pending_pcm[:chunk_bytes])
                            del pending_pcm[:chunk_bytes]
                            if not await send_chunk(chunk):
                                interrupted = True
                                break
                        if interrupted:
                            break
                    if not interrupted and pending_pcm:
                        interrupted = not await send_chunk(bytes(pending_pcm))
                    if not audio_started and not interrupted:
                        raise RuntimeError("Qwen TTS returned no audio")
                finally:
                    close = getattr(stream, "aclose", None)
                    if close is not None:
                        await close()
            else:
                speech_parts = [""] if pcm_override is not None else split_spoken_text(text)
                pcm = pcm_override if pcm_override is not None else await self.tts.synthesize(
                    speech_parts[0], tts_speed_for(speech_parts[0], route, self.tts.speed)
                )
                tts_ms = round((time.perf_counter() - tts_started) * 1000)
                LOGGER.info(
                    "response_stream_start id=%s route=%s chars=%d parts=%d backend=kokoro first_pcm_bytes=%d first_tts_ms=%d",
                    response_id, route, len(text), len(speech_parts), len(pcm), tts_ms,
                )
                await socket.send_json({
                    "type": "output_audio_buffer.started", "responseId": response_id
                })
                for index, _ in enumerate(speech_parts):
                    next_synthesis: asyncio.Task[bytes] | None = None
                    if pcm_override is None and index + 1 < len(speech_parts):
                        next_text = speech_parts[index + 1]
                        next_synthesis = asyncio.create_task(self.tts.synthesize(
                            next_text, tts_speed_for(next_text, route, self.tts.speed)
                        ))
                    for offset in range(0, len(pcm), chunk_bytes):
                        if not await send_chunk(pcm[offset:offset + chunk_bytes]):
                            interrupted = True
                            break
                    if interrupted:
                        if next_synthesis is not None:
                            next_synthesis.cancel()
                        break
                    if next_synthesis is not None:
                        wait_started = time.perf_counter()
                        pcm = await next_synthesis
                        tts_ms += round((time.perf_counter() - wait_started) * 1000)
            await socket.send_json({"type": "response.output_audio.done", "responseId": response_id})
            LOGGER.info(
                "response_stream_done id=%s route=%s pcm_bytes=%d tts_wait_ms=%d lock_wait_ms=%d interrupted=%s",
                response_id, route, total_pcm_bytes, tts_ms, lock_wait_ms, interrupted,
            )
        return tts_ms

    async def process_turn(
        self, socket: web.WebSocketResponse, audio: bytes, agent_pending: bool = False,
        turn_generation: int | None = None,
        handoff_callback: Callable[[str, str | None, str], Awaitable[None]] | None = None,
        transcript_override: str | None = None,
        capture_id: str | None = None,
    ) -> tuple[str, str, str | None] | None:
        started = time.perf_counter()
        incremental_speech: IncrementalSpeechStream | None = None
        if turn_generation is None:
            turn_generation = self.speech_generation
        pending_confirmation_before = self.pending_confirmation
        metrics = TurnMetrics()
        try:
            await socket.send_json({"type": "state", "state": "transcribing"})
            stage = time.perf_counter()
            transcript = transcript_override or await self.transcribe(audio)
            metrics.asr_ms = round((time.perf_counter() - stage) * 1000)
            if not transcript:
                self.update_audio_capture(
                    capture_id, status="no_clear_speech", transcript="",
                    asr_ms=metrics.asr_ms,
                )
                await socket.send_json({"type": "state", "state": "listening", "detail": "No clear speech detected."})
                return
            self.update_audio_capture(
                capture_id, status="transcribed", transcript=transcript,
                asr_ms=metrics.asr_ms,
            )
            await socket.send_json({"type": "transcript", "text": transcript})
            if is_silent_stop_command(transcript):
                self.update_audio_capture(capture_id, status="silent_stop")
                self.interrupt_speech()
                await socket.send_json({
                    "type": "state", "state": "listening", "detail": "Silent stop command."
                })
                return None

            stage = time.perf_counter()
            turn_id = f"turn-{time.time_ns()}"
            response_id = f"response-{time.monotonic_ns()}"
            incremental_speech = IncrementalSpeechStream(
                self, socket, response_id, turn_generation
            )
            assembled_text = transcript
            handoff_request: str | None = None
            policy_command = confirmation_policy_command(assembled_text)
            answer = confirmation_answer(transcript) if self.pending_confirmation else None
            if policy_command:
                if policy_command == "status":
                    mode = "on" if self.confirmation_required else "off"
                    reply = f"Action confirmation is {mode}."
                else:
                    self.set_confirmation_required(policy_command == "confirm")
                    if self.confirmation_required:
                        reply = "Action confirmation is on. I'll ask before taking actions."
                    else:
                        reply = "Action confirmation is off. Explicit requests will proceed immediately."
                    await socket.send_json(self.settings_payload())
                route, target_session = "direct", None
            elif answer is not None:
                pending_request, pending_target, pending_route = self.pending_confirmation
                self.pending_confirmation = None
                if answer:
                    route, reply, target_session = pending_route, "Okay, proceeding.", pending_target
                    handoff_request = pending_request
                else:
                    route, reply, target_session = "direct", "Okay, I won't take that action.", None
            else:
                # speech_reply has deterministic fast paths which intentionally
                # bypass semantic-model classification. Clear the per-turn side
                # channel before every call so one of those paths cannot reuse
                # the previous turn's assembled meaning.
                self.last_turn_understanding = None
                routing_task = asyncio.create_task(self.speech_reply(
                    transcript, agent_pending=agent_pending,
                    stream_callback=incremental_speech.feed,
                ))
                while not routing_task.done():
                    await asyncio.wait({routing_task}, timeout=0.05)
                    if turn_generation != self.speech_generation:
                        routing_task.cancel()
                        try:
                            await routing_task
                        except asyncio.CancelledError:
                            pass
                        await incremental_speech.cancel()
                        self.pending_confirmation = pending_confirmation_before
                        self.remember(
                            "user", transcript, turn_id=turn_id,
                            status="superseded",
                            metadata={"assembled_text": assembled_text},
                        )
                        self.record_event(
                            "interruption", turn_id=turn_id,
                            status="model_generation_cancelled",
                        )
                        self._save_history()
                        await socket.send_json({
                            "type": "turn_superseded", "text": transcript
                        })
                        return None
                route, reply, target_session = routing_task.result()
                understanding = self.last_turn_understanding
                if understanding is not None:
                    assembled_text = understanding.assembled_text or transcript
                if route == "wait":
                    await incremental_speech.cancel()
                    self.pending_fragment_turn_id = turn_id
                    self.record_event(
                        "fragment", turn_id=turn_id, status="awaiting_continuation",
                        content=transcript,
                        metadata={
                            "confidence": understanding.confidence if understanding else 0,
                            "reason": understanding.reason if understanding else "incomplete",
                        },
                    )
                    self._save_history()
                    self.update_audio_capture(
                        capture_id, status="awaiting_continuation",
                        turn_id=turn_id, assembled_text=assembled_text,
                    )
                    await socket.send_json({
                        "type": "state", "state": "listening",
                        "detail": "Waiting for you to finish the thought.",
                    })
                    return None
                agent_request = assembled_text
                if route in ("agent", "new_agent", "session"):
                    if route in {"agent", "new_agent"} and target_session is None:
                        target_session = self.allocate_agent_session_key(agent_request)
                    if self.confirmation_required:
                        self.pending_confirmation = (agent_request, target_session, route)
                        summary = re.sub(r"\s+", " ", assembled_text).strip()[:180].rstrip(" .?!")
                        route = "confirmation"
                        target_session = None
                        reply = f"Before I take that action, should I proceed with: {summary}?"
                    else:
                        handoff_request = agent_request
            metrics.routing_ms = round((time.perf_counter() - stage) * 1000)
            metrics.route = route

            if route == "ignore":
                self.update_audio_capture(
                    capture_id, status="ignored_ambient", turn_id=turn_id,
                    assembled_text=assembled_text, route=route,
                )
                await incremental_speech.cancel()
                await socket.send_json({"type": "state", "state": "listening", "detail": "Ignored likely background speech."})
                return None

            if route != "direct":
                await incremental_speech.cancel()

            self.remember(
                "user", transcript, turn_id=turn_id,
                metadata={"assembled_text": assembled_text},
            )
            if turn_generation != self.speech_generation:
                await incremental_speech.cancel()
                self.pending_confirmation = pending_confirmation_before
                LOGGER.info(
                    "turn_superseded_by_newer_speech transcript=%r", transcript
                )
                await socket.send_json({
                    "type": "turn_superseded", "text": transcript
                })
                return None
            self.remember(
                "assistant", reply, turn_id=turn_id,
                metadata={"route": route, "session_key": target_session},
            )
            self.update_audio_capture(
                capture_id,
                status="complete",
                turn_id=turn_id,
                response_id=response_id,
                assembled_text=assembled_text,
                route=route,
                reply=reply,
                session_key=target_session,
                routing_ms=metrics.routing_ms,
            )

            prepared_handoff: tuple[str, str | None] | None = None
            if handoff_request is not None:
                repaired_request = repair_known_transcription_errors(handoff_request)
                if repaired_request != handoff_request:
                    LOGGER.info(
                        "transcript_repaired original=%r repaired=%r",
                        handoff_request, repaired_request,
                    )
                prepared_handoff = (repaired_request, target_session)
                await socket.send_json({
                    "type": "action_status", "state": "acknowledged",
                    "route": route, "sessionKey": target_session or "",
                })
                # Authorized work starts as soon as routing is final. Spoken
                # acknowledgment proceeds concurrently and cannot add latency.
                if handoff_callback is not None:
                    if len(inspect.signature(handoff_callback).parameters) >= 3:
                        await handoff_callback(*prepared_handoff, turn_id)
                    else:
                        await handoff_callback(*prepared_handoff)

            if route == "direct":
                metrics.tts_ms = await incremental_speech.finish(reply, route)
            else:
                metrics.tts_ms = await self.send_spoken_response(
                    socket, reply, metrics.route, response_id,
                    expected_generation=turn_generation,
                )
            metrics.total_ms = round((time.perf_counter() - started) * 1000)
            self.observe_turn_metrics(metrics)
            self.update_audio_capture(
                capture_id, tts_ms=metrics.tts_ms, total_ms=metrics.total_ms,
            )
            await socket.send_json({"type": "metrics", **asdict(metrics)})
            if prepared_handoff is not None and handoff_callback is None:
                return prepared_handoff[0], reply, prepared_handoff[1]
            await socket.send_json({"type": "state", "state": "listening"})
        except Exception as error:
            self.metric_counters["turn_errors_total"] += 1
            self.update_audio_capture(capture_id, status="error", error=str(error))
            if incremental_speech is not None:
                await incremental_speech.cancel()
            await socket.send_json({"type": "error", "message": str(error)})
        return None

def render_page() -> str:
    return """<!doctype html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Live Conversation</title><style>
html,body{margin:0;min-height:100%;background:#060a10;color:#f4f8fc;font-family:system-ui,sans-serif}main{padding:18px 14px 28px;max-width:760px;margin:auto}h1{font-size:23px;margin:0 0 5px}.sub{color:#9aa9b8;margin:0 0 7px}.setting{color:#7fcbb4;font-size:12px;margin-bottom:16px}.state{font-size:18px;color:#54e0b4;margin:12px 0}.meter{height:14px;background:#101820;border:1px solid #304050;border-radius:8px;overflow:hidden}.meter div{height:100%;width:0;background:#00ab7e;transition:width 60ms}.buttons{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin:14px 0}button{min-height:48px;border:1px solid #00ab7e;border-radius:8px;background:#1e2630;color:#fff;font-size:15px}button:disabled{opacity:.55}.debug-card{border:1px solid #31475a}.debug-line{display:flex;align-items:center;gap:8px;margin:5px 0 10px}.debug-icon{width:12px;height:12px;border-radius:50%;background:#708090;box-shadow:0 0 8px currentColor}.debug-icon.collecting,.debug-icon.working{background:#e0a84f;color:#e0a84f}.debug-icon.fixed{background:#54e0b4;color:#54e0b4}.debug-icon.release_available{background:#59a9ff;color:#59a9ff}.debug-icon.gateway_updated{background:#bf8cff;color:#bf8cff}.debug-icon.failed{background:#ff6b72;color:#ff6b72}.card{background:#0a0e14;border-radius:8px;padding:12px;margin-top:10px;min-height:48px}.label{color:#8797a8;font-size:11px;text-transform:uppercase;letter-spacing:.08em}.metrics{font-size:12px;color:#aebdca;margin-top:12px}.route{display:inline-block;border:1px solid #36556a;border-radius:10px;padding:2px 7px;font-size:11px;margin-left:6px}.history{margin-top:18px}.history-list{display:flex;flex-direction:column;gap:8px;margin-top:8px}.history-empty{color:#718294;font-size:13px}.message{max-width:88%;padding:9px 11px;border-radius:12px;line-height:1.35;white-space:pre-wrap;overflow-wrap:anywhere}.message.user{align-self:flex-end;background:#0e5948}.message.assistant{align-self:flex-start;background:#182431}.message-role{font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:#9db0bf;margin-bottom:3px}
</style></head><body><main><h1>Live Conversation</h1><p class="sub">Accurate local speech recognition. Simple requests answer here; complex work routes to the right OpenClaw session.</p><div id="confirmation" class="setting">Action confirmation: loading…</div><div id="recording" class="setting">Test audio capture: loading…</div><p class="sub">Audio capture is off by default. When enabled, microphone turns and test metadata are saved locally on the OpenClaw host for regression testing.</p><div class="card debug-card"><div class="label">Conversation diagnostics</div><div class="debug-line"><span id="debugIcon" class="debug-icon idle" aria-hidden="true"></span><span id="debugStatus" role="status">No debug submission pending.</span></div><button id="sendDebug">Send for Debug</button></div><div id="state" class="state">Ready</div><div class="meter"><div id="bar"></div></div><div class="buttons"><button id="start">Start conversation</button><button id="stop">Stop</button></div><div class="card"><div class="label">You</div><div id="user">—</div></div><div class="card"><div class="label">Assistant <span id="route" class="route">waiting</span></div><div id="assistant">—</div></div><div id="metrics" class="metrics"></div><section class="history"><div class="label">Recent messages · newest first · last 120</div><div id="history" class="history-list"><div class="history-empty">No conversation history yet.</div></div></section></main><script>
const state=document.getElementById('state'),bar=document.getElementById('bar'),user=document.getElementById('user'),assistant=document.getElementById('assistant'),route=document.getElementById('route'),metrics=document.getElementById('metrics'),confirmation=document.getElementById('confirmation'),recordingSetting=document.getElementById('recording'),debugIcon=document.getElementById('debugIcon'),debugStatus=document.getElementById('debugStatus'),sendDebug=document.getElementById('sendDebug'),historyList=document.getElementById('history');const AUDIO_FRAME_MS=20,START_CONFIRM_MS=300,BARGE_IN_CONFIRM_MS=300,ONSET_PREROLL_MS=2500,PREBUFFER_FRAMES=Math.ceil((START_CONFIRM_MS+ONSET_PREROLL_MS)/AUDIO_FRAME_MS),MAX_DRAIN_FRAMES=24,SEMANTIC_CHECK_MS=750,HARD_ENDPOINT_MS=3500;let ws,timer,recording=false,awaitingResponse=false,responseActive=false,responseTailTimer=null,speechMs=0,silenceMs=0,candidateSpeechMs=0,pre=[],historyMessages=[],pendingTranscript='',endpointPending=false,nextEndpointAt=SEMANTIC_CHECK_MS,confirmationMode='automatic',audioCapture=false,pendingMessages=[];const confirmationToggle=document.createElement('button');confirmationToggle.textContent='Toggle confirmation';confirmation.insertAdjacentElement('afterend',confirmationToggle);const recordingToggle=document.createElement('button');recordingToggle.textContent='Enable test audio capture';recordingSetting.insertAdjacentElement('afterend',recordingToggle);
const deleteRecordings=document.createElement('button');deleteRecordings.textContent='Delete all recordings';deleteRecordings.ariaLabel='Delete all locally retained microphone recordings';recordingToggle.insertAdjacentElement('afterend',deleteRecordings);
function renderHistory(){historyList.replaceChildren();if(!historyMessages.length){const empty=document.createElement('div');empty.className='history-empty';empty.textContent='No conversation history yet.';historyList.appendChild(empty);return}for(const message of historyMessages.slice(-120).reverse()){const bubble=document.createElement('div');bubble.className='message '+message.role;const who=document.createElement('div');who.className='message-role';who.textContent=message.role==='user'?'You':'Assistant';const content=document.createElement('div');content.textContent=message.content;bubble.append(who,content);historyList.appendChild(bubble)}}
function addHistory(role,content){if(!content)return;historyMessages.push({role,content});historyMessages=historyMessages.slice(-120);renderHistory()}
function rms(b64){const s=atob(b64||'');let sum=0,n=0;for(let i=0;i+1<s.length;i+=2){let v=(s.charCodeAt(i)&255)|((s.charCodeAt(i+1)&255)<<8);if(v&32768)v-=65536;const f=v/32768;sum+=f*f;n++}return n?Math.sqrt(sum/n):0}
function send(x){if(ws&&ws.readyState===1){ws.send(JSON.stringify(x));return true}return false}
function sendWhenConnected(x){if(send(x))return;pendingMessages.push(x);start()}
function renderDebugStatus(value){const current=value&&value.state?value:{state:'idle'};debugIcon.className='debug-icon '+current.state;debugStatus.textContent=current.message||'No debug submission pending.';sendDebug.disabled=current.state==='collecting'||current.state==='working';sendDebug.textContent=sendDebug.disabled?'Debug in progress…':'Send for Debug';sendDebug.ariaLabel=debugStatus.textContent}
function report(event,detail){send({type:'client_event',event:event,detail:String(detail||'')})}
function finishTurn(){if(!recording)return;recording=false;awaitingResponse=true;candidateSpeechMs=0;endpointPending=false;try{OpenClawNativeAudio.prepareAgentResponsePlayback()}catch(e){}send({type:'commit'});state.textContent='Transcribing…'}
function begin(){if(recording)return;const interruptedWait=awaitingResponse;recording=true;awaitingResponse=false;candidateSpeechMs=0;speechMs=0;silenceMs=0;endpointPending=false;nextEndpointAt=SEMANTIC_CHECK_MS;if(responseActive){responseActive=false;if(responseTailTimer){clearTimeout(responseTailTimer);responseTailTimer=null}try{OpenClawNativeAudio.interruptAgentResponsePlayback()}catch(e){}report('barge_in','confirmed_user_speech')}send({type:'input_audio_buffer.speech_started'});send({type:'start'});for(const audioBase64 of pre)send({type:'audio',audioBase64});pre=[];report('speech_started',interruptedWait?'while_awaiting_response':'ready');state.textContent='Listening…'}
function processChunk(chunk){const level=rms(chunk);bar.style.width=Math.min(100,Math.round(level*850))+'%';if(!recording){pre.push(chunk);while(pre.length>PREBUFFER_FRAMES)pre.shift();const speechThreshold=responseActive?.025:.012;const speechRequiredMs=responseActive?BARGE_IN_CONFIRM_MS:START_CONFIRM_MS;candidateSpeechMs=level>=speechThreshold?candidateSpeechMs+AUDIO_FRAME_MS:0;if(candidateSpeechMs>=speechRequiredMs)begin();return true}send({type:'audio',audioBase64:chunk});if(level>=.006){speechMs+=AUDIO_FRAME_MS;silenceMs=0;endpointPending=false;nextEndpointAt=SEMANTIC_CHECK_MS}else silenceMs+=AUDIO_FRAME_MS;if(speechMs>=200&&silenceMs>=HARD_ENDPOINT_MS){finishTurn();return false}if(speechMs>=200&&silenceMs>=nextEndpointAt&&!endpointPending){endpointPending=true;nextEndpointAt=silenceMs+400;send({type:'endpoint_candidate'});state.textContent='Listening for more…'}return true}
function tick(){for(let drained=0;drained<MAX_DRAIN_FRAMES;drained++){let chunk='';try{chunk=OpenClawNativeAudio.readChunkBase64()||''}catch(e){state.textContent='Microphone error';return}if(!chunk)return;if(processChunk(chunk)===false)return}}
function start(){if(ws&&(ws.readyState===0||ws.readyState===1))return;ws=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/ws');let audioChunks=0,audioChars=0;ws.onopen=()=>{OpenClawNativeAudio.startCapture(16000,20);timer=setInterval(tick,20);state.textContent='Listening…';for(const message of pendingMessages.splice(0))send(message)};ws.onmessage=e=>{const m=JSON.parse(e.data);if(m.type==='history'){historyMessages=Array.isArray(m.messages)?m.messages.slice(-120):[];renderHistory()}if(m.type==='settings'){confirmation.textContent='Action confirmation: '+(m.action_confirmation==='confirm'?'On — asks before actions':'Off — replies before acting');audioCapture=m.audio_capture===true;recordingSetting.textContent='Test audio capture: '+(audioCapture?'On — saving microphone turns locally':'Off');recordingToggle.textContent=audioCapture?'Disable test audio capture':'Enable test audio capture';renderDebugStatus(m.debug_status)}if(m.type==='state'){state.textContent=m.state[0].toUpperCase()+m.state.slice(1)+'…';if(m.state==='listening'&&!recording){awaitingResponse=false;candidateSpeechMs=0}if((m.detail||'').startsWith('Ignored')||(m.detail||'').startsWith('Silent stop'))pendingTranscript=''}if(m.type==='action_status'&&m.state==='acknowledged')state.textContent='Working…';if(m.type==='partial_transcript'){user.textContent=m.text;pendingTranscript=m.text}if(m.type==='transcript'){user.textContent=m.text;pendingTranscript='';addHistory('user',m.text)}if(m.type==='reply'){assistant.textContent=m.text;route.textContent=m.route;addHistory('assistant',m.text);report('reply',{route:m.route})}if(m.type==='output_audio_buffer.started'){if(responseTailTimer){clearTimeout(responseTailTimer);responseTailTimer=null}awaitingResponse=false;responseActive=true;audioChunks=0;audioChars=0;try{OpenClawNativeAudio.prepareAgentResponsePlayback();report('playback_prepared',m.responseId)}catch(err){report('playback_prepare_error',err)}}if(m.type==='response.output_audio.delta'){audioChunks++;audioChars+=(m.audioBase64||'').length;try{OpenClawNativeAudio.playAgentResponsePcm16Base64(m.audioBase64,m.sampleRate);if(audioChunks===1)report('first_pcm_enqueued','rate='+m.sampleRate+' chars='+(m.audioBase64||'').length)}catch(err){report('pcm_enqueue_error',err)}}if(m.type==='response.output_audio.done'){if(responseTailTimer)clearTimeout(responseTailTimer);responseTailTimer=setTimeout(()=>{responseActive=false;responseTailTimer=null},1500);report('pcm_delivery_done','chunks='+audioChunks+' chars='+audioChars)}if(m.type==='metrics')metrics.textContent=`ASR ${m.asr_ms} ms · Agent ${m.response_ms} ms · TTS ${m.tts_ms} ms · Ready ${m.total_ms} ms`;if(m.type==='error'){awaitingResponse=false;responseActive=false;pendingTranscript='';state.textContent='Error';assistant.textContent=m.message;report('server_error',m.message)}};ws.onerror=()=>state.textContent='Connection error';ws.onclose=()=>state.textContent='Stopped'}
let realtimeListenerSocket=null;
function installRealtimeListeners(){if(!ws||realtimeListenerSocket===ws)return;realtimeListenerSocket=ws;ws.addEventListener('message',event=>{const m=JSON.parse(event.data);if(m.type==='settings'){confirmationMode=m.action_confirmation;confirmationToggle.textContent=confirmationMode==='confirm'?'Turn confirmation off':'Turn confirmation on'}if(m.type==='endpoint_decision'){endpointPending=false;report('semantic_endpoint',m.source+':'+m.probability);if(recording&&m.complete)finishTurn()}if(m.type==='backchannel'){assistant.textContent=m.text;route.textContent='listening'}});setTimeout(()=>{try{report('voice_processing',OpenClawNativeAudio.getVoiceProcessingStatus())}catch(e){report('voice_processing','unavailable')}},250)}
const originalStart=start;start=function(){originalStart();installRealtimeListeners()};confirmationToggle.onclick=()=>sendWhenConnected({type:'set_confirmation',required:confirmationMode!=='confirm'});recordingToggle.onclick=()=>sendWhenConnected({type:'set_audio_capture',enabled:!audioCapture});sendDebug.onclick=()=>sendWhenConnected({type:'send_for_debug'});renderDebugStatus({state:'idle'});
deleteRecordings.onclick=()=>{if(window.confirm('Delete all locally retained microphone recordings? This cannot be undone.'))sendWhenConnected({type:'delete_audio_captures'})};
function stop(){if(timer)clearInterval(timer);timer=null;if(responseTailTimer)clearTimeout(responseTailTimer);responseTailTimer=null;try{OpenClawNativeAudio.stopCapture()}catch(e){}if(ws)ws.close();ws=null;recording=false;awaitingResponse=false;responseActive=false;candidateSpeechMs=0;bar.style.width='0%';state.textContent='Stopped'}
document.getElementById('start').onclick=start;document.getElementById('stop').onclick=()=>{stop();try{OpenClawNativeApp.liveConversationStopped()}catch(e){}};window.addEventListener('pagehide',stop);if(new URLSearchParams(location.search).get('autostart')==='1')start();
</script></body></html>"""


async def index(_: web.Request) -> web.Response:
    return web.Response(text=render_page(), content_type="text/html", headers={"Cache-Control": "no-store"})


async def health(request: web.Request) -> web.Response:
    service: LiveConversationService = request.app["service"]
    router_mode = (
        "gpu" if service.speech_model_accelerated is True
        else "degraded_cpu" if service.speech_model_accelerated is False
        else "unknown"
    )
    return web.json_response({
        "ok": service.stt is not None,
        "status": "healthy" if service.stt is not None else "starting",
        "uptime_seconds": round(time.time() - service.started_at),
        "speech_router": {
            "accelerated": service.speech_model_accelerated,
            "mode": router_mode,
        },
        "components": {
            "asr": service.stt is not None,
            "semantic_turn": service.semantic_turn is not None,
            "tts": bool(service.tts_backend_description),
            "session_catalog": service.sessions_error is None,
        },
        "privacy": {
            "audio_capture_enabled": service.audio_capture_enabled,
            "recording_retention_days": service.recording_retention_days,
            "debug_retention_days": service.debug_retention_days,
        },
    })


async def metrics(request: web.Request) -> web.Response:
    service: LiveConversationService = request.app["service"]
    lines = [
        "# HELP openclaw_live_conversation_uptime_seconds Service uptime.",
        "# TYPE openclaw_live_conversation_uptime_seconds gauge",
        f"openclaw_live_conversation_uptime_seconds {time.time() - service.started_at:.3f}",
    ]
    for name, value in sorted(service.metric_counters.items()):
        metric = f"openclaw_live_conversation_{_prometheus_name(name)}"
        lines.extend((f"# TYPE {metric} counter", f"{metric} {value}"))
    for name, samples in service.metric_samples.items():
        if not samples:
            continue
        metric = f"openclaw_live_conversation_{name}"
        ordered = sorted(samples)
        for percentile, position in (("p50", .50), ("p95", .95), ("p99", .99)):
            index = min(len(ordered) - 1, max(0, int(len(ordered) * position) - 1))
            lines.append(f'{metric}{{quantile="{percentile}"}} {ordered[index]:.3f}')
    return web.Response(text="\n".join(lines) + "\n", content_type="text/plain")


@web.middleware
async def security_headers(request: web.Request, handler: Callable[..., Awaitable[web.StreamResponse]]) -> web.StreamResponse:
    response = await handler(request)
    response.headers.update({
        "Content-Security-Policy": "default-src 'self'; connect-src 'self'; style-src 'unsafe-inline'",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Permissions-Policy": "camera=(), geolocation=(), payment=(), usb=()",
        "Server": "OpenClaw",
    })
    return response


async def wake_websocket(request: web.Request) -> web.WebSocketResponse:
    """Detect the configured wake word from rolling raw 16 kHz PCM windows."""
    service: LiveConversationService = request.app["service"]
    if not _origin_matches_request(request):
        raise web.HTTPForbidden(text="WebSocket Origin is not allowed.")
    sockets: set[web.WebSocketResponse] | None = request.app.get("wake_sockets")
    if sockets is None:
        sockets = set()
    if len(sockets) >= MAX_WAKE_CONNECTIONS:
        raise web.HTTPServiceUnavailable(text="Wake listener capacity reached.")
    socket = web.WebSocketResponse(heartbeat=20, max_msg_size=MAX_WS_MESSAGE_BYTES)
    await socket.prepare(request)
    sockets.add(socket)
    audio = bytearray()
    window_bytes = round(SAMPLE_RATE * 2 * WAKE_WINDOW_SECONDS)
    retain_bytes = SAMPLE_RATE * 2
    last_trigger = 0.0
    async for message in socket:
        if message.type != WSMsgType.BINARY:
            continue
        if len(message.data) > MAX_WS_MESSAGE_BYTES:
            await socket.close(code=1009, message=b"Wake audio frame too large")
            break
        audio.extend(message.data)
        if len(audio) < window_bytes:
            continue
        window = bytes(audio[-window_bytes:])
        audio[:] = audio[-retain_bytes:]
        transcript = await service.transcribe(window, purpose="wake")
        LOGGER.info("wake_word_probe transcript=%r", transcript)
        if is_wake_word(transcript) and time.monotonic() - last_trigger >= WAKE_COOLDOWN_SECONDS:
            last_trigger = time.monotonic()
            await socket.send_json({"type": "wake", "word": WAKE_WORD})
            LOGGER.info("wake_word_detected word=%s", WAKE_WORD)
    sockets.discard(socket)
    return socket


async def websocket(request: web.Request) -> web.WebSocketResponse:
    service: LiveConversationService = request.app["service"]
    if not _origin_matches_request(request):
        raise web.HTTPForbidden(text="WebSocket Origin is not allowed.")
    sockets: set[web.WebSocketResponse] | None = request.app.get("live_sockets")
    if sockets is None:
        sockets = set()
    if len(sockets) >= MAX_LIVE_CONNECTIONS:
        raise web.HTTPServiceUnavailable(text="Conversation capacity reached.")
    socket = web.WebSocketResponse(heartbeat=20, max_msg_size=MAX_WS_MESSAGE_BYTES)
    await socket.prepare(request)
    sockets.add(socket)
    _increment_metric(service, "websocket_connections_total")
    await socket.send_json({"type": "history", "messages": service.recent_history()})
    await socket.send_json(service.settings_payload())
    while service.pending_spoken_replies:
        reply = service.pending_spoken_replies.popleft()
        await service.send_spoken_response(
            socket, reply, "agent", f"agent-reconnect-{time.monotonic_ns()}"
        )
    audio = bytearray()
    turn_queue: asyncio.Queue[tuple[bytes, int, str | None, str | None]] = asyncio.Queue(
        maxsize=MAX_PENDING_TURNS
    )
    turn_worker: asyncio.Task[None] | None = None
    partial_task: asyncio.Task[None] | None = None
    silent_stop_detected = False
    turn_sequence = 0
    next_partial_bytes = round(SAMPLE_RATE * 2 * PARTIAL_TRANSCRIPT_INTERVAL_SECONDS)
    online_transcript = OnlineTranscript.empty()
    endpoint_task: asyncio.Task[None] | None = None
    backchannel_used = False
    prefetched_kinds: set[str] = set()
    agent_tasks: set[asyncio.Task[None]] = set()
    user_input_active = False
    conversation_idle = asyncio.Event()
    conversation_idle.set()
    callback_delivery_lock = asyncio.Lock()
    client_context: dict[str, Any] = {
        "user_agent": request.headers.get("User-Agent", ""),
    }

    async def run_partial(
        snapshot: bytes, sequence: int, transcript_state: OnlineTranscript
    ) -> None:
        nonlocal silent_stop_detected
        try:
            if hasattr(service, "transcribe_online"):
                stable, unstable, transcript = await service.transcribe_online(
                    snapshot, transcript_state, purpose="partial"
                )
            else:
                transcript = await service.transcribe(snapshot, purpose="partial")
                stable, unstable = "", transcript
            if transcript and sequence == turn_sequence and is_silent_stop_command(transcript):
                silent_stop_detected = True
                service.interrupt_speech()
                await socket.send_json({"type": "output_audio_buffer.cleared"})
            elif transcript and sequence == turn_sequence and not socket.closed:
                await socket.send_json({
                    "type": "partial_transcript", "text": transcript,
                    "stable": stable, "unstable": unstable,
                })
                if stable and hasattr(service, "prefetch_for_partial"):
                    kind = await service.prefetch_for_partial(stable)
                    if kind and kind not in prefetched_kinds:
                        prefetched_kinds.add(kind)
                        service.record_event(
                            "prefetch", source="stable_asr", target=kind,
                            status="complete", content=stable[-240:],
                        )
        except Exception as error:
            LOGGER.warning("partial_transcription_failed error=%s", error)

    async def run_agent(
        transcript: str, target_session: str | None, origin_turn_id: str
    ) -> None:
        if target_session is None:
            target_session = service.allocate_agent_session_key(transcript)
        service.register_agent_session(target_session, transcript, origin_turn_id)
        work = asyncio.create_task(service.agent_reply(transcript, target_session))
        refresh = asyncio.create_task(service.refresh_sessions(force=True))
        started = time.monotonic()
        reply = ""
        try:
            if not socket.closed:
                try:
                    await socket.send_json({
                        "type": "agent_status", "state": "working", "sessionKey": target_session
                    })
                except (ConnectionError, RuntimeError):
                    pass
            reply = await work
            await refresh
            service.update_agent_session(target_session, "complete", reply)
            await service.refresh_sessions(force=True)
            if origin_turn_id in service.superseded_turn_ids:
                LOGGER.info(
                    "agent_callback_suppressed_superseded_turn turn_id=%s session=%s",
                    origin_turn_id, target_session,
                )
                if not socket.closed:
                    await socket.send_json({
                        "type": "agent_status", "state": "superseded",
                        "sessionKey": target_session,
                    })
                return
            if socket.closed:
                service.remember("assistant", reply)
                service.queue_pending_reply(reply)
                return
            async with callback_delivery_lock:
                await conversation_idle.wait()
                if socket.closed:
                    service.remember("assistant", reply)
                    service.queue_pending_reply(reply)
                    return
                conversation_idle.clear()
                callback_generation = service.speech_generation
                service.remember("assistant", reply)
                try:
                    await service.send_spoken_response(
                        socket, reply, "agent", f"agent-{time.monotonic_ns()}",
                        expected_generation=callback_generation,
                    )
                finally:
                    if turn_queue.empty() and not user_input_active:
                        conversation_idle.set()
            await socket.send_json({
                "type": "agent_status",
                "state": "complete",
                "sessionKey": target_session,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            })
            await socket.send_json({"type": "state", "state": "listening"})
        except asyncio.CancelledError:
            work.cancel()
            refresh.cancel()
            raise
        except Exception as error:
            service.update_agent_session(target_session, "failed")
            if reply:
                service.queue_pending_reply(reply)
                return
            if service.gateway_is_transient(error):
                message = "The gateway is still reconnecting. Please ask me to retry when it is back."
            else:
                message = "I couldn't complete that agent request."
                LOGGER.exception("agent_request_failed error=%s", error)
            if socket.closed:
                service.queue_pending_reply(message)
            else:
                async with callback_delivery_lock:
                    await conversation_idle.wait()
                    conversation_idle.clear()
                    try:
                        await service.send_spoken_response(
                            socket, message, "agent",
                            f"agent-error-{time.monotonic_ns()}",
                        )
                    finally:
                        if turn_queue.empty() and not user_input_active:
                            conversation_idle.set()
                await socket.send_json({
                    "type": "agent_status", "state": "error", "sessionKey": target_session
                })

    async def run_debug(request_id: str) -> None:
        target_session = service.allocate_agent_session_key(
            f"debug and correct live conversation {request_id}"
        )
        try:
            bundle_path, baseline = await service.create_debug_bundle(
                request_id, client_context
            )
            service.set_debug_status(
                "working", request_id=request_id, requested_at=int(time.time()),
                session_key=target_session, bundle_path=str(bundle_path),
                message="Correction agent is diagnosing the captured conversation.",
            )
            service.register_agent_session(
                target_session,
                "Diagnose and correct the submitted Live Conversation issue.",
                f"debug-{request_id}",
            )
            if not socket.closed:
                await socket.send_json(service.settings_payload())
            prompt = (
                "You are the dedicated Live Conversation correction agent. The user pressed "
                "Send for Debug, explicitly authorizing diagnosis, implementation, verification, "
                "and delivery for the submitted problem. Read the complete local diagnostic bundle "
                f"at {bundle_path}. Inspect the referenced real microphone WAV files, conversation "
                "events, timings, service logs, gateway logs, and repository state. Reproduce the "
                "failure before changing code. Correct the root cause in "
                f"{DEFAULT_DASHBOARD_REPO}, add regression coverage that uses the captured audio "
                "when relevant, and run proportionate end-to-end tests. Deploy affected services, "
                "restart the Live Conversation service when its code changes, and verify loopback "
                "and Android-facing health. Preserve unrelated user "
                "changes. If app or served application code changes, commit and push it and publish "
                "a new signed GitHub release when an installable release is required. If the root "
                "cause is OpenClaw configuration or an available supported gateway update, apply it "
                "using supported update-safe configuration and the safe gateway restart workflow. "
                "Do not patch installed OpenClaw runtime/package code. Finish with a concise report "
                "including root cause, verification, release tag if any, and gateway change if any."
            )
            reply = await service.agent_reply(prompt, target_session)
            service.update_agent_session(target_session, "complete", reply)
            after = await service.system_update_snapshot()
            release_available = (
                bool(after.get("dashboard_tag"))
                and after.get("dashboard_tag") != baseline.get("dashboard_tag")
            )
            gateway_updated = any(
                after.get(key) != baseline.get(key)
                for key in ("gateway_version", "gateway_service", "gateway_config_stamp")
            )
            state_name = (
                "release_available" if release_available else
                "gateway_updated" if gateway_updated else "fixed"
            )
            message = (
                f"New release {after.get('dashboard_tag')} is available."
                if release_available else
                "Gateway update applied." if gateway_updated else
                "Correction completed; no new install is required."
            )
            service.set_debug_status(
                state_name, request_id=request_id,
                requested_at=service.debug_status.get("requested_at"),
                completed_at=int(time.time()), session_key=target_session,
                bundle_path=str(bundle_path), message=message,
                release_available=release_available,
                release_tag=after.get("dashboard_tag") if release_available else "",
                gateway_updated=gateway_updated,
                result=re.sub(r"\s+", " ", reply).strip()[:1000],
            )
            if not socket.closed:
                await socket.send_json(service.settings_payload())
        except asyncio.CancelledError:
            raise
        except Exception as error:
            LOGGER.exception("debug_correction_failed request_id=%s error=%s", request_id, error)
            service.update_agent_session(target_session, "failed")
            service.set_debug_status(
                "failed", request_id=request_id, completed_at=int(time.time()),
                session_key=target_session,
                message="The correction session failed. The diagnostic bundle was retained.",
                error=f"{error.__class__.__name__}: {error}",
            )
            if not socket.closed:
                await socket.send_json(service.settings_payload())

    async def run_turn(
        turn: bytes, generation: int, transcript: str | None, capture_id: str | None
    ) -> None:
        async def start_handoff(
            transcript: str, target_session: str | None, origin_turn_id: str
        ) -> None:
            task = asyncio.create_task(run_agent(
                transcript, target_session, origin_turn_id
            ))
            agent_tasks.add(task)
            task.add_done_callback(agent_tasks.discard)

        process_arguments = {
            "agent_pending": bool(agent_tasks),
            "turn_generation": generation,
            "handoff_callback": start_handoff,
        }
        if transcript is not None:
            process_arguments["transcript_override"] = transcript
        if capture_id is not None:
            process_arguments["capture_id"] = capture_id
        handoff = await service.process_turn(socket, turn, **process_arguments)
        # Compatibility for test doubles and older service implementations.
        if handoff:
            transcript, _, target_session = handoff
            await start_handoff(
                transcript, target_session, f"legacy-turn-{time.time_ns()}"
            )

    async def run_turn_queue() -> None:
        while True:
            turn, generation, transcript, capture_id = await turn_queue.get()
            conversation_idle.clear()
            try:
                await run_turn(turn, generation, transcript, capture_id)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                LOGGER.exception("queued_turn_failed error=%s", error)
            finally:
                turn_queue.task_done()
                if turn_queue.empty() and not user_input_active:
                    conversation_idle.set()

    turn_worker = asyncio.create_task(run_turn_queue())

    async for message in socket:
        if message.type != WSMsgType.TEXT:
            continue
        try:
            payload = json.loads(message.data)
        except (ValueError, TypeError):
            _increment_metric(service, "invalid_messages_total")
            await socket.send_json({"type": "error", "message": "Invalid JSON message."})
            continue
        if not isinstance(payload, dict):
            _increment_metric(service, "invalid_messages_total")
            await socket.send_json({"type": "error", "message": "Message must be an object."})
            continue
        kind = payload.get("type")
        if kind == "input_audio_buffer.speech_started":
            user_input_active = True
            conversation_idle.clear()
            service.interrupt_speech()
            await socket.send_json({"type": "output_audio_buffer.cleared"})
        elif kind == "set_confirmation":
            service.set_confirmation_required(bool(payload.get("required")))
            await socket.send_json(service.settings_payload())
        elif kind == "set_audio_capture":
            service.set_audio_capture_enabled(bool(payload.get("enabled")))
            await socket.send_json(service.settings_payload())
        elif kind == "delete_audio_captures":
            deleted = service.delete_audio_captures()
            await socket.send_json({"type": "recordings_deleted", **deleted})
        elif kind == "send_for_debug":
            if service.debug_status.get("state") in {"collecting", "working"}:
                await socket.send_json(service.settings_payload())
                continue
            request_id = f"debug-{datetime.now(ZoneInfo('America/Detroit')):%Y%m%dT%H%M%S}-{secrets.token_hex(3)}"
            service.set_debug_status(
                "collecting", request_id=request_id, requested_at=int(time.time()),
                message="Collecting redacted diagnostics and recent captured audio.",
            )
            await socket.send_json(service.settings_payload())
            task = asyncio.create_task(run_debug(request_id))
            agent_tasks.add(task)
            task.add_done_callback(agent_tasks.discard)
        elif kind == "start":
            turn_sequence += 1
            silent_stop_detected = False
            backchannel_used = False
            prefetched_kinds.clear()
            online_transcript = OnlineTranscript.empty()
            audio.clear()
            next_partial_bytes = round(
                SAMPLE_RATE * 2 * PARTIAL_TRANSCRIPT_INTERVAL_SECONDS
            )
        elif kind == "audio":
            try:
                chunk = base64.b64decode(payload.get("audioBase64", ""), validate=True)
            except (ValueError, TypeError):
                _increment_metric(service, "invalid_audio_frames_total")
                await socket.send_json({"type": "error", "message": "Invalid audio frame."})
                continue
            if len(chunk) > MAX_WS_MESSAGE_BYTES or len(audio) + len(chunk) > MAX_TURN_AUDIO_BYTES:
                audio.clear()
                _increment_metric(service, "audio_limit_rejections_total")
                await socket.send_json({
                    "type": "error", "message": "Audio turn exceeded the production safety limit."
                })
                continue
            audio.extend(chunk)
            if (
                len(audio) >= next_partial_bytes
                and (not partial_task or partial_task.done())
            ):
                snapshot = bytes(audio)
                sequence = turn_sequence
                next_partial_bytes = len(snapshot) + round(
                    SAMPLE_RATE * 2 * PARTIAL_TRANSCRIPT_INTERVAL_SECONDS
                )
                partial_task = asyncio.create_task(
                    run_partial(snapshot, sequence, online_transcript)
                )
        elif kind == "endpoint_candidate":
            if endpoint_task and not endpoint_task.done():
                continue
            sequence = turn_sequence
            snapshot = bytes(audio)
            transcript_snapshot = online_transcript.display()

            async def evaluate_endpoint() -> None:
                nonlocal backchannel_used
                try:
                    decision = await service.endpoint_decision(
                        snapshot, transcript_snapshot
                    )
                    if sequence != turn_sequence or socket.closed:
                        return
                    await socket.send_json({
                        "type": "endpoint_decision",
                        "complete": decision.complete,
                        "probability": round(decision.probability, 4),
                        "source": decision.source,
                    })
                    duration = len(snapshot) / (SAMPLE_RATE * 2)
                    if (
                        not decision.complete and not backchannel_used
                        and duration >= 4.0 and len(online_transcript.stable_words) >= 3
                    ):
                        backchannel_used = True
                        await service.send_spoken_response(
                            socket, "", "backchannel",
                            f"backchannel-{time.monotonic_ns()}",
                            message_type="backchannel",
                            expected_generation=service.speech_generation,
                            display_text=BACKCHANNEL_DISPLAY_TEXT,
                            pcm_override=affirmative_hum_pcm(),
                        )
                except Exception as error:
                    LOGGER.warning("semantic_endpoint_failed error=%s", error)
                    if sequence == turn_sequence and not socket.closed:
                        await socket.send_json({
                            "type": "endpoint_decision", "complete": False,
                            "probability": 0.0, "source": "error",
                        })

            endpoint_task = asyncio.create_task(evaluate_endpoint())
        elif kind == "commit":
            turn_sequence += 1
            user_input_active = False
            if not audio:
                if turn_queue.empty():
                    conversation_idle.set()
                continue
            if silent_stop_detected:
                audio.clear()
                silent_stop_detected = False
                await socket.send_json({
                    "type": "state", "state": "listening", "detail": "Silent stop command."
                })
                if turn_queue.empty():
                    conversation_idle.set()
                continue
            turn = bytes(audio)
            audio.clear()
            capture_id = (
                service.begin_audio_capture(turn, turn_sequence, client_context)
                if hasattr(service, "begin_audio_capture") else None
            )
            if partial_task and not partial_task.done():
                try:
                    await partial_task
                except Exception:
                    pass
            final_transcript = None
            if hasattr(service, "finalize_online_transcript"):
                final_transcript = await service.finalize_online_transcript(
                    turn, online_transcript
                )
            try:
                turn_queue.put_nowait(
                    (turn, service.speech_generation, final_transcript or None, capture_id)
                )
            except asyncio.QueueFull:
                _increment_metric(service, "turn_queue_rejections_total")
                await socket.send_json({
                    "type": "error", "message": "Conversation is busy. Please retry this turn."
                })
        elif kind == "client_event":
            event = str(payload.get("event", ""))[:80]
            detail = str(payload.get("detail", ""))[:2000]
            if event in {"voice_processing", "speech_started", "barge_in"}:
                client_context[event] = detail
            LOGGER.info("client_event event=%s detail=%s", payload.get("event", ""), payload.get("detail", ""))
        else:
            _increment_metric(service, "invalid_messages_total")
            await socket.send_json({"type": "error", "message": "Unsupported message type."})
    if turn_worker and not turn_worker.done():
        turn_worker.cancel()
    conversation_idle.set()
    sockets.discard(socket)
    # Agent tasks deliberately survive a WebView disconnect. Their final reply
    # is persisted and queued for speech when Live Conversation reconnects.
    return socket


async def build_app(args: argparse.Namespace) -> web.Application:
    service = LiveConversationService(
        args.session_key, args.tts_speed,
        getattr(args, "history_path", DEFAULT_HISTORY_PATH),
        getattr(args, "settings_path", DEFAULT_SETTINGS_PATH),
        getattr(args, "recordings_path", DEFAULT_RECORDINGS_PATH),
        getattr(args, "debug_path", DEFAULT_DEBUG_PATH),
        getattr(args, "recording_retention_days", DEFAULT_RECORDING_RETENTION_DAYS),
        getattr(args, "debug_retention_days", DEFAULT_DEBUG_RETENTION_DAYS),
        getattr(args, "recording_max_bytes", DEFAULT_RECORDING_MAX_BYTES),
        getattr(args, "debug_max_bytes", DEFAULT_DEBUG_MAX_BYTES),
    )
    await service.start()
    app = web.Application(client_max_size=MAX_HTTP_BODY_BYTES, middlewares=[security_headers])
    app["service"] = service
    app["live_sockets"] = set()
    app["wake_sockets"] = set()
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/metrics", metrics)
    app.router.add_get("/wake", wake_websocket)
    app.router.add_get("/ws", websocket)
    async def cleanup(_: web.Application) -> None:
        await service.stop()

    app.on_cleanup.append(cleanup)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--session-key", default=DEFAULT_SESSION_KEY)
    parser.add_argument("--history-path", default=DEFAULT_HISTORY_PATH)
    parser.add_argument("--settings-path", default=DEFAULT_SETTINGS_PATH)
    parser.add_argument("--recordings-path", default=DEFAULT_RECORDINGS_PATH)
    parser.add_argument("--debug-path", default=DEFAULT_DEBUG_PATH)
    parser.add_argument(
        "--recording-retention-days", type=int,
        default=int(os.environ.get("LIVE_CONVERSATION_RECORDING_RETENTION_DAYS", DEFAULT_RECORDING_RETENTION_DAYS)),
    )
    parser.add_argument(
        "--debug-retention-days", type=int,
        default=int(os.environ.get("LIVE_CONVERSATION_DEBUG_RETENTION_DAYS", DEFAULT_DEBUG_RETENTION_DAYS)),
    )
    parser.add_argument(
        "--recording-max-bytes", type=int,
        default=int(os.environ.get("LIVE_CONVERSATION_RECORDING_MAX_BYTES", DEFAULT_RECORDING_MAX_BYTES)),
    )
    parser.add_argument(
        "--debug-max-bytes", type=int,
        default=int(os.environ.get("LIVE_CONVERSATION_DEBUG_MAX_BYTES", DEFAULT_DEBUG_MAX_BYTES)),
    )
    parser.add_argument("--tts-speed", type=float, default=1.08)
    args = parser.parse_args()
    web.run_app(build_app(args), host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
