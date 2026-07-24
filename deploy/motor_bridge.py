"""Linux -> MCU bridge: send a target bin to the motor-control app.

On the UNO Q the STM32 microcontroller isn't reachable as a plain serial tty —
it talks to the Linux side over the Arduino RouterBridge (RPC over an internal
link). That Bridge is only reachable from inside an Arduino App Lab app, so the
motor control lives in the "Trashbin Motor" app (~/ArduinoApps/nema17):

    sketch.ino    exposes  Bridge.provide("sort", ...)     (drives the stepper)
    python/main.py         Bridge.call("sort", bin)  behind an HTTP endpoint

That app publishes an HTTP port to the host, so from here (the classifier loop,
which runs on the host under cron) we just POST the target bin:

    POST http://127.0.0.1:8071/sort   {"bin": <int>}   ->   {"ok":..., "landed":...}

The app rotates the pole to the bin by the shorter direction (clockwise on a
tie) and the request blocks until the move finishes — so waiting on the HTTP
response is our "ack".

Config (env):
    MOTOR_ENABLED     "1" to actually drive motors (default off = camera-only)
    MOTOR_URL         base URL of the motor app (default http://127.0.0.1:8071)
    MOTOR_ACK_TIMEOUT seconds to wait for the move to finish (default 20)

Everything here is best-effort: an unreachable motor app logs a warning and
never crashes the classifier loop.
"""
import os
import time

import requests

MOTOR_ENABLED = os.environ.get("MOTOR_ENABLED", "0") == "1"
MOTOR_URL = os.environ.get("MOTOR_URL", "http://127.0.0.1:8071").rstrip("/")
MOTOR_ACK_TIMEOUT = float(os.environ.get("MOTOR_ACK_TIMEOUT", "20"))

# Dedicated debug log for the motor path. The UNO Q has a single USB port, so
# you can't be on adb AND have the camera plugged in at once — persist every
# motor decision to a file, then reconnect adb later and read it. Set
# MOTOR_LOG="" to disable, or point it elsewhere. Wipe with:  : > motor.log
MOTOR_LOG = os.environ.get(
    "MOTOR_LOG", os.path.expanduser("~/trashbin/motor.log")
)


def _log(msg):
    """Timestamped line to stdout (captured in trashbin.log) and MOTOR_LOG."""
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} [motor] {msg}"
    print(line, flush=True)
    if not MOTOR_LOG:
        return
    try:
        with open(MOTOR_LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass  # logging must never break the sort path


def send_sort(bin_index, wait_ack=True):
    """Tell the motor app to sort the current item into `bin_index` (0-3).

    Returns True if the app acknowledged the move. No-op returning False when
    MOTOR_ENABLED is off or the motor app is unreachable/errored.

    `wait_ack` is accepted for compatibility with the old serial bridge; the
    HTTP request always blocks until the move completes, so it has no effect
    beyond the (shorter) timeout used when a caller doesn't care about the ack.
    """
    if not MOTOR_ENABLED:
        _log(f"SKIP bin={bin_index}: MOTOR_ENABLED is off (set MOTOR_ENABLED=1)")
        return False
    timeout = MOTOR_ACK_TIMEOUT if wait_ack else 3
    _log(f"POST {MOTOR_URL}/sort bin={bin_index} (timeout={timeout}s)")
    t0 = time.perf_counter()
    try:
        resp = requests.post(
            f"{MOTOR_URL}/sort",
            json={"bin": int(bin_index)},
            timeout=timeout,
        )
        dt = (time.perf_counter() - t0) * 1000
        data = resp.json()
        if resp.ok and data.get("ok") and data.get("landed") != -1:
            # Motor app acked. If the pole still doesn't physically turn after
            # this line prints, the fault is downstream: driver ENA, wiring, or
            # the 12V supply — not the software path.
            _log(f"ACK bin={bin_index} landed={data.get('landed')} in {dt:.0f}ms")
            return True
        _log(f"REJECTED bin={bin_index} http={resp.status_code} body={data}")
        return False
    except requests.RequestException as e:
        _log(f"UNREACHABLE {MOTOR_URL}: {e} — is the 'Trashbin Motor' app running?")
        return False
    except ValueError as e:  # non-JSON response
        _log(f"BAD RESPONSE from motor app: {e}")
        return False


# One-time config banner at import, so every log has a header showing the
# settings the classifier actually booted with.
_log(f"config: MOTOR_ENABLED={MOTOR_ENABLED} MOTOR_URL={MOTOR_URL} log={MOTOR_LOG}")
