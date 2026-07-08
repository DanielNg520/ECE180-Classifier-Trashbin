# ECE180 Smart Trashbin — Recyclable & Household Waste Classification

Transfer-learning image classifier for a smart trashbin built on the **Arduino UNO Q**.
Inference runs on the UNO Q's Dragonwing Linux MPU (Cortex-A53); an RTOS on the
STM32 MCU handles camera capture and real-time bin control, handing frames to the
Linux side for classification.

**Course:** ECE 180, UC San Diego
**Dataset:** [Recyclable and Household Waste Classification](https://www.kaggle.com/datasets/alistairking/recyclable-and-household-waste-classification) (30 classes, ~15k images; each class has `default` studio and `real_world` cluttered subsets)

---

## Approach

- **Models:** MobileNetV3-Small (deployment target) and EfficientNet-B0 (accuracy reference), both ImageNet-pretrained, fine-tuned at 224×224 with a 30-way head
- **Split:** stratified 70/15/15 by (class, subset) so `real_world` images are proportionally represented in val/test — reported metrics reflect what the trashbin camera actually sees
- **Training:** two-stage fine-tune (head-only warmup → full fine-tune), AdamW + cosine schedule, label smoothing 0.1, early stopping on val accuracy
- **Augmentation:** camera-realistic — exposure/color jitter, Gaussian blur, random resized crop, random erasing — to close the studio→live-camera gap
- **Evaluation:** test accuracy, macro-F1, 30×30 confusion matrix, and a **domain-shift check** reporting `default` vs. `real_world` accuracy separately
- **Export:** PyTorch → ONNX (+ TFLite path) with int8 quantization for the UNO Q, plus `labels.txt` and a reference `classify_frame()` entry point for the RTOS→Linux handoff

## Running

The notebook is self-contained and designed for **Google Colab Pro (GPU runtime)**:

1. Add Colab Secrets: `GITHUB_TOKEN`, `KAGGLE_USERNAME`, `KAGGLE_KEY`
2. Run `ECE180_Complete_Notebook.ipynb` top to bottom

The dataset downloads once via kagglehub into Google Drive
(`MyDrive/ECE180_project/`); checkpoints and results persist there too.
Training is **multi-session safe** — if Colab disconnects, re-run the notebook
and it resumes from the last epoch, skipping completed models.

## Repo Structure

```
.
├── ECE180_Complete_Notebook.ipynb   # Full pipeline: download → train → eval → export
├── results/                         # test_results.json, domain_shift.json, confusion_matrix.png
└── README.md
```

Drive layout (created by the notebook):

```
MyDrive/ECE180_project/
├── WasteDataset/      # 30 class dirs × {default, real_world}
├── checkpoints/       # *_best.pt, *_resume.pt, progress.json
├── results/           # metrics JSON + plots
└── export/            # ONNX / TFLite models + labels.txt
```

## Deployment Notes (UNO Q)

- The Linux-side inference code **must replicate the notebook's `eval_tf`
  preprocessing exactly**: resize shorter side to 256 → center-crop 224 →
  ImageNet mean/std normalization. Preprocessing mismatch is the most common
  cause of "works in Colab, fails on device."
- If TFLite conversion isn't done in Colab, convert the exported ONNX with
  `onnx2tf` and post-training int8 quantization using ~200 training images as
  the representative dataset.
- MobileNetV3-Small (~2.5M params) runs comfortably on the quad Cortex-A53;
  the notebook prints a CPU latency estimate for reference.
