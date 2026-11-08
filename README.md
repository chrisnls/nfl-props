# Prop Edge — NFL

Personal NFL player-prop analysis app. Zero monthly cost:

| Piece | Source | Cost |
|---|---|---|
| Player stats, schedule, injuries, snap counts | [nflverse](https://github.com/nflverse/nflverse-data) releases (updated nightly during the season) | free |
| Prop lines (pass yds/TDs, rush yds/att, rec yds, receptions) | [The-Odds-API](https://the-odds-api.com) free tier | free, 500 credits/mo |
| Scheduled builds | GitHub Actions | free |
| Hosting | GitHub Pages | free |

Each build pulls stats + lines, computes projections, hit rates, probabilities and edges, and writes `site/data/props.json`. The site is a single static page that reads that file.

## One-time setup (~10 minutes)

1. **Get an Odds API key** — sign up at <https://the-odds-api.com> (free plan).
2. **Create a GitHub repo** and push this folder:
   ```bash
   cd nfl-props
   git init && git add -A && git commit -m "init"
   gh repo create nfl-props --private --source . --push     # or create on github.com and push
   ```
3. **Add the key as a secret** — repo → Settings → Secrets and variables → Actions → New repository secret → name `ODDS_API_KEY`.
4. **Make the repo public** — GitHub Pages is only free on public repos. (The workflow enables Pages on its own.)
5. **Run it once** — Actions tab → "Build props" → Run workflow. In about a minute the site is live at `https://<your-user>.github.io/nfl-props/`.

Nothing secret lives in the repo — the Odds API key is a GitHub secret and never appears in code or output.

## Schedule and credit budget

The workflow runs Thu 9am, Sat 8am, Sun 7am Central (live lines + stats) and Tue 9am (stats only, cached lines — no credits). A full-slate pull of 6 markets across ~14 games costs roughly **80–90 credits** (6 per game), so three pulls a week is ~260/month, well inside the 500 free. The header shows credits remaining after each build. To spend fewer: drop a market in `MARKETS`, or remove the Thursday run.

**Updating for free.** Every live pull saves its lines to `pipeline/lines_cache.json`. Actions → Build props → Run workflow has a **lines** choice that defaults to **cached**: it re-runs the whole model with fresh stats, injuries and snap counts against this week's last pulled lines and spends **zero credits**. Pick **live** only when you want new lines. Cached lines are only reused within the same week; if there are none, the build uses sample lines.

**Credit guard.** Before a live pull, the build checks credits remaining (the Odds API's events list is free). If there isn't enough for the full slate, it skips the pull and uses cached lines instead of half-pulling.

## Running locally

```bash
pip install -r pipeline/requirements.txt
ODDS_API_KEY=xxx python pipeline/build.py        # live lines (saves pipeline/lines_cache.json)
python pipeline/build.py --lines cached          # reuse this week's last pull, no credits
python pipeline/build.py --lines sample          # no key: lines set at recent averages
cd site && python -m http.server 8000            # open http://localhost:8000
```

## How the numbers work

- **Projection** = 0.45·L5 avg + 0.30·L10 avg + 0.25·season avg, then nudged (half-weight, clamped ±20%) by how the opponent's defense has done vs that position this season.
- **σ** = the player's game-to-game standard deviation with a floor, inflated 15% for model error. **P(over)** is a normal-curve estimate from projection and σ (with a half-point continuity correction on count markets).
- **Edge** = projection − consensus line (median across books). The board says it in words ("1.6 under the line") with a strength label based on edge ÷ σ, i.e. the gap measured against the player's normal game-to-game swing: **Strong** ≥ 1×, **Solid** ≥ 0.6×, **Lean** ≥ 0.3×, **Thin** below (hover for the exact number). The default sort is by that ratio, so a 15-yard edge on a volatile receiver doesn't outrank a 2-catch edge on a steady one.
- **Role change** — a line ≥2 historical σ from the player's season average (or ≥1.5σ and outside every game in the log) gets a *Role ↑/↓* tag and is demoted to a separate section at the bottom of the board. Books only move that far on news (starter out, depth-chart change) the game log can't see yet, so those "edges" are usually the model being stale. Also flagged: a snap-share swing of 20+ points (and 40%+) over the last 2 games that contradicts the model's lean, e.g. snaps down while the model says over. Thresholds: `ROLE_*` in `build.py`.
- **EV** uses the best available price at that line, after stripping the vig from the book's implied probability for comparison.
- **Detail panel analysis.** *Why the last 2 games went over/under* breaks the result into volume (carries/targets/attempts), snap share, efficiency (yards per carry, etc.), which teammate took work, and game script, then names the main driver. The *defense deep dive* shows the opponent's rank, average vs median (and which blowouts skew the average), per-carry/target efficiency, the last 3 games, the spread/total for this game, a game-by-game table, and how much the model's matchup adjustment moved the projection.
- **Hit rates** are the share of games this season where the player cleared the *current* line.
- The **parlay slip** multiplies leg probabilities as if independent. Same-game legs are correlated; the slip warns when you do that.

This is a v1 model — deliberately simple and transparent. Good next steps: usage/target-share trend weighting, pace and game-total adjustments, teammate-out redistribution, and a results tracker to grade the model's closing-line value week over week.

## Layout

```
pipeline/build.py        data pull + model → site/data/props.json
pipeline/requirements.txt
site/index.html          the app (no build step, no framework)
site/data/props.json     generated; committed by the workflow
.github/workflows/build.yml
docs/HANDOFF.md          project handoff: model, schedule, change log, open ideas
```
