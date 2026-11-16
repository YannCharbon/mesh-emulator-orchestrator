#!/usr/bin/env python3
"""Measurement scenarios of the emulator (docs/SCENARIOS.md).

A scenario is a JSON file: an ordered list of phases ("Start when [trigger];
set the load; do [actions]; measure [set]; end when [condition] or after
[cap]"), or of building blocks ("use": "probe", "capacity", ...) that expand
to phases. This program checks a scenario, previews it, prints its JSON
Schema, and drives a run with it.

usage:
  scenario.py --check FILE          errors as JSON: {"ok": bool, "errors": [{"path", "message"}]}
  scenario.py --dry FILE            the expanded phases and an upper bound of the virtual duration (JSON)
  scenario.py --schema              the JSON Schema of a scenario file (titles, descriptions, units, defaults)
  scenario.py --blocks              the building blocks and their parameters (JSON)
  scenario.py drive --ctl SOCK --run DIR --scenario FILE [--programs FILE]
                                    drive a run (sim_utils/vtime/run.sh SCENARIO=FILE does it)

Architecture: the run's conductor (wssimserver --vtime-control=SOCK) pauses
virtual time whenever this driver has not allowed it to go further; at each
pause every node is idle and its log is written up to that time. The driver
reads the logs (sim_utils/runlog.py), evaluates triggers and conditions, and
sends commands: the application load and statistics requests to the nodes,
interferers, link cuts and node failures to the conductor. Decisions are taken
only at virtual times the scenario fixes (phase boundaries, polls every
poll_s), from data that depends only on the seed: a run is reproducible from
its seed and its scenario file.

Standard library only.
"""
import json, math, os, random, re, socket, subprocess, sys, time, traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runlog  # noqa: E402

from scenario_schema import (FORMAT, METRICS, SAMPLED, BLOCKS, schema, check, dry, expand,  # noqa: E402
                             default_op, PER_NODE, RANK_METRICS, RANKED_PICKS, REPORT_WINDOW_S,
                             RANK_ALIASES, fmt_value)

RESULT_FORMAT = 'wssim-scenario-result 1'

# --------------------------------------------------------------------- driver


class Abort(Exception):
    """The scenario cannot go on: an error (the run fails)."""


class Stop(Exception):
    """The scenario ends as it says (on_timeout: abort): not an error."""


class Skip(Exception):
    """The phase cannot act (no node selected): it is skipped, with its reason."""


# The per-node value of each metric that has one (scenario_schema.PER_NODE)
PER_NODE_KEY = {'pdr': 'pdr', 'loss': 'loss', 'rtt.mean_ms': 'rtt_mean_ms', 'rtt.p95_ms': 'rtt_p95_ms',
                'rtt.max_ms': 'rtt_max_ms', 'queue.mean_max': 'queue_mean', 'queue.drops': 'drops',
                'mac.retries': 'retries', 'mac.retries_per_frame': 'retries_per_frame', 'mac.give_up': 'give_up',
                'medium.collisions': 'collisions', 'medium.collision_share': 'collision_share',
                'power.avg_ma': 'power_ma'}
# rank_by -> the per-node key
RANK_KEY = {'queue.mean': 'queue_mean', 'queue.max': 'queue_max', 'rtt.mean_ms': 'rtt_mean_ms',
            'rtt.max_ms': 'rtt_max_ms', 'mac.retries_per_frame': 'retries_per_frame', 'mac.retries': 'retries',
            'medium.collisions': 'collisions', 'mac.collisions': 'collisions', 'loss': 'loss'}
DISRUPTING = ('kill', 'restart', 'leave', 'cut_links', 'degrade_links')


def compress_ids(ids):
    ids = sorted(set(ids))
    out, i = [], 0
    while i < len(ids):
        j = i
        while j + 1 < len(ids) and ids[j + 1] == ids[j] + 1:
            j += 1
        out.append(str(ids[i]) if i == j else f'{ids[i]}-{ids[j]}')
        i = j + 1
    return ','.join(out)


class Driver:
    def __init__(self, ctl, rundir, doc, programs=None, log=sys.stderr):
        self.doc = doc
        self.phases = expand(doc)
        self.settings = doc.get('settings', {})
        self.poll = self.settings.get('poll_s', 5)
        self.drain = self.settings.get('drain_s', 10)
        self.dir = rundir
        self.run = runlog.Run(rundir)
        self.log = log
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        for _ in range(600):
            try:
                self.sock.connect(ctl)
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise SystemExit(f'scenario: cannot connect to {ctl}')
        self.rf = self.sock.makefile('r')
        self.now = 0.0
        self.ended = None
        self.results = {}           # phase name -> metrics (for relative_to, rate_of)
        self.labels = {}            # label -> node IDs
        self.record = {'format': RESULT_FORMAT, 'scenario': doc.get('name'), 'phases': [], 'steps': {},
                       'events': []}
        self.pending = []           # (t, description, function) undo actions
        self.step = 0
        self.load = None            # the load settings in force (dict) or None
        self.load_args = None       # their command
        self.killed = set()
        self.programs = {}
        self.instances = {}
        self.children = []
        if programs and os.path.exists(programs):
            for line in open(programs):
                k, _, cmd = line.rstrip('\n').partition(' ')
                if k.isdigit():
                    self.programs[int(k)] = cmd
        self.seed = 1
        try:
            m = re.search(r'seed=(\d+)', open(os.path.join(rundir, 'run_info.txt')).read())
            self.seed = int(m.group(1)) if m else 1
        except OSError:
            pass
        self.power = os.path.exists(os.path.join(rundir, 'run_info.txt')) and \
            'power=' in open(os.path.join(rundir, 'run_info.txt')).read()
        self.positions = self._positions()
        self.need_net = False
        self.graph = None           # hearing graph {node: {neighbour: dBm}}, asked once
        self.cut = set()            # (src, dst) links cut by the scenario
        self.min_samples = self.settings.get('min_samples', 100)
        self.min_node_samples = self.settings.get('min_node_samples', 5)
        self.rank_sent = None
        self.off = {}               # node -> [[t_off, t_on or None]]: when the scenario had it switched off
        self.parent_min_dbm = self.settings.get('parent_min_dbm', -80)
        self.seq = 0                # node commands: sequence of their acknowledgements
        self.pending_acks = {}      # seq -> (nodes expected, command)
        self.disruptions = []       # every disruption, tracked until repaired or the end
        self.last_track = None
        self.restart_times = {}     # node -> virtual times it was started again
        self.window_s = (doc.get('report') or {}).get('window_s', REPORT_WINDOW_S)
        self.next_tick = self.window_s
        self.snaps = []             # (t, power snapshot) of every snapshot
        self.ctr_samples = []       # (t, counters with "_rx") of every counters() call
        self.step_off = {}          # step name -> the nodes it switched off
        self.skipped_steps = {}     # step index -> reason
        self.last_sel = None
        self.plan = dry(doc)
        self.record['user_steps'] = [{'step': st['step'], 'name': st['name'], 'title': st['title'],
                                      'use': st['use'], 'state': 'pending', 'phases': [], 't_start_s': None,
                                      't_end_s': None} for st in self.plan['steps']]
        self.record['window_s'] = self.window_s

    # ------------------------------------------------------------ protocol

    def cmd(self, line):
        self.sock.sendall((line + '\n').encode())
        r = self.rf.readline()
        if not r:
            raise Abort('the conductor closed the control connection')
        r = r.rstrip('\n')
        if r.startswith('ended'):
            self.ended = r
            raise Abort('the run ended: ' + r)
        return r

    def node(self, ids, text, ask=False):
        """A command to Nanostack programs (ids: "all" or a list), with a
        sequence number: each answers "CTL1 ok|err <seq>", checked at the next
        pause. ask: a measurement request, whose wakes are not charged."""
        self.seq += 1
        r = self.cmd(f'{"ask" if ask else "node"} {ids} #{self.seq} {text}')
        if not r.startswith('ok '):
            raise Abort(f'conductor refused "{text[:80]}": {r}')
        self.pending_acks[self.seq] = (int(r.split()[1]), text)
        return int(r.split()[1])

    def check_acks(self):
        for seq, (n, text) in list(self.pending_acks.items()):
            got = [(nid, ok, why) for (nid, sq), (ok, why) in self.run.acks.items() if sq == seq]
            bad = [(nid, why) for nid, ok, why in got if not ok]
            if bad:
                raise Abort(f'node command "{text[:80]}" failed: ' +
                            '; '.join(f'node {nid}: {why}' for nid, why in bad[:5]))
            if len(got) < n:
                raise Abort(f'node command "{text[:80]}": {len(got)} of {n} nodes answered')
            del self.pending_acks[seq]

    def run_until(self, t):
        """Let virtual time go to t (s); the logs are then read, and the
        node commands sent before checked."""
        us = int(round(t * 1e6))
        r = self.cmd(f'run_until {max(us, int(round(self.now * 1e6)))}')
        if not r.startswith('at '):
            raise Abort('unexpected answer: ' + r)
        self.now = int(r.split()[1]) / 1e6
        self.run.update()
        self.check_acks()

    def open_disruptions(self):
        return [d for d in self.disruptions if d.get('open')]

    def advance(self, t):
        """Go to t, doing on the way the undo actions due and, while a
        disruption is open, a poll every poll_s that follows its repair."""
        while True:
            nxt = t
            due = [p for p in self.pending if p[0] <= t]
            if due:
                nxt = min(nxt, min(p[0] for p in due))
            if self.open_disruptions():
                nxt = min(nxt, (self.last_track or self.now) + self.poll)
            nxt = min(nxt, self.next_tick)
            if nxt > self.now:
                self.run_until(nxt)
            if self.now >= self.next_tick - 1e-9:
                self.sample('win')
                while self.next_tick <= self.now + 1e-9:
                    self.next_tick += self.window_s
            for p in sorted([p for p in self.pending if p[0] <= self.now + 1e-9], key=lambda x: x[0]):
                self.pending.remove(p)
                self.note(f'undo: {p[1]}')
                p[2]()
            if self.open_disruptions() and self.now >= (self.last_track or self.now) + self.poll - 1e-9:
                self.track()
            if self.now >= t - 1e-9:
                return

    def refresh(self, net=True, tree=False):
        """Ask the nodes for their NET1 line (and the border router for its
        table) now, and read them. These requests are not charged in the
        power ledger (the conductor's "ask")."""
        if net:
            self.node('all', 'stats', ask=True)
        if tree:
            self.node('0', 'tree', ask=True)
        self.run_until(self.now)
        if self.open_disruptions():
            self.track(refreshed=True)

    def note(self, text):
        self.record['events'].append({'t_s': round(self.now, 6), 'text': text})
        try:
            self.cmd('log ' + text.replace('\n', ' '))
        except Abort:
            raise
        print(f'scenario {self.now:9.3f} s: {text}', file=self.log, flush=True)

    def counters(self):
        """The conductor's totals so far, with "_rx": per receiver (heard,
        collisions, ok) (central medium)."""
        r = self.cmd('counters')
        out = {}
        for kv in r.split()[1:]:
            k, _, v = kv.partition('=')
            try:
                out[k] = int(v)
            except ValueError:
                pass
        rx = {}
        r = self.cmd('rxcounters')
        if r.startswith('ok'):
            for x in r.split()[1:]:
                f = x.split(':')
                if len(f) == 4:
                    rx[int(f[0])] = (int(f[1]), int(f[2]), int(f[3]))
        out['_rx'] = rx
        self.ctr_samples.append((self.now, out))
        return out

    def ctr_at(self, t):
        """The latest counters taken at or before t (None: none)."""
        best = None
        for tt, c in self.ctr_samples:
            if tt <= t + 1e-9:
                best = c
            else:
                break
        return best

    def sample(self, tag):
        """A sample of every series source now: the nodes' NET1 lines (not
        charged), the medium's counters, a power snapshot; one line in
        series_samples.jsonl (report.json "series")."""
        self.node('all', 'stats', ask=True)
        self.run_until(self.now)
        c = self.counters()
        self._snap(f'{tag}:{self.now:.3f}')
        with open(os.path.join(self.dir, 'series_samples.jsonl'), 'a') as f:
            f.write(json.dumps({'t_s': round(self.now, 6), 'tag': tag,
                                'counters': {k: v for k, v in c.items() if k != '_rx'},
                                'rx': {str(k): v for k, v in c['_rx'].items()}}) + '\n')

    def power_snap(self, tag):
        if self.power:
            self.cmd(f'power snap {os.path.join(self.dir, "power_snap.jsonl")} {tag}')

    # ------------------------------------------------------------ topology

    def _positions(self):
        try:
            info = open(os.path.join(self.dir, 'run_info.txt')).read()
        except OSError:
            return {}
        m = re.search(r'--positions[= ](\S+)', info)
        if not m:
            return {}
        pos = {}
        try:
            for line in open(m.group(1)):
                line = line.split('#')[0].split()
                if len(line) >= 4:
                    floor = int(line[4]) if len(line) >= 5 else round(float(line[3]) / 3.5)
                    pos[int(line[0])] = (float(line[1]), float(line[2]), float(line[3]), floor)
        except (OSError, ValueError):
            return {}
        return pos

    def topology(self):
        """Parents (from fresh NET1 lines), depths, children of the running routers."""
        self.refresh(net=True, tree=True)
        par = {n: p for n, p in self.run.parents(self.now).items() if n not in self.killed}
        return par, runlog.depths(par), runlog.children(par)

    def select(self, sel, why_prefix=''):
        """Resolve a selector now: (node IDs, why)."""
        par, dep, ch = self.topology()
        routers = [n for n in self.run.routers() if n not in self.killed]
        k = sel.get('select', 'all')
        rng = random.Random(f'{self.seed}:{self.now:.6f}:{json.dumps(sel, sort_keys=True)}')
        if k == 'id':
            cand, why = [sel['id']], f'node {sel["id"]}'
        elif k == 'ids':
            cand, why = list(sel['ids']), 'nodes ' + compress_ids(sel['ids'])
        elif k == 'all':
            cand, why = routers, 'every router'
        elif k in ('hop', 'br_children'):
            h = sel.get('hop', 1) if k == 'hop' else 1
            cand, why = [n for n in routers if dep.get(n) == h], f'routers at hop {h}'
        elif k == 'ancestor':
            n, path = sel['of'], []
            while n is not None and n != 0 and n not in path:
                path.append(n)
                n = par.get(n)
            cand = [n for n in path if dep.get(n) == sel['hop']]
            why = f'the hop-{sel["hop"]} node on the path of node {sel["of"]}'
        elif k == 'storey':
            cand = [n for n in routers if self.positions.get(n, (0, 0, 0, None))[3] == sel['storey']]
            why = f'routers on storey {sel["storey"]}'
        elif k == 'area':
            x0, y0, x1, y1 = sel['box']
            cand = [n for n in routers if n in self.positions and
                    min(x0, x1) <= self.positions[n][0] <= max(x0, x1) and
                    min(y0, y1) <= self.positions[n][1] <= max(y0, y1)]
            why = f'routers in the area {sel["box"]}'
        elif k == 'label':
            cand, why = list(self.labels.get(sel['label'], [])), f'the nodes labelled {sel["label"]}'
        elif k == 'step':
            cand, why = list(self.step_off.get(sel['step'], [])), f'the nodes step {sel["step"]} switched off'
        else:
            cand, why = [], 'nothing'
        cand = sorted(set(cand))
        pick, count = sel.get('pick', 'top' if 'rank_by' in sel else 'all'), sel.get('count', 1)
        sub = {n: len(runlog.subtree(par, n)) for n in cand}
        self.last_sel = {'candidates': len(cand)}
        if pick in RANKED_PICKS:
            rb = RANK_ALIASES.get(sel['rank_by'], sel['rank_by'])
            vals, win = self.rank_values(rb, sel.get('rank_window_s', 60), cand, par, ch)
            ranked = sorted((n for n in cand if n != 0 and vals.get(n) is not None), key=lambda n: (-vals[n], n))
            unit, title = RANK_METRICS[rb][0], RANK_METRICS[rb][1]
            if pick == 'top':
                cand = ranked[:count]
                why += f', the {count} highest by {title}'
            elif pick == 'top_fraction':
                cand = ranked[:max(1, int(sel['fraction'] * len(ranked)))] if ranked else []
                why += f', the highest {100 * sel["fraction"]:g} % by {title}'
            else:
                cand = [n for n in ranked if vals[n] >= sel['value']]
                why += f', {title} at least {fmt_value(sel["value"], unit)}'
            why += f' ({len(ranked)} ranked'
            why += (': ' + ', '.join(f'{n}: {vals[n]:.4g}' for n in cand) + ')') if cand else ', none picked)'
            self.last_sel.update({'rank_by': rb, 'rank_unit': unit, 'rank_window': win, 'ranked': len(ranked),
                                  'chosen': [dict({'node': n, 'value': round(vals[n], 6)},
                                                  **({'sent': self.rank_sent.get(n)} if self.rank_sent else {}))
                                             for n in cand]})
            return sorted(cand), why_prefix + why
        if pick == 'largest_subtree':
            cand = sorted(cand, key=lambda n: (-sub[n], n))[:count]
            why += ', largest subtree (' + ', '.join(f'{n}: {sub[n]} below' for n in cand) + ')'
        elif pick == 'most_children':
            cand = sorted(cand, key=lambda n: (-len(ch.get(n, [])), n))[:count]
            why += ', most children (' + ', '.join(f'{n}: {len(ch.get(n, []))}' for n in cand) + ')'
        elif pick == 'random':
            cand = sorted(rng.sample(cand, min(count, len(cand))))
            why += f', {len(cand)} drawn from the seed'
        elif pick == 'fraction':
            cand = sorted(n for n in cand if rng.random() < sel.get('fraction', 1))
            why += f', a fraction {sel.get("fraction")} drawn from the seed'
        elif pick == 'first':
            cand = cand[:count]
            why += ', the first'
        self.last_sel['chosen'] = [{'node': n, 'value': None} for n in cand]
        return cand, why_prefix + why

    def rank_values(self, rb, w, cand, par, ch):
        """{node: value} of a ranking metric for the candidates, measured over
        the w seconds before now (packets: sent in the w seconds before the
        last drain_s, whose echoes may still come); (values, [t_from, t_to])."""
        if rb == 'subtree':
            return {n: len(runlog.subtree(par, n)) for n in cand}, [self.now, self.now]
        if rb == 'children':
            return {n: len(ch.get(n, [])) for n in cand}, [self.now, self.now]
        self.rank_sent = None
        if rb in ('rtt.mean_ms', 'rtt.max_ms', 'loss'):
            t_to = max(self.now - self.drain, 0)
            t_from = max(t_to - w, 0)
            pn = self.per_node(t_from, t_to, self.now, {'traffic'})
            self.rank_sent = {n: x.get('sent') for n, x in pn.items()}
        elif rb in ('medium.collisions', 'mac.collisions'):
            t_from = max(self.now - w, 0)
            t_to = self.now
            pn = self.per_node(t_from, t_to, self.now, {'medium'}, self.ctr_at(t_from), self.counters())
        else:
            t_from = max(self.now - w, 0)
            t_to = self.now
            pn = self.per_node(t_from, t_to, self.now, {'queues'})
        key = RANK_KEY[rb]
        return ({n: pn[n][key] for n in cand if n in pn and pn[n].get(key) is not None},
                [round(t_from, 6), round(t_to, 6)])

    def per_node(self, t_from, t_to, t_eval, sets, ctr_a=None, ctr_b=None, snap_a=None, snap_b=None):
        """Per node, the per-node values of the measurement sets over
        [t_from, t_to) (docs/SCENARIOS.md §4.3): packets it originated,
        retries, queues, collisions at it as a receiver, power."""
        pn = {}
        if 'traffic' in sets:
            for n, a in runlog.node_packets(self.run, t_from, t_to, t_eval).items():
                r = a['rtt_ms']
                x = pn.setdefault(n, {'sent': a['sent'], 'lost': a['lost']})
                x.update(sent=a['sent'], lost=a['lost'])
                if a['sent'] >= self.min_node_samples:      # too few packets: no value
                    x.update({'loss': a['loss'], 'pdr': a['pdr'], 'rtt_mean_ms': r.get('mean'),
                              'rtt_p95_ms': r.get('p95'), 'rtt_max_ms': r.get('max')})
        if 'queues' in sets:
            nd = runlog.net_delta(self.run, t_from, t_eval)
            peaks = runlog.queue_peaks(self.run, t_from, t_eval)
            for n, d in nd.items():
                pn.setdefault(n, {}).update({'retries': d['retries'], 'retries_per_frame': d['retries_per_frame'],
                                             'uc_frames': d['uc_frames'], 'give_up': d['give_up'],
                                             'drops': d['red_drop'], 'queue_mean': d['mac_q_mean'] + d['al_q_mean'],
                                             'queue_max': peaks.get(n)})
        if 'medium' in sets and ctr_b is not None:
            ra = (ctr_a or {}).get('_rx', {})
            for n, (h, c, ok) in ctr_b.get('_rx', {}).items():
                h0, c0, _ = ra.get(n, (0, 0, 0))
                pn.setdefault(n, {}).update({'heard': h - h0, 'collisions': c - c0,
                                             'collision_share': (c - c0) / (h - h0) if h > h0 else None})
        if 'power' in sets and snap_a and snap_b:
            span = (snap_b['t_us'] - snap_a['t_us']) / 1e6
            a = {x[0]: x for x in snap_a['nodes']}
            for x in snap_b['nodes']:
                # a node switched off meanwhile is not counted (the ledger
                # charges it as asleep, radio off)
                if x[0] in a and span > 0 and not self.was_off(x[0], t_from, t_to):
                    pn.setdefault(x[0], {})['power_ma'] = (x[1] - a[x[0]][1]) / 1e3 / span
        return pn

    def was_off(self, n, t_from, t_to):
        """The scenario had node n switched off during part of [t_from, t_to]."""
        return any(a < t_to - 1e-9 and (b is None or b > t_from + 1e-9) for a, b in self.off.get(n, []))

    @staticmethod
    def worst_of(pn):
        """Per metric with a per-node value, the worst router: {metric: {node, value}}."""
        out = {}
        for metric, key in PER_NODE_KEY.items():
            vals = [(v[key], n) for n, v in pn.items() if n != 0 and v.get(key) is not None]
            if not vals:
                continue
            low = METRICS[metric][3] == 'low'
            v, n = (min if low else max)(vals, key=lambda x: (x[0], -x[1]) if not low else (x[0], x[1]))
            out[metric] = {'node': n, 'value': round(v, 6)}
            if metric in ('pdr', 'loss') or metric.startswith('rtt.'):
                out[metric]['sent'] = pn[n].get('sent')
        return out

    # --------------------------------------------------------------- load

    def apply_load(self, spec, phase_name, iteration=None, rate=None):
        """Send the load settings to every node. rate (packets/s) overrides."""
        if spec == 'off':
            self.node('all', 'load stop')
            self.load = None
            self.load_args = None
            self.note(f'load off')
            return
        base = dict(self.settings.get('load', {}))
        base.update(spec)
        flows = base.get('flows', ['up'])
        members, why = self.select(base.get('nodes', {'select': 'all'}))
        # Only routers with a route now take part (a parent chain to the border
        # router, from the samples select() just asked for): one cut off by a
        # failure would only lower the delivery ratio
        par = {n: p for n, p in self.run.parents(self.now).items() if n not in self.killed}
        dep = runlog.depths(par)
        if any(v is not None for k, v in dep.items() if k):
            left_out = [n for n in members if dep.get(n) is None]
            members = [n for n in members if dep.get(n) is not None]
        else:
            left_out = []
        m = max(len(members), 1)
        if rate is None:
            if 'rate_of' in base:
                ro = base['rate_of']
                ref = self.results.get(ro['phase'], {})
                v = ref.get(ro.get('metric', 'capacity_pps'))
                if v is None and ro.get('metric', 'capacity_pps') != 'capacity_pps':
                    v = self.metric_value(ref.get('metrics', {}), ro['metric'], ref.get('flow', 'up'))
                if v is None:
                    raise Abort(f'phase {ro["phase"]} has no {ro.get("metric", "capacity_pps")}')
                rate = v * ro.get('factor', 1)
            elif 'rate_pps' in base:
                rate = base['rate_pps']
            else:
                rate = m / base.get('interval_s', 30)
                if base.get('max_rate_pps'):
                    rate = min(rate, base['max_rate_pps'])
        if not rate or rate <= 0 or not math.isfinite(rate):
            raise Abort(f'load rate {rate} packets/s: nothing to send')
        interval = m / rate
        self.step = (self.step + 1) % 256
        args = [f'flows={",".join(flows)}', f'interval_s={interval:.6f}',
                f'down_interval_s={interval:.6f}', f'mcast_interval_s={1 / rate:.6f}',
                f'p2p_interval_s={interval:.6f}', f'payload={base.get("payload_b", 64)}',
                f'dist={base.get("dist", "exp")}', f'echo={"true" if base.get("echo", True) else "false"}',
                'jitter_s=-1', 'after_ready_s=-1',
                f'nodes={"all" if base.get("nodes") is None and not left_out else compress_ids(members)}',
                f'step={self.step}']
        self.load_args = ' '.join(args)
        self.node('all', 'load set ' + self.load_args)
        self.load = {'flows': flows, 'rate_pps': rate, 'interval_s': interval, 'members': len(members),
                     'left_out': left_out, 'member_ids': members,
                     'payload_b': base.get('payload_b', 64), 'dist': base.get('dist', 'exp'), 'spec': base}
        self.record['steps'][str(self.step)] = {'phase': phase_name, 'iteration': iteration, 't_s': self.now,
                                               'rate_pps': rate, 'interval_s': interval, 'flows': flows,
                                               'members': len(members), 'payload_b': base.get('payload_b', 64)}
        self.note(f'load {",".join(flows)} {rate:.4g} packets/s per flow ({len(members)} routers, '
                  f'one every {interval:.4g} s each), step tag {self.step}'
                  + (f'; without a route, left out: {compress_ids(left_out)}' if left_out else ''))

    # ------------------------------------------------------------ actions

    def do_action(self, a, rec, pre=None):
        do = a['do']
        out = {'do': do}
        if do == 'mark':
            self.note('mark: ' + a.get('text', ''))
        elif do == 'interferer_add':
            spec = a['spec'] + (f',name={a["name"]}' if a.get('name') and 'name=' not in a['spec'] else '')
            r = self.cmd('intf add ' + spec)
            out['result'] = r
            if not r.startswith('ok'):
                raise Abort(f'interferer refused by the conductor: {spec}: {r}')
            if r.startswith('ok') and a.get('for_s'):
                nm = r.split()[1]
                self.pending.append((self.now + a['for_s'], f'interferer {nm} removed',
                                     lambda nm=nm: self.cmd('intf remove ' + nm)))
            self.note(f'interferer added: {spec} ({r})')
        elif do == 'interferer_remove':
            r = self.cmd('intf remove ' + a.get('name', 'all'))
            out['result'] = r
            self.note(f'interferer {a.get("name", "all")} removed ({r})')
        else:
            if do == 'partition':
                ga, wa = self.select(a['a'])
                gb, wb = self.select(a['b'])
                nodes, why = ga + gb, f'a: {wa}; b: {wb}'
            elif pre:
                nodes, why, info = pre
                out.update({k: v for k, v in info.items() if k in ('chosen', 'rank_by', 'rank_unit', 'rank_window',
                                                                   'candidates', 'ranked')})
            else:
                nodes, why = self.select(a['nodes'])
                out.update({k: v for k, v in (self.last_sel or {}).items() if k in ('chosen', 'rank_by', 'rank_unit',
                                                                                    'rank_window', 'candidates',
                                                                                    'ranked')})
            out.update({'nodes': nodes, 'why': why})
            if do in ('kill', 'leave'):
                off = self.step_off.setdefault(rec.get('step_name'), [])
                off += [n for n in nodes if n not in off]
            if a.get('label'):
                self.labels[a['label']] = nodes
            par, dep, ch = self.topology()
            before = dict(par)
            if do in ('kill', 'restart'):
                for n in nodes:
                    self._kill(n)
                if do == 'restart':
                    for n in nodes:
                        self._start(n)
                elif a.get('for_s'):
                    self.pending.append((self.now + a['for_s'], f'restart {compress_ids(nodes)}',
                                         lambda nodes=nodes: [self._start(n) for n in nodes]))
            elif do == 'leave':
                self.node(compress_ids(nodes), 'iface down')
                if a.get('for_s'):
                    def up(nodes=nodes):
                        self.node(compress_ids(nodes), 'iface up')
                        self.release(nodes)
                    self.pending.append((self.now + a['for_s'], f'interface up {compress_ids(nodes)}', up))
            elif do in ('cut_links', 'partition', 'degrade_links'):
                pairs = []
                if do == 'partition':
                    pairs = [(x, y) for x in ga for y in gb if x != y]
                else:
                    peers = set(self.select(a['peers'])[0]) if a.get('peers') else None
                    for n in nodes:
                        pairs += [(n, m) for m in self.neighbors(n) if peers is None or m in peers]
                undo = []
                for x, y in pairs:
                    if do == 'degrade_links':
                        old = self._link_rssi(x, y)
                        if self.cmd(f'link {x} {y} {a["rssi_dbm"]}').startswith('ok') and old is not None:
                            undo.append(f'link {x} {y} {old}')
                    else:
                        self.cmd(f'cut {x} {y}')
                        self.cmd(f'cut {y} {x}')
                        self.cut |= {(x, y), (y, x)}
                        undo += [f'uncut {x} {y}', f'uncut {y} {x}']
                out['links'] = len(pairs)
                if a.get('for_s'):
                    def restore(undo=undo, pairs=pairs):
                        for u in undo:
                            self.cmd(u)
                        for x, y in pairs:
                            self.cut -= {(x, y), (y, x)}
                    self.pending.append((self.now + a['for_s'], f'{do} undone ({len(pairs)} links)', restore))
            self.note(f'{do}: {why}: nodes {compress_ids(nodes)}')
            if do in ('kill', 'leave', 'cut_links', 'partition', 'degrade_links'):
                failed = set(nodes) if do in ('kill', 'leave') else set()
                # cut off: the nodes below the failed ones; with links cut or
                # degraded, the nodes themselves too
                affected = set() if do in ('kill', 'leave') else set(nodes)
                for n in nodes:
                    affected.update(runlog.subtree(before, n))
                if do == 'partition':
                    side = set(ga) if 0 not in ga else set(gb)
                    affected = {n for n in side if n != 0}
                affected -= failed
                strong, weak = self.reachable(failed)
                isolated = sorted(n for n in affected if n not in weak)
                marginal = sorted(n for n in affected if n in weak and n not in strong)
                affected = {n for n in affected if n in strong}
                d = {'t_s': self.now, 'phase': rec['name'], 'do': do, 'failed': sorted(failed),
                     'down': sorted(failed),    # the failed nodes not back yet
                     'affected': sorted(affected), 'marginal': marginal, 'isolated': isolated,
                     'parents_before': {str(k): v for k, v in before.items()},
                     'depths_before': {str(k): v for k, v in dep.items() if v is not None},
                     'detect_t_s': None, 'repair_t_s': None, 'repair_e2e_t_s': None, 'orphan_s': {},
                     'marginal_repaired_s': {}, 'open': True}
                self.disruptions.append(d)
                self.last_track = self.now
                out['affected'] = sorted(affected)
                out['marginal'] = marginal
                out['isolated'] = isolated
                rec.setdefault('disruptions', []).append(d)
        rec.setdefault('actions', []).append(out)

    def release(self, nodes):
        """Failed nodes back (restarted, interface up): the repair of the
        disruptions in progress may then go through them again."""
        for d in self.open_disruptions():
            back = [n for n in nodes if n in d['down']]
            d['down'] = [n for n in d['down'] if n not in back]
            if back:
                d.setdefault('back', []).extend({'node': n, 't_s': round(self.now, 3)} for n in back)

    def neighbors(self, n):
        """{neighbour: link level (dBm, the weaker direction)} in the hearing graph."""
        if self.graph is None:
            self.graph = {}
        if n not in self.graph:
            r = self.cmd(f'neighbors {n}')
            self.graph[n] = {int(x.split(':')[0]): float(x.split(':')[1]) for x in r.split()[1:]}
        return self.graph[n]

    def reachable(self, failed=()):
        """(strong, weak): the nodes that can still reach the border router
        through the hearing graph, without the failed, stopped or cut parts,
        over links above parent_min_dbm (strong: a new parent is possible) or
        over any link (weak)."""
        for n in sorted(set(self.run.nodes) | {0}):
            self.neighbors(n)
        down = set(failed) | self.killed
        def bfs(min_dbm):
            seen, todo = {0}, [0]
            while todo:
                x = todo.pop()
                for y, dbm in self.graph.get(x, {}).items():
                    if y in seen or y in down or (y, x) in self.cut or (x, y) in self.cut:
                        continue
                    if min_dbm is not None and dbm <= min_dbm:
                        continue
                    seen.add(y)
                    todo.append(y)
            return seen
        return bfs(self.parent_min_dbm), bfs(None)

    def _kill(self, n):
        r = self.cmd(f'kill {n}')
        if r.startswith('ok'):
            self.killed.add(n)
            self.off.setdefault(n, []).append([round(self.now, 6), None])
            # no more packets to or from it (down streams, p2p peers)
            self.node('all', f'load exclude {n}')

    def _start(self, n):
        cmdline = self.programs.get(n)
        if not cmdline:
            self.note(f'restart of node {n}: no command line (programs.txt)')
            return
        base = 'br' if n == 0 else f'node{n}'
        k = 1
        while os.path.exists(os.path.join(self.dir, f'{base}.r{k}.log')):
            k += 1
        self.instances[n] = k
        name = f'{base}.r{k}.log'
        logf = open(os.path.join(self.dir, name), 'w')
        p = subprocess.Popen(cmdline, shell=True, cwd=self.dir, stdout=logf, stderr=subprocess.STDOUT)
        self.children.append((n, name, p))
        r = self.cmd(f'start {n}')
        if r.startswith('ok'):
            self.killed.discard(n)
            if self.off.get(n) and self.off[n][-1][1] is None:
                self.off[n][-1][1] = round(self.now, 6)
            self.restart_times.setdefault(n, []).append(self.now)
            self.release([n])
            self.run.update()
            self.node('all', f'load include {n}')
            # The new instance gets the load in force (its step tag included)
            if self.load_args:
                self.node(str(n), f'load set {self.load_args}')
        self.note(f'node {n} started again ({name}: {r})')

    def _link_rssi(self, x, y):
        try:
            for line in open(os.path.join(self.dir, 'topology.txt')):
                m = re.match(r'(\d+),(\d+):(-?[\d.]+)$', line.strip())
                if m and {int(m.group(1)), int(m.group(2))} == {x, y}:
                    return float(m.group(3))
        except OSError:
            pass
        return None

    # ---------------------------------------------------------- measuring

    def flow_of(self, c):
        if c and c.get('flow'):
            return c['flow']
        fl = (self.load or {}).get('flows') or ['up']
        return 'up' if 'up' in fl else fl[0]

    def window_metrics(self, t_from, t_to, t_eval, sets, ctr_a=None, ctr_b=None, snap_a=None, snap_b=None):
        """Every metric of the measurement sets over packets sent in [t_from, t_to)."""
        m = {'t_from_s': round(t_from, 6), 't_to_s': round(t_to, 6)}
        if 'traffic' in sets:
            m['flows'] = runlog.packet_metrics(self.run, t_from, t_to, t_eval)
        if 'queues' in sets or 'mesh' in sets:
            nd = runlog.net_delta(self.run, t_from, t_eval)
            routers = [n for n in nd if n != 0]
            if 'queues' in sets:
                cca = sum(d['mac_cca_att'] for d in nd.values())
                qm = max(nd.items(), key=lambda kv: kv[1]['mac_q_mean'] + kv[1]['al_q_mean'], default=(None, None))
                m['queues'] = {
                    'mean_max': (qm[1]['mac_q_mean'] + qm[1]['al_q_mean']) if qm[1] else None, 'mean_max_node': qm[0],
                    'now_max': max((d['mac_q_now'] + d['al_q_now'] for d in nd.values()), default=None),
                    'drops': sum(d['red_drop'] for d in nd.values()),
                    'retries_per_frame': runlog.retries_per_frame(nd.values()),
                    'retries': sum(d['retries'] for d in nd.values()),
                    'give_up': sum(d['give_up'] for d in nd.values()),
                    'cca_fail_rate': sum(d['mac_cca_fail'] for d in nd.values()) / cca if cca else None,
                    'per_node': {str(k): {x: (round(y, 4) if isinstance(y, float) else y) for x, y in d.items()
                                          if x not in ('parent', 'rank', 'dag_rank')} for k, d in nd.items()},
                }
            if 'mesh' in sets:
                par = {n: nd[n]['parent'] for n in routers if n not in self.killed}
                dep = runlog.depths(par)
                hops = [dep.get(n) for n in par]
                ch = runlog.children(par)
                m['mesh'] = {
                    'routers': len(par),
                    'joined_fraction': sum(1 for h in hops if h is not None) / len(par) if par else None,
                    'orphans': sum(1 for h in hops if h is None),
                    'max_hops': max((h for h in hops if h is not None), default=None),
                    'hops_histogram': {str(h): hops.count(h) for h in sorted({h for h in hops if h is not None})},
                    'max_children': max((len(v) for v in ch.values()), default=0),
                    'parent_changes': self.parent_changes(t_from, t_eval),
                    'control_messages': {k: sum(nd[n].get('ctl_' + k, 0) for n in nd) for k in ('dis', 'dio', 'dao')},
                    'parents': {str(k): v for k, v in par.items()},
                }
                t_tree, tree = self.run.br_tree(t_eval)
                m['mesh']['br_table_size'] = len(tree) if t_tree is not None else None
        if 'medium' in sets and ctr_a and ctr_b:
            span = max(ctr_b.get('t_us', 0) - ctr_a.get('t_us', 0), 1) / 1e6
            d = {k: ctr_b.get(k, 0) - ctr_a.get(k, 0) for k in ctr_b if k != '_rx'}
            heard = sum(v for k, v in d.items() if k in ('ok', 'noise', 'interference', 'captured', 'below_lock',
                                                         'busy', 'tx_abort', 'left'))
            m['medium'] = {'span_s': span, 'frames': d.get('frames_in'), 'frames_per_s': d.get('frames_in', 0) / span,
                           'airtime_s_per_s': d.get('airtime_us', 0) / 1e6 / span,
                           'loss_share': (heard - d.get('ok', 0)) / heard if heard else None,
                           'collisions': sum(d.get(k, 0) for k in ('interference', 'captured', 'busy')),
                           'collision_share': (sum(d.get(k, 0) for k in ('interference', 'captured', 'busy')) / heard
                                               if heard else None),
                           'outcomes': {k: d[k] for k in ('ok', 'noise', 'interference', 'captured', 'below_lock',
                                                          'busy', 'not_listening', 'tx_abort', 'left') if k in d}}
        if 'power' in sets and snap_a and snap_b:
            span = (snap_b['t_us'] - snap_a['t_us']) / 1e6
            a = {n[0]: n for n in snap_a['nodes']}
            per = {}
            for n in snap_b['nodes']:
                o = a.get(n[0])
                if o is None or span <= 0:
                    continue
                q = [x - y for x, y in zip(n[1:], o[1:])]
                per[str(n[0])] = {'avg_ma': q[0] / 1e3 / span, 'charge_uc': q[0], 'mcu_uc': q[1], 'listen_uc': q[2],
                                  'receive_uc': q[3], 'tx_uc': q[4], 'other_uc': q[5]}
            vals = [v['avg_ma'] for k, v in per.items() if k != '0' and not self.was_off(int(k), t_from, t_to)]
            m['power'] = {'span_s': span, 'avg_ma': sum(vals) / len(vals) if vals else None,
                          'max_ma': max(vals) if vals else None, 'per_node': per}
        pn = self.per_node(t_from, t_to, t_eval, set(sets), ctr_a, ctr_b, snap_a, snap_b)
        if pn:
            m['per_node'] = {str(n): {k: (round(v, 6) if isinstance(v, float) else v) for k, v in x.items()}
                             for n, x in sorted(pn.items())}
            m['worst'] = self.worst_of(pn)
        return m

    def metric_value(self, m, name, flow, hop=None, over='mesh'):
        """A metric of the vocabulary from window metrics m (over worst_node:
        the worst router's value)."""
        if name == 'capacity_pps':
            return m.get('capacity_pps')
        if over == 'worst_node':
            return ((m.get('worst') or {}).get(name) or {}).get('value')
        if name == 'loss':
            p = self.metric_value(m, 'pdr', flow, hop)
            return None if p is None else 1 - p
        top, _, sub = name.partition('.')
        if top in ('pdr', 'rtt', 'latency', 'offered_pps', 'delivered_pps'):
            f = (m.get('flows') or {}).get(flow)
            if not f:
                return None
            if top == 'pdr':
                return f.get('pdr')
            if top in ('offered_pps', 'delivered_pps'):
                return f.get(top)
            st = f['rtt_ms' if top == 'rtt' else 'latency_ms']
            if hop is not None:
                st = f['rtt_ms_by_hops' if top == 'rtt' else 'latency_ms_by_hops'].get(str(hop), {'n': 0})
            key = sub.replace('_ms', '')
            return st.get(key) if st.get('n') else None
        if top == 'queue':
            q = m.get('queues') or {}
            return {'mean_max': q.get('mean_max'), 'now_max': q.get('now_max'), 'drops': q.get('drops')}.get(sub)
        if top == 'mac':
            q = m.get('queues') or {}
            return {'retries_per_frame': q.get('retries_per_frame'), 'give_up': q.get('give_up'),
                    'retries': q.get('retries'),
                    'cca_fail_rate': q.get('cca_fail_rate')}.get(sub)
        if top == 'mesh':
            return (m.get('mesh') or {}).get(sub)
        if top == 'power':
            return (m.get('power') or {}).get(sub)
        if top == 'medium':
            return (m.get('medium') or {}).get(sub)
        return None

    def sets_for(self, c):
        """The measurement sets a condition needs."""
        if not isinstance(c, dict):
            return set()
        if c.get('is') in ('all', 'any'):
            out = set()
            for x in c.get('of', []):
                out |= self.sets_for(x)
            return out
        if c.get('is') == 'metric':
            need = METRICS[c['metric']][2]
            return {'queues' if need == 'queues' else need}
        if c.get('is') in ('formed', 'repaired', 'node_event'):
            return {'mesh'}
        return set()

    def cond(self, c, ref_t, ctx):
        """Evaluate a condition now; returns (bool, detail)."""
        k = c.get('is', 'immediately')
        if k == 'immediately':
            return True, 'immediately'
        if k == 'time':
            return self.now >= c['t_s'] - 1e-9, f'time {c["t_s"]} s'
        if k == 'elapsed':
            return self.now >= ref_t + c['s'] - 1e-9, f'{c["s"]} s elapsed'
        if k in ('all', 'any'):
            res = [self.cond(x, ref_t, ctx) for x in c['of']]
            ok = all(r[0] for r in res) if k == 'all' else any(r[0] for r in res)
            return ok, f'{k}(' + '; '.join(r[1] for r in res if r[0] == ok or k == 'all') + ')'
        if k == 'formed':
            return self.cond_formed(c, ctx)
        if k == 'node_event':
            return self.cond_event(c, ref_t, ctx)
        if k == 'repaired':
            return self.cond_repaired(c)
        if k == 'metric':
            return self.cond_metric(c, ctx)
        return False, 'unknown'

    def cond_formed(self, c, ctx):
        ms = c.get('milestone', 'ready')
        if c.get('nodes'):
            nodes = self.select(c['nodes'])[0]
        else:
            nodes = [n for n in self.run.routers() if n not in self.killed]
        # Joined: the milestone in the node's current instance, and the node in
        # the border router's routing table now (its DAO accepted)
        t_tree, tree = self.run.br_tree(self.now)
        done = [n for n in nodes if n in self.run.nodes and ms in self.run.nodes[n].ms and n in tree]
        frac = len(done) / len(nodes) if nodes else 0
        ok = frac >= c.get('fraction', 1) - 1e-9
        detail = f'{len(done)}/{len(nodes)} routers {ms} and in the border router\'s table'
        stable = c.get('stable_s', 0)
        if ok and stable:
            par = self.run.parents(self.now)
            hist = ctx.setdefault('parents_hist', [])
            hist.append((self.now, par))
            last_change = ctx.get('last_change', self.now if len(hist) == 1 else None)
            if len(hist) >= 2 and hist[-2][1] != par:
                last_change = self.now
            if last_change is None:
                last_change = hist[0][0]
            ctx['last_change'] = last_change
            ok = self.now - last_change >= stable - 1e-9
            detail += f', parents unchanged for {self.now - last_change:.0f} s'
        return ok, detail

    def cond_event(self, c, ref_t, ctx):
        nodes = ctx.get('event_nodes')
        if nodes is None:
            nodes = ctx['event_nodes'] = self.select(c['nodes'])[0]
        ev = c['event']
        hit = []
        for n in nodes:
            node = self.run.nodes.get(n)
            if not node:
                continue
            if ev == 'joined':
                if any(t >= ref_t for inst, k, t in node.ms_history if k == c.get('milestone', 'operational')):
                    hit.append(n)
            elif ev == 'restarted':
                if any(t >= ref_t - 1e-9 for t in self.restart_times.get(n, [])):
                    hit.append(n)
            else:
                first = self.run.last_net(n, ref_t)
                last = self.run.last_net(n, self.now)
                p0 = first.get('rpl', {}).get('parent') if first else None
                p1 = last.get('rpl', {}).get('parent') if last else None
                if ev == 'parent_changed' and last and p1 != p0 and last['t'] > ref_t:
                    hit.append(n)
                if ev == 'parent_lost' and last and p1 is None and last['t'] > ref_t:
                    hit.append(n)
        return bool(nodes) and len(hit) == len(nodes), f'{ev}: {len(hit)}/{len(nodes)} nodes'

    def cond_repaired(self, c):
        """The last disruption's repair (tracked by track(), whatever the
        conditions): its routers that can be repaired have a route again; with
        e2e, also end to end."""
        if not self.disruptions:
            return True, 'no disruption'
        d = self.disruptions[-1]
        if d.get('open'):
            self.track()
        key = 'repair_e2e_t_s' if c.get('e2e') else 'repair_t_s'
        ok = d[key] is not None
        n = len(d['affected'])
        return ok, (f'{n}/{n} cut-off routers have a route again' + (' end to end' if c.get('e2e') else '')
                    if ok else f'{d.get("routed", 0)}/{n} cut-off routers have a route again'
                    + (f', {d.get("e2e_ok", 0)}/{n} end to end' if c.get('e2e') else ''))

    def track(self, refreshed=False):
        """One poll of every open disruption: detection, routes, end-to-end
        repair; a disruption closes when both repairs are seen."""
        if not refreshed:
            self.node('all', 'stats', ask=True)
            self.node('0', 'tree', ask=True)
            self.run_until(self.now)
        self.last_track = self.now
        par = {n: p for n, p in self.run.parents(self.now).items() if n not in self.killed}
        dep = runlog.depths(par)
        t_tree, tree = self.run.br_tree(self.now)
        members = set((self.load or {}).get('member_ids', []))
        echoed = self.load and 'up' in self.load['flows'] and self.load.get('spec', {}).get('echo', True)
        for d in self.open_disruptions():
            bad = set(d['down'])
            routed = e2e = 0
            for n in d['affected'] + d['marginal']:
                if n in self.killed:
                    continue
                chain, x, ok = [], n, True
                while x not in (0, None):
                    if x in bad or x in chain:
                        ok = False
                        break
                    chain.append(x)
                    x = par.get(x)
                ok = ok and x == 0 and dep.get(n) is not None
                if n in d['marginal']:
                    if ok:
                        d['marginal_repaired_s'].setdefault(str(n), round(self.now - d['t_s'], 3))
                    continue
                if ok:
                    routed += 1
                    d['orphan_s'].setdefault(str(n), round(self.now - d['t_s'], 3))
                ok_e2e = ok and n in tree and tree.get(n) not in bad
                if ok_e2e and echoed and n in members:
                    ok_e2e = any(t_tx >= d['t_s'] and (1, n, sq) in self.run.echo
                                 for (f, src, sq), (t_tx, *_r) in self.run.tx.items() if f == 1 and src == n)
                e2e += ok_e2e
            d['routed'], d['e2e_ok'] = routed, e2e
            changed = [n for n in d['affected'] + d['marginal'] if str(n) in d['parents_before']
                       and par.get(n) != d['parents_before'][str(n)]]
            if changed and d['detect_t_s'] is None:
                d['detect_t_s'] = round(self.now - d['t_s'], 3)
            if routed == len(d['affected']) and d['repair_t_s'] is None:
                d['repair_t_s'] = round(self.now - d['t_s'], 3)
            if e2e == len(d['affected']) and d['repair_e2e_t_s'] is None:
                d['repair_e2e_t_s'] = round(self.now - d['t_s'], 3)
            if d['repair_t_s'] is not None and d['repair_e2e_t_s'] is not None:
                self.finalize(d)

    def ref_value(self, rel, metric, hop=None):
        ph = self.results.get(rel['phase'])
        if ph is None:
            return None
        name = rel.get('metric', metric)
        if name in ('capacity_pps', 'offered_pps') and name in ph:
            v = ph[name]
        else:
            v = self.metric_value(ph.get('metrics', {}), name, ph.get('flow', 'up'), hop)
        return None if v is None else v * rel.get('factor', 1)

    def compare(self, v, op, thr):
        return {'>=': v >= thr, '>': v > thr, '<=': v <= thr, '<': v < thr, '==': v == thr}[op]

    def cond_metric(self, c, ctx):
        m = ctx.get('metrics')
        if m is None:   # a trailing window, now
            w = c.get('window_s', 60)
            if c['metric'].startswith(SAMPLED):
                # long enough for min_samples packets at the load in force
                w = self.window_for(w, c.get('min_samples', self.min_samples), cap=10)
            t_to = self.now - self.drain
            if t_to - w < ctx.get('t_start', 0) - 1e-9:
                return False, f'{c["metric"]}: window of {w} s not full yet'
            sets = {METRICS[c['metric']][2]}
            if 'queues' in sets or 'mesh' in sets:
                self.refresh(net=True)
            t_from = max(t_to - w, ctx.get('t_start', 0))
            if c.get('over') == 'worst_node' and c['metric'] == 'power.avg_ma':
                sets.add('power')
            ca = self.ctr_at(t_from) if 'medium' in sets else None
            cb = self.counters() if 'medium' in sets else None
            snaps = (None, None)
            if 'power' in sets and self.power:
                snaps = (self.snap_at(t_from), self._snap(f'cond:{self.now:.3f}'))
            m = self.window_metrics(t_from, t_to, self.now, sets, ca, cb, *snaps)
        flow = self.flow_of(c)
        op = c.get('op') or default_op(c['metric'])
        if c['metric'].startswith(SAMPLED):
            # too few packets in the window: not decided yet
            need = c.get('min_samples', self.min_samples)
            sent = ((m.get('flows') or {}).get(flow) or {}).get('sent', 0)
            if sent < need:
                return False, f'{c["metric"]}: {sent} packets in the window, {need} needed'
        hops = [None]
        if c.get('per_hop'):
            f = (m.get('flows') or {}).get(flow, {})
            hops = sorted(int(h) for h in f.get('rtt_ms_by_hops' if c['metric'].startswith('rtt') else
                                                  'latency_ms_by_hops', {}))
        over = c.get('over', 'mesh')
        for h in hops:
            v = self.metric_value(m, c['metric'], flow, h, over)
            thr = self.ref_value(c['relative_to'], c['metric'], h) if c.get('relative_to') else c.get('value')
            if v is None or thr is None:
                continue
            if self.compare(v, op, thr):
                who = f' (worst router: {m["worst"][c["metric"]]["node"]})' if over == 'worst_node' else ''
                return True, f'{c["metric"]}{"" if h is None else f" at {h} hops"}{who} = {v:.4g} {op} {thr:.4g}'
        v = self.metric_value(m, c['metric'], flow, None, over)
        who = ''
        if over == 'worst_node' and v is not None:
            who = f' (worst router: {m["worst"][c["metric"]]["node"]})'
        return False, f'{c["metric"]}{who} = {v if v is None else round(v, 4)}'

    def window_for(self, window_s, need, cap=4):
        """A window long enough for about need packets at the load in force
        (at most cap x window_s)."""
        rate = (self.load or {}).get('rate_pps')
        if not rate:
            return window_s
        # 25 % more than the mean: a Poisson count falls short of its mean
        # half the time
        return min(max(window_s, math.ceil(1.25 * need / rate)), cap * window_s)

    def wait(self, c, ref_t, timeout, ctx):
        """Poll until c holds or timeout: (met, detail)."""
        t_end = self.now + timeout
        needs_net = bool(self.sets_for(c) & {'mesh', 'queues'})
        while True:
            if needs_net:
                self.refresh(net=True, tree=True)
            ok, detail = self.cond(c, ref_t, ctx)
            if ok:
                return True, detail
            if self.now >= t_end - 1e-9:
                return False, detail
            self.advance(min(self.now + self.poll, t_end))

    # ------------------------------------------------------------- phases

    def user_step(self, p):
        for us in self.record['user_steps']:
            if us['step'] == p['x_index']:
                return us
        return {}

    def end_step(self, p, rec, state=None):
        """The user's step after this phase: ended when it is its last part."""
        us = self.user_step(p)
        if state is None and p.get('x_part', 0) < p.get('x_parts', 1) - 1:
            return
        if us.get('state') in ('completed', 'not_met', 'skipped'):
            return
        if state is None:
            parts = [r for r in self.record['phases'] if r.get('step') == p['x_index']]
            state = 'not_met' if any(r.get('end', {}).get('met') is False for r in parts) else 'completed'
        us['state'] = state
        us['t_end_s'] = round(self.now, 6)
        if state == 'skipped':
            self.note(f'step {p["x_index"]} {p.get("x_step_name")} skipped: {rec.get("skip_reason")}')
        else:
            self.note(f'step {p["x_index"]} {p.get("x_step_name")} ends ({state})')

    def skip_phase(self, p, rec, reason):
        rec['skipped'] = True
        rec['skip_reason'] = reason
        self.skipped_steps.setdefault(p['x_index'], reason)
        self.note(f'phase {p["name"]} skipped: {reason}')
        self.end_step(p, rec, 'skipped')

    def run_phase(self, p, prev_end):
        k = len(self.record['phases'])
        pl = self.plan['phases'][k] if k < len(self.plan['phases']) else {}
        rec = {'name': p['name'], 'block': p.get('x_block'), 'index': p['x_index'], 'step': p['x_index'],
               'step_name': p.get('x_step_name'), 'title': pl.get('title'), 'part': p.get('x_part', 0),
               'parts': p.get('x_parts', 1)}
        self.record['phases'].append(rec)
        us = self.user_step(p)
        us.setdefault('phases', []).append(p['name'])
        if p['x_index'] in self.skipped_steps:
            rec['skipped'] = True
            rec['skip_reason'] = f'an earlier part of the step was skipped ({self.skipped_steps[p["x_index"]]})'
            rec['start'] = {'t_s': round(self.now, 6), 'met': False, 'detail': rec['skip_reason']}
            return
        if us.get('state') == 'pending':
            us['state'] = 'running'
            us['t_start_s'] = round(self.now, 6)
            self.note(f'step {p["x_index"]} {p.get("x_step_name")} starts')
            self.write_result('running')
        # start
        start = p.get('start')
        if isinstance(start, dict) and start.get('is', 'immediately') != 'immediately':
            ctx = {}
            met, detail = self.wait(start, prev_end, start.get('timeout_s', 3600), ctx)
            rec['start'] = {'t_s': round(self.now, 6), 'met': met, 'detail': detail,
                            'timeout': not met, 'condition': start}
            if not met:
                how = start.get('on_timeout', 'start')
                rec['start']['on_timeout'] = how
                self.note(f'phase {p["name"]}: start condition not met after {start.get("timeout_s", 3600)} s ({detail}): {how}')
                if how == 'skip':
                    self.skip_phase(p, rec, f'start condition not met after {start.get("timeout_s", 3600)} s')
                    return
                if how == 'abort':
                    rec['aborted'] = True
                    raise Stop(f'phase {p["name"]}: start condition not met (on_timeout: abort)')
        else:
            rec['start'] = {'t_s': round(self.now, 6), 'met': True, 'detail': 'right after the previous phase'}
        t0 = self.now
        self.note(f'phase {p["name"]} starts ({rec["start"]["detail"]})')
        # Test hooks (sim_utils/vtime/test_scenario.sh): a raw node command,
        # an error inside the driver
        if p.get('x_test_command'):
            self.node('all', p['x_test_command'])
            self.run_until(self.now)
        if p.get('x_test_crash'):
            raise RuntimeError('x_test_crash')
        measure = p.get('measure', {})
        sets = set(measure.get('sets', ['traffic']))
        if 'repeat' in p:
            sets |= self.sets_for(p['repeat'].get('until'))
        elif isinstance(p.get('end'), dict):
            sets |= self.sets_for(p['end'])
        if 'mesh' in sets or 'queues' in sets:
            self.refresh(net=True, tree=True)
        snap0 = self._snap(f'{p["name"]}:start')
        ctr0 = self.counters() if 'medium' in sets else None
        # A "Switch nodes off" step (one action) chooses its nodes now, before
        # its load changes (a ranking measures the window before the step):
        # none, the step is skipped. Hand-written phases select in each
        # action, in order, as before (a later action may use an earlier one's
        # label or kills)
        pre = {}
        for j, a in enumerate(p.get('actions') or []):
            if p.get('x_block') == 'node_outage' and a.get('do') in DISRUPTING and a.get('nodes'):
                nodes, why = self.select(a['nodes'])
                if not nodes:
                    self.skip_phase(p, rec, f'{a["do"]}: no node selected ({why})')
                    return
                pre[j] = (nodes, why, dict(self.last_sel or {}))
        # The load first, then the actions: a failure happens under the load
        load = p.get('load', 'keep')
        if 'repeat' not in p and load != 'keep':
            self.apply_load(load, p['name'])
        for j, a in enumerate(p.get('actions') or []):
            self.do_action(a, rec, pre.get(j))
        if 'repeat' in p:
            self.run_iterations(p, rec, sets, load)
        else:
            end = p.get('end', {'after_s': 60})
            settle = measure.get('settle_s', 0)
            if 'after_s' in end and 'is' not in end:
                self.advance(t0 + end['after_s'])
                rec['end'] = {'t_s': round(self.now, 6), 'met': True, 'detail': f'after {end["after_s"]} s'}
            else:
                ctx = {'t_start': t0 + settle}
                met, detail = self.wait(end, t0, end.get('max_s', 600), ctx)
                rec['end'] = {'t_s': round(self.now, 6), 'met': met, 'detail': detail, 'timeout': not met,
                              'condition': end}
            if 'mesh' in sets or 'queues' in sets:
                self.refresh(net=True, tree=True)
            snap1 = self._snap(f'{p["name"]}:end')
            ctr1 = self.counters() if 'medium' in sets else None
            t_to = max(self.now - self.drain, t0 + settle) if (self.now - t0) > self.drain else self.now
            m = self.window_metrics(t0 + settle, t_to, self.now, sets, ctr0, ctr1, snap0, snap1)
            rec['metrics'] = m
            rec['load'] = self.load and {k: v for k, v in self.load.items() if k not in ('spec', 'member_ids')}
            fl = ((m.get('flows') or {}).get(self.flow_of(None)) or {})
            self.results[p['name']] = {'metrics': m, 'flow': self.flow_of(None),
                                       'offered_pps': fl.get('offered_pps') or (self.load and self.load['rate_pps'])}
        self.note(f'phase {p["name"]} ends ({rec.get("end", {}).get("detail", "")})')
        self.end_step(p, rec)
        self.write_result('running')

    def write_result(self, status):
        """scenario_result.json as it stands (status "running" until the end)."""
        self.record['status'] = status
        self.record['end_t_s'] = self.now
        self.record['labels'] = self.labels
        self.record['step_off'] = {str(k): v for k, v in self.step_off.items()}
        self.record['off_intervals'] = {str(k): v for k, v in self.off.items()}
        try:
            self.record['summary'] = summarize(self.record)
        except Exception as e:      # noqa: BLE001
            self.record['summary'] = {'status': status, 'error': f'summary: {e}'}
        tmp = os.path.join(self.dir, 'scenario_result.json.tmp')
        with open(tmp, 'w') as f:
            json.dump(self.record, f, indent=1, default=str)
        os.replace(tmp, os.path.join(self.dir, 'scenario_result.json'))

    def _snap(self, tag):
        if not self.power:
            return None
        path = os.path.join(self.dir, 'power_snap.jsonl')
        size = os.path.getsize(path) if os.path.exists(path) else 0
        self.power_snap(tag)
        with open(path) as f:
            f.seek(size)
            line = f.readline()
        if not line:
            return None
        snap = json.loads(line)
        self.snaps.append((self.now, snap))
        return snap

    def snap_at(self, t):
        best = None
        for tt, sn in self.snaps:
            if tt <= t + 1e-9:
                best = sn
            else:
                break
        return best

    def measure_window(self, p, i, sets, settle):
        """One measured window at the load in force: (metrics, t_start)."""
        r = p['repeat']
        self.advance(self.now + settle)
        if 'mesh' in sets or 'queues' in sets:
            self.refresh(net=True)
        snap0 = self._snap(f'{p["name"]}:{i}:start')
        ctr0 = self.counters() if 'medium' in sets else None
        t_a = self.now
        w = self.window_for(r.get('window_s', 60), self.min_samples)
        self.advance(t_a + w + self.drain)
        if 'mesh' in sets or 'queues' in sets:
            self.refresh(net=True, tree=True)
        snap1 = self._snap(f'{p["name"]}:{i}:end')
        ctr1 = self.counters() if 'medium' in sets else None
        return self.window_metrics(t_a, t_a + w, self.now, sets, ctr0, ctr1, snap0, snap1), t_a

    def run_iterations(self, p, rec, sets, load):
        """A capacity search. Each step: settle, then a window long enough for
        min_samples packets; the condition must hold on `confirm` windows in a
        row at the same rate. Going up until it holds; when the first rate
        already holds, going down until it does not. The capacity is the
        measured offered rate of the last step that did not hold."""
        r = p['repeat']
        until = r['until']
        confirm = r.get('confirm', 2)
        spec = load if load not in ('keep', 'off', None) else (self.load or {}).get('spec', {})
        rec['iterations'] = []
        flow = None
        direction = 1
        sat = last_ok = None
        rate = None
        i = 0
        while i < r.get('max', 12):
            if rate is None:
                self.apply_load(spec, p['name'], iteration=i)
            else:
                self.apply_load(spec, p['name'], iteration=i, rate=rate)
            flow = self.flow_of(None)
            held = 0
            its = []
            while True:
                m, t_a = self.measure_window(p, i, sets, r.get('settle_s', 15) if not its else 0)
                ok, detail = self.cond(until, t_a, {'metrics': m})
                f = (m.get('flows') or {}).get(flow, {})
                it = {'iteration': i, 'step_tag': self.step, 'rate_pps': self.load['rate_pps'],
                      'offered_pps': f.get('offered_pps'), 'delivered_pps': f.get('delivered_pps'),
                      'samples': f.get('sent'), 'interval_s': self.load['interval_s'], 't_s': round(t_a, 6),
                      'met': ok, 'detail': detail, 'direction': 'up' if direction > 0 else 'down',
                      'confirmation': len(its), 'metrics': m}
                rec['iterations'].append(it)
                its.append(it)
                i += 1
                self.note(f'phase {p["name"]} window {i - 1}: {self.load["rate_pps"]:.4g} packets/s '
                          f'(measured {f.get("offered_pps") or 0:.4g}): {detail}' + (' (condition met)' if ok else ''))
                held = held + 1 if ok else 0
                if not ok or held >= confirm or i >= r.get('max', 12):
                    break
            step_ok = held >= confirm
            cur = self.load['rate_pps']
            if direction > 0:
                if step_ok:
                    if last_ok is None and len(rec['iterations']) == len(its):
                        direction = -1      # the first rate already holds: down
                        sat = its[-1]
                        self.note(f'phase {p["name"]}: the first rate already meets the condition: lowering it')
                        rate = cur / r['mul'] if 'add_pps' not in r else max(cur - r['add_pps'], cur / 2)
                        continue
                    sat = its[-1]
                    break
                last_ok = its[-1]
                rate = cur * r['mul'] if 'add_pps' not in r else cur + r['add_pps']
            else:
                if not step_ok:
                    last_ok = its[-1]
                    break
                sat = its[-1]
                rate = cur / r['mul'] if 'add_pps' not in r else max(cur - r['add_pps'], cur / 2)
        res = {'metrics': (last_ok or rec['iterations'][0])['metrics'], 'flow': flow,
               'offered_pps': (last_ok or rec['iterations'][0])['offered_pps']}
        # The capacity: the measured offered rate (the nominal one differs
        # with the Poisson draws and the routers left out)
        res['capacity_pps'] = round(last_ok['offered_pps'], 6) if last_ok and last_ok['offered_pps'] else None
        rec['capacity'] = {'capacity_pps': res['capacity_pps'],
                           'capacity_nominal_pps': last_ok['rate_pps'] if last_ok else None,
                           'delivered_pps': last_ok['delivered_pps'] if last_ok else None,
                           'saturated_rate_pps': sat['rate_pps'] if sat else None,
                           'saturated_offered_pps': sat['offered_pps'] if sat else None,
                           'reached': sat is not None and last_ok is not None,
                           'searched': 'down' if direction < 0 else 'up', 'confirm': confirm,
                           'min_samples': self.min_samples,
                           'detail': (sat['detail'] if sat else 'the condition never held: the capacity is at least '
                                      'the last rate') if last_ok else
                           'every rate tried met the condition: the capacity is below the lowest'}
        base = self._baseline_of(until)
        if base:
            rec['capacity']['baseline'] = base
        rec['end'] = {'t_s': round(self.now, 6), 'met': sat is not None,
                      'detail': rec['capacity']['detail']}
        rec['metrics'] = res['metrics']
        rec['load'] = self.load and {k: v for k, v in self.load.items() if k not in ('spec', 'member_ids')}
        self.results[p['name']] = res

    def _baseline_of(self, c):
        """The phases a condition is relative to, and their sample counts."""
        out = {}
        for x in [c] + (c.get('of') or []):
            rel = x.get('relative_to') if isinstance(x, dict) else None
            if rel and rel['phase'] in self.results:
                f = ((self.results[rel['phase']].get('metrics') or {}).get('flows') or {}).get(
                    self.results[rel['phase']].get('flow', 'up'), {})
                out[rel['phase']] = {'samples': f.get('sent'), 'enough': (f.get('sent') or 0) >= self.min_samples}
        return out

    def finalize(self, d, at_end=False):
        """Close a disruption: its repair and what it cost."""
        d['open'] = False
        d['tracked_until_s'] = round(self.now, 3)
        d['open_at_end'] = at_end
        par = {n: p for n, p in self.run.parents(self.now).items() if n not in self.killed}
        dep = runlog.depths(par)
        d['depths_after'] = {str(k): v for k, v in dep.items() if v is not None}
        t_end = d['t_s'] + d['repair_e2e_t_s'] if d.get('repair_e2e_t_s') is not None else self.now
        nd_rep = runlog.net_delta(self.run, d['t_s'], t_end)
        d['churn'] = {'parent_changes': self.parent_changes(d['t_s'], t_end),
                      'rpl_better_parent_counter': sum(x['parent_changes'] for x in nd_rep.values()),
                      'depth_changes': sum(1 for n in par if str(n) in d['parents_before'] and
                                           (d['depths_before'].get(str(n)) != d['depths_after'].get(str(n)))),
                      'control_messages': {k: sum(x.get('ctl_' + k, 0) for x in nd_rep.values())
                                           for k in ('dis', 'dio', 'dao')}}
        pm = runlog.packet_metrics(self.run, d['t_s'], t_end, self.now)
        d['packets_during_repair'] = {f: {'sent': v['sent'], 'lost': round(v['sent'] * (1 - (v['pdr'] or 0)))}
                                      for f, v in pm.items()}
        aff = set(d['affected'])
        sent = [k for k, v in self.run.tx.items() if k[1] in aff and d['t_s'] <= v[0] < t_end and v[4] >= 0]
        lost = sum(1 for k in sent if not (self.run.echo.get(k) and self.run.echo[k][0] <= self.now))
        d['packets_of_cut_off_routers'] = {'sent': len(sent), 'lost': lost}
        d['nodes_affected'] = len(d['affected'])
        d['orphan_time_s'] = runlog.stats(list(d['orphan_s'].values()))
        d['repaired'] = d['repair_t_s'] is not None
        d['repaired_e2e'] = d['repair_e2e_t_s'] is not None
        self.note(f'disruption of {d["phase"]} ({d["do"]}) closed: route repair '
                  f'{d["repair_t_s"]} s, end to end {d["repair_e2e_t_s"]} s'
                  + (' (scenario ended first)' if at_end else ''))

    def parent_changes(self, t_from, t_to):
        """Changes to a new parent seen in the NET1 samples (resolution: their
        times), all routers."""
        seen = 0
        for n in self.run.routers():
            last = None
            for smp in self.run.nodes[n].net:
                if smp['t'] < t_from - 1e-9 or smp['t'] > t_to + 1e-9:
                    continue
                p = smp.get('rpl', {}).get('parent')
                if p is None:
                    continue        # a new parent counts once, not its loss
                if last is not None and p != last:
                    seen += 1
                last = p
        return seen

    def drive(self):
        """Run the scenario. Exit status 0 when it completed (or stopped as it
        says, on_timeout: abort), 1 on any error: the result is written in
        every case, with the status and, on an error, its traceback."""
        self.record['start_wall'] = time.time()
        with open(os.path.join(self.dir, 'scenario_plan.json'), 'w') as f:
            json.dump(self.plan, f, indent=1)
        status, code = 'completed', 0
        try:
            r = self.rf.readline()     # the first pause: "at 0"
            if not r.startswith('at '):
                raise Abort('unexpected greeting from the conductor: ' + r.strip())
            self.now = int(r.split()[1]) / 1e6
            self.run.update()
            prev_end = self.now
            for p in self.phases:
                self.run_phase(p, prev_end)
                prev_end = self.now
            self.note('scenario done')
        except Stop as e:
            status = f'aborted: {e}'
        except Abort as e:
            status, code = f'error: {e}', 1
        except Exception as e:      # a bug: the run must not look successful
            status, code = f'error: {type(e).__name__}: {e}', 1
            self.record['traceback'] = traceback.format_exc()
            traceback.print_exc(file=self.log)
        print(f'scenario: {status}', file=self.log, flush=True)
        try:
            for d in self.open_disruptions():
                self.finalize(d, at_end=True)
        except Exception as e:      # noqa: BLE001 (the result is still written)
            print(f'scenario: closing the disruptions: {e}', file=self.log)
        for us in self.record['user_steps']:
            if us['state'] == 'running':
                us['state'] = 'error' if code else 'not_met'
                us['t_end_s'] = round(self.now, 6)
        self.write_result(status)
        if not self.ended:
            try:
                self.sock.sendall(b'quit\n')
                self.rf.readline()
            except OSError:
                pass
        for n, name, p in self.children:
            try:
                p.wait(timeout=30)
            except subprocess.TimeoutExpired:
                p.kill()
            with open(os.path.join(self.dir, 'scenario_programs.txt'), 'a') as f:
                f.write(f'{name} {p.returncode}\n')
        return code


def _fmt(v, nd=1):
    return None if v is None else round(v, nd)


def summarize(rec):
    """The outcome of a scenario in a few figures (report.json "scenario.summary")."""
    out = {'status': rec.get('status'), 'phases': []}
    for ph in rec['phases']:
        row = {'name': ph['name'], 'start_s': ph.get('start', {}).get('t_s'), 'end_s': ph.get('end', {}).get('t_s'),
               'start_detail': ph.get('start', {}).get('detail'), 'start_timeout': ph.get('start', {}).get('timeout'),
               'end_detail': ph.get('end', {}).get('detail'), 'skipped': ph.get('skipped', False),
               'skip_reason': ph.get('skip_reason'), 'step': ph.get('step'), 'step_name': ph.get('step_name'),
               'title': ph.get('title')}
        m = ph.get('metrics') or {}
        row['retries'] = (m.get('queues') or {}).get('retries')
        row['retries_per_frame'] = _fmt((m.get('queues') or {}).get('retries_per_frame'), 4)
        row['collisions'] = (m.get('medium') or {}).get('collisions')
        row['worst'] = m.get('worst')
        chosen = [x for a in ph.get('actions') or [] for x in (a.get('chosen') or [])]
        if chosen:
            row['chosen'] = chosen
            row['rank_by'] = next((a.get('rank_by') for a in ph['actions'] if a.get('rank_by')), None)
        f = (m.get('flows') or {}).get('up') or next(iter((m.get('flows') or {}).values()), None)
        if f:
            row['pdr'] = _fmt(f.get('pdr'), 4)
            row['loss'] = None if f.get('pdr') is None else _fmt(1 - f['pdr'], 4)
            row['rtt_ms'] = {k: _fmt(f['rtt_ms'].get(k)) for k in ('n', 'mean', 'p95', 'max')} if f['rtt_ms']['n'] else None
            row['rtt_ms_by_hops'] = {h: {'n': s['n'], 'mean': _fmt(s.get('mean')), 'p95': _fmt(s.get('p95'))}
                                     for h, s in f['rtt_ms_by_hops'].items()}
            row['offered_pps'] = _fmt(f.get('offered_pps'), 3)
        if 'capacity' in ph:
            row['capacity'] = ph['capacity']
        if ph.get('disruptions'):
            row['disruptions'] = [{k: d.get(k) for k in ('do', 'failed', 'nodes_affected', 'marginal', 'isolated',
                                                         'detect_t_s', 'repair_t_s', 'repair_e2e_t_s', 'repaired',
                                                         'repaired_e2e', 'open_at_end', 'orphan_time_s', 'churn',
                                                         'packets_during_repair', 'packets_of_cut_off_routers',
                                                         'marginal_repaired_s')}
                                  for d in ph['disruptions']]
        if (m.get('power') or {}).get('avg_ma') is not None:
            row['power_avg_ma'] = _fmt(m['power']['avg_ma'], 4)
        out['phases'].append(row)
    return out

# ---------------------------------------------------------------------- main


def load_doc(path):
    with open(path) as f:
        return json.load(f)


def main(argv):
    if not argv or argv[0] in ('-h', '--help'):
        print(__doc__)
        return 0
    if argv[0] == '--schema':
        print(json.dumps(schema(), indent=1))
        return 0
    if argv[0] == '--blocks':
        print(json.dumps({n: {'title': b['title'], 'description': b['doc'],
                              'parameters': b['params'], 'required': b.get('required', [])}
                          for n, b in BLOCKS.items()}, indent=1, default=str))
        return 0
    if argv[0] in ('--check', '--dry'):
        try:
            doc = load_doc(argv[1])
        except (OSError, ValueError, IndexError) as e:
            print(json.dumps({'ok': False, 'errors': [{'path': '', 'message': f'cannot read the file: {e}'}]}))
            return 1
        errs = check(doc)
        if errs or argv[0] == '--check':
            print(json.dumps({'ok': not errs, 'errors': errs}, indent=1))
            return 1 if errs else 0
        print(json.dumps(dry(doc), indent=1))
        return 0
    if argv[0] == 'drive':
        opts = dict(zip(argv[1::2], argv[2::2]))
        doc = load_doc(opts['--scenario'])
        errs = check(doc)
        if errs:
            print(json.dumps({'ok': False, 'errors': errs}, indent=1), file=sys.stderr)
            return 2
        d = Driver(opts['--ctl'], opts['--run'], doc, opts.get('--programs'))
        return d.drive()
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
