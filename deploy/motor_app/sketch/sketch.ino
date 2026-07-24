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
 * Stepper wiring (from the known-good motor_control.ino bench setup — the
 * driver is a TB6600-class, common-anode / active-LOW board):
 *   PUL- = pin 2, DIR- = pin 3, ENA- = pin 4   (PUL+/DIR+/ENA+ tied to +5V).
 *   Driver DIP set to 1/8 microstep => 1600 pulses/rev.
 * Servo arm + homing switch are left OFF here because their wiring isn't
 * confirmed yet — flip SERVO_ENABLED / HOMING_ENABLED once they're wired.
 */

#include <Arduino_RouterBridge.h>
// NOTE: the servo arm is not wired yet, so the Servo library is intentionally
// left out to keep the App Lab build self-contained. When you wire the servo,
// add `#include <Servo.h>`, restore the Servo object, and set SERVO_ENABLED.

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
const bool SERVO_ENABLED  = false;
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

long currentBin = 0;   // where the pole is now (0..NUM_BINS-1)

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

// Rotate from currentBin to `target` by the shorter direction.
// Bins wrap 0..NUM_BINS-1. On an exact tie (opposite bin) we go CLOCKWISE.
void gotoBin(long target) {
  long cwSteps  = ((target - currentBin) % NUM_BINS + NUM_BINS) % NUM_BINS; // 0..N-1 going CW
  if (cwSteps == 0) return;
  long ccwSteps = NUM_BINS - cwSteps;
  if (cwSteps <= ccwSteps) rotate(cwSteps  * STEPS_PER_BIN, CW);   // tie -> clockwise
  else                     rotate(ccwSteps * STEPS_PER_BIN, CCW);
  currentBin = target;
}

void sweepArm() {
  if (!SERVO_ENABLED) return;
  // Servo not wired yet — see the note by the includes to re-enable.
}

void homePole() {
  if (!HOMING_ENABLED) { currentBin = 0; return; }
  digitalWrite(DIR_PIN, CCW);
  delayMicroseconds(50);
  long moved = 0;
  while (digitalRead(HOME_PIN) == HIGH && moved < HOME_MAX_STEPS) {
    stepPulse();
    moved++;
  }
  currentBin = 0;   // define the stop as bin 0
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
  return (int)currentBin;
}

// Re-home the pole to the zero stop. Returns the current bin (0).
int rpcHome() {
  homePole();
  return (int)currentBin;
}

void setup() {
  pinMode(PUL_PIN, OUTPUT);
  pinMode(DIR_PIN, OUTPUT);
  pinMode(ENA_PIN, OUTPUT);
  digitalWrite(ENA_PIN, ENA_ACTIVE);
  if (HOMING_ENABLED) pinMode(HOME_PIN, INPUT_PULLUP);
  // (servo attach goes here once SERVO_ENABLED and the Servo lib are restored)

  Bridge.begin();
  Monitor.begin(115200);

  homePole();

  Bridge.provide("sort", rpcSort);
  Bridge.provide("goto", rpcGoto);
  Bridge.provide("home", rpcHome);

  Monitor.println("trashbin-motor ready");
}

void loop() {
  // RPC requests are serviced by the RouterBridge background thread, so the
  // main loop has nothing to do.
  delay(10);
}
