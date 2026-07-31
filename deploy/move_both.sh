#!/usr/bin/env bash
# Bench check: move both motors and put everything back where it started.
#
#   ./deploy/move_both.sh              # stepper a quarter turn, arm one sweep
#   BIN=3 ./deploy/move_both.sh        # aim at a different bin
#
# Positions are relative, so this is safe to run without hand-zeroing first.
set -euo pipefail

: "${MOTOR_SSH:=arduino@100.119.45.76}"   # tailnet; uno-q.local often won't resolve
export MOTOR_SSH
CLI="python3 $(dirname "$0")/motor_cli.py"
BIN="${BIN:-2}"

echo "== before =="
$CLI pos

echo "== stepper: out ${BIN} bins (${BIN}00 pulses) =="
$CLI nudge $((BIN * 400))
sleep 1

echo "== servo: sweep ${ARM_SWEEP:-0} and back =="
$CLI servo "${ARM_SWEEP:-0}"
sleep 3
$CLI servo "${ARM_REST:-160}"

echo "== stepper: back =="
$CLI nudge -$((BIN * 400))

echo "== after (should match 'before') =="
$CLI pos
