#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Tests of the floor plan (docs/MEDIUM.md §11.11) and of --describe=json
(§11.12), with fake nodes and --prop-dump.

    python3 sim_utils/vtime/test_floorplan.py build-vt/wssimserver [--sock /tmp/fp_sock] [--logdir DIR]

Exit status 0 when every test passes.
"""
import argparse
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_conductor as tc  # noqa: E402
from test_conductor import Radio, Server, check, medium_run  # noqa: E402
from test_interf import energy  # noqa: E402

ARGS = None
SLABS = ["--prop-floor-model=slabs"]
MAT = {}            # built-in materials, from --describe=json


def write(name, text):
    path = os.path.join(ARGS.logdir, name)
    with open(path, "w") as f:
        f.write(text)
    return path


def fspl(d, f=915.0):
    return 20 * math.log10(f) - 27.55 + 20 * math.log10(max(d, 1.0))


def build(pos, fp, opts=(), seed="1", env=None):
    """Start the server with a floor plan, stop it once the model is built.
    Returns (output, {(rx, tx): row}) from --prop-dump."""
    dump = os.path.join(ARGS.logdir, "dump.txt")
    if os.path.exists(dump):
        os.unlink(dump)
    args = ["--vtime=1", f"--positions={write('n.pos', pos)}", f"--prop-dump={dump}", "--prop-shadow=0",
            "--prop-cutoff=-250"]
    if fp is not None:
        args.append(f"--prop-floorplan={write('n.fp', fp)}")
    srv = Server(args + list(opts), {"WSSIM_SEED": seed, **(env or {})})
    srv.p.terminate()
    st = srv.wait()
    out = srv.output()
    check(os.path.exists(dump), f"no dump (status {st}): {out[-800:]}")
    rows = {}
    with open(dump) as f:
        for ln in f:
            if ln.startswith("#"):
                continue
            a = ln.split()
            r = {"d": float(a[2]), "floors": int(a[3]), "walls": int(a[4]), "pl": float(a[5]), "dbm": float(a[8])}
            if len(a) > 11:
                r.update(wn=float(a[11]), wdb=float(a[12]), fdb=float(a[13]), crossed=a[14])
            rows[(int(a[0]), int(a[1]))] = r
    return out, rows


def refused(opts, msg, pos="0 0 0 0\n1 10 0 0\n", fp=None):
    args = ["--vtime=1", *opts]
    if pos is not None:
        args.append(f"--positions={write('r.pos', pos)}")
    if fp is not None:
        args.append(f"--prop-floorplan={write('r.fp', fp)}")
    srv = Server(args)
    st = srv.wait()
    check(st != 0 and msg in srv.output(), f"{opts} {fp!r} accepted, or no \"{msg}\": {srv.output()[-500:]}")


# --- tests -------------------------------------------------------------------

def test_describe():
    """--describe=json: valid JSON, every option of --help described, the
    presets' values from the preset table, the built-in materials."""
    p = subprocess.run([ARGS.bin, "--describe=json"], capture_output=True, text=True, timeout=10)
    check(p.returncode == 0, f"status {p.returncode}: {p.stderr[-500:]}")
    d = json.loads(p.stdout)
    names = {o["name"] for o in d["options"]}
    keys = {"name", "group", "arg", "type", "values", "default", "effective_default", "presets", "repeatable", "needs",
            "section", "help"}
    for o in d["options"]:
        check(set(o) == keys, f"keys of {o['name']}: {sorted(o)}")
        check(o["type"] in ("flag", "int", "number", "enum", "string", "file", "spec", "list"), f"type {o}")
        check(o["type"] != "enum" or o["values"], f"enum without values: {o}")
        check(o["help"] and o["group"], f"no help or group: {o}")
        check(o["needs"] is None or o["needs"] in names, f"needs an unknown option: {o}")
    h = subprocess.run([ARGS.bin, "--help"], capture_output=True, text=True, timeout=10)
    in_help = set(re.findall(r"--([a-z][a-z0-9-]+)", h.stdout))
    missing = in_help - names
    check(not missing, f"options in --help without a description: {sorted(missing)}")
    pre = {p["name"]: p for p in d["presets"]}
    check(set(pre) == {"calibrated", "realistic", "basic", "em4"} and pre["calibrated"]["default"], f"{pre}")
    def as_opts(name, v):
        """The command-line options of a preset value: a string, true (a
        flag) or a list (the option given several times)."""
        return [f"--{name}" if x is True else f"--{name}={x}" for x in (v if isinstance(v, list) else [v])]

    dflt = next(p for p in d["presets"] if p["default"])
    for o in d["options"]:
        for pname, v in o["presets"].items():
            check(all(x in pre[pname]["options"] for x in as_opts(o["name"], v)), f"{o['name']} {pname} {v}")
        # effective_default: the default preset's value, else the built-in default
        want = o["presets"].get(dflt["name"], o["default"])
        check(o["effective_default"] == want, f"effective_default of {o['name']}: {o['effective_default']} != {want}")
    for pname, p in pre.items():
        for opt in p["options"]:
            n = opt[2:].split("=", 1)[0]
            o = next(x for x in d["options"] if x["name"] == n)
            check(opt in as_opts(n, o["presets"].get(pname)), f"preset {pname} {opt} not in {o}")
    eff = {o["name"]: o["effective_default"] for o in d["options"]}
    check(eff["medium-rx-model"] == "layered" and eff["medium-tx-max"] == "16.9" and eff["prop-shadow"] == "4",
          f"effective defaults {eff['medium-rx-model']} {eff['medium-tx-max']} {eff['prop-shadow']}")
    mats = {m["name"] for m in d["materials"]}
    check({"plasterboard", "wood", "glass", "brick", "concrete", "concrete-slab", "reinforced-concrete", "metal",
           "partition"} <= mats, f"materials {mats}")
    for m in d["materials"]:
        check(re.fullmatch(r"#[0-9a-f]{6}", m["color"]) and m["basis"].split(":")[0] in ("measured", "computed", "assumed"),
              f"{m}")
    check(subprocess.run([ARGS.bin, "--describe=xml"], capture_output=True, timeout=10).returncode != 0,
          "--describe=xml accepted")
    print(f"     {len(d['options'])} options, {len(d['materials'])} materials, {len(d['presets'])} presets")


def test_parser():
    ok = "material m 3\nwall 0 0 0 1 0 m\n"
    refused(["--prop-floorplan=/dev/null"], "need --positions", pos=None)
    refused(["--prop-floor-model=slabs"], "need --prop-floorplan")
    refused(["--prop-floor-b=0.3"], "need --prop-floorplan")
    refused(["--prop-floor-model=bogus"], "cost231, slabs or table", fp=ok)
    # Each floor option belongs to one floor term
    refused(["--prop-floor-model=table"], "needs --prop-floor-db", fp=ok)
    refused(["--prop-floor-db=10"], "needs --prop-floor-model=table", fp=ok)
    refused(["--prop-floor-db=10", "--prop-floor-model=slabs"], "needs --prop-floor-model=table", fp=ok)
    refused(["--prop-floor-b=0.3", "--prop-floor-model=slabs"], "needs --prop-floor-model=cost231", fp=ok)
    refused(["--prop-floor-b=0.3", "--prop-floor-model=table", "--prop-floor-db=5"], "needs --prop-floor-model=cost231",
            fp=ok)
    refused([f"--prop-floorplan={ARGS.logdir}/missing.fp"], "missing.fp")
    for fp, msg in [
        ("door 0 1 2\n", "unknown directive"),
        ("wall 0 0 0 1 0\n", "expected wall FLOOR"),
        ("wall 0 0 0 1 0 brick extra\n", "expected wall FLOOR"),
        ("wall 0.5 0 0 1 0 brick\n", "bad FLOOR"),
        ("wall 20000 0 0 1 0 brick\n", "bad FLOOR"),
        ("wall 0 nan 0 1 0 brick\n", "bad coordinate"),
        ("wall 0 0 0 2e6 0 brick\n", "bad coordinate"),
        ("wall 0 1 1 1 1 brick\n", "shorter than 1 mm"),
        ("wall 0 0 0 1 0 unobtainium\n", "unknown material"),
        ("slab 0 unobtainium\n", "unknown material"),
        ("material m 3\nmaterial m 4\n", "defined twice"),
        ("material m -1\n", "bad loss"),
        ("material 9m 3\n", "starts with a letter"),
        ("material m/x 3\n", "bad material name"),
        ("material m\n", "expected material"),
        ("slab 0 brick\nslab 0 glass\n", "given twice"),
        ("floorheight 3\nfloorheight 4\n", "given twice"),
        ("floorheight 0\n", "bad floor height"),
        ("outline 0 0 0 1 0\n", "at least 3 points"),
        ("outline 0 0 0 1 0 0 0\n", "at least 3 points"),     # the closing point repeats the first
        ("outline 0 0 0 1 0 1 1 brick wood\n", "bad coordinate"),
        ("outline 0 0 0 1 0 1 1 1 1 brick\n", "shorter than 1 mm"),
    ]:
        if msg:
            refused([], msg, fp=fp)
    # Accepted: a colour, a material after its use, overriding a built-in,
    # long lines, an outline without material (no walls)
    out, r = build("0 0 0 0\n1 10 0 0\n",
                   "#" + "x" * 5000 + "\nwall 0 5 -1 5 1 later   # " + "y" * 3000 + "\n"
                   "material later 7.5 #a0b0c0\nmaterial brick 1\nwall 0 6 -1 6 1 brick\n"
                   "outline 0 -1 -1 20 -1 20 20\n")
    check(abs(r[(1, 0)]["wdb"] - 8.5) < 1e-9 and r[(1, 0)]["walls"] == 2, f"{r[(1, 0)]}")
    check("2 walls on 1 storeys" in out and "1 outlines" in out and "(2 from the file)" in out, out[-1500:])
    # A colour and anything after it are a comment: material m is 3 dB
    out, r = build("0 0 0 0\n1 10 0 0\n", "material m 3 #fff extra words\nwall 0 5 -1 5 1 m\n")
    check(r[(1, 0)]["wdb"] == 3, f"colour: {r[(1, 0)]}")
    # An outline closed by repeating its first point: 4 edges, not 5 (no
    # zero-length edge); the path crosses edge 2 and edge 4
    out, r = build("0 -5 5 0\n1 15 5 0\n", "outline 0 0 0 10 0 10 10 0 10 0 0 glass\n")
    check(r[(1, 0)]["crossed"] == "1.2,1.4" and "4 walls" in out, f"closed outline: {r[(1, 0)]}")
    # Warnings: the file's floor height over the option's, double counting,
    # walls overlapping on one line (counted once where a path crosses both)
    out, r = build("0 0 0 0\n1 10 0 0\n", "floorheight 3\noutline 0 -5 -5 5 -5 5 5 -5 5 brick\n"
                   "wall 0 5 -2 5 2 concrete\n", ["--prop-floor-height=4", "--prop-model=logdist", "--prop-walls=8:1"])
    check("replaces --prop-floor-height 4" in out and "counted twice" in out and "add to the floor plan" in out and
          "1 pairs of walls overlap" in out, out[-1500:])
    check(r[(1, 0)]["crossed"] == "3" and abs(r[(1, 0)]["wdb"] - MAT["concrete"]) < 1e-9, f"overlap: {r[(1, 0)]}")


def test_geometry():
    """Crossing; a contact at the path's end (half); parallel and collinear
    walls (none); vertices on the path: a corner the path grazes (none), a
    wall end touching the path (none), a wall split at the path (one), a T
    (one), a bar with a stem ending on it (one), two crossing walls (two); a
    vertical path; walls of other storeys."""
    pos = ("0 0 0 0 0\n"
           "1 10 0 0 0\n"
           "2 0 4 0 0\n"
           "3 10 4 0 0\n"
           "4 5 2 0 0\n"        # on wall 5 (x = 5)
           "5 0 0 7 2\n")       # above node 0, two floors up
    fp = ("material m 10\n"
          "wall 0 5 -1 5 1 m\n"         # 2: crossed by 0-1
          "wall 0 2 0.5 2 3 m\n"        # 3: crossed by 0-4 at (2, 0.8)
          "wall 0 7 0 7 4 m\n"          # 4: its ends touch 0-1 and 2-3, from one side: not crossed
          "wall 0 5 1.5 5 2.5 m\n"      # 5: node 4 at (5, 2) stands on it
          "wall 0 0 -2 10 -2 m\n"       # 6: parallel to 0-1
          "wall 0 1 4 2 4 m\n"          # 7: along 2-3 (collinear)
          "wall 0 8 -1 8 0 m\n"         # 8, 9: a corner at (8, 0), both below 0-1: grazed
          "wall 0 8 0 9 -1 m\n"
          "wall 1 5 -10 5 10 m\n"       # 10: storey 1 only
          "wall 0 3 3 3 4 m\n"          # 11, 12, 13: a T at (3, 4) on 2-3: the bar split at the path
          "wall 0 3 4 3 5 m\n"          #   (11 below, 12 above) and a stem above (13): 1 below, 2 above
          "wall 0 3 4 4 5 m\n"
          "wall 0 5.5 3 5.5 5 m\n"      # 14: a bar crossed at (5.5, 4) ...
          "wall 0 5.5 4 6.5 5 m\n"      # 15: ... and a stem ending on it there
          "wall 0 8 3 9 5 m\n"          # 16, 17: two walls crossing at (8.5, 4) on 2-3
          "wall 0 9 3 8 5 m\n"
          )
    out, r = build(pos, fp, SLABS)
    x = r[(1, 0)]
    check(x["crossed"] == "2" and x["wn"] == 1 and x["wdb"] == 10, f"0-1: {x}")
    check(abs(x["pl"] - (fspl(10) + 10)) < 0.01, f"0-1 loss {x}")
    x = r[(3, 2)]
    check(x["crossed"] == "11,14,16,17" and x["wdb"] == 40, f"2-3: {x}")
    check(r[(4, 0)]["crossed"] == "3,5:0.5", f"0-4: {r[(4, 0)]}")
    x = r[(5, 0)]
    check(x["crossed"] == "-" and abs(x["fdb"] - 2 * MAT["concrete-slab"]) < 0.01 and x["floors"] == 2, f"0-5 {x}")
    # 5-1: from storey 2 above node 0 down to node 1: s = 2.02 - 2 t; wall 10
    # (storey 1) at x = 5 (t 0.5, s 1.02) counts, wall 2 (storey 0) there does not
    check(r[(5, 1)]["crossed"] == "10", f"5-1: {r[(5, 1)]}")
    # A wall split at the path: one part on each side, the lighter one counts
    out, r = build("0 0 0 0\n1 4 0 0\n2 0 1 0\n3 4 1 0\n",
                   "material m 10\nwall 0 2 -1 2 0 m\nwall 0 2 0 2 1 glass\nwall 0 1 1 1 2 m\nwall 0 1 1 1.5 0 m\n")
    check(r[(1, 0)]["crossed"] == "3" and r[(1, 0)]["wdb"] == MAT["glass"], f"split: {r[(1, 0)]}")
    # 2-3 (y = 1) passes (1, 1), where walls 4 (above) and 5 (below) meet: one of
    # them (equal losses and counts: the side with the lower line); wall 3's end
    # (2, 1) touches it from below: not crossed
    check(r[(3, 2)]["crossed"] == "4", f"corner crossed: {r[(3, 2)]}")


def test_storeys():
    """A wall counts on the storey the path is on at the crossing: a path
    from storey 0 to storey 2 crosses three parallel walls (x = 2, 5, 8),
    one per storey, only where it is on that wall's storey."""
    pos = "0 0 0 1 0\n1 10 0 8 2\n"         # z within the storeys: u = 1/3.5, (8 - 7)/3.5
    fp = "floorheight 3.5\nmaterial m 1\n"
    for f in range(3):
        for x in (2, 5, 8):
            fp += f"wall {f} {x} -1 {x} 1 m\n"   # lines 3.. : storey f, x
    out, r = build(pos, fp, SLABS)
    sa, sb = 1 / 3.5, 2 + 1 / 3.5
    want = []
    for k, x in enumerate((2, 5, 8)):
        f = math.floor(sa + x / 10 * (sb - sa))
        want.append(3 + 3 * f + k)
    check(r[(1, 0)]["crossed"] == ",".join(map(str, sorted(want))), f"{r[(1, 0)]} want {want}")
    # Same with z outside the storey (heights brought into it): 0 and 2 slabs
    out, r = build("0 0 0 50 0\n1 10 0 -9 2\n", fp, SLABS)
    check(r[(1, 0)]["floors"] == 2 and abs(r[(1, 0)]["fdb"] - 2 * MAT["concrete-slab"]) < 0.01, f"{r[(1, 0)]}")


def test_losses():
    """Losses against hand computation: free space + walls + slabs; slab
    materials per floor; the cost231 and table floor terms; an explicit
    model; the wall grid on top; the floor height from the file."""
    pos = "0 0 0 0 0\n1 20 0 0 0\n2 20 0 3 1\n3 20 0 9 3\n4 20 0 -3 -1\n"
    fp = ("floorheight 3\nmaterial thin 2.5\n"
          "wall 0 4 -5 4 5 thin\nwall 0 8 -5 8 5 glass\nwall 0 12 -5 12 5 brick\nwall 0 16 -5 16 5 concrete\n"
          "slab 0 reinforced-concrete\nslab 2 wood\nslab -1 metal\n")
    walls = 2.5 + MAT["glass"] + MAT["brick"] + MAT["concrete"]
    out, r = build(pos, fp, SLABS)
    x = r[(1, 0)]
    check(abs(x["pl"] - (fspl(20) + walls)) < 0.01 and x["walls"] == 4, f"0-1 {x}")
    slab = {0: MAT["reinforced-concrete"], 1: MAT["concrete-slab"], 2: MAT["wood"], -1: MAT["metal"]}
    d = math.hypot(20, 3)
    x = r[(2, 0)]       # storey 0 at s 0.02 to storey 1: walls on storey 0 up to x = 20 * 0.98 / 1.0
    check(abs(x["fdb"] - slab[0]) < 0.01 and abs(x["d"] - d) < 0.01, f"0-2 {x}")
    x = r[(3, 0)]
    check(abs(x["fdb"] - (slab[0] + slab[1] + slab[2])) < 0.01 and x["floors"] == 3, f"0-3 {x}")
    check(abs(r[(4, 0)]["fdb"] - slab[-1]) < 0.01, f"0-4 {r[(4, 0)]}")
    check(abs(x["pl"] - (fspl(math.hypot(20, 9)) + x["wdb"] + x["fdb"])) < 0.015, f"sum {x}")
    # The walls the 0-3 path meets: storey 0 while s < 1, s = 0.02 + 3 t
    wx = [w for w in (4, 8, 12, 16) if 0.02 + 3 * w / 20 < 1]
    check(r[(3, 0)]["walls"] == len(wx), f"0-3 walls {r[(3, 0)]} want {wx}")
    # COST 231 (the default): Lf k^((k+2)/(k+1) - b), Lf the mean slab loss, b 0.46;
    # here the light wood slab lowers the mean of the three, so the term is that of
    # the two heavy slabs below it (never decreasing, test_cost231_monotone)
    lf = (slab[0] + slab[1] + slab[2]) / 3
    out, r = build(pos, fp)
    want = max(lf * 3 ** (5 / 4 - 0.46), (slab[0] + slab[1]) / 2 * 2 ** (4 / 3 - 0.46))
    check(abs(r[(3, 0)]["fdb"] - want) < 0.01, f"cost231 default {r[(3, 0)]} want {want}")
    out, r = build(pos, fp, ["--prop-floor-model=cost231", "--prop-floor-b=0.5"])
    want = max(lf * 3 ** (5 / 4 - 0.5), (slab[0] + slab[1]) / 2 * 2 ** (4 / 3 - 0.5))
    check(abs(r[(3, 0)]["fdb"] - want) < 0.01, f"cost231 {r[(3, 0)]}")
    check(abs(r[(2, 0)]["fdb"] - slab[0]) < 0.01, "cost231, one floor: Lf")
    # table
    out, r = build(pos, fp, ["--prop-floor-model=table", "--prop-floor-db=7,11"])
    check(abs(r[(3, 0)]["fdb"] - 15) < 0.01 and abs(r[(2, 0)]["fdb"] - 7) < 0.01, f"table {r[(3, 0)]}")
    # An explicit model: its own PL0 and exponent, walls on top; the grid adds
    out, r = build(pos, fp, ["--prop-model=logdist", "--prop-walls=100:3", *SLABS])
    check(abs(r[(1, 0)]["pl"] - (fspl(1) + 30 * math.log10(20) + walls)) < 0.01, f"logdist {r[(1, 0)]}")
    out, r = build(pos, fp, ["--prop-walls=8:3", *SLABS])
    check(abs(r[(1, 0)]["pl"] - (fspl(20) + walls + 3 * 3)) < 0.01 and r[(1, 0)]["walls"] == 4 + 3, f"grid {r[(1, 0)]}")
    check("multi-wall (floor plan)" in build(pos, fp)[0], "model name")
    # floorheight 3 places a node without FLOOR at Z 3 on floor 1
    out, r = build("0 0 0 0\n1 0 0 3\n", "floorheight 3\n")
    check(r[(1, 0)]["floors"] == 1, f"floorheight {r[(1, 0)]}")
    out, r = build("0 0 0 0\n1 0 0 3\n", "")
    check(r[(1, 0)]["floors"] == 0 and "floor height 3.50 m" in out, "default floor height 3.5: Z 3 on storey 0")


def test_storey_from_z():
    """With a floor plan, a node without FLOOR is on the storey whose height
    range holds its Z (floor(Z / H)), not the rounded one: ceiling-mounted
    detectors at F·H + 2.6 m stay on F; a Z that is a multiple of H by
    floating point (9.6 / 3.2) is on that storey."""
    pos = "0 0 0 2.6\n1 10 0 5.8\n2 10 0 9.6\n3 0 0 12.79\n4 5 0 1.0\n"
    out, r = build(pos, "floorheight 3.2\nwall 0 2 -1 2 1 glass\nwall 1 2 -1 2 1 brick\n", SLABS)
    fl = {0: 0, 1: 1, 2: 3, 3: 3, 4: 0}
    for (rx, tx), x in r.items():
        check(x["floors"] == abs(fl[rx] - fl[tx]), f"{rx}<-{tx}: {x} storeys {fl}")
    # 0 (storey 0, u 0.81) to 1 (storey 1, u 0.81): s = 0.81 + t; at x = 2 (t 0.2) storey 1: brick
    check(r[(1, 0)]["crossed"] == "3", f"0-1: {r[(1, 0)]}")
    # Without a floor plan, rounding as before: Z 2.6 with H 3.2 is floor 1
    out, r = build(pos, None, ["--prop-floor-height=3.2"])
    check(r[(4, 0)]["floors"] == 1, f"no floor plan: {r[(4, 0)]}")


def test_cost231_monotone():
    """The cost231 term never decreases with more floors, even when a light
    slab lowers the mean loss: at least the term of each shorter run."""
    pos = "".join(f"{f} 0 0 {f * 3 + 1} {f}\n" for f in range(6))
    fp = "floorheight 3\nmaterial light 0.5\nslab 0 metal\nslab 1 light\nslab 2 light\nslab 3 metal\nslab 4 light\n"
    out, r = build(pos, fp)
    slab = [MAT["metal"], 0.5, 0.5, MAT["metal"], 0.5]

    def raw(lo, k):
        return sum(slab[lo:lo + k]) / k * k ** ((k + 2) / (k + 1) - 0.46)

    def mono(lo, k):
        return raw(lo, k) if k == 1 else max(raw(lo, k), mono(lo, k - 1), mono(lo + 1, k - 1))

    for a in range(6):
        for b in range(a + 1, 6):
            got = r[(b, a)]["fdb"]
            check(abs(got - mono(a, b - a)) < 0.01, f"{a}-{b}: {got} want {mono(a, b - a):.2f}")
            if b > a + 1:
                check(got >= r[(b - 1, a)]["fdb"] - 1e-9, f"decreasing {a}-{b}")
    check(raw(0, 2) < MAT["metal"] and abs(r[(1, 0)]["fdb"] - MAT["metal"]) < 0.01 and
          abs(r[(2, 0)]["fdb"] - MAT["metal"]) < 0.01, f"clamped: {r[(2, 0)]}")


def test_interferer():
    """pos= (§13.1) uses the floor plan: the level at a node behind two walls
    and one slab, without shadowing."""
    pos = write("i.pos", "1 0 0 0 0\n2 10 0 3.5 1\n")
    fp = write("i.fp", "material m 6\nwall 0 3 -5 3 5 m\nwall 1 6 -5 6 5 m\nwall 0 6 -5 6 5 glass\n")
    spec = "--interferer=chan=0,pos=-5/0/3,eirp=10"
    r = {n: Radio(n, {1000: [("sense", 0)]}) for n in (1, 2)}
    medium_run(r, [], opts=[f"--positions={pos}", f"--prop-floorplan={fp}", "--prop-shadow=0", spec])
    # The interferer at z = 3 m is on storey 0 (floor(3 / 3.5)), u = 0.857. Node 1:
    # no wall between x = -5 and 0; node 2 (storey 1): s from 0.857 to 1.02, so x = 3
    # (t 8/15, s 0.94) and x = 6 (t 11/15, s 0.98: glass) are on storey 0; one slab
    want1 = 10 - fspl(math.hypot(5, 3))
    want2 = 10 - (fspl(math.hypot(15, 0.5)) + 6 + MAT["glass"] + MAT["concrete-slab"])
    check(abs(r[1].sensed[0][2] - energy(want1)) < 0.01, f"node 1 {r[1].sensed} want {energy(want1)}")
    check(abs(r[2].sensed[0][2] - energy(want2)) < 0.01, f"node 2 {r[2].sensed} want {energy(want2)}")
    print(f"     node 1 {want1:.2f} dBm, node 2 {want2:.2f} dBm")


def random_building(nodes, floors, seed, aligned=False):
    """A floor plan of rooms, corridors and random walls; with aligned, nodes
    and wall ends on a 1 m grid (many exact contacts)."""
    rnd = random.Random(seed)
    W, H = 60, 30
    fp = ["floorheight 3.5", "material part 1.4 #cccccc"]
    for f in range(floors):
        fp.append(f"outline {f} 0 0 {W} 0 {W} {H} 0 {H} concrete")
        for x in range(6, W, 6):
            fp.append(f"wall {f} {x} 0 {x} 12 brick")
            fp.append(f"wall {f} {x} 18 {x} {H} part")
        fp.append(f"wall {f} 0 12 {W} 12 plasterboard")
        fp.append(f"wall {f} 0 18 {W} 18 plasterboard")
        for _ in range(20):
            if aligned:
                x1, y1 = rnd.randint(0, W), rnd.randint(0, H)
                x2, y2 = x1 + rnd.randint(-6, 6), y1 + rnd.randint(-6, 6)
                if (x1, y1) == (x2, y2):
                    continue
            else:
                x1, y1 = rnd.uniform(0, W), rnd.uniform(0, H)
                a, l = rnd.uniform(0, math.pi), rnd.uniform(1, 10)
                x2, y2 = x1 + l * math.cos(a), y1 + l * math.sin(a)
            fp.append(f"wall {f} {x1:.3f} {y1:.3f} {x2:.3f} {y2:.3f} {rnd.choice(['glass', 'wood', 'metal'])}")
    pos = []
    for n in range(nodes):
        f = n % floors
        if aligned:
            x, y = rnd.randint(0, W), rnd.randint(0, H)
        else:
            x, y = rnd.uniform(0, W), rnd.uniform(0, H)
        pos.append(f"{n} {x} {y} {f * 3.5 + 1.5:.2f} {f}")
    return "\n".join(pos) + "\n", "\n".join(fp) + "\n"


def test_grid_vs_every_wall():
    """The grid finds the same crossings as testing every wall
    (WSSIM_FLOORPLAN_BRUTE=1), in random and grid-aligned buildings; the
    dump is the same for the same seed (determinism)."""
    for aligned in (False, True):
        pos, fp = random_building(120, 3, 7, aligned)
        a = build(pos, fp)[1]
        b = build(pos, fp, env={"WSSIM_FLOORPLAN_BRUTE": "1"})[1]
        c = build(pos, fp)[1]
        check(a == b, f"grid and every wall differ (aligned {aligned}): "
                      f"{[(k, a[k], b[k]) for k in a if a[k] != b.get(k)][:3]}")
        check(a == c, "not deterministic")
        halves = sum(":" in v["crossed"] for v in a.values())
        print(f"     aligned {aligned}: {len(a)} directions, mean {sum(v['wn'] for v in a.values()) / len(a):.1f} "
              f"walls, {halves} with a node on a wall (a weight below 1)")
    # Shadowing on top: the seed's draws, unchanged by the floor plan
    pos, fp = random_building(30, 2, 3)
    s1 = build(pos, fp, ["--prop-shadow=4"])[1]
    s2 = build(pos, fp, ["--prop-shadow=4"], seed="2")[1]
    check(s1 != s2 and all(s1[k]["pl"] == s2[k]["pl"] for k in s1), "shadowing changes pl_db or not the level")


def _orient(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _proper(p, q, a, b):
    """Segments p-q and a-b cross at one interior point of both (general
    position: no endpoint on the other segment's line)."""
    return _orient(p, q, a) * _orient(p, q, b) < 0 and _orient(a, b, p) * _orient(a, b, q) < 0


def _dist_seg(p, a, b):
    ax, ay = b[0] - a[0], b[1] - a[1]
    t = max(0, min(1, ((p[0] - a[0]) * ax + (p[1] - a[1]) * ay) / (ax * ax + ay * ay)))
    return math.hypot(p[0] - a[0] - t * ax, p[1] - a[1] - t * ay)


def test_oracle():
    """An independent oracle on random plans snapped to a 1 m grid (walls of
    random materials, many shared vertices, no overlapping collinear walls),
    nodes at half-metre offsets (never on a wall), one storey. The path is
    moved 1 mm to its left and to its right; each moved path crosses walls in
    general position, counted by orientation tests. A path through one vertex
    where walls meet must have the loss of the lighter of the two moved paths;
    with several such vertices, at most that loss (the server chooses at each
    vertex)."""
    rnd = random.Random(11)
    mats = {"glass": MAT["glass"], "wood": MAT["wood"], "brick": MAT["brick"], "concrete": MAT["concrete"]}
    n_eq = n_le = n_vert = 0
    for plan in range(4):
        walls = []
        while len(walls) < 40:
            a = (rnd.randint(0, 12), rnd.randint(0, 12))
            if walls and rnd.random() < 0.6:        # start from an existing vertex
                a = rnd.choice(walls)[rnd.randint(0, 1)]
            b = (a[0] + rnd.randint(-4, 4), a[1] + rnd.randint(-4, 4))
            if a == b:
                continue
            # no overlap with a collinear wall
            bad = False
            for (c, e, _) in walls:
                if _orient(a, b, c) == 0 and _orient(a, b, e) == 0:
                    ux, uy = b[0] - a[0], b[1] - a[1]
                    s1 = (c[0] - a[0]) * ux + (c[1] - a[1]) * uy
                    s2 = (e[0] - a[0]) * ux + (e[1] - a[1]) * uy
                    if min(ux * ux + uy * uy, max(s1, s2)) - max(0, min(s1, s2)) > 0:
                        bad = True
            if not bad:
                walls.append((a, b, rnd.choice(list(mats))))
        nodes = []
        while len(nodes) < 30:
            p = (rnd.randint(0, 12) + 0.5, rnd.randint(0, 12) + 0.5)
            if p not in nodes and all(_dist_seg(p, a, b) > 0.01 for a, b, _ in walls):
                nodes.append(p)
        fp = "".join(f"wall 0 {a[0]} {a[1]} {b[0]} {b[1]} {m}\n" for a, b, m in walls)
        pos = "".join(f"{i} {p[0]} {p[1]} 1 0\n" for i, p in enumerate(nodes))
        r = build(pos, fp)[1]
        for i in range(len(nodes)):
            for j in range(i + 1, len(nodes)):
                p, q = nodes[i], nodes[j]
                dx, dy = q[0] - p[0], q[1] - p[1]
                ln = math.hypot(dx, dy)
                nx, ny = -dy / ln * 1e-3, dx / ln * 1e-3
                side = []
                for sg in (1, -1):
                    pp, qq = (p[0] + sg * nx, p[1] + sg * ny), (q[0] + sg * nx, q[1] + sg * ny)
                    side.append(sum(mats[m] for a, b, m in walls if _proper(pp, qq, a, b)))
                # wall ends on the path's interior, by vertex
                verts = {v for a, b, _ in walls for v in (a, b)
                         if _orient(p, q, v) == 0 and 0 < (v[0] - p[0]) * dx + (v[1] - p[1]) * dy < ln * ln}
                got = r[(j, i)]["wdb"]
                if len(verts) <= 1:
                    check(abs(got - min(side)) < 1e-6, f"plan {plan} {p}-{q}: {got} != {min(side)} ({side}), "
                                                       f"{r[(j, i)]['crossed']}, vertices {verts}")
                    n_eq += 1
                    n_vert += len(verts)
                else:
                    check(got <= min(side) + 1e-6, f"plan {plan} {p}-{q}: {got} > {min(side)}")
                    n_le += 1
    print(f"     {n_eq} paths equal to the oracle ({n_vert} through one vertex), {n_le} through several vertices "
          f"at most the oracle")


def test_performance():
    """1000 nodes on 10 storeys with about 1800 walls (the building of
    MEDIUM.md §11.11): the model's time from the server's log."""
    for nodes, floors, limit in ((1000, 10, 5.0), (4000, 20, 60.0)):
        rnd = random.Random(1)
        W, H = 100, 50
        fp = ["floorheight 3.5"]
        for f in range(floors):
            for k in range(6):
                for i in range(20):
                    fp.append(f"wall {f} {i * 5} {k * 10} {i * 5 + 5} {k * 10} "
                              f"{'concrete' if k in (0, 5) else 'plasterboard'}")
            for j in range(11):
                for i in range(5):
                    fp.append(f"wall {f} {j * 10} {i * 10} {j * 10} {i * 10 + 10} brick")
        pos = "".join(f"{n} {rnd.uniform(0, W):.3f} {rnd.uniform(0, H):.3f} {(n * floors // nodes) * 3.5 + 1.2:.2f} "
                      f"{n * floors // nodes}\n" for n in range(nodes))
        p = write("perf.pos", pos)
        f = write("perf.fp", "\n".join(fp) + "\n")
        sock = ARGS.sock + ".perf"
        if os.path.exists(sock):
            os.unlink(sock)
        with open(os.path.join(ARGS.logdir, "perf.log"), "w") as lf:
            srv = subprocess.Popen([ARGS.bin, "--medium-preset=basic", "--vtime=1", f"--positions={p}",
                                    f"--prop-floorplan={f}", sock], stdout=lf, stderr=subprocess.STDOUT)
            t0 = time.monotonic()
            while not os.path.exists(sock) and srv.poll() is None and time.monotonic() - t0 < 120:
                time.sleep(0.05)
            srv.terminate()
            srv.wait()
        with open(os.path.join(ARGS.logdir, "perf.log")) as lf:
            out = lf.read()
        m = re.search(r"([0-9.]+) s$", [ln for ln in out.splitlines() if "pairs in the graph" in ln][0])
        t = float(m.group(1))
        check(t < limit, f"{nodes} nodes: {t} s")
        print(f"     {nodes} nodes, {floors} storeys, {len(fp) - 1} walls: {t:.2f} s")


def main():
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("server", help="wssimserver binary (copied before use)")
    ap.add_argument("--sock", default="/tmp/fp_sock")
    ap.add_argument("--logdir", default=None)
    ap.add_argument("-k", default="", help="run only tests whose name contains this")
    ARGS = ap.parse_args()
    if ARGS.sock == "/tmp/sim_socket":
        sys.exit("refusing the default socket: it may belong to a real run")
    ARGS.logdir = ARGS.logdir or tempfile.mkdtemp(prefix="fp_")
    os.makedirs(ARGS.logdir, exist_ok=True)
    ARGS.copy = os.path.join(ARGS.logdir, "wssimserver-fp")
    shutil.copy2(ARGS.server, ARGS.copy)
    ARGS.bin = ARGS.copy
    tc.ARGS = ARGS
    d = json.loads(subprocess.run([ARGS.bin, "--describe=json"], capture_output=True, text=True, timeout=10).stdout)
    MAT.update({m["name"]: m["loss_db"] for m in d["materials"]})
    tests = [
        ("describe", test_describe),
        ("parser", test_parser),
        ("geometry", test_geometry),
        ("storeys", test_storeys),
        ("losses", test_losses),
        ("storey_from_z", test_storey_from_z),
        ("cost231_monotone", test_cost231_monotone),
        ("interferer", test_interferer),
        ("grid_vs_every_wall", test_grid_vs_every_wall),
        ("oracle", test_oracle),
        ("performance", test_performance),
    ]
    failed = 0
    for name, fn in tests:
        if ARGS.k not in name:
            continue
        t0 = time.monotonic()
        try:
            fn()
            print(f"PASS {name} ({time.monotonic() - t0:.1f} s)")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {type(e).__name__}: {e}")
    print(f"logs in {ARGS.logdir}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
