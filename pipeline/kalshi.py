#!/usr/bin/env python3
"""
Kalshi as a read-only price source (public market data: no API key, nothing is traded).

Kalshi lists player props as yes/no contracts: "Jalen Brunson: 25+ points" pays $1 if he scores 25 or more.
Its strike for that contract is 24.5 ("greater than"), which is exactly a book's Over 24.5, so a prop
(player, market, line) maps to one Kalshi market: buying Yes = the Over, buying No = the Under.

attach(props, games, sport) adds to every matched prop:
    p["kalshi"] = {"ticker", "over_c", "under_c", "over", "under", "ev_over", "ev_under", "vol", "oi"}
over_c / under_c are the ask prices in cents (what one contract costs right now, before the fee);
over / under are the same prices as American odds AFTER Kalshi's taker fee (so they compare
directly with the sportsbooks' prices); ev_* uses the model's probability like the books' EVs do.

It never raises: any failure leaves the props untouched and returns a status dict for the banner.

    python pipeline/kalshi.py --probe nba     # print what would match (for checking the mapping)
"""
from __future__ import annotations

import datetime as dt
import math
import re
import sys
import time
import unicodedata

import requests

BASE = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = {
    "nfl": {
        "player_pass_yds": "KXNFLPASSYDS", "player_pass_tds": "KXNFLPASSTDS", "player_pass_attempts": "KXNFLPASSATT",
        "player_rush_yds": "KXNFLRSHYDS", "player_rush_attempts": "KXNFLRSHATT",
        "player_reception_yds": "KXNFLRECYDS", "player_receptions": "KXNFLREC", "player_anytime_td": "KXNFLANYTD",
    },
    "nba": {
        "player_points": "KXNBAPTS", "player_rebounds": "KXNBAREB", "player_assists": "KXNBAAST",
        "player_threes": "KXNBA3PT", "player_points_rebounds_assists": "KXNBAPRA",
        "player_double_double": "KXNBA2D", "player_triple_double": "KXNBA3D",
    },
}
# series titles as Kalshi lists them; the web URL is kalshi.com/markets/<series>/<title-slug>/<event>
TITLES = {
    "KXNFLPASSYDS": "Pro Football Passing Yards", "KXNFLPASSTDS": "Pro Football player Passing Touchdowns",
    "KXNFLPASSATT": "Pro Football Passing Attempts", "KXNFLRSHYDS": "Pro Football Rushing Yards",
    "KXNFLRSHATT": "Pro Football Rushing Attempts", "KXNFLRECYDS": "Pro Football Receiving Yards",
    "KXNFLREC": "Pro Football Player Receptions", "KXNFLANYTD": "Pro Football Anytime Touchdown",
    "KXNBAPTS": "NBA Player Points", "KXNBAREB": "NBA Player Rebounds", "KXNBAAST": "NBA Player Assists",
    "KXNBA3PT": "NBA Player Threes", "KXNBAPRA": "NBA Player Points + Rebounds + Assists",
    "KXNBA2D": "NBA Player Double Double", "KXNBA3D": "NBA Player Triple Double",
}
FEE_RATE = 0.07          # Kalshi taker fee per contract = 0.07 * P * (1 - P), rounded up to the cent per order
MIN_PRICE, MAX_PRICE = 0.02, 0.98   # ignore 1¢/99¢ asks: no real liquidity, and the fee math breaks down
MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}
ALIASES = {"hollywood brown": "marquise brown", "nathaniel dell": "tank dell", "joshua palmer": "josh palmer",
           "chigoziem okonkwo": "chig okonkwo", "zonovan knight": "bam knight", "cameron ward": "cam ward",
           "kevin concepcion": "kc concepcion"}


def norm(name) -> str:
    if not isinstance(name, str):
        return ""
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    n = re.sub(r"[.'’]", "", n)
    n = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", n)
    n = " ".join(re.sub(r"[^a-z ]", " ", n).split())
    return ALIASES.get(n, n)


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _get(path, session, **params):
    for attempt in range(5):
        r = session.get(BASE + path, params=params, timeout=30, headers={"Accept": "application/json"})
        if r.status_code == 429:               # public API rate limit: back off and retry
            time.sleep(1.5 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"rate limited on {path}")


def fetch_series(series: str, session) -> list[dict]:
    """All open events (with their markets) for one series."""
    out, cursor = [], None
    for _ in range(10):
        params = {"series_ticker": series, "status": "open", "with_nested_markets": "true", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        body = _get("/events", session, **params)
        out += body.get("events") or []
        cursor = body.get("cursor")
        time.sleep(0.3)
        if not cursor:
            break
    return out


def event_date(event_ticker: str):
    """KXNBAPTS-26NOV14DALCLE -> 2026-11-14 (the game's US date)."""
    m = re.match(r"^[A-Z0-9]+-(\d{2})([A-Z]{3})(\d{2})", event_ticker or "")
    if not m or m.group(2) not in MONTHS:
        return None
    try:
        return dt.date(2000 + int(m.group(1)), MONTHS[m.group(2)], int(m.group(3))).isoformat()
    except ValueError:
        return None


def parse_market(m: dict, binary: bool):
    """-> (player_norm, line) or None. line is the book-style x.5 line that Yes is the Over of."""
    sub = m.get("yes_sub_title") or m.get("title") or ""
    player = norm(sub.split(":")[0])
    if not player or "d/st" in sub.lower():
        return None
    strike, kind = _num(m.get("floor_strike")), (m.get("strike_type") or "").lower()
    if binary:
        # yes/no props (anytime TD, double-double): Yes = over 0.5. Skip "2+ TDs"-style ladders.
        if strike is not None and strike > 0.5 and kind in ("greater", "greater_or_equal"):
            return None
        tail = sub.split(":", 1)[1].strip() if ":" in sub else ""
        if re.match(r"^[2-9]\+", tail):
            return None
        return player, 0.5
    if strike is None:
        mm = re.search(r":\s*(\d+)\+", sub)          # fall back to the "25+" in the subtitle
        return (player, int(mm.group(1)) - 0.5) if mm else None
    if kind == "greater":
        return player, strike
    if kind == "greater_or_equal":
        return player, strike - 0.5
    return None


def web_url(event_ticker: str) -> str:
    series = (event_ticker or "").split("-")[0]
    slug = re.sub(r"[^a-z0-9]+", "-", TITLES.get(series, series).lower()).strip("-")
    return f"https://kalshi.com/markets/{series.lower()}/{slug}/{(event_ticker or '').lower()}"


def cost_with_fee(price: float) -> float:
    return price + math.ceil(FEE_RATE * price * (1 - price) * 100 - 1e-9) / 100


def american(dec: float):
    if dec <= 1:
        return None
    return round((dec - 1) * 100) if dec >= 2 else round(-100 / (dec - 1))


def index_markets(sport: str, session=None) -> tuple[dict, dict]:
    """-> ({(market, player, line, date): quote}, status)."""
    session = session or requests.Session()
    idx, n_series, n_mk = {}, 0, 0
    for market, series in SERIES[sport].items():
        binary = market in ("player_anytime_td", "player_double_double", "player_triple_double")
        for ev in fetch_series(series, session):
            day = event_date(ev.get("event_ticker"))
            for m in ev.get("markets") or []:
                if (m.get("status") or "active") not in ("active", "open"):
                    continue
                key = parse_market(m, binary)
                if not key:
                    continue
                yes_ask, yes_bid = _num(m.get("yes_ask_dollars")), _num(m.get("yes_bid_dollars"))
                no_ask = _num(m.get("no_ask_dollars"))
                if no_ask is None and yes_bid is not None:
                    no_ask = round(1 - yes_bid, 4)
                q = {"ticker": m.get("ticker"), "event": ev.get("event_ticker"), "yes": yes_ask, "no": no_ask,
                     "vol": _num(m.get("volume_fp")) or _num(m.get("volume")), "oi": _num(m.get("open_interest_fp"))}
                idx[(market, key[0], key[1], day)] = q
                n_mk += 1
        n_series += 1
    return idx, {"ok": True, "series": n_series, "markets": n_mk}


def quote_for(p: dict, idx: dict, day: str | None):
    name = norm(p.get("player"))
    q = idx.get((p["market"], name, float(p["line"]), day))
    if q is None:                       # date mismatch (timezones / ticker date) -> unique match on any date
        hits = [v for k, v in idx.items() if k[0] == p["market"] and k[1] == name and k[2] == float(p["line"])]
        q = hits[0] if len(hits) == 1 else None
    return q


def attach(props: list[dict], games: list[dict], sport: str) -> dict:
    """Adds p['kalshi'] to props with a matching open Kalshi market. Never raises."""
    try:
        idx, status = index_markets(sport)
    except Exception as e:                           # network / API change: the board just has no Kalshi prices
        return {"ok": False, "error": str(e)[:200]}
    day_of = {g["id"]: str(g.get("kickoff") or "")[:10] or None for g in games or []}
    matched = 0
    for p in props:
        p.pop("kalshi", None)
        q = quote_for(p, idx, day_of.get(p.get("game")))
        if not q:
            continue
        k = {"ticker": q["ticker"], "url": web_url(q["event"]), "vol": q["vol"], "oi": q["oi"], "over_c": None, "under_c": None,
             "over": None, "under": None, "ev_over": None, "ev_under": None}
        pov = p.get("p_over")
        for side, price in (("over", q["yes"]), ("under", q["no"])):
            if price is None or not (MIN_PRICE <= price <= MAX_PRICE):
                continue
            c = cost_with_fee(price)
            k[side + "_c"] = round(price * 100)
            k[side] = american(1 / c)
            if pov is not None and not (p.get("role_change") and p["role_change"].get("why") == "thin"):
                prob = pov if side == "over" else 1 - pov
                k["ev_" + side] = round(prob / c - 1, 3)
        if k["over"] is None and k["under"] is None:
            continue
        p["kalshi"] = k
        matched += 1
    status.update({"matched": matched, "fetched": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")})
    return status


if __name__ == "__main__":
    import json
    from pathlib import Path
    sport = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "--probe" else "nba"
    f = Path(__file__).resolve().parents[1] / "site" / "data" / ("nba/props.json" if sport == "nba" else "props.json")
    data = json.loads(f.read_text())
    st = attach(data["props"], data["games"], sport)
    print(json.dumps(st))
    hits = [p for p in data["props"] if p.get("kalshi")]
    for p in hits[:40]:
        k = p["kalshi"]
        print(f"{p['player']:24s} {p['label']:14s} {p['line']:>6} | yes {k['over_c']}¢ ({k['over']}) EV {k['ev_over']}"
              f" | no {k['under_c']}¢ ({k['under']}) EV {k['ev_under']} | book {p.get('over')}/{p.get('under')} | {k['ticker']}")
    idx, _ = index_markets(sport)
    print(f"\nindexed {len(idx)} Kalshi markets; sample keys:")
    for key in list(idx)[:25]:
        print("  ", key, idx[key])
