#!/usr/bin/env bash
# Calibration round: declare here as bin 0, then sort into all four bins in
# turn, timing each and checking the pole comes home to step 0 every time.
#
# Hand-align first -- this anchors bin 0 to wherever the pole is pointing NOW:
#     motor_cli.py release      # drop torque
#     ...turn the pole to the physical paper bin by hand...
#     ./deploy/calibrate_round.sh
#
# Expected shape: bin 0 fastest (servo only, no stepper travel), bins 1 and 3
# equal (400 pulses each, opposite directions), bin 2 slowest (800). Every row
# must end "step 0" -- anything else means the retrace lost steps.
set -euo pipefail

: "${MOTOR_SSH:=arduino@100.119.45.76}"
export MOTOR_SSH
CLI="python3 $(cd "$(dirname "$0")" && pwd)/motor_cli.py"

echo "== anchoring: this spot is now bin 0 =="
$CLI zero

for b in 0 1 2 3; do
  start=$(date +%s.%N)
  $CLI sort "$b" >/dev/null
  end=$(date +%s.%N)
  printf "bin %d  %5.2fs  %s\n" "$b" "$(echo "$end - $start" | bc)" "$($CLI pos)"
done

echo "== done: every line above should read 'step 0' =="
