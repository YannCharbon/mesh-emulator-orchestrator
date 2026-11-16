#!/usr/bin/env python3
"""Run report of a virtual-time run (docs/TRAFFIC_AND_REPORT.md §5).

usage: report.py <run dir> [--json OUT] [--top N] [--pdr-min R] [--latency-factor F] [--tail S] [--window S] [--quiet]
       report.py --aggregate <run dir>... [--json OUT]

Reads the run directory once (every log streamed once, sim_utils/runlog.py)
and writes report.json (format "wssim-report 1"): run parameters, final
topology, join timeline, application traffic (delivery, RTT and one-way delay
by hop count and by load step, offered against delivered throughput, the
saturation point of a ramp), per-node bottleneck table and the top
bottlenecks, medium totals, power per node and in total, and the scenario's
phases when the run had one. Prints a short summary.

--aggregate: several runs of the same configuration (other seeds): the
median, minimum and maximum of the main figures over the runs.

Options:
  --json OUT          write the report there (default: <run dir>/report.json)
  --top N             bottleneck nodes listed (default 5)
  --pdr-min R         a load step is saturated below this delivery ratio (default 0.99)
  --latency-factor F  ... or when its RTT p95 (one-way p95 without echoes) is
                      F x the first step's (default 3)
  --tail S            packets sent in the last S s of the run are not counted (default 20)
  --window S          length of a row of the time series (report.json "series"; default: the
                      scenario's report.window_s, else 10)
  --quiet             no summary on stdout

Standard library only.
"""
import json, os, re, statistics, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runlog  # noqa: E402
from scenario_schema import SERIES_COLUMNS, REPORT_WINDOW_S  # noqa: E402

FORMAT = 'wssim-report 1'
OUTCOMES = ['ok', 'noise', 'interference', 'captured', 'below_lock', 'busy', 'not_listening', 'tx_abort', 'left']


def r4(v, n=4):
    return None if v is None else round(v, n)


def read_info(d):
    info = {}
    try:
        txt = open(os.path.join(d, 'run_info.txt')).read()
    except OSError:
        return info, ''
    for line in txt.splitlines():
        for m in re.finditer(r'(\w+)=(\S*)', line.split(' phy_env=')[0]):
            info.setdefault(m.group(1), m.group(2))
        for key in ('app_opts', 'sim_opts', 'events', 'power', 'scenario'):
            if line.startswith(key + '='):
                info[key] = line[len(key) + 1:]
    return info, txt


def load_settings(app_opts):
    out = {}
    for m in re.finditer(r'-o (load_\w+|stats\w*|queue_limit|log_level)=(\S+)', app_opts or ''):
        out[m.group(1)] = m.group(2)
    return out


def topology_links(d):
    """{(a, b): rssi} of topology.txt's explicit links (both directions)."""
    links = {}
    try:
        for line in open(os.path.join(d, 'topology.txt')):
            m = re.match(r'(\d+),(\d+):(-?[\d.]+)\s*$', line.strip())
            if m:
                a, b, r = int(m.group(1)), int(m.group(2)), float(m.group(3))
                links[(a, b)] = r
                links.setdefault((b, a), r)
    except OSError:
        pass
    return links


def positions(sim_opts):
    m = re.search(r'--positions[= ](\S+)', sim_opts or '')
    pos = {}
    if not m:
        return pos
    try:
        for line in open(m.group(1)):
            w = line.split('#')[0].split()
            if len(w) >= 4:
                pos[int(w[0])] = [float(w[1]), float(w[2]), float(w[3])] + ([int(w[4])] if len(w) >= 5 else [])
    except (OSError, ValueError):
        pass
    return pos


def simserver(d):
    out = {'outcomes': {}}
    try:
        for line in open(os.path.join(d, 'simserver.log'), errors='replace'):
            m = re.match(r'^medium:\s{2,}([a-z_]+)\s+(\d+)\s*$', line)
            if m:
                out['outcomes'][m.group(1)] = int(m.group(2))
            m = re.match(r'^virtual time: reached ([0-9.]+) s, (\d+) wakes, (\d+) frames sent, (\d+) frames delivered', line)
            if m:
                out.update(reached_s=float(m.group(1)), wakes=int(m.group(2)), frames_sent=int(m.group(3)),
                           frames_delivered=int(m.group(4)))
            m = re.match(r'^virtual time: wall ([0-9.]+) s', line)
            if m:
                out['wall_s'] = float(m.group(1))
            m = re.match(r'^medium: preset (\w+)', line)
            if m:
                out['preset'] = m.group(1)
            m = re.match(r'^medium: (\d+) CCA queries, (\d+) with a frame on air', line)
            if m:
                out['cca_queries'], out['cca_with_frame'] = int(m.group(1)), int(m.group(2))
            if line.startswith('virtual time: end ('):
                out['end_reason'] = line.strip()[len('virtual time: end ('):-1]
            if 'medium=legacy' in line or line.startswith('medium: legacy'):
                out['preset'] = 'legacy'
    except OSError:
        pass
    heard = sum(v for k, v in out['outcomes'].items() if k != 'not_listening')
    out['loss_share'] = r4((heard - out['outcomes'].get('ok', 0)) / heard) if heard else None
    coll = sum(out['outcomes'].get(k, 0) for k in ('interference', 'captured', 'busy'))
    out['collision_share'] = r4(coll / heard) if heard else None
    return out


def power(d):
    try:
        p = json.load(open(os.path.join(d, 'power.json')))
    except (OSError, ValueError):
        return None
    nodes = []
    for n in p['nodes']:
        span = n['check']['run_time_us'] or 1
        ms, rs = n['mcu']['states'], n['radio']['states']
        def q(states, k):
            return states.get(k, {}).get('charge_uc', 0)
        row = {'node': n['node'], 'avg_current_ma': r4(n['avg_current_ma'], 6), 'charge_uc': r4(n['charge_uc'], 1),
               'energy_mj': r4(n.get('energy_mj'), 3),
               'time_share': {'mcu_active': r4(ms['active']['time_us'] / span, 6),
                              'radio_listen': r4(rs['listen']['time_us'] / span, 6),
                              'radio_receive': r4(rs['receive']['time_us'] / span, 6),
                              'radio_tx': r4(rs['tx']['time_us'] / span, 6)},
               'charge_share': {}}
        tot = n['charge_uc'] or 1
        for k in ('active', 'sleep', 'deep_sleep'):
            row['charge_share']['mcu_' + k] = r4(q(ms, k) / tot)
        for k in ('listen', 'receive', 'tx', 'idle', 'off'):
            row['charge_share']['radio_' + k] = r4(q(rs, k) / tot)
        if n.get('battery'):
            row['battery_life_years'] = n['battery'].get('life_years')
        nodes.append(row)
    routers = [x['avg_current_ma'] for x in nodes if x['node'] != 0]
    return {'run_time_s': p['run_time_us'] / 1e6, 'profiles': [x['name'] for x in p['profiles']], 'nodes': nodes,
            'totals': {'routers_mean_ma': r4(statistics.mean(routers), 6) if routers else None,
                       'routers_max_ma': r4(max(routers), 6) if routers else None,
                       'network_charge_c': r4(sum(x['charge_uc'] for x in nodes) / 1e6, 6)}}


def steps_of(run, info, scen):
    """The load steps: [(tag, label, t_from, t_to, extra)] from a ramp or a scenario."""
    out = []
    if scen:
        tags = sorted(scen.get('steps', {}).items(), key=lambda kv: kv[1]['t_s'])
        for i, (tag, s) in enumerate(tags):
            t_to = tags[i + 1][1]['t_s'] if i + 1 < len(tags) else scen.get('end_t_s', s['t_s'])
            label = s['phase'] + (f' #{s["iteration"]}' if s.get('iteration') is not None else '')
            out.append((int(tag), label, s['t_s'], t_to, {'rate_pps': s['rate_pps'], 'interval_s': s['interval_s']}))
        return out
    ls = load_settings(info.get('app_opts'))
    ramp = float(ls.get('load_ramp_step_s', 0))
    start = float(ls.get('load_start_s', 0))
    if ramp > 0:
        n = int(ls.get('load_ramp_steps', 6))
        for k in range(n):
            out.append((k, f'step {k}', start + k * ramp, start + (k + 1) * ramp, {}))
    return out


def read_jsonl(path):
    out = []
    try:
        for line in open(path):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return out


def _at(samples, t):
    """The latest sample (t, x) at or before t: x, or None."""
    best = None
    for tt, x in samples:
        if tt <= t + 1e-6:
            best = x
        else:
            break
    return best


COLL = ('interference', 'captured', 'busy')


class Series:
    """The rows of report.json "series" (docs/TRAFFIC_AND_REPORT.md §5.6):
    the main flow's packets, the NET1 samples, the medium's counters and the
    power snapshots, over any interval."""

    def __init__(self, d, run, routers, end_s):
        self.run, self.routers, self.end_s = run, set(routers), end_s
        smp = read_jsonl(os.path.join(d, 'series_samples.jsonl'))
        self.ctr = sorted((x['t_s'], x) for x in smp)
        snaps = read_jsonl(os.path.join(d, 'power_snap.jsonl'))
        self.snaps = sorted(((x['t_us'] / 1e6, x) for x in snaps), key=lambda kv: kv[0])
        flows = {k[0] for k in run.tx}
        self.flow = 'up' if 1 in flows or not flows else runlog.FLOW_NAMES.get(min(flows), str(min(flows)))

    def row(self, a, b):
        r = {k: None for k in SERIES_COLUMNS}
        r.update(t_start_s=round(a, 3), t_end_s=round(b, 3), t_mid_s=round((a + b) / 2, 3))
        f = runlog.packet_metrics(self.run, a, b, self.end_s, flows=[self.flow]).get(self.flow)
        if f and f['sent']:
            r['offered_pps'] = r4(f['offered_pps'])
            r['delivered_pps'] = r4(f['delivered_pps'])
            r['pdr'] = r4(f['pdr'])
            r['loss'] = r4(1 - f['pdr']) if f['pdr'] is not None else None
            for k in ('min', 'mean', 'p95', 'max'):
                r[f'rtt.{k}_ms'] = r4(f['rtt_ms'].get(k), 2)
            for k in ('mean', 'p95'):
                r[f'latency.{k}_ms'] = r4(f['latency_ms'].get(k), 2)
        nd = runlog.net_delta(self.run, a, b)
        if nd:
            r['mac.retries'] = sum(x['retries'] for x in nd.values())
            r['mac.retries_per_frame'] = r4(runlog.retries_per_frame(nd.values()))
            r['queue.mean_max'] = r4(max(x['mac_q_mean'] + x['al_q_mean'] for x in nd.values()))
            par = {n: x['parent'] for n, x in nd.items() if n in self.routers}
            dep = runlog.depths(par)
            if par:
                r['mesh.joined_fraction'] = r4(sum(1 for n in par if dep.get(n) is not None) / len(par))
        ca, cb = _at(self.ctr, a), _at(self.ctr, b)
        if cb and cb is not ca and cb['t_s'] > a:
            c0 = (ca or {}).get('counters', {})
            c1 = cb['counters']
            heard = sum(c1.get(k, 0) - c0.get(k, 0) for k in OUTCOMES if k != 'not_listening')
            coll = sum(c1.get(k, 0) - c0.get(k, 0) for k in COLL)
            r['medium.collisions'] = coll
            r['medium.collision_share'] = r4(coll / heard) if heard else None
        sa, sb = _at(self.snaps, a), _at(self.snaps, b)
        if sb and sb is not sa and sb['t_us'] / 1e6 > a:
            span = (sb['t_us'] - (sa['t_us'] if sa else 0)) / 1e6
            old = {x[0]: x[1] for x in (sa['nodes'] if sa else [])}
            per = {x[0]: (x[1] - old.get(x[0], 0)) / 1e3 / span for x in sb['nodes']} if span > 0 else {}
            rt = [(v, n) for n, v in per.items() if n != 0]
            if rt:
                r['power.min_ma'] = r4(min(rt)[0])
                r['power.mean_ma'] = r4(sum(v for v, _ in rt) / len(rt))
                mx = max(rt, key=lambda x: (x[0], -x[1]))
                r['power.max_ma'], r['power.max_node'] = r4(mx[0]), mx[1]
            if 0 in per:
                r['power.br_ma'] = r4(per[0])
        return r


def build_series(d, run, routers, end_s, scen, window_s):
    sr = Series(d, run, routers, end_s)
    phases = (scen or {}).get('phases') or []

    def step_at(t):
        for ph in phases:
            a = (ph.get('start') or {}).get('t_s')
            b = (ph.get('end') or {}).get('t_s')
            if a is not None and b is not None and a <= t < b and not ph.get('skipped'):
                return ph
        return None
    windows = []
    k = 0
    while k * window_s < end_s - 1e-9:
        a, b = k * window_s, min((k + 1) * window_s, end_s)
        r = sr.row(a, b)
        ph = step_at((a + b) / 2)
        r['step_index'] = ph.get('step', ph.get('index')) if ph else None
        r['step_name'] = ph.get('step_name', ph.get('name')) if ph else None
        r['phase'] = ph['name'] if ph else None
        windows.append(r)
        k += 1
    steps = []
    for ph in phases:
        if ph.get('skipped'):
            continue
        base = {'step_index': ph.get('step', ph.get('index')), 'step_name': ph.get('step_name', ph['name']),
                'phase': ph['name']}
        if ph.get('iterations'):
            levels = {}
            for it in ph['iterations']:
                m = it.get('metrics') or {}
                lv = levels.setdefault(it['step_tag'], {'a': m.get('t_from_s', it['t_s']), 'b': m.get('t_to_s'),
                                                        'rate': it['rate_pps'], 'iteration': it['iteration']})
                lv['b'] = m.get('t_to_s', lv['b'])
            for tag, lv in levels.items():
                if lv['b'] is None or lv['b'] <= lv['a']:
                    continue
                r = sr.row(lv['a'], lv['b'])
                r.update(base, label=f'{ph["name"]} at {lv["rate"]:.4g} packets/s', iteration=lv['iteration'],
                         rate_pps=r4(lv['rate']), kind='level')
                steps.append(r)
            continue
        m = ph.get('metrics') or {}
        a = m.get('t_from_s', (ph.get('start') or {}).get('t_s'))
        b = (ph.get('end') or {}).get('t_s')
        if a is None or b is None or b <= a:
            continue
        r = sr.row(a, b)
        r.update(base, label=ph['name'], iteration=None, rate_pps=r4(((ph.get('load') or {}).get('rate_pps'))),
                 kind='phase')
        steps.append(r)
    cols = {k: {'unit': v[0], 'title': v[1], 'description': v[2]} for k, v in SERIES_COLUMNS.items()}
    return {'window_s': window_s, 'flow': sr.flow, 'columns': cols, 'windows': windows, 'steps': steps}


def resolve_plots(plots, series):
    out = []
    cols = series['columns']
    for pl in plots or []:
        ys = pl['y'] if isinstance(pl['y'], list) else [pl['y']]
        pts = pl.get('points', 'windows')
        rows = series.get(pts) or []
        if pl.get('steps'):
            rows = [r for r in rows if r.get('step_name') in pl['steps']]
        kind = pl.get('kind', 'line' if pts == 'windows' else 'scatter')
        ser = []
        for y in ys:
            p = [(r[pl['x']], r[y], r.get('label') or r.get('step_name')) for r in rows
                 if r.get(pl['x']) is not None and r.get(y) is not None]
            if kind == 'line':
                p.sort(key=lambda x: x[0])
            ser.append({'column': y, 'title': cols[y]['title'], 'unit': cols[y]['unit'],
                        'points': [[x, v] for x, v, _ in p], 'labels': [lb for _, _, lb in p]})
        title = pl.get('title') or (', '.join(cols[y]['title'] for y in ys) + ' against ' + cols[pl['x']]['title'])
        out.append({'title': title, 'x': {'column': pl['x'], 'title': cols[pl['x']]['title'],
                                         'unit': cols[pl['x']]['unit']},
                    'y': [{'column': y, 'title': cols[y]['title'], 'unit': cols[y]['unit']} for y in ys],
                    'points': pts, 'kind': kind, 'steps': pl.get('steps'), 'series': ser})
    return out


def report(d, top=5, pdr_min=0.99, lat_factor=3.0, tail=20.0, window_s=None):
    info, info_txt = read_info(d)
    run = runlog.Run(d)
    run.update()
    srv = simserver(d)
    end_s = srv.get('reached_s') or float(info.get('vtime_end', 0) or 0)
    scen = None
    try:
        scen = json.load(open(os.path.join(d, 'scenario_result.json')))
    except (OSError, ValueError):
        pass
    links = topology_links(d)
    pos = positions(info.get('sim_opts'))
    routers = run.routers()
    rep = {'format': FORMAT, 'run_dir': os.path.abspath(d)}
    ls = load_settings(info.get('app_opts'))
    rep['run'] = {'seed': int(info.get('seed', 1)), 'nodes': int(info.get('nodes', 0) or 0),
                  'routers': len(routers), 'virtual_s': end_s, 'vtime_end_s': float(info.get('vtime_end', 0) or 0),
                  'end_reason': srv.get('end_reason'), 'wall_s': srv.get('wall_s'), 'conf': info.get('conf'),
                  'medium_preset': srv.get('preset'), 'sim_opts': info.get('sim_opts'),
                  'power_profile': info.get('power') and info['power'].split()[0], 'load': ls,
                  'scenario': info.get('scenario', '').split()[0] if info.get('scenario') else None}

    # ---- topology (the end of the run): NET1 parents, else the BR's table
    par = run.parents()
    src = 'NET1 (each router\'s RPL preferred parent)'
    if not par:
        t, par = run.br_tree()
        src = 'border router table (brtree)'
    dep = runlog.depths(par)
    ch = runlog.children(par)
    t_tree, tree = run.br_tree()
    nodes = []
    for n in sorted(set(routers) | {0}):
        s = run.last_net(n)
        rpl = (s or {}).get('rpl', {})
        ws = (s or {}).get('ws', {})
        p = par.get(n)
        row = {'node': n, 'parent': p, 'hops': dep.get(n), 'rank': rpl.get('rank'), 'dag_rank': rpl.get('dag_rank'),
               'children': len(ch.get(n, [])), 'subtree': len(runlog.subtree(par, n)),
               'rssi_to_parent_dbm': links.get((n, p)) if p is not None else None,
               'rsl_in_dbm': ws.get('rsl_in_dbm'), 'rsl_out_dbm': ws.get('rsl_out_dbm'),
               'in_br_table': n in tree if n else None, 'position': pos.get(n)}
        nodes.append(row)
    hops = [dep.get(n) for n in routers]
    rep['topology'] = {
        'source': src, 'br_table_t_s': t_tree, 'br_table_size': len(tree),
        'nodes': nodes,
        'hops_histogram': {str(h): hops.count(h) for h in sorted({h for h in hops if h is not None})},
        'unreachable': sum(1 for h in hops if h is None),
        'max_hops': max((h for h in hops if h is not None), default=None),
        'max_children': max((len(v) for v in ch.values()), default=0),
    }

    # ---- join
    ms_names = [k for k, _ in runlog.MILESTONES]
    join = []
    for n in routers:
        node = run.nodes[n]
        first = {}
        for inst, k, t in node.ms_history:
            if inst == 0:
                first.setdefault(k, round(t, 3))
        join.append(dict({'node': n, 'instances': node.instances}, **{k: first.get(k) for k in ms_names}))
    ready = sorted(j['ready'] for j in join if j['ready'] is not None)
    rep['join'] = {'nodes': join, 'ready': len(ready), 'of': len(routers),
                   'ready_median_s': r4(statistics.median(ready), 2) if ready else None,
                   'last_ready_s': ready[-1] if ready else None,
                   'operational': sum(1 for j in join if j['operational'] is not None)}

    # ---- traffic
    t_first = min((v[0] for v in run.tx.values()), default=None)
    t_last = end_s - tail
    traffic = None
    if t_first is not None:
        traffic = {'window_s': [r4(t_first, 3), r4(t_last, 3)], 'tail_s': tail,
                   'flows': runlog.packet_metrics(run, t_first, t_last, end_s),
                   'skipped': len(run.skips)}
        steps = steps_of(run, info, scen)
        if steps:
            rows = []
            for tag, label, a, b, extra in steps:
                sel = {k: v for k, v in run.tx.items() if v[3] == tag and v[0] < t_last}
                sub = runlog.Run.__new__(runlog.Run)
                sub.tx, sub.rx, sub.echo, sub.cfg, sub.nodes = sel, run.rx, run.echo, run.cfg, run.nodes
                # the last step ends with the counted window (--tail)
                b = min(b, t_last)
                if b <= a:
                    continue
                pm = runlog.packet_metrics(sub, a, b, end_s)
                rows.append(dict({'tag': tag, 'label': label, 't_from_s': r4(a, 3), 't_to_s': r4(b, 3),
                                  'flows': pm, 'queues': step_queues(run, a, b)}, **extra))
            traffic['steps'] = rows
            traffic['saturation'] = saturation(rows, pdr_min, lat_factor)
            # RTT and delay by hop count at light load: the first step
            if rows:
                traffic['light_load'] = {'tag': rows[0]['tag'], 'label': rows[0]['label'],
                                         'flows': {n: {k: f[k] for k in ('sent', 'pdr', 'rtt_ms', 'rtt_ms_by_hops',
                                                                         'latency_ms', 'latency_ms_by_hops')}
                                                   for n, f in rows[0]['flows'].items()}}
        flag = queue_flag(run, rep['run']['load'], rows if steps else [])
        if flag:
            traffic['queue_warning'] = flag
    rep['traffic'] = traffic

    # ---- bottlenecks: counters over the load period (or the whole run)
    a = t_first if t_first is not None else 0
    nd = runlog.net_delta(run, a, end_s)
    npk = runlog.node_packets(run, a, t_last if t_first is not None else end_s, end_s)
    ctr = sorted((x['t_s'], x) for x in read_jsonl(os.path.join(d, 'series_samples.jsonl')))
    rx0, rx1 = (_at(ctr, a) or {}).get('rx', {}), (ctr[-1][1].get('rx', {}) if ctr else None)
    table = []
    for n, x in sorted(nd.items()):
        row = {'node': n, 'hops': dep.get(n) if n else 0, 'children': len(ch.get(n, [])),
               'subtree': len(runlog.subtree(par, n)) if n else len(routers),
               'mac_q_mean': r4(x['mac_q_mean']), 'al_q_mean': r4(x['al_q_mean']),
               'mac_q_peak': x['mac_q_peak'], 'al_q_peak': x['al_q_peak'],
               'queue_drops': x['red_drop'], 'mac_tx': x['mac_tx'],
               'uc_frames': x['uc_frames'], 'retries_per_frame': r4(x['retries_per_frame']),
               'uc_retry_hist': x['uc_retry_hist'], 'give_up': x['give_up'], 'give_up_by_reason': x['uc_give_up'],
               'sock_tx_fail': x['sock_tx_fail'], 'retry_attempts': x['mac_retry_attempts'],
               'cca_attempts': x['mac_cca_att'], 'cca_fail': x['mac_cca_fail'], 'rx': x['mac_rx'],
               'parent_changes': x['parent_changes'], 'parent_tx_fail': x['parent_tx_fail'],
               'no_route': x['no_route'], 'control_messages': {k: x['ctl_' + k] for k in ('dis', 'dio', 'dao')},
               'retries': x['retries'], 'heard': None, 'collisions': None, 'collision_share': None,
               'sent': npk.get(n, {}).get('sent'), 'lost': npk.get(n, {}).get('lost'),
               'loss': r4(npk.get(n, {}).get('loss'))}
        if rx1 is not None:
            h1, c1, _ = rx1.get(str(n), (0, 0, 0))
            h0, c0, _ = rx0.get(str(n), (0, 0, 0))
            row.update(heard=h1 - h0, collisions=c1 - c0, collision_share=r4((c1 - c0) / (h1 - h0)) if h1 > h0 else None)
        row['score'] = r4(x['mac_q_mean'] + x['al_q_mean'] + 0.1 * x['red_drop'] / max(1, (end_s - a) / 60)
                          + 0.01 * x['give_up'])
        table.append(row)
    for row in table:
        why = []
        if row['al_q_mean'] or row['mac_q_mean']:
            if (row['al_q_mean'] or 0) + (row['mac_q_mean'] or 0) >= 0.5:
                why.append(f'mean queue {row["al_q_mean"] + row["mac_q_mean"]:.2f} frames (adaptation '
                           f'{row["al_q_mean"]:.2f}, MAC {row["mac_q_mean"]:.2f}), peak {row["al_q_peak"]}')
        if row['queue_drops']:
            why.append(f'{row["queue_drops"]} packets dropped by its full queue')
        if row['retries_per_frame'] and row['retries_per_frame'] >= 0.3:
            why.append(f'{row["retries_per_frame"]:.2f} MAC retries per unicast frame')
        if row['give_up']:
            why.append(f'{row["give_up"]} unicast frames given up by the MAC ({row["give_up_by_reason"]})')
        if row['cca_attempts'] and row['cca_fail'] / row['cca_attempts'] > 0.05:
            why.append(f'{100 * row["cca_fail"] / row["cca_attempts"]:.1f} % channel-access failures')
        if row['subtree'] and row['node'] != 0:
            why.append(f'relays for {row["subtree"]} routers below it')
        if row['node'] == 0:
            why.append('the border router: every packet of the network')
        row['reasons'] = why
    ranked = sorted(table, key=lambda r: (-(r['score'] or 0), r['node']))
    rep['nodes'] = {'window_s': [r4(a, 3), end_s], 'table': table,
                    'top_bottlenecks': [{'node': r['node'], 'score': r['score'], 'reasons': r['reasons']}
                                        for r in ranked[:top] if (r['score'] or 0) > 0],
                    'score': 'mean queue depth (adaptation + MAC, frames) + 0.1 x queue drops per minute + '
                             '0.01 x frames given up'}
    rep['network'] = {'mac_frames': sum(r['mac_tx'] for r in table),
                      'mac_unicast_frames': sum(r['uc_frames'] for r in table),
                      'retries_per_frame': r4(runlog.retries_per_frame(nd.values())),
                      'give_up': sum(r['give_up'] for r in table), 'sock_tx_fail': sum(r['sock_tx_fail'] for r in table),
                      'queue_drops': sum(r['queue_drops'] for r in table),
                      'retries': sum(r['retries'] for r in table),
                      'collisions': sum(r['collisions'] for r in table) if rx1 is not None else None,
                      'loss': r4(sum(r['lost'] or 0 for r in table) / sum(r['sent'] or 0 for r in table))
                      if sum(r['sent'] or 0 for r in table) else None,
                      'parent_changes': sum(r['parent_changes'] for r in table),
                      'control_messages': {k: sum(r['control_messages'][k] for r in table) for k in ('dis', 'dio', 'dao')}}
    rep['medium'] = srv
    rep['power'] = power(d)
    if scen:
        rep['scenario'] = {'name': scen.get('scenario'), 'status': scen.get('status'), 'summary': scen.get('summary'),
                           'phases': scen.get('phases'), 'events': scen.get('events'), 'labels': scen.get('labels'),
                           'user_steps': scen.get('user_steps')}
    # ---- time series and plots
    doc = {}
    try:
        doc = json.load(open(os.path.join(d, 'scenario.json')))
    except (OSError, ValueError):
        pass
    if window_s is None:
        window_s = (doc.get('report') or {}).get('window_s', REPORT_WINDOW_S)
    rep['series'] = None
    rep['plots'] = []
    if traffic is not None or scen or os.path.exists(os.path.join(d, 'power_snap.jsonl')):
        rep['series'] = build_series(d, run, routers, end_s, scen, window_s)
        rep['plots'] = resolve_plots(doc.get('plots'), rep['series'])
    return rep


def step_queues(run, a, b):
    """Queues, drops and retries of a load step, from the NET1 samples at or
    before its start and end (their period sets the resolution)."""
    nd = runlog.net_delta(run, a, b)
    if not nd:
        return None
    top = max(nd.items(), key=lambda kv: kv[1]['mac_q_mean'] + kv[1]['al_q_mean'])
    return {'mean_max': r4(top[1]['mac_q_mean'] + top[1]['al_q_mean']), 'mean_max_node': top[0],
            'drops': sum(x['red_drop'] for x in nd.values()), 'give_up': sum(x['give_up'] for x in nd.values()),
            'retries_per_frame': r4(runlog.retries_per_frame(nd.values()))}


def saturation(rows, pdr_min, lat_factor):
    """Per flow, the first saturated step: delivery below pdr_min, or p95 RTT
    (one-way p95 for a flow without echoes) above lat_factor x the flow's
    first step's; overall: the earliest over the flows."""
    out = {'criterion': f'delivery ratio < {pdr_min} or p95 delay > {lat_factor} x the first step\'s',
           'flows': {}, 'saturated_step': None, 'last_good_step': None}
    names = sorted({n for r in rows for n in r['flows']})
    for name in names:
        fo = {'saturated_step': None, 'last_good_step': None}
        base = None
        for r in rows:
            f = r['flows'].get(name)
            if not f or not f['sent']:
                continue
            st = f['rtt_ms'] if f['rtt_ms']['n'] else f['latency_ms']
            pdr, p95 = f['pdr'], st.get('p95')
            if base is None:
                base = p95
            if (pdr is not None and pdr < pdr_min) or (p95 is not None and base and p95 > lat_factor * base):
                fo['saturated_step'] = r['tag']
                fo['saturated_offered_pps'] = r4(f['offered_pps'])
                break
            fo['last_good_step'] = r['tag']
            fo['capacity_offered_pps'] = r4(f['offered_pps'])
            fo['capacity_delivered_pps'] = r4(f['delivered_pps'])
        fo['max_delivered_pps'] = r4(max((r['flows'][name]['delivered_pps'] for r in rows if name in r['flows']),
                                         default=None))
        out['flows'][name] = fo
    order = [r['tag'] for r in rows]
    sat = [fo for fo in out['flows'].values() if fo['saturated_step'] is not None]
    if sat:
        first = min(sat, key=lambda fo: order.index(fo['saturated_step']))
        out['saturated_step'] = first['saturated_step']
        i = order.index(first['saturated_step'])
        out['last_good_step'] = order[i - 1] if i > 0 else None
    else:
        out['last_good_step'] = order[-1] if order else None
    return out


def queue_flag(run, load_opts, rows):
    """Whether delays were made by queues larger than a device holds: without
    queue_limit the stack sizes the adaptation-layer queues from an unbounded
    heap on a PC. Flags each step whose highest mean queue exceeds 16 frames."""
    if 'queue_limit' in (load_opts or {}):
        return None
    peaks = {}
    for nid in run.nodes:
        s = run.last_net(nid)
        if s:
            peaks[nid] = s.get('al', {}).get('q_max', 0)
    worst = max(peaks.items(), key=lambda kv: kv[1], default=(None, 0))
    steps = [r['tag'] for r in rows if (r.get('queues') or {}).get('mean_max') and r['queues']['mean_max'] > 16]
    if worst[1] <= 32 and not steps:
        return None
    return {'text': 'no queue_limit: queues grew beyond what a device holds (heap-sized on a PC); delays at '
                    'these loads include waits a device would turn into drops',
            'al_q_peak': worst[1], 'node': worst[0], 'steps': steps}


def summary(rep):
    lines = [f'run {rep["run_dir"]}: seed {rep["run"]["seed"]}, {rep["run"]["routers"]} routers, '
             f'{rep["run"]["virtual_s"]} s virtual, {rep["run"]["wall_s"]} s wall']
    j = rep['join']
    lines.append(f'join: {j["ready"]}/{j["of"]} ready, median {j["ready_median_s"]} s, last {j["last_ready_s"]} s; '
                 f'hops {rep["topology"]["hops_histogram"]}, max children {rep["topology"]["max_children"]}')
    tr = rep.get('traffic')
    if tr:
        for name, f in tr['flows'].items():
            lines.append(f'flow {name}: {f["sent"]} sent, PDR {r4(f["pdr"])}, RTT ms {fmt(f["rtt_ms"])}, one-way ms '
                         f'{fmt(f["latency_ms"])}')
            for h, s in f['rtt_ms_by_hops'].items():
                lines.append(f'   {h} hops: RTT ms {fmt(s)}')
        for r in tr.get('steps', []):
            for name, f in r['flows'].items():
                q = r.get('queues') or {}
                lines.append(f'step {r["tag"]} {r["label"]}: {name} offered {f["offered_pps"]:.3f}/s delivered '
                             f'{f["delivered_pps"]:.3f}/s PDR {r4(f["pdr"])} RTT p95 {r4(f["rtt_ms"].get("p95"), 1)} ms; '
                             f'queue mean max {q.get("mean_max")} (node {q.get("mean_max_node")}), retries/frame '
                             f'{q.get("retries_per_frame")}, drops {q.get("drops")}')
        if tr.get('saturation'):
            lines.append(f'saturation: {tr["saturation"]}')
    for b in rep['nodes']['top_bottlenecks']:
        lines.append(f'bottleneck node {b["node"]} (score {b["score"]}): {"; ".join(b["reasons"])}')
    n = rep['network']
    lines.append(f'network: {n["mac_frames"]} MAC frames, {n["retries_per_frame"]} retries per unicast frame, '
                 f'{n["give_up"]} given up, {n["queue_drops"]} queue drops, {n["parent_changes"]} parent changes')
    m = rep['medium']
    lines.append(f'medium: {m.get("frames_sent")} frames, loss share {m.get("loss_share")}, collisions {m.get("collision_share")}')
    if rep['power']:
        lines.append(f'power: routers mean {rep["power"]["totals"]["routers_mean_ma"]} mA, max '
                     f'{rep["power"]["totals"]["routers_max_ma"]} mA')
    sc = rep.get('scenario')
    if sc:
        lines.append(f'scenario {sc["name"]}: {sc["status"]}')
        for p in sc['summary']['phases']:
            lines.append(f'  {p["name"]}: {p.get("start_s")}-{p.get("end_s")} s; PDR {p.get("pdr")}; RTT {p.get("rtt_ms")}'
                         + (f'; capacity {p["capacity"]["capacity_pps"]} pkt/s' if p.get('capacity') else '')
                         + (f'; disruptions {p["disruptions"]}' if p.get('disruptions') else ''))
    return '\n'.join(lines)


def fmt(s):
    if not s.get('n'):
        return 'n=0'
    return f'n={s["n"]} min {s["min"]:.0f} mean {s["mean"]:.0f} p50 {s["p50"]:.0f} p95 {s["p95"]:.0f} max {s["max"]:.0f}'


def aggregate(dirs):
    reps = []
    for d in dirs:
        p = os.path.join(d, 'report.json')
        reps.append(json.load(open(p)) if os.path.exists(p) else report(d))
    def agg(vals):
        v = [x for x in vals if x is not None]
        return {'n': len(v), 'median': r4(statistics.median(v)), 'min': r4(min(v)), 'max': r4(max(v))} if v else {'n': 0}
    flows = set()
    for r in reps:
        flows |= set(((r.get('traffic') or {}).get('flows') or {}).keys())
    out = {'format': 'wssim-report-aggregate 1', 'runs': [r['run_dir'] for r in reps],
           'seeds': [r['run']['seed'] for r in reps],
           'join_ready_median_s': agg([r['join']['ready_median_s'] for r in reps]),
           'join_last_ready_s': agg([r['join']['last_ready_s'] for r in reps]),
           'max_hops': agg([r['topology']['max_hops'] for r in reps]),
           'retries_per_frame': agg([r['network']['retries_per_frame'] for r in reps]),
           'queue_drops': agg([r['network']['queue_drops'] for r in reps]),
           'flows': {}}
    for fl in sorted(flows):
        fs = [((r.get('traffic') or {}).get('flows') or {}).get(fl) for r in reps]
        fs = [f for f in fs if f]
        hops = sorted({h for f in fs for h in f['rtt_ms_by_hops']}, key=int)
        out['flows'][fl] = {'pdr': agg([f['pdr'] for f in fs]),
                            'rtt_mean_ms': agg([f['rtt_ms'].get('mean') for f in fs]),
                            'rtt_p95_ms': agg([f['rtt_ms'].get('p95') for f in fs]),
                            'rtt_mean_ms_by_hops': {h: agg([f['rtt_ms_by_hops'].get(h, {}).get('mean') for f in fs])
                                                    for h in hops}}
    caps = [((r.get('traffic') or {}).get('saturation') or {}).get('capacity_offered_pps') for r in reps]
    out['capacity_offered_pps'] = agg(caps)
    sc = [p.get('capacity', {}).get('capacity_pps') for r in reps if r.get('scenario')
          for p in r['scenario']['summary']['phases'] if p.get('capacity')]
    if sc:
        out['scenario_capacity_pps'] = agg(sc)
    pw = [r['power']['totals']['routers_mean_ma'] for r in reps if r.get('power')]
    if pw:
        out['power_routers_mean_ma'] = agg(pw)
    return out


def main(argv):
    args, opts = [], {'--top': '5', '--pdr-min': '0.99', '--latency-factor': '3', '--tail': '20', '--window': None}
    it = iter(argv)
    quiet = agg = False
    out = None
    for a in it:
        if a in ('-h', '--help'):
            print(__doc__)
            return 0
        if a == '--quiet':
            quiet = True
        elif a == '--aggregate':
            agg = True
        elif a == '--json':
            out = next(it)
        elif a in opts:
            opts[a] = next(it)
        else:
            args.append(a)
    if not args:
        print(__doc__, file=sys.stderr)
        return 2
    if agg:
        rep = aggregate(args)
        js = json.dumps(rep, indent=1)
        if out:
            open(out, 'w').write(js + '\n')
        if not quiet:
            print(js)
        return 0
    rep = report(args[0], int(opts['--top']), float(opts['--pdr-min']), float(opts['--latency-factor']),
                 float(opts['--tail']), float(opts['--window']) if opts.get('--window') else None)
    out = out or os.path.join(args[0], 'report.json')
    with open(out, 'w') as f:
        json.dump(rep, f, indent=1)
    if not quiet:
        print(summary(rep))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
