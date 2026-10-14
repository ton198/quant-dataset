"""Small HTML encoding and visibility helpers used by report cleaning."""

from __future__ import annotations

import re
from typing import Any


def html_encoding(raw: bytes) -> str:
    """Choose a deterministic encoding from BOM/meta declarations, defaulting to UTF-8."""
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if raw.startswith(b"\xff\xfe\x00\x00"):
        return "utf-32-le"
    if raw.startswith(b"\x00\x00\xfe\xff"):
        return "utf-32-be"
    if raw.startswith(b"\xff\xfe"):
        return "utf-16-le"
    if raw.startswith(b"\xfe\xff"):
        return "utf-16-be"
    match = re.search(rb"(?is)<meta\b[^>]*(?:charset\s*=\s*[\"']?)([a-z0-9._-]+)", raw[:8192])
    if match:
        try:
            return match.group(1).decode("ascii").lower()
        except UnicodeDecodeError:
            pass
    return "utf-8"


def sec_payload_html_encoding(payload: bytes, fallback: str | None = None) -> str:
    """Resolve report payload encoding, preferring its own BOM/meta over wrapper encoding."""
    return html_encoding(payload) if payload else (fallback or "utf-8")


def excluded(node: Any) -> bool:
    """Whether a DOM node is non-visible or machine-only boilerplate."""
    tag = str(getattr(node, "tag", "")).casefold()
    if tag in {"script", "style", "noscript", "template", "svg", "head", "meta", "link"}:
        return True
    if tag in {"ix:hidden", "hidden"} or node.get("hidden") is not None:
        return True
    classes = {part.casefold() for part in str(node.get("class", "")).split()}
    if classes & {"hidden", "sr-only", "visually-hidden", "xbrl", "ix-hidden"}:
        return True
    style = re.sub(r"\s+", "", str(node.get("style", "")).casefold())
    return "display:none" in style or "visibility:hidden" in style or "mso-hide:all" in style
