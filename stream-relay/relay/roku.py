"""Roku External Control Protocol (ECP): wake the TV, open the sideloaded channel and
hand it the new session.

Rokus are tracked by serial number, so DHCP address changes don't break anything: when
a Roku isn't where we last saw it, the local subnet is swept for ECP (TCP 8060) and it
is found again. The sweep is plain unicast TCP, so it works where SSDP multicast is
filtered (managed Wi-Fi) or its replies are firewalled.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import socket
import time
import xml.etree.ElementTree as ET
from dataclasses import replace
from urllib.parse import urlencode

import aiohttp

from .config import Config, RokuDevice
from .util import upstream_url

log = logging.getLogger("relay.roku")

ECP_PORT = 8060
SCAN_COOLDOWN = 30.0


def _split(host: str) -> tuple[str, int]:
    if ":" in host:
        h, p = host.rsplit(":", 1)
        return h, int(p)
    return host, ECP_PORT


def local_ip_towards(host: str) -> str:
    """The address of this machine on the route to `host` (no packets are sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((_split(host)[0], 9))
        return s.getsockname()[0]
    finally:
        s.close()


class RokuController:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.found: dict[str, RokuDevice] = {}  # serial -> last known (name, host)
        self._scan_lock = asyncio.Lock()
        self._last_scan = 0.0
        self._load()

    # -- persistence ---------------------------------------------------------------

    @property
    def _state_file(self) -> str:
        return os.path.join(self.cfg.data_dir, "rokus.json")

    def _load(self) -> None:
        try:
            with open(self._state_file, encoding="utf-8") as f:
                for serial, d in json.load(f).items():
                    self.found[serial] = RokuDevice(name=d["name"], host=d["host"], serial=serial)
        except (OSError, ValueError, KeyError):
            pass

    def _save(self) -> None:
        try:
            os.makedirs(self.cfg.data_dir, exist_ok=True)
            with open(self._state_file, "w", encoding="utf-8") as f:
                json.dump({s: {"name": d.name, "host": d.host} for s, d in self.found.items()}, f, indent=1)
        except OSError as e:
            log.warning("could not save Roku addresses: %s", e)

    # -- device list ---------------------------------------------------------------

    @property
    def devices(self) -> list[RokuDevice]:
        """Configured Rokus with their latest known address (everything found only with
        ROKU_DEVICES=auto)."""
        if not self.cfg.roku_devices:
            return sorted(self.found.values(), key=lambda d: d.name) if self.cfg.roku_auto else []
        out = []
        for d in self.cfg.roku_devices:
            known = self.found.get(d.serial or "")
            out.append(replace(d, host=known.host) if known else d)
        return out

    def select(self, wanted: list[str] | None) -> list[RokuDevice]:
        """Match configured Rokus by name, serial or host. Anything else is ignored, so a
        request can't steer launches at arbitrary devices on the network."""
        if not wanted or any(w.lower() == "all" for w in wanted):
            return self.devices
        out = []
        for w in wanted:
            wl = w.lower()
            match = next(
                (d for d in self.devices if wl in (d.name.lower(), d.host.lower(), (d.serial or "").lower())), None
            )
            if match and match not in out:
                out.append(match)
        return out

    @property
    def needs_scan(self) -> bool:
        if self.cfg.roku_auto:
            return True
        return any(d.serial and d.serial not in self.found for d in self.cfg.roku_devices)

    # -- ECP -----------------------------------------------------------------------------

    def _url(self, host: str, path: str) -> str:
        h, p = _split(host)
        return f"http://{h}:{p}{path}"

    async def _request(self, method: str, host: str, path: str, timeout: float = 5) -> str:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as http:
            # Keep query escaping as written: Roku 404s on a decoded "relay=http://..." param.
            url = upstream_url(self._url(host, path))
            async with http.request(method, url, data=b"" if method == "POST" else None) as resp:
                text = await resp.text(errors="replace")
                if resp.status >= 400:
                    hint = (" (on the Roku: Settings > System > Advanced system settings >"
                            " Control by mobile apps > Network access: Permissive)") if resp.status == 403 else ""
                    raise RuntimeError(f"ECP {path.split('?')[0]} -> HTTP {resp.status}{hint}")
                return text

    async def device_info(self, host: str, timeout: float = 5) -> dict[str, str]:
        root = ET.fromstring(await self._request("GET", host, "/query/device-info", timeout))
        return {child.tag: (child.text or "") for child in root}

    async def active_app(self, host: str) -> str | None:
        root = ET.fromstring(await self._request("GET", host, "/query/active-app"))
        app = root.find("app")
        return app.get("id") if app is not None else None

    def _remember(self, host: str, info: dict[str, str]) -> RokuDevice | None:
        serial = (info.get("serial-number") or "").upper()
        if not serial:
            return None
        # Rooms ("Family room") tell identical boxes apart better than "Roku Ultra".
        name = (info.get("user-device-location") or info.get("user-device-name")
                or info.get("friendly-device-name") or info.get("model-name") or host)
        dev = RokuDevice(name=name, host=host, serial=serial)
        if self.found.get(serial) != dev:
            self.found[serial] = dev
            self._save()
        return dev

    async def locate(self, dev: RokuDevice) -> tuple[RokuDevice, dict[str, str]]:
        """Return the device at its current address plus its device-info, rescanning
        the network if it has moved."""
        if dev.host:
            try:
                info = await self.device_info(dev.host, timeout=3)
                if not dev.serial or (info.get("serial-number") or "").upper() == dev.serial:
                    self._remember(dev.host, info)
                    return dev, info
            except Exception:
                pass
        if not dev.serial:
            raise RuntimeError(f"unreachable at {dev.host}")
        await self.scan()
        moved = self.found.get(dev.serial)
        if not moved:
            raise RuntimeError(f"serial {dev.serial} not found on the network (off, or a different subnet?)")
        log.info("Roku %s is now at %s", dev.name, moved.host)
        return replace(dev, host=moved.host), await self.device_info(moved.host)

    def relay_base_for(self, dev: RokuDevice) -> str:
        """URL the Roku should use for us: PUBLIC_BASE_URL, else our IP on its network."""
        if self.cfg.public_base_url:
            return self.cfg.public_base_url
        return f"http://{local_ip_towards(dev.host)}:{self.cfg.port}"

    async def cast(self, dev: RokuDevice, session_id: str, relay_base: str | None = None) -> str:
        """Bring the TV to our channel and point it at the session. Returns a summary."""
        dev, info = await self.locate(dev)
        relay_base = relay_base or self.relay_base_for(dev)
        steps = [f"at {_split(dev.host)[0]}"]
        if self.cfg.roku_power_on and info.get("power-mode") not in (None, "", "PowerOn"):
            await self._request("POST", dev.host, "/keypress/PowerOn")
            steps.append("powered on")
            await asyncio.sleep(3)

        params = urlencode({"contentId": session_id, "mediaType": "live", "relay": relay_base})
        app_id = self.cfg.roku_app_id
        current = None
        try:
            current = await self.active_app(dev.host)
        except Exception:
            pass
        sent = False
        if current == app_id:
            # Already running: deliver as an roInputEvent instead of relaunching. Current
            # Roku OS wants the app id in the path; older builds take plain /input.
            for path in (f"/input/{app_id}?{params}", f"/input?{params}"):
                try:
                    await self._request("POST", dev.host, path)
                    steps.append("sent to running channel")
                    sent = True
                    break
                except RuntimeError:
                    continue
        if not sent:
            await self._request("POST", dev.host, f"/launch/{app_id}?{params}", timeout=10)
            steps.append("launched channel")
        return ", ".join(steps)

    # -- discovery ---------------------------------------------------------------------

    async def scan(self, force: bool = False) -> list[RokuDevice]:
        """Find every Roku on the local network via SSDP and a TCP sweep of the subnet."""
        async with self._scan_lock:
            if not force and time.monotonic() - self._last_scan < SCAN_COOLDOWN:
                return list(self.found.values())
            self._last_scan = time.monotonic()
            hosts = set(await self.ssdp_hosts()) | set(await self.sweep_hosts())
            results = await asyncio.gather(*(self.device_info(h, timeout=3) for h in hosts), return_exceptions=True)
            for host, info in zip(hosts, results):
                if isinstance(info, dict):
                    self._remember(host, info)
            log.info("Roku scan: %s", ", ".join(f"{d.name} {d.host}" for d in self.found.values()) or "none")
            return list(self.found.values())

    def _subnet(self) -> ipaddress.IPv4Network | None:
        if self.cfg.roku_scan_subnet:
            return ipaddress.ip_network(self.cfg.roku_scan_subnet, strict=False)
        try:
            ip = local_ip_towards("1.1.1.1")  # interface holding the default route
        except OSError:
            return None
        net = ipaddress.ip_network(f"{ip}/24", strict=False)
        return net if net.is_private else None

    async def sweep_hosts(self, timeout: float = 0.6) -> list[str]:
        net = self._subnet()
        if net is None or net.num_addresses > 1024:
            return []
        sem = asyncio.Semaphore(128)

        async def probe(ip: str) -> str | None:
            async with sem:
                try:
                    _, w = await asyncio.wait_for(asyncio.open_connection(ip, ECP_PORT), timeout)
                    w.close()
                    return ip
                except (OSError, asyncio.TimeoutError):
                    return None

        found = await asyncio.gather(*(probe(str(ip)) for ip in net.hosts()))
        return [ip for ip in found if ip]

    async def ssdp_hosts(self, timeout: float = 2.0) -> list[str]:
        msg = (
            "M-SEARCH * HTTP/1.1\r\nHost: 239.255.255.250:1900\r\n"
            'Man: "ssdp:discover"\r\nST: roku:ecp\r\nMX: 1\r\n\r\n'
        ).encode()
        hosts: set[str] = set()

        class Proto(asyncio.DatagramProtocol):
            def datagram_received(self, data: bytes, addr: tuple) -> None:
                m = re.search(rb"(?im)^location:\s*http://([^:/\s]+)", data)
                if m:
                    hosts.add(m.group(1).decode())

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            sock.bind(("", 0))
            transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(Proto, sock=sock)
        except OSError:
            return []
        try:
            transport.sendto(msg, ("239.255.255.250", 1900))
            await asyncio.sleep(timeout)
        finally:
            transport.close()
        return sorted(hosts)
