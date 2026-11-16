#!/usr/bin/env python3
"""Incremental reader of a virtual-time run directory (docs/TRAFFIC_AND_REPORT.md).

Shared by sim_utils/report.py (once, after the run) and sim_utils/scenario.py
(live, at every pause of the run). Each log is read once, from where the last
call stopped: only complete lines are taken.

What it keeps:
  - per node: the join milestones of analyze.py (first time per instance),
    the instance count (node<i>.r<k>.log), the NET1 samples (docs/
    TRAFFIC_AND_REPORT.md §3);
  - the border router's node-table snapshots ("brtree" lines);
  - the application packets (LOAD1 lines): sends, receptions, echoes.

Times are seconds since the run's start (virtual time). Standard library only.
"""
import json, os, re

ANSI = re.compile(r'\x1b\[[0-9;]*m')
MILESTONES = [   # as sim_utils/migration/analyze.py
    ('parent', 'selected parent:'),
    ('auth_ok', 'authentication success'),
    ('operational', 'operation start'),
    ('ready', 'ARM_NWK_BOOTSTRAP_READY'),
]
FLOW_NAMES = {1: 'up', 2: 'down', 3: 'mcast', 4: 'p2p'}
FLOW_IDS = {v: k for k, v in FLOW_NAMES.items()}
LOG_RE = re.compile(r'^(?:br(?:\.r(\d+))?|node(\d+)(?:\.r(\d+))?|if(\d+))\.log$')


def mac_id(mac):
    b = mac.split(':')
    return int(b[6], 16) << 8 | int(b[7], 16)


class Tail:
    """A file read from where the last call stopped, complete lines only."""
    def __init__(self, path):
        self.path, self.pos, self.rest = path, 0, b''

    def lines(self):
        try:
            with open(self.path, 'rb') as f:
                f.seek(self.pos)
                data = f.read()
        except OSError:
            return []
        if not data:
            return []
        self.pos += len(data)
        data = self.rest + data
        cut = data.rfind(b'\n')
        if cut < 0:
            self.rest = data
            return []
        self.rest = data[cut + 1:]
        return data[:cut].decode('utf-8', 'replace').split('\n')


class Node:
    def __init__(self, nid):
        self.id = nid
        self.instances = 1
        self.ms = {}                # milestone -> time (the current instance)
        self.ms_history = []        # (instance, milestone, time) of every instance
        self.net = []               # NET1 samples (dicts), in time order
        self.restart_t = []         # start time of each later instance


class Run:
    def __init__(self, d, keep_net=True):
        self.dir = d
        try:
            self.t0 = float(open(os.path.join(d, 't0')).read())
        except OSError:
            self.t0 = 1700000000.0
        self.tails = {}
        self.nodes = {}
        self.tree = []              # (t, {node: parent}) from brtree, complete snapshots
        self._tree_cur = None
        self.keep_net = keep_net
        # packets
        self.tx = {}                # (flow, src, seq) -> [t_tx, dst, len, step, rc, type]
        self.cfg = {}               # node -> [(t, flows, member, echo, step)] (LOAD1 cfg)
        self.acks = {}              # (node, seq) -> (ok, reason) (CTL1)
        self.skips = []             # (flow, src, dst, step, t, reason)
        self.rx = {}                # (flow, src, seq) -> list of (dst, t_rx, hops)
        self.echo = {}              # (flow, src, seq) -> (t_rx, t_peer_rx, hops_back)
        self.lines_read = 0
        self._cur_nid = None

    def node(self, nid):
        n = self.nodes.get(nid)
        if n is None:
            n = self.nodes[nid] = Node(nid)
        return n

    def _files(self):
        for f in os.listdir(self.dir):
            m = LOG_RE.match(f)
            if not m or f in self.tails:
                continue
            self.tails[f] = Tail(os.path.join(self.dir, f))
        # instance order: node3.log before node3.r1.log
        def key(f):
            m = LOG_RE.match(f)
            return (not f.startswith('br'), int(m.group(2) or m.group(4) or 0), int(m.group(1) or m.group(3) or 0))
        return sorted(self.tails, key=key)

    def update(self):
        """Read what the logs gained since the last call."""
        for f in self._files():
            m = LOG_RE.match(f)
            if f.startswith('br'):
                nid, inst = 0, int(m.group(1) or 0)
            elif m.group(4) is not None:
                nid, inst = int(m.group(4)), 0
            else:
                nid, inst = int(m.group(2)), int(m.group(3) or 0)
            self._read(f, nid, inst)

    def _read(self, f, nid, inst):
        node = self.node(nid)
        if inst + 1 > node.instances:
            node.instances = inst + 1
            node.ms = {}
        for line in self.tails[f].lines():
            self.lines_read += 1
            if ' LOAD1 ' in line:
                self._cur_nid = nid
                self._load1(line)
                continue
            if ' CTL1 ' in line:
                w = line.split(' CTL1 ', 1)[1].split(' ', 2)
                try:
                    self.acks[(nid, int(w[1]))] = (w[0] == 'ok', w[2] if len(w) > 2 else '')
                except (IndexError, ValueError):
                    pass
                continue
            if ' NET1 ' in line:
                try:
                    j = json.loads(line.split(' NET1 ', 1)[1])
                except ValueError:
                    continue
                j['t'] = j['t_us'] / 1e6
                # The instance that wrote it: a restarted node's counters start again from 0
                j['inst'] = inst
                if self.keep_net:
                    node.net.append(j)
                else:
                    node.net[-1:] = [j]
                continue
            if nid == 0:
                if 'brtree t=' in line:
                    self._brtree(line)
                if inst == 0:
                    continue
            # Every occurrence goes to ms_history (an interface brought down
            # and up joins again in the same instance); ms keeps the first of
            # the current instance
            for k, pat in MILESTONES:
                if pat in line:
                    t = self._t(line)
                    if t is None:
                        continue
                    node.ms.setdefault(k, t)
                    node.ms_history.append((inst, k, t))
                if inst and inst > len(node.restart_t) and 'ns_port_sim: started at' in line:
                    t = self._t(line)
                    node.restart_t.append(t)

    def _t(self, line):
        try:
            return float(line.split(' ', 1)[0]) - self.t0
        except ValueError:
            return None

    def _load1(self, line):
        w = line.split(' LOAD1 ', 1)[1].split()
        try:
            if w[0] == 'cfg':
                t = self._t(line)
                self.cfg.setdefault(self._cur_nid, []).append((t, int(w[1]), w[2] == '1', w[3] == '1', int(w[4])))
                return
            kind, flow, seq, src, dst = w[0], int(w[1]), int(w[2]), int(w[3]), int(w[4])
            if kind == 'tx':
                # type: 0 request (echoed), 2 one-way; older logs have none
                typ = int(w[9]) if len(w) > 9 else (2 if flow == 3 else 0)
                self.tx[(flow, src, seq)] = [int(w[7]) / 1e6, dst, int(w[5]), int(w[6]), int(w[8]), typ]
            elif kind == 'rx':
                hl_tx, hl_rx = int(w[9]), int(w[10])
                hops = hl_tx - hl_rx + 1 if hl_rx > 0 else None
                self.rx.setdefault((flow, src, seq), []).append((dst, int(w[8]) / 1e6, hops))
            elif kind == 'echo':
                hl_tx, hl_rx = int(w[10]), int(w[11])
                hops = hl_tx - hl_rx + 1 if hl_rx > 0 else None
                self.echo[(flow, src, seq)] = (int(w[9]) / 1e6, int(w[8]) / 1e6, hops)
            elif kind == 'skip':
                self.skips.append((flow, src, dst, int(w[6]), int(w[7]) / 1e6, w[8]))
        except (IndexError, ValueError):
            pass

    def _brtree(self, line):
        m = re.search(r'brtree t=(\S+) (?:node=(\S+) parent=(\S+)|nodes=(\d+))', line)
        if not m:
            return
        t = float(m.group(1))
        if m.group(2):
            if self._tree_cur is None or self._tree_cur[0] != t:
                self._tree_cur = (t, {})
            self._tree_cur[1][mac_id(m.group(2))] = mac_id(m.group(3))
        else:
            tree = self._tree_cur[1] if self._tree_cur and self._tree_cur[0] == t else {}
            self.tree.append((t, tree))
            self._tree_cur = None

    # ------------------------------------------------------------ views

    def routers(self):
        return sorted(n for n in self.nodes if n != 0)

    def last_net(self, nid, until=None):
        """The node's last NET1 sample at or before until."""
        net = self.nodes[nid].net if nid in self.nodes else []
        if until is None:
            return net[-1] if net else None
        for s in reversed(net):
            if s['t'] <= until + 1e-9:
                return s
        return None

    def parents(self, until=None):
        """Each node's RPL preferred parent from its last NET1 sample (None: none)."""
        out = {}
        for nid in self.routers():
            s = self.last_net(nid, until)
            if s is not None:
                out[nid] = s.get('rpl', {}).get('parent')
        return out

    def member_at(self, nid, t):
        """Whether the router took part in the load at t (its last LOAD1 cfg
        line), None if it printed none."""
        last = None
        for c in self.cfg.get(nid, []):
            if c[0] <= t + 1e-9:
                last = c
        return None if last is None else last[2]

    def mcast_receivers(self, t):
        """Routers expected to receive a group packet sent at t: members then,
        operational (Wi-SUN join done) by then."""
        return {nid for nid in self.routers()
                if self.member_at(nid, t) and any(k == 'operational' and tt <= t for _, k, tt in self.nodes[nid].ms_history)}

    def br_tree(self, until=None):
        for t, tree in reversed(self.tree):
            if until is None or t <= until + 1e-9:
                return t, tree
        return None, {}


def depths(parents):
    """Hop count to the border router of each node, from a {node: parent} map
    (None: no route, a loop or a parent with no route)."""
    out = {0: 0}
    def depth(n, seen):
        if n in out:
            return out[n]
        p = parents.get(n)
        if p is None or n in seen:
            out[n] = None
            return None
        seen.add(n)
        d = depth(p, seen)
        out[n] = None if d is None else d + 1
        return out[n]
    for n in parents:
        depth(n, set())
    return out


def children(parents):
    ch = {}
    for n, p in parents.items():
        if p is not None:
            ch.setdefault(p, []).append(n)
    return ch


def subtree(parents, root):
    """The nodes below root (not root itself)."""
    ch, out, todo = children(parents), [], [root]
    while todo:
        n = todo.pop()
        for c in ch.get(n, []):
            if c not in out and c != root:
                out.append(c)
                todo.append(c)
    return sorted(out)


def stats(vals):
    """n, min, mean, p50, p95, max of a list (ms values stay in their unit)."""
    if not vals:
        return {'n': 0}
    v = sorted(vals)
    n = len(v)
    def q(p):
        k = (n - 1) * p
        lo = int(k)
        hi = min(lo + 1, n - 1)
        return v[lo] + (v[hi] - v[lo]) * (k - lo)
    return {'n': n, 'min': v[0], 'mean': sum(v) / n, 'p50': q(0.5), 'p95': q(0.95), 'max': v[-1]}


def packet_metrics(run, t_from, t_to, t_eval, flows=None, hop_of=None):
    """Delivery and delay of the packets sent in [t_from, t_to), with what
    arrived by t_eval. Per flow: sent, delivered (target received), echoed
    (originator got the echo), PDR (echoed / sent when the flow is echoed, else
    delivered / sent), RTT and one-way latency (ms) overall and by hop count.
    hop_of(flow, src, dst, seq, echo_hops, rx_hops) may override the hop count."""
    out = {}
    exp_cache = {}
    for key, v in run.tx.items():
        flow, src, seq = key
        t_tx, dst, length, step, rc = v[:5]
        typ = v[5] if len(v) > 5 else (2 if flow == 3 else 0)
        if not (t_from <= t_tx < t_to) or (flows and FLOW_NAMES.get(flow) not in flows):
            continue
        f = out.setdefault(FLOW_NAMES.get(flow, str(flow)), {
            'sent': 0, 'send_errors': 0, 'delivered': 0, 'echoed': 0, 'bytes': 0, '_requests': 0,
            '_rtt': [], '_lat': [], '_rtt_hop': {}, '_lat_hop': {}, '_rcv': 0, '_exp': 0})
        if typ == 0:
            f['_requests'] += 1
        if rc < 0:
            f['send_errors'] += 1
            continue
        f['sent'] += 1
        f['bytes'] += length
        rxs = [r for r in run.rx.get((flow, src, seq), []) if r[1] <= t_eval]
        if flow == 3:
            f['_rcv'] += len(rxs)
            tk = round(t_tx, 1)
            if tk not in exp_cache:
                exp_cache[tk] = run.mcast_receivers(t_tx) if hasattr(run, 'mcast_receivers') else set()
            # expected: the members operational then, and any that received it anyway
            f['_exp'] += len(exp_cache[tk] | {r[0] for r in rxs})
        if rxs:
            f['delivered'] += 1
            for r in rxs:
                lat = (r[1] - t_tx) * 1e3
                f['_lat'].append(lat)
                if r[2] is not None:
                    f['_lat_hop'].setdefault(r[2], []).append(lat)
        e = run.echo.get((flow, src, seq))
        if e and e[0] <= t_eval:
            f['echoed'] += 1
            rtt = (e[0] - t_tx) * 1e3
            f['_rtt'].append(rtt)
            h = e[2]
            if h is None and rxs:
                h = rxs[0][2]
            if hop_of:
                h = hop_of(flow, src, dst, seq, e[2], rxs[0][2] if rxs else None)
            if h is not None:
                f['_rtt_hop'].setdefault(h, []).append(rtt)
    for name, f in out.items():
        # Echoed: the packets were requests (a property of the flow's sends,
        # not of the echoes that came back)
        echoed_flow = f.pop('_requests') > f['sent'] / 2
        f['echoed_flow'] = echoed_flow
        f['delivery'] = f['delivered'] / f['sent'] if f['sent'] else None
        if name == 'mcast':
            # receptions / (sends x the members expected to receive each)
            f['receptions'] = f.pop('_rcv')
            f['expected_receptions'] = f.pop('_exp')
            f['pdr'] = f['receptions'] / f['expected_receptions'] if f['expected_receptions'] else None
        else:
            f.pop('_rcv')
            f.pop('_exp')
            f['pdr'] = (f['echoed'] if echoed_flow else f['delivered']) / f['sent'] if f['sent'] else None
        f['rtt_ms'] = stats(f.pop('_rtt'))
        f['latency_ms'] = stats(f.pop('_lat'))
        f['rtt_ms_by_hops'] = {str(h): stats(v) for h, v in sorted(f.pop('_rtt_hop').items())}
        f['latency_ms_by_hops'] = {str(h): stats(v) for h, v in sorted(f.pop('_lat_hop').items())}
        span = max(t_to - t_from, 1e-9)
        f['offered_pps'] = f['sent'] / span
        f['delivered_pps'] = (f['echoed'] if echoed_flow else f['delivered']) / span
        if name == 'mcast' and f['expected_receptions']:
            f['delivered_pps'] = f['sent'] * f['pdr'] / span
        f['offered_bps'] = f['bytes'] * 8 / span
    return out


def net_delta(run, t_from, t_to, nodes=None):
    """Per node, the change of its NET1 counters between its last samples at or
    before t_from and t_to, with its mean queue depths over the interval.

    A node restarted in the interval (node<i>.r<k>.log) has its counters
    starting again from 0 in each instance: the change is the sum over the
    instances that wrote samples in the interval, each from its own first
    reference (the sample at or before t_from for the instance running then,
    0 for a later one) to its last sample at or before t_to. Peaks are the
    highest of the instances'."""
    out = {}
    for nid in (nodes or sorted(run.nodes)):
        a, b = run.last_net(nid, t_from), run.last_net(nid, t_to)
        if b is None:
            continue
        # (reference, last sample) of each instance, in order
        segs, cur = [], None
        for smp in run.nodes[nid].net:
            if smp['t'] > t_to + 1e-9:
                break
            if a is not None and smp['t'] <= a['t'] + 1e-9 and smp is not a:
                continue
            inst = smp.get('inst', 0)
            if cur is None or cur[0] != inst:
                ref = a if (a is not None and a.get('inst', 0) == inst) else None
                cur = [inst, ref, smp]
                segs.append(cur)
            else:
                cur[2] = smp
        parts = [_net_delta1(ref, last) for _, ref, last in segs if last is not ref] or [_net_delta1(b, b)]
        dt = (b['t_us'] - (a['t_us'] if a else 0)) or 1
        d = {}
        for k in parts[0]:
            v0 = parts[0][k]
            if k in ('mac_q_int', 'al_q_int'):
                d[k] = sum(p[k] for p in parts)
            elif k in ('mac_q_peak', 'al_q_peak'):
                d[k] = max(p[k] for p in parts)
            elif k == 'uc_retry_hist':
                n = max(len(p[k]) for p in parts)
                d[k] = [sum((p[k][i] if i < len(p[k]) else 0) for p in parts) for i in range(n)]
            elif k == 'uc_give_up':
                d[k] = {r: sum(p[k][r] for p in parts) for r in v0}
            elif isinstance(v0, (int, float)) and not isinstance(v0, bool):
                d[k] = sum(p[k] for p in parts)
        d['mac_q_mean'] = d.pop('mac_q_int') / dt
        d['al_q_mean'] = d.pop('al_q_int') / dt
        d['give_up'] = sum(d['uc_give_up'].values())
        d['uc_frames'] = sum(d['uc_retry_hist']) + d['give_up']
        d['mac_q_now'] = _g(b, 'mac', 'q')
        d['al_q_now'] = _g(b, 'al', 'q')
        ok = sum(d['uc_retry_hist'])
        # retries per unicast frame delivered (bin 8 counts 8 or more)
        d['retries'] = sum(i * x for i, x in enumerate(d['uc_retry_hist']))
        d['retries_per_frame'] = (d['retries'] / ok) if ok else None
        d['parent'] = _g(b, 'rpl', 'parent') if b.get('rpl', {}).get('parent') is not None else None
        d['dag_rank'] = b.get('rpl', {}).get('dag_rank')
        d['rank'] = b.get('rpl', {}).get('rank')
        d['instances'] = len(segs)
        out[nid] = d
    return out


def _g(s, *path):
    for p in path:
        if s is None:
            return 0
        s = s.get(p)
    return s or 0


def _net_delta1(a, b):
    """The counters' change from sample a (None: 0, a fresh instance) to b, of one instance."""
    g = _g
    d = {}
    for k in ('tx', 'rx', 'bc_tx', 'retry_attempts', 'cca_att', 'cca_fail', 'rx_drop', 'bc_done'):
        d['mac_' + k] = g(b, 'mac', k) - g(a, 'mac', k)
    hb = g(b, 'mac', 'uc_retry_hist') or []
    ha = g(a, 'mac', 'uc_retry_hist') or [0] * len(hb)
    d['uc_retry_hist'] = [x - y for x, y in zip(hb, ha)]
    d['uc_give_up'] = {k: g(b, 'mac', 'uc_give_up', k) - g(a, 'mac', 'uc_give_up', k)
                       for k in ('no_ack', 'channel_access', 'other')}
    d['sock_tx_fail'] = g(b, 'app', 'sock', 'tx_fail') - g(a, 'app', 'sock', 'tx_fail')
    d['mac_q_int'] = g(b, 'mac', 'q_int_us') - g(a, 'mac', 'q_int_us')
    d['al_q_int'] = g(b, 'al', 'q_int_us') - g(a, 'al', 'q_int_us')
    d['mac_q_peak'] = g(b, 'mac', 'q_max')
    d['al_q_peak'] = g(b, 'al', 'q_max')
    d['red_drop'] = g(b, 'al', 'red_drop') - g(a, 'al', 'red_drop')
    d['no_route'] = g(b, 'ip', 'no_route') - g(a, 'ip', 'no_route')
    d['parent_changes'] = g(b, 'rpl', 'parent_changes') - g(a, 'rpl', 'parent_changes')
    d['parent_tx_fail'] = g(b, 'rpl', 'parent_tx_fail') - g(a, 'rpl', 'parent_tx_fail')
    for k in ('dis', 'dio', 'dao'):
        d['ctl_' + k] = g(b, 'ctl', k) - g(a, 'ctl', k)
    return d


def queue_peaks(run, t_from, t_to):
    """Per node, the highest queue depth (MAC plus adaptation layer) seen in
    (t_from, t_to]: the largest q_win_max of its NET1 samples there (each
    line's peak since the previous line), else the depth at those samples."""
    out = {}
    for nid, node in run.nodes.items():
        best = None
        for smp in node.net:
            if smp['t'] <= t_from + 1e-9:
                continue
            if smp['t'] > t_to + 1e-9:
                break
            v = smp.get('q_win_max')
            if v is None:
                v = _g(smp, 'mac', 'q') + _g(smp, 'al', 'q')
            best = v if best is None else max(best, v)
        if best is not None:
            out[nid] = best
    return out


def node_packets(run, t_from, t_to, t_eval):
    """Per router, its packets sent in [t_from, t_to) (send errors left out):
    those it originated (up, p2p) and, for the down flow, those the border
    router sent to it (the router they concern): sent, lost (not echoed for a request,
    not delivered for a one-way packet, by t_eval), loss, pdr, and the RTT
    statistics (ms) of its echoed packets."""
    acc = {}
    for key, v in run.tx.items():
        flow, src, seq = key
        t_tx, rc = v[0], v[4]
        typ = v[5] if len(v) > 5 else (2 if flow == 3 else 0)
        if not (t_from <= t_tx < t_to) or rc < 0 or flow == 3:
            continue
        who = v[1] if flow == 2 else src
        a = acc.setdefault(who, {'sent': 0, 'lost': 0, '_rtt': []})
        a['sent'] += 1
        if typ == 0:
            e = run.echo.get(key)
            if e and e[0] <= t_eval:
                a['_rtt'].append((e[0] - t_tx) * 1e3)
            else:
                a['lost'] += 1
        elif not any(r[1] <= t_eval for r in run.rx.get(key, [])):
            a['lost'] += 1
    for a in acc.values():
        a['loss'] = a['lost'] / a['sent'] if a['sent'] else None
        a['pdr'] = 1 - a['loss'] if a['loss'] is not None else None
        a['rtt_ms'] = stats(a.pop('_rtt'))
    return acc


def retries_per_frame(deltas):
    """MAC retries per unicast frame delivered, over several nodes' net_delta()."""
    num = den = 0
    for d in deltas:
        h = d.get('uc_retry_hist') or []
        num += sum(i * x for i, x in enumerate(h))
        den += sum(h)
    return num / den if den else None
