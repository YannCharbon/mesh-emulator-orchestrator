# Testing a stack that is not Nanostack: emulator, web app and MTS bench

This guide is the starting point for developers of a mesh stack that is not built on Nanostack and lives in its own repository. It explains how to test that stack in the emulator, observe it in the web application (`mesh-testbed-controller`) and then run it on the MTS bench. It gives the order of the work and the commands, and it links to the specifications for the details; it does not repeat them.

Every command in this guide was run once on 2026-11-09, on emulator `main` at `1a6082a`, controller `9297149` and bootloader `12ea43af`, except those that need the bench, which are marked. Paths are relative to the emulator checkout unless stated otherwise; `<root>` is the checkout of `mesh-emulator-rework`, which holds the emulator, the controller, the bootloader and the stack as sibling directories.

## 1. What the environment offers, and its limits

**Virtual time.** The server, `wssimserver --vtime`, is the radio medium and the clock of the whole network. It runs one node at a time: it wakes a node at a virtual time, the node does its work in zero virtual time, and it tells the server when it next needs to wake. A run therefore does not depend on the speed of the PC and is usually much faster than real time. Each node is a separate process connected to the server through a UNIX socket; the protocol is the stack-neutral radio interface ([RADIO_INTERFACE.md](RADIO_INTERFACE.md)). Any language can be used.

**Central radio medium.** The server decides every physical outcome: who hears a frame and at what power, locking, collisions, capture, half duplex, CCA energy and the loss of each frame from its signal-to-noise-and-interference ratio. The client decides everything its radio does and the whole protocol above it ([RADIO_INTERFACE.md](RADIO_INTERFACE.md) §8, [MEDIUM.md](MEDIUM.md)). From interface version 2 a node can sniff a channel for a preamble, lock onto the frame that follows, and send frames with a long wake-up preamble ([MEDIUM.md](MEDIUM.md) §10, [RADIO_INTERFACE.md](RADIO_INTERFACE.md) §6.7).

**Power report.** With a power profile, the server keeps a charge ledger per node from the radio states the client reports and from its wakes, and writes `power.json` at the end of the run ([RADIO_INTERFACE.md](RADIO_INTERFACE.md) §7, [POWER_MODEL.md](POWER_MODEL.md) §6–§7). The ledger knows nothing of the protocol, so the measured EFR32FG25 profiles apply to any stack that drives that radio in the same way.

**Scale.** Runs of up to 4096 node IDs work. A 1000-node building for one virtual hour with a light load (a 20 ms window every second, a beacon flood every 60 s) took 123 s of wall time; wall time is about 7–12 µs per node wake ([SCALE.md](SCALE.md) §1, §3). For the 48-node, four-floor building of §3 below, 300 virtual seconds take about 1 s in the server.

**Determinism.** For a given seed (`WSSIM_SEED`) and given options, two runs give the same events at the same times, provided the client uses no wall-clock time and draws its random numbers from the seed in its welcome ([RADIO_INTERFACE.md](RADIO_INTERFACE.md) §1, §6.4). Node logs, the medium log, the sniff log and `power.json` are then byte-identical between runs; only the server log's connection order and wall-time line differ.

**Limits to keep in mind when reading results:**

| Limit | Effect on results |
| --- | --- |
| Preamble detection figures (probability, dwell, settling time) are model values; the FG25 datasheet gives none | Sniffing results are indicative until the kits measure them ([MEDIUM.md](MEDIUM.md) §10.5–§10.6) |
| Interference is treated like noise in the loss curve, which needs about 20 dB of SINR for 1 % loss on a 50-byte frame; the FG25 datasheet gives a co-channel rejection of −4.4 to −7.2 dB | Collision losses are upper bounds until the kits measure FSK-on-FSK behaviour |
| No adjacent-channel interference, fading, external interferers, node positions or RX/TX turnaround times ([MEDIUM.md](MEDIUM.md) §9) | Topologies give fixed link values; channel plans are not penalised for adjacent channels |
| Code runs in zero virtual time; MCU time is modelled from cycle counts in the profile | A slow MCU does not delay the protocol in the emulator |
| The power profiles are measured on the FG25 kits (an always-listening router and our low-power firmware) | Another radio or MCU needs its own profile ([POWER_MODEL.md](POWER_MODEL.md) §6.1) |
| One process per node; each wake is a round trip between two processes | Wall time grows with the number of wakes, not with the network's size alone |

## 2. Step 1: write a client

**Build the server.** Only `wssimserver` is needed, from a virtual-time build. The build still needs the stack's sources and mbedtls to configure, as described in the emulator [README](../README.md), "Compile mbedtls":

```bash
git clone --branch=v3.0.0 https://github.com/ARMmbed/mbedtls
(cd mbedtls && cmake -G Ninja -DCMAKE_C_FLAGS=-DMBEDTLS_NO_PLATFORM_ENTROPY -DENABLE_PROGRAMS=OFF -DENABLE_TESTING=OFF . && ninja)
cmake -S . -B build-vt -G Ninja -DNANOSTACK_PORT=nanostack_port_sim -DCMAKE_PREFIX_PATH=$(pwd)/mbedtls \
    -DNANOSTACK_DIR=<root>/nanostack-generic-standalone -DCMAKE_C_FLAGS="-std=gnu17 -fpermissive"
ninja -C build-vt wssimserver
python3 radio_if/test_ref_client.py build-vt/wssimserver     # the server's interface tests, about 2 s
```

**What to implement first,** in this order, each section of [RADIO_INTERFACE.md](RADIO_INTERFACE.md) being the reference:

1. Connect, hello and welcome (§2, §4.2–§4.3): ask for version 2 if the stack sniffs, and check the `SNIFF` flag in the welcome. Take the seed, clock drift and start time from the welcome.
2. The wake loop (§2, §5): send `"xi"` with the next deadline, read `"xa"` and exactly the number of messages it announces, then handle them before the node's own timers. The skeleton in §11 is the minimal correct loop.
3. The node's clock and timers (§6.3) and its random numbers from the seed (§6.4).
4. Radio-state reports (§6.1): every change of the radio's state, including `OFF` between windows; the medium and the power report both depend on them.
5. Receiving (§6.5): a frame `"xx"` followed by its verdict `"xv"`; accept any PSDU of any length up to 4096 bytes, since other nodes' frames may be foreign.
6. Transmitting with CCA (§6.2, §6.6): report `TX` before sending, and leave `TX` only after the frame's airtime.
7. Sniffing and long-preamble frames if the protocol uses them (§6.7).
8. `MOP1` lines in the node's output (§10, "Node logs"): the first word of every line is the virtual time in µs, and a `MOP1` line carries one JSON report or event. A report with the node's ID, its state and its parent is enough for the web app ([STACK_NEUTRAL_MODEL.md](../../mesh-testbed-controller/docs/STACK_NEUTRAL_MODEL.md), "Minimum implementation").
9. Optionally the `"xc"` wake report (§4.10) to charge the MCU's work per cause, and MOP 1.1 settings read from a schedule file (§5 of this guide).

The clients in `radio_if/` (Python and C) illustrate the interface. The stack's client is written from the specification and is not copied from them (§6).

**Test against the server by hand** before using any script: three nodes, a topology on the command line, the medium log and the power report.

```bash
SOCK=/tmp/mystack.sock
WSSIM_SEED=1 build-vt/wssimserver --vtime=3 --vtime-end=60 -g 1,2:-70 -g 1,3:-70 -g 2,3:-75 \
    --medium-log=medium.log --power=examples/power/efr32fg25-lfn.ini --power-out=power.json \
    $SOCK > simserver.log 2>&1 &
for i in $(seq 200); do [ -S $SOCK ] && break; sleep 0.05; done
for n in 1 2 3; do my_client --sock $SOCK --id $n > if$n.log 2>&1 & done
wait
```

Then check:

- the end of `simserver.log`: the virtual time reached, wakes and frames, the medium's outcomes, one power line per node, and no interface warning (the warnings and their causes are in [RADIO_INTERFACE.md](RADIO_INTERFACE.md) §9);
- `medium.log`: one line per frame and receiver, `t_end_us src dst frame_id outcome rx_dbm sinr_min_db`; `ok` where a frame should have been received, `not_listening` where the receiver's radio was off;
- that a second run with the same seed gives identical node logs, medium log and `power.json` (§3 below).

## 3. Step 2: run a network

**Topology.** A topology file has one link per line, `A,B:RSSI` (the power at B of A's frames, and at A of B's frames unless a line `B,A:RSSI` gives the reverse direction); the format is in [sim_utils/vtime/README.md](../sim_utils/vtime/README.md), "Topology files", and the rules in [MEDIUM.md](MEDIUM.md) §2. A link without a value is a perfect 0 dBm link, which destroys every frame it overlaps; always give values when collisions matter. For a building, use the generator rather than writing another one ([SCALE.md](SCALE.md) §2, "The building generator on its own"):

```bash
python3 radio_if/scale/gen_building.py --nodes 48 --floors 4 -o topo48.txt --positions topo48.pos
# topology: {"nodes": 48, "floors": 4, "grid": "3x5", "links": 478, ..., "hops_max": 2}   (on stderr)
```

The generator draws nothing at random: the same options give the same file.

**Run with `run.sh`.** `sim_utils/vtime/run.sh` starts the server and one client per ID in `IF_NODES`, writes one log per client and records the run's settings ([sim_utils/vtime/README.md](../sim_utils/vtime/README.md), "run.sh"). With node 0 among the clients, the run has no Nanostack program and needs no TUN device:

```bash
TOPO=topo48.txt IF_NODES="$(seq -s ' ' 0 47)" IF_CMD=/abs/path/to/my_client WSSIM_SEED=1 TUN=none \
    POWER=examples/power/efr32fg25-lfn.ini SIM_OPTS="--medium-log=medium.log --sniff-log=sniff.log" \
    sim_utils/vtime/run.sh build-vt . runs/mystack/seed1 300
```

- `IF_CMD` is the client's command, given `--sock <socket> --id <i>`; `IF_OPTS_<i>` and `IF_CMD_<i>` give one node its own arguments or program. The programs run in the output directory, so `IF_CMD` must be an absolute path or a command on `PATH`.
- `SIM_OPTS` passes any server option (`wssimserver --help`): the medium and sniff logs, clock drift (`--drift-random=20`), cut links and link changes in virtual time (`CUT`, `LINK`), late power-up (`POWER_UP`, `SPREAD`).
- The output directory must be new or empty.

**Choice of power profile** ([RADIO_INTERFACE.md](RADIO_INTERFACE.md) §7, "Which profile and `phy_mode`"): `examples/power/efr32fg25-lfn.ini` for battery nodes that open short receive windows or sniff and sleep in between; `examples/power/efr32fg25-soc.ini` for nodes whose receiver is on most of the time. Report the `phy_mode` number that matches the client's bit rate and modulation index, or 0; with 0 the profile's common values (EU mode 1a, 50 kbit/s) apply. Every value is a user setting: copy a profile and edit it for another radio, or give one per node with `POWER_OPTS="--power-node=IDS:PROFILE"`.

**Seeds and repeatability.** One seed per run (`WSSIM_SEED`). To check that the client is deterministic, run the same seed twice into two directories and compare:

```bash
cd runs/mystack/seed1
for f in if*.log medium.log sniff.log power.json; do cmp -s $f ../seed1b/$f || echo "differs: $f"; done
```

Size campaigns to what they must show: a few seeds of a reasonable length, not many repeats.

**Logs to read** in the run directory:

| File | Content |
| --- | --- |
| `simserver.log` | Settings of the medium and of sniffing, then at the end: virtual time reached, wakes, frames sent and delivered, outcomes at receivers, CCA queries, sniff outcomes, one power line per node; warnings about the interface |
| `medium.log` | One line per frame and receiver with its outcome (`ok`, `noise`, `interference`, `captured`, `below_lock`, `busy`, `not_listening`, `tx_abort`, `left`): [MEDIUM.md](MEDIUM.md) §7 |
| `sniff.log` | One line per sniff: request, window, outcome (`detected`, `none`, …), frame, power, SINR, lock: [MEDIUM.md](MEDIUM.md) §10.5 |
| `power.json` | Per node: average current, charge, time and charge per MCU and radio state, wakes, the sum check: [POWER_MODEL.md](POWER_MODEL.md) §7. `python3 sim_utils/power_summary.py <run directory>` prints a table |
| `if<i>.log` | The client's own output, with its `MOP1` lines |
| `run_info.txt`, `exit_codes.txt`, `topology.txt` | Seed, nodes, options, the wall-clock start and end; each program's exit status; the topology as used |

**Large runs.** Above a few hundred nodes, start the server and the clients by hand with `--group-file` instead of `run.sh` (§7): a 1000-node building took 2.5 min for `run.sh` to prepare and 6 s by hand, with identical results.

```bash
python3 radio_if/scale/gen_building.py --nodes 1000 -o topo1000.txt
SOCK=/tmp/mystack-$$.sock; N=1000
WSSIM_SEED=1 build-vt/wssimserver --vtime=$N --vtime-end=60 --group-file=topo1000.txt \
    --power=examples/power/efr32fg25-lfn.ini --power-out=power.json $SOCK > simserver.log 2>&1 &
for i in $(seq 200); do [ -S $SOCK ] && break; sleep 0.05; done
for i in $(seq 0 $((N - 1))); do my_client --sock $SOCK --id $i > if$i.log 2>&1 & done
wait
```

Plan the memory as the client's resident size × N ([SCALE.md](SCALE.md) §3–§4).

## 4. Step 3: observe the network in the web app

The web app reads each client's `MOP1` lines and shows the nodes, their states, parents and links, counters and events ([STACK_NEUTRAL_MODEL.md](../../mesh-testbed-controller/docs/STACK_NEUTRAL_MODEL.md), "The emulator" and "Nodes of other stacks: clients of the radio interface"). Installation of .NET and the settings are in the controller's [README](../../mesh-testbed-controller/README.md).

**Start an instance for the emulator only.** The default settings also open the real bench: always switch it off in an instance used for emulator work.

```bash
cd <root>/mesh-testbed-controller
./run.sh --urls http://127.0.0.1:5091 --Environments:MtsReal=false --Environments:MtsSimulated=false \
    --Environments:Emulator=true --Environments:Default=Emulator \
    --Emulator:EmulatorDir=<emulator checkout> --Emulator:Tun=none \
    --Emulator:ExportRoot=<dir>/emulator --Data:ConfigDir=<dir>/configs \
    --Data:RecordDir=<dir>/records --Data:EventLogFile=<dir>/events.log
```

**Write the configuration.** A configuration names the stack, its program and arguments, the nodes and the topology ([STACK_NEUTRAL_MODEL.md](../../mesh-testbed-controller/docs/STACK_NEUTRAL_MODEL.md), "Configuration of nodes of other stacks, and the neutral TX power"). It can be edited as JSON on the Configuration page, or written as a file in the configuration directory. This script turns a topology file into a configuration in which every node runs the stack:

```python
import json, sys
# mkconfig.py <topology file> <name> <program> > <config dir>/<name>.json
topo, name, program = sys.argv[1:4]
links, ids = [], set()
for line in open(topo):
    line = line.split("#")[0].strip()
    if not line:
        continue
    ab, rssi = line.split(":")
    a, b = (int(x) for x in ab.split(","))
    links.append({"a": a, "b": b, "rssiDbm": float(rssi)})
    ids |= {a, b}
print(json.dumps({
    "schema": 1, "name": name,
    "radio": {"txPowerDbm": 14},
    "topology": {"kind": "edges", "links": links},
    "stacks": {"my-stack": {"program": program, "arguments": "", "settings": {}, "txPowerSetting": "tx-dbm"}},
    "run": {"durationS": 300, "seeds": [1], "virtualTime": True},
    "nodes": [{"id": i, "stack": "my-stack"} for i in sorted(ids)],
}, indent=1))
```

- `txPowerSetting` names the client's argument for the TX power: the neutral TX power `radio.txPowerDbm` is passed as `--tx-dbm 14`. Settings in `stacks.<name>.settings` and `nodes[i].settings` become `--name value` arguments; a value must not contain spaces or quotes.
- `run.mediumTxRefDbm` makes the link values the received power at that TX power, so that a node's TX power changes what others receive.
- `"program"` may use `$EMU`, the emulator checkout (for example `python3 $EMU/radio_if/ref_client.py`).

**Run it.** On the Configuration page: choose the configuration, **Load**, **Plan**, **Apply selected steps** (this writes `config.json`, `topology.txt` and `run.sh` into `<ExportRoot>/<name>/`). On **Runs and recording**: **Start**. **Nodes**, **Mesh** and **Events** then show the run; event times carry the run's virtual time ("(run t=97.803 s)"). Do these steps in one browser tab (§7).

**Power report and other options from a web-app configuration.** The web app's runs do not record power yet. Run the exported script instead, which passes the environment through to `run.sh`; its runs appear under **Show a run**:

```bash
cd <ExportRoot>/<name>
POWER=<emulator checkout>/examples/power/efr32fg25-lfn.ini SIM_OPTS="--medium-log=medium.log" ./run.sh 2
```

**Setting changes at a virtual time.** In the emulator a setting changes at a virtual time given in the configuration, not from the node details, so that a run stays repeatable ([STACK_NEUTRAL_MODEL.md](../../mesh-testbed-controller/docs/STACK_NEUTRAL_MODEL.md), "Setting changes during an emulator run"). The client must list its settings in its reports (`ops`, `settings`) and read a schedule of MOP `set` requests from the file named by `setFileArgument`:

```json
"stacks": { "my-stack": { "program": "...", "txPowerSetting": "tx-dbm", "setFileArgument": "--mop-set-file" } },
"settingChanges": [ { "atS": 60, "node": 1, "settings": { "tx_power_dbm": "0" } } ]
```

The export writes `mop-set-1.txt` (`60 {"mop":1,"op":"set",...}`); the client applies the request at its first wake at or after 60 s and prints the `ack` and a `settings` event as `MOP1` lines ([STACK_NEUTRAL_MODEL.md](../../mesh-testbed-controller/docs/STACK_NEUTRAL_MODEL.md), "Settings (revision 1.1)"). `tx_power_dbm` is the well-known name for the TX power; a stack's own settings have a dot in their name (`my.window_ms`).

## 5. Step 4: the MTS bench

The bench has 13 STM32F769I-DISCO boards with an S2-LP radio shield, on a closed lab LAN, with our bootloader on every board ([Test_Bench_MTS.md](../../documents/pre-study/investigation/Test_Bench_MTS.md), [mts-bootloader README](../../mts-bootloader/README.md), [MTS_Bootloader_Design.md](../../documents/mts/MTS_Bootloader_Design.md)). The bench is shared: agree its use with its owner, and use the controller, which takes the per-node locks, rather than ad hoc tools.

**Image contract** ([STACK_NEUTRAL_MODEL.md](../../mesh-testbed-controller/docs/STACK_NEUTRAL_MODEL.md), "Contract for an image that is not our Wi-SUN firmware"):

- link the code at 0x08080200; the 512-byte header goes at 0x08080000 and is written by the signing tool. The application slot holds at most 1535 KB, header included. Keep every section in the slot (sector 3 at 0x08018000 is free for the application's data, but a section there makes the flat binary span the gap);
- leave the RAM block 0x2007FF00–0x2007FFFF unused (bootloader handoff);
- refresh the independent watchdog within `boot.wdg_s` (16 s by default);
- either be installed as permanent (no trial), or confirm the trial boot once the image's own management link works; an image that never confirms is rolled back after three boots. The confirmation is a word written in the slot trailer ([MTS_Bootloader_Design.md](../../documents/mts/MTS_Bootloader_Design.md) §4.3, §7.3): the trailer is the last 1 KB of the slot (0x081FFC00), and the `CONFIRMED` flag is the first erased word of the four at trailer offset 0x10, programmed with 0x464E4F43 ("CONF", `MTSBL_TR_CONFIRMED` in the bootloader's `include/mtsbl/image.h`). During development, installing as permanent is simpler;
- to be reset and updated over Ethernet, serve MOP with `reset` on UDP 42583 (or the bootloader's management protocol on UDP 42580). An image with neither is recovered only at the board (§7).

**Sign the image** with the bench key on the bench PC (the bench bootloaders verify the signature against the bench key; the committed test key is for host tests and development builds only):

```bash
cd <root>/mts-bootloader
python3 tools/mtsimg.py sign --type app --load-addr 0x08080000 --version 0.1.0 --name my-stack \
    --key ~/.config/mts-bootloader/bench-signing.key -o my-stack.img my-stack.bin
python3 tools/mtsimg.py show my-stack.img        # "... key ea7bfd8b80de9d2a ... OK"
```

`--load-addr` is the slot address (the header's position), not the code's. The input may also be an ELF, converted with `arm-none-eabi-objcopy`.

**MOP over UDP** ([STACK_NEUTRAL_MODEL.md](../../mesh-testbed-controller/docs/STACK_NEUTRAL_MODEL.md), "Mesh Observation Protocol, version 1"): the same JSON as the `MOP1` lines, one object per datagram on UDP 42583, at most 1400 bytes. The minimum is to answer `get`, unicast and broadcast, with a report that has the node's ID or EUI-64, its state and its parent; `subscribe`, `reset` and `set` follow. Test the handler on the host before the bench, as the probe image does (`mts-bootloader/tests/probe_mop/`); a `get` on loopback:

```bash
python3 -c 'import socket,json; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.settimeout(2); s.sendto(json.dumps({"mop":1,"op":"get","seq":1}).encode(),("127.0.0.1",42583)); print(s.recvfrom(1500)[0].decode())'
```

The probe image (`mts-bootloader/app-probe/`) is a minimal image without Nanostack that implements this contract and MOP 1.1; it shows the behaviour expected, and like the emulator's clients it is not copied (§6).

**Install, observe, reset and roll back through the controller** (not run for this guide: they need the bench). On the bench PC, start the controller with writes allowed (`./run.sh --Mts:AllowWrites=true`; README "Run" and "Settings"). Then:

- **Firmware** page: upload the signed image to one or several nodes, as a trial or with **permanent (no trial)**, with progress, slots and rollback. For a node that serves only MOP, the controller resets it into the bootloader through MOP, installs the image, and checks its arrival from its MOP report (image name, version, trial flag) ([STACK_NEUTRAL_MODEL.md](../../mesh-testbed-controller/docs/STACK_NEUTRAL_MODEL.md), "What the controller does for a node that serves only MOP");
- **Nodes**, **Mesh**, **Events**: the node appears as "application (MOP)"; its reports, parent and pushed events are shown as in the emulator;
- **Reboot**: MOP `reset`, normal or into the bootloader;
- **Settings**: MOP `set` from the node details for any setting the node lists; the neutral TX power reaches every node that lists `tx_power_dbm`. Settings needed at the first start are written as bootloader configuration keys 0x8000–0xFFFE through `stacks.<name>.benchKeys` in the configuration, before the image update;
- **Back to the Wi-SUN image**: install it through the Firmware page as for any image.

The simulated bench of the controller (`--Environments:MtsSimulated=true`, with `Mts:SimProbeNodes`) shows these pages with a simulated probe, without hardware.

## 6. Rules

- **No copied code.** The stack and its client are written from the specifications. Nothing is copied from the emulator, from Nanostack or our generic stack, from the reference clients, from the probe image or from `libmtsbl`, and none of these is linked into the stack's image. The emulator, the controller and the bench are test tools that the stack runs against; only knowledge crosses (interfaces, measured figures).
- **Reproducibility.** No wall-clock time and no random source other than the welcome's seed in the client. Each reported result names the emulator commit, the client's commit, the seed(s), the topology file or generator options, the server options, the power profile and the duration. Keep the topology file with the results.
- **What to report** for a result: the setup above; the figures with their range over seeds and the number of runs; delivery from the node logs and the medium log; power from `power.json` with the profile named; the limits of §1 that apply (sniffing model values, pessimistic collisions, the profile's hardware).
- **Settings** change at a virtual time in the configuration, never during a run from outside.
- **The bench** is used through the controller, with the owner's agreement, and left as found (Wi-SUN images on every node unless agreed otherwise).

## 7. Troubleshooting

| Symptom | Cause and remedy |
| --- | --- |
| `cmake` fails on `MbedTLS` or on the stack's directory | The virtual-time build configures the whole project even for `wssimserver`: build mbedtls and point `-DNANOSTACK_DIR` at a checkout of `nanostack-generic-standalone` (§2) |
| A client exits with "can't open file" or "not found" under `run.sh` | The programs run in the output directory: give `IF_CMD` as an absolute path or a command on `PATH` |
| `run.sh` takes minutes before the run starts, or fails with "Argument list too long" | It reads the topology line by line and passes each link as `-g`: slow above a few hundred nodes and limited to about 2000. Start the server by hand with `--group-file` (§3) |
| `run.sh: ... is not empty` | Each run needs a new output directory |
| The server closes the connection right after the hello | Wrong size or version, an ID used twice or above 4095, more clients than `--vtime=N`, or a server without `--vtime`: the warning is in `simserver.log` ([RADIO_INTERFACE.md](RADIO_INTERFACE.md) §9) |
| The run stops when one client exits | A client that disconnects during the run stops it; read that client's log |
| Frames lost where they should arrive: `not_listening` | The receiver's reported state was `OFF` or the channel differed when the frame started |
| Every overlapping frame lost | A link without a value is 0 dBm and destroys any frame it overlaps: give link values |
| A link behaves differently in each direction | A line `B,A:RSSI` sets the reverse direction; the server warns when a later line changes a value ([MEDIUM.md](MEDIUM.md) §2) |
| Two runs with the same seed differ | The client reads the wall clock or another random source, or iterates over something unordered; compare the node logs to find the first difference |
| The power report is missing from a web-app run | Run the exported `run.sh` with `POWER` (§4) |
| A run made by hand is not offered under "Show a run" | The web app lists the runs under its export directory: run the exported `run.sh` there |
| The run started from the web app is not the configuration just loaded | The active configuration belongs to the browser tab: load it and start the run in the same tab |
| A node is shown "unknown" | Its log has no `MOP1` report line, or the line's first word is not the virtual time in µs |
| A second controller instance contacts the real bench | Its default settings enable the real bench: start it with `--Environments:MtsReal=false` |
| The power figures look like mode 1a at another bit rate | The client reports `phy_mode` 0: report the number matching its PHY ([RADIO_INTERFACE.md](RADIO_INTERFACE.md) §7) |
| An image on the bench is rolled back after three boots | It never confirmed its trial: install it as permanent, or confirm once its management link works (§5) |
| A node on the bench no longer answers on the network | Without MOP `reset` or the management protocol, reset it at the board while the host sends `HOLD` (`tools/mtsbl.py hold all --seconds 60`), or reset it three times so that the bootloader rolls back ([MTS_Bootloader_Design.md](../../documents/mts/MTS_Bootloader_Design.md) §13) |
