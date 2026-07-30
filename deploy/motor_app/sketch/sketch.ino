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
 * Stepper wiring (from the known-good motor_control.ino bench setup — the
 * driver is a TB6600-class, common-anode / active-LOW board):
 *   PUL- = pin 2, DIR- = pin 3, ENA- = pin 4   (PUL+/DIR+/ENA+ tied to +5V).
 *   Driver DIP set to 1/8 microstep => 1600 pulses/rev.
 * Servo arm + homing switch are left OFF here because their wiring isn't
 * confirmed yet — flip SERVO_ENABLED / HOMING_ENABLED once they're wired.
 */

#include <Arduino_RouterBridge.h>

// The servo arm is not wired yet, so the Servo library is compiled out to keep
// the App Lab build self-contained. Flip SERVO_ENABLED to 1 once it is wired —
// that is the only change needed; the include and the Servo object come with it.
#define SERVO_ENABLED 0

#if SERVO_ENABLED
#include <Servo.h>
static Servo arm;
#endif

// ---- Stepper pins (match the physical wiring: PUL/DIR/ENA = 2/3/4) ----
const int PUL_PIN = 2;
const int DIR_PIN = 3;
const int ENA_PIN = 4;
const int ENA_ACTIVE = LOW;   // common-anode: LOW = driver enabled / holding torque

// Signals are active-LOW (common-anode wiring): a pulse asserts LOW then
// releases HIGH, matching the known-good motor_control.ino.
const int PUL_ASSERT  = LOW;
const int PUL_RELEASE = HIGH;

// ---- Optional peripherals (enable once physically wired) ----
// SERVO_ENABLED is the #define above (it gates the Servo include too).
const int  SERVO_PIN      = 9;   // clear of the stepper pins 2/3/4
const bool HOMING_ENABLED = false;
const int  HOME_PIN       = 5;   // limit switch to GND, INPUT_PULLUP

// ---- Geometry ----
// The driver DIP is set to 1/8 microstep => 1600 pulses per full revolution
// (see motor_control.ino). Four bins around the circle => 90 deg / 400 pulses
// each. If you change the DIP microstep setting, rescale STEPS_PER_REV.
const int  NUM_BINS       = 4;
const long STEPS_PER_REV  = 1600;
const long STEPS_PER_BIN  = STEPS_PER_REV / NUM_BINS;   // 400 at 1/8 microstep
const int  PULSE_US       = 800;                         // matches bench timing

// Direction levels on DIR_PIN. If the pole turns the WRONG way physically,
// just swap these two values (one-line fix, no other logic changes).
const int  CW  = HIGH;
const int  CCW = LOW;

// The pole can stop OFFSET bins short of the target so a servo arm sweeps the
// item the rest of the way. With the servo disabled we stop AT the target.
const int  OFFSET = 0;

// ---- Servo sweep angles (only used when SERVO_ENABLED) ----
const int  ARM_REST   = 20;
const int  ARM_SWEEP  = 160;
const int  SWEEP_MS   = 500;

// ---- Homing ----
const long HOME_MAX_STEPS = STEPS_PER_REV * 2;  // give up after 2 revs

// Absolute stepper position in pulses, 0..STEPS_PER_REV-1, wrapping. This is
// the single source of truth: `currentBin` is derived from it.
long currentStep = 0;

// Last angle commanded to the servo arm (meaningless while SERVO_ENABLED is 0).
int currentAngle = ARM_REST;

void stepPulse() {
  digitalWrite(PUL_PIN, PUL_ASSERT);
  delayMicroseconds(PULSE_US);
  digitalWrite(PUL_PIN, PUL_RELEASE);
  delayMicroseconds(PULSE_US);
}

// Rotate `steps` pulses in direction `dir` (CW/CCW level on DIR_PIN).
void rotate(long steps, int dir) {
  digitalWrite(DIR_PIN, dir);
  delayMicroseconds(50);            // DIR setup time before first pulse
  for (long i = 0; i < steps; i++) stepPulse();
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
  if (deg < 0) deg = 0;
  if (deg > 180) deg = 180;
  arm.write(deg);
  currentAngle = deg;
  return true;
#else
  (void)deg;
  return false;
#endif
}

void sweepArm() {
#if SERVO_ENABLED
  setArmAngle(ARM_SWEEP);
  delay(SWEEP_MS);
  setArmAngle(ARM_REST);
  delay(SWEEP_MS);
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
  gotoBin(stop);
  sweepArm();
  Monitor.print("sorted -> bin ");
  Monitor.println(bin);
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
  if (HOMING_ENABLED) pinMode(HOME_PIN, INPUT_PULLUP);
#if SERVO_ENABLED
  arm.attach(SERVO_PIN);
  arm.write(ARM_REST);
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

  Monitor.println("trashbin-motor ready");
}

void loop() {
  // RPC requests are serviced by the RouterBridge background thread, so the
  // main loop has nothing to do.
  delay(10);
}
