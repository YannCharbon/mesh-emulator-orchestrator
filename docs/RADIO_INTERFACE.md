# Radio interface: running any protocol stack as an emulated node

This document specifies how a program connects to the emulator as a radio node, sends and receives frames through the emulated radio medium, keeps time and sleeps. It is written for developers who implement a client in their own stack, in any language, from this document alone: no emulator code, no Nanostack code and no header of this repository is needed.

**Interface version 2** (version 1 is still served, §13). The server side is `wssimserver` in virtual-time mode (`--vtime`) with the central radio medium (the default in that mode). A reference client in Python and an example client in C are in `radio_if/` (§12); they are illustrations, and this document is normative: a client is written from this document, not copied from them.

## 1. Model

- **One process per node.** Each emulated node is a separate program that opens one connection to the server. The server is the radio medium and the clock of the whole network.
- **Virtual time.** Time in the emulation is the server's virtual time, in microseconds from the start of the run. It does not follow the wall clock. The server runs **one node at a time**: it wakes a node at a virtual time `t`, the node does its work, and the node tells the server when it next needs to wake. Code runs in zero virtual time.
- **The node is asleep between two wakes.** The interval between the end of one wake and the next wake is the node's idle time, which the power report counts as MCU sleep.
- **A frame is bytes plus physical parameters:** channel number, bit rate, preamble/sync, header and checksum lengths, and transmit power. The interface carries no protocol semantics: no addresses, no acknowledgements, no MAC. Everything above the radio is the client's.
- **The medium decides every physical outcome** (§8): who hears a frame, at what power, whether the receiver locks onto it, collisions, capture, half duplex, and whether the frame is received correctly.
- **The client decides everything its radio does:** when to transmit, when to listen and on which channel, when to turn the radio off. It reports each change of its radio's state to the server, which uses it for reception and for the power report.
- **Runs are reproducible.** For a given seed and given server options, two runs give the same events at the same times, provided the client uses no wall-clock time and draws its random numbers from the seed (§6.4).

## 2. Life of a node

```
client                                         server (wssimserver --vtime=N)
connect to the server's socket
"xh" hello {node ID, flags, version} -------->
                                               waits until N nodes have said hello
                                     <-------- "xw" welcome {version, flags, seed, drift, start}
first run at virtual time start:
  "xr" radio state reports, "xx" frames,
  "xs" CCA queries (answered at once) -------->
"xi" idle {next deadline} ------------------->
                                     <-------- "xa" wake {now = t, count = k}
                                     <-------- k messages: "xx" frames received, "xv" verdicts,
                                               "xd" sniff results (version 2)
work at time t (messages, then own timers) ...
"xi" idle {next deadline} ------------------->
...
                                     <-------- "xq" end of the run
close the connection and exit
```

1. **Connect.** The server listens on a UNIX socket of type `SOCK_SEQPACKET`; its path is the last argument of `wssimserver`. Linux limits such paths to 107 bytes.
2. **Hello** (`"xh"`, §4.2). It gives the node ID, the flags `VTIME | RADIO_IF` and the interface version. The server refuses a hello it cannot serve by closing the connection (§9).
3. **Welcome** (`"xw"`, §4.3). The server sends the welcomes once all the nodes of the run (`--vtime=N`) have said hello, one node at a time in ascending ID order. Each welcome starts the node's **first run**, at virtual time `start_us` (0, unless the node is powered up later, §6.3).
4. **Run.** During a run the client may send radio-state reports, frames and CCA queries, and from version 2 sniff requests and long-preamble frames (§6.7). A run ends when the client sends `"xi"` (idle) with its next deadline.
5. **Wake** (`"xa"`, §4.4). The server wakes the node at the time of its deadline, or earlier when a frame, a verdict or a sniff result arrives for it. The `"xa"` gives the new time and the number of messages that follow it; the client reads exactly that many. The client then does its work at that time and sends `"xi"` again.
6. **End.** At the end of the run the server sends `"xq"`. The client closes the connection and exits with status 0. A node stopped during the run (`--vtime-kill`) sees its connection closed by the server, without `"xq"`; it exits too.

The client never waits for anything other than the server. It must not sleep, poll a wall clock or read other inputs while it waits, because the server waits for the client's `"xi"` before anything else happens in the network.

## 3. Units and conventions

| Quantity | Unit and type |
| --- | --- |
| Time | microseconds of virtual time, unsigned 64-bit, from the start of the run |
| "No deadline" | `NEVER` = 2⁶⁴ − 1 |
| Power | dBm: `int8` for transmit power, `float32` for received power, energy and SINR (dB) |
| Bit rate | bit/s, unsigned 32-bit |
| Lengths | bytes |
| Clock drift | parts per billion (ppb), signed 32-bit, between −10⁸ and +10⁸ (the server's `--drift` takes −100 000 to +100 000 ppm) |
| Node ID | 0 to 4095, unique in a run |
| Channel | an unsigned 16-bit number chosen by the client; the medium only compares channel numbers for equality |

## 4. Messages

### 4.1 Encoding

- Each message is **one `SOCK_SEQPACKET` packet**: one `send()` per message and one `recv()` per message. The server and client never split or merge messages.
- Every message starts with a **two-character magic** (ASCII, no terminator), for example `"xh"`.
- Fields are **packed** at the offsets given below, with no padding beyond the reserved fields shown.
- Integers and floats are in **little-endian** byte order (the host byte order of the x86-64 and AArch64 Linux systems the emulator runs on). Floats are IEEE 754 single precision.
- **Reserved** fields are sent as 0 and ignored on receipt.
- Sizes are exact: the server rejects a message whose length differs from its size (§9), except frames, whose length is the header plus the PSDU.

### 4.2 Hello `"xh"`: client → server, 8 bytes, first message

| Offset | Size | Field | Value |
| --- | --- | --- | --- |
| 0 | 2 | magic | `"xh"` |
| 2 | 2 | node_id | 0 to 4095 |
| 4 | 2 | flags | `0x0003`: `VTIME` (0x0001, the node runs on virtual time) and `RADIO_IF` (0x0002, a client of this interface). Other bits are refused |
| 6 | 2 | version | the interface version the client implements: 1 or 2 (§13) |

### 4.3 Welcome `"xw"`: server → client, 32 bytes

| Offset | Size | Field | Value |
| --- | --- | --- | --- |
| 0 | 2 | magic | `"xw"` |
| 2 | 2 | version | the interface version in use: the one in the hello |
| 4 | 4 | flags | `POWER` (0x0001): the server records power, so the client may send `"xc"` (§7). `MEDIUM` (0x0002): the central medium decides reception; always set for clients of this interface. `SNIFF` (0x0004): the server serves preamble sniffing for this client, `"xn"` and `"xl"` (§6.7); set only for version 2 or later. Ignore unknown bits |
| 8 | 8 | seed | the run's base seed (§6.4) |
| 16 | 4 | drift_ppb | the node's clock error, signed (§6.3) |
| 20 | 4 | cca_us | the node's CCA duration in µs, `uint32`: its power profile's `cca_us` (§6.6, §7), for the PHY mode set with `--power-mode` or else the profile's common value; 0 when the server records no power. Sent to clients of this interface only. Servers before 2026-11-09 sent 0 (reserved) |
| 24 | 8 | start_us | the virtual time of the node's start (power-up); 0 for a node that starts with the run |

The welcome is also the first wake: the node's current time is `start_us`.

### 4.4 Wake `"xa"`: server → client, 16 bytes

| Offset | Size | Field | Value |
| --- | --- | --- | --- |
| 0 | 2 | magic | `"xa"` |
| 2 | 2 | count | number of messages that follow (frames, verdicts and, in version 2, sniff results), 0 to 65535 |
| 4 | 4 | reserved | |
| 8 | 8 | now_us | the new virtual time; never earlier than the previous one |

The `count` messages are separate packets; the client reads each of them with a blocking `recv()`, because they may arrive after the `"xa"`. They are in the order the medium produced them (§5.2).

### 4.5 Idle `"xi"`: client → server, 16 bytes, ends every run

| Offset | Size | Field | Value |
| --- | --- | --- | --- |
| 0 | 2 | magic | `"xi"` |
| 2 | 6 | reserved | |
| 8 | 8 | next_us | the virtual time of the node's next deadline, or `NEVER` |

### 4.6 Frame `"xx"`: both directions, 28-byte header + PSDU

| Offset | Size | Field | Sent by the client | Received by the client |
| --- | --- | --- | --- | --- |
| 0 | 2 | magic | `"xx"` | `"xx"` |
| 2 | 2 | len | PSDU length in bytes, at most 4096 | the same |
| 4 | 2 | chan | channel of the transmission | the same |
| 6 | 2 | seq | any value (for traces); not used by the medium | the sender's value |
| 8 | 2 | src | 0 (the server writes the sender's ID) | the sender's node ID |
| 10 | 1 | shr_len | bytes of preamble and sync word | the same |
| 11 | 1 | fcs_len | bytes of checksum after the PSDU | the same |
| 12 | 1 | phr_len | bytes of PHY header | the same |
| 13 | 1 | tx_dbm | transmit power, `int8` | the same |
| 14 | 1 | flags | 0 | `0x01` (`MEDIUM`): the medium locked this receiver onto the frame. `0x02` (`LONG_PREAMBLE`, version 2 only): the sender sent it with a long preamble (`"xl"`, §4.14); a version-1 client never gets this bit |
| 15 | 1 | reserved | 0 | |
| 16 | 4 | rate_bps | bit rate | the same |
| 20 | 4 | frame_id | 0 | the run-wide identifier of the frame, from 1 |
| 24 | 4 | rx_dbm | 0 | the received power at this node, `float32` |
| 28 | len | PSDU | the bytes the protocol sends | the same bytes |

The PSDU is what the receiver's protocol gets. The preamble, sync word, PHY header and checksum are not transmitted as bytes: only their lengths count, for the airtime and the error model.

### 4.7 Verdict `"xv"`: server → client, 16 bytes

| Offset | Size | Field | Value |
| --- | --- | --- | --- |
| 0 | 2 | magic | `"xv"` |
| 2 | 1 | ok | 1: the frame was received correctly; 0: it was lost |
| 3 | 1 | cause | 0 received; 1 lost to noise (no interferer during the frame); 2 lost with at least one interferer |
| 4 | 4 | frame_id | the frame this verdict ends |
| 8 | 4 | rx_dbm | received power, `float32` |
| 12 | 4 | sinr_db | the lowest signal-to-interference-plus-noise ratio over the frame, `float32` |

### 4.8 Radio state report `"xr"`: client → server, 8 bytes

| Offset | Size | Field | Value |
| --- | --- | --- | --- |
| 0 | 2 | magic | `"xr"` |
| 2 | 1 | state | 0 `OFF`, 1 `IDLE`, 2 `LISTEN`, 3 `RECEIVE`, 4 `TX` (§6.1) |
| 3 | 1 | tx_dbm | the transmit power, `int8`, in `TX`; 0 otherwise |
| 4 | 1 | events | `0x01` (`CCA`): a CCA ended now (§6.6); 0 otherwise |
| 5 | 1 | phy_mode | 0, unless the radio runs one of the Wi-SUN FSK PHY modes and the power profile has values per mode (§7) |
| 6 | 2 | channel | the channel the radio is on |

### 4.9 CCA query `"xs"`: client → server and back, 8 bytes

| Offset | Size | Field | Value |
| --- | --- | --- | --- |
| 0 | 2 | magic | `"xs"` |
| 2 | 2 | channel | the channel to assess |
| 4 | 4 | dbm | client: 0. Server's answer: the energy on that channel at this node now, `float32` |

The server answers at once with the same message, `dbm` filled in. It is the only message the server sends during a node's run, so the client sends `"xs"` and then blocks on `recv()` for the answer.

### 4.10 Wake report `"xc"`: client → server, 16 bytes, optional

| Offset | Size | Field | Value |
| --- | --- | --- | --- |
| 0 | 2 | magic | `"xc"` |
| 2 | 2 | events | units of work done in this wake (messages handled, events dispatched), saturating at 65535 |
| 4 | 12 | timers[6] | timer expiries in this wake, `uint16` each, by cause: 0 tick, 1 protocol, 2 MAC, 3 FHSS, 4 radio, 5 other |

Sent just before `"xi"`, only when the welcome has `POWER`. The server ignores it otherwise. Causes 0–3 are the categories of the Nanostack nodes; a client of this interface normally uses 4 for timers of its radio driver and 5 for the others (§7).

### 4.11 End `"xq"`: server → client, 2 bytes

The run is over. The client closes the connection and exits with status 0.

### 4.12 Sniff request `"xn"`: client → server, 16 bytes, version 2

| Offset | Size | Field | Value |
| --- | --- | --- | --- |
| 0 | 2 | magic | `"xn"` |
| 2 | 2 | channel | the channel to sniff |
| 4 | 4 | duration_us | the length of the sniff window, from now (0: the `"xd"` comes in a new wake at the same time) |
| 8 | 4 | sniff_id | the client's own tag, echoed in the `"xd"` |
| 12 | 1 | flags | `0x01` (`LOCK`): on a detection, lock the receiver onto the frame if it can still be received. Other bits: 0 |
| 13 | 1 | end_state | the radio state at the window's end when nothing is detected: 0 `OFF`, 1 `IDLE` or 2 `LISTEN` (§4.8 numbering); any other value means `OFF` |
| 14 | 2 | reserved | |

Not answered at once: one `"xd"` comes in a later wake (§6.7).

### 4.13 Sniff result `"xd"`: server → client, 32 bytes, version 2

| Offset | Size | Field | Value |
| --- | --- | --- | --- |
| 0 | 2 | magic | `"xd"` |
| 2 | 1 | detected | 1: a frame was detected; 0: nothing in the window |
| 3 | 1 | locked | 0: no lock (`LOCK` not asked, or the frame can no longer be received); 1 (`LOCKED`): the medium locked the receiver onto the frame now, and its `"xx"` is the next message of this wake; 2 (`RECEIVING`): the receiver was already locked onto that frame, whose `"xx"` came at its start (with or without `LOCK`) |
| 4 | 4 | sniff_id | the request's tag |
| 8 | 4 | frame_id | the frame detected; 0 if none |
| 12 | 2 | src | its sender's node ID; 0 if none |
| 14 | 2 | reserved | |
| 16 | 8 | t_us | the detection time, or the window's end if nothing was detected; always the time of this wake |
| 24 | 4 | rssi_dbm | the detected frame's received power; if none, the energy on the channel at the window's end (as `"xs"` would answer), `float32` |
| 28 | 4 | sinr_db | the lowest SINR over the detecting dwell (0 if none), `float32` |

### 4.14 Long-preamble frame `"xl"`: client → server, 8 bytes + a complete `"xx"`, version 2

| Offset | Size | Field | Value |
| --- | --- | --- | --- |
| 0 | 2 | magic | `"xl"` |
| 2 | 2 | reserved | |
| 4 | 4 | preamble_us | the wake-up preamble on air before the frame's own SHR, in µs |
| 8 | 28 + len | frame | a complete `"xx"` message as sent by the client (§4.6) |

One message: the 8-byte header and the `"xx"` in a single `send()`. Receivers get the frame as an ordinary `"xx"` with the `LONG_PREAMBLE` flag (version-2 clients only). With `preamble_us` = 0 it is an ordinary frame, without the flag.

## 5. Sequencing in virtual time

### 5.1 How the server orders the network

- The server holds a queue of events: each node's deadline (from its last `"xi"`; a new `"xi"` replaces it) and messages due to a node (frames at their start, verdicts at their end, sniff results at their time).
- It takes the earliest event by **time, then node ID, then queue order**, and wakes that node with every event due to it at that time: one `"xa"` carries them all. Nodes at the same time run in ascending ID order.
- Until the client's `"xi"`, nothing else happens in the network. Frames the client sends during its run start at the current time.
- A deadline earlier than the current time is moved to the current time, with a warning in the server's log. A deadline equal to the current time wakes the node again at that time, after the nodes with lower IDs that are due then.
- A node with no deadline (`NEVER`) is woken only by messages.
- If more than 100 000 wakes follow one another without time advancing, the server stops the run with an error.
- The run ends at `--vtime-end` (the first event after that time is not served), or when no event is left.

### 5.2 Within a wake

1. The `"xa"` gives the time `t` and the count `k`. The client reads the `k` messages. Verdicts and frames are in the medium's order. When a frame ends at `t` and another starts at `t` at the same receiver, the verdict of the first comes before the second frame.
2. The client handles those messages, and then its own timers due at `t`. This order is a recommendation (the server does not see it), and it matches the Nanostack nodes.
3. The client sends whatever its work produces: `"xr"`, `"xx"`, `"xs"` (waiting for each answer), and `"xc"` if the welcome has `POWER`.
4. The client sends `"xi"` with its earliest deadline.

## 6. The radio

### 6.1 Radio states and reports

The client reports **every change of its radio's state** with `"xr"`, at the time it happens (during the wake at that time). The server keeps the last reported state and channel of each node: the medium uses them to decide reception (§8), and the power report charges each state's current (§7).

| State | Meaning | The node hears frames |
| --- | --- | --- |
| `OFF` | powered down | no |
| `IDLE` | powered, neither listening nor sending | no |
| `LISTEN` | receiver on, on the reported channel, no frame | yes, frames that start on that channel |
| `RECEIVE` | receiving the frame the medium locked the node onto | no new frame, except a capture (§6.5) |
| `TX` | sending, at `tx_dbm` | no |

- **The radio starts `OFF`** at each start (welcome) of a node.
- **During a sniff** (§6.7) the server applies the states the sniff implies; the client reports only the others.
- A channel change is a report with the new channel; the state may stay the same.
- Report `RECEIVE` in the wake that delivers a locked frame (§6.5), and leave it when the verdict comes. The medium gives the verdict only to a node whose reported state is `RECEIVE` at the frame's end: a client that stays in `LISTEN` gets no verdict, and the frame is lost (outcome `left`).
- A report applies from the current time. A frame already on air when the node starts listening is not received (the receiver missed its preamble), but it does count as interference and as CCA energy.

### 6.2 Transmitting

To send a frame at time `t` ("timed TX"), the client sets a deadline at `t` and, in that wake:

1. reports `TX` with the transmit power (`"xr"`);
2. sends the frame (`"xx"`); it starts at `t`;
3. computes the frame's end, `t + airtime`, and sets a deadline there:

   `airtime_us = (shr_len + phr_len + len + fcs_len) × 8 × 1 000 000 / rate_bps`, with integer division, the formula the medium uses;
4. at the end, reports the next state (`LISTEN`, `IDLE` or `OFF`).

A long-preamble frame (`"xl"`, §6.7) follows the same steps; its transmission lasts `preamble_us + airtime_us`, and that is its end for every rule below.

Rules, checked by the server: a frame is sent only while the node's reported state is `TX`, and not before the end of the node's previous frame. A frame that breaks a rule is **not sent**; the server logs a warning and counts it in its end-of-run summary. A frame always stays on air until its computed end: the node must stay in `TX` until then. A report that leaves `TX` earlier is applied (the node may then hear frames while its own is on air, and the power report charges the new state), but the server warns about it and counts it.

The medium uses the frame's own `chan` and `tx_dbm`; the power report uses the `tx_dbm` of the `TX` report. The server does not compare them: report the channel and power the frame is sent with. The medium never delays or refuses a frame for any other reason: there is no automatic CSMA, backoff or acknowledgement. The sender receives no confirmation; its frame ends at its own computed end.

### 6.3 Clock, timers and start time

- The client's notion of the current time is the last `now_us` (or the welcome's `start_us`). Virtual time is the same for every node.
- **The node's own clock** models its crystal. It counts from the node's start and drifts by `drift_ppb`:

  `local(v) = e + floor(e × drift_ppb / 10⁹)`, with `e = v − start_us`.

  A timer of `d` µs on the node's clock, started at virtual time `v0`, fires at the first virtual time `v` with `local(v) ≥ local(v0) + d`. The deadlines in `"xi"` are always **virtual** times: a client that models drift converts its local deadlines to virtual time with this formula. With `drift_ppb` = 0 and `start_us` = 0 the node's clock is virtual time. The product `e × drift_ppb` exceeds 64 bits for long runs at large drifts (at 100 ppm, after about 2.9 years of virtual time): compute it in 128 bits, or check the bound. A client that does not model drift may ignore `drift_ppb`, and the run then behaves as if `--drift` were 0 for that node.
- **Late power-up and restart.** With `--vtime-power-up=ID:SECONDS` the node connects with the others but is welcomed at that time: until then it does not run, and its radio is off. With `--vtime-start=ID:SECONDS` a second process with the same node ID connects at the start of the run and is welcomed at that time, after the server has stopped the first one. In both cases the welcome's `start_us` is that time, and the node's clock starts at 0 there.

### 6.4 Randomness

The welcome's `seed` is the run's base seed (`WSSIM_SEED`). For reproducible runs the client draws every random number from a generator seeded from `seed` and its node ID, and never from the operating system. The derivation is the client's choice; it should give different nodes uncorrelated streams. The reference client draws none.

### 6.5 Receiving

1. The client reports `LISTEN` on a channel. Its receive window is open from that time ("timed RX window").
2. When a frame starts on that channel at a node that hears its sender, the medium decides whether the node **locks** onto it (§8). If it does, the node gets the frame (`"xx"` with flag `MEDIUM`) in a wake at the frame's **start**, with the received power in `rx_dbm`. Frames the node does not lock onto are never delivered.
3. The client reports `RECEIVE` in that wake and holds the frame. Without that report the frame is lost and no verdict comes (§6.1).
4. At the frame's **end** the node gets the verdict `"xv"` for that `frame_id`. If `ok` is 1 the protocol receives the PSDU; if it is 0 the frame is dropped, as a failed checksum would be. The client then reports its next state.

Exceptions while a frame is held:

- **Capture.** A new `"xx"` arrives before the verdict: a stronger frame captured the receiver during the first frame's preamble and sync word. The client drops the first frame and holds the new one; no verdict comes for the first.
- **Leaving.** If the client reports any state other than `RECEIVE` while it holds a frame (it turns the radio off, or starts to transmit), the frame is lost and no verdict comes. Transmitting while receiving is how half duplex appears: the medium records the outcome `tx_abort`.
- A verdict for a frame the client does not hold is ignored.

A frame sent with a long preamble (`"xl"`) is received in the same way by a node that is listening when its transmission starts: its `"xx"` comes at the start of the long preamble and its verdict at the end of the frame. A node that turns its receiver on later can still receive it with a sniff (§6.7).

A receive window that closes while a frame is being received normally waits for the verdict, as a real radio's driver does; the client chooses.

### 6.6 Clear-channel assessment

The client sends `"xs"` with a channel and receives the energy at its antenna on that channel at the current time: the noise floor (−119.93 dBm) plus every frame on air on that channel from the node's neighbours, in dBm. The node's own frame is excluded. The client compares the energy with its own threshold; the server has no threshold. A CCA takes no virtual time. For the power report, the client then sends `"xr"` with its current state and the `CCA` event, and the server charges the profile's CCA duration (`cca_us`) at the end of the listening before it.

A CCA needs the receiver on: report `LISTEN` on the channel at least the CCA duration before the query (a radio needs that time anyway). The CCA duration is the welcome's `cca_us` (§4.3): the power profile's value, 160 µs in both FG25 profiles for the common values (8 symbols at 50 ksym/s) and 53 µs for EU mode 3 (`cca_us@3`). It follows the PHY mode the client reports (§7): a client that reports a mode with its own value (`phy_mode` 5 for mode 3) is charged that value, which the welcome gives only if `--power-mode` selects that mode, so such a client takes it from the profile. With `cca_us` = 0 (no power recording) the client uses its own radio's figure; the duration then only matters for its own timing. The server answers a query whatever the reported state, and the power report cuts the CCA to the length of the interval before it, so a receiver turned on in the same wake as the query charges no CCA time.

### 6.7 Preamble sniffing (version 2)

Preamble sampling: a node turns its receiver on for a short window on one channel, the medium tells it whether a preamble was on air, and on a detection the node can receive the frame that follows. Senders put a wake-up preamble longer than the receivers' sniff period before their frames (`"xl"`). The detection and lock rules are the medium's, specified in `docs/MEDIUM.md` §10 (§10.2 detection, §10.3 receiving after a detection, §10.5 the server's settings); this section gives what a client sends, receives and must do.

**Availability.** Only for a client whose hello asked for version 2 and whose welcome has `SNIFF` (§4.3). A version-1 client's `"xn"` and `"xl"` are dropped with a warning and counted (§9).

**A sniff.** The client sends `"xn"` (§4.12) during its run at time `t`, with channel `C`, duration `d`, a tag, the `LOCK` flag or not, and an end state:

1. The receiver is on, on `C`, from `t_on` to `t1 = t + d`. `t_on` is `t`, or the end of the node's own frame if one is still on air (half duplex). Detection starts after the receiver's settling time (server option `--sniff-settle-us`, 0 by default), counted from `t_on`; the settling time counts as listening.
2. The server answers with one `"xd"` (§4.13), in a wake of the node at `t_us`:
   - at the **detection time**, the end of the first detection try that succeeds (`detected` = 1). A try observes a fixed number of bits of a frame's preamble (`--sniff-dwell-bits`, 16 by default), so a detection comes one try's duration or more after the later of the preamble's start and the window's start (after the settling time): 320 µs at 50 kbit/s with the defaults;
   - or at `t1` with `detected` = 0.
3. With `LOCK`, a detection locks the receiver onto the frame if its sync word is still ahead and it passes the lock threshold (`docs/MEDIUM.md` §10.3): `locked` = 1 and the frame's `"xx"` (flag `MEDIUM`, and `LONG_PREAMBLE` if it was sent with `"xl"`) is the next message of the same wake. Its verdict `"xv"` comes at the frame's end, as for any lock (§6.5), and capture by a stronger frame applies until the end of its SHR.
4. A frame that **starts** on `C` during the window locks the receiver at its start, as in §6.5: the client gets its `"xx"` then and reports `RECEIVE` (a report that keeps the sniff, below), and the sniff reports that frame later with `locked` = 2 if it detects it (with or without `LOCK`). If its verdict comes before the `"xd"`, the client reports `LISTEN` on `C` to go back to the sniff, which goes on, or `OFF` to end it (then no `"xd"` comes). If the window ends while the frame is still being received and the sniff did not detect it (a preamble shorter than one try, or a missed try), the `"xd"` has `detected` = 0 and the radio stays in `RECEIVE`.

**Ordering in virtual time.** The `"xd"`, and the `"xx"` of a lock it made, are messages of a wake like frames and verdicts: they count in the `"xa"` count and come in the medium's order (§5.2), the `"xx"` right after its `"xd"`. A sniff that detects nothing costs the node one wake, at `t1`. A node has at most one sniff: a new `"xn"` replaces the one in progress, which then ends **without** `"xd"`. A frame the node sends during the window (`"xx"` or `"xl"`), a radio report that leaves the sniff (below) and the node's stop also end it without `"xd"`.

**Radio states during a sniff.** The server applies the states a sniff implies, in the power report and in the state the medium uses for the node, so the client does not report them:

| When | State applied by the server |
| --- | --- |
| `t_on` | `LISTEN` on `C`; nothing if the node's own frame lasts until `t1` or later |
| `t1`, nothing detected | the request's `end_state`, on `C`; nothing if the receiver is locked onto a frame (case 4) or the node's own frame is still on air |
| detection with `locked` = 1 | `RECEIVE`, on the frame's channel |

The client reports every other change with `"xr"`, as in §6.1:

- after `"xd"` with `detected` = 1 and `locked` = 0, the radio stays in `LISTEN` until the client reports otherwise (for example `OFF`);
- after `locked` = 2, the client has already reported `RECEIVE` when the frame's `"xx"` came (§6.5);
- after the verdict of a frame it received, its next state, as for any frame;
- a report of the state the server applied is harmless (the power report ignores a report of the current state).

**Reports during the window.** Every report is applied (the medium and the power report use it). A report that leaves the sniff **ends it, without `"xd"`** (outcome `left` in the sniff log): `OFF` or `IDLE`, `TX` from another state, or `LISTEN` or `RECEIVE` on a channel other than `C`. A report of `LISTEN` or `RECEIVE` on `C` keeps the sniff: the detection goes on, and the server applies the end state at `t1`. So a client reports, during a window, only `RECEIVE` for a frame that starts in it (case 4), `LISTEN` on `C` after that frame's verdict, or a change that ends the sniff on purpose, after which it waits for no `"xd"`.

**`"xn"` while holding a frame.** The sniff puts the receiver in `LISTEN`, which leaves the frame the client holds (§6.5): that frame is lost at once (medium outcome `left`) and no verdict comes. The sniff may then detect the same frame again if its preamble is still on air, and with `LOCK` lock onto it: its `"xx"` comes again, with the same `frame_id`, after the `"xd"`.

**`"xn"` during the node's own frame.** The client stays in `TX` until the frame's end, as §6.2 requires; the server switches it to `LISTEN` on `C` at that end (`t_on`), and the client reports nothing else then (or `LISTEN` on `C`; any other report ends the sniff). If the frame lasts until `t1` or later, the receiver is never on: the `"xd"` comes at `t1` with `detected` = 0, the server applies no state, and the client reports its next state at its frame's end as usual.

**Sending with a long preamble.** `"xl"` (§4.14) is a frame for every rule of §6.2: the client reports `TX` before it, the server refuses it otherwise or while the node's previous frame is on air, and the transmission ends at `t + preamble_us + airtime_us`, until which the client stays in `TX`. The power report charges the long preamble from the client's `TX` report, as the rest of the frame. Receivers that lock onto it get the frame's PSDU only; the preamble is not delivered.

**Choosing the parameters.** For a sniff every `T` µs of `d` µs each, a long preamble of at least `T + d` µs makes every neighbour's sniff fall at least once wholly inside it. The detection rate per try depends on the received power and on interference (`docs/MEDIUM.md` §10.2); the server's `--sniff-*` options set the model, and its `--sniff-log` has one line per sniff.

## 7. Power report

With `--power=PROFILE` the server keeps a charge ledger per node and writes `power.json` at the end of the run (`docs/POWER_MODEL.md` §7). It takes everything from this interface:

- **Radio:** the states of the `"xr"` reports and those a sniff implies (§6.7), each charged with the profile's current for that state (`TX` per `tx_dbm`), the transitions between them, and the CCA events.
- **MCU:** a wake starts at each `"xa"` (and at the welcome) and ends at the next `"xi"`. Its active time is modelled from the profile's cycle counts: a fixed cost per wake, plus a cost per frame delivered, per frame received (`RECEIVE` reports) and sent (`TX` reports), per unit of work and per timer expiry of each cause given in `"xc"`. The rest of the interval until the next wake is sleep. The sleep is deep when the radio is `OFF` (or `IDLE`, if the profile says an idle radio does not keep the fast clock) and the idle time is long enough to make deep sleep cheaper; otherwise it is light sleep.
- **`"xc"` is optional.** Without it each wake costs the fixed cost plus its frames.
- **`phy_mode`:** with 0, the profile's common values apply; a profile may hold values per Wi-SUN FSK PHY mode (`docs/POWER_MODEL.md` §6.2), selected when the client reports the mode it runs. The numbers are those of 2-FSK with bit rate and modulation index h: 1 = mode 1a (50 kbit/s, h = 0.5), 2 = 1b (50 kbit/s, h = 1), 3 = 2a (100 kbit/s, h = 0.5), 4 = 2b (100 kbit/s, h = 1), 5 = mode 3 (150 kbit/s, h = 0.5), 6 = 4a (200 kbit/s, h = 0.5), 7 = 4b (200 kbit/s, h = 1), 8 = mode 5 (300 kbit/s, h = 0.5).

Every current, time and cycle count is in the profile, which the user chooses (`examples/power/*.ini`); the client does not send currents.

**Which profile and `phy_mode` for a stack that is not Wi-SUN.** The ledger knows nothing of the protocol: it charges radio states, transitions, CCAs and wakes, so a profile describes the hardware and how its firmware drives the radio, not Wi-SUN. The two measured profiles are of the EFR32FG25 SoC with its radio driven by RAIL:

| The node | Profile | Why |
| --- | --- | --- |
| Battery node that turns its receiver on in short windows or sniffs and sleeps in between (a duty-cycled leaf or router) | `examples/power/efr32fg25-lfn.ini` | Measured on our low-power (LFN) firmware: RX plateau 4.32 mA above EM1, EM2 floor 7.0 µA, wake from EM2 260 µs, and a fixed cost of about 7.35 µC per receive window beyond the listening itself (mostly the `listen_off` transition, 1000 µs at 6.56 mA, charged at each radio-off; the rest is the wake and the cycles). That fixed cost is mostly RAIL, the power manager and interrupts, not the Wi-SUN stack (`docs/POWER_MODEL.md` §12.9), so it applies to any stack that opens windows through RAIL in the same way. "LFN only" in that document means "not for Wi-SUN routers"; it is the profile for duty-cycled nodes of any stack |
| Node whose receiver is on most of the time (a mains-powered relay or gateway) | `examples/power/efr32fg25-soc.ini` | Measured on our always-listening Wi-SUN router (HW5): the MCU stays in EM1 at 97.5 MHz while the radio listens, RX 6.0 mA above EM1 |

Both are FG25 figures: another radio or MCU needs its own profile (copy one and edit it, `docs/POWER_MODEL.md` §6.1; `--power-node=IDS:PROFILE` gives a profile per node). Report `phy_mode` = the number above whose bit rate and modulation index match the client's PHY, else 0. With 0 a node gets the profile's common values, which for both FG25 profiles are EU mode 1a (50 kbit/s): a client that sends at 150 kbit/s with h = 0.5 reports 5 and gets the mode-3 TX currents, CCA time (53 µs) and TX cycles. The currents of listening and receiving do not depend on the mode in these profiles.

## 8. What the medium decides and what the client decides

| The medium (server) decides | The client decides |
| --- | --- |
| Who hears a frame at all: the topology (`-g`), cut links (`--vtime-cut`) | When to transmit, on which channel, with which PHY parameters and power |
| Received power: the link's value (`-g A,B:RSSI`, `--vtime-link`), or the link's value plus the sender's power above `--medium-tx-ref` | When the receiver is on, on which channel, and when the radio is off |
| Whether a listening node locks onto a frame: signal-to-noise-and-interference ratio at the frame's start ≥ the lock threshold (`--medium-lock-snr`, 0 dB) | What to do with a received frame, and with a lost one |
| Capture: a frame at least `--medium-capture-db` (6 dB) stronger during the first frame's preamble and sync word | Whether and when to assess the channel, and the CCA threshold |
| Interference: every overlapping frame on the same channel from a neighbour | Retransmissions, backoff, acknowledgements, addressing, routing: the whole protocol |
| Success or loss of each frame: one seeded draw from the error model over the frame's segments of different SINR (`docs/MEDIUM.md` §2) | Its own clock's drift handling and its random numbers |
| Half duplex: a node that transmits loses the frame it was receiving, and a node in `TX` hears nothing | Its radio state, which it must report |
| The CCA energy | |
| Whether a sniff detects a frame, when, and whether it locks onto it (`docs/MEDIUM.md` §10) | When to sniff, for how long, on which channel, and the wake-up preamble of its frames |

Links are given in dBm, the received power at the receiver. Their values are directional and default to symmetric: `-g A,B:RSSI` is the power at B of A's frames and, unless a `-g B,A:RSSI` option gives the other direction, also at A of B's frames. So a link given once is the same both ways, and giving the reverse direction too makes it asymmetric (`-g 1,2:-70 -g 2,1:-80`: node 2 hears node 1 at −70 dBm, node 1 hears node 2 at −80 dBm). A range or a list of other than two IDs (`-g 0-3:-70`) sets every pair, both directions. Options apply in order, the later one wins, and the server's log warns when a later option changes a value an earlier one set. Who hears whom stays symmetric: a one-way link is a reverse value too weak to be received. The full rules are in `docs/MEDIUM.md` §2. A link without a value (`-g A,B` alone) is a perfect link at 0 dBm, which destroys any frame it overlaps; give values when collisions matter. Without any `-g` option every node hears every other one, at 0 dBm.

## 9. Errors

| Case | What the server does |
| --- | --- |
| Hello of the wrong size or magic, node ID above 4095, unknown flag bits | closes the connection; warning |
| Interface version outside the server's range (1 to 2) | closes the connection; warning with the supported range |
| `RADIO_IF` without `VTIME`, or with `--medium=legacy`, or a server without `--vtime` | closes the connection; warning |
| A node ID already connected, or more hellos than `--vtime=N` | closes the connection; warning |
| A node disconnects before the run starts | drops it and waits for another hello |
| A node disconnects during the run | stops the run; exit status 1 |
| Frame whose length differs from its header's `len`, or whose airtime cannot be computed (`rate_bps` = 0) | drops the frame; warning |
| Frame while the node's reported state is not `TX`, or before the end of its previous frame | does not send it; warning (at the 1st, 2nd, 4th, 8th… occurrence) and a count per node at the end |
| `"xr"` that leaves `TX` before the end of the node's frame | applies it; warning and count, as above |
| `"xr"` with an unknown state | ignores it; warning and count |
| `"xn"` or `"xl"` from a version-1 client | drops it; warning and count |
| `"xl"` shorter than its header and a frame header | drops it; warning |
| `"xi"` of the wrong size | stops the run; exit status 1 |
| Any other unknown message | drops it; warning |
| Deadline in the past | moves it to the current time; warning |
| More than 100 000 wakes without time advancing | stops the run; exit status 1 |

A client should treat any message it does not expect as a fatal error, and exit when the server closes the connection.

## 10. Running clients

Build the server in a virtual-time build (`docs/VIRTUAL_TIME.md` §2; only `wssimserver` is needed for clients of this interface). Then start the server, then the clients, in any order once the socket exists:

```bash
WSSIM_SEED=1 build-vt/wssimserver --vtime=3 --vtime-end=60 \
    -g 1,2:-70 -g 1,3:-70 -g 2,3:-75 \
    --medium-log=medium.log --power=examples/power/efr32fg25-lfn.ini --power-out=power.json \
    /tmp/radio_sock &
for n in 1 2 3; do my_client --sock /tmp/radio_sock --id $n & done
wait
```

The profile is the one for duty-cycled nodes (§7, "Which profile"). `sim_utils/vtime/run.sh` does the same with `IF_NODES` (below) and writes `simserver.log`, `if<i>.log`, `run_info.txt` and, with `POWER`, `power.json`; with node 0 among the clients the run has no Nanostack program and needs no `examples/` directory.

**Topologies in a file.** `--group-file=FILE` reads the `-g` options from a file, one per line: `A,B:RSSI`, `A,B` or a group such as `0-3:RSSI`, with or without a leading `-g` (or `--group=`); blank lines and `#` comments (whole line or end of line) are skipped, and a malformed line stops the server with its file name and line number. It has no size limit, unlike the command line (about 2000 nodes of a building, `docs/SCALE.md` §4). `run.sh`'s `TOPO=` file has the same format (`sim_utils/vtime/README.md`, "Topology files"). `radio_if/scale/gen_building.py` writes such a file for a multi-floor building from a path-loss model (`docs/SCALE.md` §2):

```
# topology.txt: the link 0-1 the same in both directions, 0-2 asymmetric
0,1:-71.7
0,2:-85.1
2,0:-88.0      # node 0 hears node 2 at -88.0 dBm, node 2 hears node 0 at -85.1
-g 1,2:-79.5   # a leading -g is accepted
```

**Node logs.** The server does not read a client's output; `run.sh` writes it to `if<i>.log`. The controller of the mesh test bench (repository `mesh-testbed-controller`) reads these logs: it takes the **first word of each line as the virtual time in µs** since the start of the run (a decimal integer, the `now_us` of the wake that printed it), and a line that holds `MOP1 ` followed by one JSON object to the end of the line as an observation in its Mesh Observation Protocol, for example:

```
97803200 MOP1 {"mop":1,"type":"report","seq":12,"t_ms":97803,"node":{"id":4,"state":"operational"},"counters":{"tx":31}}
```

Text may come between the time and `MOP1 ` (the reference client prints its node ID there). The JSON is specified in that repository's `docs/STACK_NEUTRAL_MODEL.md` (§3 for the messages, §3.1 for the text-line form, §6.1 for clients of this interface). Other lines are free text.

Useful server options (all in `wssimserver --help`): `--vtime-end`, `-g` (topology and link values), `--vtime-link`, `--vtime-cut`, `--vtime-kill`, `--vtime-start`, `--vtime-power-up`, `--drift`, `--drift-random`, `--medium-*` (thresholds, the per-frame log, §8), `--sniff-*` (the detection model and the per-sniff log, §6.7), `--power*`, `--vtime-frames` (every frame sent, with its PSDU in hex). `--vtime-drop` reads the PSDU as an IEEE 802.15.4 frame (its sequence number) and is meaningful only for such frames. `--power` cannot be combined with `--vtime-kill` or `--vtime-start`.

The server's log ends with the virtual time reached, the wake and frame counts, the medium's outcome counts and, with `--power`, one line per node. The medium log (`--medium-log`) has one line per frame and receiver: `t_end_us src dst frame_id outcome rx_dbm sinr_min_db`, with outcome `ok`, `noise`, `interference`, `captured`, `below_lock`, `busy`, `not_listening`, `tx_abort` or `left`.

Clients of this interface and Nanostack nodes (`wsnode-inproc`, `wsbrd-inproc`) can share a run, since they share the medium: each locks onto the other's frames, which collide with its own, and receives them as foreign bytes. A client must therefore accept any PSDU, of any length up to 4096 bytes. `sim_utils/vtime/run.sh` starts such runs with `IF_NODES` (the clients' IDs) and `IF_CMD` (the command, given `--sock` and `--id`); `radio_if/test_mixed.sh` is the test (§12).

**A stack developed elsewhere.** The emulator is a test tool for a client: the client runs against the server's binaries, and its code stays in its own repository. The clients in `radio_if/` (§12) illustrate the interface; a client is written from this document, not copied from them or from any other code of this repository.

## 11. A minimal client

```
connect(sock_path); send "xh" {id, flags = 0x0003, version = 1}
w = recv "xw"; check w.version == 1; now = w.start_us; state = OFF
on_start()                                   # set the first timers
loop:
    send "xc" {...} if w.flags & POWER       # optional
    send "xi" {min(timers) or NEVER}
    m = recv; if m is "xq" or the connection closed: exit(0)
    "xa": now = m.now_us; msgs = [recv() for _ in range(m.count)]
    for each msg:
        "xx": held = msg; state = RECEIVE; send "xr" {RECEIVE, chan}
        "xv": if held and held.frame_id == msg.frame_id:
                  deliver held.psdu if msg.ok; held = None
                  state = LISTEN or OFF; send "xr" {state, chan}
    for each timer due at now, by deadline:
        window opens:  state = LISTEN; send "xr" {LISTEN, chan}
        window closes: if state != RECEIVE: state = OFF; send "xr" {OFF, chan}
        rx_on:         state = LISTEN; send "xr" {LISTEN, chan}   # at least cca_us before transmit
        transmit:      e = CCA "xs" {chan}; send "xr" {state, CCA event}
                       if e < threshold: send "xr" {TX, chan, dBm}; send "xx" {...}
                                         timer tx_end = now + airtime
        tx_end:        state = LISTEN or OFF; send "xr" {state, chan}
```

## 12. The reference client and its tests

`radio_if/ref_client.py` (Python standard library only) implements this interface, version 2 (`RadioIf`; `--if-version=1` asks for version 1), and two small protocols on top of it: `WindowNode`, periodic receive windows on the node's own clock, the radio off between them, frames at given offsets in the window with CCA before each; and `SniffNode`, preamble sampling (§6.7): a sniff of `--sniff-us` every `--sniff-period-ms` with the lock flag, the radio off in between, and frames sent with a wake-up preamble (`--lp-tx`, `--lp-preamble-ms`).

`radio_if/c_example/rif_node.c` is a second client, in C, written from this document alone (its message structures come from §4, with no header of this repository): a frame every period at a given offset on the node's own clock, CCA before each, the receiver on between frames (`--listen`) or turned on only for the CCA. It speaks version 1 by default (it shows that version-1 clients keep working) and version 2 with `--if-version 2`: then it can sniff (`--sniff-period-ms`, `--sniff-us`, `--sniff-phase-ms`, `--no-lock`; the radio off between sniffs) and send its frames with a wake-up preamble (`--lp-preamble-ms`). It builds with `cc -O2 -o rif_node rif_node.c`.

`radio_if/test_ref_client.py` runs them against `wssimserver`:

| Test | What it shows |
| --- | --- |
| `exchange` | Three nodes, four windows: every frame is delivered through the medium at its end; virtual time reaches the last window; the power report has the exact TX and RX times, the radio on only in the windows, deep sleep the rest of the time, and the state times add up to the run time |
| `determinism` | The same seed gives identical node logs and medium log |
| `collision` | Two hidden nodes send at once: the receiver loses the frame to interference and the second frame is `busy`; their CCAs found the channel clear |
| `capture` | A frame 20 dB stronger, inside the first frame's preamble, captures the receiver |
| `half_duplex` | Two nodes send at once without CCA: the one receiving drops its frame (`tx_abort`), the other hears nothing |
| `cca_busy` | With CCA, the second node finds the first frame on air (−60 dBm), does not send, and receives it |
| `window_closed` | Frames sent while the receiver's radio is off are not heard (`not_listening`) |
| `drift_power_up` | A node 100 ppm fast and powered up at 0.5 s opens its windows at the virtual times the clock formula gives and still receives |
| `c_client` | The C client and the Python client in one run exchange every frame; a C node with its radio off between frames has its CCAs charged inside its short listens |
| `hello_refused` | The server refuses an unknown version (0 and 3), unknown flags, the interface without virtual time (also for a later instance, `--vtime-start`), and the legacy medium |
| `sniff_lp` | A sender with a 105 ms wake-up preamble and two receivers sniffing 1 ms every 100 ms: each receiver detects each preamble one dwell into its first sniff inside it, is locked onto the frame and receives it; its other sniffs find the noise floor; the server applies the sniff states (the clients report none), and the power report has exactly the listening, receiving and TX times |
| `sniff_frame_starts` | A frame with an ordinary preamble that starts inside a sniff locks the receiver at its start (the server put the client in `LISTEN`), and the sniff reports it with `locked` = 2 |
| `c_sniff` | The C client in version 2: a C sender and a Python sender with a 105 ms wake-up preamble, a C and a Python receiver sniffing 1 ms every 100 ms; each receiver detects, locks onto and receives every frame through its sniffs, with exactly the listening, receiving and TX times in the power report; a version-1 C client that listens receives the same frames from their start, without the `LONG_PREAMBLE` flag |
| `tx_rules` | Frames outside `TX` and overlapping frames are not sent and are counted; leaving `TX` while the frame is on air is counted |
| `sniff_version` | A version-2 welcome has `SNIFF` and the server answers its `"xn"`; a version-1 welcome does not, and its `"xn"` and `"xl"` are dropped and counted; leaving `TX` after the frame's airtime but before the end of the long preamble plus the frame is counted |
| `sniff_xn_while_receiving` | `"xn"` while holding a frame, after its preamble: the frame is lost (`left`), no verdict comes, the sniff ends with nothing detected and the frame's power as the energy; `"xn"` during the node's own frame that outlasts the window: the `"xd"` at the window's end, the radio left in `TX` until the frame's end, no rule broken |
| `asymmetric_link` | A link given in both directions with different values: each receiver gets its own direction's power in the medium log, and the server logs the asymmetric link; a link given once is the same both ways |
| `welcome_cca_us` | The welcome's `cca_us`: the profile's common value (160 µs), the mode-3 value with `--power-mode=3` (53 µs), 0 without power recording |
| `sniff_report_in_window` | During a sniff, a report of `OFF` and one of `LISTEN` on another channel end the sniff with no `"xd"` (`left`) and the radio hears nothing more; `LISTEN` and then `RECEIVE` on the sniff channel keep it: a frame that starts in the window is received and the sniff reports it with `locked` = 2 |

```bash
python3 radio_if/test_ref_client.py build-vt/wssimserver      # about 3 s
```

`radio_if/test_mixed.sh <bindir> <workdir> <outdir>` runs `dag_simple` (15 Nanostack nodes) with three clients (two C, one Python) near four of its routers, on one of the Wi-SUN nodes' channels, for 600 virtual seconds, and the same seed without the clients. It checks that every router joins as in the baseline, that the clients receive each other's frames and Wi-SUN frames and the Wi-SUN nodes the clients' frames (each client's log against the medium log), that collisions between the two kinds of node occur and are applied by the medium, that the power report covers every node with exact state sums and each client's TX time, and that the server logged no interface error (about 10 s). With `SNIFF=1` the clients sniff instead (two C clients in version 2 that sniff 1 ms every 100 ms and send with a 110 ms wake-up preamble, a Python client that sniffs 10 ms every 50 ms, a version-1 C client that listens all the time): the test checks the detections, locks and receptions through sniffs, Wi-SUN frames detected and received through sniffs, the version-1 client next to them, the collisions across the two kinds, the power report (TX times with the preambles, listening within the sniffs) and the server's sniff counts against the clients' logs.

## 13. Versioning

- The interface version is negotiated in the hello; the welcome confirms it. The server supports a range of versions (today 1 to 2) and refuses others by closing the connection.
- Additions that a client of an older version can ignore do not change the version: new welcome flags, new server options, new optional client messages, a value in a field that was reserved (ignored on receipt, §4.1). A client ignores unknown welcome flags. Such an addition so far: the welcome's `cca_us` (2026-11-09, both versions).
- Any change that a client of an older version would misread (a field's meaning or a message's layout, a new message the server sends during a wake) gives a new version. The server keeps the older versions for as long as clients use them.

| Version | Adds |
| --- | --- |
| 1 | §1–§12 without §6.7 |
| 2 | Preamble sniffing (§6.7): the client messages `"xn"` and `"xl"`, the server message `"xd"` in a wake, the welcome flag `SNIFF` and the frame flag `LONG_PREAMBLE` |

**Discovering sniffing.** A client that needs sniffing asks for version 2 and checks `SNIFF` in its welcome before it sends `"xn"` or `"xl"`. A server that serves only version 1 refuses the version-2 hello by closing the connection: the client may then connect again with version 1 and run without sniffing, or stop. A version-1 client is served exactly as before: its welcome has no `SNIFF`, it never receives `"xd"` or `LONG_PREAMBLE`, and its `"xn"` and `"xl"` are dropped with a warning and counted (§9), so it cannot wait for an answer that never comes without the server's log saying why.

## 14. Not in this interface

- **Wall-clock mode and the legacy medium.** The interface needs virtual time and the central medium.
- **Physical-layer details** listed in `docs/MEDIUM.md` §9: adjacent-channel interference, fading and shadowing, external interferers, node positions and path-loss models, RX/TX turnaround and settling times (except the sniff's settling time, §6.7), an error on the reported RSSI. A receiver also decodes frames whatever their bit rate.
- **The receive timestamp** is the frame's start (the time of the wake that delivers it); a radio that timestamps the end of the sync word adds `shr_len × 8 × 10⁶ / rate_bps` itself. After a sniff's lock, it is the detection time.
- **MCU active time chosen by the client.** The power report models it from cycle counts (§7); code still runs in zero virtual time, so a slow MCU does not delay the protocol.
- **Peripherals that keep the MCU out of deep sleep** (a UART, for example) cannot be reported yet.
- **Changes during a reception:** the medium checks the node's state at the frame's end only. A channel change reported while in `RECEIVE` does not lose the frame, nor does a short excursion out of `RECEIVE` reported back before the end (unless another frame reaches the node meanwhile); a client that follows §6.5 does neither.
- **One radio per node.**
- **Sniffing limits** (`docs/MEDIUM.md` §10.6): no false detection on noise, detection per frame and not on summed energy, one channel per `"xn"` (a scan of `n` channels costs `n` wakes), no hardware channel hopping between sense windows, and default detection figures that are model values until measured on hardware.
