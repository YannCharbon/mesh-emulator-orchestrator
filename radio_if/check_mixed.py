#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Checks of radio_if/test_mixed.sh (see its header).

    check_mixed.py <outdir> <baseline run.sh status> <mixed run.sh status> <seconds>
"""
import json
import os
import re
import sys

out, base_rc, mixed_rc, dur = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4])
IF = (20, 21, 22)                         # interface clients
NS = tuple(range(0, 15))                  # dag_simple: border router 0, routers 1-14
SHR, PHR, FCS, RATE = 8, 2, 2, 150000     # the clients' defaults, and their --rate
results = []


def res(name, ok, detail):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")


def text(*p):
    path = os.path.join(out, *p)
    if not os.path.exists(path):
        return ""
    with open(path) as f:
        return f.read()


def joined(run):
    n = re.findall(r"brtree t=\S+ nodes=(\d+)", text(run, "br.log"))
    return int(n[-1]) if n else 0


def exit_codes(run):
    return dict(ln.split() for ln in text(run, "exit_codes.txt").splitlines())


codes = {r: exit_codes(r) for r in ("base", "mixed")}
bad = {r: {k: v for k, v in c.items() if v != "0"} for r, c in codes.items()}
res("run", base_rc == 0 and mixed_rc == 0 and codes["base"] and codes["mixed"] and not any(bad.values()),
    f"run.sh status baseline {base_rc}, mixed {mixed_rc}; {len(codes['mixed'])} programs in the mixed run "
    f"(the 3 clients included: {all(f'if{i}' in codes['mixed'] for i in IF)}); non-zero {bad}")

jb, jm = joined("base"), joined("mixed")
res("formation", jm == 14 and jb == 14, f"routers in the tree at {dur:.0f} s: baseline {jb}, mixed {jm} (of 14)")

# medium log: t_end src dst frame_id outcome rx_dbm sinr
med = [ln.split() for ln in text("mixed", "medium.log").splitlines()]
oc = {}
for t, src, dst, fid, outcome, *_ in med:
    k = (int(src), int(dst), outcome)
    oc[k] = oc.get(k, 0) + 1


def count(srcs, dsts, outcomes):
    return sum(v for (s, d, o), v in oc.items() if s in srcs and d in dsts and o in outcomes)


# client logs: "<t> <node> <event> k=v ..."
ev = {}
for i in IF:
    ev[i] = []
    for ln in text("mixed", f"if{i}.log").splitlines():
        f = ln.split()
        if len(f) >= 3 and f[0].isdigit():
            ev[i].append((int(f[0]), f[2], dict(kv.split("=", 1) for kv in f[3:] if "=" in kv)))


def n_ev(i, kind, src=None):
    return sum(1 for t, k, kv in ev[i] if k == kind and (src is None or int(kv["src"]) == src))


sent = {i: n_ev(i, "tx") for i in IF}
lines, consistent = [], True
for s in IF:
    for d in IF:
        if s == d:
            continue
        got, lost = n_ev(d, "rx_ok", s), n_ev(d, "rx_lost", s)
        m_ok, m_lost = oc.get((s, d, "ok"), 0), count({s}, {d}, {"noise", "interference"})
        consistent &= got == m_ok and lost == m_lost and got > 0
        lines.append(f"{s}->{d} {got}/{sent[s]}" + ("" if got == m_ok and lost == m_lost else
                                                  f" (medium ok {m_ok} lost {m_lost}, client lost {lost})"))
# the always-on receivers (20, 22) take most frames of the other clients
num = sum(n_ev(d, "rx_ok", s) for d in (20, 22) for s in IF if s != d)
den = sum(sent[s] for d in (20, 22) for s in IF if s != d)
rate = num / den if den else 0
res("if_if", consistent and rate >= 0.8 and all(sent.values()),
    f"frames sent {sent}; received/sent per pair (client logs = medium log): {', '.join(lines)}; "
    f"always-on receivers {100 * rate:.1f} %")

ns_rx = {i: sum(n_ev(i, "rx_ok", s) for s in NS) for i in IF}
m_ns_if = {i: count(set(NS), {i}, {"ok"}) for i in IF}
res("ns_to_if", all(ns_rx[i] > 0 and ns_rx[i] == m_ns_if[i] for i in IF),
    f"Wi-SUN frames received by the clients {ns_rx} (medium log {m_ns_if})")

if_ns = count(set(IF), set(NS), {"ok"})
res("if_to_ns", if_ns > 0 and not bad["mixed"],
    f"client frames received by Wi-SUN nodes {if_ns}; every program exited with status 0: {not bad['mixed']}")

# Collisions across the kinds, from the medium log and frames.txt: a "busy"
# line (frame X reaching receiver D while D receives frame Y on the same
# channel: X interferes with Y) is paired with Y, the frame D was locked onto
# then. Frame IDs are run-wide and in sending order, as frames.txt's lines.
frames = [ln.split() for ln in text("mixed", "frames.txt").splitlines()]
start = {k + 1: (int(f[0]), int(f[1])) for k, f in enumerate(frames)}   # frame ID -> (start, sender)
locks = {}                                # receiver -> [(start, end, frame ID, outcome)]
for t, src, dst, fid, outcome, *_ in med:
    if outcome in ("ok", "noise", "interference", "captured", "tx_abort", "left"):
        locks.setdefault(int(dst), []).append((start[int(fid)][0], int(t), int(fid), outcome))
cross = {"if_on_ns": [0, 0], "ns_on_if": [0, 0]}   # [collisions, of which the frame was lost]
for t, src, dst, fid, outcome, *_ in med:
    if outcome not in ("busy", "captured"):
        continue
    x_start = start[int(fid)][0]
    for l_start, l_end, l_fid, l_out in locks.get(int(dst), []):
        if l_fid != int(fid) and l_start <= x_start < l_end:
            y_src = start[l_fid][1]
            kind = ("if_on_ns" if int(src) in IF and y_src in NS and int(dst) in NS else
                    "ns_on_if" if int(src) in NS and y_src in IF and int(dst) in IF else None)
            if kind:
                cross[kind][0] += 1
                cross[kind][1] += l_out != "ok"
lost_ns = count(set(NS), set(NS), {"interference"})
base_med = [ln.split() for ln in text("base", "medium.log").splitlines()]
base_lost_ns = sum(1 for t, s, d, f, o, *_ in base_med if o == "interference")
res("collisions", cross["if_on_ns"][0] > 0 and cross["ns_on_if"][0] > 0 and
    cross["if_on_ns"][1] + cross["ns_on_if"][1] > 0,
    f"client frames on Wi-SUN receptions {cross['if_on_ns'][0]} (frame lost {cross['if_on_ns'][1]}), "
    f"Wi-SUN frames on client receptions {cross['ns_on_if'][0]} (lost {cross['ns_on_if'][1]}); Wi-SUN frames "
    f"lost to interference at Wi-SUN nodes {lost_ns} (baseline {base_lost_ns}), at clients "
    f"{count(set(NS), set(IF), {'interference'})}; client frames lost at clients "
    f"{count(set(IF), set(IF), {'interference'})}, at Wi-SUN nodes {count(set(IF), set(NS), {'interference'})}")

rep = json.loads(text("mixed", "power.json") or "{}")
pn = {n["node"]: n for n in rep.get("nodes", [])}
issues = []
if sorted(pn) != sorted(NS + IF):
    issues.append(f"nodes {sorted(pn)}")
for nid, n in pn.items():
    c = n["check"]
    if abs(c["mcu_time_us"] - dur * 1e6) > 1 or abs(c["radio_time_us"] - dur * 1e6) > 1:
        issues.append(f"node {nid} sums {c}")
for i in IF:
    if i not in pn:
        continue
    # a frame that starts before the run's end is charged up to the end
    air = sum(min(int(kv["airtime_us"]), int(dur * 1e6) - t) for t, k, kv in ev[i] if k == "tx")
    radio = pn[i]["radio"]["states"]
    tx_us, rx_us = radio["tx"]["time_us"], radio["receive"]["time_us"]
    rx_frames = n_ev(i, "rx_ok") + n_ev(i, "rx_lost")
    if abs(tx_us - air) > 1:
        issues.append(f"node {i} TX {tx_us} us, its frames {air} us")
    if rx_frames == 0 or rx_us <= 0:
        issues.append(f"node {i} receive {rx_us} us for {rx_frames} frames")
avg = {i: round(pn[i]["avg_current_ma"], 3) for i in IF if i in pn}
avg_ns = sum(pn[i]["avg_current_ma"] for i in NS if i in pn) / len(NS)
res("power", not issues, f"{len(pn)} nodes, state times sum to {dur:.0f} s; client average current (mA) {avg}, "
    f"Wi-SUN nodes {avg_ns:.3f} mA on average" + (f"; {issues}" if issues else ""))

srv = text("mixed", "simserver.log")
errs = [ln for ln in srv.splitlines() if "radio interface:" in ln or "unexpected message" in ln]
res("server", not errs and "end (end time reached)" in srv, f"{len(errs)} interface warnings" +
    (f": {errs[:3]}" if errs else "") + "; " + " ".join(re.findall(r"virtual time: reached .*", srv)))
print(f"{'PASS' if all(results) else 'FAIL'} test_mixed ({sum(results)}/{len(results)} checks), logs in {out}")
sys.exit(0 if all(results) else 1)
