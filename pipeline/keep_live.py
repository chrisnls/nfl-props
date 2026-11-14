#!/usr/bin/env python3
"""
Each workflow deploys the WHOLE site, but only rebuilds its own sport. The other sport's props.json in the checkout
is the last *committed* copy (committed only on live line pulls), so deploying it would roll the live board back
(losing the hourly Kalshi prices, news and stat updates). Before deploying, keep whichever is newer: the checkout's
file or the one currently live on GitHub Pages.

    python pipeline/keep_live.py site/data/props.json https://chrisnls.github.io/nfl-props/data/props.json
"""
import json
import sys
from pathlib import Path

import requests

path, url = Path(sys.argv[1]), sys.argv[2]
try:
    local = json.loads(path.read_text())
    live = requests.get(url, params={"t": "1"}, timeout=30).json()
    if live.get("props") and str(live.get("generated", "")) > str(local.get("generated", "")) \
            and live.get("season") == local.get("season"):
        path.write_text(json.dumps(live, separators=(",", ":")))
        print(f"kept live {path} (generated {live['generated']} > checkout {local.get('generated')})")
    else:
        print(f"checkout {path} is current ({local.get('generated')} vs live {live.get('generated')})")
except Exception as e:
    print(f"keep_live: {e}; deploying the checkout copy")
