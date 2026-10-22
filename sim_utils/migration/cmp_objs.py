#!/usr/bin/env python3
"""Compare every object compiled by the reference build with the object
compiled from the same source by the extracted build.

Compares, per object: disassembly of all code sections with symbolic
relocations, plus raw contents of every data section except debug info.
Usage: cmp_objs.py <ref_build_dir> <orch_build_dir> <stack_src_dir>
"""
import os, re, subprocess, sys, glob, hashlib

ref, orch, stack = sys.argv[1:4]
stack = os.path.abspath(stack)

def norm_dis(path):
    out = subprocess.run(['objdump', '-d', '-r', '-w', '--no-show-raw-insn', path],
                         capture_output=True, text=True, check=True).stdout
    lines = out.splitlines()[2:]  # drop "file format" header
    return '\n'.join(lines)

def sections(path):
    out = subprocess.run(['readelf', '-S', '-W', path], capture_output=True, text=True).stdout
    names = re.findall(r'\]\s+(\S+)\s+(PROGBITS|NOBITS)', out)
    return [n for n, t in names if not n.startswith('.debug') and t == 'PROGBITS'
            and n not in ('.comment', '.note.GNU-stack') and not n.startswith('.text')]

def data(path):
    h = {}
    for s in sections(path):
        out = subprocess.run(['objcopy', '-O', 'binary', '-j', s, path, '/dev/stdout'],
                             capture_output=True).stdout
        h[s] = out
    return h

def new_obj(target, rel):
    """Where the extracted build compiled <rel> (path relative to old root)."""
    m = re.match(r'(nanostack/source/.*)', rel)
    if m:
        lib = {'wsbrd': 'nanostack_ws_border_router', 'wsnode': 'nanostack_ws_router'}[target]
        return f'{orch}/CMakeFiles/{lib}.dir{stack}/{rel}.o'
    if rel.startswith('nanostack-event-loop/'):
        return f'{orch}/nanostack-build/CMakeFiles/nanostack_eventloop.dir/{rel}.o'
    if rel.startswith('mbed-client-libservice/'):
        return f'{orch}/nanostack-build/CMakeFiles/nanostack_libservice.dir/{rel}.o'
    m = re.match(r'app_wshwsim/(mac|fhss|ccm)/(.*)', rel)
    if m:
        return f'{orch}/CMakeFiles/nanostack_rcp.dir{stack}/rcp/{m.group(1)}/{m.group(2)}.o'
    m = re.match(r'common/(hal_interrupt|os_timer|os_scheduler|log|rand|slist|backtrace_show)\.c', rel)
    if m:
        return (f'{orch}/CMakeFiles/wssimserver.dir{stack}/port/linux/{rel}.o' if target == 'wssimserver' else f'{orch}/nanostack-build/CMakeFiles/nanostack_port_linux.dir/port/linux/{rel}.o')
    return f'{orch}/CMakeFiles/{target}.dir/{rel}.o'

same = diff = missing = 0
report = []
for o in sorted(glob.glob(f'{ref}/CMakeFiles/*.dir/**/*.o', recursive=True)):
    target = o.split('/CMakeFiles/')[1].split('.dir/')[0]
    rel = o.split('.dir/')[1][:-2]
    n = new_obj(target, rel)
    if not os.path.exists(n):
        missing += 1; report.append(f'MISSING {target} {rel} -> {n}'); continue
    d1, d2 = norm_dis(o), norm_dis(n)
    x1, x2 = data(o), data(n)
    if d1 == d2 and x1 == x2:
        same += 1
    else:
        diff += 1
        what = []
        if d1 != d2: what.append('code')
        for s in sorted(set(x1) | set(x2)):
            if x1.get(s) != x2.get(s): what.append(s)
        report.append(f'DIFF {target} {rel}: {",".join(what)}')
print('\n'.join(report))
print(f'identical={same} different={diff} missing={missing}')
