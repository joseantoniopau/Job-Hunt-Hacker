/* Job Hunt Hacker — extension popup.
 *
 * Responsibilities:
 *   - Show connection status for the local JHH server
 *     (GET /api/extension/status) + a link to open the app.
 *   - "Scan & autofill": fetch GET /api/extension/fill-data for the active
 *     tab's URL, inject content.js into the active tab (activeTab +
 *     scripting, user-initiated), and hand the payload over via messaging.
 *     The content script renders the panel; it makes no network calls.
 *   - "Save page to vault": POST {url} to /api/evidence/url (legacy
 *     capture feature, kept).
 *
 * The ONLY network destination is the local server (127.0.0.1:8731 by
 * default). No external calls, no tracking.
 */

const DEFAULT_SERVER = "http://127.0.0.1:8731";
const STORAGE_KEY = "jhh_server_url";

// chrome / browser API compatibility shim — Firefox uses `browser.*`.
const xt = (typeof browser !== "undefined" && browser) ||
           (typeof chrome !== "undefined" && chrome) || null;

function $(id) { return document.getElementById(id); }

function showToast(message, kind) {
  const el = $("toast");
  el.className = "";
  el.classList.add(kind === "err" ? "err" : "ok");
  el.textContent = message;
}

function getStoredServer() {
  return new Promise((resolve) => {
    if (!xt || !xt.storage || !xt.storage.local) { resolve(DEFAULT_SERVER); return; }
    try {
      const out = xt.storage.local.get([STORAGE_KEY], (res) => {
        resolve((res && res[STORAGE_KEY]) || DEFAULT_SERVER);
      });
      if (out && typeof out.then === "function") {
        out.then((res) => resolve((res && res[STORAGE_KEY]) || DEFAULT_SERVER))
           .catch(() => resolve(DEFAULT_SERVER));
      }
    } catch (_) {
      resolve(DEFAULT_SERVER);
    }
  });
}

function setStoredServer(value) {
  return new Promise((resolve) => {
    if (!xt || !xt.storage || !xt.storage.local) { resolve(false); return; }
    try {
      const out = xt.storage.local.set({ [STORAGE_KEY]: value }, () => resolve(true));
      if (out && typeof out.then === "function") {
        out.then(() => resolve(true)).catch(() => resolve(false));
      }
    } catch (_) {
      resolve(false);
    }
  });
}

function getActiveTab() {
  return new Promise((resolve) => {
    if (!xt || !xt.tabs || !xt.tabs.query) { resolve(null); return; }
    try {
      const out = xt.tabs.query({ active: true, currentWindow: true }, (tabs) => {
        resolve((tabs && tabs[0]) || null);
      });
      if (out && typeof out.then === "function") {
        out.then((tabs) => resolve((tabs && tabs[0]) || null)).catch(() => resolve(null));
      }
    } catch (_) {
      resolve(null);
    }
  });
}

function sendTabMessage(tabId, message) {
  return new Promise((resolve) => {
    try {
      const out = xt.tabs.sendMessage(tabId, message, (resp) => {
        // Swallow "no receiver" runtime errors — resolve null instead.
        void (xt.runtime && xt.runtime.lastError);
        resolve(resp || null);
      });
      if (out && typeof out.then === "function") {
        out.then((resp) => resolve(resp || null)).catch(() => resolve(null));
      }
    } catch (_) {
      resolve(null);
    }
  });
}

function injectContentScript(tabId) {
  return new Promise((resolve) => {
    if (!xt || !xt.scripting || !xt.scripting.executeScript) { resolve(false); return; }
    try {
      const out = xt.scripting.executeScript(
        { target: { tabId }, files: ["content.js"] },
        () => {
          void (xt.runtime && xt.runtime.lastError);
          resolve(true);
        }
      );
      if (out && typeof out.then === "function") {
        out.then(() => resolve(true)).catch(() => resolve(false));
      }
    } catch (_) {
      resolve(false);
    }
  });
}

function normalizeServer(raw) {
  let s = (raw || "").trim();
  if (!s) return DEFAULT_SERVER;
  if (!/^https?:\/\//i.test(s)) s = "http://" + s;
  return s.replace(/\/+$/, "");
}

// ---------------------------------------------------------------------
// connection status
// ---------------------------------------------------------------------

async function refreshStatus(server) {
  const line = $("status-line");
  const detail = $("status-detail");
  line.className = "err";
  line.textContent = "checking…";
  detail.textContent = "";
  let resp;
  try {
    resp = await fetch(`${server}/api/extension/status`);
  } catch (_) {
    line.textContent = "OFFLINE — app not reachable";
    detail.textContent = `Start the app, then reopen this popup. (${server})`;
    return;
  }
  let body = {};
  try { body = await resp.json(); } catch (_) {}
  const data = (body && body.data) || {};
  if (resp.ok && body.ok) {
    line.className = "ok";
    const who = data.profile_name ? ` — ${data.profile_name}` : " — no profile name set";
    line.textContent = `CONNECTED${who}`;
    const c = data.counts || {};
    detail.textContent =
      `v${data.version || "?"} · jobs:${c.jobs ?? 0} claims:${c.claims ?? 0} ` +
      `resumes:${c.tailored_resumes ?? 0} letters:${c.cover_letters ?? 0}`;
  } else {
    line.textContent = `ERROR — HTTP ${resp.status}`;
    detail.textContent = (body && body.detail) || "";
  }
}

// ---------------------------------------------------------------------
// scan & autofill
// ---------------------------------------------------------------------

async function autofill() {
  const btn = $("autofill-btn");
  btn.disabled = true;
  try {
    const server = normalizeServer($("server-url").value);
    const tab = await getActiveTab();
    if (!tab || !tab.id) { showToast("No active tab.", "err"); return; }
    const url = tab.url || "";
    if (!/^https?:\/\//i.test(url)) {
      showToast("This page can't be autofilled (not an http/https page).", "err");
      return;
    }

    let resp;
    try {
      resp = await fetch(`${server}/api/extension/fill-data?url=${encodeURIComponent(url)}`);
    } catch (_) {
      showToast(`Could not reach ${server}. Is the app running?`, "err");
      return;
    }
    let body = {};
    try { body = await resp.json(); } catch (_) {}
    if (!resp.ok || !body.ok) {
      showToast(`Fill-data failed: ${(body && body.detail) || `HTTP ${resp.status}`}`, "err");
      return;
    }
    const payload = body.data || {};

    const injected = await injectContentScript(tab.id);
    if (!injected) {
      showToast("Could not inject the autofill panel into this page.", "err");
      return;
    }
    const result = await sendTabMessage(tab.id, { type: "JHH_SHOW_PANEL", payload });
    if (result && result.ok) {
      const n = result.fields_detected ?? 0;
      const jobNote = payload.job ? ` Matched: ${payload.job.company || "?"}.` : " No job match.";
      showToast(`Panel opened — ${n} field(s) detected.${jobNote}`, "ok");
    } else {
      showToast("Panel injection failed on this page.", "err");
    }
  } finally {
    btn.disabled = false;
  }
}

// ---------------------------------------------------------------------
// save page to vault (legacy capture feature)
// ---------------------------------------------------------------------

async function saveURL() {
  const btn = $("save-btn");
  btn.disabled = true;
  try {
    const url = $("current-url").textContent.trim();
    const server = normalizeServer($("server-url").value);
    if (!url || url === "(loading)" || url === "(no URL on this tab)") {
      showToast("No active tab URL", "err");
      return;
    }
    let resp;
    try {
      resp = await fetch(`${server}/api/evidence/url`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url }),
      });
    } catch (_) {
      showToast(`Could not reach ${server}. Is the app running?`, "err");
      return;
    }
    let data = {};
    try { data = await resp.json(); } catch (_) {}
    if (resp.ok && (data.ok === undefined || data.ok)) {
      showToast("Saved to Job Hunt Hacker.", "ok");
    } else {
      showToast(`Save failed: ${(data && data.detail) || `HTTP ${resp.status}`}`, "err");
    }
  } finally {
    btn.disabled = false;
  }
}

// ---------------------------------------------------------------------
// init
// ---------------------------------------------------------------------

async function init() {
  const [server, tab] = await Promise.all([getStoredServer(), getActiveTab()]);
  $("server-url").value = server;
  $("open-app").href = server;
  $("current-url").textContent = (tab && tab.url) || "(no URL on this tab)";

  refreshStatus(server);

  $("autofill-btn").addEventListener("click", autofill);
  $("save-btn").addEventListener("click", saveURL);
  $("save-settings").addEventListener("click", async () => {
    const value = normalizeServer($("server-url").value);
    $("server-url").value = value;
    const ok = await setStoredServer(value);
    $("open-app").href = value;
    showToast(ok ? "Server URL saved." : "Could not save settings.", ok ? "ok" : "err");
    refreshStatus(value);
  });
}

document.addEventListener("DOMContentLoaded", init);
