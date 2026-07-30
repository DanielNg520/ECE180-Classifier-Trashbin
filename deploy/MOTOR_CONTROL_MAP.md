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

- The servo arm is compiled out (`#define SERVO_ENABLED 0` at the top of
  `sketch.ino`) because its wiring isn't confirmed. Until it's flipped to `1`,
  `servo N` returns `landed=-1` and the CLI says the peripheral is disabled.
  Flipping that one line pulls in `<Servo.h>` and the `arm` object with it.
- Manual control needs the *"Trashbin Motor"* App running on the board — the
  same App the classifier needs (`start_motor_app.sh` starts it at boot).
- After editing the sketch:
  `arduino-app-cli app restart ~/ArduinoApps/nema17`.
- SSH target comes from `--ssh` / `$MOTOR_SSH` (default `arduino@uno-q.local`).
  Through the droplet's reverse tunnel: `--ssh arduino@localhost --port 2222
  --jump root@<droplet>`.
