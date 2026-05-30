"""Job source adapter interface. All adapters subclass this and self-register
by calling `REGISTRY.register(name, instance)` at import time.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

from ...utils.text import content_hash


@dataclass
class JobSearchQuery:
    query: str = ""
    location: Optional[str] = None
    is_remote: bool = False
    results_per_site: int = 25
    hours_old: Optional[int] = 168
    country: str = "usa"
    employment_type: Optional[str] = None
    distance: Optional[int] = 50
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class JobRecord:
    source: str
    title: str
    company: str = ""
    location: str = ""
    remote_type: str = ""              # remote | hybrid | onsite | ""
    employment_type: str = ""
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    currency: str = ""
    bonus_equity_text: str = ""
    description: str = ""
    requirements: list[str] = field(default_factory=list)
    benefits: list[str] = field(default_factory=list)
    apply_url: str = ""
    company_url: str = ""
    posted_at: str = ""                # ISO date string
    external_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def hash(self) -> str:
        return content_hash(self.source, self.company.lower(), self.title.lower(),
                            self.location.lower(), (self.posted_at or "")[:7])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SourcePolicy:
    name: str
    display_name: str
    official_api: bool
    scraping: bool
    apply_automation_allowed: bool
    recommended_mode: str             # research | assisted | auto
    risk_level: str                   # LEGAL | GRAY | TOS-RISK
    rate_limit_note: str = ""
    notes: str = ""
    last_reviewed: str = "2026-05-30"


class JobSourceAdapter:
    """Subclass + override .search(). Adapters should be defensive — return
    [] on missing credentials, network failure, or empty result.
    """

    name: str = ""
    policy: SourcePolicy

    def search(self, q: JobSearchQuery) -> list[JobRecord]:  # pragma: no cover
        raise NotImplementedError

    def healthy(self) -> bool:
        """Cheap check: are credentials/network OK enough to attempt a search?"""
        return True


class _Registry(dict):
    def register(self, adapter: JobSourceAdapter) -> None:
        if not adapter.name:
            raise ValueError("adapter.name is required")
        self[adapter.name] = adapter


REGISTRY = _Registry()
