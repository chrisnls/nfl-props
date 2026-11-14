"""
Grading loop: score saved props and parlays against what actually happened.

Run via `python pipeline/build.py --grade` (the workflow does this before every build).

Reads
  site/data/history/week_NN.json   snapshots written by every live build (pre-game lines only)
  PARLAY_SHEET_CSV_URL             optional: the "Parlays" Google Sheet, published to the web as CSV

Writes
  site/data/results.json   model record, ROI, edge buckets, calibration, Brier, corrections
  site/data/parlays.json   saved parlays graded leg by leg
"""
from __future__ import annotations

import datetime as dt
import io
import json
import math
import os
import statistics

from pathlib import Path

import pandas as pd
import requests

import hashlib

import build
from build import (BACKTEST_CACHE, CORRECTIONS_MIN_N, HISTORY, MARKETS, OUT, RESULTS, SEASON, log)

PARLAYS = OUT / "parlays.json"
JUICE_WIN = 100 / 110            # profit per 1u staked at -110
EDGE_BUCKETS = [("<0.5σ", 0, 0.5), ("0.5–1σ", 0.5, 1.0), (">1σ", 1.0, math.inf)]
# corrections: need this many graded (unflagged) projections in a market before suggesting one;
# they auto-apply at CORRECTIONS_MIN_N. Each is shrunk toward 1.0 by n / (n + CORR_SHRINK_K).
CORR_MIN_N = 30
CORR_SHRINK_K = 100
BACKTEST_FIRST_WEEK = 5      # sample-line rule needs 4+ prior games
PROJ_MULT_RANGE = (0.85, 1.15)
SD_MULT_RANGE = (0.7, 1.5)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def american_to_decimal(price) -> float | None:
    if price is None or (isinstance(price, float) and math.isnan(price)):
        return None
    price = float(price)
    return 1 + price / 100 if price > 0 else 1 + 100 / -price


class Outcomes:
    """Actual stat lookups from nflverse weekly stats + which weeks/games are final."""

    def __init__(self, stats: pd.DataFrame, games: pd.DataFrame):
        self.stats = stats.set_index(["player_id", "week"])
        self.stat_games = set(stats.game_id.unique())
        self.games = games

    def week_complete(self, week: int) -> bool:
        """Every game that week is final AND has stat lines (scores can land before the
        nightly stats refresh; grading then would wrongly void that game's players)."""
        g = self.games[self.games.week == week]
        return len(g) > 0 and g.home_score.notna().all() and set(g.game_id) <= self.stat_games

    def actual(self, pid: str, week: int, market: str) -> float | None:
        """Stat value, or None if the player has no stat line that week (didn't play)."""
        col = MARKETS[market][0]
        try:
            row = self.stats.loc[(pid, week)]
        except KeyError:
            return None
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]
        v = row[col]
        return 0.0 if pd.isna(v) else float(v)


def side_result(actual: float, line: float) -> str:
    return "over" if actual > line else "under" if actual < line else "push"


# ----------------------------------------------------------------------------- props
def grade_snapshots(out: Outcomes) -> tuple[list[dict], list[int], list[int]]:
    rows, graded, pending = [], [], []
    for path in sorted(HISTORY.glob("week_*.json")):
        snap = json.loads(path.read_text())
        if snap.get("season", SEASON) != SEASON:
            continue
        week = int(snap["week"])
        if not out.week_complete(week):
            pending.append(week)
            continue
        graded.append(week)
        for p in snap["props"]:
            actual = out.actual(p["pid"], week, p["market"])
            if actual is None:
                continue   # did not play -> void, not graded
            res = side_result(actual, p["line"])
            pick = "over" if (p.get("edge_sd") or p.get("edge") or 0) > 0 else "under"
            rows.append({**p, "week": week, "actual": actual, "result": res, "pick": pick,
                         "pick_result": "push" if res == "push" else "win" if res == pick else "loss"})
    return rows, graded, pending


def record(rows: list[dict]) -> dict:
    """W-L-P of the model's pick (the side it leans), ROI flat 1u at -110, Brier of P(over)."""
    w = sum(r["pick_result"] == "win" for r in rows)
    l = sum(r["pick_result"] == "loss" for r in rows)
    p = sum(r["pick_result"] == "push" for r in rows)
    units = w * JUICE_WIN - l
    decided = [r for r in rows if r["result"] != "push" and r.get("p_over") is not None]
    brier = statistics.fmean((r["p_over"] - (r["result"] == "over")) ** 2 for r in decided) if decided else None
    return {"n": len(rows), "w": w, "l": l, "p": p,
            "hit_pct": round(100 * w / (w + l), 1) if w + l else None,
            "units": round(units, 2),
            "roi": round(100 * units / len(rows), 1) if rows else None,
            "brier": round(brier, 4) if brier is not None else None}


def calibration(rows: list[dict]) -> list[dict]:
    out = []
    for i in range(10):
        lo, hi = i / 10, (i + 1) / 10
        b = [r for r in rows if r["result"] != "push" and r.get("p_over") is not None
             and (lo <= r["p_over"] < hi or (i == 9 and r["p_over"] == 1.0))]
        out.append({"bucket": f"{i * 10}–{(i + 1) * 10}%", "lo": lo, "hi": hi, "n": len(b),
                    "predicted": round(statistics.fmean(r["p_over"] for r in b), 3) if b else None,
                    "actual": round(sum(r["result"] == "over" for r in b) / len(b), 3) if b else None})
    return out


def backtest_rows(frames: dict, out: Outcomes) -> tuple[list[dict], list[int]]:
    """Corrections don't need betting lines -- only projection, σ and the actual result.
    So replay the model on every finished week: for week W it sees only games before W,
    and projects every player who'd plausibly have a prop (same rule as sample lines:
    4+ games and a meaningful recent average). Cached per week; the cache resets whenever
    build.py changes, so it always reflects the current model."""
    stats, games = frames["stats"], frames["games"]
    version = hashlib.sha1(Path(build.__file__).read_bytes()).hexdigest()[:12]
    cache = json.loads(BACKTEST_CACHE.read_text()) if BACKTEST_CACHE.exists() else {}
    if cache.get("model_version") != version or cache.get("season") != SEASON:
        cache = {"season": SEASON, "model_version": version, "weeks": {}}
    weeks = [w for w in sorted(int(x) for x in games.week.unique())
             if w >= BACKTEST_FIRST_WEEK and out.week_complete(w)]
    for w in weeks:
        if str(w) in cache["weeks"]:
            continue
        g = games.copy()
        g.loc[g.week == w, ["home_score", "away_score"]] = float("nan")   # as of before kickoff
        played = set(stats[stats.week == w].player_id)
        lines = build.sample_odds(stats, w, played)
        props, _ = build.build_props(stats, g, frames["injuries"], frames["snaps"], w, lines)
        rows = []
        for p in props:
            a = out.actual(p["pid"], w, p["market"])
            if a is not None:
                rows.append([p["market"], p["proj_raw"], p["sd_raw"], a, (p["role_change"] or {}).get("why")])
        cache["weeks"][str(w)] = rows
        log(f"backtest: week {w} -> {len(rows)} projections")
    BACKTEST_CACHE.write_text(json.dumps(cache, separators=(",", ":")))
    out_rows = [{"market": m, "proj_raw": pr, "sd_raw": sd, "actual": a, "role_change": f, "week": int(w)}
                for w, rs in cache["weeks"].items() if int(w) in weeks for m, pr, sd, a, f in rs]
    return out_rows, weeks


def corrections(graded: list[dict], backtest: list[dict]) -> dict:
    """Per-market suggestions from unflagged projections (backtest + real graded props),
    measured against the *raw* model (before any correction), so applying them never compounds.
      proj_mult: total actual / total projected  -> fixes systematic over/under-projection
      sd_mult:   RMS of (actual − corrected proj) / σ  -> 1.0 means σ is the right width;
                 >1 means outcomes spread wider than the model thinks (P(over) too confident)
    Both are shrunk toward 1.0 by n/(n+CORR_SHRINK_K), then clamped."""
    out = {}
    for market in MARKETS:
        ok = lambda r: r["market"] == market and not r.get("role_change") and r.get("proj_raw") and r.get("sd_raw")
        g_rows, b_rows = [r for r in graded if ok(r)], [r for r in backtest if ok(r)]
        rs = g_rows + b_rows
        n = len(rs)
        base = {"n": n, "n_graded": len(g_rows), "n_backtest": len(b_rows)}
        if market in build.BINARY_MARKETS:
            out[market] = {**base, "proj_mult": None, "sd_mult": None, "active": False,
                           "note": "not used: probability is calibrated directly (TD_CAL in build.py)"}
            continue
        if n < CORR_MIN_N:
            out[market] = {**base, "proj_mult": None, "sd_mult": None, "active": False,
                           "note": f"needs {CORR_MIN_N}+ graded projections"}
            continue
        w = n / (n + CORR_SHRINK_K)
        tot_a, tot_p = sum(r["actual"] for r in rs), sum(r["proj_raw"] for r in rs)
        pm_raw = tot_a / tot_p if tot_p else 1.0
        pm = clamp(1 + (pm_raw - 1) * w, *PROJ_MULT_RANGE)
        z = [(r["actual"] - r["proj_raw"] * pm) / r["sd_raw"] for r in rs]
        sm_raw = math.sqrt(statistics.fmean(x * x for x in z))
        sm = clamp(1 + (sm_raw - 1) * w, *SD_MULT_RANGE)
        out[market] = {**base, "bias": round(statistics.fmean(r["actual"] - r["proj_raw"] for r in rs), 2),
                       "proj_mult": round(pm, 3), "proj_mult_raw": round(pm_raw, 3),
                       "sd_mult": round(sm, 3), "sd_mult_raw": round(sm_raw, 3),
                       "active": n >= CORRECTIONS_MIN_N}
    return out


BLEND_DEFAULT_W = 0.25   # until enough graded props exist
BLEND_MIN_N = 300


def fit_blend(rows: list[dict]) -> dict:
    """How much to trust the model vs the market: P = book_no_vig + w·(model − book_no_vig).
    w is fitted on graded props that had both sides priced (minimum Brier over a grid). Week 9 alone gave
    w ≈ 0.2: the model claimed 62% on its picks, the book said 51%, they hit 53.6%. The parlay builder uses it."""
    pts = []
    for r in rows:
        if r.get("role_change") or r["result"] == "push" or r.get("over") is None or r.get("under") is None \
                or r.get("p_over") is None or r["market"] in getattr(build, "BINARY_MARKETS", ()):
            continue
        io, iu = build.american_to_prob(r["over"]), build.american_to_prob(r["under"])
        pts.append((r["p_over"], io / (io + iu), 1 if r["result"] == "over" else 0))
    if len(pts) < BLEND_MIN_N:
        return {"w": BLEND_DEFAULT_W, "n": len(pts), "fitted": False}
    grid = [i / 20 for i in range(21)]
    br = {w: statistics.fmean(((b + w * (m - b)) - y) ** 2 for m, b, y in pts) for w in grid}
    w = min(grid, key=br.get)
    return {"w": w, "n": len(pts), "fitted": True, "brier_model": round(br[1.0], 4),
            "brier_market": round(br[0.0], 4), "brier_blend": round(br[w], 4)}


def build_results(out: Outcomes, frames: dict) -> dict:
    rows, graded, pending = grade_snapshots(out)
    bt, bt_weeks = backtest_rows(frames, out)
    corr = corrections(rows, bt)
    unflagged = [r for r in rows if not r.get("role_change")]
    flagged = [r for r in rows if r.get("role_change")]

    def bucket(rs, lo, hi):
        return [r for r in rs if lo <= abs(r.get("edge_sd") or 0) < hi]

    weeks_n = len(graded)
    return {
        "season": SEASON, "updated": now_iso(),
        "graded_weeks": weeks_n, "graded_week_list": graded, "pending_weeks": pending,
        "corrections_min_n": CORRECTIONS_MIN_N,
        "corrections_active": any(c.get("active") for c in corr.values()),
        "backtest_weeks": bt_weeks,
        "note": "Record = the side the model leans (projection vs line), 1u flat at -110. "
                "Main tables use unflagged props (what the board ranks); role-change props are graded separately.",
        "overall": record(unflagged),
        "by_market": {MARKETS[m][1]: record([r for r in unflagged if r["market"] == m]) for m in MARKETS},
        "by_edge": [{"bucket": b, **record(bucket(unflagged, lo, hi))} for b, lo, hi in EDGE_BUCKETS],
        "flagged_vs_unflagged": {"unflagged": record(unflagged), "flagged": record(flagged),
                                 "flagged_line": record([r for r in flagged if r["role_change"] == "line"]),
                                 "flagged_usage": record([r for r in flagged if r["role_change"] == "usage"]),
                                 "flagged_absent": record([r for r in flagged if r["role_change"] == "absent"]),
                                 "flagged_lowuse": record([r for r in flagged if r["role_change"] == "lowuse"]),
                                 "flagged_thin": record([r for r in flagged if r["role_change"] == "thin"])},
        "calibration": calibration(unflagged),
        "by_week": [{"week": w, **record([r for r in unflagged if r["week"] == w])} for w in graded],
        "corrections": corr,
        "blend": fit_blend(rows),
    }


# ----------------------------------------------------------------------------- parlays
def load_parlay_sheet(url: str) -> pd.DataFrame:
    """The Parlays sheet as CSV: the Apps Script web app's `?format=csv` (or any CSV URL)."""
    r = requests.get(url, timeout=60)   # Apps Script answers via a redirect; requests follows it
    r.raise_for_status()
    text = r.text.strip()
    if not text:
        return pd.DataFrame(columns=["legs_json"])          # sheet exists, no rows yet
    if text.startswith("{") or text.lstrip().startswith("<"):
        raise ValueError("got JSON/HTML instead of CSV -- redeploy docs/parlay-sheet.gs "
                         "as a new version so ?format=csv is supported")
    df = pd.read_csv(io.StringIO(text), dtype=str).fillna("")
    df.columns = [c.strip().lower() for c in df.columns]
    if "legs_json" not in df.columns:
        raise ValueError(f"no legs_json column (got {list(df.columns)[:8]})")
    return df


def grade_leg(leg: dict, week: int | None, out: Outcomes) -> dict:
    g = dict(leg)
    pid, _, market = str(leg.get("prop_id", "")).partition(":")
    if not week or market not in MARKETS:
        return {**g, "status": "unknown"}
    actual = out.actual(pid, week, market)
    if actual is None:
        g["status"] = "void" if out.week_complete(week) else "pending"   # DNP once the week is final
        return g
    res = side_result(actual, float(leg["line"]))
    side = str(leg.get("side", "")).lower()
    g["actual"] = actual
    g["status"] = "push" if res == "push" else "hit" if res == side else "miss"
    return g


def grade_parlays(out: Outcomes) -> dict:
    url = os.environ.get("PARLAY_SHEET_CSV_URL", "").strip()
    base = {"updated": now_iso(), "configured": bool(url), "parlays": [],
            "summary": {"n": 0, "w": 0, "l": 0, "p": 0, "pending": 0, "units": 0.0}}
    if not url:
        # local runs don't have the sheet URL (only the workflow does): keep the last graded file rather than
        # overwriting it with an empty "not configured" one
        if PARLAYS.exists():
            log("parlays: PARLAY_SHEET_CSV_URL not set; keeping the existing parlays.json")
            return json.loads(PARLAYS.read_text())
        log("parlays: PARLAY_SHEET_CSV_URL not set; skipping")
        return base
    try:
        df = load_parlay_sheet(url)
    except Exception as e:
        log(f"parlays: could not read sheet ({e})")
        return {**base, "error": str(e)[:200]}
    parlays, s = [], base["summary"]
    for row in df.to_dict("records"):
        try:
            legs = json.loads(row.get("legs_json") or "[]")
        except json.JSONDecodeError:
            continue
        if not isinstance(legs, list) or not legs:
            continue
        if any(str(l.get("prop_id", "")).startswith("nba:") for l in legs):
            continue   # NBA parlay -- graded by nba.py
        week = int(float(row["week"])) if row.get("week") else None
        graded = [grade_leg(l, week, out) for l in legs]
        st = [l["status"] for l in graded]
        book = float(row["book_odds"]) if row.get("book_odds") else None
        if "miss" in st:
            status, units = "lost", -1.0
        elif "pending" in st or "unknown" in st:
            status, units = "pending", None
        else:
            live = [l for l in graded if l["status"] == "hit"]
            if not live:
                status, units = "push", 0.0
            elif len(live) == len(graded) and book is not None:
                status, units = "won", american_to_decimal(book) - 1
            else:   # pushes/voids drop out; book reprices on the remaining legs
                decs = [american_to_decimal(l.get("price")) for l in live]
                status = "won"
                units = math.prod(decs) - 1 if all(decs) else None
        parlays.append({"id": row.get("id"), "created": row.get("created"), "week": week,
                        "book_odds": book, "model_prob": float(row["model_prob"]) if row.get("model_prob") else None,
                        "legs": graded, "status": status,
                        "units": round(units, 2) if units is not None else None})
        s["n"] += 1
        if status == "won":
            s["w"] += 1
        elif status == "lost":
            s["l"] += 1
        elif status == "push":
            s["p"] += 1
        else:
            s["pending"] += 1
        s["units"] = round(s["units"] + (units or 0), 2)
    parlays.sort(key=lambda x: x.get("created") or "", reverse=True)
    log(f"parlays: {s}")
    return {**base, "parlays": parlays, "summary": s}


# ----------------------------------------------------------------------------- entry
def run(frames: dict) -> None:
    out = Outcomes(frames["stats"], frames["games"])
    OUT.mkdir(parents=True, exist_ok=True)
    results = build_results(out, frames)
    RESULTS.write_text(json.dumps(results, separators=(",", ":")))
    log(f"results: {results['graded_weeks']} graded weeks {results['graded_week_list']}, "
        f"pending {results['pending_weeks']}, overall {results['overall']}")
    PARLAYS.write_text(json.dumps(grade_parlays(out), separators=(",", ":")))
