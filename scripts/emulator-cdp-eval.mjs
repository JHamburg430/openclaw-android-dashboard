#!/usr/bin/env node

const port = Number(process.env.CDP_PORT || "9223");
const expression = process.argv.slice(2).join(" ");

if (!expression) {
  throw new Error("Usage: emulator-cdp-eval.mjs <JavaScript expression>");
}

const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const target = targets.find((candidate) => candidate.type === "page");
if (!target?.webSocketDebuggerUrl) {
  throw new Error(`No WebView page target found on CDP port ${port}.`);
}

const socket = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  socket.onopen = resolve;
  socket.onerror = reject;
});

const response = await new Promise((resolve, reject) => {
  socket.onmessage = (event) => {
    const message = JSON.parse(event.data);
    if (message.id !== 1) return;
    if (message.error) reject(new Error(message.error.message));
    else resolve(message);
  };
  socket.send(JSON.stringify({
    id: 1,
    method: "Runtime.evaluate",
    params: {
      expression,
      awaitPromise: true,
      returnByValue: true,
    },
  }));
});

socket.close();
const result = response.result?.result;
if (result?.exceptionDetails) {
  throw new Error(result.exceptionDetails.text || "Evaluation failed.");
}
console.log(JSON.stringify(result?.value ?? null, null, 2));
