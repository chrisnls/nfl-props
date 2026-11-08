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
import statistics
import sys
import time
from pathlib import Path

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
ROLE_Z_OUTSIDE = 1.5
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


def norm_name(name) -> str:
    if not isinstance(name, str):
        return ""
    n = name.lower()
    n = re.sub(r"[.'’]", "", n)
    n = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", n)
    n = re.sub(r"[^a-z ]", " ", n)
    return " ".join(n.split())


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


def defense_factors(stats: pd.DataFrame) -> dict[tuple[str, str, str], float]:
    """(team, position_group, stat) -> multiplier vs league average allowed."""
    factors: dict[tuple[str, str, str], float] = {}
    pos_groups = {"QB": ["QB"], "RB": ["RB"], "WR": ["WR"], "TE": ["TE"]}
    for _, (col, _label, _pos, _disc) in MARKETS.items():
        for grp, positions in pos_groups.items():
            sub = stats[stats.position.isin(positions)]
            per_game = sub.groupby(["opponent_team", "game_id"])[col].sum().reset_index()
            by_team = per_game.groupby("opponent_team")[col].mean()
            league = by_team.mean()
            if not league or math.isnan(league):
                continue
            for team, val in by_team.items():
                factors[(team, grp, col)] = float(val / league)
    return factors


# ----------------------------------------------------------------------------- odds
def fetch_odds(week_game_list: list[dict], api_key: str) -> tuple[dict, dict]:
    """Return ({(norm_player, market): [line dicts]}, usage_info)."""
    session = requests.Session()
    events = session.get(f"{ODDS_BASE}/events", params={"apiKey": api_key}, timeout=60)
    events.raise_for_status()
    usage = {"remaining": events.headers.get("x-requests-remaining"),
             "used": events.headers.get("x-requests-used")}
    want = {(g["away"], g["home"]) for g in week_game_list if not g["played"]}
    lines: dict[tuple[str, str], list] = {}
    matched_events = 0
    for ev in events.json():
        pair = (TEAM_NAMES.get(ev["away_team"]), TEAM_NAMES.get(ev["home_team"]))
        if pair not in want:
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
                    over, under = sides.get("over"), sides.get("under")
                    point = (over or under or (None, None))[0]
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


def role_change(values: list[float], line: float, discrete: bool) -> dict | None:
    """Flag a line that sits far outside the player's own game-to-game history."""
    season = statistics.fmean(values)
    raw_sd = statistics.pstdev(values)
    floor = max(1.0, 0.25 * season) if discrete else max(6.0, 0.25 * season)
    z = (line - season) / max(raw_sd, floor)
    outside = line > max(values) or line < min(values)
    if abs(z) < ROLE_Z and not (outside and abs(z) >= ROLE_Z_OUTSIDE):
        return None
    return {"dir": "up" if z > 0 else "down", "z": round(z, 1),
            "lo": min(values), "hi": max(values)}


def build_props(stats, games, injuries, snaps, week, lines) -> list[dict]:
    hist = stats[stats.week < week].copy()
    this_week = games[games.week == week]
    team_game = {}
    for _, g in this_week.iterrows():
        if not pd.isna(g.home_score):
            continue   # already played
        team_game[g.home_team] = (g.game_id, g.away_team, True)
        team_game[g.away_team] = (g.game_id, g.home_team, False)

    factors = defense_factors(hist)
    latest_team = stats.sort_values("week").groupby("player_id").team.last()

    inj = injuries[injuries.week == injuries.week.max()]
    def clean(v):
        return v if isinstance(v, str) else None
    inj_by_player = {
        norm_name(r.full_name): {"status": clean(r.report_status), "injury": clean(r.report_primary_injury),
                                 "practice": clean(r.practice_status)}
        for r in inj.itertuples() if isinstance(r.full_name, str)
    }
    team_injuries: dict[str, list] = {}
    for r in inj.itertuples():
        if isinstance(r.report_status, str) and r.position in ("QB", "RB", "WR", "TE", "T", "G", "C"):
            team_injuries.setdefault(r.team, []).append(
                {"name": r.full_name, "pos": r.position, "status": r.report_status, "injury": clean(r.report_primary_injury)})

    snap_pct = {}
    if len(snaps):
        s = snaps[snaps.week < week].sort_values("week")
        for name, grp in s.groupby("player"):
            snap_pct[norm_name(name)] = [r1(v * 100) for v in grp.offense_pct.tail(5)]

    props: list[dict] = []
    unmatched = set()
    by_name = {norm_name(n): pid for pid, n in
               stats.drop_duplicates("player_id").set_index("player_id").player_display_name.items()}

    for (pname, market), book_lines in lines.items():
        col, label, positions, discrete = MARKETS[market]
        pid = by_name.get(pname)
        if pid is None:
            unmatched.add(book_lines[0]["name"])
            continue
        glog = hist[hist.player_id == pid].sort_values("week")
        if len(glog) < 3:
            continue
        team = latest_team.get(pid)
        if team not in team_game:
            continue
        game_id, opp, is_home = team_game[team]
        pos = glog.position.iloc[-1]
        cons = consensus(book_lines)
        line = cons["line"]
        values = [float(v) for v in glog[col].fillna(0)]
        factor = factors.get((opp, pos if pos in ("QB", "RB", "WR", "TE") else "WR", col), 1.0)
        proj, sd = project(values, factor, discrete)

        z_line = (line + (0.5 if discrete else 0)) - proj   # continuity for counts
        p_over = 1 - norm_cdf(z_line / sd)
        imp_over = american_to_prob(cons["over"]) if cons["over"] is not None else None
        imp_under = american_to_prob(cons["under"]) if cons["under"] is not None else None
        # strip vig for fair implied probability
        fair_over = imp_over / (imp_over + imp_under) if imp_over and imp_under else imp_over
        ev_over = (p_over * (cons["over"] / 100 if cons["over"] > 0 else 100 / -cons["over"]) - (1 - p_over)) if cons["over"] is not None else None
        ev_under = ((1 - p_over) * (cons["under"] / 100 if cons["under"] > 0 else 100 / -cons["under"]) - p_over) if cons["under"] is not None else None

        role = role_change(values, line, discrete)
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
            "proj": r1(proj),
            "sd": r1(sd),
            "edge": r1(proj - line),
            "edge_sd": round((proj - line) / sd, 2),
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
            "games": [
                {"wk": int(r.week), "opp": r.opponent_team, "home": r.team == r.game_id.split("_")[3],
                 "val": float(getattr(r, col)) if not pd.isna(getattr(r, col)) else 0.0}
                for r in glog.itertuples()
            ],
            "snaps": snap_pct.get(pname),
            "injury": inj_by_player.get(pname),
            "team_injuries": team_injuries.get(team, []),
            "opp_injuries": [i for i in team_injuries.get(opp, []) if i["status"] in ("Out", "Doubtful")],
            "headshot": glog.headshot_url.iloc[-1] if isinstance(glog.headshot_url.iloc[-1], str) else None,
        })
    if unmatched:
        log(f"unmatched players from odds feed: {sorted(unmatched)[:20]}{' ...' if len(unmatched) > 20 else ''}")
    props.sort(key=lambda p: (p["role_change"] is not None, -abs(p["edge_sd"])))
    return props


# ----------------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", type=int)
    ap.add_argument("--sample", action="store_true", help="skip Odds API, synthesize lines")
    args = ap.parse_args()

    frames = load_frames()
    stats, games = frames["stats"], frames["games"]
    week = current_week(games, args.week)
    wg = week_games(games, week)
    log(f"season {SEASON} week {week}: {len(wg)} games, stats through week {int(stats.week.max())}")

    api_key = os.environ.get("ODDS_API_KEY", "").strip()
    teams_this_week = {g["home"] for g in wg} | {g["away"] for g in wg}
    players_in_week = set(stats[stats.team.isin(teams_this_week)].player_id)
    if args.sample or not api_key:
        log("no ODDS_API_KEY -> sample lines")
        lines, usage, mode = sample_odds(stats, week, players_in_week), {}, "sample"
    else:
        try:
            lines, usage, mode = *fetch_odds(wg, api_key), "live"
        except Exception as e:  # bad key, quota, outage -> keep the site alive
            log(f"ODDS API FAILED ({e}); falling back to sample lines")
            lines, usage, mode = sample_odds(stats, week, players_in_week), {"error": str(e)[:200]}, "sample"

    props = build_props(stats, games, frames["injuries"], frames["snaps"], week, lines)
    OUT.mkdir(parents=True, exist_ok=True)
    payload = {
        "season": SEASON, "week": week, "mode": mode,
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
        "stats_through_week": int(stats.week.max()),
        "usage": usage,
        "markets": {k: v[1] for k, v in MARKETS.items()},
        "games": wg,
        "props": props,
    }
    (OUT / "props.json").write_text(json.dumps(payload, separators=(",", ":")))
    log(f"wrote {len(props)} props -> {OUT / 'props.json'}")


if __name__ == "__main__":
    main()
