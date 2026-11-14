#!/usr/bin/env python3
"""
One-off probe of Kalshi's PUBLIC market data (no API key, read-only, nothing is traded).

Answers: which NFL/NBA player-prop series exist, what a market looks like (player, stat, threshold,
yes/no prices), and whether combo ("multivariate") collections are listed. Writes
pipeline/kalshi_probe/summary.txt and probe.json so the results can be read from the repo.

    python pipeline/kalshi_probe.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import requests

BASES = ["https://api.elections.kalshi.com/trade-api/v2", "https://trading-api.kalshi.com/trade-api/v2"]
OUT = Path(__file__).resolve().parent / "kalshi_probe"
PROP_WORDS = re.compile(r"yard|point|rebound|assist|touchdown|td|three|double|reception|pass|rush|receiv|player|prop|pts|reb|ast",
                        re.I)
LEAGUE = re.compile(r"\b(NFL|NBA)\b|pro football|pro basketball", re.I)

lines: list[str] = []
dump: dict = {}


def say(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    lines.append(s)


def get(base, path, **params):
    r = requests.get(base + path, params=params, timeout=30, headers={"Accept": "application/json"})
    return r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text[:300])


def main():
    base = None
    for b in BASES:
        try:
            code, body = get(b, "/exchange/status")
            say(f"{b}/exchange/status -> {code} {str(body)[:200]}")
            if code == 200:
                base = base or b
        except Exception as e:
            say(f"{b} -> error {e}")
    if not base:
        say("no reachable Kalshi API base")
        return
    dump["base"] = base

    # 1) series list (sports)
    series = []
    for params in ({"category": "Sports"}, {}):
        try:
            code, body = get(base, "/series", **params)
            say(f"GET /series {params} -> {code}")
            if code == 200 and isinstance(body, dict):
                series = body.get("series") or []
                if series:
                    break
        except Exception as e:
            say(f"GET /series error {e}")
    say(f"series returned: {len(series)}")
    nfl_nba = [s for s in series if LEAGUE.search(" ".join(str(s.get(k, "")) for k in ("ticker", "title", "tags", "category")))
               or str(s.get("ticker", "")).upper().startswith(("KXNFL", "KXNBA"))]
    say(f"NFL/NBA series: {len(nfl_nba)}")
    for s in sorted(nfl_nba, key=lambda x: x.get("ticker", "")):
        say(f"  {s.get('ticker')} | {s.get('title')} | tags={s.get('tags')} | freq={s.get('frequency')}")
    dump["nfl_nba_series"] = nfl_nba
    if series:
        dump["series_fields"] = sorted(series[0].keys())

    # 2) sample open events + markets for prop-looking series
    props = [s for s in nfl_nba if PROP_WORDS.search(str(s.get("title", "")) + " " + str(s.get("ticker", "")))]
    say(f"\nprop-looking series: {len(props)}")
    samples = {}
    for s in props[:40]:
        t = s.get("ticker")
        try:
            code, body = get(base, "/events", series_ticker=t, status="open", with_nested_markets="true", limit=3)
        except Exception as e:
            say(f"  {t}: error {e}")
            continue
        evs = (body or {}).get("events", []) if isinstance(body, dict) else []
        say(f"  {t}: {len(evs)} open events (HTTP {code})")
        for ev in evs[:2]:
            mk = ev.get("markets") or []
            say(f"     event {ev.get('event_ticker')} | {ev.get('title')} | {ev.get('sub_title')} | markets={len(mk)}")
            for m in mk[:4]:
                say(f"        {m.get('ticker')} | {m.get('title')} | yes_sub={m.get('yes_sub_title')} | strike={m.get('floor_strike')}/{m.get('strike_type')}"
                    f" | yes bid/ask {m.get('yes_bid')}/{m.get('yes_ask')} last {m.get('last_price')} | vol {m.get('volume')} | close {m.get('close_time')}")
        samples[t] = evs[:2]
    dump["samples"] = samples
    first_market = next((m for evs in samples.values() for ev in evs for m in (ev.get("markets") or [])), None)
    if first_market:
        dump["market_fields"] = sorted(first_market.keys())
        say("\nmarket fields:", ", ".join(sorted(first_market.keys())))

    # 3) combos (multivariate event collections)
    for path in ("/multivariate_event_collections",):
        try:
            code, body = get(base, path, status="open", limit=50)
            cols = (body or {}).get("multivariate_contracts") or (body or {}).get("collections") or [] if isinstance(body, dict) else []
            say(f"\nGET {path} -> {code}; {len(cols)} collections; keys={list(body.keys())[:6] if isinstance(body, dict) else body[:120]}")
            for c in cols[:25]:
                say(f"  {c.get('collection_ticker')} | {c.get('title')} | series={c.get('series_ticker')} | events={len(c.get('associated_events') or [])}")
            dump["collections"] = cols[:25]
        except Exception as e:
            say(f"GET {path} error {e}")

    # 4) a broader look if no NFL/NBA series matched (names may differ)
    if not nfl_nba and series:
        say("\nfirst 60 series (to see naming):")
        for s in series[:60]:
            say(f"  {s.get('ticker')} | {s.get('title')} | {s.get('category')}")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        main()
    finally:
        (OUT / "summary.txt").write_text("\n".join(lines) + "\n")
        (OUT / "probe.json").write_text(json.dumps(dump, indent=1, default=str)[:900_000])
    sys.exit(0)
