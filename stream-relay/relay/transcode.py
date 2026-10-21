"""FFmpeg jobs that turn non-HLS sources (MP4, DASH, separate yt-dlp audio/video,
RTMP...) into an HLS stream on disk that the relay can serve."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from collections import deque
from typing import Callable

from .config import Config
from .util import header_lines, redact

log = logging.getLogger("relay.ffmpeg")

ROKU_VIDEO = {"h264", "hevc"}
ROKU_AUDIO = {"aac", "ac3", "eac3", "mp3"}


def _input_args(url: str, headers: dict[str, str]) -> list[str]:
    hdrs = {k: v for k, v in headers.items() if k.lower() != "user-agent"}
    args: list[str] = []
    ua = headers.get("User-Agent") or headers.get("user-agent")
    if ua:
        args += ["-user_agent", ua]
    if hdrs:
        args += ["-headers", header_lines(hdrs)]
    if url.lower().startswith(("http://", "https://")):
        args += [
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_on_network_error", "1",
            "-reconnect_delay_max", "5",
            "-rw_timeout", "20000000",
        ]
    return args + ["-i", url]


async def probe_codecs(url: str, headers: dict[str, str], timeout: float = 20.0) -> dict[str, str]:
    """Return {"video": codec, "audio": codec} for the first streams of each type."""
    if not shutil.which("ffprobe"):
        return {}
    cmd = ["ffprobe", "-v", "error"]
    ua = headers.get("User-Agent")
    if ua:
        cmd += ["-user_agent", ua]
    other = {k: v for k, v in headers.items() if k.lower() != "user-agent"}
    if other:
        cmd += ["-headers", header_lines(other)]
    cmd += ["-show_entries", "stream=codec_type,codec_name", "-of", "json", "-i", url]
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except (asyncio.TimeoutError, OSError):
        if proc and proc.returncode is None:
            proc.kill()
        return {}
    result: dict[str, str] = {}
    try:
        for s in json.loads(out or b"{}").get("streams", []):
            t = s.get("codec_type")
            if t in ("video", "audio") and t not in result:
                result[t] = s.get("codec_name", "")
    except json.JSONDecodeError:
        pass
    return result


class FFmpegJob:
    def __init__(
        self,
        cfg: Config,
        sid: str,
        inputs: list[str],
        headers: dict[str, str],
        live: bool,
        on_event: Callable[[str], None],
        out_dir: str,
        restarts: int = 0,
    ):
        self.cfg = cfg
        self.sid = sid
        self.inputs = inputs
        self.headers = headers
        self.live = live
        self.on_event = on_event
        self.out_dir = out_dir
        self.playlist = os.path.join(self.out_dir, "index.m3u8")
        self.proc: asyncio.subprocess.Process | None = None
        self.stderr_tail: deque[str] = deque(maxlen=20)
        self.restarts = restarts
        self._stopping = False
        self._watch: asyncio.Task | None = None
        self.codecs: dict[str, str] = {}

    async def build_command(self) -> list[str]:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin", "-y"]
        for url in self.inputs:
            cmd += _input_args(url, self.headers)

        # Probe once; copy codecs the Roku can play, re-encode the rest.
        if not self.codecs:
            if len(self.inputs) == 1:
                self.codecs = await probe_codecs(self.inputs[0], self.headers)
            else:
                v = await probe_codecs(self.inputs[0], self.headers)
                a = await probe_codecs(self.inputs[1], self.headers)
                self.codecs = {"video": v.get("video", ""), "audio": a.get("audio", "")}
        vcodec, acodec = self.codecs.get("video"), self.codecs.get("audio")

        if len(self.inputs) > 1:
            cmd += ["-map", "0:v:0", "-map", "1:a:0"]
        else:
            cmd += ["-map", "0:v:0?", "-map", "0:a:0?"]

        if vcodec in ROKU_VIDEO:
            cmd += ["-c:v", "copy"]
        else:
            enc = self.cfg.ffmpeg_video_encoder
            cmd += ["-c:v", enc]
            if enc == "libx264":
                cmd += ["-preset", self.cfg.ffmpeg_preset, "-crf", "21", "-pix_fmt", "yuv420p",
                        "-maxrate", "8M", "-bufsize", "16M"]
            cmd += ["-force_key_frames", f"expr:gte(t,n_forced*{self.cfg.hls_time})",
                    "-vf", "scale='min(1920,iw)':-2"]
        if acodec in ROKU_AUDIO:
            cmd += ["-c:a", "copy"]
        else:
            cmd += ["-c:a", "aac", "-b:a", "160k", "-ac", "2"]

        cmd += ["-f", "hls", "-hls_time", str(self.cfg.hls_time), "-hls_segment_type", "mpegts",
                "-hls_base_url", f"/ff/{self.sid}/",
                "-hls_segment_filename", os.path.join(self.out_dir, "seg_%05d.ts")]
        if self.live:
            cmd += ["-hls_list_size", "15",
                    "-hls_flags", "delete_segments+append_list+discont_start+temp_file+omit_endlist"]
        else:
            cmd += ["-hls_playlist_type", "event", "-hls_list_size", "0", "-hls_flags", "temp_file"]
        cmd.append(self.playlist)
        return cmd

    async def start(self) -> None:
        os.makedirs(self.out_dir, exist_ok=True)
        cmd = await self.build_command()
        self.on_event(
            f"ffmpeg: video={self.codecs.get('video') or '?'} audio={self.codecs.get('audio') or '?'}"
            f" -> {'copy' if self.codecs.get('video') in ROKU_VIDEO else 'transcode'}"
            f" from {', '.join(redact(u) for u in self.inputs)}"
        )
        self.proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self._watch = asyncio.create_task(self._drain(self.proc))

    async def _drain(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr
        async for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                # tokens appear in ffmpeg error lines; scrub query strings
                line = re.sub(r"\?[^\s'\"]+", "?...", line)
                self.stderr_tail.append(line)
                log.debug("[%s] %s", self.sid, line)
        await proc.wait()

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    def segment_count(self) -> int:
        try:
            with open(self.playlist, encoding="utf-8") as f:
                return sum(1 for line in f if line.startswith("#EXTINF"))
        except OSError:
            return 0

    async def wait_ready(self, timeout: float) -> None:
        need = 2 if self.live else 1
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if self.segment_count() >= need:
                return
            if self.proc and self.proc.returncode is not None:
                if self.segment_count() >= 1:
                    return  # short VOD finished fast
                raise RuntimeError("ffmpeg exited: " + (self.stderr_tail[-1] if self.stderr_tail else f"code {self.proc.returncode}"))
            await asyncio.sleep(0.5)
        raise RuntimeError("ffmpeg produced no segments in time")

    async def wait_exit(self) -> int | None:
        if self._watch:
            await self._watch
        return self.proc.returncode if self.proc else None

    def read_playlist(self) -> str | None:
        try:
            with open(self.playlist, encoding="utf-8") as f:
                return f.read()
        except OSError:
            return None

    def file_path(self, name: str) -> str | None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or name.startswith("."):
            return None
        path = os.path.join(self.out_dir, name)
        return path if os.path.isfile(path) else None

    async def stop(self, remove: bool = True) -> None:
        self._stopping = True
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), 5)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    self.proc.kill()
                except ProcessLookupError:
                    pass
        if self._watch:
            self._watch.cancel()
        if remove:
            shutil.rmtree(self.out_dir, ignore_errors=True)
