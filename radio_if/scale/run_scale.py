#!/usr/bin/env python3
"""Scale run: N load clients (rif_load.c) in a multi-floor building, against
wssimserver in virtual time with the central medium (docs/SCALE.md).

usage: run_scale.py --server build-vt/wssimserver --client <rif_load> --nodes N
                    --duration SECONDS --out DIR [options]

The topology is the building of gen_building.py (same options: --floors,
--spacing, --floor-height, --tx-dbm, --pl-exp, --wall-db-per-m, --floor-db,
--min-rssi), given to wssimserver with --group-file. The gateway (node 0) is
in the middle of floor 0. Node IDs are 0..N-1.

Results go to DIR: server.log, topology.txt, nodes/<id>.txt (each client's
output), result.json (wall time, CPU, memory, flood coverage and latency),
and a one-line summary on stdout.
"""
import argparse, collections, json, math, os, random, re, resource, shutil, signal, statistics, subprocess, sys, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gen_building     # noqa: E402  the building (radio_if/scale/gen_building.py)


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
    gen_building.add_args(p)     # --floors, --spacing, ... (gen_building.py)
    p.add_argument("--alarms", type=int, default=2, help="alarms originated by random nodes during the run")
    p.add_argument("--power", help="power profile (--power of wssimserver)")
    p.add_argument("--medium-log", action="store_true")
    p.add_argument("--client-opts", default="", help="more options for every client")
    p.add_argument("--server-opts", default="", help="more options for the server")
    p.add_argument("--topology-only", action="store_true")
    p.add_argument("--positions", action="store_true",
                   help="links from the server's propagation model (wssimserver --positions, docs/MEDIUM.md §11, "
                        "its default indoor model unless --server-opts sets --prop-*) instead of the generator's: "
                        "writes positions.txt, no topology file; the server's --prop-tx-dbm is --tx-dbm")
    p.add_argument("--group-args", action="store_true",
                   help="topology as -g arguments instead of --group-file (limited to about 2000 nodes by ARG_MAX)")
    a = p.parse_args()

    n = a.nodes
    os.makedirs(a.out, exist_ok=True)
    edges, pos, topo = gen_building.generate(n, a)
    with open(os.path.join(a.out, "topology.txt"), "w") as f:
        gen_building.write_topology(f, edges)
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
    if a.positions:
        with open(os.path.join(a.out, "positions.txt"), "w") as f:
            f.write("# id x_m y_m z_m floor\n")
            for i, (x, y, z, fl) in enumerate(pos):
                f.write(f"{i} {x:g} {y:g} {z:g} {fl}\n")
        cmd += [f"--positions={os.path.join(a.out, 'positions.txt')}", f"--prop-tx-dbm={a.tx_dbm:g}"]
    elif a.group_args:
        for (x, y), r in sorted(edges.items()):
            cmd += ["-g", f"{x},{y}:{r}"]
    else:
        cmd += [f"--group-file={os.path.join(a.out, 'topology.txt')}"]
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
