#!/bin/bash
# Plug-and-play boot starter for the "Trashbin Motor" Arduino App.
# Launched at boot by the arduino user's @reboot crontab (no root needed).
#
# The App Lab daemon does not re-launch previously-running apps after a reboot
# and the app container's restart policy is "no", so we start it explicitly.
# We first wait for docker and the app daemon to be ready, then start the app
# (idempotent: a no-op if it is somehow already running). Logs to motor-app.log.
set -u

APP_DIR="$HOME/ArduinoApps/nema17"     # the "Trashbin Motor" app
LOG="$HOME/trashbin/motor-app.log"
mkdir -p "$HOME/trashbin"

log() { echo "$(date -u +%FT%TZ) $*" >>"$LOG"; }

log "=== boot: waiting for docker + app daemon ==="

# Wait for the docker socket (up to ~2 min).
for i in $(seq 1 60); do
  if docker info >/dev/null 2>&1; then break; fi
  sleep 2
done

# Wait for the arduino-app-cli daemon HTTP port (up to ~1 min).
for i in $(seq 1 30); do
  if curl -s --max-time 2 http://127.0.0.1:8800/ >/dev/null 2>&1; then break; fi
  sleep 2
done

# Give the RouterBridge / router socket a moment to settle after the daemon.
sleep 3

log "starting app at $APP_DIR"
arduino-app-cli app start "$APP_DIR" >>"$LOG" 2>&1
log "app start exit=$?"

# Confirm the command port is up.
for i in $(seq 1 15); do
  if curl -s --max-time 2 http://127.0.0.1:8071/health >/dev/null 2>&1; then
    log "motor command port :8071 is up"
    exit 0
  fi
  sleep 2
done
log "WARNING: motor command port :8071 not responding after start"
