"""GitHub profile + repo ingestion via the public REST API.

No scraping; we only call the documented API. When a token is configured
we include it to lift the rate limit.
"""
from __future__ import annotations

import base64
import logging
import re
from typing import Any
from urllib.parse import urlparse

from ..config import settings

log = logging.getLogger("jhh.evidence")

try:
    import httpx  # type: ignore
    _HTTPX_OK = True
except Exception as _e:  # noqa: BLE001
    httpx = None  # type: ignore
    _HTTPX_OK = False

_API = "https://api.github.com"
_UA = "JobHuntHacker/0.1 GitHubIngestor"
_TIMEOUT = 15.0


def _headers() -> dict[str, str]:
    h = {"User-Agent": _UA, "Accept": "application/vnd.github+json"}
    if settings.github_token:
        h["Authorization"] = f"token {settings.github_token}"
    return h


# Whitelist of hosts this module is allowed to call. Defense-in-depth:
# all current callers use paths (resolved to api.github.com), but if a
# future caller ever passes a full URL we won't fetch arbitrary hosts.
_ALLOWED_HOSTS = {"api.github.com", "raw.githubusercontent.com"}


def _get(path_or_url: str) -> tuple[int, Any]:
    if not _HTTPX_OK:
        raise RuntimeError("install httpx to use github_ingestion")
    url = path_or_url if path_or_url.startswith("http") else f"{_API}{path_or_url}"
    # SSRF defense: refuse anything outside the GitHub host whitelist.
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        host = ""
    if host not in _ALLOWED_HOSTS:
        log.warning("github GET refused for non-GitHub host: %s", host)
        return 0, {"error": f"refused: {host} is not a GitHub host"}
    try:
        r = httpx.get(url, headers=_headers(), timeout=_TIMEOUT, follow_redirects=True)
    except Exception as e:  # noqa: BLE001
        log.warning("github GET %s failed: %s", url, e)
        return 0, {"error": str(e)}
    try:
        data = r.json()
    except Exception:
        data = {"_raw": r.text[:500]}
    return r.status_code, data


def _parse_profile_url(url: str) -> str | None:
    if not url:
        return None
    if "/" not in url and "github.com" not in url:
        return url.strip().lstrip("@")
    try:
        p = urlparse(url if "://" in url else f"https://{url}")
        host = (p.netloc or "").lower()
        if host and "github.com" not in host:
            return None
        parts = [s for s in (p.path or "").split("/") if s]
        if not parts:
            return None
        return parts[0]
    except Exception:
        return None


def _parse_repo_url(url: str) -> tuple[str, str] | None:
    if not url:
        return None
    try:
        if url.count("/") == 1 and "github.com" not in url:
            owner, repo = url.split("/", 1)
            return owner.strip(), repo.strip().removesuffix(".git")
        p = urlparse(url if "://" in url else f"https://{url}")
        host = (p.netloc or "").lower()
        if host and "github.com" not in host:
            return None
        parts = [s for s in (p.path or "").split("/") if s]
        if len(parts) < 2:
            return None
        return parts[0], parts[1].removesuffix(".git")
    except Exception:
        return None


def ingest_profile(profile_url: str) -> dict[str, Any]:
    """Return a clean dict for a single GitHub profile."""
    login = _parse_profile_url(profile_url)
    if not login:
        return {"error": "could not parse github profile url", "input": profile_url}

    status, user = _get(f"/users/{login}")
    if status == 404:
        return {"error": "github user not found", "login": login, "status": 404}
    if status in (401, 403):
        return {"error": "github auth/rate-limit", "login": login, "status": status}
    if status != 200 or not isinstance(user, dict):
        return {"error": "github fetch failed", "login": login, "status": status}

    status_r, repos = _get(f"/users/{login}/repos?per_page=100&sort=updated")
    if status_r != 200 or not isinstance(repos, list):
        repos = []

    # rank: stars desc then recency (already sorted by updated)
    repos_sorted = sorted(repos, key=lambda r: (-int(r.get("stargazers_count") or 0),),)
    top = repos_sorted[:30]
    rsummary = []
    for r in top:
        rsummary.append({
            "name": r.get("name") or "",
            "full_name": r.get("full_name") or "",
            "description": (r.get("description") or "").strip(),
            "language": r.get("language") or "",
            "stars": int(r.get("stargazers_count") or 0),
            "forks": int(r.get("forks_count") or 0),
            "topics": r.get("topics") or [],
            "url": r.get("html_url") or "",
            "pushed_at": r.get("pushed_at") or "",
            "fork": bool(r.get("fork")),
            "archived": bool(r.get("archived")),
        })

    return {
        "login": user.get("login") or login,
        "name": user.get("name") or "",
        "bio": (user.get("bio") or "").strip(),
        "company": user.get("company") or "",
        "blog": user.get("blog") or "",
        "location": user.get("location") or "",
        "public_repos": int(user.get("public_repos") or 0),
        "followers": int(user.get("followers") or 0),
        "following": int(user.get("following") or 0),
        "html_url": user.get("html_url") or f"https://github.com/{login}",
        "created_at": user.get("created_at") or "",
        "repos": rsummary,
    }


def ingest_repo(repo_url: str) -> dict[str, Any]:
    """Return a clean dict for a single repository, including README."""
    parsed = _parse_repo_url(repo_url)
    if not parsed:
        return {"error": "could not parse github repo url", "input": repo_url}
    owner, repo = parsed

    status, info = _get(f"/repos/{owner}/{repo}")
    if status == 404:
        return {"error": "github repo not found", "owner": owner, "repo": repo, "status": 404}
    if status in (401, 403):
        return {"error": "github auth/rate-limit", "owner": owner, "repo": repo, "status": status}
    if status != 200 or not isinstance(info, dict):
        return {"error": "github fetch failed", "owner": owner, "repo": repo, "status": status}

    readme_text = ""
    status_r, readme = _get(f"/repos/{owner}/{repo}/readme")
    if status_r == 200 and isinstance(readme, dict) and readme.get("content"):
        try:
            encoding = (readme.get("encoding") or "base64").lower()
            if encoding == "base64":
                readme_text = base64.b64decode(readme["content"]).decode("utf-8", errors="replace")
            else:
                readme_text = str(readme["content"])
        except Exception as e:  # noqa: BLE001
            log.warning("README decode failed for %s/%s: %s", owner, repo, e)

    return {
        "owner": owner,
        "name": info.get("name") or repo,
        "full_name": info.get("full_name") or f"{owner}/{repo}",
        "description": (info.get("description") or "").strip(),
        "language": info.get("language") or "",
        "topics": info.get("topics") or [],
        "stars": int(info.get("stargazers_count") or 0),
        "forks": int(info.get("forks_count") or 0),
        "open_issues": int(info.get("open_issues_count") or 0),
        "pushed_at": info.get("pushed_at") or "",
        "created_at": info.get("created_at") or "",
        "html_url": info.get("html_url") or f"https://github.com/{owner}/{repo}",
        "homepage": info.get("homepage") or "",
        "license": (info.get("license") or {}).get("spdx_id") or "",
        "fork": bool(info.get("fork")),
        "archived": bool(info.get("archived")),
        "readme": readme_text,
    }
