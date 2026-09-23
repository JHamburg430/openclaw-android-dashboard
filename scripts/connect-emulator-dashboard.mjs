#!/usr/bin/env node

import { spawnSync } from "node:child_process";

const serial = process.argv[2] || "emulator-5554";
const adb = process.env.ADB || "/home/john/.android-build/android-sdk/platform-tools/adb";
const packageName = "ai.openclaw.dashboard";
const localGatewayUrl = "http://127.0.0.1:18789/";

function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    encoding: "utf8",
    stdio: options.input == null ? ["ignore", "pipe", "pipe"] : ["pipe", "pipe", "pipe"],
    input: options.input,
  });
  if (result.status !== 0) {
    throw new Error(`${command} ${args.join(" ")} failed: ${result.stderr.trim()}`);
  }
  return result.stdout.trim();
}

const dashboard = JSON.parse(run("openclaw", ["dashboard", "--json", "--no-open"]));
if (!dashboard.browserUrl || !dashboard.browserUrl.startsWith(localGatewayUrl)) {
  throw new Error("OpenClaw did not return a loopback browser bootstrap URL.");
}

const prefs = `<?xml version="1.0" encoding="utf-8" standalone="yes" ?>
<map>
<string name="url">${localGatewayUrl}</string>
<string name="bootstrapToken"></string>
<string name="token"></string>
<string name="password"></string>
<boolean name="nodeEnabled" value="false" />
<boolean name="keep_screen_awake" value="false" />
</map>
`;

// An APK upgrade can leave Android's permission controller above the replaced activity.
run(adb, ["-s", serial, "shell", "input", "keyevent", "KEYCODE_BACK"]);
run(adb, ["-s", serial, "shell", "am", "force-stop", packageName]);
run(adb, ["-s", serial, "shell", "run-as", packageName, "mkdir", "-p", "shared_prefs"]);
run(adb, ["-s", serial, "shell", "run-as", packageName, "dd", "of=shared_prefs/openclaw_dashboard.xml"], { input: prefs });
run(adb, ["-s", serial, "shell", "am", "start", "-W", "-n", `${packageName}/.MainActivity`]);

let pid = "";
for (let attempt = 0; attempt < 20 && !pid; attempt += 1) {
  const result = spawnSync(adb, ["-s", serial, "shell", "pidof", packageName], { encoding: "utf8" });
  pid = result.status === 0 ? result.stdout.trim() : "";
  if (!pid) await new Promise((resolve) => setTimeout(resolve, 500));
}
if (!pid) throw new Error("Dashboard process did not start.");

const devtoolsPort = 9222;
run(adb, ["-s", serial, "forward", `tcp:${devtoolsPort}`, `localabstract:webview_devtools_remote_${pid}`]);

let target;
for (let attempt = 0; attempt < 30 && !target; attempt += 1) {
  try {
    const targets = await (await fetch(`http://127.0.0.1:${devtoolsPort}/json`)).json();
    target = targets.find((candidate) => candidate.type === "page");
  } catch {}
  if (!target) await new Promise((resolve) => setTimeout(resolve, 500));
}
if (!target) throw new Error("Dashboard WebView debugging target did not appear.");

const socket = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  socket.onopen = resolve;
  socket.onerror = reject;
});
let nextId = 1;
const pending = new Map();
socket.onmessage = (event) => {
  const message = JSON.parse(event.data);
  const resolve = pending.get(message.id);
  if (resolve) {
    pending.delete(message.id);
    resolve(message);
  }
};
function cdp(method, params = {}) {
  return new Promise((resolve) => {
    const id = nextId++;
    pending.set(id, resolve);
    socket.send(JSON.stringify({ id, method, params }));
  });
}

await cdp("Page.navigate", { url: dashboard.browserUrl });

let outcome;
for (let attempt = 0; attempt < 40; attempt += 1) {
  await new Promise((resolve) => setTimeout(resolve, 500));
  const response = await cdp("Runtime.evaluate", {
    expression: `({href: location.href, title: document.title, body: document.body ? document.body.innerText : ""})`,
    returnByValue: true,
  });
  const value = response.result?.result?.value;
  if (!value) continue;
  const body = value.body || "";
  if (body.includes("Switch to a different Gateway?")) {
    await cdp("Runtime.evaluate", {
      expression: `(() => { const button = [...document.querySelectorAll("button")].find((candidate) => candidate.textContent.includes("Switch to 127.0.0.1:18789")); if (button) button.click(); return Boolean(button); })()`,
      returnByValue: true,
    });
    continue;
  }
  if (body.includes("Gateway unreachable") || body.includes("expects its password")) {
    outcome = { connected: false, title: value.title, href: value.href };
  } else if (body.includes("Talk") || body.includes("Sessions") || body.includes("SESSIONS") || body.includes("Overview")) {
    outcome = { connected: true, title: value.title, href: value.href };
    break;
  }
}
socket.close();

if (!outcome?.connected) {
  throw new Error("Control UI did not reach a connected state through the browser bootstrap URL.");
}
console.log(`Dashboard connected on ${serial}: ${outcome.title} (${new URL(outcome.href).pathname})`);
