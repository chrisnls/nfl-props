#!/usr/bin/env python3
"""One-off: shape of ESPN's public news feeds (league + team + athlete), read-only."""
import json
from pathlib import Path

import requests

OUT = Path(__file__).resolve().parent / "kalshi_probe" / "news.txt"
lines = []


def say(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    lines.append(s)


URLS = [
    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/news?limit=50",
    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/news?team=11&limit=20",
    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/news?team=ind&limit=20",
    "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/news?limit=50",
    "https://site.api.espn.com/apis/fantasy/v2/games/ffl/news/players?limit=50",
    "https://site.api.espn.com/apis/common/v3/sports/football/nfl/athletes/4431611/overview",
    "https://site.api.espn.com/apis/fantasy/v2/games/ffl/news/players?playerId=4431611&limit=10",
    "https://site.web.api.espn.com/apis/common/v3/sports/football/nfl/athletes/4431611/news?limit=10",
]
for u in URLS:
    try:
        r = requests.get(u, timeout=30)
        say(f"\n== {r.status_code} {u}")
        if r.status_code != 200:
            say(r.text[:200]); continue
        j = r.json()
        say("top keys:", list(j.keys())[:15])
        arts = j.get("articles") or j.get("feed") or j.get("news", {}).get("articles") if isinstance(j.get("news"), dict) else j.get("articles") or j.get("feed") or []
        if isinstance(j.get("news"), list):
            arts = j["news"]
        say("n articles:", len(arts or []))
        for a in (arts or [])[:3]:
            say(json.dumps({k: a.get(k) for k in ("headline", "description", "published", "lastModified", "type", "story")} | {
                "links": (a.get("links") or {}).get("web"), "categories": [
                    {k: c.get(k) for k in ("type", "athleteId", "teamId", "description", "athlete")} for c in (a.get("categories") or [])][:6]},
                default=str)[:1500])
        if not arts:
            say(json.dumps(j, default=str)[:1500])
    except Exception as e:
        say(f"ERR {u} {e}")
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text("\n".join(lines) + "\n")
