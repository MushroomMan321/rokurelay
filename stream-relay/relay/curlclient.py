"""Upstream HTTP client with a real browser's TLS/JA3 + HTTP/2 fingerprint.

Some anti-leech CDNs (the strmd/embed.st family) answer plain aiohttp with 403 no
matter which headers you send — they look at the TLS handshake. curl_cffi replays
Chrome's fingerprint, which passes. This adapter exposes the slice of the
aiohttp.ClientSession API that StreamSession and Resolver use, so sources resolved
by the headless browser keep fetching through that same fingerprint for the whole
playback (playlists, keys, segments, byte ranges).
"""

from __future__ import annotations

import asyncio
from http.cookies import SimpleCookie
from typing import Any, AsyncIterator

import aiohttp
from curl_cffi import requests as cc
from multidict import CIMultiDict
from yarl import URL

DEFAULT_IMPERSONATION = "chrome"


class CurlContent:
    """aiohttp-style .content on top of curl_cffi streaming."""

    def __init__(self, resp: Any):
        self._resp = resp
        self._it: Any = None
        self._buf = b""
        self._eof = False

    async def _fill(self) -> bool:
        """Pull one more chunk into the buffer; False at EOF."""
        if self._eof:
            return False
        try:
            if self._it is None:
                self._it = self._resp.aiter_content(1 << 16)
            self._buf += await anext(self._it)
            return True
        except StopAsyncIteration:
            self._eof = True
            return False
        except (asyncio.CancelledError, asyncio.TimeoutError):
            raise
        except Exception as e:
            self._eof = True
            raise aiohttp.ClientError(str(e)) from e

    async def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            while await self._fill():
                pass
            out, self._buf = self._buf, b""
            return out
        while len(self._buf) < n and await self._fill():
            pass
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    async def iter_chunked(self, n: int) -> AsyncIterator[bytes]:
        while True:
            while len(self._buf) >= n:
                out, self._buf = self._buf[:n], self._buf[n:]
                yield out
            if self._eof:
                if self._buf:
                    out, self._buf = self._buf, b""
                    yield out
                return
            await self._fill()


class CurlResponse:
    def __init__(self, resp: Any):
        self._resp = resp
        self.status = int(resp.status_code)
        items = getattr(resp.headers, "multi_items", None)
        self.headers = CIMultiDict(items() if items else resp.headers.items())
        self.url = URL(str(resp.url or ""))
        self.content = CurlContent(resp)

    async def read(self) -> bytes:
        return await self.content.read()

    async def text(self, encoding: str | None = None, errors: str = "strict") -> str:
        raw = await self.content.read()
        cs = encoding or ""
        if not cs:
            cs = (self.headers.get("Content-Type", "").split("charset=")[-1] or "").split(";")[0].strip() or "utf-8"
        try:
            return raw.decode(cs, errors)
        except LookupError:
            return raw.decode("utf-8", errors)

    def release(self) -> None:
        try:
            asyncio.get_running_loop().create_task(self._discard())
        except RuntimeError:
            pass

    async def _discard(self) -> None:
        try:
            await self._resp.aclose()
        except Exception:
            pass


class _Request:
    """So both `async with http.get(...)` and `resp = await http.get(...)` work."""

    def __init__(self, coro):
        self._coro = coro
        self._resp: CurlResponse | None = None

    def __await__(self):
        return self._coro.__await__()

    async def __aenter__(self) -> CurlResponse:
        self._resp = await self._coro
        return self._resp

    async def __aexit__(self, *exc) -> None:
        if self._resp is not None:
            self._resp.release()


class CurlClient:
    """Drop-in stand-in for the pieces of aiohttp.ClientSession the relay uses."""

    def __init__(self, cfg, impersonate: str = DEFAULT_IMPERSONATION):
        self._impersonate = impersonate
        self.cookie_jar = aiohttp.CookieJar(unsafe=True)
        verify: Any = True if getattr(cfg, "verify_tls", True) else False
        self._sess = cc.AsyncSession(impersonate=impersonate, verify=verify, timeout=None)

    def get(
        self,
        url: Any,
        headers: Any = None,
        timeout: Any = None,
        allow_redirects: bool = True,
        auto_decompress: bool = True,
        **kw: Any,
    ) -> _Request:
        return _Request(self._get(str(url), dict(headers or {}), timeout, allow_redirects))

    async def _get(self, url: str, headers: dict, timeout: Any, allow_redirects: bool) -> CurlResponse:
        seen_names = {p.split("=", 1)[0].strip() for p in headers.get("Cookie", "").split(";") if "=" in p}
        jar_pairs = [f"{k}={m.value}" for k, m in self.cookie_jar.filter_cookies(URL(url)).items() if k not in seen_names]
        if jar_pairs:
            headers["Cookie"] = "; ".join(x for x in (headers.get("Cookie"), "; ".join(jar_pairs)) if x)
        total = getattr(timeout, "total", None)
        try:
            resp = await self._sess.get(url, headers=headers, timeout=total, allow_redirects=allow_redirects, stream=True)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            raise
        except Exception as e:
            if "timeout" in type(e).__name__.lower() or "timeout" in str(e).lower():
                raise asyncio.TimeoutError(str(e)) from e
            raise aiohttp.ClientError(str(e)) from e
        r = CurlResponse(resp)
        self._absorb(r, str(resp.url or url))
        return r

    def _absorb(self, resp: CurlResponse, url: str) -> None:
        for value in [v for k, v in resp.headers.items() if k.lower() == "set-cookie"]:
            jar = SimpleCookie()
            try:
                jar.load(value)
            except Exception:
                continue
            if jar:
                try:
                    self.cookie_jar.update_cookies(jar, URL(url))
                except Exception:
                    pass

    async def close(self) -> None:
        try:
            await self._sess.close()
        except Exception:
            pass
