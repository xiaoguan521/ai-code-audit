"""Parse YAML front matter from Harness Markdown reports.

The front matter carries structured scores and counts that the runner logs
alongside the raw report. Only the small subset of YAML used by the report
template (scalars, flow mappings, comments) is supported — no anchors, tags,
multi-line values or block sequences.
"""
from __future__ import annotations

import re
from typing import Any

_FRONT_RE = re.compile(r"\A\s*---[ \t]*\n(.*?)\n---[ \t]*\n", re.DOTALL)


def parse(text: str) -> dict[str, Any] | None:
    """Extract front matter dict from Markdown. Returns ``None`` if absent."""
    match = _FRONT_RE.match(text)
    if not match:
        return None
    result: dict[str, Any] = {}
    for line in match.group(1).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, value = stripped.partition(":")
        if not sep:
            continue
        key = key.strip()
        # Value may itself contain colons (e.g. flow mappings); take everything
        # after the *first* colon and strip inline comments carefully.
        value = value.strip()
        # Strip trailing inline comment only outside braces.
        if "#" in value and not (value.startswith("{") and value.endswith("}")):
            # Only strip if the # is preceded by whitespace (avoid mangling strings).
            value = re.sub(r"\s+#.*$", "", value)
        result[key] = _value(value)
    return result


def _value(raw: str) -> Any:
    """Parse a single YAML scalar or flow mapping."""
    if not raw:
        return ""
    low = raw.lower()
    if low in ("n/a", "null", "~"):
        return None
    if low == "true":
        return True
    if low == "false":
        return False
    # Flow mapping: {key: val, key: val}
    if raw.startswith("{") and raw.endswith("}"):
        return _flow_mapping(raw[1:-1])
    # Number
    try:
        if "." in raw:
            return float(raw)
        return int(raw)
    except ValueError:
        pass
    # Quoted string
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        return raw[1:-1]
    return raw


def _flow_mapping(inner: str) -> dict[str, Any]:
    """Parse ``{K: V, K: V}`` interior, handling nested colons in keys/values."""
    result: dict[str, Any] = {}
    for pair in inner.split(","):
        pair = pair.strip()
        if not pair:
            continue
        key, sep, val = pair.partition(":")
        if not sep:
            continue
        result[key.strip()] = _value(val.strip())
    return result


# ---------------------------------------------------------------------------
# Convenience: extract the fields the runner needs for the run log.
# ---------------------------------------------------------------------------

_RUN_LOG_KEYS = frozenset({
    "score", "tier", "security_score", "dimension_scores",
    "issues", "results", "coverage", "dimensions_scored",
    "validation", "scan_type", "scan_date",
})


def run_log_fields(text: str) -> dict[str, Any]:
    """Return the subset of front matter fields used by the runner's run log."""
    fm = parse(text)
    if not fm:
        return {}
    return {k: fm[k] for k in _RUN_LOG_KEYS if k in fm}

