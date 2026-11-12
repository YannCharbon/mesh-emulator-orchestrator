#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""The calibration of the layered receiver curve (docs/MEDIUM.md §12.3).

    python3 sim_utils/vtime/layered_fit.py [--search]

The frame loss rate of the steep curve BER = 0.5 exp(-(SNR - X0) / B),
averaged over Rician fading (mean power 1), against the empirical curve
BER = 0.5 exp(-SNR / 2), as functions of the mean received power, for 50-,
100- and 250-byte PSDUs (8 (2 + PSDU + 4) counted bits). Prints the table
and the 10 % points of §12.3; --search also prints the K search and the
fit of X0 (a few seconds).
"""
import argparse
import math

NOISE_DBM = -153.930645 + 30 + 4
K_DEFAULT, X0_DEFAULT, B_DEFAULT = 10, 9.0, 0.9


def i0(z):
    s = t = 1.0
    k = 0
    while True:
        k += 1
        t *= (z / 2) ** 2 / (k * k)
        s += t
        if t < s * 1e-17:
            return s


_grids = {}


def rician_grid(k, n=3000, lo=-25.0, hi=2.5):
    """[(power gain, probability)] over a log grid, mean power 1."""
    if (k, n) in _grids:
        return _grids[(k, n)]
    out, du, tot = [], (hi - lo) / n, 0
    for i in range(n):
        u = lo + (i + 0.5) * du
        x = math.exp(u)
        f = (k + 1) * math.exp(-k - (k + 1) * x) * i0(2 * math.sqrt(k * (k + 1) * x)) if k else math.exp(-x)
        out.append((x, f * x * du))
        tot += f * x * du
    _grids[(k, n)] = [(x, w / tot) for x, w in out]
    return _grids[(k, n)]


def rician_cdf(k, x0):
    return sum(w for x, w in rician_grid(k) if x < x0)


def per(snr_db, bits, x0, b):
    ber = min(0.5, 0.5 * math.exp(-(snr_db - x0) / b))
    return -math.expm1(bits * math.log1p(-ber))


def per_layered_avg(p_dbm, bits, k=K_DEFAULT, x0=X0_DEFAULT, b=B_DEFAULT, n=3000):
    return sum(w * per(p_dbm - NOISE_DBM + 10 * math.log10(x), bits, x0, b) for x, w in rician_grid(k, n))


def per_empirical(p_dbm, bits):
    return per(p_dbm - NOISE_DBM, bits, 0, 2)


def point10(fn):
    lo, hi = -130.0, -60.0
    for _ in range(60):
        m = (lo + hi) / 2
        if fn(m) > 0.1:
            lo = m
        else:
            hi = m
    return m


def worst_ratio(bits_list, k, x0, b, n=1500):
    return max(abs(math.log(per_layered_avg(p, bits, k, x0, b, n) / per_empirical(p, bits)))
               for bits in bits_list for p in range(-100, -84))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--search", action="store_true")
    a = ap.parse_args()
    lengths = {50: 448, 100: 848, 250: 2048}
    if a.search:
        print("K search, B 0.9 dB, X0 fitted for 848 bits: largest ratio from -100 to -85 dBm")
        for k in (6, 8, 10, 12, 15):
            best = min((worst_ratio([848], k, x0 / 10, 0.9), x0 / 10) for x0 in range(0, 160, 2))
            print(f"  K {k:2d}: X0 {best[1]:4.1f} dB, factor {math.exp(best[0]):.2f}")
    print(f"layered: K {K_DEFAULT}, X0 {X0_DEFAULT} dB, B {B_DEFAULT} dB")
    levels = list(range(-104, -83, 2))
    print("level dBm " + " ".join(f"{p:>8d}" for p in levels))
    print("emp 100B  " + " ".join(f"{per_empirical(p, 848):8.2e}" for p in levels))
    print("lay 100B  " + " ".join(f"{per_layered_avg(p, 848):8.2e}" for p in levels))
    for psdu, bits in lengths.items():
        print(f"ratio {psdu:3d}B " + " ".join(f"{per_layered_avg(p, bits) / per_empirical(p, bits):8.2f}"
                                           for p in levels))
    for psdu, bits in lengths.items():
        w = math.exp(worst_ratio([bits], K_DEFAULT, X0_DEFAULT, B_DEFAULT, 3000))
        print(f"{psdu:3d} bytes: largest ratio -100..-85 dBm {w:.2f}; 10 % points: empirical "
              f"{point10(lambda p: per_empirical(p, bits)):.1f}, layered average "
              f"{point10(lambda p: per_layered_avg(p, bits)):.1f}, steep alone "
              f"{point10(lambda p: per(p - NOISE_DBM, bits, X0_DEFAULT, B_DEFAULT)):.1f} dBm")


if __name__ == "__main__":
    main()
