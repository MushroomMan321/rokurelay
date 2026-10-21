"""Turn whatever the phone sent (a page URL, a sniffed manifest, cookies...) into a
playable upstream URL plus the headers needed to fetch it.

Resolution order for mode="auto":
  1. An explicit or sniffed stream URL (what the phone's browser actually loaded).
  2. The shared URL itself, if it is a manifest/media file.
  3. yt-dlp on the page (then on any embedded player iframes).
  4. A plain scan of the page HTML for .m3u8/.mpd links.
  5. Headless-browser sniff: load the page and watch what its player fetches
     (BROWSER_SNIFF=1; sees streams a cross-origin iframe assembles in script).
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from http.cookiejar import Cookie
from typing import Any, Mapping
from urllib.parse import urljoin, urlsplit

import aiohttp

from . import hls
from .config import Config
from .util import (
    MEDIA_HINT_RE,
    extension_of,
    extract_url,
    guess_kind,
    looks_like_media,
    parse_expiry,
    pick_candidate,
    redact,
    upstream_url,
)

log = logging.getLogger("relay.resolver")

MODES = ("auto", "direct", "ytdlp", "scan", "browser", "ffmpeg")


class ResolveError(Exception):
    pass


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [v for v in re.split(r"[\n\r]+", value) if v.strip()]
    return [str(v) for v in value if v]


def _parse_headers(value: Any) -> dict[str, str]:
    if not value:
        return {}
    if isinstance(value, Mapping):
        return {str(k).strip(): str(v).strip() for k, v in value.items() if str(k).strip()}
    headers = {}
    for line in str(value).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            if k.strip():
                headers[k.strip()] = v.strip()
    return headers


@dataclass
class CastRequest:
    url: str
    stream_url: str | None = None
    candidates: list[str] = field(default_factory=list)
    frames: list[str] = field(default_factory=list)
    referer: str | None = None
    user_agent: str | None = None  # explicit override, applied to everything
    client_user_agent: str | None = None  # the phone's UA, used for sniffed/direct streams
    cookies: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    mode: str = "auto"
    title: str | None = None
    devices: list[str] | None = None  # device names/hosts to launch on; None = all
    launch: bool = True

    @classmethod
    def from_payload(cls, data: Mapping[str, Any]) -> "CastRequest":
        def s(key: str) -> str | None:
            v = data.get(key)
            if isinstance(v, list):
                v = v[0] if v else None
            v = (str(v).strip() if v is not None else "") or None
            return v

        url = s("url") or extract_url(s("text")) or s("stream_url") or s("stream")
        if not url or not url.lower().startswith(("http://", "https://")):
            raise ResolveError("A http(s) link is required")
        candidates = _as_list(data.get("candidates")) + _as_list(data.get("candidate"))
        devices = data.get("devices", data.get("device"))
        mode = (s("mode") or "auto").lower()
        if mode not in MODES:
            raise ResolveError(f"Unknown mode {mode!r}")
        launch = data.get("launch", True)
        if isinstance(launch, list):
            launch = launch[0] if launch else True
        return cls(
            url=url,
            stream_url=s("stream_url") or s("stream"),
            candidates=candidates,
            frames=_as_list(data.get("frames")) + _as_list(data.get("frame")),
            referer=s("referer") or s("ref"),
            user_agent=s("user_agent"),
            client_user_agent=s("client_user_agent") or s("ua"),
            cookies=s("cookies") or s("cookie"),
            headers=_parse_headers(data.get("headers")),
            mode=mode,
            title=s("title"),
            devices=_as_list(devices) or None,
            launch=str(launch).lower() not in ("0", "false", "no", "off"),
        )

    def to_dict(self) -> dict:
        return asdict(self)

    def describe(self) -> dict:
        """Safe-to-display summary (no cookies or tokens)."""
        return {
            "url": redact(self.url),
            "stream_url": redact(self.stream_url),
            "candidates": len(self.candidates),
            "mode": self.mode,
            "has_cookies": bool(self.cookies),
        }


@dataclass
class Resolved:
    kind: str  # "hls" | "dash" | "progressive"
    urls: list[str]
    headers: dict[str, str]
    cookies: list[Cookie] = field(default_factory=list)
    title: str | None = None
    is_live: bool | None = None
    via: str = "direct"
    needs_ffmpeg: bool = False
    container: str | None = None  # file extension hint for progressive sources

    @property
    def url(self) -> str:
        return self.urls[0]

    @property
    def expires_at(self) -> float | None:
        times = [t for t in (parse_expiry(u) for u in self.urls) if t]
        return min(times) if times else None


def _origin(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


class Resolver:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    async def resolve(self, req: CastRequest, http: aiohttp.ClientSession) -> Resolved:
        errors: list[str] = []
        mode = req.mode
        force_ffmpeg = mode == "ffmpeg"

        def done(r: Resolved) -> Resolved:
            r.needs_ffmpeg = r.needs_ffmpeg or force_ffmpeg
            r.title = req.title or r.title
            return r

        if mode in ("auto", "direct", "ffmpeg"):
            direct = req.stream_url or pick_candidate(req.candidates)
            if not direct and (looks_like_media(req.url) or mode == "direct"):
                direct = req.url
            via = "sniffed" if (req.candidates and not req.stream_url) else "direct"
            if direct:
                try:
                    return done(await self.direct(direct, req, http, via=via))
                except ResolveError as e:
                    errors.append(f"direct: {e}")
                    if mode == "direct":
                        raise ResolveError("; ".join(errors)) from e
            elif mode == "auto":
                # Extension-less links can still be manifests; one cheap request tells us.
                try:
                    return done(await self.direct(req.url, req, http, via="direct", must_be_media=True))
                except ResolveError:
                    pass

        targets = [req.url] + [f for f in req.frames if f != req.url]
        for target in targets:
            referer = req.url if target != req.url else req.referer
            if mode in ("auto", "ytdlp", "ffmpeg"):
                try:
                    return done(await self.ytdlp(target, req, referer))
                except ResolveError as e:
                    errors.append(f"yt-dlp({redact(target)}): {e}")
            if mode in ("auto", "scan", "ffmpeg"):
                try:
                    return done(await self.scan(target, req, http, referer))
                except ResolveError as e:
                    errors.append(f"scan({redact(target)}): {e}")
        if mode == "browser" or (self.cfg.browser_sniff and mode in ("auto", "ffmpeg")):
            try:
                return done(await self.browser(req, http))
            except ResolveError as e:
                errors.append(f"browser: {e}")
        raise ResolveError("; ".join(errors) or "Nothing could be resolved")

    # -- headers ---------------------------------------------------------------

    def direct_headers(self, url: str, req: CastRequest, referer: str | None = None) -> dict[str, str]:
        h = {"User-Agent": req.user_agent or req.client_user_agent or self.cfg.default_user_agent}
        referer = req.referer or referer or (req.url if req.url != url else None)
        if referer:
            h["Referer"] = referer
            if _origin(referer) != _origin(url):
                # Browser players fetch cross-origin with XHR, which sends Origin.
                h["Origin"] = _origin(referer)
        if req.cookies:
            h["Cookie"] = req.cookies
        h.update(req.headers)
        return h

    def _apply_overrides(self, headers: dict[str, str], req: CastRequest, referer: str | None) -> dict[str, str]:
        h = dict(headers)
        if req.user_agent:
            h["User-Agent"] = req.user_agent
        if req.referer or (referer and "Referer" not in h):
            h["Referer"] = req.referer or referer  # type: ignore[assignment]
        if req.cookies:
            h["Cookie"] = req.cookies
        h.update(req.headers)
        return {k: v for k, v in h.items() if v}

    # -- strategies ------------------------------------------------------------

    async def direct(
        self,
        url: str,
        req: CastRequest,
        http: aiohttp.ClientSession,
        via: str = "direct",
        referer: str | None = None,
        must_be_media: bool = False,
    ) -> Resolved:
        headers = self.direct_headers(url, req, referer)
        kind, is_live = await self.probe(url, headers, http)
        if kind is None:
            if must_be_media:
                raise ResolveError("not a media URL")
            raise ResolveError(f"{redact(url)} is not a stream (HLS, DASH or video file)")
        return Resolved(
            kind=kind, urls=[url], headers=headers, is_live=is_live, via=via,
            container=extension_of(url, "mp4"),
        )

    async def probe(
        self, url: str, headers: dict[str, str], http: aiohttp.ClientSession
    ) -> tuple[str | None, bool | None]:
        """Fetch the start of a URL and work out what it is. Raises on HTTP errors."""
        timeout = aiohttp.ClientTimeout(total=self.cfg.upstream_timeout)
        try:
            async with http.get(upstream_url(url), headers=headers, timeout=timeout) as resp:
                if resp.status >= 400:
                    raise ResolveError(f"HTTP {resp.status} from {redact(url)}")
                ct = resp.headers.get("Content-Type", "")
                kind = guess_kind(str(resp.url), ct) or guess_kind(url)
                if kind == "progressive" and ct.startswith(("video/", "application/octet-stream")):
                    return kind, False
                chunk = await resp.content.read(1 << 20)
                final_url = str(resp.url)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise ResolveError(f"{type(e).__name__} fetching {redact(url)}: {str(e)[:140]}") from e

        if hls.looks_like_playlist(chunk, ct):
            text = chunk.decode("utf-8", "replace")
            if "#EXT-X-STREAM-INF" not in text:
                return "hls", "#EXT-X-ENDLIST" not in text
            return "hls", await self._master_is_live(text, final_url, headers, http)
        if b"<MPD" in chunk[:4096]:
            return "dash", b'type="dynamic"' in chunk[:4096]
        if kind == "progressive" or ct.startswith("video/"):
            return "progressive", False
        if chunk[:1] == b"G" and len(chunk) > 188 and chunk[188:189] == b"G":
            return "progressive", None  # raw MPEG-TS
        return None, None

    async def _master_is_live(
        self, text: str, base: str, headers: dict[str, str], http: aiohttp.ClientSession
    ) -> bool | None:
        variant = None
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF"):
                for nxt in lines[i + 1 :]:
                    if nxt.strip() and not nxt.startswith("#"):
                        variant = urljoin(base, nxt.strip())
                        break
                break
        if not variant:
            return None
        try:
            timeout = aiohttp.ClientTimeout(total=self.cfg.upstream_timeout)
            async with http.get(upstream_url(variant), headers=headers, timeout=timeout) as resp:
                if resp.status >= 400:
                    return None
                body = await resp.text(errors="replace")
            return "#EXT-X-ENDLIST" not in body
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None

    async def ytdlp(self, target: str, req: CastRequest, referer: str | None) -> Resolved:
        try:
            import yt_dlp  # noqa: F401  (imported lazily; it's slow to load)
        except ImportError as e:  # pragma: no cover
            raise ResolveError("yt-dlp is not installed") from e

        cookie_tmp = None
        if os.path.isfile(self.cfg.cookies_file):
            # yt-dlp writes cookies back on exit; give it a scratch copy.
            fd, cookie_tmp = tempfile.mkstemp(suffix=".txt")
            os.close(fd)
            shutil.copyfile(self.cfg.cookies_file, cookie_tmp)

        http_headers: dict[str, str] = {}
        if req.user_agent:
            http_headers["User-Agent"] = req.user_agent
        if req.referer or referer:
            http_headers["Referer"] = req.referer or referer  # type: ignore[assignment]
        if req.cookies:
            http_headers["Cookie"] = req.cookies
        http_headers.update(req.headers)

        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "skip_download": True,
            "noplaylist": True,
            "format": self.cfg.ytdlp_format,
            "socket_timeout": self.cfg.upstream_timeout,
            "extractor_retries": 2,
            "http_headers": http_headers,
            "logger": _YtdlpLogger(),
        }
        if cookie_tmp:
            opts["cookiefile"] = cookie_tmp

        def run() -> tuple[dict, list[Cookie]]:
            import yt_dlp

            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(target, download=False)
                return info, list(ydl.cookiejar)

        loop = asyncio.get_running_loop()
        try:
            info, cookies = await asyncio.wait_for(
                loop.run_in_executor(None, run), timeout=self.cfg.resolve_timeout
            )
        except asyncio.TimeoutError as e:
            raise ResolveError("timed out") from e
        except Exception as e:  # yt-dlp raises a zoo of DownloadError/ExtractorError
            msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "")
            raise ResolveError(msg.splitlines()[0][:300] if msg else type(e).__name__) from e
        finally:
            if cookie_tmp:
                try:
                    os.unlink(cookie_tmp)
                except OSError:
                    pass

        if info.get("entries"):
            info = next((e for e in info["entries"] if e), None) or {}
        formats = info.get("requested_formats") or [info]
        if not formats or not formats[0].get("url"):
            raise ResolveError("yt-dlp returned no playable format")

        protos = [str(f.get("protocol") or "") for f in formats]
        manifests = {f.get("manifest_url") for f in formats}
        shared_manifest = manifests.pop() if len(manifests) == 1 else None
        if len(formats) > 1 and shared_manifest:
            if self.cfg.prefer_hls_master and all(p.startswith("m3u8") for p in protos):
                # Separate audio/video renditions of one HLS master (YouTube live does
                # this): play the master itself, the Roku handles EXT-X-MEDIA audio + ABR.
                formats = [{**formats[0], "url": shared_manifest}]
            elif all("dash" in p for p in protos):
                formats = [formats[0]]  # one MPD input; ffmpeg picks the best tracks

        needs_ffmpeg = len(formats) > 1
        urls: list[str] = []
        kind = "progressive"
        for f in formats:
            proto = str(f.get("protocol") or "")
            url = f["url"]
            if proto.startswith("m3u8"):
                kind = "hls"
                if self.cfg.prefer_hls_master and f.get("manifest_url"):
                    url = f["manifest_url"]
            elif "dash" in proto:
                kind = "dash"
                url = f.get("manifest_url") or url
            elif not proto.startswith("http"):
                needs_ffmpeg = True  # rtmp, websocket, etc.
            urls.append(url)
        if needs_ffmpeg and kind != "dash":
            kind = "progressive"

        base_headers = formats[0].get("http_headers") or info.get("http_headers") or {}
        headers = self._apply_overrides(base_headers, req, referer)
        log.info("yt-dlp resolved %s via %s (%s)", redact(target), info.get("extractor_key"), kind)
        return Resolved(
            kind=kind,
            urls=urls,
            headers=headers,
            cookies=cookies,
            title=info.get("title"),
            is_live=info.get("is_live"),
            via=f"yt-dlp:{info.get('extractor_key') or 'generic'}",
            needs_ffmpeg=needs_ffmpeg,
            container=formats[0].get("ext"),
        )

    async def scan(
        self, target: str, req: CastRequest, http: aiohttp.ClientSession, referer: str | None
    ) -> Resolved:
        headers = self.direct_headers(target, req, referer)
        headers.setdefault("Accept", "text/html,application/xhtml+xml,*/*;q=0.8")
        timeout = aiohttp.ClientTimeout(total=self.cfg.upstream_timeout)
        try:
            async with http.get(upstream_url(target), headers=headers, timeout=timeout) as resp:
                if resp.status >= 400:
                    raise ResolveError(f"HTTP {resp.status}")
                body = (await resp.content.read(4 << 20)).decode("utf-8", "replace")
                base = str(resp.url)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise ResolveError(type(e).__name__) from e

        text = html.unescape(body.replace("\\/", "/").replace("\\u002F", "/").replace("\\u0026", "&"))
        found: list[str] = []
        for m in re.finditer(r"https?://[^\s\"'<>\\]+", text):
            if MEDIA_HINT_RE.search(m.group(0)):
                found.append(m.group(0))
        for m in re.finditer(r"[\"'](/[^\"'\s<>]+\.(?:m3u8|mpd)[^\"'\s<>]*)[\"']", text):
            found.append(urljoin(base, m.group(1)))
        candidate = pick_candidate(found)
        if not candidate:
            raise ResolveError("no stream links in page")
        sub = CastRequest(**{**req.to_dict(), "referer": req.referer or target})
        r = await self.direct(candidate, sub, http, via="scan")
        return r

    async def browser(self, req: CastRequest, http: aiohttp.ClientSession) -> Resolved:
        from .browser import sniff  # playwright drags in a lot; only load it when used
        from .curlclient import CurlClient

        # The point of a browser-resolved source is a CDN that only trusts browser
        # fingerprints, so probe it with one too — aiohttp would just 403.
        client = CurlClient(self.cfg)
        try:
            return await sniff(req, self.cfg, self.probe, client)
        finally:
            await client.close()


class _YtdlpLogger:
    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)
