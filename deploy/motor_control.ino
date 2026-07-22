/*
 * motor_control.ino — MCU side of the ECE180 trashbin sorter (Arduino Uno Q).
 *
 * Runs on the microcontroller. The Linux side classifies an item and sends a
 * line over the internal serial link:
 *
 *     SORT <bin>\n     bin = 0..3   (see deploy/bin_map.py)
 *
 * On each command this sketch:
 *   1. rotates the pole to the position NEXT TO the target bin (OFFSET), so
 *   2. the servo arm can sweep the item across into the target bin,
 *   3. returns the arm and replies "OK <bin>\n" so Linux knows it may proceed.
 *
 * On boot it HOMES the pole to the physical zero stop, then treats that as
 * bin 0. Homing is what keeps an open-loop stepper in sync after a power blip.
 *
 * --- WIRING (TB6600-class driver, common-anode / active-LOW) ---
 *   Tie PUL+, DIR+, ENA+ all to +5V (buck output).
 *   Drive PUL-, DIR-, ENA- from PIN_PUL/PIN_DIR/PIN_EN below (LOW = asserted).
 *   Driver VCC/GND = 12V rail;  A+/A-/B+/B- = the two NEMA-17 coils.
 *   Servo signal = PIN_SERVO, servo power = 5V buck (NOT the board's 3V3).
 *   Zero-stop switch across PIN_HOME -> GND (uses internal pull-up).
 *   ALL grounds common: 12V PSU, both bucks, and the MCU.
 *
 * Set the driver DIP switches to 8 microsteps => 1600 pulse/rev
 * (SW1 OFF, SW2 ON, SW3 OFF), which makes STEPS_PER_BIN below exact.
 */

#include <Servo.h>

// ---- Pins (adjust to your actual wiring on the Uno Q MCU header) ----
const int PIN_PUL   = 2;   // -> PUL- on the driver
const int PIN_DIR   = 3;   // -> DIR-
const int PIN_EN    = 4;   // -> ENA-
const int PIN_SERVO = 9;   // -> servo signal
const int PIN_HOME  = 5;   // -> zero-stop limit switch (to GND)

// ---- Motion constants ----
// 1600 pulses/rev (1/8 microstep) / 4 bins = 400 pulses per 90 degree bin.
const long STEPS_PER_BIN = 400;
// The pole stops one bin away from the target; the arm sweeps it the rest of
// the way. Flip between +1 and -1 after a bench test to match the sweep side.
const int  OFFSET        = -1;
// Pulse timing: HIGH then LOW each PULSE_US. Larger = slower but more torque /
// fewer missed steps. 800us is gentle; drop toward 300us once it runs clean.
const int  PULSE_US      = 800;

// Signals are active-LOW (common-anode wiring): assert = LOW, release = HIGH.
const int  ASSERT   = LOW;
const int  RELEASE  = HIGH;

// ---- Servo sweep angles (degrees) — tune to your arm geometry ----
const int  ARM_REST  = 20;    // parked, clear of the platform
const int  ARM_SWEEP = 160;   // full push across the platform
const int  SWEEP_MS  = 500;   // dwell at each end

// ---- Homing ----
const long HOME_MAX_STEPS = 1600 * 2;  // give up after 2 revs (switch fault)
const int  HOME_DIR       = HIGH;      // direction that drives toward the stop

Servo arm;
long currentBin = 0;   // where the pole is now (0..3), valid after homing

void stepPulse() {
  digitalWrite(PIN_PUL, ASSERT);
  delayMicroseconds(PULSE_US);
  digitalWrite(PIN_PUL, RELEASE);
  delayMicroseconds(PULSE_US);
}

// Rotate `steps` pulses in `dir` (HIGH/LOW on DIR-).
void rotate(long steps, int dir) {
  digitalWrite(PIN_DIR, dir);
  delayMicroseconds(50);  // DIR setup time before first pulse
  for (long i = 0; i < steps; i++) stepPulse();
}

void homePole() {
  Serial.println("HOME start");
  digitalWrite(PIN_DIR, HOME_DIR);
  delayMicroseconds(50);
  long moved = 0;
  // Drive toward the stop until the switch closes (reads LOW with pull-up).
  while (digitalRead(PIN_HOME) == HIGH && moved < HOME_MAX_STEPS) {
    stepPulse();
    moved++;
  }
  if (moved >= HOME_MAX_STEPS) {
    Serial.println("HOME fail: switch not found — check PIN_HOME wiring");
  } else {
    Serial.println("HOME ok");
  }
  currentBin = 0;  // define the stop as bin 0
}

void sweepArm() {
  arm.write(ARM_SWEEP);
  delay(SWEEP_MS);
  arm.write(ARM_REST);
  delay(SWEEP_MS);
}

// Rotate from currentBin to `stopBin` by the shortest direction (bins wrap 0..3).
void gotoBin(long stopBin) {
  long diff = ((stopBin - currentBin) % 4 + 4) % 4;  // 0..3 forward steps
  if (diff == 0) return;
  int dir;
  long bins;
  if (diff <= 2) { dir = HIGH; bins = diff; }        // forward
  else           { dir = LOW;  bins = 4 - diff; }    // shorter to go backward
  rotate(bins * STEPS_PER_BIN, dir);
  currentBin = stopBin;
}

void handleSort(long targetBin) {
  if (targetBin < 0 || targetBin > 3) {
    Serial.println("ERR bad bin");
    return;
  }
  long stopBin = ((targetBin + OFFSET) % 4 + 4) % 4;
  gotoBin(stopBin);
  sweepArm();
  Serial.print("OK ");
  Serial.println(targetBin);
}

void setup() {
  pinMode(PIN_PUL, OUTPUT);
  pinMode(PIN_DIR, OUTPUT);
  pinMode(PIN_EN, OUTPUT);
  pinMode(PIN_HOME, INPUT_PULLUP);
  digitalWrite(PIN_PUL, RELEASE);
  digitalWrite(PIN_EN, ASSERT);   // enable the driver (active-LOW)

  arm.attach(PIN_SERVO);
  arm.write(ARM_REST);

  Serial.begin(115200);
  delay(200);
  homePole();
  Serial.println("READY");
}

void loop() {
  static String buf;
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      buf.trim();
      if (buf.startsWith("SORT")) {
        handleSort(buf.substring(4).toInt());
      } else if (buf == "HOME") {
        homePole();
        Serial.println("OK home");
      } else if (buf.length()) {
        Serial.println("ERR unknown cmd");
      }
      buf = "";
    } else {
      buf += c;
    }
  }
}
