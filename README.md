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

The workflow runs Thu 9am, Sat 8am, Sun 7am Central (lines + stats) and Tue 9am (stats only — no credits). A full-slate pull of 6 markets across ~14 games costs roughly **80–90 credits**, so three pulls a week is ~260/month, well inside the 500 free. The header shows credits remaining after each build. To spend fewer: drop a market in `MARKETS`, or remove the Thursday run.

## Running locally

```bash
pip install -r pipeline/requirements.txt
ODDS_API_KEY=xxx python pipeline/build.py        # live lines
python pipeline/build.py --sample                # no key: lines set at recent averages
cd site && python -m http.server 8000            # open http://localhost:8000
```

## How the numbers work

- **Projection** = 0.45·L5 avg + 0.30·L10 avg + 0.25·season avg, then nudged (half-weight, clamped ±20%) by how the opponent's defense has done vs that position this season.
- **σ** = the player's game-to-game standard deviation with a floor, inflated 15% for model error. **P(over)** is a normal-curve estimate from projection and σ (with a half-point continuity correction on count markets).
- **Edge** = projection − consensus line (median across books). **EV** uses the best available price at that line, after stripping the vig from the book's implied probability for comparison.
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
```
