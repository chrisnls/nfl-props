# Prop Edge — NFL + NBA

Personal NFL player-prop analysis app. Zero monthly cost:

| Piece | Source | Cost |
|---|---|---|
| Player stats, schedule, injuries, snap counts | [nflverse](https://github.com/nflverse/nflverse-data) releases (updated nightly during the season) | free |
| Prop lines (pass yds/TDs/attempts, anytime TD, rush yds/att, rec yds, receptions) | [The-Odds-API](https://the-odds-api.com) free tier | free, 500 credits/mo |
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

Live lines are pulled (Central time) **Wed 9:07am, Thu 9:07am, Thu 5:37pm (TNF), Sat 8:07am, Sun 7:07am / 10:07am / 11:17am (after noon inactives) / 2:37pm (after late inactives) / 6:37pm (SNF), and Mon 5:37pm (MNF)**. Tue 9:07am is stats + grading only (cached lines, no credits). A full-slate pull of 8 markets costs about **8 credits per game (~112)**, and live pulls skip games that have already kicked off, so later pulls cost less. That's about **690 credits a week (~3,000/month)**, comfortably inside the 20K paid plan. The header shows credits remaining after each build. Every pre-game pull also updates the week's grading snapshot, so props are graded against the last line before kickoff. To spend less, delete cron lines in `.github/workflows/build.yml`.

**Updating for free.** Every live pull saves its lines to `pipeline/lines_cache.json`. Actions → Build props → Run workflow has a **lines** choice that defaults to **cached**: it re-runs the whole model with fresh stats, injuries and snap counts against this week's last pulled lines and spends **zero credits**. Pick **live** only when you want new lines. Cached lines are only reused within the same week; if there are none, the build uses sample lines.

**Credit guard.** Before a live pull, the build checks credits remaining (the Odds API's events list is free). If there isn't enough for the full slate, it skips the pull and uses cached lines instead of half-pulling.

## Running locally

```bash
pip install -r pipeline/requirements.txt
ODDS_API_KEY=xxx python pipeline/build.py        # live lines (saves pipeline/lines_cache.json)
python pipeline/build.py --lines cached          # reuse this week's last pull, no credits
python pipeline/build.py --lines sample          # no key: lines set at recent averages
python pipeline/build.py --grade                 # grade finished weeks + saved parlays (no odds calls)
cd site && python -m http.server 8000            # open http://localhost:8000
```

## How the numbers work

- **Projection** = 0.45·L5 avg + 0.30·L10 avg + 0.25·season avg, then nudged (half-weight, clamped ±20%) by how the opponent's defense has done vs that position this season — measured on its *typical game* (median allowed vs the league's median), so one or two blowouts can't make a defense look soft. Pass TDs use the average (too few per game for a median).
- **σ** = the player's game-to-game standard deviation with a floor, inflated 15% for model error. **P(over)** is a normal-curve estimate from projection and σ (with a half-point continuity correction on count markets).
- **Edge** = projection − consensus line (median across books). The board says it in words ("1.6 under the line") with a strength label based on edge ÷ σ, i.e. the gap measured against the player's normal game-to-game swing: **Strong** ≥ 1×, **Solid** ≥ 0.6×, **Lean** ≥ 0.3×, **Thin** below (hover for the exact number). The default sort is by that ratio, so a 15-yard edge on a volatile receiver doesn't outrank a 2-catch edge on a steady one.
- **Role change** — a line ≥2 historical σ from the player's season average (or ≥1.5σ and outside every game in the log) gets a *Role ↑/↓* tag and is demoted to a separate section at the bottom of the board. Books only move that far on news (starter out, depth-chart change) the game log can't see yet, so those "edges" are usually the model being stale. Also flagged: a snap-share swing of 20+ points (and 40%+) over the last 2 games that contradicts the model's lean, e.g. snaps down while the model says over. Thresholds: `ROLE_*` in `build.py`.
- **EV** uses the best available price at that line, after stripping the vig from the book's implied probability for comparison.
- **Detail panel analysis.** *Why the last 2 games went over/under* breaks the result into volume (carries/targets/attempts), snap share, efficiency (yards per carry, etc.), which teammate took work, and game script, then names the main driver. The *defense deep dive* shows the opponent's rank, average vs median (and which blowouts skew the average), per-carry/target efficiency, the last 3 games, the spread/total for this game, a game-by-game table, and how much the model's matchup adjustment moved the projection.
- **Anytime TD** (yes/no, treated as over/under 0.5 TDs). Expected TDs λ = a blend of usage-based xTD (projected carries and targets × the league's TD rate per carry and per target at his position) and his own TD rate, shrunk by games played (n/(n+8)). That's scaled by his team's implied points tonight (spread + total), then recalibrated λ′ = 0.12 + 0.75·λ, fitted on the weeks 5–9 backtest because low-usage players score more often than their touches suggest. P(scores) = 1 − e^(−λ′). Strength is the gap vs the book's no-vig probability (Lean/Solid/Strong = 3/6/10 points); if a book posts only a Yes price, the comparison includes the vig. Backtest calibration: predicted 18/25/35/45/56% → actual 19/25/32/49/58%. Tight ends use their own baseline (0.02): with the shared one they were overpredicted (32.6% vs 25.9% actual), and the TE-only fit held up out of sample. Players under 4 touches a game are outside the tested range and aren't rated. Books post only Yes prices, so the book's view is the **median** Yes price across books (the best price is used for EV), and a model lean to No is shown as *No bet*.
- **Hasn't played lately** (all NFL markets): a player who missed his team's last 2 games has a stale game log, so his props are flagged and demoted.
- **Strength** labels (Strong ≥1σ, Solid ≥0.6σ, Lean ≥0.3σ, Thin below) can be sorted (*Strong over → Strong under* or the reverse, strongest first within each bucket) and filtered (a single rating, or Strong + Solid on either or both sides). The board remembers your sort and filters.
- **Hit rates** are the share of games this season where the player cleared the *current* line.
- The **parlay slip** multiplies leg probabilities as if they were independent, and warns about same-game legs. It also **flags legs that work against each other**, using each player's game log:
  - *These legs compete* (red): overs on two players who share a job (same team + position), with how often both overs actually hit in their games together vs what independence would predict.
  - *Inflated role* (red): a player's last game came with a position-mate out who is playing tonight (e.g. Singletary's 93%-snap week without Skattebo).
  - *Backup over* / *Part-time role* (amber): an over on a clear backup (NFL <45% of snaps vs a 55%+ starter; NBA <22 min vs 26+), or on a part-timer when the starter isn't on the board.
  Saving a slip with a red warning asks you to confirm first. Rebuild a slip here before placing it elsewhere to check it.

This is a v1 model — deliberately simple and transparent. Good next steps: usage/target-share trend weighting, pace and game-total adjustments, teammate-out redistribution, and a results tracker to grade the model's closing-line value week over week.

## Grading loop (Results tab)

1. **Snapshots.** Every *live* build merges its props into `site/data/history/week_NN.json`: line, prices, projection, σ, P(over), edge, role-change flag and game. Only props whose game hasn't kicked off when the lines were pulled are kept, so in-game lines are never graded. A later pre-game pull replaces an earlier one, and live pulls skip games that have already started.
2. **Grading.** `build.py --grade` runs at the start of every workflow run (it's free). Once every game in a snapshot's week is final *and* has nflverse stat lines, it grades each prop: actual stat, then over/under/push. Players who didn't play are voided. It writes `site/data/results.json` with:
   - record and ROI of the model's side (the side the projection leans), 1u flat at -110, overall and by market;
   - record by edge strength (|edge ÷ σ| < 0.5, 0.5–1, > 1);
   - calibration by P(over) decile (predicted vs actual over rate) and Brier score per market;
   - role-flagged vs unflagged props graded separately. The main tables use unflagged props, which is what the board ranks.
3. **Corrections.** Per market, a **projection ×** (total actual ÷ total projected) and a **σ ×** (RMS of the standardized misses). These don't need betting lines, so they're learned from a **backtest** as well as graded props. The backtest replays the model on every finished week from week 5 on, using only earlier games, for every player who'd plausibly have a prop. It's cached in `pipeline/backtest_cache.json` and recomputed whenever `build.py` changes. Corrections use unflagged projections only, are measured against the *uncorrected* model (so they never compound), are shrunk toward ×1 by n/(n+100), and are clamped to 0.85–1.15 for projection and 0.7–1.5 for σ. A market's correction **applies automatically once 100+ projections back it** (`CORRECTIONS_MIN_N`), and it updates every build.

## Saved parlays (Parlays tab)

The slip's **Save parlay** button posts the slip to a Google Sheet; every build grades it from the sheet's published CSV.

1. Create a Google Sheet (any name). **Extensions → Apps Script**, replace the code with [`docs/parlay-sheet.gs`](docs/parlay-sheet.gs), and save.
2. **Deploy → New deployment →** type **Web app**; *Execute as:* **Me**; *Who has access:* **Anyone**. Authorize it, then copy the **Web app URL** (ends in `/exec`). Opening it in a browser should show `{"ok":true,...}`. The script creates the **Parlays** tab on first use.
3. Put that URL in `PARLAY_WEBHOOK_URL` in `site/index.html` and in the `PARLAY_SHEET_CSV_URL` default in `.github/workflows/build.yml` (with `?format=csv` on the end), then push. The build reads the rows back from the same web app, so the sheet never needs to be shared or published. A repo secret or variable named `PARLAY_SHEET_CSV_URL` overrides the default.

When you change the script later: **Deploy → Manage deployments → ✏️ Edit → Version: New version → Deploy.** The URL stays the same.

Columns: `id, created, week, legs_json, book_odds, model_prob, status`. `status` stays `open` in the sheet; graded results live in `site/data/parlays.json`. Legs grade as hit/miss/push, or void if the player didn't play. A push or void leg drops out and the parlay is paid on the remaining legs at their saved prices. Units are 1u per parlay at the saved odds.

The web-app URL sits in the public page source and repo, so anyone who finds it could add rows or read the parlays back. The script only accepts the expected fields (max 20 legs), and a new deployment gives a new URL whenever you want.

## NBA

The site has an **NFL | NBA** switch in the header (`?sport=nba`, remembered per browser). Each sport has its own board, Results, Parlays and parlay slip.

- **Data:** sportsdataverse / hoopR ESPN player box scores and schedule (`sportsdataverse-data` GitHub releases, free, updated nightly). Lines come from The-Odds-API (`basketball_nba`): points, rebounds, assists, threes, and PRA.
- **Model** (`pipeline/nba.py`): projection = projected minutes × per-minute rate × opponent factor. Minutes and rates use the same 0.45·L5 + 0.30·L10 + 0.25·season weighting as the NFL model. The opponent factor is what that defense allows **per minute** to the player's position group (G/F/C) vs league, half-weighted and capped at ±20%. σ = game-to-game SD with a floor, ×1.15. Role-change flags use a **minutes** shift (≥8 min and ≥30% over the last 2 games, against the model's lean) plus the line-far-from-history rule. There's also an **implied-minutes** check: the points (or PRA) line ÷ the player's per-minute rate gives the minutes the book is pricing. If that's ≥35% above or ≤30% below the model's projected minutes, all of that player's props are flagged and demoted (a minutes restriction lifting, a starter returning, and so on). Players with fewer than 3 games are shown as thin history, not rated.
- **Teammate out:** when a rotation player (15+ projected minutes) is ruled out after playing the team's last game, his teammates' props get a *Teammate out* tag. Their projections are **not** boosted: a backtest (`pipeline/eval_redistribution.py`, 15 slates) found those teammates were already projected without bias, and every redistribution variant made projections worse. The code is in `nba.py` behind `REDIST_ON = False`. Results tracks these props separately, to see whether books overreact when they raise those lines.
- **Grading:** every live pull writes `site/data/nba/history/YYYY-MM-DD.json`. `nba.py --grade` grades each slate once its games are final and in the box scores, and writes `site/data/nba/results.json`. Corrections come from a backtest of every finished slate (`pipeline/nba_backtest_cache.json`) plus graded props, and apply per market at 100+ projections, same as the NFL.
- **Parlays:** NBA slips save to the same sheet. Legs have `prop_id` = `nba:<athlete>:<market>` and the week column holds the date as YYYYMMDD. `nba.py --grade` writes `site/data/nba/parlays.json`.
- **Workflow:** `.github/workflows/nba.yml` ("Build NBA props") runs daily at 10:07am and 5:07pm CT. It costs about 5 credits per game per pull (~100/day). Manual runs default to cached (free).
- **Injuries:** ESPN's public NBA injury report (free, no key), fetched on every build. Statuses show as tags (OUT, DBT, Q, GTD, with the injury type and ESPN's note on hover). Players listed **out or doubtful** who still have a line are flagged and demoted. Each player's detail panel lists his team's injuries and the opponent's outs. If ESPN is unreachable, the board still builds without tags (`injuries_ok: false` in props.json).

```bash
python pipeline/nba.py --lines sample      # or live / cached
python pipeline/nba.py --grade
```

## Layout

```
pipeline/build.py        data pull + model → site/data/props.json (+ history snapshot on live builds)
pipeline/grade.py        --grade: results.json + parlays.json
pipeline/nba.py          NBA: data + model + grading -> site/data/nba/
pipeline/requirements.txt
site/index.html          the app (no build step, no framework)
site/data/props.json     generated; committed by the workflow
site/data/history/       week_NN.json snapshots of live builds (graded later)
site/data/results.json   model record, calibration, corrections
site/data/parlays.json   saved parlays, graded
docs/parlay-sheet.gs     Google Apps Script web app for saving parlays
.github/workflows/build.yml   NFL
.github/workflows/nba.yml     NBA
docs/HANDOFF.md          project handoff: model, schedule, change log, open ideas
```
