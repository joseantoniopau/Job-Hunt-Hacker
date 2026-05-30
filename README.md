# Job Hunt Hacker

**Find better jobs. Tailor honestly. Apply with discipline.**

Job Hunt Hacker is a full local-first career-search command center. It builds a verified **Career Evidence Vault** from your real career artifacts (resumes, LinkedIn export, GitHub, portfolio, docs), searches live job boards in parallel, scores every job against that evidence, and generates tailored resumes and cover letters that **never invent facts**. Every output bullet maps back to an evidence source ID — no hallucinated employers, no fake metrics, no skills you don't actually have.

Brutalist UI. Vanilla HTML/CSS/JS frontend. FastAPI backend. SQLite vault. Runs entirely on your laptop. Optional LLMs (Anthropic / OpenAI / Ollama). Optional Gmail / Google Calendar.

It is the third public skill in the *Hacker* series, after [Flight Hacker](https://github.com/joseantoniopau/flight_hacker) (cheapest flights, points-aware) and [Hotel Hacker](https://github.com/joseantoniopau/Hotel-Hacker) (cheapest stays, FHR-aware). Same disciplined philosophy: do the real math, surface the honest answer, no fluff.

---

## What it does

- **Career Evidence Vault** — ingest resumes (PDF/DOCX/TXT/MD/HTML), LinkedIn (paste or HTML), GitHub profile + repos, portfolio URLs, blog posts, performance reviews, certificates, project writeups. Extracts and normalizes career claims with provenance, confidence, and contradiction detection.
- **Live job search** — fans out across JobSpy (Indeed / Glassdoor / Google / LinkedIn / ZipRecruiter), Greenhouse, Lever, Ashby, Remotive, We Work Remotely, custom RSS, and Google Jobs via SerpApi. Deduplicates by company+title+location+month.
- **Honest scoring** — transparent weighted score (skills, experience, salary, location, seniority, keywords, evidence) with plain-English explanation per job. Weights configurable in Settings.
- **ATS Keyword Matrix** — for each job, classifies every keyword as `supported / transferable / weak / unsupported`. Only supported and honestly-transferable keywords appear in tailored resumes. Unsupported keywords go into the gap report, not the resume.
- **Resume tailoring (12 styles)** — master, one-page, two-page senior, technical, leadership, executive, project-heavy, transition, AI/ML, cybersecurity, engineering, product. Exports markdown, plain text (ATS-safe), DOCX, PDF.
- **Cover letters, recruiter messages, interview prep** — all evidence-grounded, all with provenance.
- **Application packets** — bundles resume + cover letter + recruiter message + interview prep + provenance.json + manifest.json into a single folder per job.
- **Pipeline tracker** — kanban board (Saved → Prepared → Applied → Replied → Interview → Offer / Rejected). Follow-up reminders.
- **Inbox monitoring** (optional Gmail OAuth or IMAP) — classifies recruiter replies (rejection / interview / assessment / offer), drafts responses (never auto-sends).
- **Calendar integration** (optional Google Calendar or ICS fallback) — manages interview availability windows, suggests slots, creates events on approval.
- **Saved searches + scheduler** — APScheduler runs configured searches on a cron, dedupes new jobs, scores them, surfaces a daily digest.
- **Auto-apply (off by default, heavily gated)** — even when enabled, *prepares packets autonomously*, never auto-submits to platforms. Explicit kill switch, daily cap, min-score floor, source allowlist.

## What it does NOT do

- Does not invent career facts. Ever.
- Does not bypass logins, CAPTCHAs, or anti-bot systems on job sites.
- Does not auto-submit applications to LinkedIn / Indeed / Glassdoor. Those platforms prohibit it and we respect that — assisted-apply only.
- Does not send emails on your behalf without confirmation.
- Does not scrape LinkedIn profiles. You paste / export your own.
- Does not require any paid services or API keys to run.

---

## The Career Evidence Vault — the core idea

Most resume tools start from a job posting and stretch your resume to fit. We start from **what is actually true about you** and only ever emphasize, reorganize, compress, expand, or translate that truth — never invent it.

Every claim in your vault has:
- `claim_text` — the literal evidence
- `source_id` — which document / URL / repo it came from
- `confidence` — how strongly the source supports it
- `user_verified` — you've reviewed and approved it
- `allowed_for_resume` — you've said it's OK to use

Every resume bullet, cover-letter paragraph, recruiter message, and interview talking point ships with a `provenance` map of `{segment_id → [evidence_id, ...]}`. The guardrails layer drops any segment without ≥1 supporting evidence ID. The honesty report shows you, per generated document, exactly which facts were used, which job-required keywords were excluded as unsupported, and what gaps the system identified.

If you ask the AI to "just add Kubernetes to my resume because the job needs it" and you have no Kubernetes evidence, it will refuse. Add a real project as evidence, or accept the gap.

---

## Five-minute install

Requirements: macOS or Linux, Python 3.10+.

```bash
git clone https://github.com/joseantoniopau/Job-Hunt-Hacker.git
cd Job-Hunt-Hacker
./install.sh
./run.sh
```

Then open **http://127.0.0.1:8731** in your browser.

The install script:
1. Installs Python dependencies (`fastapi`, `python-jobspy`, `pdfminer.six`, `python-docx`, `httpx`, `feedparser`, `apscheduler`, etc.).
2. Creates `data/`, `uploads/`, `resumes/`, `packets/`, `cache/`.
3. Copies `.env.example` → `.env`.
4. Seeds `data/seed/companies_remoteintech.json` (~880 remote-first companies).
5. Initializes the SQLite vault at `data/jhh.db`.
6. Symlinks the skill into `~/.claude/skills/job-hunt-hacker/` so Claude Code picks it up.

To add optional API keys interactively:

```bash
./setup-keys.sh
```

To uninstall:

```bash
./install.sh --uninstall
```

---

## First workflow (5 minutes)

1. Open http://127.0.0.1:8731.
2. **SETUP** — enter your target titles, location, salary floor, employment preferences. Upload your current resume (PDF / DOCX). Paste your LinkedIn profile text. Add your GitHub username. Add portfolio URL.
3. **VAULT** — review the extracted claims. Approve / disable / edit. Run contradiction scan.
4. **DASHBOARD** — search "Senior Python Engineer", Remote, sites=[indeed, google, remotive]. Hit Search.
5. Click the top result → review match, keyword matrix, evidence-backed fit.
6. Click **Build Packet** → resume + cover letter + recruiter message land in `packets/packet_<id>_<company>_<title>/`.
7. **PIPELINE** — drag the card from Saved → Prepared → Applied as you progress.

---

## Architecture

```
backend/
  app/
    main.py              FastAPI app, router loader, static UI mount
    config.py            .env → settings
    db.py                SQLite + schema migrations + audit log
    models/              Pydantic request schemas
    routers/             1 file per HTTP feature surface
    services/
      document_parser.py      PDF / DOCX / TXT / MD / HTML
      html_parser.py
      url_ingestion.py        Fetch + extract main content
      github_ingestion.py     GitHub REST API
      linkedin_ingestion.py   Parse pasted text / HTML export
      evidence_extractor.py   Rule + LLM hybrid claim extraction
      career_vault.py         Vault CRUD + semantic retrieval
      vector_store.py         SQLite + OpenAI / sentence-transformers / hashing fallback
      resume_parser.py        Structured resume parse
      claim_verifier.py       Tailoring guardrail: claim ↔ evidence match
      contradiction_detector.py
      job_sources/
        base.py               JobSourceAdapter + JobRecord + Registry
        jobspy_adapter.py     LinkedIn/Indeed/Glassdoor/Google/ZipRecruiter
        greenhouse_adapter.py Public Greenhouse boards API
        lever_adapter.py      Public Lever postings API
        ashby_adapter.py      Public Ashby boards API
        remotive_adapter.py
        weworkremotely_adapter.py  RSS
        google_jobs_adapter.py     SerpApi / SearchAPI
        remoteintech_adapter.py    ~880 remote-first companies
        custom_rss_adapter.py
        pipeline.py           Parallel fan-out + dedup + persist
    matching/
      scorer.py            Weighted overall score
      skills_extractor.py  ATS keyword set with aliases
      salary_parser.py
      location_parser.py
      seniority_parser.py
      keyword_classifier.py supported/transferable/weak/unsupported
      ats_analyzer.py      Keyword Matrix
    llm/
      base.py              LLMProvider abstract
      anthropic_provider.py
      openai_provider.py
      ollama_provider.py
      template_provider.py Always-available fallback
      prompts.py
      guardrails.py        No-fabrication enforcement
      json_repair.py
    tailoring/
      resume_tailor.py     12 styles
      cover_letter.py
      recruiter_messages.py
      interview_prep.py
      provenance.py
      honesty_report.py
    applications/
      packet_builder.py
      pipeline.py          Application CRUD + kanban
      assisted_apply.py
      auto_apply.py        Disabled by default; prepare-and-queue only
      compliance.py        Source allowlist + kill switch
    integrations/
      gmail.py             OAuth, draft-only
      imap.py              Fallback
      calendar_google.py
      ics.py
      scheduler.py         APScheduler
    utils/
      text.py
      exporters.py         Markdown / TXT / DOCX / PDF
ui/                       Vanilla brutalist SPA
  index.html              10 pages: Landing/Setup/Vault/Dashboard/Resume Lab/Pipeline/Inbox/Calendar/Settings
  styles.css
  app.js
docs/                     GitHub Pages landing
data/
  jhh.db                  SQLite vault
  seed/                   ats_keywords.json, seniority_signals.json, source_policies.json, scoring_weights_default.json, companies_remoteintech.json
scripts/
  smoke_test.py
  refresh_remoteintech.py
  search_jobs.py          CLI search
tests/                    pytest suite
.env.example              All optional env vars
install.sh, run.sh, setup-keys.sh
Dockerfile, docker-compose.yml
```

---

## Job sources

| Source | Type | Risk | Recommended Mode | Needs |
|---|---|---|---|---|
| JobSpy (Indeed/Glassdoor/Google/LinkedIn/ZipRecruiter) | Scrape | GRAY | Research / assisted | `python-jobspy` (auto). Proxies for LinkedIn at scale. |
| Greenhouse | Official API | LEGAL | Assisted | Public, no key |
| Lever | Official API | LEGAL | Assisted | Public, no key |
| Ashby | Official API | LEGAL | Assisted | Public, no key |
| Remotive | Official API | LEGAL | Assisted | Public, no key |
| We Work Remotely | RSS | LEGAL | Assisted | Public, no key |
| Google Jobs | SerpApi / SearchAPI | LEGAL | Assisted | `SERPAPI_API_KEY` or `SEARCHAPI_API_KEY` |
| Remote In Tech | Company directory | LEGAL | Research | Public, no key |
| Custom RSS | User-defined | LEGAL | Assisted | `data/custom_rss_feeds.json` |

LinkedIn / Indeed / Glassdoor automation violates platform TOS. Tailored packets are prepared regardless, but you click through to apply.

---

## LLM providers

The app works fully **without any LLM** via the deterministic `TemplateProvider` — resumes are still assembled from your real evidence, just with simpler wording.

With API keys:
- **Anthropic** (`ANTHROPIC_API_KEY`) — recommended for highest-quality resume tailoring and cover letters. Default model: `claude-sonnet-4-6`.
- **OpenAI** (`OPENAI_API_KEY`) — also works with any OpenAI-compatible endpoint via `OPENAI_BASE_URL` (e.g. point at `http://localhost:11434/v1` for Ollama).
- **Ollama** (`OLLAMA_BASE_URL`) — local-first, no cloud calls.

Order of preference is auto-detected; override with `JHH_LLM_PROVIDER=anthropic|openai|ollama|template`.

---

## Compliance & auto-apply

Auto-apply is **off by default** and even when enabled it only *prepares packets and queues them for your review*. It does not submit forms, click buttons, solve CAPTCHAs, bypass logins, or do anything that would violate a platform's TOS.

Enabling requires all of:
1. `JHH_AUTO_APPLY_ENABLED=true` in `.env`
2. Explicit confirmation in Settings (type the word `ENABLE` to confirm)
3. Source on the allowlist (only sources whose policy declares `apply_automation_allowed: true`)
4. Match score ≥ `JHH_AUTO_APPLY_MIN_SCORE` (default 85)
5. Daily cap not exhausted (default 5)
6. Kill switch not active (`POST /api/auto-apply/halt` toggles it on)

Every auto-apply attempt is audit-logged. The review queue lives at `GET /api/auto-apply/queue` and the Pipeline page.

---

## Privacy

- Everything is local-first. The SQLite vault lives in `data/jhh.db`. Uploaded files live in `uploads/`. Tailored resumes live in `resumes/`. Packets live in `packets/`.
- No telemetry. No analytics. No background uploads.
- When you configure an LLM API key, the contents of your evidence + job posts are sent to that provider as part of normal completion calls. Use Ollama if you want zero cloud egress.
- Delete all data: Settings → "Delete my data" (double-confirm, removes `data/jhh.db` + reinitializes empty).

---

## Roadmap

Phase 1 — scaffold, vault, ingestion, JobSpy/RemoteInTech, basic scoring. **Shipped.**
Phase 2 — LinkedIn / GitHub / portfolio ingestion, keyword matrix, Resume Lab. **Shipped.**
Phase 3 — Resume tailoring (12 styles), cover letters, provenance, honesty reports, exports. **Shipped.**
Phase 4 — Greenhouse / Lever / Ashby / Remotive / WWR / Google Jobs adapters. **Shipped.**
Phase 5 — Scheduler, saved searches, application pipeline, follow-up reminders. **Shipped.**
Phase 6 — Gmail / IMAP / Google Calendar / ICS integrations. **Shipped (OAuth setup required).**
Phase 7 — Optional local browser form-fill assistant for *eligible* platforms only, with explicit per-platform consent. **Future.**

---

## Contributing

PRs welcome — keep the brutalist visual discipline, never weaken the no-fabrication guardrails, and document any new dependency.

## License

MIT. See `LICENSE`.

## Acknowledgements

- [speedyapply/JobSpy](https://github.com/speedyapply/JobSpy) — MIT, used as the broad-board scrape adapter.
- [remoteintech/remote-jobs](https://github.com/remoteintech/remote-jobs) — ISC, used as the remote-first company seed list.
- Inspiration only (no code copied): [Resume-Matcher](https://github.com/srbhr/Resume-Matcher), [open-resume](https://github.com/xitanggg/open-resume), [jobs_applier_ai_agent_aihawk](https://github.com/feder-cr/jobs_applier_ai_agent_aihawk).

The system is designed by **José Antonio Pau**. Built as a public skill — share freely. The dignity of your job search is non-negotiable; this tool exists to give it some leverage.
