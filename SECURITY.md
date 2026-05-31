# Security Policy

Job Hunt Hacker runs on your laptop and stores your career history locally. This document explains the trust boundaries, threat model, and how to report issues.

## Threat model

The tool is built for **local-first single-user deployment**. Concretely:

1. **Anyone with shell access** to the machine can read `data/jhh.db`, the uploaded files in `uploads/`, the generated resumes in `resumes/`, and the packets in `packets/`. Use disk encryption (FileVault / LUKS / BitLocker).
2. **Anyone with network access** to the listening port (default `127.0.0.1:8731`) can do everything the user can: read the vault, run searches, build packets, change settings. If you ever bind to `0.0.0.0` or expose via a tunnel, set `JHH_AUTH_TOKEN` (see below).
3. **OAuth tokens** for Gmail / Google Calendar are stored in `data/oauth_tokens.json`. They are Fernet-encrypted only if the `cryptography` package is installed — otherwise plain JSON with file mode `0600`. Install `cryptography` for at-rest encryption.
4. **Your LLM provider** (Anthropic / OpenAI / Ollama) sees the contents of your evidence vault + every job description on every tailor call. Choose Ollama for zero cloud egress.
5. **The job-source adapters** make outbound requests to public job boards. JobSpy scrapes; the rest use official APIs.
6. **SSRF protection** is enabled on all URL-fetching endpoints (`/api/urls/preview`, `/api/evidence/url`, `/api/github/ingest`). They refuse non-http(s) schemes and any host resolving to loopback, link-local, RFC1918-private, multicast, or reserved address ranges.

## Authentication

Auth is **off by default** (local-first). To enable:

```bash
export JHH_AUTH_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
./run.sh
```

Once set, every `/api/*` request must include `Authorization: Bearer <token>`. The static UI bundle (`/`, `/styles.css`, `/app.js`) and `/api/health` stay open.

## Rate limiting

The hot endpoints (`/api/search`, `/api/autopilot/start`, `/api/profile/infer`, `/api/urls/preview`, `/api/resume/tailor`, `/api/cover-letter`) are rate-limited per-IP. The defaults block 10 requests / minute from any single source — enough for normal use, low enough to stop accidental F5 storms.

## Upload limits

File uploads are capped at `JHH_MAX_UPLOAD_MB` (default `10`). MIME type is validated against the file extension at the boundary. Larger or mismatched files return `413` / `415`.

## Auto-apply

Auto-apply is **disabled by default**. Enabling it requires:
1. `JHH_AUTO_APPLY_ENABLED=true` in `.env` OR clicking ENABLE in Settings (which also flips the runtime flag).
2. Source policy must allow it (LinkedIn / Indeed / Glassdoor never qualify — they're `GRAY` risk).
3. Match score ≥ `JHH_AUTO_APPLY_MIN_SCORE` (default 85 / 100).
4. Daily cap not exhausted (`JHH_AUTO_APPLY_DAILY_CAP`, default 5).
5. Kill switch not engaged (`POST /api/auto-apply/halt`).

Even when all gates pass, **auto-apply never submits applications**. It prepares packets and queues them in the review queue with `status=auto_packet_ready`. The user still clicks through to the platform.

## Reporting issues

Open a GitHub issue. For security-sensitive reports, prefer email to the address in the GitHub profile of the repository owner.

The tool is MIT-licensed; you are responsible for how you deploy it.
