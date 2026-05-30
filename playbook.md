# playbook.md — strategy by job-search archetype

Read this before tailoring or recommending. Different archetypes need different defaults.

---

## Archetype 1: Active urgent search (laid off, runway < 3 months)

**Posture:** volume + freshness over precision.

| Lever | Setting |
|---|---|
| Sites | indeed, glassdoor, google, greenhouse, lever, remotive, wwr (all) |
| Hours old | 72 (last 3 days) |
| Results per site | 50 |
| Min score | 65 |
| Daily cap on assisted-apply packets | 20 |
| Saved search frequency | 12h |
| Tailor depth | 2 styles per top job (one_page + technical) |
| Cover letter | Always — short version |
| Recruiter message | Always |
| Pipeline | Track every packet; close-loop on rejections fast |

Priority advice to the user: "Apply broad; iterate on what gets recruiter screens within 5 days. Re-tailor weekly based on signal."

---

## Archetype 2: Passive opportunistic

**Posture:** high precision, low volume.

| Lever | Setting |
|---|---|
| Sites | greenhouse, lever, ashby, remotive (high-quality only) |
| Hours old | 168 (1 week) |
| Results per site | 25 |
| Min score | 80 |
| Daily cap | 3 |
| Saved search frequency | 48h |
| Tailor depth | 1 deep packet per top job |
| Cover letter | Always — full version |
| Pipeline | Track 5 top opportunities; nurture intentionally |

Priority advice: "Only the top 5% should pull you out of your current role. Wait for the right one."

---

## Archetype 3: Career transition (e.g. backend eng → product manager, or non-tech → tech)

**Posture:** heavy keyword-gap reporting; transferable evidence weighting; explicit story.

| Lever | Setting |
|---|---|
| Sites | indeed, google, remotive |
| Min score | 55 (transition roles will score lower) |
| Keyword classifier weight | `transferable` heavily upweighted |
| Tailor style | `transition` — leads with skill-bridge summary, projects > roles |
| Cover letter | Always — long version; explicit narrative on the transition |
| Honesty report | Surface gap list as a learning roadmap, not as disqualification |

Priority advice: "Spend two weeks closing 1-2 of the top gap skills via a real project, then re-run. The vault will absorb the new evidence and tailored output will sharpen."

---

## Archetype 4: Level-up (senior → staff, or staff → principal)

**Posture:** seniority signal matters most; leadership accomplishments weighted x2.

| Lever | Setting |
|---|---|
| Seniority targets | Only one level up; never two |
| Tailor style | `leadership` — emphasize scope, scale, ownership, mentorship, system design |
| Resume sections | Add "Selected Projects" with architectural impact |
| Cover letter | Yes, long form — articulate the next-level scope you're ready for |
| Interview prep | Focus on system design + leadership scenarios |

Priority advice: "Level-up moves are won on demonstrated scope, not years. Ensure 2-3 evidence claims show org-wide impact."

---

## Archetype 5: Lateral specialist (deep stack match)

**Posture:** tool/platform match weighted x2; domain experience filter on.

| Lever | Setting |
|---|---|
| Required skills weight | x1.5 |
| Domain industries | Filtered tightly |
| Tailor style | `technical` — list specific systems, scale, performance numbers |
| Cover letter | Optional; the resume usually carries it |

Priority advice: "Lead with the platform/tool list — that's what hiring managers grep for."

---

## Archetype 6: Executive search

**Posture:** few jobs, deep packets, recruiter messages crafted; **never auto-apply**.

| Lever | Setting |
|---|---|
| Sites | Lever (exec-friendly), referrals (manual entry) |
| Min score | 85 |
| Tailor style | `executive` — narrative summary, board-level outcomes, P&L impact |
| Cover letter | Always — addressed to the specific person if known |
| Recruiter message | Always — assume the recruiter is a partner at a search firm |
| Auto-apply | DISABLED — exec hiring is relationship-driven |

Priority advice: "Quality of one packet > quantity of ten. Use the Inbox to track every conversation."

---

## Archetype 7: New grad / early career

**Posture:** internship + entry roles; portfolio/GitHub evidence weighted higher; willingness signals matter.

| Lever | Setting |
|---|---|
| Employment types | full-time, internship |
| Seniority | entry, intern |
| Evidence weight | GitHub repos + projects upweighted vs sparse work history |
| Tailor style | `project_heavy` |
| Cover letter | Always — convey curiosity and learning trajectory |

Priority advice: "Tell the story of three projects deeply; that beats a thin work-history list."

---

## Archetype 8: Contract / freelance

**Posture:** rate per hour visible; remote default; portfolio-first packets.

| Lever | Setting |
|---|---|
| Employment types | contract, freelance |
| Remote | true by default |
| Salary parser | Surface hourly rate prominently |
| Tailor style | `project_heavy` or `technical` |
| Cover letter | Short; rate range + availability windows |

Priority advice: "Lead with availability and rate; the technical fit is a closer."

---

## Decision flow

```
Did the user say "I just got laid off" / "runway < 3 months"?
  → Archetype 1 (Active urgent)

Is the user employed and casually exploring?
  → Archetype 2 (Passive opportunistic)

Is the user pivoting domain or function?
  → Archetype 3 (Transition)

Is the user >= Senior wanting next level?
  → Archetype 4 (Level-up)

Is the user deeply specialized and looking for same-stack match?
  → Archetype 5 (Lateral specialist)

Is the user a current/former Director+ / VP / C-level?
  → Archetype 6 (Executive)

Is the user in college / first-job-out?
  → Archetype 7 (New grad)

Is the user explicitly seeking contract work?
  → Archetype 8 (Freelance)
```

If unsure, default to Archetype 2 and ask the user to refine. Never assume urgent — that produces noisy output for someone who wanted quality.
