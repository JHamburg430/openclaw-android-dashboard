# Pipecat Live Conversation

This is the dedicated low-latency voice surface opened by **Live Conversation**
in the Android Dashboard plus menu.

## Pipeline

1. The Android native audio bridge captures 16 kHz PCM in 20 ms frames.
2. Browser-side VAD commits a turn after 600 ms of silence.
3. Pipecat's persistent `faster-whisper` `tiny.en` service transcribes locally.
4. A fast local speech model returns either a short speakable response or the
   hidden `[[OPENCLAW_AGENT]]` control token.
5. The service intercepts that token before display/TTS and sends the original
   transcript to the normal OpenClaw agent in the dedicated
   `agent:main:live-conversation` session, preserving tools and context.
6. A persistent local Jarvis TTS worker returns 24 kHz PCM to the Android
   native playback bridge.

The speech-model prompt keeps the direct route conservative: requests needing
current information, personal context, tools, memory, judgment, or side effects
emit the hidden control token and escalate to the normal agent.

The bridge forwards escalated transcripts without adding response-length or
reasoning instructions. Voice response policy belongs to the gateway agent's
workspace instructions so complex requests can finish normal tool-backed work
before the final answer is shortened for speech.

## Install and run

```bash
python3.13 -m venv ~/.openclaw/tools/pipecat-live-conversation/venv
~/.openclaw/tools/pipecat-live-conversation/venv/bin/pip install -r live-conversation/requirements.txt
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
