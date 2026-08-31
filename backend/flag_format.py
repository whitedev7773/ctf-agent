"""Conservative flag-format validation used before live submissions."""

from __future__ import annotations

import re


def _pattern_from_hint(hint: str) -> re.Pattern[str] | None:
    hint = hint.strip()
    if not hint:
        return None
    if hint.lower().startswith("regex:"):
        try:
            return re.compile(hint[6:].strip())
        except re.error:
            return None

    # Common CTF notation: TEAM{...}, flag{*}, CCE2026{anything}.
    marker = next((item for item in ("<...>", "<flag>", "...", "*") if item in hint), None)
    if marker:
        before, after = hint.split(marker, 1)
        return re.compile(rf"^{re.escape(before)}.+{re.escape(after)}$")

    # A literal prefix ending in an opening brace is also a useful hint.
    if hint.endswith("{"):
        return re.compile(rf"^{re.escape(hint)}.+\}}$")
    return None


def flag_matches_format(flag: str, format_hint: str) -> bool:
    """Validate a candidate when the format is machine-readable; otherwise allow it."""
    candidate = flag.strip()
    if not candidate:
        return False
    pattern = _pattern_from_hint(format_hint)
    return True if pattern is None else pattern.fullmatch(candidate) is not None
