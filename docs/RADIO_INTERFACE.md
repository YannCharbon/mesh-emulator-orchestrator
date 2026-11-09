# Radio interface: running any protocol stack as an emulated node

This document specifies how a program connects to the emulator as a radio node, sends and receives frames through the emulated radio medium, keeps time and sleeps. It is written for developers who implement a client in their own stack, in any language, from this document alone: no emulator code, no Nanostack code and no header of this repository is needed.

**Interface version 1.** The server side is `wssimserver` in virtual-time mode (`--vtime`) with the central radio medium (the default in that mode). A reference client in Python is in `radio_if/` (§12); it is an example, and this document is normative.

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
                                     <-------- k messages: "xx" frames received, "xv" verdicts
work at time t (messages, then own timers) ...
"xi" idle {next deadline} ------------------->
...
                                     <-------- "xq" end of the run
close the connection and exit
```

1. **Connect.** The server listens on a UNIX socket of type `SOCK_SEQPACKET`; its path is the last argument of `wssimserver`. Linux limits such paths to 107 bytes.
2. **Hello** (`"xh"`, §4.2). It gives the node ID, the flags `VTIME | RADIO_IF` and the interface version. The server refuses a hello it cannot serve by closing the connection (§9).
3. **Welcome** (`"xw"`, §4.3). The server sends the welcomes once all the nodes of the run (`--vtime=N`) have said hello, one node at a time in ascending ID order. Each welcome starts the node's **first run**, at virtual time `start_us` (0, unless the node is powered up later, §6.3).
4. **Run.** During a run the client may send radio-state reports, frames and CCA queries. A run ends when the client sends `"xi"` (idle) with its next deadline.
5. **Wake** (`"xa"`, §4.4). The server wakes the node at the time of its deadline, or earlier when a frame or a verdict arrives for it. The `"xa"` gives the new time and the number of messages that follow it; the client reads exactly that many. The client then does its work at that time and sends `"xi"` again.
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
| Clock drift | parts per billion (ppb), signed 32-bit |
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
| 6 | 2 | version | the interface version the client implements: 1 |

### 4.3 Welcome `"xw"`: server → client, 32 bytes

| Offset | Size | Field | Value |
| --- | --- | --- | --- |
| 0 | 2 | magic | `"xw"` |
| 2 | 2 | version | the interface version in use: the one in the hello |
| 4 | 4 | flags | `POWER` (0x0001): the server records power, so the client may send `"xc"` (§7). `MEDIUM` (0x0002): the central medium decides reception; always set for clients of this interface. Ignore unknown bits |
| 8 | 8 | seed | the run's base seed (§6.4) |
| 16 | 4 | drift_ppb | the node's clock error, signed (§6.3) |
| 20 | 4 | reserved | |
| 24 | 8 | start_us | the virtual time of the node's start (power-up); 0 for a node that starts with the run |

The welcome is also the first wake: the node's current time is `start_us`.

### 4.4 Wake `"xa"`: server → client, 16 bytes

| Offset | Size | Field | Value |
| --- | --- | --- | --- |
| 0 | 2 | magic | `"xa"` |
| 2 | 2 | count | number of messages that follow (frames and verdicts), 0 to 65535 |
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
| 14 | 1 | flags | 0 | `0x01` (`MEDIUM`): the medium locked this receiver onto the frame |
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

## 5. Sequencing in virtual time

### 5.1 How the server orders the network

- The server holds a queue of events: each node's deadline (from its last `"xi"`; a new `"xi"` replaces it) and messages due to a node (frames at their start, verdicts at their end).
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
- A channel change is a report with the new channel; the state may stay the same.
- Report `RECEIVE` in the wake that delivers a locked frame (§6.5), and leave it when the verdict comes.
- A report applies from the current time. A frame already on air when the node starts listening is not received (the receiver missed its preamble), but it does count as interference and as CCA energy.

### 6.2 Transmitting

To send a frame at time `t` ("timed TX"), the client sets a deadline at `t` and, in that wake:

1. reports `TX` with the transmit power (`"xr"`);
2. sends the frame (`"xx"`); it starts at `t`;
3. computes the frame's end, `t + airtime`, and sets a deadline there:

   `airtime_us = (shr_len + phr_len + len + fcs_len) × 8 × 1 000 000 / rate_bps`, with integer division, the formula the medium uses;
4. at the end, reports the next state (`LISTEN`, `IDLE` or `OFF`).

Rules, checked by the server: a frame is sent only while the node's reported state is `TX`, and not before the end of the node's previous frame. A frame that breaks a rule is **not sent**; the server logs a warning and counts it in its end-of-run summary. The medium never delays or refuses a frame for any other reason: there is no automatic CSMA, backoff or acknowledgement. The sender receives no confirmation; its frame ends at its own computed end.

### 6.3 Clock, timers and start time

- The client's notion of the current time is the last `now_us` (or the welcome's `start_us`). Virtual time is the same for every node.
- **The node's own clock** models its crystal. It counts from the node's start and drifts by `drift_ppb`:

  `local(v) = e + floor(e × drift_ppb / 10⁹)`, with `e = v − start_us`.

  A timer of `d` µs on the node's clock, started at virtual time `v0`, fires at the first virtual time `v` with `local(v) ≥ local(v0) + d`. The deadlines in `"xi"` are always **virtual** times: a client that models drift converts its local deadlines to virtual time with this formula. With `drift_ppb` = 0 and `start_us` = 0 the node's clock is virtual time. A client that does not model drift may ignore `drift_ppb`, and the run then behaves as if `--drift` were 0 for that node.
- **Late power-up and restart.** With `--vtime-power-up=ID:SECONDS` the node connects with the others but is welcomed at that time: until then it does not run, and its radio is off. With `--vtime-start=ID:SECONDS` a second process with the same node ID connects at the start of the run and is welcomed at that time, after the server has stopped the first one. In both cases the welcome's `start_us` is that time, and the node's clock starts at 0 there.

### 6.4 Randomness

The welcome's `seed` is the run's base seed (`WSSIM_SEED`). For reproducible runs the client draws every random number from a generator seeded from `seed` and its node ID, and never from the operating system. The derivation is the client's choice; it should give different nodes uncorrelated streams. The reference client draws none.

### 6.5 Receiving

1. The client reports `LISTEN` on a channel. Its receive window is open from that time ("timed RX window").
2. When a frame starts on that channel at a node that hears its sender, the medium decides whether the node **locks** onto it (§8). If it does, the node gets the frame (`"xx"` with flag `MEDIUM`) in a wake at the frame's **start**, with the received power in `rx_dbm`. Frames the node does not lock onto are never delivered.
3. The client reports `RECEIVE` in that wake and holds the frame.
4. At the frame's **end** the node gets the verdict `"xv"` for that `frame_id`. If `ok` is 1 the protocol receives the PSDU; if it is 0 the frame is dropped, as a failed checksum would be. The client then reports its next state.

Exceptions while a frame is held:

- **Capture.** A new `"xx"` arrives before the verdict: a stronger frame captured the receiver during the first frame's preamble and sync word. The client drops the first frame and holds the new one; no verdict comes for the first.
- **Leaving.** If the client reports any state other than `RECEIVE` while it holds a frame (it turns the radio off, or starts to transmit), the frame is lost and no verdict comes. Transmitting while receiving is how half duplex appears: the medium records the outcome `tx_abort`.
- A verdict for a frame the client does not hold is ignored.

A receive window that closes while a frame is being received normally waits for the verdict, as a real radio's driver does; the client chooses.

### 6.6 Clear-channel assessment

The client sends `"xs"` with a channel and receives the energy at its antenna on that channel at the current time: the noise floor (−119.93 dBm) plus every frame on air on that channel from the node's neighbours, in dBm. The node's own frame is excluded. The client compares the energy with its own threshold; the server has no threshold. A CCA takes no virtual time. For the power report, the client then sends `"xr"` with its current state and the `CCA` event, and the server charges the profile's CCA duration at the end of the listening before it.

### 6.7 Preamble and energy sniff (reserved)

> **Reserved for a later revision.** Detection of a preamble or of energy during a short listen, without receiving a frame, is being added to the medium separately. Its messages and rules will be specified in this section. Until then, a client can test the channel with a CCA query (§6.6) or open a short receive window (§6.5).

## 7. Power report

With `--power=PROFILE` the server keeps a charge ledger per node and writes `power.json` at the end of the run (`docs/POWER_MODEL.md` §7). It takes everything from this interface:

- **Radio:** the states of the `"xr"` reports, each charged with the profile's current for that state (`TX` per `tx_dbm`), the transitions between them, and the CCA events.
- **MCU:** a wake starts at each `"xa"` (and at the welcome) and ends at the next `"xi"`. Its active time is modelled from the profile's cycle counts: a fixed cost per wake, plus a cost per frame delivered, per frame received (`RECEIVE` reports) and sent (`TX` reports), per unit of work and per timer expiry of each cause given in `"xc"`. The rest of the interval until the next wake is sleep. The sleep is deep when the radio is `OFF` (or `IDLE`, if the profile says an idle radio does not keep the fast clock) and the idle time is long enough to make deep sleep cheaper; otherwise it is light sleep.
- **`"xc"` is optional.** Without it each wake costs the fixed cost plus its frames.
- **`phy_mode`:** with 0, the profile's common values apply; a profile may hold values per Wi-SUN FSK PHY mode (1 = mode 1a … 8 = mode 5, `docs/POWER_MODEL.md` §6.2), selected when the client reports the mode it runs.

Every current, time and cycle count is in the profile, which the user chooses (`examples/power/*.ini`); the client does not send currents.

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

Links are directional and given in dBm. A link without a value (`-g A,B` alone) is a perfect link at 0 dBm, which destroys any frame it overlaps; give values when collisions matter. Without any `-g` option every node hears every other one, at 0 dBm.

## 9. Errors

| Case | What the server does |
| --- | --- |
| Hello of the wrong size or magic, node ID above 4095, unknown flag bits | closes the connection; warning |
| Interface version outside the server's range (1 to 1) | closes the connection; warning with the supported range |
| `RADIO_IF` without `VTIME`, or with `--medium=legacy`, or a server without `--vtime` | closes the connection; warning |
| A node ID already connected, or more hellos than `--vtime=N` | closes the connection; warning |
| A node disconnects before the run starts | drops it and waits for another hello |
| A node disconnects during the run | stops the run; exit status 1 |
| Frame whose length differs from its header's `len`, or whose airtime cannot be computed (`rate_bps` = 0) | drops the frame; warning |
| Frame while the node's reported state is not `TX`, or before the end of its previous frame | does not send it; warning (at the 1st, 2nd, 4th, 8th… occurrence) and a count per node at the end |
| `"xr"` with an unknown state | ignores it; warning and count |
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

Useful server options (all in `wssimserver --help`): `--vtime-end`, `-g` (topology and link values), `--vtime-link`, `--vtime-cut`, `--vtime-kill`, `--vtime-start`, `--vtime-power-up`, `--drift`, `--drift-random`, `--medium-*` (thresholds, the per-frame log, §8), `--power*`, `--vtime-frames` (every frame sent, with its PSDU in hex). `--vtime-drop` reads the PSDU as an IEEE 802.15.4 frame (its sequence number) and is meaningful only for such frames. `--power` cannot be combined with `--vtime-kill` or `--vtime-start`.

The server's log ends with the virtual time reached, the wake and frame counts, the medium's outcome counts and, with `--power`, one line per node. The medium log (`--medium-log`) has one line per frame and receiver: `t_end_us src dst frame_id outcome rx_dbm sinr_min_db`, with outcome `ok`, `noise`, `interference`, `captured`, `below_lock`, `busy`, `not_listening`, `tx_abort` or `left`.

Clients of this interface and Nanostack nodes (`wsnode-inproc`, `wsbrd-inproc`) can share a run, since they share the medium; each hears the other's frames as foreign bytes. This combination is not tested.

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
        transmit:      e = CCA "xs" {chan}; send "xr" {state, CCA event}
                       if e < threshold: send "xr" {TX, chan, dBm}; send "xx" {...}
                                         timer tx_end = now + airtime
        tx_end:        state = LISTEN or OFF; send "xr" {state, chan}
```

## 12. The reference client and its tests

`radio_if/ref_client.py` (Python standard library only) implements this interface (`RadioIf`) and a small protocol on top of it (`WindowNode`): periodic receive windows on the node's own clock, the radio off between them, frames at given offsets in the window with CCA before each. `radio_if/test_ref_client.py` runs it against `wssimserver`:

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
| `hello_refused` | The server refuses an unknown version, unknown flags, the interface without virtual time, and the legacy medium |
| `tx_rules` | Frames outside `TX` and overlapping frames are not sent and are counted |

```bash
python3 radio_if/test_ref_client.py build-vt/wssimserver      # about 1 s
```

## 13. Versioning

- The interface version is negotiated in the hello; the welcome confirms it. The server supports a range of versions and refuses others.
- Additions that a version-1 client can ignore do not change the version: new welcome flags, new server options, new optional client messages. A client ignores unknown welcome flags.
- Any change that a version-1 client would misread (a field's meaning or a message's layout, a new message the server sends during a wake) gives a new version. The server will keep version 1 for as long as clients use it.

## 14. Not in version 1

- **Wall-clock mode and the legacy medium.** The interface needs virtual time and the central medium.
- **Physical-layer details** listed in `docs/MEDIUM.md` §9: adjacent-channel interference, fading and shadowing, external interferers, node positions and path-loss models, RX/TX turnaround and settling times, an error on the reported RSSI. A receiver also decodes frames whatever their bit rate.
- **The receive timestamp** is the frame's start (the time of the wake that delivers it); a radio that timestamps the end of the sync word adds `shr_len × 8 × 10⁶ / rate_bps` itself.
- **MCU active time chosen by the client.** The power report models it from cycle counts (§7); code still runs in zero virtual time, so a slow MCU does not delay the protocol.
- **Peripherals that keep the MCU out of deep sleep** (a UART, for example) cannot be reported yet.
- **One radio per node.**
- **Preamble and energy sniff** (§6.7).
