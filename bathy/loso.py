"""Leave-one-square-out check of the shoal correction (2026-10-14).

For each square's work directory (a finished bathy-pilot run through the
fit stage, tide-corrected: icesat_lat.npz, fit.json, sdb.tif, truth.npz),
the check data are the held-out ICESat-2 passes and the chart soundings.
For each sizing margin, the correction (bathy.pilot.size_curve: shift by
predicted depth, then the 3.5 m keep-out) is sized on all squares but one
and checked on that one, for every square in turn. The smallest margin
with every band from 2 m down passing in every held-out square is chosen
(0-2 m is reported, it is the keep-out's), and the published curve is
sized on all squares together with that margin.

Writes loso.json and loso.txt in --out.
"""
import argparse
import json
import os
import sys

import numpy as np

from bathy import pilot as P

MARGINS = (0.0, 0.01, 0.02, 0.03, 0.04)


def check_sets(work, icesat_file):
    import rasterio
    fit = json.load(open(os.path.join(work, "fit.json")))
    I = np.load(os.path.join(work, icesat_file))["pts"]
    T = np.load(os.path.join(work, "truth.npz"), allow_pickle=True)
    ds = rasterio.open(os.path.join(work, "sdb.tif"))
    H = I[np.array(fit["holdout_idx"], dtype=int)]
    S = T["soundings"]
    return {"icesat": (H[:, 2], P._sample(ds, H[:, 0], H[:, 1], [1])[:, 0]),
            "chart": (S[:, 2], P._sample(ds, S[:, 0], S[:, 1], [1])[:, 0]) if len(S) else (np.empty(0), np.empty(0))}


def pooled(sets_list):
    out = {}
    for name in ("icesat", "chart"):
        t = np.concatenate([s[name][0] for s in sets_list]); p = np.concatenate([s[name][1] for s in sets_list])
        out[name] = (t, p)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bathy-loso.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("works", nargs="+", help="square work directories")
    ap.add_argument("--icesat", default="icesat_lat.npz")
    ap.add_argument("--out", default="data/bathy/LANT/loso")
    a = ap.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    names = [os.path.basename(os.path.normpath(w)) for w in a.works]
    sets = {n: check_sets(w, a.icesat) for n, w in zip(names, a.works)}
    for n in names:
        print(f"{n}: chart soundings with SDB {int(np.isfinite(sets[n]['chart'][1]).sum()):,}, "
              f"held-out ICESat-2 with SDB {int(np.isfinite(sets[n]['icesat'][1]).sum()):,}", flush=True)
    results, chosen = [], None
    for margin in MARGINS:
        target = P.DEEPER_MAX_SHARE - margin
        folds = {}
        for hold in names:
            f, knots, _ = P.size_curve(pooled([sets[n] for n in names if n != hold]), target)
            check = {nm: P.band_stats(t, P.apply_final(f, pr)) for nm, (t, pr) in sets[hold].items() if len(t)}
            folds[hold] = {"knots": knots, "check": check, "pass": all(P._passes(r) for r in check.values())}
        ok = all(v["pass"] for v in folds.values())
        results.append({"margin": margin, "folds": folds, "pass": ok})
        print(f"margin {margin:.0%}: " + ", ".join(f"{h} {'pass' if v['pass'] else 'FAIL'}" for h, v in folds.items()), flush=True)
        if ok:
            chosen = margin
            break
    rep = {"squares": names, "cv": results, "chosen_margin": chosen}
    lines = [f"leave-one-square-out over {len(names)} squares: {', '.join(names)}"]
    if chosen is not None:
        f, knots, _ = P.size_curve(pooled(list(sets.values())), P.DEEPER_MAX_SHARE - chosen)
        rep["curve"] = knots
        lines.append(f"chosen margin {chosen:.0%}; published curve (predicted depth, shift): " +
                     ", ".join(f"({x:.1f} m, {y:.2f} m)" for x, y in knots))
    else:
        lines.append("NO margin passes every held-out square")
    last = results[-1]["folds"]
    lines.append("")
    for hold, v in last.items():
        lines.append(f"held out {hold} (curve {', '.join(f'{x:.1f}:{y:.2f}' for x, y in v['knots'])})")
        for nm, rows in v["check"].items():
            lines.append(f"  vs {nm:6s}: " + "; ".join(
                f"{r['band']} n={r['n']} {r['share_too_deep']:.1%}" + ("" if r["band"].startswith("0-") else f" {'pass' if r['pass'] else 'FAIL'}")
                for r in rows if r["n"]))
    json.dump(rep, open(os.path.join(a.out, "loso.json"), "w"), indent=1, default=str)
    open(os.path.join(a.out, "loso.txt"), "w").write("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
