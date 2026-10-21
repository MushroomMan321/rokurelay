"""End-to-end: a fake streaming site that checks Referer, issues expiring tokens and
fails some requests, with the relay in front of it."""

import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web

from relay.config import Config
from relay.server import create_app

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("media")
    mp4 = d / "video.mp4"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25",
         "-f", "lavfi", "-i", "sine=frequency=440", "-t", "8", "-c:v", "libx264", "-g", "50",
         "-c:a", "aac", "-shortest", "-movflags", "+faststart", str(mp4)],
        check=True,
    )
    hlsdir = d / "hls"
    hlsdir.mkdir()
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-i", str(mp4), "-c", "copy", "-f", "hls", "-hls_time", "2",
         "-hls_list_size", "0", "-hls_segment_filename", str(hlsdir / "seg%d.ts"), str(hlsdir / "index.m3u8")],
        check=True,
    )
    return d


class FakeSite:
    """/page embeds a tokenised master URL; everything else needs Referer + valid token."""

    def __init__(self, media: Path):
        self.media = media
        self.ttl = 60.0
        self.fail_once = {"seg1.ts"}
        self.hits: dict[str, int] = {}
        self.port = 0

    def token(self) -> str:
        return str(time.time())

    def valid(self, request: web.Request) -> bool:
        tok = request.query.get("token", "0")
        referer_ok = "/page" in request.headers.get("Referer", "")
        return referer_ok and time.time() - float(tok) < self.ttl

    def app(self) -> web.Application:
        app = web.Application()

        async def page(request):
            url = f"http://127.0.0.1:{self.port}/live/master.m3u8?token={self.token()}"
            js = json.dumps({"src": url}).replace("/", "\\/")
            return web.Response(text=f"<html><script>var player={js};</script></html>", content_type="text/html")

        async def master(request):
            if not self.valid(request):
                raise web.HTTPForbidden()
            tok = request.query["token"]
            body = f"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=500000,RESOLUTION=320x240\nv/index.m3u8?token={tok}\n"
            return web.Response(text=body, content_type="application/vnd.apple.mpegurl")

        async def variant(request):
            if not self.valid(request):
                raise web.HTTPForbidden()
            tok = request.query["token"]
            text = (self.media / "hls" / "index.m3u8").read_text()
            text = "\n".join(f"{l}?token={tok}" if l.endswith(".ts") else l for l in text.splitlines())
            return web.Response(text=text, content_type="application/vnd.apple.mpegurl")

        async def segment(request):
            name = request.match_info["name"]
            self.hits[name] = self.hits.get(name, 0) + 1
            if name in self.fail_once:
                self.fail_once.discard(name)
                raise web.HTTPServiceUnavailable()
            if not self.valid(request):
                raise web.HTTPForbidden()
            return web.FileResponse(self.media / "hls" / name, headers={"Content-Type": "image/png"})

        async def video(request):
            if "/page" not in request.headers.get("Referer", ""):
                raise web.HTTPForbidden()
            return web.FileResponse(self.media / "video.mp4")

        app.router.add_get("/page", page)
        app.router.add_get("/live/master.m3u8", master)
        app.router.add_get("/live/v/index.m3u8", variant)
        app.router.add_get("/live/v/{name}", segment)
        app.router.add_get("/files/video.mp4", video)
        return app


async def start(app: web.Application) -> tuple[web.AppRunner, int]:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, port


def run(coro):
    return asyncio.run(coro)


async def with_stack(media: Path, tmp: Path, body):
    fake = FakeSite(media)
    up_runner, fake.port = await start(fake.app())
    cfg = Config(data_dir=str(tmp / "data"), work_dir=str(tmp / "work"), refresh_cooldown=0.5,
                 resume_on_start=False, upstream_retries=3, hls_time=2, prefetch_segments=2)
    relay_runner, relay_port = await start(create_app(cfg))
    try:
        async with aiohttp.ClientSession(base_url=f"http://127.0.0.1:{relay_port}") as http:
            await body(fake, http)
    finally:
        await relay_runner.cleanup()
        await up_runner.cleanup()


async def cast(http, **payload):
    async with http.post("/api/cast", json={"launch": False, "wait": True, **payload}) as r:
        assert r.status == 200, await r.text()
        return await r.json()


async def get_text(http, path, expect=200):
    async with http.get(path) as r:
        assert r.status == expect, f"{path}: {r.status} {await r.text()}"
        return await r.text()


def test_page_scan_proxy_retry_and_token_refresh(media, tmp_path):
    async def body(fake, http):
        idle = await (await http.get("/api/status")).json()
        assert idle["state"] == "idle"

        page = f"http://127.0.0.1:{fake.port}/page"
        st = await cast(http, url=page, mode="scan")
        assert st["state"] == "ready", st
        assert st["via"] == "scan" and st["format"] == "hls"

        master = await get_text(http, "/now-playing.m3u8")
        assert "token=" not in master
        variant_path = next(l for l in master.splitlines() if l.startswith("/hls/"))
        assert variant_path.endswith("/v0.m3u8")

        media_pl = await get_text(http, variant_path)
        segs = [l for l in media_pl.splitlines() if l.startswith("/hls/")]
        assert len(segs) == 4 and all(s.endswith(".ts") for s in segs)

        original = (media / "hls" / "seg0.ts").read_bytes()
        async with http.get(segs[0]) as r:
            assert r.status == 200 and r.headers["Content-Type"] == "video/mp2t"
            assert await r.read() == original
        async with http.get(segs[1]) as r:  # upstream 503s once; relay retries
            assert r.status == 200

        # Expire every token: playlists and segments must recover via a re-scan.
        fake.ttl = 0.5
        await asyncio.sleep(1.0)
        fake.ttl = 60.0
        before = fake.hits.get("seg3.ts", 0)
        media_pl2 = await get_text(http, variant_path)
        assert media_pl2.count("/hls/") == 4
        async with http.get(segs[3]) as r:
            assert r.status == 200
        assert fake.hits.get("seg3.ts", 0) >= before

        status = await (await http.get("/api/status")).json()
        assert status["stats"]["refreshes"] >= 1, status["events"]
        assert status["stats"]["retries"] >= 1

    run(with_stack(media, tmp_path, body))


def test_sniffed_candidate_uses_page_as_referer(media, tmp_path):
    async def body(fake, http):
        page = f"http://127.0.0.1:{fake.port}/page"
        master = f"http://127.0.0.1:{fake.port}/live/master.m3u8?token={fake.token()}"
        st = await cast(http, url=page, candidates=[master], client_user_agent="PhoneUA/1.0")
        assert st["state"] == "ready" and st["via"] == "sniffed", st
        assert "/hls/" in await get_text(http, "/now-playing.m3u8")

    run(with_stack(media, tmp_path, body))


def test_progressive_passthrough_supports_ranges(media, tmp_path):
    async def body(fake, http):
        url = f"http://127.0.0.1:{fake.port}/files/video.mp4"
        st = await cast(http, url=url, referer=f"http://127.0.0.1:{fake.port}/page")
        assert st["state"] == "ready" and st["format"] == "mp4", st
        assert "/now-playing.mp4" in st["stream_url"]
        async with http.get("/now-playing.mp4", headers={"Range": "bytes=0-99"}) as r:
            assert r.status == 206
            assert len(await r.read()) == 100
            assert r.headers["Content-Range"].startswith("bytes 0-99/")

    run(with_stack(media, tmp_path, body))


def test_ffmpeg_converts_to_hls(media, tmp_path):
    async def body(fake, http):
        url = f"http://127.0.0.1:{fake.port}/files/video.mp4"
        st = await cast(http, url=url, referer=f"http://127.0.0.1:{fake.port}/page", mode="ffmpeg")
        assert st["state"] == "ready" and st["kind"] == "ffmpeg", st
        playlist = await get_text(http, "/now-playing.m3u8")
        assert "#EXT-X-START:TIME-OFFSET=0" in playlist  # VOD starts at the beginning
        seg = next(l for l in playlist.splitlines() if l.startswith("/ff/"))
        async with http.get(seg) as r:
            assert r.status == 200 and len(await r.read()) > 1000
        assert any("copy" in e["msg"] for e in st["events"]), st["events"]

        await (await http.post("/api/stop", json={})).json()
        assert (await (await http.get("/api/status")).json())["state"] == "idle"
        await get_text(http, "/now-playing.m3u8", expect=404)

    run(with_stack(media, tmp_path, body))


def test_bad_link_reports_error(media, tmp_path):
    async def body(fake, http):
        st = await cast(http, url=f"http://127.0.0.1:{fake.port}/nope", mode="scan")
        assert st["state"] == "error" and "404" in st["error"]
        async with http.post("/api/cast", json={"url": "ftp://x"}) as r:
            assert r.status == 400

    run(with_stack(media, tmp_path, body))


def test_token_required_when_configured(media, tmp_path, monkeypatch):
    async def go():
        cfg = Config(data_dir=str(tmp_path / "d"), work_dir=str(tmp_path / "w"), api_token="s3cret", resume_on_start=False)
        runner, port = await start(create_app(cfg))
        try:
            async with aiohttp.ClientSession(base_url=f"http://127.0.0.1:{port}") as http:
                async with http.post("/api/cast", json={"url": "https://x/a.m3u8", "launch": False}) as r:
                    assert r.status == 401
                async with http.get("/api/status") as r:
                    assert r.status == 200  # the TV polls this without a token
        finally:
            await runner.cleanup()

    run(go())
