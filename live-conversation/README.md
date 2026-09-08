# Pipecat Live Conversation

This is the dedicated low-latency voice surface opened by **Live Conversation**
in the Android Dashboard plus menu.

## Pipeline

1. The Android native audio bridge captures 16 kHz PCM in 20 ms frames.
2. Browser-side VAD requires 300 ms of near-field audio above a 0.012 RMS
   threshold before opening a turn, then commits it after 600 ms of silence.
   This rejects the lower-level television/road speech that the former 0.006
   start gate treated as if it came from the person holding the phone.
3. Pipecat's persistent `faster-whisper` `small.en` service transcribes locally
   with CUDA `float16` beam search on GPU 0 and falls back to CPU `int8` if CUDA
   initialization fails. Five-candidate beam search and a small vocabulary hint
   for Jarvis, OpenClaw, Live Conversation, agents, and sessions improve the
   recurring proper-name and system-term substitutions. The larger cached model
   improves recognition of terms such as “live agent,” session names, and
   longer technical requests. While a
   user is speaking, the service retranscribes the growing audio buffer about
   once per second and displays partial text. Final decoding consumes
   Faster-Whisper's lazy segment generator entirely on a worker thread, so
   inference cannot wedge the WebSocket event loop. No transcription timeout or
   fixed turn-length cap truncates a user's input. Silero VAD filters non-speech
   audio before decoding, with a stricter threshold for normal turns than for
   the wake word, reducing false transcripts from road and ambient noise.
4. The local `qwen3.5:4b` speech supervisor is installed under the dedicated
   Ollama identity `openclaw-live-conversation:4b` and returns either a short
   speakable response or the hidden `[[OPENCLAW_AGENT]]` control token. This model is the
   local latency/quality balance: materially stronger than the former 0.8B
   supervisor while remaining sub-second once warm on the installed GPUs. Its
   fixed 8k context and dedicated identity prevent unrelated qwen3.5 callers
   with 16k or 32k contexts from repeatedly replacing and reloading its runner.
   A loopback-only companion Ollama process on port 11439 isolates the live
   runner from the shared system Ollama scheduler, which otherwise evicted it
   even while its 30-minute keep-alive was active.
   Only the newest 24 routing-relevant history messages are included, while all
   80 messages remain persisted and visible. The
   Ollama request keeps it warm for 30 minutes to avoid repeated cold starts
   during a conversation. The model is also warmed when the service starts.
   The reply budget is 256 tokens rather than the former 80-token ceiling. If
   Ollama reports that the budget was exhausted (or returns exactly the capped
   token count), the request is retried once with 512 tokens instead of showing
   and speaking a syntactically valid JSON response whose `reply` ends midway
   through a sentence.
5. A bounded conversation history supplies the previous user and assistant
   turns to the speech supervisor. It is persisted at
   `~/.openclaw/state/live-conversation-history.json`, so context survives a
   WebView reconnect or service restart. Up to 80 messages and 48,000
   characters are retained; the newest context that fits the local model's
   prompt budget is selected dynamically. The page displays the same rolling
   80-message history as user and assistant bubbles.
6. The supervisor receives a compact, continuously background-refreshed summary of the
   last seven days of gateway sessions, including the exact session key,
   title, latest message, and authoritative `hasActiveRun` state. It can answer
   status questions deterministically without launching an agent or waiting for
   the local language model.
7. The service intercepts control tokens before display/TTS. Ordinary tool work
   continues in `agent:main:live-conversation`; explicit requests for another
   agent get an independent session; and follow-ups can be sent to an exact
   active or recent gateway session. Concurrent agents are supported, and each
   final CLI result is returned to the bridge and spoken when it completes.
   Tool-only `sessions_yield` handoffs are recognized as delegated work rather
   than failed responses; Live Conversation watches that exact session for its
   eventual visible result. If the WebView disconnects while an agent works,
   the task continues and its final reply is queued for speech on the next Live
   Conversation connection.
8. A persistent local Kokoro TTS worker using the British male George voice
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
   producing a reply.
9. When OpenClaw Dashboard holds Android's Assistant role, its lightweight
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
or other agent work. A reply postcondition removes unrequested promises to
monitor, verify, investigate, or keep sessions active while preserving factual
conversation around them. Common testing statements bypass the local routing
model, which also removes routing-model latency from those turns.

Action confirmation is a persistent voice-controlled setting stored in
`~/.openclaw/state/live-conversation-settings.json`. Say “Always ask me before
taking actions” to enable it, “Don't ask for confirmation before actions” to
disable it, or ask “What is the confirmation setting?” When enabled, agent,
new-agent, and existing-session actions are held until a short affirmative reply
such as “go ahead”; a negative reply cancels the pending action. The current mode
is shown near the top of the Live Conversation page.

The bridge forwards escalated transcripts without adding response-length or
reasoning instructions. Voice response policy belongs to the gateway agent's
workspace instructions so complex requests can finish normal tool-backed work
before the final answer is shortened for speech.

## Incremental speech behavior

Rolling partial transcription makes speech visible before the turn ends and
keeps capture open for natural-length input. The final transcript is sent to the
speech supervisor as soon as endpointing detects 600 ms of silence. Partial
text is deliberately not allowed to launch tools or agents because early
Whisper hypotheses can change as more words arrive.

Ollama's `/api/chat` request cannot append more text to a user message after
generation has begun. Safe future overlap is therefore to use stable partial
prefixes for intent prediction and read-only prefetch, then start the definitive
response from the final transcript. A true streaming speech-to-speech model or
an online ASR decoder would be required to revise an already-running model turn.

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
