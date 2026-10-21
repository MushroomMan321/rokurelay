"""HLS playlist parsing and rewriting.

Every URI in a playlist (variants, renditions, segments, keys, init sections) is handed
to a mapper callback, which registers it with the session and returns the relay URL the
Roku should request instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urljoin

URI_ATTR_RE = re.compile(r'URI="([^"]*)"')

# role names passed to the mapper
VARIANT = "variant"  # #EXT-X-STREAM-INF target
MEDIA = "media"  # #EXT-X-MEDIA alternate renditions (audio, subtitles)
IFRAME = "iframe"
SEGMENT = "segment"
KEY = "key"
MAP = "map"
OTHER = "other"

PLAYLIST_ROLES = (VARIANT, MEDIA, IFRAME)

_TAG_ROLES = {
    "#EXT-X-MEDIA": MEDIA,
    "#EXT-X-I-FRAME-STREAM-INF": IFRAME,
    "#EXT-X-RENDITION-REPORT": MEDIA,
    "#EXT-X-KEY": KEY,
    "#EXT-X-SESSION-KEY": KEY,
    "#EXT-X-MAP": MAP,
    "#EXT-X-PART": SEGMENT,
    "#EXT-X-PRELOAD-HINT": SEGMENT,
}

Mapper = Callable[[str, str, int], str]


@dataclass
class Rewritten:
    text: str
    is_master: bool
    endlist: bool
    target_duration: float | None
    segments: list[str] = field(default_factory=list)  # mapped segment URIs, in order
    upstream: list[tuple[str, str]] = field(default_factory=list)  # (role, absolute url)


def looks_like_playlist(body: bytes | str, content_type: str | None = None) -> bool:
    head = body[:64] if isinstance(body, str) else body[:64].decode("utf-8", "ignore")
    head = head.lstrip("\ufeff \r\n\t")
    if "#EXT" in head:
        return True
    # an empty body is judged on its Content-Type alone; a non-playlist body is not,
    # whatever it claims to be (some 404 pages come back labelled "application/mpegurl").
    return not head.strip() and "mpegurl" in (content_type or "").lower()


def _rewritable(uri: str) -> bool:
    # skd:// (FairPlay), data: and similar can't be proxied; leave them alone.
    return not re.match(r"^[a-z][a-z0-9+.-]*:", uri, re.I) or uri.lower().startswith(("http://", "https://"))


def drop_master_variants(text: str, bad: set) -> str:
    """Drop quarantined variants (keys v0, v1, ... in #EXT-X-STREAM-INF order) from a
    master playlist, so the TV doesn't switch back to a quality the CDN is rejecting.
    If every variant would be dropped, keep everything — a fresh chance beats empty."""
    if not bad or "#EXT-X-STREAM-INF" not in text:
        return text
    out: list[str] = []
    idx = -1
    skip = False
    for line in text.splitlines():
        if line.startswith("#EXT-X-STREAM-INF"):
            idx += 1
            skip = f"v{idx}" in bad
            if not skip:
                out.append(line)
            continue
        if skip:
            if not line or line.startswith("#"):
                continue  # leftover tags of the dropped variant
            skip = False  # this is its URI line; drop it and resume
            continue
        out.append(line)
    if not any(l.startswith("#EXT-X-STREAM-INF") for l in out):
        return text
    return "\n".join(out) + "\n"


def rewrite(text: str, base_url: str, mapper: Mapper) -> Rewritten:
    text = text.lstrip("﻿")
    lines = text.splitlines()
    out: list[str] = []
    is_master = any(line.startswith("#EXT-X-STREAM-INF") for line in lines)
    endlist = False
    target: float | None = None
    counters: dict[str, int] = {}
    segments: list[str] = []
    upstream: list[tuple[str, str]] = []
    expect_variant = False

    def map_uri(uri: str, role: str) -> str:
        if not _rewritable(uri):
            return uri
        absolute = urljoin(base_url, uri)
        idx = counters.get(role, 0)
        counters[role] = idx + 1
        upstream.append((role, absolute))
        return mapper(absolute, role, idx)

    for raw in lines:
        line = raw.strip()
        if not line:
            out.append("")
            continue
        if line.startswith("#"):
            tag = line.split(":", 1)[0]
            if tag == "#EXT-X-ENDLIST":
                endlist = True
            elif tag == "#EXT-X-TARGETDURATION":
                try:
                    target = float(line.split(":", 1)[1])
                except (IndexError, ValueError):
                    pass
            elif tag == "#EXT-X-STREAM-INF":
                expect_variant = True
            role = _TAG_ROLES.get(tag)
            if role and 'URI="' in line:
                line = URI_ATTR_RE.sub(lambda m: f'URI="{map_uri(m.group(1), role)}"', line)
            out.append(line)
            continue
        if expect_variant:
            out.append(map_uri(line, VARIANT))
            expect_variant = False
        else:
            mapped = map_uri(line, SEGMENT)
            segments.append(mapped)
            out.append(mapped)

    return Rewritten(
        text="\n".join(out) + "\n",
        is_master=is_master,
        endlist=endlist,
        target_duration=target,
        segments=segments,
        upstream=upstream,
    )
