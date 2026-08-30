"""URL helpers shared by challenge download paths."""

from __future__ import annotations

from urllib.parse import urlparse


def same_origin(left: str, right: str) -> bool:
    """Return whether two absolute HTTP(S) URLs have the same origin."""

    def _origin(url: str) -> tuple[str, str, int | None] | None:
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower()
        if scheme not in ("http", "https") or not host:
            return None
        try:
            port = parsed.port
        except ValueError:
            return None
        if port is None:
            port = 443 if scheme == "https" else 80
        return scheme, host, port

    left_origin = _origin(left)
    return left_origin is not None and left_origin == _origin(right)
