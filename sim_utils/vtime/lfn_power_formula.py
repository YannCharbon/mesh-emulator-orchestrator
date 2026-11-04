#!/usr/bin/env python3
"""The LFN power formula (root repo documents/pre-study/investigation/LFN_Capabilities_and_Power.md §5)
against the emulator ledger: the JSON line of lfn_power_breakdown.py, saved as <runs>/<CASE>_s<seed>.json
for runs of test_lfn_profiles.sh named after the cases below (LFNs at tx_power=10).
usage: lfn_power_formula.py <runs dir>"""
import json, glob, os, re, sys
R = sys.argv[1]
# hardware constants: emulator LFN profile efr32fg25-lfn.ini, EU mode 3, +10 dBm
I_FLOOR = 7.0          # uA
I_W = 4.13 + 2.145     # mA, radio RX + MCU EM1 while a window is open
Q_FIX = 0.86           # uC per window wake (set-up, teardown, CPU)
Q_SYNC = 2.0           # uC extra per received sync frame
Q_TIMER = 0.70         # uC per timer wake (emulator; kits 0.1)
N_TIMER = 0.094        # timer wakes per s, idle
Q_CHECK = 16.6e3       # uC per better-parent check (2 LPAS sweeps of 35 frames + 0.6 s window)
Q_XCH = 440.0          # uC per request/echo exchange (uplink 283 + downlink increment 158)
cases = {  # T_u, T_b, N, sync_only, r_ppm, BR period, LFN period
    'T5': (5, 10, 5, False, 5, 1800, 2700), 'T2': (2, 10, 5, False, 5, 1800, 2700),
    'T30': (30, 10, 5, False, 5, 1800, 2700), 'TS': (5, 10, 5, True, 5, 1800, 2700),
    'TW': (5, 10, 5, False, 50, 1800, 2700), 'TU': (5, 10, 5, False, 5, 1800, 60),
    'TD': (5, 10, 5, False, 5, 60, 2700), 'BAL': (60, 60, 15, False, 5, 1800, 2700),
    'ECO': (300, 600, 60, True, 5, 1800, 2700), 'EX2': (300, 60, 15, True, 5, 1800, 2700),
    'ECO4H': (14400, 600, 60, True, 5, 18500, 27000)}
def idle(Tu, Tb, N, so, r):
    S = Tb * N
    Lu = 5.0 + r * 1e-3 * S                    # ms: W + 2 * r * S/2
    Lb = 5.0 + (2 * r * 1e-3 * S if so else r * 1e-3 * Tb * (N + 1))
    qu, qb = I_W * Lu + Q_FIX, I_W * Lb + Q_FIX
    iu = qu / Tu
    ib = qb / (S if so else Tb)
    return Lu, Lb, iu, ib, Q_SYNC / S, N_TIMER * Q_TIMER
rows = []
for f in sorted(glob.glob(f'{R}/*.json')):
    name = os.path.basename(f)[:-5]
    case = re.sub(r'_s\d+$', '', name)
    if case not in cases:
        continue
    Tu, Tb, N, so, r, brp, lfp = cases[case]
    d = json.load(open(f))
    for node, x in d.items():
        Lu, Lb, iu, ib, isy, it = idle(Tu, Tb, N, so, r)
        c = x['classes']
        led_idle = x['floor_uA'] + sum(c.get(k, {}).get('c', 0) for k in ('uc', 'bc', 'bc+rx', 'timer'))
        # the ledger's uc excludes the windows that carried a downlink: count them as plain windows
        ndl = sum(c.get(k, {}).get('n', 0) for k in ('uc+dl', 'uc+rx', 'uc+tx'))
        led_idle += ndl * (I_W * Lu + Q_FIX) / x['span_s']
        pred_idle = I_FLOOR + iu + ib + isy + it
        nchk = x['win_n'].get('disc', 0)
        xch = (1 / brp + 1 / lfp + (1 / brp if node == '3' else 0))  # node 3: also ff02::1 + echo
        pred_tot = pred_idle + nchk * Q_CHECK / x['span_s'] + xch * Q_XCH
        rows.append((name, node, Lu * 1000, x['win_mean_us'].get('uc', 0), Lb * 1000, x['win_mean_us'].get('bc', 0),
                     pred_idle, led_idle, pred_tot, x['total_uA'], nchk))
print('run node | Lu pred/led us | Lb pred/led us | idle pred/led uA (dev) | total pred/led uA (dev) | checks')
for r_ in rows:
    n, nd, lup, lul, lbp, lbl, pi, li, pt, lt, nc = r_
    print(f'{n:8s} {nd} | {lup:7.0f} {lul:7.0f} | {lbp:7.0f} {lbl:7.0f} | {pi:6.2f} {li:6.2f} ({(pi / li - 1) * 100:+5.1f} %) | '
          f'{pt:6.2f} {lt:6.2f} ({(pt / lt - 1) * 100:+5.1f} %) | {nc}')
