# Wi-SUN MBED SIM (TCP, MQTT and FHSS ready)

* You need mbedtls for this repo.
* See license before fork/copy.

> **mesh-emulator-orchestrator** is `wisun-mbed-simulator` (commit `d86eb34`)
> with Nanostack moved out to
> [`nanostack-generic-standalone`](../nanostack-generic-standalone). This repo
> keeps the emulator: the simulated medium (`wssimserver`), the simulated RCP
> (`wshwsim`), the node applications (`wsbrd`, `wsnode`), the Spinel/UART
> transport and the run scripts. The stack is built from the sibling
> repository and linked as static libraries. Behaviour is unchanged; see
> [MIGRATION.md](MIGRATION.md). The stack has since been made portable, and
> this repo uses its POSIX port's run loop and offers in-process nodes
> (`wsnode-inproc`, `wsbrd-inproc`); see the stack's `docs/ARCHITECTURE.md`.
>
> Status (2026-10-27): the first emulator fixes (workspace item EM1) are in:
> node IDs that no longer depend on the start order, one socket message per
> frame, correct RSSI indexing up to 4096 nodes, a corrected loss formula with
> a seeded generator per node, and a clean shutdown. See "Node IDs, frames and
> losses" below. Sleep/energy states, virtual time and the central medium are
> still to come. They are tracked in `documents/pre-study/STATUS.md` of
> [`mesh-emulator-rework`](https://github.com/YannCharbon/mesh-emulator-rework).

## Compile mbedtls

0) Install dependencies:
```bash
sudo apt-get install libnl-3-dev libnl-route-3-dev libcap-dev \
    libsystemd-dev libdbus-1-dev cargo cmake ninja-build pkg-config lrzsz
   ```
1) Clone mbetls (ver.3) in the pwd `git clone --branch=v3.0.0 https://github.com/ARMmbed/mbedtls`
2) Do cd into mbedtls dir and `cmake -G Ninja -DCMAKE_C_FLAGS=-DMBEDTLS_NO_PLATFORM_ENTROPY -DENABLE_PROGRAMS=OFF -DENABLE_TESTING=OFF .`
3) Then do: `ninja`

mbedtls is built without its own entropy source (`MBEDTLS_NO_PLATFORM_ENTROPY`,
as in `examples/mbedtls-config.h`): all TLS randomness comes from the stack's
porting API (`ns_port_random_bytes()`, `/dev/urandom` on the POSIX port), which
this repo's build registers as a strong source
(`NANOSTACK_TLS_STRONG_PORT_ENTROPY`, ON by default here). This is needed for
virtual time (`docs/VIRTUAL_TIME.md`). An mbedtls built with the default
configuration, such as the one in `wisun-mbed-simulator`, still works: TLS then
also reads `/dev/urandom` itself, and `wsbrd` warns about it at start-up.

## Compile mbed OS

1) Compilation of the simulation tools is now enabled to build the relevant binaries. Clone `nanostack-generic-standalone` next to this repo (or pass `-DNANOSTACK_DIR=<path>`), return from mbedtls dir to this repo's dir and:

```bash
cmake -DCMAKE_PREFIX_PATH="$(pwd)/mbedtls/" -G Ninja .
```

With GCC 14 or newer, add `-DCMAKE_C_FLAGS="-std=gnu17 -fpermissive"`: GCC 15 defaults to C23 and turns legacy warnings in this code into errors.

and then: `ninja`

* FHSS mode is ON and channel hopping is working. CSMA-CA MAC mode disabled.

## Run

### Script for running the simulations

Run `bash tunup.sh` to setup a TUN interface (This needs sudo). Then run `bash run.sh <topology>`. This will open the simulation server and MAC layer processes in new tabs, and the mbed nanostack in separate windows.

### Example script of 3 nodes (old method)

In the main dir of this repo, you see a file called `run.sh`. This file contains enough lines of bash code to run 3 nodes: One BR and 2 Router nodes.\
To do this, overall we need 7 process:

1) Simulation server: `wssimserver` (1 instances)
2) Wi-SUN MAC simulator and PHY model: `wshwsim` (3 instances)
3) Wi-SUN Router node: `wsnode` (2 instances)
4) Wi-SUN BR node: `wsbrd` (1 instances)

* Note that BR process may need root access to create a tun interface, so `sudo` is used for the case of BR process and it will prompt for your password.
* There must be different config files for Router nodes and BR. An example of this file `examples` dir. All Router nodes can use the same config file.

### Run in docker

It is possible to run multiple simulations in parallel. Meaning that a mesh network per docker instance. This is realized by using Docker which allows the networks to be run isolated from eachother. At the end of the simulation, the results and logs will be saved in a directory in the host, then docker container will exit and removed.

First, build a docker image. For this see the `docker` directory.

To run, modify the available parameters in `run_docker.bash`, and run it.


### Topology setup

The topologies of the network are defined within the `run.sh` script. When running the script, the name of the topology must be provided. The user can easily add new topologies.

Here is a short explanation of the script content. The line

```bash
`gnome-terminal --tab -- $DIR/wssimserver -g 0-1 -g 1-2 /tmp/sim_socket --dump`
```

runs `wssimserver` which is a server to make connection between nodes RF interfaces. The `-g` option can be used to group the nodes and make a custom topology.\
For example `-g 0-1 -g 1-2` will make a line topology:

```text
0 --- 1 --- 2
```

But the result of `-g 0-2 -g 1-3` will be:

```text
     0
   /   \
  1 --- 2
   \   /
     3
```

The `-g` option also supports providing an RSSI value for a specified edge. The format `-g 2,5:-67.0` will create an edge from node 2 to 5 with an RSSI of -67.0 [dBm], and `-g 0-3:-70` gives every pair of nodes 0 to 3 that RSSI. By default, if no value is provided, the RSSI value is `0 [dBm]` to guarantee a very good link quality.

The RSSI is used within the simulator to calculate a frame transmission error rate at PHY layer. The bit error rate is based on the non-coherent 2-FSK BER formula, and a frame is lost if any of its bits is:

$BER(P_r) = \frac{1}{2} e^{\frac{1}{2} (10 \cdot \log_{10}(kTB) + 30 + NF_{LNA} + 10\cdot \log_{10}(\frac{B}{R_b} ) - P_{r})}$

$FER = 1 - (1 - BER)^{8 \cdot (L_{frame} + 6)}$

where $P_{r} \approx RSSI$ [dBm], $k$ = Bolzman constant ($k = 1.3806 \cdot 10^{−23}$ [J/K]), $T$ = temperature [K], $B$ = channel bandwith [Hz], $NF_{LNA}$ = noise figure of the LNA, $R_b$ = transmission bitrate [bit/s], $L_{frame}$ = frame byte count. The 6 bytes are the PHY header (2) and the FCS (4), which the receiver decodes too.

In this simulator, we use $T$ = 293 [K], a channel $B$ = 100 [kHz], a $R_b$ 100 [kbit/s] and $NF_{LNA}$ = 4 [dB] (the formula can be edited in `sl_rf_driver.c`). Before 2026-10-27 the frame error rate was $BER \cdot 8 \cdot L_{frame}$, which is the same at small BER but can exceed 1.

The following table presents the probability of packet loss of a 100 bytes frame depending on the RSSI value (it was computed with the earlier formula; the values differ by under 10 %):

| RSSI [dBm] | Frame error rate [-] |
|------|------------------|
| 0.0  | 3.63e-24 |
| -10.0  | 5.38e-22 |
| -20.0  | 7.99e-20 |
| -30.0  | 1.19e-17 |
| -40.0  | 1.76e-15 |
| -50.0  | 2.61e-13 |
| -60.0  | 3.88e-11 |
| -70.0  | 5.75e-09 |
| -80.0  | 8.54e-07 |
| -90.0  | 1.27e-04 |
| -100.0  | 1.88e-02 |

> **_NOTE:_** be aware that, by default, Wi-SUN does not accept a neighbour as a parent if the RSSI is below -80 dBm (edit `DEVICE_MIN_SENS` variable to change the limit).

### Node IDs, frames and losses

- **Node ID.** A node's ID is the last two bytes of its MAC, big-endian: `01:02:03:04:05:06:01:2C` is node 300. IDs go up to 4095. Each radio (`wshwsim`, or `wsnode-inproc`/`wsbrd-inproc`) sends its ID to `wssimserver` when it connects, and the server places it in the topology by that ID. So the processes can start in any order and in parallel; the server refuses a second node with the same ID. The run scripts build the MAC from the node index (`run.sh`, `run_orchestrator.py`, `sim_utils/migration/headless_run.sh`, and the GUI's `confgen.py`).
- **Frames.** A frame goes to the server as one socket message (a header, then the frame), and the server stamps the sender's ID on it. The receiver takes the link's RSSI from that ID, so frames without a source address (acknowledgements) get the right RSSI too. The messages are defined in `app_wssimserver/sim_proto.h`.
- **Losses.** Each node draws its losses from its own generator, seeded with a base seed plus its node ID. Set `WSSIM_SEED=<n>` in the environment of the node processes to choose the base seed; otherwise it comes from the clock. Each node logs `loss model: WSSIM_SEED=<n>, node <id>` at start. This replays the loss draws only: the timing still comes from the host clock, so two runs with the same seed still differ.
- **Shutdown.** `wssimserver` stops on SIGINT or SIGTERM, and removes its socket and its shared memory (`/dev/shm/wssimcca`, `/dev/shm/wssimnodesinfos`). It also recreates both at start, so a run never sees the tables of the previous one. `run_orchestrator.py` starts each process in its own session and stops the whole session at the end.
- **Radio off.** The emulated radio has a real off state (GS8 stage 1): `PHY_INTERFACE_DOWN` from the MAC turns it off, and frames that arrive while it is off are lost (`docs/POWER_MODEL.md` §3.1). A Wi-SUN router keeps its receiver on; `rx_on_when_idle = false` (or `rx_off_when_idle_after = <s>`) in `wsnode.conf` makes its MAC turn the radio off whenever it is idle, a test of the radio-off path, not an LFN. `WSSIM_RADIO_OFF=0` in the nodes' environment restores the old behaviour, where `DOWN` left RX on, to compare with older runs.
- **PHY airtime.** A frame lasts its bytes on air at the data rate of the PHY mode the stack configures (`mode` in `wsbrd.conf`/`wsnode.conf`: 1a/1b 50 kbit/s, 2 100 kbit/s, 3 150 kbit/s...): the SHR (8-byte preamble, 2-byte SFD), the 2-byte PHR, the frame and its FCS (4 bytes). `WSSIM_PHY_DATARATE`, `WSSIM_PHY_PREAMBLE`, `WSSIM_PHY_SFD`, `WSSIM_PHY_PHR` and `WSSIM_PHY_FCS` (2 or 4) in the nodes' environment change them; `WSSIM_PHY_LEGACY=1` restores the PHY before them (150 kbit/s whatever the mode, the frame alone), to compare with older runs (`docs/POWER_MODEL.md` §3.1, §12.5). `examples/wsbrd-eu1a.conf` and `wsnode-eu1a.conf` are the FG25 firmware's PHY: EU, mode 1a, class 1 (69 channels), +10 dBm. **Which to use:** the default `examples/wsbrd.conf`/`wsnode.conf` stay EU mode 3 (150 kbit/s), the mode most of our hardware applications use, and are the base for regression checks; `CONF=eu1a` is for comparing with our FG25 kits (HW5), whose firmware runs mode 1a.
- **FAN version.** `fan_version = 1.1` in `wsbrd.conf`/`wsnode.conf` turns FAN 1.1 on at run time (default 1.0). The LFN features of FAN 1.1 are not implemented yet in the upper layers (GS8).
- **LFN MAC mechanics (GS8 stage 2).** The stack's MAC and FHSS have an LFN's timed receive windows and its parent's hold-until-window (the stack's `docs/LFN_MAC.md`). `wslfntest-inproc` (`app_lfntest/`) runs them on two nodes without the upper layers, and `sim_utils/vtime/test_lfn_mac.sh` checks them in virtual time with the power ledger (`docs/POWER_MODEL.md` §13).
- **Several runs on one PC.** The shared-memory names belong to the socket path: the default `/tmp/sim_socket` keeps the names above, and any other path gets its own (`/dev/shm/wssimcca._tmp_my_sock` for `/tmp/my_sock`). Give `wssimserver` and the nodes (`wshwsim`, `-u` of the in-process programs) the same path.

### Virtual time

With `--vtime=N`, `wssimserver` is also the conductor of a virtual-time run (EM3, design in `docs/VIRTUAL_TIME.md`): it waits for N nodes built on the simulation port, then wakes them one at a time in virtual-time order, and the run takes no account of the host clock.

Build: the nodes run on the stack's simulation port, in a separate build directory. They need the mbedtls built without platform entropy (section "Compile mbedtls"; here in `mbedtls/install`), or TLS reads `/dev/urandom` and runs are not reproducible.

```bash
cmake -S . -B build-vt -G Ninja -DNANOSTACK_PORT=nanostack_port_sim \
  -DCMAKE_PREFIX_PATH=$(pwd)/mbedtls/install -DCMAKE_C_FLAGS="-std=gnu17 -fpermissive"
ninja -C build-vt          # wssimserver, wsnode-inproc, wsbrd-inproc (in-process nodes only, for now)
```

Run: `sim_utils/vtime/run.sh` does one run (dag_simple by default, about 3 s for 300 virtual seconds), `sim_utils/vtime/campaign.sh` several at once, and `sim_utils/migration/analyze.py` reads the results (`sim_utils/vtime/README.md`). By hand, start the server first, then the nodes with the same socket:

```bash
WSSIM_SEED=7 wssimserver --vtime=15 --vtime-end=300 -g 0-2 ... /tmp/vt_socket
```

- `--vtime=N`: the number of nodes. Nodes start in any order; the run starts when all N said hello. A node without the virtual-time flag is refused (and a node with it is refused without `--vtime`).
- `--vtime-end=SECONDS`: the run ends at this virtual time. Without it, it ends when no event is left, or on SIGINT/SIGTERM.
- `WSSIM_SEED` (default 1) is read by the server and sent to the nodes, for the loss draws and the stack's random source.
- At the end, the server logs the virtual time reached, the number of wakes and frames, the wall time and the wall seconds per simulated second. It exits 1 if a node disconnects during the run or if more than 100 000 wakes happen at one virtual time.
- `sim_utils/vtime/test_conductor.py <wssimserver>` tests the conductor with fake nodes (`sim_utils/vtime/fake_node.py`), on its own socket and with a renamed copy of the binary.

### Run with python orchestrator
A Python script (`run_orchestrator.py`) has been created to streamline the use of this simulator by minimizing user interaction, making it easier to automate multiple test runs (e.g., batch testing of network topologies). This implementation also reduces the number of terminal windows opened during simulations, which lower CPU usage overhead.

**Note:** Node-related logs are currently not displayed due to limitations in this script.

The script must be run once interactively to generate the required configuration files, which need root privileges. You will be prompted for your password at two specific points during this process.

Here is how to use the script:
```bash
# Display help
python run_orchestrator.py --help

# Run with a default topology
python run_orchestrator.py

# Run with a custom topology
python run_orchestrator.py -g 0,1:0.71 -g 0,2:0.71 -g 1,3:0.65 ...
```
### Quick simulator

As Wi-SUN can be low to establish the whole mesh network, this simulator is also able to run using a 10 times faster timebase. Please checkout the `10x_faster` branch of this repository to access this feature.

### Wireshark

You can add `-w` option to every node's `wshwsim` process and it will bring up a Wireshark window for you. But Note that you need to run the corresponding node processes manually in another terminal window. Otherwise it will not connect to its MAC/PHY.

### GTK and GAK keys

Wi-SUN uses GAK instead of GTK, so even if you set a known GTK in the config file, you can't use it directly in the Wireshark to decrypt the packets. Thus, a python code is added to example dir in this repo to generate GAK from GTK.

### Features, current status and known bugs

RF driver implemented using fd and sockets.

FHSS timing is working well, channel hopping is OFF at the start, but gets ON after configurable `x` minutes. Because sometimes ASYNC packets are not sent over all available channels which makes it difficult to join. This `x` should be proportional to the location of the node in the network: distant nodes need to spend more time on FHSS OFF mode.


### References

1. Simulator by [SiLabs](https://github.com/SiliconLabs/wisun-br-linux) (version 1.2)
2. TCP re-add by [Eric](https://github.com/ercclpn)
3. FHSS MAC and all related functions and modifications added and applied by Mahboob Karimian
4. MQTT adaptation to work with timers by Mahboob Karimian ([source](https://github.com/eclipse/paho.mqtt.embedded-c))
5. RF driver added by Mahboob Karimian inspired by EFR32 driver implementation (private) by [Yann Charbon](https://github.com/YannCharbon), and open source S2LP RF driver by STM
