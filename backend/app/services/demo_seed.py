"""Onboarding demo mode — seed and wipe a clearly-fictional sample vault.

A first-run user sees an empty app; demo mode fills it with a fictional
profile ("Alex Rivera", Senior Product Manager, demo@example.invalid),
two demo evidence sources, ~15 hand-built claims with verbatim provenance
into those sources, six demo job postings (scored deterministically), and
two applications in different pipeline stages — so every screen has data.

Every row is tagged for exact cleanup:
  * evidence_source.source_type = 'demo'  (claims cascade via FK)
  * job_posting.source = 'demo'           (job_match / application /
                                           cover_letter / llm_job_score /
                                           gap_event cascade via FK)
  * profile fields are only reset on delete if their current value still
    equals the demo value we wrote, so user edits always survive.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import date, timedelta

from ..applications import pipeline
from ..db import audit, get_conn, tx
from ..matching import scorer
from . import career_vault, vector_store

log = logging.getLogger("jhh.demo_seed")

DEMO_SOURCE_TYPE = "demo"
DEMO_JOB_SOURCE = "demo"


class DemoSeedConflict(Exception):
    """Raised when seeding is requested but the vault already has data."""


# ---------------------------------------------------------------------------
# Fictional profile — every value is recognizably fake (.invalid TLD, 555
# phone). Lists are stored as JSON text, matching the profile router.
# ---------------------------------------------------------------------------

DEMO_PROFILE: dict[str, object] = {
    "name": "Alex Rivera",
    "email": "demo@example.invalid",
    "phone": "+1 (555) 010-0199",
    "location": "Austin, TX",
    "linkedin_url": "https://www.linkedin.example.invalid/in/alex-rivera-demo",
    "target_titles": json.dumps(
        ["Senior Product Manager", "Product Manager", "Group Product Manager"]
    ),
    "target_keywords": json.dumps(
        ["product strategy", "roadmap", "A/B testing", "SQL",
         "user research", "analytics", "usage-based pricing"]
    ),
    "preferred_locations": json.dumps(["Austin, TX", "Remote"]),
    "remote_preference": "remote",
    "minimum_salary": 150000,
    "preferred_salary": 175000,
    "seniority_targets": json.dumps(["senior"]),
}


# ---------------------------------------------------------------------------
# Demo evidence sources — realistic resume + LinkedIn pastes. Every demo
# claim's claim_text below appears VERBATIM in one of these blobs so the
# provenance chain (claim -> source span) holds up to inspection.
# ---------------------------------------------------------------------------

DEMO_RESUME_TITLE = "Demo Resume — Alex Rivera (Senior Product Manager)"
DEMO_RESUME_TEXT = """\
Alex Rivera
Senior Product Manager
Austin, TX · demo@example.invalid · +1 (555) 010-0199

SUMMARY
Senior Product Manager with 9 years of experience shipping B2B SaaS
products across analytics, billing, and growth. Known for pairing
rigorous data analysis with crisp stakeholder communication.

EXPERIENCE
Senior Product Manager — Northwind Analytics (Mar 2021 - Present)
- Led roadmap and delivery for a self-serve analytics platform serving 40,000 monthly active users.
- Grew annual recurring revenue from $8M to $21M in two years by repositioning the pricing model around usage tiers.
- Shipped an embedded dashboards product that closed 12 enterprise deals worth $4.2M in the first year.
- Cut onboarding time from 14 days to 3 days by leading a cross-functional activation squad of 8 engineers and 2 designers.

Product Manager — Contoso Cloud (Jun 2017 - Feb 2021)
- Owned the billing and subscriptions platform processing $120M in annual payment volume.
- Reduced involuntary churn by 18% by introducing smart dunning and card-retry logic.
- Launched usage-based pricing across 3 product lines in partnership with finance and sales engineering.

Associate Product Manager — Fabrikam Software (Jul 2015 - May 2017)
- Ran an A/B testing program across the signup funnel, lifting trial-to-paid conversion by 22%.

SKILLS
Product strategy, roadmap planning, SQL, A/B testing, user research,
stakeholder management, agile delivery, Jira, Figma, Amplitude

EDUCATION
B.S. in Economics, University of Texas at Austin (2015)
Certified Scrum Product Owner (CSPO), 2018
"""

DEMO_LINKEDIN_TITLE = "Demo LinkedIn — Alex Rivera"
DEMO_LINKEDIN_TEXT = """\
Alex Rivera
Senior Product Manager at Northwind Analytics
Austin, Texas, United States · 500+ connections

About
Product leader focused on analytics and monetization. I turn ambiguous
problems into shipped products that customers pay for.
Mentored 4 associate product managers, two of whom were promoted to product manager within 18 months.
Speaker at ProductCon Austin 2024 on usage-based pricing migrations.

Experience
Senior Product Manager
Northwind Analytics
Mar 2021 - Present
Leading the analytics platform group: 3 squads, 24 engineers, $21M ARR line.
Drove the launch of real-time alerting, adopted by 60% of enterprise accounts within two quarters.

Product Manager
Contoso Cloud
Jun 2017 - Feb 2021
Partnered with data science to build a churn-risk model that prioritized save offers for at-risk accounts.
"""


def _demo_claims(resume_sid: int, linkedin_sid: int) -> list[tuple[int, dict]]:
    """Hand-built, deterministic claim rows. Each claim_text is a verbatim
    substring of its source text (checked by tests), so provenance is real.
    Returns (source_id, claim_dict) pairs ready for career_vault.add_claims.
    """
    def c(sid: int, claim_type: str, text: str, *, skill: str | None = None,
          employer: str | None = None, date_start: str | None = None,
          date_end: str | None = None, strength: str = "strong",
          confidence: float = 0.9) -> tuple[int, dict]:
        return (sid, {
            "claim_type": claim_type,
            "claim_text": text,
            "skill": skill,
            "employer": employer,
            "date_start": date_start,
            "date_end": date_end,
            "confidence": confidence,
            "evidence_strength": strength,
            "user_verified": True,
            "allowed_for_resume": True,
        })

    r, li = resume_sid, linkedin_sid
    return [
        # --- roles (resume) ---
        c(r, "role", "Senior Product Manager — Northwind Analytics (Mar 2021 - Present)",
          employer="Northwind Analytics", date_start="Mar 2021"),
        c(r, "role", "Product Manager — Contoso Cloud (Jun 2017 - Feb 2021)",
          employer="Contoso Cloud", date_start="Jun 2017", date_end="Feb 2021"),
        c(r, "role", "Associate Product Manager — Fabrikam Software (Jul 2015 - May 2017)",
          employer="Fabrikam Software", date_start="Jul 2015", date_end="May 2017"),
        # --- accomplishments (resume) ---
        c(r, "accomplishment",
          "Grew annual recurring revenue from $8M to $21M in two years by repositioning the pricing model around usage tiers.",
          employer="Northwind Analytics"),
        c(r, "accomplishment",
          "Shipped an embedded dashboards product that closed 12 enterprise deals worth $4.2M in the first year.",
          employer="Northwind Analytics"),
        c(r, "accomplishment",
          "Cut onboarding time from 14 days to 3 days by leading a cross-functional activation squad of 8 engineers and 2 designers.",
          employer="Northwind Analytics"),
        c(r, "accomplishment",
          "Reduced involuntary churn by 18% by introducing smart dunning and card-retry logic.",
          employer="Contoso Cloud"),
        c(r, "accomplishment",
          "Ran an A/B testing program across the signup funnel, lifting trial-to-paid conversion by 22%.",
          employer="Fabrikam Software", skill="A/B testing"),
        # --- responsibility (resume) ---
        c(r, "responsibility",
          "Owned the billing and subscriptions platform processing $120M in annual payment volume.",
          employer="Contoso Cloud", strength="medium"),
        # --- skills (resume — tokens appear verbatim in the SKILLS line) ---
        c(r, "skill", "SQL", skill="SQL", strength="medium", confidence=0.8),
        c(r, "skill", "A/B testing", skill="A/B testing", strength="medium", confidence=0.8),
        c(r, "skill", "user research", skill="user research", strength="medium", confidence=0.8),
        c(r, "skill", "Amplitude", skill="Amplitude", strength="medium", confidence=0.8),
        # --- education / certification (resume) ---
        c(r, "education", "B.S. in Economics, University of Texas at Austin (2015)"),
        c(r, "certification", "Certified Scrum Product Owner (CSPO), 2018"),
        # --- linkedin ---
        c(li, "accomplishment",
          "Mentored 4 associate product managers, two of whom were promoted to product manager within 18 months."),
        c(li, "accomplishment",
          "Speaker at ProductCon Austin 2024 on usage-based pricing migrations."),
        c(li, "responsibility",
          "Leading the analytics platform group: 3 squads, 24 engineers, $21M ARR line.",
          employer="Northwind Analytics", strength="medium"),
        c(li, "accomplishment",
          "Drove the launch of real-time alerting, adopted by 60% of enterprise accounts within two quarters.",
          employer="Northwind Analytics"),
        c(li, "accomplishment",
          "Partnered with data science to build a churn-risk model that prioritized save offers for at-risk accounts.",
          employer="Contoso Cloud"),
    ]


# ---------------------------------------------------------------------------
# Demo jobs — product-management flavored so scoring against the demo
# claims produces meaningful (non-zero) matches.
# ---------------------------------------------------------------------------

DEMO_JOBS: list[dict] = [
    {
        "external_id": "demo-1",
        "title": "Senior Product Manager, Analytics Platform",
        "company": "Lakeshore Data",
        "location": "Remote (US)",
        "remote_type": "remote",
        "employment_type": "full_time",
        "salary_min": 160000, "salary_max": 190000,
        "description": (
            "Lakeshore Data builds self-serve analytics for mid-market SaaS "
            "companies. You will own the roadmap for our dashboards and "
            "embedded analytics product line, partnering with 3 engineering "
            "squads. We expect strong SQL, a habit of A/B testing, and a "
            "track record of growing recurring revenue through pricing and "
            "packaging work. You will run user research and translate it "
            "into a quarterly product strategy."
        ),
        "requirements": ["6+ years product management", "SQL", "A/B testing",
                         "analytics products", "user research"],
    },
    {
        "external_id": "demo-2",
        "title": "Senior Product Manager, Monetization",
        "company": "Brightpath SaaS",
        "location": "Austin, TX (hybrid)",
        "remote_type": "hybrid",
        "employment_type": "full_time",
        "salary_min": 155000, "salary_max": 185000,
        "description": (
            "Own billing, subscriptions, and usage-based pricing at "
            "Brightpath. You will reduce involuntary churn, design dunning "
            "flows, and launch new pricing tiers with finance and sales "
            "engineering. Experience with payment platforms processing "
            "significant annual volume is required, plus comfort with SQL "
            "and experimentation."
        ),
        "requirements": ["billing/subscriptions experience", "usage-based pricing",
                         "churn reduction", "SQL", "stakeholder management"],
    },
    {
        "external_id": "demo-3",
        "title": "Group Product Manager, Data Products",
        "company": "Meridian Cloud",
        "location": "Remote (US)",
        "remote_type": "remote",
        "employment_type": "full_time",
        "salary_min": 185000, "salary_max": 220000,
        "description": (
            "Lead a group of three product managers building Meridian's "
            "data platform: real-time alerting, dashboards, and a metrics "
            "layer. You will set product strategy, mentor PMs, and be "
            "accountable for a $20M+ ARR line. Strong analytics background "
            "and prior people mentorship required."
        ),
        "requirements": ["8+ years product management", "people mentorship",
                         "data/analytics platforms", "product strategy"],
    },
    {
        "external_id": "demo-4",
        "title": "Product Manager, Growth",
        "company": "Sunburst Apps",
        "location": "Austin, TX",
        "remote_type": "onsite",
        "employment_type": "full_time",
        "salary_min": 135000, "salary_max": 160000,
        "description": (
            "Run Sunburst's activation and conversion funnel. You will own "
            "the A/B testing program end to end, lift trial-to-paid "
            "conversion, and cut onboarding time for new workspaces. "
            "Hands-on SQL and Amplitude experience expected."
        ),
        "requirements": ["growth experimentation", "A/B testing", "SQL",
                         "Amplitude", "onboarding funnels"],
    },
    {
        "external_id": "demo-5",
        "title": "Senior Product Manager, Platform",
        "company": "Quartz Systems",
        "location": "New York, NY",
        "remote_type": "onsite",
        "employment_type": "full_time",
        "salary_min": 170000, "salary_max": 200000,
        "description": (
            "Quartz is hiring a platform PM to own internal APIs and the "
            "billing integration layer. You will work with 24 engineers "
            "across 4 squads, write crisp specs, and drive agile delivery. "
            "Prior experience with subscriptions platforms and enterprise "
            "deals is a plus."
        ),
        "requirements": ["platform/API products", "agile delivery",
                         "billing integrations", "enterprise sales support"],
    },
    {
        "external_id": "demo-6",
        "title": "Principal Product Manager, Pricing",
        "company": "Helios Robotics",
        "location": "Remote (US)",
        "remote_type": "remote",
        "employment_type": "full_time",
        "salary_min": 190000, "salary_max": 230000,
        "description": (
            "Helios needs a principal PM to lead the migration from seat "
            "pricing to usage-based pricing across three product lines. "
            "You will model revenue impact in SQL, partner with finance, "
            "and present the strategy to the executive team. Public "
            "speaking on pricing topics is a plus."
        ),
        "requirements": ["pricing strategy", "usage-based pricing", "SQL",
                         "executive communication"],
    },
]

# (job external_id, application status) — two pipeline stages.
DEMO_APPLICATIONS: list[tuple[str, str]] = [
    ("demo-1", "applied"),
    ("demo-3", "interview"),
]


# ---------------------------------------------------------------------------
# State checks
# ---------------------------------------------------------------------------

def vault_is_empty() -> bool:
    """'Effectively empty' = zero evidence sources AND no user-entered
    profile name. Job postings from searches don't count — demo mode is
    about the vault/profile onboarding surface.
    """
    conn = get_conn()
    n_sources = int(conn.execute(
        "SELECT COUNT(*) FROM evidence_source").fetchone()[0])
    row = conn.execute("SELECT name FROM user_profile WHERE id = 1").fetchone()
    has_name = bool(row and (row[0] or "").strip())
    return n_sources == 0 and not has_name


def demo_status() -> dict:
    """Snapshot of demo rows currently present.

    Response: {active, sources, claims, jobs, applications}
    """
    conn = get_conn()
    sources = int(conn.execute(
        "SELECT COUNT(*) FROM evidence_source WHERE source_type = ?",
        (DEMO_SOURCE_TYPE,)).fetchone()[0])
    claims = int(conn.execute(
        "SELECT COUNT(*) FROM career_claim WHERE source_id IN "
        "(SELECT id FROM evidence_source WHERE source_type = ?)",
        (DEMO_SOURCE_TYPE,)).fetchone()[0])
    jobs = int(conn.execute(
        "SELECT COUNT(*) FROM job_posting WHERE source = ?",
        (DEMO_JOB_SOURCE,)).fetchone()[0])
    apps = int(conn.execute(
        "SELECT COUNT(*) FROM application WHERE job_id IN "
        "(SELECT id FROM job_posting WHERE source = ?)",
        (DEMO_JOB_SOURCE,)).fetchone()[0])
    return {
        "active": bool(sources or jobs),
        "sources": sources,
        "claims": claims,
        "jobs": jobs,
        "applications": apps,
    }


# ---------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------

def _seed_profile() -> list[str]:
    """Fill profile fields that are currently empty with demo values.
    Returns the list of fields we actually set (audited; delete only resets
    fields whose value still equals our demo value)."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM user_profile WHERE id = 1").fetchone()
    current = dict(row) if row else {}
    sets: list[str] = []
    vals: list = []
    for field, value in DEMO_PROFILE.items():
        cur_val = current.get(field)
        if cur_val is None or (isinstance(cur_val, str) and not cur_val.strip()):
            sets.append(field)
            vals.append(value)
    if not sets:
        return []
    sql = ("UPDATE user_profile SET "
           + ", ".join(f"{f} = ?" for f in sets)
           + ", updated_at = ? WHERE id = 1")
    vals.append(time.time())
    with tx() as c:
        c.execute(sql, vals)
    return sets


def _insert_demo_jobs() -> list[int]:
    now = time.time()
    today = date.today()
    job_ids: list[int] = []
    with tx() as c:
        for i, job in enumerate(DEMO_JOBS):
            h = hashlib.sha256(
                f"demo:{job['external_id']}".encode("utf-8")).hexdigest()
            cur = c.execute(
                "INSERT INTO job_posting (external_id, source, title, company, "
                "location, remote_type, employment_type, salary_min, salary_max, "
                "currency, description, requirements, apply_url, posted_at, "
                "discovered_at, raw_json, hash, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job["external_id"], DEMO_JOB_SOURCE, job["title"],
                    job["company"], job["location"], job["remote_type"],
                    job["employment_type"], job["salary_min"], job["salary_max"],
                    "USD", job["description"], json.dumps(job["requirements"]),
                    f"https://jobs.example.invalid/{job['external_id']}",
                    (today - timedelta(days=i)).isoformat(),
                    now, json.dumps({"demo": True}), h, "new",
                ),
            )
            job_ids.append(int(cur.lastrowid))
    return job_ids


def seed_demo() -> dict:
    """Populate the demo vault. Raises DemoSeedConflict unless the vault is
    effectively empty (no evidence sources, no user-entered profile name).

    Returns: {profile_fields_set, source_ids, claims_inserted, job_ids,
              jobs_scored, score_errors, application_ids}
    """
    if not vault_is_empty():
        raise DemoSeedConflict(
            "vault already has data (evidence sources or a profile name) — "
            "demo mode only seeds an empty vault"
        )

    # 1) Profile
    profile_fields_set = _seed_profile()

    # 2) Evidence sources (source_type='demo')
    resume_sid = career_vault.add_source(
        DEMO_SOURCE_TYPE,
        title=DEMO_RESUME_TITLE,
        raw_text=DEMO_RESUME_TEXT,
        parsed_json={"demo": True, "kind": "resume-text"},
    )
    linkedin_sid = career_vault.add_source(
        DEMO_SOURCE_TYPE,
        title=DEMO_LINKEDIN_TITLE,
        raw_text=DEMO_LINKEDIN_TEXT,
        parsed_json={"demo": True, "kind": "linkedin-text"},
    )

    # 3) Claims with provenance to those sources (deterministic, hand-built)
    by_source: dict[int, list[dict]] = {}
    for sid, claim in _demo_claims(resume_sid, linkedin_sid):
        by_source.setdefault(sid, []).append(claim)
    claim_ids: list[int] = []
    for sid, claims in by_source.items():
        claim_ids.extend(career_vault.add_claims(sid, claims))

    # 4) Jobs + deterministic scoring (no LLM polish)
    job_ids = _insert_demo_jobs()
    scored = 0
    score_errors: list[str] = []
    for jid in job_ids:
        try:
            scorer.score_job(jid, llm_polish=False)
            scored += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("demo job %s scoring failed: %s", jid, exc)
            score_errors.append(f"job {jid}: {type(exc).__name__}: {exc}")

    # 5) Applications in two different pipeline stages
    ext_to_id = {DEMO_JOBS[i]["external_id"]: job_ids[i]
                 for i in range(len(DEMO_JOBS))}
    application_ids: list[int] = []
    for ext, status in DEMO_APPLICATIONS:
        app_id = pipeline.create_application(
            ext_to_id[ext], status=status, mode="demo",
            notes="Demo application — seeded by onboarding demo mode.",
        )
        application_ids.append(int(app_id))

    result = {
        "profile_fields_set": profile_fields_set,
        "source_ids": [int(resume_sid), int(linkedin_sid)],
        "claims_inserted": len(claim_ids),
        "job_ids": job_ids,
        "jobs_scored": scored,
        "score_errors": score_errors,
        "application_ids": application_ids,
    }
    audit("demo_seed", "vault", None, **result)
    return result


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

def _reset_demo_profile_fields() -> list[str]:
    """NULL out profile fields whose value still equals the demo value we
    wrote — user-edited fields are left alone."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM user_profile WHERE id = 1").fetchone()
    if row is None:
        return []
    current = dict(row)
    resets: list[str] = []
    for field, demo_val in DEMO_PROFILE.items():
        if current.get(field) == demo_val:
            resets.append(field)
    if not resets:
        return []
    sql = ("UPDATE user_profile SET "
           + ", ".join(f"{f} = NULL" for f in resets)
           + ", updated_at = ? WHERE id = 1")
    with tx() as c:
        c.execute(sql, (time.time(),))
    return resets


def delete_demo() -> dict:
    """Wipe exactly the demo rows. Idempotent — returns zero counts when
    nothing demo-tagged exists.

    Returns: {sources_deleted, claims_deleted, jobs_deleted,
              applications_deleted, profile_fields_reset}
    """
    conn = get_conn()

    # Collect ids before deleting so embedding cleanup is exact.
    source_ids = [int(r[0]) for r in conn.execute(
        "SELECT id FROM evidence_source WHERE source_type = ?",
        (DEMO_SOURCE_TYPE,)).fetchall()]
    claim_ids = [int(r[0]) for r in conn.execute(
        "SELECT id FROM career_claim WHERE source_id IN "
        "(SELECT id FROM evidence_source WHERE source_type = ?)",
        (DEMO_SOURCE_TYPE,)).fetchall()]
    job_ids = [int(r[0]) for r in conn.execute(
        "SELECT id FROM job_posting WHERE source = ?",
        (DEMO_JOB_SOURCE,)).fetchall()]
    app_count = int(conn.execute(
        "SELECT COUNT(*) FROM application WHERE job_id IN "
        "(SELECT id FROM job_posting WHERE source = ?)",
        (DEMO_JOB_SOURCE,)).fetchone()[0])

    with tx() as c:
        # career_claim cascades from evidence_source (FK ON DELETE CASCADE);
        # job_match / application / cover_letter / gap_event / llm_job_score
        # cascade from job_posting.
        c.execute("DELETE FROM evidence_source WHERE source_type = ?",
                  (DEMO_SOURCE_TYPE,))
        c.execute("DELETE FROM job_posting WHERE source = ?",
                  (DEMO_JOB_SOURCE,))

    # Embedding rows have no FK — remove them explicitly.
    for sid in source_ids:
        try:
            vector_store.remove("evidence", sid)
        except Exception:  # noqa: BLE001
            pass
    for cid in claim_ids:
        try:
            vector_store.remove("claim", cid)
        except Exception:  # noqa: BLE001
            pass

    profile_fields_reset = _reset_demo_profile_fields()

    result = {
        "sources_deleted": len(source_ids),
        "claims_deleted": len(claim_ids),
        "jobs_deleted": len(job_ids),
        "applications_deleted": app_count,
        "profile_fields_reset": profile_fields_reset,
    }
    audit("demo_delete", "vault", None, **result)
    return result
