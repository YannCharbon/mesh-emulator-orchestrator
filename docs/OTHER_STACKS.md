# Testing a stack that is not Nanostack: emulator, web app and MTS bench

This guide is the starting point for developers of a mesh stack that is not built on Nanostack and lives in its own repository. It explains how to test that stack in the emulator, observe it in the web application (`mesh-testbed-controller`) and then run it on the MTS bench. It gives the order of the work and the commands, and it links to the specifications for the details; it does not repeat them.

Every command in this guide was run once on 2026-11-09, on emulator `main` at `1a6082a`, controller `9297149` and bootloader `12ea43af`, except those that need the bench, which are marked. The server-only build (§2), `run.sh` with relative client paths and large topologies (§3), and the web-app steps of §4 (on a test instance of controller `1c346d6`) were run again after the fixes of the same day; the plan of §4 with a relative topology file, on the emulator and on the simulated bench, was run again on controller `4cdfe18`. After the second dry run (an 80-node client written from this guide alone), the commands of §2 and §3 were run again on emulator `b6a9397`: the server-only build, `test_ref_client.py`, the hand test, and `run.sh` with the 80-node client, a relative per-node profile in `POWER_OPTS` and the medium and sniff logs (results identical to the dry run's except the paths), and the error cases of §7 (a missing profile, a socket path that is too long, a server that fails during a run). The web-app steps of §4 that changed with controller `d3188e4` were run again on a test instance of that controller with emulator `main` at `78c52d1` and its server-only build: the build directory check, the run options in the configuration, the schedule file, the files of the run on the Runs page, the Events page's run column, filter and clearing, and the run shown at start-up. Paths are relative to the emulator checkout unless stated otherwise; `<root>` is the checkout of `mesh-emulator-rework`, which holds the emulator, the controller, the bootloader and the stack as sibling directories.

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
| Collisions, capture and adjacent-channel interference follow FG25 kit measurements (EU modes 3 and 1a, wanted signal far above sensitivity, no fading; [MEDIUM.md](MEDIUM.md) §2.1); capture and break are steps at their margins, other PHYs take the mode-3 values | Collision losses are close to the kits' for strong links; near sensitivity they are slightly optimistic, and in the 0–3 dB band the medium loses both frames where the radio often keeps the stronger one ([MEDIUM.md](MEDIUM.md) §9.1). `--medium-model=em4` gives the earlier, pessimistic model for comparison |
| No fading, external interferers, node positions or RX/TX turnaround times ([MEDIUM.md](MEDIUM.md) §9) | Topologies give fixed link values |
| Code runs in zero virtual time; MCU time is modelled from cycle counts in the profile | A slow MCU does not delay the protocol in the emulator |
| The power profiles are measured on the FG25 kits (an always-listening router and our low-power firmware) | Another radio or MCU needs its own profile ([POWER_MODEL.md](POWER_MODEL.md) §6.1) |
| One process per node; each wake is a round trip between two processes | Wall time grows with the number of wakes, not with the network's size alone |

## 2. Step 1: write a client

**Build the server.** Only `wssimserver` is needed. The option `-DWSSIM_SERVER_ONLY=ON` builds it alone, without mbedtls, libnl, libsystemd or any stack library. It still reads six source files of a `nanostack-generic-standalone` checkout (the logger and the simulation port's protocol headers, which the server shares with the Nanostack nodes; nothing of the stack is configured or built), so clone that repository next to the emulator or pass its path ([README](../README.md), "The server alone"):

```bash
cmake -S . -B build-srv -G Ninja -DWSSIM_SERVER_ONLY=ON -DNANOSTACK_DIR=<root>/nanostack-generic-standalone \
    -DCMAKE_C_FLAGS="-std=gnu17 -fpermissive"
ninja -C build-srv
python3 radio_if/test_ref_client.py build-srv/wssimserver     # the server's interface tests, about 2 s
```

The server is the same as in the full virtual-time build (`build-vt`, emulator [README](../README.md), "Compile mbedtls"), which is needed to run Nanostack nodes next to the clients. The commands below use `build-srv`; `build-vt` works the same way. The web app (§4) runs the programs of the setting `Emulator:BuildDir`: a directory relative to the emulator checkout, or absolute; by default `build-vt`. With only `build-srv`, start the web app with `--Emulator:BuildDir=build-srv`, which serves configurations of clients only; a configuration with Nanostack nodes needs `build-vt`. The plan checks the build: when the directory is missing, or lacks a program the configuration needs (`wsbrd-inproc` for a Nanostack border router, for example), the plan fails with the names of the missing programs and the build commands, and the run cannot start.

**What to implement first,** in this order, each section of [RADIO_INTERFACE.md](RADIO_INTERFACE.md) being the reference:

1. Connect, hello and welcome (§2, §4.2–§4.3): ask for version 2 if the stack sniffs, and check the `SNIFF` flag in the welcome. Take the seed, clock drift and start time from the welcome.
2. The wake loop (§2, §5): send `"xi"` with the next deadline, read `"xa"` and exactly the number of messages it announces, then handle them before the node's own timers. The skeleton in §11 is the minimal correct loop.
3. The node's clock and timers (§6.3) and its random numbers from the seed (§6.4).
4. Radio-state reports (§6.1): every change of the radio's state, including `OFF` between windows; the medium and the power report both depend on them.
5. Receiving (§6.5): a frame `"xx"` followed by its verdict `"xv"`; accept any PSDU of any length up to 4096 bytes, since other nodes' frames may be foreign.
6. Transmitting with CCA (§6.2, §6.6): report `TX` before sending, and leave `TX` only after the frame's airtime.
7. Sniffing and long-preamble frames if the protocol uses them (§6.7).
8. `MOP1` lines in the node's output (§10, "Node logs"): the first word of every line is the virtual time in µs, and a `MOP1` line carries one JSON report or event. A report with the node's ID, its state and its parent is enough for the web app ([STACK_NEUTRAL_MODEL.md](../../mesh-testbed-controller/docs/STACK_NEUTRAL_MODEL.md), "Minimum implementation").
9. Optionally the `"xc"` wake report (§4.10) to charge the MCU's work per cause, and MOP 1.1 settings read from a schedule file (§4 of this guide, "Setting changes at a virtual time").

The clients in `radio_if/` (Python and C) illustrate the interface. The stack's client is written from the specification and is not copied from them (§6).

**Test against the server by hand** before using any script: three nodes, a topology on the command line, the medium log and the power report.

```bash
SOCK=/tmp/mystack.sock
WSSIM_SEED=1 build-srv/wssimserver --vtime=3 --vtime-end=60 -g 1,2:-70 -g 1,3:-70 -g 2,3:-75 \
    --medium-log=medium.log --power=examples/power/efr32fg25-lfn.ini --power-out=power.json \
    $SOCK > simserver.log 2>&1 &
for i in $(seq 200); do [ -S $SOCK ] && break; sleep 0.05; done
for n in 1 2 3; do my_client --sock $SOCK --id $n > if$n.log 2>&1 & done
wait
```

A UNIX socket path is at most 107 bytes: keep the socket in `/tmp` with a short name, not in a deep working directory. The server refuses a longer path with its length and the limit, and `run.sh` does the same for `SOCK` (its default, `/tmp/wssim-vt-*.sock`, is short). Servers before `be5f339` stopped with `fatal error: "strlen(argv[optind]) >= sizeof(ctxt->addr.sun_path)"` instead.

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
TOPO=topo48.txt IF_NODES="$(seq -s ' ' 0 47)" IF_CMD=../mystack/build/my_client WSSIM_SEED=1 TUN=none \
    POWER=examples/power/efr32fg25-lfn.ini SIM_OPTS="--medium-log=medium.log --sniff-log=sniff.log" \
    sim_utils/vtime/run.sh build-srv . runs/mystack/seed1 300
```

- `IF_CMD` is the client's command, given `--sock <socket> --id <i>`; `IF_OPTS_<i>` and `IF_CMD_<i>` give one node its own arguments or program. The programs run in the output directory. In `IF_CMD`, `IF_CMD_<i>` and `IF_OPTS_<i>`, a word that contains a `/` and names an existing file or directory relative to the directory `run.sh` is called from is made absolute (`../mystack/build/my_client`, `./my_client`, `python3 radio_if/ref_client.py`, a schedule file `sched/3.txt`); a bare name (`my_client`) is looked up on `PATH`, as by the shell. A file that the client creates is relative to the output directory unless its path is absolute.
- `SIM_OPTS` passes any server option (`wssimserver --help`): the medium and sniff logs, clock drift (`--drift-random=20`), cut links and link changes in virtual time (`CUT`, `LINK`), late power-up (`POWER_UP`, `SPREAD`). `POWER_OPTS` passes the power options (`--power-node`, `--power-mode`, `--power-cell`). `SIM_OPTS` and `POWER_OPTS` are for `run.sh` called by hand; a run started from the web app takes these options from fields of the configuration (§4).
- Paths in `POWER_OPTS` and `SIM_OPTS`: the files the server reads (`--power-node=IDS:PROFILE`, `--power`, `--group-file`) are relative to the directory `run.sh` is called from, as `POWER` and `TOPO`, and `run.sh` stops at once if one does not exist; the files the server writes (`--medium-log`, `--sniff-log`, `--power-log`, `--vtime-frames`) are relative to the output directory, so each run keeps its own.
- When the server fails, `run.sh` prints the end of `simserver.log`, which holds its error; the clients' non-zero exit statuses that follow are its consequence.
- The output directory must be new or empty.

**Choice of power profile** ([RADIO_INTERFACE.md](RADIO_INTERFACE.md) §7, "Which profile and `phy_mode`"): `examples/power/efr32fg25-lfn.ini` for battery nodes that open short receive windows or sniff and sleep in between; `examples/power/efr32fg25-soc.ini` for nodes whose receiver is on most of the time. Report the `phy_mode` number that matches the client's bit rate and modulation index, or 0; with 0 the profile's common values (EU mode 1a, 50 kbit/s) apply. Every value is a user setting: copy a profile and edit it for another radio, or give one per node with `POWER_OPTS="--power-node=IDS:PROFILE"`, for example a gateway that listens all the time next to battery nodes: `POWER=examples/power/efr32fg25-lfn.ini POWER_OPTS="--power-node=0:examples/power/efr32fg25-soc.ini"`; in a web-app configuration, the same mains-powered gateway is `"run": {"powerProfile": "examples/power/efr32fg25-lfn.ini", "powerNodes": [{"nodes": "0", "profile": "examples/power/efr32fg25-soc.ini"}]}` (§4). `simserver.log` names each profile's hardware kind (`soc` means MCU and radio on one chip, for both FG25 profiles) and, at the end, each node's profile, the PHY mode it reported and whether that mode's own values or the common values were used.

**A node that receives many frames should listen, not sniff.** A sniff detects and locks onto at most one frame, and frames that arrive while the radio is off between sniffs are lost (`not_listening`). In the dry run, a gateway that sniffed received one frame per sniff of the many sent to it. Keep the receiver of a gateway or of a busy parent in `LISTEN` and give it the SoC profile; sniff on battery nodes that receive little.

**Seeds and repeatability.** One seed per run (`WSSIM_SEED`). To check that the client is deterministic, run the same seed twice into two directories and compare:

```bash
cd runs/mystack/seed1
for f in if*.log medium.log sniff.log power.json; do cmp -s $f ../seed1b/$f || echo "differs: $f"; done
```

Size campaigns to what they must show: a few seeds of a reasonable length, not many repeats.

**Logs to read** in the run directory:

| File | Content |
| --- | --- |
| `simserver.log` | Settings of the medium and of sniffing, the power profiles (hardware kind, modes with values of their own), then at the end: virtual time reached, wakes, frames sent and delivered, outcomes at receivers, CCA queries, sniff outcomes, one power line per node; warnings about the interface |
| `medium.log` | One line per frame and receiver with its outcome (`ok`, `noise`, `interference`, `captured`, `below_lock`, `busy`, `not_listening`, `tx_abort`, `left`): [MEDIUM.md](MEDIUM.md) §7 |
| `sniff.log` | One line per sniff: request, window, outcome (`detected`, `none`, …), frame, power, SINR, lock: [MEDIUM.md](MEDIUM.md) §10.5 |
| `power.json` | Per node: average current, charge, time and charge per MCU and radio state, wakes, the sum check: [POWER_MODEL.md](POWER_MODEL.md) §7. `python3 sim_utils/power_summary.py <run directory>` prints a table |
| `if<i>.log` | The client's own output, with its `MOP1` lines |
| `run_info.txt`, `exit_codes.txt`, `topology.txt` | Seed, nodes, options, the wall-clock start and end; each program's exit status; the topology as used |

**Large runs.** `run.sh` gives the topology to the server with `--group-file`, so it takes any network up to 4096 node IDs: a 1000-node building (19 156 links) is prepared in under 1 s, and its 60 virtual seconds took 3.6 s in all (2.5 min before 2026-11-09, when `run.sh` passed every link as `-g`; same results); a 4000-node building ran through it too. Starting the server and the clients by hand ([SCALE.md](SCALE.md) §2) is no longer needed for size. Use the building generator for the topology:

```bash
python3 radio_if/scale/gen_building.py --nodes 1000 -o topo1000.txt
TOPO=topo1000.txt IF_NODES="$(seq -s ' ' 0 999)" IF_CMD=./my_client WSSIM_SEED=1 TUN=none \
    POWER=examples/power/efr32fg25-lfn.ini sim_utils/vtime/run.sh build-srv . runs/mystack/n1000 60
```

Plan the memory as the client's resident size × N ([SCALE.md](SCALE.md) §3–§4).

## 4. Step 3: observe the network in the web app

The web app reads each client's `MOP1` lines and shows the nodes, their states, parents and links, counters and events ([STACK_NEUTRAL_MODEL.md](../../mesh-testbed-controller/docs/STACK_NEUTRAL_MODEL.md), "The emulator" and "Nodes of other stacks: clients of the radio interface"). Installation of .NET and the settings are in the controller's [README](../../mesh-testbed-controller/README.md).

**Start an instance for the emulator only,** with the test profile, its own port and its own data directory. The test profile switches the real bench off whatever the other options say, keeps the configurations, event log, exports and runs under the data directory, uses no TUN device, and builds the program in `<data dir>/build` ([README](../../mesh-testbed-controller/README.md), "Extra instances"). The emulator runs use the programs of `Emulator:BuildDir`, by default the full virtual-time build `build-vt` of the emulator checkout (§2); add `--Emulator:BuildDir=build-srv` (relative to the emulator checkout, or an absolute path) to use the server-only build for configurations of clients only. The Runs page names the build directory in use. Switching the simulated bench off makes the emulator the instance's only environment; without `--Environments:MtsSimulated=false`, the simulated bench is shown first, and the emulator is chosen in the environment selector at the top of the Configuration page:

```bash
cd <root>/mesh-testbed-controller
./run.sh --profile test --dataDir <dir> --urls http://127.0.0.1:5091 \
    --Environments:MtsSimulated=false --Emulator:EmulatorDir=<emulator checkout> --Emulator:BuildDir=build-srv
```

Every page then shows "Test instance: no real bench". Use a port of your own: another instance may already use 5080 or 5081. A test instance shows no run at start-up: the Runs page reads "no run selected" until a run is started, chosen or opened (setting `Emulator:StartupRun`, `none` for the test profile). A main instance, or one started with `--Emulator:StartupRun=latest`, shows the newest run under its run roots at start-up and says so in the Runs page's status ("the newest run at start-up") and in the event timeline.

**Write the configuration.** A configuration names the stack, its program and arguments, the topology and the run ([STACK_NEUTRAL_MODEL.md](../../mesh-testbed-controller/docs/STACK_NEUTRAL_MODEL.md), "Configuration of nodes of other stacks, and the neutral TX power"). It can be edited as JSON on the Configuration page, or written as a file in the configuration directory (`<dir>/configs` of a test instance). The topology can stay in its file (`"kind": "file"`, the emulator's topology format, read at each plan and run), and `defaultStack` puts every node of the topology on the stack without listing the nodes:

```json
{
 "schema": 1, "name": "mystack48",
 "radio": {"txPowerDbm": 14},
 "topology": {"kind": "file", "file": "topo48.txt"},
 "defaultStack": "my-stack",
 "stacks": {"my-stack": {"program": "/abs/path/to/my_client", "arguments": "", "settings": {}, "txPowerSetting": "tx-dbm"}},
 "run": {"durationS": 300, "seeds": [1], "virtualTime": true, "powerProfile": "examples/power/efr32fg25-lfn.ini"}
}
```

- `txPowerSetting` names the client's argument for the TX power: the neutral TX power `radio.txPowerDbm` is passed as `--tx-dbm 14`. Settings in `stacks.<name>.settings` and `nodes[i].settings` become `--name value` arguments; a value must not contain spaces or quotes.
- `run.mediumTxRefDbm` makes the link values the received power at that TX power, so that a node's TX power changes what others receive.
- `"program"` may use `$EMU`, the emulator checkout (for example `python3 $EMU/radio_if/ref_client.py`); otherwise give absolute paths.
- `topology.file`: an absolute path, or a path relative to the configuration directory (`<dir>/configs` of a test instance; the Configuration page shows it next to the field). The example expects `topo48.txt` next to the configuration file.
- The Configuration page can also import a topology file into the configuration's links (`"kind": "edges"`), and chooses the default stack and the power profile. A node keeps its own stack with an entry in `nodes` (`{"id": 0, "stack": "wisun"}` for a Nanostack border router).
- `run.powerProfile` (an `.ini` of the emulator's `examples/power/`, or an absolute path) and `run.powerCell` (`mAh[:V[:efficiency]]`) are exported as `POWER` and `POWER_CELL` of `run.sh`; both are also on the Configuration page and the Runs page.
- The server's options are fields of `run`, not `SIM_OPTS` or `POWER_OPTS` (those stay for `run.sh` called by hand, §3). Each field is also on the Configuration page ([STACK_NEUTRAL_MODEL.md](../../mesh-testbed-controller/docs/STACK_NEUTRAL_MODEL.md) §7):

  ```json
  "run": {"durationS": 300, "seeds": [1], "virtualTime": true, "powerProfile": "examples/power/efr32fg25-lfn.ini",
          "mediumLog": true, "sniffLog": true, "driftRandomPpm": 20, "drift": [{"nodes": "3", "ppm": -15}],
          "powerNodes": [{"nodes": "0", "profile": "examples/power/efr32fg25-soc.ini"}],
          "serverOptions": "--medium-lock-snr=2"}
  ```

  `mediumLog` and `sniffLog` write `medium.log` and `sniff.log` in the run directory; `driftRandomPpm` draws each node's clock error within ±20 ppm from the seed, and `drift` fixes some (`nodes` in the server's list form: `0`, `2-5`, `1,4,7-9`; without `nodes`, every node); `powerNodes` gives some nodes another profile than `powerProfile` (here a mains-powered gateway on the SoC profile); `serverOptions` passes further `wssimserver` options as they are, one `--name=value` word each, without quotes. The options that `run.sh` sets itself (`--vtime`, `--group-file`, `--power`, …) are refused, and the others are not checked: the plan warns, and a wrong option stops the server at the start of the run (the error is in `simserver.log`).

**Run it.** On the Configuration page: choose the configuration, **Load**, **Plan**, **Apply selected steps** (this writes `config.json`, `topology.txt`, `run.sh` and any schedule files into `<ExportRoot>/<name>/`; a run without Nanostack gets no `wsbrd`/`wsnode` configuration files and no certificate links). Before applying, read the plan: it names the programs taken from the build directory, the TX power each node gets ("TX power 14 dBm: nodes 0-47 get --tx-dbm 14"), whether the link values are taken at a TX power (`--medium-tx-ref=14` with `run.mediumTxRefDbm`) and the complete server options of the run. On **Runs and recording**: **Start**; the button names what it starts ("Start 'mystack48' (seed 1, 300 s, power)"). The server has one current configuration, shared by every browser tab and shown at the top of every page, so the run may be started from any tab. **Nodes**, **Mesh** and **Events** then show the run.

The Runs page lists the run's files under "Files of the shown run": `simserver.log`, `medium.log` and `sniff.log` (with a count per outcome and their first lines), `power.json`, `run_info.txt` and the others, each with a link to view or download it. On the Events page, events of an emulator run carry its virtual time in the time column ("run 97.803 s") and the run they come from in the "Run" column; within a run they are in virtual-time order. The "Run" filter shows one run's events, and **Clear the shown events** removes the events the filters show (for example an earlier run) from the page, not from the event log file. Opening a run again replaces its events instead of repeating them.

**Power report.** With a power profile in the configuration or chosen on the Runs page, the run writes `power.json`, shown on the Runs page as a per-node table (average current, charge, RX and TX time, deep sleep, wakes; CSV export) and in each node's details.

**Runs made outside the web app.** A run made with `run.sh` or by hand opens on the Runs page by its path (**Open a run directory**), if it lies under the export directory, the emulator's `runs/` or a directory of `Emulator:RunRoots` (`--Emulator:RunRoots:0=/home/me/runs`). It needs the clients' `if<i>.log` (or `br.log`); its `topology.txt`, `t0` and `power.json` are used when present. For example, from the emulator checkout:

```bash
TOPO=topo48.txt IF_NODES="$(seq -s ' ' 0 47)" IF_CMD=./my_client TUN=none \
    POWER=examples/power/efr32fg25-lfn.ini sim_utils/vtime/run.sh build-srv . runs/hand1 60
# Runs page, "Open a run directory": <emulator checkout>/runs/hand1
```

**Setting changes at a virtual time.** In the emulator a setting changes at a virtual time given in the configuration, not from the node details, so that a run stays repeatable ([STACK_NEUTRAL_MODEL.md](../../mesh-testbed-controller/docs/STACK_NEUTRAL_MODEL.md), "Setting changes during an emulator run"). The client must list its settings in its reports (`ops`, `settings`) and read a schedule of MOP `set` requests from the file named by `setFileArgument`:

```json
"stacks": { "my-stack": { "program": "...", "txPowerSetting": "tx-dbm", "setFileArgument": "--mop-set-file" } },
"settingChanges": [ { "atS": 60, "node": 1, "settings": { "tx_power_dbm": "0" } } ]
```

The export writes `mop-set-1.txt` next to `run.sh` and passes it to node 1 as `--mop-set-file <absolute path>`. Its format is exact, so the client may read it strictly ([STACK_NEUTRAL_MODEL.md](../../mesh-testbed-controller/docs/STACK_NEUTRAL_MODEL.md) §7.1, "The schedule file"):

- ASCII text with LF line ends, the last line included;
- one line per change and nothing else: no header or comment line, no blank line;
- each line is `<seconds> <JSON>`: the virtual time in seconds since the run's start, as digits with optionally `.` and 1 to 6 decimals (`0`, `60`, `30.25`, `0.000001`; no sign or exponent), one space, then the MOP 1.1 `set` request on one line without the `MOP1 ` prefix;
- lines sorted by time; changes at the same time keep the configuration's order.

```
60 {"mop":1,"op":"set","seq":1,"req":"cfg-1-1","settings":[{"name":"tx_power_dbm","type":"int","value":0}]}
```

The client applies the request at its first wake at or after 60 s and prints the `ack` and a `settings` event as `MOP1` lines ([STACK_NEUTRAL_MODEL.md](../../mesh-testbed-controller/docs/STACK_NEUTRAL_MODEL.md), "Settings (revision 1.1)"). `tx_power_dbm` is the well-known name for the TX power; a stack's own settings have a dot in their name (`my.window_ms`).

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
- **What to report** for a result: the setup above; the figures with their range over seeds and the number of runs; delivery from the node logs and the medium log; power from `power.json` with the profile named; the limits of §1 that apply (sniffing model values, the collision model's limits, the profile's hardware).
- **Settings** change at a virtual time in the configuration, never during a run from outside.
- **The bench** is used through the controller, with the owner's agreement, and left as found (Wi-SUN images on every node unless agreed otherwise).

## 7. Troubleshooting

| Symptom | Cause and remedy |
| --- | --- |
| `cmake` fails on `MbedTLS`, `libnl-route-3.0` or the stack's directory | Configure with `-DWSSIM_SERVER_ONLY=ON`, which needs neither mbedtls nor libnl, and point `-DNANOSTACK_DIR` at a checkout of `nanostack-generic-standalone` (§2). The full build (`build-vt`) needs mbedtls and is only for Nanostack nodes |
| `WSSIM_SERVER_ONLY: no stack checkout in NANOSTACK_DIR` | `-DNANOSTACK_DIR` does not point at a `nanostack-generic-standalone` checkout (§2) |
| A client exits with "can't open file" or "not found" under `run.sh` | The programs run in the output directory. `run.sh` makes a relative path absolute only if it contains a `/` and exists from the directory it is called from: write `./my_client`, not `my_client` (a bare name is looked up on `PATH`), and call `run.sh` from the directory the paths are relative to. A file the client opens through an option whose value is joined to it (`--file=x/y`) is not resolved: give it as a separate word or absolute (§3) |
| `run.sh` takes minutes before the run starts, or fails with "Argument list too long" | An emulator checkout from before 2026-11-09: `run.sh` then passed each link as `-g`. Update it; `run.sh` now uses `--group-file` (§3) |
| `--group-file: ...topology.txt:N: ...` in `simserver.log` | Line N of the topology (as cleaned in the output directory's `topology.txt`) is malformed: one `A,B:RSSI` per line, IDs 0 to 4095 ([sim_utils/vtime/README.md](../sim_utils/vtime/README.md), "Topology files") |
| `run.sh: ... is not empty` | Each run needs a new output directory |
| `run.sh: --power-node: no file ...` | The profile path in `POWER_OPTS` is relative to the directory `run.sh` is called from, as `POWER` (§3). Emulator checkouts before `4e8706b` passed it unchanged to the server, which runs in the output directory: every client then exited with status 1 and `run.sh` printed only "non-zero exit status"; the cause was at the end of `simserver.log` (`power profile ...: No such file or directory`) |
| `socket path too long` or `run.sh: SOCK is ... bytes long` | A UNIX socket path is at most 107 bytes: use `SOCK=/tmp/<name>.sock` or leave `SOCK` unset (§2) |
| `run.sh: wssimserver failed ...` | The server's error follows, from the end of `simserver.log`: a bad option, an unreadable file, an interface error. The clients' exit statuses are a consequence |
| The server closes the connection right after the hello | Wrong size or version, an ID used twice or above 4095, more clients than `--vtime=N`, or a server without `--vtime`: the warning is in `simserver.log` ([RADIO_INTERFACE.md](RADIO_INTERFACE.md) §9) |
| The run stops when one client exits | A client that disconnects during the run stops it; read that client's log |
| Frames lost where they should arrive: `not_listening` | The receiver's reported state was `OFF` or the channel differed when the frame started |
| A sniffing gateway or parent receives only some of the frames sent to it | A sniff locks onto at most one frame, and the radio is off between sniffs: let a node that receives many frames listen all the time, with the SoC profile (§3) |
| `simserver.log` names the profile kind `soc` for an LFN profile, or "values ... for PHY mode 3" while the clients report mode 1a | `soc` is the hardware's architecture (MCU and radio on one chip), not the use of the profile. The mode-3 line lists the modes that have values of their own; a node in mode 1a takes the common values. Each node's line at the end names the values it used (since `be5f339`) |
| `/tmp/radio_if_*` directories pile up | Left by `test_ref_client.py` before `b6a9397`; it now removes its directory when every test passes, keeps it on a failure and prints its path. The old ones can be deleted |
| Every overlapping frame lost | A link without a value is 0 dBm and destroys any frame it overlaps: give link values |
| A link behaves differently in each direction | A line `B,A:RSSI` sets the reverse direction; the server warns when a later line changes a value ([MEDIUM.md](MEDIUM.md) §2) |
| Two runs with the same seed differ | The client reads the wall clock or another random source, or iterates over something unordered; compare the node logs to find the first difference |
| The plan says `Emulator:BuildDir: ... has no wsbrd-inproc` (or another program) | The build directory lacks a program the configuration needs: a server-only build (`build-srv`) serves clients only. Build `build-vt` for Nanostack nodes and start the instance with its `--Emulator:BuildDir`, or remove the Nanostack nodes from the configuration (§2, §4) |
| A web-app run stops at once and `simserver.log` reads `unrecognized option` | An option of `run.serverOptions` is wrong: the controller passes them unchecked (§4) |
| The power report is missing from a web-app run | No power profile was chosen: set `run.powerProfile` on the Configuration page or choose one on the Runs page before **Start** (§4) |
| A run made by hand cannot be opened | Open it by its path on the Runs page; it must lie under the export directory, the emulator's `runs/` or a directory of `Emulator:RunRoots`, and contain `if<i>.log` or `br.log` (§4) |
| The run started from the web app is not the configuration expected | The Runs page starts the current configuration (shown at the top of every page, shared by all tabs) or the saved one chosen there; the **Start** button names it (§4) |
| The plan says `topology.file: no file ...` | The path in the message is the one read: a relative path is relative to the configuration directory (`<dir>/configs` of a test instance), not to the emulator checkout or the controller; move the file there or give an absolute path (§4) |
| The plan lists no step to write files and warns "bench node(s) 0-12 run another stack ... but have no image" | The plan targets the simulated bench (the button reads "Apply to MTS bench (simulated)"): choose the Emulator environment at the top of the page, or start the instance with `--Environments:MtsSimulated=false` (§4). Controllers before `4cdfe18` showed "Object reference not set to an instance of an object" there instead |
| A node is shown "unknown" | Its log has no `MOP1` report line, or the line's first word is not the virtual time in µs |
| A second controller instance contacts the real bench | Start extra instances with `--profile test` (§4), which keeps the real bench off; an instance with the real bench warns on every page when another one uses it |
| The power figures look like mode 1a at another bit rate | The client reports `phy_mode` 0: report the number matching its PHY ([RADIO_INTERFACE.md](RADIO_INTERFACE.md) §7) |
| An image on the bench is rolled back after three boots | It never confirmed its trial: install it as permanent, or confirm once its management link works (§5) |
| A node on the bench no longer answers on the network | Without MOP `reset` or the management protocol, reset it at the board while the host sends `HOLD` (`tools/mtsbl.py hold all --seconds 60`), or reset it three times so that the bootloader rolls back ([MTS_Bootloader_Design.md](../../documents/mts/MTS_Bootloader_Design.md) §13) |
