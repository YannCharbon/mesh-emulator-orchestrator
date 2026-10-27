#!/usr/bin/env python3
"""Generate a connected topology for wssimserver, deterministic from a seed.

usage: gen_topology.py <nodes> [--shape rgg|grid] [--degree D] [--seed S] [-o FILE]

Writes one edge per line, "a,b:rssi" (wssimserver's -g syntax, as read by
sim_utils/vtime/run.sh TOPO=<file>). Node 0 is the border router.

  rgg   random geometric graph (default): the nodes are placed uniformly in a
        unit square, the border router in the middle; two nodes are linked
        when they are closer than the radius r that gives a mean degree D
        (default 6). RSSI falls linearly with distance, from -2 dBm at
        distance 0 to -80 dBm at r: the range of dag_simple's links
        (-2 to -80 dBm; parents are refused below -80 dBm, DEVICE_MIN_SENS).
        Nodes left out of the border router's component are linked to their
        nearest node in it (RSSI -80 dBm at most).
  grid  a square grid (the last row may be partial), border router at a
        corner (node 0); 4-neighbour links with an RSSI drawn uniformly in
        [-60, -20] dBm, diagonal links in [-80, -60] dBm.

A summary (nodes, links, degree, hops from the border router) goes to stderr.
"""
import argparse, math, random, sys, collections


def rssi_at(d, r):
    return round(max(-80.0, min(-2.0, -2.0 - 78.0 * d / r)), 1)


def rgg(n, degree, rnd):
    pos = [(0.5, 0.5)] + [(rnd.random(), rnd.random()) for _ in range(n - 1)]
    r = math.sqrt(degree / (math.pi * max(n - 1, 1)))
    edges = {}
    for a in range(n):
        for b in range(a + 1, n):
            d = math.dist(pos[a], pos[b])
            if d < r:
                edges[(a, b)] = rssi_at(d, r)
    # Link every component that cannot reach the border router to it.
    while True:
        comp = component(n, edges, 0)
        if len(comp) == n:
            break
        outside = [x for x in range(n) if x not in comp]
        # the closest pair (inside, outside)
        d, a, b = min((math.dist(pos[a], pos[b]), a, b) for a in comp for b in outside)
        edges[(min(a, b), max(a, b))] = rssi_at(min(d, r), r)
    return edges


def grid(n, rnd):
    w = math.ceil(math.sqrt(n))
    edges = {}
    for i in range(n):
        x, y = i % w, i // w
        for dx, dy, lo, hi in ((1, 0, -60, -20), (0, 1, -60, -20), (1, 1, -80, -60), (-1, 1, -80, -60)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < w:
                j = ny * w + nx
                if j < n:
                    edges[(min(i, j), max(i, j))] = round(rnd.uniform(lo, hi), 1)
    return edges


def component(n, edges, start):
    adj = collections.defaultdict(list)
    for a, b in edges:
        adj[a].append(b)
        adj[b].append(a)
    seen = {start}
    todo = [start]
    while todo:
        x = todo.pop()
        for y in adj[x]:
            if y not in seen:
                seen.add(y)
                todo.append(y)
    return seen


def hops(n, edges):
    adj = collections.defaultdict(list)
    for a, b in edges:
        adj[a].append(b)
        adj[b].append(a)
    dist = {0: 0}
    q = collections.deque([0])
    while q:
        x = q.popleft()
        for y in adj[x]:
            if y not in dist:
                dist[y] = dist[x] + 1
                q.append(y)
    return dist


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('nodes', type=int, help='number of nodes, border router included')
    ap.add_argument('--shape', choices=('rgg', 'grid'), default='rgg')
    ap.add_argument('--degree', type=float, default=6.0, help='rgg: mean degree (default 6)')
    ap.add_argument('--seed', type=int, default=1)
    ap.add_argument('-o', '--output', help='output file (default stdout)')
    args = ap.parse_args()
    if not 2 <= args.nodes <= 4096:
        sys.exit('gen_topology.py: 2 to 4096 nodes')
    rnd = random.Random(args.seed)
    edges = rgg(args.nodes, args.degree, rnd) if args.shape == 'rgg' else grid(args.nodes, rnd)
    out = open(args.output, 'w') if args.output else sys.stdout
    out.write(f'# gen_topology.py {args.nodes} --shape {args.shape} --degree {args.degree} --seed {args.seed}\n')
    for (a, b), r in sorted(edges.items()):
        out.write(f'{a},{b}:{r}\n')
    if args.output:
        out.close()
    deg = collections.Counter()
    for a, b in edges:
        deg[a] += 1
        deg[b] += 1
    h = hops(args.nodes, edges)
    rs = sorted(edges.values())
    print(f'{args.nodes} nodes, {len(edges)} links, degree mean {2 * len(edges) / args.nodes:.1f} '
          f'min {min(deg[i] for i in range(args.nodes))} max {max(deg.values())}, '
          f'hops from BR max {max(h.values())} mean {sum(h.values()) / len(h):.1f}, '
          f'RSSI median {rs[len(rs) // 2]} dBm, connected: {len(h) == args.nodes}', file=sys.stderr)


if __name__ == '__main__':
    main()
