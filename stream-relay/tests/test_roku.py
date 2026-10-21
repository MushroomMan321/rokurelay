import asyncio

from aiohttp import web

from relay.config import Config, RokuDevice, parse_devices
from relay.roku import RokuController


class FakeRoku:
    def __init__(self, power="DisplayOff", app="562859", serial="X000000AAAAA"):
        self.power, self.app, self.serial, self.calls = power, app, serial, []

    def app_(self):
        app = web.Application()

        async def info(r):
            return web.Response(text=f"<device-info><is-tv>true</is-tv><power-mode>{self.power}</power-mode>"
                                     f"<user-device-name>Den TV</user-device-name><serial-number>{self.serial}</serial-number></device-info>")

        async def active(r):
            return web.Response(text=f'<active-app><app id="{self.app}" type="appl">X</app></active-app>')

        async def post(r):
            # like a real Roku: an unescaped URL in the query string is a 404
            if "relay=http://" in r.raw_path:
                raise web.HTTPNotFound()
            self.calls.append((r.path, dict(r.query)))
            if r.path == "/keypress/PowerOn":
                self.power = "PowerOn"
            if r.path.startswith("/launch/"):
                self.app = r.path.rsplit("/", 1)[-1]
            return web.Response()

        app.router.add_get("/query/device-info", info)
        app.router.add_get("/query/active-app", active)
        app.router.add_post("/{tail:.*}", post)
        return app


async def serve(app):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]


def test_cast_wakes_launches_then_uses_input(tmp_path):
    async def go():
        fake = FakeRoku()
        runner, port = await serve(fake.app_())
        try:
            ctl = RokuController(Config(data_dir=str(tmp_path), roku_devices=[RokuDevice("Den", f"127.0.0.1:{port}")]))
            dev = ctl.select(["den"])[0]
            summary = await ctl.cast(dev, "abc123", "http://10.0.0.5:8080")
            assert summary == "at 127.0.0.1, powered on, launched channel"
            assert fake.calls[0][0] == "/keypress/PowerOn"
            path, query = fake.calls[1]
            assert path == "/launch/dev" and query == {"contentId": "abc123", "mediaType": "live", "relay": "http://10.0.0.5:8080"}

            summary = await ctl.cast(dev, "def456", "http://10.0.0.5:8080")
            assert summary == "at 127.0.0.1, sent to running channel"
            assert fake.calls[-1][0] == "/input/dev" and fake.calls[-1][1]["contentId"] == "def456"
        finally:
            await runner.cleanup()

    asyncio.run(go())


def test_device_selection_and_parsing(tmp_path):
    devs = parse_devices("Living Room=192.168.1.50, 192.168.1.51")
    assert [d.name for d in devs] == ["Living Room", "Roku 2"]
    ctl = RokuController(Config(data_dir=str(tmp_path), roku_devices=devs))
    assert [d.host for d in ctl.select(None)] == ["192.168.1.50", "192.168.1.51"]
    assert [d.host for d in ctl.select(["living room"])] == ["192.168.1.50"]
    assert ctl.select(["192.168.1.99"]) == []  # unconfigured devices are never targeted
    # Nothing configured: found Rokus are listed for discovery but not launched on.
    open_ctl = RokuController(Config(data_dir=str(tmp_path / "x")))
    open_ctl.found["X000000AAAAA"] = RokuDevice("Neighbour", "192.168.1.70", "X000000AAAAA")
    assert open_ctl.devices == [] and open_ctl.select(None) == []
    auto = RokuController(Config(data_dir=str(tmp_path / "x"), roku_auto=True))
    auto.found = dict(open_ctl.found)
    assert [d.name for d in auto.devices] == ["Neighbour"]


def test_serials_parse_and_moved_roku_is_found_again(tmp_path):
    devs = parse_devices("Den=X000000AAAAA, Office=192.168.2.9")
    assert devs[0].serial == "X000000AAAAA" and devs[0].host == ""
    assert devs[1].serial is None and devs[1].host == "192.168.2.9"
    assert parse_devices("auto") == []

    async def go():
        fake = FakeRoku(power="PowerOn")
        runner, port = await serve(fake.app_())
        try:
            cfg = Config(data_dir=str(tmp_path), port=8080, roku_devices=[RokuDevice("Den", "127.0.0.1:1", "X000000AAAAA")])
            ctl = RokuController(cfg)
            new_home = f"127.0.0.1:{port}"

            async def no_ssdp():
                return []

            async def sweep():
                return [new_home]

            ctl.ssdp_hosts, ctl.sweep_hosts = no_ssdp, sweep
            summary = await ctl.cast(ctl.select(["den"])[0], "s1")
            assert summary == "at 127.0.0.1, launched channel"
            # the relay told the Roku its own address on the route to that Roku
            assert fake.calls[-1][1]["relay"] == "http://127.0.0.1:8080"
            assert ctl.devices[0].host == new_home
            # remembered across restarts
            assert RokuController(cfg).devices[0].host == new_home
        finally:
            await runner.cleanup()

    asyncio.run(go())


def test_wrong_serial_at_address_is_not_used(tmp_path):
    async def go():
        fake = FakeRoku(power="PowerOn", serial="X000000BBBBB")
        runner, port = await serve(fake.app_())
        try:
            ctl = RokuController(Config(data_dir=str(tmp_path), roku_devices=[RokuDevice("Den", f"127.0.0.1:{port}", "X000000AAAAA")]))

            async def nothing():
                return []

            ctl.ssdp_hosts = ctl.sweep_hosts = nothing
            try:
                await ctl.cast(ctl.devices[0], "s1")
                raise AssertionError("should not launch on a different Roku")
            except RuntimeError as e:
                assert "not found" in str(e)
            assert fake.calls == []
        finally:
            await runner.cleanup()

    asyncio.run(go())


def test_casts_without_a_tv_go_to_the_last_picked_one(tmp_path):
    import aiohttp

    from relay.server import create_app

    async def wait_for(cond):
        for _ in range(50):
            if cond():
                return
            await asyncio.sleep(0.1)

    async def go():
        den, patio = FakeRoku(power="PowerOn", serial="X000000AAAAA"), FakeRoku(power="PowerOn", serial="X000000BBBBB")
        r1, p1 = await serve(den.app_())
        r2, p2 = await serve(patio.app_())
        cfg = Config(data_dir=str(tmp_path), work_dir=str(tmp_path / "w"), resume_on_start=False,
                     roku_devices=[RokuDevice("Den", f"127.0.0.1:{p1}", "X000000AAAAA"),
                                   RokuDevice("Patio", f"127.0.0.1:{p2}", "X000000BBBBB")])
        relay_runner, port = await serve(create_app(cfg))
        cast = {"url": "https://example.invalid/a.m3u8"}
        try:
            async with aiohttp.ClientSession(base_url=f"http://127.0.0.1:{port}") as http:
                # nothing picked yet: no TV is woken
                await http.post("/api/cast", json=cast)
                await asyncio.sleep(0.5)
                assert den.calls == [] and patio.calls == []

                # pick the patio on the page
                await http.post("/api/cast", json={**cast, "devices": ["X000000BBBBB"]})
                await wait_for(lambda: patio.calls)
                assert den.calls == [] and patio.calls[-1][0] == "/launch/dev"
                devs = (await (await http.get("/api/devices")).json())["devices"]
                assert [d["selected"] for d in devs] == [False, True]

                # a bookmarklet cast (no TV named) follows that choice
                n = len(patio.calls)
                await http.get("/share", params={"url": "https://example.invalid/b.m3u8", "json": "1"})
                await wait_for(lambda: len(patio.calls) > n)
                assert den.calls == []
        finally:
            await relay_runner.cleanup()
            await r1.cleanup()
            await r2.cleanup()

    asyncio.run(go())
