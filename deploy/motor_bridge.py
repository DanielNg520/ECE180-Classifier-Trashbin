"""Linux -> MCU bridge: send a target bin to the motor-control sketch.

The Uno Q's Linux side and its STM32 microcontroller talk over an internal
serial link. This module opens that port and sends one line per sort request:

    SORT <bin>\n        e.g. "SORT 2\n"

The sketch (motor_control.ino) rotates the pole to the bin NEXT TO <bin>
(the OFFSET lives in firmware) and sweeps the servo arm. We optionally wait
for an "OK <bin>\n" ack so the camera loop doesn't fire the next item while
the arm is still moving.

Config (env):
    MOTOR_ENABLED   "1" to actually drive motors (default off = camera-only)
    MCU_PORT        serial device to the STM32 (default /dev/ttyMCU)
    MCU_BAUD        baud, must match the sketch (default 115200)
    MCU_ACK_TIMEOUT seconds to wait for the sketch's "OK" (default 8)

Everything here is best-effort: a missing/errored motor link logs a warning
and never crashes the classifier loop.
"""
import os
import time

MOTOR_ENABLED = os.environ.get("MOTOR_ENABLED", "0") == "1"
MCU_PORT = os.environ.get("MCU_PORT", "/dev/ttyMCU")
MCU_BAUD = int(os.environ.get("MCU_BAUD", "115200"))
MCU_ACK_TIMEOUT = float(os.environ.get("MCU_ACK_TIMEOUT", "8"))

_serial = None  # lazily opened, reused across calls


def _get_serial():
    """Open (once) the serial link to the MCU, or return None on failure."""
    global _serial
    if _serial is not None:
        return _serial
    try:
        import serial  # pyserial
    except ImportError:
        print("[motor] pyserial not installed — run: pip install pyserial")
        return None
    try:
        _serial = serial.Serial(MCU_PORT, MCU_BAUD, timeout=MCU_ACK_TIMEOUT)
        # Most boards reset when the port opens; give the sketch time to boot
        # and finish homing before we send the first command.
        time.sleep(2.0)
        _serial.reset_input_buffer()
        print(f"[motor] connected {MCU_PORT} @ {MCU_BAUD}")
    except Exception as e:  # noqa: BLE001 — never let a bad port kill the loop
        print(f"[motor] cannot open {MCU_PORT}: {e}")
        _serial = None
    return _serial


def send_sort(bin_index, wait_ack=True):
    """Tell the MCU to sort the current item into `bin_index` (0-3).

    Returns True if the command was sent (and acked, when wait_ack). No-op
    that returns False when MOTOR_ENABLED is off or the link is unavailable.
    """
    if not MOTOR_ENABLED:
        return False
    ser = _get_serial()
    if ser is None:
        return False
    try:
        ser.reset_input_buffer()
        ser.write(f"SORT {int(bin_index)}\n".encode())
        ser.flush()
        if not wait_ack:
            return True
        deadline = time.time() + MCU_ACK_TIMEOUT
        while time.time() < deadline:
            line = ser.readline().decode(errors="replace").strip()
            if not line:
                continue
            if line.startswith("OK"):
                return True
            print(f"[motor] {line}")  # surface HOME/DEBUG lines from the sketch
        print("[motor] timed out waiting for OK — is the sketch running?")
        return False
    except Exception as e:  # noqa: BLE001
        print(f"[motor] send failed: {e}")
        global _serial
        _serial = None  # force reopen next time
        return False
