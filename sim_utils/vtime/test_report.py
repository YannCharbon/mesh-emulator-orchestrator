#!/usr/bin/env python3
"""Self-test of sim_utils/report.py, sim_utils/runlog.py and the scenario
validator (sim_utils/scenario.py), on synthetic run directories whose figures
are known. No emulator program runs: about one second.

usage: test_report.py [<scratch dir>]
"""
import json, os, shutil, sys, tempfile

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


def net1(node, t_us, parent, q_int_mac=0, q_int_al=0, retries=0, tx=0, bc_tx=0, red=0, hist=None, final=False):
    j = {'v': 1, 'node': node, 't_us': t_us, 'final': final,
         'mac': {'tx': tx, 'tx_bytes': 0, 'rx': 0, 'rx_bytes': 0, 'bc_tx': bc_tx, 'bc_rx': 0, 'retries': retries,
                 'tx_fail': 0, 'cca_att': tx, 'cca_fail': 0, 'rx_drop': 0, 'q': 0, 'q_max': 3,
                 'q_int_us': q_int_mac, 'lat_max_ms': 0, 'retry_hist': hist or [tx - bc_tx, 0, 0, 0, 0, 0, 0, 0, 0]},
         'al': {'q': 0, 'q_max': 9, 'q_int_us': q_int_al, 'red_drop': red, 'lat_max_s': 0},
         'ip': {'tx': 0, 'rx': 0, 'tx_bytes': 0, 'rx_bytes': 0, 'routed': 0, 'routed_bytes': 0, 'no_route': 0,
                'rx_drop': 0, 'frag_tx_err': 0, 'frag_rx_err': 0, 'buf_fail': 0},
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
        nodes[1].append(net1(1, tu, 0, q_int_al=al, tx=t, retries=t // 2))
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


def main():
    scratch = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp()
    scratch = os.path.join(scratch, 'test_report')
    shutil.rmtree(scratch, ignore_errors=True)
    os.makedirs(scratch)
    test_report(scratch)
    test_runlog_incremental(scratch)
    test_validator()
    shutil.rmtree(scratch, ignore_errors=True)
    print('test_report:', f'FAILED ({len(FAILS)})' if FAILS else 'PASSED')
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
