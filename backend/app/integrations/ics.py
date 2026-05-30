"""ICS file generator — fallback when no Google account is connected."""
from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path


def _ical_dt(s: str) -> str:
    """Convert ISO string to iCal DTSTAMP format (UTC, YYYYMMDDTHHMMSSZ)."""
    if not s:
        s = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt_utc = dt.astimezone(timezone.utc)
    return dt_utc.strftime("%Y%m%dT%H%M%SZ")


def _escape(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")


def to_ics(event: dict) -> str:
    """Build a valid VCALENDAR/VEVENT string from {title,start,end,description,attendees?,location?,uid?}."""
    title = event.get("title") or "Interview"
    start = _ical_dt(event.get("start") or event.get("start_iso") or "")
    end = _ical_dt(event.get("end") or event.get("end_iso") or "")
    description = _escape(event.get("description") or "")
    location = _escape(event.get("location") or "")
    attendees = event.get("attendees") or []
    uid = event.get("uid") or hashlib.sha256(f"{title}{start}{end}{time.time()}".encode()).hexdigest()[:24] + "@jobhunthacker"
    stamp = _ical_dt(datetime.now(timezone.utc).isoformat())

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Job Hunt Hacker//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{stamp}",
        f"DTSTART:{start}",
        f"DTEND:{end}",
        f"SUMMARY:{_escape(title)}",
    ]
    if description:
        lines.append(f"DESCRIPTION:{description}")
    if location:
        lines.append(f"LOCATION:{location}")
    for a in attendees:
        lines.append(f"ATTENDEE;CN={_escape(a)}:mailto:{a}")
    lines += [
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"


def save_to_file(event: dict, path: str | Path) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(to_ics(event), encoding="utf-8")
    return str(p)
