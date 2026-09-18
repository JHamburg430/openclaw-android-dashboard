import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const api = readFileSync(new URL("../live-conversation/test_results_api.py", import.meta.url), "utf8");
const html = readFileSync(new URL("../live-conversation/tests.html", import.meta.url), "utf8");
const js = readFileSync(new URL("../live-conversation/tests.js", import.meta.url), "utf8");
const server = readFileSync(new URL("../live-conversation/server.py", import.meta.url), "utf8");
const settings = readFileSync(new URL("../live-conversation/settings.js", import.meta.url), "utf8");

assert.ok(server.includes("test_results_api.install(app, _origin_matches_request)"));
assert.ok(settings.includes("testsLink.href='/tests'"));
assert.ok(html.includes('id="suites"'));
assert.ok(html.includes('id="active"'));
assert.ok(html.includes('id="console"'));
assert.ok(js.includes("fetch('/api/tests'"));
assert.ok(js.includes("fetch('/api/tests/runs'"));
assert.ok(js.includes("schedule(running?750:5000)"));
assert.ok(api.includes('set(body) != {"suite"}'));
assert.ok(api.includes('raise RuntimeError("A test run is already in progress.")'));
assert.equal(js.includes("innerHTML"), false, "runner output is rendered as text, never HTML");

console.log("Live Conversation test-results page is wired to its allowlisted runner");
