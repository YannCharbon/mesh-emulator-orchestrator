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
| Profiles | `examples/power/*.ini` | `efr32fg25-soc.ini`: the FG25 SoC calibrated with HW5's measurements (§6, §12); `efr32fg25-soc-datasheet.ini`: the same from the datasheet only, for comparison |
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
| `DRVSTATE_OFF` | `phy_rf_state_control(PHY_INTERFACE_DOWN)` (GS8 stage 1); left by `PHY_INTERFACE_UP` | off |
| `DRVSTATE_IDLE` | init; `PHY_EXTENSION_SET_CSMA_PARAMETERS` with backoff 0 (cancel); `PHY_TX_NOT_ALLOWED` | idle |
| `DRVSTATE_IDLE_WAITING_RX` | `_rf_set_rx()`, end of TX/RX, CCA busy | listen |
| `DRVSTATE_CSMA_STARTED` | `phy_rf_tx()`: the backoff before a TX | listen |
| `DRVSTATE_RX` | `phy_rf_rx_now()`: a frame on our channel, not lost | receive |
| `DRVSTATE_TX` | `_rf_execute_tx()`; ends at the TX-done timer (airtime) | tx |

- **CCA:** `_rf_check_cca()` (only when the MAC enabled CCA) sends an "xr" with `SIM_RADIO_EV_CCA`. The driver does the CCA in no time; the ledger charges `cca_us` at the end of the listening that precedes it (§5.2).
- **TX power:** `PHY_EXTENSION_SET_TX_POWER` records the dBm the MAC asked for; each TX carries it.
- **Airtime** (§12.5): TX lasts the frame's bytes on air at the data rate, `rf_airtime_us()`: SHR (preamble + SFD) + PHR + the PSDU the MAC gives + FCS. The rate is the one the MAC sets with `PHY_EXTENSION_SET_RF_CONFIGURATION` (from the PHY mode: 1a/1b 50 kbit/s, 3 150 kbit/s...), 150 kbit/s only before that; `PHY_EXTENSION_GET_SYMBOLS_PER_SECOND` returns it, so the MAC's own timings (ACK wait) follow, and the RX/TX backup timer (`MAX_PACKET_SENDING_TIME`) is recomputed. Each frame's header (`sim_frame_hdr`) carries the sender's SHR and FCS lengths (on air, the FCS length is the PHR's FCS type bit); a receiver stays in `receive` for that SHR, its own PHR, the PSDU and that FCS, at its own rate, and its loss draw covers the PHR, PSDU and FCS bits. The settings are §6.1's `WSSIM_PHY_*`. EM4 will give exact airtimes at the medium later.
- **Off** (GS8 stage 1, LFN study E1–E2): `PHY_INTERFACE_DOWN` turns the radio off. A frame being received is lost; a CSMA backoff is cancelled; a frame being sent finishes first. While off, a channel change only records the channel (it does not turn RX on), the CCA shared memory and D-Bus show state 7 (`DRVSTATE_OFF`, added last so that the other values do not change), and frames that arrive are lost before the loss draw ("rf rx lost: radio off" in the node's log; they count as unseen frames, §5.3). `PHY_INTERFACE_UP` turns RX on again.
  - Who turns it off: the MAC, when RX-off-when-idle is set (`ws_management_rx_on_when_idle_set()`, or `rx_on_when_idle = false` / `rx_off_when_idle_after = <s>` in `wsnode.conf`): it is off whenever the MAC is idle, and on for a transmission, its CCA and the ACK wait. An FFN keeps RX on: its MAC calls `DOWN` only for an instant when it starts (`DOWN` then `UP`), which now costs a frame that was being received at that instant, as on the hardware.
  - `WSSIM_RADIO_OFF=0` in the environment of the nodes keeps the old behaviour (`DOWN` leaves RX on), to compare with runs made before GS8 stage 1: with it and `NANOSTACK_EVENTLOOP_TICKLESS=OFF`, `dag_simple` logs are identical to before for seeds 1–10.

### 3.2 MCU: the run loop's idle intervals

In virtual time the node's run loop (`NGS/port/sim/sim_scheduler.c`) runs until idle, sends "xi" with its next deadline, and blocks until "xa". So:

- **A wake** starts at each "xa" (time t). The welcome is the first wake, at 0.
- **The wake's work** is known at its "xi": the port's "xc" (§5.1) and the "xr" seen in between.
- **The idle** that follows lasts until the next "xa" of that node.

The ledger reads this in the conductor, so it works for any program that speaks the conductor protocol: EM7's clients get the same accounting by sending "xi" (and, if they can, "xc" and "xr"). On the firmware, the same intervals are the FreeRTOS idle task's sleeps (HW5).

### 3.3 Radio off and tickless loop: checks (GS8 stage 1, 2026-10-28)

`dag_simple`, 300 virtual seconds, seeds 1–10, `SETARCH=1`, the datasheet FG25 profile (`efr32fg25-soc-datasheet.ini`; wakes do not depend on the profile):

| | Before (periodic tick) | Tickless |
| --- | --- | --- |
| Router wakes/s over the run (min / median / max) | 103.5 / 115.7 / 122.6 | 40.9 / 61.9 / 104.3 |
| Router wakes/s once joined (200–300 s, seed 1) | 110–121 | 36–42 (node 14, joined at 290 s: 88) |
| Border router wakes/s (median) | 125.9 | 47.7 |
| Tick callbacks per second (median) | 100 | 0.10 |
| Empty ticks per second (median) | 54.7 | 0 |
| Router current (median, datasheet / HW5 profile) | 7.400 / 8.619 mA | 7.405 / 8.592 mA |

- **What is left once joined, per second:** the protocol timer 20, FHSS about 14 (TX-slot poll `fhss_event_timer_cb` about 8, unicast hop 4, broadcast schedule 2), the radio driver's timers about 5 under `dag_simple`'s traffic. Before a node joins, FHSS polls its TX queue every 10 ms (`DEFAULT_POLL_PERIOD`), so unjoined nodes still wake about 100 times a second. The idle-sleep study's 26–30 wakes/s left out the TX-slot poll and the radio's timers.
- **An FFN's current hardly moves** (−0.03 mA with the HW5 profile): its radio listens all the time and its MCU stays in EM1.
- **Sums:** Σ state and transition times = run time for every node of every run.
- **A node with RX off when idle** (`NODE_OPTS_1="-o rx_off_when_idle_after=150"`, seed 1): after 150 s node 1 receives no frame (284 before), 839 frames are lost while off; its radio is off for 145.3 of the last 150 s (listen 2.6 s, TX 2.1 s, CCA 0.07 s), and the MCU sleeps deep 143.6 s of them (§4: no hold once the radio is off). Average over the run 4.52 mA, against 7.64 mA for node 2. With `rx_on_when_idle = false` from the start, node 1 never joins, sleeps deep 95.7 % of the time and averages 0.44 mA (with 103 wakes/s, FHSS's 10 ms poll before joining).

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
charge      = active time × active_ma + wake_charge_uc     (per wake)
```

- **Causes** (`enum nsim_cause`): the port names each timer callback by its symbol (libbacktrace reads the program's symbol table once per callback; the stack's callbacks are static): `tick` (`tick_timer_eventOS_callback`), `protocol` (`protocol_timer_interrupt`), `fhss` (`fhss_*`, and every FHSS platform timer), `radio` (the driver's `_rf_*` timers: RAIL events on the board), `mac` (`timer_mac_interrupt`, `mac_*_timer_cb`), `other`.
- **Events:** the port counts the event-loop events it dispatches (`eventOS_scheduler_dispatch_event()` in a loop, which is what `eventOS_scheduler_run_until_idle()` does).
- **Frames received and sent:** the radio's `receive` and `tx` entries in the wake.
- **Values:** the FG25 profile's cycles are HW5's DWT measurements (§12.1), mapped onto these causes; the MAC and "other" timers are still estimates. Cryptography is inside `rx_frame`/`tx_frame` (CCM; mbedTLS software AES on the board, 990 cycles a block); the TLS handshake's ECC during joining is not separate yet (§9).
- **Charge per wake** (`mcu.wake_charge_uc`, optional, 0 when absent): a fixed charge for each wake, on top of the cycles, with no time of its own (it does not move the sleep). HW5 found the CPU's real cost per wake 3.5–6 times what cycles × (EM0 − EM1) gives (0.5–0.8 µC against 0.14 µC): interrupt work that the power manager counts as EM1, clock start-up, flash wait states. It is a charge rather than more cycles so that the active time keeps matching the measured EM0 time, and `active_ma` stays the datasheet's EM0. The report gives it per node as `mcu.wake_charge` (count, µC each, total); it is part of `charge_uc` and of the average.
- **Timeline:** the work runs from the wake at t to t + active time; the sleep starts then. A wake that comes before the previous work is done extends it (`wakes_while_active`). Virtual time itself does not advance by the active time: that option (slow MCUs delaying the protocol) is for later.

### 5.2 Accounting rules

- **Transitions are cut out of the states they join,** so the sums hold: a sleep is `enter`, then the sleep, then `wake` (no wake at the end of the run); a radio transition is charged at the start of the state it enters; a CCA at the end of the listening before it. A transition longer than the interval is cut to fit.
- **Charge** = time × current of the state, in µC. Chip current = MCU state current + radio state current: the profile's radio currents are what the radio adds to the MCU's.
- **End of the run:** every node runs until `--vtime-end` (or the last wake if the run stopped early).

### 5.3 Wake causes, empty ticks, unseen frames

Per node the report counts:

- **wakes**, and per cause the wakes in which it appeared (`wakes_with`) and its callbacks (`timer_callbacks`);
- **empty ticks:** wakes where only the 10 ms tick fired and no event was dispatched and no frame came. This is what a tickless event loop removes (idle-sleep study §3). With the stack's tickless event loop (`NANOSTACK_EVENTLOOP_TICKLESS`, default ON since GS8 stage 1) the tick fires only when an event timer is due, so empty ticks are about 0; build with it `OFF` to see the periodic tick's;
- **unseen frame wakes:** wakes with only frames that the radio did not take (another channel, lost to the link, or the radio busy). The emulator delivers every frame to every neighbour; a board's radio would not have interrupted the CPU. They are not counted as wakes and cost nothing (`frames_unseen` keeps the count).

## 6. Profile format

An INI file; `#` or `;` start a comment; every key is required unless said otherwise. Currents in mA, times in µs, clock in MHz. Each figure should say where it comes from (`[M]` measured, with the document and section, `[D]` datasheet table, `[V]` our code, `[I]` estimate).

| Section | Keys |
| --- | --- |
| `[profile]` | `name`, `kind` (`soc`, `mcu+transceiver`; `mcu+rcp` after EM3 step 2), `supply_v` |
| `[mcu]` | `clock_mhz`; `active_ma`, `sleep_ma`, `deep_sleep_ma`; `sleep_enter_us/_ma`, `sleep_wake_us/_ma`, `deep_enter_us/_ma`, `deep_wake_us/_ma`; `wake_charge_uc` (optional, default 0: µC charged per wake on top of the cycles, §5.1) |
| `[cycles]` | `wake`, `tick`, `protocol`, `mac`, `fhss`, `radio`, `other`, `event`, `frame_in`, `rx_frame`, `tx_frame` |
| `[radio]` | `off_ma`, `idle_ma`, `listen_ma`, `receive_ma`, `cca_ma`, `cca_us`, `idle_holds_clock` (0/1), `tx_ma = dBm:mA dBm:mA ...` (increasing dBm; linear in between, clamped outside, and the report flags a clamped level) |
| `[radio_transitions]` (optional) | `<from>_<to> = <µs> <mA>` for the states `off idle listen receive tx`; unlisted transitions are free |
| `[link]` | later (EM3 step 2): link current, bytes per second, per-message cost |

**MCU + external transceiver** (STM32F769 + S2-LP, the MTS bench): the same machines; the transceiver's currents go in `[radio]`, and the SPI work per frame in `rx_frame`/`tx_frame`. No such profile yet.

Two FG25 profiles:

- **`examples/power/efr32fg25-soc.ini`, the default:** HW5's measurements where HW5 has them (RX listening, TX at +10 dBm, the charge per wake, the cycles per kind of work, the supply voltage), the datasheet or an estimate elsewhere, each figure tagged. Its conditions: 3.33 V, EU mode 1a (50 kbit/s), +10 dBm: the nodes' configuration `CONF=eu1a` (§12.5). §12.1 lists every change and what is still not measured.
- **`examples/power/efr32fg25-soc-datasheet.ini`:** the profile as it was before HW5, from the datasheet (Rev. 1.2) only, with placeholder cycles. Kept so that a user can see how far the datasheet is from the board (§12.2): its radio currents are the datasheet's whole-chip RX/TX figures minus EM1 at 39 MHz, because they were measured with the MCU in EM1 at 39 MHz.

### 6.1 Every consumption parameter is the user's to set

The profiles are examples, not fixed models: a user sets them to the hardware and use case being studied.

| Parameter | Where it is set | Per node |
| --- | --- | --- |
| Currents, transition times and charges, MCU clock, supply voltage, cycles per kind of work, charge per wake, CCA time, TX current per dBm | The profile file (§6); copy `examples/power/efr32fg25-soc.ini` and edit it | `--power-node=IDS:PROFILE` |
| TX power (dBm) the stack asks for | `tx_power` in `wsbrd.conf` / `wsnode.conf` (default 20, the stack's), or `-o tx_power=N` | `run.sh`: `NODE_OPTS_<i>="-o tx_power=N"` |
| Battery cell | `--power-cell=mAh[:V[:eff]]` (`POWER_CELL`) | One per run |
| PHY data rate | The PHY mode in `wsbrd.conf` / `wsnode.conf` (`mode`; `examples/ws*-eu1a.conf`: the FG25 firmware's, 1a), or `-o`/`-m`; `WSSIM_PHY_DATARATE=<bit/s>` in the nodes' environment fixes it whatever the mode | `run.sh`: `NODE_OPTS_<i>`, `NODE_ENV_<i>` |
| PHY overhead per frame (it lengthens every TX and RX) | `WSSIM_PHY_PREAMBLE` (bytes, default 8, Wi-SUN FAN's minimum), `WSSIM_PHY_SFD` (2), `WSSIM_PHY_PHR` (2), `WSSIM_PHY_FCS` (4 or 2, default 4 as FAN requires) in the nodes' environment. `WSSIM_PHY_LEGACY=1`: the PHY before 2026-10-28 (150 kbit/s whatever the mode, PSDU only), to compare with older runs | `run.sh`: `NODE_ENV_<i>="WSSIM_PHY_PREAMBLE=16"`, `BR_ENV` |

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
- **Plausibility (FG25 profile as it was then, now `efr32fg25-soc-datasheet.ini`; the measured profile's figures are in §12.2):** routers 6.86–7.13 mA (median 6.99), close to the always-listening figure (EM1 at 97.5 MHz 2.145 mA + RX 4.686 mA = 6.83 mA); TX adds the rest (0.8 % of the time at 0 dBm; these runs predate the fix below). The border router 8.65–8.83 mA (median 8.73): it sends more, at 20 dBm clamped to 15.9 dBm. MCU active about 1 % of the time; 104–123 wakes/s; empty ticks 0–76/s.
- **Determinism:** the same seed gives an identical `power.json` on another `tunvt` device.
- **Ledger arithmetic:** `test_power.py` (a scripted node with deep sleep, radio off, transitions, CCA, TX between two levels) matches the hand-computed totals exactly.
- **Stack:** the port's unit tests pass (one new: "xc" before each "xi"). The POSIX build's objects of HEAD and of this change, with `-ffile-prefix-map`: all stack objects identical; `sl_rf_driver.o` differs only by `__LINE__` immediates (the new code is under `NS_PORT_SIM`). The cross-build check links, and its images are byte-identical to HEAD's.

## 10. Known biases and limits of part 1

- **Active time is a model**, with HW5's cycles per kind of work (§12.1). The ECC of the TLS handshake is not counted separately. The emulator has fewer wakes than the board (§12.2), so the charge per wake adds up to less.
- **Hardware-only interrupts** (UART, sleeptimer, RAIL events other than the driver's timers) are absent; the timer series do not drift apart, so separate wakes are undercounted (about 15–20 %, study §5.1).
- **Frames lost to the link** never enter `receive`; a real radio would often receive them up to a failed CRC.
- **The emulated driver has no RX→TX turnaround or idle→RX settling of its own:** the profile's transitions only move time between states. RAIL's state timings are HW5.
- **CCA duration** is the profile's, not the PHY's; it is cut from the backoff, which is long enough in practice.
- **Supply voltage:** the measured profile is at HW5's 3.33 V; its EM0/EM1 figures are still the datasheet's (3.6 V), only their sum with the radio was measured.
- **Airtime: fixed** (§12.5). The emulated PHY used to send at a fixed 150 kbit/s with no preamble, SFD, PHR or FCS; it now takes the PHY mode's rate and counts the overhead. `examples/wsbrd.conf`/`wsnode.conf` stay EU mode 3, class 2 (35 channels, 150 kbit/s), the mode most of our hardware applications use (decided 2026-10-28); `examples/ws*-eu1a.conf` (`CONF=eu1a`) are the board's mode 1a, class 1. Left: the RX timestamp the MAC reads (`PHY_EXTENSION_READ_RX_TIME`) is the frame's arrival at the sender's TX start, not the end of the SHR as on RAIL; a receiver on another PHY mode than the sender's still decodes its frames (on its own rate).
- **Routers' TX power, fixed:** `wsnode` used to ignore `tx_power` (only `wsbrd` called `arm_nwk_set_tx_output_power()`), so routers sent at the driver's default, 0 dBm. It now applies it like `wsbrd` (default 20 dBm, clamped to 15.9 dBm by the FG25 profile). Seeds 1–10: logs identical apart from the new "change tx power" lines and line numbers, the same join milestones; routers 7.42 mA instead of 6.99 mA.

## 11. What part 1 leaves for later

| Item | Needs | Then |
| --- | --- | --- |
| Radio off, deep sleep in real runs | GS8 stage 1 (done: the driver's off state, the MAC's RX-off-when-idle, the tickless event loop); stage 2: timed RX windows | Done: a node with RX off when idle sleeps deep (§3.1, §5.3); empty ticks are gone. Left: timed RX windows (an LFN listening) |
| First real deep-sleep figures | GS8 stages 2–4: an LFN | Check against the AEM on the kits |
| MCU + RCP and module profiles | EM3 step 2 (split programs in virtual time) | The link machine; Spinel messages charged to both chips |
| Drift | EM3 step 5 | Separate wakes of the timer series; LFN window widening |
| Measured profile | HW5 (done, §12) | Left: the radio and MCU transitions, EM2, TX at other levels, RX while receiving: HW8 and more measurements |
| PHY airtime as on the hardware | §12.4 (done, §12.5) | Done: the border router within 3 % of HW5's TX time, frame rate and current; the router's current within 3 %, its TX time 15–18 % above (more PA/PC sweeps, §12.5) |
| Exact airtimes | EM4 | RX and TX durations at the medium |
| Active time in virtual time | — (option) | Slow MCUs delay the protocol as on a board |

## 12. Calibration against HW5 (2026-10-28)

HW5 measured our FAN 1.0 firmware on an FG25 kit with the BRD4001A's energy monitor (AEM) and firmware counters: the root repo's `documents/pre-study/investigation/HW5_BlackBox_Phase1_Results.md` (below "HW5 §x"). Setup: a border router and one router, side by side, idle, EU mode 1a (2-FSK 50 kbit/s), 69 channels, +10 dBm, 3.33 V, 10 windows of 30 s per role, 77–381 s after the router joined. AEM averages: **router 9.77 mA, border router 11.32 mA**.

Runs below: `wssimserver` with this change; `wsbrd-inproc` and `wsnode-inproc` from `build-vt` at `db1a881` (the stack at `127d991`), copied before the runs, so no later working-tree change is in them. Seeds 1–10, `SETARCH=1`, campaigns on `tunvt*`.

### 12.1 Profile changes

`examples/power/efr32fg25-soc.ini` now holds HW5's figures; the datasheet-only profile is `efr32fg25-soc-datasheet.ini`.

| Key | Before [D/I] | Now | Source |
| --- | --- | --- | --- |
| `supply_v` | 3.3 | 3.33 | [M] HW5 §1 |
| `radio.listen_ma` (and `receive_ma`, `cca_ma`) | 4.686 | **6.0** | [M] HW5 §2.4: 8.15 mA chip with RX on and the MCU in EM1 (fit over 40 windows, 10 kHz trace median 8.13–8.14), minus EM1 2.145 [D]. Receive and CCA not measured apart [I] |
| `radio.tx_ma` | 0:23.9 12:53.5 15.9:75.8 | 0:17.59 **10:35.75** 12:39.38 15.9:55.80 | [M] HW5 §2.4 at +10 dBm: 37.9 mA chip in TX (CCA and ramp included) − 2.145. Other levels [I]: the datasheet's 915 MHz curve scaled by 35.75 / 48.55 = 0.736; the datasheet's 868 MHz 14 dBm point is 6.6 % above the scaled curve. A table the user edits |
| `mcu.wake_charge_uc` (new) | — | **0.56** | [M] HW5 §2.4: 0.5–0.8 µC per wake above the RX + EM1 floor, minus the cycle part of a mean wake, 9 730 cycles × 0.875 mA / 97.5 MHz = 0.087 µC |
| `cycles.wake` | 3 000 | 4 560 | [M] HW5 §2.5: tick wake 9 190 − tick handler 4 630 |
| `cycles.tick`, `protocol`, `event` | 1 500, 6 000, 4 000 | 4 630, 4 950, 3 520 | [M] HW5 §2.5 |
| `cycles.fhss` | 3 000 | 10 200 | [M] HW5 §2.5: mean of the FHSS port timer (14 900 at 5.9/s) and `fhss_event_timer_cb` (3 170 at 3.9/s) |
| `cycles.radio` | 1 500 | 5 830 | [M] HW5 §2.5: TX-done and CCA_PREPARE callbacks |
| `cycles.rx_frame` | 25 000 | 22 300 | [M] HW5 §2.5: RX wake 32 700 − `wake` − one `radio` callback (49 900 with a ping) |
| `cycles.tx_frame` | 15 000 | 67 000 | [M] HW5 §2.5: TX-prepare wake 56 200 + TX-event wake 31 600 − 2 × (`wake` + `radio`); RAIL's TX start alone is 43 800 |
| `cycles.mac`, `other` | 2 000, 2 000 | 5 000, 5 000 | [I] not seen in HW5's idle windows; like the other callback timers |

Unchanged, **still not measured**: EM0 and EM1 on their own (HW5 measured EM1 + RX), EM2 and every MCU transition (the FFN firmware never goes below EM1: HW8), the radio's idle and off currents, every radio transition, TX at levels other than +10 dBm, the CCA alone (HW5 bounds CCA + PA ramp + RAIL's wait at ≤ 0.25 ms per frame: TX-state time minus airtime; `cca_us` stays 128).

Why a new key rather than more cycles: the measured cost per wake is 3.5–6 times what the cycle counter and the datasheet's EM0 − EM1 give. Raising `active_ma` to about 8.6 mA would reproduce the charge but make the EM0 current and the active time inconsistent with the datasheet and with the measured EM0 time (2.5 %); adding cycles would stretch the active time. A charge per wake keeps both right and is what a tickless loop removes, wake by wake.

The fit of HW5 (AEM = 8.15 mA + 29.7 mA × TX fraction) also holds TX's CPU work: about 0.8 µC per frame in this profile's `tx_frame`, against 680 µC of TX current per frame: 0.1 %, ignored.

### 12.2 Router and border router against the AEM

`dag_simple`, 300 s, all nodes at +10 dBm (`NODE_OPTS="-o tx_power=10" BR_OPTS="-o tx_power=10"`) unless said; medians over 10 seeds (routers: 14 × 10 node-runs).

| | Routers | Border router | Router TX time | BR TX time |
| --- | --- | --- | --- | --- |
| Datasheet profile, 20 dBm (clamped to 15.9) | 7.40 mA | 8.73 mA | 0.79 % | 2.65 % |
| Datasheet profile, +10 dBm | 7.19 mA | 8.00 mA | 0.79 % | 2.65 % |
| Measured profile, 20 dBm (clamped to 15.9) | 8.62 mA | 9.55 mA | 0.79 % | 2.65 % |
| **Measured profile, +10 dBm** | **8.46 mA** (8.26–8.68) | **9.02 mA** (8.99–9.07) | 0.79 % | 2.65 % |
| HW5 AEM, idle | **9.77 mA** | **11.32 mA** | 5.4 % | 10.7 % |

What is comparable: the floor, not the totals. The emulated network has 15 nodes and the HW5 one two; the emulated runs start at the cold start (joining, EAPOL, DAOs within the 300 s), HW5's windows were 77–381 s after the join. Above all, emulated nodes spend 7 times less time in TX (§12.3), and TX is what separates the two figures:

- **Floor without TX**: the emulated router at 0 % TX, from its average minus its TX share (29.75 mA × TX fraction, as in HW5's fit), is **8.22 mA**: EM1/EM0 2.16, charge per wake 0.065, RX 5.95. HW5: 8.15 mA + 0.08–0.13 mA of CPU = 8.23–8.28 mA.
- **The profile at the board's TX time**: 8.22 + 29.75 × 5.43 % = **9.84 mA** for the router (AEM 9.77, +0.7 %), 8.23 + 29.75 × 10.7 % = **11.41 mA** for the border router (AEM 11.32, +0.8 %).
- **CPU**: 116 wakes/s for an emulated router against 156 on the board (hardware interrupts and the board's 23–29 "none" wakes are absent, §10), while the cycles per second are close: 1.84 M/s modelled (median; 1.5–2.3), 1.52 M/s measured (the emulated FHSS timers fire 19 times a second, the board's 10). The charge per wake therefore adds 0.065 mA here against 0.087 mA on the board.

With the datasheet profile the floor is 6.84 mA: the datasheet's RX current at 38.4 kbit/s is 1.3 mA below the board's at 50 kbit/s (HW5 §2.4).

### 12.3 Why the board transmits 7 times more

HW5's router: 2.95 frames/s, TX 5.4 % of the time (airtime 5.3 %); the border router 6.95 frames/s, 10.7 % (airtime 10.3 %). Emulated `dag_simple` routers: 1.97 frames/s, 0.79 %.

**Frame types.** The board's frames in the idle windows are almost all PAN advertisement (PA) and PAN configuration (PC) sweeps: asynchronous frames, sent once on **every** channel of the plan. The trace before the windows shows them (`TX_start 66, 67, 68`: channel numbers), and the windows' airtime per frame gives their length: (10 B SHR + 2 B PHR + PSDU + 4 B FCS) × 8 / 50 kbit/s, so 19.0 ms is a 105-byte PC and 10.9 ms a 54-byte PA, the same lengths as in the emulator. The router's 898 frames in 304 s are about 13 sweeps of 69 frames (≈ 11 PC, 2 PA), plus a DIO or DAO now and then. The emulated nodes send the same sweeps. Classified from the node logs (`Send PAN ...` then the `Transmitting ... chan=` lines, `ACK TX`, `transmit dio`, `waiting_ack` for unicast), per node:

| Run | Node | PA | PC | Others | Frames/s | Mean PSDU | Airtime, as emulated | Airtime at 50 kbit/s + 16 B |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| HW5, 2 boards, idle | Router | ≈ 0.45/s | ≈ 2.5/s | < 0.05/s | 2.95 | ≈ 100 B | — | 5.3 % (counted) |
| | BR | ≈ 3.5/s | ≈ 3.5/s | | 6.95 | ≈ 80 B | — | 10.3 % (counted) |
| Emulated, 2 nodes, 35 channels (mode 3, class 2), 170–470 s | Router | 0.77/s | 0.67/s | 0.04/s | 1.47 | 79 B | 0.62 % | 2.23 % |
| | BR | 1.74/s | 1.68/s | 0.03/s | 3.45 | 80 B | 1.46 % | 5.27 % |
| Emulated, 2 nodes, **69 channels (mode 1a, class 1)**, 170–470 s | Router | 1.48/s | 1.42/s | 0.10/s | **3.00** | 80 B | 1.28 % | **4.6 %** |
| | BR | 3.31/s | 3.39/s | 0.22/s | **6.92** | 80 B | 2.94 % | **10.6 %** |
| Emulated `dag_simple`, 35 channels, 300–600 s | Router | 0.89/s | 0.89/s | 0.06/s | 1.85 | 80 B | 0.79 % | 2.84 % |
| | BR | 1.73/s | 1.70/s | 0.13/s | 3.56 | 80 B | 1.51 % | 5.45 % |
| Emulated `dag_simple`, 69 channels, 300–600 s | Router | 1.88/s | 1.78/s | 0.22/s | 3.88 | 79 B | 1.64 % | 5.90 % |
| | BR | 3.24/s | 3.26/s | 0.46/s | 6.96 | 80 B | 2.98 % | 10.7 % |

(HW5's PA/PC split is inferred from the window airtimes. The emulated "69 channels" runs use `-m 1a -c 1` on every node, a configuration change only: the stack sweeps the 69 channels, the emulated PHY still sends at 150 kbit/s. "Airtime at 50 kbit/s + 16 B" recomputes each emulated frame as the board sends it.)

**The causes, multiplied** (router, 2 nodes, the board's setup):

1. **Airtime per frame, ×3.6.** The emulated driver's TX (and RX) lasts `len × 8 / 150 kbit/s` (`CALC_SLEEP_TIME`, `phy_subghz.datarate`, fixed): the PSDU only, without the 8-byte preamble, 2-byte SFD, 2-byte PHR and 4-byte FCS, and at 150 kbit/s whatever PHY mode the stack configures (`PHY_EXTENSION_SET_RF_CONFIGURATION` is ignored: with `-m 1a -c 1` the MAC logs `Datarate: 50000bps`, and a 45-byte frame still lasts 2 400 µs). The board sends at 50 kbit/s with 16 bytes more: a 105-byte PC lasts 5.6 ms emulated, 19.4 ms on air; a 54-byte PA 2.9 ms against 11.2 ms. The TX state itself does cover the whole emulated airtime (it starts at `_rf_execute_tx()` and ends at the TX-done timer, `CALC_SLEEP_TIME(len)` later): nothing is lost there.
2. **Channels per sweep, ×1.97.** `examples/wsbrd.conf` and `wsnode.conf` use EU mode 3, class 2: 35 channels at 200 kHz. The firmware uses EU mode 1a, class 1: 69 channels at 100 kHz. With `-m 1a -c 1` the emulated frame rates become the board's: 3.00/s against 2.95/s (router), 6.92/s against 6.95/s (BR).
3. **Sweep rate: the same.** Trickle settings are the same on both sides (`NETWORK_SIZE_SMALL`), and so is the number of sweeps: about 2.5 per minute for the router, 6 per minute for the BR. The emulated 15-node network does not send fewer sweeps per router than the 2-node one (it sends more: more neighbours reset the trickle timers).
4. **Mix, ×1.2.** The board's router sent mostly PC sweeps (≈ 11 of 13), the emulated one PA and PC in equal numbers, so the board's mean sweep frame lasts 18.1 ms on air against 15.0 ms for the emulated mix. Thirteen sweeps are a small sample; the cause is not known (the trickle suppression of PA versus PC with one neighbour).
5. **Not causes:** ACKs (one per unicast frame; ≈ 0 in idle on both sides), unicast and DIO traffic (< 0.05 frames/s on both sides), the CCA (counted in the board's TX state: TX time − airtime = 0.17 % of the time; the emulator charges it apart, 128 µs per CCA, at the RX current), the TX state's duration in the driver (1. above).

Product: 0.62 % × 3.6 (airtime) × 1.97 (channels) ≈ 4.4 %, × 1.2 (mix) ≈ 5.3 %, against 5.3 % measured. With 69 channels and the board's airtime the emulated border router is at 10.6 % against 10.3 %. **The emulator runs the right protocol at the wrong PHY**: the stack's frames, their types and their rates match the board; the emulated PHY makes each frame 3.6 times shorter, and the example configuration halves the channels. Applied to the power: 8.22 + 29.75 × 5.9 % ≈ 9.98 mA per emulated `dag_simple` router at the board's PHY.

A smaller finding on the board's side: the firmware's airtime counter (`rail_phy.c`, `ns_pstats_tx_frame()` and the TX deadline) counts the SHR as 8 bytes; the preamble is 8 bytes and the SFD 2 more (`radioconf_generation_log.json`: 64-bit preamble, 16-bit sync word), so every frame's airtime is 0.32 ms short (1.7 % of a PC).

### 12.4 Proposed emulator changes (1–3 made, §12.5)

The airtime is behaviour of the emulated PHY, not a power parameter, so it was not changed with the profile; these were the proposals. 1, 2 and 3 are done (§12.5); 4 belongs to EM4.

1. **PHY rate from the stack's configuration.** In `sl_rf_driver.c`, honour `PHY_EXTENSION_SET_RF_CONFIGURATION` (the stack sends `phy_rf_channel_configuration_s` with the datarate of the configured PHY mode) for `CALC_SLEEP_TIME`, `MAX_PACKET_SENDING_TIME` and `PHY_EXTENSION_GET_SYMBOLS_PER_SECOND`, instead of the fixed 150 kbit/s. Then the `mode` in the `.conf` sets the airtime, as on a board, and the MAC's own timing (ACK wait, CSMA) follows.
2. **PHY overhead.** Count the SHR (preamble, default 8 bytes as Wi-SUN FAN requires, plus a 2-byte SFD), the 2-byte PHR and the FCS (4 bytes in FAN; 2 when the PHR says so) in TX and RX durations: a user setting (driver option or conductor option), defaulting to the FAN values.
3. **A hardware-matching configuration.** `examples/wsbrd-eu1a.conf` / `wsnode-eu1a.conf` (or a `run.sh` option) with EU mode 1a, class 1 as the firmware uses; or move the examples to it. With 1. only, mode 3 / class 2 would still be valid, just a different PHY.
4. **Exact airtimes at the medium (EM4)** should use the same rate and overhead, so that collisions and CCA see frames as long as on air.

Expected effect, from §12.3: emulated routers at 5–6 % TX and the border router at 10–11 %, within about 15 % of HW5, and router averages near 9.8–10 mA with the measured profile. A sweep would also keep the radio busy for about 1.7 s instead of 0.3–0.4 s, which matters for latency studies, not only for power.

Reproduce (§12.2–12.3, with the PHY as it was: add `WSSIM_PHY_LEGACY=1` on a build with §12.5): `POWER=examples/power/efr32fg25-soc.ini NODE_OPTS="-o tx_power=10" BR_OPTS="-o tx_power=10" sim_utils/vtime/campaign.sh build-vt $(pwd) runs/hw5 300 $(seq 1 10)` (add `-m 1a -c 1` to both options for the 69-channel plan, `TOPO=` a one-line `0,1:-14.0` file for the 2-node network).

### 12.5 The PHY fixes and their effect (2026-10-28)

Made (§12.4, 1–3), in `app_wshwsim/sl_rf_driver.c`, `app_wssimserver/sim_proto.h` and `sim_utils/vtime/run.sh`:

1. **Data rate from the stack.** `PHY_EXTENSION_SET_RF_CONFIGURATION` sets the driver's rate (`rf_phy_configure()`); TX and RX airtimes, `MAX_PACKET_SENDING_TIME` and `PHY_EXTENSION_GET_SYMBOLS_PER_SECOND` use it. The 150 kbit/s of `phy_subghz` holds only until the MAC configures the PHY, at its start (`PHY: 50000 bit/s from the MAC's configuration` in the node's log, after the MAC's `Datarate: 50000bps`). EU mode 3, the examples' mode, is 150 kbit/s: for them only the overhead changes.
2. **PHY overhead**, user settings with Wi-SUN FAN's values as defaults (§6.1): preamble 8 B, SFD 2 B, PHR 2 B, FCS 4 B. A frame of `n` bytes from the MAC lasts (n + 16) × 8 / rate: a 105-byte PC 19.4 ms at 50 kbit/s, as on the board. The sender's SHR and FCS lengths travel in the frame header (two new bytes in `sim_frame_hdr`; `fake_node.py` and `test_conductor.py` follow).
3. **The firmware's configuration**: `examples/wsbrd-eu1a.conf`, `wsnode-eu1a.conf` (EU, mode 1a, class 1, 69 channels, `tx_power = 10`, as `app_config.h` with `FW_REGION_EU` and `RAIL_PHY_TX_POWER_FIXED_DDBM`); `run.sh` takes them with `CONF=eu1a`. `run.sh` also gained `BR_ENV` and `NODE_ENV_<i>` (environment for one program: per-node PHY settings).

Checks, seeds 1–10, `SETARCH=1`, campaigns on `tunvt*`, the measured profile:

- **`WSSIM_PHY_LEGACY=1` against the build before the change**, `dag_simple` 300 s: every node, BR and `log_wsbr` log identical once the one new `PHY: legacy` line is removed and printed addresses masked; `power.json` byte-identical for all 10 seeds.
- **Default configuration (mode 3, class 2), before and after**, `dag_simple` 300 s, 20 dBm: the frame count is unchanged and each frame is 16 bytes longer (≈ +20 % for an 80-byte PSDU at 150 kbit/s). `analyze.py`: nodes ready within 300 s 133 → 130 of 140 (12–14 of 14 per run, both), median time to ready 146.4 → 146.7 s, to parent selection 89.3 → 92.2 s; every parent a `dag_simple` neighbour. Medians per node-run: routers TX 0.80 → 0.94 % of the time, 1.99 → 1.98 frames/s, 8.59 → 8.66 mA; border router TX 2.67 → 3.19 %, 5.32 → 5.41 frames/s, 9.51 → 9.77 mA. A small, expected change.
- **The board's setup** (`CONF=eu1a`, +10 dBm): two nodes (border router + one router, `0,1:-14.0`; the router ready at 68–92 s), and `dag_simple`. Figures over a 300 s window, from the difference of two runs of the same seed ending at the window's start and end (the shorter run is a prefix of the longer one). Medians over the seeds (range):

| | TX time | Frames/s | Average current |
| --- | --- | --- | --- |
| HW5 router (one 304 s sample) | 5.4 % (airtime 5.3 %) | 2.95 | 9.77 mA |
| Emulated router, 2 nodes, 170–470 s | **6.2 %** (3.2–8.5) | **3.87** (1.86–5.65) | **10.0 mA** (9.1–10.7) |
| Same, PHY before the change | 1.27 % (1.05–1.57) | 3.12 (2.31–3.52) | 8.55 mA |
| Emulated routers, `dag_simple`, 300–600 s (140 node-runs) | 5.8 % (0.6–10.8) | 3.81 (0.49–7.0) | 9.9 mA (8.3–11.4) |
| HW5 border router | 10.7 % (airtime 10.3 %) | 6.95 | 11.32 mA |
| Emulated BR, 2 nodes, 170–470 s | **10.6 %** (10.4–10.8) | **6.91** (6.80–7.04) | **11.3 mA** (11.3–11.4) |
| Same, PHY before the change | 2.94 % | 6.92 | 9.05 mA |
| Emulated BR, `dag_simple`, 300–600 s | 10.7 % (10.1–11.3) | 6.97 (6.53–7.24) | 11.4 mA (11.2–11.5) |

The emulated TX state is the airtime (the CCA is charged apart), so the board's airtime is the closer figure.

- **Border router: matches.** TX time +3 % on the board's airtime, frames −0.6 %, current −0.2 %, the seeds within ±2 %.
- **Router: the current matches** (+2.4 %), **the TX time is 15–18 % above** the board's and the frame rate 31 % above, with a wide spread over the seeds. The mean frame is shorter (16.1 ms against 18.3 ms: the PA/PC mix of §12.3), and the router sends more sweeps: PA + PC sweeps in the window, median 17 (8–25) against 13.5 (10–16) with the old PHY and about 13 on the board in 304 s. With frames 3.6 times longer, a sweep keeps the radio in TX for about 1 s, during which the node hears nothing; sweeps of the two nodes overlap more often (seed 2: 35 CCAs busy, 25 sweeps), frames heard are fewer, and Trickle suppresses fewer of the router's own. The board has the same physics; its single 304 s sample lies inside the emulated range. More board windows (HW5 had 10 of 30 s in one run) would tell whether the median differs.
- **`dag_simple` at the board's PHY**: as §12.3 predicted from the frame logs (routers 5.9 %, BR 10.7 %). Joining is slower than with mode 3 at 150 kbit/s over 35 channels, as expected with 69 channels at a third of the rate: 121 of 140 nodes ready within 300 s (11–14 per run), median 162.0 s against 146.4 s; every parent a neighbour. Over the first 300 s the border router transmits 15.3 % of the time.

Reproduce: `CONF=eu1a POWER=examples/power/efr32fg25-soc.ini TOPO=<0,1:-14.0 file> sim_utils/vtime/campaign.sh build-vt $(pwd) runs/eu1a_170 170 $(seq 1 10)`, the same with 470, then the difference of the two `power.json` per seed.
