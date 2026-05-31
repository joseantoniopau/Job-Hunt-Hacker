/* Job Hunt Hacker — browser extension popup.
 *
 * On open: shows the current tab URL + the configured local server URL
 * (default http://127.0.0.1:8731, stored in chrome.storage.local).
 *
 * On "Save" click: POSTs {url} to <server>/api/evidence/url and shows a
 * toast with the result. The local server's evidence ingester takes it
 * from there (fetches the page, extracts text, runs claim extraction).
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
    if (!xt || !xt.storage || !xt.storage.local) {
      resolve(DEFAULT_SERVER);
      return;
    }
    try {
      const out = xt.storage.local.get([STORAGE_KEY], (res) => {
        const v = (res && res[STORAGE_KEY]) || DEFAULT_SERVER;
        resolve(v);
      });
      // Firefox returns a Promise instead of using a callback.
      if (out && typeof out.then === "function") {
        out.then((res) => {
          const v = (res && res[STORAGE_KEY]) || DEFAULT_SERVER;
          resolve(v);
        }).catch(() => resolve(DEFAULT_SERVER));
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

function getActiveTabURL() {
  return new Promise((resolve) => {
    if (!xt || !xt.tabs || !xt.tabs.query) { resolve(""); return; }
    try {
      const out = xt.tabs.query({ active: true, currentWindow: true }, (tabs) => {
        resolve((tabs && tabs[0] && tabs[0].url) || "");
      });
      if (out && typeof out.then === "function") {
        out.then((tabs) => {
          resolve((tabs && tabs[0] && tabs[0].url) || "");
        }).catch(() => resolve(""));
      }
    } catch (_) {
      resolve("");
    }
  });
}

function normalizeServer(raw) {
  let s = (raw || "").trim();
  if (!s) return DEFAULT_SERVER;
  if (!/^https?:\/\//i.test(s)) s = "http://" + s;
  return s.replace(/\/+$/, "");
}

async function saveURL() {
  const btn = $("save-btn");
  btn.disabled = true;
  try {
    const url = $("current-url").textContent.trim();
    const server = normalizeServer($("server-url").value);
    if (!url || url === "(loading)") {
      showToast("No active tab URL", "err");
      return;
    }
    const endpoint = `${server}/api/evidence/url`;
    let resp;
    try {
      resp = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url }),
      });
    } catch (e) {
      showToast(`Could not reach ${server}. Is the app running?`, "err");
      return;
    }
    let data = {};
    try { data = await resp.json(); } catch (_) {}
    if (resp.ok && (data.ok === undefined || data.ok)) {
      showToast("Saved to Job Hunt Hacker.", "ok");
    } else {
      const detail = (data && data.detail) || `HTTP ${resp.status}`;
      showToast(`Save failed: ${detail}`, "err");
    }
  } finally {
    btn.disabled = false;
  }
}

async function init() {
  const [server, tabUrl] = await Promise.all([getStoredServer(), getActiveTabURL()]);
  $("server-url").value = server;
  $("current-url").textContent = tabUrl || "(no URL on this tab)";

  $("save-btn").addEventListener("click", saveURL);
  $("save-settings").addEventListener("click", async () => {
    const value = normalizeServer($("server-url").value);
    $("server-url").value = value;
    const ok = await setStoredServer(value);
    showToast(ok ? "Server URL saved." : "Could not save settings.", ok ? "ok" : "err");
  });
}

document.addEventListener("DOMContentLoaded", init);
