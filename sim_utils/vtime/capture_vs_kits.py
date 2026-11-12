#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""The medium's capture models against every capture point measured on the
FG25 kits (capture_kit_points.json: 256 power pairs of firmware repo
docs/COCHANNEL_RESULTS.md §4.2 and §5; docs/MEDIUM.md §13.3).

    python3 sim_utils/vtime/capture_vs_kits.py build-vt/wssimserver [--sock /tmp/wssim-cvk.sock] [-n 100] [--table]

For each measured power pair (frame I first, frame W `offset` us later, W
stronger by the measured SIR, negative when I is stronger), fake nodes send n
pairs through the medium, once with the default (step) capture model and
once with --medium-capture-model=smooth; the shares of W and of I received
are compared with the kit's. Frames as on the kits: mode 3, PSDU of 45 bytes
("50 bytes") or 245 bytes ("250 bytes"), SHR 14 bytes; mode 1a, PSDU 46
bytes, SHR 10 bytes. Powers: I at -70 dBm, W at -70 + SIR.

The medium uses the measured SIR as exact, while the kits' SIR axis has an
error of its own (about 0.7 dB, capture_fit.py): near 0 dB, points of the
same nominal SIR differ by tens of points, which no model at the measured
SIR can follow. The figures are residuals of the fit (capture_fit.py fits
these same points); the cross-validated figure is capture_fit.py's.
"""
import argparse
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_conductor as tc  # noqa: E402
from test_conductor import Radio, got, medium_run  # noqa: E402

PSDU = {("3", 50): 45, ("3", 250): 245, ("1a", 50): 46}


def frame(phy, length):
    f = {"payload": b"q" * PSDU[(phy, length)]}
    if phy == "3":
        f.update(rate_bps=150000, shr_len=14)
    return f


def shares(pt, n, opts):
    """Shares of W and I received over n pairs."""
    w = frame(pt["phy"], pt["len"])
    period = 40000 if pt["len"] < 100 else 60000
    r = {1: Radio(1, {10000 + k * period: [("tx", dict(w))] for k in range(n)}), 2: Radio(2),
         3: Radio(3, {10000 + pt["offset"] + k * period: [("tx", dict(w))] for k in range(n)})}
    d, out, rows = medium_run(r, [(1, 2, -70), (3, 2, round(-70 + pt["sir"], 2))], opts=opts)
    xv = [m for t, m in got(r[2], "xv")]
    return (sum(m["ok"] for m in xv if m["frame_id"] % 2 == 0) / n,      # 3's frames: W
            sum(m["ok"] for m in xv if m["frame_id"] % 2 == 1) / n)      # 1's frames: I


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("server")
    ap.add_argument("--sock", default="/tmp/wssim-cvk.sock")
    ap.add_argument("-n", type=int, default=100)
    ap.add_argument("--logdir", default=None)
    ap.add_argument("--table", action="store_true", help="print every point")
    args = ap.parse_args()
    args.logdir = args.logdir or tempfile.mkdtemp(prefix="cvk_")
    os.makedirs(args.logdir, exist_ok=True)
    args.copy = os.path.join(args.logdir, "wssimserver-cvk")
    shutil.copy2(args.server, args.copy)
    args.bin = args.copy
    tc.ARGS = args
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "capture_kit_points.json")) as f:
        pts = json.load(f)["points"]
    if args.table:
        print("| PHY | File | Offset (us) | SIR of W (dB) | Kit W / I | Step W / I | Smooth W / I |")
        print("| --- | --- | --- | --- | --- | --- | --- |")
    err = {"step": [[], []], "smooth": [[], []]}
    for pt in pts:
        kit = (pt["w"] / pt["n"], pt["i"] / pt["n"])
        res = {"step": shares(pt, args.n, []), "smooth": shares(pt, args.n, ["--medium-capture-model=smooth"])}
        trans = any(0.05 < x < 0.95 for x in kit)
        for name, (w, i) in res.items():
            e = abs(w - kit[0]) + abs(i - kit[1])
            err[name][0].append(e / 2)
            if trans:
                err[name][1].append(e / 2)
        if args.table:
            print(f"| {pt['phy']} | {pt['file']} | {pt['offset']} | {pt['sir']:+.1f} | {kit[0] * 100:.0f} / "
                  f"{kit[1] * 100:.0f} % ({pt['n']}) | {res['step'][0] * 100:.0f} / {res['step'][1] * 100:.0f} % | "
                  f"{res['smooth'][0] * 100:.0f} / {res['smooth'][1] * 100:.0f} % |")
    for name, (a, t) in err.items():
        print(f"{name}: mean absolute difference {100 * sum(a) / len(a):.1f} points over {len(a)} points; "
              f"{100 * sum(t) / len(t):.1f} points over the {len(t)} points in a transition (kit share of W or I "
              f"between 5 and 95 %); {sum(1 for x in a if x > 0.2)} points more than 20 points away")


if __name__ == "__main__":
    main()
