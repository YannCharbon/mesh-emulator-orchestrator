#!/usr/bin/env python3
"""Scale run: N load clients (rif_load.c) in a multi-floor building, against
wssimserver in virtual time with the central medium (docs/SCALE.md).

usage: run_scale.py --server build-vt/wssimserver --client <rif_load> --nodes N
                    --duration SECONDS --out DIR [options]

The topology is a building of --floors floors, each a grid of nodes spaced
--spacing m (floors --floor-height m apart). Received power from a log-distance
path loss at 915 MHz:

    PL(d) = 31.7 + 10 n log10(d) + wall_db_per_m * d_horizontal + floor_db * |floors apart|

    rssi = tx_dbm - PL(d)

and a link (wssimserver -g A,B:RSSI, both directions) where rssi >= --min-rssi.
The gateway (node 0) is in the middle of floor 0. Node IDs are 0..N-1.

Results go to DIR: server.log, topology.txt, nodes/<id>.txt (each client's
output), result.json (wall time, CPU, memory, flood coverage and latency),
and a one-line summary on stdout.
"""
import argparse, collections, json, math, os, random, re, resource, shutil, signal, statistics, subprocess, sys, tempfile, time


def building(n, floors, spacing, floor_height):
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


def proc_mem_kb(pid):
    """(RSS, PSS) in kB of a process, or None"""
    try:
        with open(f"/proc/{pid}/smaps_rollup") as f:
            t = f.read()
        rss = int(re.search(r"^Rss:\s+(\d+)", t, re.M).group(1))
        pss = int(re.search(r"^Pss:\s+(\d+)", t, re.M).group(1))
        return rss, pss
    except (OSError, AttributeError):
        return None


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--server", required=True)
    p.add_argument("--client", required=True)
    p.add_argument("--nodes", type=int, required=True)
    p.add_argument("--duration", type=float, required=True, help="virtual seconds")
    p.add_argument("--out", required=True)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--floors", type=int, default=0, help="default: about 200 nodes per floor")
    p.add_argument("--spacing", type=float, default=8.0)
    p.add_argument("--floor-height", type=float, default=3.5)
    p.add_argument("--tx-dbm", type=float, default=14.0)
    p.add_argument("--pl-exp", type=float, default=3.0)
    p.add_argument("--wall-db-per-m", type=float, default=1.8)
    p.add_argument("--floor-db", type=float, default=18.0)
    p.add_argument("--min-rssi", type=float, default=-100.0)
    p.add_argument("--alarms", type=int, default=2, help="alarms originated by random nodes during the run")
    p.add_argument("--power", help="power profile (--power of wssimserver)")
    p.add_argument("--medium-log", action="store_true")
    p.add_argument("--client-opts", default="", help="more options for every client")
    p.add_argument("--server-opts", default="", help="more options for the server")
    p.add_argument("--topology-only", action="store_true")
    a = p.parse_args()

    n = a.nodes
    if n < 2 or n > 4096:
        sys.exit("--nodes: 2 to 4096")
    floors = a.floors or max(1, round(n / 200))
    os.makedirs(a.out, exist_ok=True)
    pos, rows, cols = building(n, floors, a.spacing, a.floor_height)
    edges = links(pos, a)
    deg = collections.Counter()
    for x, y in edges:
        deg[x] += 1
        deg[y] += 1
    hops = hops_from(n, edges, 0)
    topo = {"nodes": n, "floors": floors, "grid": f"{rows}x{cols}", "links": len(edges),
            "degree_mean": round(2 * len(edges) / n, 1), "degree_min": min(deg[i] for i in range(n)),
            "degree_max": max(deg[i] for i in range(n)), "reachable": len(hops),
            "hops_max": max(hops.values())}
    with open(os.path.join(a.out, "topology.txt"), "w") as f:
        for (x, y), r in sorted(edges.items()):
            f.write(f"{x},{y}:{r}\n")
    print("topology:", json.dumps(topo), file=sys.stderr)
    if a.topology_only:
        return

    rnd = random.Random(a.seed)
    alarms = collections.defaultdict(list)
    for _ in range(a.alarms):
        alarms[rnd.randrange(1, n)].append(round(rnd.uniform(5, max(6, a.duration - 10)), 3))

    sock = tempfile.mktemp(suffix=".sock", prefix="wssim-scale-", dir="/tmp")
    nodes_dir = os.path.join(a.out, "nodes")
    shutil.rmtree(nodes_dir, ignore_errors=True)
    os.makedirs(nodes_dir)
    cmd = ["/usr/bin/time", "-f", "%e %U %S %M", "-o", os.path.join(a.out, "server_time.txt"),
           a.server, f"--vtime={n}", f"--vtime-end={a.duration}"]
    for (x, y), r in sorted(edges.items()):
        cmd += ["-g", f"{x},{y}:{r}"]
    if a.power:
        cmd += [f"--power={a.power}", f"--power-out={os.path.join(a.out, 'power.json')}"]
    if a.medium_log:
        cmd += [f"--medium-log={os.path.join(a.out, 'medium.log')}"]
    cmd += a.server_opts.split() + [sock]
    env = dict(os.environ, WSSIM_SEED=str(a.seed))
    t0 = time.monotonic()
    slog = open(os.path.join(a.out, "server.log"), "w")
    server = subprocess.Popen(cmd, stdout=slog, stderr=subprocess.STDOUT, env=env)
    clients = []
    try:
        while not os.path.exists(sock):
            if server.poll() is not None:
                sys.exit(f"server exited ({server.returncode}), see {a.out}/server.log")
            time.sleep(0.02)
        t_listen = time.monotonic()
        for i in range(n):
            c = [a.client, "--sock", sock, "--id", str(i), "--out", os.path.join(nodes_dir, f"{i}.txt"), "--origins"]
            for t in sorted(alarms.get(i, [])):
                c += ["--alarm-at", str(t)]
            c += a.client_opts.split()
            clients.append(subprocess.Popen(c, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL))
        t_spawned = time.monotonic()
        # memory: sample while the run goes on
        srv_pid = None
        mem = {"server_rss_kb": 0, "clients_rss_kb": 0, "clients_pss_kb": 0}
        next_sample = time.monotonic()
        while server.poll() is None:
            if time.monotonic() >= next_sample:
                if srv_pid is None:
                    try:
                        srv_pid = int(open(f"/proc/{server.pid}/task/{server.pid}/children").read().split()[0])
                    except (OSError, IndexError, ValueError):
                        pass
                if srv_pid:
                    m = proc_mem_kb(srv_pid)
                    if m:
                        mem["server_rss_kb"] = max(mem["server_rss_kb"], m[0])
                rs = ps = 0
                for c in clients:
                    m = proc_mem_kb(c.pid)
                    if m:
                        rs += m[0]
                        ps += m[1]
                mem["clients_rss_kb"] = max(mem["clients_rss_kb"], rs)
                mem["clients_pss_kb"] = max(mem["clients_pss_kb"], ps)
                next_sample = time.monotonic() + 2.0
            time.sleep(0.05)
        t_end = time.monotonic()
        for c in clients:
            try:
                c.wait(timeout=30)
            except subprocess.TimeoutExpired:
                c.kill()
                c.wait()
    except BaseException:
        for c in clients:
            if c.poll() is None:
                c.kill()
        if server.poll() is None:
            server.send_signal(signal.SIGINT)
            server.wait()
        raise
    finally:
        slog.close()
        if os.path.exists(sock):
            os.unlink(sock)
    ru = resource.getrusage(resource.RUSAGE_CHILDREN)
    st = open(os.path.join(a.out, "server_time.txt")).read().split()
    srv_wall, srv_user, srv_sys, srv_maxrss = float(st[-4]), float(st[-3]), float(st[-2]), int(st[-1])
    log = open(os.path.join(a.out, "server.log")).read()
    m = re.search(r"wall ([\d.]+) s, ([\d.]+) wall s per simulated s", log)
    m2 = re.search(r"reached ([\d.]+) s, (\d+) wakes, (\d+) frames sent, (\d+) frames delivered", log)
    outcomes = dict(re.findall(r"medium:\s+(\w+)\s+(\d+)$", log, re.M))

    # floods: coverage and latency
    origin_t = {}
    first = collections.defaultdict(dict)
    summ = collections.Counter()
    for i in range(n):
        try:
            lines = open(os.path.join(nodes_dir, f"{i}.txt")).read().splitlines()
        except OSError:
            continue
        for ln in lines:
            f = ln.split()
            if f[0] == "F":
                first[(f[1], int(f[2]), int(f[3]))][i] = (int(f[4]), int(f[5]))
            elif f[0] == "O":
                origin_t[(f[1], int(f[2]), int(f[3]))] = int(f[4])
            elif f[0] == "S":
                for kv in f[1:]:
                    k, v = kv.split("=")
                    if k != "id":
                        summ[k] += int(v)
    floods = []
    for key, t0f in sorted(origin_t.items(), key=lambda x: x[1]):
        got = first.get(key, {})
        lat = sorted((t - t0f) / 1e6 for t, _ in got.values())
        if a.duration * 1e6 - t0f < 30e6:       # too close to the end to complete: not counted
            continue
        floods.append({"kind": key[0], "origin": key[1], "seq": key[2], "t_s": t0f / 1e6,
                       "coverage": round(len(got) / (n - 1), 4),
                       "latency_median_s": round(statistics.median(lat), 3) if lat else None,
                       "latency_max_s": round(lat[-1], 3) if lat else None,
                       "hops_max": max((h for _, h in got.values()), default=None)})
    res = {
        "nodes": n, "duration_vs": a.duration, "seed": a.seed, "topology": topo, "power": bool(a.power),
        "medium_log": a.medium_log,
        "wall_run_s": float(m.group(1)) if m else None,
        "wall_per_vs": float(m.group(2)) if m else None,
        "wakes": int(m2.group(2)) if m2 else None, "frames_sent": int(m2.group(3)) if m2 else None,
        "frames_delivered": int(m2.group(4)) if m2 else None,
        "outcomes": {k: int(v) for k, v in outcomes.items()},
        "server_cpu_s": round(srv_user + srv_sys, 2), "server_user_s": srv_user, "server_sys_s": srv_sys,
        "server_maxrss_mb": round(srv_maxrss / 1024, 1),
        "clients_cpu_s": round(ru.ru_utime + ru.ru_stime - srv_user - srv_sys, 2),
        "clients_rss_mb": round(mem["clients_rss_kb"] / 1024, 1),
        "clients_pss_mb": round(mem["clients_pss_kb"] / 1024, 1),
        "setup_s": round(t_spawned - t_listen, 2), "total_wall_s": round(t_end - t0, 2),
        "server_status": server.returncode,
        "clients": dict(summ), "floods": floods,
    }
    json.dump(res, open(os.path.join(a.out, "result.json"), "w"), indent=1)
    cov = [f["coverage"] for f in floods]
    print(f"nodes={n} vs={a.duration:g} wall_run={res['wall_run_s']} s wall/vs={res['wall_per_vs']} "
          f"server_cpu={res['server_cpu_s']} s clients_cpu={res['clients_cpu_s']} s "
          f"server_rss={res['server_maxrss_mb']} MB clients_pss={res['clients_pss_mb']} MB total_wall={res['total_wall_s']} s "
          f"wakes={res['wakes']} frames={res['frames_sent']} floods={len(floods)} "
          f"coverage_min={min(cov) if cov else None} status={server.returncode}")
    sys.exit(0 if server.returncode == 0 else 1)


if __name__ == "__main__":
    main()
