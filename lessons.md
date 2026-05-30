# lessons.md — hard-won corrections, things to never forget

Read this before every job-hunt-hacker session. These are the lessons that produce different output from a naive "AI resume tool".

---

## 1. Never fabricate. Ever.

The single most damaging failure mode in AI-assisted job hunting is generating bullets the candidate cannot back up in interview. Recruiters detect mismatch between the resume and the screen within ten minutes; once detected, the candidate is permanently labeled. The cost of one fabricated metric outweighs ten honest gaps.

**The rule:** every output segment maps to ≥1 EvidenceSource ID via `provenance`. If `claim_verifier.verify_against_evidence()` returns False, the segment is dropped, period. The honesty report surfaces the dropped segment so the user knows what *could* have been said if they had evidence for it.

This is why guardrails are non-optional and the LLM prompts hammer the no-fabrication rule three times.

---

## 2. Tailored ≠ keyword-stuffed.

ATS bots are dumber than the recruiters who read what comes through. But the recruiter is the actual gatekeeper. A resume that scores 100/100 on keyword density and reads like a thesaurus vomit will be flagged as bot-generated and skipped.

**The rule:** every keyword added must (a) appear in the job posting, AND (b) be backed by evidence in the user's vault. The `keyword_classifier` enforces this — `unsupported` keywords are excluded even if they would boost ATS score. The honesty report surfaces them as gaps, and the gap report is the user's roadmap for closing them through real work.

---

## 3. LinkedIn rate-limits scraping at ~10 pages.

JobSpy's LinkedIn adapter without proxies will return zero results after a few searches. The README documents this. Do not chase the limit. For LinkedIn coverage, suggest the user paste their saved search URL → we render an assisted-apply packet and they click through.

**The rule:** never silently degrade. If LinkedIn returns zero, surface "LinkedIn rate-limited — try Indeed/Glassdoor or use proxies." in the search response. Do not hide the failure.

---

## 4. Indeed is the most reliable scrape source.

If you have to pick one site to lean on for volume, it's Indeed. JobSpy documents it as the most reliable, with no rate limiting. Glassdoor is second-most reliable, Google Jobs third, LinkedIn last.

---

## 5. Greenhouse / Lever / Ashby are gold and underused.

Most candidates ignore the official ATS public APIs because they're per-company. But they're the cleanest job data on the planet: structured, current, no rate limits, no TOS issues, no scraping needed. The seed list of curated companies (Stripe, Anthropic, Plaid, Linear, Replicate, etc.) covers a high-quality slice of the market. **Prefer these results when scoring** — give them a small freshness boost.

---

## 6. Salary signal is noisier than it looks.

`$120k-$160k` is easy. But:
- `$75/hr` → annualize at 2080 hrs only if it's a salaried full-time post; many `/hr` posts are contract and the equivalent annual is misleading.
- `Up to $200,000` → cap, not floor; `$200k` is the max not min. Don't promise the user $200k.
- Equity-heavy startups often post `$130k + 0.05% equity` — surface the equity to the user separately, never inflate the cash number with imagined equity value.
- European posts often miss the currency symbol; assume EUR if location is in EU, GBP if UK, USD otherwise.

**The rule:** when `salary_parser` is uncertain, surface both numbers AND mark `currency_unsure` in the keyword matrix.

---

## 7. Seniority mismatches eat months.

A Staff Engineer applying to a Senior role will be cap-rated and unmatched; a Senior Engineer applying to Staff will be screened out as under-leveled before the recruiter calls. The seniority signal in the job title is a hard filter for most recruiters.

**The rule:** if the detected job seniority is more than one level off the user's target seniority, score the job ≤ 0.5 on the seniority axis and surface "Seniority mismatch — your trajectory says X, this is Y" in the explanation. Don't silently penalize; tell the user why.

---

## 8. Cover letters are read by humans, not bots.

Two paragraphs maximum for most roles. Three for executive. Specific to the company and role. No "I am writing to express my interest" boilerplate. No "I am a results-driven professional with a passion for excellence" sludge. Lead with the strongest evidence-backed fit; close with a clear ask.

**The rule:** if the cover letter exceeds 250 words, compress. If the first sentence is generic, regenerate. The user can always expand.

---

## 9. Recruiter messages must be short.

≤120 words. Specific. No CV in the message. The goal is to earn a five-minute reply, not to make the case. Lead with the most relevant single fact, name the role, ask for a brief intro.

---

## 10. The contradiction detector matters more than you think.

If the user's old resume claims "Director of Engineering at Acme 2018-2022" and their LinkedIn export says "Senior Engineer at Acme 2018-2022", you have a credibility ticking bomb. Surface the contradiction; do not auto-resolve. The user picks which to keep and the system updates `allowed_for_resume` accordingly.

---

## 11. Auto-apply: never to LinkedIn/Indeed/Glassdoor.

Their TOS prohibit it; their detection is good; their consequence for a flagged account is permanent. The packet system prepares everything autonomously and queues for human review; the human clicks through to apply. This is the right architecture both ethically and operationally.

---

## 12. Provenance is shown, not just computed.

It is not enough to have provenance internally. The Resume Lab UI shows the provenance panel next to the resume; the user can click a bullet to see which evidence sources back it. This is the trust signal that lets the user actually send the resume.

---

## 13. The honesty report is the value, not the resume.

A polished resume from an AI is now a commodity. A polished resume + a precise list of "you said this; here's the evidence; here are the keywords we excluded because you don't have evidence; here's what's missing for this role" is a coaching tool. Lead with the honesty report when presenting tailored output.

---

## 14. Ingestion failures should be loud.

If `pdfminer` can't parse a PDF (scanned image), say so. If GitHub returns 403, surface the rate limit and recommend adding `GITHUB_TOKEN`. If LinkedIn paste has zero detected sections, ask the user to paste the "Experience" block specifically. Silent partial ingestion produces silent partial vaults produces dishonest tailoring.

---

## 15. The vector store is fallback-rich for a reason.

Order of preference: OpenAI text-embedding-3-small → sentence-transformers local → hashing TF-IDF. The hashing fallback is dumb but real — it lets the app run on a machine with zero API keys and zero ML deps. Don't make the vault dependent on ML infrastructure the user might not have.

---

## 16. Saved searches should be opinionated.

When the user creates a saved search, default `frequency_hours=24` (not 1, not 168). One scan per day is the right cadence for a serious search — frequent enough to catch fresh postings, infrequent enough to avoid noise.

---

## 17. The inbox classifier is heuristic, not LLM.

Recruiter email patterns are stable across years. A regex set catches 90%+ of rejection / interview / assessment / offer signals. The classifier should run offline, fast, deterministic. Save the LLM budget for tailoring.

---

## 18. Calendar slot suggestions respect work hours.

Default 9am-5pm in the user's timezone. Never suggest 11pm. Default minimum slot = 30min. Default buffer = 15min between events. Never auto-create the event; always show the user the suggested time and require approval.

---

## 19. The compliance banner is non-dismissible on the Auto-Apply page.

It stays at the top of the auto-apply tab forever. This is not over-cautious — it's how you survive the day a user's account gets restricted because they forgot the warning was there.

---

## 20. Share the result, not the secret.

The packet folder is shareable: resume.md, cover_letter.txt, manifest.json. The vault is private: `data/jhh.db`. Make this distinction obvious in the UI so users can hand a packet to a coach or friend without leaking their full career intelligence.
