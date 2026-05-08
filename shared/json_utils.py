"""JSON-repair utilities for LLM output parsing."""
from __future__ import annotations
import json
import re


def strip_fence(raw: str) -> str:
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = s.rstrip("`").strip()
    return s


def clean_json_text(s: str) -> str:
    """Fix common local-model JSON emissions: smart quotes and trailing commas."""
    if not s:
        return s
    s = s.replace("“", '"').replace("”", '"')
    s = s.replace("‘", "'").replace("’", "'")
    s = re.sub(r",\s*([}\]])", r"\1", s)
    return s


def extract_json_object(raw: str) -> str:
    """Return the first JSON object substring from raw text; empty string on failure."""
    s = strip_fence(raw)
    start = s.find("{")
    if start < 0:
        return ""
    obj_depth = 0
    arr_depth = 0
    in_str = False
    escape = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            obj_depth += 1
        elif ch == "}":
            obj_depth -= 1
            if obj_depth == 0 and arr_depth == 0:
                return s[start:i + 1]
        elif ch == "[":
            arr_depth += 1
        elif ch == "]":
            arr_depth -= 1
    # truncated — best-effort close
    tail = s[start:]
    if in_str:
        tail += '"'
    tail = re.sub(r",\s*$", "", tail)
    tail += "]" * arr_depth
    tail += "}" * obj_depth
    return tail


def parse_json_object(raw: str) -> dict:
    """Parse the first JSON object from raw LLM text. Raises ValueError on failure."""
    s = clean_json_text(strip_fence(raw))
    start = s.find("{")
    if start < 0:
        raise ValueError("no JSON object found in LLM output")
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            escape = not escape and ch == "\\"
            if not escape and ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(s[start:i + 1])
    raise ValueError("unbalanced JSON object in LLM output")


def salvage_list(raw: str, key: str) -> list:
    """Extract a named JSON array from malformed model output."""
    s = clean_json_text(strip_fence(raw))
    m = re.search(rf'"{key}"\s*:\s*\[', s, re.IGNORECASE)
    if not m:
        return []
    start = m.end()
    depth = 1
    in_str = False
    escape = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start - 1:i + 1])
                except json.JSONDecodeError:
                    return []
    return []


def salvage_queries(raw: str) -> list[str]:
    """Pull a queries array from malformed model output via regex string extraction.

    More robust than salvage_list for the research-planner case: handles
    single-quoted and unquoted key spellings, and extracts strings by regex
    rather than JSON parsing — so it works on truncated or heavily malformed
    output.
    """
    if not raw:
        return []
    s = clean_json_text(strip_fence(raw))
    for key_re in (r'"queries"\s*:\s*\[', r"'queries'\s*:\s*\[", r"queries\s*:\s*\["):
        m = re.search(key_re, s, re.IGNORECASE)
        if not m:
            continue
        start = m.end()
        depth = 1
        in_str = False
        escape = False
        for i in range(start, len(s)):
            ch = s[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    inner = s[start:i]
                    items = re.findall(r'"((?:[^"\\]|\\.)*)"', inner)
                    if not items:
                        items = re.findall(r"'((?:[^'\\]|\\.)*)'", inner)
                    return [it.strip() for it in items if it.strip()]
        inner = s[start:]
        items = re.findall(r'"((?:[^"\\]|\\.)*)"', inner)
        return [it.strip() for it in items if it.strip()][:20]
    return []
