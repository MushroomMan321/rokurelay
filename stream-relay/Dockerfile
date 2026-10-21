# yt-dlp needs a JavaScript runtime to solve YouTube's player challenges.
FROM denoland/deno:bin-2.5.7 AS deno

FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    DATA_DIR=/data \
    WORK_DIR=/tmp/stream-relay

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*
COPY --from=deno /deno /usr/local/bin/deno

# The venv belongs to the runtime user so yt-dlp can update itself on start.
RUN useradd --system --uid 1000 --create-home relay \
 && python -m venv /opt/venv \
 && mkdir -p /data /tmp/stream-relay \
 && chown -R relay:relay /opt/venv /data /tmp/stream-relay

USER relay
WORKDIR /app
COPY --chown=relay:relay requirements.txt .
RUN pip install -r requirements.txt

# Browser sniff fallback (BROWSER_SNIFF): Chromium + its system libraries.
USER root
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN apt-get update \
 && python -m playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/* \
 && chmod -R a+rX /ms-playwright
USER relay
COPY --chown=relay:relay relay ./relay
COPY --chown=relay:relay --chmod=755 entrypoint.sh /entrypoint.sh

EXPOSE 8080
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:' + __import__('os').environ.get('RELAY_PORT', '8080') + '/healthz', timeout=4)"
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
