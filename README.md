# RokuRelay

Cast a web video to your Roku from your phone — even when the site is stubborn about it.

Your phone hands a link to a small relay you run on any Linux box on the LAN (Docker image
included). The relay finds the real stream behind the page — yt-dlp, a page scan, or a
headless-Chromium sniff for players built by script inside cross-origin iframes — fetches it
with the headers, cookies and TLS fingerprint the site expects, and re-serves it on one fixed
LAN address. A sideloaded Roku channel plays that address and heals itself when playback breaks.
Think *Web Video Caster*, but open source, server-side, and it also works from an iOS share sheet.

```
 phone ──(link / sniffed .m3u8 / cookies)──▶  relay (Docker, :8080)  ──(Referer, UA, cookies, tokens)──▶  site / CDN
                                               │  yt-dlp · page scan · headless browser · FFmpeg
                                              │  rewrites playlists, retries, refreshes tokens
                                              ▼
 Roku ◀──── ECP: wake TV + launch channel ─── relay
 Roku ────── GET /now-playing.m3u8 ─────────▶ relay      (polls /api/status every 3 s)
```

```
stream-relay/    the relay: Python 3.12 + aiohttp + yt-dlp + FFmpeg, Docker image + compose
roku-channel/    SceneGraph channel: one Video node, a status poller, self-healing
tools/           roku_channel.py (package + sideload to every Roku), make_icons.py
```

## Why, when Web Video Caster / Chromecast / DLNA exist?

- **Roku has no receiver app.** WVC and friends work by running the player *on your phone* and
  pushing playable URLs to the Roku's player. This runs the player *on the Roku* and fixes
  everything upstream of it, so the TV gets a clean, permanent, self-healing HLS feed.
- **It survives real anti-leech setups**, which is where simple yt-dlp-to-Roku scripts die:
  Referer/UA/cookie checks, tokens that expire mid-film, segments re-signed every few seconds,
  CDNs that reject non-browser TLS handshakes (curl_cffi replays Chrome's), load-balanced
  backends where one quality variant is broken (the relay quarantines it and the TV switches),
  and DASH/MKV/RTMP sources (FFmpeg remuxes to HLS; codecs the Roku plays are stream-copied).
- **One tap from the phone** via a bookmarklet (Android/desktop) or a Safari share-sheet
  shortcut (iOS) that hands over the exact URL + headers + cookies the page gave your phone.
- [castor](https://github.com/stupside/castor) is the closest sibling and worth a look too —
  CLI-first, covers DLNA/Chromecast/Roku. RokuRelay is relay-first: a persistent service with a
  phone UI, its own channel, and the playlist surgery above.

## Quick start

**1. Run the relay** (Linux; `network_mode: host` in the compose file — on Docker Desktop swap
in the `ports:` mapping):

```bash
git clone https://github.com/MushroomMan321/rokurelay && cd rokurelay/stream-relay
cp .env.example .env        # set ROKU_DEVICES (see below)
docker compose up -d --build
```

Open `http://<server-ip>:8080` — add it to your phone's home screen. ⚠️ **Casting is
unauthenticated by default** — set `RELAY_TOKEN` if anything beyond your own LAN can reach it.

`ROKU_DEVICES=Living Room=X000000AAAAA,Bedroom=X000000BBBBB` uses serial numbers
(Settings → System → About, or `http://<roku-ip>:8060/query/device-info`), so nothing needs a
DHCP reservation: when a Roku's IP changes, the relay re-finds it by scanning the subnet's Roku
control ports and remembers it in `data/rokus.json`.

**2. Sideload the channel** on each Roku. Enable developer mode (Home ×3, Up ×2, Right, Left,
Right, Left, Right; set a password), and set *Settings → System → Advanced system settings →
Control by mobile apps → Network access* to **Default** (or **Permissive** if launches return 403).
Then, on the relay box (it knows each Roku's current IP by then):

```bash
python tools/roku_channel.py deploy
```

Or `package` the zip and upload it at `http://<roku-ip>` as `rokudev`.

**3. Cast.** Paste a link on the relay page, or drag the bookmarklet (from the page) to your
bookmarks bar: start the video, run the bookmark, pick the TV. On iOS, run
[`ios-shortcut.js`](stream-relay/relay/static/ios-shortcut.js) in a Share Sheet shortcut that
POSTs the result to `/api/cast` — it forwards cookies too, which is why the phone works where
the bookmarklet can't. The relay wakes the TV, launches the channel, and pushes the cast in —
no remote needed.

## How a link gets resolved (`mode=auto`)

1. A stream URL your phone already loaded (sent by the bookmarklet/shortcut)
2. The link itself, if it is a manifest or video file
3. **yt-dlp** on the page, then on any embedded player iframes
4. A scan of the page HTML for `.m3u8` / `.mpd` links
5. **Headless-browser sniff** — the relay loads the page in its own Chromium and records what
   the player actually fetches (the Web Video Caster trick, server-side). Only runs when the
   fast paths fail; costs ~5–10 s; `BROWSER_SNIFF=0` disables.

Browser-resolved sources keep playing through the browser's TLS fingerprint (curl_cffi), since
CDNs that need the sniff usually also 403 plain HTTP clients.

Once playing, the relay rewrites every URI in every playlist (variants, renditions, segments,
keys, init sections) through itself, reads token expiry out of URLs and re-resolves before it
lapses, re-signs seconds-lived segment URLs by re-polling their playlist (~100 ms, no
re-resolve), drops quality variants whose segments keep getting rejected, retries flaky CDNs
with backoff, and prefetches upcoming segments into RAM. `data/last_cast.json` resumes the last
stream after a restart.

## The channel self-heals

Polls `/api/status` every 3 s and plays new sessions or reloads when the relay bumps the
stream *generation*. On a player error it reloads with backoff, reloads on a 15 s freeze or a
25 s stall, and after 3 failed attempts asks the relay to re-resolve (`/api/heal`).
Remote: **Up** shows state, **\*** refreshes the link, **Back** exits.

## API (handy for automation)

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/now-playing.m3u8` · `/now-playing.mp4` | The current cast; fixed addresses |
| GET | `/api/status` | `?compact=1` for the TV |
| POST | `/api/cast` | `url`, `stream_url`, `candidates[]`, `frames[]`, `referer`, `user_agent`, `cookies`, `headers`, `mode` (`auto`/`direct`/`ytdlp`/`scan`/`browser`/`ffmpeg`), `devices[]`, `launch`, `wait` |
| GET/POST | `/share` | Same fields as query params (also `text=`) — pairs with Android *HTTP Shortcuts* |
| POST | `/api/refresh` · `/api/stop` · `/api/relaunch` · `/api/discover` · `/api/heal` | |

`RELAY_TOKEN` adds `X-Relay-Token` to control endpoints; TV-facing polling stays open.
Query strings, cookies and tokens never appear in logs.

## Limitations

- **DRM (Widevine/FairPlay): no.** Nothing can relay those; the relay passes through
  unencrypted HLS it can find.
- **HttpOnly cookies** a page won't expose: export a Netscape `cookies.txt` to
  `stream-relay/data/cookies.txt` and yt-dlp will use it.
- Sports/piracy embed sites rotate hosts and break extractors constantly — restarting the
  container updates yt-dlp (`YTDLP_AUTO_UPDATE=1`) and the browser sniff adapts, but
  expectations belong on the floor here. Check your local rules about what you cast.
- The relay is one cast at a time (it models *the* now-playing stream, deliberately).

## Development

```bash
cd stream-relay
pip install -r requirements-dev.txt
python -m pytest        # unit tests + end-to-end against a fake hostile streaming site (needs ffmpeg)
```

The e2e suite runs a fake site that enforces Referer, expires tokens, 503s randomly and labels
segments `image/png`; it exercises scanning, sniffed links, retries, refresh, Range
passthrough, FFmpeg conversion and the Roku ECP launch logic. Lint the channel without a Roku:
`cd roku-channel && npm install && npm run lint`.

`smoke_browser.py` resolves a real JS-heavy page end-to-end inside the container — handy when a
site changes its mind.

## License

MIT — see [LICENSE](LICENSE).
