"use strict";

const ICONS = {
  "control-ui": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h16v11H4zM8 20h8M12 16v4"/></svg>',
  "live-conversation": '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3M9 21h6"/></svg>',
  "teams-help": '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="9" cy="8" r="3"/><circle cx="17" cy="10" r="2"/><path d="M3 20c0-4 2-7 6-7s6 3 6 7M15 15c3 0 5 2 5 5"/></svg>',
  contacts: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="3" width="14" height="18" rx="2"/><circle cx="12" cy="9" r="3"/><path d="M8 17c.8-2 2.1-3 4-3s3.2 1 4 3M3 7h2M3 12h2M3 17h2"/></svg>',
  monitor: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 12h4l2-6 4 12 2-6h6"/></svg>',
  "manuals-rag": '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 4h6a4 4 0 0 1 4 4v12a4 4 0 0 0-4-4H4zM20 4h-6v16a4 4 0 0 1 4-4h2z"/></svg>',
};

const APPEARANCE = {
  "control-ui": { color: "blue", kicker: "COMMAND CENTER" },
  "live-conversation": { color: "violet", kicker: "VOICE" },
  "teams-help": { color: "green", kicker: "COLLABORATION" },
  contacts: { color: "orange", kicker: "PEOPLE" },
  monitor: { color: "cyan", kicker: "SYSTEM" },
  "manuals-rag": { color: "gold", kicker: "KNOWLEDGE" },
};

function appUrl(publicPort) {
  const port = Number(publicPort) === 443 ? "" : `:${publicPort}`;
  return `https://${location.hostname}${port}/`;
}

function renderCard(app, status) {
  const appearance = APPEARANCE[app.id] || { color: "blue", kicker: "APP" };
  const state = status?.online ? "online" : "offline";
  const detail = status?.online
    ? `Online · ${status.latencyMs} ms`
    : "Unavailable";
  return `
    <a class="app-card ${appearance.color}" href="${appUrl(app.publicPort)}" data-app="${app.id}">
      <div class="card-top">
        <span class="app-icon">${ICONS[app.id] || ""}</span>
        <span class="state ${state}"><span></span>${detail}</span>
      </div>
      <div class="card-body">
        <p class="kicker">${appearance.kicker}</p>
        <h2>${app.name}</h2>
        <p>${app.description}</p>
      </div>
      <span class="open-label">Open app <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 10h12M11 5l5 5-5 5"/></svg></span>
    </a>`;
}

async function refresh() {
  const button = document.querySelector("#refresh");
  const appsElement = document.querySelector("#apps");
  const summaryDot = document.querySelector("#summaryDot");
  const summaryText = document.querySelector("#summaryText");
  button.disabled = true;
  button.classList.add("spinning");
  summaryDot.className = "summary-dot loading";
  summaryText.textContent = "Checking services…";
  try {
    const response = await fetch("/api/apps", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    const statuses = new Map(data.statuses.map((status) => [status.id, status]));
    appsElement.innerHTML = data.apps.map((app) => renderCard(app, statuses.get(app.id))).join("");
    const online = data.statuses.filter((status) => status.online).length;
    const allOnline = online === data.apps.length;
    summaryDot.className = `summary-dot ${allOnline ? "online" : "warning"}`;
    summaryText.textContent = allOnline ? `All ${online} services online` : `${online} of ${data.apps.length} services online`;
    document.querySelector("#updatedAt").textContent = `Updated ${new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
  } catch (error) {
    appsElement.innerHTML = '<div class="error-card"><strong>Hub status unavailable</strong><span>Refresh to try again.</span></div>';
    summaryDot.className = "summary-dot offline";
    summaryText.textContent = "Could not check services";
  } finally {
    button.disabled = false;
    button.classList.remove("spinning");
  }
}

document.querySelector("#refresh").addEventListener("click", refresh);
document.querySelector("#hostLabel").textContent = location.hostname;
refresh();
