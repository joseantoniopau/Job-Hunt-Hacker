# Job Hunt Hacker — Browser Extension

A tiny Manifest V3 popup that sends the active tab's URL to your local
Job Hunt Hacker server's evidence pipeline. Useful for one-click capture
of job postings, blog posts about your work, conference talks, GitHub
projects, or anything else you want pulled into the Career Evidence
Vault.

## What it does

- Reads the current tab's URL via `activeTab`.
- POSTs `{"url": "..."}` to `<server>/api/evidence/url`.
- Lets you configure the local server URL (stored in `chrome.storage.local`).
- No tracking, no remote calls — only your local server.

Default server: `http://127.0.0.1:8731`.

---

## Load in Chrome / Edge / Brave

1. Start the Job Hunt Hacker app locally (the popup will hit it at
   `http://127.0.0.1:8731` by default):

   ```
   cd /path/to/Job-Hunt-Hacker
   ./run.sh
   ```

2. Open `chrome://extensions/`.
3. Toggle **Developer mode** on (top right).
4. Click **Load unpacked**.
5. Select this `extension/` directory.
6. Pin the new "Job Hunt Hacker — Save to Vault" extension to the toolbar.

You should now see the JHH icon. Click it on any page (e.g. a job
posting on LinkedIn) and hit **Save to Job Hunt Hacker**.

---

## Load in Firefox

Firefox supports MV3 but requires you to load the extension as a
"temporary add-on" during development (it's removed when you restart
the browser):

1. Open `about:debugging#/runtime/this-firefox`.
2. Click **Load Temporary Add-on…**
3. Pick **any file** inside this `extension/` directory (e.g. `manifest.json`).
4. The extension icon appears in the toolbar.

For permanent installation in Firefox, the extension needs to be
packaged and signed by Mozilla — out of scope here.

---

## Permissions explained

The extension requests only:

- `activeTab` — read the URL of the tab you're currently looking at.
  This is granted *per click*; the extension can't read your other tabs.
- `storage` — persist the server URL between popups.
- `host_permissions: http://127.0.0.1/*, http://localhost/*` — needed
  so the popup's `fetch()` to `127.0.0.1:8731` succeeds.

No remote network access is requested. The extension cannot reach the
internet — only your local Job Hunt Hacker server.

---

## Customizing the server URL

Click the extension icon → edit the **Local server** field → click
**Save server URL**. The new value is remembered across sessions via
`chrome.storage.local`.

Common values:

- `http://127.0.0.1:8731` — default
- `http://127.0.0.1:<your-port>` — if you launched JHH on a custom port
- `http://localhost:8731` — same thing, alternate hostname

If `host_permissions` in `manifest.json` doesn't cover your hostname,
add it there and reload the extension (`chrome://extensions/` → reload).

---

## Troubleshooting

- **"Could not reach <server>..."** — the JHH server isn't running, or
  it's on a port the manifest doesn't allow. Check the manifest's
  `host_permissions` block.
- **Save returns 422 / "no readable text at url"** — the page didn't
  render any text the readability extractor could parse (login wall,
  JS-only SPA, etc.). Save it as text manually via the JHH UI instead.
- **Save returns 401 / 403** — the JHH server is gated behind a bearer
  token. The extension does not currently support custom auth headers
  — disable token auth locally or extend `popup.js` with the right
  `Authorization` header.

---

## Files in this directory

| File          | Purpose                                                |
|---------------|--------------------------------------------------------|
| `manifest.json` | Manifest V3 declaration (name, version, permissions). |
| `popup.html`    | Popup UI shown when the toolbar icon is clicked.      |
| `popup.js`      | Talks to the local server + manages stored settings.  |
| `README.md`     | You are here.                                         |
