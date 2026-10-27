# Virtual-time runs (EM3)

Scripts for the step-1 gate of `docs/VIRTUAL_TIME.md` (§7). They need a virtual-time build (§2): `wssimserver`, `wsbrd-inproc` and `wsnode-inproc` from `build-vt/`. `<workdir>` is the directory that holds `examples/` (the repo root).

| Script | What |
| --- | --- |
| `run.sh` | One run: `wssimserver --vtime=<nodes> --vtime-end=<s>`, `wsbrd-inproc` and one `wsnode-inproc` per router, all on a socket of the run's own |
| `compare_logs.py` | Compares two runs' logs byte for byte; prints the first differing line of each file |
| `determinism.sh` | Same seed twice and another seed once (§7.2) |
| `gen_topology.py` | Connected topologies (random geometric graph or grid), deterministic from a seed |
| `speed.sh` | Wall time per simulated second and peak RSS at several sizes (§7.4) |

Analysis: `sim_utils/migration/analyze.py` reads these runs as it reads the wall-clock ones (`brtree` lines of `br.log` instead of `dbus_nodes.txt`).

```bash
B=build-vt; W=$(pwd)
# one dag_simple run, 300 virtual seconds, seed 3
WSSIM_SEED=3 TUN=tunvt0 sim_utils/vtime/run.sh $B $W runs/vt/seed3 300
python3 sim_utils/migration/analyze.py runs/vt/seed*      # -- runs/inproc/seed* to compare groups
# determinism: seed 1 twice (a, b) and seed 2 (c), under setarch -R
TUN=tunvt0 sim_utils/vtime/determinism.sh $B $W runs/vt/det 300 1
# speed at 15 (dag_simple), 100 and 500 nodes, 60 virtual seconds each
TUN=tunvt0 sim_utils/vtime/speed.sh $B $W runs/vt/speed 60 15 100 500
python3 sim_utils/vtime/gen_topology.py 500 --seed 2 -o topo500.txt   # then TOPO=topo500.txt run.sh ...
```

## run.sh

Output: one log per program (`simserver.log`, `br.log`, `node<i>.log`), with no timestamping wrapper: the simulation port starts every line with `NSIM_EPOCH_S` + virtual time, and `t0` holds `NSIM_EPOCH_S` (1700000000). Also `topology.txt`, `run_info.txt` (seed, socket, wall-clock start and end), `exit_codes.txt`, `log_wsbr` and `storage/`.

The run ends when `wssimserver` exits (`--vtime-end`, or SIGTERM from `WALL_TIMEOUT`). A program that exits with an error before that stops the run. The script never uses `pkill`; it stops only the processes it started. Options are environment variables, listed at the top of the script: `WSSIM_SEED`, `TOPO`, `SOCK`, `TUN`, `SETARCH=1`, `KEEP_DBUS=1`, `WALL_TIMEOUT`, `RSS=1`, `BR_OPTS`, `NODE_OPTS`.

## Running several runs at once

What each run gets of its own, and what it shares:

| Resource | Per run? | How |
| --- | --- | --- |
| Conductor socket | yes | `SOCK`, default a new `/tmp/wssim-vt-*.sock` |
| Shared memory (CCA, node infos) | yes | named from the socket path (`sim_shm_name()`, §3) |
| Key storage (`*_pae_*`, the BR's PAN ID) | yes | `storage_prefix=storage/<node>_`, relative to the run directory |
| BR trace file (`/tmp/log_wsbr`) | yes | `WSBRD_LOG_FILE=<run>/log_wsbr` |
| D-Bus names (`com.silabs.Wisun.BorderRouter`, `com.wshwsim.mac*`) | not shared | the programs cannot reach D-Bus (`KEEP_DBUS=1` to allow it). They register with `REPLACE_EXISTING`, so otherwise a run would take the names of another run, including a wall-clock run's |
| **TUN device** | **no** | `wsbrd-inproc` opens it with `TUNSETIFF`, which fails while another border router holds it. Give each concurrent run its own: `sudo ip tuntap add mode tun user $USER name tunvt0 && sudo ip link set tunvt0 up`, then `TUN=tunvt0`. Use the same TUN for runs you compare byte for byte (the BR reads the TUN's link-local address) |
| Process names | **no** | `sim_utils/migration/headless_run.sh` runs `pkill -x wsbrd-inproc`, `wsnode-inproc` and `wssimserver`: it kills every run on the PC, virtual-time runs included |

`/tmp/wsbrd/` is not used by any program (`headless_run.sh` only creates it). The default `storage_prefix` of the programs is `/var/lib/wsbrd/`, shared by all runs, which is why `run.sh` always sets it.
