#!/usr/bin/env python3
"""One-off: find which kalshi.com URL shapes open a given market/event page (read-only GETs)."""
import json
import re
from pathlib import Path

import requests

API = "https://api.elections.kalshi.com/trade-api/v2"
OUT = Path(__file__).resolve().parent / "kalshi_probe" / "links.txt"
H = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130 Safari/537.36"}
lines = []


def say(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    lines.append(s)


def main():
    for series in ("KXNBAPTS", "KXNFLREC"):
        ser = requests.get(f"{API}/series/{series}", timeout=30).json().get("series", {})
        say(f"\n== {series} series fields:", json.dumps({k: v for k, v in ser.items() if k not in ("settlement_sources",)})[:900])
        evs = requests.get(f"{API}/events", params={"series_ticker": series, "status": "open", "limit": 1,
                                                    "with_nested_markets": "true"}, timeout=30).json().get("events", [])
        if not evs:
            say("no open events")
            continue
        ev = evs[0]
        et, mk = ev["event_ticker"], ev["markets"][0]["ticker"]
        say("event fields:", json.dumps({k: v for k, v in ev.items() if k != "markets"})[:900])
        say("market sample:", mk, "| title:", ev["markets"][0].get("title"))
        slug = re.sub(r"[^a-z0-9]+", "-", (ser.get("title") or "").lower()).strip("-")
        cands = [
            f"https://kalshi.com/markets/{series.lower()}",
            f"https://kalshi.com/markets/{et.lower()}",
            f"https://kalshi.com/markets/{series.lower()}/{et.lower()}",
            f"https://kalshi.com/markets/{series.lower()}/{slug}/{et.lower()}",
            f"https://kalshi.com/markets/{series.lower()}/x/{et.lower()}",
            f"https://kalshi.com/markets/{series.lower()}/{slug}/{mk.lower()}",
            f"https://kalshi.com/events/{et.lower()}",
            f"https://kalshi.com/markets/{mk.lower()}",
        ]
        for u in cands:
            try:
                r = requests.get(u, headers=H, timeout=30, allow_redirects=True)
                html = r.text
                t = re.search(r"<title[^>]*>(.*?)</title>", html, re.S)
                og = re.search(r'property="og:title"\s+content="([^"]*)"', html)
                say(f"{r.status_code} {u}\n     -> {r.url}\n     title={t.group(1).strip()[:120] if t else None} | og={og.group(1)[:120] if og else None}"
                    f" | has_event={et.lower() in html.lower()} | has_market={mk.lower() in html.lower()} | 404text={'not found' in html.lower()[:20000]}")
            except Exception as e:
                say(f"ERR {u} {e}")


if __name__ == "__main__":
    try:
        main()
    finally:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text("\n".join(lines) + "\n")
