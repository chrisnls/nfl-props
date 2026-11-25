#!/usr/bin/env python3
"""
Line movement ("closing line value"): does a signal come BEFORE the market moves its way?

Win-loss records need thousands of bets to show a 2-3% edge. Line movement needs a few hundred: if the side a
signal liked at the first pull keeps getting more expensive by the last pull before the game, the signal knows
something early (and the edge is in betting early). If the market keeps moving the other way, the signal is
picking stale prices (what the paused diamonds did: 75% moved against them).

Each history snapshot row keeps its FIRST pull ("open_*", including the signals as they stood then); later
pre-game pulls overwrite the closing fields as before. summarize() compares the two.
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path

SIGNALS = {
    "model": "Model lean (0.3σ+ / 3+ pts)",
    "minutes": "NBA minutes trend (L3 vs season ±15%)",
    "value": "Value: best price 2%+ over the market",
    "diamond": "Diamond (paused)",
    "over": "Baseline: every Over",
}


def _dec(a):
    return 1 + (a / 100 if a > 0 else 100 / -a)


def signals(p: dict) -> dict:
    """Which side each signal liked for this prop at this pull (None = no opinion)."""
    out = {"over": "over"}
    a = abs(p.get("edge_sd") or 0)
    if not p.get("role_change") and a >= 0.3:
        out["model"] = "over" if (p.get("edge_sd") or 0) > 0 else "under"
    mins = [g.get("vol") for g in p.get("games") or [] if g.get("vol") is not None]
    if str(p.get("id", "")).startswith("nba:") and len(mins) >= 6:
        l3, szn = statistics.fmean(mins[-3:]), statistics.fmean(mins)
        if l3 >= 1.15 * szn:
            out["minutes"] = "over"
        elif l3 <= 0.85 * szn:
            out["minutes"] = "under"
    fair = p.get("fair_med")
    if fair is not None and (p.get("n_fair") or 0) >= 3:
        best = None
        for side, prob in (("over", fair), ("under", 1 - fair)):
            prices = [x for x in (p.get(side), (p.get("kalshi") or {}).get(side)) if x is not None]
            if prices:
                ev = prob * _dec(max(prices, key=_dec)) - 1
                if ev >= 0.02 and (best is None or ev > best[1]):
                    best = (side, ev)
        if best:
            out["value"] = best[0]
    if p.get("diamond"):
        out["diamond"] = p["diamond"]["side"]
    return out


def with_open(row: dict, prev: dict | None, p: dict) -> dict:
    """Carry the first pull's line, prices, market view and signals forward; set them on the first pull."""
    if prev and prev.get("open_fetched"):
        for k, v in prev.items():
            if k.startswith("open_"):
                row[k] = v
        return row
    row.update({"open_fetched": row.get("fetched"), "open_line": row.get("line"), "open_over": row.get("over"),
                "open_under": row.get("under"), "open_fair": row.get("fair_med"), "open_signals": signals(p)})
    return row


def summarize(paths) -> dict:
    stats = {k: {"n": 0, "moves": [], "line_toward": 0, "line_against": 0} for k in SIGNALS}
    for path in paths:
        for r in json.loads(Path(path).read_text()).get("props", []):
            if not r.get("open_fetched") or r.get("fetched") == r["open_fetched"]:
                continue        # only one pull before the game: nothing to compare
            for sig, side in (r.get("open_signals") or {}).items():
                if sig not in stats or side not in ("over", "under"):
                    continue
                s = stats[sig]
                if r.get("line") != r.get("open_line"):
                    up = r["line"] > r["open_line"]
                    s["line_toward" if up == (side == "over") else "line_against"] += 1
                    s["n"] += 1
                elif r.get("fair_med") is not None and r.get("open_fair") is not None:
                    mv = (r["fair_med"] - r["open_fair"]) * (1 if side == "over" else -1)
                    s["moves"].append(mv)
                    s["n"] += 1
    out = {}
    for sig, s in stats.items():
        m = s["moves"]
        toward = sum(x > 0.005 for x in m) + s["line_toward"]
        against = sum(x < -0.005 for x in m) + s["line_against"]
        out[sig] = {"label": SIGNALS[sig], "n": s["n"], "toward": toward, "against": against,
                    "toward_pct": round(100 * toward / (toward + against), 1) if toward + against else None,
                    "avg_move_pts": round(100 * statistics.fmean(m), 2) if m else None,
                    "line_toward": s["line_toward"], "line_against": s["line_against"]}
    return out
