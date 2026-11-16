#!/usr/bin/env python3
"""Scenario files: vocabulary, JSON Schema, building blocks, validation and
dry check (docs/SCENARIOS.md). Used by sim_utils/scenario.py; standard library
only.

The JSON Schema carries every rule a form generator needs: titles,
descriptions, units ("x-unit"), defaults, enumerations, mutually exclusive
fields (oneOf / not), fields required per kind (if / then), and a plain-words
message per composite rule ("x-message"). check() validates a document in one
round: the schema with a small validator of the keywords used here, then the
meaning (references to earlier phases, labels, disruptions, interferer
specifications), with every error as {"path", "message"} against what the
user wrote (a block's errors point at the block's own parameters).
"""
import copy, math, re

FORMAT = 'wssim-scenario 1'

# --------------------------------------------------------------- vocabulary

# name: (unit, description, measurement set, worse: "high" or "low")
METRICS = {
    'pdr': ('ratio', 'Delivery ratio of the flow: echoes received / requests sent; for multicast, receptions / '
            '(sends x members expected to receive)', 'traffic', 'low'),
    'rtt.mean_ms': ('ms', 'Mean round-trip time (request and echo)', 'traffic', 'high'),
    'rtt.p50_ms': ('ms', 'Median round-trip time', 'traffic', 'high'),
    'rtt.p95_ms': ('ms', '95th percentile of the round-trip time', 'traffic', 'high'),
    'rtt.max_ms': ('ms', 'Largest round-trip time', 'traffic', 'high'),
    'rtt.min_ms': ('ms', 'Smallest round-trip time', 'traffic', 'high'),
    'latency.mean_ms': ('ms', 'Mean one-way delay (send to reception at the target)', 'traffic', 'high'),
    'latency.p95_ms': ('ms', '95th percentile of the one-way delay', 'traffic', 'high'),
    'latency.max_ms': ('ms', 'Largest one-way delay', 'traffic', 'high'),
    'offered_pps': ('packets/s', 'Packets sent per second by the flow (measured), whole network', 'traffic', 'high'),
    'delivered_pps': ('packets/s', 'Packets delivered (echoed) per second, whole network', 'traffic', 'low'),
    'queue.mean_max': ('frames', 'Highest mean queue depth of a node (MAC and adaptation layer) over the window',
                       'queues', 'high'),
    'queue.now_max': ('frames', 'Highest queue depth of a node at the end of the window', 'queues', 'high'),
    'queue.drops': ('packets', 'Packets dropped by full adaptation-layer queues (all nodes) in the window', 'queues',
                    'high'),
    'mac.retries_per_frame': ('retries', 'MAC retries per unicast frame delivered, whole network', 'queues', 'high'),
    'mac.give_up': ('frames', 'Unicast frames the MAC gave up (no ACK, channel access, other), all nodes, in the '
                    'window', 'queues', 'high'),
    'mac.cca_fail_rate': ('ratio', 'Channel-access failures / CCA attempts, whole network', 'queues', 'high'),
    'mesh.joined_fraction': ('ratio', 'Routers with a route to the border router (parent chain)', 'mesh', 'low'),
    'mesh.orphans': ('nodes', 'Running routers without a route to the border router', 'mesh', 'high'),
    'mesh.parent_changes': ('changes', 'Changes to a new parent seen in the window, all routers', 'mesh', 'high'),
    'mesh.max_hops': ('hops', 'Depth of the deepest router', 'mesh', 'high'),
    'power.avg_ma': ('mA', 'Mean over the routers of their average current in the window (needs POWER)', 'power',
                     'high'),
    'medium.frames_per_s': ('frames/s', 'Frames sent per second, whole network', 'medium', 'high'),
    'medium.airtime_s_per_s': ('s/s', 'Airtime of every frame sent per second of the window (sum over all nodes)',
                               'medium', 'high'),
    'medium.loss_share': ('ratio', 'Frames lost at receivers that were listening, over those heard (central medium)',
                          'medium', 'high'),
}
SAMPLED = ('pdr', 'rtt.', 'latency.')     # metrics that need min_samples packets
MEASURE_SETS = {
    'traffic': 'Delivery, RTT and one-way delay overall and per hop count, offered and delivered throughput',
    'queues': 'Per node: queue depths, drops, MAC retries and give-ups, channel-access failures',
    'mesh': 'Topology: parents, depths, orphans, parent changes, RPL control messages',
    'power': 'Per node: charge and average current in the phase, by state (needs POWER)',
    'medium': 'Frames, airtime and reception outcomes of the radio medium',
}
FLOWS = ['up', 'down', 'mcast', 'p2p']
CONDITION_KINDS = ['immediately', 'time', 'elapsed', 'formed', 'node_event', 'repaired', 'metric', 'all', 'any']
NODE_EVENTS = ['joined', 'parent_changed', 'parent_lost', 'restarted']
DISRUPTIONS = ['kill', 'leave', 'cut_links', 'partition', 'degrade_links']
ACTIONS = DISRUPTIONS + ['restart', 'interferer_add', 'interferer_remove', 'mark']
SELECTS = ['id', 'ids', 'all', 'hop', 'br_children', 'ancestor', 'storey', 'area', 'label']
PICKS = ['all', 'largest_subtree', 'most_children', 'random', 'fraction', 'first']
OPS = ['>=', '>', '<=', '<', '==']
INTERFERER_KEYS = ['chan', 'level', 'at', 'pos', 'eirp', 'pattern', 'hop', 'kind', 'rate', 'pre', 'adj', 'name',
                   'from', 'until']


def default_op(metric):
    """The comparison when a condition gives none: "worse than" the value."""
    return '<' if METRICS.get(metric, ('', '', '', 'high'))[3] == 'low' else '>='

# ------------------------------------------------------------- JSON Schema


def S(t, title, desc, unit=None, **kw):
    d = {'type': t, 'title': title, 'description': desc}
    if unit:
        d['x-unit'] = unit
    d.update(kw)
    return d


def obj(title, desc, props, required=(), **kw):
    d = {'type': 'object', 'title': title, 'description': desc, 'properties': props, 'additionalProperties': False}
    if required:
        d['required'] = list(required)
    d.update(kw)
    return d


def when(field, value, then, message):
    """if field == value then ... (with a message for the user)"""
    return {'if': {'properties': {field: {'const': value}}, 'required': [field]}, 'then': then, 'x-message': message}


def exclusive(fields, message):
    """At most one of these fields."""
    return {'not': {'anyOf': [{'required': [a, b]} for i, a in enumerate(fields) for b in fields[i + 1:]]},
            'x-message': message}


SELECTOR = obj('Nodes', 'Which nodes an action acts on, resolved from the topology when the action runs', {
    'select': S('string', 'Selection', 'id: one node; ids: a list; all: every router; hop: the routers at a hop '
                'count; br_children: the border router\'s children (hop 1); ancestor: the node at a hop count on the '
                'path of a given node; storey or area: by position (needs a positions file); label: the nodes an '
                'earlier action picked', enum=SELECTS),
    'id': S('integer', 'Node ID', 'For select id (0: the border router)', minimum=0, maximum=4095),
    'ids': S('array', 'Node IDs', 'For select ids', items={'type': 'integer', 'minimum': 0, 'maximum': 4095},
             minItems=1),
    'hop': S('integer', 'Hop count', 'For select hop and ancestor: hops to the border router', 'hops', minimum=1,
             maximum=64),
    'of': S('integer', 'Of node', 'For select ancestor: the node whose path is taken', minimum=1, maximum=4095),
    'storey': S('integer', 'Storey', 'For select storey: the FLOOR of the positions file (else Z / 3.5 m)'),
    'box': S('array', 'Area', 'For select area: [x0, y0, x1, y1]', 'm', items={'type': 'number'},
             minItems=4, maxItems=4),
    'label': S('string', 'Label', 'For select label: the label of an earlier action'),
    'pick': S('string', 'Pick', 'Among the candidates: all; largest_subtree or most_children (the worst case); '
              'random (drawn from the seed); fraction; first (lowest ID)', enum=PICKS, default='all'),
    'count': S('integer', 'Count', 'How many to pick (largest_subtree, most_children, random, first)', 'nodes',
               minimum=1, default=1),
    'fraction': S('number', 'Fraction', 'For pick fraction: the share of the candidates, drawn from the seed',
                  'ratio', minimum=0, maximum=1),
}, ['select'], allOf=[
    when('select', 'id', {'required': ['id']}, 'select id needs id'),
    when('select', 'ids', {'required': ['ids']}, 'select ids needs ids'),
    when('select', 'hop', {'required': ['hop']}, 'select hop needs hop'),
    when('select', 'ancestor', {'required': ['of', 'hop']}, 'select ancestor needs of and hop'),
    when('select', 'storey', {'required': ['storey']}, 'select storey needs storey'),
    when('select', 'area', {'required': ['box']}, 'select area needs box'),
    when('select', 'label', {'required': ['label']}, 'select label needs label'),
    when('pick', 'fraction', {'required': ['fraction']}, 'pick fraction needs fraction'),
])

REL = obj('Relative to', 'Factor x the same metric (or another one) measured in an earlier phase', {
    'phase': S('string', 'Phase', 'Name of an earlier phase'),
    'metric': S('string', 'Metric', 'The earlier phase\'s metric (default: the condition\'s metric); a capacity '
                'search also has capacity_pps', enum=sorted(METRICS) + ['capacity_pps']),
    'factor': S('number', 'Factor', 'Multiplier of the earlier value', 'ratio', exclusiveMinimum=0, default=1),
}, ['phase'])

COND_PROPS = {
    'is': S('string', 'Kind', 'immediately: true at once; time: virtual time reached; elapsed: time since the '
            'reference (the previous phase\'s end for a start, the phase\'s start for an end); formed: the network '
            'has joined; node_event: a node joined, changed or lost its parent, restarted; repaired: every router '
            'cut off by the last disruption that can be repaired has a route again; metric: a measured value '
            'compared with a threshold; all / any: a combination', enum=CONDITION_KINDS),
    't_s': S('number', 'Time', 'For time: virtual time since the run\'s start', 's', minimum=0),
    's': S('number', 'Seconds', 'For elapsed', 's', minimum=0),
    'fraction': S('number', 'Share of routers', 'For formed: the share of the routers (or of nodes) that must have '
                  'joined', 'ratio', minimum=0, maximum=1, default=1),
    'milestone': S('string', 'Milestone', 'For formed (default ready) and node_event joined (default operational): '
                   'operational (Wi-SUN join done) or ready (RPL routing ready; not repeated when an interface comes '
                   'back up)', enum=['operational', 'ready']),
    'stable_s': S('number', 'Stable for', 'For formed: and no parent change anywhere for this long', 's', minimum=0,
                  default=0),
    'nodes': SELECTOR,
    'event': S('string', 'Event', 'For node_event', enum=NODE_EVENTS),
    'e2e': S('boolean', 'End to end', 'For repaired: also every cut-off router is in the border router\'s routing '
             'table and got an echo for a packet sent after the disruption (when it takes part in an echoed flow)',
             default=False),
    'metric': S('string', 'Metric', 'For metric', enum=sorted(METRICS)),
    'op': S('string', 'Comparison', 'For metric. Default: "worse than" the value: >= for delays, queues, '
            'retries...; < for pdr, delivered_pps and joined_fraction', enum=OPS),
    'value': S('number', 'Value', 'For metric: the threshold, in the metric\'s unit (or relative_to instead)'),
    'relative_to': REL,
    'per_hop': S('boolean', 'Per hop count', 'For metric (rtt, latency): true if the comparison holds for any hop '
                 'count, each against the same hop count of relative_to', default=False),
    'flow': S('string', 'Flow', 'For traffic metrics: the flow (default: up, else the first one running)',
              enum=FLOWS),
    'window_s': S('number', 'Window', 'For metric: the trailing window of packets sent', 's', minimum=1, default=60),
    'min_samples': S('integer', 'Samples', 'For pdr, rtt and latency metrics: at least this many packets in the '
                     'window, else the condition is not decided yet (default: settings.min_samples)', 'packets',
                     minimum=1),
    'of': S('array', 'Conditions', 'For all / any', items={'$ref': '#/$defs/condition'}, minItems=1),
}
COND_RULES = [
    when('is', 'time', {'required': ['t_s']}, 'a time condition needs t_s'),
    when('is', 'elapsed', {'required': ['s']}, 'an elapsed condition needs s'),
    when('is', 'node_event', {'required': ['event', 'nodes']}, 'a node_event condition needs event and nodes'),
    when('is', 'metric', {'required': ['metric'], 'oneOf': [
        {'required': ['value'], 'not': {'required': ['relative_to']}},
        {'required': ['relative_to'], 'not': {'required': ['value']}}]},
        'a metric condition needs a metric and either value or relative_to (not both)'),
    when('is', 'all', {'required': ['of']}, 'all needs of'),
    when('is', 'any', {'required': ['of']}, 'any needs of'),
]
CONDITION = obj('Condition', 'A condition on the run, evaluated live at each poll', COND_PROPS, ['is'],
                allOf=COND_RULES)

START = obj('Start', 'When the phase starts (default: right after the previous phase): a condition, with a timeout '
            'and what to do when it expires', dict(COND_PROPS, **{
                'timeout_s': S('number', 'Timeout', 'Give up waiting after this long', 's', minimum=0, default=3600),
                'on_timeout': S('string', 'On timeout', 'start: start anyway (flagged in the result); skip: skip this '
                                'phase; abort: end the scenario', enum=['start', 'skip', 'abort'], default='start'),
            }), ['is'], allOf=COND_RULES)

END = obj('End', 'When the phase ends: after a fixed duration (after_s), or when a condition holds, with a cap '
          '(max_s). Default: after 60 s', dict(COND_PROPS, **{
              'after_s': S('number', 'Duration', 'A fixed duration (no condition)', 's', minimum=0),
              'max_s': S('number', 'Cap', 'With a condition: end after this long anyway', 's', minimum=0,
                         default=600),
          }), [], allOf=COND_RULES + [
              {'oneOf': [{'required': ['after_s'], 'not': {'anyOf': [{'required': ['is']}, {'required': ['max_s']}]}},
                         {'required': ['is'], 'not': {'required': ['after_s']}}],
               'x-message': 'end is either after_s alone (a fixed duration) or a condition ("is") with max_s'}])

LOAD = obj('Load', 'The application load from the start of the phase', {
    'flows': S('array', 'Flows', 'up: routers to border router; down: border router to each router; mcast: border '
               'router to a group; p2p: router to router. Up, down and p2p are echoed',
               items={'type': 'string', 'enum': FLOWS}, minItems=1, default=['up']),
    'rate_pps': S('number', 'Rate', 'Offered packets per second of each flow, whole network', 'packets/s',
                  exclusiveMinimum=0),
    'interval_s': S('number', 'Interval', 'Instead of rate_pps: mean interval of each router (default 30 s)', 's',
                    minimum=0.001),
    'rate_of': REL,
    'max_rate_pps': S('number', 'Rate cap', 'With interval_s: at most this rate, whole network (a light probe '
                      'whatever the network size)', 'packets/s', exclusiveMinimum=0),
    'payload_b': S('integer', 'Payload', 'UDP payload', 'bytes', minimum=32, maximum=1232, default=64),
    'dist': S('string', 'Intervals', 'fixed or exp (Poisson)', enum=['fixed', 'exp'], default='exp'),
    'nodes': SELECTOR,
    'echo': S('boolean', 'Echo', 'Targets answer, so that originators measure the RTT', default=True),
}, [], allOf=[exclusive(['rate_pps', 'interval_s', 'rate_of'], 'give one of rate_pps, interval_s and rate_of'),
              exclusive(['rate_pps', 'max_rate_pps'], 'max_rate_pps goes with interval_s, not rate_pps'),
              exclusive(['rate_of', 'max_rate_pps'], 'max_rate_pps goes with interval_s, not rate_of')])

ACTION = obj('Action', 'Something done at the start of the phase, after the load is set', {
    'do': S('string', 'Action', 'kill: power off; restart: power off and a fresh start; leave: the interface goes '
            'down (a graceful leave); cut_links: every link of the nodes (or to peers) cut; partition: links '
            'between groups a and b cut; degrade_links: every link of the nodes at rssi_dbm; interferer_add, '
            'interferer_remove: external interference (central medium); mark: a note in the logs', enum=ACTIONS),
    'nodes': SELECTOR,
    'peers': SELECTOR,
    'a': SELECTOR,
    'b': SELECTOR,
    'for_s': S('number', 'For', 'Undo after this long: restart the killed nodes, interface up, links back, '
               'interferer removed. Omitted: permanent', 's', exclusiveMinimum=0),
    'rssi_dbm': S('number', 'RSSI', 'For degrade_links', 'dBm', maximum=0, minimum=-200),
    'spec': S('string', 'Interferer', 'For interferer_add: as wssimserver --interferer (docs/MEDIUM.md §13.1), '
              'e.g. "chan=0-9,level=-75,kind=fsk,pattern=ge:20ms:80ms"'),
    'name': S('string', 'Name', 'For interferer_add and interferer_remove ("all": every one)'),
    'label': S('string', 'Label', 'Name of the nodes this action picked, for later selections (select label)'),
    'text': S('string', 'Text', 'For mark'),
}, ['do'], allOf=[
    when('do', 'kill', {'required': ['nodes']}, 'kill needs nodes'),
    when('do', 'restart', {'required': ['nodes']}, 'restart needs nodes'),
    when('do', 'leave', {'required': ['nodes']}, 'leave needs nodes'),
    when('do', 'cut_links', {'required': ['nodes']}, 'cut_links needs nodes'),
    when('do', 'degrade_links', {'required': ['nodes', 'rssi_dbm']}, 'degrade_links needs nodes and rssi_dbm'),
    when('do', 'partition', {'required': ['a', 'b']}, 'partition needs a and b'),
    when('do', 'interferer_add', {'required': ['spec']}, 'interferer_add needs spec'),
    when('do', 'interferer_remove', {'required': ['name']}, 'interferer_remove needs name'),
])

MEASURE = obj('Measure', 'What the phase measures (default: traffic)', {
    'sets': S('array', 'Measurements', 'Any of: ' + '; '.join(f'{k}: {v}' for k, v in MEASURE_SETS.items()),
              items={'type': 'string', 'enum': list(MEASURE_SETS)}, default=['traffic']),
    'settle_s': S('number', 'Settle', 'Time after the start not measured', 's', minimum=0, default=0),
}, [])

REPEAT = obj('Repeat', 'Iterations: the load changes at each, until a condition holds (a capacity search)', {
    'max': S('integer', 'Iterations', 'At most this many (confirmations included)', minimum=1, maximum=100, default=12),
    'mul': S('number', 'Multiply', 'The rate is multiplied by this at each iteration', 'ratio', exclusiveMinimum=1,
             default=1.5),
    'add_pps': S('number', 'Add', 'Instead of mul: the rate grows by this at each iteration', 'packets/s',
                 exclusiveMinimum=0),
    'window_s': S('number', 'Window', 'Measured time of an iteration (made longer when the rate gives fewer than '
                  'min_samples packets)', 's', minimum=1, default=60),
    'settle_s': S('number', 'Settle', 'Time at the new load before the window', 's', minimum=0, default=15),
    'confirm': S('integer', 'Confirm', 'The condition must hold on this many windows in a row at the same load',
                 'windows', minimum=1, maximum=5, default=2),
    'until': {'$ref': '#/$defs/condition', 'description': 'Stop when this holds on an iteration\'s measurements'},
}, ['until'], allOf=[exclusive(['mul', 'add_pps'], 'give mul or add_pps, not both')])

PHASE = obj('Phase', 'Start when [start]; set [load]; do [actions]; measure [measure]; end when [end] (or repeat)', {
    'name': S('string', 'Name', 'Unique name, used by later phases'),
    'start': START,
    'actions': S('array', 'Actions', 'Done in order at the start, after the load', items=ACTION),
    'load': {'oneOf': [LOAD, {'type': 'string', 'enum': ['off', 'keep']}], 'title': 'Load',
             'description': 'The load from this phase on: settings, "off", or "keep" (the default)',
             'x-message': 'load is an object, "off" or "keep"'},
    'measure': MEASURE,
    'end': END,
    'repeat': REPEAT,
}, ['name'], allOf=[exclusive(['end', 'repeat'], 'a phase has end or repeat, not both')])

SETTINGS = obj('Settings', 'Defaults of the whole scenario', {
    'poll_s': S('number', 'Poll', 'How often conditions and disruptions are evaluated while waiting', 's',
                minimum=0.5, default=5),
    'drain_s': S('number', 'Drain', 'Packets sent in the last drain_s before an evaluation are not counted yet', 's',
                 minimum=0, default=10),
    'min_samples': S('integer', 'Samples', 'Packets a window needs before a pdr, rtt or latency condition is '
                     'decided', 'packets', minimum=1, default=100),
    'parent_min_dbm': S('number', 'Parent threshold', 'Links at or below this level cannot carry a new parent: '
                        'routers that would need them are not counted as repairable (the stack\'s DEVICE_MIN_SENS '
                        '-93 dBm + CAND_PARENT_THRESHOLD 10 dB + hysteresis 3 dB)', 'dBm', default=-80),
    'load': LOAD,
}, [])

SEL_HOP1_WORST = {'select': 'hop', 'hop': 1, 'pick': 'largest_subtree'}
ALL_SETS = ['traffic', 'queues', 'mesh', 'power', 'medium']


def _probe_phase(name, p, rate_key='rate_pps'):
    load = {'flows': p.get('flows', ['up']), 'payload_b': p.get('payload_b', 64), 'dist': 'exp'}
    if rate_key in p:
        load['rate_pps'] = p[rate_key]
    else:
        load.update(interval_s=30, max_rate_pps=0.5)
    return {'name': name, 'load': load, 'measure': {'sets': ALL_SETS, 'settle_s': p.get('settle_s', 30)},
            'end': {'after_s': p.get('duration_s', 300) + p.get('settle_s', 30)}}


def _expand_capacity(p, name, base, start_load):
    metric = p.get('rtt_metric', 'rtt.p95_ms')
    w = p.get('window_s', 60)
    until = [{'is': 'metric', 'metric': metric, 'op': '>=',
              'relative_to': {'phase': base, 'factor': p.get('rtt_factor', 2)}, 'window_s': w},
             {'is': 'metric', 'metric': 'pdr', 'op': '<', 'value': p.get('min_pdr', 0.95), 'window_s': w}]
    if 'max_rtt_ms' in p:
        until.append({'is': 'metric', 'metric': 'rtt.max_ms', 'op': '>=', 'value': p['max_rtt_ms'], 'window_s': w})
    load = dict({'flows': p.get('flows', ['up']), 'payload_b': p.get('payload_b', 64), 'dist': 'exp'}, **start_load)
    return {'name': name, 'load': load, 'measure': {'sets': ALL_SETS},
            'repeat': {'max': p.get('max_steps', 12), 'mul': p.get('mul', 1.5), 'window_s': w,
                       'settle_s': p.get('settle_s', 15), 'confirm': p.get('confirm', 2),
                       'until': {'is': 'any', 'of': until}}}


def _capacity(p):
    base = p.get('baseline', 'probe')
    if 'start_rate_pps' in p:
        start = {'rate_pps': p['start_rate_pps']}
    else:
        start = {'rate_of': {'phase': base, 'metric': 'offered_pps', 'factor': 2}}
    return [_expand_capacity(p, p.get('name', 'capacity'), base, start)]


def _interference(p):
    name = p.get('name', 'interference')
    load = {'rate_of': {'phase': p.get('capacity_phase', 'capacity'), 'metric': 'capacity_pps',
                        'factor': p.get('load_fraction', 0.8)}}
    settle = p.get('settle_s', 30)
    out = []
    if p.get('reference_s', 0) > 0:    # the same load without the interferer: a recovery's baseline
        out.append({'name': name + '_reference', 'load': load, 'measure': {'sets': ALL_SETS, 'settle_s': settle},
                    'end': {'after_s': p['reference_s'] + settle}})
    out.append({'name': name,
                'actions': [{'do': 'interferer_add', 'spec': p['spec'], 'name': p.get('interferer_name', 'jam')}],
                'load': load, 'measure': {'sets': ALL_SETS, 'settle_s': settle},
                'end': {'after_s': p.get('duration_s', 300)}})
    return out


def _capacity_check(p):
    name = p.get('name', 'capacity_check')
    out = []
    base = p.get('baseline', 'self')
    if base == 'self':          # its own light-load probe: a baseline of the network as it is now
        base = name + '_probe'
        out.append(_probe_phase(base, {'duration_s': p.get('probe_s', 300), 'settle_s': 30,
                                       'flows': p.get('flows', ['up']), 'payload_b': p.get('payload_b', 64)}))
    start = {'rate_of': {'phase': p.get('capacity_phase', 'capacity'), 'metric': 'capacity_pps',
                         'factor': p.get('start_fraction', 0.5)}}
    out.append(_expand_capacity(dict(p, max_steps=p.get('max_steps', 8)), name, base, start))
    return out


P = {   # parameters shared by the blocks
    'flows': LOAD['properties']['flows'],
    'payload_b': LOAD['properties']['payload_b'],
    'mul': S('number', 'Step factor', 'Rate multiplied by this at each step', 'ratio', exclusiveMinimum=1, default=1.5),
    'window_s': S('number', 'Window', 'Measured time of a step (longer when the rate gives too few packets)', 's',
                  minimum=1, default=60),
    'settle_s_cap': S('number', 'Settle', 'Time at the new load before measuring', 's', minimum=0, default=15),
    'rtt_factor': S('number', 'RTT factor', 'Saturated when the RTT reaches this x the baseline\'s', 'ratio',
                    exclusiveMinimum=1, default=2),
    'min_pdr': S('number', 'Delivery floor', 'Saturated when the delivery ratio falls below this', 'ratio', minimum=0,
                 maximum=1, default=0.95),
    'confirm': REPEAT['properties']['confirm'],
}

# Each block: its parameters, its phases, and where its parameters land in
# the expanded phases (to report errors against what the user wrote)
BLOCKS = {
    'formed': {
        'title': 'Wait for the network',
        'doc': 'Wait until the routers have joined (ready and in the border router\'s table) and no parent changed '
               'for stable_s',
        'params': {
            'fraction': S('number', 'Share of routers', 'Share that must have joined', 'ratio', minimum=0, maximum=1,
                          default=1),
            'stable_s': S('number', 'Stable for', 'No parent change for this long', 's', minimum=0, default=60),
            'timeout_s': S('number', 'Timeout', 'Start anyway after this long, flagged', 's', minimum=0, default=3600),
        },
        'expand': lambda p: [{
            'name': p.get('name', 'formed'),
            'start': {'is': 'formed', 'fraction': p.get('fraction', 1), 'milestone': 'ready',
                      'stable_s': p.get('stable_s', 60), 'timeout_s': p.get('timeout_s', 3600)},
            'measure': {'sets': ['mesh']}, 'end': {'after_s': 0}}],
        'map': {},
    },
    'probe': {
        'title': 'Per-hop RTT probe',
        'doc': 'A light load, its RTT, delay and delivery per hop count: the baseline of later phases',
        'params': {
            'rate_pps': S('number', 'Rate', 'Offered packets/s, whole network (default: one packet per router every '
                          '30 s, at most 0.5 packets/s)', 'packets/s', exclusiveMinimum=0),
            'flows': P['flows'],
            'duration_s': S('number', 'Duration', 'Measured time', 's', minimum=1, default=300),
            'settle_s': S('number', 'Settle', 'Not measured at the start', 's', minimum=0, default=30),
            'payload_b': P['payload_b'],
        },
        'expand': lambda p: [_probe_phase(p.get('name', 'probe'), p)],
        'map': {},
    },
    'capacity': {
        'title': 'Capacity search',
        'doc': 'Raise the load step by step until the RTT reaches rtt_factor x the baseline\'s, the largest RTT '
               'max_rtt_ms, or the delivery falls below min_pdr (on confirm windows in a row); the capacity is the '
               'measured offered rate of the last step before',
        'params': {
            'baseline': S('string', 'Baseline phase', 'The phase whose RTT is the reference', default='probe'),
            'start_rate_pps': S('number', 'First rate', 'Packets/s of the first step (default: 2 x the baseline\'s)',
                                'packets/s', exclusiveMinimum=0),
            'mul': P['mul'],
            'max_steps': S('integer', 'Steps', 'At most this many windows', minimum=1, maximum=100, default=12),
            'window_s': P['window_s'],
            'settle_s': P['settle_s_cap'],
            'confirm': P['confirm'],
            'rtt_metric': S('string', 'RTT metric', 'The RTT compared with the baseline',
                            enum=['rtt.mean_ms', 'rtt.p50_ms', 'rtt.p95_ms'], default='rtt.p95_ms'),
            'rtt_factor': P['rtt_factor'],
            'max_rtt_ms': S('number', 'RTT ceiling', 'Saturated when the largest RTT reaches this', 'ms', minimum=1),
            'min_pdr': P['min_pdr'],
            'flows': P['flows'],
            'payload_b': P['payload_b'],
        },
        'expand': _capacity,
        'map': {'0/repeat/until/of/0/relative_to': 'baseline', '0/load/rate_of': 'baseline'},
    },
    'interference': {
        'title': 'Interference',
        'doc': 'An external interferer from the start of the phase, under a share of the capacity found; optionally '
               'first a reference at the same load without it (<name>_reference), the baseline of a recovery',
        'required': ['spec'],
        'params': {
            'spec': ACTION['properties']['spec'],
            'interferer_name': S('string', 'Interferer name', 'Its name, to remove it later', default='jam'),
            'capacity_phase': S('string', 'Capacity phase', 'Where the capacity comes from', default='capacity'),
            'load_fraction': S('number', 'Load', 'Share of the capacity offered', 'ratio', exclusiveMinimum=0,
                               maximum=2, default=0.8),
            'reference_s': S('number', 'Reference', 'Before the interferer: this long measured at the same load '
                             'without it, as the phase <name>_reference (0: none)', 's', minimum=0, default=0),
            'duration_s': S('number', 'Duration', 'Length of the phase', 's', minimum=1, default=300),
            'settle_s': S('number', 'Settle', 'Not measured at the start (of the reference too)', 's', minimum=0,
                          default=30),
        },
        'expand': _interference,
        'map': {'-1/actions/0/spec': 'spec', '-1/load/rate_of': 'capacity_phase', '0/load/rate_of': 'capacity_phase'},
    },
    'recovery': {
        'title': 'Recovery',
        'doc': 'Remove the interferer (if any) at the same load and measure until the RTT is back near the baseline',
        'params': {
            'interferer_name': S('string', 'Interferer name', 'The interferer removed ("all": every one; "": none)',
                                 default='all'),
            'baseline': S('string', 'Baseline phase', 'The reference RTT', default='probe'),
            'within': S('number', 'Back within', 'Ends when the RTT p95 is at most this x the baseline\'s', 'ratio',
                        minimum=1, default=1.2),
            'max_s': S('number', 'Cap', 'At most this long', 's', minimum=1, default=600),
        },
        'expand': lambda p: [{
            'name': p.get('name', 'recovery'),
            'actions': ([{'do': 'interferer_remove', 'name': p.get('interferer_name', 'all')}]
                        if p.get('interferer_name', 'all') else []),
            'measure': {'sets': ALL_SETS},
            'end': {'is': 'metric', 'metric': 'rtt.p95_ms', 'op': '<=', 'window_s': 60,
                    'relative_to': {'phase': p.get('baseline', 'probe'), 'factor': p.get('within', 1.2)},
                    'max_s': p.get('max_s', 600)}}],
        'map': {'0/end/relative_to': 'baseline'},
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
            'load_fraction': S('number', 'Load', 'Share of the capacity offered', 'ratio', exclusiveMinimum=0,
                               maximum=2, default=0.5),
            'max_s': S('number', 'Cap', 'At most this long', 's', minimum=1, default=600),
            'e2e': COND_PROPS['e2e'],
        },
        'expand': lambda p: [dict({
            'name': p.get('name', 'node_failure'),
            'actions': [{'do': 'kill', 'nodes': p.get('nodes', SEL_HOP1_WORST), 'label': p.get('label', 'failed')}],
            'measure': {'sets': ALL_SETS},
            'end': {'is': 'repaired', 'e2e': p.get('e2e', True), 'max_s': p.get('max_s', 600)}},
            **({'load': {'rate_of': {'phase': p.get('capacity_phase', 'capacity'), 'metric': 'capacity_pps',
                                     'factor': p.get('load_fraction', 0.5)}}}
               if p.get('capacity_phase', 'capacity') else {}))],
        'map': {'0/actions/0/nodes': 'nodes', '0/load/rate_of': 'capacity_phase'},
    },
    'node_restart': {
        'title': 'Node restart',
        'doc': 'Start the failed nodes again (fresh instances) under a share of the capacity, and measure until they '
               'are ready again and in the border router\'s routing table',
        'params': {
            'label': S('string', 'Label', 'The nodes of an earlier action', default='failed'),
            'capacity_phase': S('string', 'Capacity phase', 'Where the capacity comes from ("": keep the load)',
                                default='capacity'),
            'load_fraction': S('number', 'Load', 'Share of the capacity offered', 'ratio', exclusiveMinimum=0,
                               maximum=2, default=0.5),
            'max_s': S('number', 'Cap', 'At most this long', 's', minimum=1, default=600),
        },
        'expand': lambda p: [dict({
            'name': p.get('name', 'node_restart'),
            'actions': [{'do': 'restart', 'nodes': {'select': 'label', 'label': p.get('label', 'failed')}}],
            'measure': {'sets': ALL_SETS},
            'end': {'is': 'formed', 'milestone': 'ready', 'fraction': 1,
                    'nodes': {'select': 'label', 'label': p.get('label', 'failed')}, 'max_s': p.get('max_s', 600)}},
            **({'load': {'rate_of': {'phase': p.get('capacity_phase', 'capacity'), 'metric': 'capacity_pps',
                                     'factor': p.get('load_fraction', 0.5)}}}
               if p.get('capacity_phase', 'capacity') else {}))],
        'map': {'0/actions/0/nodes': 'label', '0/end/nodes': 'label', '0/load/rate_of': 'capacity_phase'},
    },
    'capacity_check': {
        'title': 'Capacity again',
        'doc': 'A capacity search from a share of an earlier capacity (e.g. after a repair), against a fresh '
               'baseline: by default a probe of the network as it is now, then the search',
        'params': {
            'capacity_phase': S('string', 'Earlier capacity', 'The capacity it starts from', default='capacity'),
            'start_fraction': S('number', 'First rate', 'Share of the earlier capacity of the first step', 'ratio',
                                exclusiveMinimum=0, default=0.5),
            'baseline': S('string', 'Baseline', '"self": a probe of its own first (<name>_probe); else an earlier '
                          'phase', default='self'),
            'probe_s': S('number', 'Probe duration', 'With baseline self: the probe\'s measured time', 's', minimum=1,
                         default=300),
            'mul': P['mul'],
            'max_steps': S('integer', 'Steps', 'At most this many windows', minimum=1, maximum=100, default=8),
            'window_s': P['window_s'],
            'settle_s': P['settle_s_cap'],
            'confirm': P['confirm'],
            'rtt_factor': P['rtt_factor'],
            'min_pdr': P['min_pdr'],
            'flows': P['flows'],
            'payload_b': P['payload_b'],
        },
        'expand': _capacity_check,
        'map': {'-1/load/rate_of': 'capacity_phase', '-1/repeat/until/of/0/relative_to': 'baseline'},
    },
}


def block_schema():
    alts = []
    for name, b in BLOCKS.items():
        props = {'use': S('string', 'Block', b['doc'], enum=[name]),
                 'name': S('string', 'Name', 'Phase name (default: the block\'s)')}
        props.update(b['params'])
        alts.append(obj(b['title'], b['doc'], props, ['use'] + b.get('required', [])))
    return alts


def _refs(x, top=True):
    """The schema with the selector, the relative reference and the condition as $refs."""
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
    sch = {
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
                        items={'oneOf': [{'$ref': '#/$defs/phase'}] + [{'$ref': f'#/$defs/block_{n}'} for n in BLOCKS],
                               'x-discriminator': 'use'}),
        },
        '$defs': dict({'condition': CONDITION, 'phase': PHASE},
                      **{f'block_{n}': a for n, a in zip(BLOCKS, block_schema())}),
        'x-metrics': {k: {'unit': v[0], 'description': v[1], 'set': v[2], 'worse': v[3]} for k, v in METRICS.items()},
    }
    sch['$defs'] = {k: _refs(v, False) for k, v in sch['$defs'].items()}
    sch['properties'] = _refs(sch['properties'], False)
    sch['$defs']['selector'] = _refs(SELECTOR)
    sch['$defs']['relative'] = _refs(REL)
    return sch

# --------------------------------------------------------------- validation


class Validator:
    """The keywords this schema uses: type, enum, const, minimum, maximum,
    exclusiveMinimum, minItems, maxItems, items, properties, required,
    additionalProperties, $ref, allOf, anyOf, oneOf, not, if/then; a
    composite rule that fails gives its "x-message"."""

    def __init__(self, root):
        self.root = root

    def resolve(self, s):
        while '$ref' in s:
            node = self.root
            for p in s['$ref'].lstrip('#/').split('/'):
                node = node[p]
            s = dict(node, **{k: v for k, v in s.items() if k != '$ref'})
        return s

    def ok(self, v, s):
        e = []
        self.run(v, s, '', e)
        return not e

    def run(self, v, s, path, errs):
        s = self.resolve(s)
        if 'oneOf' in s and 'x-discriminator' in s:
            # a phase, or a block named by "use"
            if not isinstance(v, dict):
                return errs.append({'path': path, 'message': 'must be an object (a phase or a block)'})
            for a in s['oneOf']:
                a = self.resolve(a)
                use = a.get('properties', {}).get('use', {}).get('enum')
                if ('use' in v and use == [v['use']]) or ('use' not in v and use is None):
                    return self.run(v, a, path, errs)
            errs.append({'path': path + '/use', 'message': f'unknown block "{v["use"]}" (known: {", ".join(BLOCKS)})'})
            return
        t = s.get('type')
        types = {'object': dict, 'array': list, 'string': str, 'boolean': bool}
        if t == 'integer' and (not isinstance(v, int) or isinstance(v, bool)):
            return errs.append({'path': path, 'message': 'must be a whole number'})
        if t == 'number' and (not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v)):
            return errs.append({'path': path, 'message': 'must be a number'})
        if t in types and not isinstance(v, types[t]):
            return errs.append({'path': path, 'message': f'must be {"an" if t[0] in "aeio" else "a"} {t}'})
        if 'const' in s and v != s['const']:
            return errs.append({'path': path, 'message': f'must be "{s["const"]}"'})
        if 'enum' in s and v not in s['enum']:
            return errs.append({'path': path, 'message': f'must be one of: {", ".join(map(str, s["enum"]))}'})
        if t in ('integer', 'number'):
            unit = f' {s["x-unit"]}' if s.get('x-unit') and s['x-unit'] not in ('ratio',) else ''
            if 'minimum' in s and v < s['minimum']:
                errs.append({'path': path, 'message': f'must be at least {s["minimum"]}{unit}'})
            if 'exclusiveMinimum' in s and v <= s['exclusiveMinimum']:
                errs.append({'path': path, 'message': f'must be more than {s["exclusiveMinimum"]}{unit}'})
            if 'maximum' in s and v > s['maximum']:
                errs.append({'path': path, 'message': f'must be at most {s["maximum"]}{unit}'})
        if t == 'array':
            if 'minItems' in s and len(v) < s['minItems']:
                errs.append({'path': path, 'message': f'needs at least {s["minItems"]} entries'})
            if 'maxItems' in s and len(v) > s['maxItems']:
                errs.append({'path': path, 'message': f'has at most {s["maxItems"]} entries'})
            if 'items' in s:
                for i, x in enumerate(v):
                    self.run(x, s['items'], f'{path}/{i}', errs)
        if isinstance(v, dict) and (t == 'object' or 'required' in s or 'properties' in s):
            props = s.get('properties', {})
            for k in s.get('required', []):
                if k not in v:
                    errs.append({'path': f'{path}/{k}', 'message': 'is required'})
            for k, x in v.items():
                if k in props:
                    self.run(x, props[k], f'{path}/{k}', errs)
                elif s.get('additionalProperties') is False and not k.startswith('x_'):
                    errs.append({'path': f'{path}/{k}', 'message': f'unknown field (known: {", ".join(props)})'})
        for sub in s.get('allOf', []):
            self.run(v, sub, path, errs)
        if 'if' in s:
            if self.ok(v, s['if']) and not self.ok(v, s['then']):
                if s.get('x-message'):
                    errs.append({'path': path, 'message': s['x-message']})
                else:
                    self.run(v, s['then'], path, errs)
        if 'oneOf' in s and 'x-discriminator' not in s:
            n = sum(1 for a in s['oneOf'] if self.ok(v, a))
            if n != 1:
                # one alternative of the value's type: its own, precise errors
                alts = [self.resolve(a) for a in s['oneOf']]
                same = [a for a in alts if 'type' in a and (a['type'] == 'object') == isinstance(v, dict)]
                if len(same) == 1 and n == 0:
                    self.run(v, same[0], path, errs)
                else:
                    errs.append({'path': path, 'message': s.get('x-message', 'not a valid value here')})
        if 'anyOf' in s and not any(self.ok(v, a) for a in s['anyOf']):
            errs.append({'path': path, 'message': s.get('x-message', 'not a valid value here')})
        if 'not' in s and self.ok(v, s['not']):
            errs.append({'path': path, 'message': s.get('x-message', 'not a valid combination of fields')})


def expand(doc):
    """The phases, blocks expanded. Each phase gets x_index (its entry in the
    file) and, from a block, x_block and x_part (its rank in the block)."""
    out = []
    for i, p in enumerate(doc.get('phases', []) if isinstance(doc, dict) else []):
        if isinstance(p, dict) and p.get('use') in BLOCKS:
            try:
                qs = BLOCKS[p['use']]['expand'](p)
            except (KeyError, TypeError, ValueError):
                continue
            for k, q in enumerate(qs):
                q['x_block'] = p['use']
                q['x_index'] = i
                q['x_part'] = k
                q['x_parts'] = len(qs)
                out.append(q)
        elif isinstance(p, dict):
            q = copy.deepcopy(p)
            q['x_index'] = i
            out.append(q)
    return out


_NUM = r'-?\d+(\.\d+)?'
_TIME = r'\d+(\.\d+)?(us|ms|s)?'


def check_interferer(spec):
    """The errors of an interferer SPEC (the server's rules: docs/MEDIUM.md
    §13.1); channels beyond the PHY's plan are accepted, as by the server."""
    errs = []
    seen = set()
    kind = None
    has_level = False
    for kv in spec.split(','):
        if '=' not in kv:
            errs.append(f'"{kv}": expected KEY=VALUE')
            continue
        k, v = kv.split('=', 1)
        if k not in INTERFERER_KEYS:
            errs.append(f'"{k}": unknown key (known: {", ".join(INTERFERER_KEYS)})')
            continue
        seen.add(k)
        def num(x, lo, hi):
            try:
                f = float(x)
                return lo <= f <= hi
            except ValueError:
                return False
        if k == 'chan':
            chans = []
            for part in v.split('/'):
                m = re.fullmatch(r'(\d+)(?:-(\d+))?', part)
                if not m or int(m.group(1)) > 65535 or (m.group(2) and not int(m.group(1)) <= int(m.group(2)) <= 65535):
                    errs.append(f'chan: "{part}" is not a channel or an ascending range (0-65535)')
                    continue
                chans += list(range(int(m.group(1)), int(m.group(2) or m.group(1)) + 1))
            if len(chans) != len(set(chans)):
                errs.append('chan: a channel given twice')
        elif k == 'level':
            has_level = v != 'off'
            if v != 'off' and not num(v, -200, 50):
                errs.append('level: dBm from -200 to 50, or off')
        elif k == 'at':
            m = re.fullmatch(r'(\d+)(?:-(\d+))?:(.+)', v)
            if not m or (m.group(3) != 'off' and not num(m.group(3), -200, 50)):
                errs.append('at: IDS:DBM (e.g. 3:-70, 10-20:-80, 5:off)')
            has_level = True
        elif k == 'pos':
            if not re.fullmatch(rf'{_NUM}/{_NUM}(/{_NUM})?', v):
                errs.append('pos: X/Y[/Z] in metres')
            has_level = True
        elif k == 'eirp' and not num(v, -100, 60):
            errs.append('eirp: dBm from -100 to 60')
        elif k == 'pattern':
            if not (v == 'const' or re.fullmatch(rf'periodic:{_TIME}:{_TIME}(:{_TIME})?', v)
                    or re.fullmatch(rf'(ge|burst):{_TIME}:{_TIME}', v)):
                errs.append('pattern: const, periodic:PERIOD:ON[:PHASE], ge:MEAN_ON:MEAN_OFF or burst:ON:MEAN_OFF '
                            '(times in us, or with us, ms, s)')
        elif k == 'hop' and v not in ('on', 'off'):
            errs.append('hop: on or off')
        elif k == 'kind':
            kind = v
            if v not in ('noise', 'fsk'):
                errs.append('kind: noise or fsk')
        elif k == 'rate' and not num(v, 1000, 10000000):
            errs.append('rate: bit/s from 1000 to 10000000')
        elif k in ('pre', 'from', 'until') and not re.fullmatch(_TIME, v):
            errs.append(f'{k}: a time (us, or with us, ms, s)')
        elif k == 'adj' and not all(x == 'off' or num(x, -100, 200) for x in v.split('/')):
            errs.append('adj: DB/DB/... (or off)')
        elif k == 'name' and (not v or ' ' in v or len(v) > 31):
            errs.append('name: 1 to 31 characters, no space')
    if 'chan' not in seen:
        errs.append('needs chan=')
    if not has_level:
        errs.append('needs level=, at= or pos=')
    if 'pre' in seen and kind != 'fsk':
        errs.append('pre= needs kind=fsk')
    return errs


def _cond_kinds(c):
    if not isinstance(c, dict):
        return set()
    out = {c.get('is')}
    for x in c.get('of') or []:
        out |= _cond_kinds(x)
    return out


def _semantic(doc, errs):
    earlier, labels = {}, set()
    names = set()
    disrupted = False

    def cond(c, path, here=None):
        if not isinstance(c, dict):
            return
        for i, x in enumerate(c.get('of') or []):
            cond(x, f'{path}/of/{i}', here)
        rel = c.get('relative_to')
        if isinstance(rel, dict):
            ok = rel.get('phase') in earlier or (here is not None and rel.get('phase') == here)
            if not ok:
                errs.append({'path': path + '/relative_to/phase', 'message': f'"{rel.get("phase")}" is not an earlier phase'})
            elif rel.get('metric') == 'capacity_pps' and 'repeat' not in earlier.get(rel['phase'], {}):
                errs.append({'path': path + '/relative_to/metric', 'message': f'"{rel["phase"]}" is not a capacity search'})
        if c.get('per_hop') and not str(c.get('metric', '')).startswith(('rtt.', 'latency.')):
            errs.append({'path': path + '/per_hop', 'message': 'only for rtt and latency metrics'})
        if c.get('is') == 'repaired' and not disrupted:
            errs.append({'path': path, 'message': 'repaired needs a disruption before it (kill, leave, cut_links, '
                                                  'partition or degrade_links in this phase or an earlier one)'})
        if 'nodes' in c:
            sel(c['nodes'], path + '/nodes')

    def sel(s, path):
        if isinstance(s, dict) and s.get('select') == 'label' and s.get('label') not in labels:
            errs.append({'path': path + '/label', 'message': f'no earlier action has the label "{s.get("label")}"'})

    for p in expand(doc):
        path = f'/phases/{p["x_index"]}' + (f'#{p["x_part"]}' if p.get('x_block') else '')
        name = p.get('name')
        if name in names:
            errs.append({'path': path + '/name', 'message': f'the name "{name}" is used twice'})
        names.add(name)
        if isinstance(p.get('start'), dict):
            cond(p['start'], path + '/start')
        load = p.get('load')
        if isinstance(load, dict):
            ro = load.get('rate_of')
            if isinstance(ro, dict):
                if ro.get('phase') not in earlier:
                    errs.append({'path': path + '/load/rate_of', 'message': f'"{ro.get("phase")}" is not an earlier phase'})
                elif ro.get('metric') == 'capacity_pps' and 'repeat' not in earlier[ro['phase']]:
                    errs.append({'path': path + '/load/rate_of', 'message': f'"{ro["phase"]}" is not a capacity search'})
            if 'nodes' in load:
                sel(load['nodes'], path + '/load/nodes')
        for j, a in enumerate(p.get('actions') or []):
            if not isinstance(a, dict):
                continue
            ap = f'{path}/actions/{j}'
            if a.get('do') == 'interferer_add' and isinstance(a.get('spec'), str):
                for m in check_interferer(a['spec']):
                    errs.append({'path': ap + '/spec', 'message': m})
            for f in ('nodes', 'peers', 'a', 'b'):
                if f in a:
                    sel(a[f], f'{ap}/{f}')
            if a.get('label'):
                labels.add(a['label'])
            if a.get('do') in DISRUPTIONS:
                disrupted = True
        if isinstance(p.get('end'), dict):
            cond(p['end'], path + '/end')
        if isinstance(p.get('repeat'), dict):
            cond(p['repeat'].get('until'), path + '/repeat/until', here=None)
        earlier[name] = p


def _map_block_errors(doc, errs):
    """Errors inside an expanded block, against the block's own parameters."""
    out = []
    for e in errs:
        m = re.match(r'^/phases/(\d+)#(-?\d+)(/.*)?$', e['path'])
        if not m:
            out.append(e)
            continue
        i, part, rest = int(m.group(1)), int(m.group(2)), (m.group(3) or '').lstrip('/')
        use = doc['phases'][i].get('use')
        parts = len(BLOCKS[use]['expand'](doc['phases'][i]))
        param = None
        for k, v in BLOCKS[use]['map'].items():
            kp, _, kr = k.partition('/')
            if int(kp) % parts == part and (rest == kr or rest.startswith(kr + '/')):
                param = v
        if param:
            out.append({'path': f'/phases/{i}/{param}', 'message': e['message']})
        else:
            out.append({'path': f'/phases/{i}', 'message': f'block {use}: {rest or "phase"}: {e["message"]}'})
    return out


def check(doc):
    """Every error of a scenario document (schema and meaning), as {path, message}."""
    if not isinstance(doc, dict):
        return [{'path': '', 'message': 'a scenario is a JSON object'}]
    sch = schema()
    errs = []
    Validator(sch).run(doc, sch, '', errs)
    sem = []
    try:
        _semantic(doc, sem)
    except (TypeError, AttributeError, KeyError, ValueError) as e:
        # a part the schema refused (its error is above): the meaning of the
        # rest is checked as far as it goes
        if not errs:
            sem.append({'path': '', 'message': f'could not be checked further ({type(e).__name__}: {e})'})
    errs += _map_block_errors(doc, sem)
    seen, out = set(), []
    for e in errs:
        key = (e['path'], e['message'])
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


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
            # a window can grow to 4 x window_s for its samples
            body = r.get('max', 12) * (r.get('settle_s', 15) + 4 * r.get('window_s', 60) + st.get('drain_s', 10))
        else:
            e = p.get('end', {'after_s': 60})
            body = e['after_s'] if 'after_s' in e else e.get('max_s', 600)
        total += wait + body
        rows.append({'name': p['name'], 'block': p.get('x_block'), 'entry': p['x_index'], 'wait_max_s': wait,
                     'run_max_s': body, 'phase': {k: v for k, v in p.items() if not k.startswith('x_')}})
    return {'ok': True, 'phases': rows, 'virtual_s_max': total}
