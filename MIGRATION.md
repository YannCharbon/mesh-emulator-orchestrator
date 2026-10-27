# Migration from wisun-mbed-simulator

`mesh-emulator-orchestrator` and
[`nanostack-generic-standalone`](../nanostack-generic-standalone) together
replace `wisun-mbed-simulator` at commit `d86eb34` (branch `main`). The goal of
this step is a 1-to-1 split: the same code, the same build flags, the same
behaviour. No bug was fixed and nothing was redesigned. The old repository
stays untouched as the reference.

## What moved where

| `wisun-mbed-simulator` | Now |
| --- | --- |
| `nanostack/`, `nanostack-event-loop/`, `mbed-client-libservice/` | stack: same paths |
| `app_wshwsim/mac/`, `app_wshwsim/fhss/`, `app_wshwsim/ccm/` | stack: `rcp/mac/`, `rcp/fhss/`, `rcp/ccm/` |
| `common/` OS layer: `hal_interrupt`, `os_timer`, `os_scheduler`, `os_types.h`, `log`, `rand`, `slist`, `utils.h`, `backtrace_show` | stack: `port/linux/common/`, renamed `port/posix/common/` in step S2 (see below) |
| `common/` transport: `bus_uart`, `spinel_buffer`, `spinel_defs.h` | orchestrator: `common/` |
| `app_wsbrd/`, `app_wshwsim/` (rest), `app_wssimserver/`, `application_protocols/`, scripts, examples, docker, `sim_utils/` | orchestrator: same paths |

Both repositories were created with `git filter-repo` from the reference, so
`git log --follow` and `git blame` still reach the original commits.

## Changes needed by the split

- **`CMakeLists.txt`.** The executables no longer list stack sources. They link
  libraries declared by the stack instead:
  - `nanostack_add_library(... CONFIG ws_border_router BORDER_ROUTER DEFINITIONS EXTRA_DEBUG_INFO)` for `wsbrd`;
  - `nanostack_add_library(... CONFIG ws_router)` for `wsnode`;
  - `nanostack_add_rcp_library(...)` for `wshwsim`.

  Each keeps the defines the old targets applied to those files. The stack's
  location is the `NANOSTACK_DIR` cache variable, which defaults to
  `../nanostack-generic-standalone`.
- **`CMAKE_BUILD_TYPE Debug` moved before `add_subdirectory()`,** so that the
  stack is compiled with the same flags as before.
- **`wssimserver` compiles the port's `log.c` itself** (`port/linux/common/log.c` then, `port/posix/common/log.c` since S2). It used to have
  its own copy of `log.c` without `HAVE_BACKTRACE`. Linking the port library
  would have switched backtraces on.
- **Transport includes.** `common/bus_uart.c`, `common/spinel_buffer.[ch]`:
  `#include "log.h"` / `"utils.h"` / `"os_types.h"` became
  `"common/log.h"` and so on, because those headers now live in the stack.
- **Documentation:** this file, the README build section and `.gitignore`.

Nothing else changed. The run scripts, configs and binary locations (in-source
build) are as before.

## Toolchain note

With GCC ≥ 14 the inherited code needs
`-DCMAKE_C_FLAGS="-std=gnu17 -fpermissive"`. GCC 15 defaults to C23 and turns
`incompatible-pointer-types`, `int-conversion` and
`implicit-function-declaration` into errors. This applies equally to the
reference, and it was built the same way for the comparison.

## Verification

Tools are in [`sim_utils/migration/`](sim_utils/migration/):

- `cmp_objs.py <ref_build> <orch_build> <stack_dir>` compares every object of
  the reference build with the object built from the same source here.
- `headless_run.sh <bindir> <workdir> <outdir> <seconds>` runs `dag_simple`
  without gnome-terminal. It starts the same processes, with the same
  arguments, in the same order as `run.sh`. Each log line gets a timestamp,
  and the BR's D-Bus `Nodes` property is read every 30 s.
- `analyze.py <ref runs...> -- <new runs...>` extracts per-node join
  milestones, parents and a message-type histogram, then compares the groups.

### Build equivalence

Both builds used GCC 15.2, `-std=gnu17 -fpermissive` and mbedtls v3.0.0.

- **Stack objects:** the old build compiled stack sources into 317 objects,
  counted per executable. All 317 are identical. The comparison covers
  disassembly with symbolic relocations plus every non-debug section.
- **Application objects:** 50 of 57 are identical. The other 7 differ only by
  the absolute source path that `__FILE__` embeds:
  - `bus_uart.c` and `spinel_buffer.c` in three executables;
  - `sl_rf_driver.c`.
- **Linked executables:** each defines a subset of the old executable's
  symbols. Static archives drop objects that nothing references: 29 symbols in
  `wsbrd`, 174 in `wsnode` (mostly authenticator-only security protocols) and
  5 in `wshwsim` (IPv6 string parsing). Dead code can't change behaviour. The
  only side effect is a different memory layout.

### Runtime equivalence (`dag_simple`, 15 nodes)

Setup: 3 reference runs and 3 extracted runs, alternated, 300 s each, with
the same harness, the same machine and a non-root `wsbrd` (see below). The
first 295 s of each run are compared.

| | Reference (3 runs) | Extracted (3 runs) |
| --- | --- | --- |
| Nodes `BOOTSTRAP_READY` by 295 s | 40 / 42 (14, 13, 13) | 40 / 42 (13, 13, 14) |
| Ready time, median [min–max] | 148.5 s [54.1–282.0] | 149.4 s [59.1–280.6] |
| Wi-SUN parent selected, median | 98.0 s | 94.0 s |
| Authentication success, median | 102.8 s | 97.6 s |
| Parents that aren't `dag_simple` links | none | none |

- **Late node.** It was node 14 every time, in both groups. It is the deepest
  node (4 hops, last link at -59 dBm) and its join lands around 270–290 s.
- **RPL trees.** The trees reported by the BR over D-Bus are the same in both
  groups. Only the usual alternatives vary from run to run in both: node 7
  under 1 or 8, node 9 under 3 or 8.
- **Log messages.** Message-type histograms match. Per-type counts vary by run
  in both groups, mostly advertisement and solicit counts. No message type
  shows up in only one group, apart from a handful of rare ones (a GKH replay
  counter error, seen once, in the reference).

The emulator isn't deterministic: the RNG is unseeded and it runs on wall-clock
time. So equivalence here is statistical. The build-level check above is the
strict one.

**Fresh-clone check.** Both repositories were cloned into an empty
directory and built from scratch. The object comparison gave the same result
(stack objects identical; 7 application objects differing only by source path),
and one 300 s `dag_simple` run matched the runs above: 13/14 ready, node 14
operational at 253 s, all parents real links.

**Harness pitfall found on the way.** `wssimserver` numbers nodes in the order
they connect, not from their MAC address. If the `wshwsim` processes are
started in parallel, the topology is silently scrambled, and parents that
aren't neighbours show up. A first batch had exactly that problem in both
groups. `headless_run.sh` now waits 0.1 s between MAC starts, as
`run_orchestrator.py` does, and `analyze.py` flags any parent that isn't a
`dag_simple` link. This is an existing emulator defect (the node-ID handshake
in the pre-study's fix list), not something the migration introduced.

## After the split: the genericity steps

The stack then went through steps S1–S5 of its
[`docs/GENERICITY_PLAN.md`](../nanostack-generic-standalone/docs/GENERICITY_PLAN.md).
What changed here:

- **S2.** The UART fields of the port's `struct os_ctxt` became
  `struct uart_ctxt` (`common/bus_uart.h`), embedded in the wsbrd, wsnode and
  wshwsim contexts. The FHSS timer `app_wshwsim/hal_fhss_timer.c` moved into the
  stack's port as `ns_port_fhss_timer`. `cmp_objs.py` maps the moved files.
- **S4.** The `pselect()` loops in `main()` of `wsbr.c`, `wsrouter.c` and
  `sl_wsrcp.c` are gone. Each program registers its inputs with the port's
  `ns_port_run()` in the order its loop handled them, and the port runs the
  callback timers and FHSS timers. wshwsim still dispatches one event per
  iteration; wsbrd keeps the poll timeout its loop had (the leftover of its
  wait for the RCP). Callback timer IDs are now small indices, not timerfds.
- **S5.** Two more programs run the MAC in the same process as the stack, on
  wshwsim's simulated radio: `wsnode-inproc` and `wsbrd-inproc` (`MAC_INPROCESS`
  in `wsrouter.c` and `wsbr.c`, `app_wshwsim/sl_rf_inproc.c`).
  - They take `--eui64=ADDR` (wshwsim's `-m`), and `-u` names the wssimserver
    socket instead of a UART.
  - They connect to wssimserver themselves, so start them in node order, with
    the border router (node 0) first.
  - `sl_rf_driver.c` now reads its socket, pcap output and the neighbour list
    of its CCA from `g_rf_host` instead of wshwsim's context. In-process, the
    neighbour list is filled by wrapping the stack's
    `ns_fhss_ws_update_neighbor()` (`-Wl,--wrap`), as wshwsim fills it from
    Spinel.
  - Run them as a normal user, like the non-root wsbrd below: their radio
    registers on the user D-Bus, and the shared memory (`/wssimcca`,
    `/wssimnodesinfos`) must stay writable by the wshwsim processes.
  - `headless_run.sh` takes a fifth argument: `split` (default), `mixed` (split
    border router, in-process routers) or `inproc`.

Gates:

| Step | Build | Runtime (`dag_simple`, 300 s runs) |
| --- | --- | --- |
| S2 | Stack objects identical to the reference | 5 reference + 5 new runs: 64/70 vs 66/70 routers ready, median ready time 152 s vs 144 s, no parent outside the topology, same message types |
| S4 | Stack objects identical to the reference | 5 split runs: 63/70 routers ready, median 152 s, no parent outside the topology. The only message types missing are rare reference ones (under 1 per run) that depend on which node became a parent |
| S5, mixed | — | 5 runs (split BR, in-process routers): 66/70 ready, median 137 s, no parent outside the topology |
| S5, in-process | — | 5 runs: 66/70 ready, median 148 s, no parent outside the topology |
| Stack fixes (workspace GS6) | Only `ws_bootstrap.c`, `fhss_ws.c` and the `rand` helpers differ from the reference | 5 + 3 split runs: 68/70 and 38/42 ready, median 152 s and 144 s. With `allowed_channels = 0-15` on every node, which failed before (0/14 joined, unicast frames sent to excluded channels): 3 split runs 41/42, 3 in-process runs 40/42 |
| Emulator fixes (workspace EM1) | Stack unchanged; `wssimserver` and the radio driver changed (README, "Node IDs, frames and losses") | `WSSIM_SEED` 1–5: 5 split runs 64/70 ready, median 154 s [55–288]; 5 in-process runs 65/70, median 154 s [50–286]; no parent outside the topology. In-process nodes started all at once in reverse order: no parent outside the topology |
| Time and randomness from the port (workspace EM3 step 0) | Stack unchanged; built with `NANOSTACK_TLS_STRONG_PORT_ENTROPY=ON` against an mbedtls v3.0.0 built with `MBEDTLS_NO_PLATFORM_ENTROPY` (README, "Compile mbedtls"). The radio driver and the applications read time through `ns_port/time.h`; the FHSS-state thread is a poll hook | `WSSIM_SEED` 1–5: 5 split runs 63/70 ready, median 150 s [56–288]; `WSSIM_SEED` 1–10: 10 in-process runs 133/140, median 149 s [51–292] (seeds 1–5: 66/70, 148 s); no parent outside the topology; EAP-TLS authentication succeeded on every node that became ready |
| Virtual time, in-process nodes (workspace EM3 step 1) | Stack: new simulation port `port/sim` (`NANOSTACK_PORT=nanostack_port_sim`); POSIX objects unchanged. `wssimserver --vtime` is the conductor; virtual-time build in `build-vt/` (`docs/VIRTUAL_TIME.md`) | `dag_simple`, 300 virtual s, `WSSIM_SEED` 1–10: 134/140 ready, median 146 s [55–293], against the 10 wall-clock in-process runs of step 0 (133/140, 149 s [51–292]); no parent outside the topology. Determinism: 8 runs of seed 1 at the same time byte-identical, seed 2 twice identical and different from seed 1 (`setarch -R`, printed pointers masked). Final figures, after the pre-merge review's fixes (per-node random seeds hashed: before, nodes' random streams were shifted copies of one another; the rare "invalid replay counter" errors of the first gate did not reappear). Wall-clock build after the merge: one split run 14/14 ready, one in-process run 13/14. Speed, 60 virtual s: 15 nodes 0.015 wall s per simulated s, 100 nodes 0.072, 500 nodes 0.41 (2.5 GB in all) |

All runs were on 2026-10-23/27 against the same 5 reference runs (64/70 ready,
median 152 s). In the in-process groups the node logs also hold the radio
driver's messages, so their message histograms are not comparable with the
split ones. In-process mode is expected to differ slightly (GENERICITY_PLAN.md
§2.2: exact timestamps, MTU starting at 127); no difference shows at this
scale. The emulator's default stays the split mode; `run.sh`, the GUI and
`run_orchestrator.py` still start wsbrd/wsnode with wshwsim.

## Running without root

`wsbrd` can run as a normal user when `tun0` already exists, is owned by that
user and is up. This is supported upstream (`app_wsbrd/tun.c`), so the
verification runs used no `sudo`:

```bash
sudo ip tuntap add mode tun user $USER tun0
sudo ip addr add fd12:3456::2e99:8528:350:bbb7/64 dev tun0
sudo ip link set tun0 up
```

As non-root, `wsbrd` registers `com.silabs.Wisun.BorderRouter` on the user
bus instead of the system bus.
