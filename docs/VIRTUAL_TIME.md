# Virtual time (EM3): design for implementers

Plan and rationale: the root repo's `documents/pre-study/investigation/Virtual_Time_Study.md` (§4–§6) and `STATUS.md` EM3. This note fixes the interfaces of steps 0 and 1 so they can be built in parallel. Scope of step 1: **in-process nodes only** (`wsnode-inproc`, `wsbrd-inproc`), batch mode, one node at a time.

## 1. Pieces and where they live

| Piece | Repo / path | What |
| --- | --- | --- |
| Simulation port | `nanostack-generic-standalone/port/sim/` | `NANOSTACK_PORT=nanostack_port_sim`. Replaces the POSIX port's `os_timer.c`, `fhss_timer.c`, `time.c`, `os_scheduler.c`, `random.c`; shares the others from `port/posix/common/`. API and wire messages: `port/sim/ns_port_sim.h` |
| Conductor | `app_wssimserver/wssimserver.c`, option `--vtime` | Global virtual time, event queue, one node at a time |
| Hello and shared memory | `app_wssimserver/sim_proto.h` | `SIM_HELLO_VTIME` flag; `sim_shm_name()` |
| Radio side | `app_wshwsim/sl_rf_driver.c`, `sl_rf_inproc.c` | Under `NS_PORT_SIM`: hello with the flag, `ns_port_sim_attach()`, seed from the conductor, no D-Bus thread |
| BR batch output | `app_wsbrd/wsbr.c` | Under `NS_PORT_SIM`: the node table logged every 30 s of virtual time |
| Tooling | `sim_utils/vtime/` | Run script, determinism and speed checks; `sim_utils/migration/analyze.py` reads virtual-time logs |

## 2. Build

A virtual-time build is a separate out-of-tree build directory, with the whole stack on the simulation port:

```bash
cmake -S . -B build-vt -G Ninja -DNANOSTACK_PORT=nanostack_port_sim \
  -DCMAKE_PREFIX_PATH=<mbedtls> -DCMAKE_C_FLAGS="-std=gnu17 -fpermissive"
ninja -C build-vt wssimserver wsnode-inproc wsbrd-inproc
```

`nanostack_port_sim` defines `NS_PORT_SIM` publicly, so programs can test it. The in-tree build stays on the POSIX port. In a virtual-time build, the split programs (`wsbrd`, `wsnode`, `wshwsim`) are not built until step 2.

## 3. Conductor protocol

Node side: `ns_port_sim.h`. Summary:

```
node                                   conductor (wssimserver --vtime)
connect; "xh"{id, flags=VTIME} ---->
                               <---- "xw"{version, seed}       (when all N nodes said hello; one node at a time)
run until idle; "xi"{next} ------->
                               <---- "xa"{t, frames=k} + k frames for this node at t
deliver frames; fire timers due at t; run until idle;
any "xx" frames it sends ------->   (queued for the neighbours at t)
"xi"{next} ---------------------->
...
                               <---- "xq"   (end of run)
```

Rules:

- **Start.** `--vtime=N` makes the conductor wait for N hellos before it sends any welcome, so the nodes' start order and start time do not matter, as long as the server is already listening (a node whose connect fails stops). All nodes start at virtual time 0. Welcomes go out one node at a time in node-ID order, and the conductor reads each node's first run up to its `"xi"` before welcoming the next, so frames queued at t = 0 are in a deterministic order. A node that disconnects before the run starts is dropped and the conductor waits for another hello. A hello without `SIM_HELLO_VTIME` is refused in `--vtime` mode, and one with it is refused in wall-clock mode.
- **Seeds.** The conductor reads `WSSIM_SEED` (default 1) and sends it in the welcome. The radio's loss generator uses `seed + node ID`, as in EM1, so a seed means the same loss draws in both modes. The port's random source uses its own stream: splitmix64 started at `mix64(seed + mix64(node ID + 1))`, where `mix64` is splitmix64's output function. (A plain `seed ^ GOLDEN_GAMMA × (ID + 1)` would put nodes on one sequence a few draws apart.)
- **Order.** The conductor waits until no node is running. It then takes the earliest event by `(time, node ID, sequence)`. The sequence number grows with every event queued, so ties are ordered by when they were queued. Two kinds of event: a node's deadline (from its last `"xi"`; a new `"xi"` replaces it) and a frame delivery.
- **Wake.** For the chosen node and time t, the conductor sends `"xa"{t, frames = k}`, then the k frames queued for that node at time t, in queue order, and waits for its `"xi"`. Messages the node sends before that `"xi"` are frames: each goes to the sender's neighbours in the graph (as `relay()` does today) with delivery time t. A node is never woken at a time earlier than its last wake; a deadline earlier than now in an `"xi"` is moved to now, with a warning.
- **Same-time loops.** If more than 100 000 wakes in a row happen without time moving forward, the conductor logs the queue head and stops.
- **End.** `--vtime-end=<seconds>`: when the next event is later than that, the conductor sends `"xq"` to every node, waits for them to close, and exits 0. SIGINT and SIGTERM also send `"xq"`.
- **Frames.** Unchanged from EM1 (`sim_frame_hdr`, server-stamped `src`). Delivery at TX start; the receiver's airtime and RX-end timers run on virtual time.
- **Power recording** (`--power`, EM2): the welcome's `flags` word has `NSIM_WELCOME_POWER`, and the nodes then also send "xc" before each "xi" and "xr" for their radio's state changes. See [POWER_MODEL.md](POWER_MODEL.md) §8.
- **Shared memory.** Its names come from `sim_shm_name(base, socket path)`: the default socket keeps `/wssimcca` and `/wssimnodesinfos`, and any other socket gets its own names. This is one of the conditions for several runs on one PC; the others are in `sim_utils/vtime/README.md` (a TUN device per border router, D-Bus names, per-run storage, and no `headless_run.sh` at the same time, since it uses `pkill`). CCA reads the neighbours' state there, which stays correct while nodes run one at a time.

## 4. The simulation port

- **Timers.** Callback timers and FHSS timers are entries with an absolute 64-bit deadline in µs, or unarmed. `eventOS_callback_timer_start(id, slots)` arms at `now + slots × 50 µs`. **A zero-length start disarms the timer**, as `timerfd_settime` does in the POSIX port; this keeps step 1 comparable with wall-clock runs. Count such calls and log the count at exit. `_expired()` and `_expired_usec()` return 1 when the timer is unarmed or due. `fhss_get_remaining_slots` returns `deadline − now` (0 if due or unarmed). `fhss_get_timestamp` and `ns_port_time_us()` return the low 32 bits of `now + NSIM_CLOCK32_OFFSET_US`: the 32-bit clock starts 120 s before its wrap, so no code sees a timestamp of 0 and every run crosses the wrap. `ns_port_time_wall_s()` returns `NSIM_EPOCH_S + now / 10⁶`.
- **Run loop.** `ns_port_run()` repeats until idle:
  1. The sources in the order they were added (`os_scheduler.h`): the event loop (one event, or until idle, as configured) when it was signalled; for application sources, `cb()` only while `pending()` returns true. File descriptors other than the conductor's are never polled in batch mode.
  2. The callback timers due at `now`, in order of deadline then timer ID, each disarmed before its callback runs; then the FHSS timers due, in the same order.
  3. The poll hooks.

  Idle: no event signalled, nothing pending, no timer due. It then sends `"xi"` and blocks on the conductor socket. On `"xa"` it sets `now`; then, k times (`frames` in the `"xa"`), it waits until the next message is readable (blocking `poll()`; it may not have arrived yet) and calls the callback registered for the conductor fd, which reads it. `ns_port_posix_set_busy()` is not supported (split mode, step 2): FATAL if called.
- **`eventOS_scheduler_signal()`** sets a flag; there is no pipe.
- **Random.** `ns_port_random_bytes()` draws from the seeded splitmix64 stream. Calling it before `ns_port_sim_attach()` is FATAL, so no unseeded draw goes unnoticed.
- **Logs.** At program start (a constructor), the port replaces `stdout` and `stderr` with `fopencookie()` streams that write whole lines to fds 1 and 2, each prefixed with `"%llu.%06llu "` of `NSIM_EPOCH_S` + virtual time. Lines printed before attach get virtual time 0. `TRACE()`'s own timestamp (`log.h`) also uses virtual time. The run scripts then need no timestamping wrapper, and `analyze.py` reads the same format as today's (`<seconds> <message>`), with `t0` = `NSIM_EPOCH_S`.
- **Unit tests** in `nanostack-generic-standalone/tests/port_sim/`, against a fake conductor over `socketpair()`: arm, re-arm, stop, zero length, ordering of same-time timers, `_expired`, FHSS remaining slots, 32-bit wrap of `ns_port_time_us()`, frame delivery before timers, idle reports, random reproducibility, quit.

## 5. Border router in batch mode

`wsbrd-inproc` still opens `tun0` and registers on D-Bus, but neither is read in batch mode. Every 30 s of virtual time it logs one line per node of the table that D-Bus `Nodes` returns (`dbus.c`, `dbus_get_nodes()`): `brtree t=<s> node=<eui64> parent=<eui64>` with real EUI-64s (the table holds IIDs, whose U/L bit is flipped back), then `brtree t=<s> nodes=<n>`. `analyze.py` uses these lines when there is no `dbus_nodes.txt`.

**mbedTLS.** The virtual-time build needs an mbedTLS built with `MBEDTLS_NO_PLATFORM_ENTROPY` (README, "Compile mbedtls"; in this repo's ignored `mbedtls/` directory, installed to `mbedtls/install`). With the reference's default mbedTLS, TLS also reads `/dev/urandom` and runs are not reproducible.

## 6. Step 0 (wall clock, before any of the above)

Remove the time and random leaks outside the port, without changing behaviour:

- `sl_rf_driver.c`: `rf_get_timestamp()` returns `ns_port_time_us()`; `get_sys_time_ms()` is removed; the FHSS-state thread (`set_fhss_status`) becomes a poll hook (`ns_port_add_poll_hook()`) that sets `fhss_on` once `fhss_synch_global_state_get()` is true, and is never cleared again.
- `wsbr_mac.c`, `wsbr_fhss_mac.c`: `ns_port_time_us()`.
- `wsbr.c`, `wsrouter.c` log file, `multicast_helper.c`, the MQTT timer (`MQTTMbed.c`): `ns_port_time_wall_s()` / `ns_port_time_us()`.
- mbedTLS: `MBEDTLS_NO_PLATFORM_ENTROPY` in `examples/mbedtls-config.h`, and `NANOSTACK_TLS_STRONG_PORT_ENTROPY=ON` in this repo's build, so all TLS randomness comes from `ns_port_random_bytes()`.

Gate: `dag_simple`, 5 split and 5 in-process runs (`sim_utils/migration/headless_run.sh`, 300 s), compared with the EM1 figures (split 64/70 routers ready, median 154 s; in-process 65/70, 154 s) with `analyze.py`; no parent outside the topology.

## 7. Step 1 gate

1. The port's unit tests pass.
2. **Determinism:** two runs with the same seed give byte-identical node and border-router logs (not `simserver.log`, which reports wall time) (run under `setarch -R` to remove address randomisation from printed pointers); a different seed gives different logs.
3. **Equivalence:** in-process `dag_simple`, 10 seeds in virtual time, against 10 wall-clock in-process runs of the step-0 build: routers ready by 295 s, ready-time median and range, parent-selection and authentication medians, no parent outside the topology. Expected differences: zero CPU time (faster joins).
4. **Speed:** wall time per simulated second at 15, 100 and 500 nodes.
