"""Runtime configuration, read once from environment variables."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field


DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

# Prefer the HLS master (adaptive bitrate on the Roku), then H.264/AAC that can be
# remuxed without re-encoding, then whatever is best.
DEFAULT_YTDLP_FORMAT = (
    "best[protocol^=m3u8]"
    "/bv*[vcodec^=avc1][height<=1080]+ba[acodec^=mp4a]"
    "/b[ext=mp4][vcodec^=avc1]"
    "/bv*[height<=1080]+ba"
    "/b"
)


@dataclass(frozen=True)
class RokuDevice:
    name: str
    host: str = ""  # IP or IP:port; may be stale or empty when a serial is given
    serial: str | None = None  # stable identity, survives DHCP address changes

    def to_dict(self) -> dict:
        return {"name": self.name, "host": self.host, "serial": self.serial}


SERIAL_RE = re.compile(r"[A-Za-z0-9]{12}")


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else default


def parse_devices(raw: str) -> list[RokuDevice]:
    """Parse "Living Room=X000000AAAAA, Bedroom=192.168.1.51" (names optional).

    Each value is a serial number (preferred: found again if its IP changes) or an IP.
    "auto" (every Roku found) is handled by Config.roku_auto.
    """
    devices = []
    if raw.strip().lower() == "auto":
        return devices
    for i, part in enumerate(p.strip() for p in raw.split(",")):
        if not part:
            continue
        if "=" in part:
            name, value = (s.strip() for s in part.split("=", 1))
        else:
            name, value = f"Roku {i + 1}", part
        if SERIAL_RE.fullmatch(value):
            devices.append(RokuDevice(name=name, serial=value.upper()))
        else:
            devices.append(RokuDevice(name=name, host=value))
    return devices


@dataclass(frozen=True)
class Config:
    host: str = "0.0.0.0"
    port: int = 8080
    # How the Roku should reach the relay, e.g. http://192.168.1.10:8080. When unset
    # the Host header of the phone's request is used.
    public_base_url: str | None = None
    data_dir: str = "/data"
    work_dir: str = "/tmp/stream-relay"
    api_token: str | None = None

    roku_devices: list[RokuDevice] = field(default_factory=list)
    roku_app_id: str = "dev"
    roku_power_on: bool = True
    auto_launch: bool = True
    roku_scan_subnet: str | None = None  # e.g. 192.168.2.0/24; default: this host's /24
    # Launch on every Roku found. Off unless ROKU_DEVICES=auto: on shared/managed networks
    # most Rokus belong to someone else.
    roku_auto: bool = False

    default_user_agent: str = DEFAULT_UA
    ytdlp_format: str = DEFAULT_YTDLP_FORMAT
    prefer_hls_master: bool = True
    resolve_timeout: float = 60.0
    # Load the page in headless Chromium and watch what the player fetches (last
    # resort for JS-assembled streams, e.g. players inside cross-origin iframes).
    browser_sniff: bool = True
    browser_timeout: float = 25.0

    upstream_timeout: float = 15.0
    upstream_retries: int = 4
    refresh_cooldown: float = 15.0
    segment_cache_mb: int = 192
    prefetch_segments: int = 2
    verify_tls: bool = True

    progressive_mode: str = "direct"  # "direct" proxies MP4 with seeking, "hls" remuxes
    ffmpeg_video_encoder: str = "libx264"
    ffmpeg_preset: str = "veryfast"
    hls_time: int = 4
    ffmpeg_ready_timeout: float = 45.0

    resume_on_start: bool = True

    @property
    def cookies_file(self) -> str:
        return os.path.join(self.data_dir, "cookies.txt")

    @classmethod
    def from_env(cls) -> "Config":
        e = os.environ.get
        return cls(
            host=e("RELAY_HOST", "0.0.0.0"),
            port=_int("RELAY_PORT", 8080),
            public_base_url=(e("PUBLIC_BASE_URL") or "").rstrip("/") or None,
            data_dir=e("DATA_DIR", "/data"),
            work_dir=e("WORK_DIR", "/tmp/stream-relay"),
            api_token=e("RELAY_TOKEN") or None,
            roku_devices=parse_devices(e("ROKU_DEVICES", "")),
            roku_app_id=e("ROKU_APP_ID", "dev"),
            roku_power_on=_bool("ROKU_POWER_ON", True),
            roku_scan_subnet=e("ROKU_SCAN_SUBNET") or None,
            roku_auto=e("ROKU_DEVICES", "").strip().lower() == "auto",
            auto_launch=_bool("AUTO_LAUNCH", True),
            default_user_agent=e("DEFAULT_USER_AGENT", DEFAULT_UA),
            ytdlp_format=e("YTDLP_FORMAT", DEFAULT_YTDLP_FORMAT),
            prefer_hls_master=_bool("PREFER_HLS_MASTER", True),
            resolve_timeout=_float("RESOLVE_TIMEOUT", 60.0),
            browser_sniff=_bool("BROWSER_SNIFF", True),
            browser_timeout=_float("BROWSER_TIMEOUT", 25.0),
            upstream_timeout=_float("UPSTREAM_TIMEOUT", 15.0),
            upstream_retries=_int("UPSTREAM_RETRIES", 4),
            refresh_cooldown=_float("REFRESH_COOLDOWN", 15.0),
            segment_cache_mb=_int("SEGMENT_CACHE_MB", 192),
            prefetch_segments=_int("PREFETCH_SEGMENTS", 2),
            verify_tls=_bool("VERIFY_TLS", True),
            progressive_mode=e("PROGRESSIVE_MODE", "direct"),
            ffmpeg_video_encoder=e("FFMPEG_VIDEO_ENCODER", "libx264"),
            ffmpeg_preset=e("FFMPEG_PRESET", "veryfast"),
            hls_time=_int("HLS_TIME", 4),
            ffmpeg_ready_timeout=_float("FFMPEG_READY_TIMEOUT", 45.0),
            resume_on_start=_bool("RESUME_ON_START", True),
        )
