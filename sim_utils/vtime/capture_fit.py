#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Fit of the smooth capture model (docs/MEDIUM.md §13.3) to every capture
point measured on the FG25 kits (capture_kit_points.json: 256 power pairs,
24 911 cycles, firmware repo docs/COCHANNEL_RESULTS.md §4.2 and §5), and its
cross-validation.

    python3 sim_utils/vtime/capture_fit.py [--folds 5] [--quick]

The model is the medium's own (sim_medium.c, med_smooth_capture() and the
presync curve), written here in closed form for two frames: I first, W
starting `offset` us later, W stronger by SIR dB (negative: I stronger).
The kits' SIR axis has its own error (COCHANNEL_RESULTS.md §2: points with
the same nominal SIR differ by up to about 1 dB): the fit integrates the
model over a normal SIR error of standard deviation sigma, fitted too; the
medium then uses the model at the exact SIR. Maximum likelihood (binomial
counts of W received and I received), Nelder-Mead.

Output: the fitted parameters, the residual (mean absolute difference
between model and kit, per point, in percentage points) on the data of the
fit, and the K-fold cross-validated difference (each fold's points
predicted by a fit made without them): the validation figure. Standard
library only.
"""
import argparse
import json
import math
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))
SFD_BITS = 16
BREAK_DB, BRK_S = 2.5, 0.5
MAIN = {"3": (-2.85, 0.90), "1a": (-2.40, 0.90)}
RATE = {"3": 150000, "1a": 50000}
SHR = {"3": 14, "1a": 10}
PSDU = {("3", 50): 45, ("3", 250): 245, ("1a", 50): 46}
# Gauss-Hermite nodes and weights (probabilists', 9 points)
GH = [(-4.512745863399783, 2.2345844007746e-05), (-3.2054290028564703, 0.002789141321231), (-2.07684797867783,
      0.049916406765218), (-1.0232556637891326, 0.244097502894939), (0.0, 0.406349206349206),
      (1.0232556637891326, 0.244097502894939), (2.07684797867783, 0.049916406765218), (3.2054290028564703,
      0.002789141321231), (4.512745863399783, 2.2345844007746e-05)]

NAMES = ["pre_x0", "pre_b", "m50_3", "s_3", "early_3", "w_3", "rise_3", "fall_3",
         "m50_1a", "s_1a", "early_1a", "slope_1a", "w_1a", "sigma"]
START = [-11.2, 1.5, 0.5, 1.0, 38, 2, -4, 1, 0.5, 0.5, 12, 3.9, 6, 0.5]
# The capture margin's centre is at least 0 dB (a weaker frame does not win more often than a
# stronger one) and its scale at least 0.2 dB
LOW = [-40, 0.1, 0, 0.2, 0, 0.3, -40, -40, 0, 0.2, 0, 0, 0.3, 0.05]
HIGH = [10, 10, 5, 5, 120, 30, 40, 40, 5, 5, 80, 20, 40, 2.0]


def logistic(x):
    if x < -700:
        return 0.0
    return 1 / (1 + math.exp(-x))


def params(v, phy):
    p = dict(zip(NAMES, v))
    if phy == "3":
        return dict(x0=p["pre_x0"], b=p["pre_b"], m50=p["m50_3"], s=p["s_3"], early=p["early_3"], slope=0.0,
                    w=p["w_3"], rise=p["rise_3"], fall=p["fall_3"], sfd=True)
    return dict(x0=p["pre_x0"], b=p["pre_b"], m50=p["m50_1a"], s=p["s_1a"], early=p["early_1a"],
                slope=p["slope_1a"], w=p["w_1a"], rise=0.0, fall=0.0, sfd=False)


def frame(phy, length):
    rate = RATE[phy]
    psdu = PSDU[(phy, length)]
    shr_us = SHR[phy] * 8 * 1000000 // rate
    air_us = (SHR[phy] + 2 + psdu + 4) * 8 * 1000000 // rate
    pre_us = shr_us - SFD_BITS * 1000000 // rate
    return rate, shr_us, pre_us, air_us, 8 * (2 + psdu + 4)


def survive(sir, x0, b, bits, share):
    ber = min(0.5, 0.5 * math.exp(-(sir - x0) / b))
    return (1 - ber) ** (bits * share) if share > 0 else 1.0


def smooth_pc_pb(q, rate, shr_us, pre_us, d_us, m):
    d = d_us * rate / 1e6
    tp, tshr = pre_us * rate / 1e6, shr_us * rate / 1e6
    if d >= tshr + (q["fall"] if q["sfd"] else 0) + 8 * q["w"]:
        return 0.0, 0.0
    e = min(max(q["early"] + q["slope"] * max(m - 5, 0), 0), tp)
    early = 1 - logistic((d - e) / q["w"])
    sfd = logistic((d - tp - q["rise"]) / q["w"]) * (1 - logistic((d - tshr - q["fall"]) / q["w"])) if q["sfd"] else 0
    w = max(early, sfd)
    p_c = w * logistic((m - q["m50"]) / q["s"])
    p_b = (1 - w) * logistic((m - BREAK_DB) / BRK_S) if d < tshr else 0
    if p_c < 1e-9 and p_b < 1e-9:
        return 0.0, 0.0
    return p_c, p_b


def model(q, phy, length, d, m):
    """P(W received), P(I received) at the exact SIR m of W."""
    rate, shr_us, pre_us, air_us, bits = frame(phy, length)
    p_c, p_b = smooth_pc_pb(q, rate, shr_us, pre_us, d, m)
    # Captured: W received through I's remaining bits (I on air before W's SHR end: presync)
    share_w = max(0, air_us - shr_us - d) / (air_us - shr_us)
    pw = p_c * survive(m, q["x0"], q["b"], bits, share_w)
    # Neither: I received through W's bits (presync if W started during I's SHR)
    x0, b = (q["x0"], q["b"]) if d < shr_us else MAIN[phy]
    share_i = (air_us - max(d, shr_us)) / (air_us - shr_us)
    pi = (1 - p_c - p_b) * survive(-m, x0, b, bits, share_i)
    return pw, pi


def model_step(phy, length, d, m):
    """The step model of §3 (fg25 defaults), for comparison."""
    rate, shr_us, pre_us, air_us, bits = frame(phy, length)
    early_us = (37 if phy == "3" else 20) * 1000000 // rate
    cap_win = d < early_us or (phy == "3" and pre_us <= d < shr_us)
    search = d < shr_us and not cap_win
    if cap_win and m >= 3:
        share_w = max(0, air_us - shr_us - d) / (air_us - shr_us)
        return survive(m, *MAIN[phy], bits, share_w), 0.0
    if search and m >= 2.5:
        return 0.0, 0.0
    share_i = (air_us - max(d, shr_us)) / (air_us - shr_us)
    return 0.0, survive(-m, *MAIN[phy], bits, share_i)


def blurred(v, pt):
    """The model at the measured SIR, averaged over the SIR error."""
    q = params(v, pt["phy"])
    sig = v[NAMES.index("sigma")]
    pw = pi = 0.0
    for z, wt in GH:
        a, b = model(q, pt["phy"], pt["len"], pt["offset"], pt["sir"] + sig * z)
        pw += wt * a
        pi += wt * b
    return pw, pi


def nll(v, pts):
    for x, lo, hi in zip(v, LOW, HIGH):
        if not lo <= x <= hi:
            return 1e18
    total = 0.0
    for pt in pts:
        pw, pi = blurred(v, pt)
        for p, k in ((pw, pt["w"]), (pi, pt["i"])):
            p = min(max(p, 1e-9), 1 - 1e-9)
            total -= k * math.log(p) + (pt["n"] - k) * math.log(1 - p)
    return total


def nelder_mead(f, x0, steps, iters):
    n = len(x0)
    pts = [list(x0)] + [[x0[j] + (steps[j] if j == i else 0) for j in range(n)] for i in range(n)]
    vals = [f(p) for p in pts]
    for _ in range(iters):
        order = sorted(range(n + 1), key=lambda i: vals[i])
        pts, vals = [pts[i] for i in order], [vals[i] for i in order]
        c = [sum(p[j] for p in pts[:-1]) / n for j in range(n)]
        xr = [c[j] + (c[j] - pts[-1][j]) for j in range(n)]
        fr = f(xr)
        if fr < vals[0]:
            xe = [c[j] + 2 * (c[j] - pts[-1][j]) for j in range(n)]
            fe = f(xe)
            pts[-1], vals[-1] = (xe, fe) if fe < fr else (xr, fr)
        elif fr < vals[-2]:
            pts[-1], vals[-1] = xr, fr
        else:
            xc = [c[j] + 0.5 * (pts[-1][j] - c[j]) for j in range(n)]
            fc = f(xc)
            if fc < vals[-1]:
                pts[-1], vals[-1] = xc, fc
            else:
                pts = [pts[0]] + [[pts[0][j] + 0.5 * (p[j] - pts[0][j]) for j in range(n)] for p in pts[1:]]
                vals = [vals[0]] + [f(p) for p in pts[1:]]
    i = min(range(n + 1), key=lambda i: vals[i])
    return pts[i], vals[i]


def fit(pts, iters):
    steps = [2, 0.3, 0.5, 0.3, 4, 1, 2, 2, 0.5, 0.2, 4, 1, 2, 0.2]
    v, val = nelder_mead(lambda x: nll(x, pts), START, steps, iters)
    v, val = nelder_mead(lambda x: nll(x, pts), v, [s / 4 for s in steps], iters)   # restart
    return v, val


def mad(v, pts, exact=False):
    """Mean absolute difference, percentage points, over W and I."""
    e = []
    for pt in pts:
        pw, pi = (model(params(v, pt["phy"]), pt["phy"], pt["len"], pt["offset"], pt["sir"]) if exact
                  else blurred(v, pt))
        e += [abs(pw - pt["w"] / pt["n"]), abs(pi - pt["i"] / pt["n"])]
    return 100 * sum(e) / len(e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--iters", type=int, default=1500)
    ap.add_argument("--quick", action="store_true", help="no cross-validation")
    args = ap.parse_args()
    with open(os.path.join(HERE, "capture_kit_points.json")) as f:
        pts = json.load(f)["points"]
    v, val = fit(pts, args.iters)
    print("fit on all", len(pts), "points:")
    for n, x in zip(NAMES, v):
        print(f"  {n:9s} {x:8.3f}")
    step = []
    for pt in pts:
        pw, pi = model_step(pt["phy"], pt["len"], pt["offset"], pt["sir"])
        step += [abs(pw - pt["w"] / pt["n"]), abs(pi - pt["i"] / pt["n"])]
    print(f"residual, mean absolute difference per point and frame: smooth {mad(v, pts):.1f} points "
          f"(at the measured SIR, without the SIR error: {mad(v, pts, True):.1f}); "
          f"step model {100 * sum(step) / len(step):.1f}")
    if args.quick:
        return
    rnd = random.Random(1)
    idx = list(range(len(pts)))
    rnd.shuffle(idx)
    errs = []
    for k in range(args.folds):
        test = [pts[i] for i in idx[k::args.folds]]
        train = [pts[i] for i in idx if i not in set(idx[k::args.folds])]
        vk, _ = fit(train, args.iters)
        errs.append((mad(vk, test), len(test)))
        print(f"  fold {k + 1}: {len(test)} points held out, difference {errs[-1][0]:.1f} points")
    cv = sum(e * n for e, n in errs) / sum(n for e, n in errs)
    print(f"cross-validated ({args.folds} folds): mean absolute difference {cv:.1f} points")


if __name__ == "__main__":
    main()
