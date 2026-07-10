# ECE180 Smart Trashbin — Recyclable & Household Waste Classification

Transfer-learning image classifier for a smart trashbin built on the **Arduino UNO Q**.
Inference runs on the UNO Q's Qualcomm Dragonwing Linux MPU; an RTOS on the
companion STM32 MCU handles camera capture and real-time bin control, handing
frames to the Linux side for classification.

**Course:** ECE 180, UC San Diego
**Dataset:** [Recyclable and Household Waste Classification](https://www.kaggle.com/datasets/alistairking/recyclable-and-household-waste-classification) (30 classes, ~15k images; each class has `default` studio and `real_world` cluttered subsets)

---

## Target Hardware — Arduino UNO Q

| Component | Spec |
|-----------|------|
| SoC | **Qualcomm Dragonwing QRB2210** |
| Inference compute | Quad-core **Cortex-A53** CPU + **Adreno 702** GPU |
| OS | Debian Linux (on the MPU side) |
| Real-time control | STM32 MCU running an RTOS (camera + actuation) |

The model runs on the Linux side. Two viable TFLite runtimes on this SoC:

- **int8 on CPU via XNNPACK** — fastest, smallest, minor accuracy trade-off
- **fp16 / fp32 on the Adreno GPU delegate** — near-zero accuracy loss

The notebook exports both int8 and float variants and **measures each one's real
accuracy**, so the deployment choice is data-driven.

---

## Approach

- **Model:** **MobileNetV3-Small** (deployment target), ImageNet-pretrained, fine-tuned at **256×256** with a fresh 30-way head. EfficientNet-B0 is available as an optional accuracy reference but is **off by default** to save compute.
- **Split:** stratified 70/15/15 by (class, subset) so `real_world` images are proportionally represented in val/test — reported metrics reflect what the trashbin camera actually sees.
- **Training:** two-stage fine-tune (head-only warmup → full fine-tune), AdamW + cosine schedule, label smoothing 0.1, mixed-precision (AMP), early stopping on val accuracy.
- **Accuracy techniques** (all training-time only — the exported model is a single plain network):
  - **EMA** of weights — the averaged model is checkpointed and deployed
  - **MixUp + CutMix** regularization
  - **Class-balanced sampling** across the 30 classes (helps macro-F1)
- **Augmentation:** camera-realistic — exposure/color jitter, Gaussian blur, random resized crop, random erasing — to close the studio→live-camera gap.
- **Evaluation:** test accuracy, macro-F1, 30×30 confusion matrix, and a **domain-shift check** reporting `default` vs. `real_world` accuracy separately.
- **Export:** PyTorch → ONNX + three TFLite variants (fp32, dynamic-int8, static/per-channel int8), each with **measured on-test accuracy vs. the fp32 baseline**, plus `labels.txt` and a reference `classify_frame()` entry point for the RTOS→Linux handoff.

---

## Training Setup

Designed for **Google Colab** on a **T4 GPU runtime** (most compute-unit-efficient
for a model this size — a full run is roughly **2–3 compute units**).

| Setting | Value |
|---------|-------|
| GPU | T4 (batch 64, 2 workers) |
| Resolution | 256×256 |
| Schedule | Stage 1: 3 epochs head-only · Stage 2: up to 22 epochs full fine-tune |
| Early stop | patience 5 (typically ends stage 2 around epoch 12–18) |
| Optimizer | AdamW, cosine LR, weight decay 1e-4, label smoothing 0.1 |

Cell 9 prints a **live compute-unit meter** (auto-detects the GPU and its Colab
billing rate) so you can watch cost accrue per epoch.

---

## Running

1. Set the runtime to **T4 GPU** (Runtime → Change runtime type).
2. Add Colab Secrets (🔑 sidebar), each with notebook access: `GITHUB_TOKEN`, `KAGGLE_USERNAME`, `KAGGLE_KEY`.
3. Run `ECE180_Complete_Notebook.ipynb` top to bottom. **Run the export cell (Cell 13) last** — `ai-edge-torch` pins torch versions and its install can disturb the training environment.

The dataset downloads once via kagglehub into Google Drive
(`MyDrive/ECE180_project/`); checkpoints and results persist there too.
Training is **multi-session safe** — if Colab disconnects, re-run the notebook
and it resumes from the last epoch (including EMA state), skipping completed models.

## Repo Structure

```
.
├── ECE180_Complete_Notebook.ipynb   # Full pipeline: download → train → eval → export
├── deploy/                          # UNO Q runtime: inference, confidence threshold,
│                                    #   webapp clarification client, EI upload reference
├── results/                         # test_results.json, domain_shift.json, confusion_matrix.png
└── README.md
```

Drive layout (created by the notebook):

```
MyDrive/ECE180_project/
├── WasteDataset/      # 30 class dirs × {default, real_world}
├── checkpoints/       # *_best.pt, *_resume.pt, progress.json
├── results/           # metrics JSON + plots
└── export/            # ONNX / TFLite models + labels.txt + quantization_report.json
```

## Deployment Notes (UNO Q)

- The Linux-side inference code **must replicate the notebook's `eval_tf`
  preprocessing exactly**: resize shorter side to ~293 → center-crop 256 →
  ImageNet mean/std normalization. Preprocessing mismatch is the most common
  cause of "works in Colab, fails on device."
- Prefer the variant chosen from `export/quantization_report.json`: **dynamic-int8**
  is usually within a fraction of a percent of fp32 and runs on XNNPACK CPU;
  **static-int8** is fastest; fall back to **fp32 on the Adreno GPU delegate** if
  int8 accuracy is unacceptable.
- The static-int8 model is calibrated on `real_world` training images so its
  quantization ranges match the live camera domain.
- MobileNetV3-Small (~2.5M params) runs comfortably on the quad Cortex-A53; the
  export cell prints a CPU latency proxy (the A53 is ~2–4× slower than Colab CPU).

## Confidence-Gated Clarification Loop

Every classification carries a softmax confidence score. Below
`CONFIDENCE_THRESHOLD` (default **0.60**), the device asks a human instead of
guessing, and that correction feeds back into the shared model:

```
UNO Q (deploy/infer_uno_q.py)
  classify frame -> (label, confidence)
  confidence < 0.60?
     -> deploy/clarification_client.py: POST frame + top-k to webapp,
        queue locally if offline (retried via flush_pending())
                    |
                    v
Webapp (separate repo/service) — notifies a human, they pick the right label,
webapp persists the correction
                    |
                    v
deploy/edge_impulse_upload.py — webapp backend calls this (or ports the
pattern into its own stack) to push the corrected {image, label} into the
shared Edge Impulse project's training data
                    |
                    v
Next retrain/export cycle -> updated .tflite -> redeployed to every bin
```

`deploy/` implements the device side end-to-end (TFLite inference matching
`eval_tf` preprocessing, thresholding, webhook client). The webapp and the
Edge Impulse-triggered retrain/redeploy job are out of this repo's scope —
`deploy/clarification_client.py`'s module docstring is the API contract the
webapp must implement (`POST /api/clarifications`), and
`deploy/edge_impulse_upload.py` is a reference the webapp backend can call
once a human confirms a label.

**Threshold calibration note:** 60% is a starting point, not a measured
number. Raw softmax confidence from a label-smoothed model isn't a calibrated
probability — before locking the threshold in, run it against `val_df`/`test_df`
(precision/recall of "was top-1 actually correct" vs. confidence) and adjust
per exported variant (int8 quantization adds logit noise vs. fp32).
