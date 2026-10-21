"""The Web Video Caster trick, server-side: load the page in a headless browser and
record which media URL its player actually requests.

Unlike a bookmarklet or a page scan this sees everything the engine fetches, so it
covers players that build their stream URL with script inside a cross-origin iframe.
Needs the ``playwright`` package plus the Chromium build (the Docker image ships
both); disable with ``BROWSER_SNIFF=0``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from .resolver import CastRequest, ResolveError, Resolved
from .util import extension_of, guess_kind, pick_candidate, redact

log = logging.getLogger("relay.browser")

BLOCKED = ("image", "font")  # keep ads' pixels from burning the time budget


@dataclass
class Seen:
    url: str
    referer: str | None
    ctype: str


def _host(url: str | None) -> str:
    try:
        return urlsplit(url or "").hostname or ""
    except ValueError:
        return ""


def _kind(s: Seen) -> str | None:
    return guess_kind(s.url, s.ctype)


def pick_media(seen: list[Seen], page_hosts: set[str]) -> Seen | None:
    """Best stream among the URLs the browser watched: manifests over progressive
    files, and requests made by the page's own frames over third-party ads."""
    related = [s for s in seen if page_hosts and _host(s.referer) in page_hosts]
    for group in (related, seen):
        for kind in ("hls", "dash", "progressive"):
            by_url: dict[str, Seen] = {}
            for s in group:
                if _kind(s) == kind:
                    by_url.setdefault(s.url, s)
            best = pick_candidate(list(by_url))
            if best:
                return by_url[best]
    return None


def _cookies(raw: str, url: str) -> list[dict]:
    out = []
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip():
                out.append({"name": k.strip(), "value": v.strip(), "url": url})
    return out


async def sniff(req: CastRequest, cfg, probe, http) -> Resolved:
    """Open the page (and its player frames) in headless Chromium and resolve the
    media URL the player loads. ``probe`` is Resolver.probe."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        raise ResolveError("playwright is not installed") from e

    ua = req.user_agent or req.client_user_agent or cfg.default_user_agent
    targets = [f for f in req.frames if f != req.url] + [req.url]  # the player frame usually *is* the page
    page_hosts = {_host(t) for t in targets} | {_host(req.referer)}
    seen: list[Seen] = []
    deadline = time.monotonic() + max(5.0, cfg.browser_timeout)

    try:
        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(headless=True)
            except Exception as e:
                msg = " ".join(str(e).split())[:200]
                hint = " (run: playwright install chromium)" if "doesn't exist" in msg or "not found" in msg else ""
                raise ResolveError(f"chromium unavailable{hint}") from e
            try:
                context = await browser.new_context(user_agent=ua, ignore_https_errors=not cfg.verify_tls)
                if req.cookies:
                    await context.add_cookies(_cookies(req.cookies, targets[0]))
                async def block_images(route) -> None:
                    if route.request.resource_type in BLOCKED:
                        await route.abort()
                    else:
                        await route.continue_()

                await context.route("**/*", block_images)
                page = await context.new_page()

                def on_response(resp) -> None:
                    try:
                        ct = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                        if guess_kind(resp.url, ct) is not None:
                            seen.append(Seen(resp.url, resp.request.headers.get("referer"), ct))
                    except Exception:
                        pass

                page.on("response", on_response)
                for target in targets:
                    remaining = deadline - time.monotonic()
                    if remaining <= 2:
                        break
                    try:
                        await page.goto(target, wait_until="domcontentloaded", timeout=int(min(15000, remaining * 1000)))
                    except Exception:
                        pass  # ad-padded pages often never settle; sniff whatever loaded
                    grace = min(5.0, max(1.0, deadline - time.monotonic() - 1))
                    chosen = None
                    step = 0.5
                    waited = 0.0
                    while waited < grace:  # let the player ask for its manifest, then stop early
                        await asyncio.sleep(step)
                        waited += step
                        chosen = pick_media(seen, page_hosts)
                        if chosen and _kind(chosen) in ("hls", "dash"):
                            break
                    if chosen and _kind(chosen) in ("hls", "dash"):
                        break
            finally:
                await browser.close()
    except ResolveError:
        raise
    except Exception as e:
        raise ResolveError(f"browser failed: {type(e).__name__}: {' '.join(str(e).split())[:160]}") from e

    def req_headers(s: Seen) -> dict[str, str]:
        h = {"User-Agent": ua}
        if s.referer:
            h["Referer"] = s.referer
            if _host(s.referer) != _host(s.url):
                h["Origin"] = f"{urlsplit(s.referer).scheme}://{_host(s.referer)}"
        if req.cookies:
            h["Cookie"] = req.cookies
        return h

    chosen = None
    kind = None
    is_live = None
    rejected: set[str] = set()
    why = "browser saw no media requests"
    for _ in range(4):  # a pick that fails its probe may be a stale/decoy playlist: try the next
        pick = pick_media([s for s in seen if s.url not in rejected], page_hosts)
        if pick is None:
            break
        try:
            try:
                kind, is_live = await probe(pick.url, req_headers(pick), http)
            except ResolveError:
                await asyncio.sleep(0.7)  # load balancer pools behind these sites are flaky
                kind, is_live = await probe(pick.url, req_headers(pick), http)
        except ResolveError as e:
            rejected.add(pick.url)
            why = f"browser pick {redact(pick.url)} failed its probe: {e}"
            continue
        if kind is None:
            rejected.add(pick.url)
            why = f"browser pick {redact(pick.url)} is not a stream"
            continue
        chosen = pick
        break
    if chosen is None:
        raise ResolveError(why)
    headers = req_headers(chosen)
    log.info("browser resolved %s (%s, %d requests watched)", redact(chosen.url), kind, len(seen))
    return Resolved(
        kind=kind,
        urls=[chosen.url],
        headers=headers,
        is_live=is_live,
        via="browser",
        container=extension_of(chosen.url, "mp4"),
    )
