"""POST /api/github/ingest — pull profile + repos via the GitHub API and store
them as evidence sources, then run claim extraction.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException

from ..db import audit
from ..models.schemas import GitHubIngestRequest
from ..services import career_vault, evidence_extractor, github_ingestion

log = logging.getLogger("jhh.evidence")

router = APIRouter(prefix="/api/github", tags=["github"])


def _profile_to_text(p: dict) -> str:
    parts: list[str] = []
    login = p.get("login") or ""
    name = p.get("name") or ""
    parts.append(f"GitHub profile: {name} ({login})".strip())
    if p.get("bio"):
        parts.append(f"Bio: {p['bio']}")
    if p.get("company"):
        parts.append(f"Company: {p['company']}")
    if p.get("location"):
        parts.append(f"Location: {p['location']}")
    if p.get("blog"):
        parts.append(f"Blog: {p['blog']}")
    parts.append(f"Public repos: {p.get('public_repos', 0)}, "
                 f"Followers: {p.get('followers', 0)}")
    parts.append("")
    parts.append("Top repositories:")
    for r in p.get("repos", []):
        line = f"- {r.get('name')} ({r.get('language') or 'n/a'}, "
        line += f"{r.get('stars', 0)} stars): {r.get('description', '')}"
        if r.get("topics"):
            line += f" [topics: {', '.join(r['topics'])}]"
        parts.append(line)
    return "\n".join(parts)


def _repo_to_text(r: dict) -> str:
    parts = [f"GitHub repo: {r.get('full_name')}"]
    if r.get("description"):
        parts.append(f"Description: {r['description']}")
    if r.get("language"):
        parts.append(f"Primary language: {r['language']}")
    if r.get("topics"):
        parts.append(f"Topics: {', '.join(r['topics'])}")
    parts.append(f"Stars: {r.get('stars', 0)}, Forks: {r.get('forks', 0)}")
    if r.get("homepage"):
        parts.append(f"Homepage: {r['homepage']}")
    if r.get("readme"):
        parts.append("")
        parts.append("README:")
        parts.append(r["readme"][:8000])
    return "\n".join(parts)


@router.post("/ingest")
def ingest(body: GitHubIngestRequest) -> dict:
    if not body.profile_url and not body.repo_urls:
        raise HTTPException(400, "profile_url or repo_urls required")

    sources_added: list[dict] = []
    claims_total = 0
    errors: list[dict] = []

    if body.profile_url:
        profile = github_ingestion.ingest_profile(body.profile_url)
        if "error" in profile:
            errors.append({"target": body.profile_url, "error": profile["error"]})
        else:
            text = _profile_to_text(profile)
            source_id = career_vault.add_source(
                source_type="github_profile",
                title=f"GitHub: {profile.get('login') or body.profile_url}",
                url=profile.get("html_url") or body.profile_url,
                raw_text=text,
                parsed_json=profile,
            )
            existing = career_vault.list_claims(source_id=source_id)
            if not existing:
                claims = evidence_extractor.extract_claims(
                    source_id, text, "github_profile")
                inserted = career_vault.add_claims(source_id, claims)
                claims_total += len(inserted)
            sources_added.append({
                "source_id": source_id,
                "type": "github_profile",
                "login": profile.get("login"),
            })

    for repo_url in body.repo_urls or []:
        repo = github_ingestion.ingest_repo(repo_url)
        if "error" in repo:
            errors.append({"target": repo_url, "error": repo["error"]})
            continue
        text = _repo_to_text(repo)
        source_id = career_vault.add_source(
            source_type="github_repo",
            title=f"GitHub repo: {repo.get('full_name')}",
            url=repo.get("html_url") or repo_url,
            raw_text=text,
            parsed_json=repo,
        )
        existing = career_vault.list_claims(source_id=source_id)
        if not existing:
            claims = evidence_extractor.extract_claims(
                source_id, text, "github_repo")
            inserted = career_vault.add_claims(source_id, claims)
            claims_total += len(inserted)
        sources_added.append({
            "source_id": source_id,
            "type": "github_repo",
            "full_name": repo.get("full_name"),
        })

    audit("github_ingest", "evidence_source", None,
          count=len(sources_added), errors=len(errors))
    return {"ok": True, "data": {
        "sources_added": sources_added,
        "claims_extracted": claims_total,
        "errors": errors,
    }}
