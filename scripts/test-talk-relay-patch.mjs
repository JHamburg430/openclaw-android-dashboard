import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../app/src/main/java/ai/openclaw/dashboard/MainActivity.java", import.meta.url), "utf8");
assert.equal(source.includes("TextToSpeech"), false, "dashboard must not use Android TextToSpeech fallback for Talk output");
assert.equal(source.includes("setStreamVolume"), false, "dashboard must not force Android media volume for Talk output");
assert.equal(source.includes("talk.relay.tts_fallback"), false, "relay patch must not schedule text-to-speech fallback");
assert.equal(source.includes("OUTPUT_DRAIN_WAIT_MS"), true, "native PCM output must wait for AudioTrack playback to drain");
assert.equal(source.includes("AudioAttributes.USAGE_VOICE_COMMUNICATION"), true, "Talk output must use the Android communication audio route");
assert.equal(source.includes("AudioAttributes.CONTENT_TYPE_SPEECH"), true, "Talk output must identify conversational speech");
assert.equal(source.includes("getAvailableCommunicationDevices()"), true, "speaker routing must use Android communication devices");
assert.equal(source.includes("setCommunicationDevice(device)"), true, "Talk must explicitly select its communication output device");

const methodStart = source.indexOf("private String buildTalkGatewayRelayPatchScript()");
assert.notEqual(methodStart, -1, "buildTalkGatewayRelayPatchScript not found");
const methodEnd = source.indexOf("private String buildNativeAudioBridgeScript()", methodStart);
assert.notEqual(methodEnd, -1, "buildNativeAudioBridgeScript not found");
const method = source.slice(methodStart, methodEnd);

const returnStart = method.indexOf("return ");
assert.notEqual(returnStart, -1, "return statement not found");
const returnedExpression = method.slice(returnStart + "return ".length, method.lastIndexOf(";"));
const script = Array.from(returnedExpression.matchAll(/"((?:\\.|[^"\\])*)"/g))
  .map((match) => JSON.parse(`"${match[1]}"`))
  .join("");

const diagnostics = [];
const played = [];
const spoken = [];
const sent = [];
const sockets = [];
const timers = [];
let prepared = 0;
let finished = 0;
let interrupted = 0;
let nativePlaybackActive = false;

class FakeSocket {
  constructor(url, protocols) {
    this.url = url;
    this.protocols = protocols;
    this.listeners = new Map();
    sockets.push(this);
  }

  addEventListener(type, listener) {
    const listeners = this.listeners.get(type) ?? [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  send(data) {
    sent.push(data);
  }

  emit(type, data) {
    let stopped = false;
    const event = {
      data,
      stopImmediatePropagation() {
        stopped = true;
      },
    };
    for (const listener of this.listeners.get(type) ?? []) {
      listener(event);
      if (stopped) break;
    }
    return stopped;
  }

  dispatchEvent(event) {
    return !this.emit(event.type, event.data);
  }
}

FakeSocket.CONNECTING = 0;
FakeSocket.OPEN = 1;
FakeSocket.CLOSING = 2;
FakeSocket.CLOSED = 3;

const window = {
  OpenClawNativeAudio: {
    prepareAgentResponsePlayback() {
      prepared += 1;
    },
    finishAgentResponsePlayback() {
      finished += 1;
    },
    interruptAgentResponsePlayback() {
      interrupted += 1;
      nativePlaybackActive = false;
    },
    isAgentResponsePlaybackActive() {
      return nativePlaybackActive;
    },
    playAgentResponsePcm16Base64(base64, sampleRate) {
      nativePlaybackActive = true;
      played.push({ base64, sampleRate, agent: true });
    },
    playPcm16Base64(base64, sampleRate) {
      played.push({ base64, sampleRate, agent: false });
    },
    speakAgentResponseText(text) {
      spoken.push(text);
    },
  },
  __OPENCLAW_DIAG__: {
    emit(kind, payload) {
      diagnostics.push({ kind, payload });
    },
  },
  WebSocket: FakeSocket,
  MessageEvent: class MessageEvent {
    constructor(type, init) {
      this.type = type;
      this.data = init.data;
    }
  },
};
window.setTimeout = (callback) => {
  timers.push(callback);
  return timers.length;
};

vm.runInNewContext(script, {
  window,
  Object,
  JSON,
  String,
  setTimeout: window.setTimeout,
  MessageEvent: window.MessageEvent,
});

const socket = new window.WebSocket("wss://gateway.example/ws");
const downstreamRelayTypes = [];
socket.addEventListener("message", (event) => {
  const message = JSON.parse(event.data);
  if (message.event === "talk.event") downstreamRelayTypes.push(message.payload?.type);
});

socket.send(JSON.stringify({
  type: "req",
  id: 1,
  method: "talk.client.create",
  params: {
    mode: "realtime",
    transport: "gateway-relay",
    brain: "agent-consult",
    language: "en-US",
    sessionKey: "abc",
    vadThreshold: 0.006,
  },
}));

assert.equal(sent.length, 1);
const forwarded = JSON.parse(sent[0]);
assert.equal(forwarded.method, "talk.client.create",
  "the current Control UI must own its talk.client.create to talk.session.create fallback lifecycle");
assert.deepEqual(forwarded.params, {
  mode: "realtime",
  transport: "gateway-relay",
  brain: "agent-consult",
  language: "en-US",
  sessionKey: "abc",
  vadThreshold: 0.006,
});

const emitTalk = (payload) => socket.emit("message", JSON.stringify({
  type: "event",
  event: "talk.event",
  payload,
}));

emitTalk({ type: "output.audio.started" });
const audioStops = [
  emitTalk({ type: "audio", audioBase64: "AAAA", sampleRate: 24000 }),
  emitTalk({ type: "output.audio.delta", delta: "BBBB", sampleRateHz: 16000 }),
  emitTalk({ type: "output.audio.delta", payload: { audioBase64: "CCCC", sampleRateHz: 22050 } }),
  emitTalk({ type: "response.audio.delta", audio: { data: "data:audio/pcm;base64,DDDD", sampleRateHz: 8000 } }),
];
assert.deepEqual(audioStops, [true, true, true, true], "native-owned PCM must not reach the Web UI playback/barge-in path");
const markStopped = emitTalk({ type: "mark", markName: "response-one" });
assert.equal(markStopped, true, "playback mark waits for native AudioTrack drain");
nativePlaybackActive = false;
for (const timer of timers.splice(0)) timer();
emitTalk({ type: "audioDone" });
// A provider without explicit started still establishes a fresh boundary.
emitTalk({ type: "audio", audioBase64: "EEEE", sampleRate: 24000 });
const clearStopped = emitTalk({ type: "clear", reason: "provider_speech_started" });
assert.equal(clearStopped, false, "provider-confirmed clear still reaches the Control UI state machine");
emitTalk({ type: "output.audio.delta" });
emitTalk({ type: "output.text.done", text: "This text already has relay audio." });
const noReplyStopped = emitTalk({ type: "output.text.done", text: "NO_REPLY" });
const emptyTextStopped = emitTalk({ type: "output.text.done" });
emitTalk({ type: "session.ready" });
for (const timer of timers.splice(0)) timer();

assert.deepEqual(played, [
  { base64: "AAAA", sampleRate: 24000, agent: true },
  { base64: "BBBB", sampleRate: 16000, agent: true },
  { base64: "CCCC", sampleRate: 22050, agent: true },
  { base64: "DDDD", sampleRate: 8000, agent: true },
  { base64: "EEEE", sampleRate: 24000, agent: true },
]);
assert.equal(prepared, 2, "explicit and lazy response starts both prepare native playback");
assert.equal(finished, 1, "the completed response drains its native playback boundary");
assert.equal(interrupted, 1, "provider-confirmed clear interrupts native playback exactly once");
assert.equal(noReplyStopped, true);
assert.equal(emptyTextStopped, true);
assert.deepEqual(spoken, []);
assert.ok(diagnostics.some((entry) => entry.kind === "talk.relay.audio_missing"));
assert.ok(diagnostics.some((entry) => entry.kind === "talk.relay.event" && entry.payload.type === "session.ready"));
assert.ok(diagnostics.some((entry) => entry.kind === "talk.relay.suppressed"));
assert.ok(diagnostics.some((entry) => entry.kind === "talk.relay.audio_native_owned"));
assert.ok(diagnostics.some((entry) => entry.kind === "talk.relay.mark_deferred"));
assert.ok(diagnostics.some((entry) => entry.kind === "talk.relay.provider_clear"));
assert.equal(downstreamRelayTypes.includes("audio"), false,
  "native-owned PCM never reaches the Control UI audio queue");
assert.equal(downstreamRelayTypes.filter((type) => type === "mark").length, 1,
  "the completion mark reaches the Control UI once after native drain");
assert.equal(downstreamRelayTypes.filter((type) => type === "clear").length, 1,
  "provider-confirmed interruption still reaches the Control UI once");

console.log("talk relay patch preserves the Control UI session lifecycle and handles native audio playback shapes");
