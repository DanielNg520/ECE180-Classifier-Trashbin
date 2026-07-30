# Trashbin motor App — Linux/Python side.
#
# Bridges the host-side classifier to the MCU motor sketch. The classifier
# (deploy/camera_loop.py, running under cron on the host) POSTs a target bin
# here; we forward it to the microcontroller over the RouterBridge RPC:
#
#     POST /sort   body: {"bin": <int>}   ->   Bridge.call("sort", bin)
#
# The call blocks until the pole finishes moving, then we return the bin the
# MCU reports landing on. GET /health is a liveness probe.
#
# For manual/bench control (deploy/motor_cli.py, driven over ssh) we also expose
# the direct motor endpoints:
#
#     POST /step   {"step": 0..1600}   absolute stepper position, in pulses
#     POST /nudge  {"delta": <int>}    relative stepper move, signed pulses
#     POST /servo  {"angle": 0..180}   absolute servo-arm angle
#     POST /home   {}                  re-home the stepper to step 0
#     GET  /pos                        current stepper position (no movement)
#
# We listen on 0.0.0.0:8071; app.yaml publishes that port to the host, so the
# classifier reaches us at http://127.0.0.1:8071.

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from arduino.app_utils import App, Bridge

PORT = 8071

# Bridge.call is not guaranteed thread-safe across concurrent RPCs, and the
# motor can only do one move at a time anyway — serialize every command.
_lock = threading.Lock()


# Geometry mirrored from the sketch (1/8 microstep => 1600 pulses/rev), used
# only to reject out-of-range requests before they reach the MCU. Keep in sync
# with STEPS_PER_REV in sketch/sketch.ino.
STEPS_PER_REV = 1600


def _call(rpc, arg):
    with _lock:
        # On the Python side Bridge.call returns the RPC result directly
        # (the C++ .result() wrapper is MCU-side only).
        return Bridge.call(rpc, int(arg))


def do_sort(bin_index):
    return _call("sort", bin_index)


def do_step(step):
    """Move the stepper to absolute pulse `step` (0..STEPS_PER_REV)."""
    step = int(step)
    if not 0 <= step <= STEPS_PER_REV:
        raise ValueError(f"step must be 0..{STEPS_PER_REV}, got {step}")
    return _call("step", step)


def do_nudge(delta):
    """Move the stepper `delta` pulses from where it is (negative = CCW)."""
    delta = int(delta)
    if abs(delta) > STEPS_PER_REV:
        raise ValueError(f"delta must be within +/-{STEPS_PER_REV}, got {delta}")
    return _call("nudge", delta)


def do_servo(angle):
    """Move the servo arm to absolute `angle` (0..180 degrees)."""
    angle = int(angle)
    if not 0 <= angle <= 180:
        raise ValueError(f"angle must be 0..180, got {angle}")
    return _call("servo", angle)


def do_home():
    return _call("home", 0)


def do_zero():
    """Declare the pole's current physical position to be step 0 / bin 0.

    The manual alternative to a homing switch: release, hand-turn to physical
    zero, then zero. Also needed after a reflash/App restart, which resets the
    MCU's counter while the pole stays put.
    """
    return _call("zero", 0)


def do_release(on):
    """Energize (on truthy) or release (on falsy) the stepper's holding torque.

    Released, the pole turns freely by hand. Position is not tracked while
    released, so follow a hand-turn with /zero.
    """
    return _call("release", 1 if int(on) else 0)


def do_pos():
    return _call("pos", 0)


# POST path -> (required body key, handler). A key of None means the endpoint
# takes no argument. Every handler returns the MCU's report of where it ended up.
_POST_ROUTES = {
    "/sort": ("bin", do_sort),
    "/step": ("step", do_step),
    "/nudge": ("delta", do_nudge),
    "/servo": ("angle", do_servo),
    "/home": (None, do_home),
    "/zero": (None, do_zero),
    "/release": ("on", do_release),
}


class Handler(BaseHTTPRequestHandler):
    def _reply(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.rstrip("/")
        if path in ("/health", ""):
            self._reply(200, {"ok": True, "service": "trashbin-motor"})
        elif path == "/pos":
            try:
                self._reply(200, {"ok": True, "step": do_pos()})
            except Exception as e:
                self._reply(500, {"ok": False, "error": str(e)})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.rstrip("/")
        route = _POST_ROUTES.get(path)
        if route is None:
            self._reply(404, {"error": "not found"})
            return
        key, handler = route
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
            arg = int(data[key]) if key else None
        except (ValueError, KeyError, TypeError) as e:
            self._reply(400, {"error": f"bad request: {e}"})
            return
        label = f"{path[1:]}{'' if key is None else f' {key}={arg}'}"
        try:
            landed = handler() if key is None else handler(arg)
        except ValueError as e:  # out-of-range argument: caller's fault
            print(f"[motor] {label} REJECTED: {e}", flush=True)
            self._reply(400, {"ok": False, "error": str(e)})
            return
        except Exception as e:  # never crash the server on one bad move
            print(f"[motor] {label} FAILED: {e}", flush=True)
            self._reply(500, {"ok": False, "error": str(e)})
            return
        print(f"[motor] {label} -> landed={landed}", flush=True)
        payload = {"ok": True, "landed": landed}
        if key:
            payload[key] = arg
        self._reply(200, payload)

    def log_message(self, *_):  # quiet the default per-request stderr spam
        pass


def start_http_server():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[motor] HTTP bridge listening on :{PORT}", flush=True)
    server.serve_forever()


def main():
    # Run the HTTP server in a background thread; App.run() keeps the app alive
    # (and keeps the Bridge serviced). Guarded under __main__ so the handler
    # logic can be imported (e.g. by unit tests) without binding the port or
    # blocking in App.run(). App Lab launches this file as the main script.
    threading.Thread(target=start_http_server, daemon=True).start()
    App.run()


if __name__ == "__main__":
    main()
