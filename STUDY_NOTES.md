# ECE180 Smart Trashbin — Study Notes

*Personal study notes for `ECE180_Complete_Notebook.ipynb`. Written to be readable
from zero — every concept is explained before it's used. Numbers quoted are from
the actual training run (see `results/` and `exports/`).*

---

## Table of Contents

1. [The problem and the hardware](#1-the-problem-and-the-hardware)
2. [The dataset and why splitting it correctly matters](#2-the-dataset-and-why-splitting-it-correctly-matters)
3. [Transfer learning — why we don't train from scratch](#3-transfer-learning--why-we-dont-train-from-scratch)
4. [Model choice: MobileNetV3-Large vs. the alternatives](#4-model-choice-mobilenetv3-large-vs-the-alternatives)
5. [Data augmentation — simulating a cheap camera](#5-data-augmentation--simulating-a-cheap-camera)
6. [The training recipe, hyperparameter by hyperparameter](#6-the-training-recipe-hyperparameter-by-hyperparameter)
7. [Colab survival engineering](#7-colab-survival-engineering)
8. [Evaluation — accuracy, macro-F1, confusion matrix, domain shift](#8-evaluation)
9. [Confidence calibration — temperature scaling and the threshold](#9-confidence-calibration)
10. [Quantization and export for the UNO Q](#10-quantization-and-export-for-the-uno-q)
11. [Deployment contract — what must match on the device](#11-deployment-contract)
12. [The design philosophy, and roads not taken](#12-design-philosophy-and-roads-not-taken)

---

## 1. The problem and the hardware

**Goal:** a trashbin that looks at the item someone is about to throw away and
classifies it into one of **30 waste categories** (aluminum cans, plastic water
bottles, cardboard boxes, food waste, glass jars, …) so it can route
recyclables vs. trash automatically.

**Hardware: Arduino UNO Q.** It's a dual-brain board:

| Chip | OS | Role |
|---|---|---|
| STM32 microcontroller | RTOS (real-time OS) | camera capture, motors/servos, timing-critical control |
| Qualcomm Dragonwing QRB2210 (4× ARM Cortex-A53 @ ~2 GHz + Adreno 702 GPU) | Debian Linux | runs the neural network |

The split exists because a Linux system can't make hard real-time guarantees
(the kernel might schedule something else at the wrong moment), while a
microcontroller can't run a modern CNN. So: RTOS grabs a frame → hands it to
Linux → Linux runs the classifier → answer goes back.

**Key deployment constraint to keep in mind through everything below:** the
Cortex-A53 is a small, in-order ARM core from ~2012's design lineage. It cannot
run desktop-sized models, and it's dramatically faster at 8-bit integer math
than 32-bit float math. But — critically — the bin only classifies **one frame
per item drop**, not a live video stream, so we have a *latency budget of
hundreds of milliseconds*, not tens. That single fact drives the model choice
(Section 4) and lets us prioritize accuracy over speed.

**The notebook** (`ECE180_Complete_Notebook.ipynb`) is the entire ML pipeline,
run top-to-bottom on Google Colab with a T4 GPU:

```
Cell 0-4   setup: clone repo, check GPU, mount Drive, download dataset, pip installs
Cell 5     manifest + stratified split + transforms
Cell 6     Dataset class + model factory
Cell 7     hyperparameter config
Cell 8     training helpers (checkpointing, EMA, MixUp, epoch loop)
Cell 9     the actual two-stage training run
Cell 10    test-set evaluation
Cell 11    confusion matrix + per-class F1
Cell 12    domain-shift check (default vs real_world)
Cell 12b   confidence calibration (temperature + threshold)
Cell 13    export: ONNX + fp32/dynamic-int8/static-int8 TFLite, each accuracy-measured
Cell 14    reference live-inference function
Cell 15    push metrics/reports back to GitHub
```

Every cell is **idempotent** — safe to re-run after a Colab disconnect
(Section 7 explains how).

---

## 2. The dataset and why splitting it correctly matters

**Dataset:** Kaggle's *Recyclable and Household Waste Classification* —
~15,000 images, 30 classes. Each class folder contains two subsets:

- `default/` — clean studio photos: object centered on a plain background,
  good lighting. Easy.
- `real_world/` — cluttered scenes, occlusion, weird angles, bad lighting.
  This is what the trashbin camera will actually see.

### The domain-shift problem

A model trained and tested on studio photos can score 95%+ and then fall apart
on a real camera feed. The mismatch between training-data distribution and
deployment-data distribution is called **domain shift**, and it is the single
most common way embedded-vision projects fail. This notebook attacks it in
three separate places:

1. **The split is stratified by (class, subset)** — so `real_world` images are
   proportionally present in validation and test. The reported metric
   therefore reflects deployment conditions, not just studio conditions.
2. **Augmentation simulates the camera** (Section 5).
3. **Quantization calibration uses `real_world` images** (Section 10).

### Train / validation / test — why three splits?

- **Train (70%)** — the model learns from these via gradient descent.
- **Validation (15%)** — used for *decisions during development*: when to
  early-stop, which epoch's weights are "best", fitting the temperature,
  choosing the confidence threshold. The model never trains on these, but our
  *choices* are fit to them, so val performance is slightly optimistic.
- **Test (15%)** — touched exactly once, at the end. Because no decision was
  ever made using it, its number is an unbiased estimate of real performance.

If you tune anything on the test set (even "just" a threshold), the test score
stops being trustworthy — you've leaked information. This is why Cell 12b
picks the threshold on **val** and only *verifies* it on test.

### How the split is built (Cell 5)

```python
def split_manifest(df, val_frac=0.15, test_frac=0.15):
    for _, g in df.groupby(['label', 'subset']):   # every (class, subset) group
        rng = np.random.RandomState(SEED)          # SEED = 42, deterministic
        rng.shuffle(idx)
        # first 15% → test, next 15% → val, rest → train
```

- **Stratified** = each of the 60 groups (30 classes × 2 subsets) is split in
  the same 70/15/15 ratio. Without stratification, random chance could put
  most of some rare class's `real_world` images in train and none in test.
- **Deterministic** = fixed seed means the same images land in the same split
  every session. This matters enormously for a multi-session Colab workflow —
  if the split changed between sessions, images trained on yesterday could
  appear in today's test set (leakage), and results wouldn't be comparable.

A **manifest DataFrame** (`filepath, label, class, subset`) is built by
walking the directory tree once; everything downstream indexes into it. This
is cleaner than torchvision's `ImageFolder` because we need the `subset`
column for stratification and the domain-shift evaluation.

---

## 3. Transfer learning — why we don't train from scratch

A CNN trained from random initialization needs to *discover* everything: what
an edge is, what texture is, what a curved metallic surface looks like.
ImageNet-scale training (1.28M images) is what it takes to learn those
general visual features well. With only ~10.5k training images across 30
classes (~350/class), a from-scratch CNN would either underfit (too small) or
memorize the training set (too big) — this is **overfitting**: great train
accuracy, poor test accuracy.

**Transfer learning** sidesteps this:

1. Take a network **pretrained on ImageNet**. Its early layers detect edges,
   colors, and textures; middle layers detect parts and materials; late layers
   detect object-like concepts. Almost all of that transfers to waste
   classification — a crushed can is still made of edges, gloss, and metal
   texture.
2. **Replace the head**: the final linear layer maps 1280 features → 1000
   ImageNet classes; we swap it for a fresh 1280 → 30 layer
   (`m.classifier[3] = nn.Linear(..., 30)`).
3. **Fine-tune**: continue training on our data, gently, so the pretrained
   features adapt without being destroyed.

The result: we get the benefit of 1.28M training images while only paying for
~10.5k of our own. This is *the* standard approach for small/medium vision
datasets and it's why the notebook reaches ~89% on a 30-way task in a few GPU
hours.

One free bonus: the notebook loads `IMAGENET1K_V2` weights for
MobileNetV3-Large. Same architecture as V1, but torchvision retrained it with
a modern recipe (longer schedule, better augmentation) — about **+1 pp**
ImageNet accuracy at zero cost to us.

---

## 4. Model choice: MobileNetV3-Large vs. the alternatives

The notebook trains **one** deployment model: `mobilenet_v3_large`,
ImageNet-pretrained, at **256×256** input. Here's the full reasoning, because
model choice is where most of the engineering judgment lives.

### What makes MobileNetV3 "mobile"?

Standard convolutions are expensive: a 3×3 conv with C_in input and C_out
output channels costs 9·C_in·C_out multiply-adds per pixel. MobileNets replace
these with **depthwise-separable convolutions**: a depthwise 3×3 (each channel
filtered independently, cost 9·C_in) followed by a pointwise 1×1 mixing
channels (cost C_in·C_out). That's roughly an 8–9× compute reduction per
layer. V3 adds:

- **Inverted residual blocks** (expand → depthwise → project) from V2,
- **Squeeze-and-Excite (SE) blocks** — a tiny attention mechanism that
  reweights channels,
- **hard-swish / hard-sigmoid activations** — piecewise-linear approximations
  of swish/sigmoid chosen specifically because they're cheap *and quantize
  cleanly to int8* (a real sigmoid's smooth curve is awkward to represent in
  8-bit fixed point; a piecewise-linear one is not),
- an architecture found partly by **neural architecture search (NAS)**
  optimizing directly for accuracy-vs-mobile-latency.

That last two points matter here: MobileNetV3 was *co-designed* for exactly
our deployment (int8 on ARM), so its quantization behavior is well-trodden and
predictable.

### The candidates

| Model | ImageNet top-1 | Params | Why / why not |
|---|---|---|---|
| **MobileNetV3-Small** | 67.7% | ~2.5M | 3–4 pp weaker features. Its speed advantage buys nothing here — the bin isn't real-time. Kept wired into `make_model` as a fallback if on-device latency surprises us. |
| **MobileNetV3-Large** ✅ | 75.2% (V2 weights ~76.2%) | ~5.4M (4.24M with the 30-class head) | Best accuracy that still runs comfortably int8 on a quad-A53. ~0.22 GFLOPs/frame. |
| **EfficientNet-B0** | ~77.7% | ~5.3M | ~1–2 pp more accurate, but ~2× the A53 latency, and its *smooth* swish + SE combination is notoriously finicky for **static** int8 quantization (bigger, less predictable accuracy drops). |
| **ResNet-50** | ~80% | 25.6M | 6× the parameters, far more FLOPs. Wrong weight class for this device. |
| **ViT / anything transformer** | high | 22M+ | Transformers need more data to fine-tune well, and tiny-ViT int8 support on LiteRT/XNNPACK is immature compared to CNNs. |

**The decision rule:** on a 30-class *fine-grained* task (many classes are
visually similar — e.g., different kinds of plastic), backbone quality shows
up directly in accuracy. Since latency is not the binding constraint,
choose the most accurate backbone that (a) fits the device and (b) quantizes
predictably. That's Large. EfficientNet-B0's ~1 pp edge isn't worth 2× latency
plus quantization risk; Small's speed isn't needed.

### Why 256×256 instead of the standard 224?

Small objects (bottle caps, cutlery, straws) and cluttered scenes benefit from
more pixels. Compute grows roughly with the square of resolution
(256² / 224² ≈ 1.31, so ~31% more), which the latency budget absorbs. The eval
resize keeps ImageNet's resize:crop ratio (`RESIZE_SIZE = round(256·256/224) =
293`): resize the short side to 293, center-crop 256. Keeping this ratio fixed
matters because the pretrained features were learned at a particular
object-to-frame scale.

---

## 5. Data augmentation — simulating a cheap camera

**Augmentation** = randomly perturbing training images each epoch so the model
never sees the exact same picture twice. It's a regularizer (fights
overfitting) and, done thoughtfully, a *domain-gap closer*: if training images
are randomly degraded in the same ways the deployment camera degrades images,
the live feed stops being out-of-distribution.

The training transform (Cell 5), line by line:

```python
transforms.RandomResizedCrop(256, scale=(0.6, 1.0))  # random zoom/crop
transforms.RandomHorizontalFlip()                     # mirror
transforms.RandomRotation(20)                         # ±20° tilt
transforms.ColorJitter(brightness=0.4, contrast=0.3,
                       saturation=0.3, hue=0.05)      # exposure/white-balance
transforms.RandomApply([GaussianBlur(5, (0.1, 2.0))], p=0.3)  # focus/motion blur
transforms.ToTensor()
transforms.Normalize(IMAGENET_MEAN, IMAGENET_STD)
transforms.RandomErasing(p=0.2)                       # random occlusion patch
```

The camera-realism mapping:

| Augmentation | Real-world failure it simulates |
|---|---|
| RandomResizedCrop (down to 60% scale) | item not centered, partially out of frame, variable distance |
| Rotation ±20° | items land at arbitrary angles |
| Strong brightness/contrast jitter | cheap sensor auto-exposure, shadows inside the bin |
| Hue/saturation jitter | wrong white balance under artificial light |
| Gaussian blur (30% of the time) | fixed-focus lens, motion blur mid-drop |
| RandomErasing | occlusion by other trash or the bin lid |

**Evaluation uses none of this** — just deterministic resize → center-crop →
normalize (`eval_tf`). Two reasons: metrics must be reproducible, and the
device will run this exact deterministic pipeline. (The `Normalize` values are
the ImageNet channel means/stds — mandatory because the pretrained backbone
expects inputs in that statistical range.)

---

## 6. The training recipe, hyperparameter by hyperparameter

### The two-stage schedule

**Stage 1 — head warmup (3 epochs, LR 1e-3, backbone frozen).**
The new 30-way head starts with random weights. If the whole network trained
immediately, the head's large, essentially random gradients would backpropagate
into and scramble the delicate pretrained features. So the backbone is frozen
(`requires_grad=False`) and only the head trains — cheap (few parameters, no
backbone gradients) and it doubles as a learning-rate warmup.

**Stage 2 — full fine-tune (up to 30 epochs, LR 1e-4, everything unfrozen).**
Now the head is sensible, so the whole network can adapt together — but at
10× lower LR, because pretrained weights need nudging, not rewriting. Too high
an LR here causes **catastrophic forgetting** (the backbone loses its ImageNet
knowledge faster than it learns waste features).

### Every knob in Cell 7, and its justification

| Knob | Value | What it does / why this value |
|---|---|---|
| `BATCH_SIZE` | 48 | Images per gradient step. Sized to fit Large @ 256px in a T4's ~15 GB VRAM with AMP on. Bigger batches train faster per epoch but need more memory; 48 is the T4 sweet spot. (A100 config: 160, but ~7× the Colab unit cost.) |
| Optimizer | **AdamW** | Adam adapts a per-parameter learning rate from gradient history — robust default for fine-tuning. The "W" = *decoupled* weight decay: in plain Adam, L2 penalty gets tangled with the adaptive scaling and under-regularizes; AdamW applies decay directly to weights, as intended. |
| `WEIGHT_DECAY` | 1e-4 | Shrinks weights toward zero each step — an overfitting brake. 1e-4 is the standard fine-tuning value; higher would fight the pretrained weights. |
| LR schedule | **CosineAnnealingLR** | LR follows a half-cosine from the initial value down to ~0 over the stage. Early: big steps to move fast. Late: tiny steps to settle into a good minimum. Smoother than step-drops, no extra hyperparameters. |
| `LABEL_SMOOTH` | 0.1 | Instead of target = [0,…,1,…,0], target = 0.903 on the true class and 0.0033 on each other. Discourages the network from pushing logits to extremes → less overconfidence, better generalization, measurably better calibration behavior. **Side effect:** raw softmax stops being a probability, which is exactly why Cell 12b (temperature scaling) exists. |
| `EARLY_STOP_PATIENCE` | 6 | Stop stage 2 if val accuracy hasn't improved for 6 consecutive epochs. Saves compute and prevents late-stage overfitting. Set slightly longer than you'd use for Small because Large keeps improving later into training. In practice runs end around epoch 16–24 of 30. |
| `USE_WEIGHTED_SAMPLER` | True | The 30 classes have unequal image counts. A **WeightedRandomSampler** draws each image with probability inversely proportional to its class frequency, so every batch is roughly class-balanced. Without it, gradient pressure favors common classes and rare-class F1 suffers — this knob directly targets **macro-F1**. |
| `USE_MIXUP` + `MIXUP_ALPHA=0.2`, `CUTMIX_ALPHA=1.0` | True | **MixUp**: blend two images pixel-wise (`x = λx₁ + (1−λ)x₂`) and blend their labels the same way. **CutMix**: paste a rectangular patch of image 2 onto image 1; label = area-weighted mix. Each batch randomly uses one or the other (`v2.RandomChoice`). Both force the model to learn features that survive weird composites — strong regularization, empirically worth ~0.5–1 pp on fine-grained tasks. The α values control the Beta distribution λ is drawn from; 0.2/1.0 are the standard published values. Training-time only — the deployed network is unchanged. |
| `EMA_DECAY` | 0.9995 | See below. |
| AMP (`torch.autocast` + `GradScaler`) | on | **Automatic Mixed Precision**: forward/backward run in fp16 where numerically safe, fp32 where not. ~2× throughput and ~half the activation memory on a T4, with no accuracy loss. `GradScaler` scales the loss up before backward so tiny fp16 gradients don't underflow to zero, then unscales before the optimizer step. |

### EMA — the deployed weights are an average

`ModelEMA` keeps a shadow copy of the model updated every step:

```
ema_weight ← 0.9995 · ema_weight + 0.0005 · current_weight
```

SGD-family training bounces around a loss valley; the **exponential moving
average** of the trajectory sits nearer the valley floor and consistently
generalizes a bit better (the same idea powers most modern training recipes).
Two implementation details worth noting:

- The EMA copy averages **floating-point buffers too** (BatchNorm running
  mean/var), and copies integer buffers verbatim — averaging BN stats is
  necessary for the EMA model to actually work standalone.
- **Validation and checkpointing use the EMA weights**, because that's what
  ships. Validating the raw weights but deploying the EMA (or vice versa)
  would mean selecting the "best epoch" with the wrong model.
- 0.9995 (vs a common 0.999) = a longer averaging horizon, matched to the
  longer Large training schedule.

The EMA weights are ordinary fp32 — they quantize exactly like any other
weights, so this costs nothing at deployment.

### The loss

`nn.CrossEntropyLoss(label_smoothing=0.1)` — cross-entropy measures how far
the predicted distribution is from the target. Conveniently, PyTorch's
implementation accepts **either** integer class labels **or** soft
probability targets, so the same criterion handles normal batches and
MixUp/CutMix batches. Note the accuracy metric always compares against the
*hard* label (`y_hard` kept before mixup), since "accuracy against a blended
label" isn't meaningful.

### Compute budgeting (a Colab-specific concern)

Colab Pro bills in compute units: T4 ≈ 1.8 units/hr, A100 ≈ 13 units/hr. The
run targets a T4: ~2–3.5 hours ≈ 4–6 units for the full train. Cell 9 even
tracks a live `units_spent` estimate. The strategy is explicitly **one model,
trained well** rather than a zoo of half-trained models — for a fixed budget,
depth beats breadth when you already know the deployment target.

---

## 7. Colab survival engineering

Colab sessions die: idle timeouts, GPU reclaims, browser crashes. The notebook
treats disconnection as normal, not exceptional:

- **Atomic checkpoint writes** (`safe_torch_save`): write to `file.tmp`, copy
  the previous version to `file.bak`, then `os.replace(tmp, file)` —
  `os.replace` is atomic, so a crash mid-write can never leave a half-written
  checkpoint as the only copy. `safe_torch_load` falls back to `.bak` if the
  main file is corrupt.
- **Resume checkpoints**: every epoch saves `_resume.pt` containing model
  weights, EMA weights, current stage, epoch, best accuracy, and
  early-stopping counter. Re-running Cell 9 picks up exactly where it died —
  including **fast-forwarding the cosine scheduler** to the right epoch
  (otherwise the LR would restart too high and damage converged weights).
- **`progress.json`** marks the model as completed, so re-running the training
  cell after success is a no-op skip.
- **Best checkpoint separate from resume checkpoint**: `_best.pt` holds the
  EMA weights of the best-val-accuracy epoch (what we deploy); `_resume.pt`
  holds the latest raw state (what we continue from). These are different
  things and conflating them is a classic bug.
- **Dataset marker file** (`.complete`): the Kaggle download + copy-to-Drive
  only happens once; a partial copy from an earlier crash is detected and
  wiped before recopying.
- All heavy artifacts live on **Google Drive** (persists across sessions), not
  Colab's ephemeral `/content` disk.

---## 8. Evaluation

### Headline results (from `results/test_results.json`)

| Metric | Value |
|---|---|
| Test accuracy | **89.08%** |
| Macro-F1 | **0.890** |
| Parameters | 4.24M |

### Why report macro-F1 and not just accuracy?

- **Accuracy** = fraction of correct predictions. With imbalanced classes it
  can hide disasters: a model that's great on the 5 biggest classes and
  useless on 10 small ones can still post high accuracy.
- **F1** per class = harmonic mean of precision (of the times we said class X,
  how often were we right?) and recall (of the true class-X items, how many
  did we catch?).
- **Macro-F1** = the unweighted mean of the 30 per-class F1 scores — every
  class counts equally, so neglected rare classes drag it down visibly.

Here macro-F1 (0.890) ≈ accuracy (0.891), which tells us performance is
*even across classes* — the weighted sampler did its job.

### Confusion matrix (Cell 11)

A 30×30 grid: rows = true class, columns = predicted class, **row-normalized**
so each row shows "when the item was truly X, what did the model say?"
independent of class size. The diagonal is correct predictions; bright
off-diagonal cells reveal *systematic* confusions (e.g., visually similar
plastic categories). Cell 11 also prints the 10 weakest classes by F1 — the
actionable output: those are the classes to collect more data for, or to merge
if the downstream sorting decision doesn't distinguish them anyway.

### Domain-shift check (Cell 12) — the honest number

Test accuracy split by subset (`results/domain_shift.json`):

| Subset | Accuracy |
|---|---|
| `default` (studio) | 89.21% |
| `real_world` (cluttered) | **88.95%** |
| Gap | **+0.26 pp** |

This is arguably the most important result in the project. A typical
studio-trained classifier drops 5–15 pp on real-world imagery. A 0.26 pp gap
means the camera-realistic augmentation + stratified splitting essentially
closed the domain gap: **the model is as good on deployment-like images as on
easy ones**, and 88.95% is the number to quote for expected live performance.

---

## 9. Confidence calibration

### The problem

The trashbin has a **clarification loop**: when the model isn't sure, the
device asks the user ("is this recyclable?") instead of silently mis-sorting.
That requires a confidence threshold. Naively you'd use `max(softmax)` and a
hand-picked cutoff like 0.6. Two things are wrong with that:

1. **Softmax confidence is not a probability of being correct.** Modern
   networks are systematically miscalibrated, and label smoothing (which we
   used) distorts it further — in our case toward *under*confidence.
2. **A hand-picked threshold has no guarantee attached.** "0.6" means nothing
   until you measure what accuracy the accepted predictions actually achieve.

### Step 1 — Temperature scaling

**Temperature scaling** divides the logits by a single scalar T before
softmax: `probs = softmax(logits / T)`.

- T > 1 softens the distribution (less confident);
- T < 1 sharpens it (more confident);
- **argmax never changes** — the ranking of classes is invariant to a positive
  scalar divide. So calibration cannot hurt accuracy; it only fixes the
  *meaning* of the confidence number.

T is fitted on the **validation** set by grid search over [0.5, 5.0] in steps
of 0.05, minimizing **NLL** (negative log-likelihood = cross-entropy of the
scaled probabilities against true labels — the standard calibration
objective). One parameter fitted on ~2,280 points can't overfit.

**Fitted result: T = 0.55.** T < 1 means the raw model was *under*confident —
exactly the expected fingerprint of label smoothing (it trained the network to
never output extreme probabilities). Dividing by 0.55 re-sharpens the outputs
so that "0.8 confidence" once again means ≈80% chance of being right.

### Step 2 — Threshold sweep

With calibrated confidences, sweep thresholds t from 0.30 to 0.95 on the val
set and compute, for each:

- **coverage** = fraction of items with confidence ≥ t (auto-handled, no human);
- **accepted-accuracy** = accuracy on just those items.

There's an inherent trade: raise the threshold → accepted predictions get more
accurate, but you bug the human more often.

### Step 3 — Pick by target, verify on test

Decision rule: *the lowest threshold whose val accepted-accuracy ≥ 95%*
(`TARGET_ACCEPT_ACC = 0.95`). Lowest, because among all thresholds meeting the
accuracy guarantee, the lowest one maximizes coverage (fewest human
interruptions).

**Result: threshold = 0.80.** Verified on the untouched test set
(`exports/confidence_calibration.json`):

| Test-set check @ 0.80 | Value |
|---|---|
| Coverage (auto-handled) | 83.3% |
| Accepted-accuracy | **95.7%** ✓ (target ≥95%) |
| Clarification rate | 16.7% |

Translation for the product: the bin sorts ~5 of every 6 items automatically
and is right 95.7% of the time when it does; for the other ~1 in 6 it asks.
That's a *measured guarantee*, not a vibe.

One more subtlety handled in Cell 13: **quantization perturbs logits**, so the
threshold is re-measured per exported model variant (Section 10) — the shipped
variant uses *its own* threshold, not the fp32 one.

---

## 10. Quantization and export for the UNO Q

### What quantization is

Training produces **fp32** weights (32-bit floats). **Quantization** maps them
(and optionally the activations flowing between layers) to **int8**: pick a
scale s and zero-point z per tensor (or per channel) such that
`real ≈ s · (int8_value − z)`. Benefits on an ARM CPU:

- **~4× smaller** model (8 bits vs 32),
- **much faster** — A53-class cores have SIMD int8 dot-product paths (via the
  XNNPACK kernel library) that vastly outrun their float units,
- lower power.

Cost: rounding error in weights and activations → some accuracy drop. The
size of the drop depends on the architecture (Section 4: MobileNetV3 was
designed to keep it small) and on the quantization flavor.

### The three flavors, and the four exports

| Variant | Weights | Activations | Needs calibration data? | Runtime on UNO Q |
|---|---|---|---|---|
| fp32 `.tflite` | float | float | no | **Adreno 702 GPU delegate** (or CPU, slower) |
| **dynamic int8** | int8 | float (quantized on the fly per batch) | no | XNNPACK CPU |
| **static int8** (per-channel) | int8 | int8 with *fixed, pre-computed* ranges | **yes** | XNNPACK CPU — fastest |
| ONNX fp32 | float | float | no | portable fallback for other toolchains (onnx2tf, onnxruntime) |

- *Dynamic* quantization computes activation ranges at inference time —
  no calibration needed, accuracy near fp32, but the range computation adds
  overhead.
- *Static* quantization fixes activation ranges ahead of time by running
  **calibration data** through the prepared model and recording observed
  min/max per tensor. Fastest at inference, but if calibration data doesn't
  match deployment data, the ranges clip real activations and accuracy craters.
- *Per-channel* = each output channel of a conv gets its own scale, instead of
  one scale per whole tensor. Nearly free, and it substantially reduces weight
  quantization error — always use it.

**Calibration set choice — the domain-shift theme again:** ~10 images per
class, drawn from the **`real_world`** subset of the *training* split
(never val/test — that would leak). The int8 activation ranges are thereby
fitted to cluttered-camera statistics, i.e., what the bin actually sees.

### The export pipeline (modern PyTorch → TFLite path)

```
torch.export.export(model)                 # capture the graph (PT2, torch ≥2.6)
  → prepare_pt2e(captured, quantizer)      # insert observers (torchao PT2E API)
  → run calibration batches through it     # observers record ranges
  → convert_pt2e(...)                      # bake in quant/dequant ops
  → litert_torch.convert(...).export(path) # lower to a .tflite flatbuffer
```

(`litert-torch` is the renamed `ai-edge-torch`; LiteRT is the renamed
TensorFlow Lite.) The ONNX export uses the classic TorchScript exporter
(`dynamo=False`, opset 17) because it's the most robust across torch versions.

### Measured results (`exports/quantization_report.json`)

The critical engineering practice here: **never assume the quantization drop —
measure it.** Every variant is evaluated on the full 2,280-image test set
through the actual TFLite interpreter (4 threads, matching the quad A53), and
each gets its own re-measured confidence threshold:

| Variant | Test acc | Δ vs fp32 | Size | Threshold | Coverage @ ≥95% accept-acc |
|---|---|---|---|---|---|
| PyTorch fp32 (reference) | 89.08% | — | — | 0.80 | 83.3% |
| fp32 TFLite | **89.08%** | 0.00 pp | 17.1 MB | 0.75 | 85.5% |
| dynamic int8 | 87.37% | −1.71 pp | 4.6 MB | 0.55 | 79.8% |
| static int8 | 86.36% | −2.72 pp | 4.9 MB | 0.80 | 79.3% |

(Latency numbers in the report are Colab-CPU proxies — useful for *relative*
comparison only; real numbers must be measured on the A53.)

Notice how much the per-variant threshold matters: dynamic int8's calibrated
threshold is 0.55 while static int8's is 0.80 — quantization noise reshapes
the confidence distribution differently per variant. Shipping the fp32
threshold with an int8 model would silently break the 95% guarantee.

**The open deployment decision this data surfaces:** static int8 costs 2.7 pp
of accuracy for CPU speed we may not need. Since the bin is not
latency-bound, the fp32 TFLite on the **Adreno GPU delegate** (zero accuracy
loss) is worth benchmarking on-device before defaulting to int8. The whole
point of exporting four measured variants is that this choice is now
data-driven, not assumed.

---

## 11. Deployment contract

Cell 14 gives the reference `classify_frame(pil_image) → [(class, prob), …]`
function. The device-side implementation (`deploy/infer_uno_q.py`) must match
the notebook **exactly** on four things, or the accuracy and calibration
numbers become fiction:

1. **Preprocessing** = `eval_tf` precisely: resize short side to 293 →
   center-crop 256 → scale to [0,1] → normalize with ImageNet mean/std,
   RGB channel order. (Off-by-one in resize, BGR vs RGB, or skipping
   normalization are the classic silent killers.)
2. **Label order** = the sorted class list (`exports/labels.txt`). Index 7 in
   the logits must mean the same class on device as in training.
3. **Temperature division** — confidence must be computed as
   `softmax(logits / T)` with T = 0.55, or the threshold is meaningless.
4. **The variant-specific threshold** from `quantization_report.json` for the
   variant actually shipped.

Cell 15 pushes the small artifacts (metrics JSONs, confusion matrix PNG,
labels, calibration + quantization reports) to GitHub; the multi-MB
`.tflite`/`.onnx` binaries stay in Drive (and are gitignored).

---

## 12. Design philosophy, and roads not taken

Every choice in the notebook follows one principle: **optimize for the number
the deployed device achieves, not the number the notebook prints.**

| Decision | Deployment-first reasoning |
|---|---|
| Stratify split by (class, subset) | test score reflects the camera's world |
| Camera-realistic augmentation | closed the domain gap to 0.26 pp |
| MobileNetV3-Large over Small/EfficientNet | most accuracy that fits + quantizes predictably; latency budget says speed is not the constraint |
| Validate/deploy the EMA weights | select the model you actually ship |
| Calibrate confidence, threshold-by-target | "asks a human" has a measured ≥95% guarantee |
| Export 4 variants, measure each, per-variant thresholds | fp32-vs-int8 is decided by data, per-device benchmarking |
| One model trained well, not a model zoo | fixed compute budget → depth over breadth when the target is known |

**Roads not taken, and why:**

- *Training in int8 (quantization-aware training, QAT)* — would recover some
  of the 2.7 pp static-int8 drop, but adds significant training complexity;
  post-training quantization (PTQ) was tried first because it's cheap, and the
  fp32-on-GPU-delegate option may make the question moot.
- *Knowledge distillation from a big teacher* — could add ~1 pp, but needs a
  second (large) model trained first; poor fit for the compute budget.
- *Test-time augmentation / ensembling* — accuracy for multiple inference
  passes per frame; unattractive on-device even with a loose latency budget,
  and it complicates calibration.
- *A 2-way or 5-way "material group" head* — the sorting decision may not need
  30 classes; collapsing classes would raise accuracy substantially. Kept
  30-way because per-class granularity aids analysis, and grouping can happen
  *after* the argmax in device logic without retraining.

### Quick-reference card

```
Task        30-class waste classification, Kaggle dataset (~15k images)
Model       MobileNetV3-Large, IMAGENET1K_V2 pretrained, 256×256 input, 4.24M params
Training    2-stage: 3 ep head-only @1e-3 → ≤30 ep full @1e-4, AdamW, cosine LR,
            wd 1e-4, label smoothing 0.1, batch 48, AMP, early stop (patience 6)
Tricks      weighted sampler, MixUp(0.2)+CutMix(1.0), EMA 0.9995 (deployed)
Results     test acc 89.08%, macro-F1 0.890, domain gap +0.26 pp (rw 88.95%)
Calibration T = 0.55, threshold 0.80 → 83% coverage @ 95.7% accepted-accuracy
Export      fp32 TFLite 89.08% / dyn-int8 87.37% / static-int8 86.36%,
            per-variant thresholds 0.75 / 0.55 / 0.80
Target      UNO Q: 4×Cortex-A53 (XNNPACK int8) or Adreno 702 (fp32 delegate)
```
