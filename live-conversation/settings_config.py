"""Browser-safe settings contract, independent of the audio/model dependencies.

The server owns persistence: merge ``configuration`` into the existing settings
file using its secure atomic writer. Validate the complete merged configuration
before writing. Deployment paths, URLs, executables, and credentials are never
editable here. Defaults are factory defaults; pass effective CLI/environment
values as ``current`` so deployment overrides remain intact.
"""
from __future__ import annotations

from copy import deepcopy
import math
import re
from typing import Any

SETTINGS_SCHEMA: list[dict[str, Any]] = []


def _setting(key, label, group, default, minimum=None, maximum=None, *, options=None,
             description='', apply='restart', kind=None):
    kind = kind or ('boolean' if type(default) is bool else
                    'integer' if type(default) is int else
                    'number' if type(default) is float else 'string')
    SETTINGS_SCHEMA.append(dict(key=key, label=label, group=group, type=kind,
                               default=default, min=minimum, max=maximum,
                               options=options, description=description or label,
                               apply=apply))


_setting('action_confirmation', 'Confirm agent actions', 'General', 'automatic',
         options=['automatic', 'confirm'], apply='immediate')
_setting('audio_capture', 'Save test audio locally', 'Diagnostics', False, apply='immediate',
         description='Record microphone turns locally for debugging; retention limits apply.')

# These names map directly to the existing module constants with key.upper().
GLOBAL_KEYS = []

def _global(key, label, group, default, minimum=None, maximum=None, **kwargs):
    GLOBAL_KEYS.append(key)
    _setting(key, label, group, default, minimum, maximum, **kwargs)

_global('speech_model', 'Primary speech model', 'Model', 'openclaw-live-conversation:4b', 1, 200)
_global('speech_model_keep_alive', 'Model keep-alive (seconds; -1 keeps loaded)', 'Model', -1, -1, 86400)
_global('speech_model_context', 'Primary context tokens', 'Model', 8192, 1024, 131072)
_global('speech_num_predict', 'Primary response token budget', 'Model', 384, 32, 8192)
_global('speech_retry_num_predict', 'Retry token budget', 'Model', 640, 32, 8192)
_global('degraded_speech_model', 'Fallback speech model', 'Model', 'qwen3.5:0.8b', 1, 200)
_global('degraded_speech_context', 'Fallback context tokens', 'Model', 4096, 1024, 131072)
_global('degraded_speech_timeout_seconds', 'Fallback timeout (seconds)', 'Model', 8, 1, 120)
_global('min_speech_model_vram_fraction', 'Minimum model GPU residency', 'Model', .85, 0.0, 1.0)
_global('partial_transcript_interval_seconds', 'Partial transcript interval (seconds)', 'ASR', 1.0, .1, 10.0)
_global('online_asr_window_seconds', 'Online recognition window (seconds)', 'ASR', 14.0, 2.0, 60.0)
_global('online_asr_overlap_seconds', 'Online recognition overlap (seconds)', 'ASR', 2.0, 0.0, 20.0)
_global('playback_prefill_seconds', 'Playback prefill (seconds)', 'Speech output', .3, 0.0, 3.0)
_global('max_history_messages', 'Stored history message limit', 'History', 120, 8, 2000)
_global('max_history_chars', 'Stored history character limit', 'History', 72000, 4000, 2000000)
_global('max_conversation_events', 'Conversation event limit', 'History', 500, 20, 10000)
_global('prompt_history_messages', 'Model history message limit', 'History', 8, 1, 100)
_global('prompt_history_chars', 'Model history character limit', 'History', 4000, 256, 100000)
_global('capability_cache_seconds', 'Capability cache (seconds)', 'Runtime', 60, 1, 3600)
_global('session_cache_seconds', 'Session cache (seconds)', 'Runtime', 3, 1, 600)
_global('session_poll_seconds', 'Session polling interval (seconds)', 'Runtime', 10, 1, 600)
_global('agent_result_poll_seconds', 'Agent result polling (seconds)', 'Runtime', 3.0, .5, 60.0)
_global('agent_result_wait_seconds', 'Agent result wait (seconds)', 'Runtime', 600.0, 10.0, 3600.0)
_global('max_pending_turns', 'Queued turn limit', 'Runtime', 4, 1, 32)
_global('max_live_connections', 'Live connection limit', 'Runtime', 4, 1, 32)
_global('max_wake_connections', 'Wake connection limit', 'Runtime', 2, 1, 16)
_global('wake_window_seconds', 'Wake recognition window (seconds)', 'Microphone', 2.5, .5, 10.0)
_global('wake_cooldown_seconds', 'Wake cooldown (seconds)', 'Microphone', 5.0, .5, 60.0)
_setting('tts_speaker_id', 'Kokoro speaker ID', 'Speech output', 9, 0, 10)
_setting('tts_threads', 'Kokoro worker threads', 'Speech output', 8, 1, 32)
_global('default_qwen_first_utterance_chars', 'Qwen first utterance characters', 'Speech output', 320, 40, 2000)
_global('default_qwen_followup_utterance_chars', 'Qwen follow-up utterance characters', 'Speech output', 480, 40, 4000)

_setting('tts_backend', 'Speech engine', 'Speech output', 'kokoro', options=['kokoro', 'qwen'])
_setting('tts_speed', 'Kokoro speaking speed', 'Speech output', 1.08, .5, 2.0)
_setting('qwen_tts_model', 'Qwen speech model', 'Speech output', 'Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice', 1, 200)
_setting('qwen_tts_voice', 'Qwen voice', 'Speech output', 'ryan', 1, 100)
_setting('qwen_tts_instructions', 'Qwen voice direction', 'Speech output',
         'Speak in a natural, relaxed conversational voice at a moderately brisk pace and normal indoor volume. Use ordinary sentence rhythm and neutral emphasis.', 0, 2000)
_setting('qwen_tts_initial_chunk_frames', 'Qwen initial chunk frames', 'Speech output', 10, 1, 100)
_setting('qwen_tts_startup_wait_seconds', 'Qwen startup wait (seconds)', 'Speech output', 0.0, 0.0, 600.0)
_setting('asr_model', 'Whisper model', 'ASR', 'small.en', options=['tiny.en', 'base.en', 'small.en', 'medium.en', 'tiny', 'base', 'small', 'medium', 'large-v3', 'turbo'])
_setting('asr_device', 'Recognition device', 'ASR', 'cuda', options=['cuda', 'cpu'])
_setting('asr_compute_type', 'Recognition precision', 'ASR', 'float16', options=['float16', 'int8', 'float32', 'int8_float16'])
_setting('asr_language', 'Recognition language', 'ASR', 'en', options=['en'], description='The current conversational pipeline supports English.')
_setting('asr_no_speech_prob', 'No-speech probability threshold', 'ASR', .6, 0.0, 1.0)
_setting('asr_beam_size', 'Recognition beam size', 'ASR', 5, 1, 20)
_setting('asr_best_of', 'Recognition candidates', 'ASR', 5, 1, 20)
_setting('asr_temperature', 'Recognition temperature', 'ASR', 0.0, 0.0, 1.0)
_setting('asr_vad_filter', 'Filter non-speech audio', 'ASR', True)
for purpose, value in [('final', .35), ('wake', .5), ('partial', .6)]:
    _setting('asr_vad_' + purpose + '_threshold', purpose.title() + ' speech detection threshold', 'ASR', value, 0.0, 1.0)
_setting('asr_min_speech_duration_ms', 'Minimum detected speech (ms)', 'ASR', 250, 0, 2000)
_setting('asr_min_silence_duration_ms', 'Minimum detected silence (ms)', 'ASR', 300, 0, 3000)
_setting('asr_speech_pad_final_ms', 'Final speech padding (ms)', 'ASR', 300, 0, 2000)
_setting('asr_speech_pad_partial_ms', 'Partial speech padding (ms)', 'ASR', 200, 0, 2000)
_setting('asr_hotwords', 'Recognition vocabulary hints', 'ASR',
         'John, Jarvis, OpenClaw, Live Conversation, live agent, subagent, gateway, sessions', 0, 2000)
for key, label, default, minimum, maximum in [
    ('recording_retention_days', 'Recording retention (days)', 30, 1, 365),
    ('debug_retention_days', 'Debug retention (days)', 14, 1, 365),
    ('recording_max_bytes', 'Recording storage quota (bytes)', 2147483648, 1048576, 107374182400),
    ('debug_max_bytes', 'Debug storage quota (bytes)', 536870912, 1048576, 107374182400),
]:
    _setting(key, label, 'Diagnostics', default, minimum, maximum)
for key, label, default, minimum, maximum in [
    ('speech_threshold', 'Microphone speech threshold', .006, .001, .2),
    ('barge_in_threshold', 'Interruption speech threshold', .025, .001, .5),
    ('start_confirm_ms', 'Speech onset confirmation (ms)', 300, 40, 2000),
    ('barge_in_confirm_ms', 'Interruption confirmation (ms)', 300, 40, 2000),
    ('onset_preroll_ms', 'Speech onset pre-roll (ms)', 2500, 0, 5000),
    ('max_drain_frames', 'Microphone frames per tick', 24, 1, 100),
    ('semantic_check_ms', 'First semantic endpoint check (ms)', 750, 100, 5000),
    ('hard_endpoint_ms', 'Maximum endpoint silence (ms)', 3500, 300, 10000),
    ('min_speech_ms', 'Minimum utterance speech (ms)', 200, 40, 2000),
    ('endpoint_retry_ms', 'Semantic endpoint retry (ms)', 400, 100, 2000),
    ('response_tail_ms', 'Playback tail protection (ms)', 1500, 0, 5000),
]:
    _setting('browser_' + key, label, 'Microphone', default, minimum, maximum, apply='reconnect')

_global('max_http_body_bytes', 'HTTP request limit (bytes)', 'Runtime', 65536, 16384, 1048576)
_global('max_ws_message_bytes', 'WebSocket message limit (bytes)', 'Runtime', 524288, 16384, 4194304)
_global('max_turn_audio_bytes', 'Maximum turn audio (bytes)', 'Runtime', 19200000, 320000, 115200000)
_global('conversation_instructions', 'Conversation instructions', 'Model', '', 0, 8000,
        description='Additional directions for the local conversation model. Routing and structured-response requirements remain built in.')
_global('speech_temperature', 'Conversation temperature', 'Model', 0.0, 0.0, 2.0)
_global('speech_timeout_seconds', 'Conversation model timeout (seconds)', 'Model', 30.0, 1.0, 300.0)
_global('semantic_turn_threshold', 'Smart Turn completion threshold', 'Microphone', .5, 0.0, 1.0)
_global('semantic_confidence_threshold', 'Semantic completion confidence', 'Microphone', .7, 0.0, 1.0)
_global('semantic_timeout_seconds', 'Semantic check timeout (seconds)', 'Model', 4.0, .5, 60.0)
_global('semantic_num_predict', 'Semantic check token budget', 'Model', 32, 16, 512)
_global('fragment_timeout_seconds', 'Fragment resolution timeout (seconds)', 'Model', 8.0, 1.0, 120.0)
_global('fragment_num_predict', 'Fragment resolution token budget', 'Model', 160, 32, 2048)
_global('spoken_backchannels_enabled', 'Spoken listening acknowledgments', 'Speech output', False,
        description='Normally disabled to avoid interrupting speech with listening sounds.')
_global('backchannel_display_text', 'Listening acknowledgment text', 'Speech output', '', 0, 120)
_global('wake_word', 'Wake word', 'Microphone', 'jarvis', 1, 40,
        description='Host recognition word. Android Assistant role and wake-listening switch must also be enabled.')
_setting('wake_aliases', 'Wake word aliases', 'Microphone', 'jarvis,jervis', 1, 300,
         description='Comma-separated single-word spellings accepted by the wake recognizer.')

_SCHEMA_BY_KEY = {entry['key']: entry for entry in SETTINGS_SCHEMA}
_MODEL_KEYS = {'speech_model', 'degraded_speech_model', 'qwen_tts_model'}


def defaults() -> dict[str, Any]:
    """Return an independent copy of the unchanged factory defaults."""
    return {entry['key']: deepcopy(entry['default']) for entry in SETTINGS_SCHEMA}


def validate_settings(patch: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate a partial update and return a complete, independent configuration.

    Rejects coercion (including bool-as-int), unknown keys, non-finite floats,
    control characters and inconsistent dependent limits. Never mutates inputs.
    """
    if not isinstance(patch, dict) or (current is not None and not isinstance(current, dict)):
        raise ValueError('Settings must be a JSON object')
    values = defaults()
    for source in (current or {}, patch):
        unknown = set(source) - set(_SCHEMA_BY_KEY)
        if unknown:
            raise ValueError('Unknown settings: ' + ', '.join(sorted(map(str, unknown))))
        values.update(source)
    for key, value in values.items():
        entry = _SCHEMA_BY_KEY[key]
        kind = entry['type']
        valid_type = (type(value) is bool if kind == 'boolean' else
                      type(value) is int if kind == 'integer' else
                      type(value) in (int, float) if kind == 'number' else
                      type(value) is str)
        if not valid_type:
            raise ValueError(f'{key}: expected {kind}')
        if kind in ('integer', 'number'):
            if type(value) is float and not math.isfinite(value):
                raise ValueError(f'{key}: must be finite')
            size = value
        elif kind == 'string':
            if any(ord(c) < 32 and c not in '\n\t' or ord(c) == 127 for c in value):
                raise ValueError(f'{key}: control characters are not allowed')
            size = len(value)
            if key in _MODEL_KEYS and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]*', value):
                raise ValueError(f'{key}: invalid model identifier')
            if entry['min'] and not value.strip():
                raise ValueError(f'{key}: must not be blank')
        else:
            size = None
        if size is not None and ((entry['min'] is not None and size < entry['min']) or
                                 (entry['max'] is not None and size > entry['max'])):
            raise ValueError(f'{key}: must be between {entry["min"]} and {entry["max"]}')
        if entry['options'] is not None and value not in entry['options']:
            raise ValueError(f'{key}: unsupported option')
    for smaller, larger in [
        ('online_asr_overlap_seconds', 'online_asr_window_seconds'),
        ('speech_num_predict', 'speech_retry_num_predict'),
        ('speech_retry_num_predict', 'speech_model_context'),
        ('prompt_history_messages', 'max_history_messages'),
        ('prompt_history_chars', 'max_history_chars'),
        ('agent_result_poll_seconds', 'agent_result_wait_seconds'),
        ('browser_semantic_check_ms', 'browser_hard_endpoint_ms'),
        ('browser_speech_threshold', 'browser_barge_in_threshold'),
    ]:
        if values[smaller] > values[larger] or (
            smaller == 'online_asr_overlap_seconds' and values[smaller] == values[larger]
        ):
            raise ValueError(f'{smaller}: must be less than {larger}' if smaller == 'online_asr_overlap_seconds'
                             else f'{smaller}: must not exceed {larger}')
    if values['asr_device'] == 'cpu' and values['asr_compute_type'] in ('float16', 'int8_float16'):
        raise ValueError('asr_compute_type: CPU requires int8 or float32')
    if any(not word.strip().isalpha() for word in values['wake_aliases'].split(',')) or not values['wake_word'].isalpha():
        raise ValueError('Wake word and aliases must contain single alphabetic words')
    return deepcopy(values)
