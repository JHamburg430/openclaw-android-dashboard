import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("../app/src/main/java/ai/openclaw/dashboard/MainActivity.java", import.meta.url), "utf8");

const methodStart = source.indexOf("private String buildNativeAudioBridgeScript()");
assert.notEqual(methodStart, -1, "buildNativeAudioBridgeScript not found");
const methodEnd = source.indexOf("private String buildViewportTighteningScript()", methodStart);
assert.notEqual(methodEnd, -1, "buildViewportTighteningScript not found");
const method = source.slice(methodStart, methodEnd);
const script = Array.from(method.matchAll(/"((?:\\.|[^"\\])*)"/g))
  .map((match) => JSON.parse(`"${match[1]}"`))
  .join("");

class FakeAudioContext {}
FakeAudioContext.prototype.createMediaStreamSource = function createMediaStreamSource() {};
FakeAudioContext.prototype.createScriptProcessor = function createScriptProcessor() {
  return { connect() {}, disconnect() {} };
};

const nativeChunks = [];
const bridge = {
  isAvailable: () => true,
  startCapture() {},
  stopCapture() {},
  readChunkBase64: () => nativeChunks.shift() ?? "",
};
const navigator = { mediaDevices: {} };
const window = { AudioContext: FakeAudioContext, OpenClawNativeAudio: bridge, navigator };
window.window = window;
let intervalCallback = null;
window.setInterval = (callback) => { intervalCallback = callback; return 1; };
window.clearInterval = () => {};

vm.runInNewContext(script, {
  window,
  navigator,
  Promise,
  Function,
  Float32Array,
  Math,
  String,
  Object,
  Array,
  JSON,
  setInterval: window.setInterval,
  clearInterval: window.clearInterval,
  atob: (value) => Buffer.from(value, "base64").toString("binary"),
});

const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
const [track] = stream.getTracks();
const controller = new AbortController();
let ended = 0;
track.addEventListener("ended", () => { ended += 1; }, { signal: controller.signal });
track.stop();

assert.equal(track.readyState, "ended");
assert.equal(ended, 1);
assert.equal(stream.getAudioTracks()[0], track);
assert.equal(typeof track.removeEventListener, "function");
assert.equal(typeof track.dispatchEvent, "function");

const liveStream = await navigator.mediaDevices.getUserMedia({ audio: true });
const context = new FakeAudioContext();
context.sampleRate = 16000;
const processor = context.createScriptProcessor(4096, 1, 1);
const frames = [];
processor.onaudioprocess = (event) => frames.push(event.inputBuffer.getChannelData(0));
context.createMediaStreamSource(liveStream).connect(processor);

const tenMsPcm16 = Buffer.alloc(160 * 2).toString("base64");
for (let index = 0; index < 25; index += 1) {
  nativeChunks.push(tenMsPcm16);
  intervalCallback();
}
assert.equal(frames.length, 0, "10 ms chunks must not become individual Talk requests");
nativeChunks.push(tenMsPcm16);
intervalCallback();
assert.equal(frames.length, 1);
assert.equal(frames[0].length, 4096);
processor.disconnect();

console.log("native input bridge implements the MediaStreamTrack contract and emits paced 4096-sample Talk frames");
