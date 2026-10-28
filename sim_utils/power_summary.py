#!/usr/bin/env python3
"""Summarise the power reports of virtual-time runs (docs/POWER_MODEL.md §7).

usage: power_summary.py [options] <run dir or power.json>...

Per run: one line per node (average current, MCU and radio time shares,
wakes and empty ticks per second, battery life) and the checks: the MCU's and
the radio's state times (transitions included) must each add up to the run
time. With several runs: per node, the median and range of the average
current over the runs.

Options:
  --cell MAH[:V[:EFF]]  battery life for this cell (capacity in mAh; with V,
                        the cell voltage and the converter's efficiency, the
                        cell current is I x supply_V / (V x EFF)). Overrides
                        the report's own (--power-cell).
  --nodes 1-14          only these nodes (also "1,3,5-7")
  --quiet               only the multi-run table and the checks
Exit status: 1 if a check fails, 0 otherwise.
"""
import json, os, statistics, sys

TOL_US = 1.0   # the sums are of doubles: allow 1 µs over a run


def parse_ids(spec):
    ids = set()
    for part in spec.split(','):
        a, _, b = part.partition('-')
        ids.update(range(int(a), int(b or a) + 1))
    return ids


def load(path):
    if os.path.isdir(path):
        path = os.path.join(path, 'power.json')
    with open(path) as f:
        return path, json.load(f)


def battery(node, supply_v, cell):
    if cell:
        mah, v, eff = cell
        i = node['avg_current_ma'] * (supply_v / (v * eff) if v else 1)
        return mah / i / (24 * 365.25) if i > 0 else float('inf')
    b = node.get('battery')
    return b['life_years'] if b else None


def main(argv):
    cell, only, quiet, paths = None, None, False, []
    it = iter(argv)
    for a in it:
        if a == '--cell':
            parts = [float(x) for x in next(it).split(':')]
            cell = (parts[0], parts[1] if len(parts) > 1 else 0, parts[2] if len(parts) > 2 else 1)
        elif a == '--nodes':
            only = parse_ids(next(it))
        elif a == '--quiet':
            quiet = True
        elif a in ('-h', '--help'):
            print(__doc__)
            return 0
        else:
            paths.append(a)
    if not paths:
        print(__doc__, file=sys.stderr)
        return 2

    failed = 0
    per_node = {}
    for p in paths:
        path, d = load(p)
        run_us = d['run_time_us']
        supply = {pr['name']: pr['supply_v'] for pr in d['profiles']}
        if not quiet:
            print(f"== {path}: {run_us / 1e6:.1f} s, seed {d['seed']}, profiles "
                  + ', '.join(f"{pr['name']} ({pr['kind']})" for pr in d['profiles']))
            print(f"{'node':>4} {'avg mA':>8} {'MCU act%':>8} {'deep%':>6} {'listen%':>7} {'recv%':>6} "
                  f"{'TX%':>6} {'CCA/s':>6} {'wakes/s':>7} {'empty/s':>7} {'unseen/s':>8} {'life y':>7}")
        for n in d['nodes']:
            if only is not None and n['node'] not in only:
                continue
            c = n['check']
            for part in ('mcu_time_us', 'radio_time_us'):
                if abs(c[part] - c['run_time_us']) > TOL_US:
                    print(f"CHECK FAILED: {path} node {n['node']}: {part} {c[part]:.3f} != run {c['run_time_us']:.3f}")
                    failed += 1
            ms, rs, w = n['mcu']['states'], n['radio']['states'], n['wakes']
            sec = run_us / 1e6
            life = battery(n, supply[n['profile']], cell)
            per_node.setdefault(n['node'], []).append(n['avg_current_ma'])
            if not quiet:
                pct = lambda t: 100 * t / run_us
                print(f"{n['node']:>4} {n['avg_current_ma']:>8.4f} {pct(ms['active']['time_us']):>8.3f} "
                      f"{pct(ms['deep_sleep']['time_us']):>6.2f} {pct(rs['listen']['time_us']):>7.2f} "
                      f"{pct(rs['receive']['time_us']):>6.3f} {pct(rs['tx']['time_us']):>6.3f} "
                      f"{rs['cca']['count'] / sec:>6.2f} {w['per_s']:>7.1f} {w['empty_ticks_per_s']:>7.1f} "
                      f"{w['unseen_frame_wakes'] / sec:>8.2f} "
                      + (f"{life:>7.3f}" if life is not None else f"{'-':>7}"))
        if not quiet:
            print()
    if len(paths) > 1:
        print(f"== {len(paths)} runs: average current per node (mA)")
        print(f"{'node':>4} {'median':>8} {'min':>8} {'max':>8}")
        for node in sorted(per_node):
            v = per_node[node]
            print(f"{node:>4} {statistics.median(v):>8.4f} {min(v):>8.4f} {max(v):>8.4f}")
        allv = [x for v in per_node.values() for x in v]
        print(f" all {statistics.median(allv):>8.4f} {min(allv):>8.4f} {max(allv):>8.4f}")
    print(f"checks: {'FAILED (' + str(failed) + ')' if failed else 'state times add up to the run time for every node'}")
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
