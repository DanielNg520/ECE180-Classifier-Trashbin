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

import requests

MOTOR_ENABLED = os.environ.get("MOTOR_ENABLED", "0") == "1"
MOTOR_URL = os.environ.get("MOTOR_URL", "http://127.0.0.1:8071").rstrip("/")
MOTOR_ACK_TIMEOUT = float(os.environ.get("MOTOR_ACK_TIMEOUT", "20"))


def send_sort(bin_index, wait_ack=True):
    """Tell the motor app to sort the current item into `bin_index` (0-3).

    Returns True if the app acknowledged the move. No-op returning False when
    MOTOR_ENABLED is off or the motor app is unreachable/errored.

    `wait_ack` is accepted for compatibility with the old serial bridge; the
    HTTP request always blocks until the move completes, so it has no effect
    beyond the (shorter) timeout used when a caller doesn't care about the ack.
    """
    if not MOTOR_ENABLED:
        return False
    timeout = MOTOR_ACK_TIMEOUT if wait_ack else 3
    try:
        resp = requests.post(
            f"{MOTOR_URL}/sort",
            json={"bin": int(bin_index)},
            timeout=timeout,
        )
        data = resp.json()
        if resp.ok and data.get("ok") and data.get("landed") != -1:
            return True
        print(f"[motor] app rejected sort {bin_index}: {data}")
        return False
    except requests.RequestException as e:
        print(f"[motor] cannot reach motor app at {MOTOR_URL}: {e}")
        return False
    except ValueError as e:  # non-JSON response
        print(f"[motor] bad response from motor app: {e}")
        return False
