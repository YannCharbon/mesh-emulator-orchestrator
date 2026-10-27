#!/usr/bin/env python3
"""Compare the logs of two runs byte for byte.

usage: compare_logs.py <run A> <run B> [--quiet]

Compares every node<i>.log, br.log, log_wsbr and exit_codes.txt that is in
either run, and reports, per file, "same", "missing in A/B", or the first line
that differs (line number and both lines). simserver.log is compared too but
only for information: wssimserver prints wall-clock figures (§3 of
docs/VIRTUAL_TIME.md), so it is expected to differ.

Exit status: 0 if all the compared files (simserver.log aside) are identical,
1 otherwise.
"""
import os, re, sys

CHECKED = re.compile(r'(node\d+|br)\.log|log_wsbr|exit_codes\.txt')
INFO_ONLY = {'simserver.log'}


def sort_key(name):
    m = re.fullmatch(r'node(\d+)\.log', name)
    return (1, int(m.group(1)), '') if m else (0, 0, name)


def first_diff(pa, pb):
    """None if the files are identical, else (line number, line A, line B)."""
    with open(pa, 'rb') as fa, open(pb, 'rb') as fb:
        n = 0
        while True:
            la, lb = fa.readline(), fb.readline()
            n += 1
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
    args = [x for x in sys.argv[1:] if x != '--quiet']
    if len(args) != 2:
        sys.exit(__doc__)
    sys.exit(0 if compare(args[0], args[1], '--quiet' in sys.argv) else 1)
