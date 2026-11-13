#!/bin/bash
# Live test of the scenario engine (docs/SCENARIOS.md) on dag_simple: every
# action kind and the main trigger and end kinds in one short run, then checks
# of scenario_result.json and report.json. About 15 s of wall time.
#
# usage: test_scenario.sh <bindir> <workdir(holds examples/)> <outdir>
# Environment: WSSIM_SEED (1), KEEP=1 keeps the run directory.
set -u
[ $# = 3 ] || { echo "usage: test_scenario.sh <bindir> <workdir> <outdir>" >&2; exit 2; }
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$3"; OUT=$(realpath "$3")
cat > "$OUT/test.json" <<'EOF'
{
  "format": "wssim-scenario 1",
  "name": "engine test",
  "settings": {"poll_s": 5},
  "phases": [
    {"use": "formed", "fraction": 0.9, "stable_s": 20, "timeout_s": 900},
    {"name": "probe", "load": {"flows": ["up", "down"], "rate_pps": 1},
     "measure": {"sets": ["traffic", "queues", "mesh", "power", "medium"], "settle_s": 10}, "end": {"after_s": 70}},
    {"name": "steps", "load": {"flows": ["up"], "rate_pps": 0.5},
     "repeat": {"max": 3, "mul": 2, "window_s": 30, "settle_s": 5,
                "until": {"is": "metric", "metric": "rtt.p95_ms", "op": ">=", "relative_to": {"phase": "probe", "factor": 50}}}},
    {"name": "jam", "actions": [{"do": "interferer_add", "spec": "chan=0-34,level=-60,kind=noise,pattern=ge:30ms:70ms", "name": "j1", "for_s": 30},
                                {"do": "mark", "text": "jammed"}],
     "end": {"after_s": 40}},
    {"name": "leave", "actions": [{"do": "leave", "nodes": {"select": "hop", "hop": 1, "pick": "most_children"}, "for_s": 60, "label": "left"}],
     "measure": {"sets": ["traffic", "mesh"]}, "end": {"is": "repaired", "max_s": 300}},
    {"name": "back", "start": {"is": "formed", "milestone": "operational", "nodes": {"select": "label", "label": "left"}, "timeout_s": 600},
     "end": {"after_s": 10}},
    {"name": "partition", "actions": [{"do": "partition", "a": {"select": "id", "id": 0}, "b": {"select": "ids", "ids": [1, 7]}, "for_s": 40}],
     "measure": {"sets": ["mesh"]}, "end": {"after_s": 50}},
    {"name": "degrade", "actions": [{"do": "degrade_links", "nodes": {"select": "id", "id": 9}, "rssi_dbm": -95, "for_s": 30}],
     "end": {"after_s": 35}},
    {"name": "fail", "actions": [{"do": "kill", "nodes": {"select": "ancestor", "of": 6, "hop": 1}, "label": "dead"}],
     "measure": {"sets": ["traffic", "mesh"]}, "end": {"is": "repaired", "max_s": 400}},
    {"name": "reboot", "start": {"is": "elapsed", "s": 10}, "actions": [{"do": "restart", "nodes": {"select": "label", "label": "dead"}}],
     "end": {"is": "node_event", "event": "joined", "nodes": {"select": "label", "label": "dead"}, "max_s": 600}},
    {"name": "br", "actions": [{"do": "restart", "nodes": {"select": "id", "id": 0}}], "load": "off", "end": {"after_s": 5}},
    {"name": "reformed", "start": {"is": "formed", "fraction": 0.5, "timeout_s": 900, "on_timeout": "start"}, "end": {"after_s": 0}}
  ]
}
EOF
python3 "$HERE/../scenario.py" --check "$OUT/test.json" > /dev/null || { python3 "$HERE/../scenario.py" --check "$OUT/test.json"; exit 1; }
rm -rf "$OUT/run"
T0=$SECONDS
WSSIM_SEED=${WSSIM_SEED:-1} TUN=none POWER="$(realpath "$2")/examples/power/efr32fg25-soc.ini" SCENARIO="$OUT/test.json" \
    "$HERE/run.sh" "$1" "$2" "$OUT/run" 6000 > "$OUT/run.out" 2>&1
RC=$?
echo "test_scenario: run status $RC, $((SECONDS - T0)) s wall time"
python3 - "$OUT/run" <<'EOF'
import json, os, sys
d = sys.argv[1]
r = json.load(open(os.path.join(d, 'scenario_result.json')))
rep = json.load(open(os.path.join(d, 'report.json')))
ph = {p['name']: p for p in r['phases']}
fails = []
def check(cond, what):
    print(('ok   ' if cond else 'FAIL ') + what)
    if not cond:
        fails.append(what)
check(r['status'] == 'completed', f'scenario completed ({r["status"]})')
check(ph['formed']['start']['met'], 'formed: start condition met')
f = ph['probe']['metrics']['flows']
check(f['up']['sent'] > 40 and f['up']['pdr'] > 0.9, f'probe: up flow delivered ({f["up"]["sent"]} sent, PDR {f["up"]["pdr"]})')
check('down' in f and f['down']['echoed'] > 0, 'probe: down flow echoed')
check(len(f['up']['rtt_ms_by_hops']) >= 2, 'probe: RTT for 2 hop counts or more')
check(ph['probe']['metrics'].get('power', {}).get('avg_ma'), 'probe: power per phase')
check(ph['probe']['metrics'].get('medium', {}).get('frames'), 'probe: medium counters per phase')
check(ph['probe']['metrics'].get('queues', {}).get('per_node'), 'probe: queues per node')
its = ph['steps']['iterations']
check(len(its) >= 2 and its[1]['rate_pps'] > its[0]['rate_pps'], f'steps: {len(its)} iterations, rate rising')
check(ph['steps'].get('capacity', {}).get('capacity_pps'), 'steps: a capacity')
check(any('interferer added' in e['text'] for e in r['events']) and any('interferer j1 removed' in e['text'] for e in r['events']),
      'jam: interferer added and removed after for_s')
lv = ph['leave']['disruptions'][0]
check(lv['nodes_affected'] + len(lv['isolated']) >= 1 and lv.get('repaired'), f'leave: {lv["nodes_affected"]} cut-off routers repaired in {lv.get("repair_t_s")} s')
check(ph['back']['start']['met'], f'back: the router that left is in the network again ({ph["back"]["start"]["detail"]})')
check(any('partition undone' in e['text'] for e in r['events']), 'partition: links back after for_s')
check(any('degrade_links undone' in e['text'] for e in r['events']), 'degrade: links back after for_s')
fl = ph['fail']['disruptions'][0]
check(fl['failed'] and fl['detect_t_s'] is not None and fl['nodes_affected'] >= 1,
      f'fail: node {fl["failed"]} killed, {fl["nodes_affected"]} cut off, detected after {fl["detect_t_s"]} s, '
      f'repaired: {fl.get("repaired")} ({fl.get("repair_t_s")} s)')
check(ph['reboot']['end']['met'], 'reboot: the killed node joined again after its restart')
check(os.path.exists(os.path.join(d, 'br.r1.log')), 'br: border router restarted (br.r1.log)')
check(ph['reformed']['start']['met'], f'reformed: network formed again after the border router restart ({ph["reformed"]["start"]["detail"]})')
check(rep['format'] == 'wssim-report 1' and rep['scenario']['status'] == 'completed', 'report.json with the scenario')
check(rep['power'] and rep['power']['totals']['routers_mean_ma'] > 0, 'report: power')
print('test_scenario:', 'FAILED (%d)' % len(fails) if fails else 'PASSED')
sys.exit(1 if fails else 0)
EOF
OK=$?
[ "${KEEP:-0}" = 1 ] || rm -rf "$OUT/run"

# Failures must fail the run: a node command a node refuses, an error inside
# the driver, the driver killed; and a scenario the validator must refuse.
check_fail() {  # check_fail <name> <status> <expected status prefix>
    local d="$OUT/fail_$1" st
    st=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['status'])" "$d/scenario_result.json" 2>/dev/null)
    if [ "$2" != 0 ] && [[ $st == $3* ]]; then echo "ok   $1: the run failed (status $2), scenario status: $st"
    else echo "FAIL $1: run status $2, scenario status: ${st:-none} ($d.out)"; OK=1; fi
    [ "${KEEP:-0}" = 1 ] || rm -rf "$d" "$d.json" "$d.out"
}
for c in badcmd crash intf; do
    case $c in
    intf) ph='{"name": "x", "actions": [{"do": "interferer_add", "spec": "chan=1,level=-70,hop=on"}], "end": {"after_s": 5}}' ;;
    badcmd) ph='{"name": "x", "x_test_command": "load set payload=5", "end": {"after_s": 5}}' ;;
    crash) ph='{"name": "x", "x_test_crash": true, "end": {"after_s": 5}}' ;;
    esac
    d="$OUT/fail_$c"; rm -rf "$d"
    printf '{"format": "wssim-scenario 1", "phases": [{"name": "w", "start": {"is": "time", "t_s": 60}, "end": {"after_s": 0}}, %s]}\n' "$ph" > "$d.json"
    WSSIM_SEED=1 TUN=none SCENARIO="$d.json" "$HERE/run.sh" "$1" "$2" "$d" 400 > "$d.out" 2>&1
    check_fail $c $? error
done
# the driver killed: the conductor sees its connection closed without "quit"
d="$OUT/fail_kill"; rm -rf "$d"
printf '{"format": "wssim-scenario 1", "phases": [{"name": "w", "start": {"is": "time", "t_s": 300}, "end": {"after_s": 0}}]}\n' > "$d.json"
WSSIM_SEED=1 TUN=none SCENARIO="$d.json" "$HERE/run.sh" "$1" "$2" "$d" 400 > "$d.out" 2>&1 &
RUN=$!
for (( i = 0; i < 200; i++ )); do
    P=$(pgrep -f "scenario.py drive .*--run $d " | head -1)
    [ -n "$P" ] && grep -q "scenario: driver connected" "$d/simserver.log" 2>/dev/null && break
    sleep 0.05
done
[ -n "$P" ] && kill -9 "$P"
wait $RUN; RC=$?
if [ "$RC" != 0 ] && grep -q "driver lost" "$d/simserver.log"; then echo "ok   kill: the run failed (status $RC), simserver.log: scenario driver lost"
else echo "FAIL kill: run status $RC ($d.out)"; OK=1; fi
[ "${KEEP:-0}" = 1 ] || rm -rf "$d" "$d.json" "$d.out"
# refused by the validator before any run (the review's reproducer)
printf '{"format": "wssim-scenario 1", "phases": [{"use": "probe"}, {"use": "capacity"}, {"use": "node_failure", "load_fraction": 0}]}\n' > "$OUT/bad.json"
if python3 "$HERE/../scenario.py" --check "$OUT/bad.json" | grep -q '"/phases/2/load_fraction"'; then echo "ok   validator: load_fraction 0 refused"
else echo "FAIL validator: load_fraction 0 accepted"; OK=1; fi
rm -f "$OUT/bad.json"
echo "test_scenario: $([ $OK = 0 ] && echo PASSED || echo FAILED) ($((SECONDS - T0)) s wall time)"
exit $OK
