"""One cast = one StreamSession. It owns the upstream HTTP client (headers + cookie jar),
the playlist/segment key map, the segment cache, token refresh and any FFmpeg job."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shutil
import time
from collections import OrderedDict, deque
from http.cookies import SimpleCookie
from http.cookiejar import Cookie
from typing import Any, Callable

import aiohttp
from yarl import URL

from . import hls
from .config import Config
from .resolver import CastRequest, Resolved, Resolver
from .transcode import FFmpegJob
from .util import extension_of, redact, stable_key, upstream_url

log = logging.getLogger("relay.session")

AUTH_STATUSES = {401, 403, 410}
RETRYABLE_STATUSES = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
MEDIA_EXTS = {"ts", "aac", "m4s", "mp4", "m4a", "m4v", "mp3", "ac3", "ec3", "vtt", "webvtt", "cmfv", "cmfa", "fmp4", "key"}
CONTENT_TYPES = {
    "m3u8": "application/vnd.apple.mpegurl",
    "ts": "video/mp2t",
    "aac": "audio/aac",
    "m4s": "video/iso.segment",
    "mp4": "video/mp4",
    "m4a": "audio/mp4",
    "mp3": "audio/mpeg",
    "vtt": "text/vtt",
    "webvtt": "text/vtt",
}


class UpstreamError(Exception):
    def __init__(self, status: int, message: str = ""):
        super().__init__(message or f"upstream HTTP {status}")
        self.status = status


class LRU(OrderedDict):
    def __init__(self, maxlen: int):
        super().__init__()
        self.maxlen = maxlen

    def __setitem__(self, key: Any, value: Any) -> None:
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        while len(self) > self.maxlen:
            self.popitem(last=False)


class SegmentCache:
    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        self.size = 0
        self.items: OrderedDict[str, tuple[bytes, str]] = OrderedDict()

    def get(self, key: str) -> tuple[bytes, str] | None:
        item = self.items.get(key)
        if item:
            self.items.move_to_end(key)
        return item

    def put(self, key: str, body: bytes, ctype: str) -> None:
        if len(body) > self.max_bytes // 4:
            return
        if key in self.items:
            self.size -= len(self.items.pop(key)[0])
        self.items[key] = (body, ctype)
        self.size += len(body)
        while self.size > self.max_bytes and self.items:
            _, (old, _) = self.items.popitem(last=False)
            self.size -= len(old)

    def clear(self) -> None:
        self.items.clear()
        self.size = 0


class StreamSession:
    def __init__(self, cfg: Config, req: CastRequest, resolver: Resolver):
        self.cfg = cfg
        self.req = req
        self.resolver = resolver
        self.sid = secrets.token_hex(4)
        self.state = "resolving"  # resolving | ready | error | stopped
        self.error: str | None = None
        self.generation = 0  # bumped when the TV should reload the stream
        self.title = req.title
        self.is_live: bool | None = None
        self.kind: str | None = None  # "hls" | "progressive" | "ffmpeg"
        self.resolved: Resolved | None = None
        self.entry_url: str | None = None
        self.headers: dict[str, str] = {}
        self.started_at = time.time()
        self.events: deque[dict] = deque(maxlen=80)
        self.stats = {"playlists": 0, "segments": 0, "bytes": 0, "retries": 0, "refreshes": 0, "failures": 0, "resigns": 0}

        self.resources: LRU = LRU(30000)  # key -> (upstream url, role)
        self._seen_url: LRU = LRU(30000)  # absolute url -> when a playlist first pointed at it
        self.playlist_keys: LRU = LRU(64)  # playlist key -> segment keys it pointed at, in order
        self.seg_pos: LRU = LRU(30000)  # segment key -> (playlist key, position in it)
        self.bad_variants: set[str] = set()  # variants whose segments keep getting rejected
        self.next_segments: LRU = LRU(30000)  # key -> [following segment keys]
        self.active_playlists: LRU = LRU(4)  # media playlist keys the TV is following
        self.cache = SegmentCache(cfg.segment_cache_mb * 1024 * 1024)
        self.inflight: dict[str, asyncio.Future] = {}
        self.ffmpeg: FFmpegJob | None = None

        self._prefetch_sem = asyncio.Semaphore(2)
        self._refresh_lock = asyncio.Lock()
        self._last_refresh = 0.0
        self._last_refresh_ok = False
        self._skip_expiry: float | None = None
        self._tasks: set[asyncio.Task] = set()
        self.closed = False
        self._t_resolved = time.time()

        connector = aiohttp.TCPConnector(limit=24, ssl=None if cfg.verify_tls else False)
        self.http = aiohttp.ClientSession(
            connector=connector,
            cookie_jar=aiohttp.CookieJar(unsafe=True),
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=10),
        )

    # -- bookkeeping -------------------------------------------------------------

    def event(self, msg: str) -> None:
        self.events.append({"t": round(time.time(), 1), "msg": msg})
        log.info("[%s] %s", self.sid, msg)

    def spawn(self, coro: Any) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    @property
    def format(self) -> str:
        return "mp4" if self.kind == "progressive" else "hls"

    def status(self, base_url: str, compact: bool = False) -> dict:
        ready = self.state == "ready"
        path = "/now-playing.mp4" if self.kind == "progressive" else "/now-playing.m3u8"
        data: dict[str, Any] = {
            "session_id": self.sid,
            "generation": self.generation,
            "state": self.state,
            "title": self.title or redact(self.req.url),
            "format": self.format,
            "live": bool(self.is_live) if self.is_live is not None else self.kind != "progressive",
            "stream_url": f"{base_url}{path}?sid={self.sid}&g={self.generation}" if ready else None,
            "error": self.error,
        }
        if compact:
            return data
        expires = self.resolved.expires_at if self.resolved else None
        data.update(
            {
                "kind": self.kind,
                "via": self.resolved.via if self.resolved else None,
                "source": self.req.describe(),
                "upstream": redact(self.entry_url),
                "started_at": self.started_at,
                "expires_at": expires,
                "stats": self.stats,
                "events": list(self.events),
            }
        )
        return data

    # -- lifecycle -------------------------------------------------------------------

    async def start(self) -> None:
        self.event(f"Casting {redact(self.req.url)} (mode {self.req.mode})")
        try:
            await self._apply(await self.resolver.resolve(self.req, self.http), initial=True)
        except Exception as e:
            if self.closed:
                return
            self.error = str(e) or type(e).__name__
            self.state = "error"
            self.event(f"Could not start: {self.error}")
            return
        if self.closed:
            return
        self.state = "ready"
        self.event("Ready")
        self.spawn(self._expiry_loop())

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.state = "stopped"
        for t in list(self._tasks):
            t.cancel()
        if self.ffmpeg:
            await self.ffmpeg.stop()
        shutil.rmtree(os.path.join(self.cfg.work_dir, self.sid), ignore_errors=True)
        await self.http.close()
        self.cache.clear()

    async def _adopt_client(self, r: Resolved) -> None:
        """A browser-resolved source lives behind a CDN that trusts only a real
        browser's TLS fingerprint. Swap the session's aiohttp client for the
        curl_cffi one (Chrome handshake) so playlists and segments keep passing."""
        from .curlclient import CurlClient

        if isinstance(self.http, CurlClient) or r.via != "browser":
            return
        new = CurlClient(self.cfg)
        for domain, jar_cookies in getattr(self.http.cookie_jar, "_cookies", {}).items():
            host = str(domain).lstrip(".")
            if not host:
                continue
            for name, m in jar_cookies.items():
                if not m.value:
                    continue
                jar = SimpleCookie()
                jar[name] = m.value
                jar[name]["path"] = m["path"] if m["path"] else "/"
                if str(domain).startswith("."):
                    jar[name]["domain"] = str(domain)
                if m["secure"]:
                    jar[name]["secure"] = True
                try:
                    new.cookie_jar.update_cookies(jar, URL(f"https://{host}/"))
                except Exception:
                    pass
        await self.http.close()
        self.http = new
        self.event("Using browser-fingerprint client for this CDN")

    async def _apply(self, r: Resolved, initial: bool) -> None:
        """Adopt a (re)resolved source: set up HLS proxying, direct MP4 or FFmpeg."""
        await self._adopt_client(r)
        self._load_cookies(r.cookies)
        self.resolved = r
        self.title = self.req.title or r.title or self.title
        if r.is_live is not None:
            self.is_live = r.is_live
        self.headers = r.headers
        exp = r.expires_at
        self._t_resolved = time.time()
        self.event(
            f"Resolved via {r.via}: {r.kind}{' (live)' if r.is_live else ''}"
            f"{' as separate audio+video' if len(r.urls) > 1 else ''} {redact(r.url)}"
            + (f", link expires in {int((exp - time.time()) / 60)} min" if exp else "")
        )

        container = (r.container or "mp4").lower()
        use_ffmpeg = (
            r.needs_ffmpeg
            or r.kind == "dash"
            or (r.kind == "progressive" and (self.cfg.progressive_mode == "hls" or container not in ("mp4", "m4v", "mov")))
        )
        new_kind = "ffmpeg" if use_ffmpeg else r.kind
        if not initial and new_kind != self.kind:
            self.generation += 1
        old_kind, self.kind = self.kind, new_kind
        self.entry_url = r.url

        if new_kind == "hls":
            if old_kind == "ffmpeg" and self.ffmpeg:
                await self.ffmpeg.stop()
                self.ffmpeg = None
            await self.entry_playlist(allow_refresh=False)  # validates + primes variant keys
            if not initial:
                await self._refresh_media_playlists()
        elif new_kind == "progressive":
            if self.is_live is None:
                self.is_live = False
        else:
            await self._start_ffmpeg(r, initial)

    async def _start_ffmpeg(self, r: Resolved, initial: bool) -> None:
        live = self.is_live if self.is_live is not None else r.kind in ("hls", "dash")
        self.is_live = live
        restarts = 0
        if self.ffmpeg:
            restarts = self.ffmpeg.restarts + 1
            await self.ffmpeg.stop(remove=not live)
        if not live and not initial:
            self.generation += 1  # VOD restarts from scratch; TV must reload
        out_dir = os.path.join(self.cfg.work_dir, self.sid, "live" if live else f"vod{restarts}")
        job = FFmpegJob(self.cfg, self.sid, r.urls, self._ffmpeg_headers(), live, self.event, out_dir, restarts)
        self.ffmpeg = job
        await job.start()
        await job.wait_ready(self.cfg.ffmpeg_ready_timeout)
        self.spawn(self._supervise_ffmpeg(job))

    def _ffmpeg_headers(self) -> dict[str, str]:
        """ffmpeg can't see our cookie jar, so fold jar cookies into a Cookie header."""
        h = dict(self.headers)
        if self.entry_url:
            jar = self.http.cookie_jar.filter_cookies(URL(self.entry_url))
            pairs = [f"{k}={m.value}" for k, m in jar.items()]
            if h.get("Cookie"):
                pairs.insert(0, h["Cookie"])
            if pairs:
                h["Cookie"] = "; ".join(pairs)
        return h

    async def _supervise_ffmpeg(self, job: FFmpegJob) -> None:
        code = await job.wait_exit()
        if self.closed or job is not self.ffmpeg or job._stopping:
            return
        if not job.live and code == 0:
            self.event("ffmpeg finished converting")
            return
        tail = job.stderr_tail[-1] if job.stderr_tail else ""
        self.event(f"ffmpeg stopped (code {code}) {tail}".strip())
        self.stats["failures"] += 1
        if job.restarts >= 8:
            self.state, self.error = "error", "ffmpeg keeps failing: " + tail
            return
        await asyncio.sleep(min(2 ** job.restarts, 20))
        if self.closed or job is not self.ffmpeg:
            return
        if not await self.refresh("ffmpeg stopped", force=True) and self.resolved and not self.closed:
            try:
                await self._start_ffmpeg(self.resolved, initial=False)
            except Exception as e:
                self.event(f"ffmpeg restart failed: {e}")

    def _load_cookies(self, cookies: list[Cookie]) -> None:
        for c in cookies:
            if c.value is None or not c.domain:
                continue
            jar = SimpleCookie()
            try:
                jar[c.name] = c.value
            except Exception:
                continue
            m = jar[c.name]
            m["domain"] = c.domain
            m["path"] = c.path or "/"
            if c.secure:
                m["secure"] = True
            self.http.cookie_jar.update_cookies(jar, URL(f"https://{c.domain.lstrip('.')}/"))

    # -- token refresh ---------------------------------------------------------------

    async def refresh(self, reason: str, force: bool = False) -> bool:
        """Re-run resolution to get fresh URLs/tokens. Concurrent callers share one run."""
        if self.closed:
            return False
        waited = self._refresh_lock.locked()
        async with self._refresh_lock:
            if self.closed:
                return False
            since = time.monotonic() - self._last_refresh
            if waited or (since < self.cfg.refresh_cooldown and not force):
                return self._last_refresh_ok
            self._last_refresh = time.monotonic()
            self.event(f"Refreshing stream link ({reason})")
            try:
                r = await self.resolver.resolve(self.req, self.http)
                await self._apply(r, initial=False)
            except Exception as e:
                self._last_refresh_ok = False
                self.stats["failures"] += 1
                self.event(f"Refresh failed: {e}")
                return False
            self.stats["refreshes"] += 1
            self._last_refresh_ok = True
            if self.state == "error":
                self.state, self.error = "ready", None
                self.generation += 1
            return True

    async def resign_segment(self, key: str) -> bool:
        """Some CDNs sign segment URLs very briefly and vary the signing per playlist
        poll. Re-poll the playlist that listed this segment and, if it now lists the
        same position under a new URL, point the TV's key at the fresh URL."""
        loc = self.seg_pos.get(key)
        if not loc or loc[0] not in self.resources:
            return False
        pl, pos = loc
        try:
            await self.playlist_resource(pl, allow_refresh=False)
        except Exception as e:
            log.debug("re-sign poll of %s failed: %s", pl, e)
            return False
        fresh = self.playlist_keys.get(pl) or []
        if not fresh:
            return False
        new_key = fresh[min(pos, len(fresh) - 1)]
        entry = self.resources.get(new_key)
        if not entry or new_key == key:
            return False
        self.resources[key] = entry
        self.stats["resigns"] += 1
        if self.stats["resigns"] % 5 == 1:
            self.event("Re-signing segment links from playlist re-polls (CDN signs them very briefly)")
        return True

    async def _expiry_loop(self) -> None:
        while not self.closed:
            exp = self.resolved.expires_at if self.resolved else None
            if exp is None or exp == self._skip_expiry:
                await asyncio.sleep(30)
                continue
            wait = exp - time.time() - 90
            if wait > 0:
                await asyncio.sleep(min(wait, 60))
                continue
            await self.refresh("link about to expire", force=True)
            new_exp = self.resolved.expires_at if self.resolved else None
            if new_exp == exp:
                self._skip_expiry = exp  # refreshing didn't give us a newer link
            await asyncio.sleep(10)

    async def _refresh_media_playlists(self) -> None:
        """After a refresh, re-read the playlists the TV follows so segment keys point at
        freshly signed URLs before the TV asks for them."""
        for key in list(self.active_playlists.keys()):
            try:
                await self.playlist_resource(key, allow_refresh=False)
            except Exception as e:
                log.debug("re-prime %s failed: %s", key, e)

    # -- upstream fetching -------------------------------------------------------------

    async def fetch(
        self, url_fn: Callable[[], str], role: str, allow_refresh: bool = True, key: str | None = None
    ) -> tuple[bytes, str, str]:
        """GET with retries/backoff. Auth failures (and 404s on playlists) trigger a
        re-resolve, after which url_fn() yields the refreshed URL."""
        attempt = 0
        refreshed = False
        rehash = False
        timeout = aiohttp.ClientTimeout(total=self.cfg.upstream_timeout * (2 if role == hls.SEGMENT else 1))
        while True:
            url = url_fn()
            status = 0
            try:
                async with self.http.get(upstream_url(url), headers=self.headers, timeout=timeout) as resp:
                    status = resp.status
                    age = time.monotonic() - self._seen_url.get(url, self._t_resolved)
                    if status < 400:
                        body = await resp.read()
                        if role == hls.SEGMENT:
                            log.debug("segment fetched ok %.1fs after playlist: %dB", age, len(body))
                        return body, resp.headers.get("Content-Type", ""), str(resp.url)
                    snippet = (await resp.content.read(200))[:200]
                    log.info(
                        "upstream %d on %s %.1fs after playlist: %s [ct=%r sent=%s] %r",
                        status, role, age, url, resp.headers.get("Content-Type", ""), sorted(self.headers), snippet[:60],
                    )
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.debug("fetch %s failed: %r", redact(url), e)
                status = 0

            is_playlist = role in hls.PLAYLIST_ROLES or role == "entry"
            if status in AUTH_STATUSES and role == hls.SEGMENT and key:
                if not rehash:
                    rehash = True
                    if await self.resign_segment(key):
                        continue
                loc = self.seg_pos.get(key)
                if loc and loc[0].startswith(("v", "m")) and loc[0] not in self.bad_variants:
                    self.bad_variants.add(loc[0])
                    self.generation += 1
                    self.event(f"Variant {loc[0]} keeps getting rejected upstream - dropping it, the TV will use the other quality")
            if (status in AUTH_STATUSES or (status == 404 and is_playlist)) and allow_refresh and not refreshed:
                refreshed = True
                if await self.refresh(f"HTTP {status} on {role}"):
                    continue
            if (status == 0 or status in RETRYABLE_STATUSES) and attempt < self.cfg.upstream_retries:
                attempt += 1
                self.stats["retries"] += 1
                await asyncio.sleep(min(0.4 * 2**attempt, 4))
                continue
            self.stats["failures"] += 1
            raise UpstreamError(status or 502, f"upstream {'unreachable' if not status else f'HTTP {status}'} for {role} {redact(url)}")

    async def open_stream(self, url_fn: Callable[[], str], range_header: str | None) -> aiohttp.ClientResponse:
        """Open a streaming upstream response (progressive MP4 / byte-range segments).
        Caller must release() it."""
        headers = dict(self.headers)
        headers["Accept-Encoding"] = "identity"
        if range_header:
            headers["Range"] = range_header
        attempt, refreshed = 0, False
        while True:
            url = url_fn()
            try:
                resp = await self.http.get(
                    upstream_url(url), headers=headers, auto_decompress=False,
                    timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=self.cfg.upstream_timeout * 2),
                )
            except (aiohttp.ClientError, asyncio.TimeoutError):
                resp, status = None, 0
            else:
                status = resp.status
                if status < 400:
                    return resp
                resp.release()
            if status in AUTH_STATUSES and not refreshed:
                refreshed = True
                if await self.refresh(f"HTTP {status} on video"):
                    continue
            if (status == 0 or status in RETRYABLE_STATUSES) and attempt < self.cfg.upstream_retries:
                attempt += 1
                self.stats["retries"] += 1
                await asyncio.sleep(min(0.4 * 2**attempt, 4))
                continue
            raise UpstreamError(status or 502)

    # -- HLS -----------------------------------------------------------------------------

    def _mapper(self, positional: bool) -> hls.Mapper:
        def mapper(absolute: str, role: str, idx: int) -> str:
            if role in hls.PLAYLIST_ROLES:
                # Variants in the entry master get positional keys (v0, v1, m0...) so a
                # refreshed master with brand-new URLs still lines up with what the TV has.
                key = f"{role[0]}{idx}" if positional else stable_key(absolute)
                ext = "m3u8"
            else:
                key = stable_key(absolute)
                ext = extension_of(absolute, "ts")
                if role == hls.SEGMENT and ext not in MEDIA_EXTS:
                    ext = "ts"  # disguised segments (.jpg, .png, .html...) are usually TS
                elif role == hls.KEY:
                    ext = "key"
                elif ext not in MEDIA_EXTS:
                    ext = "bin"
            self.resources[key] = (absolute, role)
            self._seen_url[absolute] = time.monotonic()
            return f"/hls/{self.sid}/{key}.{ext}"

        return mapper

    def _after_rewrite(self, playlist_key: str, rw: hls.Rewritten) -> None:
        self.stats["playlists"] += 1
        if rw.is_master:
            return
        self.active_playlists[playlist_key] = time.time()
        keys = [u.rsplit("/", 1)[-1].split(".", 1)[0] for u in rw.segments]
        self.playlist_keys[playlist_key] = keys
        for pos, k in enumerate(keys):
            self.seg_pos[k] = (playlist_key, pos)
        n = self.cfg.prefetch_segments
        if n <= 0 or not keys:
            return
        for i, k in enumerate(keys):
            self.next_segments[k] = keys[i + 1 : i + 1 + n]
        if not rw.endlist:
            for k in keys[-n:]:
                self._prefetch(k)

    async def entry_playlist(self, allow_refresh: bool = True) -> str:
        if self.kind != "hls" or not self.entry_url:
            raise UpstreamError(404, "no HLS stream")
        body, ctype, final = await self.fetch(lambda: self.entry_url or "", "entry", allow_refresh)
        if not hls.looks_like_playlist(body, ctype):
            raise UpstreamError(502, "upstream did not return an HLS playlist")
        text = hls.drop_master_variants(body.decode("utf-8", "replace"), self.bad_variants)
        rw = hls.rewrite(text, final, self._mapper(positional=True))
        self._after_rewrite("entry", rw)
        return rw.text

    async def playlist_resource(self, key: str, allow_refresh: bool = True) -> str:
        entry = self.resources.get(key)
        if not entry:
            raise UpstreamError(404, "unknown playlist")
        role = entry[1]
        body, ctype, final = await self.fetch(lambda: self.resources[key][0], role, allow_refresh)
        if not hls.looks_like_playlist(body, ctype):
            raise UpstreamError(502, "upstream did not return an HLS playlist")
        rw = hls.rewrite(body.decode("utf-8", "replace"), final, self._mapper(positional=False))
        self._after_rewrite(key, rw)
        return rw.text

    def resource_role(self, key: str) -> str | None:
        entry = self.resources.get(key)
        return entry[1] if entry else None

    def resource_url(self, key: str) -> str:
        entry = self.resources.get(key)
        if not entry:
            raise UpstreamError(404, "unknown resource")
        return entry[0]

    async def segment(self, key: str) -> tuple[bytes, str]:
        cached = self.cache.get(key)
        if cached:
            self._prefetch_next(key)
            return cached
        result = await self._fetch_segment(key)
        self._prefetch_next(key)
        return result

    async def _fetch_segment(self, key: str) -> tuple[bytes, str]:
        """Fetch once per key even if the TV and the prefetcher ask at the same time.
        The download runs in its own task so a TV disconnect doesn't abort it."""
        task = self.inflight.get(key)
        if task is None:
            role = self.resource_role(key)
            if role is None:
                raise UpstreamError(404, "unknown resource")

            async def run() -> tuple[bytes, str]:
                try:
                    body, ctype, _ = await self.fetch(lambda: self.resource_url(key), role, key=key)
                    self.stats["segments"] += 1
                    self.stats["bytes"] += len(body)
                    self.cache.put(key, body, ctype)
                    return body, ctype
                finally:
                    self.inflight.pop(key, None)

            task = self.spawn(run())
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
            self.inflight[key] = task
        return await asyncio.shield(task)

    def _prefetch_next(self, key: str) -> None:
        for k in self.next_segments.get(key, ()):
            self._prefetch(k)

    def _prefetch(self, key: str) -> None:
        if self.closed or key in self.inflight or self.cache.get(key) or key not in self.resources:
            return

        async def run() -> None:
            async with self._prefetch_sem:
                if key in self.inflight or self.cache.get(key):
                    return
                try:
                    await self._fetch_segment(key)
                except Exception as e:
                    log.debug("prefetch %s failed: %s", key, e)

        self.spawn(run())
