#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Test of the conductor's power ledger (docs/POWER_MODEL.md), with one
scripted node over a real socket and a test profile with round numbers.

usage: test_power.py <wssimserver> [<scratch dir>]

The node's script exercises what FFN runs cannot yet: the radio off, deep
sleep chosen by the rule of include/ns_port/power.h, wake and entry
transitions, radio transitions, a CCA and TX between two profile levels. The
expected totals are worked out by hand below (one line per interval), and the
report must match them exactly. Exit status 0 when it does.

Per-PHY-mode values (POWER_MODEL.md §6.2): the same script again with a
profile that has "@3" values, the radio reporting mode 3 (5), mode 1a (1) or
nothing, and with --power-mode; each case says which values must apply.
"""
import json, os, socket, struct, subprocess, sys, tempfile, time

HELLO = struct.Struct("=2sHHH")
WELCOME = struct.Struct("=2sHIQiI")
ADVANCE = struct.Struct("=2s3HQ")
IDLE = struct.Struct("=2s3HQ")
RADIO = struct.Struct("=2sBbBBH")         # magic, state, tx_dbm, events, phy_mode, channel
WAKE = struct.Struct("=2sH6H")            # magic, events, timers[tick protocol mac fhss radio other]
NEVER = 2**64 - 1
OFF, IDLE_ST, LISTEN, RECEIVE, TX = range(5)
EV_CCA = 1
TICK, RADIO_CAUSE = 0, 4

PROFILE = """
[profile]
name = test
kind = soc
supply_v = 2.0
[mcu]
clock_mhz = 1            # one cycle = 1 us
active_ma = 1
sleep_ma = 0.5
deep_sleep_ma = 0.001
sleep_enter_us = 1
sleep_enter_ma = 1
sleep_wake_us = 1
sleep_wake_ma = 1
deep_enter_us = 10
deep_enter_ma = 1
deep_wake_us = 100
deep_wake_ma = 0.2
wake_charge_uc = 0.5     # optional key: 6 wakes = 3 uC
[cycles]
wake = 100
tick = 10
protocol = 0
mac = 0
fhss = 0
radio = 20
other = 0
event = 0
frame_in = 0
rx_frame = 0
tx_frame = 0
[radio]
off_ma = 0
idle_ma = 0.1
listen_ma = 5
receive_ma = 5
cca_ma = 6
cca_us = 100
idle_holds_clock = 0
tx_ma = 0:20 20:40       # 12 dBm: 32 mA
[radio_transitions]
listen_tx = 50 30
tx_listen = 20 10
"""

# The same, with values for PHY mode 3 (cca 200 us, 12 dBm: 52 mA). The
# lines may come before or after the common value, and in any section.
PROFILE_MODES = PROFILE.replace("cca_us = 100\n", "cca_us@3 = 200\ncca_us = 100\n") + """
[radio]
tx_ma@3 = 0:40 20:60
"""


def wake(events=0, tick=0, radio=0):
    t = [0] * 6
    t[TICK], t[RADIO_CAUSE] = tick, radio
    return WAKE.pack(b"xc", events, *t)


PHY_MODE = 0   # what the radio reports in each "xr" (sim_proto.h, SIM_PHY_MODE_*)


def xr(state, dbm=0, events=0):
    return RADIO.pack(b"xr", state, dbm, events, PHY_MODE, 0)


# The node's script: for each wake (time), what it sends before "xi", and
# its next deadline. A function: xr() takes PHY_MODE when called.
def script():
    return [
        (0,         [xr(LISTEN), wake()],                                   10_000),
        (10_000,    [wake(tick=1)],                                         20_000),
        (20_000,    [xr(LISTEN, events=EV_CCA), xr(TX, 12), wake(radio=1)], 21_000),
        (21_000,    [xr(LISTEN), wake(radio=1)],                            1_000_000),
        (1_000_000, [xr(OFF), wake()],                                      3_000_000),
        (3_000_000, [wake(tick=1)],                                         NEVER),
    ]
END_US = 4_000_000

# Expected, by hand (µs; charges in µC = mA × µs / 1000):
# MCU   active 100+110+120+120+100+110 = 660            × 1     = 0.66
#       sleep  9898+9888+878+978878 = 999542            × 0.5   = 499.771
#       deep   1999790 (1000100..3e6) + 999880 (to end) × 0.001 = 2.99967
#       sleep_enter 4 × 1 us, sleep_wake 4 × 1 us       × 1     = 0.004 + 0.004
#       deep_enter 2 × 10 us (× 1 mA) = 0.02; deep_wake 1 × 100 us (× 0.2) = 0.02
# Radio listen 19900 + 978980 = 998880                  × 5     = 4994.4
#       cca 100 × 6 = 0.6; tx 950 × 32 = 30.4
#       listen>tx 50 × 30 = 1.5; tx>listen 20 × 10 = 0.2; off 3000000 × 0
# Wake charge 6 wakes × 0.5 µC = 3, no time of its own
EXPECT = {
    'mcu': {'active': (660, 0.66), 'sleep': (999542, 499.771), 'deep_sleep': (2999670, 2.99967),
            'sleep_enter': (4, 0.004), 'sleep_wake': (4, 0.004),
            'deep_enter': (20, 0.02), 'deep_wake': (100, 0.02)},
    'radio': {'listen': (998880, 4994.4), 'cca': (100, 0.6), 'tx': (950, 30.4), 'off': (3_000_000, 0),
              'idle': (0, 0), 'receive': (0, 0)},
    'transitions': {'listen>tx': (50, 1.5), 'tx>listen': (20, 0.2)},
    'wake_charge_uc': 3.0,
    'charge_uc': 5533.57867,
    'wakes': 6, 'empty_ticks': 2,
}

# With the mode 3 values: the CCA is 100 us longer (taken from listening) at
# 6 mA instead of 5, and TX at 12 dBm is 52 mA.
EXPECT_M3 = json.loads(json.dumps(EXPECT))
EXPECT_M3['radio'].update({'listen': (998780, 4993.9), 'cca': (200, 1.2), 'tx': (950, 49.4)})
EXPECT_M3['charge_uc'] = 5533.57867 - 0.5 + 0.6 + 19.0

# (name, profile, PHY mode the radio reports, wssimserver options,
#  expected totals, phy_mode, profile_values, profile_values_from)
CASES = [
    ("common profile",            PROFILE,       0, [],                     EXPECT,    None, "common", "radio"),
    ("common profile, mode 3",    PROFILE,       5, [],                     EXPECT,    "3",  "common", "radio"),
    ("mode 3 values, mode 3",     PROFILE_MODES, 5, [],                     EXPECT_M3, "3",  "3",      "radio"),
    ("mode 3 values, mode 1a",    PROFILE_MODES, 1, [],                     EXPECT,    "1a", "common", "radio"),
    ("mode 3 values, no mode",    PROFILE_MODES, 0, [],                     EXPECT,    None, "common", "radio"),
    ("--power-mode=common",       PROFILE_MODES, 5, ["--power-mode=common"], EXPECT,   "3",  "common", "--power-mode"),
    ("--power-mode=0:3",          PROFILE_MODES, 1, ["--power-mode=0:3"],   EXPECT_M3, "1a", "3",      "--power-mode"),
    ("--power-mode=1-4:3, =1a", PROFILE_MODES, 5, ["--power-mode=1-4:3", "--power-mode=1a"], EXPECT, "3", "common",
     "--power-mode"),
]


def recv_exact(s, st):
    data = s.recv(64)
    if len(data) < st.size:
        raise SystemExit(f"short message {data!r}")
    return st.unpack(data[:st.size])


def run_case(server, work, profile, phy_mode, opts, expect, want_mode, want_values, want_from):
    global PHY_MODE
    PHY_MODE = phy_mode
    os.makedirs(work, exist_ok=True)
    prof = os.path.join(work, "test.ini")
    out = os.path.join(work, "power.json")
    sock = tempfile.mktemp(prefix="wssim-power-", suffix=".sock", dir="/tmp")  # sun_path is short
    with open(prof, "w") as f:
        f.write(profile)
    log = open(os.path.join(work, "simserver.log"), "w")
    p = subprocess.Popen([server, "--vtime=1", f"--vtime-end={END_US / 1e6}", f"--power={prof}",
                          f"--power-out={out}", f"--power-log={work}/power_log.txt", *opts, sock],
                         stdout=log, stderr=subprocess.STDOUT)
    for _ in range(100):
        if os.path.exists(sock):
            break
        time.sleep(0.05)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    s.connect(sock)
    s.send(HELLO.pack(b"xh", 0, 1, 0))
    magic, version, flags, seed, _, _ = recv_exact(s, WELCOME)
    assert magic == b"xw" and flags & 1, (magic, flags)
    for i, (t, msgs, nxt) in enumerate(script()):
        if i:
            magic, frames, _, _, now = recv_exact(s, ADVANCE)
            assert magic == b"xa" and now == t and frames == 0, (magic, now, t)
        for m in msgs:
            s.send(m)
        s.send(IDLE.pack(b"xi", 0, 0, 0, nxt))
    assert s.recv(64)[:2] == b"xq"
    s.close()
    p.wait(10)
    log.close()
    if os.path.exists(sock):
        os.unlink(sock)

    d = json.load(open(out))
    n = d['nodes'][0]
    bad = []

    def near(what, got, want, tol=1e-6):
        if abs(got - want) > tol:
            bad.append(f"{what}: {got}, expected {want}")

    for st, (t, q) in expect['mcu'].items():
        near(f"mcu {st} time", n['mcu']['states'][st]['time_us'], t)
        near(f"mcu {st} charge", n['mcu']['states'][st]['charge_uc'], q)
    for st, (t, q) in expect['radio'].items():
        near(f"radio {st} time", n['radio']['states'][st]['time_us'], t)
        near(f"radio {st} charge", n['radio']['states'][st]['charge_uc'], q)
    for tr, (t, q) in expect['transitions'].items():
        near(f"{tr} time", n['radio']['transitions'][tr]['time_us'], t)
        near(f"{tr} charge", n['radio']['transitions'][tr]['charge_uc'], q)
    near("wake charge", n['mcu']['wake_charge']['charge_uc'], expect['wake_charge_uc'])
    near("charge", n['charge_uc'], expect['charge_uc'])
    near("average", n['avg_current_ma'], expect['charge_uc'] * 1000 / END_US)
    near("mcu sum", n['check']['mcu_time_us'], END_US)
    near("radio sum", n['check']['radio_time_us'], END_US)
    near("wakes", n['wakes']['total'], expect['wakes'])
    near("empty ticks", n['wakes']['empty_ticks'], expect['empty_ticks'])
    near("deep_min_us", d['profiles'][0]['deep_min_us'], 110)
    for key, want in (("phy_mode", want_mode), ("profile_values", want_values), ("profile_values_from", want_from)):
        if n.get(key, "missing") != want:
            bad.append(f"{key}: {n.get(key, 'missing')}, expected {want}")
    return bad


def main():
    server = sys.argv[1]
    work = sys.argv[2] if len(sys.argv) > 2 else tempfile.mkdtemp(prefix="test_power.")
    failed = 0
    for i, case in enumerate(CASES):
        bad = run_case(server, os.path.join(work, f"case{i}"), *case[1:])
        for b in bad:
            print(f"FAIL [{case[0]}]", b)
        print(f"  {case[0]}: {'FAILED' if bad else 'ok'}")
        failed += bool(bad)
    # Profiles the parser must reject, with the message's start
    for name, profile, msg in [
        ("unknown mode", PROFILE_MODES.replace("cca_us@3", "cca_us@3c"), "unknown PHY mode"),
        ("[profile] key per mode", PROFILE.replace("supply_v = 2.0", "supply_v = 2.0\nsupply_v@3 = 3"),
         "[profile] keys do not depend"),
        ("mode value twice", PROFILE_MODES + "cca_us@3 = 1\n", "radio.cca_us given twice"),
        ("unknown key per mode", PROFILE + "[radio]\nfoo@3 = 1\n", "unknown key radio.foo"),
    ]:
        d = os.path.join(work, "reject")
        os.makedirs(d, exist_ok=True)
        prof = os.path.join(d, "test.ini")
        with open(prof, "w") as f:
            f.write(profile)
        sock = tempfile.mktemp(prefix="wssim-power-", suffix=".sock", dir="/tmp")
        # The profiles load when the nodes are there: connect one
        r = subprocess.Popen([server, "--vtime=1", "--vtime-end=1", f"--power={prof}", sock],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for _ in range(100):
            if os.path.exists(sock):
                break
            time.sleep(0.05)
        c = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        c.connect(sock)
        c.send(HELLO.pack(b"xh", 0, 1, 0))
        try:
            out, _ = r.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            r.kill()
            out, _ = r.communicate()
        c.close()
        ok = r.returncode != 0 and msg in out
        if os.path.exists(sock):
            os.unlink(sock)
        print(f"  rejects {name}: {'ok' if ok else 'FAILED'}")
        failed += not ok
    print(f"test_power: {'FAILED' if failed else 'PASSED'} ({work})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
