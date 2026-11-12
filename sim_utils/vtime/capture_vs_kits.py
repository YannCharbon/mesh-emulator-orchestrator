#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""The medium's capture models against the FG25 kit measurements
(firmware repo docs/COCHANNEL_RESULTS.md §4.2 and §5; docs/MEDIUM.md §13.3).

    python3 sim_utils/vtime/capture_vs_kits.py build-vt/wssimserver [--sock /tmp/wssim-cvk.sock] [-n 200]

For each measured point (two frames, the second starting `offset` us after
the first, one of them `margin` dB stronger), fake nodes send n pairs
through the medium, once with the default (step) capture model and once
with --medium-capture-model=smooth, and the share of the stronger frame
received is printed next to the kit's. Frames as on the kits: mode 3, PSDU
of 45 bytes ("50 bytes") or 245 bytes ("250 bytes"), SHR 14 bytes; mode 1a,
PSDU 46 bytes, SHR 10 bytes. The two frames' powers: -70 dBm and -70 +
margin (-70 - margin when the first frame is the stronger one).
"""
import argparse
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_conductor as tc  # noqa: E402
from test_conductor import Radio, got, medium_run  # noqa: E402

M3_50 = {"rate_bps": 150000, "shr_len": 14, "payload": b"q" * 45}
M3_250 = {"rate_bps": 150000, "shr_len": 14, "payload": b"q" * 245}
A1_50 = {"payload": b"q" * 46}

# (PHY, frame, offset us, margin dB, which frame is stronger, kit received, kit cycles)
POINTS = [
    # §4.2, margin sweeps (the second frame stronger)
    ("3", M3_50, 0, 0.3, 2, 240, 305), ("3", M3_50, 0, 2.2, 2, 284, 300), ("3", M3_50, 0, 4.8, 2, 85, 85),
    ("3", M3_50, 100, 0.9, 2, 173, 300), ("3", M3_50, 100, 2.7, 2, 271, 303), ("3", M3_50, 100, 4.4, 2, 83, 84),
    ("3", M3_250, 100, 1.1, 2, 88, 203), ("3", M3_250, 100, 3.7, 2, 196, 204), ("3", M3_250, 100, 4.7, 2, 63, 63),
    ("3", M3_50, 700, 1.7, 2, 222, 307), ("3", M3_50, 700, 3.8, 2, 85, 87), ("3", M3_50, 700, 4.6, 2, 299, 305),
    ("3", M3_50, 700, 6.3, 2, 294, 300),
    # §4.2: the first frame stronger, offset 300 us
    ("3", M3_50, 300, 2.4, 1, 86, 86), ("3", M3_50, 300, 3.5, 1, 84, 85),
    # §4.2, timing scan at +5, +11 and +21 dB (350-500 us: 400; 800-2000 us: 1000)
    *[("3", M3_50, off, m, 2, k, n) for m, row in (
        (5, [(0, 65, 65), (100, 64, 64), (200, 64, 64), (250, 81, 154), (400, 0, 62), (550, 0, 60), (600, 68, 156),
             (650, 146, 150), (700, 149, 153), (750, 71, 152), (1000, 0, 63)]),
        (11, [(0, 60, 60), (200, 60, 61), (250, 103, 155), (400, 0, 63), (550, 6, 151), (600, 37, 150),
              (650, 63, 64), (700, 63, 63), (750, 98, 154), (1000, 0, 63)]),
        (21, [(0, 61, 61), (200, 65, 65), (250, 96, 156), (400, 0, 63), (550, 0, 61), (600, 20, 154),
              (650, 59, 60), (700, 62, 63), (750, 79, 152), (1000, 0, 65)])) for off, k, n in row],
    # §5, mode 1a
    *[("1a", A1_50, off, m, 2, k, n) for m, row in (
        (5.5, [(0, 61, 61), (100, 100, 153), (200, 103, 155), (400, 32, 153), (600, 11, 155), (1000, 0, 63)]),
        (11, [(0, 65, 65), (100, 66, 66), (200, 66, 66), (400, 66, 66), (600, 146, 156), (1000, 0, 63)])) for off, k, n in row],
    ("1a", A1_50, 0, 1.3, 2, 67, 67), ("1a", A1_50, 0, 0.1, 2, 161, 206),
]


def share(frame, offset, margin, stronger, n, opts):
    period = 40000 if len(frame["payload"]) < 100 else 60000
    w = dict(frame)
    p1, p3 = (-70, -70 + margin) if stronger == 2 else (-70, -70 - margin)
    r = {1: Radio(1, {10000 + k * period: [("tx", dict(w))] for k in range(n)}), 2: Radio(2),
         3: Radio(3, {10000 + offset + k * period: [("tx", dict(w))] for k in range(n)})}
    d, out, rows = medium_run(r, [(1, 2, p1), (3, 2, p3)], opts=opts)
    want = 0 if stronger == 2 else 1      # frame IDs: 1's odd, 3's even
    return sum(m["ok"] for t, m in got(r[2], "xv") if m["frame_id"] % 2 == want) / n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("server")
    ap.add_argument("--sock", default="/tmp/wssim-cvk.sock")
    ap.add_argument("-n", type=int, default=200)
    ap.add_argument("--logdir", default=None)
    args = ap.parse_args()
    args.logdir = args.logdir or tempfile.mkdtemp(prefix="cvk_")
    os.makedirs(args.logdir, exist_ok=True)
    args.copy = os.path.join(args.logdir, "wssimserver-cvk")
    shutil.copy2(args.server, args.copy)
    args.bin = args.copy
    tc.ARGS = args
    print("| PHY | Frame | Offset (us) | Margin (dB) | Stronger | Kit | Step model | Smooth model |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- |")
    err = {"step": [], "smooth": []}
    for phy, frame, off, m, strong, k, n in POINTS:
        step = share(frame, off, m, strong, args.n, [])
        smooth = share(frame, off, m, strong, args.n, ["--medium-capture-model=smooth"])
        kit = k / n
        err["step"].append(abs(step - kit))
        err["smooth"].append(abs(smooth - kit))
        size = 50 if len(frame["payload"]) < 100 else 250
        print(f"| {phy} | {size} B | {off} | +{m} | {'second' if strong == 2 else 'first'} | {kit * 100:.0f} % "
              f"({k}/{n}) | {step * 100:.0f} % | {smooth * 100:.0f} % |")
    for name, e in err.items():
        print(f"{name}: mean absolute difference {100 * sum(e) / len(e):.1f} points, "
              f"{sum(1 for x in e if x > 0.2)} of {len(e)} points more than 20 points away")


if __name__ == "__main__":
    main()
