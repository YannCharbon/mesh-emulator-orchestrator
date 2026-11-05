# Central radio medium (EM4): design for implementers

Plan and rationale: the root repo's `documents/pre-study/investigation/Investigation_Nanostack_Emulator.md` §5 (P2, "Target architecture") and `STATUS.md` EM4. This note fixes the interfaces of the MVP so that the server side and the radio side can be built in parallel. Scope: **virtual time only** (`wssimserver --vtime`, in-process nodes on the simulation port). Wall-clock mode and the split programs keep today's behaviour.

## 1. What changes

Until EM4, each radio decides reception alone: the server relays a frame to the sender's graph neighbours at the sender's TX start, and the receiver draws a loss from its link's RSSI (`phy_rf_rx_now()`), keeps the first frame and drops any frame that arrives while it receives, and its CCA looks at the state of the neighbours in its own neighbour table (shared memory `/wssimcca`).

With the central medium (`wssimserver --medium=central`):

- **The server decides every physical outcome:** the power each receiver gets, whether the receiver locks onto the frame, capture by a stronger frame, the interference over the frame, and success or loss at the frame's end (one draw per frame and receiver, from the server's seeded streams).
- **The radio keeps its own state machine** (off, listening, receiving, CSMA, sending) and reports it in shared memory, as today. It no longer draws losses, and its RX end comes from the server's verdict, not from its own airtime timer.
- **CCA asks the server** for the energy on the channel at that moment, and compares it with the CCA threshold the MAC set.

`--medium=legacy` (the default until the EM4 gate passes, §8) is today's behaviour, byte for byte: the nodes' logs must stay identical to the permanent reference runs.

## 2. Physical model

All powers in dBm at the receiver's antenna; sums are done in mW.

- **Received power.** `P = link(rx, tx)` from the `-g` options (and `--vtime-link`), as today: the value is the received power, whatever TX power the sender uses. With `--medium-tx-ref=DBM`, the value is the received power when the sender transmits at DBM, and `P = link + (tx_dbm − DBM)`, with `tx_dbm` from the frame header. An edge without a value (0 dBm) stays a perfect link. Pairs that are not graph neighbours do not hear each other at all: no reception, no interference, no energy in CCA.
- **Noise floor.** `N = −119.93 dBm`, the constant of today's curve (`kTB` at 293 K and 100 kHz, NF 4 dB: `−153.93 + 30 + 4`).
- **Error model.** Today's empirical curve, with the SINR in place of `P − N`: `BER = min(0.5, 0.5 · exp(−0.5 · SINR_dB))`, `SINR = P / (N + I)`, `I` the sum of the interferers' powers in mW. Without interference this is exactly today's formula. The counted bits are those of today's draw: `8 · (PHR + PSDU + FCS)`.
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

A lock also ends when the radio's state in shared memory is no longer `SIM_RADIO_RECEIVE` at the next decision about `r` (the radio turned off: outcome `left`).

When `r` locks onto `F`, the server delivers `F` to `r` at `t` (an `"xx"` message with `SIM_FRAME_MEDIUM`, §4) and queues a **verdict** event for `r` at `t_end(F)`. The verdict is computed when that event is popped, so every interferer that started before `t_end` is known. Same-time order: at `t_end`, the verdict comes before any frame that starts at `t_end` for the same receiver (it was queued earlier, so its sequence number is lower).

## 4. Messages

All in `app_wssimserver/sim_proto.h`. Each is one `SOCK_SEQPACKET` message on the node's conductor socket.

- **Frame** `"xx"` (`struct sim_frame_hdr` + PSDU). New fields, filled by the sender's radio in both modes: `phr_len`, `tx_dbm` (the TX power the MAC asked for), `rate_bps`. The server fills `src` (as today) and, in central mode only, `frame_id` (a run-wide counter from 1), `rx_dbm` (the received power `P`) and `flags = SIM_FRAME_MEDIUM`. In legacy mode `flags`, `frame_id` and `rx_dbm` are 0 and the radio behaves as today.
- **Verdict** `"xv"` (`struct sim_verdict`), central mode only, to the receiver at `t_end`: `frame_id`, `ok`, `cause` (`enum sim_rx_cause`), `rx_dbm`, `sinr_db` (the lowest segment SINR). Not sent for a captured frame (the next `"xx"` replaces it) or a `tx_abort`.
- **Sense** `"xs"` (`struct sim_sense`), central mode only: the radio sends it during its run with its channel; the conductor answers at once with the same struct, `dbm` = `10·log10(N + Σ P)` over the frames on air at the radio on that channel at the current time (the radio's own excluded). The radio blocks on the reply (it is the only message the conductor sends to a running node).
- **Welcome flag** `NSIM_WELCOME_MEDIUM` (stack's `port/sim/ns_port_sim.h`, protocol version 3) and `ns_port_sim_medium()`: the node's radio runs in central mode.

Frames in a node's `"xa"` batch now include verdicts: the count in `"xa"` is the number of messages that follow (frames and verdicts), in queue order.

## 5. Shared memory

`/wssimcca` grows to 4 bytes per node: driver state (1 byte, as today), channel (2 bytes, as today), then **the radio's hardware state** (`enum sim_radio_state`, what `rf_power_state()` returns). The radio writes all four in `set_shm_cca_state()` in both modes. The server's medium reads byte 3 and the channel; the legacy CCA keeps reading byte 0. `vt_kill` sets byte 0 to `DRVSTATE_OFF` and byte 3 to `SIM_RADIO_OFF`.

## 6. The radio in central mode

Under `NS_PORT_SIM` and `ns_port_sim_medium()` (`app_wshwsim/sl_rf_driver.c`):

- **TX:** unchanged, apart from the new header fields. The sender's own TX end stays on its own timer.
- **`"xx"` with `SIM_FRAME_MEDIUM`:** the server has decided that the radio locks. If the radio is receiving, it drops that frame (captured; counted) and takes the new one. It enters `DRVSTATE_RX` (power state RECEIVE), stores the frame with `rssi = rx_dbm` and its RX timestamp as today, and arms **no** airtime timer (the backup timer stays as a safety net). No loss draw. If its own state says it cannot receive (off, sending, deaf backoff), it drops the frame and counts it as a disagreement with the server (should stay 0).
- **`"xv"`:** if `frame_id` is the frame it holds: `ok` → hand it to the MAC (`phy_rx_cb`) as `_rf_handle_rx_end()` does; not `ok` → drop it (as a CRC failure), back to listening. Either way the radio leaves RX as it does today. A verdict for any other frame is ignored (counted).
- **CCA:** `"xs"` to the conductor; busy when the returned energy ≥ the CCA threshold. The threshold is the one the MAC sets through `PHY_EXTENSION_SET_CCA_THRESHOLD` / `PHY_EXTENSION_SET_CHANNEL_CCA_THRESHOLD` (stored instead of ignored), with a documented default when the MAC sets none. In practice only the second carries a value: the MAC sends it in dBm (`int8_t`) before each CCA, from Nanostack's automatic per-channel threshold (`rcp/mac/ieee802154/mac_cca_threshold.c`, started by `ws_bootstrap.c`): −60 dBm at start (`CCA_DEFAULT_DBM`), lowered to the weakest RSSI received on that channel minus 1 dB, raised by 1 dB after a channel access failure, within −100…−60 dBm. The percentage form (`PHY_EXTENSION_SET_CCA_THRESHOLD`, `macCCAThreshold`) is not used by Wi-SUN and is ignored, as in the EFR32FG25 firmware. **Default** until the MAC sets one: **−85 dBm**, the firmware's `RAIL_PHY_CCA_THRESHOLD_DBM`.
- **Logs:** one line per drop, as today's loss line, with the cause.

## 7. Server options and outputs

- `--medium=legacy|central` (virtual time only; refused without `--vtime`).
- `--medium-tx-ref=DBM`, `--medium-lock-snr=DB`, `--medium-capture-db=DB`.
- `--medium-log=FILE`: one line per (frame, receiver) outcome: `t_end_us src dst frame_id outcome rx_dbm sinr_min_db`, outcome one of `ok`, `noise` (lost, no interferer), `interference` (lost with an interferer), `captured`, `below_lock` (heard, not locked: too weak against the noise and interference), `busy` (heard while receiving another frame and not captured), `not_listening`, `tx_abort`, `left`.
- At the end of the run, a summary line per outcome (totals), and the CCA query count with the busy count.
- Random: the verdict draws use a stream per receiver, `mix64(seed + mix64(node ID + 1) + K)` with a constant `K` that separates it from the port's and the drift's streams.

## 8. Checks (the EM4 gate)

1. **Legacy is unchanged:** `sim_utils/migration/nonreg_dag_simple.sh` against `reference-runs/dag_simple_b1b9cc5`: identical logs, 10 seeds × 300 s. The LFN suite (`sim_utils/vtime/lfn_suite.sh`) passes.
2. **Server unit tests** with fake nodes (`sim_utils/vtime/test_conductor.py`): two overlapping frames at one receiver (the second is interference, the first lost or kept by its SINR); capture inside and outside the SHR window; hidden terminal (A and C not neighbours, both to B); half duplex; channel mismatch; not listening; verdict before a same-time start; CCA energy from a sender that is a graph neighbour but not in the receiver's MAC neighbour table (which today's CCA misses); without interference, the loss rate at a few RSSI values equals today's curve; determinism by seed.
3. **Central against legacy, light traffic:** `dag_simple`, 6 seeds × 600 s per mode (`sim_utils/vtime/medium_gate_dag.sh`): the group medians of routers ready and of the join milestones close to legacy, and no significant difference in the pooled milestones (`medium_stats.py`, Mann-Whitney). A single run outside the other group's range is not a failure: with 6 runs per group this happens by chance. `dag_simple` has no data traffic, so delivery is judged in item 4.
4. **Where it matters:** `sim_utils/vtime/medium_gate_star.sh`, a border router and 40 routers in its range, in four groups that do not hear each other (`hidden`) and, as a control, all hearing each other (`clique`), with UDP requests to the border router: interference, captures and busy outcomes appear at the border router, more interference in `hidden` than in `clique`, CCA busy counts (from the radios' exit counters; the server only knows how many queries had a frame on air) higher in `clique`; the effect on delivery and join times is reported against legacy.
5. **LFN suite in central mode:** passes, or each difference is explained.

When the gate passes, `--medium=central` becomes the default with `--vtime` and the non-regression scripts pass `--medium=legacy`, including the LFN tests that start `wssimserver` with options of their own (`test_lfn_mac.sh`, `test_lfn_keyreq.sh`; `medium_lfn_suite.sh` shows a wrapper that reaches them).

## 9. Not in the MVP

Adjacent-channel rejection, fading and shadowing, bursts of losses, external interferers, preamble-sniff detection, node positions and path-loss models, RX↔TX turnaround and settling times, a reported RSSI with its own error (Investigation §5, P4–P11 and the layered loss model). The radio still decides its own TX timing; the medium does not delay or refuse a transmission.
