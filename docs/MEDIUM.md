# Central radio medium (EM4): design for implementers

Plan and rationale: the root repo's `documents/pre-study/investigation/Investigation_Nanostack_Emulator.md` §5 (P2, "Target architecture") and `STATUS.md` EM4. This note fixes the interfaces of the MVP so that the server side and the radio side can be built in parallel. Scope: **virtual time only** (`wssimserver --vtime`, in-process nodes on the simulation port), where it is the default since the gate passed (§8). Wall-clock mode and the split programs keep the per-radio behaviour.

## 1. What changes

Until EM4, each radio decides reception alone: the server relays a frame to the sender's graph neighbours at the sender's TX start, and the receiver draws a loss from its link's RSSI (`phy_rf_rx_now()`), keeps the first frame and drops any frame that arrives while it receives, and its CCA looks at the state of the neighbours in its own neighbour table (shared memory `/wssimcca`).

With the central medium (the default with `--vtime`; `--medium=central` says it explicitly):

- **The server decides every physical outcome:** the power each receiver gets, whether the receiver locks onto the frame, capture by a stronger frame, the interference over the frame, and success or loss at the frame's end (one draw per frame and receiver, from the server's seeded streams).
- **The radio keeps its own state machine** (off, listening, receiving, CSMA, sending) and reports it in shared memory, as today. It no longer draws losses, and its RX end comes from the server's verdict, not from its own airtime timer.
- **CCA asks the server** for the energy on the channel at that moment, and compares it with the CCA threshold the MAC set.

`--medium=legacy` (the default until the EM4 gate passed, §8) is the behaviour before EM4, byte for byte: with it, the nodes' logs stay identical to the permanent reference runs, so the non-regression scripts pass it.

## 2. Physical model

All powers in dBm at the receiver's antenna; sums are done in mW.

- **Received power.** `P = link(rx, tx)` from the `-g` options (and `--vtime-link`), or, with `--positions`, from the node positions and a path-loss model (§11), which also derives who hears whom. With `-g` values: the value is the received power, whatever TX power the sender uses. With `--medium-tx-ref=DBM`, the value is the received power when the sender transmits at DBM, and `P = link + (tx_dbm − DBM)`, with `tx_dbm` from the frame header: the TX power the MAC last set (`wsnode` and `wsbrd` set it from their `tx_power` setting at start; a radio whose MAC never set one reports 0 dBm). An edge without a value (0 dBm) stays a perfect link; it is also a 0 dBm interferer and 0 dBm of CCA energy, so two frames over such links always destroy each other and neither can capture the other. Give RSSIs to the edges when collisions matter. Pairs that are not graph neighbours do not hear each other at all: no reception, no interference, no energy in CCA.
- **Links and their direction.** `link(rx, tx)` is directional: the power at `rx` of `tx`'s frames. The `-g` options (and `--group-file`, one option per line) set it, in order:
  - `-g A,B:RSSI` (exactly two IDs) sets `A → B` (heard by B) to RSSI, and also `B → A`, unless an option `-g B,A:RSSI` gives that direction itself, before or after. A link given once is therefore the same in both directions, as before 2026-11-09, and a reverse option makes it **asymmetric** (different TX powers, noise or antennas at the two ends): `-g 0,1:-70 -g 1,0:-80` is −70 dBm at node 1 and −80 dBm at node 0. Giving both directions with the same value is the same as giving the link once.
  - Any other list, a range (`0-3:RSSI`, even `0-1:RSSI`) or more than two IDs (`1,2,5:RSSI`), sets every pair of its nodes, both directions.
  - Options apply in order and the later one wins. A later option that changes a value an earlier option set (not only as the reverse of a link) is a warning in the server's log; the log also counts the asymmetric links. `-l`/`--dump` prints the map, one row per receiver.
  - Who hears whom (the graph) stays symmetric: a link exists in both directions or in neither. A one-way link is a reverse value so weak that nothing is received (e.g. −150 dBm), or `--vtime-cut`. `--vtime-link` changes both directions.
  - Before 2026-11-09, a second option for the same pair overwrote both directions (`-g 0,1:-70 -g 1,0:-80` gave −80 dBm both ways): only topologies that gave the two directions different values read differently now; none of the emulator's scripts and tests does.
- **Noise floor.** `N = −119.93 dBm`, the constant of today's curve (`kTB` at 293 K and 100 kHz, NF 4 dB: `−153.93 + 30 + 4`).
- **Error model.** Two curves of the same form, `BER = min(0.5, 0.5 · exp(−(x_dB − x0) / b))`:
  - **noise:** the radio's empirical curve, `x0 = 0`, `b = 2`, of the SNR `P − N` (dB): `BER_noise = min(0.5, 0.5 · exp(−0.5 · SNR_dB))`; unchanged since before EM4;
  - **interference:** measured on the FG25 kits (EM5, §2.1), of the SIR `P / I`, `I` the sum of the interferers' powers in mW (each interferer on the next channel 10 dB weaker): mode 3 `x0 = −2.85 dB`, `b = 0.90 dB`.

  Per segment, `BER = max(BER_noise(SNR), BER_int(SIR))` (only the noise curve without an interferer). Without interference this is exactly the radio's formula. With `--medium-model=em4` (the MVP, before EM5), interference is noise: `BER = BER_noise(SINR)`, `SINR = P / (N + I)`, which needs about 20 dB of SIR for 1 % loss on a 50-byte frame, against 5–6 dB on the kits. The counted bits are those of the radio's draw: `8 · (PHR + PSDU + FCS)`.
- **Over a frame.** The counted bits are spread uniformly over `[t_bits, t_end)`: with the measured curve, `t_bits` is the end of the frame's SHR (the kits measured the interferer over the PHR, payload and FCS; interference during the SHR is the business of the capture rules, §3); with `--medium-model=em4`, the frame's start, as before EM5. That range is cut into segments wherever an interferer starts or ends. Each segment `k` holds a share of the counted bits proportional to its duration, `bits_k`, and has its own `SNR` and `SIR_k`. `P_success = Π_k (1 − BER_k)^bits_k`. One uniform draw from the receiver's stream decides; draws are made only when `0 < P_success < 1`. The SINR reported in the verdict and the medium log is the lowest `P / (N + I)` over the segments.
- **Interferers** of a frame at receiver `r`: every other frame whose sender is a graph neighbour of `r`, overlapping in time (end exclusive: a frame ending at `t` does not overlap one starting at `t`), on the same channel at its received power, or on the next channel (channel number ± 1) 10 dB weaker (`--medium-adjacent-db`, §2.1). Frames two or more channels away do not interfere. The same rule gives the energy of the lock threshold (§3) and of CCA (§6): a frame on the next channel adds its power less 10 dB.
- **Airtime** at the medium: `bytes = shr_len + phr_len + len + fcs_len`, `airtime_us = bytes · 8 · 10⁶ / rate_bps` with integer division, from the frame header. This is the sender's own formula (`rf_airtime_us()`), so the sender's TX end and the medium's frame end coincide.
- **Defaults** (all settable): lock threshold `--medium-lock-snr=0` dB (chosen); interference curve, capture margin and windows, break margin and adjacent channel per PHY from the kit measurements (§2.1).

### 2.1 Calibration on the FG25 kits (EM5)

Source: the firmware repo's `docs/COCHANNEL_RESULTS.md` (three EFR32FG25 kits, BRD4271A, RAILtest 2.19.3, EU mode 3 and mode 1a, 2026-11-09; raw data in `~/kit-runs/2026-11-09_cochannel/`). A frame takes the **profile** of its data rate: 50 kbit/s the mode-1a values (also mode 1b), 150 kbit/s the mode-3 values, any other rate `other` (the mode-3 values, not measured). A profile is a set of user settings: `--medium-model` gives the base, and each option below, with an optional PHY prefix (`1a:`, `1b:` (= 1a), `3:`, `other:`; without one, every profile), sets one value over it, in the order given. The server prints the profiles at the start (`medium: PHY 3: ...`).

| Setting (option) | Mode 3 (150 kbit/s) | Mode 1a (50 kbit/s) | Source |
| --- | --- | --- | --- |
| Interference curve `x0`, `b` (`--medium-int-curve=[MODE:]X0,B`, or `noise` for the noise curve of the SINR) | −2.85 dB, 0.90 dB | −2.40 dB, 0.90 dB | §4.1 and §5: a locked 50-byte frame, an interferer over its PHR to FCS, 50 % received at SIR +2.65 dB (1a: +2.9 dB), 99 % at +5.0 dB (1a: +5.3 dB); the fit of the three mode-3 overlaps together is within 0.4 dB at 50 % and 1–2 dB conservative at 99 % |
| Capture margin (`--medium-capture-db=[MODE:]DB`) | 3 dB | 3 dB | §4.2: the stronger frame received 90–100 % from +2.5 to +4 dB; §6 item 2. Mode 1a: same start, the stronger frame wins from +1.3 dB (§5); 3 dB kept |
| Early capture window (`--medium-capture-early-bits=[MODE:]N`, or `shr` for the whole SHR) | 37 bits (246 µs) | 20 bits (400 µs) | §4.2: captured in 100 % of cycles at 0–200 µs, 53–66 % at 250 µs, so the step is at the 50 % point (§6 item 3 rounds it to 200 µs); §5: 66 % at 100–200 µs and 21 % at 400 µs with +5.5 dB, 100 % to 400 µs and 94 % at 600 µs with +11 dB, the step at 400 µs (§6 item 3) |
| Capture during the SFD (`--medium-capture-sfd=[MODE:]on\|off`) | on | off | §4.2: 97–100 % at 650–700 µs (the SFD is on air at 640–747 µs), transitions at 600 and 750 µs; §5: no late window in mode 1a |
| Break margin (`--medium-break-db=[MODE:]DB\|off`) | 2.5 dB | 2.5 dB | §4.2: both frames lost at 300–550 µs with +5, +11 and +21 dB; §6 item 3 gives 2.5 dB as enough to break the sync word. Mode 1a: both lost at 800–1700 µs with +5.5 and +11 dB (raw data) |
| Adjacent channel (`--medium-adjacent-db=[MODE:]DB\|off`) | 10 dB | 10 dB | §4.4: a modulated interferer on the next channel (±200 kHz) acts as a co-channel one about 10 dB weaker; two channels away no effect down to −29.8 dB. Not measured in mode 1a; the mode-3 value |

The profile of the frame being received sets the curve and the capture rules; the profile of the interfering frame sets its adjacent-channel attenuation (its own spectrum leaks into the next channel).

**The emulated radio's preamble** (`app_wshwsim/sl_rf_driver.c`). With the central medium, the Nanostack nodes send the FG25 PHY's preamble: 12 bytes in mode 3 (SHR 747 µs), 8 bytes in the other modes (`COCHANNEL_RESULTS.md` §2, §6 item 4: Silicon Labs' `PHY_WISUN_FAN_1v1_863MHz_Plan33_2FSK_3_EU`; mode 1a sends 8 bytes; other modes keep Wi-SUN FAN's minimum of 8, not measured). `WSSIM_PHY_PREAMBLE` in the nodes' environment fixes it for every mode; the legacy medium keeps 8 bytes, as the permanent reference runs. A client of the radio interface sends the SHR it puts in its frame headers ([RADIO_INTERFACE.md](RADIO_INTERFACE.md)): 14 bytes (12 + SFD) to match the FG25 in mode 3.

**The model before EM5:** `--medium-model=em4` (interference as noise, capture by 6 dB over the whole SHR, no break, no adjacent channel, counted bits over the whole airtime), with `WSSIM_PHY_PREAMBLE=8` in the nodes' environment, gives the MVP's behaviour: a 41-node star with collisions (`medium_gate_star.sh`, hidden, seed 1, 600 s) gives node logs and a medium log (397 220 lines) identical to emulator `da8e09c`.

## 3. Reception at a receiver

Each receiver `r` has, in the server, at most one **lock** (the frame it is receiving). At the start `t` of a frame `F` from `s` on channel `ch`, for each graph neighbour `r` of `s` (except cut links and `--vtime-drop`ped frames, which are neither received nor interference):

1. **Not listening:** `r`'s radio state in shared memory (§5) is not `SIM_RADIO_LISTEN` or `SIM_RADIO_RECEIVE`, or its channel is not `ch`. No lock; `F` still interferes with whatever `r` receives later (it does not, since `r` is not receiving: nothing to do).
2. **Listening, no lock:** `r` locks onto `F` if `P_F − 10·log10(N + I(t))` ≥ lock threshold, `I(t)` being the frames already on air at `r` on `ch`. Otherwise `F` is only interference.
3. **Receiving `A`:** where `r` is in its search for `A`'s sync word decides, with `d = t − t_shr(A)`, `t_shr(A)` the start of `A`'s own SHR (after an `"xl"` preamble), `Tp` its preamble and `Tshr` its SHR airtime, and the profile of `A`'s rate (§2.1):
   - **capture window:** during an `"xl"` preamble (`t < t_shr(A)`), in the early window (`d` < the early bits at `A`'s rate), or, where the profile says so (mode 3), during `A`'s SFD (`Tp ≤ d < Tshr`): if `P_F ≥ P_A + capture margin`, `r` drops `A` (**captured**) and locks onto `F`;
   - **searching** (the rest of the SHR): if `P_F ≥ P_A + break margin`, the receiver loses `A`'s sync word and finds no other: `A` is lost at once (outcome `interference`; its failed verdict, cause interference, is sent at `t`, not at `A`'s end, and `r` listens again), `F` is `busy` and not received. The kits show neither frame received there (`COCHANNEL_RESULTS.md` §4.2: "R reports no sync at all");
   - **synchronised** (`d ≥ Tshr`): no capture.

   Otherwise `F` is interference to `A` (outcome `busy` for `F`). Within the margins, both frames are then usually lost by `A`'s SIR, as measured: two frames within about 1 dB of each other are both lost, and a first frame about 2.5 dB stronger survives. With `--medium-model=em4` the capture window is the whole SHR (`--medium-capture-early-bits=shr`) and there is no break. A lock made by a sniff (§10.3) follows the same rules from its frame's own SHR.
4. **Half duplex:** when `r` itself starts a frame, its lock, if any, ends with no verdict (outcome `tx_abort` in the log). A radio does not start to send while it receives (`_rf_is_busy()`), so this should not occur; it is counted.

A lock also ends when the radio's state in shared memory is no longer `SIM_RADIO_RECEIVE` at the next decision about `r` or at the verdict (the radio turned off: outcome `left`). This is checked only once `r` has run since the frame was delivered: before that, its shared memory still shows its state from before the delivery. A stopped node (`--vtime-kill`) loses its lock the same way; a stopped sender's frame stays on air until its natural end.

Same-time ties: frames that start at the same µs are decided in the order of their senders' wakes (node ID), so the lower ID's frame gets the lock and the other is `busy` (or captures it, by the rule above). At `t_end(A)`, a frame starting at that µs can lock `r` although `r` has not yet seen `A`'s verdict, since its shared memory still says RECEIVE; `r` then gets `A`'s verdict and the new frame in the same wake. Both need exact-µs coincidences and are rare.

When `r` locks onto `F`, the server delivers `F` to `r` at `t` (an `"xx"` message with `SIM_FRAME_MEDIUM`, §4) and queues a **verdict** event for `r` at `t_end(F)`. The verdict is computed when that event is popped, so every interferer that started before `t_end` is known. Same-time order: at `t_end`, the verdict comes before any frame that starts at `t_end` for the same receiver (it was queued earlier, so its sequence number is lower).

## 4. Messages

All in `app_wssimserver/sim_proto.h`. Each is one `SOCK_SEQPACKET` message on the node's conductor socket.

- **Frame** `"xx"` (`struct sim_frame_hdr` + PSDU). New fields, filled by the sender's radio in both modes: `phr_len`, `tx_dbm` (the TX power the MAC asked for), `rate_bps`. The server fills `src` (as today) and, in central mode only, `frame_id` (a run-wide counter from 1), `rx_dbm` (the received power `P`) and `flags = SIM_FRAME_MEDIUM`. In legacy mode `flags`, `frame_id` and `rx_dbm` are 0 and the radio behaves as today.
- **Verdict** `"xv"` (`struct sim_verdict`), central mode only, to the receiver at `t_end`: `frame_id`, `ok`, `cause` (`enum sim_rx_cause`), `rx_dbm`, `sinr_db` (the lowest segment SINR). Not sent for a captured frame (the next `"xx"` replaces it), a `tx_abort`, a `left` or a stopped node: a radio that leaves RX by itself drops the frame it holds and waits for no verdict.
- **Sense** `"xs"` (`struct sim_sense`), central mode only: the radio sends it during its run with its channel; the conductor answers at once with the same struct, `dbm` = `10·log10(N + Σ P)` over the frames on air at the radio on that channel at the current time (the radio's own excluded). The radio blocks on the reply (it is the only message the conductor sends to a running node).
- **Welcome flag** `NSIM_WELCOME_MEDIUM` (stack's `port/sim/ns_port_sim.h`, protocol version 3) and `ns_port_sim_medium()`: the node's radio runs in central mode.

Frames in a node's `"xa"` batch now include verdicts: the count in `"xa"` is the number of messages that follow (frames and verdicts), in queue order.

Preamble sniffing adds three messages, `"xl"`, `"xn"` and `"xd"` (§10.4); the `"xa"` count includes the `"xd"` results.

## 5. Shared memory

For clients of the stack-neutral radio interface ([RADIO_INTERFACE.md](RADIO_INTERFACE.md)), which write no shared memory, the server writes their entry from their "xr" reports. `/wssimcca` grows to 4 bytes per node: driver state (1 byte, as today), channel (2 bytes, as today), then **the radio's hardware state** (`enum sim_radio_state`, what `rf_power_state()` returns). The radio writes all four in `set_shm_cca_state()` in both modes. The server's medium reads byte 3 and the channel; the legacy CCA keeps reading byte 0. `vt_kill` sets byte 0 to `DRVSTATE_OFF` and byte 3 to `SIM_RADIO_OFF`.

## 6. The radio in central mode

Under `NS_PORT_SIM` and `ns_port_sim_medium()` (`app_wshwsim/sl_rf_driver.c`):

- **TX:** unchanged, apart from the new header fields. The sender's own TX end stays on its own timer.
- **`"xx"` with `SIM_FRAME_MEDIUM`:** the server has decided that the radio locks. If the radio is receiving, it drops that frame (captured; counted) and takes the new one. It enters `DRVSTATE_RX` (power state RECEIVE), stores the frame with `rssi = rx_dbm` and its RX timestamp as today, and arms **no** airtime timer (the backup timer stays as a safety net). No loss draw. If its own state says it cannot receive (off, sending, deaf backoff), it drops the frame and counts it as a disagreement with the server (should stay 0).
- **`"xv"`:** if `frame_id` is the frame it holds: `ok` → hand it to the MAC (`phy_rx_cb`) as `_rf_handle_rx_end()` does; not `ok` → drop it (as a CRC failure), back to listening. Either way the radio leaves RX as it does today. A verdict for any other frame is ignored (counted).
- **CCA:** `"xs"` to the conductor; busy when the returned energy ≥ the CCA threshold. The threshold is the one the MAC sets through `PHY_EXTENSION_SET_CCA_THRESHOLD` / `PHY_EXTENSION_SET_CHANNEL_CCA_THRESHOLD` (stored instead of ignored), with a documented default when the MAC sets none. In practice only the second carries a value: the MAC sends it in dBm (`int8_t`) before each CCA, from Nanostack's automatic per-channel threshold (`rcp/mac/ieee802154/mac_cca_threshold.c`, started by `ws_bootstrap.c`): −60 dBm at start (`CCA_DEFAULT_DBM`), lowered to the weakest RSSI received on that channel minus 1 dB, raised by 1 dB after a channel access failure, within −100…−60 dBm. The percentage form (`PHY_EXTENSION_SET_CCA_THRESHOLD`, `macCCAThreshold`) is not used by Wi-SUN and is ignored, as in the EFR32FG25 firmware. **Default** until the MAC sets one: **−85 dBm**, the firmware's `RAIL_PHY_CCA_THRESHOLD_DBM`.
- **Logs:** one line per drop, as today's loss line, with the cause.

## 7. Server options and outputs

- `--medium=legacy|central` (virtual time only; refused without `--vtime`). Default with `--vtime`: `central`.
- `--medium-tx-ref=DBM`, `--medium-lock-snr=DB` (refused with `--medium=legacy`).
- The reception model (§2.1; refused with `--medium=legacy`): `--medium-model=fg25|em4`, and per PHY profile `--medium-int-curve=[MODE:]X0,B|noise`, `--medium-capture-db=[MODE:]DB`, `--medium-capture-early-bits=[MODE:]N|shr`, `--medium-capture-sfd=[MODE:]on|off`, `--medium-break-db=[MODE:]DB|off`, `--medium-adjacent-db=[MODE:]DB|off`.
- Propagation from node positions: `--positions`, `--prop-*` (both media), `--rssi-offset`, `--rssi-noise` (central only); §11.8.
- Preamble sniffing: `--sniff-detect`, `--sniff-dwell-bits`, `--sniff-threshold`, `--sniff-curve`, `--sniff-sfd-bits`, `--sniff-log` (§10.5; refused with `--medium=legacy`).
- `--medium-log=FILE`: one line per (frame, receiver) outcome: `t_end_us src dst frame_id outcome rx_dbm sinr_min_db`, outcome one of `ok`, `noise` (lost, no interferer), `interference` (lost with an interferer), `captured`, `below_lock` (heard, not locked: too weak against the noise and interference), `busy` (heard while receiving another frame and not captured), `not_listening`, `tx_abort`, `left`.
- At the end of the run, a summary line per outcome (totals), the number of sync searches broken by a stronger frame (§3, counted in `interference`), and the CCA query count with the number of queries that found a frame on air. The server does not know the radios' CCA thresholds: the busy counts are in the radios' exit lines (`rf medium: N CCA, N busy`).
- Random: the verdict draws use a stream per receiver, `mix64(seed + mix64(node ID + 1) + K)` with a constant `K` that separates it from the port's and the drift's streams.

## 8. Checks (the EM4 gate)

1. **Legacy is unchanged:** `sim_utils/migration/nonreg_dag_simple.sh` against `reference-runs/dag_simple_b1b9cc5`: identical logs, 10 seeds × 300 s. The LFN suite (`sim_utils/vtime/lfn_suite.sh`) passes.
2. **Server unit tests** with fake nodes (`sim_utils/vtime/test_conductor.py`): two overlapping frames at one receiver (the second is interference, the first lost or kept by its SINR); capture inside and outside the SHR window; hidden terminal (A and C not neighbours, both to B); half duplex; channel mismatch; not listening; verdict before a same-time start; CCA energy from a sender that is a graph neighbour but not in the receiver's MAC neighbour table (which today's CCA misses); without interference, the loss rate at a few RSSI values equals today's curve; determinism by seed. Since the review of the gate: the bits shared over segments (an interferer over a quarter of the frame, at its end and at its start, the loss rate against §2's formula), a lock refused by the interference already on air, a frame on another channel during and before a lock, cut links and `--vtime-drop` left out of interference and CCA energy, capture exactly at the margin and just below, a receiver stopped and restarted while it holds a frame. The radio's side (§6) with real nodes: `sim_utils/vtime/test_medium_radio.sh` (in the LFN suite) checks the medium log against the radios' logs frame by frame, their exit counters, and every CCA busy answer, in a run with collisions and captures. The radio's path without a verdict (`left`) with a real LFN: `sim_utils/vtime/test_lfn_left.sh` (in the LFN suite): a window that closes during a frame waits for it (the MAC defers the close while the radio receives), and the MAC's ACK timeout during an overheard frame turns the radio off, the radio drops the frame, the server logs `left` and the ledger charges RX up to the drop.
3. **Central against legacy, light traffic:** `dag_simple`, 6 seeds × 600 s per mode (`sim_utils/vtime/medium_gate_dag.sh`): the group medians of routers ready and of the join milestones close to legacy, and no significant difference in the pooled milestones (`medium_stats.py`, Mann-Whitney). A single run outside the other group's range is not a failure: with 6 runs per group this happens by chance. `dag_simple` has no data traffic, so delivery is judged in item 4.
4. **Where it matters:** `sim_utils/vtime/medium_gate_star.sh`, a border router and 40 routers in its range, in four groups that do not hear each other (`hidden`) and, as a control, all hearing each other (`clique`), with UDP requests to the border router: interference, captures and busy outcomes appear at the border router, more interference in `hidden` than in `clique`, CCA busy counts (from the radios' exit counters; the server only knows how many queries had a frame on air) higher in `clique`; the effect on delivery and join times is reported against legacy.
5. **LFN suite in central mode:** passes, or each difference is explained.

The gate passed: the central medium is the default with `--vtime`. What compares with stored runs passes `--medium=legacy`: `sim_utils/migration/nonreg_dag_simple.sh` (the permanent reference runs), the legacy groups of `medium_gate_dag.sh` and `medium_gate_star.sh`, and the fake-node tests of the conductor's own frame relay in `test_conductor.py`. The LFN suite runs in each mode with one command: `lfn_suite.sh` in the default (central), `medium_lfn_suite.sh` in legacy, through a wrapper of `wssimserver` that also reaches the tests that start it with options of their own (`test_lfn_mac.sh`, `test_lfn_keyreq.sh`).

Since EM5 (§2.1), `test_conductor.py` also checks the calibrated model: the measured locked-frame points (`medium_calib_sir`: mode 3 at SIR +0.4, +2.65, +3.8, +5.0 and +8 dB, mode 1a at +2.9 and +5.3 dB, each inside the measured band and equal to the formula, and the EM4 model at +5 dB for comparison), the capture windows and margins in modes 3 and 1a, the break and a lock after it (`medium_calib_capture`), the adjacent channel in reception, lock and CCA (`medium_calib_adjacent`, `medium_cca`), and the options (`medium_calib_options`). The tests that encoded the MVP's rules run them with `--medium-model=em4` or were moved to the new values, each change explained in its docstring. `radio_if/test_ref_client.py` has a capture inside the early window and a broken search (`capture`, `capture_break`).

**Effect at network level** (2026-11-09, `--medium-model=em4` with `WSSIM_PHY_PREAMBLE=8` against the default; same seeds):

| Scenario | Runs per model | EM4 model | Calibrated (EM5) |
| --- | --- | --- | --- |
| `dag_simple` (`medium_gate_dag.sh`, 15 nodes, no data traffic) | 6 × 600 s | collisions 2.05 % of the frames heard, interference losses 32 per run; routers ready median 148 s | 1.52 %, 19 per run; 152 s; join milestones not significantly different (Mann-Whitney p = 0.64–0.82), 14/14 routers ready in every run |
| Star, 40 routers in 4 hidden groups (`medium_gate_star.sh`, from 300 s) | 3 × 900 s | at the border router 56 % of the frames heard received, 628 interference losses per run, collisions 42 %; requests delivered 99.82 % (4 of 2280 lost); routers ready median 125 s | 64 %, 338, 34 %; 99.65 % (8 of 2280; one run lost 8, the others none: not significant); 112 s |
| Star, 40 routers all hearing each other (clique) | 3 × 900 s | collisions 0.58 %, delivered 100 % | 0.77 %, 99.96 % (1 of 2280); CCA finds a frame on air in 12.7 % of the queries (10 %): adjacent channels and the longer mode-3 preamble |
| Building, 1000 interface clients, light load (`docs/SCALE.md`, 100 kbit/s: the `other` profile) | 2 × 3600 s | 55 % of the frames heard received, 539 k interference losses; every beacon and alarm flood reached every node, beacon latency median 3 s, max 7 s | 58 %, 388 k (−28 %), 22 k broken searches among them; floods unchanged (every node, same latencies) |
| Building, heavy load (beacon every 10 s, 20 alarms) | 2 × 180 s | 23.5–24.0 % received, collisions 62 %; one alarm flood reached 93.2 % of the nodes (seed 2), all others 100 %; beacon latency max 24–30 s | 28.0–28.8 %, 57 %; every flood 100 %; beacon latency max 19–20 s, median 6–8 s (8–9 s) |

Most remaining collision losses are `busy` frames (a receiver holds one frame at a time), which the calibration does not change. Wall time is unchanged.

## 9. Not in the MVP

Fading and time-varying shadowing, bursts of losses, external interferers, RX↔TX turnaround and settling times (the kits measured RAIL's RX→TX 130 µs and TX→RX 100 µs, `COCHANNEL_RESULTS.md` §4.5; not applied) (Investigation §5, P4–P11 and the layered loss model). Node positions with path-loss models, static shadowing and a reported RSSI with its own error are in §11. The radio still decides its own TX timing; the medium does not delay or refuse a transmission. A frame that will be lost keeps its receiver in RX for its whole airtime, as on a real radio (legacy dropped it at its start), so central runs show more RX time.

### 9.1 Limits of the calibration

The collision figures of the MVP were upper bounds (interference treated as noise, 9–16 dB pessimistic for a locked frame); since EM5 the defaults follow the kit measurements (§2.1). What they do not cover:

- **No fading.** The kits stood a few tens of centimetres apart on a desk; links keep fixed values (§9).
- **Nothing near sensitivity.** The wanted frames were 70 dB above sensitivity (−38 … −14 dBm). Near sensitivity, noise and interference add; the medium takes the larger of the two error rates, which is optimistic there by up to a few dB: the datasheet's co-channel rejection, measured 3 dB above sensitivity with a CW interferer, needs the wanted signal 4.4 dB (mode 3) to 7.1 dB (mode 1a) above the interferer for 10 % packet loss, against 3.8 dB here for 10 % loss (mode 3, a modulated interferer, `COCHANNEL_RESULTS.md` §4.1, §4.3).
- **Transitions are coarse steps.** Capture and break are deterministic at their margins, whereas the kits show probabilistic transitions: with 0–3 dB of margin the stronger frame was still received in 58–95 % of cycles (0 and 100 µs, §4.2), which the medium counts as both lost. The capture timing was scanned at three margins in mode 3 and two in mode 1a; the transition points (mode 3: 250, 600, 750 µs) are placed at their 50 % points or at the SFD's edges.
- **Not measured, values carried over:** mode 1a's capture margin, break margin and adjacent channel (mode-3 values); every other PHY (the `other` profile, the mode-3 values, including the early window of 37 bits whatever the preamble length); long `"xl"` preambles (the whole long preamble is a capture window, as a receiver that restarts its search would take a stronger frame; not measured); frames of other lengths than 50 and 250 bytes.
- **The interference curve** fits the 50-byte, 250-byte and partial-overlap cases with one constant pair, within 0.4 dB at 50 % and 1–2 dB conservative at 99 % (§4.1). The SIR axis of the measurement is accurate to about 1 dB.
- **Preamble sniffing** (§10) keeps the noise curve of the SINR for detection, and frames on the next channel take no part in it: the kits have not measured preamble detection yet.
- RAILtest only: the firmware's MAC (CCA, ACK timing) was not in the loop.

## 10. Preamble sniffing

For sniff MACs (low-power listening, preamble sampling): a node wakes for a short window on a period, its receiver looks for a preamble on one channel, and it stays in RX only if it found one. Senders put a wake-up preamble longer than the receivers' period before their frame. The medium decides each detection from the frames on air, and a receiver that detects a long preamble can then receive the frame that follows. Central medium only (§1); the Nanostack nodes do not use it.

### 10.1 The primitive

A radio sends `"xn"` (`struct sim_sniff`) during its run at time `t`: channel `C`, duration `d`, a tag, flags, an end state. Its receiver is on, on `C`, from `t_on` to `t1 = t + d`: `t_on = t`, or the end of the node's own frame if one is on air (half duplex, §10.3). It detects over the **window** `[t0, t1)`, `t0 = t_on + --sniff-settle-us` (0 by default), at most `t1`. The conductor answers with one `"xd"` (`struct sim_sniff_result`), not at once but in a later wake of the node:

- at the **detection time** `td`, the end of the first dwell that detects a frame (§10.2): `detected = 1`, the frame's `frame_id` and sender, `t_us = td`, `rssi_dbm` = the frame's received power `P`, `sinr_db` = the lowest SINR over that dwell;
- or at `t1` if nothing was detected: `detected = 0`, `t_us = t1`, `rssi_dbm` = the energy on `C` at the node at `t1` (noise plus the frames on air then that it hears, as `"xs"` would answer; on an empty channel, the noise floor −119.93 dBm).

A new `"xn"` while a sniff is in progress replaces it (no `"xd"` for the old one). A frame sent by the node ends its sniff, with no `"xd"` (half duplex, §10.3). A stopped node's sniff ends with it. For a client of the stack-neutral radio interface, whose radio state the conductor knows from its reports, a report that leaves the sniff (`OFF`, `IDLE`, `TX` from another state, or a receiver on another channel) also ends it with no `"xd"` (outcome `left`), and an `"xn"` sent while it holds a frame loses that frame (outcome `left`) before the sniff starts ([RADIO_INTERFACE.md](RADIO_INTERFACE.md) §6.7).

### 10.2 Detection rules

The candidates are the frames on `C` whose sender is a graph neighbour of the node, not cut and not dropped for it (as for reception, §3), other than the node's own; they are also each other's interferers, over their whole airtime. For each candidate `F`, received at power `P_F`:

- **Detectable part.** `--sniff-detect=preamble` (default): its preamble, `[t_start, pre_end)`, `pre_end` = the end of its SHR minus the SFD (`--sniff-sfd-bits`, 16 bits, at the frame's rate), including an `"xl"` preamble. `--sniff-detect=frame`: the whole frame (a carrier or energy detector that also catches a frame in progress).
- **Dwells.** A detection try observes `--sniff-dwell-bits` bits (16) of the frame, `D_F = ⌈bits · 10⁶ / rate_F⌉` µs (320 µs at 50 kbit/s, 107 µs at 150 kbit/s). The tries are consecutive dwells `[a + k·D_F, a + (k+1)·D_F)` from `a = max(t0, t_start(F))`, each ending at or before `min(t1, end of the detectable part)`. A frame that overlaps the window by less than one dwell is never detected; a long preamble that covers the whole window gets `⌊(t1 − t0) / D_F⌋` tries.
- **Probability of one try.** 0 if `P_F` is below `--sniff-threshold` (off by default); otherwise `curve(SINR_min)`, `SINR_min` the lowest SINR of `F` over the dwell, `P_F / (N + I)` with `I` the sum of the other candidates on air (it is lowest at the dwell's start or where an interferer starts). Default curve `ber:16`: `(1 − BER(SINR))^16`, `BER` the medium's noise curve (§2) of the SINR, whatever the reception model (§9.1), that is, 16 preamble bits received without an error. A table `--sniff-curve=SINR:P,SINR:P,...` (dB, ascending) replaces it: linear between the points, the end values beyond them.
- **Draws.** Each try with `0 < p < 1` takes one uniform draw from the node's detection stream, `mix64(seed + mix64(node ID + 1) + K')`, separate from the verdict streams; `p = 0` or `1` takes none. The tries are decided in the order of their dwell ends (then frame start, then frame ID), each when the conductor reaches its end, so every interferer that started before it is known. The first try that succeeds is the detection; the rest are not drawn.
- **Busy receiver.** A receiver locked onto a frame (at that frame's start during the window, §10.3) is synchronised on it and detects no other frame: the tries of other frames whose dwell overlaps that reception have probability 0 (no draw). With the default lock threshold this only matters when the locked frame is on another channel or with `--medium-lock-snr` below 0, since otherwise the locked frame's power keeps the other frames' SINR low.

Values of the default curve without interference (`P = N + SINR`, `N` = −119.93 dBm):

| SINR (dB) | 3 | 4.9 | 6 | 8.7 | 10 | 13.4 | 20 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Received power (dBm) | −116.9 | −115.0 | −113.9 | −111.3 | −109.9 | −106.5 | −99.9 |
| One try | 0.15 | 0.50 | 0.67 | 0.90 | 0.95 | 0.99 | 1.00 |
| Four tries (an 8-byte preamble wholly inside the window) | 0.48 | 0.94 | 0.99 | 1.00 | 1.00 | 1.00 | 1.00 |

### 10.3 Receiving after a detection

**The lock rule (§3) extended.** Until now a receiver could only lock onto a frame at the frame's start; a frame whose preamble started before the receiver was on was never received, however much preamble remained. Real receivers acquire such a frame if the preamble lasts long enough to synchronise and the SFD is still ahead. The rule here: with `SIM_SNIFF_LOCK` in the request, a detection of `F` at `td` **locks the receiver onto `F`** if

1. the SFD is still ahead: `td ≤ pre_end(F)` (always true with `--sniff-detect=preamble`, whose dwells lie in the preamble; with `frame`, a detection in the payload does not lock);
2. the receiver holds no lock (else `locked = 2` if it already receives `F`, 0 if another frame). A detection of a frame the receiver already receives is reported with `locked = 2` without `SIM_SNIFF_LOCK` too;
3. `F` passes the lock threshold at `td` against the frames then on air (§3, rule 2).

The `"xd"` then has `locked = 1` and is followed, in the same wake, by `F`'s `"xx"` (`SIM_FRAME_MEDIUM`, its `frame_id`, `rx_dbm` = `P_F`, and `SIM_FRAME_LONG_PREAMBLE` if it came with `"xl"`); the verdict `"xv"` comes at `F`'s end as for any lock, and capture by a stronger frame during the rest of the SHR applies. The bits counted in the verdict (§2) are spread over `[max(td, t_bits), t_end)`, `t_bits` being the frame's start or the end of its `"xl"` preamble: interference during the preamble before the lock does not count, the detection rule covers it.

Without `SIM_SNIFF_LOCK`, the detection is reported only; the client decides (a new sniff with the flag at that time detects again after one dwell and locks if the preamble lasts). A radio that turns its receiver on without a sniff still locks only at a frame's start (§3 unchanged): `"xn"` is how a client says "my receiver is on now, acquire a frame already on air".

**During the window the radio is in RX.** It writes LISTEN and the channel in its shared-memory entry (§5) as any radio (for a client of the stack-neutral radio interface, which writes no shared memory, the conductor writes the states of the sniff for it: LISTEN at `t_on`, the end state at `t1`, RECEIVE on a lock; [RADIO_INTERFACE.md](RADIO_INTERFACE.md) §6.7), so a frame that **starts** during the window locks it at its start by §3; its sniff then reports that frame with `locked = 2` when its dwell detects it.

**Half duplex.** A sniff asked while the node's own frame is on air starts when that frame ends (`t_on`; `t0` in the sniff log adds the settling time). A frame the node sends during the window ends the sniff (outcome `tx_abort`, no `"xd"`).

### 10.4 Messages

All in `app_wssimserver/sim_proto.h`, central mode only (the legacy medium drops them with a warning). New message types; no existing message changed. Clients of the radio interface use them from its version 2, which the welcome announces with `SIM_WELCOME_SNIFF` ([RADIO_INTERFACE.md](RADIO_INTERFACE.md) §6.7, §13); a version-1 client's `"xn"` and `"xl"` are dropped with a warning and counted.

| Message | From | Struct | Content |
| --- | --- | --- | --- |
| `"xl"` | a node, during its run | `struct sim_lp_frame` + a complete `"xx"` | `preamble_us`: preamble on air before the frame's own SHR. The frame is on air for `preamble_us` + its airtime (§2); its capture window is `preamble_us` + its SHR. The sender's TX end is the client's own. |
| `"xn"` | a node, during its run | `struct sim_sniff` (16 bytes) | `channel`, `duration_us`, `sniff_id` (echoed), `flags` (`SIM_SNIFF_LOCK`), `end_state` (`enum sim_radio_state` after a window with nothing detected, for the ledger: OFF, IDLE or LISTEN; any other value means OFF) |
| `"xd"` | conductor, in the node's wake at `t_us` | `struct sim_sniff_result` (32 bytes) | `detected`, `locked` (0, `SIM_SNIFF_LOCKED` = 1, `SIM_SNIFF_RECEIVING` = 2), `sniff_id`, `frame_id`, `src`, `t_us`, `rssi_dbm`, `sinr_db` |

`"xd"` and the `"xx"` of a lock it made count in the `"xa"` count, in queue order. A sniff with nothing on the channel costs the node one wake, at `t1`.

**Power ledger** (with `--power`, [POWER_MODEL.md](POWER_MODEL.md)): the conductor records the states the sniff implies, so a client that sends no `"xr"` is still charged: LISTEN on `C` from `t_on` (the request, or the end of the node's own frame: its TX time is kept; the settling time counts as listening; nothing if its own frame fills the window); at `t1` with nothing detected, `end_state` (unless the receiver holds a lock, then it is receiving, or its own frame is still on air, then it is sending and reports its next state at that frame's end); RECEIVE from a lock the sniff made. Every other change is the client's to report with `"xr"`: after a detection without a lock the radio stays in LISTEN until the client reports otherwise, and after the verdict as for any frame. The ledger ignores a report of the state it already has, so a client that also reports these states is not charged twice. The sender of an `"xl"` reports its TX state with `"xr"` as for any frame and keeps it for `preamble_us` plus the frame's airtime: the conductor records nothing for it, so the long preamble's charge is the client's TX report.

### 10.5 Settings and defaults

All are user settings. The FG25 datasheet gives no preamble-detection figure (no detection time, probability or threshold; root repo `documents/pre-study/datasheets/efr32fg25-datasheet.pdf`, Rev. 1.2, §4.9.5); the defaults below are therefore chosen to be consistent with the medium's own error model and the FG25 radio's documented behaviour, and are to be replaced by kit measurements (§10.6).

| Option | Default | Source or reason |
| --- | --- | --- |
| `--sniff-detect=preamble\|frame` | `preamble` | RAIL's RX duty-cycle "preamble sense" mode: the radio stays in RX if it has a preamble at the end of the sense time (`RAIL_RX_CHANNEL_HOPPING_MODE_PREAMBLE_SENSE`, `rail_types.h` of the Simplicity SDK 2026.6.2) |
| `--sniff-dwell-bits=N` | 16 | Assumption: a quarter of the Wi-SUN FSK preamble the emulated radio sends (8 bytes, `WSSIM_PHY_PREAMBLE`); no datasheet value |
| `--sniff-threshold=DBM\|off` | `off` | RAIL applies an RSSI threshold in duty-cycle mode only if `RAIL_RX_CHANNEL_HOPPING_OPTION_RSSI_THRESHOLD` is set (`rssiThresholdDbm`, same header); off by default. The medium compares it with the candidate frame's own received power, not with the total energy on the channel the radio measures; they differ only with interferers, whose SINR then already lowers the try's probability |
| `--sniff-curve=ber:N\|SINR:P,...` | `ber:16` | The medium's BER curve (§2) over 16 bits: 50 % at −115.0 dBm, 90 % at −111.3 dBm per try without interference. For comparison, FG25 packet sensitivity (10 % PER, 250 octets) in the 866 MHz band: −110.8 dBm in mode 1a, −108.8 dBm in mode 3 with FEC (datasheet Table 4.30, p. 92); a preamble detector is expected to work at or below packet sensitivity |
| `--sniff-sfd-bits=N` | 16 | IEEE 802.15.4 SUN FSK SFD of 2 octets, as the emulated radio sends (`WSSIM_PHY_SFD` = 2 bytes) |
| `--sniff-settle-us=N` | 0 | Receiver settling (RX warm-up, AGC, timing recovery) before detection can start, after the request or after the node's own frame. 0 keeps the medium's exact timing (§9); no datasheet value is used, to be set from kit measurements (§10.6) |
| `--sniff-log=FILE` | none | One line per sniff: `t_req node sniff_id chan t0 t1 outcome t frame_id src rssi_dbm sinr_db locked`, outcome one of `detected`, `none`, `tx_abort`, `replaced`, `stopped`, `left` (a client of the radio interface reported a state that left the sniff) |

The run's end prints the number of sniffs per outcome, the locks made by a sniff and the detection draws.

### 10.6 Limits and calibration

- **What the kits should measure (EM5, FA2):** the detection probability against the input level, with no interferer, for one sense window of a given length and for each PHY mode used (the table form of `--sniff-curve`, converted from power to SINR by adding 119.93 dB, and `--sniff-dwell-bits` from the shortest window that detects at a strong level); the same with a co-channel FSK interferer at a few U/D ratios (the SINR axis); the time from the window's start to the detection event, against how much preamble is left; how much preamble must remain after detection for the frame to be received (the SFD rule). Until then detection figures are model values.
- Detection is per frame: interferers only lower a frame's SINR. A detector that triggers on the summed energy of several frames (for example on a collision whose frames are each too weak) is not modelled.
- The settling time (`--sniff-settle-us`) is 0 by default, as elsewhere in the medium (§9). A frame that starts during the settling time still locks the receiver at its start by §3 if LISTEN is in its shared-memory entry (the radio writes it, or the conductor for a client of the radio interface).
- No false detection: a window with no frame on air never detects (the curve at very low SINR gives `2^-16` per try with `ber:16`, and only for a frame on air). Real preamble detectors trigger falsely on noise now and then, which keeps the receiver on until the client's timeout; that charge is not modelled.
- **Energy carrier sense** is approximated, not modelled: `--sniff-detect=frame` with `--sniff-threshold` and a table curve detects a frame from its own power, while an energy detector compares the total energy on the channel with its threshold.
- **Several channels in sequence:** one `"xn"` per channel, each answered by its own `"xd"` in a wake of the node, so a scan of `n` channels costs `n` wakes in the ledger; a radio that hops between sense windows in hardware (RAIL's RX channel hopping) wakes the MCU only on a detection.
- A node that starts (or restarts) while frames are on air does not hear those frames (as for reception: the receivers of a frame are fixed at its start), so its first sniff cannot detect them.
- The legacy medium drops `"xn"` and `"xl"` with a warning: a client waiting for an `"xd"` gets none.
- Frames on the next channel neither detect nor lower a candidate's SINR (the adjacent-channel rule of §2 applies to reception, locks and CCA only).
- Sniffing works in virtual time with the central medium only.

### 10.7 Checks

`sim_utils/vtime/test_sniff.py` (fake nodes, in the LFN suite as `sniff`): detection of a frame on air at the expected time and power; an empty channel (noise floor, one wake); the threshold; partial overlap at both edges of the window, with both detection modes; an interferer (SINR against the formula, no detection at equal power); the wrong channel; half duplex (deaf during its own frame, a frame sent during the window ends the sniff); one-dwell detection rate at the margin against the curve, and determinism by seed; a table curve (no draws at 0 and 1); a 100 ms `"xl"` preamble detected and locked onto mid-preamble, its `"xx"` and verdict, against a radio that only listened from the start, one that turned on late without a sniff, and a sniff without the lock flag; the lock rules (no lock after the SFD, an existing lock reported, bits counted from the lock, the lock threshold against the frames still on air only, no detection of another frame while locked); the ledger (listening during the window, receiving after the lock, the TX time kept when the sniff is asked during the node's own frame); the settling time; many frames joining a window (one chain of check events); option errors.

## 11. Propagation from node positions

The links of section 2 can come from node positions and a path-loss model instead of `-g` values. The server computes, once at the start, the mean received power of every pair of positioned nodes in each direction, with a static shadowing term, and derives from it who hears whom. The time-varying part of a link (fading, slow shadowing, bursts of losses) is a separate layer that adds to this mean; external interferers are another. The option is disabled by default: without `--positions`, every output is unchanged.

The model lives in `app_wssimserver/sim_propagation.c`. The medium asks it for one value per frame and receiver, `prop_link_dbm(rx, tx, tx_dbm)`, which is a table lookup and one addition; there is no per-frame search.

### 11.1 Positions file

`--positions=FILE`: one node per line, `ID X Y Z [FLOOR [GAIN_DBI]]`, coordinates in metres. Blank lines and lines starting with `#` are skipped. `FLOOR` is an integer; without it, the floor is `Z / --prop-floor-height` rounded (3.5 m). `GAIN_DBI` is the node's antenna gain, applied in transmission and in reception; without it, `--prop-gain` (0 dBi). A node ID may appear once. Nodes that are not in the file keep the `-g` links only.

`radio_if/scale/gen_building.py --positions FILE` writes this format (`ID X Y Z FLOOR` after a `#` header), so a generated building can be used directly: `gen_building.py --nodes 200 -o /dev/null --positions b.pos`.

### 11.2 Received power

For a frame that `tx` sends at `tx_dbm` (from the frame header, as with `--medium-tx-ref`), the mean power at `rx` is

    P(rx ← tx) = tx_dbm + G_tx + G_rx − PL(tx, rx) − X(rx, tx)

with `G` the antenna gains (§11.1), `PL` the path loss of the model (§11.3) and `X` the static shadowing (§11.4). At the start, the server stores `P` at the nominal TX power `--prop-tx-dbm` (14 dBm) in its RSSI map, and the medium adds `tx_dbm − 14` to it for each frame. The legacy medium and wall-clock runs use the map as it is: their radios do not apply the sender's TX power, so `--prop-tx-dbm` should be the TX power the nodes use.

### 11.3 Path-loss models

`d` is the 3-D distance between the two nodes, at least `--prop-min-dist` (1 m); `d_h` the horizontal distance; `k` the number of floors between them; `f` the frequency in MHz (`--prop-freq`, 915).

    PL = PL0 + 10 · n · log10(d) + w · d_h + L_wall · walls + L_floor(k)

| Model (`--prop-model`) | `PL0` (`--prop-pl0`) | `n` (`--prop-exp`) | `L_floor(k)` (`--prop-floor-db`) | Source |
| --- | --- | --- | --- | --- |
| `indoor` (default) | `20·log10(f) − 28` (31.2 dB at 915 MHz, 30.8 dB at 868 MHz) | 3.3 | 9, 19, 24 dB for 1, 2, 3 floors; 5 dB more per further floor | ITU-R P.1238 site-general indoor model, office, 900 MHz: `L = 20·log10(f) + N·log10(d) + Lf(n) − 28` with `N` = 33 and `Lf` = 9, 19, 24 dB (versions 0 to 3, 1997–2003). `N` already allows for the walls within a floor. Beyond 3 floors the Recommendation gives no value; the last step is repeated. |
| `logdist` | free space at 1 m: `20·log10(f) − 27.55` (31.7 dB at 915 MHz) | 3.0 | none | Log-distance model; the exponent is a chosen value for indoor commercial buildings (P.1238 gives 3.3 for offices and 2.0 for commercial areas at 900 MHz) |
| `freespace` | free space at 1 m | 2.0 | none | Free-space path loss, `20·log10(d) + 20·log10(f) − 27.55` |

Every term is a user setting, for every model:

- `--prop-floor-db=L1[,L2,...]`: the cumulative loss for 1, 2, ... floors apart, not decreasing; beyond the list, its last step is repeated. One value is a loss per floor (`--prop-floor-db=18`: 18 dB per floor). Seidel and Rappaport measured 12.9 and 16.2 dB for one floor at 914 MHz in two office buildings ("914 MHz path loss prediction models for indoor wireless communications in multifloored buildings", IEEE Trans. Antennas Propag., 1992).
- `--prop-wall-db-per-m=W` (0): a wall loss proportional to the horizontal distance, for a model whose exponent does not include the walls.
- `--prop-walls=SX[,SY]:DB` (none): a regular grid of walls, every `SX` metres across `x` and every `SY` metres across `y` (`SY` = `SX` if omitted), at `(k + 1/2)·S`, so that nodes on a grid of the same spacing stand in the middle of their rooms; each wall crossed adds `DB`. The same study measured 1.4 dB per cloth-covered office partition and 2.4 dB per concrete wall at 914 MHz.

The model of `gen_building.py` (`docs/SCALE.md` §2) is `--prop-model=logdist --prop-pl0=31.7 --prop-exp=3 --prop-wall-db-per-m=1.8 --prop-floor-db=18 --prop-shadow=0`: with `--prop-cutoff=-100 --prop-margin=0`, the server gives the same links as the generator's topology file (`test_propagation.py`, `gen_building`).

**Choosing the model.** The default is site-general and has no wall term beyond its exponent: in large open floors it predicts long ranges. With the defaults and 14 dBm, the two farthest nodes of a 152 m × 72 m floor of `gen_building.py` receive each other at about −91 dBm before shadowing, and a 4000-node building of 20 such floors has about 2890 neighbours per node on average in the hearing graph. Buildings with many solid walls are closer to the wall grid or the per-metre term: with `--prop-walls=8:2.4`, the same building has about 1730 neighbours per node, and with the `gen_building.py` model, 162. The parameters should be set from measurements in the target building when they exist.

### 11.4 Static shadowing

`X(rx, tx)` is a normal variable with mean 0, drawn once per run:

    X(rx, tx) = σ_link · z(pair) + σ_dir · z(rx, tx) + σ_node · (z(rx) + z(tx))

- `σ_link`, `--prop-shadow` (4 dB): shared by both directions, which keeps the link reciprocal, as the propagation channel is.
- `σ_dir`, `--prop-shadow-dir` (0 dB): drawn per direction; it makes links asymmetric (the two directions differ with a standard deviation of `√2·σ_dir`). On real hardware, most asymmetry comes from the TX power and receiver sensitivity of each device rather than from the channel.
- `σ_node`, `--prop-shadow-node` (0 dB): one draw per node, in all its links in both directions: a simple spatial correlation (a node in a poor location is poor with every neighbour). Two links that share a node have the correlation `σ_node² / (σ_link² + σ_dir² + 2·σ_node²)`.

The total standard deviation is `√(σ_link² + σ_dir² + 2·σ_node²)`. Each `z` is a standard normal draw from a hash of the run's seed (`WSSIM_SEED`, 1 by default) and the pair or the node, so the values do not depend on the order of computation, the same seed gives the same links, and another seed gives other links. The default of 4 dB is our choice for the static part only: published standard deviations around indoor path-loss models (several dB to more than 10 dB) also include variations that the time-varying layer models. The value should be fitted from measured links.

### 11.5 Hearing graph and `-g` options

A pair of positioned nodes hears each other when the stronger of its two mean levels at `--prop-tx-dbm`, plus `--prop-margin` (10 dB), reaches `--prop-cutoff` (−125 dBm). The graph stays symmetric (§2). Pairs below that level are neither received nor interferers, nor counted in CCA energy.

- The cutoff is 5 dB below the noise floor (−119.93 dBm, §2): one interferer at −125 dBm raises the noise by 1.2 dB, and a weaker one by less. Weak far nodes therefore still interfere, while a building larger than the model's range stays sparse.
- The margin leaves room for the time-varying layer: a link whose mean is 10 dB below the cutoff is kept, because fading can raise it above the cutoff for short periods.
- The graph uses the nominal TX power. A node that sends at a higher power than `--prop-tx-dbm` reaches farther than its graph: set `--prop-tx-dbm` to the highest TX power in use.

**With `-g` options:**

- A `-g` option with a value (`A,B:RSSI`, a range or list with `:RSSI`, `--group-file`) sets the link of those pairs as in §2, for the directions it gives; the model does not change them, and `--medium-tx-ref` applies to them only. The pairs are in the graph whatever their model level.
- A `-g` option without a value adds its pairs to the graph; a pair of positioned nodes then gets the model's value (a pair below the cutoff is added with its weak level), and any other pair stays a perfect link (0 dBm), as before.
- `--vtime-link` sets both directions of a pair to its value from its time on, in place of the model's.
- Without `-g`, the graph is the model's alone (without `--positions`, the absence of `-g` means that every node hears every other one, as before).

`run.sh` takes the node count from its `TOPO` file: for a building, a `TOPO` file with one line `0,N-1` (a link without value between the first and the last node) gives N nodes, and the links come from `--positions` in `SIM_OPTS` (an absolute path).

### 11.6 Reported RSSI

The RSSI that a real receiver reports has its own measurement error, which matters because the stack uses it to select parents. Two optional terms, central medium only, apply to the value the server delivers (the `rx_dbm` of `"xx"` and `"xv"`, and the `rssi_dbm` of a detection in `"xd"`), never to the power the medium uses for its decisions or writes in `--medium-log`:

- `--rssi-offset=σ` (0 dB): a fixed offset per receiver, a normal draw from the seed and the node (a calibration error);
- `--rssi-noise=σ` (0 dB): a normal draw per frame and receiver, from the seed, the node and the frame ID (the same value in the frame's `"xx"` and its verdict).

The FG25 datasheet (Rev. 1.2, Tables 4.23 and 4.24) gives an RSSI resolution of 0.25 dB and a maximum absolute error, under typical conditions and with offset correction, of 1.2 dB in the 915 MHz band and 1.8 dB in the 868 MHz band, from −100 to −30 dBm. A standard deviation of half the maximum puts 95 % of the receivers within it: `--rssi-offset=0.6` (915 MHz) or `0.9` (868 MHz). The energy that CCA measures (`"xs"`) keeps the true value.

### 11.7 Outputs

- At the start, the server's log gives the model and its parameters, the shadowing, and the graph: the number of pairs that hear each other, the degree (mean, minimum, maximum), the pairs at −100 dBm or more, the asymmetric pairs, the directions set by `-g`, and the time the computation took.
- `--prop-dump=FILE`: one line per direction of every pair in the graph among the positioned nodes: `rx tx dist_m floors walls pl_db shadow_db gains_db mean_dbm heard given`, `mean_dbm` at `--prop-tx-dbm`, `heard` 1 if the model puts the pair in the graph, `given` 1 if a `-g` value sets that direction (the medium then uses the `-g` value, not `mean_dbm`).
- `-l`/`--dump` prints the full map as before; for large buildings, `--prop-dump` is more practical.

### 11.8 Options

| Option | Default | Meaning |
| --- | --- | --- |
| `--positions=FILE` | none | positions (§11.1); enables the model |
| `--prop-model=indoor\|logdist\|freespace` | `indoor` | path-loss model (§11.3) |
| `--prop-freq=MHZ` | 915 | frequency |
| `--prop-pl0=DB`, `--prop-exp=N` | per model | path loss at 1 m, distance exponent |
| `--prop-min-dist=M` | 1 | shortest distance in the formula |
| `--prop-wall-db-per-m=DB` | 0 | wall loss per horizontal metre |
| `--prop-walls=SX[,SY]:DB` | none | wall grid |
| `--prop-floor-db=L1[,L2,...]` | indoor: 9,19,24; others: none | floor loss |
| `--prop-floor-height=M` | 3.5 | floor of a node without a `FLOOR` column |
| `--prop-shadow=DB`, `--prop-shadow-dir=DB`, `--prop-shadow-node=DB` | 4, 0, 0 | shadowing (§11.4) |
| `--prop-gain=DBI` | 0 | antenna gain of a node without a `GAIN` column |
| `--prop-tx-dbm=DBM` | 14 | nominal TX power: the RSSI map and the graph |
| `--prop-cutoff=DBM`, `--prop-margin=DB` | −125, 10 | hearing graph (§11.5) |
| `--prop-dump=FILE` | none | the computed links (§11.7) |
| `--rssi-offset=DB`, `--rssi-noise=DB` | 0, 0 | reported RSSI error (§11.6), central medium only |

The `--prop-*` options need `--positions`.

### 11.9 Checks

`sim_utils/vtime/test_propagation.py <wssimserver>` (fake nodes and `--prop-dump`): option errors; each model against hand computation (floors from the column and from `Z`, the floor table and its extrapolation, the wall grid, the per-metre walls, the minimum distance, the gains, the nominal TX power); shadowing reproducible by seed and different with another seed, its standard deviation over 44 850 pairs, reciprocal by default, the per-direction and per-node parts and their correlation; the graph cutoff and margin, in the dump and in the medium (no lock and no log line beyond the cutoff); a `-g` link without value below the cutoff, with the model's level; the frame's TX power applied to a modelled link, a `-g` value overriding it with `--medium-tx-ref`, `--vtime-link` replacing it; the `gen_building.py` model reproduced link for link; a 4000-node building built in 0.53 s; the reported RSSI (offset fixed per receiver, noise per frame with its standard deviation, the same value in `"xx"` and `"xv"`, the true power in the medium log, reproducible by seed).

Without the new options, the outputs are unchanged: `nonreg_dag_simple.sh` against the permanent reference runs, `medium_gate_star.sh` (seed 1, hidden, central) with node logs and medium log identical to a build without the model, `test_conductor.py`, `test_sniff.py`, `radio_if/test_ref_client.py` and `lfn_suite.sh quick` pass.

**Network-level demonstration** (`sim_utils/vtime/prop_building.sh`, defaults): a border router and 149 FAN 1.0 routers on 3 floors of a generated building (`gen_building.py`, 5 × 10 nodes per floor, 10 m apart, the border router in the middle of floor 0), the default model and shadowing, the routers' default TX power of 20 dBm (`--prop-tx-dbm=20`), no `-g` link, central medium, 3 seeds × 1200 virtual seconds. 111 s of wall time for the three runs at once.

| Figure | Seed 1 | Seed 2 | Seed 3 |
| --- | --- | --- | --- |
| Pairs in the hearing graph (of 11 175) | 11 175 | 11 175 | 11 175 |
| Link level at 20 dBm, 5th / 50th / 95th percentile (dBm) | −89.2 / −70.5 / −49.6 | −89.2 / −70.6 / −49.8 | −89.5 / −70.7 / −49.5 |
| Links at −80 dBm or more | 78.5 % | 78.8 % | 78.9 % |
| Shadowing, measured standard deviation (dB) | 4.03 | 4.02 | 4.01 |
| Routers ready at the end | 149 / 149 | 149 / 149 | 149 / 149 |
| Median / last router ready (s) | 210 / 476 | 205 / 478 | 208 / 464 |
| RPL depth at the end: 1 / 2 / 3 hops | 36 / 98 / 15 | 40 / 94 / 15 | 38 / 95 / 16 |
| Frames heard received / collision share | 98.87 % / 0.88 % | 99.03 % / 0.77 % | 99.06 % / 0.72 % |

With the default model, every node of this building hears every other one, and most links are strong; the network still forms a tree of up to 3 hops, because parent selection prefers the stronger links. The same command repeated gives the same figures.

### 11.10 Limits

- **Walls are not geometric.** The model knows no floor plan: the walls are an average (the exponent, a loss per metre) or a regular grid. Paths through windows, stairwells or around corners, and corridor guiding, are not modelled.
- **One site-general model per run.** Every pair uses the same parameters; a building with mixed construction needs the parameters of its dominant part, or `-g` values for the pairs that differ.
- **Static mean.** The values are fixed for the run (apart from `--vtime-link`); time variation is the business of a separate layer.
- **Nominal TX power for the graph and for the legacy medium** (§11.2, §11.5).
- **Memory.** The model uses the server's existing RSSI map (4096 × 4096 values in shared memory) and a bitmap of 2 MB; nothing grows with the number of frames. Runs with very dense graphs (thousands of neighbours per node) cost more per frame in the medium, in proportion to the number of receivers.
- **Reported RSSI:** a normal error only; no quantisation to the 0.25 dB resolution or to the integer dBm the MAC stores, no saturation outside −100 to −30 dBm, and no error on the CCA energy.
