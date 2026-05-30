"""Pluck a JSON object out of arbitrary text. Returns None if nothing valid."""
from __future__ import annotations

import json
import re


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_json(text: str) -> dict | list | None:
    if not text:
        return None
    s = text.strip()
    # 1) direct
    try:
        return json.loads(s)
    except Exception:
        pass
    # 2) fenced
    m = _FENCE.search(s)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except Exception:
            pass
    # 3) first {...} or [...] block by brace-balance
    for opener, closer in (("{", "}"), ("[", "]")):
        start = s.find(opener)
        if start == -1:
            continue
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(s)):
            c = s[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == opener:
                    depth += 1
                elif c == closer:
                    depth -= 1
                    if depth == 0:
                        chunk = s[start:i + 1]
                        try:
                            return json.loads(chunk)
                        except Exception:
                            break
    return None
