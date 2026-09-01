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

const bridge = {
  isAvailable: () => true,
  startCapture() {},
  stopCapture() {},
  readChunkBase64: () => "",
};
const navigator = { mediaDevices: {} };
const window = { AudioContext: FakeAudioContext, OpenClawNativeAudio: bridge, navigator };
window.window = window;

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
  setInterval,
  clearInterval,
  atob: () => "",
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
console.log("native input bridge implements the Gateway 2026.8.2 MediaStreamTrack event contract");

