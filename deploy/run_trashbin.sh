#!/bin/bash
# Plug-and-play launcher for the ECE180 trashbin classifier on the UNO Q.
# Started at boot by the arduino user's @reboot crontab (no root needed).
# Keeps the camera loop alive: if it exits/crashes, waits and relaunches.
set -u

cd "$HOME/trashbin" || exit 1

# User-site pip packages (ai-edge-litert, opencv, numpy, ...) live in ~/.local
export PYTHONPATH="$HOME/.local/lib/python3.13/site-packages:${PYTHONPATH:-}"

export WEBAPP_URL="https://ece180.duythe.dev"
export DEVICE_ID="trashbin-1"
export CONFIDENCE_THRESHOLD="0.8"
export TEMPERATURE="0.55"
# Motors are driven through the "Trashbin Motor" Arduino App (App Lab), which
# owns the RouterBridge link to the STM32 and publishes an HTTP port. The
# classifier just POSTs the target bin to it (see motor_bridge.py).
export MOTOR_ENABLED="1"
export MOTOR_URL="http://127.0.0.1:8071"

MODEL="mobilenet_v3_large_waste30_static_int8.tflite"
LABELS="labels.txt"
LOG="$HOME/trashbin/trashbin.log"

while true; do
  echo "=== $(date -u +%FT%TZ) starting camera_loop ===" >>"$LOG"
  /usr/bin/python3 camera_loop.py "$MODEL" "$LABELS" >>"$LOG" 2>&1
  echo "=== $(date -u +%FT%TZ) camera_loop exited ($?), restarting in 10s ===" >>"$LOG"
  sleep 10
done
