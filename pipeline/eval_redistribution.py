#!/usr/bin/env python3
"""Does NBA teammate-out redistribution make projections closer to what actually happened?

For every finished slate (sample lines at recent averages), "outs" = rotation players who didn't
play that night. Compares projection error on the props that got a boost, redistribution on vs off.

    python pipeline/eval_redistribution.py            # REDIST_ALPHA as configured (x0.8 baseline)
    python pipeline/eval_redistribution.py 0.8 0.4    # try alpha scales

11/12 result (15 slates): every variant made bias and MAE worse -> REDIST_ON stays False in nba.py.
Takes ~3 minutes per scale."""
import statistics
import sys

import nba

alphas = [float(a) for a in sys.argv[1:]] or [None]
frames = nba.load_frames()
box, sched = frames["box"], frames["sched"]
out = nba.Outcomes(box, sched)
dates = [d for d in sorted(sched.date.unique()) if out.date_complete(d) and d >= "2026-10-27"]
base_alpha = dict(nba.REDIST_ALPHA)


def run(on, scale=None):
    nba.REDIST_ON = on
    if scale is not None:
        nba.REDIST_ALPHA = {m: min(1.2, a * scale / 0.8) for m, a in base_alpha.items()}
    rows = []
    for d in dates:
        s2 = sched.copy(); s2.loc[s2.date == d, "status_type_completed"] = False
        teams = set(s2[s2.date == d].home_abbreviation) | set(s2[s2.date == d].away_abbreviation)
        lines = nba.sample_lines(box, d, teams)
        props, _, _ = nba.build_props(box, s2, d, lines, None, None, nba.outs_from_box(box, d, teams))
        for p in props:
            a = out.actual(p["pid"], d, p["market"])
            if a is not None:
                rows.append((p["id"] + d, p["market"], p["proj_raw"], a, bool(p.get("redistrib"))))
    return rows


off = {r[0]: r for r in run(False)}
for scale in alphas:
    on = run(True, scale)
    boosted = [r for r in on if r[4]]
    print(f"alpha x{scale or 0.8}: {len(boosted)} boosted props over {len(dates)} slates")
    for m in nba.MARKETS:
        b = [r for r in boosted if r[1] == m]
        if not b:
            continue
        e_on = [r[3] - r[2] for r in b]
        e_off = [off[r[0]][3] - off[r[0]][2] for r in b]
        print(f"  {nba.MARKETS[m][1]:9s} n={len(b):4d}  bias off {statistics.fmean(e_off):+.2f} -> on {statistics.fmean(e_on):+.2f}   "
              f"MAE off {statistics.fmean(map(abs, e_off)):.2f} -> on {statistics.fmean(map(abs, e_on)):.2f}")
