# Pipecat Live Conversation

This is the dedicated low-latency voice surface opened by **Live Conversation**
in the Android Dashboard plus menu.

## Pipeline

1. The Android native audio bridge captures 16 kHz PCM in 20 ms frames.
2. Browser-side VAD commits a turn after 600 ms of silence.
3. Pipecat's persistent `faster-whisper` `small.en` service transcribes locally
   with CPU `int8` beam search. The larger cached model improves recognition of
   terms such as “live agent,” session names, and longer technical requests.
4. The local `qwen3.5:4b` speech supervisor returns either a short speakable
   response or the hidden `[[OPENCLAW_AGENT]]` control token. This model is the
   local latency/quality balance: materially stronger than the former 0.8B
   supervisor while remaining sub-second once warm on the installed GPUs. The
   Ollama request keeps it warm for 30 minutes to avoid repeated cold starts
   during a conversation.
5. A bounded conversation history supplies the previous user and assistant
   turns to the speech supervisor. It is persisted at
   `~/.openclaw/state/live-conversation-history.json`, so context survives a
   WebView reconnect or service restart. Up to 80 messages and 48,000
   characters are retained; the newest context that fits the local model's
   prompt budget is selected dynamically. The page displays the same rolling
   80-message history as user and assistant bubbles.
6. The supervisor receives a compact, automatically refreshed summary of the
   last seven days of gateway sessions, including the exact session key,
   title, latest message, and authoritative `hasActiveRun` state. It can answer
   status questions directly without launching an agent.
7. The service intercepts control tokens before display/TTS. Ordinary tool work
   continues in `agent:main:live-conversation`; explicit requests for another
   agent get an independent session; and follow-ups can be sent to an exact
   active or recent gateway session. Concurrent agents are supported, and each
   final CLI result is returned to the bridge and spoken when it completes. If
   the WebView disconnects while an agent works, the task continues and its
   final reply is queued for speech on the next Live Conversation connection.
8. A persistent local Kokoro TTS worker using the British male George voice
   returns 24 kHz PCM to the Android
   native playback bridge.
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

The bridge forwards escalated transcripts without adding response-length or
reasoning instructions. Voice response policy belongs to the gateway agent's
workspace instructions so complex requests can finish normal tool-backed work
before the final answer is shortened for speech.

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
systemctl --user daemon-reload
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
```
