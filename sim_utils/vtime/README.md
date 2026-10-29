# Virtual-time runs (EM3)

Scripts for the step-1 gate of `docs/VIRTUAL_TIME.md` (§7). They need a virtual-time build (§2): `wssimserver`, `wsbrd-inproc` and `wsnode-inproc` from `build-vt/`. `<workdir>` is the directory that holds `examples/` (the repo root).

| Script | What |
| --- | --- |
| `run.sh` | One run: `wssimserver --vtime=<nodes> --vtime-end=<s>`, `wsbrd-inproc` and one `wsnode-inproc` per router, all on a socket of the run's own |
| `compare_logs.py` | Compares two runs' logs byte for byte; prints the first differing line of each file (`--mask-pointers`, `--mask-source-lines`) |
| `determinism.sh` | Same seed twice and another seed once (§7.2) |
| `gen_topology.py` | Connected topologies (random geometric graph or grid), deterministic from a seed |
| `tun_setup.sh` | Creates `tunvt0`… (run once per boot with sudo): one TUN per border router running at the same time, all with the same fixed link-local address |
| `campaign.sh` | Several runs at once, one per `tunvt` device: `campaign.sh <bindir> <workdir> <outdir> <seconds> <seed>...` (`JOBS` limits how many at a time) |
| `speed.sh` | Wall time per simulated second and peak RSS at several sizes (§7.4) |
| `test_radio_off.sh` | Radio off end to end (GS8 stage 1): router 1 turns RX off when idle at 150 s; checks that it then receives nothing, that frames are lost, that the ledger shows its radio off and its MCU in deep sleep, and the sums: `test_radio_off.sh build-vt $(pwd) runs/radio_off` (`TUN=tunvt0`) |
| `test_lfn_mac.sh` | FAN 1.1 LFN unicast mechanics in the MAC and FHSS (GS8 stage 2): a parent and an LFN (`wslfntest-inproc`, MAC only); checks that the LFN receives only in its windows (on the LTO grid after an LTO, `PARENT_OPTS="--lto-offset-ms=1200 --lto-interval-ms=5000"`), on the right channels, that its radio does not listen otherwise (TX, ACK, off; idle during backoffs), that its uplinks avoid the parent's broadcast dwell and that the parent's frames wait for a window: `test_lfn_mac.sh build-vt $(pwd) runs/lfn_mac` (`WSSIM_SEED`, `RSSI`, `DUR`, `LFN_OPTS`, `PARENT_OPTS`; `docs/POWER_MODEL.md` §13) |
| `test_lfn_join.sh` | FAN 1.1 LFN under the border router (GS8 stage 3; the stack's `docs/LFN_STACK.md`): `wsbrd-inproc` with `examples/wsbrd-lfn.conf`, `wsnode-inproc` as an LFN with `examples/wsnode-lfn.conf`, UDP test traffic both ways and to `ff02::1` (`app_wsbrd/test_traffic.c`, keys `test_udp_*`); checks the join, the traffic, the multicast copies, that the LFN receives only in its windows and keeps its parent, the LFN's LGTKs (join with an LGTK; for each LGTK installed later: LPC with the new hash, key request after `lgtk_max_mismatch_s`, key before activation, new active index; the timings are printed), and prints the LFN's steady-state current by part: `test_lfn_join.sh build-vt $(pwd) runs/lfn_join` (`TUN`, `WSSIM_SEED`, `DUR`, `RSSI`, `STEADY_FROM`, `TX_POWER`, `BR_PERIOD`, `LFN_PERIOD`, `LFN_EXTRA`, `BR_EXTRA`, `SILENT_AT`, `SILENT_FOR`, `LGTK_ROTATE_AT`: an LGTK rotation at that second; short LGTK lifetimes with `BR_EXTRA="-o lfn_lgtk_lifetime_s=900 -o lfn_lgtk_new_install_required=50 -o lfn_lgtk_new_activation_time=3"`) |
| `wakes_window.py` | Wakes per second by cause and average current in a time window, from two runs of the same seed that differ only in duration (the difference of their `power.json` totals): `wakes_window.py [--nodes 1-14] <short run> <long run> ...` (`docs/POWER_MODEL.md` §15) |
| `test_power.py` | Test of the power ledger (`docs/POWER_MODEL.md`): one scripted node, totals checked against hand-computed ones, also with per-PHY-mode profile values (§6.2) and `--power-mode`, and profiles the parser must reject: `test_power.py build-vt/wssimserver` |

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

Power recording (EM2, `docs/POWER_MODEL.md`): `POWER=examples/power/efr32fg25-soc.ini` adds `power.json` to the run (`POWER_LOG=1`: also `power_log.txt`, every state interval; `POWER_CELL=mAh[:V[:eff]]`: battery life; `POWER_OPTS="--power-mode=3"`: whose profile values apply, by default the PHY mode the node runs, `docs/POWER_MODEL.md` §6.2). `campaign.sh` passes these through; `sim_utils/power_summary.py runs/...` summarises the runs.

Output: one log per program (`simserver.log`, `br.log`, `node<i>.log`), with no timestamping wrapper: the simulation port starts every line with `NSIM_EPOCH_S` + virtual time, and `t0` holds `NSIM_EPOCH_S` (1700000000). Also `topology.txt`, `run_info.txt` (seed, socket, wall-clock start and end), `exit_codes.txt`, `log_wsbr` and `storage/`.

The run ends when `wssimserver` exits (`--vtime-end`, or SIGTERM from `WALL_TIMEOUT`). A program that exits with an error before that stops the run. The script never uses `pkill`; it stops only the processes it started. Options are environment variables, listed at the top of the script: `WSSIM_SEED`, `TOPO`, `SOCK`, `TUN`, `SETARCH=1`, `KEEP_DBUS=1`, `WALL_TIMEOUT`, `RSS=1`, `CONF` (`CONF=eu1a`: `examples/wsbrd-eu1a.conf` and `wsnode-eu1a.conf`, the FG25 firmware's PHY), `BR_OPTS`, `NODE_OPTS`, `NODE_OPTS_<i>`, `BR_ENV`, `NODE_ENV_<i>` (environment for one program), and the `POWER*` options. The programs inherit the environment, so `WSSIM_RADIO_OFF=0` (the emulated radio's `PHY_INTERFACE_DOWN` leaves RX on, as before GS8 stage 1) and the `WSSIM_PHY_*` settings (data rate and PHY overhead, `docs/POWER_MODEL.md` §6.1) apply to a whole run or campaign; `NODE_ENV_<i>` sets them for one node.

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

## Campaigns: several runs at once

Each run uses about 1.4 CPU cores (the conductor and the one node it is running), so a PC runs many at once. Virtual time does not depend on the CPU load, so a run gives the same logs alone or in a campaign.

```bash
sudo sim_utils/vtime/tun_setup.sh 12        # once per boot: tunvt0..tunvt11
SETARCH=1 sim_utils/vtime/campaign.sh build-vt $(pwd) runs/vt/campaign 300 $(seq 1 12)
```

Measured on a 24-core PC (2026-10-27, `dag_simple`, 300 virtual seconds, 12 seeds): 48.0 s one at a time, 10.5 s six at a time, 7.0 s twelve at a time (6.8×). Every seed's logs were identical in the three cases, and identical to earlier runs on `tun0` (`compare_logs.py --mask-pointers`: only printed heap addresses depend on the TUN's name).

**Memory.** A run needs about 5 MB per node (measured: 71 MB at 15 nodes, 0.5 GB at 100, 2.5 GB at 500), so a campaign needs about `JOBS` times that. Before starting, `campaign.sh` estimates it (5 MB per node plus 20%, `MB_PER_NODE`) and compares it with the available memory minus a reserve (`RESERVE_GB`, default 4). If `JOBS` runs do not fit, it lowers `JOBS` to what fits and prints why, for example `campaign: JOBS=12 needs ~36 GB, 24 GB usable: running 6 at a time (FORCE=1 to keep 12)`. If not even one run fits, it does not start. `FORCE=1` keeps `JOBS` as given. The check is made once, at the start, and the estimate is from `dag_simple` and generated topologies: denser networks may need more.

The `tunvt` devices carry no generated addresses and the same fixed link-local address, because `wsbrd` reads and logs it: with tun0's random address a run would log a different address on each device.
