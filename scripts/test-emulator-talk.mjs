#!/usr/bin/env node

import fs from "node:fs";

const port = Number(process.env.CDP_PORT || "9223");
const turnTimeoutMs = Number(process.env.TURN_TIMEOUT_MS || "90000");
const audioPath = process.argv[2];

if (!audioPath) {
  throw new Error("Usage: test-emulator-talk.mjs <24kHz-mono-pcm16-file>");
}

const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const target = targets.find((candidate) => candidate.type === "page");
if (!target?.webSocketDebuggerUrl) {
  throw new Error(`No WebView page target found on CDP port ${port}.`);
}

const cdp = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  cdp.onopen = resolve;
  cdp.onerror = reject;
});

let commandId = 0;
const pending = new Map();
const wire = {
  requests: [],
  responses: [],
  events: [],
  closes: [],
  sessionId: null,
  talkSocketRequestId: null,
};

function parseWireFrame(frame) {
  if (frame?.opcode !== 1 || typeof frame.payloadData !== "string") return null;
  try { return JSON.parse(frame.payloadData); } catch { return null; }
}

function summarizeTalkEvent(message) {
  const payload = message?.payload && typeof message.payload === "object" ? message.payload : {};
  const event = payload.talkEvent && typeof payload.talkEvent === "object" ? payload.talkEvent : payload;
  const detail = event.payload && typeof event.payload === "object" ? event.payload : {};
  const audioBase64 = [event.audioBase64, event.delta, detail.audioBase64, detail.delta]
    .find((candidate) => typeof candidate === "string") || null;
  return {
    sequence: message.seq ?? message.sequence ?? null,
    type: String(event.type || ""),
    role: typeof event.role === "string" ? event.role : typeof detail.role === "string" ? detail.role : null,
    turnId: typeof event.turnId === "string" ? event.turnId : typeof payload.turnId === "string" ? payload.turnId : null,
    text: typeof event.text === "string" ? event.text : typeof detail.text === "string" ? detail.text : null,
    transcript: typeof event.transcript === "string" ? event.transcript : typeof detail.transcript === "string" ? detail.transcript : null,
    message: typeof event.message === "string" ? event.message : typeof detail.message === "string" ? detail.message : null,
    reason: typeof event.reason === "string" ? event.reason : typeof detail.reason === "string" ? detail.reason : null,
    code: typeof event.code === "string" ? event.code : typeof detail.code === "string" ? detail.code : null,
    error: event.error && typeof event.error === "object" ? event.error : null,
    detail: event.detail && typeof event.detail === "object" ? event.detail : null,
    markName: typeof event.name === "string" ? event.name : typeof event.markName === "string" ? event.markName : typeof detail.name === "string" ? detail.name : typeof detail.markName === "string" ? detail.markName : null,
    final: event.final === true || detail.final === true,
    relaySessionId: payload.relaySessionId || null,
    audioBytes: audioBase64 ? Math.floor(audioBase64.length * 3 / 4) : 0,
    raw: String(event.type || "").includes("error") ? JSON.stringify(message).slice(0, 4_000) : null,
    at: Date.now(),
  };
}

cdp.onmessage = (event) => {
  const message = JSON.parse(event.data);
  if (message.method === "Network.webSocketFrameSent") {
    const frame = parseWireFrame(message.params?.response);
    if (frame && String(frame.method || "").startsWith("talk.")) {
      wire.talkSocketRequestId = message.params.requestId;
      wire.requests.push({
        id: frame.id,
        method: frame.method,
        sessionId: frame.params?.sessionId || null,
        audioBytes: typeof frame.params?.audioBase64 === "string"
          ? Math.floor(frame.params.audioBase64.length * 3 / 4)
          : 0,
        at: Date.now(),
      });
    }
  } else if (message.method === "Network.webSocketFrameReceived") {
    const frame = parseWireFrame(message.params?.response);
    if (frame?.type === "res") {
      const request = wire.requests.find((entry) => entry.id === frame.id);
      if (request) {
        const result = frame.payload || frame.result || null;
        wire.responses.push({ id: frame.id, ok: frame.ok === true, result, error: frame.error || null, at: Date.now() });
        if (request.method === "talk.session.create" && frame.ok) {
          wire.sessionId = result?.sessionId || result?.voiceSessionId || null;
        }
      }
    } else if (frame?.type === "event" && frame.event === "talk.event") {
      wire.events.push(summarizeTalkEvent(frame));
    }
  } else if (
    message.method === "Network.webSocketClosed" &&
    message.params?.requestId === wire.talkSocketRequestId
  ) {
    wire.closes.push({ at: Date.now() });
  }
  const waiter = pending.get(message.id);
  if (!waiter) return;
  pending.delete(message.id);
  if (message.error) waiter.reject(new Error(message.error.message));
  else waiter.resolve(message);
};

async function command(method, params = {}) {
  const id = ++commandId;
  return await new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    cdp.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(expression) {
  const response = await command("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
  });
  const result = response.result?.result;
  if (result?.exceptionDetails) {
    throw new Error(result.exceptionDetails.text || "Evaluation failed");
  }
  return result?.value;
}

async function waitFor(predicate, description, timeoutMs = 60_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate(wire)) return wire;
    const terminal = wire.events.find((event) => event.type === "session.error" || event.type === "session.closed");
    if (terminal) {
      throw new Error(`Talk session terminated while waiting for ${description}: ${JSON.stringify(terminal)}`);
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`Timed out waiting for ${description}: ${JSON.stringify({
    sessionId: wire.sessionId,
    requestCounts: wire.requests.reduce((counts, entry) => {
      counts[entry.method] = (counts[entry.method] || 0) + 1;
      return counts;
    }, {}),
    failedResponses: wire.responses.filter((entry) => !entry.ok),
    eventCounts: wire.events.reduce((counts, entry) => {
      counts[entry.type] = (counts[entry.type] || 0) + 1;
      return counts;
    }, {}),
    textEvents: wire.events.filter((entry) =>
      entry.text || entry.transcript || entry.type.includes("transcript") || entry.type.includes("text")
    ).map((entry) => ({ type: entry.type, role: entry.role, text: entry.text, transcript: entry.transcript, turnId: entry.turnId })),
    recentEvents: wire.events.slice(-20).map((entry) => ({ type: entry.type, sequence: entry.sequence })),
    closes: wire.closes,
  })}`);
}

function audioCompletionCount(candidate) {
  return candidate.events.filter((event) => event.type === "mark" || event.type === "output.audio.done").length;
}

function userTranscriptCompletionCount(candidate) {
  return candidate.events.filter((event) =>
    (event.type === "transcript" || event.type === "transcript.done") && event.role === "user"
  ).length;
}

function outputAudioDeltaCount(candidate) {
  return candidate.events.filter((event) => event.type === "audio" || event.type === "output.audio.delta").length;
}

function outputAudioSettled(candidate, baselineCount, quietMs = 3_000) {
  const events = candidate.events.filter((event) => event.type === "audio" || event.type === "output.audio.delta");
  return events.length > baselineCount && Date.now() - events.at(-1).at >= quietMs;
}

await command("Network.enable");

const installed = await evaluate(`(() => {
  if (window.__openclawTalkAcceptanceProbeInstalled) return true;
  const originalSend = WebSocket.prototype.send;
  WebSocket.prototype.send = function(data) {
    let parsed = null;
    try { parsed = JSON.parse(data); } catch (_) {}
    if (parsed && String(parsed.method || '').startsWith('talk.')) {
      window.__openclawTalkAcceptanceSocket = this;
    }
    return originalSend.call(this, data);
  };
  window.__openclawTalkAcceptanceProbeInstalled = true;
  return true;
})()`);
if (!installed) throw new Error("Could not install Talk acceptance probe");

await evaluate(`(() => {
  const sidebarClose = Array.from(document.querySelectorAll('button')).find((candidate) =>
    candidate.getAttribute('aria-label') === 'Close assistant sidebar'
  );
  if (sidebarClose) sidebarClose.click();
  return true;
})()`);
for (let attempt = 0; attempt < 40; attempt += 1) {
  const talkControlReady = await evaluate(`Array.from(document.querySelectorAll('button'))
    .some((candidate) => ['Tap to talk', 'Stop voice input'].includes(candidate.getAttribute('aria-label')))`);
  if (talkControlReady) break;
  if (attempt === 39) throw new Error("Talk control did not render within 10 seconds");
  await new Promise((resolve) => setTimeout(resolve, 250));
}

async function trustedClick(label) {
  const point = await evaluate(`(() => {
    const button = Array.from(document.querySelectorAll('button')).find((candidate) =>
      candidate.getAttribute('aria-label') === ${JSON.stringify(label)}
    );
    if (!button) return null;
    const rect = button.getBoundingClientRect();
    return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
  })()`);
  if (!point) return false;
  await command("Input.dispatchMouseEvent", {
    type: "mousePressed",
    x: point.x,
    y: point.y,
    button: "left",
    clickCount: 1,
  });
  await command("Input.dispatchMouseEvent", {
    type: "mouseReleased",
    x: point.x,
    y: point.y,
    button: "left",
    clickCount: 1,
  });
  return true;
}

const active = await evaluate(`Array.from(document.querySelectorAll('button'))
  .some((candidate) => candidate.getAttribute('aria-label') === 'Stop voice input')`);
if (active) {
  await evaluate(`Array.from(document.querySelectorAll('button'))
    .find((candidate) => candidate.getAttribute('aria-label') === 'Stop voice input')?.click()`);
  for (let attempt = 0; attempt < 40; attempt += 1) {
    const stopped = await evaluate(`Array.from(document.querySelectorAll('button'))
      .some((candidate) => candidate.getAttribute('aria-label') === 'Tap to talk')`);
    if (stopped) break;
    if (attempt === 39) throw new Error("Existing Talk session did not stop within 10 seconds");
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  wire.requests.length = 0;
  wire.responses.length = 0;
  wire.events.length = 0;
  wire.closes.length = 0;
  wire.sessionId = null;
}
let started = false;
for (let attempt = 0; attempt < 3; attempt += 1) {
  if (!await trustedClick("Tap to talk")) throw new Error("Talk button not found");
  const startDeadline = Date.now() + 3_000;
  while (Date.now() < startDeadline) {
    if (wire.requests.some((request) => request.method === "talk.session.create")) {
      started = true;
      break;
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  if (started) break;
}
if (!started) throw new Error("Trusted Talk activation did not create a session");

const speech = fs.readFileSync(audioPath);
const frameBytes = 4_800;
const silence = Buffer.alloc(frameBytes);
const frames = [
  ...Array.from({ length: 9 }, () => silence),
  ...Array.from({ length: Math.ceil(speech.length / frameBytes) }, (_, index) => {
    const frame = speech.subarray(index * frameBytes, (index + 1) * frameBytes);
    return frame.length === frameBytes ? frame : Buffer.concat([frame, Buffer.alloc(frameBytes - frame.length)]);
  }),
  ...Array.from({ length: 10 }, () => silence),
].map((frame) => frame.toString("base64"));

let state = await waitFor(
  (candidate) => candidate.sessionId && candidate.events.some((event) => event.type === "session.ready"),
  "session.ready",
  45_000,
);
const baselineAudioCompletionCount = audioCompletionCount(state);
const baselineUserTranscriptCount = userTranscriptCompletionCount(state);
const baselineOutputAudioCount = outputAudioDeltaCount(state);
await evaluate(`(() => {
  if (window.OpenClawNativeAudio?.stopCapture) window.OpenClawNativeAudio.stopCapture();
  return true;
})()`);

async function injectTurn(turn) {
  const injection = await evaluate(`(async () => {
    const socket = window.__openclawTalkAcceptanceSocket;
    const sessionId = ${JSON.stringify(state.sessionId)};
    if (!socket || socket.readyState !== WebSocket.OPEN || !sessionId) {
      return { ok: false, error: 'Talk socket/session unavailable' };
    }
    const frames = ${JSON.stringify(frames)};
    for (let index = 0; index < frames.length; index += 1) {
      socket.send(JSON.stringify({
        type: 'req',
        id: 'emulator-turn-${turn}-' + Date.now() + '-' + index,
        method: 'talk.session.appendAudio',
        params: {
          sessionId,
          audioBase64: frames[index],
          timestamp: Date.now(),
        },
      }));
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    return { ok: true, frames: frames.length, sessionId };
  })()`);
  if (!injection?.ok) throw new Error(injection?.error || `Turn ${turn} audio injection failed`);
  return injection;
}

const firstInjection = await injectTurn(1);
state = await waitFor(
  (candidate) =>
    userTranscriptCompletionCount(candidate) >= baselineUserTranscriptCount + 1 &&
    outputAudioSettled(candidate, baselineOutputAudioCount),
  "the first final input transcript and settled audio response",
  turnTimeoutMs,
);
const firstAudioCompletionCount = audioCompletionCount(state);
const firstUserTranscriptCount = userTranscriptCompletionCount(state);
const firstOutputAudioCount = outputAudioDeltaCount(state);

const secondInjection = await injectTurn(2);
state = await waitFor(
  (candidate) =>
    userTranscriptCompletionCount(candidate) >= firstUserTranscriptCount + 1 &&
    outputAudioSettled(candidate, firstOutputAudioCount),
  "the second final input transcript and settled audio response",
  turnTimeoutMs,
);
await new Promise((resolve) => setTimeout(resolve, 2_000));
state = wire;

const buttonLabel = await evaluate(`Array.from(document.querySelectorAll('button'))
  .find((candidate) => ['Tap to talk', 'Stop voice input'].includes(candidate.getAttribute('aria-label')))
  ?.getAttribute('aria-label') || null`);
const audioCompletions = state.events.filter((event) => event.type === "mark" || event.type === "output.audio.done");
const userTranscripts = state.events.filter((event) =>
  (event.type === "transcript" || event.type === "transcript.done") && event.role === "user"
);
const assistantTranscripts = state.events.filter((event) => event.type === "output.text.done");
const appendRequests = state.requests.filter((request) => request.method === "talk.session.appendAudio");
const failedAppendResponses = state.responses.filter((response) => {
  const request = state.requests.find((entry) => entry.id === response.id);
  return request?.method === "talk.session.appendAudio" && !response.ok;
});
const socketReadyState = await evaluate("window.__openclawTalkAcceptanceSocket?.readyState ?? null");
const result = {
  passed:
    userTranscripts.length >= firstUserTranscriptCount + 1 &&
    outputAudioDeltaCount(state) > firstOutputAudioCount &&
    state.closes.length === 0 &&
    buttonLabel === "Stop voice input" &&
    failedAppendResponses.length === 0,
  sessionId: state.sessionId,
  firstAudioCompletionCount,
  finalAudioCompletionCount: audioCompletions.length,
  firstUserTranscriptCount,
  finalUserTranscriptCount: userTranscripts.length,
  userTranscripts: userTranscripts.map((event) => event.text || event.transcript).filter(Boolean),
  assistantTranscripts: assistantTranscripts.map((event) => event.text || event.transcript).filter(Boolean),
  outputAudioEvents: outputAudioDeltaCount(state),
  injectedFrames: firstInjection.frames + secondInjection.frames,
  appendRequests: appendRequests.length,
  failedAppendResponses: failedAppendResponses.length,
  closes: state.closes,
  buttonLabel,
  socketReadyState,
};

console.log(JSON.stringify(result, null, 2));
cdp.close();
if (!result.passed) process.exitCode = 1;
