"""Build the bookmarklet exactly as the page does and make sure it is valid JavaScript
whose stream regex still matches manifests (a lost backslash breaks it silently)."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

INDEX = Path(__file__).resolve().parent.parent / "relay" / "static" / "index.html"


@pytest.mark.skipif(not shutil.which("node"), reason="node required")
def test_bookmarklet_is_valid_js_and_matches_manifests(tmp_path):
    html = INDEX.read_text(encoding="utf-8")
    start = html.index("  const bm = ")
    end = html.index('  $("bookmarklet").textContent = bm;')
    script = (
        "const origin='http://relay:8080'; const tokenParam='';\n"
        + html[start:end]
        + "\nprocess.stdout.write(JSON.stringify(bm));"
    )
    (tmp_path / "gen.js").write_text(script, encoding="utf-8")
    bm = json.loads(subprocess.run(["node", str(tmp_path / "gen.js")], capture_output=True, text=True, check=True).stdout)
    assert bm.startswith("javascript:")
    code = bm[len("javascript:"):]
    # parses?
    subprocess.run(["node", "-e", f"new Function({json.dumps(code)})"], check=True)
    # regex behaves?
    regex = re.search(r"&&(/\\.m3u8.*?/i)\.test", code).group(1)
    check = (
        f"const r={regex};"
        "const t=['https://a/b/master.m3u8?x=1','https://a/v.mp4','https://a/m.mpd'].every(u=>r.test(u));"
        "const f=['https://a/script.js','https://a/xm3u8.css'].some(u=>r.test(u));"
        "process.exit(t&&!f?0:1)"
    )
    subprocess.run(["node", "-e", check], check=True)
