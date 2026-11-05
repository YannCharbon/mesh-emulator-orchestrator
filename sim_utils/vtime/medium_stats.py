#!/usr/bin/env python3
"""Statistics of the central radio medium (docs/MEDIUM.md), per run and per
group of runs, and the comparison of groups (e.g. legacy against central).

usage: medium_stats.py [options] RUN... [-- RUN... [-- ...]]

RUN is a run directory of sim_utils/vtime/run.sh (with medium.log, the
wssimserver --medium-log file, when the run has one) or a --medium-log file.
'--' separates groups; the first group is the baseline of the comparison.

From the medium log ("t_end_us src dst frame_id outcome rx_dbm sinr_min_db",
MEDIUM.md §7), per group (pooled over its runs):
  - counts per outcome and their share of the frames heard while listening
    (every outcome but not_listening);
  - the collision share: (interference + captured + busy) / heard; per run
    too (min, median, max);
  - per receiver (--receivers N: the N with the most collisions) and per link
    (--links N: the N with the most losses): heard, ok, the losses by cause,
    the delivery rate, the median SINR of the lost frames;
  - the server's CCA figures from simserver.log's end-of-run summary: the
    queries, and those with a frame on air at the radio (the server does not
    know the radio's CCA threshold, so not the busy answers themselves).
From the node logs (every run, legacy or central):
  - join milestones of every router (as sim_utils/migration/analyze.py), the
    routers ready (ARM_NWK_BOOTSTRAP_READY) at the end, the time the last
    one became ready, the border router's last node count (brtree);
  - the radios' loss lines ("was lost due to ..."), by cause;
  - test traffic (test-udp, app_wsbrd/test_traffic.c), when there is some:
    the requests the routers send (to the border router: the runners of the
    gate send no other) from --traffic-from to 30 s before the end, those the
    border router received, and those echoed back to their router.
With two groups or more, every later group is compared with the first: the
per-run figures (routers ready, median and last ready time, delivery) of each
run, and their median, against the first group's range (min..max): "in" or
"OUT"; and every join milestone of all routers pooled, with the two-sided
Mann-Whitney U test's p-value (normal approximation). With n runs per group,
a run from the same distribution falls outside the other group's range with
probability 2/(n + 1) (2/7 with 6 runs): one "OUT" run is expected by chance.

Options:
  --names A,B,...   group names (default: g1, g2, ...)
  --from S, --to S  medium-log outcomes whose frame ended in [S, S') only
                    (seconds since the run's start)
  --receivers N     receivers printed (default 10; 0: none)
  --links N         links printed (default 10; 0: none)
  --traffic-from S  test traffic counted from S (default 0)
  --per-run         also print every run's own figures

Standard library only. Exit status 0 (the figures are for a person to read;
the gate's criteria are in MEDIUM.md §8).
"""
import collections, math, os, re, statistics, sys

OUTCOMES = ['ok', 'noise', 'interference', 'captured', 'below_lock', 'busy',
            'not_listening', 'tx_abort', 'left']
LOSSES = ['noise', 'interference', 'captured', 'below_lock', 'busy', 'tx_abort', 'left']
COLLISIONS = ['interference', 'captured', 'busy']
MILESTONES = [   # as sim_utils/migration/analyze.py
    ('parent', r'selected parent:'),
    ('auth_ok', r'authentication success'),
    ('operational', r'operation start'),
    ('ready', r'ARM_NWK_BOOTSTRAP_READY'),
]
ANSI = re.compile(r'\x1b\[[0-9;]*m')
BRTREE = re.compile(r'brtree t=(\S+) nodes=(\d+)')
LOST = re.compile(r'was lost due to ([a-z_ ]+?)(?: \(|$|,)')
UDP = re.compile(r'test-udp (tx to|rx from) (\S+): ([QR]) (\d+)')


def fnum(s):
    try:
        v = float(s)
        return v if math.isfinite(v) else None
    except ValueError:
        return None


# ---------------------------------------------------------------- medium log

def medium_file(run):
    if os.path.isfile(run):
        return run
    p = os.path.join(run, 'medium.log')
    return p if os.path.isfile(p) else None


def read_medium(path, t_from, t_to, t0_us):
    """Counters of one medium log: totals, per receiver, per link."""
    tot = collections.Counter()
    rx = collections.defaultdict(collections.Counter)
    link = collections.defaultdict(collections.Counter)
    lost_sinr = collections.defaultdict(list)       # (src, dst) -> SINR of lost frames
    bad = 0
    with open(path, errors='replace') as f:
        for line in f:
            p = line.split()
            if len(p) < 5 or p[0].startswith('#'):
                continue
            try:
                t = int(p[0]); src = int(p[1]); dst = int(p[2])
            except ValueError:
                bad += 1
                continue
            if t > 10**15:              # absolute (epoch-based) times
                t -= t0_us
            out = p[4]
            if out not in OUTCOMES:
                bad += 1
                continue
            if t_from is not None and t < t_from * 1e6:
                continue
            if t_to is not None and t >= t_to * 1e6:
                continue
            tot[out] += 1
            rx[dst][out] += 1
            link[(src, dst)][out] += 1
            if out in LOSSES and len(p) > 6:
                s = fnum(p[6])
                if s is not None:
                    lost_sinr[(src, dst)].append(s)
    return {'tot': tot, 'rx': rx, 'link': link, 'lost_sinr': lost_sinr, 'bad': bad}


def heard(c):
    return sum(v for k, v in c.items() if k != 'not_listening')


def share(num, den):
    return num / den if den else float('nan')


def pct(x):
    return '-' if x is None or (isinstance(x, float) and math.isnan(x)) else f'{100 * x:.2f}%'


def server_summary(run):
    """Medium and CCA lines of simserver.log's end, with the CCA query and
    busy counts when the line gives them."""
    p = os.path.join(run, 'simserver.log') if os.path.isdir(run) else None
    if not p or not os.path.isfile(p):
        return [], None, None
    keep, q, b = [], None, None
    with open(p, errors='replace') as f:
        for line in f:
            l = ANSI.sub('', line.rstrip('\n'))
            if not re.search(r'medium|\bcca\b', l, re.I):
                continue
            keep.append(l)
            if re.search(r'\bcca\b', l, re.I):
                m = re.search(r'(\d+)\s+(?:cca\s+)?quer', l, re.I) or re.search(r'quer\w*\D{0,4}(\d+)', l, re.I) \
                    or re.search(r'\bcca\w*\D{0,4}(\d+)', l, re.I)
                # the server knows the energy, not the radio's threshold: it
                # counts the queries with a frame on air ("busy" if a later
                # summary gives it)
                n = re.search(r'(\d+)\s+busy', l, re.I) or re.search(r'busy\D{0,4}(\d+)', l, re.I) \
                    or re.search(r'(\d+) with a frame on air', l, re.I)
                if m:
                    q = int(m.group(1))
                if n:
                    b = int(n.group(1))
    return keep[-12:], q, b


# ---------------------------------------------------------------- node logs

def t0_of(run):
    try:
        return float(open(os.path.join(run, 't0')).read())
    except (OSError, ValueError):
        return 0.0


def log_lines(path, t0):
    with open(path, errors='replace') as f:
        for line in f:
            ts, _, msg = line.partition(' ')
            try:
                yield float(ts) - t0, ANSI.sub('', msg.rstrip('\n'))
            except ValueError:
                pass


def read_nodes(run, traffic_from):
    """Join milestones, loss lines and test traffic of a run directory."""
    if not os.path.isdir(run):
        return None
    t0 = t0_of(run)
    ids = sorted(int(m.group(1)) for f in os.listdir(run)
                 if (m := re.fullmatch(r'node(\d+)\.log', f)))
    if not ids:
        return None
    end = None
    try:
        for l in open(os.path.join(run, 'run_info.txt')):
            m = re.search(r'vtime_end=(\d+)', l)
            if m:
                end = float(m.group(1))
    except OSError:
        pass
    ms = {}
    lost = collections.Counter()
    req = {}                                        # (node, n) -> t of "Q n" sent to the BR
    echo = set()                                    # (node, n) echoed back
    for i in ids:
        info = {k: None for k, _ in MILESTONES}
        for t, m in log_lines(os.path.join(run, f'node{i}.log'), t0):
            for k, rx in MILESTONES:
                if info[k] is None and re.search(rx, m):
                    info[k] = t
            if 'was lost' in m:
                c = LOST.search(m)
                lost[c.group(1).strip() if c else 'unknown'] += 1
            if 'test-udp' in m:
                u = UDP.search(m)
                # routers send their requests to the border router only
                if u and u.group(1) == 'tx to' and u.group(3) == 'Q':
                    req[(i, int(u.group(4)))] = t
                elif u and u.group(1) == 'rx from' and u.group(3) == 'R':
                    echo.add((i, int(u.group(4))))
        ms[i] = info
    tree, br_rx = None, set()
    brlog = os.path.join(run, 'br.log')
    if os.path.isfile(brlog):
        node_addr = {}
        for t, m in log_lines(brlog, t0):
            b = BRTREE.search(m)
            if b:
                tree = int(b.group(2))
            if 'test-udp rx from' in m:
                u = UDP.search(m)
                if u and u.group(3) == 'Q':
                    a = u.group(2)
                    if a not in node_addr:
                        h = a.rsplit(':', 1)[-1]
                        node_addr[a] = int(h, 16) if re.fullmatch(r'[0-9a-f]{1,4}', h) else -1
                    br_rx.add((node_addr[a], int(u.group(4))))
    ready = [v['ready'] for v in ms.values() if v['ready'] is not None]
    t_hi = (end - 30) if end else float('inf')
    sent = {k for k, t in req.items() if traffic_from <= t < t_hi}
    return {
        'routers': len(ids), 'ms': ms, 'ready_n': len(ready),
        'ready_med': statistics.median(ready) if ready else None,
        'ready_last': max(ready) if len(ready) == len(ids) else None,
        'tree': tree, 'lost': lost,
        'udp_sent': len(sent), 'udp_up': len(sent & br_rx), 'udp_echo': len(sent & echo),
    }


# ---------------------------------------------------------------- output

def fmt(v, nd=1):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return '-'
    return f'{v:.{nd}f}' if isinstance(v, float) else str(v)


def rng(vals):
    v = [x for x in vals if x is not None and not (isinstance(x, float) and math.isnan(x))]
    if not v:
        return '-', None, None
    return f'min {fmt(min(v))} med {fmt(statistics.median(v))} max {fmt(max(v))}', min(v), max(v)


def merge(meds):
    tot = collections.Counter(); rx = collections.defaultdict(collections.Counter)
    link = collections.defaultdict(collections.Counter); sinr = collections.defaultdict(list)
    for m in meds:
        tot.update(m['tot'])
        for k, c in m['rx'].items():
            rx[k].update(c)
        for k, c in m['link'].items():
            link[k].update(c)
        for k, v in m['lost_sinr'].items():
            sinr[k].extend(v)
    return tot, rx, link, sinr


def row(name, c, n_runs, sinr=None):
    h = heard(c)
    loss = sum(c[k] for k in LOSSES)
    coll = sum(c[k] for k in COLLISIONS)
    cells = [f'{c[k] / n_runs:9.1f}' for k in OUTCOMES]
    s = f'{statistics.median(sinr):7.1f}' if sinr else f'{"-":>7}'
    return (f'{name:>9} {h / n_runs:9.1f} ' + ' '.join(cells)
            + f' {pct(share(c["ok"], h)):>8} {pct(share(coll, h)):>8}' + (f' {s}' if sinr is not None else ''))


HDR = (f'{"":>9} {"heard":>9} ' + ' '.join(f'{k[:9]:>9}' for k in OUTCOMES) + f' {"ok/heard":>8} {"coll.":>8}')


def print_group(name, runs, data, args):
    meds = [d['med'] for d in data if d['med']]
    print(f'\n=== {name}: {len(runs)} runs')
    if meds:
        n = len(meds)
        tot, rx, link, sinr = merge(meds)
        h = heard(tot)
        print(f'medium log ({n} runs; counts are per run on average; heard = every outcome but not_listening)')
        for k in OUTCOMES:
            print(f'  {k:>14} {tot[k] / n:12.1f}  {pct(share(tot[k], h)) if k != "not_listening" else "":>8}')
        coll = [share(sum(m['tot'][k] for k in COLLISIONS), heard(m['tot'])) for m in meds]
        okr = [share(m['tot']['ok'], heard(m['tot'])) for m in meds]
        print(f'  collision share (interference + captured + busy) / heard: pooled '
              f'{pct(share(sum(tot[k] for k in COLLISIONS), h))}; per run '
              f'{" ".join(pct(x) for x in coll)}')
        print(f'  delivery ok / heard: pooled {pct(share(tot["ok"], h))}; per run {" ".join(pct(x) for x in okr)}')
        bad = sum(m['bad'] for m in meds)
        if bad:
            print(f'  ({bad} lines not understood)')
        if args.receivers:
            print(f'\n per receiver, the {args.receivers} with the most collisions (per run on average)')
            print(HDR)
            for k in sorted(rx, key=lambda k: -sum(rx[k][o] for o in COLLISIONS))[:args.receivers]:
                print(row(str(k), rx[k], n))
        if args.links:
            print(f'\n per link src>dst, the {args.links} with the most losses (per run on average; last column: median SINR of the lost frames, dB)')
            print(HDR + f' {"sinr":>7}')
            for k in sorted(link, key=lambda k: -sum(link[k][o] for o in LOSSES))[:args.links]:
                if not sum(link[k][o] for o in LOSSES):
                    break
                print(row(f'{k[0]}>{k[1]}', link[k], n, sinr.get(k, [])))
    else:
        print('no medium log')
    cca = [(d['cca_q'], d['cca_b']) for d in data if d['cca_q'] is not None or d['cca_b'] is not None]
    if cca:
        print('\n CCA (simserver.log), per run: ' + '; '.join(
            f'{fmt(q)} queries, {fmt(b)} with a frame on air ({pct(share(b, q)) if q and b is not None else "-"})'
            for q, b in cca))
    for d in data:
        if d['summary'] and args.per_run:
            print(f'\n server summary, {d["run"]}:')
            for l in d['summary']:
                print('   ' + l)
    nodes = [d['nodes'] for d in data if d['nodes']]
    if nodes:
        print(f'\nnode logs ({len(nodes)} runs)')
        for k, _ in MILESTONES:
            vals = [v[k] for nd in nodes for v in nd['ms'].values()]
            print(f'  {k:>12} (all routers pooled): n={sum(v is not None for v in vals)} {rng(vals)[0]}')
        print(f'  routers ready at the end, per run: {" ".join(str(nd["ready_n"]) + "/" + str(nd["routers"]) for nd in nodes)}')
        print(f'  median ready time per run (s):     {" ".join(fmt(nd["ready_med"]) for nd in nodes)}')
        print(f'  last router ready per run (s):     {" ".join(fmt(nd["ready_last"]) for nd in nodes)}  ("-": not all ready)')
        print(f'  border router node count (brtree): {" ".join(fmt(nd["tree"]) for nd in nodes)}')
        lost = collections.Counter()
        for nd in nodes:
            lost.update(nd['lost'])
        if lost:
            print('  radio loss lines per run: ' + ', '.join(f'{k} {v / len(nodes):.1f}' for k, v in lost.most_common()))
        if any(nd['udp_sent'] for nd in nodes):
            print(f'  test traffic to the border router from {args.traffic_from:g} s, per run (sent / received / echoed back):')
            print('    ' + '  '.join(f'{nd["udp_sent"]}/{nd["udp_up"]}/{nd["udp_echo"]}' for nd in nodes))
            s = sum(nd['udp_sent'] for nd in nodes)
            print(f'    pooled: received {pct(share(sum(nd["udp_up"] for nd in nodes), s))}, '
                  f'echoed back {pct(share(sum(nd["udp_echo"] for nd in nodes), s))}')
    if args.per_run:
        for d in data:
            if d['med']:
                c = d['med']['tot']
                print(f'  {d["run"]}: ' + ' '.join(f'{k}={c[k]}' for k in OUTCOMES if c[k]))


def run_metrics(d):
    nd, m = d['nodes'], d['med']
    out = {}
    if nd:
        out['routers ready'] = nd['ready_n']
        out['median ready (s)'] = nd['ready_med']
        out['last ready (s)'] = nd['ready_last']
        if nd['udp_sent']:
            out['UDP received'] = share(nd['udp_up'], nd['udp_sent'])
            out['UDP echoed'] = share(nd['udp_echo'], nd['udp_sent'])
    if m:
        out['medium ok/heard'] = share(m['tot']['ok'], heard(m['tot']))
        out['collision share'] = share(sum(m['tot'][k] for k in COLLISIONS), heard(m['tot']))
    return out


def mann_whitney(a, b):
    """Two-sided p-value of the Mann-Whitney U test (normal approximation,
    tie correction): are a and b drawn from the same distribution?"""
    a = [x for x in a if x is not None]; b = [x for x in b if x is not None]
    n1, n2 = len(a), len(b)
    if n1 < 2 or n2 < 2:
        return None
    allv = sorted((v, g) for g, vs in ((0, a), (1, b)) for v in vs)
    ranks, i, ties = [0.0] * len(allv), 0, 0.0
    while i < len(allv):
        j = i
        while j + 1 < len(allv) and allv[j + 1][0] == allv[i][0]:
            j += 1
        for k in range(i, j + 1):
            ranks[k] = (i + j) / 2 + 1
        t = j - i + 1
        ties += t ** 3 - t
        i = j + 1
    r1 = sum(r for r, (v, g) in zip(ranks, allv) if g == 0)
    u = r1 - n1 * (n1 + 1) / 2
    n = n1 + n2
    var = n1 * n2 / 12 * ((n + 1) - ties / (n * (n - 1)))
    if var <= 0:
        return 1.0
    z = (abs(u - n1 * n2 / 2) - 0.5) / math.sqrt(var)
    return min(1.0, math.erfc(max(z, 0) / math.sqrt(2)))


def compare(names, groups, data):
    base = [run_metrics(d) for d in data[0]]
    for gi in range(1, len(groups)):
        print(f'\n=== {names[gi]} against {names[0]} (each run against {names[0]}\'s range)')
        other = [run_metrics(d) for d in data[gi]]
        keys = [k for k in (base[0] if base else {}) if any(k in o for o in other)]
        for k in keys:
            israte = k.startswith('UDP') or 'medium' in k or 'collision' in k
            f = (lambda v: pct(v)) if israte else (lambda v: fmt(v))
            bv = [b.get(k) for b in base]
            ov = [o.get(k) for o in other]
            _, lo, hi = rng(bv)
            marks = []
            for v in ov:
                if k == 'last ready (s)' and (v is None or None in bv):
                    # None: not every router became ready
                    ok = v is None and None in bv or v is not None and (lo is None or v <= hi)
                    marks.append(f'{f(v)} ' + ('in' if ok else 'OUT'))
                elif v is None or lo is None:
                    marks.append(f'{f(v)}')
                else:
                    marks.append(f'{f(v)} ' + ('in' if lo <= v <= hi else 'OUT'))
            print(f'  {k:>18}: {names[0]} {" ".join(f(v) for v in bv)}  |  {names[gi]} {", ".join(marks)}')
            om = [v for v in ov if v is not None]
            if om and lo is not None:
                med = statistics.median(om)
                print(f'  {"":>18}  {names[gi]} median {f(med)}: {"in" if lo <= med <= hi else "OUT"} {names[0]}\'s range')
        # pooled milestones, as analyze.py's group comparison
        for k, _ in MILESTONES:
            a = [v[k] for d in data[0] if d['nodes'] for v in d['nodes']['ms'].values()]
            b = [v[k] for d in data[gi] if d['nodes'] for v in d['nodes']['ms'].values()]
            p = mann_whitney(a, b)
            print(f'  {k:>18} pooled: {names[0]} {rng(a)[0]}  |  {names[gi]} {rng(b)[0]}'
                  f'  |  Mann-Whitney p = {fmt(p, 3)}')
        print('  (with n runs per group, a run of the same distribution falls outside the other group\'s\n'
              '   min..max with probability 2/(n+1): read "OUT" with the median line and the p-values;\n'
              '   p < 0.05 on a pooled milestone: the groups differ for that milestone)')


def main(argv):
    class A:
        pass
    args = A()
    args.names, args.t_from, args.t_to = None, None, None
    args.receivers, args.links, args.traffic_from, args.per_run = 10, 10, 0.0, False
    groups = [[]]
    it = iter(argv)
    for a in it:
        if a == '--':
            groups.append([])
        elif a in ('-h', '--help'):
            print(__doc__); return 0
        elif a == '--per-run':
            args.per_run = True
        elif a.split('=')[0] in ('--names', '--from', '--to', '--receivers', '--links', '--traffic-from'):
            k, _, v = a.partition('=')
            if not _:
                v = next(it)
            if k == '--names':
                args.names = v.split(',')
            elif k == '--from':
                args.t_from = float(v)
            elif k == '--to':
                args.t_to = float(v)
            elif k == '--receivers':
                args.receivers = int(v)
            elif k == '--links':
                args.links = int(v)
            else:
                args.traffic_from = float(v)
        elif a.startswith('-'):
            print(f'medium_stats.py: unknown option {a}', file=sys.stderr); return 2
        else:
            groups[-1].append(a)
    groups = [g for g in groups if g]
    if not groups:
        print(__doc__, file=sys.stderr); return 2
    names = args.names or [f'g{i + 1}' for i in range(len(groups))]
    if len(names) < len(groups):
        names += [f'g{i + 1}' for i in range(len(names), len(groups))]
    data = []
    for g in groups:
        dg = []
        for r in g:
            if not os.path.exists(r):
                print(f'medium_stats.py: no {r}', file=sys.stderr); return 2
            d = {'run': r}
            run_dir = r if os.path.isdir(r) else os.path.dirname(r)
            mf = medium_file(r)
            d['med'] = read_medium(mf, args.t_from, args.t_to, t0_of(run_dir) * 1e6) if mf else None
            d['summary'], d['cca_q'], d['cca_b'] = server_summary(run_dir)
            d['nodes'] = read_nodes(run_dir, args.traffic_from)
            dg.append(d)
        data.append(dg)
    if args.t_from is not None or args.t_to is not None:
        print(f'medium-log window: [{fmt(args.t_from)}, {fmt(args.t_to)}) s')
    for name, g, d in zip(names, groups, data):
        print_group(name, g, d, args)
    if len(groups) > 1:
        compare(names, groups, data)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
