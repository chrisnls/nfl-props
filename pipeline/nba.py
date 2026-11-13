#!/usr/bin/env python3
"""
NBA prop edge pipeline -- same shape as build.py (NFL), one slate (game day) at a time.

1. Player box scores + schedule from sportsdataverse (hoopR / ESPN data, free, updated nightly).
2. Player prop lines for the slate from The-Odds-API (or cached / sample lines).
3. Projection = projected minutes x per-minute rate x opponent factor; σ, P(over), edge, EV.
4. Writes site/data/nba/props.json (same prop schema as the NFL board, so the site reuses it).
5. --grade: grades finished slates + NBA parlays -> site/data/nba/results.json, parlays.json.

Usage:
    ODDS_API_KEY=xxx python pipeline/nba.py               # live lines
    python pipeline/nba.py --lines cached | sample
    python pipeline/nba.py --grade
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import math
import os
import re
import unicodedata
import statistics
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

import build                      # shared helpers (consensus, odds math, logging)
import grade as nfl_grade         # shared grading helpers (record, calibration, clamps)
from build import american_to_prob, consensus, log, norm_cdf, r1

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "pipeline" / "raw" / "nba"
OUT = ROOT / "site" / "data" / "nba"
HISTORY = OUT / "history"                      # YYYY-MM-DD.json snapshots of live builds
RESULTS = OUT / "results.json"
PARLAYS = OUT / "parlays.json"
LINES_CACHE = ROOT / "pipeline" / "nba_lines_cache.json"
BACKTEST_CACHE = ROOT / "pipeline" / "nba_backtest_cache.json"
SEASON = int(os.environ.get("NBA_SEASON", "2027"))   # hoopR labels 2026-27 as 2027
ET = ZoneInfo("America/New_York")

SDV = "https://github.com/sportsdataverse/sportsdataverse-data/releases/download"
SOURCES = {
    "box": f"{SDV}/espn_nba_player_boxscores/player_box_{SEASON}.csv",
    "sched": f"{SDV}/espn_nba_schedules/nba_schedule_{SEASON}.csv",
}
ODDS_BASE = "https://api.the-odds-api.com/v4/sports/basketball_nba"
ESPN_INJURIES = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"
OUT_STATUSES = {"out", "doubtful", "suspension", "suspended", "inactive"}
BOOKS = build.BOOKS

# Odds API market -> (box column(s) summed, label)
MARKETS = {
    "player_points": (["points"], "Points"),
    "player_rebounds": (["rebounds"], "Rebounds"),
    "player_assists": (["assists"], "Assists"),
    "player_threes": (["three_point_field_goals_made"], "Threes"),
    "player_points_rebounds_assists": (["points", "rebounds", "assists"], "PRA"),
}
# σ floor per market (absolute, fraction of mean)
SD_FLOOR = {"player_points": (3.0, 0.25), "player_rebounds": (1.2, 0.3), "player_assists": (1.2, 0.3),
            "player_threes": (0.8, 0.4), "player_points_rebounds_assists": (4.0, 0.2)}
# sample lines: skip players averaging below this (no book would post them)
SAMPLE_MIN = {"player_points": 8, "player_rebounds": 3, "player_assists": 2, "player_threes": 1, "player_points_rebounds_assists": 15}

MIN_GAMES = 3
ROLE_Z, ROLE_Z_OUTSIDE = 2.0, 1.5
ROLE_MIN_PTS, ROLE_MIN_REL, ROLE_MIN_EDGE = 8.0, 0.30, 0.3   # minutes shift: last 2 vs before
# Minutes the BOOK is pricing: points (or PRA) line / the player's per-minute rate. If that's far from
# the model's projected minutes, the book knows something about playing time (restriction lifting,
# returning starter, blowout risk, a teammate out) that the box scores don't show yet. Points/PRA
# only -- low-count stats (threes, assists) are too coarse for this. Flags all of that player's props.
IMPLIED_MIN_HI, IMPLIED_MIN_LO = 1.35, 0.70
# Teammate-out redistribution: an out player's projected minutes and production are shared among the
# remaining rotation, weighted by what each already does (same position x REDIST_SAME_GRP), scaled by how
# often the out player was actually in the lineup lately (if he's missed the last 5, recent stats already
# reflect it), and only partly (REDIST_ALPHA): replacements are less productive than the player they replace.
REDIST_ALPHA = {"player_points": 0.8, "player_rebounds": 0.9, "player_assists": 0.8, "player_threes": 0.8,
                "player_points_rebounds_assists": 0.85}
# OFF: backtest (pipeline/eval_redistribution.py, 15 slates 10/27-11/11) found teammates of an absent player
# are already projected without bias, and every variant tried (full/partial share, fresh absences only,
# same-position backups only) made projections WORSE. Kept for re-testing as the season grows. Instead,
# props get a "teammate out" context tag and are graded separately (do books over-adjust those lines?).
REDIST_ON = False
REDIST_SAME_GRP = 1.5
REDIST_MIN_OUT = 15.0       # only players projected for 15+ minutes count as a real absence
REDIST_MAX_MIN = 10.0       # cap on extra minutes for any one player
REDIST_RECENT = 5           # team's last N games for the "was he in the lineup lately" weight
IMPLIED_MIN_MARKETS = ("player_points", "player_points_rebounds_assists")
CORRECTIONS_MIN_N = build.CORRECTIONS_MIN_N
BACKTEST_MIN_GAMES = 4
GRP_NAME = {"G": "G", "F": "F", "C": "C"}

NAME_ALIASES: dict[str, str] = {
    # sportsbook name -> ESPN name (normalized). Add when the log lists an unmatched player.
}


# ----------------------------------------------------------------------------- helpers
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


def download(name: str, url: str, max_age_hours: float = 3) -> Path:
    RAW.mkdir(parents=True, exist_ok=True)
    path = RAW / f"{name}.csv"
    if path.exists() and (time.time() - path.stat().st_mtime) < max_age_hours * 3600:
        return path
    log(f"nba: downloading {name}")
    r = requests.get(url, timeout=180)
    r.raise_for_status()
    path.write_bytes(r.content)
    return path


def load_frames() -> dict[str, pd.DataFrame]:
    box = pd.read_csv(download("box", SOURCES["box"]), low_memory=False)
    sched = pd.read_csv(download("sched", SOURCES["sched"]), low_memory=False)
    box = box[(box.season_type == 2)].copy()
    box["played"] = (~box.did_not_play.astype(bool)) & (box.minutes.fillna(0) > 0)
    box["date"] = box.game_date.astype(str)
    box["grp"] = box.athlete_position_abbreviation.fillna("F").str[0].map(lambda x: x if x in "GFC" else "F")
    for _, (cols, _l) in MARKETS.items():
        for c in cols:
            box[c] = box[c].fillna(0)
    box["pra"] = box.points + box.rebounds + box.assists
    sched = sched[sched.season_type == 2].copy()
    sched["date"] = sched.game_date.astype(str)
    sched["start"] = pd.to_datetime(sched.start_date, utc=True)
    return {"box": box, "sched": sched}


def stat_of(df_or_row, market: str):
    cols = MARKETS[market][0]
    if len(cols) == 1:
        return df_or_row[cols[0]]
    return df_or_row["pra"]


def current_slate(sched: pd.DataFrame, forced: str | None) -> str:
    if forced:
        return forced
    open_ = sched[~sched.status_type_completed.astype(bool)]
    return str(open_.date.min()) if len(open_) else str(sched.date.max())


def slate_games(sched: pd.DataFrame, date: str) -> list[dict]:
    out = []
    for g in sched[sched.date == date].sort_values("start").itertuples():
        out.append({
            "id": str(g.id), "away": g.away_abbreviation, "home": g.home_abbreviation,
            "kickoff": g.start.tz_convert(ET).strftime("%Y-%m-%dT%H:%M"),   # ET wall clock, like the NFL board
            "start_utc": g.start.isoformat(), "spread": None, "total": None,
            "played": bool(g.status_type_completed),
        })
    return out


def team_name_map(sched: pd.DataFrame) -> dict[str, str]:
    m = {}
    for side in ("home", "away"):
        for full, ab in sched[[f"{side}_display_name", f"{side}_abbreviation"]].drop_duplicates().itertuples(index=False):
            m[full] = ab
    m["LA Clippers"] = m.get("LA Clippers", "LAC")
    m["Los Angeles Clippers"] = "LAC"
    return m


def fetch_injuries(names: dict[str, str]) -> dict:
    """ESPN's public NBA injury report (free, no key). Returns
    {"by_id": {athlete_id: info}, "by_name": {(norm name, team): info}, "by_team": {team: [info...]}}.
    Any failure -> empty report (the board still builds; it just has no injury tags)."""
    empty = {"by_id": {}, "by_name": {}, "by_team": {}, "ok": False}
    try:
        r = requests.get(ESPN_INJURIES, timeout=30, headers={"User-Agent": "prop-edge/1.0"})
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log(f"nba injuries: unavailable ({e})")
        return empty
    rep = {"by_id": {}, "by_name": {}, "by_team": {}, "ok": True}
    for t in data.get("injuries") or []:
        team = names.get(t.get("displayName") or "") or (t.get("abbreviation") or "")
        for i in t.get("injuries") or []:
            a = i.get("athlete") or {}
            name = a.get("displayName") or a.get("fullName")
            if not name:
                continue
            aid = str(a.get("id") or "")
            if not aid:
                for l in a.get("links") or []:
                    m = re.search(r"/id/(\d+)", l.get("href") or "")
                    if m:
                        aid = m.group(1)
                        break
            det = i.get("details") or {}
            status = i.get("status") or ((i.get("type") or {}).get("description") or "").title() or None
            info = {"name": name, "pos": ((a.get("position") or {}).get("abbreviation")), "status": status,
                    "injury": det.get("type") or None, "return": det.get("returnDate"),
                    "note": i.get("shortComment"), "team": team}
            if aid:
                rep["by_id"][aid] = info
            rep["by_name"][(norm_name(name), team)] = info
            rep["by_team"].setdefault(team, []).append(info)
    n = sum(len(v) for v in rep["by_team"].values())
    log(f"nba injuries: {n} players on {len(rep['by_team'])} teams"
        + (f"; e.g. {next(iter(rep['by_team'].values()))[0]}" if n else ""))
    return rep


def label_date(d: str) -> str:
    x = dt.date.fromisoformat(d)
    return f"{x.month}/{x.day}"


# ----------------------------------------------------------------------------- odds
def fetch_odds(games: list[dict], api_key: str, names: dict[str, str]) -> tuple[dict, dict]:
    session = requests.Session()
    events = session.get(f"{ODDS_BASE}/events", params={"apiKey": api_key}, timeout=60)
    events.raise_for_status()
    usage = {"remaining": events.headers.get("x-requests-remaining"), "used": events.headers.get("x-requests-used")}
    now = dt.datetime.now(dt.timezone.utc)
    want = {(g["away"], g["home"]) for g in games if not g["played"] and dt.datetime.fromisoformat(g["start_utc"]) > now}
    evs = [ev for ev in events.json()
           if (names.get(ev["away_team"]), names.get(ev["home_team"])) in want
           and dt.datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00")) > now]
    needed = len(MARKETS) * len(evs)
    remaining = int(usage["remaining"]) if str(usage.get("remaining") or "").isdigit() else None
    if remaining is not None and remaining < needed:
        raise build.CreditsLow(f"{remaining} credits left, slate needs ~{needed}")
    lines: dict[tuple[str, str], list] = {}
    for ev in evs:
        r = session.get(f"{ODDS_BASE}/events/{ev['id']}/odds",
                        params={"apiKey": api_key, "regions": "us", "oddsFormat": "american",
                                "markets": ",".join(MARKETS), "bookmakers": ",".join(BOOKS)}, timeout=60)
        if r.status_code != 200:
            log(f"nba odds {ev['id']} -> {r.status_code} {r.text[:120]}")
            continue
        usage["remaining"] = r.headers.get("x-requests-remaining", usage["remaining"])
        usage["used"] = r.headers.get("x-requests-used", usage["used"])
        for bk in r.json().get("bookmakers", []):
            for m in bk.get("markets", []):
                if m["key"] not in MARKETS:
                    continue
                grouped: dict[str, dict] = {}
                for o in m["outcomes"]:
                    grouped.setdefault(o["description"], {})[o["name"].lower()] = (o.get("point"), o.get("price"))
                for player, sides in grouped.items():
                    over, under = sides.get("over"), sides.get("under")
                    point = (over or under or (None, None))[0]
                    if point is None:
                        continue
                    lines.setdefault((norm_name(player), m["key"]), []).append({
                        "book": bk["key"], "line": float(point), "over": over[1] if over else None,
                        "under": under[1] if under else None, "name": player})
    usage["events"] = len(evs)
    log(f"nba odds: {len(evs)} events, {len(lines)} player-markets, credits remaining {usage['remaining']}")
    return lines, usage


def sample_lines(box: pd.DataFrame, date: str, teams: set[str]) -> dict:
    hist = box[(box.date < date) & box.played]
    lines = {}
    for pid, g in hist[hist.team_abbreviation.isin(teams)].groupby("athlete_id"):
        if len(g) < BACKTEST_MIN_GAMES:
            continue
        g = g.sort_values("date")
        name = g.athlete_display_name.iloc[-1]
        for m in MARKETS:
            avg = float(stat_of(g, m).tail(5).mean())
            if avg < SAMPLE_MIN[m]:
                continue
            lines[(norm_name(name), m)] = [{"book": "sample", "line": math.floor(avg) + 0.5,
                                            "over": -112, "under": -112, "name": name}]
    return lines


def save_cache(lines, date, usage):
    LINES_CACHE.write_text(json.dumps({"season": SEASON, "date": date, "usage": usage,
                                       "fetched": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
                                       "lines": [{"player": k[0], "market": k[1], "books": v} for k, v in lines.items()]},
                                      separators=(",", ":")))


def load_cache(date):
    if not LINES_CACHE.exists():
        return None
    c = json.loads(LINES_CACHE.read_text())
    if c.get("date") != date:
        log(f"nba: cached lines are for {c.get('date')}, not {date}")
        return None
    return {(x["player"], x["market"]): x["books"] for x in c["lines"]}, c.get("usage", {}), c["fetched"]


# ----------------------------------------------------------------------------- model
def wavg(xs: list[float]) -> float:
    """0.45·L5 + 0.30·L10 + 0.25·season, the same recency weighting as the NFL model."""
    return 0.45 * statistics.fmean(xs[-5:]) + 0.30 * statistics.fmean(xs[-10:]) + 0.25 * statistics.fmean(xs)


def defense_tables(hist: pd.DataFrame):
    """Per (team, grp, market): what a defense allows to a position group PER MINUTE that group plays,
    vs the league. Per-minute, not per-game: ESPN's G/F/C labels split minutes very unevenly from team to
    team (a "forwards" group can log 20 or 150 minutes), so per-game totals mostly measure roster labels."""
    played = hist[hist.played]
    factors, per_game_cache = {}, {}
    for m in MARKETS:
        col = "pra" if m == "player_points_rebounds_assists" else MARKETS[m][0][0]
        pg = played.groupby(["opponent_team_abbreviation", "grp", "game_id"]).agg(
            val=(col, "sum"), mins=("minutes", "sum"), date=("date", "first"), off=("team_abbreviation", "first")).reset_index()
        per_game_cache[m] = pg
        for grp, g in pg.groupby("grp"):
            lg = g.val.sum() / g.mins.sum() if g.mins.sum() else 0
            if not lg:
                continue
            for team, tg in g.groupby("opponent_team_abbreviation"):
                if tg.mins.sum() >= 60:   # need some sample
                    factors[(team, grp, m)] = float((tg.val.sum() / tg.mins.sum()) / lg)
    return factors, per_game_cache


def defense_profile(team, grp, m, pg_cache, played, scores, memo):
    key = f"{team}|{grp}|{m}"
    if key in memo:
        return key
    pg = pg_cache[m]
    g = pg[pg.grp == grp]
    by_team = g.groupby("opponent_team_abbreviation").val.mean().sort_values(ascending=False)
    if team not in by_team.index:
        return None
    mine = g[g.opponent_team_abbreviation == team].sort_values("date")
    col = "pra" if m == "player_points_rebounds_assists" else MARKETS[m][0][0]
    games_out = []
    for r in mine.itertuples():
        sub = played[(played.game_id == r.game_id) & (played.grp == grp) & (played.opponent_team_abbreviation == team)]
        top = sub.loc[sub[col].idxmax()] if len(sub) else None
        d_pts, o_pts = scores.get((r.game_id, team), (None, None))
        games_out.append({"wk": label_date(r.date), "vs": r.off, "val": r1(r.val), "vol": int(round(r.mins)),
                          "top": top.athlete_display_name if top is not None else None,
                          "top_val": r1(top[col]) if top is not None else None,
                          "top_vol": int(round(top.minutes)) if top is not None else None,
                          "def_pts": d_pts, "off_pts": o_pts})
    vals = [x["val"] for x in games_out]
    memo[key] = {"team": team, "grp": grp, "col": m, "vol_label": "min",
                 "rank": int(list(by_team.index).index(team)) + 1, "n_teams": int(len(by_team)),
                 "avg": r1(statistics.fmean(vals)), "median": r1(statistics.median(vals)), "l3": r1(statistics.fmean(vals[-3:])),
                 "lg_avg": r1(by_team.mean()), "lg_median": r1(g.val.median()),
                 "per_vol": round(36 * float(mine.val.sum() / mine.mins.sum()), 1) if mine.mins.sum() else None,
                 "lg_per_vol": round(36 * float(g.val.sum() / g.mins.sum()), 1) if g.mins.sum() else None,
                 "vol_avg": r1(mine.mins.mean()), "lg_vol_avg": r1(g.groupby("opponent_team_abbreviation").mins.mean().mean()),
                 "games": games_out}
    return key


def role_change(values, line, mins, edge_sd):
    season = statistics.fmean(values)
    sd = statistics.pstdev(values)
    z = (line - season) / max(sd, 1.0, 0.25 * season)
    outside = line > max(values) or line < min(values)
    base = {"z": round(z, 1), "lo": min(values), "hi": max(values)}
    if abs(z) >= ROLE_Z or (outside and abs(z) >= ROLE_Z_OUTSIDE):
        return {"why": "line", "dir": "up" if z > 0 else "down", **base}
    if len(mins) >= 4:
        recent, prior = statistics.fmean(mins[-2:]), statistics.fmean(mins[:-2])
        contradicts = (recent < prior) == (edge_sd > 0) and abs(edge_sd) >= ROLE_MIN_EDGE
        if prior and contradicts and abs(recent - prior) >= ROLE_MIN_PTS and abs(recent - prior) / prior >= ROLE_MIN_REL:
            return {"why": "usage", "dir": "up" if recent > prior else "down", "metric": "minutes",
                    "snap_prior": round(prior), "snap_recent": round(recent), **base}
    return None


def team_rotation(played, team, date):
    """Projected minutes and per-market production for everyone in a team's recent rotation."""
    tg = played[played.team_abbreviation == team]
    dates = sorted(tg.date.unique())[-REDIST_RECENT:]
    out = {}
    for pid, g in tg[tg.date.isin(sorted(tg.date.unique())[-10:])].groupby("athlete_id"):
        g = played[(played.athlete_id == pid) & (played.team_abbreviation == team)].sort_values("date")
        if len(g) < MIN_GAMES:
            continue
        mins = [float(m) for m in g.minutes]
        pm = wavg(mins)
        if pm < 8:
            continue
        stats = {}
        for m in MARKETS:
            vals = [float(v) for v in stat_of(g, m)]
            stats[m] = pm * wavg([v / x for v, x in zip(vals, mins) if x > 0])
        out[pid] = {"min": pm, "stats": stats, "grp": g.grp.iloc[-1], "name": g.athlete_display_name.iloc[-1],
                    # a FRESH absence only: he played the team's most recent game. If he'd already been
                    # sitting, his teammates' recent numbers already show it (redistributing would double-count).
                    "presence": 1.0 if dates and dates[-1] in set(g.date) else 0.0}
    return out


def redistribution(played, date, teams, outs_by_team):
    """{pid: {"min": +minutes, market: +stat, "outs": [names]}} for players whose teammates are out."""
    boosts: dict = {}
    for team in teams:
        outs = outs_by_team.get(team) or set()
        if not outs:
            continue
        rot = team_rotation(played, team, date)
        real_outs = [o for o in outs if o in rot and rot[o]["min"] >= REDIST_MIN_OUT and rot[o]["presence"] > 0]
        if not real_outs:
            continue
        stay = {pid: r for pid, r in rot.items() if pid not in outs}
        for o in real_outs:
            ro, w = rot[o], rot[o]["presence"]
            aff = {pid: (REDIST_SAME_GRP if r["grp"] == ro["grp"] else 1.0) for pid, r in stay.items()}
            wmin = {pid: r["min"] * aff[pid] for pid, r in stay.items()}
            tot = sum(wmin.values()) or 1
            for pid, r in stay.items():
                b = boosts.setdefault(pid, {"min": 0.0, "outs": [], **{m: 0.0 for m in MARKETS}})
                b["min"] = min(REDIST_MAX_MIN, b["min"] + ro["min"] * w * wmin[pid] / tot)
                b["outs"].append(ro["name"])
                for m in MARKETS:
                    ws = {q: rr["stats"][m] * aff[q] for q, rr in stay.items()}
                    t = sum(ws.values()) or 1
                    b[m] += REDIST_ALPHA[m] * ro["stats"][m] * w * ws[pid] / t
    return boosts


def fresh_outs(played, date, teams, outs_by_team):
    """{team: [names]} of rotation players (15+ projected min) ruled out who played the team's last game."""
    ctx = {}
    for team in teams:
        outs = outs_by_team.get(team) or set()
        if not outs:
            continue
        rot = team_rotation(played, team, date)
        names = [rot[o]["name"] for o in outs if o in rot and rot[o]["min"] >= REDIST_MIN_OUT and rot[o]["presence"] > 0]
        if names:
            ctx[team] = sorted(names)
    return ctx


def outs_from_injuries(injuries, played, teams):
    """Map the injury report's OUT/doubtful players to athlete ids, per team."""
    outs = {}
    names = played.drop_duplicates("athlete_id").set_index("athlete_id")
    for team in teams:
        for x in injuries["by_team"].get(team, []):
            if (x["status"] or "").lower() not in OUT_STATUSES:
                continue
            pid = None
            for aid, info in injuries["by_id"].items():
                if info is x:
                    pid = int(aid) if str(aid).isdigit() else aid
            if pid is None or pid not in names.index:
                cand = names[(names.team_abbreviation == team) &
                             (names.athlete_display_name.map(norm_name) == norm_name(x["name"]))]
                pid = cand.index[0] if len(cand) else None
            if pid is not None:
                outs.setdefault(team, set()).add(pid)
    return outs


def outs_from_box(box, date, teams):
    """Backtest stand-in for the injury report: who actually sat that night (DNP or absent)."""
    day = box[box.date == date]
    played_ids = set(day[day.played].athlete_id)
    outs = {}
    for team in teams:
        recent = box[(box.team_abbreviation == team) & (box.date < date) & box.played]
        last = sorted(recent.date.unique())[-REDIST_RECENT:]
        regulars = set(recent[recent.date.isin(last)].athlete_id)
        outs[team] = {pid for pid in regulars if pid not in played_ids}
    return outs


def build_props(box, sched, date, lines, corrections=None, injuries=None, outs_override=None):
    injuries = injuries or {"by_id": {}, "by_name": {}, "by_team": {}}
    hist = box[box.date < date]
    played = hist[hist.played]
    games = slate_games(sched, date)
    team_game = {}
    for g in games:
        if g["played"]:
            continue
        team_game[g["home"]] = (g["id"], g["away"], True)
        team_game[g["away"]] = (g["id"], g["home"], False)
    factors, pg_cache = defense_tables(hist)
    scores = {}
    for r in hist[["game_id", "team_abbreviation", "team_score", "opponent_team_score"]].drop_duplicates(["game_id", "team_abbreviation"]).itertuples(index=False):
        scores[(r.game_id, r.team_abbreviation)] = (int(r.team_score) if not pd.isna(r.team_score) else None,
                                                    int(r.opponent_team_score) if not pd.isna(r.opponent_team_score) else None)
        # defense view: (game, defending team) -> (def pts, off pts) == that team's own score line
    latest = box.sort_values("date").groupby("athlete_id").agg(team=("team_abbreviation", "last"), grp=("grp", "last"),
                                                              name=("athlete_display_name", "last"), head=("athlete_headshot_href", "last"))
    by_name: dict[str, list] = {}
    for pid, row in latest.iterrows():
        by_name.setdefault(norm_name(row["name"]), []).append(pid)
    gp = played.groupby("athlete_id").size()
    teams_tonight = set(team_game)
    outs = outs_override if outs_override is not None else outs_from_injuries(injuries, played, teams_tonight)
    boosts = redistribution(played, date, teams_tonight, outs) if REDIST_ON else {}
    tm_out = fresh_outs(played, date, teams_tonight, outs)
    memo: dict = {}
    props, unmatched = [], set()
    for (pname, market), book_lines in lines.items():
        pname = norm_name(pname)
        cands = by_name.get(pname) or []
        if not cands:
            unmatched.add(book_lines[0]["name"])
            continue
        pid = max(cands, key=lambda c: (latest.team.get(c) in team_game, int(gp.get(c, 0))))
        team = latest.team.get(pid)
        if team not in team_game:
            continue
        glog = played[played.athlete_id == pid].sort_values("date")
        if len(glog) == 0:
            continue
        thin = len(glog) < MIN_GAMES
        game_id, opp, is_home = team_game[team]
        grp = latest.grp.get(pid)
        cons = consensus(book_lines)
        line = cons["line"]
        values = [float(v) for v in stat_of(glog, market)]
        mins = [float(v) for v in glog.minutes]
        rates = [v / m for v, m in zip(values, mins) if m > 0]
        proj_min = wavg(mins)
        rate = wavg(rates)
        factor = factors.get((opp, grp, market), 1.0)
        adj = 1 + 0.5 * (max(0.8, min(1.2, factor)) - 1)
        boost = boosts.get(pid)
        base = proj_min * rate * adj
        proj = base + (boost[market] * adj if boost else 0.0)
        if boost:
            proj_min = proj_min + boost["min"]
        mean = statistics.fmean(values)
        a, f = SD_FLOOR[market]
        sd = (statistics.pstdev(values) if len(values) > 2 else mean * 0.4)
        sd = max(sd, a, f * mean) * 1.15
        proj_raw, sd_raw = proj, sd
        corr = (corrections or {}).get(market)
        if corr:
            proj, sd = proj * corr["proj_mult"], sd * corr["sd_mult"]
        p_over = 1 - norm_cdf(((line + (0.5 if float(line).is_integer() else 0)) - proj) / sd)   # see build.py
        imp_o = american_to_prob(cons["over"]) if cons["over"] is not None else None
        imp_u = american_to_prob(cons["under"]) if cons["under"] is not None else None
        fair_over = imp_o / (imp_o + imp_u) if imp_o and imp_u else imp_o
        dec = lambda p: (p / 100 if p > 0 else 100 / -p)
        ev_over = p_over * dec(cons["over"]) - (1 - p_over) if cons["over"] is not None else None
        ev_under = (1 - p_over) * dec(cons["under"]) - p_over if cons["under"] is not None else None
        role = role_change(values, line, mins, (proj - line) / sd)
        if thin:
            role = {"why": "thin", "games": len(values), "dir": "up" if line > mean else "down", "z": None,
                    "lo": min(values), "hi": max(values)}
        hits = [v > line for v in values]
        rate_pct = lambda n: round(100 * sum(hits[-n:]) / len(hits[-n:])) if hits[-n:] else None
        ctx = []
        for r in glog.itertuples():
            ctx.append({"wk": label_date(r.date), "opp": r.opponent_team_abbreviation, "home": r.home_away == "home",
                        "val": float(stat_of(pd.Series(r._asdict()), market)), "vol": int(round(r.minutes)),
                        "share": None, "snap": None,
                        "pts": None if pd.isna(r.team_score) else int(r.team_score),
                        "opp_pts": None if pd.isna(r.opponent_team_score) else int(r.opponent_team_score),
                        "mate": None, "starter": bool(r.starter)})
        def_key = defense_profile(opp, grp, market, pg_cache, played, {(k[0], k[1]): v for k, v in scores.items()}, memo)
        props.append({
            "id": f"nba:{pid}:{market}", "player": latest.name.get(pid), "pid": str(pid), "pos": grp, "team": team,
            "opp": opp, "home": is_home, "game": game_id, "market": market, "label": MARKETS[market][1],
            "line": line, "over": cons["over"], "over_book": cons["over_book"], "under": cons["under"],
            "under_book": cons["under_book"], "books": cons["books"], "line_spread": cons["spread"],
            "proj": r1(proj), "sd": r1(sd), "edge": r1(proj - line), "edge_sd": round((proj - line) / sd, 2),
            "proj_raw": r1(proj_raw), "sd_raw": r1(sd_raw), "proj_min": r1(proj_min),
            "p_over": round(p_over, 3), "fair_over": round(fair_over, 3) if fair_over else None,
            "ev_over": round(ev_over, 3) if ev_over is not None else None,
            "ev_under": round(ev_under, 3) if ev_under is not None else None,
            "avg_season": r1(mean), "avg_l5": r1(statistics.fmean(values[-5:])), "avg_l10": r1(statistics.fmean(values[-10:])),
            "median": r1(statistics.median(values)),
            "hit_l5": rate_pct(5), "hit_l10": rate_pct(10), "hit_season": rate_pct(len(hits)),
            "def_factor": round(factor, 2), "def_basis": "per-minute",
            "games": ctx, "vol_label": "min", "defense": def_key, "snaps": None,
            "injury": inj_of(pid, latest.name.get(pid), team, injuries),
            "team_injuries": [x for x in injuries["by_team"].get(team, []) if x["name"] != latest.name.get(pid)],
            "opp_injuries": [x for x in injuries["by_team"].get(opp, []) if (x["status"] or "").lower() in OUT_STATUSES],
            "headshot": latest["head"].get(pid) if isinstance(latest["head"].get(pid), str) else None,
            "role_change": role,
            "tm_out": [n for n in tm_out.get(team, []) if n != latest.name.get(pid)] or None,
            "redistrib": {"outs": sorted(set(boost["outs"])), "add": r1(proj_raw - base), "add_min": r1(boost["min"]),
                          "base": r1(base)} if boost and (proj_raw - base) >= 0.05 else None,
        })
    # listed OUT / doubtful but the book still has a line: almost certainly stale or about to be pulled
    for p in props:
        st = ((p["injury"] or {}).get("status") or "").lower()
        if st in OUT_STATUSES and (not p["role_change"] or p["role_change"]["why"] != "thin"):
            p["role_change"] = {"why": "injury", "dir": "down", "status": p["injury"]["status"],
                                "injury_type": p["injury"].get("injury"), "z": None, "lo": None, "hi": None}
    # implied-minutes check, per player (prefer the points line, else PRA)
    by_pid: dict[str, list] = {}
    for p in props:
        by_pid.setdefault(p["pid"], []).append(p)
    for pid, ps in by_pid.items():
        ref = next((p for m in IMPLIED_MIN_MARKETS for p in ps if p["market"] == m and not p["role_change"]), None)
        if not ref or not ref["proj_min"] or not ref["proj_raw"]:
            continue
        rate = ref["proj_raw"] / ref["proj_min"]
        implied = ref["line"] / rate if rate > 0 else None
        if not implied:
            continue
        ratio = implied / ref["proj_min"]
        if ratio >= IMPLIED_MIN_HI or ratio <= IMPLIED_MIN_LO:
            flag = {"why": "minutes", "dir": "up" if ratio > 1 else "down", "implied_min": round(implied),
                    "proj_min": round(ref["proj_min"]), "ref": MARKETS[ref["market"]][1], "z": None,
                    "lo": None, "hi": None}
            for p in ps:
                if not p["role_change"]:
                    p["role_change"] = flag
    if unmatched:
        log(f"nba: unmatched players from odds feed: {sorted(unmatched)[:20]}")
    props.sort(key=lambda p: (p["role_change"] is not None, -abs(p["edge_sd"])))
    return props, memo, games


# ----------------------------------------------------------------------------- history + grading
SNAP_FIELDS = build.SNAP_FIELDS


def inj_of(pid, name, team, injuries):
    i = injuries["by_id"].get(str(pid)) or injuries["by_name"].get((norm_name(name), team))
    return {"status": i["status"], "injury": i["injury"], "practice": None, "note": i.get("note"),
            "return": i.get("return")} if i else None


def write_snapshot(props, games, date, fetched):
    HISTORY.mkdir(parents=True, exist_ok=True)
    path = HISTORY / f"{date}.json"
    snap = json.loads(path.read_text()) if path.exists() else {"season": SEASON, "date": date, "props": []}
    t = dt.datetime.fromisoformat(fetched)
    start = {g["id"]: dt.datetime.fromisoformat(g["start_utc"]) for g in games}
    by_id = {p["id"]: p for p in snap["props"]}
    added = 0
    for p in props:
        if p["game"] not in start or start[p["game"]] <= t:
            continue
        row = {k: p.get(k) for k in SNAP_FIELDS}
        row["role_change"] = (p["role_change"] or {}).get("why")
        row["tm_out"] = bool(p.get("tm_out"))
        row["fetched"] = fetched
        by_id[p["id"]] = row
        added += 1
    snap["props"] = sorted(by_id.values(), key=lambda r: r["id"])
    snap["updated"] = fetched
    path.write_text(json.dumps(snap, separators=(",", ":")))
    log(f"nba snapshot: {added} pre-game props -> {path.name} ({len(snap['props'])} total)")


class Outcomes:
    def __init__(self, box, sched):
        p = box[box.played]
        self.stats = {(str(r.athlete_id), r.date): r for r in p.itertuples()}
        self.box_games = set(box.game_id.astype(str))
        self.sched = sched

    def date_complete(self, date: str) -> bool:
        g = self.sched[self.sched.date == date]
        return len(g) > 0 and g.status_type_completed.astype(bool).all() and set(g.id.astype(str)) <= self.box_games

    def actual(self, pid: str, date: str, market: str):
        r = self.stats.get((str(pid), date))
        if r is None:
            return None
        return float(r.pra) if market == "player_points_rebounds_assists" else float(getattr(r, MARKETS[market][0][0]))


def backtest_rows(frames, out: Outcomes):
    box, sched = frames["box"], frames["sched"]
    version = hashlib.sha1(Path(__file__).read_bytes()).hexdigest()[:12]
    cache = json.loads(BACKTEST_CACHE.read_text()) if BACKTEST_CACHE.exists() else {}
    if cache.get("model_version") != version or cache.get("season") != SEASON:
        cache = {"season": SEASON, "model_version": version, "dates": {}}
    dates = [d for d in sorted(sched.date.unique()) if out.date_complete(d)]
    for d in dates:
        if d in cache["dates"]:
            continue
        s2 = sched.copy()
        s2.loc[s2.date == d, "status_type_completed"] = False
        teams = set(s2[s2.date == d].home_abbreviation) | set(s2[s2.date == d].away_abbreviation)
        lines = sample_lines(box, d, teams)
        props, _, _ = build_props(box, s2, d, lines) if lines else ([], None, None)
        rows = []
        for p in props:
            a = out.actual(p["pid"], d, p["market"])
            if a is not None:
                rows.append([p["market"], p["proj_raw"], p["sd_raw"], a, (p["role_change"] or {}).get("why")])
        cache["dates"][d] = rows
        log(f"nba backtest: {d} -> {len(rows)} projections")
    BACKTEST_CACHE.write_text(json.dumps(cache, separators=(",", ":")))
    return [{"market": m, "proj_raw": pr, "sd_raw": sd, "actual": a, "role_change": f}
            for d, rs in cache["dates"].items() if d in dates for m, pr, sd, a, f in rs], dates


def grade_snapshots(out: Outcomes):
    rows, graded, pending = [], [], []
    for path in sorted(HISTORY.glob("*.json")):
        snap = json.loads(path.read_text())
        d = snap["date"]
        if not out.date_complete(d):
            pending.append(d)
            continue
        graded.append(d)
        for p in snap["props"]:
            a = out.actual(p["pid"], d, p["market"])
            if a is None:
                continue
            res = "over" if a > p["line"] else "under" if a < p["line"] else "push"
            pick = "over" if (p.get("edge_sd") or 0) > 0 else "under"
            rows.append({**p, "week": d, "actual": a, "result": res, "pick": pick,
                         "pick_result": "push" if res == "push" else "win" if res == pick else "loss"})
    return rows, graded, pending


def corrections(graded, backtest):
    saved = nfl_grade.MARKETS
    nfl_grade.MARKETS = MARKETS          # reuse the same correction math over NBA markets
    try:
        return nfl_grade.corrections(graded, backtest)
    finally:
        nfl_grade.MARKETS = saved


def grade_parlays(out: Outcomes):
    """NBA parlays live in the same sheet; their legs' prop_id starts with 'nba:' and the
    week column holds the slate date as YYYYMMDD."""
    url = os.environ.get("PARLAY_SHEET_CSV_URL", "").strip()
    base = {"updated": nfl_grade.now_iso(), "configured": bool(url), "parlays": [],
            "summary": {"n": 0, "w": 0, "l": 0, "p": 0, "pending": 0, "units": 0.0}}
    if not url:
        return base
    try:
        df = nfl_grade.load_parlay_sheet(url)
    except Exception as e:
        return {**base, "error": str(e)[:200]}
    parlays, s = [], base["summary"]
    for row in df.to_dict("records"):
        try:
            legs = json.loads(row.get("legs_json") or "[]")
        except json.JSONDecodeError:
            continue
        if not legs or not all(str(l.get("prop_id", "")).startswith("nba:") for l in legs):
            continue
        wk = str(row.get("week") or "").split(".")[0]
        date = f"{wk[:4]}-{wk[4:6]}-{wk[6:8]}" if len(wk) == 8 else None
        graded = []
        for l in legs:
            g = dict(l)
            _, pid, market = str(l["prop_id"]).split(":", 2)
            a = out.actual(pid, date, market) if date and market in MARKETS else None
            if a is None:
                g["status"] = "void" if date and out.date_complete(date) else "pending"
            else:
                g["actual"] = a
                side = str(l.get("side", "")).lower()
                res = "over" if a > float(l["line"]) else "under" if a < float(l["line"]) else "push"
                g["status"] = "push" if res == "push" else "hit" if res == side else "miss"
            graded.append(g)
        st = [l["status"] for l in graded]
        book = float(row["book_odds"]) if row.get("book_odds") else None
        if "miss" in st:
            status, units = "lost", -1.0
        elif "pending" in st:
            status, units = "pending", None
        else:
            live = [l for l in graded if l["status"] == "hit"]
            if not live:
                status, units = "push", 0.0
            elif len(live) == len(graded) and book is not None:
                status, units = "won", nfl_grade.american_to_decimal(book) - 1
            else:
                decs = [nfl_grade.american_to_decimal(l.get("price")) for l in live]
                status, units = "won", (math.prod(decs) - 1 if all(decs) else None)
        parlays.append({"id": row.get("id"), "created": row.get("created"), "week": label_date(date) if date else None,
                        "book_odds": book, "model_prob": float(row["model_prob"]) if row.get("model_prob") else None,
                        "legs": graded, "status": status, "units": round(units, 2) if units is not None else None})
        s["n"] += 1
        key = {"won": "w", "lost": "l", "push": "p"}.get(status, "pending")
        s[key] += 1
        s["units"] = round(s["units"] + (units or 0), 2)
    parlays.sort(key=lambda x: x.get("created") or "", reverse=True)
    return {**base, "parlays": parlays, "summary": s}


def run_grade(frames):
    out = Outcomes(frames["box"], frames["sched"])
    OUT.mkdir(parents=True, exist_ok=True)
    rows, graded, pending = grade_snapshots(out)
    bt, bt_dates = backtest_rows(frames, out)
    corr = corrections(rows, bt)
    unflagged = [r for r in rows if not r.get("role_change")]
    flagged = [r for r in rows if r.get("role_change")]
    rec, cal = nfl_grade.record, nfl_grade.calibration
    bucket = lambda rs, lo, hi: [r for r in rs if lo <= abs(r.get("edge_sd") or 0) < hi]
    results = {
        "sport": "nba", "season": SEASON, "updated": nfl_grade.now_iso(), "period": "day",
        "graded_weeks": len(graded), "graded_week_list": graded, "pending_weeks": pending,
        "corrections_min_n": CORRECTIONS_MIN_N, "corrections_active": any(c.get("active") for c in corr.values()),
        "backtest_weeks": bt_dates, "backtest_label": f"{label_date(bt_dates[0])}–{label_date(bt_dates[-1])}" if bt_dates else "",
        "note": "Record = the side the model leans (projection vs line), 1u flat at -110. Main tables use unflagged props; role-change and thin-history props are graded separately.",
        "overall": rec(unflagged),
        "by_market": {MARKETS[m][1]: rec([r for r in unflagged if r["market"] == m]) for m in MARKETS},
        "by_edge": [{"bucket": b, **rec(bucket(unflagged, lo, hi))} for b, lo, hi in nfl_grade.EDGE_BUCKETS],
        "flagged_vs_unflagged": {"unflagged": rec(unflagged), "flagged": rec(flagged),
                                 "flagged_line": rec([r for r in flagged if r["role_change"] == "line"]),
                                 "flagged_usage": rec([r for r in flagged if r["role_change"] == "usage"]),
                                 "flagged_minutes": rec([r for r in flagged if r["role_change"] == "minutes"]),
                                 "flagged_injury": rec([r for r in flagged if r["role_change"] == "injury"]),
                                 "flagged_thin": rec([r for r in flagged if r["role_change"] == "thin"])},
        "teammate_out": {"with": rec([r for r in unflagged if r.get("tm_out")]),
                         "without": rec([r for r in unflagged if not r.get("tm_out")])},
        "calibration": cal(unflagged),
        "by_week": [{"week": d, "label": label_date(d), **rec([r for r in unflagged if r["week"] == d])} for d in graded],
        "corrections": corr,
        "blend": nfl_grade.fit_blend(rows),
    }
    RESULTS.write_text(json.dumps(results, separators=(",", ":")))
    log(f"nba results: {len(graded)} graded slates, pending {pending}, backtest {len(bt)} projections, "
        f"active corrections {[m for m, c in corr.items() if c.get('active')]}")
    PARLAYS.write_text(json.dumps(grade_parlays(out), separators=(",", ":")))


def load_corrections():
    if not RESULTS.exists():
        return {}, {"active": False}
    r = json.loads(RESULTS.read_text())
    corr = {m: {"proj_mult": c["proj_mult"], "sd_mult": c["sd_mult"]}
            for m, c in (r.get("corrections") or {}).items() if c and c.get("active")}
    return corr, ({"active": True, "by_market": corr, "from": r.get("updated")} if corr else {"active": False})


# ----------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="slate date YYYY-MM-DD (default: next unfinished)")
    ap.add_argument("--lines", choices=["live", "cached", "sample"], default="live")
    ap.add_argument("--grade", action="store_true")
    args = ap.parse_args()
    frames = load_frames()
    if args.grade:
        run_grade(frames)
        return
    box, sched = frames["box"], frames["sched"]
    date = current_slate(sched, args.date)
    games = slate_games(sched, date)
    log(f"nba slate {date}: {len(games)} games, box scores through {box.date.max()}")
    teams = {g["home"] for g in games} | {g["away"] for g in games}
    api_key = os.environ.get("ODDS_API_KEY", "").strip()
    want = args.lines if api_key or args.lines != "live" else "cached"
    lines, usage, err, fetched, mode = None, {}, {}, None, want
    if want == "live":
        try:
            lines, usage = fetch_odds(games, api_key, team_name_map(sched))
            fetched = dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")
            save_cache(lines, date, usage)
            mode = "live"
        except Exception as e:
            log(f"nba odds: {e}; falling back to cached lines")
            err, want = {"error": str(e)[:200]}, "cached"
    if want == "cached" and lines is None:
        c = load_cache(date)
        if c:
            lines, usage, fetched = c
            usage, mode = {**usage, **err}, "cached"
        elif api_key and not err:
            # no lines saved for THIS slate yet (e.g. the first build of a new day): a real pull beats sample lines
            log("nba: no cached lines for this slate -> pulling live instead of using sample lines")
            try:
                lines, usage = fetch_odds(games, api_key, team_name_map(sched))
                fetched = dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")
                save_cache(lines, date, usage)
                mode = "live"
            except Exception as e:
                err = {"error": str(e)[:200]}
    sample_reason = None
    if lines is None:
        lines, usage, mode = sample_lines(box, date, teams), err, "sample"
        sample_reason = "no_key" if not api_key else "api_error" if err else "no_lines"
    corr, corr_info = load_corrections()
    injuries = fetch_injuries(team_name_map(sched))
    props, defense, games = build_props(box, sched, date, lines, corr, injuries)
    OUT.mkdir(parents=True, exist_ok=True)
    x = dt.date.fromisoformat(date)
    payload = {
        "sport": "nba", "season": SEASON, "week": x.strftime("%a %b ") + str(x.day), "slate": date,
        "slate_num": int(date.replace("-", "")), "mode": mode, "sample_reason": sample_reason,
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"), "lines_fetched": fetched,
        "stats_through_week": label_date(str(box.date.max())), "usage": usage,
        "markets": {k: v[1] for k, v in MARKETS.items()}, "games": games, "props": props,
        "defense": defense, "corrections": corr_info, "injuries_ok": injuries.get("ok", False),
    }
    (OUT / "props.json").write_text(json.dumps(payload, separators=(",", ":")))
    log(f"nba: wrote {len(props)} props -> {OUT / 'props.json'}")
    if mode == "live":
        write_snapshot(props, games, date, fetched)


if __name__ == "__main__":
    main()
