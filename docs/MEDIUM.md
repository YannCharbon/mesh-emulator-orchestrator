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

- **Received power.** `P = link(rx, tx)` from the `-g` options (and `--vtime-link`), as today: the value is the received power, whatever TX power the sender uses. With `--medium-tx-ref=DBM`, the value is the received power when the sender transmits at DBM, and `P = link + (tx_dbm − DBM)`, with `tx_dbm` from the frame header: the TX power the MAC last set (`wsnode` and `wsbrd` set it from their `tx_power` setting at start; a radio whose MAC never set one reports 0 dBm). An edge without a value (0 dBm) stays a perfect link; it is also a 0 dBm interferer and 0 dBm of CCA energy, so two frames over such links always destroy each other and neither can capture the other. Give RSSIs to the edges when collisions matter. Pairs that are not graph neighbours do not hear each other at all: no reception, no interference, no energy in CCA.
- **Links and their direction.** `link(rx, tx)` is directional: the power at `rx` of `tx`'s frames. The `-g` options (and `--group-file`, one option per line) set it, in order:
  - `-g A,B:RSSI` (exactly two IDs) sets `A → B` (heard by B) to RSSI, and also `B → A`, unless an option `-g B,A:RSSI` gives that direction itself, before or after. A link given once is therefore the same in both directions, as before 2026-11-09, and a reverse option makes it **asymmetric** (different TX powers, noise or antennas at the two ends): `-g 0,1:-70 -g 1,0:-80` is −70 dBm at node 1 and −80 dBm at node 0. Giving both directions with the same value is the same as giving the link once.
  - Any other list, a range (`0-3:RSSI`, even `0-1:RSSI`) or more than two IDs (`1,2,5:RSSI`), sets every pair of its nodes, both directions.
  - Options apply in order and the later one wins. A later option that changes a value an earlier option set (not only as the reverse of a link) is a warning in the server's log; the log also counts the asymmetric links. `-l`/`--dump` prints the map, one row per receiver.
  - Who hears whom (the graph) stays symmetric: a link exists in both directions or in neither. A one-way link is a reverse value so weak that nothing is received (e.g. −150 dBm), or `--vtime-cut`. `--vtime-link` changes both directions.
  - Before 2026-11-09, a second option for the same pair overwrote both directions (`-g 0,1:-70 -g 1,0:-80` gave −80 dBm both ways): only topologies that gave the two directions different values read differently now; none of the emulator's scripts and tests does.
- **Noise floor.** `N = −119.93 dBm`, the constant of today's curve (`kTB` at 293 K and 100 kHz, NF 4 dB: `−153.93 + 30 + 4`).
- **Error model.** Today's empirical curve, with the SINR in place of `P − N`: `BER = min(0.5, 0.5 · exp(−0.5 · SINR_dB))`, `SINR = P / (N + I)`, `I` the sum of the interferers' powers in mW. Without interference this is exactly today's formula. The counted bits are those of today's draw: `8 · (PHR + PSDU + FCS)`. Treating interference like noise makes collision losses pessimistic (§9, "Known bias").
- **Over a frame.** The frame `[t_start, t_end)` is cut into segments wherever an interferer starts or ends. Each segment `k` holds a share of the counted bits proportional to its duration, `bits_k`, and has its own `SINR_k`. `P_success = Π_k (1 − BER_k)^bits_k`. One uniform draw from the receiver's stream decides; draws are made only when `0 < P_success < 1`.
- **Interferers** of a frame at receiver `r`: every other frame on the same channel whose sender is a graph neighbour of `r`, overlapping in time (end exclusive: a frame ending at `t` does not overlap one starting at `t`). Frames on other channels do not interfere (adjacent-channel leakage is a later phase).
- **Airtime** at the medium: `bytes = shr_len + phr_len + len + fcs_len`, `airtime_us = bytes · 8 · 10⁶ / rate_bps` with integer division, from the frame header. This is the sender's own formula (`rf_airtime_us()`), so the sender's TX end and the medium's frame end coincide.
- **Defaults** (chosen, documented, all settable): lock threshold `--medium-lock-snr=0` dB, capture margin `--medium-capture-db=6` dB, capture window = the SHR airtime of the frame being received (preamble and SFD: once the sync word is found, the receiver no longer switches).

## 3. Reception at a receiver

Each receiver `r` has, in the server, at most one **lock** (the frame it is receiving). At the start `t` of a frame `F` from `s` on channel `ch`, for each graph neighbour `r` of `s` (except cut links and `--vtime-drop`ped frames, which are neither received nor interference):

1. **Not listening:** `r`'s radio state in shared memory (§5) is not `SIM_RADIO_LISTEN` or `SIM_RADIO_RECEIVE`, or its channel is not `ch`. No lock; `F` still interferes with whatever `r` receives later (it does not, since `r` is not receiving: nothing to do).
2. **Listening, no lock:** `r` locks onto `F` if `P_F − 10·log10(N + I(t))` ≥ lock threshold, `I(t)` being the frames already on air at `r` on `ch`. Otherwise `F` is only interference.
3. **Receiving `A`:** if `t − t_start(A)` < SHR airtime of `A` and `P_F ≥ P_A + capture margin`, `r` drops `A` (**captured**) and locks onto `F`. Otherwise `F` is interference to `A`.
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
- `--medium-tx-ref=DBM`, `--medium-lock-snr=DB`, `--medium-capture-db=DB` (refused with `--medium=legacy`).
- Preamble sniffing: `--sniff-detect`, `--sniff-dwell-bits`, `--sniff-threshold`, `--sniff-curve`, `--sniff-sfd-bits`, `--sniff-log` (§10.5; refused with `--medium=legacy`).
- `--medium-log=FILE`: one line per (frame, receiver) outcome: `t_end_us src dst frame_id outcome rx_dbm sinr_min_db`, outcome one of `ok`, `noise` (lost, no interferer), `interference` (lost with an interferer), `captured`, `below_lock` (heard, not locked: too weak against the noise and interference), `busy` (heard while receiving another frame and not captured), `not_listening`, `tx_abort`, `left`.
- At the end of the run, a summary line per outcome (totals), and the CCA query count with the number of queries that found a frame on air. The server does not know the radios' CCA thresholds: the busy counts are in the radios' exit lines (`rf medium: N CCA, N busy`).
- Random: the verdict draws use a stream per receiver, `mix64(seed + mix64(node ID + 1) + K)` with a constant `K` that separates it from the port's and the drift's streams.

## 8. Checks (the EM4 gate)

1. **Legacy is unchanged:** `sim_utils/migration/nonreg_dag_simple.sh` against `reference-runs/dag_simple_b1b9cc5`: identical logs, 10 seeds × 300 s. The LFN suite (`sim_utils/vtime/lfn_suite.sh`) passes.
2. **Server unit tests** with fake nodes (`sim_utils/vtime/test_conductor.py`): two overlapping frames at one receiver (the second is interference, the first lost or kept by its SINR); capture inside and outside the SHR window; hidden terminal (A and C not neighbours, both to B); half duplex; channel mismatch; not listening; verdict before a same-time start; CCA energy from a sender that is a graph neighbour but not in the receiver's MAC neighbour table (which today's CCA misses); without interference, the loss rate at a few RSSI values equals today's curve; determinism by seed. Since the review of the gate: the bits shared over segments (an interferer over a quarter of the frame, at its end and at its start, the loss rate against §2's formula), a lock refused by the interference already on air, a frame on another channel during and before a lock, cut links and `--vtime-drop` left out of interference and CCA energy, capture exactly at the margin and just below, a receiver stopped and restarted while it holds a frame. The radio's side (§6) with real nodes: `sim_utils/vtime/test_medium_radio.sh` (in the LFN suite) checks the medium log against the radios' logs frame by frame, their exit counters, and every CCA busy answer, in a run with collisions and captures. The radio's path without a verdict (`left`) with a real LFN: `sim_utils/vtime/test_lfn_left.sh` (in the LFN suite): a window that closes during a frame waits for it (the MAC defers the close while the radio receives), and the MAC's ACK timeout during an overheard frame turns the radio off, the radio drops the frame, the server logs `left` and the ledger charges RX up to the drop.
3. **Central against legacy, light traffic:** `dag_simple`, 6 seeds × 600 s per mode (`sim_utils/vtime/medium_gate_dag.sh`): the group medians of routers ready and of the join milestones close to legacy, and no significant difference in the pooled milestones (`medium_stats.py`, Mann-Whitney). A single run outside the other group's range is not a failure: with 6 runs per group this happens by chance. `dag_simple` has no data traffic, so delivery is judged in item 4.
4. **Where it matters:** `sim_utils/vtime/medium_gate_star.sh`, a border router and 40 routers in its range, in four groups that do not hear each other (`hidden`) and, as a control, all hearing each other (`clique`), with UDP requests to the border router: interference, captures and busy outcomes appear at the border router, more interference in `hidden` than in `clique`, CCA busy counts (from the radios' exit counters; the server only knows how many queries had a frame on air) higher in `clique`; the effect on delivery and join times is reported against legacy.
5. **LFN suite in central mode:** passes, or each difference is explained.

The gate passed: the central medium is the default with `--vtime`. What compares with stored runs passes `--medium=legacy`: `sim_utils/migration/nonreg_dag_simple.sh` (the permanent reference runs), the legacy groups of `medium_gate_dag.sh` and `medium_gate_star.sh`, and the fake-node tests of the conductor's own frame relay in `test_conductor.py`. The LFN suite runs in each mode with one command: `lfn_suite.sh` in the default (central), `medium_lfn_suite.sh` in legacy, through a wrapper of `wssimserver` that also reaches the tests that start it with options of their own (`test_lfn_mac.sh`, `test_lfn_keyreq.sh`).

## 9. Not in the MVP

**Known bias: collision losses are pessimistic.** Interference is added to the noise and goes through the same empirical curve (§2), which is steep and far from the radio's behaviour against a co-channel signal. On a frame of 50 bytes of PSDU (448 counted bits), the curve gives 1 % loss at 20 dB of SINR and 78 % at 10 dB; on 250 octets, the datasheet's test length, 10 % loss needs 18.4 dB. The EFR32FG25 datasheet (root repo `documents/pre-study/datasheets/efr32fg25-datasheet.pdf`, Rev. 1.2, Table 4.30 "866 MHz Band RF Wi-SUN FSK Receiver Characteristics", p. 93) gives a co-channel rejection (U/D ratio for 10 % PER, desired signal 3 dB above sensitivity, 250 octets) of −7.1 dB in mode 1a and −7.2 dB in mode 2a without FEC, −4.4 dB in mode 1a, −5.2 dB in mode 2a and −4.4 dB in mode 3 with FEC: the radio still receives with an interferer only 4.4–7.2 dB weaker than the frame, about 11–14 dB less margin than the curve asks for. Consequences: overlapping frames are lost in the emulator where a real receiver keeps the stronger one, and a frame that captures the receiver (§3, 6 dB margin by default) is received reliably only if it is about 20 dB above the frame it displaced, since that frame stays on air as interference while they overlap. Collision figures (losses, retries, delays under load) are therefore upper bounds. The datasheet's interferer is a CW tone, not an FSK frame, so its figures are not used as they are: the co-channel, capture and adjacent-channel measurements on the FG25 kits (EM5, in progress) will give a curve for FSK-on-FSK interference that replaces this one. Collision-free results (no overlapping frames) are not affected.

Adjacent-channel rejection, fading and shadowing, bursts of losses, external interferers, node positions and path-loss models, RX↔TX turnaround and settling times, a reported RSSI with its own error (Investigation §5, P4–P11 and the layered loss model). The radio still decides its own TX timing; the medium does not delay or refuse a transmission. A frame that will be lost keeps its receiver in RX for its whole airtime, as on a real radio (legacy dropped it at its start), so central runs show more RX time.

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
- **Probability of one try.** 0 if `P_F` is below `--sniff-threshold` (off by default); otherwise `curve(SINR_min)`, `SINR_min` the lowest SINR of `F` over the dwell, `P_F / (N + I)` with `I` the sum of the other candidates on air (it is lowest at the dwell's start or where an interferer starts). Default curve `ber:16`: `(1 − BER(SINR))^16`, `BER` the medium's error curve (§2), that is, 16 preamble bits received without an error. A table `--sniff-curve=SINR:P,SINR:P,...` (dB, ascending) replaces it: linear between the points, the end values beyond them.
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
- Adjacent channels do not detect (as for reception, §9).
- Sniffing works in virtual time with the central medium only.

### 10.7 Checks

`sim_utils/vtime/test_sniff.py` (fake nodes, in the LFN suite as `sniff`): detection of a frame on air at the expected time and power; an empty channel (noise floor, one wake); the threshold; partial overlap at both edges of the window, with both detection modes; an interferer (SINR against the formula, no detection at equal power); the wrong channel; half duplex (deaf during its own frame, a frame sent during the window ends the sniff); one-dwell detection rate at the margin against the curve, and determinism by seed; a table curve (no draws at 0 and 1); a 100 ms `"xl"` preamble detected and locked onto mid-preamble, its `"xx"` and verdict, against a radio that only listened from the start, one that turned on late without a sniff, and a sniff without the lock flag; the lock rules (no lock after the SFD, an existing lock reported, bits counted from the lock, the lock threshold against the frames still on air only, no detection of another frame while locked); the ledger (listening during the window, receiving after the lock, the TX time kept when the sniff is asked during the node's own frame); the settling time; many frames joining a window (one chain of check events); option errors.
