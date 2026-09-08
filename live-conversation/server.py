#!/usr/bin/env python3
"""Dedicated low-latency voice service for the Android Dashboard."""

from __future__ import annotations

import argparse
import asyncio
import base64
from collections import deque
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
import numpy as np
from pipecat.services.whisper.stt import WhisperSTTService
from pipecat.transcriptions.language import Language


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
SPEECH_MODEL_URL = "http://127.0.0.1:11434/api/chat"
# Large enough for materially better natural-language supervision while staying
# within the sub-second warm-response budget on the local Ollama GPUs.
SPEECH_MODEL = "qwen3.5:4b"
SPEECH_MODEL_KEEP_ALIVE = "30m"
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
        "session_key": {"type": "string"},
    },
    "required": ["route", "reply", "session_key"],
    "additionalProperties": False,
}
CAPABILITY_CACHE_SECONDS = 60
SESSION_CACHE_SECONDS = 3
SESSION_POLL_SECONDS = 10
PARTIAL_TRANSCRIPT_INTERVAL_SECONDS = 1.0
AGENT_RESULT_POLL_SECONDS = 3.0
AGENT_RESULT_WAIT_SECONDS = 600.0
DEFAULT_HISTORY_PATH = "/home/john/.openclaw/state/live-conversation-history.json"
DEFAULT_SETTINGS_PATH = "/home/john/.openclaw/state/live-conversation-settings.json"
MAX_HISTORY_MESSAGES = 80
MAX_HISTORY_CHARS = 48_000
PROMPT_HISTORY_MESSAGES = 60
PROMPT_HISTORY_CHARS = 28_000
WAKE_WORD = "jarvis"
WAKE_WORD_ALIASES = frozenset(("jarvis", "jervis", "chavez"))
WAKE_WINDOW_SECONDS = 2.5
WAKE_COOLDOWN_SECONDS = 5.0


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
    if hearing_check:
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
    testing_statement = re.match(
        r"^(?:(?:i am|i'm) )?(?:just )?(?:testing|testing out|trying|trying out|"
        r"checking|checking out)\b",
        normalized,
    )
    explicit_request = re.search(
        r"\b(?:can|could|would|will) you\b|\bplease\b|"
        r"\b(?:have|ask|tell)\b.*\bagent\b|"
        r"\band (?:fix|change|update|monitor|verify|review|inspect|start|spawn|launch)\b",
        normalized,
    )
    if testing_statement and not explicit_request:
        return "I hear you. Go ahead with the test."
    return None


def confirmation_policy_command(transcript: str) -> str | None:
    """Recognize voice commands that configure action confirmation."""
    normalized = re.sub(r"[^a-z0-9']+", " ", transcript.lower()).strip()
    if re.search(r"\b(?:what is|what's|tell me)\b.*\bconfirmation (?:mode|setting|policy)\b", normalized):
        return "status"
    confirmation = re.search(r"\b(?:confirm|confirmation|ask me|permission|approval)\b", normalized)
    action = re.search(r"\b(?:action|actions|act|acting|anything|proceed|doing|do it)\b", normalized)
    if not confirmation or not action:
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
        or re.search(r"\b(?:have|ask|tell)\b.*\bagent\b", normalized)
        or re.match(
            r"^(?:also )?(?:fix|change|update|monitor|verify|review|inspect|check|"
            r"start|spawn|launch|create|send|add|remove|stop|run|build|deploy)\b",
            normalized,
        )
        or re.search(
            r"\bneeds? (?:to be )?(?:fixed|changed|updated|reviewed|checked|investigated)\b",
            normalized,
        )
    )


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


def speech_model_prompt(
    now: datetime | None = None,
    agent_pending: bool = False,
    capabilities: str = "",
    sessions: str = "",
    voice_sessions: str = "",
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
Jarvis and the Live Conversation model are the same speaker: both refer to you. You receive John's locally transcribed microphone input, choose how each turn is handled, and speak the response. A gateway agent is only a tool-backed work session that you may use; it is not a separate Live Conversation model. Never claim that you are merely a supervisor outside Live Conversation, and never ask an agent to verify whether you can hear John. If a microphone utterance reaches you as a transcript, you heard it.
You are not a general chatbot pretending to lack system access. You can see the live gateway-session summary and capability catalog below, answer questions about them directly, route a follow-up into an existing session, or launch a separate agent session. {pending_note}
{confirmation_note}
First decide whether John actually requested something. Statements describing what he is currently doing, testing, observing, or noticing are conversation—not authorization to start work. Words such as “test,” “updates,” “performance,” or an agent name do not make a statement actionable. Launch or message an agent only when the transcript contains a request, command, or unmistakable instruction. Never invent work such as monitoring, verifying, checking, or updating when John merely says he is testing something.
Return one JSON object matching the required schema. Choose `route` before writing `reply`:
- `direct`: conversation, observations, acknowledgments, timeless knowledge, or answers already available in this prompt. The reply must answer or acknowledge naturally and must not promise agent work.
- `agent`: an explicit request requiring apps, projects, private context, files, logs, calendar, messages, memory, current facts, tools, judgment, or a system action. The reply briefly acknowledges the work.
- `new_agent`: only when John explicitly requests another, new, separate, or additional agent. Multiple agents may work concurrently.
- `session`: only for a follow-up clearly aimed at one listed session. Copy its exact key into `session_key`; never invent one.
- `ignore`: only speech clearly not addressed to you and having no plausible conversational meaning. Use an empty reply.
Use an empty `session_key` for every route except `session`. A question about status or currently running work is `direct`; answer it from the session summary. If a transcript is visibly unfinished, ask John to finish it with `direct`. Imperfect grammar alone is not grounds to ignore a turn.
The current local date and time is {clock.strftime('%A, %B %-d, %Y, %-I:%M %p')} America/Detroit; time and date questions can be answered directly.
Available OpenClaw capabilities (cached and refreshed automatically):
{capabilities or 'Capability catalog is temporarily unavailable; delegate capability questions to the agent.'}
Live and recent gateway sessions (refreshed automatically; hasActiveRun is authoritative):
{sessions or 'No active or recent gateway sessions were returned.'}
Sessions launched from this Live Conversation, newest first:
{voice_sessions or 'No session has been launched from this Live Conversation yet.'}
Resolve “this agent,” “that agent,” “it,” and similar follow-ups to the newest relevant Live Conversation session above. Preserve the subject and intent established by recent user turns. Do not replace a specific referent with a generic list of all sessions.
Use this catalog to answer capability questions quickly. If the user explicitly asks to use, configure, expand, or change a capability, choose `agent`. Do not claim an unavailable capability exists. Newly added agents and skills appear after the catalog refreshes.
Interpret likely recognition mistakes using the conversation and session context. In this voice app, “five agent” or “five conversation model” means “live agent” or “live conversation model” unless John explicitly discusses the number five or five distinct agents; correct that known ASR error without asking and use the corrected word “live” in the acknowledgment. Do not silently replace any other uncertain proper noun; ask a short clarification instead.
When John asks to make the live agent or Live Conversation model more capable, that is a complete actionable request: choose `agent` so the agent can review the conversation and current implementation. Do not ask which capabilities he means unless he explicitly presents alternatives requiring a choice.
Never fabricate private, project, or agent progress. Keep `reply` short and natural for speech.
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


class PersistentTtsWorker:
    def __init__(self, command: str, runtime_dir: str, model_dir: str, speed: float = 1.15):
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
        try:
            self.process.terminate()
        except ProcessLookupError:
            pass
        try:
            await self.process.wait()
        except ProcessLookupError:
            pass
        self.process = None


class LiveConversationService:
    def __init__(
        self,
        session_key: str,
        tts_speed: float,
        history_path: str | None = None,
        settings_path: str | None = None,
    ):
        self.session_key = session_key
        self.stt: WhisperSTTService | None = None
        self.stt_description = "not loaded"
        self.tts = PersistentTtsWorker(DEFAULT_TTS_WORKER, DEFAULT_TTS_RUNTIME, DEFAULT_TTS_MODEL_DIR, tts_speed)
        self.speech_lock = asyncio.Lock()
        self.transcribe_lock = asyncio.Lock()
        self.capabilities = ""
        self.capabilities_updated_at = 0.0
        self.capability_refresh_task: asyncio.Task[str] | None = None
        self.sessions = ""
        self.session_keys: set[str] = set()
        self.session_records: dict[str, dict[str, Any]] = {}
        self.sessions_updated_at = 0.0
        self.session_refresh_task: asyncio.Task[str] | None = None
        self.session_poll_task: asyncio.Task[None] | None = None
        self.speech_generation = 0
        self.pending_spoken_replies: deque[str] = deque(maxlen=10)
        self.confirmation_required = False
        self.pending_confirmation: tuple[str, str | None, str] | None = None
        self.recent_agent_sessions: deque[dict[str, Any]] = deque(maxlen=12)
        self.history_path = Path(history_path) if history_path else None
        self.settings_path = Path(settings_path) if settings_path else None
        self.history: deque[dict[str, str]] = deque(maxlen=MAX_HISTORY_MESSAGES)
        self._load_settings()
        self._load_history()

    def _load_settings(self) -> None:
        if not self.settings_path or not self.settings_path.exists():
            return
        try:
            payload = json.loads(self.settings_path.read_text(encoding="utf-8"))
            self.confirmation_required = payload.get("action_confirmation") == "confirm"
            tracked = payload.get("recent_agent_sessions", [])
            if isinstance(tracked, list):
                for item in tracked[-12:]:
                    if (
                        isinstance(item, dict)
                        and isinstance(item.get("session_key"), str)
                        and isinstance(item.get("request"), str)
                    ):
                        self.recent_agent_sessions.append(dict(item))
        except (OSError, ValueError, TypeError) as error:
            LOGGER.warning("conversation_settings_load_failed error=%s", error)

    def _save_settings(self) -> None:
        if not self.settings_path:
            return
        try:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.settings_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps({
                    "action_confirmation": (
                        "confirm" if self.confirmation_required else "automatic"
                    ),
                    "recent_agent_sessions": list(self.recent_agent_sessions),
                }, indent=2),
                encoding="utf-8",
            )
            temporary.replace(self.settings_path)
        except OSError as error:
            LOGGER.warning("conversation_settings_save_failed error=%s", error)

    def set_confirmation_required(self, required: bool) -> None:
        self.confirmation_required = required
        if not required:
            self.pending_confirmation = None
        self._save_settings()

    def settings_payload(self) -> dict[str, str]:
        return {
            "type": "settings",
            "action_confirmation": (
                "confirm" if self.confirmation_required else "automatic"
            ),
        }

    def _load_history(self) -> None:
        if not self.history_path or not self.history_path.exists():
            return
        try:
            payload = json.loads(self.history_path.read_text(encoding="utf-8"))
            messages = payload.get("messages", []) if isinstance(payload, dict) else []
            for message in messages[-MAX_HISTORY_MESSAGES:]:
                role = message.get("role")
                content = message.get("content")
                if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                    self.history.append({"role": role, "content": content.strip()})
        except (OSError, ValueError, TypeError) as error:
            LOGGER.warning("conversation_history_load_failed error=%s", error)

    def _save_history(self) -> None:
        if not self.history_path:
            return
        try:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.history_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps({"messages": list(self.history)}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(self.history_path)
        except OSError as error:
            LOGGER.warning("conversation_history_save_failed error=%s", error)

    def remember(self, role: str, content: str) -> None:
        content = content.strip()
        if role not in ("user", "assistant") or not content:
            return
        self.history.append({"role": role, "content": content})
        while sum(len(item["content"]) for item in self.history) > MAX_HISTORY_CHARS:
            self.history.popleft()
        self._save_history()

    def recent_history(self) -> list[dict[str, str]]:
        return [dict(message) for message in self.history]

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
        return f"agent:main:live-conversation-{slug or 'task'}-{time.monotonic_ns()}"

    def register_agent_session(self, session_key: str, request: str) -> None:
        self.recent_agent_sessions = deque(
            (item for item in self.recent_agent_sessions if item.get("session_key") != session_key),
            maxlen=12,
        )
        self.recent_agent_sessions.append({
            "session_key": session_key,
            "request": re.sub(r"\s+", " ", request).strip(),
            "state": "running",
            "started_at": int(time.time()),
        })
        self.sessions_updated_at = 0.0
        self._save_settings()

    def update_agent_session(self, session_key: str, state: str) -> None:
        for item in self.recent_agent_sessions:
            if item.get("session_key") == session_key:
                item["state"] = state
                break
        self.sessions_updated_at = 0.0
        self._save_settings()

    def latest_agent_session(self) -> dict[str, Any] | None:
        return dict(self.recent_agent_sessions[-1]) if self.recent_agent_sessions else None

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
            size = len(message["content"])
            if selected and (
                len(selected) >= PROMPT_HISTORY_MESSAGES
                or used_chars + size > PROMPT_HISTORY_CHARS
            ):
                break
            selected.appendleft(dict(message))
            used_chars += size
        return list(selected)

    def contextualize_agent_request(self, transcript: str) -> str:
        """Join a continuation to the immediately preceding unfinished agent request."""
        previous_user = next(
            (item["content"] for item in reversed(self.history) if item["role"] == "user"),
            "",
        )
        unfinished_agent_request = re.search(
            r"\b(?:start|have|ask|tell)\b.*\b(?:agent|sub-?agent)\b.*(?:\bthat|\bto|\bfor|\bso|\.{2,})\s*$",
            previous_user,
            flags=re.IGNORECASE,
        )
        if unfinished_agent_request and len(previous_user) <= 240:
            return f"{previous_user.rstrip(' .')} {transcript.lstrip()}"
        return transcript

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
        await self.tts.start()
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
            "options": {"temperature": 0, "num_predict": 3, "num_ctx": 512},
        }
        try:
            timeout = aiohttp.ClientTimeout(total=60)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(SPEECH_MODEL_URL, json=payload) as response:
                    response.raise_for_status()
                    await response.read()
            LOGGER.info("speech_model_warm model=%s", SPEECH_MODEL)
        except Exception as error:
            LOGGER.warning("speech_model_warm_failed error=%s", error)

    async def poll_sessions(self) -> None:
        while True:
            await asyncio.sleep(SESSION_POLL_SECONDS)
            try:
                await self.refresh_sessions()
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

    async def transcribe(self, audio: bytes, purpose: str = "final") -> str:
        if not self.stt:
            raise RuntimeError("Speech recognizer is not ready")

        def run_to_completion() -> str:
            assert self.stt and self.stt._model
            audio_float = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0
            segments, _ = self.stt._model.transcribe(
                audio_float,
                language="en",
                beam_size=3,
                best_of=3,
                temperature=0.0,
                condition_on_previous_text=False,
                without_timestamps=True,
                no_speech_threshold=0.6,
                vad_filter=True,
                vad_parameters={
                    "threshold": 0.5 if purpose == "wake" else 0.6,
                    "min_speech_duration_ms": 250,
                    "min_silence_duration_ms": 300,
                    "speech_pad_ms": 200,
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
                    "limit": 20,
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
            LOGGER.warning("session_refresh_failed error=%s", error)
        return self.sessions

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
        self, text: str, agent_pending: bool = False
    ) -> tuple[str, str, str | None]:
        direct_reply = direct_voice_surface_reply(text)
        if direct_reply:
            return "direct", direct_reply, None
        if is_explicit_new_agent_request(text):
            return "new_agent", "I'll start a separate agent for that.", None
        status_question = is_gateway_status_question(text)
        if time.monotonic() - self.capabilities_updated_at >= CAPABILITY_CACHE_SECONDS:
            if not self.capability_refresh_task or self.capability_refresh_task.done():
                self.capability_refresh_task = asyncio.create_task(self.refresh_capabilities())
        if status_question:
            await self.refresh_sessions(force=True)
        elif time.monotonic() - self.sessions_updated_at >= SESSION_CACHE_SECONDS:
            if not self.session_refresh_task or self.session_refresh_task.done():
                self.session_refresh_task = asyncio.create_task(self.refresh_sessions())
            if not self.sessions:
                try:
                    await asyncio.wait_for(asyncio.shield(self.session_refresh_task), timeout=2.5)
                except asyncio.TimeoutError:
                    pass
        if status_question:
            if is_referential_agent_question(text):
                tracked = self.latest_agent_session()
                if tracked:
                    key = str(tracked.get("session_key") or "")
                    return "direct", summarize_tracked_agent(
                        tracked, self.session_records.get(key)
                    ), None
            return "direct", summarize_gateway_status(self.sessions, agent_pending), None
        if is_referential_agent_question(text) and has_explicit_action_request(text):
            tracked = self.latest_agent_session()
            if tracked:
                return "session", "I'll add that to the same agent.", str(tracked["session_key"])
        capabilities = self.capabilities
        payload = {
            "model": SPEECH_MODEL,
            "keep_alive": SPEECH_MODEL_KEEP_ALIVE,
            "stream": False,
            "think": False,
            "format": SPEECH_OUTPUT_SCHEMA,
            "messages": [
                {"role": "system", "content": speech_model_prompt(
                    agent_pending=agent_pending,
                    capabilities=capabilities,
                    sessions=self.sessions,
                    voice_sessions=self.voice_session_summary(),
                    confirmation_required=self.confirmation_required,
                )},
                *self.prompt_history(),
                {"role": "user", "content": text},
            ],
            "options": {"temperature": 0, "num_predict": 80, "num_ctx": 16_384},
        }
        timeout = aiohttp.ClientTimeout(total=15)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(SPEECH_MODEL_URL, json=payload) as response:
                    response.raise_for_status()
                    result = await response.json()
            LOGGER.info(
                "speech_supervisor_complete prompt_tokens=%s output_tokens=%s "
                "load_ms=%.0f prompt_ms=%.0f generation_ms=%.0f",
                result.get("prompt_eval_count", "unknown"),
                result.get("eval_count", "unknown"),
                result.get("load_duration", 0) / 1_000_000,
                result.get("prompt_eval_duration", 0) / 1_000_000,
                result.get("eval_duration", 0) / 1_000_000,
            )
            route, reply, target_session = parse_speech_model_output(
                result.get("message", {}).get("content", "")
            )
            reply = repair_known_transcription_errors(reply)
            if has_stale_identity_confusion(reply):
                LOGGER.warning("speech_supervisor_repaired_false_identity")
                route, reply, target_session = (
                    "direct",
                    "I'm Jarvis, the model operating Live Conversation. "
                    "I answer here directly and use gateway agents when tool-backed work is needed.",
                    None,
                )
            explicit_action = has_explicit_action_request(text)
            if route in {"agent", "new_agent", "session"} and not explicit_action:
                LOGGER.warning("speech_supervisor_blocked_unrequested_action route=%s", route)
                route, reply, target_session = "direct", "I understand.", None
            elif route == "direct" and is_operational_acknowledgment(reply):
                if explicit_action:
                    LOGGER.warning("speech_supervisor_repaired_missing_agent_route")
                    route, target_session = "agent", None
                else:
                    LOGGER.warning("speech_supervisor_removed_invented_action")
                    reply = remove_unrequested_action_promises(reply)
            if route == "session" and target_session not in self.session_keys:
                LOGGER.warning("speech_supervisor_invalid_session target=%s", target_session)
                route, target_session = "agent", None
            if route != "ignore" and not reply:
                raise ValueError("speech model returned no speakable text")
            return route, reply, target_session
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
        generation = self.speech_generation
        await socket.send_json({
            "type": message_type,
            "text": text,
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
                return 0
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
            prefill_bytes = round(
                OUTPUT_SAMPLE_RATE * 2 * PLAYBACK_PREFILL_SECONDS
            )
            total_pcm_bytes = 0
            interrupted = False
            for index, _ in enumerate(speech_parts):
                next_synthesis: asyncio.Task[bytes] | None = None
                if index + 1 < len(speech_parts):
                    next_synthesis = asyncio.create_task(self.tts.synthesize(speech_parts[index + 1]))
                for offset in range(0, len(pcm), chunk_bytes):
                    if generation != self.speech_generation:
                        interrupted = True
                        break
                    chunk = pcm[offset:offset + chunk_bytes]
                    await socket.send_json({
                        "type": "response.output_audio.delta",
                        "responseId": response_id,
                        "sampleRate": OUTPUT_SAMPLE_RATE,
                        "audioBase64": base64.b64encode(chunk).decode("ascii"),
                    })
                    total_pcm_bytes += len(chunk)
                    # Keep a small PCM lead ahead of Android's AudioTrack. Sending
                    # exactly one 100 ms delta every 100 ms left no jitter margin,
                    # so ordinary WebView/network scheduling pauses sounded like
                    # the reply was being cut off every few seconds.
                    if total_pcm_bytes > prefill_bytes:
                        await asyncio.sleep(len(chunk) / (OUTPUT_SAMPLE_RATE * 2))
                if interrupted:
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
        self, socket: web.WebSocketResponse, audio: bytes, agent_pending: bool = False
    ) -> tuple[str, str, str | None] | None:
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
            if is_silent_stop_command(transcript):
                self.interrupt_speech()
                await socket.send_json({
                    "type": "state", "state": "listening", "detail": "Silent stop command."
                })
                return None

            stage = time.perf_counter()
            handoff_request: str | None = None
            policy_command = confirmation_policy_command(transcript)
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
                route, reply, target_session = await self.speech_reply(
                    transcript, agent_pending=agent_pending
                )
                agent_request = self.contextualize_agent_request(transcript)
                if route in ("agent", "new_agent", "session"):
                    if route == "new_agent" and target_session is None:
                        target_session = self.allocate_agent_session_key(agent_request)
                    if self.confirmation_required:
                        self.pending_confirmation = (agent_request, target_session, route)
                        summary = re.sub(r"\s+", " ", transcript).strip()[:180].rstrip(" .?!")
                        route = "confirmation"
                        target_session = None
                        reply = f"Before I take that action, should I proceed with: {summary}?"
                    else:
                        handoff_request = agent_request
            metrics.routing_ms = round((time.perf_counter() - stage) * 1000)
            metrics.route = route

            if route == "ignore":
                await socket.send_json({"type": "state", "state": "listening", "detail": "Ignored likely background speech."})
                return None

            self.remember("user", transcript)
            self.remember("assistant", reply)

            response_id = f"response-{time.monotonic_ns()}"
            metrics.tts_ms = await self.send_spoken_response(
                socket, reply, metrics.route, response_id
            )
            metrics.total_ms = round((time.perf_counter() - started) * 1000)
            await socket.send_json({"type": "metrics", **asdict(metrics)})
            await socket.send_json({"type": "state", "state": "listening"})
            if handoff_request is not None:
                if route == "agent":
                    target_session = self.session_key
                repaired_request = repair_known_transcription_errors(handoff_request)
                if repaired_request != handoff_request:
                    LOGGER.info(
                        "transcript_repaired original=%r repaired=%r",
                        handoff_request, repaired_request,
                    )
                return repaired_request, reply, target_session
        except Exception as error:
            await socket.send_json({"type": "error", "message": str(error)})
        return None

def render_page() -> str:
    return """<!doctype html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Live Conversation</title><style>
html,body{margin:0;min-height:100%;background:#060a10;color:#f4f8fc;font-family:system-ui,sans-serif}main{padding:18px 14px 28px;max-width:760px;margin:auto}h1{font-size:23px;margin:0 0 5px}.sub{color:#9aa9b8;margin:0 0 7px}.setting{color:#7fcbb4;font-size:12px;margin-bottom:16px}.state{font-size:18px;color:#54e0b4;margin:12px 0}.meter{height:14px;background:#101820;border:1px solid #304050;border-radius:8px;overflow:hidden}.meter div{height:100%;width:0;background:#00ab7e;transition:width 60ms}.buttons{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin:14px 0}button{min-height:48px;border:1px solid #00ab7e;border-radius:8px;background:#1e2630;color:#fff;font-size:15px}.card{background:#0a0e14;border-radius:8px;padding:12px;margin-top:10px;min-height:48px}.label{color:#8797a8;font-size:11px;text-transform:uppercase;letter-spacing:.08em}.metrics{font-size:12px;color:#aebdca;margin-top:12px}.route{display:inline-block;border:1px solid #36556a;border-radius:10px;padding:2px 7px;font-size:11px;margin-left:6px}.history{margin-top:18px}.history-list{display:flex;flex-direction:column;gap:8px;margin-top:8px}.history-empty{color:#718294;font-size:13px}.message{max-width:88%;padding:9px 11px;border-radius:12px;line-height:1.35;white-space:pre-wrap;overflow-wrap:anywhere}.message.user{align-self:flex-end;background:#0e5948}.message.assistant{align-self:flex-start;background:#182431}.message-role{font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:#9db0bf;margin-bottom:3px}
</style></head><body><main><h1>Live Conversation</h1><p class="sub">Accurate local speech recognition. Simple requests answer here; complex work routes to the right OpenClaw session.</p><div id="confirmation" class="setting">Action confirmation: loading…</div><div id="state" class="state">Ready</div><div class="meter"><div id="bar"></div></div><div class="buttons"><button id="start">Start conversation</button><button id="stop">Stop</button></div><div class="card"><div class="label">You</div><div id="user">—</div></div><div class="card"><div class="label">Assistant <span id="route" class="route">waiting</span></div><div id="assistant">—</div></div><div id="metrics" class="metrics"></div><section class="history"><div class="label">Conversation history · last 80 messages</div><div id="history" class="history-list"><div class="history-empty">No conversation history yet.</div></div></section></main><script>
const state=document.getElementById('state'),bar=document.getElementById('bar'),user=document.getElementById('user'),assistant=document.getElementById('assistant'),route=document.getElementById('route'),metrics=document.getElementById('metrics'),confirmation=document.getElementById('confirmation'),historyList=document.getElementById('history');let ws,timer,recording=false,awaitingResponse=false,responseActive=false,responseTailTimer=null,speechMs=0,silenceMs=0,candidateSpeechMs=0,pre=[],historyMessages=[],pendingTranscript='';
function renderHistory(){historyList.replaceChildren();if(!historyMessages.length){const empty=document.createElement('div');empty.className='history-empty';empty.textContent='No conversation history yet.';historyList.appendChild(empty);return}for(const message of historyMessages.slice(-80)){const bubble=document.createElement('div');bubble.className='message '+message.role;const who=document.createElement('div');who.className='message-role';who.textContent=message.role==='user'?'You':'Assistant';const content=document.createElement('div');content.textContent=message.content;bubble.append(who,content);historyList.appendChild(bubble)}historyList.lastElementChild?.scrollIntoView({block:'nearest'})}
function addHistory(role,content){if(!content)return;historyMessages.push({role,content});historyMessages=historyMessages.slice(-80);renderHistory()}
function rms(b64){const s=atob(b64||'');let sum=0,n=0;for(let i=0;i+1<s.length;i+=2){let v=(s.charCodeAt(i)&255)|((s.charCodeAt(i+1)&255)<<8);if(v&32768)v-=65536;const f=v/32768;sum+=f*f;n++}return n?Math.sqrt(sum/n):0}
function send(x){if(ws&&ws.readyState===1)ws.send(JSON.stringify(x))}
function report(event,detail){send({type:'client_event',event:event,detail:String(detail||'')})}
function begin(){if(recording||awaitingResponse)return;recording=true;candidateSpeechMs=0;speechMs=0;silenceMs=0;if(responseActive){responseActive=false;if(responseTailTimer){clearTimeout(responseTailTimer);responseTailTimer=null}try{OpenClawNativeAudio.interruptAgentResponsePlayback()}catch(e){}report('barge_in','confirmed_user_speech')}send({type:'input_audio_buffer.speech_started'});send({type:'start'});for(const audioBase64 of pre)send({type:'audio',audioBase64});pre=[];state.textContent='Listening…'}
function tick(){let chunk='';try{chunk=OpenClawNativeAudio.readChunkBase64()||''}catch(e){state.textContent='Microphone error';return}if(!chunk)return;const level=rms(chunk);bar.style.width=Math.min(100,Math.round(level*850))+'%';if(!recording){pre.push(chunk);while(pre.length>20)pre.shift();if(awaitingResponse){candidateSpeechMs=0;return}const speechThreshold=responseActive?.025:.006;const speechRequiredMs=200;candidateSpeechMs=level>=speechThreshold?candidateSpeechMs+20:0;if(candidateSpeechMs>=speechRequiredMs)begin();return}send({type:'audio',audioBase64:chunk});if(level>=.006){speechMs+=20;silenceMs=0}else silenceMs+=20;if(speechMs>=200&&silenceMs>=600){recording=false;awaitingResponse=true;candidateSpeechMs=0;try{OpenClawNativeAudio.prepareAgentResponsePlayback()}catch(e){}send({type:'commit'});state.textContent='Transcribing…'}}
function start(){if(ws&&ws.readyState===1)return;ws=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/ws');let audioChunks=0,audioChars=0;ws.onopen=()=>{OpenClawNativeAudio.startCapture(16000,20);timer=setInterval(tick,20);state.textContent='Listening…'};ws.onmessage=e=>{const m=JSON.parse(e.data);if(m.type==='history'){historyMessages=Array.isArray(m.messages)?m.messages.slice(-80):[];renderHistory()}if(m.type==='settings')confirmation.textContent='Action confirmation: '+(m.action_confirmation==='confirm'?'On — asks before actions':'Off — explicit requests proceed');if(m.type==='state'){state.textContent=m.state[0].toUpperCase()+m.state.slice(1)+'…';if(m.state==='listening'){awaitingResponse=false;recording=false;candidateSpeechMs=0}if((m.detail||'').startsWith('Ignored')||(m.detail||'').startsWith('Silent stop'))pendingTranscript=''}if(m.type==='partial_transcript'){user.textContent=m.text;pendingTranscript=m.text}if(m.type==='transcript'){user.textContent=m.text;pendingTranscript=m.text}if(m.type==='reply'){assistant.textContent=m.text;route.textContent=m.route;if(pendingTranscript){addHistory('user',pendingTranscript);pendingTranscript=''}addHistory('assistant',m.text);report('reply',{route:m.route})}if(m.type==='output_audio_buffer.started'){if(responseTailTimer){clearTimeout(responseTailTimer);responseTailTimer=null}awaitingResponse=false;responseActive=true;candidateSpeechMs=0;pre=[];audioChunks=0;audioChars=0;try{OpenClawNativeAudio.prepareAgentResponsePlayback();report('playback_prepared',m.responseId)}catch(err){report('playback_prepare_error',err)}}if(m.type==='response.output_audio.delta'){audioChunks++;audioChars+=(m.audioBase64||'').length;try{OpenClawNativeAudio.playAgentResponsePcm16Base64(m.audioBase64,m.sampleRate);if(audioChunks===1)report('first_pcm_enqueued','rate='+m.sampleRate+' chars='+(m.audioBase64||'').length)}catch(err){report('pcm_enqueue_error',err)}}if(m.type==='response.output_audio.done'){candidateSpeechMs=0;pre=[];if(responseTailTimer)clearTimeout(responseTailTimer);responseTailTimer=setTimeout(()=>{responseActive=false;responseTailTimer=null},1500);report('pcm_delivery_done','chunks='+audioChunks+' chars='+audioChars)}if(m.type==='metrics')metrics.textContent=`ASR ${m.asr_ms} ms · Agent ${m.response_ms} ms · TTS ${m.tts_ms} ms · Ready ${m.total_ms} ms`;if(m.type==='error'){awaitingResponse=false;responseActive=false;pendingTranscript='';state.textContent='Error';assistant.textContent=m.message;report('server_error',m.message)}};ws.onerror=()=>state.textContent='Connection error';ws.onclose=()=>state.textContent='Stopped'}
function stop(){if(timer)clearInterval(timer);timer=null;if(responseTailTimer)clearTimeout(responseTailTimer);responseTailTimer=null;try{OpenClawNativeAudio.stopCapture()}catch(e){}if(ws)ws.close();ws=null;recording=false;awaitingResponse=false;responseActive=false;candidateSpeechMs=0;bar.style.width='0%';state.textContent='Stopped'}
document.getElementById('start').onclick=start;document.getElementById('stop').onclick=()=>{stop();try{OpenClawNativeApp.liveConversationStopped()}catch(e){}};window.addEventListener('pagehide',stop);if(new URLSearchParams(location.search).get('autostart')==='1')start();
</script></body></html>"""


async def index(_: web.Request) -> web.Response:
    return web.Response(text=render_page(), content_type="text/html", headers={"Cache-Control": "no-store"})


async def health(request: web.Request) -> web.Response:
    service: LiveConversationService = request.app["service"]
    return web.json_response({
        "ok": service.stt is not None,
        "pipecat": "1.8.1",
        "stt": service.stt_description,
        "history_messages": len(service.history),
        "action_confirmation": (
            "confirm" if service.confirmation_required else "automatic"
        ),
    })


async def wake_websocket(request: web.Request) -> web.WebSocketResponse:
    """Detect the configured wake word from rolling raw 16 kHz PCM windows."""
    service: LiveConversationService = request.app["service"]
    socket = web.WebSocketResponse(heartbeat=20)
    await socket.prepare(request)
    audio = bytearray()
    window_bytes = round(SAMPLE_RATE * 2 * WAKE_WINDOW_SECONDS)
    retain_bytes = SAMPLE_RATE * 2
    last_trigger = 0.0
    async for message in socket:
        if message.type != WSMsgType.BINARY:
            continue
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
    return socket


async def websocket(request: web.Request) -> web.WebSocketResponse:
    service: LiveConversationService = request.app["service"]
    socket = web.WebSocketResponse(heartbeat=20)
    await socket.prepare(request)
    await socket.send_json({"type": "history", "messages": service.recent_history()})
    await socket.send_json(service.settings_payload())
    while service.pending_spoken_replies:
        reply = service.pending_spoken_replies.popleft()
        await service.send_spoken_response(
            socket, reply, "agent", f"agent-reconnect-{time.monotonic_ns()}"
        )
    audio = bytearray()
    turn_task: asyncio.Task[None] | None = None
    partial_task: asyncio.Task[None] | None = None
    silent_stop_detected = False
    turn_sequence = 0
    next_partial_bytes = round(SAMPLE_RATE * 2 * PARTIAL_TRANSCRIPT_INTERVAL_SECONDS)
    agent_tasks: set[asyncio.Task[None]] = set()

    async def run_partial(snapshot: bytes, sequence: int) -> None:
        nonlocal silent_stop_detected
        try:
            transcript = await service.transcribe(snapshot, purpose="partial")
            if transcript and sequence == turn_sequence and is_silent_stop_command(transcript):
                silent_stop_detected = True
                service.interrupt_speech()
                await socket.send_json({"type": "output_audio_buffer.cleared"})
            elif transcript and sequence == turn_sequence and not socket.closed:
                await socket.send_json({"type": "partial_transcript", "text": transcript})
        except Exception as error:
            LOGGER.warning("partial_transcription_failed error=%s", error)

    async def run_agent(transcript: str, target_session: str | None) -> None:
        if target_session is None:
            target_session = service.allocate_agent_session_key(transcript)
        service.register_agent_session(target_session, transcript)
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
            service.update_agent_session(target_session, "complete")
            await service.refresh_sessions(force=True)
            service.remember("assistant", reply)
            if socket.closed:
                service.queue_pending_reply(reply)
                return
            await service.send_spoken_response(socket, reply, "agent", f"agent-{time.monotonic_ns()}")
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
                await service.send_spoken_response(
                    socket, message, "agent", f"agent-error-{time.monotonic_ns()}"
                )
                await socket.send_json({
                    "type": "agent_status", "state": "error", "sessionKey": target_session
                })

    async def run_turn(turn: bytes) -> None:
        handoff = await service.process_turn(
            socket, turn, agent_pending=bool(agent_tasks)
        )
        if handoff:
            transcript, _, target_session = handoff
            task = asyncio.create_task(run_agent(transcript, target_session))
            agent_tasks.add(task)
            task.add_done_callback(agent_tasks.discard)
    async for message in socket:
        if message.type != WSMsgType.TEXT:
            continue
        payload = json.loads(message.data)
        kind = payload.get("type")
        if kind == "input_audio_buffer.speech_started":
            service.interrupt_speech()
            await socket.send_json({"type": "output_audio_buffer.cleared"})
        elif kind == "start":
            turn_sequence += 1
            silent_stop_detected = False
            audio.clear()
            next_partial_bytes = round(
                SAMPLE_RATE * 2 * PARTIAL_TRANSCRIPT_INTERVAL_SECONDS
            )
        elif kind == "audio":
            audio.extend(base64.b64decode(payload.get("audioBase64", ""), validate=True))
            if (
                len(audio) >= next_partial_bytes
                and (not partial_task or partial_task.done())
            ):
                snapshot = bytes(audio)
                sequence = turn_sequence
                next_partial_bytes = len(snapshot) + round(
                    SAMPLE_RATE * 2 * PARTIAL_TRANSCRIPT_INTERVAL_SECONDS
                )
                partial_task = asyncio.create_task(run_partial(snapshot, sequence))
        elif kind == "commit" and audio:
            turn_sequence += 1
            if silent_stop_detected:
                audio.clear()
                silent_stop_detected = False
                await socket.send_json({
                    "type": "state", "state": "listening", "detail": "Silent stop command."
                })
                continue
            if turn_task and not turn_task.done():
                turn_task.cancel()
            turn = bytes(audio)
            audio.clear()
            turn_task = asyncio.create_task(run_turn(turn))
        elif kind == "client_event":
            LOGGER.info("client_event event=%s detail=%s", payload.get("event", ""), payload.get("detail", ""))
    if turn_task and not turn_task.done():
        turn_task.cancel()
    # Agent tasks deliberately survive a WebView disconnect. Their final reply
    # is persisted and queued for speech when Live Conversation reconnects.
    return socket


async def build_app(args: argparse.Namespace) -> web.Application:
    service = LiveConversationService(
        args.session_key, args.tts_speed, args.history_path, args.settings_path
    )
    await service.start()
    app = web.Application()
    app["service"] = service
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/wake", wake_websocket)
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
    parser.add_argument("--history-path", default=DEFAULT_HISTORY_PATH)
    parser.add_argument("--settings-path", default=DEFAULT_SETTINGS_PATH)
    parser.add_argument("--tts-speed", type=float, default=1.15)
    args = parser.parse_args()
    web.run_app(build_app(args), host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
