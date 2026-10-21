import logging
import os

from aiohttp import web

from .config import Config
from .server import create_app


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    )
    cfg = Config.from_env()
    logging.getLogger("relay").info(
        "stream relay on %s:%s, %d Roku device(s) configured", cfg.host, cfg.port, len(cfg.roku_devices)
    )
    # access_log off: share/cast URLs can carry tokens and cookies
    web.run_app(create_app(cfg), host=cfg.host, port=cfg.port, access_log=None, print=None)


if __name__ == "__main__":
    main()
