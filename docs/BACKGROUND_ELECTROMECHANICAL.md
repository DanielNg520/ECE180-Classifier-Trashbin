# Electromechanical Study Notes — Trashbin Sorter

A from-zero guide to the *non-camera* half of this project: motors, drivers,
power, and how the microcontroller drives them. Written for someone who knows
software but has never touched a motor. Everything here maps to the actual
parts in this build and to `deploy/motor_app/sketch/sketch.ino`.

Read top to bottom the first time — each section builds on the last.

> **As-built wiring (2026-07-23).** The pin examples further down are generic
> teaching examples. The *actual* firmware that ships is the Arduino App Lab
> app `deploy/motor_app/sketch/sketch.ino`. (An earlier standalone sketch,
> `motor_control.ino`, drove the MCU directly over serial; it was removed once
> the App Lab RouterBridge became the only supported path — see the README's
> *On-Device Deployment* section for why.) The
> bench-confirmed stepper wiring is **PUL=2, DIR=3, ENA=4** (common-anode,
> active-LOW) at 1/8 microstep → **1600 steps/rev = 400 steps/bin**. The servo
> arm and homing switch are **not wired yet** (`SERVO_ENABLED` / `HOMING_ENABLED`
> are off in the sketch). When you wire the servo, pick a pin that does **not**
> collide with the stepper's 2/3/4.

---

## 0. The 60-second mental model

Think of it in layers, like a software stack:

```
Camera + classifier (Linux)        "this is a plastic bottle -> bin 1"
        |  serial: "SORT 1\n"
Microcontroller / MCU (the sketch)  decides angles, counts pulses, timing
        |  logic signals: STEP / DIR / PWM  (tiny 3-5V wires, no power)
Motor DRIVERS (stepper driver, servo internals)   the "muscle amplifiers"
        |  fat wires, lots of current
MOTORS (NEMA 17 stepper, MG996R servo)            actually move things
        ^
POWER (12V supply -> buck converters)             feeds the muscle
```

Two ideas that trip up every beginner, so internalize them now:

1. **Signal wires vs power wires are different worlds.** The MCU never powers a
   motor directly — it can only supply a few milliamps, like a whisper. It
   sends a *command* (a small voltage) to a **driver**, and the driver opens
   the floodgates from the big power supply to the motor. Blowing this up:
   plugging a motor straight into an MCU pin fries the pin instantly.

2. **"Ground" is the shared zero.** Voltage is always measured *relative* to
   something. Every part must agree on where "0V" is, or signals are
   meaningless. That's why "tie all grounds together" is repeated everywhere.

📺 *Watch first (20 min, the single best beginner primer):*
- "Electronics Basics" playlist by **GreatScott!** on YouTube — start with
  "How Voltage, Current & Resistance work". Search: `GreatScott electronics basics`
- Or **Branch Education** "How does a Transistor Work" for the intuition of a
  small signal controlling big power.

---

## 1. Electricity fundamentals (the absolute minimum)

You need exactly four words: **voltage, current, ground, and PWM.**

- **Voltage (V, volts)** — "electrical pressure." Our supply is 12V. Logic
  signals are 3.3V or 5V. Think water *pressure* in a pipe.
- **Current (A, amps / mA milliamps)** — "flow rate." Motors are thirsty
  (0.5–2A). Logic pins provide almost none (~0.02A). Think water *flow*.
- **Ground (GND, 0V)** — the reference everything measures against. The
  "return path" — current flows *out* of the supply, through the motor, and
  *back* to ground. A circuit must be a complete loop.
- **A "short circuit"** is when + connects directly to − with nothing in
  between. Current spikes, things get hot/burn. This is what you're avoiding
  when you wire carefully.

Analogy that actually works: **Voltage = water pressure, Current = flow rate,
Wire = pipe, Ground = the reservoir it all drains back into, Resistor = a
narrow section of pipe.**

Ohm's Law, the one equation: **V = I × R** (Voltage = Current × Resistance).
You rarely compute it by hand here, but it explains *why* resistors exist and
why thin wires get hot.

📖 *Free resources:*
- **SparkFun** "What is a Circuit?" and "Voltage, Current, Resistance, and
  Ohm's Law" — learn.sparkfun.com (search those titles). Best written intros
  that exist, with pictures.
- **Khan Academy** → "Electrical engineering" → "Circuit analysis" for the
  math side if you want it (optional for this project).
- 📺 **The Engineering Mindset** "Voltage explained" and "Current explained".

---

## 2. Power: the 12V supply and the buck converters

Your parts: **one 12V 5A power supply** and **two 12V→5V buck converters.**

### Why not just one voltage?
Different parts want different voltages:
- The **stepper motor + its driver** want **12V** (higher voltage = more speed
  and torque). Fed straight from the supply.
- The **servo** wants **~5–6V**. Give it 12V and it dies.
- The **logic** (MCU side) wants 3.3V/5V.

A **buck converter** is a small board that efficiently steps a higher voltage
DOWN to a lower one (12V → 5V) *without* wasting most of the energy as heat
(unlike a plain resistor). "Buck" = step-down. Its cousin "boost" = step-up.

Most buck modules have a tiny screw (**potentiometer**) you turn to set the
output voltage. **You set this with a multimeter BEFORE connecting anything:**
power the input, probe the output, turn the screw until it reads 5.0V.

### Why *two* bucks?
Isolation. Motors create nasty electrical "noise" — sudden current spikes when
they start/stop that make the voltage sag and spike. If your servo and your
logic share the same 5V rail as a motor, that noise can reset your MCU or make
the servo jitter. Two separate bucks = one clean rail for logic, one for the
servo's muscle. (This is *the* most common cause of "my board randomly
reboots" in student projects.)

### The "5A" number — a budget
Amps is a *budget*, not a fixed draw. The supply can deliver *up to* 5A. Add up
what everything pulls at once (stepper ~1.5A + servo up to ~1A stall + logic
~0.5A ≈ 3A) and stay comfortably under 5A. You're fine here.

⚠️ **Safety:** 12V at 5A won't shock you (too low voltage to feel), but it
*can* deliver enough current to overheat a wire or scorch a component if you
short it. Never wire with the power plugged in. Double-check + and − before
first power-on.

📺 *Free resources:*
- **GreatScott!** "DIY Buck Converter" / "What is a buck converter?" — shows
  exactly what these boards do.
- **SparkFun** learn: "How to Power a Project" — walks through picking voltages
  and adding them up.
- 📺 **DroneBot Workshop** "Powering Your Projects" — extremely beginner
  friendly, ~20 min.

---

## 3. Grounding: the rule that silently ruins projects

**Every ground must connect to every other ground.** The 12V supply ground,
both buck grounds, the stepper driver ground, the servo ground, and the MCU
ground — all one connected node, often called "common ground."

Why: a signal wire says "5V" only *relative to its ground*. If the MCU's ground
and the driver's ground aren't the same node, the driver sees a garbage voltage
and behaves randomly. This is invisible — nothing looks wrong — which is why it
eats hours. Wire grounds first, deliberately.

You do **not** connect the positives together (12V and 5V stay separate). Only
grounds are shared.

📺 *Free resource:* Search `DroneBot Workshop common ground` — he demonstrates
this failure live.

---

## 4. Motors, part 1: the SERVO (MG996R)

A servo is the *easy* motor — it's the "hello world" of actuators.

### What it is
A small geared motor with built-in electronics that holds a **commanded
angle**. You tell it "go to 90°" and it goes there and holds. Range is usually
0–180°. In this project it's the **arm that sweeps trash off the platform.**

### The three wires
- **Brown/Black = Ground**
- **Red = Power** (5–6V — from a buck, NOT the MCU's 3.3V pin; the MG996R can
  pull ~1A when pushing hard and would brown out the board)
- **Orange/Yellow = Signal** (goes to an MCU pin — this is *just* a signal,
  tiny current)

### How you control it — PWM
**PWM = Pulse Width Modulation.** The MCU sends a repeating pulse ~50 times a
second (every 20ms). The *width* of the pulse encodes the angle:
- ~1.0ms wide → 0°
- ~1.5ms wide → 90°
- ~2.0ms wide → 180°

You never write this by hand — Arduino's **`Servo` library** does it. In the
sketch it's literally:
```cpp
#include <Servo.h>
Servo arm;
arm.attach(9);        // signal on pin 9
arm.write(160);       // go to 160 degrees
```
That's the whole API. In `sketch.ino`, `ARM_REST` and `ARM_SWEEP` are just two
angles, and `sweepArm()` moves between them.

### Gotchas
- **Power it from the buck, not the board.** #1 servo mistake.
- Common ground with the MCU (section 3).
- The MG996R is strong (~10 kg·cm) and will happily strip its own plastic gears
  or bend your arm if it jams — don't command it into a hard stop repeatedly.

📺 *Free resources (do these — servos are very "watch and learn"):*
- **DroneBot Workshop** "Servo Motors with Arduino" — the definitive beginner
  video.
- Official **Arduino** docs: search `Arduino Servo library` → the "Sweep" and
  "Knob" example sketches. Run "Sweep" first — it's 10 lines and moves a servo
  back and forth. Instant win.
- **HowToMechatronics** "How Servo Motors Work & How To Control Servos with
  Arduino" — great article + video with the PWM diagrams.

---

## 5. Motors, part 2: the STEPPER (NEMA 17) + its driver

Steppers are the *interesting* one and where most of your learning is.

### What "NEMA 17" means
Just a **size standard** — the faceplate is 1.7 inches. Says nothing about
power. It's the common hobby-CNC/3D-printer motor.

### Why a stepper (vs a servo or plain motor)?
A stepper moves in exact, countable **steps** — typically **200 full steps per
full rotation** (1.8° each). No feedback sensor needed: if you send 200 pulses,
it turned exactly once. That precision is why it rotates your **pole** to face a
specific bin. A plain DC motor just spins; a servo only reaches ~180°; a stepper
can rotate continuously *and* precisely.

### Steps, microsteps, and your "400 per bin"
The driver can split each physical step into **microsteps** for smoothness.
Your driver set to "8 microstep" means 8 microsteps per step:
`200 steps × 8 = 1600 microstep-pulses per full revolution.`
Four bins around a circle = 90° each = a quarter turn =
`1600 ÷ 4 = 400 pulses per bin.` That's `STEPS_PER_BIN = 400` in the sketch.
**This is the core idea: rotating to a bin = sending a counted number of
pulses.**

### The driver (your black "Microstep Driver" box, a TB6600 class)
The stepper has 4 wires (two coils, A and B) that must be energized in a
precise dance. Doing that from an MCU directly is impossible — the **driver**
does it. You only give the driver three simple logic signals:

- **PUL (Pulse / STEP)** — one pulse = one microstep. Send 400 pulses → moves
  one bin. This is the whole motion primitive.
- **DIR (Direction)** — HIGH or LOW picks clockwise vs counterclockwise.
- **ENA (Enable)** — turns the motor's holding power on/off.

In code, "send a pulse" is just:
```cpp
digitalWrite(PIN_PUL, LOW);  delayMicroseconds(800);
digitalWrite(PIN_PUL, HIGH); delayMicroseconds(800);   // that's ONE step
```
Do that 400 times → one bin over. That's exactly `stepPulse()` and `rotate()`
in the sketch. (Ours is "active-LOW", see section 6, so LOW=on.)

### The two sets of DIP switches on the driver
Your driver has 6 little switches:
- **SW1–3 set microstepping** — we chose "8" (1600 pulse/rev) for clean 400/bin.
- **SW4–6 set current limit** — how many amps the driver feeds the motor. Set
  this to your motor's rated current (on its label, ~1.5A typical). **Too high =
  motor cooks; too low = weak, skips steps.** The table is printed on the driver
  and I put the exact switch positions in our earlier chat.

### "Losing steps" and why you home
A stepper is **open-loop** — no sensor tells it where it actually is. If it
jams, or you cut power, it forgets its position. So on boot the sketch drives
the pole until it hits the **physical zero stop** (a switch), and *declares that
position bin 0*. Everything is counted from there. That's `homePole()`. This is
the single most important concept for reliability.

📺 *Free resources (spend real time here):*
- **DroneBot Workshop** "Stepper Motors with Arduino" — long, thorough, perfect.
  Watch the whole thing.
- **How To Mechatronics** "How Stepper Motors Work" — best animation of the
  coil dance; and his "Control Stepper with A4988" article (same STEP/DIR idea
  as your TB6600).
- **DroneBot Workshop** "Big Stepper Motors with Arduino" — specifically covers
  **TB6600-style** drivers like yours (wiring the ± inputs, DIP switches).
- Search `TB6600 wiring common anode common cathode` — a couple of short blog
  posts explain the ± input wiring we used.

---

## 6. The wiring detail we chose: "active-LOW / common-anode"

Your driver's inputs are **opto-isolated** (a tiny internal LED per signal,
which electrically separates the driver from your MCU — good, it protects your
board). Each input has a **+** and a **−** terminal: PUL+/PUL−, DIR+/DIR−,
ENA+/ENA−. The LED lights (signal "on") when current flows from + to −.

These LEDs are designed for ~5V, but the Uno Q's MCU pins output **3.3V** — a
bit weak to drive the + side reliably. The fix ("common-anode" wiring):
- Tie all the **+** terminals to a steady **5V** (from a buck).
- Connect each **−** terminal to an MCU pin.
- Now the signal turns *on* when the MCU pin goes **LOW** (0V), because that's
  when current flows through the LED. Hence **active-LOW**.

That's why the sketch has `ASSERT = LOW` — it's not a mistake, it's this wiring.
Don't overthink it; just wire + to 5V, − to the pins, and the code already
matches.

📖 Search `optocoupler explained` (GreatScott has one) if you want to see why
that little LED-and-sensor keeps motor noise from frying your MCU.

---

## 7. How software becomes motion (the full path)

Trace one item through the whole stack — this ties your camera world to the new
one:

1. **Item lands on platform.** Camera sees motion → `camera_loop.py` grabs a
   frame (your domain).
2. **Classifier** says `plastic_water_bottles`, 0.92 confidence (your domain).
3. **`bin_map.py`** turns that label into `bin 1` (containers).
4. **`motor_bridge.py`** POSTs `{"bin": 1}` to the motor App on
   `http://127.0.0.1:8071/sort`, which relays it to the MCU over the
   RouterBridge as `Bridge.call("sort", 1)`.
5. **`sketch.ino`** receives it, computes the stop position
   `(1 + OFFSET) % 4`, and calls `rotate(400 × n, dir)` — literally toggling the
   PUL pin that many times.
6. The **driver** turns those pulses into the coil dance → the **NEMA 17**
   turns the **pole** to the right spot.
7. The sketch calls `sweepArm()` → the **servo** pushes the item into the bin.
8. Sketch sends `OK 1\n` back so Python knows it can handle the next item.

Every arrow is a thing you can test in isolation (see section 8).

---

## 8. Hands-on learning path (do these in order — don't read, *build*)

You learn this by making one thing move at a time. Cheap and safe:

1. **Blink an LED** with the MCU. Proves you can flash code and toggle a pin.
   (Arduino "Blink" example.) `digitalWrite` is *the* primitive behind
   everything else.
2. **Run the servo "Sweep" example.** Power the servo from your buck. Watch the
   arm move. You now understand PWM by feel.
3. **Spin the stepper a known amount.** Wire the driver, set the DIPs, send 1600
   pulses, confirm it makes *exactly one* full turn. Then 400 → a quarter turn.
   This is the "aha" moment.
4. **Add the DIR pin** — make it go one way, then back.
5. **Add the zero-stop switch** — read it with `digitalRead`, print HIGH/LOW as
   you press it. Then combine into homing.
6. **Send commands over Serial** — open the Serial Monitor, type `SORT 2`, watch
   it move. (Our sketch already does this.)
7. **Connect the Python side** and let the classifier drive it.

Each step is ~30 min and independently satisfying. If step N breaks, you know
it's step N's wiring — that's the whole point of going one at a time.

📺 *The one channel to subscribe to:* **DroneBot Workshop**. His Arduino +
motors + power tutorials are the clearest free material on exactly this stack.
Second: **How To Mechatronics** for animations, **GreatScott!** for the
electronics theory, **SparkFun/Adafruit learn sites** for written references.

---

## 9. Glossary (skim when a word stumps you)

- **MCU / microcontroller** — the small "brain" chip that runs your sketch and
  toggles pins. On the Uno Q it's the STM32 half.
- **Driver** — power amplifier between MCU signals and a motor.
- **Stepper** — motor that moves in exact counted steps.
- **Servo** — motor that holds a commanded angle (0–180°).
- **NEMA 17** — a motor *size* (1.7" face), not a spec.
- **PWM** — encoding a value (servo angle, or motor speed) as pulse width.
- **STEP/PUL, DIR, ENA** — the three stepper-driver control signals.
- **Microstepping** — splitting one physical step into smaller ones for
  smoothness.
- **Buck converter** — steps a voltage down efficiently (12V→5V).
- **Ground / GND / common ground** — the shared 0V reference; tie them all
  together.
- **Open-loop** — no position feedback; why we home to a zero stop.
- **Homing** — driving to a known physical reference on startup to sync
  position.
- **Optocoupler / opto-isolated** — an LED+sensor that passes a signal while
  electrically isolating two circuits (protects the MCU).
- **Active-LOW** — signal is "on" when the pin is at 0V (our driver wiring).
- **Potentiometer** — an adjustable resistor; the buck's voltage-set screw.
- **Multimeter** — the one tool to buy; measures voltage/continuity. Learn
  "measure DC volts" and "continuity beep" — that's 90% of debugging.

---

## 10. If you buy/borrow one tool

A **multimeter** (a $15 one is fine). Two things it does that will save you:
- **Measure DC voltage:** confirm your buck really outputs 5.0V before you plug
  in the servo. Confirm 12V is 12V.
- **Continuity (beep) mode:** touch two points; it beeps if they're connected.
  Use it to verify all your grounds are actually one node (section 3), and to
  catch shorts before powering on.

📺 Search `how to use a multimeter DroneBot` — 20 minutes, and you'll debug
hardware like you debug code.
