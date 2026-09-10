import assert from "node:assert/strict";
import vm from "node:vm";
import { readFileSync } from "node:fs";

const source = readFileSync(
  new URL("../live-conversation/server.py", import.meta.url),
  "utf8",
);
const scriptMatch = source.match(/<script>\n([\s\S]*?)\n<\/script>/);
assert.ok(scriptMatch, "embedded Live Conversation script is present");

function pcm(level) {
  const value = Math.max(-32767, Math.min(32767, Math.round(level * 32768)));
  const buffer = Buffer.alloc(640);
  for (let offset = 0; offset < buffer.length; offset += 2) {
    buffer.writeInt16LE(value, offset);
  }
  return buffer.toString("base64");
}

function harness() {
  const sent = [];
  const chunks = [];
  const elements = new Map();
  let interrupts = 0;
  let prepares = 0;

  function element(id = "") {
    return {
      id,
      textContent: "",
      className: "",
      style: {},
      children: [],
      onclick: null,
      replaceChildren(...items) { this.children = items; },
      appendChild(item) { this.children.push(item); },
      append(...items) { this.children.push(...items); },
      scrollIntoView() {},
      get lastElementChild() { return this.children.at(-1); },
    };
  }

  const document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, element(id));
      return elements.get(id);
    },
    createElement: () => element(),
  };

  class FakeWebSocket {
    static latest;
    constructor() {
      this.readyState = 1;
      FakeWebSocket.latest = this;
    }
    send(value) { sent.push(JSON.parse(value)); }
    close() { this.readyState = 3; }
    server(message) { this.onmessage({ data: JSON.stringify(message) }); }
  }

  const context = vm.createContext({
    document,
    window: { addEventListener() {} },
    location: { protocol: "http:", host: "127.0.0.1:8790", search: "" },
    URLSearchParams,
    WebSocket: FakeWebSocket,
    OpenClawNativeAudio: {
      startCapture() {},
      stopCapture() {},
      readChunkBase64: () => chunks.shift() || "",
      prepareAgentResponsePlayback: () => { prepares += 1; },
      interruptAgentResponsePlayback: () => { interrupts += 1; },
      playAgentResponsePcm16Base64() {},
    },
    OpenClawNativeApp: { liveConversationStopped() {} },
    atob: (value) => Buffer.from(value, "base64").toString("binary"),
    setInterval: () => 1,
    clearInterval() {},
    setTimeout: () => 1,
    clearTimeout() {},
    console,
  });
  vm.runInContext(scriptMatch[1], context);
  vm.runInContext("start()", context);
  const socket = FakeWebSocket.latest;
  socket.onopen();

  return {
    sent,
    socket,
    elements,
    run(level, count) {
      for (let index = 0; index < count; index += 1) {
        chunks.push(pcm(level));
        vm.runInContext("tick()", context);
      }
    },
    count(type) { return sent.filter((message) => message.type === type).length; },
    value(expression) { return vm.runInContext(expression, context); },
    get interrupts() { return interrupts; },
    get prepares() { return prepares; },
  };
}

{
  const app = harness();
  app.run(0.010, 150);
  assert.equal(app.count("start"), 0, "sustained background noise does not start a turn");
}

{
  const app = harness();
  app.socket.server({
    type: "history",
    messages: [
      { role: "user", content: "Older message" },
      { role: "assistant", content: "Newest message" },
    ],
  });
  const bubbles = app.elements.get("history").children;
  assert.equal(bubbles[0].children[1].textContent, "Newest message",
    "the newest message is rendered at the top");
  app.socket.server({ type: "action_status", state: "acknowledged" });
  assert.equal(app.elements.get("state").textContent, "Working…",
    "the UI shows work only after the acknowledgment event");
}

{
  const app = harness();
  // A near-field sentence can begin quietly before a later vowel crosses the
  // conservative start gate.  Preserve 900 ms before the 300 ms confirmation
  // window so those first words reach ASR instead of starting mid-sentence.
  app.run(0.007, 45);
  app.run(0.030, 15);
  const captured = app.sent.filter((message) => message.type === "audio");
  assert.equal(app.count("start"), 1, "confirmed foreground speech starts a turn");
  assert.equal(captured.length, 60, "the full 900 ms quiet onset and confirmation window are retained");
  assert.equal(captured[0].audioBase64, pcm(0.007), "capture begins at the quiet sentence onset");
}

{
  const app = harness();
  app.run(0.030, 14);
  app.run(0.001, 10);
  assert.equal(app.count("start"), 0, "a sub-300 ms noise spike is rejected");
}

{
  const app = harness();
  app.run(0.030, 15);
  app.run(0.030, 10);
  app.run(0.001, 25);
  assert.equal(app.count("commit"), 0, "a 500 ms thinking pause stays in one utterance");
  app.run(0.030, 10);
  app.run(0.001, 35);
  assert.equal(app.count("commit"), 1, "700 ms of silence commits an ordinary utterance");
}

{
  const app = harness();
  app.run(0.030, 25);
  app.run(0.001, 35);
  assert.equal(app.count("commit"), 1);
  app.run(0.001, 50);
  app.run(0.030, 25);
  app.run(0.001, 35);
  assert.equal(app.count("commit"), 2, "a second user turn is captured before the first reply arrives");
  assert.ok(
    app.sent.some((message) => message.type === "client_event"
      && message.event === "speech_started"
      && message.detail === "while_awaiting_response"),
    "back-to-back input is reported as starting while awaiting a response",
  );
}

{
  const app = harness();
  app.run(0.030, 15);
  app.socket.server({ type: "state", state: "listening" });
  app.run(0.030, 10);
  app.run(0.001, 35);
  assert.equal(app.count("commit"), 1, "a stale listening event cannot reset active recording");
}

{
  const app = harness();
  app.run(0.030, 15);
  app.run(0.030, 10);
  app.socket.server({ type: "partial_transcript", text: "Are you ready?" });
  app.run(0.001, 24);
  assert.equal(app.count("commit"), 1, "a semantically complete question commits after 450 ms");
}

{
  const app = harness();
  app.run(0.030, 15);
  app.run(0.030, 10);
  app.socket.server({ type: "partial_transcript", text: "Please check the session and" });
  app.run(0.001, 35);
  assert.equal(app.count("commit"), 0, "an unfinished clause survives an ordinary 700 ms pause");
  app.run(0.001, 21);
  assert.equal(app.count("commit"), 1, "an unfinished clause eventually commits after 1100 ms");
}

{
  const app = harness();
  app.socket.server({ type: "output_audio_buffer.started", responseId: "one" });
  app.run(0.020, 30);
  assert.equal(app.count("start"), 0, "moderate playback/background energy does not self-interrupt");
  app.run(0.040, 10);
  assert.equal(app.count("start"), 1, "confirmed user speech interrupts assistant playback");
  assert.equal(app.interrupts, 1, "native playback is interrupted once");
  assert.ok(app.sent.some((message) => message.type === "client_event" && message.event === "barge_in"));
}

{
  const app = harness();
  app.socket.server({ type: "transcript", text: "First question." });
  app.socket.server({ type: "transcript", text: "Second question." });
  const history = app.value("historyMessages.map(item => item.role + ':' + item.content)");
  assert.deepEqual(
    Array.from(history),
    ["user:First question.", "user:Second question."],
    "each final transcript is recorded even when no assistant reply separates the turns",
  );
}

console.log("Live Conversation multi-turn/noise/interruption matrix passed");
