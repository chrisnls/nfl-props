#!/usr/bin/env python3
"""
Recent player news from ESPN's public fantasy news feed (Rotowire blurbs + ESPN stories), shown in the player
drawer so the reason behind a moved line ("named the starting slot receiver", "minutes limit lifted") is one
click away. Read-only, no key. The model does NOT use it. Never raises; a failed fetch just means no news.
"""
from __future__ import annotations

import datetime as dt
import html
import re
from concurrent.futures import ThreadPoolExecutor

import requests

FEED = "https://site.api.espn.com/apis/fantasy/v2/games/{game}/news/players"
GAME = {"nfl": "ffl", "nba": "fba"}
MAX_ITEMS = 3
MAX_AGE_DAYS = 10
WORKERS = 8


def _clean(s, n):
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1].rsplit(" ", 1)[0] + "…"


def fetch_one(sport: str, espn_id, session) -> list[dict]:
    r = session.get(FEED.format(game=GAME[sport]), params={"playerId": espn_id, "limit": 6}, timeout=20)
    if r.status_code != 200:
        return []
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=MAX_AGE_DAYS)
    out = []
    for a in r.json().get("feed") or []:
        try:
            when = dt.datetime.fromisoformat(str(a.get("published", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if when < cutoff:
            continue
        rot = a.get("type") == "Rotowire"
        link = (a.get("links") or {}).get("web", {}).get("href") or (a.get("links") or {}).get("href")
        out.append({"when": when.isoformat(timespec="minutes"), "headline": _clean(a.get("headline"), 260),
                    "story": _clean(a.get("story"), 420) if rot else None, "src": "Rotowire" if rot else "ESPN",
                    "url": None if rot else link})
        if len(out) >= MAX_ITEMS:
            break
    return out


def attach(props: list[dict], sport: str, espn_of) -> dict:
    """espn_of(prop) -> ESPN athlete id or None. Adds p['news'] (shared per player)."""
    try:
        ids = {}
        for p in props:
            e = espn_of(p)
            if e not in (None, "") and str(e) != "nan":
                ids.setdefault(str(int(float(e))), []).append(p)
        session = requests.Session()
        with ThreadPoolExecutor(WORKERS) as ex:
            res = dict(zip(ids, ex.map(lambda e: _safe(sport, e, session), ids)))
        n = 0
        for e, ps in ids.items():
            for p in ps:
                p["news"] = res.get(e) or None
            n += bool(res.get(e))
        return {"ok": True, "players": len(ids), "with_news": n}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def _safe(sport, e, session):
    try:
        return fetch_one(sport, e, session)
    except Exception:
        return []
