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
import copy, json, math, os, random, re, socket, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runlog  # noqa: E402

FORMAT = 'wssim-scenario 1'
RESULT_FORMAT = 'wssim-scenario-result 1'

# --------------------------------------------------------------- vocabulary

METRICS = {
    # name: (unit, description, needs)
    'pdr': ('ratio', 'Packet delivery ratio of the flow: echoes received / requests sent (receptions / sends for multicast)', 'traffic'),
    'rtt.mean_ms': ('ms', 'Mean round-trip time (request and echo)', 'traffic'),
    'rtt.p50_ms': ('ms', 'Median round-trip time', 'traffic'),
    'rtt.p95_ms': ('ms', '95th percentile of the round-trip time', 'traffic'),
    'rtt.max_ms': ('ms', 'Largest round-trip time', 'traffic'),
    'rtt.min_ms': ('ms', 'Smallest round-trip time', 'traffic'),
    'latency.mean_ms': ('ms', 'Mean one-way delay (send to reception at the target)', 'traffic'),
    'latency.p95_ms': ('ms', '95th percentile of the one-way delay', 'traffic'),
    'latency.max_ms': ('ms', 'Largest one-way delay', 'traffic'),
    'offered_pps': ('packets/s', 'Packets sent per second by the flow, whole network', 'traffic'),
    'delivered_pps': ('packets/s', 'Packets delivered (echoed) per second, whole network', 'traffic'),
    'queue.mean_max': ('frames', 'Highest mean queue depth of a node (MAC and adaptation layer) over the window', 'queues'),
    'queue.now_max': ('frames', 'Highest queue depth of a node at the end of the window', 'queues'),
    'queue.drops': ('packets', 'Packets dropped by full adaptation-layer queues (all nodes) in the window', 'queues'),
    'mac.retries_per_frame': ('retries', 'MAC retries per unicast frame, whole network', 'queues'),
    'mac.tx_fail': ('frames', 'Unicast frames given up after the last MAC retry (all nodes) in the window', 'queues'),
    'mac.cca_fail_rate': ('ratio', 'Channel-access failures / CCA attempts, whole network', 'queues'),
    'mesh.joined_fraction': ('ratio', 'Routers with a route to the border router (parent chain)', 'mesh'),
    'mesh.orphans': ('nodes', 'Running routers without a route to the border router', 'mesh'),
    'mesh.parent_changes': ('changes', 'RPL parent changes in the window, all nodes', 'mesh'),
    'mesh.max_hops': ('hops', 'Depth of the deepest router', 'mesh'),
    'power.avg_ma': ('mA', 'Mean over the nodes of their average current in the window (needs POWER)', 'power'),
    'medium.frames_per_s': ('frames/s', 'Frames sent per second, whole network', 'medium'),
    'medium.airtime_s_per_s': ('s/s', 'Airtime of every frame sent per second of the window (sum over all nodes)', 'medium'),
    'medium.loss_share': ('ratio', 'Frames lost at receivers that were listening, over those heard (central medium)', 'medium'),
}
MEASURE_SETS = {
    'traffic': 'Delivery, RTT and one-way delay overall and per hop count, offered and delivered throughput',
    'queues': 'Per node: queue depths, drops, MAC retries, channel-access failures, frames relayed',
    'mesh': 'Topology: parents, depths, orphans, parent changes, RPL control messages',
    'power': 'Per node: charge and average current in the phase, by state (needs POWER)',
    'medium': 'Frames, airtime and reception outcomes of the radio medium',
}
FLOWS = ['up', 'down', 'mcast', 'p2p']
CONDITION_KINDS = ['immediately', 'time', 'elapsed', 'formed', 'node_event', 'repaired', 'metric', 'all', 'any']
NODE_EVENTS = ['joined', 'parent_changed', 'parent_lost', 'restarted']
ACTIONS = ['kill', 'restart', 'leave', 'cut_links', 'partition', 'degrade_links', 'interferer_add',
           'interferer_remove', 'mark']
SELECTS = ['id', 'ids', 'all', 'hop', 'br_children', 'ancestor', 'storey', 'area', 'label']
PICKS = ['all', 'largest_subtree', 'most_children', 'random', 'fraction', 'first']
OPS = ['>=', '>', '<=', '<', '==']

# ------------------------------------------------------------- JSON Schema


def S(t, title, desc, **kw):
    d = {'type': t, 'title': title, 'description': desc}
    d.update(kw)
    return d


def obj(title, desc, props, required=(), **kw):
    d = {'type': 'object', 'title': title, 'description': desc, 'properties': props, 'additionalProperties': False}
    if required:
        d['required'] = list(required)
    d.update(kw)
    return d


SELECTOR = obj('Nodes', 'Which nodes an action acts on, resolved from the topology when the action runs', {
    'select': S('string', 'Selection', 'id: one node; ids: a list; all: every router; hop: the routers at a hop count; '
                'br_children: the border router\'s children (hop 1); ancestor: the node at a hop count on the path of '
                'a given node; storey or area: by position (needs a positions file); label: the nodes an earlier action '
                'picked', enum=SELECTS),
    'id': S('integer', 'Node ID', 'For select id', minimum=0, maximum=4095),
    'ids': S('array', 'Node IDs', 'For select ids', items={'type': 'integer', 'minimum': 0, 'maximum': 4095}),
    'hop': S('integer', 'Hop count', 'For select hop and ancestor: hops to the border router', minimum=1, maximum=64),
    'of': S('integer', 'Of node', 'For select ancestor: the node whose path is taken', minimum=1, maximum=4095),
    'storey': S('integer', 'Storey', 'For select storey: the FLOOR of the positions file (else Z / 3.5 m)'),
    'box': S('array', 'Area', 'For select area: [x0, y0, x1, y1] in metres', items={'type': 'number'},
             minItems=4, maxItems=4),
    'label': S('string', 'Label', 'For select label: the label of an earlier action'),
    'pick': S('string', 'Pick', 'Among the candidates: all; largest_subtree or most_children (the worst case); '
              'random (drawn from the seed); fraction; first (lowest ID)', enum=PICKS, default='all'),
    'count': S('integer', 'Count', 'How many to pick (largest_subtree, most_children, random, first)', minimum=1,
               default=1),
    'fraction': S('number', 'Fraction', 'For pick fraction: the share of the candidates, drawn from the seed',
                  minimum=0, maximum=1),
}, ['select'])

REL = obj('Relative to', 'The value is factor x the same metric (or another one) measured in an earlier phase', {
    'phase': S('string', 'Phase', 'Name of an earlier phase'),
    'metric': S('string', 'Metric', 'The earlier phase\'s metric (default: the condition\'s metric); a capacity search '
                'also has capacity_pps', enum=sorted(METRICS) + ['capacity_pps']),
    'factor': S('number', 'Factor', 'Multiplier of the earlier value', default=1),
}, ['phase'])

CONDITION = obj('Condition', 'A condition on the run, evaluated live at each poll (all fields but "is" depend on the kind)', {
    'is': S('string', 'Kind', 'immediately: true at once; time: virtual time reached; elapsed: time since the '
            'reference (the previous phase\'s end for a start, the phase\'s start for an end); formed: the network '
            'has joined; node_event: a node joined, changed or lost its parent, restarted; repaired: every node '
            'cut off by the last disruption has a route again; metric: a measured value compared with a threshold; '
            'all / any: a combination', enum=CONDITION_KINDS),
    't_s': S('number', 'Time', 'For time: virtual seconds since the run\'s start', minimum=0),
    's': S('number', 'Seconds', 'For elapsed', minimum=0),
    'fraction': S('number', 'Share of nodes', 'For formed: the share of the routers (or of nodes) that must have '
                  'reached the milestone', minimum=0, maximum=1, default=1),
    'milestone': S('string', 'Milestone', 'For formed (default ready) and node_event joined (default operational): '
                   'operational (Wi-SUN join done) or ready (RPL routing ready; not repeated when an interface comes '
                   'back up)', enum=['operational', 'ready']),
    'stable_s': S('number', 'Stable for', 'For formed: and no parent change anywhere for this long (s)', minimum=0,
                  default=0),
    'nodes': SELECTOR,
    'event': S('string', 'Event', 'For node_event', enum=NODE_EVENTS),
    'e2e': S('boolean', 'End to end', 'For repaired: also every cut-off node is in the border router\'s routing '
             'table and got an echo for a packet sent after the disruption (needs a running echoed flow)',
             default=False),
    'metric': S('string', 'Metric', 'For metric', enum=sorted(METRICS)),
    'op': S('string', 'Comparison', 'For metric', enum=OPS, default='>='),
    'value': S('number', 'Value', 'For metric: the threshold (in the metric\'s unit); with relative_to, ignored'),
    'relative_to': REL,
    'per_hop': S('boolean', 'Per hop count', 'For metric (rtt, latency): true if the comparison holds for any hop '
                 'count, each against the same hop count of relative_to', default=False),
    'flow': S('string', 'Flow', 'For traffic metrics: the flow (default: up, else the first one running)',
              enum=FLOWS),
    'window_s': S('number', 'Window', 'For metric: the trailing window of packets sent (s)', minimum=1, default=60),
    'of': S('array', 'Conditions', 'For all / any', items={'$ref': '#/$defs/condition'}),
}, ['is'])

START = copy.deepcopy(CONDITION)
START['title'] = 'Start'
START['description'] = ('When the phase starts (default: right after the previous phase). A condition, with a '
                        'timeout and what to do when it expires')
START['properties']['timeout_s'] = S('number', 'Timeout', 'Give up waiting after this long (s)', minimum=0,
                                     default=3600)
START['properties']['on_timeout'] = S('string', 'On timeout', 'start: start anyway (flagged in the report); skip: '
                                      'skip this phase; abort: end the scenario', enum=['start', 'skip', 'abort'],
                                      default='start')

END = copy.deepcopy(CONDITION)
END['title'] = 'End'
END['description'] = ('When the phase ends: after_s alone (a fixed duration, the default 60 s), or a condition '
                      'with a cap max_s')
END['properties']['after_s'] = S('number', 'Duration', 'Fixed duration of the phase (s)', minimum=0)
END['properties']['max_s'] = S('number', 'Cap', 'With a condition: end after this long anyway (s)', minimum=0,
                               default=600)
END['properties']['is']['default'] = 'elapsed'
END.pop('required')

LOAD = obj('Load', 'The application load from the start of the phase ("off": none; omitted: as before)', {
    'flows': S('array', 'Flows', 'up: routers to border router; down: border router to each router; mcast: border '
               'router to a group; p2p: router to router. Up, down and p2p are echoed',
               items={'type': 'string', 'enum': FLOWS}, default=['up']),
    'rate_pps': S('number', 'Rate', 'Offered packets per second of each flow, whole network', minimum=0.0001),
    'interval_s': S('number', 'Interval', 'Instead of rate_pps: mean interval of each node (s)', minimum=0.001),
    'rate_of': REL,
    'max_rate_pps': S('number', 'Rate cap', 'With interval_s: at most this many packets/s, whole network (a light '
                      'probe whatever the network size)', minimum=0.0001),
    'payload_b': S('integer', 'Payload', 'UDP payload (bytes)', minimum=32, maximum=1232, default=64),
    'dist': S('string', 'Intervals', 'fixed or exp (Poisson)', enum=['fixed', 'exp'], default='exp'),
    'nodes': SELECTOR,
    'echo': S('boolean', 'Echo', 'Targets answer, so that originators measure the RTT', default=True),
}, [])

ACTION = obj('Action', 'Something done at the start of the phase', {
    'do': S('string', 'Action', 'kill: power off; restart: power off and a fresh start; leave: the interface goes '
            'down (a graceful leave); cut_links: every link of the nodes (or to peers) cut; partition: links '
            'between groups a and b cut; degrade_links: every link of the nodes at rssi_dbm; interferer_add, '
            'interferer_remove: external interference (central medium); mark: a note in the logs', enum=ACTIONS),
    'nodes': SELECTOR,
    'peers': SELECTOR,
    'a': SELECTOR,
    'b': SELECTOR,
    'for_s': S('number', 'For', 'Undo after this long (s): restart the killed nodes, interface up, links back, '
               'interferer removed. Omitted: permanent', minimum=0),
    'rssi_dbm': S('number', 'RSSI', 'For degrade_links (dBm)', maximum=0, minimum=-200),
    'spec': S('string', 'Interferer', 'For interferer_add: as wssimserver --interferer (docs/MEDIUM.md §13.1), '
              'e.g. "chan=0-9,level=-75,kind=fsk,pattern=ge:20ms:80ms"'),
    'name': S('string', 'Name', 'For interferer_add and interferer_remove ("all": every one)'),
    'label': S('string', 'Label', 'Name of the nodes this action picked, for later selections (select label)'),
    'text': S('string', 'Text', 'For mark'),
}, ['do'])

MEASURE = obj('Measure', 'What the phase measures (default: traffic)', {
    'sets': S('array', 'Measurements', 'Any of: ' + '; '.join(f'{k}: {v}' for k, v in MEASURE_SETS.items()),
              items={'type': 'string', 'enum': list(MEASURE_SETS)}, default=['traffic']),
    'settle_s': S('number', 'Settle', 'Time after the start not measured (s)', minimum=0, default=0),
}, [])

REPEAT = obj('Repeat', 'Iterations: the load changes at each, until a condition holds (a capacity search)', {
    'max': S('integer', 'Iterations', 'At most this many', minimum=1, maximum=100, default=10),
    'mul': S('number', 'Multiply', 'The rate (or 1/interval) is multiplied by this at each iteration', minimum=0.01,
             default=1.5),
    'add_pps': S('number', 'Add', 'Instead of mul: the rate grows by this at each iteration (packets/s)'),
    'window_s': S('number', 'Window', 'Measured time of an iteration (s)', minimum=1, default=60),
    'settle_s': S('number', 'Settle', 'Time at the new load before the window (s)', minimum=0, default=15),
    'until': {'$ref': '#/$defs/condition', 'description': 'Stop when this holds on an iteration\'s measurements'},
}, ['until'])

PHASE = obj('Phase', 'Start when [start]; set [load]; do [actions]; measure [measure]; end when [end] (or repeat)', {
    'name': S('string', 'Name', 'Unique name, used by later phases'),
    'start': START,
    'actions': S('array', 'Actions', 'Done in order at the start', items=ACTION),
    'load': {'oneOf': [LOAD, {'type': 'string', 'enum': ['off', 'keep']}], 'title': 'Load',
             'description': 'The load from this phase on: settings, "off", or "keep" (the default)'},
    'measure': MEASURE,
    'end': END,
    'repeat': REPEAT,
}, ['name'])

SETTINGS = obj('Settings', 'Defaults of the whole scenario', {
    'poll_s': S('number', 'Poll', 'How often conditions are evaluated while waiting (virtual s)', minimum=0.5,
                default=5),
    'drain_s': S('number', 'Drain', 'Packets sent in the last drain_s before an evaluation are not counted yet (s)',
                 minimum=0, default=10),
    'load': LOAD,
}, [])

BLOCK_DOC = {}   # filled below with the blocks


def block_schema():
    alts = []
    for name, b in BLOCKS.items():
        props = {'use': S('string', 'Block', b['doc'], enum=[name]),
                 'name': S('string', 'Name', 'Phase name (default: the block\'s)')}
        for p, spec in b['params'].items():
            props[p] = spec
        alts.append(obj(b['title'], b['doc'], props, ['use'] + b.get('required', [])))
    return alts


def _refs(x, top=True):
    """The schema with the selector and the relative reference as $refs."""
    if not top and x is SELECTOR:
        return {'$ref': '#/$defs/selector'}
    if not top and x is REL:
        return {'$ref': '#/$defs/relative'}
    if isinstance(x, dict):
        return {k: _refs(v, False) for k, v in x.items()}
    if isinstance(x, list):
        return [_refs(v, False) for v in x]
    return x


def schema():
    sch = _schema()
    sch['$defs'] = {k: _refs(v, False) for k, v in sch['$defs'].items()}
    sch['properties'] = _refs(sch['properties'], False)
    sch['$defs']['selector'] = _refs(SELECTOR)
    sch['$defs']['relative'] = _refs(REL)
    return sch


def _schema():
    return {
        '$schema': 'https://json-schema.org/draft/2020-12/schema',
        '$id': 'wssim-scenario-1',
        'title': 'Emulator measurement scenario',
        'description': 'An ordered list of phases or building blocks (docs/SCENARIOS.md)',
        'type': 'object', 'additionalProperties': False,
        'required': ['format', 'phases'],
        'properties': {
            'format': S('string', 'Format', 'Always "wssim-scenario 1"', const=FORMAT),
            'name': S('string', 'Name', 'Short name of the scenario'),
            'description': S('string', 'Description', 'What it measures'),
            'settings': SETTINGS,
            'phases': S('array', 'Phases', 'In order', minItems=1,
                        items={'oneOf': [{'$ref': '#/$defs/phase'}] + [{'$ref': f'#/$defs/block_{n}'} for n in BLOCKS]}),
        },
        '$defs': dict({'condition': CONDITION, 'phase': PHASE},
                      **{f'block_{n}': a for n, a in zip(BLOCKS, block_schema())}),
    }

# ------------------------------------------------------------------- blocks

SEL_HOP1_WORST = {'select': 'hop', 'hop': 1, 'pick': 'largest_subtree'}

BLOCKS = {
    'formed': {
        'title': 'Wait for the network',
        'doc': 'Wait until the network has formed (all routers ready) and no parent changed for stable_s',
        'params': {
            'fraction': S('number', 'Share of routers', 'Share that must be ready', minimum=0, maximum=1, default=1),
            'stable_s': S('number', 'Stable for', 'No parent change for this long (s)', minimum=0, default=60),
            'timeout_s': S('number', 'Timeout', 'Start anyway after this long (s), flagged', minimum=0, default=3600),
        },
        'expand': lambda p: {
            'name': p.get('name', 'formed'),
            'start': {'is': 'formed', 'fraction': p.get('fraction', 1), 'milestone': 'ready',
                      'stable_s': p.get('stable_s', 60), 'timeout_s': p.get('timeout_s', 3600)},
            'measure': {'sets': ['mesh']}, 'end': {'after_s': 0}},
    },
    'probe': {
        'title': 'Per-hop RTT probe',
        'doc': 'A light load, its RTT, delay and delivery per hop count: the baseline of later phases',
        'params': {
            'rate_pps': S('number', 'Rate', 'Offered packets/s, whole network (default: one packet per router '
                          'every 30 s, at most 0.5 packets/s)', minimum=0.0001),
            'flows': LOAD['properties']['flows'],
            'duration_s': S('number', 'Duration', 'Measured time (s)', minimum=1, default=300),
            'settle_s': S('number', 'Settle', 'Not measured at the start (s)', minimum=0, default=30),
            'payload_b': LOAD['properties']['payload_b'],
        },
        'expand': lambda p: {
            'name': p.get('name', 'probe'),
            'load': dict({'flows': p.get('flows', ['up']), 'payload_b': p.get('payload_b', 64), 'dist': 'exp'},
                         **({'rate_pps': p['rate_pps']} if 'rate_pps' in p else {'interval_s': 30, 'max_rate_pps': 0.5})),
            'measure': {'sets': ['traffic', 'queues', 'mesh', 'power', 'medium'], 'settle_s': p.get('settle_s', 30)},
            'end': {'after_s': p.get('duration_s', 300) + p.get('settle_s', 30)}},
    },
    'capacity': {
        'title': 'Capacity search',
        'doc': 'Raise the load step by step until the RTT reaches factor x the baseline, the largest RTT a '
               'threshold, or the delivery falls below min_pdr; the capacity is the last load before that',
        'params': {
            'baseline': S('string', 'Baseline phase', 'The phase whose RTT is the reference', default='probe'),
            'start_rate_pps': S('number', 'First rate', 'Packets/s of the first step (default: 2 x the baseline\'s)',
                                minimum=0.0001),
            'mul': S('number', 'Step factor', 'Rate multiplied by this at each step', minimum=1.01, default=1.5),
            'max_steps': S('integer', 'Steps', 'At most this many', minimum=1, maximum=100, default=12),
            'window_s': S('number', 'Window', 'Measured time of a step (s)', minimum=1, default=60),
            'settle_s': S('number', 'Settle', 'Time at the new load before measuring (s)', minimum=0, default=15),
            'rtt_metric': S('string', 'RTT metric', 'The RTT compared with the baseline',
                            enum=['rtt.mean_ms', 'rtt.p50_ms', 'rtt.p95_ms'], default='rtt.p95_ms'),
            'rtt_factor': S('number', 'RTT factor', 'Saturated when the RTT reaches this x the baseline', minimum=1,
                            default=2),
            'max_rtt_ms': S('number', 'RTT ceiling', 'Saturated when the largest RTT reaches this (ms)', minimum=1),
            'min_pdr': S('number', 'Delivery floor', 'Saturated when the delivery ratio falls below this',
                         minimum=0, maximum=1, default=0.95),
            'flows': LOAD['properties']['flows'],
        },
        'expand': lambda p: _expand_capacity(p),
    },
    'interference': {
        'title': 'Interference',
        'doc': 'An external interferer from the start of the phase, under a share of the capacity found',
        'required': ['spec'],
        'params': {
            'spec': ACTION['properties']['spec'],
            'interferer_name': S('string', 'Interferer name', 'Its name, to remove it later', default='jam'),
            'capacity_phase': S('string', 'Capacity phase', 'Where the capacity comes from', default='capacity'),
            'load_fraction': S('number', 'Load', 'Share of the capacity offered', minimum=0, maximum=2, default=0.8),
            'duration_s': S('number', 'Duration', 'Length of the phase (s)', minimum=1, default=300),
            'settle_s': S('number', 'Settle', 'Not measured at the start (s)', minimum=0, default=30),
        },
        'expand': lambda p: {
            'name': p.get('name', 'interference'),
            'actions': [{'do': 'interferer_add', 'spec': p['spec'], 'name': p.get('interferer_name', 'jam')}],
            'load': {'rate_of': {'phase': p.get('capacity_phase', 'capacity'), 'metric': 'capacity_pps',
                                 'factor': p.get('load_fraction', 0.8)}},
            'measure': {'sets': ['traffic', 'queues', 'mesh', 'power', 'medium'], 'settle_s': p.get('settle_s', 30)},
            'end': {'after_s': p.get('duration_s', 300)}},
    },
    'recovery': {
        'title': 'Recovery',
        'doc': 'Remove the interferer (if any) at the same load and measure until the RTT is back near the baseline',
        'params': {
            'interferer_name': S('string', 'Interferer name', 'The interferer removed ("all": every one; "": none)',
                                 default='all'),
            'baseline': S('string', 'Baseline phase', 'The reference RTT', default='probe'),
            'within': S('number', 'Back within', 'Ends when the RTT is at most this x the baseline', minimum=1,
                        default=1.2),
            'max_s': S('number', 'Cap', 'At most this long (s)', minimum=1, default=600),
        },
        'expand': lambda p: {
            'name': p.get('name', 'recovery'),
            'actions': ([{'do': 'interferer_remove', 'name': p.get('interferer_name', 'all')}]
                        if p.get('interferer_name', 'all') else []),
            'measure': {'sets': ['traffic', 'queues', 'mesh', 'power', 'medium']},
            'end': {'is': 'metric', 'metric': 'rtt.p95_ms', 'op': '<=', 'window_s': 60,
                    'relative_to': {'phase': p.get('baseline', 'probe'), 'factor': p.get('within', 1.2)},
                    'max_s': p.get('max_s', 600)}},
    },
    'node_failure': {
        'title': 'Node failure',
        'doc': 'Power off nodes (default: the hop-1 router with the largest subtree) under a share of the capacity, '
               'and measure until the mesh is repaired',
        'params': {
            'nodes': SELECTOR,
            'label': S('string', 'Label', 'Name of the failed nodes, for a later restart', default='failed'),
            'capacity_phase': S('string', 'Capacity phase', 'Where the capacity comes from ("": keep the load)',
                                default='capacity'),
            'load_fraction': S('number', 'Load', 'Share of the capacity offered', minimum=0, maximum=2, default=0.5),
            'max_s': S('number', 'Cap', 'At most this long (s)', minimum=1, default=600),
            'e2e': CONDITION['properties']['e2e'],
        },
        'expand': lambda p: dict({
            'name': p.get('name', 'node_failure'),
            'actions': [{'do': 'kill', 'nodes': p.get('nodes', SEL_HOP1_WORST), 'label': p.get('label', 'failed')}],
            'measure': {'sets': ['traffic', 'queues', 'mesh', 'power', 'medium']},
            'end': {'is': 'repaired', 'e2e': p.get('e2e', True), 'max_s': p.get('max_s', 600)}},
            **({'load': {'rate_of': {'phase': p.get('capacity_phase', 'capacity'), 'metric': 'capacity_pps',
                                     'factor': p.get('load_fraction', 0.5)}}}
               if p.get('capacity_phase', 'capacity') else {})),
    },
    'node_restart': {
        'title': 'Node restart',
        'doc': 'Start the failed nodes again (fresh instances) and measure until they are ready again',
        'params': {
            'label': S('string', 'Label', 'The nodes of an earlier action', default='failed'),
            'max_s': S('number', 'Cap', 'At most this long (s)', minimum=1, default=600),
        },
        'expand': lambda p: {
            'name': p.get('name', 'node_restart'),
            'actions': [{'do': 'restart', 'nodes': {'select': 'label', 'label': p.get('label', 'failed')}}],
            'measure': {'sets': ['traffic', 'queues', 'mesh', 'power', 'medium']},
            'end': {'is': 'node_event', 'event': 'joined',
                    'nodes': {'select': 'label', 'label': p.get('label', 'failed')}, 'max_s': p.get('max_s', 600)}},
    },
    'capacity_check': {
        'title': 'Capacity again',
        'doc': 'A shorter capacity search from a share of an earlier capacity (e.g. after a repair)',
        'params': {
            'capacity_phase': S('string', 'Earlier capacity', 'The capacity it starts from', default='capacity'),
            'start_fraction': S('number', 'First rate', 'Share of the earlier capacity of the first step',
                                minimum=0.01, default=0.5),
            'baseline': S('string', 'Baseline phase', 'The reference RTT', default='probe'),
            'mul': S('number', 'Step factor', 'Rate multiplied by this at each step', minimum=1.01, default=1.5),
            'max_steps': S('integer', 'Steps', 'At most this many', minimum=1, maximum=100, default=8),
            'window_s': S('number', 'Window', 'Measured time of a step (s)', minimum=1, default=60),
            'settle_s': S('number', 'Settle', 'Time at the new load before measuring (s)', minimum=0, default=15),
            'rtt_factor': S('number', 'RTT factor', 'Saturated when the RTT reaches this x the baseline', minimum=1,
                            default=2),
            'min_pdr': S('number', 'Delivery floor', 'Saturated below this delivery ratio', minimum=0, maximum=1,
                         default=0.95),
        },
        'expand': lambda p: _expand_capacity(dict(p, name=p.get('name', 'capacity_check'),
                                                  start_rate_of={'phase': p.get('capacity_phase', 'capacity'),
                                                                 'metric': 'capacity_pps',
                                                                 'factor': p.get('start_fraction', 0.5)},
                                                  max_steps=p.get('max_steps', 8))),
    },
}


def _expand_capacity(p):
    base = p.get('baseline', 'probe')
    metric = p.get('rtt_metric', 'rtt.p95_ms')
    until = [{'is': 'metric', 'metric': metric, 'op': '>=',
              'relative_to': {'phase': base, 'factor': p.get('rtt_factor', 2)}, 'window_s': p.get('window_s', 60)},
             {'is': 'metric', 'metric': 'pdr', 'op': '<', 'value': p.get('min_pdr', 0.95),
              'window_s': p.get('window_s', 60)}]
    if 'max_rtt_ms' in p:
        until.append({'is': 'metric', 'metric': 'rtt.max_ms', 'op': '>=', 'value': p['max_rtt_ms'],
                      'window_s': p.get('window_s', 60)})
    load = {'flows': p.get('flows', ['up']), 'payload_b': 64, 'dist': 'exp'}
    if 'start_rate_of' in p:
        load['rate_of'] = p['start_rate_of']
    elif 'start_rate_pps' in p:
        load['rate_pps'] = p['start_rate_pps']
    else:
        load['rate_of'] = {'phase': base, 'metric': 'offered_pps', 'factor': 2}
    return {'name': p.get('name', 'capacity'), 'load': load,
            'measure': {'sets': ['traffic', 'queues', 'mesh', 'power', 'medium']},
            'repeat': {'max': p.get('max_steps', 12), 'mul': p.get('mul', 1.5), 'window_s': p.get('window_s', 60),
                       'settle_s': p.get('settle_s', 15), 'until': {'is': 'any', 'of': until}},
            'x_capacity': True}

# --------------------------------------------------------------- validation


def _err(errs, path, msg):
    errs.append({'path': path, 'message': msg})


def _resolve(ref, root):
    node = root
    for p in ref.lstrip('#/').split('/'):
        node = node[p]
    return node


def _validate(v, sch, path, errs, root):
    if '$ref' in sch:
        sch = dict(_resolve(sch['$ref'], root), **{k: x for k, x in sch.items() if k != '$ref'})
    if 'oneOf' in sch:
        alts = [(_resolve(a['$ref'], root) if '$ref' in a else a) for a in sch['oneOf']]
        # A discriminator: "use" for blocks, else the type
        if isinstance(v, dict) and 'use' in v:
            for a in alts:
                if a.get('properties', {}).get('use', {}).get('enum') == [v['use']]:
                    return _validate(v, a, path, errs, root)
            _err(errs, path + '/use', f'unknown block "{v["use"]}" (known: {", ".join(BLOCKS)})')
            return
        for a in alts:
            if (a.get('type') == 'object') == isinstance(v, dict) and 'use' not in a.get('properties', {}):
                return _validate(v, a, path, errs, root)
        _err(errs, path, 'not a valid value here')
        return
    t = sch.get('type')
    types = {'object': dict, 'array': list, 'string': str, 'boolean': bool}
    if t == 'integer':
        if not isinstance(v, int) or isinstance(v, bool):
            return _err(errs, path, 'must be a whole number')
    elif t == 'number':
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
            return _err(errs, path, 'must be a number')
    elif t in types and not isinstance(v, types[t]):
        return _err(errs, path, f'must be {"an" if t[0] in "aeio" else "a"} {t}')
    if 'const' in sch and v != sch['const']:
        return _err(errs, path, f'must be "{sch["const"]}"')
    if 'enum' in sch and v not in sch['enum']:
        return _err(errs, path, f'must be one of: {", ".join(map(str, sch["enum"]))}')
    if t in ('integer', 'number'):
        if 'minimum' in sch and v < sch['minimum']:
            _err(errs, path, f'must be at least {sch["minimum"]}')
        if 'maximum' in sch and v > sch['maximum']:
            _err(errs, path, f'must be at most {sch["maximum"]}')
    if t == 'array':
        if 'minItems' in sch and len(v) < sch['minItems']:
            _err(errs, path, f'needs at least {sch["minItems"]} entries')
        if 'maxItems' in sch and len(v) > sch['maxItems']:
            _err(errs, path, f'has at most {sch["maxItems"]} entries')
        if 'items' in sch:
            for i, x in enumerate(v):
                _validate(x, sch['items'], f'{path}/{i}', errs, root)
    if t == 'object':
        props = sch.get('properties', {})
        for k in sch.get('required', []):
            if k not in v:
                _err(errs, f'{path}/{k}', 'is required')
        for k, x in v.items():
            if k not in props:
                if sch.get('additionalProperties') is False and not k.startswith('x_'):
                    _err(errs, f'{path}/{k}', f'unknown field (known: {", ".join(props)})')
                continue
            _validate(x, props[k], f'{path}/{k}', errs, root)


def expand(doc):
    """The phases, blocks expanded, settings applied."""
    out = []
    for i, p in enumerate(doc.get('phases', [])):
        if isinstance(p, dict) and 'use' in p and p['use'] in BLOCKS:
            q = BLOCKS[p['use']]['expand'](p)
            q['x_block'] = p['use']
            q['x_index'] = i
            out.append(q)
        elif isinstance(p, dict):
            q = copy.deepcopy(p)
            q['x_index'] = i
            out.append(q)
    return out


def _check_condition(c, path, errs, earlier, labels, where):
    if not isinstance(c, dict):
        return
    k = c.get('is')
    if k in ('all', 'any'):
        if not c.get('of'):
            _err(errs, path + '/of', 'needs at least one condition')
        for i, x in enumerate(c.get('of') or []):
            _check_condition(x, f'{path}/of/{i}', errs, earlier, labels, where)
        return
    need = {'time': ['t_s'], 'elapsed': ['s'], 'node_event': ['event', 'nodes'], 'metric': ['metric']}
    for f in need.get(k, []):
        if f not in c:
            _err(errs, f'{path}/{f}', f'is required for "{k}"')
    if k == 'metric' and 'value' not in c and 'relative_to' not in c:
        _err(errs, path, 'a metric condition needs a value or relative_to')
    rel = c.get('relative_to')
    if isinstance(rel, dict) and rel.get('phase') not in earlier:
        _err(errs, path + '/relative_to/phase', f'"{rel.get("phase")}" is not an earlier phase')
    if c.get('per_hop') and not str(c.get('metric', '')).startswith(('rtt.', 'latency.')):
        _err(errs, path + '/per_hop', 'only for rtt and latency metrics')
    if k == 'repaired' and where == 'start' and not labels.get('__disruption'):
        pass
    if 'nodes' in c:
        _check_selector(c['nodes'], path + '/nodes', errs, labels)


def _check_selector(s, path, errs, labels):
    if not isinstance(s, dict):
        return
    k = s.get('select')
    need = {'id': ['id'], 'ids': ['ids'], 'hop': ['hop'], 'ancestor': ['of', 'hop'], 'storey': ['storey'],
            'area': ['box'], 'label': ['label']}
    for f in need.get(k, []):
        if f not in s:
            _err(errs, f'{path}/{f}', f'is required for select "{k}"')
    if s.get('pick') == 'fraction' and 'fraction' not in s:
        _err(errs, path + '/fraction', 'is required for pick "fraction"')
    if k == 'label' and s.get('label') not in labels:
        _err(errs, path + '/label', f'no earlier action has the label "{s.get("label")}"')


def check(doc):
    errs = []
    sch = schema()
    if not isinstance(doc, dict):
        return [{'path': '', 'message': 'a scenario is a JSON object'}]
    _validate(doc, sch, '', errs, sch)
    if errs:
        return errs
    earlier, labels = {}, {}
    names = set()
    for p in expand(doc):
        i = p['x_index']
        path = f'/phases/{i}'
        name = p.get('name')
        if name in names:
            _err(errs, path + '/name', f'the name "{name}" is used twice')
        names.add(name)
        if 'end' in p and 'repeat' in p:
            _err(errs, path, 'a phase has either end or repeat, not both')
        if isinstance(p.get('start'), dict):
            _check_condition(p['start'], path + '/start', errs, earlier, labels, 'start')
        load = p.get('load')
        if isinstance(load, dict):
            n = sum(1 for k in ('rate_pps', 'interval_s', 'rate_of') if k in load)
            if n > 1:
                _err(errs, path + '/load', 'give one of rate_pps, interval_s and rate_of')
            ro = load.get('rate_of')
            if isinstance(ro, dict):
                if ro.get('phase') not in earlier:
                    _err(errs, path + '/load/rate_of/phase', f'"{ro.get("phase")}" is not an earlier phase')
                elif ro.get('metric') == 'capacity_pps' and 'repeat' not in earlier[ro['phase']]:
                    _err(errs, path + '/load/rate_of/metric', f'"{ro["phase"]}" is not a capacity search (a phase with repeat)')
            if 'nodes' in load:
                _check_selector(load['nodes'], path + '/load/nodes', errs, labels)
        for j, a in enumerate(p.get('actions') or []):
            ap = f'{path}/actions/{j}'
            do = a.get('do')
            if do in ('kill', 'restart', 'leave', 'cut_links', 'degrade_links') and 'nodes' not in a:
                _err(errs, ap + '/nodes', f'is required for "{do}"')
            if do == 'partition' and ('a' not in a or 'b' not in a):
                _err(errs, ap, 'partition needs a and b')
            if do == 'degrade_links' and 'rssi_dbm' not in a:
                _err(errs, ap + '/rssi_dbm', 'is required for degrade_links')
            if do == 'interferer_add':
                spec = a.get('spec', '')
                if not spec:
                    _err(errs, ap + '/spec', 'is required for interferer_add')
                for kv in spec.split(','):
                    if kv and ('=' not in kv or kv.split('=')[0] not in INTERFERER_KEYS):
                        _err(errs, ap + '/spec', f'"{kv}": expected KEY=VALUE with KEY one of {", ".join(INTERFERER_KEYS)}')
                if 'chan=' not in spec:
                    _err(errs, ap + '/spec', 'needs chan=')
            if do == 'interferer_remove' and 'name' not in a:
                _err(errs, ap + '/name', 'is required for interferer_remove')
            for f in ('nodes', 'peers', 'a', 'b'):
                if f in a:
                    _check_selector(a[f], f'{ap}/{f}', errs, labels)
            if a.get('label'):
                labels[a['label']] = True
        if isinstance(p.get('end'), dict):
            _check_condition(p['end'], path + '/end', errs, earlier, labels, 'end')
        if isinstance(p.get('repeat'), dict):
            _check_condition(p['repeat'].get('until'), path + '/repeat/until', errs, dict(earlier, **{name: p}),
                             labels, 'repeat')
        earlier[name] = p
    # A block's errors point at the block's entry: its expanded fields are
    # not in the file
    out = []
    blocks = {f'/phases/{p["x_index"]}': p.get('x_block') for p in expand(doc) if p.get('x_block')}
    for e in errs:
        root = '/'.join(e['path'].split('/')[:3])
        if root in blocks and e['path'] != root:
            e = {'path': root, 'message': f'block {blocks[root]}: {e["path"][len(root) + 1:]} {e["message"]}'}
        out.append(e)
    return out


INTERFERER_KEYS = ['chan', 'level', 'at', 'pos', 'eirp', 'pattern', 'hop', 'kind', 'rate', 'pre', 'adj', 'name',
                   'from', 'until']


def dry(doc):
    """The expanded phases and an upper bound of the virtual duration."""
    st = doc.get('settings', {})
    total, rows = 0.0, []
    for p in expand(doc):
        start = p.get('start')
        wait = 0.0
        if isinstance(start, dict) and start.get('is') not in (None, 'immediately'):
            wait = start.get('timeout_s', 3600)
            if start.get('is') == 'elapsed':
                wait = min(wait, start.get('s', 0))
        if 'repeat' in p:
            r = p['repeat']
            body = r.get('max', 10) * (r.get('settle_s', 15) + r.get('window_s', 60) + st.get('drain_s', 10))
        else:
            e = p.get('end', {'after_s': 60})
            body = e['after_s'] if 'after_s' in e and 'is' not in e else e.get('max_s', 600)
        for a in p.get('actions') or []:
            if a.get('do') == 'restart':
                body += 0
        total += wait + body
        rows.append({'name': p['name'], 'block': p.get('x_block'), 'wait_max_s': wait, 'run_max_s': body,
                     'phase': {k: v for k, v in p.items() if not k.startswith('x_')}})
    return {'ok': True, 'phases': rows, 'virtual_s_max': total}

# --------------------------------------------------------------------- driver


class Abort(Exception):
    pass


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
        self.disruption = None
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
        self.graph = None           # hearing graph {node: set of neighbours}, asked once
        self.cut = set()            # (src, dst) links cut by the scenario

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

    def run_until(self, t):
        """Let virtual time go to t (s); the logs are then read."""
        us = int(round(t * 1e6))
        r = self.cmd(f'run_until {max(us, int(round(self.now * 1e6)))}')
        if not r.startswith('at '):
            raise Abort('unexpected answer: ' + r)
        self.now = int(r.split()[1]) / 1e6
        self.run.update()

    def advance(self, t):
        """Go to t, doing the undo actions due on the way."""
        while True:
            due = [p for p in self.pending if p[0] <= t]
            if not due:
                break
            p = min(due, key=lambda x: x[0])
            self.pending.remove(p)
            if p[0] > self.now:
                self.run_until(p[0])
            self.note(f'undo: {p[1]}')
            p[2]()
        if t > self.now:
            self.run_until(t)

    def refresh(self, net=True, tree=False):
        """Ask the nodes for their NET1 line (and the border router for its
        table) now, and read them."""
        if net:
            self.cmd('node all stats')
        if tree:
            self.cmd('node 0 tree')
        self.run_until(self.now)

    def note(self, text):
        self.record['events'].append({'t_s': round(self.now, 6), 'text': text})
        try:
            self.cmd('log ' + text.replace('\n', ' '))
        except Abort:
            raise
        print(f'scenario {self.now:9.3f} s: {text}', file=self.log, flush=True)

    def counters(self):
        r = self.cmd('counters')
        out = {}
        for kv in r.split()[1:]:
            k, _, v = kv.partition('=')
            try:
                out[k] = int(v)
            except ValueError:
                pass
        return out

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
        else:
            cand, why = [], 'nothing'
        cand = sorted(set(cand))
        pick, count = sel.get('pick', 'all'), sel.get('count', 1)
        sub = {n: len(runlog.subtree(par, n)) for n in cand}
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
        return cand, why_prefix + why

    # --------------------------------------------------------------- load

    def apply_load(self, spec, phase_name, iteration=None, rate=None):
        """Send the load settings to every node. rate (packets/s) overrides."""
        if spec == 'off':
            self.cmd('node all load stop')
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
                if v is None:
                    raise Abort(f'phase {ro["phase"]} has no {ro.get("metric")}')
                rate = v * ro.get('factor', 1)
            elif 'rate_pps' in base:
                rate = base['rate_pps']
            else:
                rate = m / base.get('interval_s', 30)
                if base.get('max_rate_pps'):
                    rate = min(rate, base['max_rate_pps'])
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
        self.cmd('node all load set ' + self.load_args)
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

    def do_action(self, a, rec):
        do = a['do']
        out = {'do': do}
        if do == 'mark':
            self.note('mark: ' + a.get('text', ''))
        elif do == 'interferer_add':
            spec = a['spec'] + (f',name={a["name"]}' if a.get('name') and 'name=' not in a['spec'] else '')
            r = self.cmd('intf add ' + spec)
            out['result'] = r
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
            else:
                nodes, why = self.select(a['nodes'])
            out.update({'nodes': nodes, 'why': why})
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
                self.cmd(f'node {compress_ids(nodes)} iface down')
                if a.get('for_s'):
                    def up(nodes=nodes):
                        self.cmd(f'node {compress_ids(nodes)} iface up')
                        self.release(nodes)
                    self.pending.append((self.now + a['for_s'], f'interface up {compress_ids(nodes)}', up))
            elif do in ('cut_links', 'partition', 'degrade_links'):
                pairs = []
                if do == 'partition':
                    pairs = [(x, y) for x in ga for y in gb if x != y]
                else:
                    peers = set(self.select(a['peers'])[0]) if a.get('peers') else None
                    for n in nodes:
                        nb = [int(x) for x in self.cmd(f'neighbors {n}').split()[1:]]
                        pairs += [(n, m) for m in nb if peers is None or m in peers]
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
            if do in ('kill', 'restart', 'leave', 'cut_links', 'partition', 'degrade_links') and do != 'restart':
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
                reach = self.reachable(failed)
                isolated = sorted(n for n in affected if n not in reach)
                affected = {n for n in affected if n in reach}
                self.disruption = {'t_s': self.now, 'do': do, 'failed': sorted(failed), 'affected': sorted(affected),
                                   'isolated': isolated,
                                   'parents_before': {str(k): v for k, v in before.items()},
                                   'depths_before': {str(k): v for k, v in dep.items() if v is not None},
                                   'detect_t_s': None, 'repair_t_s': None, 'orphan_since': {}, 'orphan_s': {}}
                out['affected'] = sorted(affected)
                out['isolated'] = isolated
                rec.setdefault('disruptions', []).append(self.disruption)
        rec.setdefault('actions', []).append(out)

    def release(self, nodes):
        """Failed nodes back (restarted, interface up): the repair of the
        disruption in progress may then go through them again."""
        d = self.disruption
        if d:
            back = [n for n in nodes if n in d['failed']]
            d['failed'] = [n for n in d['failed'] if n not in back]
            if back:
                d.setdefault('back', []).extend({'node': n, 't_s': round(self.now, 3)} for n in back)

    def reachable(self, failed=()):
        """The nodes that can still reach the border router through the hearing
        graph, without the failed, stopped or cut parts."""
        if self.graph is None:
            self.graph = {}
            for n in sorted(set(self.run.nodes) | {0}):
                r = self.cmd(f'neighbors {n}')
                self.graph[n] = {int(x) for x in r.split()[1:]}
        down = set(failed) | self.killed
        seen, todo = {0}, [0]
        while todo:
            x = todo.pop()
            for y in self.graph.get(x, ()):
                # y reaches the BR through x if frames go both ways
                if y in seen or y in down or (y, x) in self.cut or (x, y) in self.cut:
                    continue
                seen.add(y)
                todo.append(y)
        return seen

    def _kill(self, n):
        r = self.cmd(f'kill {n}')
        if r.startswith('ok'):
            self.killed.add(n)

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
            self.release([n])
            # The new instance gets the load in force (its step tag included)
            if getattr(self, 'load_args', None):
                self.cmd(f'node {n} load set {self.load_args}')
            self.run.update()
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
                tx = sum(d['mac_tx'] - d['mac_bc_tx'] for d in nd.values())
                cca = sum(d['mac_cca_att'] for d in nd.values())
                qm = max(nd.items(), key=lambda kv: kv[1]['mac_q_mean'] + kv[1]['al_q_mean'], default=(None, None))
                m['queues'] = {
                    'mean_max': (qm[1]['mac_q_mean'] + qm[1]['al_q_mean']) if qm[1] else None, 'mean_max_node': qm[0],
                    'now_max': max((d['mac_q_now'] + d['al_q_now'] for d in nd.values()), default=None),
                    'drops': sum(d['red_drop'] for d in nd.values()),
                    'retries_per_frame': sum(d['mac_retries'] for d in nd.values()) / tx if tx > 0 else None,
                    'tx_fail': sum(d['mac_tx_fail'] for d in nd.values()),
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
                    'parent_changes': sum(nd[n]['parent_changes'] for n in routers),
                    'control_messages': {k: sum(nd[n].get('ctl_' + k, 0) for n in nd) for k in ('dis', 'dio', 'dao')},
                    'parents': {str(k): v for k, v in par.items()},
                }
                t_tree, tree = self.run.br_tree(t_eval)
                m['mesh']['br_table_size'] = len(tree) if t_tree is not None else None
        if 'medium' in sets and ctr_a and ctr_b:
            span = max(ctr_b.get('t_us', 0) - ctr_a.get('t_us', 0), 1) / 1e6
            d = {k: ctr_b.get(k, 0) - ctr_a.get(k, 0) for k in ctr_b}
            heard = sum(v for k, v in d.items() if k in ('ok', 'noise', 'interference', 'captured', 'below_lock',
                                                         'busy', 'tx_abort', 'left'))
            m['medium'] = {'span_s': span, 'frames': d.get('frames_in'), 'frames_per_s': d.get('frames_in', 0) / span,
                           'airtime_s_per_s': d.get('airtime_us', 0) / 1e6 / span,
                           'loss_share': (heard - d.get('ok', 0)) / heard if heard else None,
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
            vals = [v['avg_ma'] for k, v in per.items() if k != '0']
            m['power'] = {'span_s': span, 'avg_ma': sum(vals) / len(vals) if vals else None,
                          'max_ma': max(vals) if vals else None, 'per_node': per}
        return m

    def metric_value(self, m, name, flow, hop=None):
        """A metric of the vocabulary from window metrics m."""
        if name == 'capacity_pps':
            return m.get('capacity_pps')
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
            return {'retries_per_frame': q.get('retries_per_frame'), 'tx_fail': q.get('tx_fail'),
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
                if any(t >= ref_t for t in node.restart_t if t is not None) or self.instances.get(n):
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
        d = self.disruption
        if not d:
            return True, 'no disruption'
        par = {n: p for n, p in self.run.parents(self.now).items() if n not in self.killed}
        dep = runlog.depths(par)
        bad = set(d['failed'])
        broken = []
        for n in d['affected']:
            if n in self.killed:
                continue
            chain, x, ok = [], n, True
            while x not in (0, None):
                if x in bad or x in chain:
                    ok = False
                    break
                chain.append(x)
                x = par.get(x)
            if x is None or dep.get(n) is None:
                ok = False
            if ok and c.get('e2e'):
                t_tree, tree = self.run.br_tree(self.now)
                if n not in tree or tree.get(n) in bad:
                    ok = False
                fl = (self.load or {}).get('flows') or []
                if ok and 'up' in fl and n in (self.load or {}).get('member_ids', []):
                    if not any(src == n and e[0] <= self.now and self.run.tx[(f, src, seq)][0] >= d['t_s']
                               for (f, src, seq), e in self.run.echo.items() if f == 1 and src == n):
                        ok = False
            if not ok:
                broken.append(n)
                d['orphan_since'].setdefault(str(n), self.now)
            elif str(n) in d['orphan_since'] and str(n) not in d['orphan_s']:
                d['orphan_s'][str(n)] = round(self.now - d['t_s'], 3)
        changed = [n for n in d['affected'] if str(n) in d['parents_before'] and par.get(n) != d['parents_before'][str(n)]]
        if changed and d['detect_t_s'] is None:
            d['detect_t_s'] = round(self.now - d['t_s'], 3)
        ok = not broken
        if ok and d['repair_t_s'] is None:
            d['repair_t_s'] = round(self.now - d['t_s'], 3)
        return ok, f'{len(d["affected"]) - len(broken)}/{len(d["affected"])} cut-off routers have a route again'

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
            t_to = self.now - self.drain
            if t_to - w < ctx.get('t_start', 0) - 1e-9:
                return False, f'{c["metric"]}: window of {w} s not full yet'
            sets = {METRICS[c['metric']][2]}
            if 'queues' in sets or 'mesh' in sets:
                self.refresh(net=True)
            ca = ctx.get('ctr_prev')
            cb = self.counters() if 'medium' in sets else None
            m = self.window_metrics(max(t_to - w, ctx.get('t_start', 0)), t_to, self.now, sets, ca, cb)
            if cb:
                ctx['ctr_prev'] = cb
        flow = self.flow_of(c)
        op = c.get('op', '>=')
        hops = [None]
        if c.get('per_hop'):
            f = (m.get('flows') or {}).get(flow, {})
            hops = sorted(int(h) for h in f.get('rtt_ms_by_hops' if c['metric'].startswith('rtt') else
                                                  'latency_ms_by_hops', {}))
        for h in hops:
            v = self.metric_value(m, c['metric'], flow, h)
            thr = self.ref_value(c['relative_to'], c['metric'], h) if c.get('relative_to') else c.get('value')
            if v is None or thr is None:
                continue
            if self.compare(v, op, thr):
                return True, f'{c["metric"]}{"" if h is None else f" at {h} hops"} = {v:.4g} {op} {thr:.4g}'
        v = self.metric_value(m, c['metric'], flow)
        return False, f'{c["metric"]} = {v if v is None else round(v, 4)}'

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

    def run_phase(self, p, prev_end):
        rec = {'name': p['name'], 'block': p.get('x_block'), 'index': p['x_index']}
        self.record['phases'].append(rec)
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
                    rec['skipped'] = True
                    return
                if how == 'abort':
                    rec['aborted'] = True
                    raise Abort(f'phase {p["name"]}: start timeout')
        else:
            rec['start'] = {'t_s': round(self.now, 6), 'met': True, 'detail': 'right after the previous phase'}
        t0 = self.now
        self.note(f'phase {p["name"]} starts ({rec["start"]["detail"]})')
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
        # The load first, then the actions: a failure happens under the load
        load = p.get('load', 'keep')
        if 'repeat' not in p and load != 'keep':
            self.apply_load(load, p['name'])
        for a in p.get('actions') or []:
            self.do_action(a, rec)
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
            self.results[p['name']] = {'metrics': m, 'flow': self.flow_of(None),
                                       'offered_pps': self.load and self.load['rate_pps']}
        if self.disruption and rec.get('disruptions'):
            self.finish_disruption(rec)
        self.note(f'phase {p["name"]} ends ({rec.get("end", {}).get("detail", "")})')

    def _snap(self, tag):
        if not self.power:
            return None
        path = os.path.join(self.dir, 'power_snap.jsonl')
        n = sum(1 for _ in open(path)) if os.path.exists(path) else 0
        self.power_snap(tag)
        with open(path) as f:
            lines = f.readlines()
        return json.loads(lines[n]) if len(lines) > n else None

    def run_iterations(self, p, rec, sets, load):
        r = p['repeat']
        until = r['until']
        rate = None
        if load not in ('keep', 'off') and load is not None:
            spec = load
        else:
            spec = (self.load or {}).get('spec', {})
        rec['iterations'] = []
        met_at = None
        direction = 1               # up; down (-1) when the first rate is already over the condition
        rate = None
        for i in range(r.get('max', 10)):
            if i == 0:
                self.apply_load(spec, p['name'], iteration=0)
            else:
                cur = self.load['rate_pps']
                if direction > 0:
                    rate = cur * r['mul'] if 'add_pps' not in r else cur + r['add_pps']
                else:
                    rate = cur / r['mul'] if 'add_pps' not in r else max(cur - r['add_pps'], cur / 2)
                self.apply_load(spec, p['name'], iteration=i, rate=rate)
            t_it = self.now
            self.advance(t_it + r.get('settle_s', 15))
            if 'mesh' in sets or 'queues' in sets:
                self.refresh(net=True)
            snap0 = self._snap(f'{p["name"]}:{i}:start')
            ctr0 = self.counters() if 'medium' in sets else None
            t_a = self.now
            self.advance(t_a + r.get('window_s', 60) + self.drain)
            if 'mesh' in sets or 'queues' in sets:
                self.refresh(net=True, tree=True)
            snap1 = self._snap(f'{p["name"]}:{i}:end')
            ctr1 = self.counters() if 'medium' in sets else None
            m = self.window_metrics(t_a, t_a + r.get('window_s', 60), self.now, sets, ctr0, ctr1, snap0, snap1)
            ok, detail = self.cond(until, t_it, {'metrics': m})
            it = {'iteration': i, 'step_tag': self.step, 'rate_pps': self.load['rate_pps'],
                  'interval_s': self.load['interval_s'], 't_s': round(t_it, 6), 'met': ok, 'detail': detail,
                  'direction': 'up' if direction > 0 else 'down', 'metrics': m}
            rec['iterations'].append(it)
            self.note(f'phase {p["name"]} iteration {i}: {self.load["rate_pps"]:.4g} packets/s: {detail}'
                      + (' (condition met)' if ok else ''))
            if i == 0 and ok:
                direction = -1      # already saturated: lower the load until it is not
                self.note(f'phase {p["name"]}: the first rate is already saturated: lowering it')
                continue
            if direction > 0 and ok:
                met_at = i
                break
            if direction < 0 and not ok:
                met_at = 0          # the first rate saturated; this one does not
                break
        its = rec['iterations']
        if direction < 0:
            good = [x for x in its if not x['met']]
            last_ok = good[-1] if good else None
            sat = its[-2] if good and len(its) >= 2 else its[0]
        else:
            last_ok = its[met_at - 1] if met_at else (its[-1] if met_at is None else None)
            sat = its[met_at] if met_at is not None else None
        res = {'metrics': (last_ok or its[0])['metrics'], 'flow': self.flow_of(None),
               'offered_pps': (last_ok or its[0])['rate_pps']}
        res['capacity_pps'] = round(last_ok['rate_pps'], 6) if last_ok else None
        f = last_ok['metrics'].get('flows', {}).get(self.flow_of(None), {}) if last_ok else {}
        rec['capacity'] = {'met_iteration': its.index(sat) if sat else None, 'reached': sat is not None,
                           'searched': 'down' if direction < 0 else 'up',
                           'capacity_pps': res['capacity_pps'], 'delivered_pps': f.get('delivered_pps'),
                           'saturated_rate_pps': sat['rate_pps'] if sat else None,
                           'detail': (sat['detail'] if sat else
                                      'condition not met within the iterations: the capacity is at least the last rate')
                           if last_ok else 'every rate tried was over the condition: the capacity is below the lowest'}
        rec['end'] = {'t_s': round(self.now, 6), 'met': met_at is not None,
                      'detail': f'condition met at iteration {met_at}' if met_at is not None else 'iterations done'}
        rec['metrics'] = res['metrics']
        rec['load'] = self.load and {k: v for k, v in self.load.items() if k not in ('spec', 'member_ids')}
        self.results[p['name']] = res

    def finish_disruption(self, rec):
        d = self.disruption
        par = {n: p for n, p in self.run.parents(self.now).items() if n not in self.killed}
        dep = runlog.depths(par)
        d['depths_after'] = {str(k): v for k, v in dep.items() if v is not None}
        nd = runlog.net_delta(self.run, d['t_s'], self.now)
        t_end = d['t_s'] + d['repair_t_s'] if d['repair_t_s'] is not None else self.now
        nd_rep = runlog.net_delta(self.run, d['t_s'], t_end)
        # Changes to a new parent seen in the NET1 samples of the polls (resolution: poll_s)
        seen = 0
        for n in self.run.routers():
            last = None
            for smp in self.run.nodes[n].net:
                if smp['t'] < d['t_s'] - 1e-9 or smp['t'] > t_end + 1e-9:
                    continue
                p = smp.get('rpl', {}).get('parent')
                if p is None:
                    continue        # a new parent counts once, not its loss
                if last is not None and p != last:
                    seen += 1
                last = p
        d['churn'] = {'parent_changes': seen,
                      'rpl_better_parent_counter': sum(x['parent_changes'] for x in nd_rep.values()),
                      'depth_changes': sum(1 for n in par if str(n) in d['parents_before'] and
                                           (d['depths_before'].get(str(n)) != d['depths_after'].get(str(n)))),
                      'control_messages': {k: sum(x.get('ctl_' + k, 0) for x in nd_rep.values())
                                           for k in ('dis', 'dio', 'dao')}}
        pm = runlog.packet_metrics(self.run, d['t_s'], t_end, self.now)
        d['packets_during_repair'] = {f: {'sent': v['sent'], 'lost': v['sent'] - (v['echoed'] if f != 'mcast' else v['delivered'])}
                                      for f, v in pm.items()}
        # ... and those of the cut-off routers that could be repaired (uplink)
        aff = set(d['affected'])
        sent = [(k, v) for k, v in self.run.tx.items() if k[1] in aff and d['t_s'] <= v[0] < t_end and v[4] >= 0]
        lost = sum(1 for k, v in sent if not (self.run.echo.get(k) and self.run.echo[k][0] <= self.now))
        d['packets_of_cut_off_routers'] = {'sent': len(sent), 'lost': lost}
        d['nodes_affected'] = len(d['affected'])
        orph = list(d['orphan_s'].values())
        d['orphan_time_s'] = runlog.stats(orph)
        d['repaired'] = d['repair_t_s'] is not None
        d.pop('orphan_since', None)
        self.disruption = None

    def drive(self):
        self.record['start_wall'] = time.time()
        r = self.rf.readline()     # the first pause: "at 0"
        if not r.startswith('at '):
            raise SystemExit('scenario: unexpected greeting ' + r)
        self.now = int(r.split()[1]) / 1e6
        self.run.update()
        status = 'completed'
        try:
            prev_end = self.now
            for p in self.phases:
                self.run_phase(p, prev_end)
                prev_end = self.now
            self.note('scenario done')
        except Abort as e:
            status = f'aborted: {e}'
            print(f'scenario: {status}', file=self.log)
        self.record['status'] = status
        self.record['end_t_s'] = self.now
        self.record['labels'] = self.labels
        self.record['summary'] = summarize(self.record)
        with open(os.path.join(self.dir, 'scenario_result.json'), 'w') as f:
            json.dump(self.record, f, indent=1)
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
        return 0 if status == 'completed' else 1


def _fmt(v, nd=1):
    return None if v is None else round(v, nd)


def summarize(rec):
    """The outcome of a scenario in a few figures (report.json "scenario.summary")."""
    out = {'status': rec.get('status'), 'phases': []}
    for ph in rec['phases']:
        row = {'name': ph['name'], 'start_s': ph.get('start', {}).get('t_s'), 'end_s': ph.get('end', {}).get('t_s'),
               'start_detail': ph.get('start', {}).get('detail'), 'start_timeout': ph.get('start', {}).get('timeout'),
               'end_detail': ph.get('end', {}).get('detail'), 'skipped': ph.get('skipped', False)}
        m = ph.get('metrics') or {}
        f = (m.get('flows') or {}).get('up') or next(iter((m.get('flows') or {}).values()), None)
        if f:
            row['pdr'] = _fmt(f.get('pdr'), 4)
            row['rtt_ms'] = {k: _fmt(f['rtt_ms'].get(k)) for k in ('n', 'mean', 'p95', 'max')} if f['rtt_ms']['n'] else None
            row['rtt_ms_by_hops'] = {h: {'n': s['n'], 'mean': _fmt(s.get('mean')), 'p95': _fmt(s.get('p95'))}
                                     for h, s in f['rtt_ms_by_hops'].items()}
            row['offered_pps'] = _fmt(f.get('offered_pps'), 3)
        if 'capacity' in ph:
            row['capacity'] = ph['capacity']
        if ph.get('disruptions'):
            row['disruptions'] = [{k: d.get(k) for k in ('do', 'failed', 'nodes_affected', 'isolated', 'detect_t_s', 'repair_t_s',
                                                         'repaired', 'orphan_time_s', 'churn', 'packets_during_repair',
                                                         'packets_of_cut_off_routers')}
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
                          for n, b in BLOCKS.items()}, indent=1))
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
