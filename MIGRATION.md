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
| `common/` OS layer: `hal_interrupt`, `os_timer`, `os_scheduler`, `os_types.h`, `log`, `rand`, `slist`, `utils.h`, `backtrace_show` | stack: `port/linux/common/` |
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
- **`wssimserver` compiles `port/linux/common/log.c` itself.** It used to have
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

**Harness pitfall found on the way.** `wssimserver` numbers nodes in the order
they connect, not from their MAC address. If the `wshwsim` processes are
started in parallel, the topology is silently scrambled, and parents that
aren't neighbours show up. A first batch had exactly that problem in both
groups. `headless_run.sh` now waits 0.1 s between MAC starts, as
`run_orchestrator.py` does, and `analyze.py` flags any parent that isn't a
`dag_simple` link. This is an existing emulator defect (the node-ID handshake
in the pre-study's fix list), not something the migration introduced.

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
