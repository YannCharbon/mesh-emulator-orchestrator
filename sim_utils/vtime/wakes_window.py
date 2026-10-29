#!/usr/bin/env python3
"""Wakes per second by cause, and average current, in a time window.

usage: wakes_window.py [--nodes 1-14] SHORT LONG [SHORT LONG ...]

SHORT and LONG are two virtual-time runs of the same seed and configuration
that differ only in their duration (run.sh, campaign.sh or test_lfn_join.sh
with POWER set). A run is deterministic, so the window between the two ends
is the difference of their power.json totals: for example 300 s and 3600 s
runs give the steady state from 300 s to 3600 s, which power.json alone does
not (it covers the whole run). Causes are those of the ledger
(docs/POWER_MODEL.md: tick, protocol, mac, fhss, radio, other, and frame).

Prints the median [min-max] over every node of every pair. Default nodes:
every node but the border router (node 0).
"""
import json, os, statistics, sys

CAUSES = ['tick', 'protocol', 'mac', 'fhss', 'radio', 'other', 'frame']


def load(path):
    if os.path.isdir(path):
        path = os.path.join(path, 'power.json')
    with open(path) as f:
        return json.load(f)


def parse_ids(spec):
    ids = set()
    for part in spec.split(','):
        a, _, b = part.partition('-')
        ids.update(range(int(a), int(b or a) + 1))
    return ids


def main(argv):
    nodes = None
    if len(argv) > 1 and argv[0] == '--nodes':
        nodes = parse_ids(argv[1])
        argv = argv[2:]
    if not argv or len(argv) % 2:
        sys.exit(__doc__)
    rows = []
    for short, long_ in zip(argv[0::2], argv[1::2]):
        a, b = load(short), load(long_)
        span = (b['run_time_us'] - a['run_time_us']) / 1e6
        if span <= 0:
            sys.exit(f'{long_} is not longer than {short}')
        na = {n['node']: n for n in a['nodes']}
        for n in b['nodes']:
            if (nodes is None and n['node'] == 0) or (nodes is not None and n['node'] not in nodes):
                continue
            wa, wb = na[n['node']]['wakes'], n['wakes']
            r = {'total': (wb['total'] - wa['total']) / span}
            for c in CAUSES:
                r[c] = (wb['wakes_with'].get(c, 0) - wa['wakes_with'].get(c, 0)) / span
            r['uA'] = (n['charge_uc'] - na[n['node']]['charge_uc']) / span
            rows.append(r)
    if not rows:
        sys.exit('no node selected')
    print(f'{len(rows)} node runs; per second in the window, median [min-max]')
    for k in ['total'] + CAUSES:
        v = [r[k] for r in rows]
        print(f'  wakes {k:9s} {statistics.median(v):9.2f} [{min(v):.2f}-{max(v):.2f}]')
    v = [r['uA'] for r in rows]
    print(f'  current (uA)    {statistics.median(v):9.2f} [{min(v):.2f}-{max(v):.2f}]')


if __name__ == '__main__':
    main(sys.argv[1:])
