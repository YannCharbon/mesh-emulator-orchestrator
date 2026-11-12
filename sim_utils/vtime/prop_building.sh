#!/bin/bash
# A FAN 1.0 network in a generated building, with the links computed by the
# server from the node positions (docs/MEDIUM.md §11): the demonstration of
# §11.9. A border router (node 0, middle of floor 0) and NODES-1 routers on
# FLOORS floors, SPACING m apart (radio_if/scale/gen_building.py), default
# propagation model, central medium. SEEDS runs at once, DUR virtual s each.
# Per run: the link levels (from --prop-dump), the medium's outcomes and the
# join milestones (medium_stats.py), and the RPL depth at the end (br.log).
#
# usage: prop_building.sh <bindir> <workdir(holds examples/)> <outdir>
# Environment: NODES (150), FLOORS (3), SPACING (10), SEEDS ("1 2 3"),
# DUR (1200), TX_DBM (20: the nodes' default tx_power, for the graph),
# PROP_OPTS (more wssimserver options, e.g. --prop-walls=8:2.4), TUN (none).
# Wall time: about 2 minutes for the defaults (3 runs at once, 24-core PC).
set -u
[ $# = 3 ] || { echo "usage: prop_building.sh <bindir> <workdir> <outdir>" >&2; exit 2; }
BIN=$(realpath "$1"); WORK=$(realpath "$2"); mkdir -p "$3"; OUT=$(realpath "$3")
HERE=$(cd "$(dirname "$0")" && pwd)
NODES=${NODES:-150}; FLOORS=${FLOORS:-3}; SPACING=${SPACING:-10}; SEEDS=${SEEDS:-1 2 3}
DUR=${DUR:-1200}; TX_DBM=${TX_DBM:-20}; TUN=${TUN:-none}
python3 "$HERE/../../radio_if/scale/gen_building.py" --nodes "$NODES" --floors "$FLOORS" --spacing "$SPACING" \
    -o /dev/null --positions "$OUT/building.pos" 2> /dev/null || exit 1
echo "0,$((NODES - 1))" > "$OUT/topo.txt"     # the node count for run.sh; the links come from the positions
T0=$SECONDS
for s in $SEEDS; do
    WSSIM_SEED=$s TUN=$TUN TOPO="$OUT/topo.txt" SOCK=/tmp/wssim-prop-$$-$s.sock \
    SIM_OPTS="--positions=$OUT/building.pos --prop-tx-dbm=$TX_DBM --prop-dump=$OUT/links$s.txt --medium-log=medium.log ${PROP_OPTS:-}" \
        "$HERE/run.sh" "$BIN" "$WORK" "$OUT/seed$s" "$DUR" > "$OUT/seed$s.run" 2>&1 &
done
wait
echo "prop_building: $(echo $SEEDS | wc -w) runs, $((SECONDS - T0)) s wall time"
for s in $SEEDS; do
    grep -h "pairs hear" "$OUT/seed$s/simserver.log" | sed "s/^/seed $s: /"
    awk -v s=$s '!/^#/ {print $9, $7}' "$OUT/links$s.txt" | sort -g | awk -v s=$s '
        {a[NR] = $1; sh += $2; sh2 += $2 * $2; if ($1 >= -80) g++}
        END {printf "seed %s: link levels at the nominal TX power (dBm): p5 %.1f, p25 %.1f, median %.1f, p75 %.1f, p95 %.1f; shadowing sd %.2f dB; %.1f %% at -80 dBm or more\n",
             s, a[int(NR * .05)], a[int(NR * .25)], a[int(NR * .5)], a[int(NR * .75)], a[int(NR * .95)], sqrt(sh2 / NR - (sh / NR) ^ 2), 100 * g / NR}'
    python3 - "$OUT/seed$s/br.log" "$s" <<'EOF'
import collections, re, sys
txt = open(sys.argv[1]).read()
snap, cur = {}, {}
for t, n, p, cnt in re.findall(r'brtree t=(\S+) (?:node=(\S+) parent=(\S+)|nodes=(\d+))', txt):
    if not n:
        snap, cur = cur, {}
        continue
    cur[int(n.replace(':', '')[-4:], 16)] = int(p.replace(':', '')[-4:], 16)
def depth(n):
    d = 0
    while n != 0 and d < 64:
        n, d = snap.get(n, 0), d + 1
    return d
c = collections.Counter(depth(n) for n in snap)
print(f"seed {sys.argv[2]}: RPL tree at the end: {len(snap)} routers, by depth {dict(sorted(c.items()))}")
EOF
done
python3 "$HERE/medium_stats.py" $(for s in $SEEDS; do echo "$OUT/seed$s"; done) | grep -E "collision share|delivery ok|routers ready|ready time|last router|brtree"
