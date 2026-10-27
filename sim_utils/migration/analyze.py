#!/usr/bin/env python3
"""Summarise one or more headless dag_simple runs and compare groups.

usage: analyze.py ref_run1 ref_run2 -- new_run1 new_run2
Per node: time (s after launch) of the Wi-SUN join milestones, the selected
Wi-SUN parent, the last RPL preferred parent; per run: message-type histogram
(numbers/addresses masked) and the BR's node list at the end.

Reads wall-clock runs (sim_utils/migration/headless_run.sh) and virtual-time
runs (sim_utils/vtime/run.sh) alike: every log line is "<seconds> <message>"
and t0 holds the start time. The routers are the node<i>.log files present.
The BR's node list comes from dbus_nodes.txt (D-Bus snapshots, wall clock) or,
without it, from the "brtree" lines of br.log (virtual time). The topology is
topology.txt ("a,b:rssi" per line) when the run has one, dag_simple otherwise.
"""
import os, re, sys, statistics, collections

ANSI = re.compile(r'\x1b\[[0-9;]*m')
MILESTONES = [
    ('parent', r'selected parent:'),
    ('auth_start', r'authentication start'),
    ('auth_ok', r'authentication success'),
    ('config', r'learn network configuration'),
    ('operational', r'operation start'),
    ('routing', r'Routing ready'),
    ('ready', r'ARM_NWK_BOOTSTRAP_READY'),
]

DAG_SIMPLE = '0,1 0,2 0,3 2,4 2,5 4,6 0,7 1,7 4,7 0,8 3,8 5,8 7,8 3,9 4,9 7,9 8,9 7,10 8,11 9,12 9,13 10,14'
LINKS = {frozenset(map(int, e.split(','))) for e in DAG_SIMPLE.split()}

def mask(s):
    s = re.sub(r'[0-9a-f]{2}(:[0-9a-f]{2}){7}', 'MAC', s)
    s = re.sub(r'\b(0x)?[0-9a-fA-F]*[0-9][0-9a-fA-F]*\b', 'N', s)
    return s

def node_ids(d):
    ids = []
    for f in os.listdir(d):
        m = re.fullmatch(r'node(\d+)\.log', f)
        if m:
            ids.append(int(m.group(1)))
    return sorted(ids)

def run_links(d):
    """The run's topology (set of frozenset edges) and its name."""
    try:
        txt = open(os.path.join(d, 'topology.txt')).read()
    except OSError:
        return LINKS, 'dag_simple'
    links = set()
    for e in txt.split():
        if e.startswith('#'):
            continue
        # wssimserver -g syntax: a group of IDs and ranges, all linked together
        group = set()
        for part in e.split(':')[0].split(','):
            lo, _, hi = part.partition('-')
            group.update(range(int(lo), int(hi or lo) + 1))
        links.update(frozenset((a, b)) for a in group for b in group if a < b)
    return links, 'dag_simple' if links == LINKS else 'topology'

def br_tree(d):
    """BR's view at its last snapshot: node -> RPL parent (node IDs)."""
    tree = {}
    try:
        # D-Bus "Nodes" printed by busctl: IIDs (U/L bit flipped), last byte = ID
        txt = open(os.path.join(d, 'dbus_nodes.txt')).read().split('== t=')[-1]
        for n, par in re.findall(r'8 3 2 3 4 5 6 0 (\d+) 1 "parent" ay 8 3 2 3 4 5 6 0 (\d+)', txt):
            tree[int(n)] = int(par)
        return tree
    except OSError:
        pass
    # brtree t=<s> node=<eui64> parent=<eui64> (wsbr.c, virtual-time builds)
    last_t = None
    try:
        for line in open(os.path.join(d, 'br.log'), errors='replace'):
            m = BRTREE.search(line)
            if not m:
                continue
            if m.group(1) != last_t:
                last_t = m.group(1)
                tree = {}
            if m.group(2) is not None:
                tree[mac_id(m.group(2))] = mac_id(m.group(3))
    except OSError:
        pass
    return tree

BRTREE = re.compile(r'brtree t=(\S+) (?:node=(\S+) parent=(\S+)|nodes=)')

def mac_id(mac):
    b = mac.split(':')
    return int(b[6], 16) << 8 | int(b[7], 16)

def run_summary(d):
    t0 = float(open(os.path.join(d, 't0')).read())
    nodes = {}
    hist = collections.Counter()
    for i in node_ids(d):
        p = os.path.join(d, f'node{i}.log')
        info = {k: None for k, _ in MILESTONES}
        info['wisun_parent'] = None
        info['rpl_parent'] = None
        for line in open(p, errors='replace'):
            line = ANSI.sub('', line.rstrip('\n'))
            try:
                ts, msg = line.split(' ', 1)
                t = float(ts) - t0
            except ValueError:
                continue
            if t > 295:   # runs differ in length; compare the first 295 s
                break
            if not msg.startswith('[DBG'):
                hist[mask(msg)] += 1
            for k, rx in MILESTONES:
                if info[k] is None and re.search(rx, msg):
                    info[k] = round(t, 1)
            m = re.search(r'selected parent:(\S+)', msg)
            if m:
                info['wisun_parent'] = m.group(1)[-2:]
            # RPL dump: preferred parent line starts with '*' after rank
            m = re.search(r'\[rplu\]:\s+\d+\*[0-9a-f]+ .* fe80::(\S+?)%', msg)
            if m:
                info['rpl_parent'] = m.group(1)
        nodes[i] = info
    tree = br_tree(d)
    for i in nodes:
        nodes[i]['br_tree_parent'] = tree.get(i)
    return nodes, hist

def fmt(v):
    return '-' if v is None else str(v)

groups = [[]]
for a in sys.argv[1:]:
    if a == '--':
        groups.append([])
    else:
        groups[-1].append(a)

all_runs = {}
for g in groups:
    for d in g:
        all_runs[d] = run_summary(d)

keys = [k for k, _ in MILESTONES]
for d, (nodes, hist) in all_runs.items():
    print(f'\n### {d}')
    print('node ' + ' '.join(f'{k:>11}' for k in keys) + '  wisun_par     rpl_parent br_tree')
    for i, info in nodes.items():
        print(f'{i:>4} ' + ' '.join(f'{fmt(info[k]):>11}' for k in keys)
              + f'  {fmt(info["wisun_parent"]):>9} {fmt(info["rpl_parent"]):>14} {fmt(info["br_tree_parent"])}')
    links, topo = run_links(d)
    bad = [(i, n['br_tree_parent']) for i, n in nodes.items()
           if n['br_tree_parent'] is not None and frozenset((i, n['br_tree_parent'])) not in links]
    print(f'parents that are not {topo} neighbours (scrambled node IDs): {bad or "none"}')
    joined = sum(1 for i in nodes.values() if i['ready'] is not None)
    print(f'joined (BOOTSTRAP_READY): {joined}/{len(nodes)}')

def stat(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return 'n=0'
    return f'n={len(vals)} med={statistics.median(vals):.1f} min={min(vals):.1f} max={max(vals):.1f}'

if len(groups) == 2:
    print('\n### group comparison (all nodes pooled)')
    for k in keys:
        for name, g in zip(('ref', 'new'), groups):
            vals = [n[k] for d in g for n in all_runs[d][0].values()]
            print(f'{k:>12} {name}: {stat(vals)}')
    print('\n### message types (per run average), largest relative differences')
    avg = []
    for g in groups:
        c = collections.Counter()
        for d in g:
            c.update(all_runs[d][1])
        avg.append({k: v / len(g) for k, v in c.items()})
    keys_all = set(avg[0]) | set(avg[1])
    only = [k for k in keys_all if (k in avg[0]) != (k in avg[1])]
    print('message types seen in only one group:', len(only))
    for k in sorted(only)[:30]:
        print(f'   {"ref" if k in avg[0] else "new"}: {avg[0].get(k, avg[1].get(k)):.1f}  {k}')
    rows = sorted(keys_all, key=lambda k: -(avg[0].get(k, 0) + avg[1].get(k, 0)))[:25]
    for k in rows:
        print(f'  ref {avg[0].get(k, 0):8.1f}  new {avg[1].get(k, 0):8.1f}  {k[:90]}')
