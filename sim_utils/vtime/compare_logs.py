#!/usr/bin/env python3
"""Compare the logs of two runs byte for byte.

usage: compare_logs.py <run A> <run B> [--quiet] [--mask-pointers] [--mask-source-lines]

Compares every node<i>.log, br.log, log_wsbr and exit_codes.txt that is in
either run, and reports, per file, "same", "missing in A/B", or the first line
that differs (line number and both lines). simserver.log is compared too but
only for information: wssimserver prints wall-clock figures (§3 of
docs/VIRTUAL_TIME.md), so it is expected to differ.

--mask-source-lines replaces the source line numbers that the programs' own
traces print ("function():123:") with "N", so that a change elsewhere in a
program's source does not count as a difference.

--mask-pointers replaces printed addresses (0x and 8 or more hex digits) with
"PTR" before comparing. They depend on the memory layout, which changes with
the environment (for example the TUN device's name), not on what the run did.

Exit status: 0 if all the compared files (simserver.log aside) are identical,
1 if not, 2 on an error (a run directory that cannot be read, for example).
"""
import os, re, sys

CHECKED = re.compile(r'(node\d+|br)\.log|log_wsbr|exit_codes\.txt')
INFO_ONLY = {'simserver.log'}


def sort_key(name):
    m = re.fullmatch(r'node(\d+)\.log', name)
    return (1, int(m.group(1)), '') if m else (0, 0, name)


POINTER = re.compile(rb'0x[0-9a-fA-F]{8,}')
mask_lines = False
SOURCE_LINE = re.compile(rb'(\w+\(\)):\d+:')
mask = False


def first_diff(pa, pb):
    """None if the files are identical, else (line number, line A, line B)."""
    with open(pa, 'rb') as fa, open(pb, 'rb') as fb:
        n = 0
        while True:
            la, lb = fa.readline(), fb.readline()
            n += 1
            if mask:
                la, lb = POINTER.sub(b'PTR', la), POINTER.sub(b'PTR', lb)
            if mask_lines:
                la, lb = SOURCE_LINE.sub(rb'\1:N:', la), SOURCE_LINE.sub(rb'\1:N:', lb)
            if la != lb:
                return n, la, lb
            if not la:
                return None


def show(line):
    if not line:
        return '<end of file>'
    s = line.decode('utf-8', 'replace').rstrip('\n')
    return s if len(s) <= 200 else s[:200] + '...'


def compare(a, b, quiet=False):
    names = {f for d in (a, b) for f in os.listdir(d) if CHECKED.fullmatch(f) or f in INFO_ONLY}
    same = differ = 0
    for f in sorted(names, key=sort_key):
        pa, pb = os.path.join(a, f), os.path.join(b, f)
        info = ' (information only)' if f in INFO_ONLY else ''
        if not os.path.exists(pa) or not os.path.exists(pb):
            print(f'{f}: missing in {"A" if not os.path.exists(pa) else "B"}{info}')
            differ += not info
            continue
        d = first_diff(pa, pb)
        if d is None:
            same += not info
            if not quiet:
                print(f'{f}: same{info}')
            continue
        differ += not info
        n, la, lb = d
        print(f'{f}: first difference at line {n}{info}')
        print(f'   A: {show(la)}')
        print(f'   B: {show(lb)}')
    print(f'{same} identical, {differ} different or missing')
    return differ == 0


if __name__ == '__main__':
    args = [x for x in sys.argv[1:] if x not in ('--quiet', '--mask-pointers', '--mask-source-lines')]
    mask = '--mask-pointers' in sys.argv
    mask_lines = '--mask-source-lines' in sys.argv
    if len(args) != 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    try:
        same = compare(args[0], args[1], '--quiet' in sys.argv)
    except Exception as e:
        print(f'compare_logs.py: {e}', file=sys.stderr)
        sys.exit(2)
    sys.exit(0 if same else 1)
