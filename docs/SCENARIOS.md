# Measurement scenarios

**Domain: internal.** A scenario turns a virtual-time run into a measurement: wait until the network has formed, measure the RTT per hop count, search the capacity, add interference or fail a router, measure the repair and the recovery. It is a JSON file of **phases**, each read as one sentence:

> Start when [trigger]; set [load]; do [actions]; measure [set]; end when [condition] or after [cap].

Ready-made **building blocks** expand to phases, so a full study is a few short entries. This note is the contract for the tools that write and read scenarios (the mesh testbed controller's scenario editor and report page). The load, the statistics and `report.json` are in [TRAFFIC_AND_REPORT.md](TRAFFIC_AND_REPORT.md).

```bash
SCENARIO=sim_utils/scenarios/capacity_interference.json POWER=examples/power/efr32fg25-soc.ini TUN=none \
  sim_utils/vtime/run.sh build-vt $(pwd) runs/sc 8000        # 8000: a cap; the scenario ends the run
python3 sim_utils/scenario.py --check  FILE    # errors as JSON {path, message}
python3 sim_utils/scenario.py --dry    FILE    # expanded phases, upper bound of the virtual duration
python3 sim_utils/scenario.py --schema         # JSON Schema (titles, descriptions, units, defaults, enums)
python3 sim_utils/scenario.py --blocks         # the building blocks and their parameters
```

## 1. Architecture

Conditions depend on measured values (the RTT of the previous step, whether the mesh is repaired), so the run must be observed and changed while it runs, and stay reproducible from its seed.

**Choice: a driver outside the conductor, which pauses virtual time.** `wssimserver --vtime-control=SOCKET` accepts one driver (`sim_utils/scenario.py drive`, started by `run.sh`). The conductor runs the events up to the time the driver allowed (`run_until`), then pauses: every node is idle and has written its log up to that time. The driver reads the new log lines (`sim_utils/runlog.py`, incrementally), evaluates the phase's conditions, and sends commands, which act at the pause time: load settings and statistics requests to the nodes (an `"xL"` message the conductor delivers like a frame, handled by `app_load.c`), node failures and restarts, link cuts, interferers, power snapshots. Decision points are virtual times that the scenario fixes (phase boundaries, iterations, polls every `poll_s`), and what the driver reads at them depends only on the seed, so a run is reproducible from its seed and its scenario file.

**Measurement is not part of the measured run.** Statistics and topology requests at polls go through the conductor's `ask` command: the nodes print a `NET1` line (and the border router its table) during a wake the power ledger does not charge, and nothing else changes in them. A dag_simple run polled every 5 s for 300 s gave a power report identical, node by node, to the same run without polls, and node logs identical apart from the `NET1` and `CTL1` lines.

**Failures are loud.** Every command to the nodes carries a sequence number, and each node answers it in its log (`CTL1 ok|err <seq>`); the driver checks the answers at the next pause. A refused command, an interferer the conductor refuses, any error in the driver: the driver writes `scenario_result.json` with `status: "error: ..."` (and the traceback for an unexpected one) and exits 1, and `run.sh` fails, as it does without a result. A driver that disappears (its connection closed without `quit`) ends the run with status 1 (`scenario driver lost` in `simserver.log`). `on_timeout: "abort"` ends a scenario as it says: status `aborted: ...`, not a failure.

Why not inside the conductor: the metrics come from the nodes' application and stack (RTT per hop, queues, RPL parents), which the conductor does not see; the driver reuses the report's log reader, and the scenario logic (Python) stays out of the C server. The cost is one pause per decision point: a 150-node run of 1955 virtual s with polls every 5 s took 178 s of wall time.

Pieces:

| Piece | Where |
| --- | --- |
| Control socket, pause, commands (§8) | `app_wssimserver/wssimserver.c` (`ctl_*`) |
| Interferers added and removed during the run; `from=`, `until=` | `app_wssimserver/sim_interf.c` ([MEDIUM.md](MEDIUM.md) §13.1) |
| Power snapshots | `app_wssimserver/sim_power.c` (`pwr_snapshot()`) |
| Commands in the nodes (`load`, `stats`, `tree`, `iface`) | `app_wsbrd/app_load.c`, `net_stats.c`, `wsbr.c`, `wsrouter.c`; `"xL"` in `app_wshwsim/sl_rf_driver.c` |
| Vocabulary, schema, blocks, validator, dry check | `sim_utils/scenario_schema.py` |
| Driver, command line | `sim_utils/scenario.py` |
| Examples | `sim_utils/scenarios/*.json` |
| Tests | `sim_utils/vtime/test_report.py` (validator), `sim_utils/vtime/test_scenario.sh` (live: every action, then the failure cases) |

## 2. File

```json
{
  "format": "wssim-scenario 1",
  "name": "Per-hop RTT, capacity, interference, recovery",
  "description": "...",
  "settings": {"poll_s": 5, "drain_s": 10, "load": {...}},
  "phases": [ PHASE or BLOCK, ... ]
}
```

Only `format` and `phases` are required. The JSON Schema (`--schema`, committed as `docs/scenario.schema.json`) carries every rule a form needs: per field a title, a one-line description, its unit (`x-unit`), its default and, for closed vocabularies, its enumeration; mutually exclusive fields as `oneOf`/`not` (`rate_pps`, `interval_s` and `rate_of`; `after_s` or a condition; `value` or `relative_to`; `end` or `repeat`), the fields each kind requires as `if`/`then` (a `metric` condition needs `metric`, a `node_event` needs `event` and `nodes`, `kill` needs `nodes`, ...), each composite rule with a plain-words `x-message`, and the metrics' units and directions (`x-metrics`). Fields starting with `x_` are ignored (free for an editor's own data).

| Settings | Default | Meaning |
| --- | --- | --- |
| `poll_s` | 5 | How often conditions are evaluated while a phase waits (virtual s) |
| `drain_s` | 10 | Packets sent in the last `drain_s` before an evaluation are not counted yet (their echo may still come) |
| `min_samples` | 100 | Packets a window needs before a `pdr`, `rtt` or `latency` condition is decided (§4.2) |
| `parent_min_dbm` | −80 | Links at or below this level cannot carry a new parent (the stack's `DEVICE_MIN_SENS` −93 dBm + `CAND_PARENT_THRESHOLD` 10 dB + hysteresis 3 dB): routers that would need them are `marginal`, not repairable (§6.2) |
| `load` | none | Defaults of every phase's `load` |

## 3. Phase

| Field | Default | Meaning |
| --- | --- | --- |
| `name` | required | Unique; later phases refer to it |
| `start` | right after the previous phase | A condition (§5) with `timeout_s` (3600) and `on_timeout`: `start` (start anyway, flagged in the result), `skip` (skip the phase), `abort` (end the scenario) |
| `load` | `keep` | The load from now on (§4.1), `off`, or `keep`; set first, so that the actions happen under it |
| `actions` | none | Done in order at the start, after the load (§6) |
| `measure` | `{"sets": ["traffic"]}` | `sets`: any of `traffic` (delivery, RTT and one-way delay overall and per hop count, throughput), `queues` (per node: queue depths, drops, retries, channel-access failures), `mesh` (parents, depths, orphans, parent changes, RPL control messages, border router table), `power` (charge and average current per node in the phase, needs `POWER`), `medium` (frames, airtime, reception outcomes); `settle_s` (0): time after the start not measured |
| `end` | `{"after_s": 60}` | Either `after_s` alone (a fixed duration), or a condition (§5) with `max_s` (600), not both: the phase ends when it holds, or after `max_s` (flagged) |
| `repeat` | none | Instead of `end`: iterations at a changing load (§4.4) |

Reference times: a `start` condition's `elapsed` and node events count from the previous phase's end; an `end` condition's from the phase's start.

### 4.1 Load

| Field | Default | Meaning |
| --- | --- | --- |
| `flows` | `["up"]` | `up`, `down`, `mcast`, `p2p` ([TRAFFIC_AND_REPORT.md](TRAFFIC_AND_REPORT.md) §2.2) |
| `rate_pps` | | Offered packets per second of each flow, whole network. The driver turns it into each member's interval (members / rate; `mcast`: 1 / rate) |
| `interval_s` | 30 | Instead: each member's mean interval |
| `max_rate_pps` | | With `interval_s`: at most this rate (a light probe whatever the size of the network) |
| `rate_of` | | Instead: `{"phase", "metric", "factor"}`, a value of an earlier phase × factor, e.g. 80 % of `capacity_pps` |
| `payload_b` | 64 | UDP payload (bytes) |
| `dist` | `exp` | `fixed` or `exp` (Poisson) |
| `nodes` | every router | A node selector (§6.1): the members. Only those with a route to the border router at that moment take part (a parent chain in their latest samples); the others are listed in the result (`left_out`): a router cut off by an earlier failure would only lower the delivery ratio. A router the scenario stops is excluded from the load until it starts again (`load exclude`, `include`) |
| `echo` | true | Targets answer |

A node started again by the scenario gets the load in force. Each load setting gets a number (the packets' step byte), recorded with its phase, iteration, time and rate: the report's `traffic.steps` are these settings.

### 4.2 Measurements

A fixed-duration phase measures the packets sent from `start + settle_s` to `end − drain_s`, with what arrived by the end. A condition phase measures the same at its end. A metric condition evaluated while waiting uses a trailing window: packets sent in `[now − drain_s − w, now − drain_s)`, false until the window is full (it starts at the phase's start plus `settle_s`); `w` is `window_s`, made longer (up to 10 ×) when the load in force would send fewer than `min_samples` packets in it. **A `pdr`, `rtt` or `latency` condition with fewer than `min_samples` packets sent in its window is not decided** (false, with the count in its detail). `queues` and `mesh` take the nodes' `NET1` counters at the window's bounds: the driver asks every node for a sample then (and at each poll when a condition needs them).

### 4.3 Metrics (closed list)

| Metric | Unit | Set |
| --- | --- | --- |
| `pdr` | ratio | traffic: echoes / requests sent; `mcast`: receptions / (sends × members expected to receive) |
| `rtt.mean_ms`, `rtt.p50_ms`, `rtt.p95_ms`, `rtt.max_ms`, `rtt.min_ms` | ms | traffic: round-trip time |
| `latency.mean_ms`, `latency.p95_ms`, `latency.max_ms` | ms | traffic: one-way delay |
| `offered_pps`, `delivered_pps` | packets/s | traffic, whole network (measured) |
| `queue.mean_max`, `queue.now_max` | frames | queues: the highest mean (or current) depth of a node, MAC plus adaptation layer |
| `queue.drops` | packets | queues: dropped by full adaptation-layer queues |
| `mac.retries_per_frame`, `mac.give_up`, `mac.cca_fail_rate` | retries, frames, ratio | queues: retries per unicast frame delivered, unicast frames the MAC gave up ([TRAFFIC_AND_REPORT.md](TRAFFIC_AND_REPORT.md) §4) |
| `mesh.joined_fraction`, `mesh.orphans`, `mesh.parent_changes`, `mesh.max_hops` | ratio, nodes, changes, hops | mesh (parent changes: changes to a new parent seen in the samples) |
| `power.avg_ma` | mA | power: mean over the routers of their average current |
| `medium.frames_per_s`, `medium.airtime_s_per_s`, `medium.loss_share` | frames/s, s/s, ratio | medium |

A capacity search also gives `capacity_pps` (for `rate_of` and `relative_to`). Without `op`, a metric condition holds when the value is **worse than** its threshold: `>=` for delays, queues, retries, give-ups, orphans, power and the medium's figures; `<` for `pdr`, `delivered_pps` and `mesh.joined_fraction`.

### 4.4 Iterations (capacity search)

`repeat`: at most `max` (12) windows. At each step the rate is multiplied by `mul` (1.5) or raised by `add_pps`; `settle_s` (15) at the new load, then a window of `window_s` (60), made longer (up to 4 ×) to hold 1.25 × `min_samples` packets on average at the rate, and `drain_s`; the `until` condition (§5) is evaluated on the window's measurements.

**Statistical rule.** A step **saturates** when `until` holds on `confirm` (2) windows in a row at the same rate, each with at least `min_samples` (100) packets sent; a single window over the threshold is measured again rather than taken. The search ends at the first saturating step: the **capacity** is the *measured* offered rate of the step before (`capacity_pps`; the nominal rate asked for is `capacity_nominal_pps`: they differ by the Poisson draws and the routers left out), with its delivered rate; the saturating rate is recorded. When the first rate already saturates, the search goes down (rate divided by `mul`) until a step does not: its measured rate is the capacity. When no step saturates, the capacity is at least the last rate (`reached: false`). The result also records the baseline phase each relative condition used and whether it had `min_samples` packets (`capacity.baseline`).

A baseline measured before a disruption is not the network's after it: the `capacity_check` block measures its own probe first (`baseline: "self"`, the default).

## 5. Conditions (`is`)

| Kind | Fields | True when |
| --- | --- | --- |
| `immediately` | | At once |
| `time` | `t_s` | Virtual time reached |
| `elapsed` | `s` | `s` seconds after the reference time |
| `formed` | `fraction` (1), `milestone` (`ready`), `stable_s` (0), `nodes` | That share of the running routers (or of `nodes`) has reached the milestone in its current instance and is in the border router's routing table now, and no parent changed anywhere for `stable_s` |
| `node_event` | `event`, `nodes`, `milestone` (`operational`) | Every selected node had the event since the reference time: `joined` (the milestone), `parent_changed`, `parent_lost`, `restarted` |
| `repaired` | `e2e` (false) | The last disruption's repair as tracked (§6.2): every repairable router it cut off has a route to the border router again (a parent chain that avoids the failed nodes); with `e2e`, also in the border router's table and an echo received for a packet sent after the disruption |
| `metric` | `metric`, `op` (worse than, §4.3), `value` or `relative_to` (not both), `per_hop`, `flow`, `window_s` (60), `min_samples` | The metric (§4.3) compared with `value`, or with `relative_to` = `{"phase", "metric" (the same), "factor" (1)}`: an earlier phase's value × factor. `per_hop`: true if it holds at any hop count, each against the same hop count of the earlier phase. `flow`: `up` if running, else the first |
| `all`, `any` | `of` | All, or any, of a list of conditions |

The milestones are those of `analyze.py`: `operational` (Wi-SUN join done) and `ready` (`ARM_NWK_BOOTSTRAP_READY`; not repeated when an interface comes back up in the same program).

## 6. Actions (`do`)

| Action | Fields | Effect |
| --- | --- | --- |
| `kill` | `nodes`, `for_s`, `label` | Power off: the conductor closes the node's connection; its radio is off; with `POWER` it is then charged as asleep with the radio off. With `for_s`: a fresh instance after that time |
| `restart` | `nodes`, `label` | Power off, then a fresh instance at once (same options and key storage; log `node<i>.r<k>.log`, `br.r<k>.log`; its clock starts at 0, as at power-up). Node 0 is the border router |
| `leave` | `nodes`, `for_s` | The node's interface goes down (`arm_nwk_interface_down()`), up again after `for_s` |
| `cut_links` | `nodes`, `peers`, `for_s` | Every link of the nodes (to `peers` only, if given) cut in both directions |
| `partition` | `a`, `b`, `for_s` | Every link between the groups `a` and `b` cut |
| `degrade_links` | `nodes`, `rssi_dbm`, `for_s` | Every link of the nodes at this RSSI (restored after `for_s` when `topology.txt` gives its level) |
| `interferer_add` | `spec`, `name`, `for_s` | An external interferer from now ([MEDIUM.md](MEDIUM.md) §13.1 `SPEC`, the syntax of the controller's spectrum page), central medium |
| `interferer_remove` | `name` | That interferer (or `all`) stops now |
| `mark` | `text` | A line in the logs and the result |

`label` names the nodes an action picked, for later selections (`{"select": "label"}`), e.g. to restart the router that was killed. `for_s` schedules the undo at that virtual time, whatever phase is running then.

### 6.1 Node selectors (`select`)

Resolved from the live topology when the action runs: the driver asks every router for its RPL parent and the border router for its table at that moment.

| `select` | Fields | Candidates |
| --- | --- | --- |
| `id`, `ids` | `id`, `ids` | Given nodes |
| `all` | | Every running router |
| `hop`, `br_children` | `hop` | The routers at that depth (`br_children`: 1) |
| `ancestor` | `of`, `hop` | The node at that depth on the path of node `of` |
| `storey`, `area` | `storey`, `box` [x0, y0, x1, y1] (m) | By position (`--positions`: the FLOOR column, else Z / 3.5 m) |
| `label` | `label` | The nodes an earlier action picked |

Then `pick`: `all` (default), `largest_subtree` or `most_children` (the worst case; `count` of them), `random` (`count`, drawn from the seed), `fraction` (drawn from the seed), `first`. The result records the nodes picked and why ("routers at hop 1, largest subtree (66: 24 below)").

### 6.2 Disruptions and mesh metrics

`kill`, `leave`, `cut_links`, `partition` and `degrade_links` start a **disruption**: its time, the failed nodes (killed or left), the routers cut off (the failed nodes' subtrees at that moment; with links cut or degraded, the nodes themselves too; for a partition, the side without the border router), split by what the hearing graph still offers (the conductor's link levels, without the failed nodes and cut links): **repairable** (a path over links above `parent_min_dbm`), **marginal** (a path only over weaker links: the stack does not take such a parent; recorded apart, with the time any of them found a route, `marginal_repaired_s`) and **isolated** (no path).

Each disruption is **tracked from its start until it is repaired or the scenario ends**, whatever phase runs and whatever the conditions ask: the driver polls every `poll_s` while a disruption is open. It records:

- `detect_t_s`: the first poll where a cut-off router has another parent (or none): the first to notice;
- `repair_t_s`: the first poll where every repairable router has a route (a parent chain to the border router that avoids the failed nodes); `repair_e2e_t_s`: the first poll where, besides, each is in the border router's table and, when it takes part in an echoed `up` flow, got an echo for a packet sent after the disruption; `orphan_s` per router and `orphan_time_s` (statistics) until its route;
- `churn`: changes to a new parent seen in the polls, the stack's better-parent counter, depth changes, RPL control messages sent (DIS, DIO, DAO), from the disruption to the end-to-end repair;
- `packets_during_repair` (per flow, sent and lost) and `packets_of_cut_off_routers` (their uplink packets, sent and lost);
- `depths_before`, `depths_after`, `parents_before`; `back` (failed nodes started again or back up, with their time: a repair may then go through them);
- `repaired`, `repaired_e2e` (true or false), `tracked_until_s`, `open_at_end` (true: the scenario ended before both repairs; the figures are then up to its end).

The resolution of these times is `poll_s`. The `repaired` condition reads the last disruption's tracking: a later phase that starts "when repaired" waits for it, and a disruption repaired earlier is seen as repaired at once.

## 7. Result

`scenario_result.json` (format `wssim-scenario-result 1`) in the run directory, also in `report.json` → `scenario`:

| Key | Content |
| --- | --- |
| `status` | `completed`; `aborted: <reason>` (`on_timeout: "abort"`); `error: <reason>` (the run failed; `traceback` for an unexpected error) |
| `phases[]` | `name`, `block`, `index`; `start` (`t_s`, `met`, `detail`: why it started, `timeout`, `on_timeout`, `condition`); `actions[]` (`do`, `nodes`, `why`, `affected`, `marginal`, `isolated`, `links`, `result`); `metrics` (per set: `flows` as [TRAFFIC_AND_REPORT.md](TRAFFIC_AND_REPORT.md) §5.1, `queues` with `per_node`, `mesh`, `power` with `per_node`, `medium`); `load`; `end` (`t_s`, `met`, `detail`, `timeout`); `iterations[]` (one per window: `iteration`, `step_tag`, `rate_pps` (nominal), `offered_pps` and `delivered_pps` (measured), `samples`, `interval_s`, `t_s`, `met`, `detail`, `direction`, `confirmation` (0: the step's first window), `metrics`); `capacity` (`capacity_pps` (measured), `capacity_nominal_pps`, `delivered_pps`, `saturated_rate_pps`, `saturated_offered_pps`, `reached`, `searched`, `confirm`, `min_samples`, `baseline`, `detail`); `disruptions[]` (§6.2); `skipped`, `aborted` |
| `steps` | Per load setting (`tag`): `phase`, `iteration`, `t_s`, `rate_pps`, `interval_s`, `flows`, `members`, `payload_b` |
| `events[]` | `t_s`, `text`: everything the driver did, also in `scenario.log` and `simserver.log` |
| `labels` | Label → nodes |
| `summary` | Per phase: start and end times and why, PDR, RTT (`n`, `mean`, `p95`, `max`) overall and per hop count, offered rate, capacity, disruptions, power |

With `POWER`, `power_snap.jsonl` holds one line per snapshot (`tag`, `t_us`, per node `[id, charge_uc, mcu_uc, listen_uc, receive_uc, tx_uc, other_uc]`), at every phase's and iteration's bounds.

## 8. Control protocol (`wssimserver --vtime-control`)

One command per line on a UNIX stream socket, one answer line each. The first line from the server is `at <µs>` (paused after the nodes' first run). When the run ends (end time, a node failure), the server sends `ended <µs> <reason>`.

| Command | Answer | Effect |
| --- | --- | --- |
| `run_until <µs>` | `at <µs>` at the next pause | Events up to that time run (events pushed at the current time included) |
| `now` | `at <µs>` | |
| `ask <IDS\|all> <text>` | `ok <n>` | As `node`, for a measurement request (`stats`, `tree`): a wake with only such messages is not charged in the power ledger |
| `node <IDS\|all> <text>` | `ok <n>` | `"xL"` message with the text to these Nanostack nodes, at the current time (delivered at the next `run_until`): `#<seq> ` then `load set KEY=VALUE ...`, `load stop`, `load exclude IDS`, `load include IDS`, `stats`, `tree` (border router), `iface down\|up`; each node answers `CTL1 ok|err <seq>` in its log |
| `kill <id>` | `ok` | As `--vtime-kill` (IDs and arguments are checked strictly: `kill abc` is an error) |
| `start <id>` | `ok` | Accepts the new instance the driver started (it connects to the simulation socket) and welcomes it now |
| `cut <src> <dst>`, `uncut <src> <dst>` | `ok` | Frames from src no longer (again) reach dst |
| `link <a> <b> <rssi>` | `ok` | As `--vtime-link` |
| `neighbors <id>` | `ok <id>:<dBm> ...` | The hearing graph, with each link's stored mean level (the weaker direction) |
| `intf add <SPEC>` | `ok <name>` | A new interferer from now; a bad SPEC is answered `err <reason>` (the server goes on); the slot of a removed interferer is reused |
| `intf remove <name\|all>` | `ok <n>` | |
| `counters` | `ok t_us=.. wakes=.. frames_in=.. frames_out=.. airtime_us=.. ok=.. noise=.. ...` | Totals so far |
| `power snap <file> <tag>` | `ok` | Appends a snapshot line |
| `log <text>` | `ok` | `scenario: <text>` in `simserver.log` |
| `quit` | `bye` | Ends the run (a closed connection without it fails the run) |

`run.sh` with `SCENARIO`: checks the file, starts the server with `--vtime-control` (socket `<SOCK>.ctl`), the programs with `load_control = true` and `stats = true`, then the driver; the driver ends the run (`<seconds>` is a cap). `programs.txt` holds every program's command line, for restarts. A node the scenario stopped does not fail the run.

## 9. Building blocks (`use`)

| Block | Parameters (defaults) | Phase |
| --- | --- | --- |
| `formed` | `fraction` (1), `stable_s` (60), `timeout_s` (3600) | Start when that share of routers is ready and in the border router's table, parents stable; ends at once |
| `probe` | `rate_pps` (one packet per router every 30 s, at most 0.5/s), `flows` (up), `duration_s` (300), `settle_s` (30), `payload_b` (64) | Light load; every set measured: the baseline |
| `capacity` | `baseline` (`probe`), `start_rate_pps` (2 × the baseline's), `mul` (1.5), `max_steps` (12), `window_s` (60), `settle_s` (15), `confirm` (2), `rtt_metric` (`rtt.p95_ms`), `rtt_factor` (2), `max_rtt_ms`, `min_pdr` (0.95), `flows`, `payload_b` (64) | Iterations until the RTT reaches `rtt_factor` × the baseline's, the largest RTT `max_rtt_ms`, or the delivery falls below `min_pdr` |
| `interference` | `spec` (required), `interferer_name` (`jam`), `capacity_phase` (`capacity`), `load_fraction` (0.8), `duration_s` (300), `settle_s` (30) | Interferer added; that share of the capacity |
| `recovery` | `interferer_name` (`all`; empty: none removed), `baseline` (`probe`), `within` (1.2), `max_s` (600) | Interferer removed, same load; ends when the RTT p95 over 60 s is within that factor of the baseline's |
| `node_failure` | `nodes` (hop 1, largest subtree), `label` (`failed`), `capacity_phase` (`capacity`; empty: keep the load), `load_fraction` (0.5), `max_s` (600), `e2e` (true) | Kill; ends when repaired |
| `node_restart` | `label` (`failed`), `max_s` (600) | Restart those nodes; ends when they joined |
| `capacity_check` | `capacity_phase` (`capacity`), `start_fraction` (0.5), `baseline` (`self`: its own probe, `<name>_probe`, of `probe_s` 300 s first; or an earlier phase), `mul`, `max_steps` (8), `window_s`, `settle_s`, `confirm`, `rtt_factor`, `min_pdr`, `flows`, `payload_b` | A capacity search from a share of an earlier capacity, against a baseline of the network as it is now (two phases with `baseline: "self"`) |

A block's `name` defaults to the block's; give one to use a block twice. The owner's study, `sim_utils/scenarios/capacity_interference.json`, is five entries:

```json
{"use": "formed", "stable_s": 60},
{"use": "probe", "duration_s": 300},
{"use": "capacity", "max_rtt_ms": 5000, "window_s": 60},
{"use": "interference", "spec": "chan=0-17,level=-75,kind=fsk,pattern=ge:40ms:60ms", "load_fraction": 0.8, "duration_s": 240},
{"use": "recovery", "max_s": 300}
```

Examples: `capacity_interference.json` (above), `node_failure.json` (formed, probe, capacity, the busiest hop-1 router killed under 50 % of the capacity, capacity after the repair, the router restarted), `link_cut_layers.json` (hand-written phases: a steady load from 300 s, every link of a random hop-2 router cut for 120 s, then until the delivery is back to 99 %).

## 10. Validation

`scenario.py --check FILE` prints `{"ok": bool, "errors": [{"path": "/phases/2/load_fraction", "message": "must be more than 0"}]}` and exits 1 on errors. One round reports everything: the schema's rules (types, enumerations, bounds with their units, unknown fields, the fields each kind requires, mutually exclusive fields) and the meaning (unique names; `relative_to` and `rate_of` naming an earlier phase, `capacity_pps` a phase with `repeat`; labels defined before use; `repaired` only after a disruption; interferer `SPEC`s checked key by key with the server's ranges). Paths are JSON Pointers into the file as written: an error inside a block points at the block's own parameter (`/phases/3/capacity_phase`), or at the block's entry with the field named. `--dry FILE` prints the expanded phases (with their block and entry) and `virtual_s_max`, an upper bound of the virtual duration (every wait and cap at its maximum, capacity windows at their longest).

## 11. Demonstrations

Seed 1 (and seed 3 for one check), `POWER=examples/power/efr32fg25-soc.ini` (radio always listening, about 8–10 mA per router), `COMPACT=1`, default medium preset (`calibrated`). Networks: `dag_simple` (15 nodes, depths 1–4) and a 150-node building on three floors, 10 m apart, links from the positions (`radio_if/scale/gen_building.py --nodes 150 --floors 3 --spacing 10`, `--positions`, `--prop-tx-dbm=20`; depths 1–3, 34 routers at hop 1). Wall time with the runs at once on the 24-core PC: 10–13 s for each `dag_simple` run, 2.9 and 3.7 min for the two building runs; 225 s for the whole set.

**`capacity_interference.json`** (the owner's study; interferer on channels 0–17 of 35, −75 dBm at every node, FSK, on 40 % of the time):

| Phase | dag_simple | 150-node building |
| --- | --- | --- |
| formed (all in the border router's table, parents stable 60 s) | 370 s | 535 s |
| probe (300 s): packets; RTT mean / p95; mean RTT by hops | 145; 361 / 732 ms; 196, 307, 524, 620 ms (1–4 hops) | 140; 333 / 654 ms; 203, 307, 489 ms (1–3 hops) |
| capacity: steps (nominal packets/s, network), 2 windows to confirm | 1.0, 1.5, 2.25, 3.38, 5.06 (p95 1629 and 2004 ms ≥ 2 × 732) | 0.97, 1.45, 2.17, 3.26, 4.89 (1559 ms, then 1285 ms: not confirmed), 7.33 (2118 and 2425 ms ≥ 2 × 654) |
| capacity found (measured offered rate) | 3.45 packets/s | 4.75 packets/s |
| interference at 80 % of it: PDR; RTT mean / p95; mean RTT by hops | 1.0; 572 / 1399 ms; 273, 558, 767, 1017 ms | 0.985; 815 / 2133 ms; 567, 797, 1242 ms |
| recovery (interferer removed): RTT p95 over the last 60 s (or more, for 100 packets) back within 1.2 × the probe's after | 295 s (817 ≤ 878 ms) | not within the 300-s cap (last value 968 ms against 785 ms) |
| routers' mean current, probe / capacity / interference | 8.90 / 9.71 / 9.47 mA | 8.26 / 8.32 / 8.29 mA |

The RTT grows with the hop count in every phase, and the interference adds most at the deepest routers. After the interferer is removed, the load stays at 80 % of the capacity, where delays run above the light-load probe's with or without interference: dag_simple came back within 1.2 × the probe's p95 after 295 s, the building not within the 300-s cap. In the building, the confirmation rule took effect: the step at 4.89 packets/s met the condition once and not on its second window, so the search went on.

**`node_failure.json`** (the busiest hop-1 router powered off under 50 % of the capacity):

| | dag_simple | dag_simple, seed 3 | 150-node building |
| --- | --- | --- | --- |
| capacity before (measured) | 3.45 packets/s | 2.68 packets/s | 4.75 packets/s |
| router killed (why) | 8 (hop 1, 4 routers below) | 8 (hop 1, 4 below) | 66 (hop 1, 22 routers below) |
| cut off: repairable / marginal / isolated | 3 / 0 / 1 (router 11 hears only router 8) | 3 / 0 / 1 | 22 / 0 / 0 |
| first noticed (`detect_t_s`) | 205 s | 205 s | 35 s |
| route and end-to-end repair | 205 s, 205 s | 205 s, 205 s | 715 s, 715 s |
| time without a route per router (min / median / max) | 205 s for the 3 | 205 s for the 3 | 35 / 515 / 715 s |
| changes to a new parent; RPL messages sent (DIS / DIO / DAO) until the repair | 1; 4 / 41 / 3 | 1; 6 / 42 / 1 | 17; 128 / 1641 / 52 |
| uplink packets of the cut-off routers during the repair: sent / lost | 61 / 14 | 50 / 0 | 197 / 34 |
| routers at hop 1, 2, 3, 4 before → after | 4, 5, 4, 1 → 3, 4, 4, 1 | 4, 5, 4, 1 → 3, 4, 4, 1 | 34, 90, 25 → 33, 89, 26 |
| fresh probe after the repair: RTT p95 (packets) | 1024 ms (131) | 859 ms (101) | 608 ms (141) |
| capacity after the repair, against that probe | 4.18 packets/s | 4.65 packets/s | 3.42 packets/s |
| restart of the killed router: joined again after | 30 s | 20 s | 30 s |

The capacity search is relative to its baseline: after the repair, `capacity_check` measures its own probe and doubles *its* RTT. In dag_simple the post-repair probe is slower (p95 1024 against 732 ms), so the threshold is higher and the capacity found is higher too; in the building the post-repair probe is faster (608 against 654 ms) and the capacity lower. Both answer "the load at which the RTT doubles from the network's own light-load RTT", not an absolute delay limit: use `max_rtt_ms` (or a fixed `value`) for that. In the previous version of the engine, seed 3 of this scenario found a capacity after the repair of 1.05 packets/s from one window 3 % over its threshold, with a pre-repair baseline of 84 packets; with the minimum of 100 packets per window, the confirmation on a second window and a fresh baseline, it finds 4.65 packets/s.

The resolution of the repair times is the poll period, 5 s. The runs: `SCENARIO=sim_utils/scenarios/<file> POWER=examples/power/efr32fg25-soc.ini COMPACT=1 TUN=none WSSIM_SEED=1 sim_utils/vtime/run.sh build-vt $(pwd) <out> 9000`, with, for the building, `TOPO=<file with "0,149">` and `SIM_OPTS="--positions=<building.pos> --prop-tx-dbm=20"`. `sim_utils/vtime/test_scenario.sh` (every action, the border router's restart included, then the failure cases) passes in about 20 s.

## 12. Limits

- Decisions and repair times have the resolution of `poll_s` (5 s by default); a phase's bounds and iterations are exact.
- Scale: each poll asks every node for a `NET1` line (about 0.9 kB) and the border router for its table; on a 1000-node network that is about 1 MB of log per poll. The driver's reader takes about 0.3 µs per log line it skips and 8 µs per `NET1` line it parses (measured on dag_simple logs): a 1000-node poll costs the driver about 10 ms plus the lines the nodes wrote since the last pause, which `COMPACT=1` makes about 10 times fewer ([TRAFFIC_AND_REPORT.md](TRAFFIC_AND_REPORT.md) §2.1). These are estimates: a 1000-node scenario was not run.
- An interferer added at run time applies to frames and sniffs that start after it; one that the medium has already computed (a frame on air) keeps its earlier levels. The medium-log line has the interferer column for the whole run when the run has a control socket and the central medium.
- `kill` with `POWER`: the node is then charged as asleep with its radio off, not as unpowered; a restarted node continues the same ledger. `--power` stays refused with the static `--vtime-kill`/`--vtime-start` options.
- A restart starts a new program with the same key storage; the border router's restart was checked once (`test_scenario.sh`: the network re-formed).
- `degrade_links` restores a link only when `topology.txt` gives its level; with positions, use `cut_links`.
- Medium settings other than interferers cannot be changed during a run.
- The driver and the conductor talk over a local socket; a run whose driver stops ends at once and fails (§1).
