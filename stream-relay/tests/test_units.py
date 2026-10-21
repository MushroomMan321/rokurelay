import time

from relay import hls
from relay.resolver import CastRequest
from relay.util import extract_url, guess_kind, parse_expiry, pick_candidate, redact, stable_key

MASTER = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="en",URI="audio/en.m3u8?tok=1"
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360,AUDIO="aud"
low/index.m3u8?tok=1
#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1280x720,AUDIO="aud"
https://cdn2.example.com/hi/index.m3u8?tok=1
"""

MEDIA = """#EXTM3U
#EXT-X-TARGETDURATION:4
#EXT-X-MEDIA-SEQUENCE:100
#EXT-X-KEY:METHOD=AES-128,URI="https://keys.example.com/k?id=9"
#EXT-X-MAP:URI="init.mp4"
#EXTINF:4.0,
seg100.m4s?tok=abc
#EXTINF:4.0,
/abs/seg101.jpg?tok=abc
"""


def collect():
    seen = []

    def mapper(url, role, idx):
        seen.append((role, url, idx))
        return f"/x/{role}{idx}"

    return seen, mapper


def test_rewrite_master_maps_variants_and_renditions():
    seen, mapper = collect()
    rw = hls.rewrite(MASTER, "https://cdn.example.com/live/master.m3u8?tok=1", mapper)
    assert rw.is_master and not rw.endlist
    assert ("media", "https://cdn.example.com/live/audio/en.m3u8?tok=1", 0) in seen
    assert ("variant", "https://cdn.example.com/live/low/index.m3u8?tok=1", 0) in seen
    assert ("variant", "https://cdn2.example.com/hi/index.m3u8?tok=1", 1) in seen
    assert 'URI="/x/media0"' in rw.text
    assert "/x/variant0\n" in rw.text and "/x/variant1\n" in rw.text
    assert "tok=1" not in rw.text


def test_rewrite_media_maps_segments_keys_and_init():
    seen, mapper = collect()
    rw = hls.rewrite(MEDIA, "https://cdn.example.com/live/low/index.m3u8", mapper)
    roles = [r for r, _, _ in seen]
    assert roles == ["key", "map", "segment", "segment"]
    assert seen[2][1] == "https://cdn.example.com/live/low/seg100.m4s?tok=abc"
    assert seen[3][1] == "https://cdn.example.com/abs/seg101.jpg?tok=abc"
    assert rw.segments == ["/x/segment0", "/x/segment1"]
    assert rw.target_duration == 4.0


def test_rewrite_leaves_drm_uris():
    text = '#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://abc"\n#EXTINF:4,\na.ts\n#EXT-X-ENDLIST\n'
    seen, mapper = collect()
    rw = hls.rewrite(text, "https://h/p.m3u8", mapper)
    assert 'URI="skd://abc"' in rw.text and rw.endlist


def test_playlist_sniffing():
    assert hls.looks_like_playlist(b"\xef\xbb\xbf#EXTM3U\n".decode("utf-8"))
    assert hls.looks_like_playlist(b"", "application/vnd.apple.mpegurl")
    assert not hls.looks_like_playlist(b"Not found\n", "application/vnd.apple.mpegurl")
    assert hls.looks_like_playlist(b"## Processed\n#EXTM3U\n#EXT-X-VERSION:3")
    assert not hls.looks_like_playlist(b"<html>")


def test_parse_expiry_variants():
    now = 1_760_000_000
    assert parse_expiry(f"https://a/b.m3u8?expires={now + 600}", now) == now + 600
    assert parse_expiry(f"https://a/b.m3u8?hdnts=st%3D1~exp%3D{now + 300}~acl%3D*", now) == now + 300
    assert parse_expiry(f"https://m.googlevideo.com/api/manifest/hls_variant/expire/{now + 900}/ei/x/file/index.m3u8", now) == now + 900
    t = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    assert parse_expiry(f"https://s3/x.mp4?X-Amz-Date={t}&X-Amz-Expires=3600", now) == now + 3600
    assert parse_expiry("https://a/b.m3u8?e=5", now) is None
    assert parse_expiry("https://a/b.m3u8", now) is None


def test_pick_candidate_prefers_master():
    c = [
        "https://cdn/x/chunklist_b800000.m3u8?t=1",
        "https://cdn/x/master.m3u8?t=1",
        "https://cdn/x/video.mp4",
    ]
    assert pick_candidate(c) == "https://cdn/x/master.m3u8?t=1"
    assert pick_candidate(["https://cdn/x/a.mpd", "https://cdn/v.mp4"]) == "https://cdn/x/a.mpd"
    assert pick_candidate([]) is None


def test_misc_helpers():
    assert extract_url("Watch this: https://example.com/live?id=3.") == "https://example.com/live?id=3"
    assert guess_kind("https://x/y", "application/x-mpegURL") == "hls"
    assert guess_kind("https://x/y.mpd") == "dash"
    assert redact("https://x.com/a/b.m3u8?token=secret") == "https://x.com/a/b.m3u8?..."
    assert stable_key("https://x/a.ts?t=1") == stable_key("https://x/a.ts?t=2")


def test_cast_request_parsing():
    r = CastRequest.from_payload({
        "text": "look https://site.tv/watch/1 now",
        "candidate": ["https://cdn/m.m3u8"],
        "headers": "Origin: https://site.tv\nX-Foo: bar",
        "launch": "0",
        "ua": "PhoneUA",
    })
    assert r.url == "https://site.tv/watch/1"
    assert r.candidates == ["https://cdn/m.m3u8"]
    assert r.headers == {"Origin": "https://site.tv", "X-Foo": "bar"}
    assert r.launch is False and r.client_user_agent == "PhoneUA"
    assert "https" in r.describe()["url"] and r.describe()["has_cookies"] is False


def test_upstream_url_keeps_signed_paths_byte_exact():
    from relay.util import upstream_url

    u = "https://rr1.googlevideo.com/videoplayback/sig/AB%3D%3D/lsig/x%2Fy/file/seg.ts?n=%3D"
    assert str(upstream_url(u)) == u
    assert upstream_url(u).raw_path == "/videoplayback/sig/AB%3D%3D/lsig/x%2Fy/file/seg.ts"
    assert str(upstream_url("https://h/a b.ts")) == "https://h/a%20b.ts"


def test_browser_pick_prefers_frame_manifest_over_ads():
    from relay.browser import Seen, pick_media

    seen = [
        Seen("https://ads.example/spot.mp4?banner=1", "https://adframe.example", "video/mp4"),
        Seen("https://lb1.example/secure/tok/rtmp/stream/t/1/playlist.m3u8", "https://embed.example/e/1", "application/vnd.apple.mpegurl"),
        Seen("https://lb1.example/secure/tok/rtmp/stream/t/1/high/mono.m3u8", "https://embed.example/e/1", "application/vnd.apple.mpegurl"),
        Seen("https://lb1.example/secure/tok/rtmp/stream/t/1/seg-9.ts", "https://embed.example/e/1", "video/mp2t"),
    ]
    pick = pick_media(seen, {"embed.example"})
    assert pick.url.endswith("/1/playlist.m3u8")  # master, not the variant or a segment
    # Nothing referred by the page: still resolves, and an mp4 alone works too.
    assert pick_media(seen, {"other.tv"}).url.endswith("/1/playlist.m3u8")
    only_ad = [Seen("https://ads.example/spot.mp4", "https://adframe.example", "video/mp4")]
    assert pick_media(only_ad, {"embed.example"}).url.endswith("spot.mp4")
    assert pick_media([], {"embed.example"}) is None


def test_browser_stage_runs_last_and_only_when_enabled(monkeypatch):
    import asyncio

    from relay.config import Config
    from relay.resolver import CastRequest, ResolveError, Resolved, Resolver

    async def fake_fail(self, *args, **kwargs):
        raise ResolveError("nothing here")

    async def fake_browser(self, req, http):
        return Resolved(kind="hls", urls=["https://cdn.example/master.m3u8"], headers={}, via="browser")

    async def go(sniff: bool, mode: str = "auto") -> Resolved:
        r = Resolver(Config(browser_sniff=sniff, resume_on_start=False))
        req = CastRequest(url="https://site.tv/watch/1", frames=["https://embed.example/e/1"], mode=mode)
        return await r.resolve(req, None)

    monkeypatch.setattr(Resolver, "direct", fake_fail)
    monkeypatch.setattr(Resolver, "ytdlp", fake_fail)
    monkeypatch.setattr(Resolver, "scan", fake_fail)
    monkeypatch.setattr(Resolver, "browser", fake_browser)

    # Disabled: the error list is the old one and never mentions the browser.
    try:
        asyncio.run(go(False))
        raise AssertionError("expected ResolveError")
    except ResolveError as e:
        assert "browser" not in str(e) and "scan" in str(e)

    # Enabled: browser is the last stage and wins.
    assert asyncio.run(go(True)).via == "browser"

    # Explicit mode goes straight there, even with the flag off.
    assert asyncio.run(go(False, mode="browser")).via == "browser"


def test_drop_master_variants():
    master = (
        "#EXTM3U\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=8000000,RESOLUTION=1920x1080\n"
        "high/mono.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=700000,RESOLUTION=960x540\n"
        "low/mono.m3u8\n"
    )
    assert hls.drop_master_variants(master, set()) == master
    kept = hls.drop_master_variants(master, {"v1"})
    assert "high/mono.m3u8" in kept and "low/mono.m3u8" not in kept and "960x540" not in kept
    kept0 = hls.drop_master_variants(master, {"v0"})
    assert "low/mono.m3u8" in kept0 and "high/mono.m3u8" not in kept0
    assert hls.drop_master_variants(master, {"v0", "v1"}) == master  # all bad: serve everything again


def test_curl_client_mimics_aiohttp_surface():
    import asyncio

    import aiohttp
    from yarl import URL

    from relay import curlclient

    body = b"a" * 65536 * 2 + b"end"

    class FakeResp:
        status_code = 200
        url = "https://cdn.example/x/playlist.m3u8?tok=1"

        class _H:
            @staticmethod
            def items():
                return [("Content-Type", "application/vnd.apple.mpegurl"), ("Set-Cookie", "sid=9; Path=/")]

        headers = _H()

        async def aiter_content(self, chunk_size=None, decode_unicode=False):
            for i in range(0, len(body), 65536):
                yield body[i : i + 65536]

        async def aclose(self):
            pass

    class FakeSess:
        def __init__(self):
            self.closed = False
            self.got_headers = None
            self.got_timeout = None

        async def get(self, url, headers=None, timeout=None, allow_redirects=True, stream=False):
            self.got_headers, self.got_timeout = headers, timeout
            return FakeResp()

        async def close(self):
            self.closed = True

    class Cfg:
        verify_tls = True

    async def go():
        c = curlclient.CurlClient(Cfg())
        fake = FakeSess()
        c._sess = fake
        async with c.get("https://cdn.example/x/playlist.m3u8", headers={"Referer": "https://p.example/"}, timeout=aiohttp.ClientTimeout(total=7)) as resp:
            assert resp.status == 200
            assert resp.headers.get("Content-Type") == "application/vnd.apple.mpegurl"
            assert fake.got_timeout == 7
            head = await resp.content.read(8)
            rest = await resp.content.read()
            assert head + rest == body
        # set-cookie was absorbed and goes out on the next request (awaitable path too)
        resp2 = await c.get("https://cdn.example/x/seg.ts")
        resp2.release()
        assert fake.got_headers["Cookie"] == "sid=9"
        assert c.cookie_jar.filter_cookies(URL("https://cdn.example/"))["sid"].value == "9"
        c3 = curlclient.CurlClient(Cfg())
        c3._sess = fake
        chunks = []
        async with c3.get("https://cdn.example/x/seg.ts") as resp3:
            async for ch in resp3.content.iter_chunked(1000):
                chunks.append(ch)
        assert b"".join(chunks) == body
        await c.close()
        assert fake.closed

    asyncio.run(go())
