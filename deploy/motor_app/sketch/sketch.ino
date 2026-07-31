/*
 * Trashbin sorter — MCU side (Arduino UNO Q, arduino:zephyr:unoq).
 *
 * Part of an Arduino App Lab app. The Linux/Python side classifies an item and
 * calls the RPC we expose over the RouterBridge:
 *
 *     Bridge.call("sort", bin)     bin = 0..NUM_BINS-1
 *
 * We rotate the pole to that bin by the SHORTER direction (clockwise or
 * counter-clockwise); on a tie we go CLOCKWISE. The call blocks until the move
 * finishes and returns the bin we landed on (or -1 for a bad bin).
 *
 * For manual/bench control there are also absolute-position RPCs, used by the
 * host-side CLI (deploy/motor_cli.py, driven over ssh):
 *
 *     Bridge.call("step",  n)      stepper -> absolute pulse 0..STEPS_PER_REV
 *     Bridge.call("nudge", d)      stepper -> relative move, signed pulses
 *     Bridge.call("pos",   0)      stepper -> current absolute pulse
 *     Bridge.call("servo", deg)    servo arm -> absolute angle 0..180
 *
 * The stepper's absolute position `currentStep` is the single source of truth;
 * bin moves are just steps at multiples of STEPS_PER_BIN, so manual jogging and
 * bin sorting can never disagree about where the pole is.
 *
 * Stepper wiring (bench-confirmed on this rig — the driver is a
 * TB6600-class, common-anode / active-LOW board):
 *   PUL- = pin 2, DIR- = pin 3, ENA- = pin 4   (PUL+/DIR+/ENA+ tied to +5V).
 *   Driver DIP set to 1/8 microstep => 1600 pulses/rev.
 * The servo arm is wired and ENABLED (pin 6). The homing switch is not wired,
 * so HOMING_ENABLED stays false and homePole() just declares the current spot
 * to be step 0 — flip it once a limit switch is on HOME_PIN.
 */

#include <Arduino_RouterBridge.h>

// Gates the Servo include, the Servo object, and every arm movement. Set to 0
// to build stepper-only (e.g. to bench the pole with the arm disconnected);
// sweepArm() then reports success so a sort still completes.
// NOTE: Servo.h is not bundled with the arduino:zephyr core — sketch.yaml must
// list `Servo (1.3.0)` under `libraries:` or the build fails and the App is
// left stopped.
#define SERVO_ENABLED 1

#if SERVO_ENABLED
#include <Servo.h>
// Constructed in setup(), NOT as a static global. The zephyr backend's Servo
// constructor calls initTimer(), which starts a Zephyr counter device; at
// static-init time that device may not be ready yet, and the failure is silent
// (initTimer returns 0, timer_is_started stays false, and every write() is a
// no-op forever). Deferring construction to setup() runs it after the kernel
// is up. `servoTimerOk` records whether the timer actually started.
static Servo *arm = nullptr;
static bool   servoTimerOk = false;
#endif

// ---- Stepper pins (match the physical wiring: PUL/DIR/ENA = 2/3/4) ----
const int PUL_PIN = 2;
const int DIR_PIN = 3;
const int ENA_PIN = 4;
const int ENA_ACTIVE = LOW;   // common-anode: LOW = driver enabled / holding torque

// Signals are active-LOW (common-anode wiring): a pulse asserts LOW then
// releases HIGH, matching the bench-confirmed driver behaviour.
const int PUL_ASSERT  = LOW;
const int PUL_RELEASE = HIGH;

// ---- Optional peripherals (enable once physically wired) ----
// SERVO_ENABLED is the #define above (it gates the Servo include too).
const int  SERVO_PIN      = 6;   // clear of stepper 2/3/4 and home switch 5
const bool HOMING_ENABLED = false;
const int  HOME_PIN       = 5;   // limit switch to GND, INPUT_PULLUP

// ---- Geometry ----
// The driver DIP is set to 1/8 microstep => 1600 pulses per full revolution.
// Four bins around the circle => 90 deg / 400 pulses each. If you change the
// DIP microstep setting, rescale STEPS_PER_REV.
const int  NUM_BINS       = 4;
const long STEPS_PER_REV  = 1600;
const long STEPS_PER_BIN  = STEPS_PER_REV / NUM_BINS;   // 400 at 1/8 microstep
// Cruise half-period once the ramp is up to speed (800 us => ~625 pulses/s).
const int  PULSE_US       = 800;                         // matches bench timing
// Starting (slowest) half-period, used for the first pulse of every move and
// the last before stopping. Must be below the motor's pull-in rate — raise it
// if moves still stall on startup, lower it if starts feel sluggish.
const int  PULSE_START_US = 2500;                        // ~200 pulses/s
// How many pulses the accel and decel ramps each take.
const long RAMP_STEPS     = 120;

// Direction levels on DIR_PIN. If the pole turns the WRONG way physically,
// just swap these two values (one-line fix, no other logic changes).
const int  CW  = HIGH;
const int  CCW = LOW;

// The pole can stop OFFSET bins short of the target so a servo arm sweeps the
// item the rest of the way. With the servo disabled we stop AT the target.
const int  OFFSET = 0;

// ---- Servo sweep angles (only used when SERVO_ENABLED) ----
// Bench-set 2026-07-30: the arm rests at 160 (its parked/home position) and
// sweeps down to 0, i.e. 160 -> 0 -> 160. This pair is the only thing that sets
// the sweep direction; there is no separate direction flag. Both ends were
// driven by hand on the real linkage first to confirm neither one binds.
const int  ARM_REST   = 160;
const int  ARM_SWEEP  = 0;
// How long the arm stays out at ARM_SWEEP before returning: the time the item
// is actually being pushed off the platform. setArmAngle() already waits
// SERVO_TRAVEL_MS for the arm to get there, so this is dwell, not travel.
const int  SWEEP_MS   = 3000;

// Pulse range handed to attach(). 1000..2000 us is the conservative standard
// range, but on this MG996R it yielded only ~half the commanded travel (a
// 180 deg command swept ~90 deg), so it is widened to the servo's own spec.
// Verified 2026-07-30: full travel, no end-stop stall. If a future horn or
// linkage binds before the ends, narrow this back rather than letting it push.
const int  SERVO_MIN_US = 600;
const int  SERVO_MAX_US = 2400;

// An MG996R holding a stalled position overheats within seconds, so we do not
// hold at all: drive to the angle, allow SERVO_TRAVEL_MS to get there, then
// detach so no pulses are sent at rest. This is what pulling the signal wire
// does by hand. The arm is unpowered between moves and back-drives freely --
// fine for a sweep arm, but it will not hold against a load.
const int  SERVO_TRAVEL_MS = 400;

// Settle time between a failed attach() and the retry, to let the zephyr timer
// actually release before we ask for it again.
const int  SERVO_RETRY_MS  = 50;

// ---- Homing ----
const long HOME_MAX_STEPS = STEPS_PER_REV * 2;  // give up after 2 revs

// Absolute stepper position in pulses, 0..STEPS_PER_REV-1, wrapping. This is
// the single source of truth: `currentBin` is derived from it.
long currentStep = 0;

// Last angle commanded to the servo arm (meaningless while SERVO_ENABLED is 0).
int currentAngle = ARM_REST;

// Whether the stepper is energized (holding torque on). Released by the
// "release" RPC so the pole can be hand-turned; any move re-energizes.
bool motorHeld = true;

// One pulse with an explicit half-period, in microseconds. The ramp in
// rotate() varies this per step; PULSE_US remains the cruise (fastest) value.
void stepPulseUs(int halfPeriodUs) {
  digitalWrite(PUL_PIN, PUL_ASSERT);
  delayMicroseconds(halfPeriodUs);
  digitalWrite(PUL_PIN, PUL_RELEASE);
  delayMicroseconds(halfPeriodUs);
}

void stepPulse() { stepPulseUs(PULSE_US); }

// Rotate `steps` pulses in direction `dir` (CW/CCW level on DIR_PIN), with a
// trapezoidal speed ramp: accelerate from PULSE_START_US to the PULSE_US
// cruise rate over RAMP_STEPS, then decelerate symmetrically before stopping.
//
// Without this the motor was commanded straight from standstill to the full
// 1/PULSE_US rate, which is above its pull-in torque: the rotor cannot stay in
// sync with the field, giving rough/growly motion and — under load — an
// outright stall (buzzing, no rotation). Ramping also crosses the motor's
// low-speed resonance quickly instead of sitting in it.
//
// Short moves never reach cruise: the ramp index is clamped by distance to
// whichever end is nearer, so a 10-pulse jog is simply slow throughout.
void rotate(long steps, int dir) {
  // Re-energize if we were released for hand-turning, and give the driver a
  // moment to establish holding current before the first pulse.
  if (!motorHeld) {
    digitalWrite(ENA_PIN, ENA_ACTIVE);
    motorHeld = true;
    delay(50);
  }
  // Re-assert the pin modes every move. The servo's attach()/detach() on
  // SERVO_PIN reconfigures a zephyr timer, and that was leaving DIR_PIN no
  // longer driven as a GPIO output: digitalWrite() below became a no-op, DIR
  // stayed stuck at its previous level, and every move AFTER a sweep turned
  // the same way -- so a sort went out to the bin and then kept going instead
  // of retracing. Cheap to redo, and it makes the direction independent of
  // whatever the servo did to the pin muxing.
  pinMode(DIR_PIN, OUTPUT);
  pinMode(PUL_PIN, OUTPUT);
  pinMode(ENA_PIN, OUTPUT);
  // pinMode(OUTPUT) leaves the pin driven LOW, and PUL_ASSERT is LOW, so PUL
  // comes out of the reconfigure already asserted: the first
  // digitalWrite(PUL_ASSERT) in the loop below would be a no-op and every move
  // would silently lose its first pulse. Release it explicitly.
  digitalWrite(PUL_PIN, PUL_RELEASE);
  delay(2);                         // let the reconfigured pins settle
  // The TB6600's DIR input is opto-isolated and latches on the TRANSITION, not
  // the level -- see the boot-time priming in setup(). pinMode() above left DIR
  // LOW, which is also CCW, so writing the target level alone gave CW an edge
  // on every move and CCW none at all: CCW moves simply did not turn. Drive the
  // opposite level first so both directions always produce a real edge.
  digitalWrite(DIR_PIN, dir == CW ? CCW : CW);
  delay(2);
  digitalWrite(DIR_PIN, dir);
  // DIR setup before the first pulse. The datasheet wants ~5 us, but the opto
  // was missing changes at 50 us. 2 ms costs nothing next to a move measured in
  // hundreds of milliseconds.
  delay(2);
  for (long i = 0; i < steps; i++) {
    long fromEnd = steps - 1 - i;
    long ramp    = i < fromEnd ? i : fromEnd;   // steps into the nearer ramp
    if (ramp > RAMP_STEPS) ramp = RAMP_STEPS;
    // Linear interpolation: ramp==0 -> PULSE_START_US, ramp==RAMP_STEPS -> PULSE_US.
    int halfPeriod = (int)(PULSE_START_US -
                           ((long)(PULSE_START_US - PULSE_US) * ramp) / RAMP_STEPS);
    stepPulseUs(halfPeriod);
  }
}

// Non-negative modulo, for wrapping positions around the circle.
long wrapMod(long v, long m) { return ((v % m) + m) % m; }

// Rotate from currentStep to absolute pulse `target` (0..STEPS_PER_REV-1) by
// the shorter direction. On an exact tie (half a revolution) we go CLOCKWISE.
void gotoStep(long target) {
  target = wrapMod(target, STEPS_PER_REV);
  long cwSteps = wrapMod(target - currentStep, STEPS_PER_REV);
  if (cwSteps == 0) return;
  long ccwSteps = STEPS_PER_REV - cwSteps;
  if (cwSteps <= ccwSteps) rotate(cwSteps,  CW);    // tie -> clockwise
  else                     rotate(ccwSteps, CCW);
  currentStep = target;
}

// Rotate to `target` in a FORCED direction, however far round that is. Used to
// retrace a move: coming back the way we went keeps the pole from creeping the
// same way turn after turn (which winds up anything routed along it), where
// gotoStep() would happily take the short way and complete a full circle.
void gotoStepVia(long target, int dir) {
  target = wrapMod(target, STEPS_PER_REV);
  long cwSteps = wrapMod(target - currentStep, STEPS_PER_REV);
  long steps = (dir == CW) ? cwSteps : STEPS_PER_REV - cwSteps;
  if (steps == 0 || steps == STEPS_PER_REV) return;
  rotate(steps, dir);
  currentStep = target;
}

// Bin index the pole is nearest to, derived from the absolute step position.
long currentBinOf(long step) {
  return wrapMod((step + STEPS_PER_BIN / 2) / STEPS_PER_BIN, NUM_BINS);
}

// Rotate to bin `target` (0..NUM_BINS-1) — just a step move to its multiple.
void gotoBin(long target) {
  gotoStep(wrapMod(target, NUM_BINS) * STEPS_PER_BIN);
}

// Drive the servo arm to an absolute angle. No-op (returns false) while the
// servo is compiled out.
bool setArmAngle(int deg) {
#if SERVO_ENABLED
  if (arm == nullptr) return false;
  if (deg < 0) deg = 0;
  if (deg > 180) deg = 180;
  // Re-attach for the move, then release again: see SERVO_TRAVEL_MS. The
  // zephyr backend's attach() does NOT always take -- when the timer fails to
  // restart, write() goes nowhere and the arm silently does not move. Verify
  // it, and retry once, or the caller is told a sweep happened that did not.
  if (!arm->attached()) {
    arm->attach(SERVO_PIN, SERVO_MIN_US, SERVO_MAX_US);
    if (!arm->attached()) {
      arm->detach();
      delay(SERVO_RETRY_MS);
      arm->attach(SERVO_PIN, SERVO_MIN_US, SERVO_MAX_US);
    }
  }
  if (!arm->attached()) {
    Monitor.println("servo: ATTACH FAILED, arm did not move");
    return false;
  }
  arm->write(deg);
  currentAngle = deg;
  delay(SERVO_TRAVEL_MS);
  arm->detach();
  return true;
#else
  (void)deg;
  return false;
#endif
}

// Returns false if the arm did not complete its sweep, so the caller can pass
// that up the RPC -- Monitor output does not reach the board's app log, so a
// printed warning would be invisible to anyone driving this remotely.
bool sweepArm() {
#if SERVO_ENABLED
  // Report whether the arm actually swept: a sort that silently skipped the
  // sweep leaves the item sitting on the platform, which looks identical to a
  // successful sort from the outside.
  bool out = setArmAngle(ARM_SWEEP);
  delay(SWEEP_MS);              // dwell out, pushing the item clear
  bool back = setArmAngle(ARM_REST);  // returns and releases; no dwell at rest
  if (!out || !back) Monitor.println("servo: SWEEP INCOMPLETE");
  return out && back;
#else
  return true;   // servo compiled out: nothing to sweep, not a failure
#endif
}

void homePole() {
  if (!HOMING_ENABLED) { currentStep = 0; return; }
  digitalWrite(DIR_PIN, CCW);
  delayMicroseconds(50);
  long moved = 0;
  while (digitalRead(HOME_PIN) == HIGH && moved < HOME_MAX_STEPS) {
    stepPulse();
    moved++;
  }
  currentStep = 0;   // define the stop as step 0 / bin 0
}

// ---- RPC handlers exposed to the Python side ----

// Sort an item into `bin`: rotate to (bin + OFFSET) and sweep. Returns the
// requested bin, or -1 if the bin index is out of range.
int rpcSort(int bin) {
  if (bin < 0 || bin >= NUM_BINS) return -1;
  long stop = ((bin + OFFSET) % NUM_BINS + NUM_BINS) % NUM_BINS;
  // Remember where the platform started so it can come home afterwards: the
  // pole goes out to the bin, the arm sweeps the item off, then the pole
  // returns to its loading position ready for the next item.
  long origin = currentStep;
  // Pick the outbound direction the same way gotoStep() would (shorter way,
  // tie -> clockwise), then come home by the exact reverse of it rather than
  // whichever way is shorter from the far side. Net rotation per sort is zero.
  long target  = wrapMod(stop * STEPS_PER_BIN, STEPS_PER_REV);
  long cwSteps = wrapMod(target - origin, STEPS_PER_REV);
  int  outDir  = (cwSteps <= STEPS_PER_REV - cwSteps) ? CW : CCW;

  gotoStepVia(target, outDir);
  bool swept = sweepArm();
  gotoStepVia(origin, outDir == CW ? CCW : CW);   // retrace, unwinding the trip
  // Come home either way -- leaving the pole parked at the bin on a failed
  // sweep would strand it -- but report the failure, or a sort that left the
  // item sitting on the platform is indistinguishable from a good one.
  if (!swept) return -1;
  Monitor.print("sorted -> bin ");
  Monitor.print(bin);
  Monitor.println(", returned to start");
  return bin;
}

// Rotate straight to `bin` (no offset, no sweep). Returns where we ended up.
int rpcGoto(int bin) {
  if (bin < 0 || bin >= NUM_BINS) return -1;
  gotoBin(bin);
  return (int)currentBinOf(currentStep);
}

// Re-home the pole to the zero stop. Returns the current bin (0).
int rpcHome() {
  homePole();
  return (int)currentBinOf(currentStep);
}

// ---- Manual/bench control (used by the host-side motor_cli.py) ----

// Move the stepper to absolute pulse `step`, 0..STEPS_PER_REV inclusive
// (STEPS_PER_REV wraps to 0 — a full turn is the same place). Shorter
// direction, tie goes clockwise. Returns the position we landed on, or -1 if
// the request is out of range.
int rpcStep(int step) {
  if (step < 0 || step > STEPS_PER_REV) return -1;
  gotoStep(step);
  Monitor.print("step -> ");
  Monitor.println(currentStep);
  return (int)currentStep;
}

// Move the stepper `delta` pulses relative to where it is now (negative =
// counter-clockwise). Returns the new absolute position.
int rpcNudge(int delta) {
  if (delta == 0) return (int)currentStep;
  rotate(delta > 0 ? delta : -delta, delta > 0 ? CW : CCW);
  currentStep = wrapMod(currentStep + delta, STEPS_PER_REV);
  Monitor.print("nudge -> ");
  Monitor.println(currentStep);
  return (int)currentStep;
}

// Report the stepper's absolute position without moving anything. The argument
// is ignored (the Bridge RPC signature needs one).
int rpcPos(int) {
  return (int)currentStep;
}

// Declare wherever the pole is RIGHT NOW to be step 0 / bin 0, without moving.
// This is the manual alternative to a homing switch: de-energize with
// "release", hand-turn the pole to physical zero, then call this. Also used
// after a reflash or App restart, which reset the MCU (and so currentStep)
// while leaving the pole physically where it was.
int rpcZero(int) {
  currentStep = 0;
  Monitor.println("zeroed at current position");
  return 0;
}

// Energize (on != 0) or release (on == 0) the stepper. Released, the driver
// stops holding and the pole can be turned by hand without fighting holding
// torque or skipping steps. Any move re-energizes automatically.
//
// Position is NOT tracked while released — the whole point is that you move it
// by hand — so currentStep is meaningless until you call "zero".
int rpcRelease(int on) {
  digitalWrite(ENA_PIN, on ? ENA_ACTIVE : !ENA_ACTIVE);
  motorHeld = on != 0;
  Monitor.println(motorHeld ? "stepper energized" : "stepper released (free to turn by hand)");
  return motorHeld ? 1 : 0;
}

// Move the servo arm to absolute angle `deg` (0..180). Returns the angle, or
// -1 if out of range / the servo is compiled out (SERVO_ENABLED == 0).
int rpcServo(int deg) {
  if (deg < 0 || deg > 180) return -1;
  if (!setArmAngle(deg)) return -1;
  Monitor.print("servo -> ");
  Monitor.println(deg);
  return currentAngle;
}

void setup() {
  pinMode(PUL_PIN, OUTPUT);
  pinMode(DIR_PIN, OUTPUT);
  pinMode(ENA_PIN, OUTPUT);
  digitalWrite(ENA_PIN, ENA_ACTIVE);
  // Prime DIR by driving it both ways before any move. The TB6600's DIR input
  // is opto-isolated, and the very FIRST level change after boot was being
  // lost: the first move went out correctly, the first move that should have
  // come back went the same way instead, and every direction change after that
  // was fine. Toggling here means a real move is never the first transition.
  digitalWrite(DIR_PIN, CCW);
  delay(20);
  digitalWrite(DIR_PIN, CW);
  delay(20);
  // Toggling the level alone did not clear it, so exercise the whole path --
  // direction change plus real pulses -- a few steps each way. Net movement is
  // zero, and whatever was swallowing the first direction change is spent here
  // instead of on the first real return leg.
  rotate(8, CCW);
  delay(100);
  rotate(8, CW);
  delay(100);
  if (HOMING_ENABLED) pinMode(HOME_PIN, INPUT_PULLUP);
#if SERVO_ENABLED
  // Built here (not statically) so the zephyr counter device is ready — see
  // the note at the Servo declaration. attached() tells us the timer handler
  // actually took the servo, which is reported by the "servo" RPC.
  arm = new Servo();
  arm->attach(SERVO_PIN, SERVO_MIN_US, SERVO_MAX_US);
  servoTimerOk = arm->attached();
  // Park at rest, then release. Before this the sketch attached at boot and
  // never detached, so a blocked or over-driven horn sat stalled at full
  // current from power-on onward -- the overheat we chased.
  setArmAngle(ARM_REST);
#endif

  Bridge.begin();
  Monitor.begin(115200);

  homePole();

  Bridge.provide("sort", rpcSort);
  Bridge.provide("goto", rpcGoto);
  Bridge.provide("home", rpcHome);
  Bridge.provide("step", rpcStep);
  Bridge.provide("nudge", rpcNudge);
  Bridge.provide("pos", rpcPos);
  Bridge.provide("servo", rpcServo);
  Bridge.provide("zero", rpcZero);
  Bridge.provide("release", rpcRelease);

#if SERVO_ENABLED
  Monitor.print("servo: pin ");
  Monitor.print(SERVO_PIN);
  Monitor.println(servoTimerOk ? " attached (timer ok)" : " ATTACH FAILED (timer not started)");
#else
  Monitor.println("servo: compiled out");
#endif

  Monitor.println("trashbin-motor ready");
}

void loop() {
  // RPC requests are serviced by the RouterBridge background thread, so the
  // main loop has nothing to do.
  delay(10);
}
