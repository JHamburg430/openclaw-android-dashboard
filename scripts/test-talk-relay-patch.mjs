import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../app/src/main/java/ai/openclaw/dashboard/MainActivity.java", import.meta.url), "utf8");
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
}

FakeSocket.CONNECTING = 0;
FakeSocket.OPEN = 1;
FakeSocket.CLOSING = 2;
FakeSocket.CLOSED = 3;

const window = {
  OpenClawNativeAudio: {
    playAgentResponsePcm16Base64(base64, sampleRate) {
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
};

vm.runInNewContext(script, {
  window,
  Object,
  JSON,
  String,
  setTimeout(callback) {
    timers.push(callback);
    return timers.length;
  },
});

const socket = new window.WebSocket("wss://gateway.example/ws");

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
const rewritten = JSON.parse(sent[0]);
assert.equal(rewritten.method, "talk.session.create");
assert.deepEqual(rewritten.params, {
  mode: "realtime",
  transport: "gateway-relay",
  brain: "agent-consult",
  sessionKey: "abc",
  vadThreshold: 0.006,
});

const emitTalk = (payload) => socket.emit("message", JSON.stringify({
  type: "event",
  event: "talk.event",
  payload,
}));

emitTalk({ type: "audio", audioBase64: "AAAA", sampleRate: 24000 });
emitTalk({ type: "output.audio.delta", delta: "BBBB", sampleRateHz: 16000 });
emitTalk({ type: "output.audio.delta", payload: { audioBase64: "CCCC", sampleRateHz: 22050 } });
emitTalk({ type: "response.audio.delta", audio: { data: "data:audio/pcm;base64,DDDD", sampleRateHz: 8000 } });
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
]);
assert.equal(noReplyStopped, true);
assert.equal(emptyTextStopped, true);
assert.deepEqual(spoken, []);
assert.ok(diagnostics.some((entry) => entry.kind === "talk.relay.audio_missing"));
assert.ok(diagnostics.some((entry) => entry.kind === "talk.relay.event" && entry.payload.type === "session.ready"));
assert.ok(diagnostics.some((entry) => entry.kind === "talk.relay.suppressed"));

console.log("talk relay patch handles rewrite and audio playback shapes");
