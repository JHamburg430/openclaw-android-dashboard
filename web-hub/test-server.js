#!/usr/bin/env node

"use strict";

const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");

const PORT = 18798;
const child = spawn(process.execPath, [require.resolve("./server.js")], {
  env: { ...process.env, HOST: "127.0.0.1", PORT: String(PORT) },
  stdio: ["ignore", "pipe", "inherit"],
});

async function waitForServer() {
  for (let attempt = 0; attempt < 30; attempt += 1) {
    try {
      const response = await fetch(`http://127.0.0.1:${PORT}/health`);
      if (response.ok) return;
    } catch (_) {}
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("test server did not start");
}

(async () => {
  try {
    await waitForServer();
    const health = await fetch(`http://127.0.0.1:${PORT}/health`);
    assert.equal(health.status, 200);
    assert.deepEqual(await health.json(), { ok: true, app: "openclaw-web-hub" });

    const index = await fetch(`http://127.0.0.1:${PORT}/`);
    assert.equal(index.status, 200);
    assert.match(await index.text(), /OpenClaw Hub/);
    assert.match(index.headers.get("content-security-policy"), /default-src 'self'/);

    const apps = await fetch(`http://127.0.0.1:${PORT}/api/apps`);
    assert.equal(apps.status, 200);
    const payload = await apps.json();
    assert.equal(payload.apps.length, 6);
    assert.equal(payload.statuses.length, 6);
    assert.equal(payload.apps.some((app) => Object.hasOwn(app, "localPort")), false);
    assert.deepEqual(payload.apps.map((app) => app.publicPort), [443, 8443, 8445, 8446, 8447, 8448]);

    const missing = await fetch(`http://127.0.0.1:${PORT}/missing`);
    assert.equal(missing.status, 404);
    console.log("web-hub tests passed");
  } finally {
    child.kill("SIGTERM");
  }
})().catch((error) => {
  console.error(error);
  child.kill("SIGTERM");
  process.exitCode = 1;
});
