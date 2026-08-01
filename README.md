# UCSD ECE 180 Team 12 Final Project: Bin-ary Sorter

*Waste sorting with computer vision, on the Arduino UNO Q.*

## Team Members

| Name | Major | Year |
|------|-------|------|
| Colin Hua | ECE — Electrical Engineering | 2027 |
| Duy Nguyen | ECE — Computer Engineering | 2027 |
| Aram Zarate Ubario | ECE — Computer Engineering | 2027 |

**Course:** ECE 180 — Prof. Silberman, UC San Diego, 2026

## Project Overview

Bin-ary Sorter is a self-contained smart trashbin that classifies an item the
moment it is dropped in and rotates it into the correct one of four bins — no
phone, no cloud call, no user input. A USB camera on the UNO Q captures the
frame, a fine-tuned **MobileNetV3-Large** runs **entirely on-device** over the
30-class Kaggle waste dataset, and the resulting label is collapsed to a bin
index that a NEMA-17 stepper drives to. Inference and the camera loop live on
the UNO Q's Qualcomm Dragonwing Linux MPU; the companion STM32 MCU owns the
real-time motor control and is reached over the Arduino **RouterBridge** RPC.
The board is plug-and-play: on power-up it auto-starts both the classifier and
the motor controller with no manual steps.

When the model is *not* confident, the bin does not guess — it pushes the frame
to a web dashboard and asks a human, and that correction feeds the next
retrain.

## Goals

### Original Objectives
- Train a transfer-learning waste classifier accurate enough to run quantized on an edge MPU
- Run inference fully on-device on the Arduino UNO Q (no cloud inference)
- Drive a rotating-pole sorting mechanism from the classification result
- Build a web dashboard showing live status and classification confidence

### Achieved Goals
- MobileNetV3-Large fine-tuned to **89.1% test accuracy / 0.890 macro-F1** across 30 classes
- Exported to ONNX + three TFLite variants with **measured** per-variant accuracy, size, and latency
- Full on-device pipeline: camera → classify → bin index → stepper move, autostarted at boot
- **2-category sorting** through the single-axis rotating-pole chassis — later extended to 4 (see Stretch Goals)
- Web dashboard with correct/wrong human feedback (reinforced-learning loop)
- Second motor (servo arm) added to the mechanism late in the schedule

### Stretch Goals
- **4-category sorting** through the 2-axis double-motored rotating system — *Done*
- Federated learning across bins for a network effect — *Done*
- Higher-throughput continuous-feed sorting rather than one item at a time — *in progress*

## System Hardware

| Component | Purpose |
|-----------|---------|
| Arduino UNO Q (Qualcomm Dragonwing QRB2210) | Primary compute — quad Cortex-A53 + Adreno 702, Debian Linux |
| STM32U585 MCU (Zephyr, on-board) | Real-time stepper/servo control, reached over RouterBridge RPC |
| USB webcam | Item capture at the drop chute |
| NEMA-17 stepper + driver | Rotates the sorting pole to the target bin |
| Servo arm | Second-stage actuation (added late; `SERVO_ENABLED` gate) |

## Model & Dataset

**Dataset:** [Recyclable and Household Waste Classification](https://www.kaggle.com/datasets/alistairking/recyclable-and-household-waste-classification)
— 30 classes, ~15k images, each class split into `default` (studio) and
`real_world` (cluttered) subsets.

The split is stratified 70/15/15 by *(class, subset)* so `real_world` images are
proportionally represented in val and test — reported metrics reflect what the
bin's camera actually sees, not a studio best case.

| Metric | Value |
|--------|-------|
| Test accuracy (fp32) | **89.08%** |
| Macro-F1 | **0.890** |
| Parameters | 4.24 M |
| `default` vs `real_world` accuracy | 89.2% vs 88.9% (no meaningful domain gap) |

Training is a two-stage fine-tune (head-only warmup → full fine-tune) with
AdamW + cosine schedule, label smoothing 0.1, AMP, EMA weights, MixUp/CutMix,
class-balanced sampling, and camera-realistic augmentation. All of it is
training-time only — the exported model is a single plain network.

### Quantization results

Every exported variant is re-scored on the real test set, so the deployment
choice is data-driven rather than assumed:

| Variant | Test acc | Size | Recommended threshold |
|---------|----------|------|-----------------------|
| fp32 | 89.08% | 17.1 MB | 0.75 |
| dynamic-int8 | 87.37% | 4.63 MB | 0.55 |
| **static-int8 (shipped)** | **86.36%** | **4.87 MB** | **0.80** |

**static-int8 is the variant deployed to the bin.** It gives up 2.7 points of
accuracy against fp32 but is 3.5× smaller and the fastest of the three on the
board, and its quantization ranges are calibrated on `real_world` training
images so they match the live camera domain. The confidence gate absorbs the
accuracy loss: at its measured threshold of 0.80 the auto-accepted predictions
are still 95.0% correct, and the rest go to a human.

### Measured on-device latency

Benchmarked on the UNO Q itself (aarch64, quad Cortex-A53, TFLite XNNPACK CPU
delegate, 256×256 int8 input, 50 timed invocations after warmup):

| Threads | Median | Mean | Min – Max |
|---------|--------|------|-----------|
| 1 | 79.8 ms | 80.1 ms | 79.2 – 83.1 ms |
| **4 (shipped)** | **26.5 ms** | **27.3 ms** | **26.0 – 35.1 ms** |

`infer_uno_q.py` runs the interpreter with `num_threads=4`, so **~27 ms per
frame** is the real per-item inference cost — far below the mechanical settling
time of the stepper, which means classification is never the bottleneck in a
drop cycle. Model inference only; camera capture and preprocessing are on top.
The GPU delegate was never needed.

## Mechanical Design

Four bins arranged in a round casing around a **rotating pole**. The pole
carries the item to the correct bin by the shorter direction (clockwise or
counter-clockwise; an exact tie goes clockwise), and the move blocks until it
completes so the software gets a real motion acknowledgment rather than a
fire-and-forget.

The design went through one full revision — the original chassis proved flawed
in seam testing and was reprinted (see the Gantt chart below).

<!-- TODO: add photos — original flawed print, revised chassis, assembled bin. -->
<!-- TODO: 1-2 sentences on material, print settings, and bin dimensions. -->

## Accomplishments

### On-device classification
The Linux side replicates the notebook's `eval_tf` preprocessing exactly —
resize shorter side to ~293, center-crop 256, ImageNet mean/std — because
preprocessing mismatch is the single most common cause of "works in Colab,
fails on device." The model, labels, and thresholds are all read from the
export directory, so swapping in a retrained model is a file copy.

### Two-processor split
On the UNO Q the STM32 is not a serial tty — it is reachable only over the
Arduino **RouterBridge**, and that bridge is exposed only inside an Arduino App
Lab app. So motor control lives in its own App (`deploy/motor_app/`, installed
as `~/ArduinoApps/nema17`) that publishes an HTTP command port on `:8071`; the
classifier POSTs a target bin to it and the App forwards a `Bridge.call("sort", bin)`
to the MCU.

### Plug-and-play autostart
Installing a systemd service needs a sudo password the board doesn't grant, so
autostart is two `@reboot` cron entries in the `arduino` user's crontab:
`start_motor_app.sh` waits for docker and the App Lab daemon then starts the
motor App, and `run_trashbin.sh` runs the camera loop in a self-restarting
wrapper. If the motor App is down, sort calls are best-effort and never crash
the classifier.

### Confidence-gated clarification loop
Raw softmax from a label-smoothed model is not a calibrated probability, so the
notebook fits **temperature scaling** on the val set (T = 0.55) and sweeps
thresholds, then re-measures the recommended threshold per TFLite variant. The
selection rule: the lowest threshold whose auto-accepted predictions are ≥95%
correct — maximizing coverage at that accuracy floor. Below threshold, the bin
posts the frame and its top-k to the webapp, a human picks the right label, and
the correction is pushed into the shared training set for the next retrain.

### Web dashboard
Recent classifications with confidence bars, a review queue for low-confidence
frames with correct/wrong feedback buttons, and system status tiles (web server,
board, model).

**Live at [ece180.duythe.dev](https://ece180.duythe.dev).**

## Challenges & Solutions

| System | Challenge | Solution |
|--------|-----------|----------|
| Chassis | Original design failed under load / seam testing | Full chassis revision and reprint before integration |
| MCU access | STM32 is not exposed as a serial device on the UNO Q | Moved motor control into an Arduino App Lab app talking over RouterBridge RPC |
| Autostart | systemd install requires a sudo password | Two `@reboot` crontab entries with self-restarting wrappers |
| Python env | Board Python is externally managed, no venv available | `pip install --user --break-system-packages`, `PYTHONPATH` pointed at `~/.local` |
| Quantization | int8 shifts logits, invalidating a single fixed threshold | Re-measured accuracy *and* threshold per exported variant |
| Confidence | Label smoothing makes softmax overconfident | Temperature scaling fitted on val, threshold chosen by measured accept-accuracy |
| <!-- TODO --> | <!-- TODO: motor wiring / stepper calibration issue --> | <!-- TODO --> |

## Lessons Learned

- Measure, don't assume — quantized accuracy, latency, and confidence thresholds all had to be re-measured per export variant rather than inherited from the fp32 model.
- Preprocessing parity between training and deployment is worth more than any extra training trick.
- Design the mechanism to be revised: the first chassis was wrong, and the schedule survived only because the reprint was planned into the timeline.
- Platform research first — the RouterBridge/App Lab constraint on the UNO Q reshaped the whole software architecture and would have been much cheaper to discover early.
- <!-- TODO: one team-process lesson, in your own words. -->

## Next Steps

- **Federated learning:** aggregate corrections across deployed bins so every bin improves from every other bin's mistakes
- **Higher-resolution / continuous feed:** sort a stream of items rather than one drop at a time
- **Recover the int8 accuracy gap:** quantization-aware training, or fp16 on the Adreno 702 GPU delegate — at 27 ms per frame there is plenty of latency headroom to spend on a more accurate variant

## Project Reconstruction

### Hardware Requirements
- Arduino UNO Q
- USB webcam
- NEMA-17 stepper + stepper driver (1/8 microstep)
- Servo (optional second stage)
- 3D-printed chassis — <!-- TODO: link STLs / CAD -->

### Training

1. Set the Colab runtime to **T4 GPU**.
2. Add Colab Secrets: `GITHUB_TOKEN`, `KAGGLE_USERNAME`, `KAGGLE_KEY`.
3. Run `ECE180_Complete_Notebook.ipynb` top to bottom — **run the export cell (Cell 13) last**, since `litert-torch` pins torch versions and can disturb the training environment.

The dataset downloads once via kagglehub into Google Drive; checkpoints and
results persist there, and training is multi-session safe — if Colab
disconnects, re-running resumes from the last epoch including EMA state.

### Deployment

1. Connect to the board over the standard UNO Q SSH login (`ssh arduino@uno-q.local`,
   or the board's IP on the same network) and copy `deploy/` plus the exported
   `.tflite` and `labels.txt` across with `scp`.
2. Install host deps to the user site: `pip install --user --break-system-packages ai-edge-litert opencv-python-headless numpy pillow requests`.
3. Install the motor App to `~/ArduinoApps/nema17` and `arduino-app-cli app start` it.
4. Verify the stepper pins in `deploy/motor_app/sketch/sketch.ino` — **PUL=2 / DIR=3 / ENA=4**, common-anode active-LOW, 1600 steps/rev → 400 steps/bin.
5. Add the two `@reboot` crontab entries (`start_motor_app.sh`, `run_trashbin.sh`).
6. Set `MOTOR_ENABLED=1`, `MOTOR_URL=http://127.0.0.1:8071`, plus `TEMPERATURE` and `CONFIDENCE_THRESHOLD` for the shipped variant.

Full technical detail, file-by-file, is in [`docs/TECHNICAL.md`](docs/TECHNICAL.md)
and [`deploy/MOTOR_CONTROL_MAP.md`](deploy/MOTOR_CONTROL_MAP.md).

### Troubleshooting Reference

Common issues: sort RPCs acknowledge (`landed=N`) but the pole never turns —
the sketch's pin constants don't match the driver wiring; the pole turns the
wrong way — swap the `CW`/`CCW` constants; the pole over- or under-shoots a bin
— rescale `STEPS_PER_REV` to the driver's DIP microstep setting; accuracy is
far worse on device than in Colab — preprocessing mismatch; motor calls
silently no-op — the App Lab app isn't running. After any sketch change:
`arduino-app-cli app restart ~/ArduinoApps/nema17`.

## Timeline

<!-- TODO: embed the Gantt chart image from the progress report. -->

| Task | Window (July 2026) |
|------|--------------------|
| Train Model | 12 – 18 |
| Design and 3D Print Chassis | 12 – 13 |
| Develop Webapp | 12 – 13 |
| Testing Mechanical Design Control | 13 – 17 |
| Improve Webapp | 18 – 23 |
| Testing Camera Model with Chassis | 18 – 20 |
| Chassis Revision | 20 – 22 |
| Seam Tests | 22 – 26 |
| Nice-to-have Implementation | 28 – Aug 2 |

## Repository Structure

```
.
├── ECE180_Complete_Notebook.ipynb   # Full pipeline: download → train → eval → export
├── deploy/                          # UNO Q runtime (camera loop, inference, motor App)
├── webapp/                          # dashboard + clarification service
├── exports/                         # ONNX model, labels.txt, quantization + calibration reports
├── results/                         # test_results.json, domain_shift.json, confusion_matrix.png
└── docs/TECHNICAL.md                # deep technical README
```
