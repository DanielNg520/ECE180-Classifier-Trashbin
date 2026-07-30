# Manual Motor Control — file map

How a manual motor command travels from a laptop to the two motors on the UNO Q,
and which file owns each hop. (For the automatic classifier path see the README's
architecture section; it enters at the same `:8071` port via `motor_bridge.py`.)

```
laptop                                board (UNO Q, Linux side)        MCU (STM32)
──────                                ─────────────────────────        ───────────
motor_cli.py  ──ssh + curl──►  motor_app/python/main.py  ──Bridge.call──►  motor_app/sketch/sketch.ino
  step 800                       POST /step {"step":800}                    rpcStep(800)
                                                                             └─ gotoStep() → pulses on PUL/DIR
```

## Files

| File | Role |
| --- | --- |
| `motor_cli.py` | **Standalone** host CLI. Builds an `ssh … curl` command, validates ranges locally, prints where the motor landed. Imports nothing else in `deploy/`. |
| `motor_app/python/main.py` | The App's HTTP layer on the board. One route per motor action; serializes everything behind `_lock` and forwards to the MCU over the RouterBridge. |
| `motor_app/sketch/sketch.ino` | MCU firmware. Owns `currentStep` (absolute pulse position) and the servo angle; exposes the RPCs below. |
| `motor_bridge.py` | The *classifier's* client for the same port (`/sort` only). Untouched by manual control. |
| `tests/test_motor_cli.py` | CLI tests — ssh/curl command shape, range rejection, REPL parsing. No board needed. |
| `tests/test_motor_app.py` | Board-side handler tests against a stubbed `arduino.app_utils`. |

## Command surface

| CLI | HTTP | RPC | Motor | Range |
| --- | --- | --- | --- | --- |
| `step N` / bare `N` in the REPL | `POST /step {"step":N}` | `step` | stepper (LM/big) | `0..1600` absolute pulses |
| `nudge ±N` / `+N`, `-N` in the REPL | `POST /nudge {"delta":N}` | `nudge` | stepper | `±1600` relative |
| `bin N` | `POST /step` at `N*400` | `step` | stepper | `0..3` |
| `sort N` | `POST /sort {"bin":N}` | `sort` | stepper (+ arm sweep) | `0..3` |
| `servo N` | `POST /servo {"angle":N}` | `servo` | servo arm | `0..180` degrees |
| `home` | `POST /home` | `home` | stepper | — |
| `pos` | `GET /pos` | `pos` | stepper | read-only |
| `health` | `GET /health` | — | — | liveness |

## Geometry (keep these three in sync)

`STEPS_PER_REV = 1600` appears in `sketch.ino` (source of truth, set by the
TB6600 DIP microstep setting), `motor_app/python/main.py` (range check), and
`motor_cli.py` (range check + the `describe()` gloss). Four bins ⇒ 400 pulses
apart: bin 0 = step 0, bin 1 = 400, bin 2 = 800, bin 3 = 1200.

`currentStep` in the sketch is the single source of truth for position — bin
moves are just step moves at multiples of 400, so manual jogging and automatic
sorting can never disagree about where the pole is. Every move takes the shorter
way around the circle; an exact tie goes clockwise.

## Notes

- The servo arm is **enabled** (`#define SERVO_ENABLED 1`, 2026-07-30). Pin 9,
  `ARM_REST=20` / `ARM_SWEEP=160`. Verified live: `POST /servo {"angle":20}`
  returns `landed=20` (it returned `-1` while compiled out).
  - Flipping that define is *not* sufficient on its own: `Servo.h` is not
    bundled with the `arduino:zephyr` core, so `sketch/sketch.yaml` must list
    `Servo (1.3.0)` under `libraries:` or the build dies with "Servo.h: No such
    file or directory" — and a failed build leaves the App **stopped**.
  - Note the param name differs by layer: HTTP takes `{"angle": N}`, the MCU
    RPC takes `deg`. `POST /servo {"deg":N}` fails with `bad request: 'angle'`.
  - **`rpcSort()` calls `sweepArm()`**, so with the servo on, every production
    sort now sweeps the arm and costs an extra `2*SWEEP_MS` (1 s). Decouple
    these if the arm should only move on manual command.
- Manual control needs the *"Trashbin Motor"* App running on the board — the
  same App the classifier needs (`start_motor_app.sh` starts it at boot).
- After editing the sketch:
  `arduino-app-cli app restart ~/ArduinoApps/nema17`.
- SSH target comes from `--ssh` / `$MOTOR_SSH` (default `arduino@uno-q.local`).
  Through the droplet's reverse tunnel: `--ssh arduino@localhost --port 2222
  --jump root@<droplet>`.
- **Manual zeroing (2026-07-30).** There is no homing switch wired, so
  `HOMING_ENABLED` stays `false` and nothing moves at boot — `homePole()` only
  declares `currentStep = 0`. The pole is returned to physical zero **by hand
  after every boot**, which makes that declaration true. Two RPCs support this:
  - `POST /release {"on":0}` — drops `ENA`, so the pole turns freely by hand
    instead of fighting holding torque (which skips steps). Position is *not*
    tracked while released. Any move re-energizes automatically (`rotate()`
    re-asserts `ENA` + 50 ms settle); `{"on":1}` does it explicitly.
  - `POST /zero {}` — declares the current physical spot step 0 / bin 0.
  Do this after **reflashes and `app restart` too**, not just power-on: those
  reset the MCU counter while the pole stays physically put.
  - Servo signal moved to **D6** (was D9). As of this writing the servo still
    does not physically move on either pin — wiring/power under investigation.
    Note `landed` echoes the commanded angle and is never proof of motion.
- **Motion ramp (2026-07-30).** `rotate()` runs a trapezoidal profile:
  `PULSE_START_US` (2500) -> `PULSE_US` cruise (800) over `RAMP_STEPS` (120),
  decelerating symmetrically. Before this, moves started instantly at the full
  625 pulses/s — above the motor's pull-in rate. A 400-pulse bin move went from
  ~0.71 s to ~1.15 s; the slower travel is *wanted*, it stops the load being
  thrown. Short moves never reach cruise (ramp index clamps to the nearer end).
  The stutter that prompted this turned out to be the casing, not firmware.
- **Board firmware sync (2026-07-30).** `~/ArduinoApps/nema17` on the board was
  stale (Jul 23–24, pre-`f8bc323`): it had only `sort`/`goto`/`home`, so every
  manual RPC 404'd. `sketch.ino`, `python/main.py` and `app.yaml` were pushed
  from `deploy/motor_app/` and the App restarted (reflashes the STM32U5).
  Previous versions kept alongside as `*.bak-2026-07-30`. Verify a sync with
  `curl -s :8071/pos` on the board — `{"error": "not found"}` means stale.
- **SSH over Tailscale (primary).** The board runs `tailscaled` (Debian trixie
  arm64 package, `systemctl enable`d) as tailnet node **`uno-q` /
  100.119.45.76**. `ssh unoq` works from anywhere, key-based, and sidesteps the
  campus wifi's client isolation. The `unoq` / `unoq-usb` aliases live in
  `~/.ssh/config` on the laptop. Node key expires 2027-01-26 unless key expiry
  is disabled for the node in the Tailscale admin console.
- **SSH over USB (ADB, fallback).** On a network with client isolation (e.g. campus guest
  wifi) the board is unreachable by IP even when both are on the same subnet.
  App Lab talks to the board over ADB, and so can we — forward the board's sshd
  to a local port and SSH through USB:

  ```bash
  ~/.arduino15/packages/arduino/tools/adb/*/adb forward tcp:2222 tcp:22
  ssh unoq          # ~/.ssh/config alias -> arduino@127.0.0.1:2222
  ```

  The forward is per-`adb-server` and is lost when the board is unplugged or the
  server dies; re-run the `adb forward` line. With this, the CLI works as
  `MOTOR_SSH=arduino@127.0.0.1 MOTOR_SSH_PORT=2222 python3 deploy/motor_cli.py pos`.
  Board hostname is `cohua`, user `arduino`, ADB serial `2723903952`.
