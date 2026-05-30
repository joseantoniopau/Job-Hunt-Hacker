"""ProvenanceMap — tracks {segment_id -> [evidence_id]} for every output."""
from __future__ import annotations

from typing import Iterable


class ProvenanceMap:
    """A simple, append-only map of segment_id -> list of evidence_ids.

    Used by every tailoring output so the final honesty report can compute
    coverage and surface segments without backing evidence.
    """

    def __init__(self) -> None:
        self._map: dict[str, list[int]] = {}

    def link(self, segment_id: str, evidence_ids: Iterable[int] | None) -> None:
        ids: list[int] = []
        for eid in evidence_ids or []:
            try:
                ids.append(int(eid))
            except Exception:
                continue
        # dedupe, preserve order
        seen: set[int] = set()
        clean: list[int] = []
        for i in ids:
            if i in seen:
                continue
            seen.add(i)
            clean.append(i)
        # merge with existing
        existing = self._map.get(segment_id) or []
        for i in clean:
            if i not in existing:
                existing.append(i)
        self._map[str(segment_id)] = existing

    def get(self, segment_id: str) -> list[int]:
        return list(self._map.get(str(segment_id)) or [])

    def all_ids(self) -> set[int]:
        out: set[int] = set()
        for ids in self._map.values():
            out.update(ids)
        return out

    def segments(self) -> list[str]:
        return list(self._map.keys())

    def to_dict(self) -> dict:
        return {
            "segments": dict(self._map),
            "distinct_evidence_ids": sorted(self.all_ids()),
            "coverage": self.coverage(),
        }

    def coverage(self) -> dict:
        n = len(self._map)
        n_with = sum(1 for ids in self._map.values() if ids)
        return {
            "n_segments": n,
            "n_with_evidence": n_with,
            "n_without": n - n_with,
        }

    def __len__(self) -> int:
        return len(self._map)

    def __contains__(self, segment_id: str) -> bool:
        return str(segment_id) in self._map
