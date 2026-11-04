#!/usr/bin/env python3
"""Per-wake breakdown of an LFN's charge from a test_lfn_profiles.sh run.

usage: lfn_power_breakdown.py RUN_DIR STEADY_FROM_S DUR_S [node ...]  (a test_lfn_profiles.sh output directory)
Prints, per LFN, the average current from STEADY_FROM to DUR split into the
floor, unicast windows, broadcast windows, discovery windows (parent checks),
transmissions outside windows (uplinks, LPAS sweeps), timer wakes, and the
mean length and charge of each kind of wake. JSON on the last line.
"""
import bisect, gzip, json, re, sys

run, t0s, t1s = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
nodes = [int(x) for x in sys.argv[4:]] or [2, 3]
T0 = 1700000000.0
t0, t1 = t0s * 1e6, t1s * 1e6
rep = json.load(open(f'{run}/power.json'))
out = {}
for node in nodes:
    n = next(x for x in rep['nodes'] if x['node'] == node)
    ma = {('mcu', k): v['ma'] for k, v in n['mcu']['states'].items()}
    ma.update({('radio', k): v['ma'] for k, v in n['radio']['states'].items() if 'ma' in v})
    tx_ma = {lv['dbm']: lv['ma'] for lv in n['radio']['states']['tx']['levels']}
    trans = {k: (v['charge_uc'] / v['count'] if v['count'] else 0) for k, v in n['radio']['transitions'].items()}
    # windows from the log
    wins = []
    with gzip.open(f'{run}/node{node}.log.gz', 'rt', errors='replace') as f:
        for line in f:
            t, _, m = line.partition(' ')
            if 'window open' not in m:
                continue
            try:
                ts = (float(t) - T0) * 1e6
            except ValueError:
                continue
            kind = 'bc' if 'broadcast window' in m else 'disc' if 'discovery window' in m else 'uc' if 'LFN window open' in m else None
            if kind:
                r = re.search(r'for (\d+) us', m)
                wins.append((ts, kind, int(r.group(1)) if r else 0))
    wins.sort()
    wt = [w[0] for w in wins]
    # intervals
    iv = []
    with gzip.open(f'{run}/power_lfn.txt.gz', 'rt') as f:
        for line in f:
            if line.startswith('#'):
                continue
            p = line.split()
            if int(p[2]) != node:
                continue
            s, e = float(p[0]), float(p[1])
            if e <= t0 or s >= t1:
                continue
            iv.append((max(s, t0), min(e, t1), p[3], p[4], int(p[5]) if len(p) > 5 else None))
    iv.sort()
    starts = sorted(s for s, e, part, st, d in iv if part == 'mcu' and st == 'deep_wake')
    ep = {}
    floor = 0.0
    for s, e, part, st, d in iv:
        dt = e - s
        if part == 'mcu' and st == 'deep_sleep':
            floor += dt * ma[('mcu', st)] / 1e3
            continue
        if part == 'radio' and st in ('off', 'idle'):
            c = 0.0
        elif part == 'radio' and st == 'tx':
            c = dt * tx_ma[d] / 1e3
        elif part == 'radio' and '>' in st:
            c = trans.get(st, 0.0) * (dt / max(dt, 1e-9))  # whole transitions only
        else:
            c = dt * ma.get((part, st), 0.0) / 1e3
        i = bisect.bisect_right(starts, s) - 1
        key = starts[i] if i >= 0 else -1
        x = ep.setdefault(key, {'c': 0.0, 'listen': 0.0, 'listen_start': None, 'rx': 0.0, 'tx': 0.0, 'ntx': 0, 'cca': 0})
        x['c'] += c
        if part == 'radio' and st == 'listen':
            x['listen'] += dt
            if x['listen_start'] is None:
                x['listen_start'] = s
        if part == 'radio' and st == 'receive':
            x['rx'] += dt
        if part == 'radio' and st == 'tx':
            x['tx'] += dt
            x['ntx'] += 1
        if part == 'radio' and st == 'cca':
            x['cca'] += 1
    cls = {}
    for k, x in ep.items():
        kind = 'timer'
        if x['listen_start'] is not None:
            j = bisect.bisect_left(wt, x['listen_start'] - 2000)
            if j < len(wins) and abs(wins[j][0] - x['listen_start']) < 2000:
                kind = wins[j][1]
            elif x['tx']:
                kind = 'tx'
            else:
                kind = 'other_listen'
        elif x['tx']:
            kind = 'tx'
        if kind in ('uc', 'bc') and x['rx'] and x['tx']:
            kind += '+dl'
        elif kind in ('uc', 'bc') and x['tx']:
            kind += '+tx'
        elif kind in ('uc', 'bc') and x['rx']:
            kind += '+rx'
        c = cls.setdefault(kind, {'n': 0, 'c': 0.0, 'listen': 0.0, 'rx': 0.0, 'tx': 0.0, 'ntx': 0})
        c['n'] += 1
        c['c'] += x['c']
        c['listen'] += x['listen']
        c['rx'] += x['rx']
        c['tx'] += x['tx']
        c['ntx'] += x['ntx']
    span = (t1 - t0) / 1e6
    total = floor + sum(c['c'] for c in cls.values())
    print(f'--- {run} node {node}: {span / 3600:.2f} h, {total / span:.3f} uA (floor {floor / span:.3f})')
    for k, c in sorted(cls.items(), key=lambda kv: -kv[1]['c']):
        print(f"  {k:12s} n={c['n']:6d} rate={c['n'] / span:.4f}/s  {c['c'] / span:7.3f} uA  per wake {c['c'] / c['n']:8.2f} uC"
              f"  listen {c['listen'] / c['n'] / 1000:7.3f} ms  rx {c['rx'] / c['n'] / 1000:6.3f} ms  tx {c['tx'] / c['n'] / 1000:6.3f} ms ({c['ntx'] / c['n']:.2f} frames)")
    wl = {}
    for ts, kind, us in wins:
        if t0 <= ts < t1:
            wl.setdefault(kind, []).append(us)
    for k, v in wl.items():
        print(f"  log {k}: {len(v)} windows, mean length {sum(v) / len(v):.0f} us, min {min(v)}, max {max(v)}")
    out[node] = {'span_s': span, 'total_uA': total / span, 'floor_uA': floor / span,
                 'classes': {k: {kk: (vv / span if kk == 'c' else vv) for kk, vv in c.items()} for k, c in cls.items()},
                 'win_mean_us': {k: sum(v) / len(v) for k, v in wl.items()}, 'win_n': {k: len(v) for k, v in wl.items()},
                 'tx_ma': tx_ma}
print(json.dumps(out))
