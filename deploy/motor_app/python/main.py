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


def do_sort(bin_index):
    with _lock:
        # On the Python side Bridge.call returns the RPC result directly
        # (the C++ .result() wrapper is MCU-side only).
        return Bridge.call("sort", int(bin_index))


class Handler(BaseHTTPRequestHandler):
    def _reply(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") in ("/health", ""):
            self._reply(200, {"ok": True, "service": "trashbin-motor"})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") != "/sort":
            self._reply(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
            bin_index = int(data["bin"])
        except (ValueError, KeyError, TypeError) as e:
            self._reply(400, {"error": f"bad request: {e}"})
            return
        try:
            landed = do_sort(bin_index)
            print(f"[motor] sort bin={bin_index} -> landed={landed}", flush=True)
            self._reply(200, {"ok": True, "bin": bin_index, "landed": landed})
        except Exception as e:  # never crash the server on one bad move
            print(f"[motor] sort bin={bin_index} FAILED: {e}", flush=True)
            self._reply(500, {"ok": False, "error": str(e)})

    def log_message(self, *_):  # quiet the default per-request stderr spam
        pass


def start_http_server():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[motor] HTTP bridge listening on :{PORT}", flush=True)
    server.serve_forever()


# Run the HTTP server in a background thread; App.run() keeps the app alive
# (and keeps the Bridge serviced).
threading.Thread(target=start_http_server, daemon=True).start()

App.run()
