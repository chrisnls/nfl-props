# Prop Edge — NFL · Handoff

**Live site:** <https://chrisnls.github.io/nfl-props/>
**Repo:** `chrisnls/nfl-props` (public; GitHub Pages deploys from the "Build props" workflow)

A personal NFL player-prop board. It costs nothing to run: nflverse stats, The-Odds-API free tier, GitHub Actions and GitHub Pages. `pipeline/build.py` writes `site/data/props.json`, and `site/index.html` is a single static page that reads it.

## How the model works

For each player × market that has a line this week:

1. **Projection.** A recency-weighted average of the game log: `0.45·L5 + 0.30·L10 + 0.25·season`. That is multiplied by a **defense factor**: how much the opponent allows to that position for that stat, relative to league average. The factor is half-weighted and clamped to ±20%, so the projection moves by at most ±10%.
2. **σ.** The player's game-to-game standard deviation, with a floor (counts: max(1.2, 35% of avg); yards: max(8, 35% of avg)), then inflated 15% for small samples and model error.
3. **P(over).** A normal curve from projection and σ, with a half-point continuity correction on count markets (TDs, attempts, receptions).
4. **Consensus line.** The median line across books (DK, FD, MGM, CZR, ESPN, Fanatics).
5. **EV.** Taken at the **best available price** at the consensus line. The board also shows the book's no-vig implied probability next to the model's for comparison.
6. **Edge.** `projection − line`. The board shows it in plain words ("1.6 under the line") plus a strength label from `edge ÷ σ`, the gap measured against the player's normal game-to-game swing: Strong ≥ 1×, Solid ≥ 0.6×, Lean ≥ 0.3×, Thin below. Hover shows the exact ratio. The board sorts by that ratio by default.
7. **Role-change flag.** If the line is ≥2 historical σ from the season average, or ≥1.5σ and outside every game in the log, the prop gets a *Role ↑/↓* tag and drops to a separate section at the bottom of the board.
8. **Hit rates.** The share of L5 / L10 / season games that cleared the *current* line.
9. **Parlay slip.** Multiplies leg probabilities as if they were independent, and warns when legs share a game.

## Schedule and credit budget

`.github/workflows/build.yml` ("Build props"); all times Central:

| When | What |
|---|---|
| Thu 9am | lines + stats (TNF and early-week lines) |
| Sat 8am | lines + stats (after injury reports) |
| Sun 7am | lines + stats (final Sunday lines) |
| Tue 9am | stats only, cached lines, **no credits** |

A full-slate pull of 6 markets costs **~85 credits**. Three pulls a week is ~260/month, well inside the **500/month** free tier. The site header shows credits remaining after each build. **Free updates:** Actions → Build props → Run workflow → lines = **cached** (the default) re-runs everything against this week's last pull, using **0 credits**. Each live pull is saved to `pipeline/lines_cache.json` for this purpose. **Credit guard:** a live pull first checks credits remaining (the events list call is free); if they can't cover the slate, it uses cached lines instead of half-pulling. If the Odds API fails or there's no cache for the week, the build falls back to sample lines so the site stays up, and the site shows a banner.

`ODDS_API_KEY` is stored as a repo secret and never appears in code or output.

## Changes this session (2026-11-08)

- **σ-edge on the board.** Each row now reads `proj (edge · ±Nσ)`, not just the raw edge. The pipeline writes `edge_sd` per prop; the front-end falls back to `edge / sd` for older data files.
- **Role-change flag + demotion.** New `role_change()` in `pipeline/build.py`, tuned by `ROLE_Z = 2.0` and `ROLE_Z_OUTSIDE = 1.5`. Measures how far the line is from the season average in units of the player's own raw σ (floor: counts 1.0 / 25% of avg; yards 6.0 / 25% of avg). Flagged props:
  - carry `role_change: {dir, z, lo, hi}` in `props.json`;
  - sort last in the pipeline and in every board sort except alphabetical;
  - render dimmed, with a *Role ↑/↓* tag, in their own "Possible role change" section that always shows (it isn't cut by the 300-row cap);
  - get an explanation note in the detail drawer.
- Tested against the week-9 live data: 5 of 472 props flagged. Devin Singletary rush att 15.5 vs 4.5 avg, and his rush yds; Alvin Kamara rush att and rush yds; Breece Hall rush att 10.5 vs 17 avg. Before this change Singletary's rush-attempts prop was #1 on the board.
- README "How the numbers work" updated.
- Follow-up: replaced the `(−1.6 · −1.1σ)` notation on the board with plain language: "1.6 under the line" + **Strong / Solid / Lean / Thin** label (tooltip has the details). Detail drawer says "Strong · 1.1× normal swing".

### Follow-up: role shifts, "why" analysis, free updates

- **Role-change flag, snap-share trigger.** Also flags a prop when snap share moved ≥20 points (and ≥40%) over the last 2 games *and* that contradicts the model's lean, i.e. |edge ÷ σ| ≥ 0.3. Prompted by J.K. Dobbins, week 9: his snaps fell from 44% to 22% as rookie Jonah Coleman took over, the book's line dropped to 24.5, and the model still said 41.5 (over). The line-based rule missed him because rushing yards are noisy. Week 9 result: 31 flags (7 line, 24 snap share).
- **Pipeline:** each game in a player's log now carries volume (car/tgt/att), team share, snap %, the final score and the teammate with the most volume. A new top-level `defense` map in `props.json` holds opponent profiles: rank, average, median, league comparisons, per-carry/target, last 3, and game-by-game with the top player and score.
- **Detail panel:** "Why the last 2 games went over/under" and "<team> defense vs <position>" sections; the game log table gained volume, snap % and score columns.
- **Credit-free runs:** `--lines cached` plus the workflow input (default cached), the credit guard, and the header now says when the lines were pulled and whether they were reused.

## Open ideas

- **Results / CLV tracker.** Store each build's lines and model probabilities, grade them against box scores, and track closing-line value week over week. This is the only way to know whether the model has an edge.
- **Usage-trend weighting.** Weight by target share, carry share and snap % trends, not just raw output.
- **Median-based matchup factor.** The defense factor uses the per-game average, which a couple of blowouts can skew (CAR vs RBs: 124% of league on the average, 98% on the median). Consider a median-based or trimmed-mean factor.
- **Pace and game-total adjustments.** Scale volume by the implied team total and the spread (already in the schedule data).
- **Teammate-out redistribution.** When a starter is Out, redistribute their volume to teammates. This is the proper fix for what the role-change flag currently only detects.
- **NBA / MLB / NHL modules.** The same pipeline shape with sport-specific markets, stat sources and projection rules.
