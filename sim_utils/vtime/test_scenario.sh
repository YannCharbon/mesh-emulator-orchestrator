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

# emu-probe: "Switch nodes off" (off_then_on, ranked top_fraction and top; pick
# above with no node: skipped, and the step that would switch them back on),
# a worst-router retries condition, the loss metric, the report's series and
# plots, and no <seconds>: the scenario's own bound
cat > "$OUT/probe.json" <<'EOF2'
{"format": "wssim-scenario 1", "name": "probe test", "settings": {"poll_s": 5},
 "phases": [
  {"use": "formed", "fraction": 0.9, "stable_s": 20, "timeout_s": 900},
  {"name": "load", "load": {"flows": ["up"], "rate_pps": 1}, "measure": {"sets": ["traffic", "queues", "mesh", "power", "medium"]},
   "end": {"is": "metric", "metric": "mac.retries", "over": "worst_node", "value": 1, "window_s": 30, "max_s": 120}},
  {"name": "lossy", "end": {"is": "metric", "metric": "loss", "op": "<=", "value": 0.5, "window_s": 30, "min_samples": 10, "max_s": 120}},
  {"use": "node_outage", "name": "out", "mode": "off_then_on", "off_s": 20, "max_s": 300, "capacity_phase": "",
   "nodes": {"select": "all", "rank_by": "mac.retries", "pick": "top_fraction", "fraction": 0.1, "rank_window_s": 60}},
  {"use": "node_outage", "name": "none", "capacity_phase": "", "nodes": {"select": "all", "rank_by": "loss", "pick": "above", "value": 2}},
  {"use": "node_outage", "name": "back", "mode": "on", "of_step": "none", "capacity_phase": ""},
  {"use": "node_outage", "name": "q", "capacity_phase": "", "max_s": 60, "nodes": {"select": "hop", "hop": 1, "rank_by": "queue.mean", "count": 1}},
  {"name": "handwritten", "actions": [{"do": "leave", "nodes": {"select": "hop", "hop": 1, "pick": "most_children"}, "label": "x", "for_s": 30},
                                      {"do": "degrade_links", "nodes": {"select": "label", "label": "x"}, "rssi_dbm": -95, "for_s": 30}],
   "end": {"after_s": 35}},
  {"name": "kills", "actions": [{"do": "kill", "nodes": {"select": "all", "pick": "first"}},
                                {"do": "kill", "nodes": {"select": "all", "pick": "first"}}], "end": {"after_s": 10}},
  {"name": "tail", "end": {"after_s": 40}}
 ],
 "report": {"window_s": 10},
 "plots": [{"x": "offered_pps", "y": "rtt.p95_ms", "points": "steps"}, {"x": "t_mid_s", "y": ["power.min_ma", "power.mean_ma", "power.max_ma"]}]
}
EOF2
rm -rf "$OUT/probe"
T1=$SECONDS
WSSIM_SEED=${WSSIM_SEED:-1} TUN=none COMPACT=1 POWER="$(realpath "$2")/examples/power/efr32fg25-soc.ini" SCENARIO="$OUT/probe.json" \
    "$HERE/run.sh" "$1" "$2" "$OUT/probe" > "$OUT/probe.out" 2>&1
echo "test_scenario: probe run status $?, $((SECONDS - T1)) s wall time"
python3 - "$OUT/probe" "$HERE/../scenario.py" "$OUT/probe.json" <<'EOF2' || OK=1
import json, os, subprocess, sys
d = sys.argv[1]
r = json.load(open(os.path.join(d, 'scenario_result.json')))
rep = json.load(open(os.path.join(d, 'report.json')))
plan = json.load(open(os.path.join(d, 'scenario_plan.json')))
ph = {p['name']: p for p in r['phases']}
st = {s['name']: s for s in r['user_steps']}
fails = []
def check(cond, what):
    print(('ok   ' if cond else 'FAIL ') + what)
    if not cond:
        fails.append(what)
check(r['status'] == 'completed', f'probe: scenario completed ({r["status"]})')
info = open(os.path.join(d, 'run_info.txt')).read()
check(f'vtime_end={plan["run_s"]} ' in info, f'probe: no <seconds>: the run\'s end is the plan\'s run_s ({plan["run_s"]})')
check(ph['load']['end']['met'] and 'worst router' in ph['load']['end']['detail'],
      f'probe: worst-router retries condition ({ph["load"]["end"]["detail"]})')
check(ph['lossy']['end']['met'] and ph['lossy']['end']['detail'].startswith('loss'), f'probe: loss condition ({ph["lossy"]["end"]["detail"]})')
a = ph['out']['actions'][0]
check(a['do'] == 'kill' and a['rank_by'] == 'mac.retries' and len(a['chosen']) >= 1 and a['chosen'][0]['value'] is not None
      and a['rank_window'][1] - a['rank_window'][0] == 60, f'probe: top_fraction by retries chose {a["chosen"]}, window {a["rank_window"]}')
check(ph['out_on']['actions'][0]['nodes'] == a['nodes'] and ph['out_on']['end']['met'] and st['out']['state'] == 'completed',
      f'probe: off_then_on switched them back on ({ph["out_on"]["end"]["detail"]}), step {st["out"]["state"]}')
check(ph['out']['end']['t_s'] - ph['out']['start']['t_s'] >= 20, 'probe: off for at least off_s')
check(ph['none'].get('skipped') and 'no node selected' in ph['none']['skip_reason'] and st['none']['state'] == 'skipped'
      and st['back']['state'] == 'skipped', f'probe: pick above with none: skipped ({ph["none"].get("skip_reason")}), its "on" step too')
q = ph['q']['actions'][0]
check(len(q['chosen']) == 1 and q['rank_by'] == 'queue.mean', f'probe: top 1 by queue.mean ({q["chosen"]})')
check(all(p.get('step') is not None and p.get('step_name') and p.get('title') for p in r['phases']), 'probe: phases carry step, step_name, title')
log = open(os.path.join(d, 'scenario.log')).read()
check('step 3 out starts' in log and 'step 4 none skipped:' in log and 'step 3 out ends (completed)' in log, 'probe: step lines in scenario.log')
s = rep['series']
w = [x for x in s['windows'] if x['offered_pps']]
check(len(s['windows']) > 30 and w and all(x['power.mean_ma'] for x in w) and any(x['medium.collisions'] is not None for x in w)
      and any(x['mac.retries'] for x in w), f'report: series.windows ({len(s["windows"])} rows, power, retries, collisions)')
check(len(s['steps']) >= 4 and set(s['columns']) >= {'power.mean_ma', 'rtt.p95_ms'}, f'report: series.steps ({len(s["steps"])})')
check(len(rep['plots']) == 2 and rep['plots'][0]['kind'] == 'scatter' and rep['plots'][0]['series'][0]['points']
      and len(rep['plots'][1]['series']) == 3, 'report: two plots resolved')
t = {x['node']: x for x in rep['nodes']['table']}
check(all(x['collisions'] is not None and x['retries'] is not None for x in t.values()) and rep['network']['loss'] is not None
      and any(x['sent'] for x in t.values()), 'report: per-node retries, collisions, loss')
m = ph['load']['metrics']
check(m['worst']['mac.retries']['node'] != 0 and m['per_node'] and m['medium']['collisions'] is not None and m['queues']['retries'],
      'probe: phase metrics with per_node and worst')
hw = ph['handwritten']
check(not hw.get('skipped') and hw['actions'][0]['nodes'] and hw['actions'][1]['nodes'] == hw['actions'][0]['nodes'],
      f'probe: a hand-written phase uses the label of its own earlier action ({hw["actions"][0]["nodes"]})')
k = ph['kills']['actions']
check(k[0]['nodes'] and k[1]['nodes'] and k[0]['nodes'] != k[1]['nodes'],
      f'probe: a second kill does not pick the node the first killed ({k[0]["nodes"]}, {k[1]["nodes"]})')
t_k = ph['kills']['start']['t_s']
after = [x for x in s['windows'] if x['t_start_s'] >= t_k and x['power.min_ma'] is not None]
check(after and min(x['power.min_ma'] for x in after) > 5, 'probe: switched-off routers are not in the power series '
      f'(lowest router current after the kills: {min((x["power.min_ma"] for x in after), default=None)} mA)')
print('test_scenario probe:', 'FAILED (%d)' % len(fails) if fails else 'PASSED')
sys.exit(1 if fails else 0)
EOF2
[ "${KEEP:-0}" = 1 ] || rm -rf "$OUT/probe" "$OUT/probe.json" "$OUT/probe.out"

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
