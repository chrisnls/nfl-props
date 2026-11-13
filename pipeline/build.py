#!/usr/bin/env python3
"""
NFL prop edge pipeline.

1. Pull weekly player stats, schedule, injuries and snap counts from nflverse (free).
2. Pull player prop lines for the current week from The-Odds-API (free tier),
   or synthesize sample lines when no key is configured.
3. Compute hit rates, projections, probabilities and edges per player/market.
4. Write site/data/props.json for the static front-end.

Usage:
    ODDS_API_KEY=xxx python pipeline/build.py
    python pipeline/build.py --sample          # no API calls for odds
    python pipeline/build.py --week 9          # force a week
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import unicodedata
import statistics
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "pipeline" / "raw"
OUT = ROOT / "site" / "data"
SEASON = int(os.environ.get("NFL_SEASON", "2026"))

NFLVERSE = "https://github.com/nflverse/nflverse-data/releases/download"
SOURCES = {
    "stats": f"{NFLVERSE}/stats_player/stats_player_week_{SEASON}.csv",
    "games": f"{NFLVERSE}/schedules/games.csv",
    "injuries": f"{NFLVERSE}/injuries/injuries_{SEASON}.csv",
    "snaps": f"{NFLVERSE}/snap_counts/snap_counts_{SEASON}.csv",
}

ODDS_BASE = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl"
# Odds API market key -> (nflverse stat column, label, positions, discrete?)
MARKETS = {
    "player_pass_yds": ("passing_yards", "Pass Yds", {"QB"}, False),
    "player_pass_tds": ("passing_tds", "Pass TDs", {"QB"}, True),
    "player_pass_attempts": ("attempts", "Pass Att", {"QB"}, True),
    "player_anytime_td": ("anytime_tds", "Anytime TD", {"RB", "WR", "TE", "QB"}, True),
    "player_rush_yds": ("rushing_yards", "Rush Yds", {"QB", "RB", "WR"}, False),
    "player_rush_attempts": ("carries", "Rush Att", {"RB", "QB"}, True),
    "player_reception_yds": ("receiving_yards", "Rec Yds", {"WR", "TE", "RB"}, False),
    "player_receptions": ("receptions", "Receptions", {"WR", "TE", "RB"}, True),
}
# Role-change flag: a line far from the player's own history usually means the book
# is pricing a new role (starter hurt, depth-chart move) that the game log can't see.
# Those "edges" are mostly the model being stale, so they get flagged and sorted
# below everything else. Flag when the line is ROLE_Z historical σ from the season
# average, or ROLE_Z_OUTSIDE σ and beyond every game in the log.
ROLE_Z = 2.0
MIN_GAMES = 3      # fewer games than this: shown as "thin history", not rated, demoted
ROLE_Z_OUTSIDE = 1.5
# ...or when the player's snap share in the last 2 games moved this much vs before
# (both absolute points and relative change must clear the bar) AND the move contradicts
# the model's lean (snaps down but model says over, or up and model says under) -- i.e.
# the book has priced the new role and the season-long game log hasn't caught up.
ROLE_SNAP_PTS = 20
ROLE_SNAP_REL = 0.40
ROLE_SNAP_MIN_EDGE = 0.3   # |edge ÷ σ|; below this the lean is too weak to matter

# Volume stat behind each market, for the "why" breakdown (volume x efficiency).
VOLUME = {"passing_yards": "attempts", "passing_tds": "attempts", "rushing_yards": "carries",
          "carries": "carries", "attempts": "attempts", "receiving_yards": "targets", "receptions": "targets",
          "anytime_tds": "touches"}
VOLUME_LABEL = {"attempts": "att", "carries": "car", "targets": "tgt", "touches": "touch"}
# Anytime TD: a yes/no market (books post "Yes +150", sometimes "No"); treated as over/under 0.5 TDs.
# Expected TDs (λ) = blend of usage-based xTD (projected carries/targets x league TD rate per carry/target
# at the position) and the player's own TD rate, shrunk by games played; scaled by the team's implied points;
# P(scores) = 1 - exp(-λ). Strength uses the probability gap vs the book: edge_sd = (P - fair) / TD_EDGE_SCALE,
# so Lean/Solid/Strong = 3/6/10 percentage points.
ANYTIME = "player_anytime_td"
BINARY_MARKETS = {ANYTIME}
TD_OWN_K = 8          # own-rate weight = n / (n + 8) games
# Recalibration λ' = C + M·λ fitted on the weeks 5-9 backtest (615 props): low-usage players score more often
# than their touches imply (9% predicted -> 21% actual). Fit on wks 5-7, tested on 8-9: Brier .2057 -> .2045.
TD_CAL_C, TD_CAL_M = 0.12, 0.75
TD_EDGE_SCALE = 0.10
LINES_CACHE = ROOT / "pipeline" / "lines_cache.json"   # committed by the workflow; not published
HISTORY = OUT / "history"          # week_NN.json: every live build's props, for grading
RESULTS = OUT / "results.json"     # written by --grade
CORRECTIONS_MIN_N = 100            # a market's correction auto-applies once this many graded projections back it
BACKTEST_CACHE = ROOT / "pipeline" / "backtest_cache.json"   # per-week backtest rows (see grade.py)
ET = ZoneInfo("America/New_York")
BOOKS = ["draftkings", "fanduel", "betmgm", "caesars", "espnbet", "fanatics"]

TEAM_NAMES = {
    "Arizona Cardinals": "ARI", "Atlanta Falcons": "ATL", "Baltimore Ravens": "BAL",
    "Buffalo Bills": "BUF", "Carolina Panthers": "CAR", "Chicago Bears": "CHI",
    "Cincinnati Bengals": "CIN", "Cleveland Browns": "CLE", "Dallas Cowboys": "DAL",
    "Denver Broncos": "DEN", "Detroit Lions": "DET", "Green Bay Packers": "GB",
    "Houston Texans": "HOU", "Indianapolis Colts": "IND", "Jacksonville Jaguars": "JAX",
    "Kansas City Chiefs": "KC", "Las Vegas Raiders": "LV", "Los Angeles Chargers": "LAC",
    "Los Angeles Rams": "LA", "Miami Dolphins": "MIA", "Minnesota Vikings": "MIN",
    "New England Patriots": "NE", "New Orleans Saints": "NO", "New York Giants": "NYG",
    "New York Jets": "NYJ", "Philadelphia Eagles": "PHI", "Pittsburgh Steelers": "PIT",
    "San Francisco 49ers": "SF", "Seattle Seahawks": "SEA", "Tampa Bay Buccaneers": "TB",
    "Tennessee Titans": "TEN", "Washington Commanders": "WAS",
}


# ----------------------------------------------------------------------------- helpers
def log(msg: str) -> None:
    print(f"[build] {msg}", file=sys.stderr)


def download(name: str, url: str, max_age_hours: float = 6) -> Path:
    RAW.mkdir(parents=True, exist_ok=True)
    path = RAW / f"{name}.csv"
    if path.exists() and (time.time() - path.stat().st_mtime) < max_age_hours * 3600:
        return path
    log(f"downloading {name}")
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    path.write_bytes(r.content)
    return path


# Sportsbooks and nflverse don't always use the same name for a player. Key = a normalized
# name a book might use; value = the normalized name nflverse uses. Keys must NOT be another
# real player's nflverse name. Add to this when the build log lists an "unmatched" player
# who does have stats under a different name.
NAME_ALIASES = {
    "hollywood brown": "marquise brown",
    "nathaniel dell": "tank dell",
    "joshua palmer": "josh palmer",
    "chigoziem okonkwo": "chig okonkwo",
    "zonovan knight": "bam knight",
    "cameron ward": "cam ward",
    "kevin concepcion": "kc concepcion",
}


def norm_name(name) -> str:
    if not isinstance(name, str):
        return ""
    # fold accents (Schröder -> schroder, Jokić -> jokic) so feeds that spell names differently still match
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    n = re.sub(r"[.'’]", "", n)
    n = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", n)
    n = re.sub(r"[^a-z ]", " ", n)
    n = " ".join(n.split())
    return NAME_ALIASES.get(n, n)


def american_to_prob(price: float) -> float:
    return 100 / (price + 100) if price > 0 else -price / (-price + 100)


def norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def r1(x: float | None) -> float | None:
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), 1)


# ----------------------------------------------------------------------------- data
def load_frames() -> dict[str, pd.DataFrame]:
    frames = {k: pd.read_csv(download(k, u), low_memory=False) for k, u in SOURCES.items()}
    g = frames["games"]
    frames["games"] = g[(g.season == SEASON) & (g.game_type == "REG")].copy()
    st = frames["stats"]
    st["anytime_tds"] = st.rushing_tds.fillna(0) + st.receiving_tds.fillna(0)
    st["touches"] = st.carries.fillna(0) + st.targets.fillna(0)
    return frames


def current_week(games: pd.DataFrame, forced: int | None) -> int:
    if forced:
        return forced
    unplayed = games[games.home_score.isna()]
    return int(unplayed.week.min()) if len(unplayed) else int(games.week.max())


def week_games(games: pd.DataFrame, week: int) -> list[dict]:
    out = []
    for _, g in games[games.week == week].sort_values(["gameday", "gametime"]).iterrows():
        out.append({
            "id": g.game_id, "away": g.away_team, "home": g.home_team,
            "kickoff": f"{g.gameday}T{g.gametime}",
            "spread": r1(g.spread_line), "total": r1(g.total_line),
            "played": not pd.isna(g.home_score),
        })
    return out


# Matchup factor basis: a defense's *typical* game (median) vs the league's typical game.
# Averages get dragged by blowouts -- e.g. a team that allowed 224 and 246 rushing yards in
# two lopsided losses looks like a sieve on average but is league-average in a normal game.
# Pass TDs (0/1/2/3 per game) are too coarse for a median, so they keep the mean.
MEAN_BASIS = {"passing_tds"}


def kickoff_utc(kickoff: str) -> dt.datetime:
    """nflverse 'YYYY-MM-DDTHH:MM' is US Eastern wall-clock."""
    return dt.datetime.fromisoformat(kickoff).replace(tzinfo=ET).astimezone(dt.timezone.utc)


def defense_factors(stats: pd.DataFrame) -> dict[tuple[str, str, str], float]:
    """(team, position_group, stat) -> multiplier vs league typical allowed."""
    factors: dict[tuple[str, str, str], float] = {}
    pos_groups = {"QB": ["QB"], "RB": ["RB"], "WR": ["WR"], "TE": ["TE"]}
    for _, (col, _label, _pos, _disc) in MARKETS.items():
        for grp, positions in pos_groups.items():
            sub = stats[stats.position.isin(positions)]
            per_game = sub.groupby(["opponent_team", "game_id"])[col].sum().reset_index()
            if col in MEAN_BASIS:
                by_team = per_game.groupby("opponent_team")[col].mean()
                league = by_team.mean()
            else:
                by_team = per_game.groupby("opponent_team")[col].median()
                league = per_game[col].median()
            if not league or math.isnan(league):
                continue
            for team, val in by_team.items():
                factors[(team, grp, col)] = float(val / league)
    return factors


def defense_profiler(hist: pd.DataFrame, scores: dict):
    """Return profile(team, grp, col): how a defense has done vs one position group on
    one stat, game by game, with league context. Memoized; only built for this week's matchups."""
    league_cache: dict = {}
    memo: dict = {}

    def per_game(grp, col):
        if (grp, col) not in league_cache:
            vcol = VOLUME[col]
            sub = hist[hist.position == grp]
            pg = sub.groupby(["opponent_team", "game_id"]).agg(
                val=(col, "sum"), vol=(vcol, "sum"), week=("week", "first"), off=("team", "first")).reset_index()
            league_cache[(grp, col)] = pg
        return league_cache[(grp, col)]

    def profile(team, grp, col):
        key = f"{team}|{grp}|{col}"
        if key in memo:
            return key
        pg = per_game(grp, col)
        by_team = pg.groupby("opponent_team").val.mean().sort_values(ascending=False)
        if team not in by_team.index:
            return None
        mine = pg[pg.opponent_team == team].sort_values("week")
        sub = hist[(hist.position == grp) & (hist.opponent_team == team)]
        games_out = []
        for r in mine.itertuples():
            g = sub[sub.game_id == r.game_id]
            top = g.loc[g[col].idxmax()] if len(g) else None
            sc = scores.get(r.game_id)
            d_pts = o_pts = None
            if sc:
                home, away, hs, as_ = sc
                d_pts, o_pts = (hs, as_) if home == team else (as_, hs)
            games_out.append({
                "wk": int(r.week), "vs": r.off, "val": r1(r.val), "vol": int(r.vol) if not pd.isna(r.vol) else None,
                "top": top.player_display_name if top is not None else None,
                "top_val": r1(top[col]) if top is not None else None,
                "top_vol": int(top[VOLUME[col]]) if top is not None and not pd.isna(top[VOLUME[col]]) else None,
                "def_pts": d_pts, "off_pts": o_pts,
            })
        vals = [g["val"] for g in games_out]
        vol_sum = mine.vol.sum()
        lg_vol = pg.vol.sum()
        memo[key] = {
            "team": team, "grp": grp, "col": col, "vol_label": VOLUME_LABEL[VOLUME[col]],
            "rank": int(list(by_team.index).index(team)) + 1, "n_teams": int(len(by_team)),
            "avg": r1(statistics.fmean(vals)), "median": r1(statistics.median(vals)),
            "l3": r1(statistics.fmean(vals[-3:])),
            "lg_avg": r1(by_team.mean()), "lg_median": r1(pg.val.median()),
            "per_vol": round(float(mine.val.sum() / vol_sum), 2) if vol_sum else None,
            "lg_per_vol": round(float(pg.val.sum() / lg_vol), 2) if lg_vol else None,
            "vol_avg": r1(mine.vol.mean()), "lg_vol_avg": r1(pg.groupby("opponent_team").vol.mean().mean()),
            "games": games_out,
        }
        return key

    return profile, memo


# ----------------------------------------------------------------------------- odds
class CreditsLow(Exception):
    pass


def save_lines_cache(lines: dict, week: int, usage: dict) -> None:
    LINES_CACHE.parent.mkdir(parents=True, exist_ok=True)
    LINES_CACHE.write_text(json.dumps({
        "season": SEASON, "week": week, "usage": usage,
        "fetched": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
        "lines": [{"player": k[0], "market": k[1], "books": v} for k, v in lines.items()],
    }, separators=(",", ":")))


def load_lines_cache(week: int) -> tuple[dict, dict, str] | None:
    """Last live pull's lines, if they're for this week. No API call, no credits."""
    if not LINES_CACHE.exists():
        return None
    c = json.loads(LINES_CACHE.read_text())
    if c.get("season") != SEASON or c.get("week") != week:
        log(f"cached lines are for week {c.get('week')}, not {week}")
        return None
    return {(x["player"], x["market"]): x["books"] for x in c["lines"]}, c.get("usage", {}), c["fetched"]


def fetch_odds(week_game_list: list[dict], api_key: str) -> tuple[dict, dict]:
    """Return ({(norm_player, market): [line dicts]}, usage_info)."""
    session = requests.Session()
    events = session.get(f"{ODDS_BASE}/events", params={"apiKey": api_key}, timeout=60)
    events.raise_for_status()
    usage = {"remaining": events.headers.get("x-requests-remaining"),
             "used": events.headers.get("x-requests-used")}
    now = dt.datetime.now(dt.timezone.utc)
    started = {ev["id"] for ev in events.json()
               if dt.datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00")) <= now}
    want = {(g["away"], g["home"]) for g in week_game_list
            if not g["played"] and kickoff_utc(g["kickoff"]) > now}
    # /events is free; each event's odds call costs len(MARKETS) credits (1 region).
    # Don't start a pull we can't finish -- a half-pulled slate is worse than cached lines.
    needed = len(MARKETS) * sum(1 for ev in events.json() if ev["id"] not in started and
                                (TEAM_NAMES.get(ev["away_team"]), TEAM_NAMES.get(ev["home_team"])) in want)
    remaining = int(usage["remaining"]) if str(usage.get("remaining") or "").isdigit() else None
    if remaining is not None and remaining < needed:
        raise CreditsLow(f"{remaining} credits left, full pull needs ~{needed}")
    lines: dict[tuple[str, str], list] = {}
    matched_events = 0
    for ev in events.json():
        pair = (TEAM_NAMES.get(ev["away_team"]), TEAM_NAMES.get(ev["home_team"]))
        if pair not in want or ev["id"] in started:
            continue
        matched_events += 1
        r = session.get(
            f"{ODDS_BASE}/events/{ev['id']}/odds",
            params={"apiKey": api_key, "regions": "us", "oddsFormat": "american",
                    "markets": ",".join(MARKETS), "bookmakers": ",".join(BOOKS)},
            timeout=60,
        )
        if r.status_code != 200:
            log(f"odds {ev['id']} -> {r.status_code} {r.text[:120]}")
            continue
        usage["remaining"] = r.headers.get("x-requests-remaining", usage["remaining"])
        usage["used"] = r.headers.get("x-requests-used", usage["used"])
        for bk in r.json().get("bookmakers", []):
            for m in bk.get("markets", []):
                if m["key"] not in MARKETS:
                    continue
                grouped: dict[str, dict] = {}
                for o in m["outcomes"]:
                    d = grouped.setdefault(o["description"], {})
                    d[o["name"].lower()] = (o.get("point"), o.get("price"))
                for player, sides in grouped.items():
                    over, under = sides.get("over") or sides.get("yes"), sides.get("under") or sides.get("no")
                    point = (over or under or (None, None))[0]
                    if point is None and m["key"] in BINARY_MARKETS:
                        point = 0.5          # yes/no market == over/under 0.5
                    if point is None:
                        continue
                    lines.setdefault((norm_name(player), m["key"]), []).append({
                        "book": bk["key"], "line": float(point),
                        "over": over[1] if over else None,
                        "under": under[1] if under else None,
                        "name": player,
                    })
    usage["events"] = matched_events
    log(f"odds: {matched_events} events, {len(lines)} player-markets, credits remaining {usage['remaining']}")
    return lines, usage


def sample_odds(stats: pd.DataFrame, week: int, players_in_week: set[str]) -> dict:
    """Fake lines at each player's recent average so the UI works without a key."""
    lines: dict[tuple[str, str], list] = {}
    hist = stats[stats.week < week]
    for market, (col, _l, positions, discrete) in MARKETS.items():
        sub = hist[hist.position.isin(positions)]
        for pid, grp in sub.groupby("player_id"):
            if pid not in players_in_week or len(grp) < 4:
                continue
            avg = grp.sort_values("week")[col].tail(5).mean()
            if market in BINARY_MARKETS:   # anytime TD: anyone with real usage gets a +150 / -190 sample line
                if grp.sort_values("week").touches.tail(5).mean() < 4:
                    continue
                name = grp.player_display_name.iloc[0]
                lines[(norm_name(name), market)] = [{"book": "sample", "line": 0.5, "over": 150, "under": -190, "name": name}]
                continue
            if avg < (1.5 if discrete else 20):
                continue
            line = math.floor(avg) + 0.5   # nearest x.5 below avg -> edges split both ways
            name = grp.player_display_name.iloc[0]
            lines[(norm_name(name), market)] = [
                {"book": "sample", "line": float(line), "over": -112, "under": -112, "name": name}
            ]
    return lines


# ----------------------------------------------------------------------------- model
def consensus(book_lines: list[dict]) -> dict:
    pts = [b["line"] for b in book_lines]
    line = statistics.median(pts)
    at_line = [b for b in book_lines if b["line"] == line] or book_lines
    best_over = max((b for b in at_line if b["over"] is not None), key=lambda b: b["over"], default=None)
    best_under = max((b for b in at_line if b["under"] is not None), key=lambda b: b["under"], default=None)
    return {
        "line": line,
        "over": best_over["over"] if best_over else None,
        "over_book": best_over["book"] if best_over else None,
        "under": best_under["under"] if best_under else None,
        "under_book": best_under["book"] if best_under else None,
        "books": sorted({b["book"] for b in book_lines}),
        "spread": round(max(pts) - min(pts), 1),
    }


def project(values: list[float], factor: float, discrete: bool) -> tuple[float, float]:
    """Return (projection, sd) from a game log (oldest -> newest)."""
    season = statistics.fmean(values)
    l5 = statistics.fmean(values[-5:])
    l10 = statistics.fmean(values[-10:])
    base = 0.45 * l5 + 0.30 * l10 + 0.25 * season
    adj = 1 + 0.5 * (max(0.8, min(1.2, factor)) - 1)   # half-weight the matchup
    proj = base * adj
    sd = statistics.pstdev(values) if len(values) > 2 else season * 0.4
    floor = max(1.2, 0.35 * season) if discrete else max(8.0, season * 0.35)
    return proj, max(sd, floor) * 1.15   # inflate: small samples, model error


def role_change(values: list[float], line: float, discrete: bool,
                snaps: list[float | None] | None = None, edge_sd: float = 0.0) -> dict | None:
    """Flag a prop whose game log probably no longer describes the player's role:
    the line sits far outside their history, or their snap share just shifted sharply."""
    season = statistics.fmean(values)
    raw_sd = statistics.pstdev(values)
    floor = max(1.0, 0.25 * season) if discrete else max(6.0, 0.25 * season)
    z = (line - season) / max(raw_sd, floor)
    outside = line > max(values) or line < min(values)
    base = {"z": round(z, 1), "lo": min(values), "hi": max(values)}
    if abs(z) >= ROLE_Z or (outside and abs(z) >= ROLE_Z_OUTSIDE):
        return {"why": "line", "dir": "up" if z > 0 else "down", **base}
    snaps = [x for x in (snaps or []) if x is not None]
    if len(snaps) >= 4:
        recent, prior = statistics.fmean(snaps[-2:]), statistics.fmean(snaps[:-2])
        contradicts = (recent < prior) == (edge_sd > 0) and abs(edge_sd) >= ROLE_SNAP_MIN_EDGE
        if prior and contradicts and abs(recent - prior) >= ROLE_SNAP_PTS and abs(recent - prior) / prior >= ROLE_SNAP_REL:
            return {"why": "usage", "dir": "up" if recent > prior else "down",
                    "snap_prior": round(prior), "snap_recent": round(recent), **base}
    return None


def build_props(stats, games, injuries, snaps, week, lines, corrections=None) -> list[dict]:
    hist = stats[stats.week < week].copy()
    this_week = games[games.week == week]
    team_game = {}
    for _, g in this_week.iterrows():
        if not pd.isna(g.home_score):
            continue   # already played
        team_game[g.home_team] = (g.game_id, g.away_team, True)
        team_game[g.away_team] = (g.game_id, g.home_team, False)

    factors = defense_factors(hist)
    # anytime-TD inputs: league TD rate per carry / per target by position, team implied points tonight
    td_rate = {}
    for pos_, g_ in hist.groupby("position"):
        c, t = g_.carries.fillna(0).sum(), g_.targets.fillna(0).sum()
        td_rate[pos_] = (g_.rushing_tds.fillna(0).sum() / c if c else 0.0, g_.receiving_tds.fillna(0).sum() / t if t else 0.0)
    done = games[games.home_score.notna() & (games.week < week)]
    league_ppg = float(pd.concat([done.home_score, done.away_score]).mean()) if len(done) else 22.0
    implied = {}
    for _, g in this_week.iterrows():
        if pd.isna(g.total_line) or pd.isna(g.spread_line):
            continue
        implied[g.home_team] = (g.total_line + g.spread_line) / 2    # spread_line > 0: home favored
        implied[g.away_team] = (g.total_line - g.spread_line) / 2
    latest_team = stats.sort_values("week").groupby("player_id").team.last()

    inj = injuries[injuries.week == injuries.week.max()]
    def clean(v):
        return v if isinstance(v, str) else None
    # keyed by (name, team): two players can share a name (e.g. Justin Jefferson MIN WR / CLE LB)
    inj_by_player = {
        (norm_name(r.full_name), r.team): {"status": clean(r.report_status), "injury": clean(r.report_primary_injury),
                                 "practice": clean(r.practice_status)}
        for r in inj.itertuples() if isinstance(r.full_name, str)
    }
    team_injuries: dict[str, list] = {}
    for r in inj.itertuples():
        if isinstance(r.report_status, str) and r.position in ("QB", "RB", "WR", "TE", "T", "G", "C"):
            team_injuries.setdefault(r.team, []).append(
                {"name": r.full_name, "pos": r.position, "status": r.report_status, "injury": clean(r.report_primary_injury)})

    snap_pct = {}
    snap_by_week: dict[tuple[str, int], float] = {}
    if len(snaps):
        s = snaps[snaps.week < week].sort_values("week")
        for (name, tm), grp in s.groupby(["player", "team"]):
            snap_pct[(norm_name(name), tm)] = [r1(v * 100) for v in grp.offense_pct.tail(5)]
        for r in s.itertuples():
            if not pd.isna(r.offense_pct):
                snap_by_week[(norm_name(r.player), r.team, int(r.week))] = round(float(r.offense_pct) * 100)

    # game-by-game context for the "why" breakdown
    scores = {g.game_id: (g.home_team, g.away_team, None if pd.isna(g.home_score) else int(g.home_score),
                          None if pd.isna(g.away_score) else int(g.away_score)) for g in games.itertuples()}
    team_vol = {vc: hist.groupby(["team", "week"])[vc].sum() for vc in set(VOLUME.values())}
    profile, defense = defense_profiler(hist, scores)

    def game_context(r, pname, pid, pos, col):
        vc = VOLUME[col]
        vol = getattr(r, vc)
        vol = None if pd.isna(vol) else int(vol)
        tot = team_vol[vc].get((r.team, r.week))
        home, away, hs, as_ = scores.get(r.game_id, (None, None, None, None))
        pts, opp_pts = (hs, as_) if home == r.team else (as_, hs)
        mates = hist[(hist.team == r.team) & (hist.week == r.week) & (hist.position == pos) & (hist.player_id != pid)]
        mate = None
        if len(mates):
            m = mates.loc[mates[vc].fillna(0).idxmax()]
            if not pd.isna(m[vc]) and m[vc] > 0:
                mate = {"name": m.player_display_name, "vol": int(m[vc]),
                        "snap": snap_by_week.get((norm_name(m.player_display_name), r.team, int(r.week)))}
        return {
            "wk": int(r.week), "opp": r.opponent_team, "home": r.team == r.game_id.split("_")[3],
            "val": float(getattr(r, col)) if not pd.isna(getattr(r, col)) else 0.0,
            "vol": vol, "share": round(100 * vol / tot) if vol is not None and tot else None,
            "snap": snap_by_week.get((pname, r.team, int(r.week))),
            "pts": pts, "opp_pts": opp_pts, "mate": mate,
        }

    props: list[dict] = []
    unmatched = set()
    by_name: dict[str, list[str]] = {}
    for pid_, n in stats.drop_duplicates("player_id").set_index("player_id").player_display_name.items():
        by_name.setdefault(norm_name(n), []).append(pid_)
    games_played = hist.groupby("player_id").size()
    last_pos = hist.sort_values("week").groupby("player_id").position.last()

    def resolve(pname: str, positions: set[str]) -> str | None:
        """Name -> player_id. When several players share a name, prefer one whose team plays
        this week and whose position fits the market, then the one with the most games."""
        cands = by_name.get(pname) or []
        if len(cands) <= 1:
            return cands[0] if cands else None
        return max(cands, key=lambda c: (latest_team.get(c) in team_game, last_pos.get(c) in positions,
                                         int(games_played.get(c, 0))))

    for (pname, market), book_lines in lines.items():
        col, label, positions, discrete = MARKETS[market]
        pname = norm_name(pname)     # re-apply aliases (cached lines may predate an alias)
        pid = resolve(pname, positions)
        if pid is None:
            unmatched.add(book_lines[0]["name"])
            continue
        glog = hist[hist.player_id == pid].sort_values("week")
        if len(glog) == 0:
            continue
        thin = len(glog) < MIN_GAMES
        team = latest_team.get(pid)
        if team not in team_game:
            continue
        game_id, opp, is_home = team_game[team]
        pos = glog.position.iloc[-1]
        cons = consensus(book_lines)
        line = cons["line"]
        values = [float(v) for v in glog[col].fillna(0)]
        factor = factors.get((opp, pos if pos in ("QB", "RB", "WR", "TE") else "WR", col), 1.0)
        if market == ANYTIME:
            car = [float(v) for v in glog.carries.fillna(0)]
            tgt = [float(v) for v in glog.targets.fillna(0)]
            wa = lambda xs: 0.45 * statistics.fmean(xs[-5:]) + 0.30 * statistics.fmean(xs[-10:]) + 0.25 * statistics.fmean(xs)
            rr, tr = td_rate.get(pos, (0.0, 0.0))
            xtd = wa(car) * rr + wa(tgt) * tr
            w_own = len(values) / (len(values) + TD_OWN_K)
            lam = w_own * wa(values) + (1 - w_own) * xtd
            env = implied.get(team, league_ppg) / league_ppg
            factor = env
            lam *= 1 + 0.5 * (max(0.7, min(1.3, env)) - 1)          # half-weight, capped
            lam = TD_CAL_C + TD_CAL_M * lam
            proj, sd = lam, max(0.1, math.sqrt(lam))
        else:
            proj, sd = project(values, factor, discrete)
        proj_raw, sd_raw = proj, sd
        corr = (corrections or {}).get(market) if market not in BINARY_MARKETS else None   # TD: calibrated on P directly
        if corr:   # learned from backtest + graded weeks (see grade.py); active per market at CORRECTIONS_MIN_N
            proj, sd = proj * corr["proj_mult"], sd * corr["sd_mult"]

        # continuity for counts: over a whole-number line L means L+1 or more (L is a push), so
        # compare at L+0.5; a half-point line (1.5) already sits between the integers -- no shift.
        z_line = (line + (0.5 if discrete and float(line).is_integer() else 0)) - proj
        p_over = 1 - math.exp(-proj) if market == ANYTIME else 1 - norm_cdf(z_line / sd)
        imp_over = american_to_prob(cons["over"]) if cons["over"] is not None else None
        imp_under = american_to_prob(cons["under"]) if cons["under"] is not None else None
        # strip vig for fair implied probability
        fair_over = imp_over / (imp_over + imp_under) if imp_over and imp_under else imp_over
        ev_over = (p_over * (cons["over"] / 100 if cons["over"] > 0 else 100 / -cons["over"]) - (1 - p_over)) if cons["over"] is not None else None
        ev_under = ((1 - p_over) * (cons["under"] / 100 if cons["under"] > 0 else 100 / -cons["under"]) - p_over) if cons["under"] is not None else None

        glog_ctx = [game_context(r, pname, pid, pos, col) for r in glog.itertuples()]
        edge_sd = ((p_over - fair_over) / TD_EDGE_SCALE if fair_over is not None else 0.0) if market == ANYTIME \
            else (proj - line) / sd
        role = role_change(values, line, discrete, [g["snap"] for g in glog_ctx], edge_sd)
        if market == ANYTIME and role and role["why"] == "line":
            role = None   # a 0.5 TD "line" says nothing about role

        if thin:   # too few games to project honestly: show the line and the games, don't rate it
            role = {"why": "thin", "games": len(values), "dir": "up" if line > statistics.fmean(values) else "down",
                    "z": None, "lo": min(values), "hi": max(values)}
        grp = pos if pos in ("QB", "RB", "WR", "TE") else "WR"
        def_key = profile(opp, grp, col)
        hits = [v > line for v in values]
        def rate(n):
            sub = hits[-n:]
            return round(100 * sum(sub) / len(sub)) if sub else None

        props.append({
            "id": f"{pid}:{market}",
            "player": glog.player_display_name.iloc[-1],
            "pid": pid,
            "pos": pos,
            "team": team,
            "opp": opp,
            "home": is_home,
            "game": game_id,
            "market": market,
            "label": label,
            "line": line,
            "over": cons["over"], "over_book": cons["over_book"],
            "under": cons["under"], "under_book": cons["under_book"],
            "books": cons["books"], "line_spread": cons["spread"],
            "proj": round(proj, 2) if market == ANYTIME else r1(proj),
            "sd": r1(sd),
            "edge": round(p_over - fair_over, 3) if market == ANYTIME and fair_over is not None else
                    (0.0 if market == ANYTIME else r1(proj - line)),
            "proj_raw": round(proj_raw, 3) if market == ANYTIME else r1(proj_raw), "sd_raw": r1(sd_raw),
            "edge_sd": round(edge_sd, 2),
            "role_change": role,
            "p_over": round(p_over, 3),
            "fair_over": round(fair_over, 3) if fair_over else None,
            "ev_over": round(ev_over, 3) if ev_over is not None else None,
            "ev_under": round(ev_under, 3) if ev_under is not None else None,
            "avg_season": r1(statistics.fmean(values)),
            "avg_l5": r1(statistics.fmean(values[-5:])),
            "avg_l10": r1(statistics.fmean(values[-10:])),
            "median": r1(statistics.median(values)),
            "hit_l5": rate(5), "hit_l10": rate(10), "hit_season": rate(len(hits)),
            "def_factor": round(factor, 2),
            "def_basis": "mean" if col in MEAN_BASIS else "median",
            "games": glog_ctx,
            "vol_label": VOLUME_LABEL[VOLUME[col]],
            "defense": def_key,
            "snaps": snap_pct.get((pname, team)),
            "injury": inj_by_player.get((pname, team)),
            "team_injuries": team_injuries.get(team, []),
            "opp_injuries": [i for i in team_injuries.get(opp, []) if i["status"] in ("Out", "Doubtful")],
            "headshot": glog.headshot_url.iloc[-1] if isinstance(glog.headshot_url.iloc[-1], str) else None,
        })
    if unmatched:
        log(f"unmatched players from odds feed: {sorted(unmatched)[:20]}{' ...' if len(unmatched) > 20 else ''}")
    props.sort(key=lambda p: (p["role_change"] is not None, -abs(p["edge_sd"])))
    return props, defense


# ----------------------------------------------------------------------------- main
# ----------------------------------------------------------------------------- history
SNAP_FIELDS = ["id", "pid", "player", "pos", "team", "opp", "game", "market", "label", "line",
               "over", "over_book", "under", "under_book", "proj", "sd", "proj_raw", "sd_raw",
               "p_over", "edge", "edge_sd"]


def write_snapshot(props: list[dict], wg: list[dict], week: int, fetched: str) -> Path:
    """Merge this live build into site/data/history/week_NN.json for grading.

    Only props whose game kicks off *after* the lines were pulled are kept, so in-game
    lines never get graded. Props are merged by id: a newer pre-game pull replaces an
    older one, and props from earlier pulls (e.g. Thursday's game) are kept."""
    HISTORY.mkdir(parents=True, exist_ok=True)
    path = HISTORY / f"week_{week:02d}.json"
    snap = json.loads(path.read_text()) if path.exists() else {"season": SEASON, "week": week, "props": []}
    t = dt.datetime.fromisoformat(fetched)
    kick = {g["id"]: kickoff_utc(g["kickoff"]) for g in wg}
    by_id = {p["id"]: p for p in snap["props"]}
    added = 0
    for p in props:
        if p["game"] not in kick or kick[p["game"]] <= t:
            continue
        row = {k: p.get(k) for k in SNAP_FIELDS}
        row["role_change"] = (p["role_change"] or {}).get("why")   # None | "line" | "usage"
        row["fetched"] = fetched
        by_id[p["id"]] = row
        added += 1
    snap["props"] = sorted(by_id.values(), key=lambda r: r["id"])
    snap["updated"] = fetched
    path.write_text(json.dumps(snap, separators=(",", ":")))
    log(f"snapshot: {added} pre-game props -> {path.name} ({len(snap['props'])} total)")
    return path


def load_corrections() -> tuple[dict, dict]:
    """Per-market corrections from results.json -- only markets grade.py marked active
    (CORRECTIONS_MIN_N+ graded projections from the backtest and real graded props)."""
    if not RESULTS.exists():
        return {}, {"active": False}
    r = json.loads(RESULTS.read_text())
    corr = {m: {"proj_mult": c["proj_mult"], "sd_mult": c["sd_mult"]}
            for m, c in (r.get("corrections") or {}).items() if c and c.get("active")}
    if not corr:
        return {}, {"active": False}
    log(f"applying corrections: {corr}")
    return corr, {"active": True, "by_market": corr, "from": r.get("updated")}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", type=int)
    ap.add_argument("--lines", choices=["live", "cached", "sample"], default="live",
                    help="live: pull from the Odds API (~6 credits/game); cached: reuse the last live "
                         "pull for this week (free); sample: synthesize lines at recent averages (free)")
    ap.add_argument("--sample", action="store_true", help="same as --lines sample")
    ap.add_argument("--grade", action="store_true",
                    help="grade completed history snapshots and saved parlays, write results.json "
                         "and parlays.json, then exit (no odds calls)")
    args = ap.parse_args()

    frames = load_frames()
    if args.grade:
        import grade
        grade.run(frames)
        return
    stats, games = frames["stats"], frames["games"]
    week = current_week(games, args.week)
    wg = week_games(games, week)
    log(f"season {SEASON} week {week}: {len(wg)} games, stats through week {int(stats.week.max())}")

    api_key = os.environ.get("ODDS_API_KEY", "").strip()
    teams_this_week = {g["home"] for g in wg} | {g["away"] for g in wg}
    players_in_week = set(stats[stats.team.isin(teams_this_week)].player_id)
    want = "sample" if args.sample else args.lines
    lines, usage, usage_err, lines_fetched = None, {}, {}, None
    if want == "live" and not api_key:
        log("no ODDS_API_KEY -> trying cached lines")
        want = "cached"
    if want == "live":
        try:
            lines, usage = fetch_odds(wg, api_key)
            lines_fetched = dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")
            save_lines_cache(lines, week, usage)
            mode = "live"
        except Exception as e:  # bad key, out of credits, outage -> keep the site alive
            log(f"ODDS API: {e}; falling back to cached lines")
            usage_err = {"error": str(e)[:200]}
            want = "cached"
    if want == "cached":
        cached = load_lines_cache(week)
        if cached:
            lines, usage, lines_fetched = cached
            usage = {**usage, **usage_err}
            mode = "cached"
            log(f"using cached lines from {lines_fetched} (no credits used)")
    if lines is None:
        log("using sample lines")
        lines, usage, mode = sample_odds(stats, week, players_in_week), usage_err, "sample"

    corrections, corr_info = load_corrections()
    props, defense = build_props(stats, games, frames["injuries"], frames["snaps"], week, lines, corrections)
    OUT.mkdir(parents=True, exist_ok=True)
    payload = {
        "season": SEASON, "week": week, "mode": mode,
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
        "lines_fetched": lines_fetched,
        "stats_through_week": int(stats.week.max()),
        "usage": usage,
        "markets": {k: v[1] for k, v in MARKETS.items()},
        "games": wg,
        "props": props,
        "defense": defense,
        "corrections": corr_info,
    }
    (OUT / "props.json").write_text(json.dumps(payload, separators=(",", ":")))
    log(f"wrote {len(props)} props -> {OUT / 'props.json'}")
    if mode == "live":
        write_snapshot(props, wg, week, lines_fetched)


if __name__ == "__main__":
    main()
