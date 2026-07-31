#!/usr/bin/env python3
"""Manual motor control for the UNO Q trashbin, driven over SSH from a laptop.

Standalone by design: this talks to nothing else in `deploy/` (no imports from
motor_bridge, no requests dependency) so you can copy this one file anywhere and
still jog the motors. It shells out to `ssh`, which runs `curl` on the board
against the "Trashbin Motor" App's HTTP port (127.0.0.1:8071) — the same port
the classifier uses, so the App must be running (`start_motor_app.sh` does that
at boot).

Two motors:

  stepper ("LM", the big one on the TB6600 driver) — absolute position in pulses,
      0..1600 for one full revolution at the 1/8 microstep DIP setting. 0 and
      1600 are the same place; each bin is 400 pulses apart (bin 0 = 0,
      bin 1 = 400, bin 2 = 800, bin 3 = 1200). Moves take the shorter way round.
  servo (the arm) — absolute angle 0..180. Only responds once the arm is wired
      and SERVO_ENABLED is flipped to 1 in motor_app/sketch/sketch.ino;
      until then the board answers landed=-1 and this prints a hint.

Usage (one-shot):

    ./motor_cli.py step 800          # stepper -> absolute pulse 800 (half turn)
    ./motor_cli.py nudge -50         # stepper -> 50 pulses counter-clockwise
    ./motor_cli.py servo 160         # arm -> 160 degrees
    ./motor_cli.py bin 2             # stepper -> bin 2 (== step 800)
    ./motor_cli.py sort 2            # full sort move for bin 2 (offset + sweep)
    ./motor_cli.py home              # re-home to step 0
    ./motor_cli.py pos               # print position, move nothing
    ./motor_cli.py health            # is the motor App up?

Usage (interactive — a bare number is a stepper position, which is the fast way
to hunt for bin alignment):

    ./motor_cli.py repl
    motor> 800            # stepper to pulse 800
    motor> +50            # nudge 50 pulses clockwise
    motor> servo 160
    motor> pos
    motor> q

Where to ssh (in precedence order): --ssh, then $MOTOR_SSH, else the default
below.

The board is normally reached over **Tailscale** (`arduino@100.119.45.76`),
which is the default here: it works from any network without port-forwarding or
exposing sshd publicly, and the link is WireGuard-encrypted and
device-authenticated. Plain ssh to the board on the same LAN works just as well
when Tailscale is down — nothing in this CLI depends on Tailscale itself:

    export MOTOR_SSH="arduino@uno-q.local"          # same-LAN fallback
    export MOTOR_SSH_PORT=22
    export MOTOR_SSH_JUMP="root@your.droplet.ip"    # ProxyJump, optional

Exit status is 0 only when the board acknowledged the move.
"""

import argparse
import json
import os
import shlex
import subprocess
import sys

DEFAULT_SSH = os.environ.get("MOTOR_SSH", "arduino@uno-q.local")
DEFAULT_SSH_PORT = os.environ.get("MOTOR_SSH_PORT", "")
DEFAULT_SSH_JUMP = os.environ.get("MOTOR_SSH_JUMP", "")
DEFAULT_URL = os.environ.get("MOTOR_URL", "http://127.0.0.1:8071")

# Geometry mirrored from sketch/sketch.ino. Keep in sync if the driver's
# microstep DIP setting changes.
STEPS_PER_REV = 1600
NUM_BINS = 4
STEPS_PER_BIN = STEPS_PER_REV // NUM_BINS   # 400

# A move blocks until the motor stops, so the ack can be slow: a half turn at
# the sketch's 800us half-period is ~1.3s, and ssh setup adds its own second.
DEFAULT_TIMEOUT = float(os.environ.get("MOTOR_ACK_TIMEOUT", "30"))


class MotorError(RuntimeError):
    """The board could not be reached, or it refused the command."""


def _ssh_argv(target, port, jump):
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    if port:
        argv += ["-p", str(port)]
    if jump:
        argv += ["-J", jump]
    argv.append(target)
    return argv


def _remote_curl(path, body, url, timeout):
    """The shell command to run ON the board: curl the motor App, print JSON."""
    cmd = ["curl", "-s", "--max-time", str(int(timeout)), f"{url}{path}"]
    if body is not None:
        cmd += [
            "-X", "POST",
            "-H", "Content-Type: application/json",
            "-d", json.dumps(body, separators=(",", ":")),
        ]
    return " ".join(shlex.quote(c) for c in cmd)


def call(
    path,
    body=None,
    ssh=None,
    port=None,
    jump=None,
    url=DEFAULT_URL,
    timeout=DEFAULT_TIMEOUT,
):
    """Hit one motor-App endpoint over ssh and return its decoded JSON.

    `body` None means GET. Raises MotorError if ssh fails, the App is
    unreachable, or the reply is not JSON.
    """
    argv = _ssh_argv(
        ssh or DEFAULT_SSH,
        DEFAULT_SSH_PORT if port is None else port,
        DEFAULT_SSH_JUMP if jump is None else jump,
    )
    argv.append(_remote_curl(path, body, url.rstrip("/"), timeout))
    try:
        # ssh's own timeout budget is the move timeout plus slack for the
        # handshake, so a hung motor surfaces as curl's error, not a kill.
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout + 20
        )
    except subprocess.TimeoutExpired:
        raise MotorError(f"ssh to {ssh or DEFAULT_SSH} timed out after {timeout + 20}s")
    except FileNotFoundError:
        raise MotorError("no `ssh` on PATH")
    out = proc.stdout.strip()
    if proc.returncode != 0 and not out:
        raise MotorError(
            f"ssh failed (exit {proc.returncode}): {proc.stderr.strip() or 'no output'}"
        )
    if not out:
        raise MotorError(
            f"no response from the motor App at {url} — is the 'Trashbin Motor' "
            "app running on the board? (arduino-app-cli app start ~/ArduinoApps/nema17)"
        )
    try:
        return json.loads(out)
    except ValueError:
        raise MotorError(f"non-JSON reply from the board: {out[:200]}")


# ---- One command per motor action -------------------------------------------
# Each returns the value the MCU reported (step position, angle, or bin), and
# raises MotorError when the board refuses the move.


def _landed(data, what):
    if not data.get("ok"):
        raise MotorError(f"{what} refused: {data.get('error', data)}")
    landed = data.get("landed", data.get("step"))
    if landed == -1:
        raise MotorError(
            f"{what} rejected by the MCU (out of range, or the peripheral is "
            "disabled in sketch.ino — see SERVO_ENABLED)"
        )
    return landed


def step(position, **kw):
    """Stepper -> absolute pulse `position`, 0..1600 (shorter direction)."""
    position = int(position)
    if not 0 <= position <= STEPS_PER_REV:
        raise MotorError(f"step must be 0..{STEPS_PER_REV}, got {position}")
    return _landed(call("/step", {"step": position}, **kw), f"step {position}")


def nudge(delta, **kw):
    """Stepper -> `delta` pulses from here; negative is counter-clockwise."""
    delta = int(delta)
    if abs(delta) > STEPS_PER_REV:
        raise MotorError(f"delta must be within +/-{STEPS_PER_REV}, got {delta}")
    return _landed(call("/nudge", {"delta": delta}, **kw), f"nudge {delta:+d}")


def servo(angle, **kw):
    """Servo arm -> absolute `angle`, 0..180 degrees."""
    angle = int(angle)
    if not 0 <= angle <= 180:
        raise MotorError(f"angle must be 0..180, got {angle}")
    return _landed(call("/servo", {"angle": angle}, **kw), f"servo {angle}")


def zero(**kw):
    """Declare the pole's current physical spot to be step 0 / bin 0.

    There is no homing switch, so this is what makes `home`'s claim true.
    Needed after every power-on, reflash and App restart.
    """
    return _landed(call("/zero", {}, **kw), "zero")


def release(on=0, **kw):
    """Drop (on falsy) or re-assert (on truthy) the stepper's holding torque.

    Released, the pole turns freely by hand; position is not tracked while
    released, so follow a hand-turn with `zero`.
    """
    on = 1 if int(on) else 0
    data = call("/release", {"on": on}, **kw)
    if not data.get("ok"):
        raise MotorError(f"release refused: {data.get('error', data)}")
    return on


def goto_bin(index, **kw):
    """Stepper -> bin `index` (0..3), i.e. pulse index*400."""
    index = int(index)
    if not 0 <= index < NUM_BINS:
        raise MotorError(f"bin must be 0..{NUM_BINS - 1}, got {index}")
    return step(index * STEPS_PER_BIN, **kw)


def sort(index, **kw):
    """Full sort move for bin `index`: the classifier's own code path."""
    index = int(index)
    if not 0 <= index < NUM_BINS:
        raise MotorError(f"bin must be 0..{NUM_BINS - 1}, got {index}")
    return _landed(call("/sort", {"bin": index}, **kw), f"sort {index}")


def home(**kw):
    """Re-home the stepper to pulse 0."""
    return _landed(call("/home", {}, **kw), "home")


def pos(**kw):
    """Current stepper pulse position; moves nothing."""
    data = call("/pos", **kw)
    if not data.get("ok"):
        raise MotorError(f"pos failed: {data.get('error', data)}")
    return data["step"]


def health(**kw):
    """True if the motor App answers its liveness probe."""
    return bool(call("/health", **kw).get("ok"))


def describe(position):
    """Human-readable gloss for a stepper position: degrees and nearest bin."""
    deg = position * 360.0 / STEPS_PER_REV
    nearest = round(position / STEPS_PER_BIN) % NUM_BINS
    off = position - nearest * STEPS_PER_BIN
    at_bin = f"bin {nearest}" if off == 0 else f"bin {nearest} {off:+d}"
    return f"step {position} ({deg:.1f} deg, {at_bin})"


# ---- CLI --------------------------------------------------------------------


def _repl(conn):
    print(
        "Manual motor control. A bare number 0-1600 is an absolute stepper "
        "position;\n'+N'/'-N' jogs relative. Also: servo <0-180>, bin <0-3>, "
        "sort <0-3>,\nhome, zero, release [0|1], pos, health, q to quit."
    )
    try:
        print(f"at {describe(pos(**conn))}")
    except MotorError as e:
        print(f"! {e}")
    while True:
        try:
            line = input("motor> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line in ("q", "quit", "exit"):
            return 0
        try:
            print(_run_line(line, conn))
        except MotorError as e:
            print(f"! {e}")
        except ValueError:
            print("! don't understand that — try a number 0-1600, or 'servo 90'")


def _run_line(line, conn):
    """Execute one REPL line, returning the text to print."""
    parts = line.split()
    head = parts[0]
    # A bare signed number is the common case: stepper position, or a jog.
    if head.lstrip("+-").isdigit() and len(parts) == 1:
        if head[0] in "+-":
            return f"-> {describe(nudge(int(head), **conn))}"
        return f"-> {describe(step(int(head), **conn))}"
    cmd = head.lower()
    arg = parts[1] if len(parts) > 1 else None
    if cmd in ("step", "s") and arg is not None:
        return f"-> {describe(step(arg, **conn))}"
    if cmd in ("nudge", "n") and arg is not None:
        return f"-> {describe(nudge(arg, **conn))}"
    if cmd == "servo" and arg is not None:
        return f"-> servo at {servo(arg, **conn)} deg"
    if cmd == "bin" and arg is not None:
        return f"-> {describe(goto_bin(arg, **conn))}"
    if cmd == "sort" and arg is not None:
        return f"-> sorted into bin {sort(arg, **conn)}"
    if cmd == "home":
        home(**conn)
        return f"-> {describe(pos(**conn))}"
    if cmd == "zero":
        zero(**conn)
        return f"-> zeroed here; {describe(pos(**conn))}"
    if cmd == "release":
        # Bare `release` drops torque — the common case; `release 1` re-holds.
        on = release(arg if arg is not None else 0, **conn)
        return "-> holding torque on" if on else "-> released; turn the pole by hand"
    if cmd == "pos":
        return f"at {describe(pos(**conn))}"
    if cmd == "health":
        return "motor App is up" if health(**conn) else "motor App is NOT healthy"
    raise ValueError(line)


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Drive the UNO Q trashbin motors over ssh.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Run with no command (or `repl`) for interactive mode.",
    )
    p.add_argument("--ssh", default=None, help=f"ssh target (default {DEFAULT_SSH})")
    p.add_argument("--port", default=None, help="ssh port (e.g. 2222 for the tunnel)")
    p.add_argument("--jump", default=None, help="ssh ProxyJump host")
    p.add_argument("--url", default=DEFAULT_URL, help=f"motor App URL on the board (default {DEFAULT_URL})")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="seconds to wait for a move")
    p.add_argument(
        "command",
        nargs="?",
        default="repl",
        choices=[
            "repl", "step", "nudge", "servo", "bin", "sort",
            "home", "zero", "release", "pos", "health",
        ],
        help="what to do",
    )
    p.add_argument("value", nargs="?", help="the number the command takes")
    args = p.parse_args(argv)

    conn = {
        "ssh": args.ssh,
        "port": args.port,
        "jump": args.jump,
        "url": args.url,
        "timeout": args.timeout,
    }

    if args.command == "repl":
        return _repl(conn)

    needs_value = args.command in ("step", "nudge", "servo", "bin", "sort")
    if needs_value and args.value is None:
        p.error(f"{args.command} needs a number")
    line = args.command if args.value is None else f"{args.command} {args.value}"
    try:
        print(_run_line(line, conn))
    except MotorError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
