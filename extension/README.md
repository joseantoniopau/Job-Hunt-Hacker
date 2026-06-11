# Job Hunt Hacker — Autofill Assistant (browser extension)

A Manifest V3 extension that connects any job-application page to your
local Job Hunt Hacker vault:

- **Scan & autofill** — detects application-form fields on the current
  page (name, email, phone, LinkedIn, GitHub, website, location, cover
  letter, resume text, "why this company", experience summary) and opens
  a floating panel with per-field **FILL** buttons, **FILL ALL**,
  **COPY RESUME TEXT**, and **COPY COVER LETTER**. Values come from
  `GET /api/extension/fill-data` — your profile, the best-matching job
  in the vault, the latest tailored resume / cover letter for that job,
  and template-composed answers grounded ONLY in verified claims.
- **Save page to vault** — POSTs the current URL to
  `/api/evidence/url` for evidence ingestion (the original capture
  feature).
- **Connection status** — the popup pings `GET /api/extension/status`
  and shows whether the app is reachable + whose profile is loaded,
  with a link to open the app.

## Hard guarantees

- **NEVER auto-submits.** The content script has no code path that
  clicks submit buttons or calls `form.submit()`. It only writes a
  field's value (dispatching `input` + `change` events) when *you*
  click FILL.
- **Evidence-grounded.** Long-form answer drafts are composed from
  verbatim verified-claim text + facts on the job posting itself.
  An empty vault produces empty drafts — never invented prose.
- **Local-only network.** The only network destination is your local
  server (`http://127.0.0.1:8731`). The injected content script makes
  zero network calls — data reaches it via extension messaging.

## Files

| File            | Purpose                                                       |
|-----------------|---------------------------------------------------------------|
| `manifest.json` | MV3 declaration: `activeTab`, `storage`, `scripting`; host access only to `http://127.0.0.1:8731/*`. |
| `popup.html`    | Brutalist popup UI (status, autofill, save-to-vault, server URL). |
| `popup.js`      | Talks to the local server, injects `content.js` on demand.     |
| `content.js`    | Field-detection heuristics + floating autofill panel. No network. |

## Load in Chrome / Edge / Brave

1. Start the app:

   ```
   cd /path/to/Job-Hunt-Hacker
   ./run.sh
   ```

2. Open `chrome://extensions/`, toggle **Developer mode** on.
3. **Load unpacked** → select this `extension/` directory.
4. Pin "Job Hunt Hacker — Autofill Assistant" to the toolbar.

On any application page: click the icon → **SCAN & AUTOFILL THIS PAGE**.
The panel lists every detected field with what would be filled. Nothing
is written until you click FILL / FILL ALL, and nothing is ever
submitted for you.

## Load in Firefox

1. Open `about:debugging#/runtime/this-firefox`.
2. **Load Temporary Add-on…** → pick `manifest.json` in this directory.

(Temporary add-ons are removed on browser restart; permanent installs
require Mozilla signing.)

## Permissions explained

- `activeTab` — read the active tab's URL and allow injection into it,
  granted per click on the extension.
- `scripting` — inject `content.js` into the active tab when you click
  **Scan & autofill** (never automatically).
- `storage` — remember your server URL.
- `host_permissions: http://127.0.0.1:8731/*` — lets the popup `fetch()`
  your local server. No other host access is requested; the extension
  cannot reach the internet.

## Custom port

If you run JHH on a non-default port, set the server URL in the popup
**and** add the matching origin to `host_permissions` in
`manifest.json`, then reload the extension.

## Troubleshooting

- **OFFLINE — app not reachable**: the server isn't running, or it's on
  a port not covered by `host_permissions`.
- **Panel doesn't appear**: some pages (chrome://, the Web Store,
  PDF viewers) forbid script injection. Use COPY buttons from a normal
  tab, or paste manually from the app.
- **Fields detected but "(no data in vault)"**: fill in your profile in
  the app (Setup), upload a resume, or generate a tailored resume /
  cover letter for the matched job first.
- **401 / 403 from the server**: local bearer-token auth is enabled;
  the extension doesn't send auth headers — disable token auth locally.
