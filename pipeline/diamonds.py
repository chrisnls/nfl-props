#!/usr/bin/env python3
"""
Diamonds in the rough: bets whose best available price is better than the market thinks they're worth.

What the research said (docs/HANDOFF.md, 11/19): on 3,000+ graded props, the classic "undervalued" angles
(hot streaks, soft defenses, career-vs-opponent, stacked signals, the model's own edges) did NOT beat the books;
they're already priced in. What's reliably undervalued is a PRICE out of line with the market: one book (or Kalshi)
paying more than the other books' consensus says the bet is worth. In the NBA, recent minutes trends also beat
the line in both directions (books are slow on rotation changes), so they nudge the estimate.

Estimate = consensus no-vig probability (median across books at the main line) + small evidence-based nudges.
A diamond = EV >= MIN_EV at the best price (sportsbook or Kalshi after its fee). Everything else on the card
(hit rates, defense, career vs this team, news, the model's view) is context only.

Every diamond is logged once, at its first price before kickoff, to site/data/[nba/]diamonds/<slate>.json and
graded separately (Results tab), so the list has to earn its keep.
"""
from __future__ import annotations

import datetime as dt
import json
import statistics
from pathlib import Path

MIN_EV = 0.03
MIN_BOOKS = 3          # books pricing both sides at the main line; fewer = no reliable consensus
MAX_LIST = 12
PER_GAME = 2
MIN_PRICE = -250       # skip heavy favorites: a few cents of mispricing on -400 isn't a diamond
MAX_PRICE = 400        # no-vig consensus is unreliable for long shots (books load extra margin there)
MAX_EV = 0.15          # a gap bigger than this is almost always a stale or mismatched quote, not a real mispricing
TREND = 0.15           # NBA minutes, last 3 vs season
TREND_NUDGE = 0.03     # measured +4 to +9 pts on 285 graded props; half of it, to be safe
SKIP_ROLES = {"injury", "absent", "uncal"}
BOOK_NAME = {"draftkings": "DraftKings", "fanduel": "FanDuel", "betmgm": "BetMGM", "caesars": "Caesars",
             "espnbet": "ESPN BET", "fanatics": "Fanatics", "kalshi": "Kalshi"}


def dec(a):
    return 1 + (a / 100 if a > 0 else 100 / -a)


def american(d):
    return round((d - 1) * 100) if d >= 2 else round(-100 / (d - 1))


def fmt(a):
    return f"+{a}" if a > 0 else str(a)


def pct(x):
    return f"{round(100 * x)}%"


def minutes_trend(p):
    mins = [g.get("vol") for g in p.get("games") or [] if g.get("vol") is not None]
    if len(mins) < 6:
        return None
    l3, season = statistics.fmean(mins[-3:]), statistics.fmean(mins)
    return l3, season


def pick(props: list[dict], sport: str, started=lambda p: False) -> list[dict]:
    """Marks p['diamond'] on the chosen props and returns the list (best EV first)."""
    cands = []
    for p in props:
        p.pop("diamond", None)
        if (p.get("role_change") or {}).get("why") in SKIP_ROLES or started(p):
            continue
        yes_only = p.get("under") is None and p["market"] in ("player_anytime_td", "player_double_double", "player_triple_double")
        fair = p.get("fair_med")
        if fair is None or (p.get("n_fair") or 0) < MIN_BOOKS:
            continue   # yes-only markets (most TD scorer lines) can't be de-vigged, so their "value" would be illusory
        nudge, why_nudge = {"over": 0.0, "under": 0.0}, {}
        if sport == "nba" and p["market"] not in ("player_double_double", "player_triple_double"):
            mt = minutes_trend(p)
            if mt:
                l3, season = mt
                if l3 >= (1 + TREND) * season:
                    nudge["over"] = TREND_NUDGE
                    why_nudge["over"] = (f"Minutes up: {l3:.0f} a game over his last 3 vs {season:.0f} this season. In testing, "
                                         f"books were slow to price rotation changes (overs hit 54% vs 50% implied).")
                elif l3 <= (1 - TREND) * season:
                    nudge["under"] = TREND_NUDGE
                    why_nudge["under"] = (f"Minutes down: {l3:.0f} a game over his last 3 vs {season:.0f} this season. In testing, "
                                          f"unders hit 59% vs 50% implied after drops like this.")
        for side in ("over", "under"):
            if yes_only and side == "under":
                continue
            p_mkt = fair if side == "over" else 1 - fair
            p_est = p_mkt + nudge[side]
            quotes = []
            if p.get(side) is not None:
                quotes.append((p[side], p.get(side + "_book") or "book"))
            k = p.get("kalshi") or {}
            if k.get(side) is not None:
                quotes.append((k[side], "kalshi"))
            if not quotes:
                continue
            price, venue = max(quotes, key=lambda q: dec(q[0]))
            if price < MIN_PRICE or price > MAX_PRICE:
                continue
            ev = p_est * dec(price) - 1
            if ev < MIN_EV or p_mkt * dec(price) - 1 > MAX_EV:
                continue
            reasons = []
            fair_price = american(1 / p_mkt) if 0 < p_mkt < 1 else None
            if fair_price is not None and dec(price) > 1 / p_mkt + 1e-9:
                reasons.append(f"{BOOK_NAME.get(venue, venue)} pays {fmt(price)}{' after its fee' if venue == 'kalshi' else ''}; "
                               f"the other books' consensus makes this a {fmt(fair_price)} bet ({pct(p_mkt)}).")
            if side in why_nudge:
                reasons.append(why_nudge[side])
            cands.append({"id": p["id"], "player": p["player"], "team": p["team"], "opp": p["opp"], "home": p["home"],
                          "game": p["game"], "market": p["market"], "label": p["label"], "line": p["line"], "side": side,
                          "price": price, "venue": venue, "p_mkt": round(p_mkt, 3), "p_est": round(p_est, 3),
                          "ev": round(ev, 3), "reasons": reasons, "context": context(p, side, sport)})
    cands.sort(key=lambda c: -c["ev"])
    out, per_game, seen = [], {}, set()
    for c in cands:
        if c["id"] in seen or per_game.get(c["game"], 0) >= PER_GAME:
            continue
        out.append(c)
        seen.add(c["id"])
        per_game[c["game"]] = per_game.get(c["game"], 0) + 1
        if len(out) >= MAX_LIST:
            break
    by_id = {p["id"]: p for p in props}
    for c in out:
        by_id[c["id"]]["diamond"] = {k: c[k] for k in ("side", "price", "venue", "p_mkt", "p_est", "ev")}
    return out


def context(p, side, sport):
    """Everything else worth knowing. Shown, not used: none of these beat the line on their own in testing."""
    ctx = []
    over = side == "over"
    if p.get("hit_l10") is not None and p.get("games"):
        n = min(10, len(p["games"]))
        ctx.append(f"Went over {p['line']} in {round(p['hit_l10'] * n / 100)} of his last {n} (season {p.get('hit_season')}%).")
    df = p.get("def_factor")
    if df and abs(df - 1) >= 0.05:
        soft = df > 1
        ctx.append(f"{p['opp']} defense allows {round(abs(df - 1) * 100)}% {'more' if soft else 'less'} than average here"
                   f" ({'helps' if soft == over else 'works against'} the {side}).")
    h = p.get("h2h")
    if h and h.get("n"):
        ctx.append(f"Career vs {p['opp']}: {h['avg']} a game in {h['n']} game{'s' if h['n'] > 1 else ''} "
                   f"(over {p['line']} in {h['over']} of {h['n']}).")
    if p.get("vacated"):
        v = p["vacated"]
        ctx.append(f"Teammates gone: {v['share']}% of the team's {v['kind']} are open ({', '.join(w['name'] for w in v['who'])}).")
    mt = minutes_trend(p) if sport == "nba" else None
    if mt and abs(mt[0] / mt[1] - 1) < TREND:
        ctx.append(f"Minutes steady: {mt[0]:.0f} last 3 vs {mt[1]:.0f} season.")
    if p.get("p_over") is not None:
        pm = p["p_over"] if over else 1 - p["p_over"]
        ctx.append(f"Our model says {pct(pm)} (it has trailed the books this season, so it's only a second opinion).")
    if p.get("news"):
        ctx.append(f"Latest news: {p['news'][0]['headline']}")
    return ctx


def log_slate(diamonds: list[dict], path: Path, now_iso: str) -> None:
    """Keep each diamond's FIRST pre-game appearance (price, estimate) so grading can't cherry-pick."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(path.read_text()) if path.exists() else {"picks": {}}
    for d in diamonds:
        key = f"{d['id']}:{d['side']}"
        if key not in data["picks"]:
            data["picks"][key] = {**{k: d[k] for k in ("id", "player", "team", "opp", "game", "market", "label", "line", "side",
                                                         "price", "venue", "p_mkt", "p_est", "ev")}, "first_seen": now_iso}
    data["updated"] = now_iso
    path.write_text(json.dumps(data, separators=(",", ":")))


def grade(paths, actual) -> dict:
    """actual(pick, slate_key) -> stat value, 'pending', or None (didn't play -> void)."""
    rows, by_slate = [], []
    for path in sorted(paths):
        data = json.loads(Path(path).read_text())
        key = Path(path).stem
        rs = []
        for d in data.get("picks", {}).values():
            a = actual(d, key)
            if a == "pending":
                continue
            if a is None:
                continue
            res = "push" if a == d["line"] else ("win" if (a > d["line"]) == (d["side"] == "over") else "loss")
            units = 0.0 if res == "push" else (dec(d["price"]) - 1 if res == "win" else -1.0)
            rs.append({**d, "actual": a, "result": res, "units": round(units, 3)})
        if rs:
            by_slate.append({"slate": key, **summary(rs)})
        rows += rs
    return {"overall": summary(rows), "by_slate": by_slate,
            "by_venue": {v: summary([r for r in rows if (r["venue"] == "kalshi") == (v == "Kalshi")]) for v in ("Books", "Kalshi")},
            "recent": sorted(rows, key=lambda r: r.get("first_seen", ""), reverse=True)[:40]}


def summary(rs):
    w = sum(r["result"] == "win" for r in rs)
    l = sum(r["result"] == "loss" for r in rs)
    p = sum(r["result"] == "push" for r in rs)
    u = sum(r["units"] for r in rs)
    exp = sum(r["p_est"] for r in rs if r["result"] != "push")
    return {"n": len(rs), "w": w, "l": l, "p": p, "units": round(u, 2),
            "roi": round(100 * u / len(rs), 1) if rs else None, "expected_w": round(exp, 1)}
