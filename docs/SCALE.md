# Scale: networks of 1000+ nodes through the radio interface

This document gives the cost of large virtual-time runs through the stack-neutral radio interface (`docs/RADIO_INTERFACE.md`) with the central medium (`docs/MEDIUM.md`). It explains how to run the scale scenario, lists the limits and records the server changes made for scale.

## 1. Summary

- Runs of **4000 nodes** work (the node ID range allows 4096).
- Wall time grows linearly with the number of **wakes**. One wake costs **7–12 µs** of wall time on the reference PC. The cost per wake rises slowly with the number of processes, mostly through caches and the scheduler.
- For a **1000-node building for one virtual hour** with the light load (§3), a measured run took **123 s** of wall time, 12 MB in the server and 119 MB for the 1000 load clients. With the heavy load, the estimate is about 9 minutes.
- The limiting cost is the **exchange between processes**: one process per node (decision DI2), and two scheduler wake-ups per node wake (server to client, then back). The server's own computation is about 10 % of the wall time after the changes in §5. The clients' CPU time is larger than the server's.

## 2. Running the scale scenario

```bash
cc -O2 -Wall -o /tmp/rif_load radio_if/scale/rif_load.c
taskset -c 2,3 python3 radio_if/scale/run_scale.py --server build-vt/wssimserver --client /tmp/rif_load \
    --nodes 1000 --duration 180 --out runs/scale1000 --power examples/power/efr32fg25-lfn.ini
```

`run_scale.py` does the following:

1. It generates the building with `gen_building.py` (below) and writes `topology.txt`.
2. It starts `wssimserver --vtime=N` with `--group-file=topology.txt`, under `/usr/bin/time`, and then starts the N clients.
3. It samples the memory of the server and of the clients every 2 s.
4. It writes `result.json` and prints a one-line summary.

`result.json` holds:

- the wall time per virtual second;
- the server's user and system CPU time and the clients' CPU time;
- the memory: the server's maximum RSS and the clients' total PSS;
- the counts of wakes and frames, and the medium's outcomes;
- for each flood, the coverage (the share of nodes reached), the latency and the number of hops.

`--topology-only` stops after the topology. Other options:

| Option | Meaning |
| --- | --- |
| `--medium-log` | write the medium's per-frame log; about 12.5 MB per 180 virtual s at 1000 nodes with the light load |
| `--alarms K` | number of alarm floods |
| `--client-opts`, `--server-opts` | options passed to every client or to the server |
| `--seed` | the run's seed (`WSSIM_SEED`) |
| `--floors`, `--spacing`, ... | the building, as `gen_building.py` (below) |

**Topology.** The building has `--floors` floors (by default about 200 nodes per floor). Each floor is a grid with `--spacing` 8 m between nodes, and floors are 3.5 m apart. Node 0, the gateway, is in the middle of floor 0. Received power is computed as follows:

- `rssi = tx_dbm − (31.7 + 10·n·log10(d) + w·d_horizontal + f·floors_apart)`, with `d` the 3-D distance (at least 1 m);
- defaults: `tx_dbm` 14 dBm, `n` = 3.0, `w` = 1.8 dB/m for internal walls, `f` = 18 dB per floor;
- there is a link (both directions, the same value) where `rssi ≥ −100 dBm`.

At 1000 nodes this gives a mean of 38 neighbours (15–52) and up to 6 hops from the gateway. Links weaker than −100 dBm are left out, including as interferers.

**The building generator on its own.** `radio_if/scale/gen_building.py` makes this topology for any run, not only the scale scenario; use it rather than writing another one:

```bash
python3 radio_if/scale/gen_building.py --nodes 48 -o topo48.txt --positions topo48.pos
# topology: {"nodes": 48, "floors": 1, "grid": "5x10", "links": 375, ...}   (on stderr)
WSSIM_SEED=1 build-vt/wssimserver --vtime=48 --group-file=topo48.txt ... /tmp/sock    # by hand, or:
TOPO=topo48.txt IF_NODES="$(seq -s ' ' 0 47)" IF_CMD=./my_client sim_utils/vtime/run.sh build-vt . out 300
```

| Option | Meaning | Default |
| --- | --- | --- |
| `--nodes N` | number of nodes, IDs 0 to N−1 (2 to 4096) | required |
| `--floors F` | floors; nodes fill floor 0 first, row by row | N/200 rounded, at least 1 |
| `--spacing M` | metres between neighbours on a floor; each floor is a grid about twice as long as wide | 8 |
| `--floor-height M` | metres between floors | 3.5 |
| `--tx-dbm`, `--pl-exp`, `--wall-db-per-m`, `--floor-db` | `tx_dbm`, `n`, `w`, `f` of the formula above | 14, 3.0, 1.8, 18 |
| `--min-rssi DBM` | no link below this | −100 |
| `-o FILE` | the topology (default: stdout) | |
| `--positions FILE` | also each node's position: `ID X Y Z FLOOR` in metres, after a `#` header line; the format of `wssimserver --positions`, which computes the links from the positions with its own models and shadowing (`docs/MEDIUM.md` §11) | |

The output has one line per link, `A,B:RSSI` with `A < B`, RSSI rounded to 0.1 dB, sorted: a `wssimserver --group-file` and a `TOPO=` file of `run.sh` (`sim_utils/vtime/README.md`, "Topology files"). Each link is given once, so it holds in both directions (`docs/MEDIUM.md` §2). The summary on stderr gives the number of links, the degree (mean, minimum, maximum) and the hops from node 0 (BFS over the links). The generator draws nothing at random: the same options give the same file. `run_scale.py` takes the same options and calls it.

**Load client** (`radio_if/scale/rif_load.c`, interface version 1). It is a load generator shaped like an alarm mesh, not a protocol:

- Each node opens a receive window of 20 ms every 1 s, on its own clock, and its radio is off in between. Channel k mod `--channels` is used for window k (default: one channel).
- The gateway sends a beacon every `--beacon-s` (60 s).
- A node relays each new beacon or alarm once, in its next window, after a random backoff and a CCA. A busy channel moves the frame later in the window, or to the next window.
- After each beacon, every node sends one report in a window drawn among the next `--report-windows` (30). Reports are not relayed.
- Frames are 20 bytes at 100 kbit/s (2.6 ms).

Random numbers come from the seed and the node ID, so runs are reproducible. Each node writes its first reception of every flood and a counter line.

- **Light load** is the defaults: about 4.6 wakes per node per virtual second and 44 frames per virtual second at 1000 nodes.
- **Heavy load** is `--beacon-s 10 --report-windows 5` with 20 alarms in 180 s: 16 wakes per node per virtual second and 280 frames per virtual second.

**Pinning.** Run on two fast cores: on the reference PC (AMD Ryzen AI 9 HX PRO 370, Zen 5 cores 0–3 and 12–15 at 5.1 GHz, Zen 5c cores at 3.3 GHz), `taskset -c 2,3` gave 0.034 s per virtual second at 1000 nodes, against 0.047 s unpinned and 0.045 s on one core. Wall time follows the speed of a single core, because the server runs one node at a time.

## 3. Figures

The figures below are with the final server (§5), the power report on (`efr32fg25-lfn.ini`, the profile of duty-cycled nodes: `docs/RADIO_INTERFACE.md` §7), no medium log, seed 1 and `taskset -c 2,3`. The PC was shared with other jobs (load average 4–9), and repeated runs vary by about ±10 %.

| Nodes | Load | Virtual s | Wall s per virtual s | Wakes | Wall µs per wake | Server user / sys CPU s | Clients CPU s | Server RSS | Clients PSS |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 100 | light | 180 | 0.0015 | 63 k | 4.4 | 0.03 / 0.11 | 0.24 | 5 MB | 11 MB |
| 300 | light | 180 | 0.0061 | 219 k | 5.0 | 0.15 / 0.40 | 0.74 | 7 MB | 31 MB |
| 1000 | light | 180 | 0.034 | 824 k | 7.4 | 0.89 / 1.80 | 4.3 | 12 MB | 116 MB |
| 1000 | light | 3600 | 0.034 | 14.9 M | 8.2 | 13.8 / 35.8 | 81 | 12 MB | 119 MB |
| 1000 | heavy | 180 | 0.145 | 2.87 M | 9.1 | 3.6 / 7.5 | 16.1 | 12 MB | 119 MB |
| 2000 | light | 180 | 0.097 | 1.69 M | 10.3 | 2.2 / 5.0 | 11.8 | 22 MB | 229 MB |
| 4000 | light | 60 | 0.28 | 1.45 M | 11.5 | 2.8 / 5.0 | 11.5 | 42 MB | 458 MB |

- These figures were measured before the medium's calibration on the FG25 kits (EM5; `docs/MEDIUM.md` §2.1, §8: with it, about 28 % fewer interference losses and the same flood coverage and latencies with the light load; with the heavy load, every flood complete and shorter beacon latencies).
- Every flood reached every node in these runs. With the light load at 1000 nodes, a beacon reached the whole building in 6 s (6 hops, one window per hop). With the heavy load, floods took up to 19 s, because CCA deferrals pushed relays to later windows.
- Starting the clients takes 0.2 s for 1000 nodes and 0.8 s for 4000. This is not part of "wall s per virtual s".
- The power report costs about 7 % of the wall time (1000 nodes, light load: 0.0317 without and 0.034 with). The medium log costs about 5 %.

**What a 1000-node, one-hour run costs.**

- Light load: measured at 123 s of wall time, 12 MB in the server and about 120 kB PSS per load client.
- Heavy load: about 0.145 × 3600 ≈ 9 minutes (estimate).
- With `--medium-log`: about 250 MB of log per hour (light load).

A client of the Partner's stack will use more memory per process than this load client. Plan with its own resident size × N.

For other loads, the wall time is roughly (wakes per node per virtual second) × N × 8–10 µs × virtual seconds.

## 4. Limits

| Limit | Value | Where |
| --- | --- | --- |
| Nodes per run | 4096 (IDs 0–4095) | `SIM_MAX_NODES`, `sim_proto.h`; the server's static tables (graph bitmap 2 MB, link table 64 MB of sparse shared memory) are sized for it |
| Connections | `RLIMIT_NOFILE` hard limit (524 288 here); the server raises its soft limit itself | `increase_limit_fd()` |
| Server log | one "node N connected" line per node; the per-connection "Connect fd" lines only with `-v` (since 2026-11-09). `--dump` prints the topology and RSSI matrices, N² entries each: `run.sh` adds it only up to 200 nodes (`DUMP=1` or `0` to choose), `run_scale.py` never | |
| Processes | one per node; `ulimit -u` here is 299 236 | |
| Command line | about 2000 nodes of a building with `-g` per link (ARG_MAX 2 MB, `E2BIG` at 4000); use `--group-file` | `run_scale.py` and `sim_utils/vtime/run.sh` (since 2026-11-09) use it: `run.sh` prepares a 1000-node building in under 1 s (150 s before) and runs 4000 nodes |
| Memory | server: 12 MB at 1000 nodes and 42 MB at 4000; load client: about 115 kB PSS each | |
| Socket path | 107 bytes; the runner uses `/tmp/wssim-scale-*.sock` | |

Each wake is a round trip between two processes. Over a run, the CPU time of the server and the clients roughly equals the wall time, whatever the number of cores. Runs scale out by running several seeds at once on different cores, not within one run.

## 5. Profile and changes to the server

**Profile** (gprof, 1000 and 2000 nodes, heavy load). Most of the server's time is system time in `send`/`recv`: the sleeps and wake-ups of a round trip per node wake. In user space, before the changes:

- **The server was compiled at `-O0`**, since the whole build is Debug.
- **The scans of the frames on air:** `med_energy_mw()` (SINR at lock, CCA) and `med_lock()` (the interferers) did a binary search in every frame on air for each receiver. The number of frames on air grows with the building, so each decision grew with N. This was 28–33 % of the server's user time.
- **The search for receivers:** `vt_queue_frame*()` tested all N nodes for each frame (131 M tests at 2000 nodes).
- **The rest:** the event heap (`vt_pop`, about 16 %) and the power ledger (`pwr_idle`, `pwr_radio`, about 15 %) are O(1) or O(log n) per event. Their cost is cache misses on per-node state, not an algorithm.

**Changes on branch `em-scale`.** Each one keeps the results identical. The check was a 100-node building with 4 alarms, drift ±20 ppm, the power report and log, and the medium log. The node outputs, medium log, `power.json`, power log and server log are byte-identical before and after each change.

| Commit | Change | Before → after |
| --- | --- | --- |
| `-O2` | `wssimserver` compiled with `-O2` in the Debug build (no fast-math) | server user time −35 % (1000 nodes: light 1.50 → 0.96 s, heavy 6.1 → 4.3 s); wall per virtual second 0.039 → 0.034 s (light) |
| bitmap | a 512-byte receiver bitmap per frame on air tests a miss before the binary search | 2000 nodes, heavy load: server user time −22 % (13.4/11.0 → 10.6/8.3 s), wall −5 % |
| neighbours | the receivers of a frame are found by walking the set bits of the sender's graph row (same order) | 131 M → 2.7 M receiver tests at 2000 nodes; server user −5 % |
| `--group-file` | the topology from a file (one `-g` argument per line) | removes the 2000-node limit of the command line |

**Overall**, at 1000 nodes with the heavy load, the server's user time went from 6.1 s to 3.6 s. Wall time improved by about 10 %, because the exchange between processes dominates.

**Tried and not kept:**

| Attempt | Result | Why not kept |
| --- | --- | --- |
| Batching the server's system calls (`recvmmsg` for the client's messages, `sendmmsg` for `"xa"` and its frames) | system calls per wake 4.5 → 2.1; system time and wall time unchanged | the cost is the scheduler's sleep and wake-up, not the number of calls; it added code with no gain |
| The server polling briefly before blocking | wall time −20 % | the server's CPU time doubled, and it would slow the other runs sharing the PC |

**Further gains would need a different execution model**, not local fixes:

- running same-time wakes in parallel (EM3 step 4; this load wakes all nodes at the same instants when drift is 0);
- a transport that avoids the scheduler (shared memory with spinning);
- a client loaded into the server's process.

Each of these is a design decision (DI2: one process per node). None is needed for 1000-node, one-hour runs.
