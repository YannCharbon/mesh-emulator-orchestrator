#!/usr/bin/env python3
"""Building topology for the emulator (docs/SCALE.md §2): N nodes on the floors
of a building, links from a log-distance path loss at 915 MHz.

usage: gen_building.py --nodes N [-o topology.txt] [--positions FILE] [options]

Layout: --floors floors (default: about 200 nodes per floor), each a grid
twice as long as wide with --spacing m between nodes, floors --floor-height m
apart. Nodes fill floor 0 first, row by row; node 0 (the gateway) is then
swapped with the node nearest the middle of floor 0. Node IDs are 0..N-1.

Received power, the same in both directions:

    d   = max(1 m, 3-D distance)
    PL  = 31.7 + 10 * pl_exp * log10(d) + wall_db_per_m * d_horizontal + floor_db * |floors apart|
    rssi = tx_dbm - PL

and a link where rssi >= --min-rssi (pairs below it do not hear each other at
all, not even as interferers).

Output (-o, default stdout): one link per line, "A,B:RSSI" with A < B and
RSSI in dBm rounded to 0.1, sorted. It is both a wssimserver --group-file and
a TOPO= file of sim_utils/vtime/run.sh: each link is given once, so it holds
in both directions. --positions writes "ID X Y Z FLOOR" per node (metres).
A summary (JSON: links, degree, hops from node 0) goes to stderr.

The figures in docs/SCALE.md use the defaults. Deterministic: no random draw.
"""
import argparse
import collections
import json
import math
import sys


def building(n, floors, spacing, floor_height):
    """[(x, y, z, floor)] of the n nodes, the grid's rows and columns."""
    per_floor = math.ceil(n / floors)
    cols = math.ceil(math.sqrt(per_floor * 2))      # floors twice as long as wide
    rows = math.ceil(per_floor / cols)
    pos = []
    for i in range(n):
        f, k = divmod(i, per_floor)
        r, c = divmod(k, cols)
        pos.append((c * spacing, r * spacing, f * floor_height, f))
    # gateway in the middle of floor 0: swap node 0 with the node nearest the centre
    cx, cy = (cols - 1) * spacing / 2, (rows - 1) * spacing / 2
    j = min(range(min(per_floor, n)), key=lambda i: (pos[i][0] - cx) ** 2 + (pos[i][1] - cy) ** 2)
    pos[0], pos[j] = pos[j], pos[0]
    return pos, rows, cols


def links(pos, a):
    """{(i, j): rssi} for i < j; a has tx_dbm, pl_exp, wall_db_per_m, floor_db, min_rssi."""
    edges = {}
    n = len(pos)
    for i in range(n):
        xi, yi, zi, fi = pos[i]
        for j in range(i + 1, n):
            xj, yj, zj, fj = pos[j]
            dh = math.hypot(xi - xj, yi - yj)
            d = max(1.0, math.sqrt(dh * dh + (zi - zj) ** 2))
            pl = 31.7 + 10 * a.pl_exp * math.log10(d) + a.wall_db_per_m * dh + a.floor_db * abs(fi - fj)
            rssi = a.tx_dbm - pl
            if rssi >= a.min_rssi:
                edges[(i, j)] = round(rssi, 1)
    return edges


def hops_from(n, edges, src):
    """{node: hops from src} over the links (BFS)."""
    adj = collections.defaultdict(list)
    for a, b in edges:
        adj[a].append(b)
        adj[b].append(a)
    dist = {src: 0}
    dq = collections.deque([src])
    while dq:
        x = dq.popleft()
        for y in adj[x]:
            if y not in dist:
                dist[y] = dist[x] + 1
                dq.append(y)
    return dist


def summary(n, floors, rows, cols, edges):
    deg = collections.Counter()
    for x, y in edges:
        deg[x] += 1
        deg[y] += 1
    hops = hops_from(n, edges, 0)
    return {"nodes": n, "floors": floors, "grid": f"{rows}x{cols}", "links": len(edges),
            "degree_mean": round(2 * len(edges) / n, 1), "degree_min": min(deg[i] for i in range(n)),
            "degree_max": max(deg[i] for i in range(n)), "reachable": len(hops),
            "hops_max": max(hops.values())}


def write_topology(f, edges):
    for (x, y), r in sorted(edges.items()):
        f.write(f"{x},{y}:{r}\n")


def add_args(p):
    """The building's options (shared with run_scale.py)."""
    p.add_argument("--floors", type=int, default=0, help="floors (default: about 200 nodes per floor)")
    p.add_argument("--spacing", type=float, default=8.0, help="m between nodes on a floor (8)")
    p.add_argument("--floor-height", type=float, default=3.5, help="m between floors (3.5)")
    p.add_argument("--tx-dbm", type=float, default=14.0, help="transmit power in dBm (14)")
    p.add_argument("--pl-exp", type=float, default=3.0, help="path-loss exponent n (3.0)")
    p.add_argument("--wall-db-per-m", type=float, default=1.8, help="internal walls, dB per horizontal m (1.8)")
    p.add_argument("--floor-db", type=float, default=18.0, help="dB per floor crossed (18)")
    p.add_argument("--min-rssi", type=float, default=-100.0, help="no link below this, dBm (-100)")


def generate(n, a):
    """(edges, positions, summary) of a building of n nodes with a's options."""
    if n < 2 or n > 4096:
        sys.exit("--nodes: 2 to 4096")
    floors = a.floors or max(1, round(n / 200))
    pos, rows, cols = building(n, floors, a.spacing, a.floor_height)
    edges = links(pos, a)
    return edges, pos, summary(n, floors, rows, cols, edges)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--nodes", type=int, required=True, help="number of nodes, 2 to 4096")
    p.add_argument("-o", "--out", help="topology file (default: stdout)")
    p.add_argument("--positions", help="also write each node's position to this file")
    add_args(p)
    a = p.parse_args()
    edges, pos, s = generate(a.nodes, a)
    if a.out:
        with open(a.out, "w") as f:
            write_topology(f, edges)
    else:
        write_topology(sys.stdout, edges)
    if a.positions:
        with open(a.positions, "w") as f:
            f.write("# id x_m y_m z_m floor\n")
            for i, (x, y, z, fl) in enumerate(pos):
                f.write(f"{i} {x:g} {y:g} {z:g} {fl}\n")
    print("topology:", json.dumps(s), file=sys.stderr)


if __name__ == "__main__":
    main()
