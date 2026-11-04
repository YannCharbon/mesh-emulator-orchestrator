#!/bin/bash
# Runs behind the check of the LFN power formula (root repo
# documents/pre-study/investigation/LFN_Capabilities_and_Power.md §6.3):
# test_lfn_profiles.sh with the LFN power profile, the LFNs at +10 dBm, two
# seeds per case, then lfn_power_breakdown.py and lfn_power_formula.py.
# About 20 min of wall time with the cases run in parallel.
# usage: lfn_power_campaign.sh <bindir> <workdir(holds examples/)> <outdir>
set -u
BIN=$(realpath "$1"); WORK=$(realpath "$2"); OUT=$3; HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$OUT" && OUT=$(realpath "$OUT")
LP=$WORK/examples/power/efr32fg25-lfn.ini
run() { # name prof seed dur steady extra [env...]
  local n=$1 p=$2 s=$3 d=$4 st=$5 x=$6; shift 6
  env WSSIM_SEED=$s DUR=$d STEADY_FROM=$st LFN_POWER=$LP LFN_EXTRA="-o tx_power=10 $x" "$@" \
      bash "$HERE/test_lfn_profiles.sh" "$BIN" "$WORK" "$OUT/${n}_s$s" "$p" > "$OUT/${n}_s$s.out" 2>&1
  python3 "$HERE/lfn_power_breakdown.py" "$OUT/${n}_s$s" "$st" "$d" > "$OUT/${n}_s$s.txt"
  tail -n 1 "$OUT/${n}_s$s.txt" > "$OUT/${n}_s$s.json"
}
for s in 1 2; do
  run T5  test $s 14400 1800 "" &
  run T2  test $s 14400 1800 "-o lfn_listen_interval_ms=2000" &
  run T30 test $s 14400 1800 "-o lfn_listen_interval_ms=30000" &
  run TS  test $s 14400 1800 "-o lfn_bc_sync_only=true" &
  run TW  test $s 14400 1800 "-o lfn_drift_floor_ppb=0" &
  run TU  test $s 14400 1800 "" LFN_PERIOD=60 &
  run TD  test $s 14400 1800 "" BR_PERIOD=60 &
  run BAL balanced $s 86400 10800 "" &
  run ECO eco $s 172800 86400 "" &
  run EX2 eco $s 86400 10800 "" PARENT_EXTRA="-o lfn_bc_interval_ms=60000 -o lfn_bc_sync_period=15" &
done
run ECO4H eco 1 259200 86400 "-o lfn_listen_interval_ms=14400000 -o lfn_listen_interval_max_ms=16777215" BR_PERIOD=18500 LFN_PERIOD=27000 &
wait
grep -h "test_lfn_profiles:" "$OUT"/*.out | sort | uniq -c
python3 "$HERE/lfn_power_formula.py" "$OUT"
