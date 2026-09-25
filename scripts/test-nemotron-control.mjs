import assert from "node:assert/strict";

const base = process.env.NEMOTRON_CONTROL_URL || "http://127.0.0.1:8790";
const root = await (await fetch(`${base}/`)).text();
assert.match(root, /Nemotron Control/);
assert.match(root, /data-action="provider-restart"/);
assert.match(root, /data-test="relay-tests"/);
assert.match(await (await fetch(`${base}/nemotron.js`)).text(), /api\/nemotron\/status/);
assert.match(await (await fetch(`${base}/nemotron.css`)).text(), /\.card/);

const status = await (await fetch(`${base}/api/nemotron/status`)).json();
assert.equal(status.provider.ActiveState, "active");
assert.equal(status.provider_health.status, "ok");
assert.equal(status.gateway.ok, true);
assert.equal(status.gateway.nemotron_loaded, true);
assert.equal(status.talk.provider, "nemotron-realtime-voice");
assert.equal(status.legacy.ActiveState, "inactive");

const start = await fetch(`${base}/api/nemotron/operations`, {
  method: "POST", headers: { "content-type": "application/json" },
  body: JSON.stringify({ kind: "test", name: "provider-health" }),
});
assert.equal(start.status, 202);
let result;
for (let i = 0; i < 20; i += 1) {
  await new Promise((resolve) => setTimeout(resolve, 250));
  result = (await (await fetch(`${base}/api/nemotron/status`)).json()).job;
  if (result.state !== "running") break;
}
assert.equal(result.state, "passed");
console.log("Nemotron control surface and provider-health operation passed");
