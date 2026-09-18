#!/usr/bin/env node

"use strict";

const http = require("node:http");
const fs = require("node:fs/promises");
const path = require("node:path");

const HOST = process.env.HOST || "127.0.0.1";
const PORT = Number(process.env.PORT || 8789);
const ROOT = __dirname;
const REQUEST_TIMEOUT_MS = 1800;

const APPS = Object.freeze([
  { id: "control-ui", name: "Control UI", description: "Chat, agents, sessions, nodes, settings, and dashboards.", publicPort: 443, localPort: 38179 },
  { id: "live-conversation", name: "Live Conversation", description: "Hands-free voice conversations with Jarvis.", publicPort: 8443, localPort: 8790 },
  { id: "teams-help", name: "Teams Help", description: "Review collaboration records and supporting evidence.", publicPort: 8445, localPort: 8504 },
  { id: "contacts", name: "Contacts", description: "Search and inspect the synchronized contact database.", publicPort: 8446, localPort: 8503 },
  { id: "monitor", name: "Monitor", description: "Gateway health, services, jobs, nodes, and recent activity.", publicPort: 8447, localPort: 8501 },
  { id: "manuals-rag", name: "Manuals RAG", description: "Search manuals and ask source-grounded technical questions.", publicPort: 8448, localPort: 8601 },
]);

const STATIC_FILES = Object.freeze({
  "/": ["index.html", "text/html; charset=utf-8"],
  "/app.js": ["app.js", "text/javascript; charset=utf-8"],
  "/styles.css": ["styles.css", "text/css; charset=utf-8"],
  "/manifest.webmanifest": ["manifest.webmanifest", "application/manifest+json; charset=utf-8"],
});

const SECURITY_HEADERS = Object.freeze({
  "Cache-Control": "no-cache",
  "Content-Security-Policy": "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
  "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
  "Referrer-Policy": "no-referrer",
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
});

function writeJson(response, status, body) {
  response.writeHead(status, { ...SECURITY_HEADERS, "Content-Type": "application/json; charset=utf-8" });
  response.end(JSON.stringify(body));
}

function probe(app) {
  const started = Date.now();
  return new Promise((resolve) => {
    const request = http.get({
      hostname: "127.0.0.1",
      port: app.localPort,
      path: "/",
      timeout: REQUEST_TIMEOUT_MS,
      headers: { "User-Agent": "OpenClaw-Web-Hub/1.0" },
    }, (response) => {
      response.resume();
      resolve({
        id: app.id,
        online: true,
        status: response.statusCode || 0,
        latencyMs: Date.now() - started,
      });
    });
    request.on("timeout", () => request.destroy(new Error("timeout")));
    request.on("error", () => resolve({
      id: app.id,
      online: false,
      status: 0,
      latencyMs: Date.now() - started,
    }));
  });
}

async function handle(request, response) {
  const url = new URL(request.url || "/", "http://localhost");

  if (request.method === "GET" && url.pathname === "/health") {
    writeJson(response, 200, { ok: true, app: "openclaw-web-hub" });
    return;
  }

  if (request.method === "GET" && url.pathname === "/api/apps") {
    const statuses = await Promise.all(APPS.map(probe));
    writeJson(response, 200, { apps: APPS.map(({ localPort, ...app }) => app), statuses });
    return;
  }

  const staticFile = STATIC_FILES[url.pathname];
  if (request.method !== "GET" || !staticFile) {
    writeJson(response, 404, { error: "not_found" });
    return;
  }

  try {
    const body = await fs.readFile(path.join(ROOT, staticFile[0]));
    response.writeHead(200, { ...SECURITY_HEADERS, "Content-Type": staticFile[1] });
    response.end(body);
  } catch (error) {
    console.error("static_file_error", staticFile[0], error.message);
    writeJson(response, 500, { error: "internal_error" });
  }
}

const server = http.createServer((request, response) => {
  handle(request, response).catch((error) => {
    console.error("request_error", error);
    if (!response.headersSent) writeJson(response, 500, { error: "internal_error" });
    else response.end();
  });
});

server.listen(PORT, HOST, () => {
  console.log(`OpenClaw Web Hub listening on http://${HOST}:${PORT}`);
});

function shutdown(signal) {
  console.log(`Received ${signal}; shutting down`);
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(1), 5000).unref();
}

process.on("SIGINT", () => shutdown("SIGINT"));
process.on("SIGTERM", () => shutdown("SIGTERM"));
