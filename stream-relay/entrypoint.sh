#!/bin/sh
# Sites change constantly and yt-dlp ships fixes almost daily, so grab the latest
# release on every start (skipped quietly if offline). Set YTDLP_AUTO_UPDATE=0 to pin.
if [ "${YTDLP_AUTO_UPDATE:-1}" = "1" ]; then
  echo "Checking for yt-dlp updates..."
  timeout 120 pip install --quiet --upgrade "yt-dlp[default,curl-cffi]" \
    && python -c "import yt_dlp.version as v; print('yt-dlp', v.__version__)" \
    || echo "yt-dlp update skipped"
fi
exec python -m relay
