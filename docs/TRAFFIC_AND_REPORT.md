# Application load, network statistics and run report

**Domain: internal.** A virtual-time run ([VIRTUAL_TIME.md](VIRTUAL_TIME.md)) can carry an application load, record per-node network statistics, and end with a machine-readable report. This note is the data contract for the tools that read the runs (the mesh testbed controller's report page and PDF export): options, line formats, `report.json` and its limits. Measurement scenarios, which drive a run through phases with live conditions, are in [SCENARIOS.md](SCENARIOS.md).

Everything here is off by default: with no `LOAD`, `STATS`, `SCENARIO` or `load_*`/`stats` key, the programs run and log exactly as before (`sim_utils/migration/nonreg_dag_simple.sh`: 10 of 10 seeds identical to `dag_simple_8c5677e`).

## 1. Pieces

| Piece | Where | What |
| --- | --- | --- |
| Load generator | `app_wsbrd/app_load.c`, `app_load.h` | In `wsbrd-inproc` and `wsnode-inproc` (and the split programs). UDP flows, echoes, `LOAD1` lines, runtime commands |
| Network statistics | `app_wsbrd/net_stats.c`, `net_stats.h` | `NET1` lines: the stack's counters, queue depths over time, each unicast frame as the MAC confirms it to the stack, RPL state |
| Link-time wrappers | `CMakeLists.txt` (in-process programs) | `--wrap` of `sw_mac_stats_update`, `protocol_stats_update` (queue depths over time), `rpl_control_transmit_dio`, `_dis`, `_dao` (RPL control messages sent), `random_early_detection_create` (`queue_limit`): they count, or change the thresholds when `queue_limit` is set, then call the stack's function; without the keys they only forward |
| Log reader | `sim_utils/runlog.py` | Streams a run's logs once (incrementally for scenarios) |
| Report | `sim_utils/report.py` | `report.json` (§5), a text summary, the aggregate of several runs |
| Run script | `sim_utils/vtime/run.sh` | `LOAD`, `LOAD_*`, `STATS`, `STATS_PERIOD`, `SCENARIO`, `QUEUE_LIMIT`, `COMPACT`, `REPORT` (§2) |
| Tests | `sim_utils/vtime/test_report.py`, `test_scenario.sh` | Report, log reader and validator on synthetic runs (about 1 s; `test_report.py <scratch dir>`, or `$TMPDIR`); a live scenario with every action, and the failure cases (about 20 s) |

The Nanostack sources (`nanostack-generic-standalone`) are not changed: the generator uses the socket API, and the statistics use the stack's existing statistics APIs (`ns_sw_mac_statistics_start()`, `protocol_stats_start()`, `ws_statistics_start()`, `ws_stack_info_get()`, RPL's DODAG information) and the wrappers above.

## 2. Options

### 2.1 run.sh

| Variable | Effect |
| --- | --- |
| `LOAD=<flows>` | Turns the load on: `up`, `down`, `mcast`, `p2p`, comma-separated. Implies `STATS=1` unless `STATS=0` |
| `LOAD_<KEY>=<value>` | Sets the conf key `load_<key>` (lower case) of every program, e.g. `LOAD_INTERVAL_S=10` |
| `STATS=1` | `NET1` lines (§4) every `STATS_PERIOD` s (default 10; 0: only at the end of the run) |
| `SCENARIO=<file>` | A measurement scenario ([SCENARIOS.md](SCENARIOS.md)); the load and statistics are then driven by it |
| `QUEUE_LIMIT=<n>` | Device queue limit (`queue_limit`, §2.2) for every program |
| `COMPACT=1` | Compact logs (`log_level = info`): no per-frame traces of the stack (MAC, FHSS) or of the emulated radio. Node logs about 10 times smaller (dag_simple, 400 s: 21.7 MB to 2.1 MB); join milestones, `LOAD1`, `NET1` and every result identical |
| `REPORT=auto\|1\|0` | `sim_utils/report.py` at the end: `<outdir>/report.json` and `report.txt`. `auto` (default): when `LOAD`, `STATS` or `SCENARIO` is set |

`run.sh` passes the keys as `-o load_...` to the border router and every router, before `BR_OPTS`, `NODE_OPTS` and `NODE_OPTS_<i>`, so a per-node option overrides them. It adds `load_max_id` (the highest router ID) and writes the options to `run_info.txt` (`app_opts=`), where the report reads them.

### 2.2 Conf keys (`load_*`, `stats`)

| Key | Default | Meaning |
| --- | --- | --- |
| `load_flows` | none | `up` (routers to the border router, echoed by it), `down` (border router to each router, unicast, echoed), `mcast` (border router to `load_group`, not echoed), `p2p` (router to a router drawn per packet among the members, echoed); `none` |
| `load_nodes` | `all` | The routers that take part: `all`, IDs and ranges (`1-20,33`), or a fraction (`0.25`, drawn per node from the seed and the ID, the same in every program). Kept as a bitmap of IDs 0 to 4095: a command sent at run time has no length limit (a conf key line is read up to 255 characters) |
| `load_payload` | 64 | UDP payload in bytes, 32 (the header, §3) to 1232 |
| `load_interval_s` | 30 | Mean interval between two packets of a node and flow (for `down`: of each target router) |
| `load_down_interval_s`, `load_mcast_interval_s`, `load_p2p_interval_s` | `load_interval_s` | Per flow |
| `load_dist` | `fixed` | `fixed` or `exp` (Poisson sends) |
| `load_jitter_s` | one interval | First packet of a stream uniformly in `[start, start + jitter)`, so that nodes do not send in step. With `exp` and no jitter given, the first gap is exponential like the others |
| `load_start_s` | 0 | No packet before this virtual time |
| `load_after_ready_s` | 0 | A router starts this long after its `ARM_NWK_BOOTSTRAP_READY` (whichever of the two starts is later); negative: `load_start_s` only |
| `load_stop_s` | 0 (none) | No packet from then on |
| `load_ramp_step_s` | 0 (no ramp) | Load ramp: steps of this length from `load_start_s`; the interval is divided by `load_ramp_factor` (2) at each step; no packet after `load_ramp_steps` (6) steps |
| `load_echo` | true | Targets answer requests |
| `load_hop_limit` | 64 | IPv6 hop limit of every packet (unicast and multicast) |
| `load_group` | `ff05::10ad` | The `mcast` flow's group (site-local: forwarded by MPL across the mesh); members join it |
| `load_log` | `packets` | `none`: no `LOAD1` line (counters in `NET1` only) |
| `load_control` | false | Runs the module without flows, for runtime commands (set by `SCENARIO`) |
| `load_step` | 0 | The step byte of packets without a ramp (set by scenarios: their load-setting number) |
| `load_max_id` | 0 | The highest router ID (set by `run.sh`), for `p2p` and fractions |
| `stats` | false | `NET1` lines |
| `stats_period_s` | 0 | Their period; 0: only the final line (at the end of the run) and those a scenario asks for |
| `queue_limit` | 0 (none) | Device queue limit, frames: the random early detection of the stack's queues (the adaptation layer's, and the border router's LLC queues) drops from `n/2` frames on average and always at `n`. Without it, the stack sizes them from the heap (`WS_CONGESTION_BR_*`, `_NODE_*`), unbounded on a PC (§6.1) |
| `log_level` | `all` | `info`: compact logs (`COMPACT=1`) |

Streams: a router has one stream per flow it originates; the border router one `down` stream per member router it has a route to (checked every 5 s) and one `mcast` stream. A router without a DODAG yet does not send (`skip` line, §3). The random draws (start offsets, exponential gaps, peers, fractions) come from the module's own generator, seeded with the run's seed and the node's ID: the stack's random stream is not drawn from, and the load is the same in every run of the same seed and options.

## 3. Packets and `LOAD1` lines

Packet (UDP port 7010, little-endian, 32 bytes then zero padding to `load_payload`): `"LD"`, version 1, type (0 request, 1 echo, 2 one-way), flow (1 up, 2 down, 3 mcast, 4 p2p), step, originator ID (u16), target ID (u16, 0xffff for the group), hop limit at send (u8), reserved, sequence (u32, per originator and flow for the whole run), the originator's send time (virtual µs, u64), and in an echo the target's receive time (virtual µs, u64).

Each program prints one line per packet (after the simulation port's time prefix, `<1700000000 + virtual s>.<µs>`):

```
LOAD1 cfg  <flows> <member> <echo> <step>          at the start and after each command: flows (bit 1 << flow), member 1|0, echo 1|0
LOAD1 tx   <flow> <seq> <src> <dst> <len> <step> <t_tx_us> <rc> <type>
LOAD1 skip <flow> <seq> <src> <dst> <len> <step> <t_us> <reason>          reason: no_dodag, no_peer
LOAD1 rx   <flow> <seq> <src> <dst> <len> <step> <t_tx_us> <t_rx_us> <hl_tx> <hl_rx>
LOAD1 echo <flow> <seq> <src> <dst> <len> <step> <t_tx_us> <t_peer_rx_us> <t_rx_us> <hl_tx> <hl_rx>
```

`type` is the packet's (0 request, echoed; 2 one-way). `src` is always the originator and `dst` the target; `(flow, src, seq)` names a packet for the whole run. `rx` is printed by the target (one-way delay `t_rx_us - t_tx_us`, exact: virtual time is shared), `echo` by the originator (RTT `t_rx_us - t_tx_us`; forward leg `t_peer_rx_us - t_tx_us`). `rc` is `socket_sendto()`'s result (negative: not sent).

**Hop count.** From the IPv6 hop limit: `hops = hl_tx - hl_rx + 1`, the number of radio links of the path the packet took (the sender's own link counts 1; each router that forwards it decrements the hop limit). Checked in `dag_simple`: it equals the depth in the RPL tree for uplink, downlink (source-routed: the inner header's hop limit is decremented too) and MPL multicast. It is per packet, so a parent change during the run is seen; the report groups RTTs by the hop count of the echo's path and one-way delays by the request's.

**Rates.** Each stream's next send is drawn from its previous scheduled time, not from the (up to 1 ms later) wake that sent it: the offered rate is the one asked for at any interval (a stream more than a second behind starts again from the current time).

**Commands** (scenarios, the conductor's `"xL"` messages, [SCENARIOS.md](SCENARIOS.md) §8): `#<seq> load set KEY=VALUE ...` (the keys of §2.2 without `load_`; checked on a copy and applied whole, or not at all), `load stop`, `load exclude IDS` and `load include IDS` (routers a scenario stopped and started again: no streams to or from them meanwhile). Every command with a sequence number is answered by a line `CTL1 ok <seq>` or `CTL1 err <seq> <reason>` in the node's log.

## 4. `NET1` lines

`<time prefix> NET1 {json}` on one line, cumulative counters since the program's start (the report takes differences between samples):

| Field | Meaning (unit) |
| --- | --- |
| `v`, `node`, `t_us`, `final` | Format 2, node ID, virtual time (µs), true for the line printed at the end of the run |
| `mac.tx`, `rx`, `bc_tx`, `bc_rx` | Frames the MAC sent and received (all, broadcast), the stack's counters |
| `mac.uc_retry_hist` | Unicast frames the MAC confirmed as delivered to the stack, by the number of retries it reports (0, 1, ..., 7, 8 or more): one count per frame, whatever happened inside the MAC (a requeue with FHSS included) |
| `mac.uc_give_up.no_ack`, `.channel_access`, `.other` | Unicast frames the MAC confirmed as not delivered (no ACK after its last retry, channel access failure, any other status) |
| `mac.bc_done` | Broadcast frames confirmed |
| `mac.retry_attempts` | The stack's counter of retry attempts, every attempt series (a requeued frame's included): not a per-frame figure |
| `mac.cca_att`, `mac.cca_fail` | CCA attempts, channel-access failures (the stack's counters) |
| `mac.rx_drop` | Received frames the MAC dropped |
| `mac.q`, `mac.q_max`, `mac.q_int_us` | MAC TX queue (unicast and broadcast) now, peak, and its integral over time (frames × µs): the mean over an interval is Δ`q_int_us` / Δ`t_us` |
| `mac.lat_max_ms` | Longest MAC data request latency |
| `al.q`, `al.q_max`, `al.q_int_us` | Adaptation-layer (6LoWPAN) direct TX queue, the same three |
| `al.red_drop` | Packets dropped by the adaptation layer's random early detection (queue full) |
| `ip.tx`, `tx_bytes` | IP packets sent |
| `ip.no_route`, `rx_drop`, `frag_tx_err`, `frag_rx_err`, `buf_fail` | Errors |
| `rpl.parent` | Preferred parent's node ID (null: none) |
| `rpl.rank`, `dag_rank`, `min_hop_inc` | Rank, rank / MinHopRankIncrease |
| `rpl.parent_changes` | The stack's counter of parent changes to a better route cost (not every change: scenarios count changes from the samples) |
| `rpl.parent_tx_fail`, `local_repair`, `global_repair`, `etx_parent` | RPL counters, ETX of the parent |
| `ws.routing_cost`, `rsl_in_dbm`, `rsl_out_dbm`, `join_state` | Wi-SUN state (routers) |
| `ws.pas_tx`, `pa_tx`, `pcs_tx`, `pc_tx`, `nbr_add`, `nbr_remove` | Wi-SUN discovery messages sent, neighbours added and removed |
| `ctl.dis`, `ctl.dio`, `ctl.dao` | RPL control messages sent: every trickle DIO, the DIS of the bootstrap and of the parent search, every DAO (unicast DIOs answering a DIS are not counted) |
| `app.<flow>` | `tx`, `tx_err`, `skip`, `rx`, `rx_bytes`, `echo_tx`, `echo_rx`; `app.sock`: the socket's `tx_done`, `tx_fail`, `no_route` events |

In the split programs (`wsbrd`, `wsnode` with `wshwsim`) the MAC runs in `wshwsim`: the `mac` counters stay 0 there. The per-frame confirmations are counted through the MAC API's request and confirmation callbacks, wrapped only when `stats` is on.

**Cost.** A periodic sample is one timer event per node and period: it adds one wake per period to the power ledger (about 0.3 % of a dag_simple router's wakes, about 30 per second, at a 10-s period). A scenario's samples are requests from the conductor (`ask`) whose wakes are not charged: a dag_simple run polled every 5 s for 300 s gave a power report identical, node by node, to the same run without polls, and the same node logs apart from the `NET1` and `CTL1` lines.

## 5. `report.json` (format `wssim-report 1`)

`python3 sim_utils/report.py <run dir> [--json OUT] [--top N] [--pdr-min R] [--latency-factor F] [--tail S] [--quiet]`, run by `run.sh` at the end (§2.1). It streams every log once: 4.4 s for a 150-node, 1955-s run with 1.7 GB of logs. Times are virtual seconds since the start of the run unless the name says otherwise; `null` when a value does not exist.

| Key | Content |
| --- | --- |
| `format`, `run_dir` | `"wssim-report 1"`, the run directory |
| `run` | `seed`, `nodes`, `routers`, `virtual_s` (reached), `vtime_end_s`, `end_reason`, `wall_s`, `conf`, `medium_preset`, `sim_opts`, `power_profile`, `load` (the `load_*`/`stats*` options), `scenario` (its file) |
| `topology` | At the end: `nodes[]` with `node`, `parent`, `hops`, `rank`, `dag_rank`, `children`, `subtree` (routers below), `rssi_to_parent_dbm` (from `topology.txt` when the link is given there), `rsl_in_dbm`/`rsl_out_dbm` (the stack's averages), `in_br_table`, `position` (`[x, y, z, floor]` with `--positions`); `hops_histogram`, `unreachable`, `max_hops`, `max_children`, `br_table_size`, `br_table_t_s`, `source` (routers' `NET1` parents, else the border router's table) |
| `join` | `nodes[]`: per router the first time of each milestone (`parent`, `auth_ok`, `operational`, `ready`, as `analyze.py`) and its number of `instances`; `ready`, `of`, `ready_median_s`, `last_ready_s`, `operational` |
| `traffic` | `null` without load. `window_s` (first send to the end minus `tail_s`), `flows.<flow>` (§5.1, the whole load period: its tables by hop count mix every load step), `skipped`; with a ramp or a scenario `steps[]` (§5.2), `saturation` (§5.3) and `light_load` (the first step's flows with their tables by hop count: the RTT per hop without load); `queue_warning` (§6.1) |
| `nodes` | `table[]`: per node over the load period: `hops`, `children`, `subtree`, `mac_q_mean`, `al_q_mean` (mean depths, frames), `mac_q_peak`, `al_q_peak`, `queue_drops`, `mac_tx`, `uc_frames` (unicast frames confirmed), `uc_retry_hist`, `retries_per_frame` (per unicast frame delivered), `give_up`, `give_up_by_reason`, `sock_tx_fail` (the load socket's TX failures), `retry_attempts`, `cca_attempts`, `cca_fail`, `rx`, `parent_changes`, `parent_tx_fail`, `no_route`, `control_messages`, `score`, `reasons[]`; `top_bottlenecks[]` (`node`, `score`, `reasons`); `score` (its definition) |
| `network` | Totals: `mac_frames`, `mac_unicast_frames`, `retries_per_frame`, `give_up`, `sock_tx_fail`, `queue_drops`, `parent_changes`, `control_messages` |
| `medium` | From `simserver.log`: `outcomes` (per receiver: `ok`, `noise`, `interference`, ...; [MEDIUM.md](MEDIUM.md) §7), `loss_share` (lost / heard while listening), `collision_share` ((interference + captured + busy) / heard), `frames_sent`, `frames_delivered`, `wakes`, `reached_s`, `wall_s`, `preset`, `cca_queries`, `cca_with_frame`, `end_reason` |
| `power` | `null` without `POWER`. `nodes[]`: `node`, `avg_current_ma`, `charge_uc`, `energy_mj`, `time_share` (MCU active, radio listen, receive, TX), `charge_share` (per MCU and radio state), `battery_life_years` (with `POWER_CELL`); `totals`: `routers_mean_ma`, `routers_max_ma`, `network_charge_c`; `run_time_s`, `profiles` |
| `scenario` | With `SCENARIO`: `name`, `status`, `summary`, `phases`, `events`, `labels` ([SCENARIOS.md](SCENARIOS.md) §7) |

### 5.1 A flow's figures

`sent`, `send_errors`, `delivered` (the target received it), `echoed` (the originator got the echo), `echoed_flow` (its packets were requests, from the `LOAD1 tx` type: a flow whose echoes are all lost still counts as echoed), `bytes`, `pdr`: `echoed / sent` for an echoed flow, `delivered / sent` for a one-way flow, and for `mcast` `receptions / expected_receptions`, where each group packet is expected at every member router operational when it was sent (their `LOAD1 cfg` and join milestones), and at any router that received it anyway; `delivery` (`delivered / sent`); `rtt_ms` and `latency_ms` (`n`, `min`, `mean`, `p50`, `p95`, `max`; percentiles by linear interpolation), `rtt_ms_by_hops` and `latency_ms_by_hops` (the same per hop count, §3), `offered_pps`, `delivered_pps`, `offered_bps` (payload bits). Packets sent in the last `--tail` seconds (20) are left out: they may still be in flight.

### 5.2 Steps

One row per load step: the ramp's steps (`load_ramp_*`), or the scenario's load settings (each `load` of a phase and each iteration). `tag` (the packets' step byte), `label`, `t_from_s`, `t_to_s`, `flows` (§5.1, packets of that step only), `queues` (`mean_max`: the highest mean queue depth of a node, `mean_max_node`, `drops`, `give_up`, `retries_per_frame`; from the `NET1` samples at or before the step's bounds, so their period sets the resolution), and for scenarios `rate_pps`, `interval_s`.

### 5.3 Saturation

Per flow, a step is **saturated** when its delivery ratio is below `--pdr-min` (0.99) or its p95 RTT (the one-way p95 for a flow without echo) is above `--latency-factor` (3) times the flow's first step's. `saturation.flows.<flow>`: `saturated_step`, `saturated_offered_pps`, `last_good_step`, `capacity_offered_pps` and `capacity_delivered_pps` (those of the last step before), `max_delivered_pps`; overall: `saturated_step` (the earliest over the flows) and `last_good_step` (the step before it), `criterion`. The last step ends with the counted window (`--tail`). This rule reads a ramp's fixed steps once; a scenario's capacity search has its own statistical rule ([SCENARIOS.md](SCENARIOS.md) §4.4).

### 5.4 Bottlenecks

`score` = the node's mean queue depth over the load period (adaptation layer plus MAC, frames) + 0.1 × its queue drops per minute + 0.01 × its unicast frames given up. `reasons` lists what applies: a mean queue of 0.5 frames or more, queue drops, 0.3 or more retries per unicast frame, unicast frames given up by the MAC, more than 5 % channel-access failures, the routers below it, and for node 0 that every packet of the network goes through it.

### 5.5 Several runs

`report.py --aggregate <run>... [--json OUT]` (format `wssim-report-aggregate 1`): per figure the `n`, `median`, `min` and `max` over the runs (their `report.json`, made if missing): join times, `max_hops`, retries per frame, queue drops, per flow `pdr`, `rtt_mean_ms`, `rtt_p95_ms`, `rtt_mean_ms_by_hops`, the ramp's capacity, the scenarios' capacities, the routers' mean current.

## 6. Demonstration (dag_simple, 15 nodes, load ramp)

`LOAD=up LOAD_START_S=300 LOAD_INTERVAL_S=20 LOAD_DIST=exp LOAD_RAMP_STEP_S=120 LOAD_RAMP_FACTOR=1.6 LOAD_RAMP_STEPS=7 POWER=examples/power/efr32fg25-soc.ini COMPACT=1`, seed 1, 1180 virtual s, default medium preset: 10 s of wall time.

| Step | Offered (packets/s) | Delivered (packets/s) | PDR | RTT p95 (ms) | Highest mean queue (frames, node) | Retries per unicast frame | Queue drops |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 0 | 0.89 | 0.89 | 1.000 | 739 | 0.08 (node 0) | 0.04 | 0 |
| 1 | 0.98 | 0.98 | 1.000 | 719 | 0.07 (node 0) | 0.07 | 0 |
| 2 | 1.77 | 1.77 | 1.000 | 984 | 0.12 (node 0) | 0.13 | 0 |
| 3 | 2.73 | 2.73 | 1.000 | 1041 | 0.20 (node 0) | 0.14 | 0 |
| 4 | 4.61 | 4.61 | 1.000 | 1290 | 0.40 (node 0) | 0.18 | 0 |
| 5 | 7.27 | 7.27 | 1.000 | 3828 | 2.88 (node 3) | 0.27 | 0 |
| 6 | 12.21 | 11.37 | 0.931 | 51226 | 109.3 (node 8) | 0.60 | 71 |

The p95 RTT stays within 1.3 s up to 4.6 packets/s and is 5.2 times the first step's at 7.3 packets/s: step 5 is the first saturated one, and the capacity is 4.6 packets/s offered and delivered. At 12.2 packets/s the delivery falls to 93 % and the adaptation-layer queues of the routers near the border router fill: router 8 (hop 1, four routers below it) holds 109 frames on average in that step, its peak is 173 frames and it drops 80 packets; the border router's queue peaks at 119 frames. The retries per unicast frame delivered grow with the load, from 0.04 to 0.60. At light load (step 0), the mean RTT by hop count is 191, 342, 506 and 649 ms for 1 to 4 hops (`traffic.light_load`); over the whole ramp the tables by hop count mix every load. The routers' mean current: 9.65 mA (`efr32fg25-soc` profile, radio always listening).

### 6.1 Queue limit

The same ramp with `QUEUE_LIMIT=32` (the stack's random early detection between 16 and 32 frames, instead of its heap-sized thresholds) gives identical steps 0 to 5, where no queue goes beyond a few frames: the capacity found is the same. Past saturation it differs:

| Step 6 (12.2 packets/s offered) | Without a limit | `QUEUE_LIMIT=32` |
| --- | --- | --- |
| Delivered (packets/s), PDR | 11.37, 0.931 | 9.55, 0.782 |
| RTT p95, max (ms) | 51 226, 57 311 | 19 614, 25 667 |
| Highest mean queue (frames, node) | 109.3 (node 8) | 27.2 (node 8) |
| Adaptation-layer peaks, routers 8 / 0 (frames) | 173 / 119 | 37 / 36 (random early detection drops on a running average of the depth, so a peak passes the limit a little) |
| Queue drops (whole run) | 80 at router 8 | 236 at router 8, 42 at the border router |

Without a limit, delays past saturation include waits in queues far larger than a device holds, and the report flags such runs (`traffic.queue_warning`, here: router 8's peak of 173 frames, step 6); the saturation point and the capacity, decided at lower loads, do not depend on it here. The value is a device setting (the heap left for packets of the firmware in question): `QUEUE_LIMIT` is the user's, with no default.

## 7. Limits

- The load runs in the Nanostack programs (`wsbrd-inproc`, `wsnode-inproc`); clients of the radio interface ([RADIO_INTERFACE.md](RADIO_INTERFACE.md)) have their own traffic and print `MOP1` lines instead.
- Mean queue depths over a step come from the `NET1` samples: with a 10-s period, a step's bounds are known within 10 s.
- The RPL control messages counted are those sent from the other objects of the stack (§4); unicast DIOs answering a DIS are not.
- Without `queue_limit`, queues near the border router grow to hundreds of frames past saturation and RTTs reach tens of seconds where a device would drop packets (§6.1); `traffic.queue_warning` flags such runs.
- A conf key line is read up to 255 characters (a long `load_nodes` list goes in a scenario's command instead).
