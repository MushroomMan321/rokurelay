"""Small helpers shared across the relay."""

from __future__ import annotations

import hashlib
import re
import time
from urllib.parse import parse_qsl, quote, unquote, urlsplit

from yarl import URL

URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)

# Things a page or a sniffer might hand us that are almost certainly a media manifest.
MEDIA_HINT_RE = re.compile(r"\.m3u8|\.mpd|format=m3u8|mpegurl|\.mp4(?:$|[?#])", re.I)

HLS_TYPES = ("application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl", "audio/x-mpegurl")
DASH_TYPES = ("application/dash+xml",)
PROGRESSIVE_TYPES = ("video/mp4", "video/quicktime", "video/x-m4v", "video/webm", "video/x-matroska", "video/mp2t")


def redact(url: str | None) -> str:
    """Strip query strings so tokens and cookies never land in logs or the UI."""
    if not url:
        return ""
    parts = urlsplit(url)
    suffix = "?..." if parts.query else ""
    path = parts.path if len(parts.path) <= 80 else parts.path[:40] + "..." + parts.path[-30:]
    return f"{parts.scheme}://{parts.netloc}{path}{suffix}"


def extract_url(text: str | None) -> str | None:
    """Pull the first http(s) URL out of shared text ("Check this out https://...")."""
    if not text:
        return None
    m = URL_RE.search(text)
    return m.group(0).rstrip(".,);]") if m else None


def guess_kind(url: str, content_type: str | None = None) -> str | None:
    """Return "hls", "dash", "progressive" or None from a URL and/or Content-Type."""
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in HLS_TYPES:
        return "hls"
    if ct in DASH_TYPES:
        return "dash"
    if ct in PROGRESSIVE_TYPES:
        return "progressive"
    path = urlsplit(url).path.lower()
    query = urlsplit(url).query.lower()
    if path.endswith(".m3u8") or "format=m3u8" in query or path.endswith(".m3u"):
        return "hls"
    if path.endswith(".mpd"):
        return "dash"
    if path.endswith((".mp4", ".m4v", ".mov", ".mkv", ".webm", ".ts", ".flv")):
        return "progressive"
    return None


def looks_like_media(url: str) -> bool:
    return guess_kind(url) is not None


def pick_candidate(candidates: list[str]) -> str | None:
    """Choose the best stream among URLs a browser sniffer saw.

    Pages load the HLS master before variant playlists, so the first HLS URL is usually
    the master. Variant/chunk playlists are only used when nothing better exists.
    """
    urls = [c for c in candidates if c and c.lower().startswith(("http://", "https://"))]
    hls = [u for u in urls if guess_kind(u) == "hls"]
    variantish = re.compile(r"chunklist|/chunks?|variant|_\d{3,4}p?\.m3u8|/\d{3,4}p?/", re.I)
    for u in hls:
        if not variantish.search(u):
            return u
    if hls:
        return hls[0]
    for kind in ("dash", "progressive"):
        for u in urls:
            if guess_kind(u) == kind:
                return u
    return urls[0] if urls else None


def stable_key(url: str) -> str:
    """Key a resource by scheme/host/path so refreshed tokens map to the same key."""
    p = urlsplit(url)
    return hashlib.sha1(f"{p.scheme}://{p.netloc}{p.path}".encode()).hexdigest()[:16]


def extension_of(url: str, default: str = "bin") -> str:
    path = urlsplit(url).path
    name = path.rsplit("/", 1)[-1]
    if "." in name:
        ext = name.rsplit(".", 1)[-1].lower()
        if re.fullmatch(r"[a-z0-9]{1,5}", ext):
            return ext
    return default


_EXPIRY_PARAMS = ("expire", "expires", "expiry", "exp", "expiration", "validto", "e")
_EMBEDDED_EXP_RE = re.compile(r"(?:^|[~&;])(?:exp|expires)=(\d{9,11})")
_PATH_EXP_RE = re.compile(r"/(?:expire|expires|exp)/(\d{9,11})(?:/|$)", re.I)


def parse_expiry(url: str, now: float | None = None) -> float | None:
    """Best-effort guess at when a signed URL stops working (unix seconds).

    Handles the common CDN conventions: ?expires=, Akamai hdnts=exp=..., YouTube's
    /expire/<ts>/ path segments and AWS X-Amz-Date + X-Amz-Expires.
    """
    now = time.time() if now is None else now
    parts = urlsplit(url)
    found: list[float] = []

    def plausible(v: float) -> bool:
        return now - 86400 < v < now + 30 * 86400

    query = dict((k.lower(), v) for k, v in parse_qsl(parts.query, keep_blank_values=True))
    for name in _EXPIRY_PARAMS:
        v = query.get(name)
        if v and v.isdigit():
            ts = float(v[:10]) if len(v) == 13 else float(v)
            if plausible(ts):
                found.append(ts)
    for v in query.values():
        for m in _EMBEDDED_EXP_RE.finditer(unquote(v)):
            if plausible(float(m.group(1))):
                found.append(float(m.group(1)))
    for m in _PATH_EXP_RE.finditer(parts.path):
        if plausible(float(m.group(1))):
            found.append(float(m.group(1)))
    amz_date, amz_exp = query.get("x-amz-date"), query.get("x-amz-expires")
    if amz_date and amz_exp and amz_exp.isdigit():
        try:
            t = time.strptime(amz_date, "%Y%m%dT%H%M%SZ")
            import calendar

            ts = calendar.timegm(t) + int(amz_exp)
            if plausible(ts):
                found.append(float(ts))
        except ValueError:
            pass
    return min(found) if found else None


def header_lines(headers: dict[str, str]) -> str:
    """Format headers for ffmpeg's -headers option."""
    return "".join(f"{k}: {v}\r\n" for k, v in headers.items() if v)


def upstream_url(url: str) -> URL:
    """Exact URL for upstream requests.

    yarl normally re-normalises percent-encoding (%3D -> =), which breaks CDNs that sign
    the literal path (YouTube, CloudFront, Akamai). Send it untouched; only escape
    characters that can't legally appear in a request line.
    """
    if re.search(r"[^!-~]", url):
        url = quote(url, safe="!#$%&'()*+,/:;=?@[]~")
    return URL(url, encoded=True)
