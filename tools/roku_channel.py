"""Package the Roku channel and sideload it onto one or more Rokus in developer mode.

    python tools/roku_channel.py package --relay http://192.168.1.10:8080
    python tools/roku_channel.py deploy                     # every Roku in stream-relay/.env
    python tools/roku_channel.py deploy --roku 192.168.1.50 --roku 192.168.1.51
    python tools/roku_channel.py launch --roku 192.168.1.50

ROKU_DEVICES and PUBLIC_BASE_URL are read from stream-relay/.env when not given.
The dev-mode password comes from --password, $ROKU_DEV_PASSWORD, or a prompt.
"""

from __future__ import annotations

import argparse
import getpass
import io
import json
import os
import re
import socket
import sys
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANNEL = ROOT / "roku-channel"
DIST = ROOT / "dist"
ENV_FILE = ROOT / "stream-relay" / ".env"
KNOWN_ROKUS = ROOT / "stream-relay" / "data" / "rokus.json"  # written by the relay
INCLUDE = ("manifest", "source", "components", "images")


def read_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def roku_hosts(arg_hosts: list[str] | None, env: dict[str, str]) -> list[str]:
    """IPs to deploy to. Serial numbers in ROKU_DEVICES are looked up in the addresses
    the relay last saw (stream-relay/data/rokus.json)."""
    if arg_hosts:
        return arg_hosts
    try:
        known = {k.upper(): v["host"] for k, v in json.loads(KNOWN_ROKUS.read_text()).items()}
    except (OSError, ValueError, KeyError):
        known = {}
    values = [p.split("=", 1)[-1].strip() for p in env.get("ROKU_DEVICES", "").split(",") if p.strip()]
    if values == ["auto"]:
        return sorted(known.values())
    if not values:
        sys.exit("ROKU_DEVICES is empty: list your Rokus' serials in stream-relay/.env or pass --roku IP")
    hosts = []
    for v in values:
        if re.fullmatch(r"[A-Za-z0-9]{12}", v):
            if v.upper() not in known:
                sys.exit(f"Don't know where Roku {v} is yet: start the relay once (it scans), or pass --roku IP")
            hosts.append(known[v.upper()])
        else:
            hosts.append(v)
    return hosts


def relay_url_for(host: str, given: str | None, env: dict[str, str]) -> str:
    """The relay's address as that Roku should see it."""
    if given:
        return given
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host.split(":")[0], 9))
        ip = s.getsockname()[0]
    finally:
        s.close()
    return f"http://{ip}:{env.get('RELAY_PORT', '8080')}"


def build_zip(relay_url: str | None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in INCLUDE:
            path = CHANNEL / item
            files = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
            for f in files:
                arc = f.relative_to(CHANNEL).as_posix()
                data = f.read_bytes()
                if arc == "manifest" and relay_url:
                    text = data.decode("utf-8")
                    text = re.sub(r"(?m)^relay_url=.*$", f"relay_url={relay_url.rstrip('/')}", text)
                    data = text.encode("utf-8")
                # PNGs are already compressed
                ztype = zipfile.ZIP_STORED if f.suffix == ".png" else zipfile.ZIP_DEFLATED
                zf.writestr(zipfile.ZipInfo(arc, date_time=(2026, 1, 1, 0, 0, 0)), data, compress_type=ztype)
    return buf.getvalue()


def sideload(host: str, password: str, archive: bytes) -> str:
    boundary = uuid.uuid4().hex
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="mysubmit"\r\n\r\nInstall\r\n'.encode(),
        (f'--{boundary}\r\nContent-Disposition: form-data; name="archive"; filename="stream-relay.zip"\r\n'
         "Content-Type: application/zip\r\n\r\n").encode() + archive + b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    body = b"".join(parts)
    url = f"http://{host}/plugin_install"
    mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    mgr.add_password(None, url, "rokudev", password)
    opener = urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(mgr))
    req = urllib.request.Request(url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with opener.open(req, timeout=60) as resp:
            page = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise RuntimeError("wrong developer password") from e
        raise RuntimeError(f"HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"unreachable ({e.reason}); is developer mode enabled?") from e

    for marker in ("Install Success", "Identical to previous version", "Application Received"):
        if marker in page:
            return marker
    failure = re.search(r"Install Failure[^<\"']*", page) or re.search(r'"text":"([^"]+)","color":"red"', page)
    raise RuntimeError(failure.group(0) if failure else "unexpected response from the Roku")


def ecp_launch(host: str, relay_url: str | None) -> None:
    query = f"?relay={urllib.request.quote(relay_url, safe='')}" if relay_url else ""
    urllib.request.urlopen(urllib.request.Request(f"http://{host}:8060/launch/dev{query}", data=b"", method="POST"), timeout=10)


def main() -> int:
    env = read_env(ENV_FILE)
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("package", "deploy", "launch"):
        sp = sub.add_parser(name)
        sp.add_argument("--relay", default=env.get("PUBLIC_BASE_URL"), help="relay base URL baked into the manifest")
        if name != "package":
            sp.add_argument("--roku", action="append", help="Roku IP (repeatable); default: ROKU_DEVICES from .env")
        if name == "deploy":
            sp.add_argument("--password", default=os.environ.get("ROKU_DEV_PASSWORD"))
            sp.add_argument("--no-launch", action="store_true", help="don't open the channel after installing")
        if name == "package":
            sp.add_argument("--out", default=str(DIST / "stream-relay-roku.zip"))
    args = p.parse_args()

    if args.cmd == "package":
        data = build_zip(args.relay)
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        print(f"Wrote {out} ({len(data) // 1024} KB), relay_url={args.relay or '(manifest default)'}")
        return 0

    hosts = roku_hosts(args.roku, env)
    if not hosts:
        p.error("no Rokus given: pass --roku IP or set ROKU_DEVICES in stream-relay/.env")

    if args.cmd == "launch":
        for host in hosts:
            ecp_launch(host, relay_url_for(host, args.relay, env))
            print(f"{host}: launched")
        return 0

    password = args.password or getpass.getpass("Roku developer password: ")
    failed = 0
    for host in hosts:
        try:
            relay = relay_url_for(host, args.relay, env)
            result = sideload(host, password, build_zip(relay))
            print(f"{host}: {result} (relay {relay})")
            if not args.no_launch:
                ecp_launch(host, relay)
        except Exception as e:
            failed += 1
            print(f"{host}: FAILED - {e}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
