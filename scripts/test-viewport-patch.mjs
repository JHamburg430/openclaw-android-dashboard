import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const source = readFileSync(new URL("../app/src/main/java/ai/openclaw/dashboard/MainActivity.java", import.meta.url), "utf8");

const methodStart = source.indexOf("private String buildViewportTighteningScript()");
assert.notEqual(methodStart, -1, "buildViewportTighteningScript not found");
const methodEnd = source.indexOf("private void injectRuntimeScripts", methodStart);
assert.notEqual(methodEnd, -1, "injectRuntimeScripts not found");
const method = source.slice(methodStart, methodEnd);

assert.equal(method.includes("[class*=composer]"), false, "viewport patch must not target composer classes");
assert.equal(method.includes("[class*=chat]"), false, "viewport patch must not target chat classes");
assert.equal(method.includes(".shell--chat"), false, "viewport patch must not force shell chat sizing");
assert.equal(method.includes("max-height:100dvh"), false, "viewport patch must not cap chat height");

console.log("viewport patch avoids broad chat layout overrides");
