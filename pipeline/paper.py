#!/usr/bin/env python3
"""
Paper trade: patient limit orders on high-probability Kalshi NBA prop rungs. Nothing is ever placed.

Why: the 11/27 history test (docs/HANDOFF.md) found that BUYING 80-99c rungs at the ask loses about 0.5-4% after
fees, but resting a bid and getting filled looked like +1-3%. That upper bound assumed every bid fills, and fills
aren't random (you get filled more often when the price is about to drop). This tests it properly.

1. post():   each build, for every open rung whose bid on either side is 0.80-0.95 (spread <= MAX_SPREAD) and whose
             game tips in 0.5-12h, log a pretend 1-contract limit buy at that bid (first time seen only).
2. settle(): after tip-off, read Kalshi's public trade tape from post time to tip. A trade at a better price than our
             bid means we'd certainly have been filled ("strict"); a trade AT our price, maybe ("loose", we may
             have been behind other orders). After settlement, grade filled orders at our price, after a maker fee.

Stored in site/data/nba/paper.json (committed by the workflow) with a running summary for the Results tab.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import re
import time
from pathlib import Path

import requests

BASE = "https://api.elections.kalshi.com/trade-api/v2"
BAND = (0.80, 0.95)
MAX_SPREAD = 0.06
WINDOW_H = (0.5, 12.0)
MAKER_FEE = 0.0175          # assumed resting-order fee rate (x P x (1-P), rounded up to the cent); taker is 0.07
MAX_SETTLE_CALLS = 400      # API calls per build for the trade tape / results (keeps each run short)
KEEP_DAYS = 45
ALIAS = {"NO": "NOP", "UTAH": "UTA", "GS": "GSW", "NY": "NYK", "SA": "SAS", "WSH": "WAS"}


def _num(v):
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def _price(t: dict, key: str):
    v = _num(t.get(key + "_dollars"))
    if v is None:
        v = _num(t.get(key))
        if v is not None and v > 1:
            v /= 100
    return v


def _get(session, path, **params):
    for attempt in range(5):
        r = session.get(BASE + path, params=params, timeout=30)
        if r.status_code == 429:
            time.sleep(1.5 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("rate limited")


def tip_of(event: str, games: list[dict]):
    """KXNBAPTS-26NOV14DALCLE -> the matching game's tip (UTC) from our schedule."""
    m = re.match(r"^[A-Z0-9]+-(\d{2}[A-Z]{3}\d{2})([A-Z]+)$", event or "")
    if not m:
        return None
    code = m.group(2)
    for g in games:
        aw, hm = g.get("away"), g.get("home")
        for a in {aw, ALIAS.get(aw, aw)}:
            for h in {hm, ALIAS.get(hm, hm)}:
                if code == f"{a}{h}" and g.get("start_utc"):
                    return dt.datetime.fromisoformat(g["start_utc"])
    return None


def load(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {"orders": {}}


def post(idx: dict, games: list[dict], path: Path) -> int:
    data = load(path)
    now = dt.datetime.now(dt.timezone.utc)
    added = 0
    for q in idx.values():
        tip = tip_of(q.get("event"), games)
        if not tip or not (WINDOW_H[0] <= (tip - now).total_seconds() / 3600 <= WINDOW_H[1]):
            continue
        for side, bid, ask in (("yes", q.get("yes_bid"), q.get("yes")), ("no", q.get("no_bid"), q.get("no"))):
            if bid is None or ask is None or not (BAND[0] <= bid <= BAND[1]) or ask - bid > MAX_SPREAD:
                continue
            key = f"{q['ticker']}:{side}"
            if key in data["orders"]:
                continue
            data["orders"][key] = {"ticker": q["ticker"], "side": side, "price": round(bid, 4), "ask": round(ask, 4),
                                   "player": q.get("player"), "market": q.get("market"), "line": q.get("line"),
                                   "posted": now.isoformat(timespec="minutes"), "tip": tip.isoformat(timespec="minutes"),
                                   "fill": None, "result": None}
            added += 1
    path.write_text(json.dumps(data, separators=(",", ":")))
    return added


def settle(path: Path) -> dict:
    data = load(path)
    now = dt.datetime.now(dt.timezone.utc)
    session, calls = requests.Session(), 0
    for key, o in sorted(data["orders"].items(), key=lambda kv: kv[1]["tip"]):
        if calls >= MAX_SETTLE_CALLS:
            break
        tip = dt.datetime.fromisoformat(o["tip"])
        if now < tip:
            continue
        try:
            if o["fill"] is None:
                t0 = int(dt.datetime.fromisoformat(o["posted"]).timestamp())
                body = _get(session, "/markets/trades", ticker=o["ticker"], min_ts=t0, max_ts=int(tip.timestamp()), limit=1000)
                calls += 1
                strict = loose = False
                for t in body.get("trades") or []:
                    yp, np_ = _price(t, "yes_price"), _price(t, "no_price")
                    if np_ is None and yp is not None:
                        np_ = 1 - yp
                    mine = yp if o["side"] == "yes" else np_
                    if mine is None:
                        continue
                    strict |= mine < o["price"] - 1e-9
                    loose |= mine <= o["price"] + 1e-9
                o["fill"] = "strict" if strict else "loose" if loose else "none"
            if o["result"] is None and now > tip + dt.timedelta(hours=3):   # unfilled too: shows the fill bias
                m = _get(session, f"/markets/{o['ticker']}").get("market") or {}
                calls += 1
                if m.get("result") in ("yes", "no"):
                    o["result"] = m["result"]
        except Exception as e:     # keep going; it retries next build
            o.setdefault("errors", 0)
            o["errors"] += 1
        time.sleep(0.1)
    cutoff = now - dt.timedelta(days=KEEP_DAYS)
    data["orders"] = {k: o for k, o in data["orders"].items() if dt.datetime.fromisoformat(o["tip"]) > cutoff}
    data["summary"] = summarize(data["orders"])
    data["updated"] = now.isoformat(timespec="minutes")
    path.write_text(json.dumps(data, separators=(",", ":")))
    return data["summary"]


def summarize(orders: dict) -> dict:
    def grade(os_):
        rows = []
        for o in os_:
            if o["result"] not in ("yes", "no"):
                continue
            p = o["price"]
            fee = math.ceil(MAKER_FEE * p * (1 - p) * 100 - 1e-9) / 100
            win = o["result"] == o["side"]
            rows.append((win, p, (1 - p - fee) if win else -(p + fee)))
        if not rows:
            return {"n": 0}
        w = sum(r[0] for r in rows)
        cost = sum(r[1] for r in rows)
        units = sum(r[2] for r in rows)
        return {"n": len(rows), "w": w, "l": len(rows) - w, "hit_pct": round(100 * w / len(rows), 1),
                "price_pct": round(100 * cost / len(rows), 1), "profit": round(units, 2),
                "roi": round(100 * units / cost, 1)}
    vals = list(orders.values())
    done = [o for o in vals if o["fill"] is not None]
    return {"posted": len(vals), "checked": len(done),
            "filled_strict": sum(o["fill"] == "strict" for o in done), "filled_loose": sum(o["fill"] in ("strict", "loose") for o in done),
            "strict": grade([o for o in done if o["fill"] == "strict"]),
            "loose": grade([o for o in done if o["fill"] in ("strict", "loose")]),
            "unfilled_would_have": grade([o for o in done if o["fill"] == "none" and o["result"]]),
            "band": list(BAND), "maker_fee": MAKER_FEE}
