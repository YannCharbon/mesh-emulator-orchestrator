# Measurement scenarios

**Domain: internal.** A scenario turns a virtual-time run into a measurement: wait until the network has formed, measure the RTT per hop count, search the capacity, add interference or fail a router, measure the repair and the recovery. It is a JSON file of **phases**, each read as one sentence:

> Start when [trigger]; set [load]; do [actions]; measure [set]; end when [condition] or after [cap].

Ready-made **building blocks** expand to phases, so a full study is a few short entries. This note is the contract for the tools that write and read scenarios (the mesh testbed controller's scenario editor and report page). The load, the statistics and `report.json` are in [TRAFFIC_AND_REPORT.md](TRAFFIC_AND_REPORT.md).

```bash
SCENARIO=sim_utils/scenarios/capacity_interference.json POWER=examples/power/efr32fg25-soc.ini TUN=none \
  sim_utils/vtime/run.sh build-vt $(pwd) runs/sc     # no duration: the scenario's own bound (§10); it ends the run
python3 sim_utils/scenario.py --check  FILE    # errors as JSON {path, message}
python3 sim_utils/scenario.py --dry    FILE    # expanded phases, upper bound of the virtual duration
python3 sim_utils/scenario.py --schema         # JSON Schema (titles, descriptions, units, defaults, enums)
python3 sim_utils/scenario.py --blocks         # the building blocks and their parameters
```

## Changes on emu-probe (contract for the controller)

This section lists what the branch adds; the sections below give the details. Older files keep working unchanged.

**Step "Switch nodes off"** (`"use": "node_outage"`, replaces `node_failure` and `node_restart` in the builder; both stay accepted, expanded as before, and are marked `x-hidden` in the schema). Fields: `name` (the step's name; default `node_outage`, `node_outage_on` for mode `on`), `mode` (`off` default, `off_then_on`, `on`), `nodes` (selector; default the hop-1 router with the largest subtree; not for mode `on`), `off_s` (mode `off_then_on` only: off at least this long), `of_step` (mode `on` only, required: the name of an earlier step that switched nodes off and has not switched them back on), `capacity_phase` (`capacity`; `""` keeps the load), `load_fraction` (0.5), `max_s` (600, the cap of each part), `e2e` (true). Expansion: `off` → one phase `<name>` (kill, ends when repaired); `off_then_on` → `<name>` (ends when repaired, and after `off_s` if given; cap max(`max_s`, `off_s`)) then `<name>_on` (restart, ends when the nodes are ready and in the border router's table); `on` → one phase `<name>`. No user-visible label: the step's name is the reference.

**Selectors**: `select: "step"` with `step: <name>` (the nodes that step switched off; `select: "label"` stays accepted, hidden: `x-hidden-values`, `x-hidden`). Ranking on any candidate set: `rank_by` (`queue.mean`, `queue.max`, `rtt.mean_ms`, `rtt.max_ms`, `mac.retries_per_frame`, `mac.retries`, `medium.collisions` (`mac.collisions` accepted as a hidden alias), `loss`, `subtree`, `children`; units, titles and descriptions in the schema's `x-rank-metrics`), `rank_window_s` (60), `pick` gains `top` (`count`), `top_fraction` (`fraction`, at least one node) and `above` (`value`, in the rank metric's unit, `x-unit-from: "rank_by"`). With `rank_by` the default `pick` is `top`. The border router is never ranked; a node without a value (e.g. no packet sent in the window) is not ranked. If `above` (or any selection of a switch-off) leaves no node, the step is **skipped** with its reason (`skipped: true`, `skip_reason`), as are the later parts of the same step and a later `on` step of it.

**Metrics** (`x-metrics`, now with `per_node`: what the per-node value is, or null): `loss` (1 − pdr, worse high), `mac.retries` (MAC retransmissions of the unicast frames delivered), `medium.collisions` and `medium.collision_share` (receptions lost to a collision — outcomes `interference`, `captured`, `busy` — over the frames heard; central medium). Condition field `over`: `mesh` (default) or `worst_node` (the worst router's value, the border router not counted; only for the metrics with `per_node`; not with `per_hop` or `relative_to`).

**Report window and plots**: top-level `report: {"window_s": 10}` and `plots: [{title, x, y (a column or a list), points: "windows"|"steps", steps: [step names], kind: "line"|"scatter"}]`; the columns, with unit, title and description, are the schema's `x-series-columns`. Default `kind`: `line` for windows, `scatter` for steps.

**Dry run** (`scenario.py --dry`): `{"ok", "steps": [{step, name, use, title, phases: [phase indices], cap_s, t_max_s}], "phases": [{index, name, title, step, step_name, block, part, parts, start, end, wait_max_s, run_max_s, cap_s, t_max_s, entry, phase}], "virtual_s_max", "run_s"}`. `start` and `end` are plain words; `cap_s` = longest wait + longest run of the phase; `t_max_s` the latest virtual time it can end; `run_s` = `virtual_s_max` + 60 s, rounded up: the run's end when no duration is given. `run.sh`'s `<seconds>` is optional with `SCENARIO` (or `auto`).

**Run records**: the driver writes `scenario_plan.json` (the dry output) in the run directory at the start, and `scenario_result.json` at every phase boundary (`status: "running"` until the end). Each phase record carries `step`, `step_name`, `title`, `part`, `parts`, and `skipped`/`skip_reason`; `user_steps[]` gives per step `step`, `name`, `title`, `state` (`pending`, `running`, `completed`, `skipped`, `not_met` (a part ended at its cap), `error`), `phases`, `t_start_s`, `t_end_s`. An action that selected nodes records `chosen: [{node, value}]` (value: the ranking value, or null), `rank_by`, `rank_unit`, `rank_window: [t_from_s, t_to_s]`, `candidates`. `scenario.log` keeps its lines and adds `step <i> <name> starts`, `step <i> <name> ends (<state>)`, `step <i> <name> skipped: <reason>`.

**Report** (`report.json`, still `wssim-report 1`, fields added; [TRAFFIC_AND_REPORT.md](TRAFFIC_AND_REPORT.md) §5): `nodes.table[]` gains `retries`, `heard`, `collisions`, `collision_share`, `sent`, `lost`, `loss`; `network` gains `retries`, `collisions`, `loss`; `series` (`window_s`, `flow`, `columns`, `windows[]`, `steps[]`); `plots[]` resolved (`title`, `x` and `y[]` as `{column, title, unit}`, `points`, `kind`, `series[]`: `{column, title, unit, points: [[x, y]], labels}`); per scenario phase `metrics.per_node` and `metrics.worst`; summary rows gain `step`, `step_name`, `retries`, `collisions`, `loss`, `worst`, `chosen`.

**Review fixes.**
- Only a "Switch nodes off" step chooses its nodes before its load is set and is skipped when none is chosen. Hand-written phases select in each action, in order, as before: a later action can use an earlier one's label, and a second `kill` does not pick a node the first killed; an empty selection there acts on no node, as before.
- Per-router delivery, loss and RTT need `settings.min_node_samples` (5) packets in the window, else the router has no value (not ranked, not the worst router); `chosen` and `worst` entries of these metrics carry `sent`. A router's packets are those it sent and, for the down flow, those the border router sent to it.
- Routers the scenario switched off during part of a window are left out of that window's power (series columns, phase `power.avg_ma` and `power.max_ma`, per-node `power_ma`). `scenario_result.json` lists `off_intervals` (node → `[[t_off, t_on or null]]`). The power ledger itself still charges a switched-off node as asleep with its radio off (§12): changing the ledger's state accounting is left for later.
- The border router cannot be switched off by a "Switch nodes off" step (selection by ID 0 refused).
- Enumerated fields carry `x-enum-titles` (plain titles per value); descriptions and messages use plain words; dry titles, starts and ends use metric titles, percentages and step names.

**Figures that change for an unchanged scenario.** The driver now samples every `report.window_s` (10 s): every node's statistics line, the medium's counters and a power snapshot, none of them charged in the power ledger and none changing the simulation (a rerun of `node_failure.json`, seed 1, gave the same capacity, the same router switched off and the same repair time as before). What can change in the reports:
- queue figures of a window (`queue.mean_max`, per-node mean queues, `mac.retries_per_frame` of a window): a window's counters are taken from the latest sample at or before each bound, and with a sample every 10 s instead of only at phase bounds and polls, the window's start is closer to the asked one;
- conditions on `mesh.parent_changes` and the disruptions' `churn.parent_changes`: they count parent changes seen in the samples, so more samples can see more changes;
- metric conditions on non-packet metrics (queues, retries, medium, power): their trailing window is now `window_s` as given (before: lengthened like the packet metrics for `min_samples`), and the medium counters of a window start at the sample at or before its start (before: the previous poll's, none at the first evaluation);
- phase power figures of a phase in which a router was switched off: that router is left out.

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
  "report": {"window_s": 10},
  "phases": [ PHASE or BLOCK, ... ],
  "plots": [ PLOT, ... ]
}
```

Only `format` and `phases` are required. Each entry of `phases` is a **step** of the user's plan (a block or a hand-written phase); its `name` (for a block, default the block's) is how later steps refer to it. `report.window_s` (10 s) is the length of a row of the report's time series: the driver samples every node's counters, the medium's counters and the power at that period (requests that are not charged in the power ledger), and `report.py` makes one row per window ([TRAFFIC_AND_REPORT.md](TRAFFIC_AND_REPORT.md) §5.6). `plots` are graphs of those series (§9.1). The JSON Schema (`--schema`, committed as `docs/scenario.schema.json`) carries every rule a form needs: per field a title, a one-line description, its unit (`x-unit`), its default and, for closed vocabularies, its enumeration; where the default depends on the network or on other fields, an `x-default` object with the rule in words (`text`) and, when a form can compute it, its parts: `value` (a constant that applies only in a given case), `rule` `per_router` (`interval_s`, `max_pps`: one packet per router every `interval_s`, at most `max_pps`), `times_baseline` (`factor` × the rate of the phase named by `field`), `setting` (the scenario setting named `setting`), `by_kind` (`map`: the condition kind's default) or `worse_than` (`up` for the metrics in `up_metrics`, else `down`); mutually exclusive fields as `oneOf`/`not` (`rate_pps`, `interval_s` and `rate_of`; `after_s` or a condition; `value` or `relative_to`; `end` or `repeat`), the fields each kind requires as `if`/`then` (a `metric` condition needs `metric`, a `node_event` needs `event` and `nodes`, `kill` needs `nodes`, ...), each composite rule with a plain-words `x-message`, and the metrics' units and directions (`x-metrics`). Fields starting with `x_` are ignored (free for an editor's own data).

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

| `loss` | ratio | traffic: 1 − `pdr` of the flow (decided with `min_samples` packets, as `pdr`) |
| `mac.retries` | retries | queues: MAC retransmissions of the unicast frames delivered (the sum of the retry histogram, bin 8 counting 8) |
| `medium.collisions`, `medium.collision_share` | frames, ratio | medium: receptions lost to a collision (outcomes `interference`, `captured`, `busy`: another frame or an external interferer overlapping), and their share of the frames heard by listening receivers; central medium only |

**Per node** (`x-metrics.*.per_node`; for `over: "worst_node"`, `metrics.worst` and `metrics.per_node` of each phase): `pdr`, `loss`, `rtt.mean_ms`, `rtt.p95_ms`, `rtt.max_ms` (the packets the router originated), `queue.mean_max` (its mean depth), `queue.drops`, `mac.retries`, `mac.retries_per_frame`, `mac.give_up`, `medium.collisions`, `medium.collision_share` (at the router as a receiver), `power.avg_ma`. A router without packets in the window has no `pdr`, `loss` or RTT; a single lost packet gives it a loss of 1, so prefer a window with several packets per router.

Since emu-probe a trailing window is made longer for `min_samples` only for the sampled metrics (`pdr`, `loss`, `rtt`, `latency`); the others use `window_s` as given. The window's `medium` counters are those of the driver's sample at or before its start (before: the previous poll's).

A capacity search also gives `capacity_pps` (for `rate_of` and `relative_to`). Without `op`, a metric condition holds when the value is **worse than** its threshold: `>=` for delays, queues, retries, give-ups, orphans, power and the medium's figures; `<` for `pdr`, `delivered_pps` and `mesh.joined_fraction`.

### 4.4 Iterations (capacity search)

`repeat`: at most `max` (12) windows. At each step the rate is multiplied by `mul` (1.5) or raised by `add_pps`; `settle_s` (15) at the new load, then a window of `window_s` (60), made longer (up to 4 ×) to hold 1.25 × `min_samples` packets on average at the rate, and `drain_s`; the `until` condition (§5) is evaluated on the window's measurements.

**Statistical rule.** A step **saturates** when `until` holds on `confirm` (2) windows in a row at the same rate, each with at least `min_samples` (100) packets sent; a single window over the threshold is measured again rather than taken. The search ends at the first saturating step: the **capacity** is the *measured* offered rate of the step before (`capacity_pps`; the nominal rate asked for is `capacity_nominal_pps`: they differ by the Poisson draws and the routers left out), with its delivered rate; the saturating rate is recorded. When the first rate already saturates, the search goes down (rate divided by `mul`) until a step does not: its measured rate is the capacity. When no step saturates, the capacity is at least the last rate (`reached: false`). The result also records the baseline phase each relative condition used and whether it had `min_samples` packets (`capacity.baseline`).

A baseline measured before a disruption is not the network's after it: a `capacity` block with `baseline: "self"` measures its own probe first (the former `capacity_check` block did so by default).

## 5. Conditions (`is`)

| Kind | Fields | True when |
| --- | --- | --- |
| `immediately` | | At once |
| `time` | `t_s` | Virtual time reached |
| `elapsed` | `s` | `s` seconds after the reference time |
| `formed` | `fraction` (1), `milestone` (`ready`), `stable_s` (0), `nodes` | That share of the running routers (or of `nodes`) has reached the milestone in its current instance and is in the border router's routing table now, and no parent changed anywhere for `stable_s` |
| `node_event` | `event`, `nodes`, `milestone` (`operational`) | Every selected node had the event since the reference time: `joined` (the milestone), `parent_changed`, `parent_lost`, `restarted` |
| `repaired` | `e2e` (false) | The last disruption's repair as tracked (§6.2): every repairable router it cut off has a route to the border router again (a parent chain that avoids the failed nodes); with `e2e`, also in the border router's table and an echo received for a packet sent after the disruption |
| `metric` | `metric`, `op` (worse than, §4.3), `value` or `relative_to` (not both), `per_hop`, `flow`, `window_s` (60), `min_samples`, `over` (`mesh`) | The metric (§4.3) compared with `value`, or with `relative_to` = `{"phase", "metric" (the same), "factor" (1)}`: an earlier phase's value × factor. `per_hop`: true if it holds at any hop count, each against the same hop count of the earlier phase. `flow`: `up` if running, else the first. `over: "worst_node"`: the value of the worst router (highest, or lowest for `pdr`), the border router not counted; only for the metrics with a per-node value (§4.3), with `value` (not `relative_to` or `per_hop`); the detail names the router |
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

`kill` and `leave` record the nodes they switched off under the step's name (`select: "step"`). `restart` of nodes that are off starts them (a fresh instance). In a "Switch nodes off" step, the action chooses its nodes at the step's start, before the step's load is set; **when none is chosen, the phase is skipped** (`skipped`, `skip_reason`), with the later parts of the same step, and the step's state is `skipped`. In a hand-written phase, each action selects its nodes when it runs, in order (it sees the labels and kills of the actions before it).

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
| `step` | `step` | The nodes an earlier step switched off (`kill`, `leave`, the "Switch nodes off" block) |
| `label` | `label` | Legacy (hidden in the builder): the nodes an earlier action labelled |

Then `pick`: `all` (default; `top` when `rank_by` is given), `top` (the `count` highest by `rank_by`), `top_fraction` (the highest `fraction` of the ranked candidates, rounded down, at least one), `above` (every candidate whose `rank_by` value is at least `value`, in its unit), `largest_subtree` or `most_children` (the worst case; `count` of them), `random` (`count`, drawn from the seed), `fraction` (drawn from the seed), `first`. The result records the nodes picked and why ("routers at hop 1, largest subtree (66: 24 below)").

**Ranking** (`rank_by`, with `top`, `top_fraction`, `above`): the candidates, the border router excepted, ordered by a value measured over `rank_window_s` (60) before the step starts, highest first (ties: the lower ID):

| `rank_by` | Unit | Value |
| --- | --- | --- |
| `queue.mean` | frames | Mean depth of the node's MAC plus adaptation-layer queues over the window (NET1 counters) |
| `queue.max` | frames | Highest depth seen in the window: the largest `q_win_max` of the node's NET1 samples in it (each sample's peak since the previous one; samples every `report.window_s`) |
| `rtt.mean_ms`, `rtt.max_ms` | ms | RTT of the packets the node originated, sent in the window ending `drain_s` before the step |
| `loss` | ratio | Share of those packets lost (not echoed, or not delivered for a one-way packet). A router's packets: those it sent and, for the down flow, those sent to it; fewer than `min_node_samples` (5): no value |
| `mac.retries`, `mac.retries_per_frame` | retries | MAC retransmissions of the node's unicast frames delivered in the window, and per frame |
| `medium.collisions` (alias `mac.collisions`) | frames | Receptions lost to a collision at the node in the window (the conductor's per-receiver counters, `rxcounters`; central medium) |
| `subtree`, `children` | nodes | Routers below the node, or whose parent it is, when the step starts |

A node without a value (no packet sent, no sample) is not ranked. The action's record gives `chosen` (`[{node, value}]`, in ranking order), `rank_by`, `rank_unit`, `rank_window` (`[t_from_s, t_to_s]`), `candidates` and `ranked` (how many had a value). The resolution of the window's start is `report.window_s` (the samples), its end exact.

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
| `phases[]` | `name`, `block`, `index`, `step`, `step_name`, `title`, `part`, `parts`; `skipped`, `skip_reason`; `start` (`t_s`, `met`, `detail`: why it started, `timeout`, `on_timeout`, `condition`); `actions[]` (`do`, `nodes`, `why`, `affected`, `marginal`, `isolated`, `links`, `result`, `chosen`, `rank_by`, `rank_unit`, `rank_window`, `candidates`, `ranked`); `metrics` (per set: `flows` as [TRAFFIC_AND_REPORT.md](TRAFFIC_AND_REPORT.md) §5.1, `queues` with `per_node` and `retries`, `mesh`, `power` with `per_node`, `medium` with `collisions` and `collision_share`; `per_node`: per node ID `sent`, `lost`, `loss`, `pdr`, `rtt_mean_ms`, `rtt_p95_ms`, `rtt_max_ms`, `retries`, `retries_per_frame`, `uc_frames`, `give_up`, `drops`, `queue_mean`, `queue_max`, `heard`, `collisions`, `collision_share`, `power_ma`, as the sets measured give them; `worst`: per per-node metric `{node, value}`, the border router not counted); `load`; `end` (`t_s`, `met`, `detail`, `timeout`); `iterations[]` (one per window: `iteration`, `step_tag`, `rate_pps` (nominal), `offered_pps` and `delivered_pps` (measured), `samples`, `interval_s`, `t_s`, `met`, `detail`, `direction`, `confirmation` (0: the step's first window), `metrics`); `capacity` (`capacity_pps` (measured), `capacity_nominal_pps`, `delivered_pps`, `saturated_rate_pps`, `saturated_offered_pps`, `reached`, `searched`, `confirm`, `min_samples`, `baseline`, `detail`); `disruptions[]` (§6.2); `skipped`, `aborted` |
| `steps` | Per load setting (`tag`): `phase`, `iteration`, `t_s`, `rate_pps`, `interval_s`, `flows`, `members`, `payload_b` |
| `events[]` | `t_s`, `text`: everything the driver did, also in `scenario.log` and `simserver.log` |
| `labels` | Label → nodes |
| `step_off` | Step name → the nodes it switched off |
| `user_steps[]` | Per step of the file: `step`, `name`, `title`, `use`, `state` (`pending`, `running`, `completed`, `not_met`: a part ended without its end condition (at its cap), `skipped`, `error`), `phases` (names), `t_start_s`, `t_end_s` |
| `window_s` | The report window (sampling period) |
| `summary` | Per phase: `step`, `step_name`, `title`, start and end times and why, `skipped`, `skip_reason`, PDR, `loss`, RTT (`n`, `mean`, `p95`, `max`) overall and per hop count, offered rate, capacity, disruptions, power, `retries`, `retries_per_frame`, `collisions`, `worst`, `chosen` and `rank_by` |

`scenario_result.json` is written at every step start and phase end with `status: "running"`, then at the end; `scenario_plan.json` (the dry output) at the start. `series_samples.jsonl`: one line per sample (`t_s`, `tag`, `counters`: the conductor's totals, `rx`: per receiver `[heard, collisions, ok]`), at every `report.window_s`.

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
| `rxcounters` | `ok <id>:<heard>:<collisions>:<ok> ...` | Per receiver, totals so far: frames heard while listening, lost to a collision (`interference`, `captured`, `busy`), received; nodes that heard nothing are left out; `ok` alone without the central medium |
| `power snap <file> <tag>` | `ok` | Appends a snapshot line |
| `log <text>` | `ok` | `scenario: <text>` in `simserver.log` |
| `quit` | `bye` | Ends the run (a closed connection without it fails the run) |

`run.sh` with `SCENARIO`: checks the file, starts the server with `--vtime-control` (socket `<SOCK>.ctl`), the programs with `load_control = true` and `stats = true`, then the driver; the driver ends the run (`<seconds>` is a cap, and may be left out: the plan's `run_s`). `programs.txt` holds every program's command line, for restarts. A node the scenario stopped does not fail the run.

## 9. Building blocks (`use`)

| Block | Parameters (defaults) | Phase |
| --- | --- | --- |
| `formed` | `fraction` (1), `stable_s` (60), `timeout_s` (3600) | Start when that share of routers is ready and in the border router's table, parents stable; ends at once |
| `probe` | `rate_pps` (one packet per router every 30 s, at most 0.5/s), `flows` (up), `duration_s` (300), `settle_s` (30), `payload_b` (64) | Light load; every set measured: the baseline |
| `capacity` | `baseline` (`probe`; `self`: its own probe, `<name>_probe`, of `probe_s` 300 s first), `start_from` (an earlier capacity search: start at `start_fraction`, 0.5, of its capacity), `start_rate_pps` (2 × the baseline's; not with `start_from`), `mul` (1.5), `max_steps` (12), `window_s` (60), `settle_s` (15), `confirm` (2), `rtt_metric` (`rtt.p95_ms`), `rtt_factor` (2), `max_rtt_ms`, `min_pdr` (0.95), `flows`, `payload_b` (64) | Iterations until the RTT reaches `rtt_factor` × the baseline's, the maximum RTT `max_rtt_ms`, or the delivery ratio falls below `min_pdr`; with `baseline: "self"`, a light-load probe phase first |
| `interference` | `spec` (required), `interferer_name` (`jam`), `capacity_phase` (`capacity`), `load_fraction` (0.8), `reference_s` (0: none), `duration_s` (300), `settle_s` (30) | Interferer added; that share of the capacity. With `reference_s`, first a phase `<name>_reference` at the same load without the interferer, measured for `reference_s` after `settle_s` (two phases) |
| `recovery` | `interferer_name` (`all`; empty: none removed), `baseline` (`probe`), `within` (1.2), `max_s` (600) | Interferer removed, same load; ends when the RTT p95 over 60 s is within that factor of the baseline's. After a loaded interference, give the interference's reference (`<name>_reference`) as `baseline`: the light-load probe's RTT is not reached again at that load |
| `node_outage` ("Switch nodes off") | `mode` (`off`, `off_then_on`, `on`), `nodes` (hop 1, largest subtree; modes `off`, `off_then_on`), `off_s` (`off_then_on`), `of_step` (`on`, required), `capacity_phase` (`capacity`; empty: keep the load), `load_fraction` (0.5), `max_s` (600, each part), `e2e` (true) | `off`: switch the nodes off; ends when repaired. `off_then_on`: the same (and at least `off_s`; cap max(`max_s`, `off_s`)), then the phase `<name>_on` switches them back on and ends when they are ready in their new instance and in the border router's routing table. `on`: switch back on the nodes of the earlier step `of_step`; ends when they are back |
| `node_failure` (legacy, hidden) | `nodes` (hop 1, largest subtree), `label` (`failed`), `capacity_phase` (`capacity`; empty: keep the load), `load_fraction` (0.5), `max_s` (600), `e2e` (true) | Kill; ends when repaired. Use `node_outage` |
| `node_restart` (legacy, hidden) | `label` (`failed`), `capacity_phase` (`capacity`; empty: keep the load), `load_fraction` (0.5), `max_s` (600) | Restart those nodes under that share of the capacity; ends when they are ready (RPL) in their new instance and in the border router's routing table. Use `node_outage` with `mode: "on"` |
| `capacity_check` (legacy, hidden) | `capacity_phase` (`capacity`), `start_fraction` (0.5), `baseline` (`self`: its own probe, `<name>_probe`, of `probe_s` 300 s first; or an earlier phase), `mul`, `max_steps` (8), `window_s`, `settle_s`, `confirm`, `rtt_factor`, `min_pdr`, `flows`, `payload_b` | A capacity search from a share of an earlier capacity, against a baseline of the network as it is now (two phases with `baseline: "self"`). Expands exactly as `capacity` with `start_from` = `capacity_phase`, `baseline: "self"` and `max_steps: 8`, which the builder uses instead |

A block's `name` defaults to the block's; give one to use a block twice. The owner's study, `sim_utils/scenarios/capacity_interference.json`, is five entries (six phases):

```json
{"use": "formed", "stable_s": 60},
{"use": "probe", "duration_s": 300},
{"use": "capacity", "max_rtt_ms": 5000, "window_s": 60},
{"use": "interference", "spec": "chan=0-17,level=-75,kind=fsk,pattern=ge:40ms:60ms", "load_fraction": 0.8, "reference_s": 120, "duration_s": 240},
{"use": "recovery", "baseline": "interference_reference", "max_s": 300}
```

Examples: `capacity_interference.json` (above), `node_failure.json` (formed, probe, capacity, the busiest hop-1 router switched off under 50 % of the capacity (step `failure`), capacity after the repair, the router switched back on under 50 % of the capacity until it is back in the border router's routing table (step `restart`, `mode: "on"`, `of_step: "failure"`)), `link_cut_layers.json` (hand-written phases: a steady load from 300 s, every link of a random hop-2 router cut for 120 s, then until the delivery is back to 99 %). Each template has default plots (§9.1). The figures of §11 were made with `node_failure` and `node_restart`, which expand exactly as the new blocks there.

### 9.1 Plots

`plots[]`: `x` (a column), `y` (a column or a list, drawn together), `points` (`windows`: a point per report window; `steps`: a point per step, a capacity search giving one per load level), `steps` (only these step names), `kind` (`line` for windows, `scatter` for steps), `title` (default: the y titles against the x title). The columns are those of the report's series, with unit, title and description in the schema's `x-series-columns`: `t_start_s`, `t_end_s`, `t_mid_s`, `step_index`, `offered_pps`, `delivered_pps`, `pdr`, `loss`, `rtt.min_ms`, `rtt.mean_ms`, `rtt.p95_ms`, `rtt.max_ms`, `latency.mean_ms`, `latency.p95_ms`, `mac.retries`, `mac.retries_per_frame`, `medium.collisions`, `medium.collision_share`, `queue.mean_max`, `power.min_ma`, `power.mean_ma`, `power.max_ma`, `power.max_node`, `power.br_ma`, `mesh.joined_fraction`. `report.json` carries them resolved ([TRAFFIC_AND_REPORT.md](TRAFFIC_AND_REPORT.md) §5.7).

## 10. Validation

The meaning also covers the steps: `select: "step"` naming an earlier step that switched nodes off; for `node_outage`, `of_step` naming an earlier step whose nodes are still off (not switched back on, not `off_then_on`), `nodes` not given with mode `on`, `off_s` only with `off_then_on`, `of_step` only with `on`; `rank_by` only with `top`, `top_fraction` or `above`; `over: "worst_node"` only for per-node metrics; plot `steps` naming steps of the file.

`scenario.py --check FILE` prints `{"ok": bool, "errors": [{"path": "/phases/2/load_fraction", "message": "must be more than 0"}]}` and exits 1 on errors. One round reports everything: the schema's rules (types, enumerations, bounds with their units, unknown fields, the fields each kind requires, mutually exclusive fields) and the meaning (unique names; `relative_to` and `rate_of` naming an earlier phase, `capacity_pps` a phase with `repeat`; labels defined before use; `repaired` only after a disruption; interferer `SPEC`s checked key by key with the server's ranges). Paths are JSON Pointers into the file as written: an error inside a block points at the block's own parameter (`/phases/3/capacity_phase`), or at the block's entry with the field named. `--dry FILE` prints the plan, for a visual timeline:

```json
{"ok": true,
 "steps": [{"step": 3, "name": "out", "use": "node_outage", "title": "Switch nodes off", "phases": [3, 4], "cap_s": 1200, "t_max_s": 8520}],
 "phases": [{"index": 3, "name": "out", "title": "Switch off every router, the highest 0.1 of them by mac.retries over the last 60 s",
             "step": 3, "step_name": "out", "block": "node_outage", "part": 0, "parts": 2,
             "start": "right after the previous phase", "end": "when the mesh is repaired end to end and after 20 s, at most 600 s",
             "wait_max_s": 0, "run_max_s": 600, "cap_s": 600, "t_max_s": 7920, "entry": 3, "phase": {...}}],
 "virtual_s_max": 9750, "run_s": 9810}
```

`steps[]` follow the file's entries: `step` (index into `phases` of the file), `name`, `use` (the block, null for a hand-written phase), `title` (the block's title, else the name), `phases` (indices into the expanded `phases`), `cap_s`, `t_max_s`. `phases[]`: `index`, `name`, `title` (what it does, in plain words), `step`, `step_name`, `block`, `part`/`parts` (its rank in the block), `start` and `end` (plain words), `wait_max_s` (longest wait for its start: the timeout), `run_max_s` (its fixed duration or cap; for a capacity search, every window at its longest), `cap_s` (both), `t_max_s` (the latest virtual time it can end), `entry` (= `step`, kept for older readers), `phase` (the expanded phase). `virtual_s_max` is the upper bound of the virtual duration (every wait and cap at its maximum; a `formed` start counts its whole timeout) and `run_s` = `virtual_s_max` + 60 s rounded up: `run.sh` uses it as the run's end when `<seconds>` is not given (or `auto`); the driver ends the run when the scenario ends.

## 11. Demonstrations

Seed 1 (and seed 3 for one check), `POWER=examples/power/efr32fg25-soc.ini` (radio always listening, about 8–10 mA per router), `COMPACT=1`, default medium preset (`calibrated`). Networks: `dag_simple` (15 nodes, depths 1–4) and a 150-node building on three floors, 10 m apart, links from the positions (`radio_if/scale/gen_building.py --nodes 150 --floors 3 --spacing 10`, `--positions`, `--prop-tx-dbm=20`; depths 1–3, 34 routers at hop 1). Wall time with the four runs at once on the 24-core PC: 9 s (`capacity_interference.json`) and 11 s (`node_failure.json`) on `dag_simple`, 2.6 and 3.6 min on the building; 214 s for the whole set. The `dag_simple` seed-3 column was not rerun after the last template change (restart phase).

**`capacity_interference.json`** (the owner's study; 120 s at 80 % of the capacity without interferer as the reference, then the interferer on channels 0–17 of 35, −75 dBm at every node, FSK, on 40 % of the time):

| Phase | dag_simple | 150-node building |
| --- | --- | --- |
| formed (all in the border router's table, parents stable 60 s) | 370 s | 535 s |
| probe (300 s): packets; RTT mean / p95; mean RTT by hops | 145; 361 / 732 ms; 196, 307, 524, 620 ms (1–4 hops) | 140; 333 / 654 ms; 203, 307, 489 ms (1–3 hops) |
| capacity: steps (nominal packets/s, network), 2 windows to confirm | 1.0, 1.5, 2.25, 3.38, 5.06 (p95 1629 and 2004 ms ≥ 2 × 732) | 0.97, 1.45, 2.17, 3.26, 4.89 (1559 ms, then 1285 ms: not confirmed), 7.33 (2118 and 2425 ms ≥ 2 × 654) |
| capacity found (measured offered rate) | 3.45 packets/s | 4.75 packets/s |
| reference at 80 % of it, no interferer (120 s): PDR; RTT mean / p95; mean RTT by hops | 1.0; 440 / 968 ms; 247, 443, 561, 676 ms | 1.0; 411 / 997 ms; 318, 418, 537 ms |
| interference at the same load (240 s): PDR; RTT mean / p95; mean RTT by hops | 1.0; 598 / 1447 ms; 289, 544, 836, 1122 ms | 0.978; 886 / 2388 ms; 616, 914, 1260 ms |
| recovery (interferer removed): RTT p95 over the last 60 s (or more, for 100 packets) back within 1.2 × the reference's after | 85 s (963 ≤ 1161 ms) | 70 s (952 ≤ 1196 ms) |
| routers' mean current, probe / capacity / reference / interference | 8.90 / 9.71 / 9.50 / 9.50 mA | 8.26 / 8.32 / 8.30 / 8.30 mA |

The RTT grows with the hop count in every phase, and the interference adds most at the deepest routers. At 80 % of the capacity, delays run above the light-load probe's with or without interference (p95 968 against 732 ms on dag_simple, 997 against 654 ms in the building), so recovery is compared with the reference taken at the same load just before the interferer, not with the probe (against the probe's p95, recovery cannot be met at that load). Both networks came back within 1.2 × the reference's p95 in the first windows after the removal: 85 and 70 s, of which 60 s is the window the first decision needs; the resolution is the poll period, 5 s. In the building, the confirmation rule took effect: the step at 4.89 packets/s met the condition once and not on its second window, so the search went on.

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
| restart of the killed router, at 50 % of the capacity before: Wi-SUN join done (operational) / back in the border router's table (its DAO acknowledged; end of the phase) after | 33 s / 80 s | not rerun (earlier version, load of the capacity search kept: joined after 20 s) | 21 s / 60 s |
| delivery during the restart phase (packets) | 0.991 (115) | | 1.0 (115) |

The capacity search is relative to its baseline: after the repair, the second capacity search measures its own probe and doubles *its* RTT. In dag_simple the post-repair probe is slower (p95 1024 against 732 ms), so the threshold is higher and the capacity found is higher too; in the building the post-repair probe is faster (608 against 654 ms) and the capacity lower. Both answer "the load at which the RTT doubles from the network's own light-load RTT", not an absolute delay limit: use `max_rtt_ms` (or a fixed `value`) for that. In the previous version of the engine, seed 3 of this scenario found a capacity after the repair of 1.05 packets/s from one window 3 % over its threshold, with a pre-repair baseline of 84 packets; with the minimum of 100 packets per window, the confirmation on a second window and a fresh baseline, it finds 4.65 packets/s.

The resolution of the repair times is the poll period, 5 s. The runs: `SCENARIO=sim_utils/scenarios/<file> POWER=examples/power/efr32fg25-soc.ini COMPACT=1 TUN=none WSSIM_SEED=1 sim_utils/vtime/run.sh build-vt $(pwd) <out> 9000`, with, for the building, `TOPO=<file with "0,149">` and `SIM_OPTS="--positions=<building.pos> --prop-tx-dbm=20"`. `sim_utils/vtime/test_scenario.sh` (every action, the border router's restart included, then the failure cases) passes in about 20 s.

## 12. Limits

- Decisions and repair times have the resolution of `poll_s` (5 s by default); a phase's bounds and iterations are exact.
- Since emu-probe the driver samples every `report.window_s` (10 s): more `NET1` lines (about 0.9 kB per node per sample) and more pauses. Conditions that read samples (parent changes, queue windows) see these too, so a scenario run before and after the change can differ slightly; runs stay reproducible from their seed.
- Per-node collisions count receptions at the node, whoever sent the frame (broadcasts included); the sender's side is not counted.
- Scale: each poll asks every node for a `NET1` line (about 0.9 kB) and the border router for its table; on a 1000-node network that is about 1 MB of log per poll. The driver's reader takes about 0.3 µs per log line it skips and 8 µs per `NET1` line it parses (measured on dag_simple logs): a 1000-node poll costs the driver about 10 ms plus the lines the nodes wrote since the last pause, which `COMPACT=1` makes about 10 times fewer ([TRAFFIC_AND_REPORT.md](TRAFFIC_AND_REPORT.md) §2.1). These are estimates: a 1000-node scenario was not run.
- An interferer added at run time applies to frames and sniffs that start after it; one that the medium has already computed (a frame on air) keeps its earlier levels. The medium-log line has the interferer column for the whole run when the run has a control socket and the central medium.
- `kill` with `POWER`: the node is then charged as asleep with its radio off, not as unpowered; a restarted node continues the same ledger. `--power` stays refused with the static `--vtime-kill`/`--vtime-start` options.
- A restart starts a new program with the same key storage; the border router's restart was checked once (`test_scenario.sh`: the network re-formed).
- The border router keeps a killed router in its routing table until the entry expires, so for a restarted router the end of `node_restart` is set in practice by its new instance being ready, that is its DAO acknowledged by the border router.
- `degrade_links` restores a link only when `topology.txt` gives its level; with positions, use `cut_links`.
- Medium settings other than interferers cannot be changed during a run.
- The driver and the conductor talk over a local socket; a run whose driver stops ends at once and fails (§1).
