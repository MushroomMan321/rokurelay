import asyncio

from relay.config import Config
from relay.curlclient import CurlClient
from relay.resolver import CastRequest, Resolver
from relay.session import StreamSession

URL = "https://streamed.pk/watch/detroit-pistons-vs-boston-celtics-2601665/admin/1"
FRAME = "https://embed.st/embed/admin/ppv-boston-celtics-vs-detroit-pistons/1"


async def main():
    cfg = Config(browser_sniff=True, data_dir="/tmp/sr", resume_on_start=False)
    req = CastRequest.from_payload({"url": URL, "frame": [FRAME], "mode": "auto", "title": "smoke"})
    s = StreamSession(cfg, req, Resolver(cfg))
    await s.start()
    print("state:", s.state, "error:", s.error)
    print("kind:", s.kind, "via:", s.resolved.via if s.resolved else None, "client:", type(s.http).__name__)
    if s.kind == "hls":
        text = await s.entry_playlist()
        print("playlist head:", text[:160].replace("\n", " | "))
        media = await s.playlist_resource("v0")
        print("variant head:", media[:140].replace("\n", " | "))
        seg_keys = [k for k, (_, role) in s.resources.items() if role == "segment"]
        print("segments mapped:", len(seg_keys))
        if seg_keys:
            body, ctype = await s.segment(seg_keys[0])
            print("segment:", seg_keys[0], len(body), "bytes", ctype)
    await s.close()


asyncio.run(main())
