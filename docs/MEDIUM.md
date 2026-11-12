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

- **Received power.** `P = link(rx, tx)` from the `-g` options (and `--vtime-link`), or, with `--positions`, from the node positions and a path-loss model (§11), which also derives who hears whom. With `-g` values: the value is the received power, whatever TX power the sender uses. With `--medium-tx-ref=DBM`, the value is the received power when the sender transmits at DBM, and `P = link + (tx_dbm − DBM)`, with `tx_dbm` from the frame header: the TX power the MAC last set (`wsnode` and `wsbrd` set it from their `tx_power` setting at start; a radio whose MAC never set one reports 0 dBm). The TX power used there and with positions (§11) is at most the sender's PA maximum, `--medium-tx-max=DBM|profile|off`: by default (`profile`) the top level of the sender's power-profile TX table, the level the power ledger clamps to (16.9 dBm in the FG25 profiles, POWER_MODEL.md §12.10), so a node asking for 20 dBm radiates 16.9 dBm; without `--power` there is no profile and no limit (the header's value, as before 2026-11-13). Plain `-g` values without `--medium-tx-ref` do not depend on the TX power. The graph of §11 (who hears whom) is still decided at `--prop-tx-dbm`. An edge without a value (0 dBm) stays a perfect link; it is also a 0 dBm interferer and 0 dBm of CCA energy, so two frames over such links always destroy each other and neither can capture the other. Give RSSIs to the edges when collisions matter. With the options of §12, the link's variation at the frame's start (shadowing, fading, bursts) is added to `P`. Pairs that are not graph neighbours do not hear each other at all: no reception, no interference, no energy in CCA.
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

  Per segment, `BER = max(BER_noise(SNR), BER_int(SIR))` (only the noise curve without an interferer). §12 offers a steep noise curve meant to be used with fading (`--medium-rx-model=layered`) and other combinations of the two (`--medium-combine`). Without interference this is exactly the radio's formula. With `--medium-model=em4` (the MVP, before EM5), interference is noise: `BER = BER_noise(SINR)`, `SINR = P / (N + I)`, which needs about 20 dB of SIR for 1 % loss on a 50-byte frame, against 5–6 dB on the kits. The counted bits are those of the radio's draw: `8 · (PHR + PSDU + FCS)`.
- **Over a frame.** The counted bits are spread uniformly over `[t_bits, t_end)`: with the measured curve, `t_bits` is the end of the frame's SHR (the kits measured the interferer over the PHR, payload and FCS; interference during the SHR is the business of the capture rules, §3); with `--medium-model=em4`, the frame's start, as before EM5. That range is cut into segments wherever an interferer starts or ends. Each segment `k` holds a share of the counted bits proportional to its duration, `bits_k`, and has its own `SNR` and `SIR_k`. `P_success = Π_k (1 − BER_k)^bits_k`. One uniform draw from the receiver's stream decides; draws are made only when `0 < P_success < 1`. The SINR reported in the verdict and the medium log is the lowest `P / (N + I)` over the segments.
- **Interferers** of a frame at receiver `r`: every other frame whose sender is a graph neighbour of `r`, overlapping in time (end exclusive: a frame ending at `t` does not overlap one starting at `t`), on the same channel at its received power, or on the next channel (channel number ± 1) 10 dB weaker (`--medium-adjacent-db`, §2.1). Frames two or more channels away do not interfere, unless the adjacent-channel table says otherwise (§13.2). External interferers (§13.1) add to the same sums. The same rule gives the energy of the lock threshold (§3) and of CCA (§6): a frame on the next channel adds its power less 10 dB.
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
- `--medium-tx-ref=DBM`, `--medium-tx-max=DBM|profile|off`, `--medium-lock-snr=DB` (refused with `--medium=legacy`).
- The reception model (§2.1; refused with `--medium=legacy`): `--medium-model=fg25|em4`, and per PHY profile `--medium-int-curve=[MODE:]X0,B|noise`, `--medium-capture-db=[MODE:]DB`, `--medium-capture-early-bits=[MODE:]N|shr`, `--medium-capture-sfd=[MODE:]on|off`, `--medium-break-db=[MODE:]DB|off`, `--medium-adjacent-db=[MODE:]DB|off`.
- Propagation from node positions: `--positions`, `--prop-*` (both media), `--rssi-offset`, `--rssi-noise` (central only); §11.8.
- Preamble sniffing: `--sniff-detect`, `--sniff-dwell-bits`, `--sniff-threshold`, `--sniff-curve`, `--sniff-sfd-bits`, `--sniff-log` (§10.5; refused with `--medium=legacy`).
- Link variation and the receiver curve: `--medium-rx-model`, `--medium-rx-curve`, `--medium-combine`, `--medium-fading`, `--medium-shadowing`, `--medium-node-share`, `--medium-reciprocal`, `--medium-burst` (§12.5; refused with `--medium=legacy`).
- External interference and spectrum: `--interferer*`, `--medium-adjacent-table`, `--medium-capture-model`, `--medium-capture-smooth`, `--medium-int-curve-presync`, `--sniff-adjacent`, `--sniff-energy*`, `--sniff-false` (§13.5).
- `--medium-log=FILE`: one line per (frame, receiver) outcome: `t_end_us src dst frame_id outcome rx_dbm sinr_min_db` (four more columns when links vary, §12.6; then, with external interferers, one more, §13.1), outcome one of `ok`, `noise` (lost, no interferer), `interference` (lost with an interferer), `captured`, `below_lock` (heard, not locked: too weak against the noise and interference), `busy` (heard while receiving another frame and not captured), `not_listening`, `tx_abort`, `left`.
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

Fading, time-varying shadowing and bursts of losses are in §12 (off by default). External interferers are in §13. Not modelled by default: RX↔TX turnaround and settling times (the emulated radio has options for the turnaround, off by default, §9.2) (Investigation §5, P4–P11 and the layered loss model). Node positions with path-loss models, static shadowing and a reported RSSI with its own error are in §11. The radio still decides its own TX timing; the medium does not delay or refuse a transmission. A frame that will be lost keeps its receiver in RX for its whole airtime, as on a real radio (legacy dropped it at its start), so central runs show more RX time.

### 9.1 Limits of the calibration

The collision figures of the MVP were upper bounds (interference treated as noise, 9–16 dB pessimistic for a locked frame); since EM5 the defaults follow the kit measurements (§2.1). What they do not cover:

- **No fading in the measurements.** The kits stood a few tens of centimetres apart on a desk. By default links keep fixed values; §12 adds variation.
- **Nothing near sensitivity.** The wanted frames were 70 dB above sensitivity (−38 … −14 dBm). Near sensitivity, noise and interference add; the medium takes the larger of the two error rates, which is optimistic there by up to a few dB: the datasheet's co-channel rejection, measured 3 dB above sensitivity with a CW interferer, needs the wanted signal 4.4 dB (mode 3) to 7.1 dB (mode 1a) above the interferer for 10 % packet loss, against 3.8 dB here for 10 % loss (mode 3, a modulated interferer, `COCHANNEL_RESULTS.md` §4.1, §4.3). `--medium-combine` (§12.4) adds the two disturbances instead.
- **Transitions are coarse steps.** Capture and break are deterministic at their margins, whereas the kits show probabilistic transitions: with 0–3 dB of margin the stronger frame was still received in 58–95 % of cycles (0 and 100 µs, §4.2), which the medium counts as both lost (`--medium-capture-model=smooth`, §13.3, models these transitions as probabilities). The capture timing was scanned at three margins in mode 3 and two in mode 1a; the transition points (mode 3: 250, 600, 750 µs) are placed at their 50 % points or at the SFD's edges.
- **Not measured, values carried over:** mode 1a's capture margin, break margin and adjacent channel (mode-3 values); every other PHY (the `other` profile, the mode-3 values, including the early window of 37 bits whatever the preamble length); long `"xl"` preambles (the whole long preamble is a capture window, as a receiver that restarts its search would take a stronger frame; not measured); frames of other lengths than 50 and 250 bytes.
- **The interference curve** fits the 50-byte, 250-byte and partial-overlap cases with one constant pair, within 0.4 dB at 50 % and 1–2 dB conservative at 99 % (§4.1). The SIR axis of the measurement is accurate to about 1 dB.
- **Preamble sniffing** (§10): its defaults are from the kits since 2026-11-12 (§10.5), in mode 3 only and against an interferer only (no detection near sensitivity); frames on the next channel take no part in it.
- RAILtest only: the firmware's MAC (CCA, ACK timing) was not in the loop.

### 9.2 RX/TX turnaround of the emulated radio (options, off by default)

The emulated radio (`app_wshwsim/sl_rf_driver.c`, the Nanostack nodes) can wait as a real radio does when it switches between receiving and sending. Two settings in the nodes' environment, as the other `WSSIM_PHY_*` settings (per node with `NODE_ENV_<i>` and `BR_ENV` in `run.sh`); both 0 by default, so runs without them are unchanged:

| Setting | Effect | FG25 values |
| --- | --- | --- |
| `WSSIM_PHY_TX_DELAY_US=N` | From the decision to send (a clear CCA, or a frame without CCA such as an ACK) to the frame's first bit on air. Meanwhile the radio is busy for the MAC, neither listens nor sends: IDLE in the shared memory (the medium does not lock it onto a frame, the legacy CCA of its neighbours does not see it sending) and in the power ledger | RAIL's RX→TX state time as configured: 192 µs in our FG25 firmware (`drivers/rail/rail_phy.c`), 130 µs in RAILtest's default. Not measured: RAILtest's TX from RX goes through idle, so the kits showed idleToTx, idleToRx and txToRx held, not rxToTx |
| `WSSIM_PHY_RX_DELAY_US=N` | From the end of the radio's own frame to its receiver being on. The TX-done callback comes at the frame's end as before; meanwhile the radio is IDLE (ledger: idle) and a frame that starts then is not received (central medium: `not_listening`; legacy medium: the radio's `rf rx lost: TX-to-RX turnaround` line). A MAC request in that time (a new channel, an RX state) takes effect at its end, and a CSMA backoff started in it listens from its end | See below: not RAIL's TX→RX time itself (182 µs in our firmware, 100 µs in RAILtest's default) |

**The RX delay is stricter than the radio.** A frame that starts during the delay is lost, whereas the FG25 still receives a frame that started before its receiver was ready if at least about 418 µs of its preamble remain when it is (§10.5: 225 µs of settling, the 47 µs detection and 22 bits). To match the kits, set the delay to the time after the node's own frame during which a frame starting then cannot be received: `max(0, txToRx + 418 µs − preamble)`, with txToRx 182 µs in our firmware and the preamble's duration: 0 with the FG25's 12-byte preamble in mode 3 (640 µs), 173 µs with an 8-byte preamble at 150 kbit/s (427 µs). The settling itself (§10.5) is not applied to receptions at a frame's start.

The FG25 values are RAIL's state timings, which RAIL holds to the µs above about 125–130 µs (idleToTx, idleToRx and txToRx shown; kits: firmware repo `docs/RADIO_TIMING_POWER_RESULTS.md` §5; `COCHANNEL_RESULTS.md` §4.5). After the receiver is on, the FG25 still needs about 225 µs before it can detect a preamble (§10.5); the medium applies that only to sniffing, not to a reception at a frame's start (§3).

Clients of the radio interface ([RADIO_INTERFACE.md](RADIO_INTERFACE.md)) report their own states and send when they decide: their turnaround is theirs to model.

Effect on a run: every frame goes on air later by the TX delay (ACKs and Wi-SUN's timestamps included), and frames that arrive just after a node's own frame are lost. dag_simple with the central medium, 300 s, seeds 1–3, with 192/182 µs against without: every one of 9 948 transmissions of seed 1 went on air exactly 192 µs after the MAC's decision; 13, 13 and 14 of 14 routers operational against 14, 13 and 13; 2 856, 2 562 and 2 842 `ok` receptions at the medium against 2 790, 2 557 and 2 545. With the FG25 profile (seed 1), node 1 goes through idle at each of its 706 frames: 0.74 µC of transitions per frame (POWER_MODEL.md §12.10) and about 370 µs of idle instead of listening; the average over the 15 nodes is 8.907 mA against 8.911 mA without the options. The timing changes every later draw, so the runs are not comparable frame by frame.

## 10. Preamble sniffing

For sniff MACs (low-power listening, preamble sampling): a node wakes for a short window on a period, its receiver looks for a preamble on one channel, and it stays in RX only if it found one. Senders put a wake-up preamble longer than the receivers' period before their frame. The medium decides each detection from the frames on air, and a receiver that detects a long preamble can then receive the frame that follows. Central medium only (§1); the Nanostack nodes do not use it.

### 10.1 The primitive

A radio sends `"xn"` (`struct sim_sniff`) during its run at time `t`: channel `C`, duration `d`, a tag, flags, an end state. Its receiver is on, on `C`, from `t_on` to `t1 = t + d`: `t_on = t`, or the end of the node's own frame if one is on air (half duplex, §10.3). It detects over the **window** `[t0, t1)`, `t0 = t_on + --sniff-settle-us` (0 by default), at most `t1`. The conductor answers with one `"xd"` (`struct sim_sniff_result`), not at once but in a later wake of the node:

- at the **detection time** `td`, the end of the first dwell that detects a frame (§10.2): `detected = 1`, the frame's `frame_id` and sender, `t_us = td`, `rssi_dbm` = the frame's received power `P`, `sinr_db` = the lowest SINR over that dwell;
- or at `t1` if nothing was detected: `detected = 0`, `t_us = t1`, `rssi_dbm` = the energy on `C` at the node at `t1` (noise plus the frames on air then that it hears, as `"xs"` would answer; on an empty channel, the noise floor −119.93 dBm).

A new `"xn"` while a sniff is in progress replaces it (no `"xd"` for the old one). A frame sent by the node ends its sniff, with no `"xd"` (half duplex, §10.3). A stopped node's sniff ends with it. For a client of the stack-neutral radio interface, whose radio state the conductor knows from its reports, a report that leaves the sniff (`OFF`, `IDLE`, `TX` from another state, or a receiver on another channel) also ends it with no `"xd"` (outcome `left`), and an `"xn"` sent while it holds a frame loses that frame (outcome `left`) before the sniff starts ([RADIO_INTERFACE.md](RADIO_INTERFACE.md) §6.7).

### 10.2 Detection rules

The candidates are the frames on `C` whose sender is a graph neighbour of the node, not cut and not dropped for it (as for reception, §3), other than the node's own; they are also each other's interferers, over their whole airtime. For each candidate `F`, received at power `P_F`:

- **Detectable part.** `--sniff-detect=preamble` (default): its preamble, `[t_start, pre_end)`, `pre_end` = the end of its SHR minus the SFD (`--sniff-sfd-bits`, 16 bits) and the margin a detection must leave before it (`--sniff-margin-bits`, 22 bits), at the frame's rate, including an `"xl"` preamble. `--sniff-detect=frame`: the whole frame (a carrier or energy detector that also catches a frame in progress).
- **Dwells.** A detection try observes `--sniff-dwell-bits` bits (7) of the frame, `D_F = ⌈bits · 10⁶ / rate_F⌉` µs (140 µs at 50 kbit/s, 47 µs at 150 kbit/s). The tries are consecutive dwells `[a + k·D_F, a + (k+1)·D_F)` from `a = max(t0, t_start(F))`, each ending at or before `min(t1, end of the detectable part)`. A frame that overlaps the window by less than one dwell is never detected; a long preamble that covers the whole window gets `⌊(t1 − t0) / D_F⌋` tries.
- **Probability of one try.** 0 if `P_F` is below `--sniff-threshold` (off by default); otherwise `curve(SINR_min)`, `SINR_min` the lowest SINR of `F` over the dwell, `P_F / (N + I)` with `I` the sum of the other candidates on air (it is lowest at the dwell's start or where an interferer starts). The curve is a table `--sniff-curve=SINR:P,SINR:P,...` (dB, ascending): linear between the points, the end values beyond them; the default `-2.8:0,0.6:0.002,3.9:0.65,5.4:1` is the kits' (§10.5). `--sniff-curve=ber:N` gives instead `(1 − BER(SINR))^N`, `BER` the medium's noise curve (§2) of the SINR, whatever the reception model (§9.1), that is, N preamble bits received without an error (`ber:16` was the default before the kit calibration).
- **Draws.** Each try with `0 < p < 1` takes one uniform draw from the node's detection stream, `mix64(seed + mix64(node ID + 1) + K')`, separate from the verdict streams; `p = 0` or `1` takes none. The tries are decided in the order of their dwell ends (then frame start, then frame ID), each when the conductor reaches its end, so every interferer that started before it is known. The first try that succeeds is the detection; the rest are not drawn.
- **Busy receiver.** A receiver locked onto a frame (at that frame's start during the window, §10.3) is synchronised on it and detects no other frame: the tries of other frames whose dwell overlaps that reception have probability 0 (no draw). With the default lock threshold this only matters when the locked frame is on another channel or with `--medium-lock-snr` below 0, since otherwise the locked frame's power keeps the other frames' SINR low.

Values of the default curve without interference (`P = N + SINR`, `N` = −119.93 dBm; the kits measured it against an interferer, the medium applies it to the SINR):

| SINR (dB) | −2.8 | 0.6 | 2 | 3 | 3.9 | 4.6 | 5.4 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Received power (dBm) | −122.7 | −119.3 | −117.9 | −116.9 | −116.0 | −115.3 | −114.5 |
| One try (7 bits) | 0 | 0.002 | 0.28 | 0.47 | 0.65 | 0.81 | 1.00 |
| Ten tries (470 µs at 150 kbit/s) | 0 | 0.02 | 0.96 | 1.00 | 1.00 | 1.00 | 1.00 |

Before the kit calibration (`--sniff-dwell-bits=16 --sniff-margin-bits=0 --sniff-settle-us=0 --sniff-curve=ber:16`): one 16-bit try 0.50 at 4.9 dB SINR, 0.90 at 8.7 dB, 0.99 at 13.4 dB.

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

All are user settings. The FG25 datasheet gives no preamble-detection figure (no detection time, probability or threshold; root repo `documents/pre-study/datasheets/efr32fg25-datasheet.pdf`, Rev. 1.2, §4.9.5). Since 2026-11-12 the dwell, margin, settling time and curve are the kits' (EM5: firmware repo `docs/RADIO_TIMING_POWER_RESULTS.md` §4, RAILtest on BRD4271A kits, EU mode 3; raw data in `~/kit-runs/2026-11-12_em5_sniff/`). With them the model detects a preamble 272 µs after the receiver is on (measured 252–293 µs, median 270, 254 cycles) and 47 µs after a preamble starts when the receiver was already on (measured 7–61 µs, median 47 µs, in clusters near 20, 35 and 48 µs; 29 cycles), and acquires a frame if the receiver was on at least 418 µs before the preamble's end (measured: 100 % at 423 µs and more, 3 of 18 at 413 µs, 0 % at 398 µs and less). **Runs that sniff give other results than before** (later and fewer detections of short windows, no lock in the last 38 bits of a SHR); `--sniff-dwell-bits=16 --sniff-margin-bits=0 --sniff-settle-us=0 --sniff-curve=ber:16` gives the earlier model.

| Option | Default | Source or reason |
| --- | --- | --- |
| `--sniff-detect=preamble\|frame` | `preamble` | RAIL's RX duty-cycle "preamble sense" mode: the radio stays in RX if it has a preamble at the end of the sense time (`RAIL_RX_CHANNEL_HOPPING_MODE_PREAMBLE_SENSE`, `rail_types.h` of the Simplicity SDK 2026.6.2) |
| `--sniff-dwell-bits=N` | 7 | Kits: a receiver already on detects a preamble 7–61 µs after it starts, median 47 µs (7 bits at 150 kbit/s); one dwell is the median, the faster detections are not modelled. Before: 16, an assumption |
| `--sniff-threshold=DBM\|off` | `off` | RAIL applies an RSSI threshold in duty-cycle mode only if `RAIL_RX_CHANNEL_HOPPING_OPTION_RSSI_THRESHOLD` is set (`rssiThresholdDbm`, same header); off by default. The medium compares it with the candidate frame's own received power, not with the total energy on the channel the radio measures; they differ only with interferers, whose SINR then already lowers the try's probability |
| `--sniff-curve=ber:N\|SINR:P,...` | `-2.8:0,0.6:0.002,3.9:0.65,5.4:1` | Kits, receiver on in a preamble under a co-channel FSK frame (SIR = wanted over interferer, 24–29 cycles per point): first try 100 % from +5.4 dB, 18/27 at +3.9 dB, none at +0.6 dB and below; within 4 ms, 4/29 at +0.6 dB (about 0.002 per try), none at −2.8 and −8 dB. Without interference it puts 50 % per try at about −116.8 dBm (3.1 dB SINR). With `--medium-rx-model=layered` (§12) and no `--sniff-curve`, the default is `ber:16` on the layered noise curve instead (50 % per try at 11.2 dB SINR, §12.3), so that detection keeps the layered model's field margin: the kits measured detection against an interferer only, not near sensitivity; for comparison, FG25 packet sensitivity (10 % PER, 250 octets) in the 866 MHz band: −108.8 dBm in mode 3 with FEC (datasheet Table 4.30, p. 92). Before: `ber:16` |
| `--sniff-sfd-bits=N` | 16 | IEEE 802.15.4 SUN FSK SFD of 2 octets, as the emulated radio sends (`WSSIM_PHY_SFD` = 2 bytes). Also the SFD of the capture rule (§2.1, `--medium-capture-sfd`) |
| `--sniff-margin-bits=N` | 22 | Kits: the frame is received only if about 150 µs (22 bits) of preamble remain after the detection, before the SFD; with less, the radio reports no detection at all. A detection must end at least margin + SFD bits before the SHR's end (§10.2), which is also the lock rule's limit (§10.3). Before: 0 (no such setting) |
| `--sniff-settle-us=N` | 225 | Receiver settling (AGC, timing recovery) before detection can start, after the request or after the node's own frame. Kits: the first detection comes 252–293 µs after the radio's RX state is reached (after TX→RX and after idle→RX alike), that is 225 µs plus one dwell. The request is taken as the moment the receiver is on: a client that models RAIL's own idle→RX time (100–120 µs) adds it before its request. Before: 0 |
| `--sniff-log=FILE` | none | One line per sniff: `t_req node sniff_id chan t0 t1 outcome t frame_id src rssi_dbm sinr_db locked`, outcome one of `detected`, `none`, `tx_abort`, `replaced`, `stopped`, `left` (a client of the radio interface reported a state that left the sniff) |

The run's end prints the number of sniffs per outcome, the locks made by a sniff and the detection draws.

### 10.6 Limits and calibration

- **Measured on the kits (EM5, 2026-11-12), and not:** detection time after the receiver is on and after a preamble starts, the preamble needed for reception, detection against a co-channel FSK interferer, in EU mode 3. Not measured: mode 1a and the other modes (the bit counts and the settling time are taken as they are), detection near sensitivity (unreachable on the desk: the curve's SIR axis is applied to the SINR, noise included), false detections on noise, our firmware's own sniffing (RAILtest only).
- **Tries are independent draws.** On the kits they are not: at +3.9 dB SIR, 5 of the 9 cycles that missed the first try needed 7 to 13 more dwells, as if a fixed alignment of the interferer kept the detector from locking. The model gives a frame several tries an independent chance each, so detection over several dwells at a marginal SINR is optimistic.
- Detection is per frame: interferers only lower a frame's SINR. A detector that triggers on the summed energy of several frames (for example on a collision whose frames are each too weak) is an option since §13.4 (`--sniff-energy`).
- The settling time (`--sniff-settle-us`, 225 µs) applies to sniffs only; reception at a frame's start (§3) has no settling time unless the radio's turnaround options delay its receiver (§9.2). A frame that starts during the settling time still locks the receiver at its start by §3 if LISTEN is in its shared-memory entry (the radio writes it, or the conductor for a client of the radio interface).
- No false detection by default: a window with no frame on air never detects (the default curve gives 0 per try below −2.8 dB SINR, and only for a frame on air). Real preamble detectors trigger falsely on noise now and then, which keeps the receiver on until the client's timeout; `--sniff-false` (§13.4) models it, at a rate still to be measured.
- **Energy carrier sense** is approximated, not modelled: `--sniff-detect=frame` with `--sniff-threshold` and a table curve detects a frame from its own power, while an energy detector compares the total energy on the channel with its threshold.
- **Several channels in sequence:** one `"xn"` per channel, each answered by its own `"xd"` in a wake of the node, so a scan of `n` channels costs `n` wakes in the ledger; a radio that hops between sense windows in hardware (RAIL's RX channel hopping) wakes the MCU only on a detection.
- A node that starts (or restarts) while frames are on air does not hear those frames (as for reception: the receivers of a frame are fixed at its start), so its first sniff cannot detect them.
- The legacy medium drops `"xn"` and `"xl"` with a warning: a client waiting for an `"xd"` gets none.
- By default, frames on the next channel neither detect nor lower a candidate's SINR (the adjacent-channel rule of §2 applies to reception, locks and CCA only); `--sniff-adjacent` (§13.4) adds them.
- Sniffing works in virtual time with the central medium only.

### 10.7 Checks

`sim_utils/vtime/test_sniff.py` (fake nodes, in the LFN suite as `sniff`): detection of a frame on air at the expected time and power; an empty channel (noise floor, one wake); the threshold; partial overlap at both edges of the window, with both detection modes; an interferer (SINR against the formula, no detection at equal power); the wrong channel; half duplex (deaf during its own frame, a frame sent during the window ends the sniff); one-dwell detection rate at the margin against the curve, and determinism by seed; a table curve (no draws at 0 and 1); a 100 ms `"xl"` preamble detected and locked onto mid-preamble, its `"xx"` and verdict, against a radio that only listened from the start, one that turned on late without a sniff, and a sniff without the lock flag; the lock rules (no lock after the SFD, an existing lock reported, bits counted from the lock, the lock threshold against the frames still on air only, no detection of another frame while locked); the ledger (listening during the window, receiving after the lock, the TX time kept when the sniff is asked during the node's own frame); the settling time; many frames joining a window (one chain of check events); option errors. These tests run with the model's settings before the kit calibration (`MODEL_OPTS`: 16-bit dwells, no margin, no settling, `ber:16`); `kit_defaults` checks the defaults (detection 225 µs + 7 bits after the request, none whose dwell would end within 38 bits of the SHR's end).

## 11. Propagation from node positions

The links of section 2 can come from node positions and a path-loss model instead of `-g` values. The server computes, once at the start, the mean received power of every pair of positioned nodes in each direction, with a static shadowing term, and derives from it who hears whom. The time-varying part of a link (fading, slow shadowing, bursts of losses) is a separate layer that adds to this mean; external interferers are another. The option is disabled by default: without `--positions`, every output is unchanged.

The model lives in `app_wssimserver/sim_propagation.c`. The medium asks it for one value per frame and receiver, `prop_link_dbm(rx, tx, tx_dbm)`, which is a table lookup and one addition; there is no per-frame search.

### 11.1 Positions file

`--positions=FILE`: one node per line, `ID X Y Z [FLOOR [GAIN_DBI]]`, coordinates in metres. Blank lines and lines starting with `#` are skipped. `FLOOR` is an integer; without it, the floor is `Z / --prop-floor-height` rounded (3.5 m). `GAIN_DBI` is the node's antenna gain, applied in transmission and in reception; without it, `--prop-gain` (0 dBi). A node ID may appear once. The ID is an integer from 0 to 4095; the coordinates are finite numbers within ±10⁶ m, `FLOOR` within ±10 000 and `GAIN_DBI` within ±50 dBi; a line with anything else is an error. Lines may have any length, and a `#` starts a comment anywhere on a line. Nodes that are not in the file keep the `-g` links only.

`radio_if/scale/gen_building.py --positions FILE` writes this format (`ID X Y Z FLOOR` after a `#` header), so a generated building can be used directly: `gen_building.py --nodes 200 -o /dev/null --positions b.pos`.

### 11.2 Received power

For a frame that `tx` sends at `tx_dbm` (from the frame header, at most the sender's PA maximum `--medium-tx-max`, §2, as with `--medium-tx-ref`), the mean power at `rx` is

    P(rx ← tx) = tx_dbm + G_tx + G_rx − PL(tx, rx) − X(rx, tx)

with `G` the antenna gains (§11.1), `PL` the path loss of the model (§11.3) and `X` the static shadowing (§11.4). At the start, the server stores `P` at the nominal TX power `--prop-tx-dbm` (20 dBm, the default `tx_power` of `wsbrd` and `wsnode`) in its RSSI map, and the medium adds `tx_dbm − 20` to it for each frame. The legacy medium and wall-clock runs use the map as it is: their radios do not apply the sender's TX power, so `--prop-tx-dbm` should be the TX power the nodes use.

The TX power is the one in each frame's header. A Nanostack node sets it from its `tx_power` setting (`-o tx_power=14`, or `NODE_OPTS_<i>` in `run.sh`). A client of the radio interface sets the header's `tx_dbm` field of every frame ([RADIO_INTERFACE.md](RADIO_INTERFACE.md)); a client that leaves it at 0 sends at 0 dBm, so its frames arrive 20 dB below the stored levels with the default `--prop-tx-dbm`. Such a client should fill the field with its real TX power, and `--prop-tx-dbm` should then match it.

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

**Choosing the model.** The default is site-general and has no wall term beyond its exponent: in large open floors it predicts long ranges. With the default model at 14 dBm (`--prop-tx-dbm=14`), the two farthest nodes of a 152 m × 72 m floor of `gen_building.py` receive each other at about −91 dBm before shadowing, and a 4000-node building of 20 such floors has about 2890 neighbours per node on average in the hearing graph. Buildings with many solid walls are closer to the wall grid or the per-metre term: with `--prop-walls=8:2.4`, the same building has about 1730 neighbours per node, and with the `gen_building.py` model, 162. The parameters should be set from measurements in the target building when they exist.

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
- `--vtime-link` sets both directions of a pair to its value from its time on, in place of the model's, whatever the TX power of the frames. A pair of positioned nodes below the cutoff joins the graph, both ways, at that time; a pair with an unpositioned node must already be in the graph (from `-g`).
- Without `-g`, the graph is the model's alone (without `--positions`, the absence of `-g` means that every node hears every other one, as before).

`run.sh` takes the node count from its `TOPO` file: for a building, a `TOPO` file with one line `0,N-1` (a link without value between the first and the last node) gives N nodes, and the links come from `--positions` in `SIM_OPTS` (an absolute path).

### 11.6 Reported RSSI

The RSSI that a real receiver reports has its own measurement error, which matters because the stack uses it to select parents. Two optional terms, central medium only, apply to the value the server delivers (the `rx_dbm` of `"xx"` and `"xv"`, and the `rssi_dbm` of a detection in `"xd"`), never to the power the medium uses for its decisions or writes in `--medium-log`:

- `--rssi-offset=σ` (0 dB): a fixed offset per receiver, a normal draw from the seed and the node (a calibration error);
- `--rssi-noise=σ` (0 dB): a normal draw per frame and receiver, from the seed, the node and the frame ID (the same value in the frame's `"xx"` and its verdict).

The FG25 datasheet (Rev. 1.2, Tables 4.23 and 4.24) gives an RSSI resolution of 0.25 dB and a maximum absolute error, under typical conditions and with offset correction, of 1.2 dB in the 915 MHz band and 1.8 dB in the 868 MHz band, from −100 to −30 dBm. A standard deviation of half the maximum puts 95 % of the receivers within it: `--rssi-offset=0.6` (915 MHz) or `0.9` (868 MHz). The energy that CCA measures (`"xs"`) keeps the true value.

### 11.7 Outputs

- At the start, the server's log gives the model and its parameters, the shadowing, and the graph among the positioned nodes: the pairs in it, how many of them come from the model and how many from `-g` options alone (below the cutoff), the degree (mean, minimum, maximum; both kinds of pairs), the pairs at −100 dBm or more, the asymmetric pairs, the directions set by `-g`, and the time the computation took.
- `--prop-dump=FILE`: the links at start-up, one line per direction of every pair in the graph among the positioned nodes: `rx tx dist_m floors walls pl_db shadow_db gains_db mean_dbm heard given`, `mean_dbm` at `--prop-tx-dbm`, `heard` 1 if the model puts the pair in the graph (0: a `-g` option alone does), `given` 1 if a `-g` value sets that direction (the medium then uses the `-g` value, not `mean_dbm`). Later changes by `--vtime-link` are not in it. The file grows with the square of the number of nodes: about 0.5 GB for the 4000-node building of §11.3. `--prop-dump-node=ID` keeps only the lines of one node.
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
| `--prop-tx-dbm=DBM` | 20 | nominal TX power: the RSSI map and the graph (§11.2) |
| `--prop-cutoff=DBM`, `--prop-margin=DB` | −125, 10 | hearing graph (§11.5) |
| `--prop-dump=FILE` | none | the computed links at start-up (§11.7) |
| `--prop-dump-node=ID` | none | with `--prop-dump`, only the links of this node |
| `--rssi-offset=DB`, `--rssi-noise=DB` | 0, 0 | reported RSSI error (§11.6), central medium only |

The `--prop-*` options need `--positions`.

### 11.9 Checks

`sim_utils/vtime/test_propagation.py <wssimserver>` (fake nodes and `--prop-dump`): option errors and malformed positions (a non-integer ID, non-finite or out-of-range values, extra columns), lines of any length; each model against hand computation (floors from the column and from `Z`, the floor table and its extrapolation, the wall grid, the per-metre walls, the minimum distance, the gains, the nominal TX power); shadowing reproducible by seed and different with another seed, its standard deviation over 44 850 pairs, reciprocal by default, the per-direction and per-node parts and their correlation; the graph cutoff and margin, in the dump and in the medium (no lock and no log line beyond the cutoff); a `-g` link without value below the cutoff, with the model's level; the frame's TX power applied to a modelled link, a `-g` value overriding it with `--medium-tx-ref`, `--vtime-link` replacing it (frames sent below the nominal TX power, so that the model's value would differ) and adding a positioned pair below the cutoff to the graph; `--prop-dump-node`; the RSSI map at the nominal TX power with `--medium=legacy`; the `gen_building.py` model reproduced link for link; a 4000-node building built in under a second (0.53–0.89 s); the reported RSSI (offset fixed per receiver, noise per frame with its standard deviation, the same value in `"xx"` and `"xv"`, the true power in the medium log, reproducible by seed; with `--positions`, the model's level plus the error; the same value in a detection's `"xd"` and its frame's `"xx"`).

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

## 12. Link variation and the layered loss model

Root repo `documents/pre-study/investigation/Investigation_Nanostack_Emulator.md` §5, "Formalising the loss model", splits the radio's empirical curve (§2) into physical layers. This section implements layers 2, 3 and 5 and a burst process, in `app_wssimserver/sim_channel.c`. Every option is off by default: without them, every output is byte-identical to the medium without this module (§12.8).

| Layer | What | Where |
| --- | --- | --- |
| 1. Mean link level | `link(rx, tx)` and `--medium-tx-ref` (§2), or the node positions with static shadowing (§11) | unchanged |
| 2. Slow shadowing | Gauss-Markov process per link, optional share per node | §12.1 |
| 3. Fast fading | Rician envelope with a coherence time and a coherence bandwidth across channels | §12.1 |
| Loss bursts | Gilbert-Elliott process per link, receiver or node | §12.2 |
| 5. Receiver decision | empirical curve, or a steep curve meant to be used with fading; noise and interference combined | §12.3, §12.4 |

Layer 4 (interference) is §2 and §3. Layer 6, the reported RSSI with its own measurement error, is §11.6: it applies to the received power with its variation. The time-varying shadowing here adds to the static shadowing of §11.4.

### 12.1 Variation of a link

The received power of a frame at a receiver is `P = link(rx, tx) + S + F + B`, in dB, evaluated once per frame and receiver at the frame's start (`t_start`, including an `"xl"` preamble) and kept for the whole frame. Every use of that power takes this value: the lock decision and capture (§3), the frame as an interferer of other frames (§2), CCA energy (§6), preamble sniffing (§10), the verdict's `rx_dbm` and the medium log. A `-g` edge without a value (0 dBm, a perfect link) does not vary; links from positions (§11) do. Who hears whom stays the hearing graph of §2 and §11.5, derived from the mean levels: a link's variation never adds or removes a neighbour.

- **Slow shadowing `S`** (`--medium-shadowing=SIGMA_DB,TAU_S`): a stationary Gauss-Markov (Ornstein-Uhlenbeck) process with standard deviation `SIGMA_DB` and autocorrelation `exp(−Δt / TAU_S)`. With `--medium-node-share=F`, `S = SIGMA_DB · (√(1 − F) · s_link + √(F / 2) · (s_rx + s_tx))`, with `s_link`, `s_rx`, `s_tx` independent unit processes, the last two shared by every link of their node. The variance stays `SIGMA_DB²`, and two links with one node in common correlate by `F / 2`: for example the two links from a child to its two parents.
- **Fast fading `F`** (`--medium-fading=K,TC_S`): the power gain of a Rician envelope with factor `K` (linear, the power of the direct component over the scattered power; `K = 0` is Rayleigh) and mean power 1: `F = 10·log10(|√(K/(K+1)) + √(1/(K+1)) · s|²)`, with `s` the scattered part, a circular complex Gaussian of unit power. In time, the scattered field's correlation is `ρ = exp(−Δt / TC_S)`: **`TC_S` is the lag at which the field correlation falls to e⁻¹** (Gauss-Markov), and the power gain's correlation is `(ρ² + 2Kρ) / (1 + 2K)`.
- **Across channels** (`--medium-fading-bc=MHZ`, 5.5 MHz; `inf`: the same fading on every channel). `s` is the sum of 8 taps of equal power, each an independent complex Gauss-Markov process in time, with delays `τ_l` drawn once per link from an exponential distribution of mean `σ_τ`; on channel `c` tap `l` turns by `exp(−j 2π f_c τ_l)`, `f_c = c · spacing`. Each channel's marginal stays Rician with factor `K`. On average over links, the scattered field's correlation between two channels `Δf` apart has magnitude `1 / √(1 + (2π σ_τ Δf)²)` (an exponential power delay profile), and `σ_τ = √3 / (2π B_c)`, so that it is 0.5 at the coherence bandwidth `B_c`. A single link, with its 8 delays, has its own correlation around that mean; the power correlation of two distant channels does not fall below about 1/8 (Rayleigh). The direct component has delay 0 and is the same on every channel. The spacing (`--medium-chan-spacing=KHZ`) comes from the frame's data rate by default: 100 kHz at 50 kbit/s (Wi-SUN FAN EU mode 1a), 200 kHz at any other rate (EU mode 3 at 150 kbit/s). The medium knows only the channel number and the rate of a frame, not the band: NA mode 1b (50 kbit/s, 200 kHz) or mode 3 (400 kHz) need the option. Channel 0's frequency is irrelevant (only differences count).
- **Reciprocity** (`--medium-reciprocal=on`, the default): a link's processes (shadowing, fading taps and delays, link bursts) are the same in both directions, as the channel between two antennas is at one frequency and time. Asymmetric mean levels (different TX powers, noise or antennas) stay in `link(rx, tx)` (§2). `off`: each direction has its own link processes. The node terms are a node's own and stay common to both directions: with `--medium-node-share` or node-scope bursts, the two directions of a link remain correlated (by the node share, or through the common node state) even with `off`.

**Determinism.** Every Gaussian process is an exact Ornstein-Uhlenbeck process built from the top down, so that its value at `t` depends only on (run seed, process, link, `t`). Anchors every `8τ` form an AR(1) of coefficient `e⁻⁸`; between two anchors, the interval holding `t` is halved repeatedly, each midpoint drawn from the exact bridge distribution given its interval's ends (mean `r (x_lo + x_hi) / (1 + r²)`, variance `(1 − r²) / (1 + r²)`, `r = e^(−half interval / τ)`), down to `max(1 µs, τ / 16384)` (18 µs at 0.3 s), then interpolated linearly. Each draw comes from a counter-based generator keyed by the anchor, the level and the interval. The values at any set of times have the exact joint distribution of the process at every lag down to that resolution, whatever the order or number of the queries. Each process caches its two anchors and the first 12 midpoints of its last path: values, never state, so a cached value is the one a fresh computation gives. The streams are separate from the verdict and detection streams (§7, §10.2). Cost: about 3 µs per sample with 8-tap fading and shadowing (`--medium-var-dump`, 200 000 samples 50 ms apart in 0.69 s); about 340 bytes per cached process (8 per link with frequency-selective fading, about 40 MB for 15 000 links). The hidden star of §12.7 with the layered model runs within 5 % of the empirical wall time.

### 12.2 Loss bursts

`--medium-burst=GOOD_S,BAD_S,DB[,SCOPE]`: a two-state process (Gilbert-Elliott) whose sojourns are exponential with means `GOOD_S` and `BAD_S`. In the bad state the received power is `DB` lower (`B = −DB`, else 0). It starts in the bad state with the stationary probability `BAD_S / (GOOD_S + BAD_S)`. The `n`-th sojourn length is drawn from (seed, process, `n`), so a state at `t` is the same whatever was asked before. The scope:

- `link` (default): one process per link (both directions with reciprocity);
- `rx`: one process per receiver, shared by all the links it hears (a receiver blocked or detuned);
- `node`: one process per node, and a link is in the bad state when either end is: a body or an obstacle at a node attenuates all its links, both ways. A link is then bad a share `1 − (1 − p)²` of the time, `p` the share of one node.

Each process caches its current sojourn: a query walks forward from it, so the cost is proportional to the number of sojourns between two queries of the process (one or none for queries closer than the mean sojourn), and a query earlier than the cached sojourn starts again from time 0 (the conductor's time only moves forward; only `--medium-var-dump`'s `shuffle` does that). The attenuation applies to the frames as wanted signals and as interferers, like `S` and `F`. External noise rising at a receiver (other users of the band) is interference, not attenuation, and is not this process.

### 12.3 Receiver curve and its calibration

`--medium-rx-model=layered` replaces the noise curve of §2 by a steep receiver curve of the same form, `BER = min(0.5, 0.5 · exp(−(SNR − X0) / B))` with `--medium-rx-curve=X0,B` (9.0 dB, 0.9 dB). It also switches Rician fading on with `K = 10`, `TC_S = 0.3 s` and `B_c = 5.5 MHz`, unless `--medium-fading` is given, and makes `sum` the default combination (§12.4). The preamble sniff's `ber:N` curve (§10.2) uses the same noise curve.

The empirical curve (§2) is the average behaviour of real links: it already contains fading. The steep curve is chosen so that, averaged over the default fading, the loss rate against the mean level reproduces it:

- `B = 0.9 dB` is the slope measured on the FG25 kits for interference (§2.1, firmware repo `docs/COCHANNEL_RESULTS.md` §4.1): the same demodulator.
- `K = 10` comes from the check in the Investigation (§5): a steep curve averaged over Rician fading with K ≈ 10 matches the empirical curve within about a factor 2 from −85 to −100 dBm. A search over K from 6 to 15 with `B = 0.9 dB` gives the smallest largest ratio at K = 8–10 (factor 2.0); K = 6 and 12 give factors 4.4 and 3.9.
- `X0 = 9.0 dB` minimises the largest ratio of the two loss rates from −100 to −85 dBm for a 100-byte PSDU (848 counted bits).

Fit, frame loss rate against the mean received power (numerical average over the Rician distribution):

| Mean level (dBm) | −104 | −102 | −100 | −98 | −96 | −94 | −92 | −90 | −88 | −86 | −84 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Empirical, 100-byte PSDU | 13.7 % | 5.3 % | 2.0 % | 0.73 % | 0.27 % | 9.9·10⁻⁴ | 3.6·10⁻⁴ | 1.3·10⁻⁴ | 4.9·10⁻⁵ | 1.8·10⁻⁵ | 6.7·10⁻⁶ |
| Layered, 100-byte PSDU | 36.7 % | 13.1 % | 3.7 % | 0.94 % | 0.24 % | 6.4·10⁻⁴ | 1.9·10⁻⁴ | 6.7·10⁻⁵ | 2.7·10⁻⁵ | 1.3·10⁻⁵ | 6.3·10⁻⁶ |
| Ratio layered / empirical, 100 bytes | 2.68 | 2.49 | 1.88 | 1.29 | 0.88 | 0.64 | 0.53 | 0.50 | 0.55 | 0.69 | 0.95 |
| Ratio, 50-byte PSDU (448 bits) | 3.77 | 3.29 | 2.41 | 1.64 | 1.13 | 0.85 | 0.73 | 0.72 | 0.83 | 1.06 | 1.50 |
| Ratio, 250-byte PSDU (2048 bits) | 1.68 | 1.67 | 1.33 | 0.93 | 0.63 | 0.44 | 0.34 | 0.31 | 0.32 | 0.38 | 0.51 |

From −100 to −85 dBm the largest ratio is 2.0 for 100 bytes, 2.4 for 50 bytes and 3.2 for 250 bytes. The fit cannot hold for every length: the empirical curve's shallow slope makes its loss grow with the frame length faster than a steep curve's does. Below −100 dBm the layered model loses more. The 10 % loss points:

| PSDU | Empirical | Layered, averaged over fading | Steep curve alone (no fading) |
| --- | --- | --- | --- |
| 50 bytes | −104.6 dBm | −102.1 dBm | −104.0 dBm |
| 100 bytes | −103.3 dBm | −101.5 dBm | −103.5 dBm |
| 250 bytes | −101.6 dBm | −100.8 dBm | −102.7 dBm |

The steep curve's 10 % point for 250 bytes, −102.7 dBm, is 6 dB above the datasheet's conducted sensitivity in mode 3 with FEC (−108.8 dBm, §10.5). The layered model keeps the empirical curve's field margin: noise figure, antenna and installation losses are not separate layers. With the layered curve, a sniff's detection try (`ber:16`, §10.2) succeeds half of the time at SINR 11.2 dB (−108.7 dBm without interference) and 90 % at 12.9 dB (−107.0 dBm), instead of −115.0 and −111.3 dBm with the empirical curve.

Changing K, the coherence time, shadowing or bursts changes the average loss: that is their purpose. The calibration above holds for the defaults only. `sim_utils/vtime/layered_fit.py` computes these tables (`--search`: the K search and the fit of X0, a few seconds); `test_channel.py` checks the medium against them (§12.8).

### 12.4 Noise and interference together

§2 takes the larger of the noise and interference error rates, which is optimistic near sensitivity (§9.1). With `--medium-combine=P`, the interference is first converted into the noise power that gives the same error rate on the noise curve, `I_eq = P_s / 10^(SNR_eq / 10)` with `SNR_eq = X0 + (SIR − x0_int) · B / b_int` (`x0_int`, `b_int` the measured curve of the frame's profile, §2.1), and the two disturbances are combined as `D = (N^p + I_eq^p)^(1/p)`. The bit error rate is the noise curve of `P_s / D`. Without interference this is the noise curve, without noise the measured interference curve. `p = 1` (`sum`) adds the powers, `p → ∞` is the larger one (`max`). For a profile where interference counts as noise (`--medium-model=em4`, or `--medium-int-curve=[MODE:]noise`), the noise curve of the SINR applies and `--medium-combine` has no effect on it: the server warns at the start for each such profile.

The datasheet's co-channel rejection is measured with the wanted signal 3 dB above sensitivity (§9.1): the noise alone then uses half of the margin, and the interferer must be weaker than its own 10 % point. With equal slopes (`B = b_int`, the default) the SIR for 10 % loss is the interference-only point plus 0 dB (`max`), 0.62 dB (`p = 2`) or 3.0 dB (`sum`). With the kits' 10 % SIR (§9.1: 3.8 dB in mode 3, 50-byte frame; 4.25 dB in mode 1a from its curve):

| | `max` | `p = 2` | `sum` (`p = 1`) | Datasheet (CW interferer) |
| --- | --- | --- | --- | --- |
| Mode 3 | 3.8 dB | 4.4 dB | 6.8 dB | 4.4 dB |
| Mode 1a | 4.25 dB | 4.9 dB | 7.25 dB | 7.1 dB |

Default: `max` with the empirical curve (unchanged), `sum` with the layered curve. `sum` is the standard treatment of independent disturbances. It is within 0.15 dB of the datasheet in mode 1a and 2.4 dB pessimistic in mode 3, where `max` is 0.6 dB and 2.85 dB optimistic. The datasheet uses a CW interferer, not a modulated one as in the kits, so neither mode settles `p`; kit measurements near sensitivity with a modulated interferer would. The combination is also available with the empirical curve (`--medium-combine=sum` alone).

### 12.5 Options and defaults

All are user settings, refused with `--medium=legacy` (central medium only, virtual time).

| Option | Default | Source or reason |
| --- | --- | --- |
| `--medium-rx-model=empirical\|layered` | `empirical` | the radio's field-fitted curve stays the reference (Investigation §5) |
| `--medium-rx-curve=X0,B` | 9.0, 0.9 dB | §12.3: B the kits' measured slope, X0 the fit. Refused without `--medium-rx-model=layered` |
| `--medium-combine=max\|sum\|P` | `max` (empirical), `sum` (layered) | §12.4 |
| `--medium-fading=K,TC_S\|off` | off (empirical); 10, 0.3 s (layered) | K: §12.3. `TC_S` is the lag of field correlation e⁻¹ (§12.1). Nodes are static and the channel changes with people moving: IEEE 802.11 TGn channel models (Erceg et al., IEEE 802.11-03/940r4) use an environment speed of 1.2 km/h, a Doppler `f_d = v / λ` of 1.0 Hz at 915 MHz (0.96 Hz at 868 MHz). For Clarke's spectrum the field correlation `J0(2π f_d Δt)` is e⁻¹ at `Δt = 0.279 / f_d`: 0.27 s at 915 MHz, 0.29 s at 868 MHz, hence 0.3 s. Rappaport's rule of thumb `T_c ≈ 0.423 / f_d` (*Wireless Communications*, §5.4.3) is a different definition (a geometric mean of two criteria) and would give 0.42 s |
| `--medium-fading-bc=MHZ\|inf` | 5.5 MHz | §12.1. Indoor RMS delay spreads of a few tens of ns up to about 100 ns are reported for office and residential buildings (Hashemi, "The indoor radio propagation channel", Proc. IEEE 81(7), 1993). 5.5 MHz is `σ_τ` = 50 ns in this model; Rappaport's rule `B_c ≈ 1 / (5 σ_τ)` (§5.4.2, 0.5 correlation) gives 4 MHz for the same delay spread. Refused without fading |
| `--medium-chan-spacing=KHZ` | from the data rate (§12.1) | Wi-SUN FAN EU channel plans; refused without frequency-selective fading |
| `--medium-shadowing=SIGMA_DB,TAU_S\|off` | off | The mean link value already holds the location's shadowing. How much a static link's level wanders over minutes to hours depends on the building and its activity, and no measured value is available. Illustration in §12.7: 4 dB, 60 s |
| `--medium-node-share=F` | 0 | no source; with F, links sharing a node correlate by F / 2. Refused without shadowing |
| `--medium-reciprocal=on\|off` | `on` | channel reciprocity (§12.1). Refused when nothing varies |
| `--medium-burst=GOOD_S,BAD_S,DB[,link\|rx\|node]\|off` | off | Burst lengths should come from field data, for example sequence-number gaps in real logs (Investigation §5) |
| `--medium-var-dump=RX,TX,FROM_S,STEP_S,COUNT[,shuffle][,ch=N\|hop=N][,rate=BPS]` | none | prints `rx tx t_us shadow_db fading_db bad` for COUNT times from FROM_S every STEP_S (the seed from `WSSIM_SEED`), on channel N (0), or sample `i` on channel `7i mod N`, at rate BPS (150000, for the spacing), in a shuffled query order with `shuffle`, and exits: for tests and plots |

The server prints the settings at the start (`medium: receiver curve ...`, `medium: fading ...`) and, at the end, the number of samples, those in a bad state, the Gaussian pairs drawn and the processes cached.

Options are matched by `getopt_long`, which also accepts unambiguous abbreviations; the new names make some earlier abbreviations ambiguous (`--medium-b` was `--medium-break-db`, now also `--medium-burst`). No script of the emulator uses an abbreviated `--medium-*` option.

### 12.6 Medium log

When a link varies (fading, shadowing or bursts on), each `--medium-log` line has four more columns: `mean_dbm shadow_db fading_db bad`, the link's mean level, `S`, `F` and the burst state (1: bad) at the frame's start. `rx_dbm` (sixth column) is `mean_dbm + shadow_db + fading_db`, less the burst attenuation when `bad` is 1. Without variation the line keeps its seven columns.

### 12.7 Demonstrations

**Loss runs and two parents** (`sim_utils/vtime/demo_link_variation.py --hop`, fake nodes; 36 runs, about 2.5 min of wall time in all, most of it the hopping table): a child sends a 100-byte frame every second for 3600 s; two parents hear it at the same mean level. Totals over seeds 1–3 and both parents (21 600 frames per model). A run is a series of consecutive frames lost at one parent; "both/indep" is the number of frames lost at both parents divided by the number expected if the two losses were independent.

| Model | Mean level | Loss | Runs | Mean run | Longest | Losses in runs ≥ 2 | Lost at both | Both / indep |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| empirical | −98 dBm | 0.82 % | 177 | 1.01 | 2 | 1.1 % | 1 | 1.3 |
| layered (K 10, 0.3 s, 5.5 MHz) | −98 dBm | 0.90 % | 194 | 1.01 | 2 | 1.0 % | 1 | 1.2 |
| layered, shadowing 4 dB, 60 s, node share 0.5 | −98 dBm | 8.34 % | 977 | 1.84 | 88 | 62.0 % | 73 | 1.1 |
| layered, bursts 300 s / 10 s / 15 dB per node | −98 dBm | 7.72 % | 315 | 5.29 | 40 | 89.6 % | 442 | 6.6 |
| layered, shadowing as above | −93 dBm | 0.88 % | 136 | 1.40 | 11 | 37.9 % | 0 | 0 |
| layered, bursts 3600 s / 8 s / 15 dB per node | −98 dBm | 1.69 % | 208 | 1.75 | 18 | 47.8 % | 33 | 10.8 |

Frames one second apart are 3.3 coherence times apart, so the default fading alone leaves losses nearly independent from frame to frame and between parents, as the empirical curve does. Clustering in time and across parents comes from the slow layers (shadowing, bursts). Their values here are illustrations: without field data, the burst and shadowing figures are not predictions.

**Frequency hopping** (same script, `--hop`): the child sends 10 frames a second (0.1 s apart, a third of the coherence time) in mode 3 (150 kbit/s, 200 kHz), 900 s per run, seeds 1–3 (54 000 frames per model at the two parents), the layered model at −98 dBm; on one channel, or hopping over 35 channels in a pseudo-random order that the parents follow.

| Channels | Fading across channels | Loss | Runs | Mean run | Longest | Losses in runs ≥ 2 |
| --- | --- | --- | --- | --- | --- | --- |
| one | — | 0.92 % | 415 | 1.20 | 5 | 28.2 % |
| 35, hopping | flat (`--medium-fading-bc=inf`) | 0.91 % | 411 | 1.19 | 6 | 27.3 % |
| 35, hopping | `B_c` 5.5 MHz (default) | 0.95 % | 458 | 1.12 | 5 | 19.8 % |
| 35, hopping | `B_c` 1 MHz | 0.98 % | 514 | 1.03 | 2 | 6.0 % |

With flat fading, hopping changes nothing: successive frames see the same fade. With frequency-selective fading, hopping spreads successive frames over channels up to 6.8 MHz apart, and the share of losses in runs falls from 27 % to 20 % (5.5 MHz) and 6 % (1 MHz, a delay spread of about 275 ns). The average loss stays the same, as it should: each channel keeps the same Rician distribution.

**Networks still form** (`medium_gate_dag.sh` and `medium_gate_star.sh` with `MODES=central`, seeds 1–3, `SIM_OPTS=--medium-rx-model=layered` against the default; 5 s and 24 s of wall time for the 6 runs of each):

| Scenario | Default (empirical) | Layered |
| --- | --- | --- |
| `dag_simple`, 600 s | 14/14 routers ready in every run, median ready 148, 140, 131 s; frames heard received 98.6 %, collisions 1.34 % | 14/14 in every run, 155, 144, 125 s; 98.5 %, 1.52 % |
| Star, 40 routers in 4 hidden groups, 900 s, requests every 30 s from 300 s | 40/40 ready, median 116, 115, 99 s; frames heard received 92.0 %, collisions 7.5 %; requests received 2272 of 2280, echoed back 2271 | 40/40, 114, 117, 109 s; 91.6 %, 7.8 %; 2280 of 2280, echoed back 2280 |

Wall time of the star runs: 11.6–12.1 s per run (empirical) and 12.1–12.3 s (layered, about 545 000 samples per run).

### 12.8 Checks

- **Off by default:** with none of the options, `nonreg_dag_simple.sh` against `dag_simple_8c5677e` (10 of 10 seeds identical), `medium_gate_star.sh` hidden seed 1 (node logs identical with pointers masked, medium log byte-identical, server log identical apart from the connection order and the wall time, against a build of the commit before this module), `test_conductor.py` (38 tests), `test_sniff.py` (18), `radio_if/test_ref_client.py` (21) and the LFN quick suite (53 runs, before `channel` was added to it) pass.
- `sim_utils/vtime/test_channel.py` (in the LFN suite as `channel`, about 11 s, 15 tests): shadowing (σ within 3 %, autocorrelation 0.37 at τ and 0.05 at 3τ within 0.02, also at other times); short lags at times that are no anchor point (1 − ρ within 5 % of `1 − exp(−Δt/τ)` at 0.002 τ, 0.05 τ and τ/16; the fading power at 0.01 `TC_S`); fading (mean power, K from the moments, shares 3 dB and 6 dB below the mean against the Rician distribution, autocorrelation at the coherence time against the formula, Rayleigh with K = 0); across channels (Rayleigh power correlation of 100 links at 1, 5.4 and 20 MHz against `1/8 + (7/8) / (1 + (2π σ_τ Δf)²)` within 0.07; `inf` identical on every channel); determinism by seed, independence from the query order and from other links' queries; reciprocity on and off; the node share's correlation; bursts (share of time bad, mean sojourns, coefficient of variation about 1, link, receiver and node scopes); through the medium, the measured layered loss rate at −90, −95 and −100 dBm against its average formula (within 4 σ), and at −100 dBm within a factor 2.5 of the empirical curve; the variation in an interferer's power and in CCA energy (30 cases: sensed energy and lowest SINR from the varied powers of the log); links from positions (§11) varying around their path-loss mean; the medium log's variation against the dump, and `rx_dbm` with bursts; a run's determinism; the combination near sensitivity (layered `sum` and `max`, empirical `sum`, each against its formula within 3.5 σ over 2000 frames); the options, the refusals of options without effect and the warning of §12.4.

### 12.9 Limits

- The variation is frozen over a frame: correct for frames much shorter than the coherence time (a 250-byte frame at 50 kbit/s lasts 42 ms against 0.3 s), not for long `"xl"` preambles of a large share of `TC_S`.
- Frequency selectivity uses 8 taps per link: a link's correlation between distant channels does not fall below about 1/8 in power (Rayleigh), and each link's correlation varies around the mean of §12.1. The medium does not know the band: the channel spacing comes from the data rate unless set (§12.1). Shadowing and bursts are the same on every channel.
- The time correlation is exponential (Gauss-Markov), not Clarke's Bessel shape, and linear below `max(1 µs, τ / 16384)`; deep fades of a Rician channel with K = 10 are rare (1.2 % of the time more than 6 dB below the mean).
- Burst and shadowing defaults are off for lack of field data, and the receiver curve's calibration holds for the default fading only (§12.3).
- The combination exponent is not measured near sensitivity (§12.4).

## 13. External interference and spectrum

Interferers that are not emulated nodes, an adjacent-channel table beyond the next channel, capture and break as probabilities, and three additions to sniffing (summed energy, false detections, adjacent channels). Central medium only (the legacy medium refuses these options). **Every feature is off by default:** without these options the server's outputs, the nodes' logs and the medium log are byte-identical to the emulator without this section (checked with `nonreg_dag_simple.sh` against the permanent reference runs, `medium_gate_star.sh` seed 1 in both topologies against a server built from the emulator before this section, `test_mixed.sh` with `SNIFF=1` on seeds 1–3 against the same build, the LFN quick suite, `test_conductor.py`, `test_sniff.py`, `test_propagation.py`, `test_channel.py` and `radio_if/test_ref_client.py`). Code: `app_wssimserver/sim_interf.c` (interferers, option dispatch) and hooks in `sim_medium.c`.

### 13.1 External interferers

`--interferer=SPEC` (repeatable) or `--interferer-file=FILE` (one SPEC per line, `#` comments) defines a source of power in the band that is not an emulated node: another network, a duty-cycled device, a constant noise rise. SPEC is `KEY=VALUE` pairs separated by commas:

| Key | Values | Default | Meaning |
| --- | --- | --- | --- |
| `chan` | `3`, `0-34`, `3/5/9` (lists of numbers and ranges, joined by `/`) | required | the channels it occupies |
| `level` | dBm, or `off` | none | its received power at every node, on each of its channels |
| `at` | `IDS:DBM` (`3:-70`, `10-20:-80`, `5:off`), repeatable | none | the level at these nodes, over `level` and `pos` |
| `pos`, `eirp` | `X/Y[/Z]` (m), dBm | none, 0 dBm | a position and an EIRP: the level at each node comes from the propagation model's hook (below) |
| `pattern` | `const`, `periodic:PERIOD:ON[:PHASE]`, `ge:MEAN_ON:MEAN_OFF`, `burst:ON:MEAN_OFF` (times in µs, or with `us`, `ms`, `s`) | `const` | when it is on (below) |
| `hop` | `on`, `off` | `off` | `off`: on every listed channel at once (a wideband source); `on`: each on-interval on one listed channel, drawn uniformly |
| `kind` | `noise`, `fsk` | `noise` | how it interferes (below) |
| `rate` | bit/s | 150000 | `fsk`: its data rate, which sets its adjacent-channel leakage (the medium's profile of that rate, §2.1) and its dwell for sniffing |
| `pre` | µs | 64 bits at `rate` | `fsk`: the preamble at the start of each on-interval, which a sniff can detect (§13.4) |
| `adj` | `DB/DB/...` (`off` for none) | `fsk`: the medium's table of its rate; `noise`: no leakage | its own attenuation 1, 2, ... channels away |
| `name` | text | `i0`, `i1`, ... | its name in the logs |

**Time patterns.** `periodic`: on for `ON` every `PERIOD`, from `PHASE` (drawn uniformly in the period when not given). `ge`: a two-state Gilbert-Elliott process with exponential on and off times of means `MEAN_ON` and `MEAN_OFF`, starting in its stationary state (duty `MEAN_ON / (MEAN_ON + MEAN_OFF)`). `burst`: on-intervals of fixed length `ON` separated by exponential gaps of mean `MEAN_OFF` (packets at Poisson times, such as a duty-cycled LoRa device: `burst:ON:ON·(1/duty − 1)`). Each interferer draws from its own stream, `mix64(seed + mix64(index + 1) + K)` with its index in the order given, and its on-intervals are generated in time order as the medium needs them: the schedule depends only on the seed and the SPEC, not on the emulated traffic or on the order of the medium's queries. Durations are rounded to the µs, at least 1 µs.

**Levels and positions.** `level` and `at` give received powers directly; a node with no level is not reached. `pos` (and `eirp`) needs node positions (`--positions`, §11): the level at each positioned node is `eirp + G_node − PL − X`, with the path loss `PL` of §11.3 between the position and the node (the same model, walls and floors; the interferer's floor comes from its `z`, as for a node without `FLOOR`), the node's antenna gain, and static shadowing `X` of §11.4: the per-pair term `σ_link` drawn for the pair (interferer position, node), seeded by the run's seed, and the node's own term `σ_node`; no per-direction term, since an interferer only transmits. The interferer's own antenna gain is part of its `eirp`. A node without a position gets `level`, if any. Without `--positions` the server refuses `pos`. `at` entries win over `pos`, which wins over `level`. (Code: `prop_point_dbm()` in `sim_propagation.c`, registered with `intf_set_position_model()`.)

**What it does in the medium.** At a receiver `r` on channel `c`, an interferer contributes its level times its coupling into `c` (1 on its own channels, its adjacent table elsewhere; a wideband source sums its channels). The same rules as for emulated frames (§2, §3, §6):

- **over a frame:** each of its on-intervals that overlaps a locked frame is an interferer of that frame in the segments of §2 (the outcome is `interference` if the frame is lost);
- **lock threshold** (§3 rule 2): its power at the frame's start is in `I(t)`;
- **CCA** (`"xs"`): its power at that time is in the energy;
- **sniffing:** §13.4.

**Which curve.** `kind=fsk` (another network of the band's own 2-FSK modulation) is in the SIR of the interference curve, as an emulated frame: the measured curve of §2.1, or, when a presync curve is set (`--medium-capture-model=smooth`, §13.3) and the interferer was on before the frame's SHR ended, the presync curve. `kind=noise`, the default, adds to the noise: `BER = max(BER_noise(P / (N + E_noise)), BER_int(P / I_fsk))` per segment. The measured FSK curve is specific to a co-channel FSK interferer (`COCHANNEL_RESULTS.md` §4.1); a LoRa chirp, an OFDM signal or wideband noise has no such measurement, so it is treated as a rise of the noise floor, which needs about 10 dB of SINR for 50 % of 26-byte frames against about 2 dB for an FSK interferer: the conservative choice until a kit measurement replaces it. A CW tone at the channel centre was measured 1.5–2 dB less harmful than a modulated FSK interferer (§4.3), so `kind=fsk` is a closer model for a narrowband carrier. With the layered receiver curve (§12.3, §12.4), the noise-like power joins the noise term and the FSK power the interference term of the combination. External levels are fixed: the link variation of §12 (fading, shadowing, bursts) does not apply to them.

**Channels.** The medium does not know the PHY's channel plan: a channel beyond it is accepted and simply never meets an emulated frame. A channel given twice in one `chan` is refused.

**Logs.** With at least one interferer, each medium-log line gets one more column after the seven of §7 and, when links vary, the four of §12.6: the highest external power at the receiver on the frame's channel during the frame (dBm), `-` if none. `--interferer-log=FILE`: one line per on-interval as it is generated, `start_us end_us name chan` (`all` for a wideband source), in time order for each interferer; it may run past the run's end. At the start the server prints each interferer; at the end, its bursts and measured duty up to the run's end (the virtual time reached), and the number of channel-energy queries (`"xs"`) made while an interferer was on.

### 13.2 Adjacent-channel table

`--medium-adjacent-table=[MODE:]DB/DB/...` sets, per profile (§2.1), the attenuation of a frame 1, 2, ... channels away; `off` for none at that distance; nothing beyond the last entry. It replaces the single next-channel value: `--medium-adjacent-db=[MODE:]DB|off` sets the table's first entry (or creates a one-entry table), so earlier command lines keep their meaning, and the later of the two options wins for that entry. The table applies wherever the next channel did: interference over frames, the lock threshold, CCA, and an `fsk` interferer's leakage (§13.1); for sniffing, §13.4.

| Distance | Default (fg25) | Source |
| --- | --- | --- |
| 1 channel | 10 dB | `COCHANNEL_RESULTS.md` §4.4: a modulated interferer on the next channel acts as a co-channel one 10 dB weaker (mode 3) |
| 2 channels and more | none (no coupling) | §4.4: two channels away, no loss down to the largest ratio the kits could make, W/I = −29.8 dB; with the interference curve (99 % received at SIR +5 dB) this means at least about 35 dB of attenuation. Datasheet (EFR32FG25 Rev. 1.2, Table 4.30, 866 MHz Wi-SUN FSK, CW interferer, wanted signal 3 dB above sensitivity): alternate channel 47.2 dB (mode 3 with FEC), 45.1 dB (mode 1a); adjacent 45.5 dB and 45.7 dB |

The default beyond the next channel stays "none" so that existing runs do not change. Where strong sources two channels away matter (a close interferer, a dense building), `--medium-adjacent-table=10/40` is the suggested setting: 40 dB lies between the kits' lower bound and the datasheet's CW figure, since a modulated interferer's spectrum spreads further than a tone's. The datasheet's CW figures are receiver selectivity: they suit a narrowband external interferer (`adj=45.5/47.2`), not a frame of the same PHY.

### 13.3 Probabilistic capture and break, and the presync curve

`--medium-capture-model=[MODE:]step|smooth` (default `step`, the rules of §3). With `smooth`, when a frame `F` starts while the receiver holds `A` on the same channel, with `margin = P_F − P_A` (dB), `d` the time since the start of `A`'s own SHR in bits, `Tp` and `Tshr` its preamble and SHR in bits:

- capture weight `w(d) = max(early, sfd)`: `early = 1 − logistic((d − E) / w_e)`, `E = early + slope · max(0, margin − 5)` within `[0, Tp]`; `sfd = logistic((d − Tp − rise) / w_e) · (1 − logistic((d − Tshr − fall) / w_e))` for a profile that captures during the SFD (mode 3), else 0; during an `"xl"` preamble `w = 1`;
- `P(capture) = w(d) · logistic((margin − m50) / s)`;
- `P(break) = (1 − w(d)) · logistic((margin − break) / brk_s)` while `d < Tshr` (with the break margin of §2.1);
- one draw from the receiver's own capture stream (separate from its verdict stream) decides capture, break or neither; none when both probabilities are below 10⁻⁹, or capture is certain. After the windows' last edge (`d ≥ Tshr + fall + 8 w_e`) nothing is drawn.

It also sets, unless `--medium-int-curve-presync` says otherwise, the **presync curve**: the interference curve of everything that was already on air when the received frame's SHR ended, `BER = 0.5 · exp(−(SIR − x0) / b)` with its own `x0`, `b`. This covers the weaker frame that overlaps a captured one, a frame that starts during the SHR without capturing it, and an external `fsk` interferer that is on at that time, constant or not (§13.1). What starts after the SHR keeps the measured curve of §2.1, so the locked-frame measurements (`COCHANNEL_RESULTS.md` §4.1) are unchanged. With mixed interferers in a segment, the presync curve applies only when all of the FSK power there was on air before the SHR's end. `--medium-int-curve-presync=[MODE:]X0,B|same` sets it with either capture model. `--medium-capture-smooth=[MODE:]KEY=V,...` overrides the other parameters (`m50`, `s`, `brk_s`, `early`, `slope`, `w`, `rise`, `fall`).

**The fit.** `sim_utils/vtime/capture_fit.py` fits the model to every capture point the kits measured: `capture_kit_points.json`, 256 power pairs (24 911 cycles) from the raw data of `COCHANNEL_RESULTS.md` §4.2 and §5, each with the share of the first frame and of the second frame received. It writes the model in closed form for two frames, by maximum likelihood on both shares. The kits' SIR axis has an error of its own (§2 there: points of the same nominal SIR differ by up to about 1 dB); near 0 dB it makes points contradict each other (mode 3, 300 µs, SIR −0.1 dB: the first frame received in 0 of 80 cycles with one pair of TX powers and in 275 of 301 with another; mode 1a, same start: 0 of 67 at −0.3 dB, 161 of 206 at +0.1 dB). The fit therefore integrates the model over a normal SIR error whose standard deviation it fits too (0.72 dB); the medium then applies the model at the exact SIR. The margin's centre is constrained to at least 0 dB (a weaker frame does not win more often than a stronger one).

| Parameter | Mode 3 | Mode 1a | Source |
| --- | --- | --- | --- |
| `m50`, `s` (capture against margin) | 0.99 dB, 0.49 dB | 0 dB (the bound), 0.85 dB | the fit |
| `early`, `slope`, `w` | 38.0 bits, 0, 1.59 bits | 12.55 bits, 3.94 bits/dB, 4.64 bits | the fit; mode 3: 53–66 % at 250 µs (37.5 bits) at every margin; mode 1a: the window grows with the margin (+5.5 and +11 dB scans) |
| `rise`, `fall` (SFD window) | −4.53 bits, +0.75 bit | (no SFD window) | the fit (mode 3: 13–44 % at 600 µs, 97–100 % at 650–700 µs, 47–64 % at 750 µs) |
| `brk_s` | 0.5 dB | 0.5 dB | assumed: the break was measured only from +5 dB (both lost at 300–550 µs) |
| presync `x0`, `b` | −2.72 dB, 0.54 dB | the same | the fit, one curve for both PHYs |

**Why a presync curve.** With the curve of §2.1 (`x0` −2.85 dB, `b` 0.90 dB) applied to the weaker frame that overlaps a captured one, the emulator cannot reach the kits' capture results at small margins: a 50-byte frame captured at +2.7 dB would survive in 66 % of cases against 89 % measured (100 µs). The fitted presync curve is steeper (`b` 0.54 dB) and less harmful: at SIR 0 dB a 50-byte frame survives in 26 % of cases (0.02 % with the curve of §2.1), at +1.35 dB in 90 % (15 %). It is a fit to end-to-end capture results, not a separate measurement; a physical reading is that an interferer present while the receiver sets its gain and frequency estimates harms less than one that appears afterwards.

**Continuous interferers.** The same curve matches the kits' measurement with a continuous PN9 interferer (`COCHANNEL_RESULTS.md` §4.3, 50-byte frames, 200 per point), which was not used in the fit; SIR = W/I + 1 dB (the kits' W/I axis reads about 1 dB low, §3.2 there). `test_interf.py` (`smooth_continuous`) checks it with a constant `kind=fsk` interferer:

| W/I (kit) | Kit received | Smooth (presync curve) | Step (curve of §2.1) |
| --- | --- | --- | --- |
| −0.9 dB | 24 % | 36 % | 0 % |
| +0.3 / +0.4 dB | 86 % / 69 % | 87 % | 14 % |
| +1.4 dB | 98 % | 98 % | 58 % |

So, with the smooth model, a constant or long FSK interferer that is on before a frame's SHR ends is judged by the presync curve, and a frame survives it at a few dB less SIR than with the step model; this matches the continuous measurement better. With the step model (the default), every FSK interferer uses the curve of §2.1.

**Against the kits.** Residuals of the fit, that is on the points it was fitted to (`sim_utils/vtime/capture_vs_kits.py build-vt/wssimserver`: every point through the emulator, 100 pairs per point and model, about 9 s; the mean absolute difference between the emulator's share and the kit's, averaged over the first and the second frame):

| | All 256 points | The 32 points in a transition (kit between 5 and 95 %) | Points more than 20 points away |
| --- | --- | --- | --- |
| Step model | 5.1 points | 25.8 points | 31 |
| Smooth model | 2.6 points | 10.4 points | 13 |

**Validation:** 5-fold cross-validation in `capture_fit.py` (each fifth of the points predicted by a fit made without it, with the SIR error): 3.7 points (residual of the full fit with the SIR error: 2.9 points; the step model: 5.0). About 50 s of wall time. Selected points (kit, step, smooth; first / second frame where both matter):

| Case | Kit | Step | Smooth |
| --- | --- | --- | --- |
| Mode 3, same start, second +0.3 / +2.2 dB | 79 / 95 % | 0 / 0 % | 16 / 87 % |
| Mode 3, same start, equal TX powers (measured +1.4 dB): both lost | 0 % and 0 % | 0 % and 0 % | 63 % (second) |
| Mode 3, 100 µs, second +0.6 / +0.9 / +2.7 dB | 15 / 58 / 89 % | 0 / 0 / 0 % | 25 / 39 / 96 % |
| Mode 3, 250 bytes, 100 µs, +1.1 / +3.7 dB | 43 / 96 % | 0 / 51 % | 24 / 99 % |
| Mode 3, 700 µs, second +0.3 dB / +0.4 dB (first received) / +1.7 dB | 14 % / 60 % / 72 % | 0 / 0 / 0 % | 18 % / 1 % / 75 % |
| Mode 3, 300 µs, first stronger by 2.4 dB | 100 % | 59 % | 97 % |
| Mode 3, 300 µs, SIR −0.1 dB, two pairs | 0 % and 91 % (first) | 0 % | 36 % |
| Mode 3, second +4.6 / +10.2 / +21.4 dB at 250 µs | 53 / 66 / 62 % | 0 % | 61 % |
| Mode 3, second +5.8 / +12.0 / +22.7 dB at 600 µs | 44 / 25 / 13 % | 0 % | 36 % |
| Mode 1a, same start, −1.1 / −0.3 dB (both lost) | 0 / 0 % (first) | 0 / 0 % | 56 / 26 % (first) |
| Mode 1a, same start, +0.1 / +1.3 dB | 78 / 100 % | 0 / 0 % | 18 / 65 % |
| Mode 1a, +5.2 / +11.1 dB at 400 µs | 21 / 100 % | 0 / 0 % | 29 / 98 % |
| Mode 1a, +10.3 / +11.5 dB at 600 / 800 µs | 94 / 0 % | 0 / 0 % | 71 / 43 % |

The remaining large differences are near 0 dB, where the kit points contradict each other within their SIR error, and at the time edges where the margin changes the window (mode 3 at 600 µs, mode 1a at 800 µs) in a way the model's single edge does not follow. At exactly equal powers the model does not give "both lost" either: in mode 3 with the same start, the first frame is received in about 23 % of cases and the second in about 3 %.

### 13.4 Sniffing: adjacent channels, summed energy, false detections

Three options of the sniffing model of §10, each off by default:

- **`--sniff-adjacent=off|on|detect`.** `on`: the frames on other channels whose power leaks onto the sniff's channel (the table of §13.2) take part in the sniff as interference (in each candidate's SINR) and in the energy (the result at the window's end, the energy detector). `detect`: they are also candidates for detection at the power that leaks, reported with their `frame_id` and sender, never locked onto (the receiver cannot receive a frame on another channel).
- **`--sniff-energy=DBM|off`, `--sniff-energy-us=N`** (107 µs: 16 bits at 150 kbit/s, the default dwell in mode 3; no datasheet value). An energy detector next to the preamble detector: it detects at the first time `t` at which the total energy on the channel (noise, the frames heard, adjacent frames with `--sniff-adjacent`, external interferers) has stayed at or above DBM over `[t − N, t)`, `t` at least N after the window's start. It catches what the preamble detector cannot, such as a collision of frames each too weak, or an external interferer. The result names the strongest emulated frame on the channel at `t` (`rssi_dbm` its power, `sinr_db` its lowest SINR over the averaging time), and with `SIM_SNIFF_LOCK` locks onto it by the rules of §10.3; with no such frame, `frame_id` 0, `src` 0xFFFF and `rssi_dbm` the total energy. No draw: the energy is exact, the RSSI's own error is not modelled.
- **`--sniff-false=P|RATE/s`.** A false detection on noise: with probability P per sniff window, at a time drawn uniformly in `(t0, t1]`; or at a rate per second of listening (Poisson, the first arrival after `t0`). Drawn at the request, from the node's own false-detection stream. If no real detection comes first, the result has `detected` 1, `frame_id` 0, `src` 0xFFFF, `rssi_dbm` the energy at that time. The receiver stays on (LISTEN in the ledger) until the client reports otherwise, as a real detector that waits for a sync word until its timeout; the cost is the client's timeout. A receiver locked onto a frame at that time does not trigger. Default 0: the FG25 datasheet gives no false-detection figure (§10.5); Investigation §5 (P4) estimates that one false wake-up per second adds about 12.8 µA to a sleeping node's budget.

External interferers (§13.1) take part in every sniff on their channels: as interference and energy, and for `kind=fsk` as a candidate during the preamble at the start of each on-interval (`pre`), detected with `frame_id` 0 and `src` 0xFFFF, never locked onto. A noise-like interferer is never a preamble. The sniff log is unchanged; the server's end-of-run summary adds, when any of these features is on, the detections by energy, false ones, those of an external interferer and those of a frame on another channel. The `"xd"` message is unchanged; `frame_id` 0 with `detected` 1 is new ([RADIO_INTERFACE.md](RADIO_INTERFACE.md) §4.13). A sniff takes the external intervals one second of its window at a time and drops the entries that can no longer matter, so that a long window with a busy interferer stays cheap (a one-hour sniff with a 2 ms burst every 10 ms on average, about 360 000 intervals: 0.2 s of wall time).

### 13.5 Options

| Option | Default | Section |
| --- | --- | --- |
| `--interferer=SPEC`, `--interferer-file=FILE` | none | §13.1 |
| `--interferer-log=FILE` | none | §13.1 |
| `--medium-adjacent-table=[MODE:]DB/DB/...` | fg25: 10 dB on the next channel only; em4: none | §13.2 |
| `--medium-capture-model=[MODE:]step\|smooth` | `step` | §13.3 |
| `--medium-capture-smooth=[MODE:]KEY=V,...` | the fit of §13.3 | §13.3 |
| `--medium-int-curve-presync=[MODE:]X0,B\|same` | `same` (`smooth`: −2.72 dB, 0.54 dB) | §13.3 |
| `--sniff-adjacent=off\|on\|detect` | `off` | §13.4 |
| `--sniff-energy=DBM\|off`, `--sniff-energy-us=N` | `off`, 107 µs | §13.4 |
| `--sniff-false=P\|RATE/s` | 0 | §13.4 |

### 13.6 Network-level demonstration

`sim_utils/vtime/interf_demo.sh build-vt $(pwd) <outdir>`: the hidden star of §8 item 4 (a border router and 40 routers in 4 mutually hidden groups, a UDP request to the border router every 30 s from 300 s), 3 seeds × 900 s, central medium, without and with an interferer; `medium_stats.py` compares the two groups. 48 s of wall time for the 6 runs, 3 at a time.

| Figure (3 seeds) | Without | With a noise-like source on channels 0–9 of 35, on 200 ms every second, −70 dBm at every node | With another FSK network hopping on all 35 channels, 4 ms bursts with 16 ms mean gaps, −65 dBm at every node |
| --- | --- | --- | --- |
| Frames received / heard (medium log, all receivers) | 92.01 % (92.49, 91.64, 91.88) | 90.57 % (91.14, 90.86, 89.72) | 91.69 % (91.47, 91.73, 91.85) |
| Lost by interference, per run | 667 | 994 | 849 |
| Collision share (interference + captured + busy) | 7.49 % | 8.71 % | 7.82 % |
| Frames heard at the border router with the interferer on, per run (of about 6 200–6 600) | — | 145–216, of which 41–68 received | 238–245, of which 106–130 received |
| UDP requests received by the border router (from 300 s) | 99.65 % | 100.00 % | 99.82 % |
| Routers ready, median ready time per run | 40/40; 116, 115, 99 s | 40/40; 108, 111, 111 s | 40/40; 121, 118, 108 s |

With these levels the interferers cost 0.3–1.4 points of frame delivery and 27–49 % more interference losses, which the MAC's retries absorb: the requests' delivery and the join times do not change significantly (Mann-Whitney on the pooled join milestones: p = 0.07–0.92 for authentication, operational and ready; parent selection p < 0.001 with the noise-like source, whose slowest router took 32.7 s against 16.6 s). The share of the border router's frames that met the duty-cycled source (2.2–3.5 %) is below its share of channel-time (20 % on 10 of 35 channels, 5.7 %), for two reasons: the senders' CCA sees the −70 dBm source and defers off the busy channels while it is on (with the source at −100 dBm, below every CCA threshold, 29.8 % of the frames sent during its on-phase meet it, against 28.6 % expected), and the traffic is partly phase-locked to the source's 1 s period. These are effects of the scenario's levels, not general figures.

### 13.7 Checks

`sim_utils/vtime/test_interf.py build-vt/wssimserver` (fake nodes, in the LFN suite as `interf`, about 3 s, 17 tests): option errors and start-up lines; CCA energy from constant interferers (own channel, FSK leakage by the medium's table, its own `adj`, per-node levels, wideband); a periodic pattern's edges and the interferer log; burst and Gilbert-Elliott duty over 200 s and the bursts' count, determinism by seed, a hopping source's channel use; reception against the formula (noise-like and FSK, 200 frames, within 4 standard deviations), the lock refused by an interferer, the medium log's eighth column; an interferer over part of a frame (segments); the adjacent-channel table (CCA, reception, its interplay with `--medium-adjacent-db`, PHY prefixes); smooth capture and break rates against the model's probabilities at 9 points of modes 3 and 1a, determinism; the presync curve against the formula, and unchanged for an interferer after the SHR; sniffing: an FSK interferer's preamble detected (`frame_id` 0), a noise-like one in the energy and the SINR, the energy detector (summed frames, an interferer, a frame on air at the start, a lock), false detections (per window and per second: counts within 4 standard deviations, times spread over the window, determinism), adjacent channels (`on`, `detect`, the energy at the end). levels from positions (free-space path loss and gains against the formula, shadowing by seed, its spread over 40 positions); the smooth model with a continuous FSK interferer against the kits' PN9 points; a one-hour sniff with a busy interferer (bounded time) and the statistics at the run's end; a 10 000-character line in an interferer file and a channel given twice. `capture_fit.py` (fit and cross-validation) and `capture_vs_kits.py` (every kit point through the emulator) are in §13.3.

### 13.8 Limits

- Interferers do not react to the emulated network (no listen-before-talk, no acknowledgements) and have fixed levels per node: no fading (unless a time-variation model adds it to the same hook) and no frequency offset within a channel.
- `kind=noise` is an assumption for non-FSK interferers (conservative, §13.1); LoRa, OFDM and wideband noise against the FG25 have not been measured.
- The smooth capture model fits end-to-end kit results with a presync curve that is itself fitted, not measured; the break below +5 dB, the capture timing at other margins, other frame lengths and every PHY but modes 3 and 1a are extrapolations (the `other` profile takes the mode-3 values). The mode-1a early window is constant below 5 dB of margin and grows linearly above, fitted on the two measured margins (+5.5 and +11 dB); elsewhere it is extrapolated.
- The adjacent-channel table beyond the next channel has no default (no coupling); the suggested 40 dB is between a lower bound from the kits and the datasheet's CW figure.
- Sniffing: the energy detector has no RSSI error and no averaging filter shape (a hard window), and false detections have no measured rate: both are placeholders for kit measurements (§10.6). The detection of an FSK interferer's preamble uses the dwell of its own rate and the default curve, as for an emulated frame.
- An interferer's on-intervals that ended more than 60 s before the latest time the medium has seen are freed. Queries start at the latest time, at the start of a frame being locked onto, or at a sniff's current step (§13.4): only a frame on air for more than 60 s (a long `"xl"` preamble) would see fewer intervals than exist.
