# Pipecat Live Conversation

This is the dedicated low-latency voice surface opened by **Live Conversation**
in the Android Dashboard plus menu.

## Pipeline

1. The Android native audio bridge captures 16 kHz PCM in 20 ms frames.
2. Browser-side VAD requires 300 ms of near-field audio above a 0.012 RMS
   threshold before opening a turn. After 750 ms of silence, the server runs
   Pipecat Smart Turn v3.2 on the final eight seconds of raw audio. Its learned
   semantic/prosodic decision ends complete thoughts quickly and keeps
   unfinished thoughts open; a 3,500 ms hard-silence fallback prevents a bad
   prediction from leaving the microphone stuck without limiting utterance
   duration. This rejects the lower-level television/road speech that the former
   0.006 start gate treated as if it came from the person holding the phone. A
   2.5-second onset pre-roll is retained in addition to the 300 ms
   confirmation window, and queued native frames are drained in bursts so UI
   scheduling jitter cannot discard the start of a sentence, so quiet initial
   words and consonants still reach transcription after the
   stricter foreground-speech gate opens.
3. Pipecat's persistent `faster-whisper` `small.en` service transcribes locally
   with CUDA `float16` beam search on GPU 0 and falls back to CPU `int8` if CUDA
   initialization fails. Five-candidate beam search and a small vocabulary hint
   for Jarvis, OpenClaw, Live Conversation, agents, and sessions improve the
   recurring proper-name and system-term substitutions. The larger cached model
   improves recognition of terms such as “live agent,” session names, and
   longer technical requests. While a
   user is speaking, a LocalAgreement online layer decodes a bounded 14-second
   tail about once per second. It exposes an immutable stable prefix separately
   from the revisable suffix, carries stable context forward, and trims already
   confirmed audio rather than repeatedly decoding an unbounded recording from
   the beginning. Stable prefixes may trigger read-only session/capability
   prefetch; they can never authorize actions. Final decoding consumes
   Faster-Whisper's lazy segment generator entirely on a worker thread, so
   inference cannot wedge the WebSocket event loop. No transcription timeout or
   fixed turn-length cap truncates a user's input. Silero VAD filters non-speech
   audio before decoding, with a stricter threshold for normal turns than for
   the wake word, reducing false transcripts from road and ambient noise.
4. The local `qwen3.5:4b` speech supervisor is installed under the dedicated
   Ollama identity `openclaw-live-conversation:4b` and returns one constrained
   semantic decision containing completeness, speech act, conversational
   relation, actionability, grounding need, supersession, assembled meaning,
   route, and spoken reply. This model is the
   local latency/quality balance: materially stronger than the former 0.8B
   supervisor while remaining responsive once warm on the installed GPUs. Its
   fixed 8k context and dedicated identity prevent unrelated qwen3.5 callers
   with 16k or 32k contexts from repeatedly replacing and reloading its runner.
   A loopback-only companion Ollama process on port 11439 isolates the live
   runner from the shared system Ollama scheduler, which otherwise evicted it
   even while its 30-minute keep-alive was active.
   Only the newest 32 routing-relevant history messages are included, while up
   to 120 messages remain persisted and visible. The
   Ollama request keeps it warm for 30 minutes to avoid repeated cold starts
   during a conversation. The model is also warmed when the service starts.
   The decision/reply budget is 384 tokens rather than the former 80-token ceiling. If
   Ollama reports that the budget was exhausted (or returns exactly the capped
   token count), the request is retried once with 640 tokens instead of showing
   and speaking a syntactically valid JSON response whose `reply` ends midway
   through a sentence.
5. A bounded conversation history supplies the previous user and assistant
   turns to the speech supervisor. Behind that projection is a versioned event
   ledger with timestamps, turn IDs, assembled text, status, provenance,
   routing metadata, interruptions, tool receipts, and exact session identity.
   It is persisted at
   `~/.openclaw/state/live-conversation-history.json`, so context survives a
   WebView reconnect or service restart. Up to 120 messages and 72,000
   characters are retained; the newest context that fits the local model's
   prompt budget is selected dynamically. The page displays the same rolling
   120-message history as user and assistant bubbles.
6. The supervisor receives a compact, continuously background-refreshed summary of the
   last seven days of gateway sessions, including the exact session key,
   title, latest message, and authoritative `hasActiveRun` state. It can answer
   status questions deterministically without launching an agent or waiting for
   the local language model.
7. The service intercepts control tokens before display/TTS. Every newly
   delegated task gets an independent session; sessions are reused only for an
   explicit semantic follow-up to an exact
   active or recent gateway session. Concurrent agents are supported, and each
   final CLI result is returned to the bridge and spoken when it completes.
   Tool-only `sessions_yield` handoffs are recognized as delegated work rather
   than failed responses; Live Conversation watches that exact session for its
   eventual visible result. If the WebView disconnects while an agent works,
   the task continues and its final reply is queued for speech on the next Live
   Conversation connection.
8. Ollama's NDJSON stream is decoded incrementally. Once the same decision has
   marked the thought complete and a direct route and stable spoken clause are
   available, Kokoro starts synthesizing that clause
   while later model tokens are still arriving. New confirmed speech cancels
   the in-flight HTTP generation as well as pending synthesis and playback.
   Authorized tool work starts as soon as routing is final and runs concurrently
   with its acknowledgment instead of waiting for the entire utterance.
9. A persistent local Kokoro TTS worker using the British male George voice
   returns 24 kHz PCM to the Android
   native playback bridge. Replies are synthesized in short, look-ahead-buffered
   units so the first audio starts promptly while later speech is generated
   during playback. The server sends a 300 ms PCM prefill before settling into
   realtime pacing, giving Android's AudioTrack enough scheduling margin to
   avoid periodic underruns without adding perceptible startup delay. Confirmed
   user speech stops playback after about 200 ms, so
   new input cannot wait behind the remainder of an older reply. Spoken
   controls such as “Jarvis stop,” “stop talking,” “be quiet,” and “that's
   enough” stop playback without entering history, invoking a model, or
   producing a reply. Each synthesis request carries a restrained contextual
   cadence: brief questions/backchannels are slightly quicker, while warnings
   and apologies slow down. Smart Turn can emit one short, nonverbal affirmative
   hum only after ASR has established a stable intelligible prefix when a long
   utterance pauses but is semantically unfinished.
10. When OpenClaw Dashboard holds Android's Assistant role, its lightweight
   voice service streams rolling microphone windows to `/wake`. Detecting the
   standalone word “Jarvis” opens and auto-starts Live Conversation over the
   regular or lock screen. Turning the screen off stops an active conversation
   and resumes wake listening.

The speech-model prompt defines Jarvis as the model operating Live Conversation,
not as a separate supervisor outside it. Hearing and identity checks have
deterministic local answers, and stale false-identity replies are excluded from
the model's prompt history. The prompt also exposes available agents and skills,
distinguishes direct status answers from actual work, documents the exact
contracts for new-agent and existing-session handoffs, and resolves likely ASR
errors from context while asking for clarification when a proper noun remains
uncertain.

Declarative remarks such as “testing out the latest Live Conversation updates”
are handled as conversation and never treated as permission to invent monitoring
or other agent work. Freshness words do not route anything by themselves: the
semantic decision distinguishes factual lookup requests from meta-questions,
corrections, continuations, and observations. Incomplete thoughts are retained
without a reply or side effect and are assembled with a later continuation only
when the complete meaning is coherent. A reply postcondition removes unrequested promises to
monitor, verify, investigate, or keep sessions active while preserving factual
conversation around them. Testing statements use the same semantic contract as
other natural speech rather than a dedicated phrase or keyword shortcut.

Action confirmation is a persistent voice-controlled setting stored in
`~/.openclaw/state/live-conversation-settings.json`. Say “Always ask me before
taking actions” to enable it, “Don't ask for confirmation before actions” to
disable it, or ask “What is the confirmation setting?” When enabled, agent,
new-agent, and existing-session actions are held until a short affirmative reply
such as “go ahead”; a negative reply cancels the pending action. The current mode
is shown near the top of the Live Conversation page and can also be changed
with its direct toggle.

The bridge forwards escalated transcripts without adding response-length or
reasoning instructions. Voice response policy belongs to the gateway agent's
workspace instructions so complex requests can finish normal tool-backed work
before the final answer is shortened for speech.

## Consented real-audio capture

The Live Conversation page includes **Enable test audio capture**. Capture is
off by default and must be explicitly enabled in the app. While enabled, every
committed microphone turn is saved on the OpenClaw host under
`~/.openclaw/state/live-conversation-recordings/YYYYMMDD/` as:

- lossless mono 16 kHz PCM WAV (`*-user.wav`), captured before ASR or routing;
- a JSON sidecar with the capture/turn/response IDs, timestamps, duration, RMS,
  transcript, assembled meaning, route, response text, latency, and Android
  voice-processing diagnostics.

Capturing before ASR intentionally preserves clipped starts, room noise,
misrecognitions, interruptions, and ignored ambient turns. Wake-word windows
are never recorded. Disabling the toggle stops new capture immediately; it does
not delete prior recordings. The files remain local and are not exposed by an
HTTP download route.

To replay up to the latest 20 consented phone captures through the production
Whisper model as part of the regression suite:

```bash
LIVE_CONVERSATION_TEST_RECORDINGS="$HOME/.openclaw/state/live-conversation-recordings" \
  PYTHONPATH=live-conversation \
  ~/.openclaw/tools/pipecat-live-conversation/venv/bin/python \
  -m unittest live-conversation/test_recorded_audio.py -v
```

The replay gate validates the WAV contract, requires every captured turn to
remain decodable, and detects material transcript drift. A normal test run skips
this optional corpus when no capture path is supplied. Android WebView captures
are labeled as real phone microphone audio; synthetic and desktop probes remain
available for diagnosis but are excluded from the real-phone regression corpus.

## Send for Debug

The Live Conversation page includes **Send for Debug**. Pressing it explicitly
authorizes one correction run. The server writes a local, redacted diagnostic
bundle under `~/.openclaw/state/live-conversation-debug/YYYYMMDD/` containing
recent conversation messages and turn events, the latest consented capture
manifests, client voice-processing details, model/service state, repository
state, and bounded Live Conversation and Gateway logs. Credentials are redacted
and the bundle is not exposed over HTTP.

Each submission gets a unique OpenClaw agent session. The correction agent is
instructed to reproduce the issue, inspect the locally referenced WAV files,
implement and test the root-cause fix, and complete delivery. It publishes a
new signed app release only when an installable app change is required, or
applies an update-safe Gateway configuration/update through the supported safe
restart workflow when that is the actual cause. It must not patch installed
OpenClaw package code.

The status dot persists across page and service restarts:

- amber: diagnostics are being collected or corrected;
- green: correction completed without a new install;
- blue: a new app release is available;
- purple: a Gateway update was applied;
- red: the correction session failed and its bundle was retained.

## Incremental speech behavior

Stable and revisable partial transcription makes speech visible before the turn
ends and keeps capture open for natural-length input. The final transcript is
sent to the speech supervisor as soon as Smart Turn detects a complete turn.
Only stable text can start read-only prefetch. Partial text is never allowed to
launch tools or agents because an early Whisper hypothesis can still change.

Ollama's `/api/chat` request cannot append more text to a user message after
generation has begun, so definitive generation still starts at semantic turn
completion. The online ASR and prefetch stages reduce duplicated recognition
work and prepare authoritative context without acting on unstable speech.

## Jarvis wake word on Android

Install dashboard version 1.0.60 or newer, open the app while unlocked, and tap
**Enable Jarvis wake word**. Approve the Android Assistant-role prompt. The app
must remain the selected Assistant for lock-screen microphone access. Saying
“Jarvis” opens and starts Live Conversation; pressing the side lock button
stops an active conversation and returns to wake listening.

## Install and run

```bash
python3.13 -m venv ~/.openclaw/tools/pipecat-live-conversation/venv
~/.openclaw/tools/pipecat-live-conversation/venv/bin/pip install -r live-conversation/requirements.txt
curl -fL -o /tmp/kokoro-en-v0_19.tar.bz2 \
  https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/kokoro-en-v0_19.tar.bz2
tar -xjf /tmp/kokoro-en-v0_19.tar.bz2 \
  -C ~/.openclaw/tools/sherpa-onnx-tts/models
mkdir -p ~/.openclaw/tools/pipecat-live-conversation/models
curl -fL -o ~/.openclaw/tools/pipecat-live-conversation/models/smart-turn-v3.2-cpu.onnx \
  https://huggingface.co/pipecat-ai/smart-turn-v3/resolve/main/smart-turn-v3.2-cpu.onnx
chmod +x live-conversation/build-tts-worker.sh
live-conversation/build-tts-worker.sh
mkdir -p ~/.config/systemd/user
cp live-conversation/openclaw-live-conversation.service ~/.config/systemd/user/
cp live-conversation/openclaw-live-ollama.service ~/.config/systemd/user/
ollama create openclaw-live-conversation:4b -f live-conversation/Modelfile
systemctl --user daemon-reload
systemctl --user enable --now openclaw-live-ollama.service
systemctl --user enable --now openclaw-live-conversation.service
curl -fsS http://127.0.0.1:8790/health
```

The checked-in unit assumes this repository is at
`/home/john/openclaw-android-dashboard` and the existing local OpenClaw ASR/TTS
assets are installed under `/home/john/.openclaw`.

## Verify

```bash
PYTHONPATH=live-conversation \
  ~/.openclaw/tools/pipecat-live-conversation/venv/bin/python \
  -m unittest discover -s live-conversation -p 'test_*.py' -v
node scripts/test-live-conversation-app.mjs
node scripts/test-live-conversation-turns.mjs
PYTHONPATH=live-conversation \
  ~/.openclaw/tools/pipecat-live-conversation/venv/bin/python \
  live-conversation/e2e_conversation_matrix.py --url http://127.0.0.1:8790/ws
```

The Python discovery suite includes `test_voice_audio.py`, a model-backed audio
matrix. It generates fresh user utterances in memory with the production Kokoro
George voice, resamples them to the Android microphone's 16 kHz PCM format, and
passes them through the production Faster-Whisper `small.en` decoder and exact
production decoding options. The integration suite uses CPU `int8` inference so
it remains deterministic outside the CUDA-configured systemd service. The cases
cover three-turn conversational continuity, close-spaced barge-in while an
earlier response is pending, spoken action confirmation, room noise, noise-only
rejection, a 500 ms thinking pause, silent spoken interruption, and non-silent
fixture validation. If the local production voice assets are not installed, these
hardware integration cases report an explicit skip while the fast unit suite
continues.

`e2e_conversation_matrix.py` is the required deployed-service release gate. It
uses production Kokoro audio as microphone input, sends wall-clock-paced 20 ms
PCM frames through the real WebSocket, and requires actual Whisper transcripts,
semantic decisions, local-model replies, and audible response PCM. It covers
slow, natural, and fast speakers; broadband background noise; 250, 800, and
1,800 ms intra-sentence gaps; a sentence committed as two separate
transcriptions; a nonverbal backchannel; and user barge-in during a real model
response. The split-sentence case must remain silent after its first fragment
and answer the semantically assembled question after its second fragment.
