"""All Pydantic schemas in one file — small enough to keep together."""
from __future__ import annotations

from typing import Any, Literal, Optional
from pydantic import BaseModel, Field


# ---------- profile ----------

class UserProfileIn(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    linkedin_url: Optional[str] = None
    github_url: Optional[str] = None
    portfolio_url: Optional[str] = None
    target_titles: list[str] = Field(default_factory=list)
    target_keywords: list[str] = Field(default_factory=list)
    excluded_keywords: list[str] = Field(default_factory=list)
    preferred_locations: list[str] = Field(default_factory=list)
    remote_preference: Optional[str] = None
    employment_types: list[str] = Field(default_factory=list)
    minimum_salary: Optional[int] = None
    preferred_salary: Optional[int] = None
    currency: str = "USD"
    seniority_targets: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    excluded_industries: list[str] = Field(default_factory=list)
    preferred_companies: list[str] = Field(default_factory=list)
    excluded_companies: list[str] = Field(default_factory=list)
    visa_preferences: list[str] = Field(default_factory=list)
    interview_availability_json: Optional[dict] = None
    scoring_weights_json: Optional[dict] = None
    mode: Optional[str] = None


# ---------- evidence ----------

class URLIngestRequest(BaseModel):
    url: str
    source_type: Optional[str] = None  # auto-detected if absent


class TextIngestRequest(BaseModel):
    title: str
    text: str
    source_type: str = "manual_paste"


class GitHubIngestRequest(BaseModel):
    profile_url: Optional[str] = None
    repo_urls: list[str] = Field(default_factory=list)


class LinkedInIngestRequest(BaseModel):
    text: Optional[str] = None
    html: Optional[str] = None
    url: Optional[str] = None


class ClaimUpdate(BaseModel):
    user_verified: Optional[bool] = None
    allowed_for_resume: Optional[bool] = None
    claim_text: Optional[str] = None
    normalized_claim: Optional[str] = None
    confidence: Optional[float] = None


# ---------- search ----------

class JobSearchRequest(BaseModel):
    query: str
    location: Optional[str] = None
    is_remote: bool = False
    sites: list[str] = Field(default_factory=lambda: ["indeed", "google", "glassdoor"])
    results_per_site: int = 25
    hours_old: Optional[int] = 168
    country: str = "usa"
    employment_type: Optional[str] = None
    distance: Optional[int] = 50
    min_score: Optional[int] = None


# ---------- tailoring ----------

class ResumeTailorRequest(BaseModel):
    job_id: int
    resume_type: str = "job_specific"
    base_resume_id: Optional[int] = None
    target_length_pages: Optional[int] = None


class CoverLetterRequest(BaseModel):
    job_id: int
    tone: str = "professional"


class RecruiterMessageRequest(BaseModel):
    job_id: int
    channel: Literal["linkedin", "email"] = "email"


# ---------- application pipeline ----------

class ApplicationCreate(BaseModel):
    job_id: int
    status: str = "saved"
    mode: Optional[str] = None
    notes: Optional[str] = None


class ApplicationUpdate(BaseModel):
    # Enum-bounded so a bad value (e.g. typo from the kanban frontend)
    # surfaces as 422 at the FastAPI boundary rather than a 500 from
    # _validate_status deep inside pipeline.py.
    status: Optional[Literal[
        "saved", "prepared", "applied", "replied", "screened",
        "interview", "offer", "rejected", "archived", "auto_packet_ready",
    ]] = None
    notes: Optional[str] = None
    next_followup_at: Optional[float] = None
    application_url: Optional[str] = None


# ---------- generic ----------

class OK(BaseModel):
    ok: bool = True
    detail: Optional[str] = None
    data: Optional[Any] = None
