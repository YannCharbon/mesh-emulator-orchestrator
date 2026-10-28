# Power model (EM2): design for implementers

**Domain: internal.** The emulator gives the power consumption a real device would have, with every state recorded. Plan and scope: the root repo's `documents/pre-study/investigation/Idle_Sleep_Study.md` §5.1 and §8, and `STATUS.md` EM2. This note fixes the design and covers **part 1** (§8.5 step 1): FFN routers as they are today, in virtual time ([VIRTUAL_TIME.md](VIRTUAL_TIME.md)).

Paths: `NGS` = `nanostack-generic-standalone/`; others are in this repo.

## 1. Pieces and where they live

| Piece | Where | What |
| --- | --- | --- |
| Power ledger | `app_wssimserver/sim_power.c`, `sim_power.h` | In the conductor. Per node: the MCU and radio state machines, the profile, the totals, the report. Stack-neutral: it only reads the conductor protocol |
| Conductor options | `app_wssimserver/wssimserver.c` | `--power`, `--power-node`, `--power-out`, `--power-log`, `--power-cell` (§7). Off unless given |
| Port: wake report | `NGS/port/sim/sim_power.c`, `ns_port_sim.h` | The "xc" message: timer callbacks per cause, events dispatched, for each wake |
| Sleep-depth rule | `NGS/include/ns_port/power.h` | Header-only rule, for the emulator and the firmware port alike (§4) |
| Radio states | `app_wshwsim/sl_rf_driver.c`, `app_wssimserver/sim_proto.h` | The "xr" message at each change of the emulated radio's state |
| Profiles | `examples/power/*.ini` | `efr32fg25-soc.ini`: the FG25 SoC from its datasheet (§6) |
| Tools | `sim_utils/power_summary.py`, `sim_utils/vtime/run.sh` (`POWER=`), `sim_utils/vtime/test_power.py` | Summary of runs; runs with recording; a test of the ledger's arithmetic |

## 2. State machines

Each node has two state machines (three for MCU + RCP hardware, later). At any time each is in exactly one state or one transition, so per node and per machine **Σ (state times) + Σ (transition times) = run time**. The report checks it.

### 2.1 MCU

| State | Meaning | FG25 |
| --- | --- | --- |
| active | code runs | EM0 |
| sleep | light sleep: clocks kept, wakes in a few cycles | EM1 |
| deep_sleep | only the low-frequency clock runs | EM2 |

| Transition | When | Cost in the profile |
| --- | --- | --- |
| sleep_enter, sleep_wake | every light sleep | time, current |
| deep_enter, deep_wake | every deep sleep (deep_wake: crystal start, PLL lock) | time, current |

The profile can later add deeper modes (EM3, EM4) as more states; part 1 has these three.

### 2.2 Radio

| State | Meaning |
| --- | --- |
| off | powered down |
| idle | ready, neither receiving nor sending |
| listen | RX on, no frame (also during CSMA backoffs, as on the hardware) |
| receive | RX of a frame, from its start to its end |
| cca | a clear-channel assessment (charged at the end of the listening before it, §5.2) |
| tx | sending, at a power level; each level has its own current |

Transitions between any two states (`listen>tx`, `tx>listen`, `off>idle`, ...) are counted, and charged with the profile's time and current when it gives them.

### 2.3 Link between MCU and RCP (later)

For MCU + RCP hardware (and modules), each chip has its own MCU machine, and a link machine (idle, transferring) charges every Spinel message to both. This needs the split programs in virtual time (EM3 step 2). The profile loader refuses a `[link]` section and the kind `mcu+rcp` until then.

## 3. Where each state change comes from

All of them come from the real stack's behaviour, not from a script.

### 3.1 Radio: the emulated radio driver

`sl_rf_driver.c` has a state variable that the real MAC drives through the PHY API (`phy_rf_tx`, `phy_rf_state_control`, the extensions). Every change of it is followed by `set_shm_cca_state()`, which now also calls `rf_power_report()`. That maps the driver's state to the hardware's and sends an "xr" when the mapped state (or the TX power, in TX) changed:

| Driver state | Set in | Radio state |
| --- | --- | --- |
| `DRVSTATE_RADIO_UNINIT`, `_INITING` | start | off |
| `DRVSTATE_IDLE` | init; `PHY_EXTENSION_SET_CSMA_PARAMETERS` with backoff 0 (cancel); `PHY_TX_NOT_ALLOWED` | idle |
| `DRVSTATE_IDLE_WAITING_RX` | `_rf_set_rx()`, end of TX/RX, CCA busy | listen |
| `DRVSTATE_CSMA_STARTED` | `phy_rf_tx()`: the backoff before a TX | listen |
| `DRVSTATE_RX` | `phy_rf_rx_now()`: a frame on our channel, not lost | receive |
| `DRVSTATE_TX` | `_rf_execute_tx()`; ends at the TX-done timer (airtime) | tx |

- **CCA:** `_rf_check_cca()` (only when the MAC enabled CCA) sends an "xr" with `SIM_RADIO_EV_CCA`. The driver does the CCA in no time; the ledger charges `cca_us` at the end of the listening that precedes it (§5.2).
- **TX power:** `PHY_EXTENSION_SET_TX_POWER` records the dBm the MAC asked for; each TX carries it.
- **Airtime:** RX and TX last the driver's own airtime (`CALC_SLEEP_TIME`, 150 kbit/s). EM4 will give exact airtimes at the medium later.
- **Off:** the driver has no off state; GS8 stage 1 (LFN study E1–E3) adds it. Until then "off" appears only before the driver starts.

### 3.2 MCU: the run loop's idle intervals

In virtual time the node's run loop (`NGS/port/sim/sim_scheduler.c`) runs until idle, sends "xi" with its next deadline, and blocks until "xa". So:

- **A wake** starts at each "xa" (time t). The welcome is the first wake, at 0.
- **The wake's work** is known at its "xi": the port's "xc" (§5.1) and the "xr" seen in between.
- **The idle** that follows lasts until the next "xa" of that node.

The ledger reads this in the conductor, so it works for any program that speaks the conductor protocol: EM7's clients get the same accounting by sending "xi" (and, if they can, "xc" and "xr"). On the firmware, the same intervals are the FreeRTOS idle task's sleeps (HW5).

## 4. Sleep depth: a port-level rule

`NGS/include/ns_port/power.h`, header-only, so the emulator and the firmware port apply the same rule and the emulator sleeps where the board would:

```c
depth = ns_port_sleep_depth(holds, idle_us, &rule);   // LIGHT or DEEP
```

- **holds:** reasons to keep the fast clock, a bit set like the firmware's EM1 requirements: `NS_PORT_HOLD_RADIO` (the radio is not off; an idle radio holds too when the profile's `idle_holds_clock` is 1, because RAIL time needs the HFXO unless `RAIL_ConfigSleep()` is used), `NS_PORT_HOLD_PERIPH` (UART, SPI).
- **idle_us:** from the end of the wake's modelled work to the next deadline in "xi".
- **rule.deep_min_us:** `ns_port_deep_min_us()` from the profile: the shortest idle for which deep sleep costs less charge than light sleep, and at least the length of its two transitions. For the FG25 profile: 219 µs.

The ledger evaluates the rule at each "xi". A board's port would call the same function before it sleeps (FreeRTOS `configPRE_SLEEP_PROCESSING`), with the same inputs. For FFNs the radio always holds, so part 1 never chooses deep sleep; the rule's deep path is exercised by `test_power.py`.

## 5. MCU active time

### 5.1 Cost model

Code runs in zero virtual time, so the active time of a wake is modelled:

```
cycles = wake + Σ_cause cycles[cause] × timer callbacks of that cause
       + event × events dispatched + frame_in × frames delivered
       + rx_frame × frames received + tx_frame × frames sent
active time = cycles / clock_mhz
```

- **Causes** (`enum nsim_cause`): the port names each timer callback by its symbol (libbacktrace reads the program's symbol table once per callback; the stack's callbacks are static): `tick` (`tick_timer_eventOS_callback`), `protocol` (`protocol_timer_interrupt`), `fhss` (`fhss_*`, and every FHSS platform timer), `radio` (the driver's `_rf_*` timers: RAIL events on the board), `mac` (`timer_mac_interrupt`, `mac_*_timer_cb`), `other`.
- **Events:** the port counts the event-loop events it dispatches (`eventOS_scheduler_dispatch_event()` in a loop, which is what `eventOS_scheduler_run_until_idle()` does).
- **Frames received and sent:** the radio's `receive` and `tx` entries in the wake.
- **Values:** placeholders in the profile until HW5 measures cycles per kind of work on the FG25 (DWT cycle counter). Cryptography is inside `rx_frame`/`tx_frame` (CCM, hardware AES); the TLS handshake's ECC during joining is not separate yet (§9).
- **Timeline:** the work runs from the wake at t to t + active time; the sleep starts then. A wake that comes before the previous work is done extends it (`wakes_while_active`). Virtual time itself does not advance by the active time: that option (slow MCUs delaying the protocol) is for later.

### 5.2 Accounting rules

- **Transitions are cut out of the states they join,** so the sums hold: a sleep is `enter`, then the sleep, then `wake` (no wake at the end of the run); a radio transition is charged at the start of the state it enters; a CCA at the end of the listening before it. A transition longer than the interval is cut to fit.
- **Charge** = time × current of the state, in µC. Chip current = MCU state current + radio state current: the profile's radio currents are what the radio adds to the MCU's.
- **End of the run:** every node runs until `--vtime-end` (or the last wake if the run stopped early).

### 5.3 Wake causes, empty ticks, unseen frames

Per node the report counts:

- **wakes**, and per cause the wakes in which it appeared (`wakes_with`) and its callbacks (`timer_callbacks`);
- **empty ticks:** wakes where only the 10 ms tick fired and no event was dispatched and no frame came. This is what a tickless event loop would remove (idle-sleep study §3), measured without building it;
- **unseen frame wakes:** wakes with only frames that the radio did not take (another channel, lost to the link, or the radio busy). The emulator delivers every frame to every neighbour; a board's radio would not have interrupted the CPU. They are not counted as wakes and cost nothing (`frames_unseen` keeps the count).

## 6. Profile format

An INI file; `#` or `;` start a comment; every key is required unless said otherwise. Currents in mA, times in µs, clock in MHz. Each figure should say where it comes from (`[D]` datasheet table, `[V]` our code, `[I]` estimate, `HW5` to measure).

| Section | Keys |
| --- | --- |
| `[profile]` | `name`, `kind` (`soc`, `mcu+transceiver`; `mcu+rcp` after EM3 step 2), `supply_v` |
| `[mcu]` | `clock_mhz`; `active_ma`, `sleep_ma`, `deep_sleep_ma`; `sleep_enter_us/_ma`, `sleep_wake_us/_ma`, `deep_enter_us/_ma`, `deep_wake_us/_ma` |
| `[cycles]` | `wake`, `tick`, `protocol`, `mac`, `fhss`, `radio`, `other`, `event`, `frame_in`, `rx_frame`, `tx_frame` |
| `[radio]` | `off_ma`, `idle_ma`, `listen_ma`, `receive_ma`, `cca_ma`, `cca_us`, `idle_holds_clock` (0/1), `tx_ma = dBm:mA dBm:mA ...` (increasing dBm; linear in between, clamped outside, and the report flags a clamped level) |
| `[radio_transitions]` (optional) | `<from>_<to> = <µs> <mA>` for the states `off idle listen receive tx`; unlisted transitions are free |
| `[link]` | later (EM3 step 2): link current, bytes per second, per-message cost |

**MCU + external transceiver** (STM32F769 + S2-LP, the MTS bench): the same machines; the transceiver's currents go in `[radio]`, and the SPI work per frame in `rx_frame`/`tx_frame`. No such profile yet.

The FG25 profile (`examples/power/efr32fg25-soc.ini`) takes every current from the datasheet (Rev. 1.2) and marks the cycles and several transitions as HW5. Its radio currents are the datasheet's whole-chip RX/TX figures minus EM1 at 39 MHz, because they were measured with the MCU in EM1 at 39 MHz.

### 6.1 Every consumption parameter is the user's to set

The profiles are examples, not fixed models: a user sets them to the hardware and use case being studied.

| Parameter | Where it is set | Per node |
| --- | --- | --- |
| Currents, transition times and charges, MCU clock, supply voltage, cycles per kind of work, CCA time, TX current per dBm | The profile file (§6); copy `examples/power/efr32fg25-soc.ini` and edit it | `--power-node=IDS:PROFILE` |
| TX power (dBm) the stack asks for | `tx_power` in `wsbrd.conf` / `wsnode.conf` (default 20, the stack's), or `-o tx_power=N` | `run.sh`: `NODE_OPTS_<i>="-o tx_power=N"` |
| Battery cell | `--power-cell=mAh[:V[:eff]]` (`POWER_CELL`) | One per run |

The profile's TX table clamps the requested power to the hardware's maximum, as the radio would (the FG25: 15.9 dBm).

## 7. Running and outputs

```bash
# one dag_simple run with recording, FG25 profile, battery life for 4 × AA (3000 mAh, 6 V, 90 % converter)
POWER=examples/power/efr32fg25-soc.ini POWER_CELL=3000:6:0.9 WSSIM_SEED=1 TUN=tunvt0 \
  sim_utils/vtime/run.sh build-vt $(pwd) runs/pw/seed1 300
# ten seeds at once; then the summary
POWER=examples/power/efr32fg25-soc.ini sim_utils/vtime/campaign.sh build-vt $(pwd) runs/pw 300 $(seq 1 10)
python3 sim_utils/power_summary.py runs/pw/seed*           # --cell 3000:6:0.9, --nodes 1-14, --quiet
```

`wssimserver` options (virtual time only): `--power=PROFILE` (every node), `--power-node=IDS:PROFILE` (repeatable, the last match wins: mixed networks), `--power-out=FILE` (default `power.json`), `--power-log=FILE`, `--power-cell=MAH[:V[:EFF]]`. `run.sh` maps them from `POWER`, `POWER_LOG=1`, `POWER_CELL` and `POWER_OPTS`.

- **Report** (`power.json`, format `wssim-power 1`): per node, the average current, total charge and energy; per MCU and radio state and transition, time, charge and count (TX per power level); wakes, causes, empty ticks, unseen frames; the sum check; the battery life when `--power-cell` is given (cell current = average × supply V / (cell V × efficiency); without a voltage, the average itself). `simserver.log` gets one line per node.
- **State log** (`--power-log`, optional, about 90 MB per `dag_simple` run of 300 s): `start_us end_us node mcu|radio state [dBm]`, one line per interval, in time order per node and part.
- **Summary** (`sim_utils/power_summary.py`): a table per run, the median and range per node over several runs, and the sum check (exit status 1 if it fails).

## 8. Protocol additions

All are sent only when the welcome has `NSIM_WELCOME_POWER`, which the conductor sets only with `--power`. Without it the nodes send exactly what they sent before.

| Message | From | Where defined | Content |
| --- | --- | --- | --- |
| "xw" | conductor | `ns_port_sim.h` | the former `reserved` word is `flags`; `NSIM_WELCOME_POWER` |
| "xc" | the port, just before each "xi" | `ns_port_sim.h` | `events`, `timers[NSIM_CAUSE_COUNT]` (saturating 16-bit counts) |
| "xr" | the radio driver, during a wake | `sim_proto.h` | `state`, `tx_dbm`, `events` (`SIM_RADIO_EV_CCA`), `channel` |

Recording does not change what the nodes do: the messages go to the conductor, which does not relay them, and nothing is printed in the nodes' logs.

## 9. Checks of part 1 (2026-10-28)

`dag_simple`, 300 virtual seconds, seeds 1–10, `SETARCH=1`, campaigns on `tunvt*`:

- **Recording off, before and after the change:** node, BR and `log_wsbr` logs identical for all 10 seeds once printed heap addresses are masked (`compare_logs.py --mask-pointers`; the larger binary moves the heap by 0x1000). `analyze.py`: identical milestones.
- **Recording on versus off, same build:** byte-identical logs for all 10 seeds, unmasked. Wall time per run +18 %.
- **Sums:** Σ state and transition times = 300 000 000 µs for every node of every run, MCU and radio.
- **Plausibility (FG25 profile):** routers 6.86–7.13 mA (median 6.99), close to the always-listening figure (EM1 at 97.5 MHz 2.145 mA + RX 4.686 mA = 6.83 mA); TX adds the rest (0.8 % of the time at 0 dBm; these runs predate the fix below). The border router 8.65–8.83 mA (median 8.73): it sends more, at 20 dBm clamped to 15.9 dBm. MCU active about 1 % of the time; 104–123 wakes/s; empty ticks 0–76/s.
- **Determinism:** the same seed gives an identical `power.json` on another `tunvt` device.
- **Ledger arithmetic:** `test_power.py` (a scripted node with deep sleep, radio off, transitions, CCA, TX between two levels) matches the hand-computed totals exactly.
- **Stack:** the port's unit tests pass (one new: "xc" before each "xi"). The POSIX build's objects of HEAD and of this change, with `-ffile-prefix-map`: all stack objects identical; `sl_rf_driver.o` differs only by `__LINE__` immediates (the new code is under `NS_PORT_SIM`). The cross-build check links, and its images are byte-identical to HEAD's.

## 10. Known biases and limits of part 1

- **Active time is a model**, with placeholder cycles until HW5. The ECC of the TLS handshake is not counted separately.
- **Hardware-only interrupts** (UART, sleeptimer, RAIL events other than the driver's timers) are absent; the timer series do not drift apart, so separate wakes are undercounted (about 15–20 %, study §5.1).
- **Frames lost to the link** never enter `receive`; a real radio would often receive them up to a failed CRC.
- **The emulated driver has no RX→TX turnaround or idle→RX settling of its own:** the profile's transitions only move time between states. RAIL's state timings are HW5.
- **CCA duration** is the profile's, not the PHY's; it is cut from the backoff, which is long enough in practice.
- **Supply voltage:** the FG25 datasheet gives the MCU at 3.6 V and the radio at 3.3 V; the profile mixes them (HW5 replaces both).
- **Routers' TX power, fixed:** `wsnode` used to ignore `tx_power` (only `wsbrd` called `arm_nwk_set_tx_output_power()`), so routers sent at the driver's default, 0 dBm. It now applies it like `wsbrd` (default 20 dBm, clamped to 15.9 dBm by the FG25 profile). Seeds 1–10: logs identical apart from the new "change tx power" lines and line numbers, the same join milestones; routers 7.42 mA instead of 6.99 mA.

## 11. What part 1 leaves for later

| Item | Needs | Then |
| --- | --- | --- |
| Radio off, deep sleep in real runs | GS8 stage 1: the driver's off state, timed RX windows; the tickless event loop | The rule of §4 chooses deep sleep; empty ticks disappear |
| First real deep-sleep figures | GS8 stages 2–4: an LFN | Check against the AEM on the kits |
| MCU + RCP and module profiles | EM3 step 2 (split programs in virtual time) | The link machine; Spinel messages charged to both chips |
| Drift | EM3 step 5 | Separate wakes of the timer series; LFN window widening |
| Measured profile | HW5 | Cycles per kind of work, transitions, RX/TX currents at the Wi-SUN PHY, the AEM check |
| Exact airtimes | EM4 | RX and TX durations at the medium |
| Active time in virtual time | — (option) | Slow MCUs delay the protocol as on a board |
