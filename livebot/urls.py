from __future__ import annotations
import re
from urllib.parse import urlparse

SUPPORTED_HOSTS = ("xham.live", "stripchat.com")
_XHAM_RESERVED = {"girls", "couples", "discover", "favorites", "terms", "privacy", "login", "signup", "tags", "categories"}


def normalize_supported_url(value: str) -> str | None:
    """Accept one individual room URL; reject listings and look-alike domains."""
    value = (value or "").strip().split()[0] if value else ""
    if not value.startswith(("https://", "http://")):
        return None
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    allowed = any(host == domain or host.endswith("." + domain) for domain in SUPPORTED_HOSTS)
    path = parsed.path.strip("/")
    # Individual rooms on both supported platforms have one path segment.
    if not allowed or not path or "/" in path:
        return None
    if host.endswith("xham.live") and path.lower() in _XHAM_RESERVED:
        return None
    return parsed._replace(fragment="").geturl()


def extract_supported_url(text: str) -> str | None:
    match = re.search(r"https?://[^\s<>]+", text or "", re.I)
    return normalize_supported_url(match.group(0).rstrip(".,!?)]}>\"'")) if match else None


def platform_name(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return "xHam Live" if host.endswith("xham.live") else "Stripchat"
