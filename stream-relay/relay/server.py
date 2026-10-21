"""HTTP surface: the phone hand-off page/API, the fixed now-playing address the Roku
plays, and the proxied playlist/segment routes."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from . import hls
from .config import Config
from .resolver import CastRequest, ResolveError, Resolver
from .roku import RokuController
from .session import CONTENT_TYPES, StreamSession, UpstreamError

log = logging.getLogger("relay.server")

STATIC = Path(__file__).parent / "static"
LIST_KEYS = {"candidate", "candidates", "frame", "frames", "device", "devices"}
NO_CACHE = {"Cache-Control": "no-cache, no-store", "Access-Control-Allow-Origin": "*"}


class Relay:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.resolver = Resolver(cfg)
        self.roku = RokuController(cfg)
        self.current: StreamSession | None = None
        self._bg: set[asyncio.Task] = set()

    @property
    def state_file(self) -> str:
        return os.path.join(self.cfg.data_dir, "last_cast.json")

    def spawn(self, coro: Any) -> None:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    async def cast(self, req: CastRequest, request: web.Request | None) -> StreamSession:
        old = self.current
        session = StreamSession(self.cfg, req, self.resolver)
        self.current = session
        if old:
            self.spawn(old.close())
        self.spawn(session.start())
        self._save(req)
        if req.launch and self.cfg.auto_launch:
            self.spawn(self.launch(session, req.devices))
        return session

    async def launch(self, session: StreamSession, wanted: list[str] | None) -> list[dict]:
        """Switch TVs to the channel. With no TVs named (bookmarklet, Shortcut, share
        menu) it uses the ones last picked on the page, so a cast never wakes every TV
        in the house."""
        if self.roku.cfg.roku_auto and not self.roku.found:
            await self.roku.scan()
        if wanted:
            devices = self.roku.select(wanted)
            self._save_targets([d.serial or d.host for d in devices])
        else:
            devices = self.roku.select(self.last_targets) if self.last_targets else []
        if not devices:
            if self.roku.devices:
                session.event("No TV picked yet: choose one on the relay page (TVs with the channel open still play it)")
            else:
                session.event("No Roku configured (ROKU_DEVICES): open the channel on the TV manually")
            return []

        async def one(dev: Any) -> dict:
            try:
                summary = await self.roku.cast(dev, session.sid)
                session.event(f"TV {dev.name}: {summary}")
                return {"device": dev.name, "ok": True, "detail": summary}
            except Exception as e:
                session.event(f"TV {dev.name}: {e}")
                return {"device": dev.name, "ok": False, "detail": str(e)}

        return list(await asyncio.gather(*(one(d) for d in devices)))

    async def stop(self) -> None:
        if self.current:
            await self.current.close()
        self.current = None
        try:
            os.unlink(self.state_file)
        except OSError:
            pass

    @property
    def _targets_file(self) -> str:
        return os.path.join(self.cfg.data_dir, "last_tvs.json")

    @property
    def last_targets(self) -> list[str]:
        try:
            with open(self._targets_file, encoding="utf-8") as f:
                return [str(t) for t in json.load(f)]
        except (OSError, ValueError, TypeError):
            return []

    def _save_targets(self, targets: list[str]) -> None:
        if not targets:
            return
        try:
            os.makedirs(self.cfg.data_dir, exist_ok=True)
            with open(self._targets_file, "w", encoding="utf-8") as f:
                json.dump(targets, f)
        except OSError as e:
            log.warning("could not save TV choice: %s", e)

    def _save(self, req: CastRequest) -> None:
        try:
            os.makedirs(self.cfg.data_dir, exist_ok=True)
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump({"saved_at": time.time(), "request": req.to_dict()}, f)
        except OSError as e:
            log.warning("could not persist last cast: %s", e)

    async def resume(self) -> None:
        try:
            with open(self.state_file, encoding="utf-8") as f:
                data = json.load(f)["request"]
        except (OSError, ValueError, KeyError):
            return
        try:
            req = CastRequest(**{**data, "launch": False})
        except TypeError:
            return
        log.info("resuming last cast")
        await self.cast(req, None)

    async def shutdown(self) -> None:
        if self.current:
            await self.current.close()


# -- helpers --------------------------------------------------------------------------

def _flatten(md: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in set(md.keys()):
        vals = md.getall(k)
        out[k] = vals if (k in LIST_KEYS or len(vals) > 1) else vals[0]
    return out


async def read_payload(request: web.Request) -> dict[str, Any]:
    data = _flatten(request.query)
    if request.method == "POST":
        if request.content_type in ("application/x-www-form-urlencoded", "multipart/form-data"):
            data.update(_flatten(await request.post()))
        else:
            raw = await request.read()
            if raw.strip():
                try:
                    body = json.loads(raw)
                except ValueError as e:
                    raise web.HTTPBadRequest(text="Body must be JSON or a form") from e
                if isinstance(body, dict):
                    data.update(body)
    return data


def check_token(request: web.Request, data: dict[str, Any] | None = None) -> None:
    token = request.app[RELAY_KEY].cfg.api_token
    if not token:
        return
    given = request.headers.get("X-Relay-Token") or (data or {}).get("token") or request.query.get("token")
    if given != token:
        raise web.HTTPUnauthorized(text="Missing or wrong relay token")


RELAY_KEY = web.AppKey("relay", Relay)


def request_base(request: web.Request) -> str:
    """Base URL as the caller sees us, so URLs we hand back resolve for that device."""
    return f"{request.scheme}://{request.host}"


def relay_of(request: web.Request) -> Relay:
    return request.app[RELAY_KEY]


def session_for(request: web.Request, sid: str | None = None) -> StreamSession:
    s = relay_of(request).current
    if s is None or s.closed:
        raise web.HTTPNotFound(text="Nothing is playing", headers=NO_CACHE)
    if sid is not None and sid != s.sid:
        raise web.HTTPGone(text="That stream has been replaced", headers=NO_CACHE)
    return s


def idle_status() -> dict:
    return {"session_id": None, "generation": 0, "state": "idle", "title": None,
            "format": None, "live": False, "stream_url": None, "error": None}


async def _wait_settled(session: StreamSession, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while session.state == "resolving" and time.monotonic() < deadline:
        await asyncio.sleep(0.3)


# -- handlers ---------------------------------------------------------------------------

async def index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


async def static_file(request: web.Request) -> web.StreamResponse:
    name = request.match_info["name"]
    path = (STATIC / name).resolve()
    if path.parent != STATIC.resolve() or not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "no-cache"})


async def api_status(request: web.Request) -> web.Response:
    s = relay_of(request).current
    compact = request.query.get("compact") in ("1", "true")
    if s is None:
        return web.json_response(idle_status(), headers=NO_CACHE)
    return web.json_response(s.status(request_base(request), compact), headers=NO_CACHE)


async def api_cast(request: web.Request) -> web.Response:
    data = await read_payload(request)
    check_token(request, data)
    try:
        req = CastRequest.from_payload(data)
    except ResolveError as e:
        raise web.HTTPBadRequest(text=str(e))
    relay = relay_of(request)
    session = await relay.cast(req, request)
    if str(data.get("wait", "")).lower() in ("1", "true", "yes"):
        await _wait_settled(session, relay.cfg.resolve_timeout + relay.cfg.ffmpeg_ready_timeout)
    return web.json_response(session.status(request_base(request)))


async def share(request: web.Request) -> web.StreamResponse:
    """GET target for bookmarklets, Android share apps and iOS Shortcuts."""
    data = await read_payload(request)
    try:
        check_token(request, data)
    except web.HTTPUnauthorized:
        if "application/json" in request.headers.get("Accept", ""):
            raise
        return web.Response(status=401, content_type="text/html", text=(
            "<meta name=viewport content='width=device-width'><body style='font:18px sans-serif;padding:16px'>"
            "<h3>RokuRelay: missing token</h3><p>This bookmarklet or shortcut was made without your relay token. "
            "Open the relay page once as <code>/?token=YOUR_TOKEN</code>, then copy the bookmarklet again.</p>"
            "<p><a href='/'>Open the relay page</a></p>"))
    try:
        req = CastRequest.from_payload(data)
    except ResolveError as e:
        raise web.HTTPBadRequest(text=str(e))
    session = await relay_of(request).cast(req, request)
    if "application/json" in request.headers.get("Accept", "") or data.get("json"):
        return web.json_response(session.status(request_base(request)))
    raise web.HTTPSeeOther(f"/?cast={session.sid}")


async def api_stop(request: web.Request) -> web.Response:
    check_token(request, await read_payload(request))
    await relay_of(request).stop()
    return web.json_response(idle_status())


async def api_refresh(request: web.Request) -> web.Response:
    check_token(request, await read_payload(request))
    s = session_for(request)
    ok = await s.refresh("requested from phone", force=True)
    return web.json_response({"ok": ok, **s.status(request_base(request), compact=True)})


async def api_heal(request: web.Request) -> web.Response:
    """Called by the Roku channel after repeated playback failures. Rate-limited by the
    refresh cooldown, so it needs no token."""
    s = session_for(request)
    ok = await s.refresh("TV reported playback trouble")
    return web.json_response({"ok": ok})


async def api_relaunch(request: web.Request) -> web.Response:
    data = await read_payload(request)
    check_token(request, data)
    relay = relay_of(request)
    s = session_for(request)
    devices = data.get("devices") or data.get("device")
    results = await relay.launch(s, devices if isinstance(devices, list) else ([devices] if devices else None))
    return web.json_response({"results": results})


async def api_devices(request: web.Request) -> web.Response:
    relay = relay_of(request)
    last = set(relay.last_targets)
    devices = [{**d.to_dict(), "selected": bool({d.serial, d.host} & last)} for d in relay.roku.devices]
    return web.json_response({"devices": devices, "app_id": relay.cfg.roku_app_id})


async def api_discover(request: web.Request) -> web.Response:
    check_token(request, await read_payload(request))
    relay = relay_of(request)
    found = await relay.roku.scan(force=True)
    return web.json_response({"devices": [d.to_dict() for d in found]})


async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "ffmpeg": bool(shutil.which("ffmpeg"))})


async def now_playing(request: web.Request) -> web.StreamResponse:
    """The fixed address: always whatever was cast last."""
    s = session_for(request)
    if s.state != "ready":
        raise web.HTTPServiceUnavailable(text=f"Stream is {s.state}", headers={**NO_CACHE, "Retry-After": "2"})
    if s.kind == "progressive":
        raise web.HTTPFound(f"/now-playing.mp4?sid={s.sid}")
    if s.kind == "ffmpeg":
        text = s.ffmpeg.read_playlist() if s.ffmpeg else None
        if not text:
            raise web.HTTPServiceUnavailable(text="Converter starting", headers={**NO_CACHE, "Retry-After": "2"})
        if not s.is_live and "#EXT-X-START" not in text:
            # A VOD still being converted is an EVENT playlist; without this players
            # jump to its (moving) end instead of starting at the beginning.
            text = text.replace("#EXTM3U\n", "#EXTM3U\n#EXT-X-START:TIME-OFFSET=0,PRECISE=YES\n", 1)
    else:
        text = await s.entry_playlist()
    return web.Response(text=text, content_type=CONTENT_TYPES["m3u8"], headers=NO_CACHE)


async def now_playing_redirect(request: web.Request) -> web.StreamResponse:
    s = session_for(request)
    raise web.HTTPFound("/now-playing.mp4" if s.kind == "progressive" else "/now-playing.m3u8")


async def now_playing_mp4(request: web.Request) -> web.StreamResponse:
    s = session_for(request)
    if s.kind != "progressive" or s.state != "ready":
        raise web.HTTPNotFound(text="Current stream is not a progressive video")
    return await _passthrough(request, s, lambda: s.entry_url or "", "video/mp4")


async def hls_resource(request: web.Request) -> web.StreamResponse:
    s = session_for(request, request.match_info["sid"])
    name = request.match_info["name"]
    key, _, ext = name.partition(".")
    role = s.resource_role(key)
    if role is None:
        raise web.HTTPNotFound(text="Unknown or expired segment", headers=NO_CACHE)
    if role in hls.PLAYLIST_ROLES:
        text = await s.playlist_resource(key)
        return web.Response(text=text, content_type=CONTENT_TYPES["m3u8"], headers=NO_CACHE)
    ctype = CONTENT_TYPES.get(ext, "application/octet-stream")
    if request.headers.get("Range"):
        return await _passthrough(request, s, lambda: s.resource_url(key), ctype)
    body, _ = await s.segment(key)
    return web.Response(body=body, content_type=ctype, headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "max-age=60"})


async def ffmpeg_file(request: web.Request) -> web.StreamResponse:
    s = session_for(request, request.match_info["sid"])
    path = s.ffmpeg.file_path(request.match_info["name"]) if s.ffmpeg else None
    if not path:
        raise web.HTTPNotFound()
    ctype = CONTENT_TYPES["m3u8"] if path.endswith(".m3u8") else "video/mp2t"
    return web.FileResponse(path, headers={"Content-Type": ctype, "Access-Control-Allow-Origin": "*"})


async def _passthrough(request: web.Request, s: StreamSession, url_fn: Any, default_ctype: str) -> web.StreamResponse:
    upstream = await s.open_stream(url_fn, request.headers.get("Range"))
    try:
        out = web.StreamResponse(status=upstream.status)
        for h in ("Content-Length", "Content-Range", "Accept-Ranges", "Last-Modified", "ETag"):
            if h in upstream.headers:
                out.headers[h] = upstream.headers[h]
        ct = upstream.headers.get("Content-Type", "")
        out.headers["Content-Type"] = ct if ct.startswith(("video/", "audio/")) else default_ctype
        out.headers.setdefault("Accept-Ranges", "bytes")
        out.headers["Access-Control-Allow-Origin"] = "*"
        await out.prepare(request)
        if request.method != "HEAD":
            async for chunk in upstream.content.iter_chunked(256 * 1024):
                await out.write(chunk)
                s.stats["bytes"] += len(chunk)
        await out.write_eof()
        return out
    except (ConnectionResetError, asyncio.CancelledError):
        raise
    finally:
        upstream.release()


@web.middleware
async def errors(request: web.Request, handler: Any) -> web.StreamResponse:
    try:
        return await handler(request)
    except UpstreamError as e:
        status = e.status if 400 <= e.status < 600 and e.status not in (401, 407) else 502
        return web.Response(status=status, text=str(e), headers=NO_CACHE)


def create_app(cfg: Config) -> web.Application:
    app = web.Application(middlewares=[errors], client_max_size=4 * 1024 * 1024)
    relay = Relay(cfg)
    app[RELAY_KEY] = relay

    async def on_startup(app: web.Application) -> None:
        shutil.rmtree(cfg.work_dir, ignore_errors=True)
        os.makedirs(cfg.work_dir, exist_ok=True)
        if cfg.resume_on_start:
            await relay.resume()
        if relay.roku.needs_scan:
            relay.spawn(relay.roku.scan())  # find configured Rokus we have no address for

    async def on_cleanup(app: web.Application) -> None:
        await relay.shutdown()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    r = app.router
    r.add_get("/", index)
    r.add_get("/static/{name}", static_file)
    r.add_get("/share", share)
    r.add_post("/share", share)
    r.add_post("/api/cast", api_cast)
    r.add_get("/api/status", api_status)
    r.add_post("/api/stop", api_stop)
    r.add_post("/api/refresh", api_refresh)
    r.add_post("/api/heal", api_heal)
    r.add_post("/api/relaunch", api_relaunch)
    r.add_get("/api/devices", api_devices)
    r.add_post("/api/discover", api_discover)
    r.add_get("/healthz", health)
    r.add_get("/now-playing.m3u8", now_playing)
    r.add_get("/now-playing.mp4", now_playing_mp4)
    r.add_get("/now-playing", now_playing_redirect)
    r.add_get("/hls/{sid}/{name}", hls_resource)
    r.add_get("/ff/{sid}/{name}", ffmpeg_file)
    return app
