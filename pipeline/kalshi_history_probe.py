#!/usr/bin/env python3
"""
One-off research probe (read-only public data): settled Kalshi NBA player-prop rungs with their pre-game prices,
to test (a) whether high-probability rungs (75-95c) win more often than their price says (favorite-longshot bias)
and (b) whether our projections pick out the rungs that do. Writes pipeline/kalshi_probe/hist_nba.json.

For each settled market: result, strike, player, event date, plus hourly candles over the game day (US Eastern
12:00 -> 23:59), so the last candle before tip can be chosen offline against the schedule.
"""
import datetime as dt
import json
import random
import sys
import time
from pathlib import Path

import requests

BASE = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = ["KXNBAPTS", "KXNBAREB", "KXNBAAST", "KXNBA3PT", "KXNBAPRA"]
DAYS = 16
MAX_MARKETS = 3200
OUT = Path(__file__).resolve().parent / "kalshi_probe" / "hist_nba.json"
S = requests.Session()


def get(path, **params):
    for attempt in range(6):
        r = S.get(BASE + path, params=params, timeout=30)
        if r.status_code == 429:
            time.sleep(1.5 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("rate limited")


def main():
    now = int(time.time())
    min_close = now - DAYS * 86400
    markets = []
    for series in SERIES:
        cursor = None
        for _ in range(60):
            params = {"series_ticker": series, "status": "settled", "limit": 1000, "min_close_ts": min_close}
            if cursor:
                params["cursor"] = cursor
            body = get("/markets", **params)
            ms = body.get("markets") or []
            markets += ms
            cursor = body.get("cursor")
            time.sleep(0.25)
            if not cursor or not ms:
                break
        print(series, "settled so far:", len(markets), flush=True)
    if markets:
        print("market fields:", sorted(markets[0].keys()), flush=True)
    random.seed(7)
    random.shuffle(markets)
    rows, fails = [], 0
    for i, m in enumerate(markets[:MAX_MARKETS]):
        t = m.get("ticker", "")
        series = t.split("-")[0]
        try:
            d = dt.datetime.strptime(t.split("-")[1][:7], "%y%b%d").date()
        except Exception:
            continue
        et = dt.timezone(dt.timedelta(hours=-5))
        start = int(dt.datetime(d.year, d.month, d.day, 11, 0, tzinfo=et).timestamp())
        end = int(dt.datetime(d.year, d.month, d.day, 23, 59, tzinfo=et).timestamp())
        try:
            c = get(f"/series/{series}/markets/{t}/candlesticks", start_ts=start, end_ts=end, period_interval=60)
            candles = c.get("candlesticks") or []
        except Exception as e:
            fails += 1
            candles = []
            if fails < 5:
                print("candle error", t, str(e)[:120], flush=True)
        rows.append({"ticker": t, "event": m.get("event_ticker"), "series": series, "date": d.isoformat(),
                     "sub": m.get("yes_sub_title"), "strike": m.get("floor_strike"), "strike_type": m.get("strike_type"),
                     "result": m.get("result"), "expiration_value": m.get("expiration_value"),
                     "occurrence": m.get("occurrence_datetime"), "volume": m.get("volume_fp") or m.get("volume"),
                     "candles": candles})
        time.sleep(0.12)
        if i % 250 == 0:
            print("candles", i, flush=True)
    if rows and rows[0]["candles"]:
        print("candle sample:", json.dumps(rows[0]["candles"][-1])[:600], flush=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"fetched": now, "n": len(rows), "rows": rows}, separators=(",", ":")))
    print("wrote", len(rows), "rows", OUT.stat().st_size // 1024, "KB", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("probe failed:", e)
        sys.exit(0)
