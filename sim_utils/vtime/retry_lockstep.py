#!/usr/bin/env python3
"""Retry lock-step statistics (stack GS12, docs/MAC_RETRY.md there) of runs
with a border router (node 0), test-udp uplinks to it and the central
medium's log (<run>/medium.log, wssimserver --medium-log).

  retry_lockstep.py [--from=S] [--gap=MS] [--names=a,b] <run> ... [-- <run> ...]

Per group of runs: uplinks sent / received at the border router, delay
(request sent -> received at the BR), collisions at the BR (frames to node 0
lost to interference, busy or captured, grouped into events of overlapping
frames), and chains: events less than GAP ms (default 200) after the
previous one at the BR. A repeat is an event that is not the first of its
chain (a retry colliding again). Uplinks from --from (200 s) to 30 s before
the last request."""
import os, re, sys, statistics

LOSS = {'interference', 'busy', 'captured'}
UDP = re.compile(r'test-udp (tx to|rx from) (\S+): ([QR]) (\d+)')


def t0_of(run):
    try:
        return float(open(os.path.join(run, 't0')).read().split()[0])
    except Exception:
        return 1700000000.0


def analyse(run, t_from, gap_ms):
    t0 = t0_of(run)
    sent, rx = {}, {}
    for f in os.listdir(run):
        m = re.fullmatch(r'node(\d+)\.log', f)
        if not m:
            continue
        i = int(m.group(1))
        for line in open(os.path.join(run, f), errors='replace'):
            if 'test-udp tx to' in line:
                u = UDP.search(line)
                if u and u.group(3) == 'Q':
                    sent[(i, int(u.group(4)))] = float(line.split()[0]) - t0
    for line in open(os.path.join(run, 'br.log'), errors='replace'):
        if 'test-udp rx from' in line:
            u = UDP.search(line)
            if u and u.group(3) == 'Q':
                h = u.group(2).rsplit(':', 1)[-1]
                rx.setdefault((int(h, 16), int(u.group(4))), float(line.split()[0]) - t0)
    end = max(sent.values(), default=0) - 30          # requests in the last 30 s may be in flight
    s = {k: t for k, t in sent.items() if t_from <= t < end}
    delays = [rx[k] - t for k, t in s.items() if k in rx]
    # collisions at the BR
    recs = []
    for line in open(os.path.join(run, 'medium.log')):
        p = line.split()
        if len(p) >= 5 and p[2] == '0' and p[4] in LOSS and int(p[0]) / 1e6 >= t_from:
            recs.append(int(p[0]))
    recs.sort()
    events = []
    for t in recs:
        if events and t - events[-1] < 10000:          # overlapping frames: one event
            continue
        events.append(t)
    chains, cur, longest = 0, 0, 0
    prev = None
    for t in events:
        if prev is None or t - prev > gap_ms * 1000:
            chains += 1
            cur = 1
        else:
            cur += 1
        longest = max(longest, cur)
        prev = t
    return dict(sent=len(s), recv=sum(1 for k in s if k in rx), delays=delays,
                events=len(events), chains=chains, longest=longest)


def pct(v, q):
    v = sorted(v)
    return v[min(len(v) - 1, int(q * len(v)))] if v else float('nan')


def main(a):
    t_from, gap = 200.0, 200.0
    names = None
    while a and a[0].startswith('--') and a[0] != '--':
        o = a.pop(0)
        if o.startswith('--from='):
            t_from = float(o.split('=')[1])
        elif o.startswith('--gap='):
            gap = float(o.split('=')[1])
        elif o.startswith('--names='):
            names = o.split('=')[1].split(',')
    groups, g = [], []
    for x in a:
        if x == '--':
            groups.append(g); g = []
        else:
            g.append(x)
    groups.append(g)
    print(f'{"group":10} {"runs":>4} {"sent":>6} {"lost":>5} {"lost%":>6} {"delay med":>9} {"p95":>7} {"max":>7} '
          f'{"coll.ev":>7} {"repeats":>7} {"rep/ev":>6} {"longest":>7}   per run: lost, repeats')
    for gi, g in enumerate(groups):
        R = [analyse(r, t_from, gap) for r in g]
        sent = sum(r['sent'] for r in R); recv = sum(r['recv'] for r in R)
        d = [x for r in R for x in r['delays']]
        ev = sum(r['events'] for r in R); rep = sum(r['events'] - r['chains'] for r in R)
        name = names[gi] if names and gi < len(names) else f'g{gi}'
        print(f'{name:10} {len(R):4} {sent:6} {sent - recv:5} {100 * (sent - recv) / max(sent, 1):5.2f}% '
              f'{1000 * statistics.median(d) if d else float("nan"):7.0f}ms {1000 * pct(d, .95):5.0f}ms {1000 * max(d, default=float("nan")):5.0f}ms '
              f'{ev:7} {rep:7} {rep / max(ev, 1):6.2f} {max(r["longest"] for r in R):7}   '
              + ' '.join(f'{r["sent"] - r["recv"]},{r["events"] - r["chains"]}' for r in R))


if __name__ == '__main__':
    main(sys.argv[1:])
