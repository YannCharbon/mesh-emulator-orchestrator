#!/usr/bin/env python3
"""Self-test of sim_utils/report.py, sim_utils/runlog.py and the scenario
validator (sim_utils/scenario.py), on synthetic run directories whose figures
are known. No emulator program runs: about one second.

usage: test_report.py <scratch dir>   (default: $TMPDIR; files go to <dir>/test_report, removed at the end)
"""
import json, os, shutil, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
import report, runlog, scenario  # noqa: E402

T0 = 1700000000
FAILS = []


def check(cond, what):
    print(('ok   ' if cond else 'FAIL ') + what)
    if not cond:
        FAILS.append(what)


def line(t_us, text):
    return f'{T0 + t_us // 1000000}.{t_us % 1000000:06d} {text}\n'


def net1(node, t_us, parent, q_int_mac=0, q_int_al=0, hist=None, tx=0, bc_tx=0, red=0, give_up=0, final=False):
    j = {'v': 2, 'node': node, 't_us': t_us, 'final': final,
         'mac': {'tx': tx, 'rx': 0, 'bc_tx': bc_tx, 'bc_rx': 0, 'retry_attempts': 0,
                 'cca_att': tx, 'cca_fail': 0, 'rx_drop': 0, 'q': 0, 'q_max': 3,
                 'q_int_us': q_int_mac, 'lat_max_ms': 0,
                 'uc_retry_hist': hist or [tx - bc_tx, 0, 0, 0, 0, 0, 0, 0, 0],
                 'uc_give_up': {'no_ack': give_up, 'channel_access': 0, 'other': 0}, 'bc_done': bc_tx},
         'al': {'q': 0, 'q_max': 9, 'q_int_us': q_int_al, 'red_drop': red, 'lat_max_s': 0},
         'ip': {'tx': 0, 'tx_bytes': 0, 'no_route': 0, 'rx_drop': 0, 'frag_tx_err': 0, 'frag_rx_err': 0, 'buf_fail': 0},
         'rpl': {'parent': parent, 'rank': 256, 'dag_rank': 2, 'min_hop_inc': 128, 'parent_changes': 0,
                 'parent_tx_fail': 0, 'local_repair': 0, 'global_repair': 0, 'etx_parent': 128},
         'ws': {}, 'ctl': {'dis': 1, 'dio': 2, 'dao': 1}, 'app': {}}
    return line(t_us, 'NET1 ' + json.dumps(j))


def synthetic(d):
    """BR 0, router 1 (hop 1) and 2 (hop 2, child of 1). Load ramp of two
    steps of 100 s from 100 s: step 0 one packet per router every 10 s, step 1
    every 5 s. Router 1: RTT 100 ms; router 2: 300 ms, and in step 1 every
    second packet of router 2 lost (no echo). Router 1's adaptation queue: mean
    2 frames over [100, 300) s."""
    os.makedirs(d)
    open(os.path.join(d, 't0'), 'w').write(f'{T0}\n')
    open(os.path.join(d, 'topology.txt'), 'w').write('0,1:-50\n1,2:-60\n')
    open(os.path.join(d, 'run_info.txt'), 'w').write(
        'seed=7 nodes=3 if_nodes= vtime_end=320 sock=x tun=none setarch=0 conf=default\n'
        'app_opts= -o load_flows=up -o load_start_s=100 -o load_ramp_step_s=100 -o load_ramp_steps=2 '
        '-o stats=true -o stats_period_s=10 -o load_max_id=2\n')
    open(os.path.join(d, 'simserver.log'), 'w').write(
        'virtual time: end (end time reached)\n'
        'virtual time: reached 320.000000 s, 100 wakes, 50 frames sent, 90 frames delivered\n'
        'virtual time: wall 1.500 s, 0.004 wall s per simulated s\n'
        'medium:   ok             90\nmedium:   interference   10\nmedium:   not_listening  5\n')
    br, nodes = [], {1: [], 2: []}
    for n, par, ready in ((1, 0, 50), (2, 1, 60)):
        nodes[n].append(line(10_000_000, 'selected parent:01:02:03:04:05:06:00:00 panid 1'))
        nodes[n].append(line(ready * 1_000_000 - 5_000_000, '[INFO][wsbs]: operation start'))
        nodes[n].append(line(ready * 1_000_000, 'wsbr_tasklet(): get event for ws interface: ARM_NWK_BOOTSTRAP_READY'))
    seq = {1: 0, 2: 0}
    for step, gap in ((0, 10), (1, 5)):
        t = 100 + step * 100
        while t < 200 + step * 100:
            for n, rtt_ms, hops in ((1, 100, 1), (2, 300, 2)):
                seq[n] += 1
                tx = t * 1_000_000 + n * 1000
                nodes[n].append(line(tx, f'LOAD1 tx 1 {seq[n]} {n} 0 64 {step} {tx} 0'))
                rx = tx + rtt_ms * 500
                br.append(line(rx, f'LOAD1 rx 1 {seq[n]} {n} 0 64 {step} {tx} {rx} 64 {65 - hops}'))
                lost = n == 2 and step == 1 and seq[n] % 2 == 0
                if not lost:
                    back = tx + rtt_ms * 1000
                    nodes[n].append(line(back, f'LOAD1 echo 1 {seq[n]} {n} 0 64 {step} {tx} {rx} {back} 64 {65 - hops}'))
            t += gap
    for t in range(0, 330, 10):
        tu = t * 1_000_000
        # router 1: the adaptation queue holds 2 frames from 100 s on
        al = 2 * max(0, tu - 100_000_000)
        # router 1: half its frames delivered at the first try, half after one retry
        nodes[1].append(net1(1, tu, 0, q_int_al=al, tx=t, hist=[t // 2, t // 2, 0, 0, 0, 0, 0, 0, 0], give_up=t // 10))
        nodes[2].append(net1(2, tu, 1, tx=t // 2))
        br.append(net1(0, tu, None, tx=t))
        if t and t % 30 == 0:
            br.append(line(tu, f'brtree t={t}.000000 node=01:02:03:04:05:06:00:01 parent=01:02:03:04:05:06:00:00'))
            br.append(line(tu, f'brtree t={t}.000000 node=01:02:03:04:05:06:00:02 parent=01:02:03:04:05:06:00:01'))
            br.append(line(tu, f'brtree t={t}.000000 nodes=2'))
    def sort(ls):
        return sorted(ls, key=lambda s: float(s.split(' ', 1)[0]))
    open(os.path.join(d, 'br.log'), 'w').writelines(sort(br))
    for n in nodes:
        open(os.path.join(d, f'node{n}.log'), 'w').writelines(sort(nodes[n]))


def near(a, b, tol=1e-6):
    return a is not None and abs(a - b) <= tol * max(1, abs(b))


def test_report(scratch):
    d = os.path.join(scratch, 'synthetic')
    synthetic(d)
    rep = report.report(d, tail=10)
    json.dumps(rep)
    check(rep['format'] == 'wssim-report 1', 'format')
    check(rep['run']['seed'] == 7 and rep['run']['routers'] == 2, 'run parameters')
    topo = {n['node']: n for n in rep['topology']['nodes']}
    check(topo[2]['parent'] == 1 and topo[2]['hops'] == 2 and topo[1]['hops'] == 1, 'topology: parents and hops')
    check(topo[1]['subtree'] == 1 and topo[0]['children'] == 1, 'topology: subtree and children')
    check(topo[2]['rssi_to_parent_dbm'] == -60, 'topology: RSSI to the parent from topology.txt')
    check(rep['join']['ready'] == 2 and rep['join']['last_ready_s'] == 60.0, 'join: ready count and time')
    up = rep['traffic']['flows']['up']
    # step 0: 10 packets per router; step 1: 20 per router, 10 of router 2's lost
    check(up['sent'] == 60 and up['echoed'] == 50, f'traffic: 60 sent, 50 echoed ({up["sent"]}, {up["echoed"]})')
    check(near(up['pdr'], 50 / 60), 'traffic: PDR 50/60')
    h = up['rtt_ms_by_hops']
    check(near(h['1']['mean'], 100, 1e-3) and near(h['2']['mean'], 300, 1e-3), 'traffic: RTT 100 ms at 1 hop, 300 ms at 2')
    check(near(up['latency_ms']['max'], 150, 1e-3), 'traffic: one-way delay half the RTT')
    steps = rep['traffic']['steps']
    check(len(steps) == 2 and near(steps[0]['flows']['up']['pdr'], 1) and near(steps[1]['flows']['up']['pdr'], 0.75),
          'steps: PDR 1 then 0.75')
    check(near(steps[1]['flows']['up']['offered_pps'], 0.4), 'steps: offered 0.4 packets/s in step 1')
    sat = rep['traffic']['saturation']
    check(sat['saturated_step'] == 1 and sat['last_good_step'] == 0, 'saturation: step 1 (PDR below 0.99)')
    tab = {r['node']: r for r in rep['nodes']['table']}
    # [100, 320): the queue held 2 frames all the time
    check(near(tab[1]['al_q_mean'], 2, 1e-3), f'bottlenecks: router 1 mean adaptation queue 2 ({tab[1]["al_q_mean"]})')
    check(rep['nodes']['top_bottlenecks'][0]['node'] == 1, 'bottlenecks: router 1 first')
    check(near(tab[1]['retries_per_frame'], 0.5, 1e-3), 'bottlenecks: 0.5 retries per frame')
    check(tab[1]['give_up'] == 32 - 10 and rep['network']['give_up'] == 22,
          f'bottlenecks: frames given up from [100, 320) ({tab[1]["give_up"]})')
    check(rep['medium']['outcomes']['ok'] == 90 and near(rep['medium']['loss_share'], 0.1), 'medium totals')
    q = steps[1]['queues']
    check(q['mean_max_node'] == 1 and near(q['mean_max'], 2, 1e-3), 'steps: queue of router 1 in step 1')
    agg = report.aggregate([d, d])
    check(agg['flows']['up']['pdr']['median'] == round(50 / 60, 4), 'aggregate of two runs')


def test_runlog_incremental(scratch):
    d = os.path.join(scratch, 'inc')
    os.makedirs(d)
    open(os.path.join(d, 't0'), 'w').write(f'{T0}\n')
    p = os.path.join(d, 'node3.log')
    r = runlog.Run(d)
    with open(p, 'w') as f:
        f.write(line(1_000_000, 'LOAD1 tx 1 1 3 0 64 0 1000000 0\n').rstrip('\n'))   # no newline yet
    r.update()
    check(not r.tx, 'runlog: a partial line waits')
    with open(p, 'a') as f:
        f.write('\n' + line(2_000_000, 'LOAD1 tx 1 2 3 0 64 0 2000000 0'))
    r.update()
    check(len(r.tx) == 2, 'runlog: lines read once, completed later')
    open(os.path.join(d, 'node3.r1.log'), 'w').write(line(5_000_000, 'ns_port_sim: started at 5.000000 s of virtual time')
                                                       + line(9_000_000, 'operation start'))
    r.update()
    check(r.nodes[3].instances == 2 and r.nodes[3].restart_t == [5.0] and r.nodes[3].ms.get('operational') == 9.0,
          'runlog: a restarted instance')


def test_restart_counters(scratch):
    """A node restarted during the interval: its counters start again from 0 in the new instance (node4.r1.log). The
    change over [10, 60] s is the first instance's from 10 s to its last sample (40 s) plus the second's from 0 to 60 s:
    never negative."""
    d = os.path.join(scratch, 'restart')
    os.makedirs(d)
    open(os.path.join(d, 't0'), 'w').write(f'{T0}\n')
    open(os.path.join(d, 'node4.log'), 'w').write(
        net1(4, 10_000_000, 0, q_int_al=1_000_000, tx=100) + net1(4, 40_000_000, 0, q_int_al=31_000_000, tx=400, final=True))
    open(os.path.join(d, 'node4.r1.log'), 'w').write(
        line(45_000_000, 'ns_port_sim: started at 45.000000 s of virtual time')
        + net1(4, 50_000_000, 0, tx=0) + net1(4, 60_000_000, 0, q_int_al=10_000_000, tx=50))
    r = runlog.Run(d)
    r.update()
    nd = runlog.net_delta(r, 10, 60)[4]
    check(nd['mac_tx'] == 300 + 50, f'restart: frames of both instances, 350 ({nd["mac_tx"]})')
    check(nd['uc_frames'] == 350 and nd['instances'] == 2, f'restart: unicast frames 350 over 2 instances ({nd["uc_frames"]})')
    check(near(nd['al_q_mean'], (30 + 10) / 50, 1e-6), f'restart: mean queue over the interval 0.8 ({nd["al_q_mean"]})')
    nd = runlog.net_delta(r, 50, 60)[4]
    check(nd['mac_tx'] == 50 and nd['instances'] == 1, f'restart: an interval in the second instance only, 50 ({nd["mac_tx"]})')


def errors_of(doc):
    return scenario.check(doc)


def test_validator():
    ok = {'format': scenario.FORMAT, 'phases': [{'use': 'formed'}, {'use': 'probe'}, {'use': 'capacity'},
                                                {'use': 'interference', 'spec': 'chan=0-9,level=-80'}, {'use': 'recovery'}]}
    check(errors_of(ok) == [], 'validator: the owner\'s example in 5 blocks')
    e = errors_of({'format': scenario.FORMAT, 'phases': [{'use': 'probe'}, {'name': 'x', 'load': {'rate_pps': -1}}]})
    check(any(x['path'] == '/phases/1/load/rate_pps' for x in e), 'validator: path of a value below its minimum')
    e = errors_of({'format': scenario.FORMAT, 'phases': [{'name': 'a', 'end': {'is': 'metric', 'metric': 'rtt.p95_ms',
                                                                                'relative_to': {'phase': 'zz'}}}]})
    check(any('not an earlier phase' in x['message'] for x in e), 'validator: relative_to an unknown phase')
    e = errors_of({'format': scenario.FORMAT, 'phases': [{'name': 'a', 'actions': [{'do': 'restart',
                                                          'nodes': {'select': 'label', 'label': 'nope'}}]}]})
    check(any('label' in x['path'] for x in e), 'validator: a label used before it is defined')
    e = errors_of({'format': scenario.FORMAT, 'phases': [{'name': 'a', 'actions': [{'do': 'interferer_add',
                                                                                     'spec': 'chan=1,lvl=-3'}]}]})
    check(any('lvl' in x['message'] for x in e), 'validator: an unknown interferer key')
    e = errors_of({'format': scenario.FORMAT, 'phases': [{'use': 'nosuch'}]})
    check(any(x['path'] == '/phases/0/use' for x in e), 'validator: an unknown block')
    e = errors_of({'format': scenario.FORMAT, 'phases': [{'name': 'a'}, {'name': 'a'}]})
    check(any('twice' in x['message'] for x in e), 'validator: duplicate names')
    e = errors_of({'format': scenario.FORMAT, 'phases': [{'name': 'a', 'bogus': 1}]})
    check(any(x['path'] == '/phases/0/bogus' for x in e), 'validator: an unknown field')
    # interference with a reference at the same load: recovery measured against it, not the light-load probe
    ref = {'format': scenario.FORMAT, 'phases': [
        {'use': 'formed'}, {'use': 'probe'}, {'use': 'capacity'},
        {'use': 'interference', 'spec': 'chan=0-9,level=-80', 'load_fraction': 0.7, 'reference_s': 90},
        {'use': 'recovery', 'baseline': 'interference_reference'}]}
    check(errors_of(ref) == [], 'validator: interference with reference_s, recovery against its reference')
    ph = [p['phase'] for p in scenario.dry(ref)['phases']]
    check([p['name'] for p in ph][3:] == ['interference_reference', 'interference', 'recovery']
          and ph[3]['load'] == ph[4]['load'] and ph[4]['load']['rate_of']['factor'] == 0.7
          and not ph[3].get('actions') and ph[3]['end'] == {'after_s': 120}
          and ph[5]['end']['relative_to']['phase'] == 'interference_reference',
          'expand: the reference at the interference\'s load, without the interferer, then the interference')
    e = errors_of(dict(ref, phases=ref['phases'][:3] + [dict(ref['phases'][3], spec='chan=1,lvl=3')]))
    check(any(x['path'] == '/phases/3/spec' for x in e), 'validator: spec error of an interference with a reference')
    # node restart: its own load and an end in the border router's table
    rs = {'format': scenario.FORMAT, 'phases': [{'use': 'probe'}, {'use': 'capacity'}, {'use': 'node_failure'},
                                                {'use': 'node_restart', 'load_fraction': 0.3}]}
    check(errors_of(rs) == [], 'validator: node_restart with a load')
    q = scenario.dry(rs)['phases'][-1]['phase']
    check(q['load']['rate_of'] == {'phase': 'capacity', 'metric': 'capacity_pps', 'factor': 0.3}
          and q['end']['is'] == 'formed' and q['end']['milestone'] == 'ready'
          and q['end']['nodes'] == {'select': 'label', 'label': 'failed'},
          'expand: node_restart at 0.3 x the capacity, ends when ready and in the border router\'s table')
    e = errors_of(dict(rs, phases=rs['phases'][:3] + [{'use': 'node_restart', 'capacity_phase': 'zz'}]))
    check(any(x['path'] == '/phases/3/capacity_phase' for x in e), 'validator: node_restart from an unknown capacity')
    # a capacity search from a share of an earlier capacity, against its own new probe: the old capacity_check
    base = [{'use': 'probe'}, {'use': 'capacity'}]
    old = scenario.dry({'format': scenario.FORMAT, 'phases': base + [{'use': 'capacity_check', 'start_fraction': 0.4}]})
    new_doc = {'format': scenario.FORMAT, 'phases': base + [{'use': 'capacity', 'name': 'capacity_check',
               'start_from': 'capacity', 'start_fraction': 0.4, 'baseline': 'self', 'max_steps': 8}]}
    new = scenario.dry(new_doc)
    check(errors_of(new_doc) == [] and [p['phase'] for p in old['phases']] == [p['phase'] for p in new['phases']]
          and [p['title'] for p in old['phases']] == [p['title'] for p in new['phases']] and len(new['phases']) == 4,
          'expand: capacity with start_from and baseline self = the legacy capacity_check')
    e = errors_of(dict(new_doc, phases=base + [{'use': 'capacity', 'name': 'c2', 'start_from': 'zz'}]))
    check([x['path'] for x in e] == ['/phases/2/start_from'], f'validator: start_from an unknown phase ({e})')
    e = errors_of(dict(new_doc, phases=base + [{'use': 'capacity', 'name': 'c2', 'start_from': 'capacity',
                                                'start_rate_pps': 1}]))
    check(len(e) == 1 and 'not both' in e[0]['message'], 'validator: start_from and start_rate_pps refused together')
    sch_b = scenario.schema()['$defs']['block_capacity_check']
    check(sch_b.get('x-hidden') is True, 'schema: capacity_check hidden (legacy)')
    dr = scenario.dry(ok)
    check(len(dr['phases']) == 5 and dr['virtual_s_max'] > 0, f'dry: 5 phases, at most {dr["virtual_s_max"]} s')
    sch = scenario.schema()
    check(all('description' in v or '$ref' in v for v in sch['$defs']['condition']['properties'].values()),
          'schema: every condition field has a description')
    committed = json.load(open(os.path.join(HERE, '..', '..', 'docs', 'scenario.schema.json')))
    check(committed == json.loads(json.dumps(sch)),
          'schema: docs/scenario.schema.json is current (else: scenario.py --schema > docs/scenario.schema.json)')
    for f in sorted(os.listdir(os.path.join(HERE, '..', 'scenarios'))):
        doc = json.load(open(os.path.join(HERE, '..', 'scenarios', f)))
        check(errors_of(doc) == [], f'example scenario {f} is valid')


def test_probe_validator():
    """emu-probe: node_outage, ranked selectors, per-node conditions, plots, dry shape."""
    F = scenario.FORMAT
    base = [{'use': 'probe'}, {'use': 'capacity'}]
    def errs(ph, **kw):
        return errors_of(dict({'format': F, 'phases': base + ph}, **kw))
    oto = {'use': 'node_outage', 'name': 'out', 'mode': 'off_then_on', 'off_s': 30,
           'nodes': {'select': 'all', 'rank_by': 'queue.mean', 'pick': 'top_fraction', 'fraction': 0.1}}
    check(errs([oto]) == [], 'node_outage: off_then_on with a ranked top_fraction is valid')
    ph = [p['phase'] for p in scenario.dry({'format': F, 'phases': base + [oto]})['phases']][2:]
    check([p['name'] for p in ph] == ['out', 'out_on'] and ph[0]['actions'][0]['do'] == 'kill'
          and ph[0]['end']['is'] == 'all' and ph[0]['end']['max_s'] == 600
          and ph[1]['actions'][0] == {'do': 'restart', 'nodes': {'select': 'step', 'step': 'out'}}
          and ph[1]['end']['nodes'] == {'select': 'step', 'step': 'out'} and 'load' not in ph[1],
          'node_outage: off_then_on expands to an off part (repaired and off_s) and an on part (select step)')
    e = errs([{'use': 'node_outage', 'mode': 'on', 'of_step': 'x'}])
    check(any(x['path'] == '/phases/2/of_step' for x in e), 'node_outage: of_step must be an earlier step')
    e = errs([{'use': 'node_outage', 'name': 'a'}, {'use': 'node_outage', 'mode': 'on', 'of_step': 'a'},
              {'use': 'node_outage', 'name': 'b', 'mode': 'on', 'of_step': 'a'}])
    check(len(e) == 1 and 'already' in e[0]['message'], 'node_outage: a step switched back on twice is refused')
    e = errs([oto, {'use': 'node_outage', 'mode': 'on', 'of_step': 'out'}])
    check(any('already' in x['message'] for x in e), 'node_outage: on after off_then_on is refused')
    e = errs([{'use': 'node_outage', 'mode': 'on'}])
    check(any(x['path'] == '/phases/2' and 'earlier step' in x['message'] for x in e), 'node_outage: mode on needs of_step')
    e = errs([{'use': 'node_outage', 'nodes': {'select': 'ids', 'ids': [3, 0]}}])
    check(any(x['path'] == '/phases/2/nodes' and 'border router' in x['message'] for x in e),
          'node_outage: the border router cannot be switched off')
    e = errs([{'use': 'node_outage', 'off_s': 5, 'of_step': 'x'}])
    check({x['path'] for x in e} >= {'/phases/2/off_s', '/phases/2/of_step'}, 'node_outage: off_s and of_step only in their mode')
    e = errs([{'use': 'node_outage', 'nodes': {'select': 'all', 'pick': 'above', 'rank_by': 'loss'}}])
    check(any('threshold' in x['message'] for x in e), 'selector: pick above needs value')
    e = errs([{'use': 'node_outage', 'nodes': {'select': 'all', 'pick': 'random', 'rank_by': 'loss'}}])
    check(any(x['path'] == '/phases/2/nodes' and 'A ranking goes with' in x['message'] for x in e),
          'selector: rank_by with a pick that does not rank is refused')
    e = errs([{'use': 'node_outage', 'nodes': {'select': 'all', 'rank_by': 'nope'}}])
    check(any('rank_by' in x['path'] for x in e), 'selector: unknown rank_by')
    e = errors_of({'format': F, 'phases': [{'name': 'a', 'end': {'is': 'metric', 'metric': 'mac.retries', 'over': 'worst_node',
                                                                    'value': 5, 'max_s': 60}},
                                           {'name': 'b', 'end': {'is': 'metric', 'metric': 'offered_pps',
                                                                 'over': 'worst_node', 'value': 5, 'max_s': 60}},
                                           {'name': 'c', 'end': {'is': 'metric', 'metric': 'loss', 'value': 0.1, 'max_s': 60}}]})
    check(len(e) == 1 and e[0]['path'] == '/phases/1/end/over', 'condition: over worst_node only for per-node metrics')
    e = errors_of({'format': F, 'phases': [{'name': 'a'}], 'plots': [{'x': 't_mid_s', 'y': ['pdr', 'nope']},
                                                                    {'x': 'pdr', 'y': 'loss', 'steps': ['zz']}]})
    check({x['path'] for x in e} == {'/plots/0/y', '/plots/1/steps/0'}, 'plots: unknown column and step refused')
    legacy = {'format': F, 'phases': base + [{'use': 'node_failure'}, {'use': 'node_restart'}]}
    q = [p['phase'] for p in scenario.dry(legacy)['phases']][2:]
    check(errors_of(legacy) == [] and q[0]['actions'][0]['label'] == 'failed'
          and q[1]['actions'][0]['nodes'] == {'select': 'label', 'label': 'failed'},
          'legacy: node_failure and node_restart accepted and expanded as before')
    d = scenario.dry({'format': F, 'phases': base + [oto]})
    r = d['phases'][3]
    check({'index', 'name', 'title', 'step', 'step_name', 'start', 'end', 'cap_s', 't_max_s'} <= set(r)
          and r['step'] == 2 and r['step_name'] == 'out' and d['steps'][2]['phases'] == [2, 3]
          and d['run_s'] >= d['virtual_s_max'] + 60, 'dry: phases with their step, plain words, caps; steps; run_s')
    sch = scenario.schema()
    check(set(sch['x-rank-metrics']) >= {'queue.mean', 'loss', 'medium.collisions'} and sch['x-metrics']['loss']['per_node'],
          'schema: x-rank-metrics, x-metrics per_node, x-series-columns')
    check(sch['$defs']['block_node_failure'].get('x-hidden') and not sch['$defs']['block_node_outage'].get('x-hidden'),
          'schema: the legacy blocks are hidden')


def test_per_node(scratch):
    """Per-node values: a minimum of packets per router (2), the down flow counted for its router (3)."""
    r = runlog.Run.__new__(runlog.Run)
    r.tx, r.rx, r.echo, r.nodes = {}, {}, {}, {}
    for k in range(3):             # router 4: 3 packets, one lost
        r.tx[(1, 4, k)] = [10 + k, 0, 64, 1, 0, 0]
        if k:
            r.echo[(1, 4, k)] = (10.5 + k, 10.2 + k, 1)
    for k in range(6):             # router 5: down flow only, 6 packets, one lost
        r.tx[(2, 0, k)] = [10 + k, 5, 64, 1, 0, 0]
        if k:
            r.echo[(2, 0, k)] = (10.4 + k, 10.2 + k, 1)
    np_ = runlog.node_packets(r, 0, 100, 100)
    check(np_[5]['sent'] == 6 and np_[5]['lost'] == 1 and 0 not in np_,
          f'per node: down-flow packets counted for their router ({np_.get(5)})')
    d = scenario.Driver.__new__(scenario.Driver)
    d.run, d.min_node_samples, d.off = r, 5, {}
    pn = d.per_node(0, 100, 100, {'traffic'})
    check(pn[4].get('loss') is None and pn[4]['sent'] == 3 and near(pn[5]['loss'], 1 / 6),
          f'per node: fewer than min_node_samples packets, no loss value ({pn[4]})')
    w = scenario.Driver.worst_of(pn)
    check(w['loss'] == {'node': 5, 'value': round(1 / 6, 6), 'sent': 6}, f'per node: the worst router with its packets ({w["loss"]})')
    d.off = {2: [[15.0, 25.0]]}
    check(d.was_off(2, 10, 20) and not d.was_off(2, 25, 35) and not d.was_off(3, 10, 20),
          'per node: a node switched off during a window is known as such')


def test_series(scratch):
    """report.py series and plots from samples and power snapshots alone."""
    d = os.path.join(scratch, 'series')
    os.makedirs(d)
    with open(os.path.join(d, 'power_snap.jsonl'), 'w') as f:
        for k in range(4):     # node 0: 1 mA, node 1: 2 mA, node 2: 4 mA (charge in µC)
            f.write(json.dumps({'tag': f'w{k}', 't_us': k * 10_000_000,
                                'nodes': [[0, k * 10_000, 0, 0, 0, 0, 0], [1, k * 20_000, 0, 0, 0, 0, 0],
                                          [2, k * 40_000, 0, 0, 0, 0, 0]]}) + '\n')
    with open(os.path.join(d, 'series_samples.jsonl'), 'w') as f:
        for k in range(4):
            f.write(json.dumps({'t_s': k * 10, 'counters': {'ok': 10 * k, 'interference': k, 'busy': k},
                                'rx': {'1': [12 * k, 2 * k, 10 * k]}}) + '\n')
    r = runlog.Run(d)
    r.update()
    s = report.build_series(d, r, [1, 2], 30, {'off_intervals': {'2': [[22.0, None]]}}, 10)
    w = s['windows']
    check(len(w) == 3 and near(w[1]['power.mean_ma'], 3) and w[1]['power.min_ma'] == 2 and w[1]['power.max_node'] == 2
          and w[1]['power.br_ma'] == 1, f'series: power min/mean/max per window ({w[1]["power.mean_ma"]})')
    check(w[2]['medium.collisions'] == 2 and near(w[2]['medium.collision_share'], 2 / 12, 1e-3),
          f'series: collisions per window ({w[2]["medium.collisions"]})')
    check(w[2]['power.max_ma'] == 2 and w[2]['power.max_node'] == 1,
          f'series: a router switched off in the window is not in its power ({w[2]["power.max_node"]})')
    pl = report.resolve_plots([{'x': 't_mid_s', 'y': ['power.min_ma', 'power.max_ma']}], s)
    check(pl[0]['kind'] == 'line' and pl[0]['series'][1]['points'] == [[5.0, 4.0], [15.0, 4.0], [25.0, 2.0]]
          and pl[0]['x']['unit'] == 's' and pl[0]['y'][0]['unit'] == 'mA', 'plots: resolved with units and points')


def main():
    # No system temporary directory: an argument, else $TMPDIR (CLAUDE.md)
    base = sys.argv[1] if len(sys.argv) > 1 else os.environ.get('TMPDIR')
    if not base:
        print('usage: test_report.py <scratch dir> (or set TMPDIR)', file=sys.stderr)
        return 2
    scratch = base
    scratch = os.path.join(scratch, 'test_report')
    shutil.rmtree(scratch, ignore_errors=True)
    os.makedirs(scratch)
    test_report(scratch)
    test_runlog_incremental(scratch)
    test_restart_counters(scratch)
    test_validator()
    test_probe_validator()
    test_per_node(scratch)
    test_series(scratch)
    shutil.rmtree(scratch, ignore_errors=True)
    print('test_report:', f'FAILED ({len(FAILS)})' if FAILS else 'PASSED')
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
