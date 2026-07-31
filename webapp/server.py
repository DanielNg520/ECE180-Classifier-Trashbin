"""Bin-ary Sort dashboard + clarification web app.

Runs on the cloud server using FastAPI, SQLite, and Uvicorn.

Endpoints:
  GET  /                              responsive dashboard
  GET  /logo.png                      Bin-ary Sort logo
  POST /api/events                    store every classification
  POST /api/clarifications            store low-confidence frames
  GET  /api/recent                    dashboard data feed
  GET  /clarifications/{id}/image     stored clarification image
  POST /api/clarifications/{id}/label save human-corrected label
"""

import json
import sqlite3
import time
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "trashbin.db"
IMAGE_DIR = BASE / "clarification_images"
LOGO_PATH = BASE / "Binsort.png"

IMAGE_DIR.mkdir(exist_ok=True)

app = FastAPI(title="Bin-ary Sort")


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


with db() as conn:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT,
            timestamp TEXT,
            model_version TEXT,
            predictions TEXT,
            needs_clarification INTEGER,
            ms_frame REAL
        );

        CREATE TABLE IF NOT EXISTS clarifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT,
            timestamp TEXT,
            model_version TEXT,
            predictions TEXT,
            image_path TEXT,
            human_label TEXT
        );
        """
    )


@app.post("/api/events")
async def post_event(payload: dict):
    with db() as conn:
        conn.execute(
            """
            INSERT INTO events (
                device_id,
                timestamp,
                model_version,
                predictions,
                needs_clarification,
                ms_frame
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                payload.get("device_id"),
                payload.get("timestamp"),
                payload.get("model_version"),
                json.dumps(payload.get("predictions", [])),
                int(bool(payload.get("needs_clarification"))),
                payload.get("ms_frame"),
            ),
        )
    return {"ok": True}


@app.post("/api/clarifications", status_code=202)
async def post_clarification(
    device_id: str = Form(...),
    timestamp: str = Form(...),
    model_version: str = Form(...),
    predictions: str = Form(...),
    image: UploadFile = File(...),
):
    filename = f"{int(time.time() * 1000)}.jpg"
    (IMAGE_DIR / filename).write_bytes(await image.read())

    with db() as conn:
        conn.execute(
            """
            INSERT INTO clarifications (
                device_id,
                timestamp,
                model_version,
                predictions,
                image_path
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (device_id, timestamp, model_version, predictions, filename),
        )

    return {"accepted": True}


@app.get("/api/recent")
async def recent():
    with db() as conn:
        events = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT 50"
            )
        ]

        pending = [
            dict(row)
            for row in conn.execute(
                """
                SELECT *
                FROM clarifications
                WHERE human_label IS NULL
                ORDER BY id DESC
                LIMIT 20
                """
            )
        ]

        total, flagged = conn.execute(
            """
            SELECT
                COUNT(*),
                COALESCE(SUM(needs_clarification), 0)
            FROM events
            """
        ).fetchone()

    return {
        "events": events,
        "pending": pending,
        "total": total,
        "flagged": flagged,
    }


@app.get("/logo.png")
async def logo():
    if not LOGO_PATH.exists():
        return JSONResponse({"error": "logo not found"}, status_code=404)
    return FileResponse(LOGO_PATH, media_type="image/png")


@app.get("/clarifications/{cid}/image")
async def clarification_image(cid: int):
    with db() as conn:
        row = conn.execute(
            "SELECT image_path FROM clarifications WHERE id = ?",
            (cid,),
        ).fetchone()

    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)

    image_path = IMAGE_DIR / row["image_path"]

    if not image_path.exists():
        return JSONResponse({"error": "image file missing"}, status_code=404)

    return FileResponse(image_path, media_type="image/jpeg")


IGNORED_LABEL = "ignored"


def forward_to_edge_impulse(image_path: Path, label: str):
    """Push a confirmed sample into Edge Impulse when EI_API_KEY is configured."""
    import os

    import requests

    api_key = os.environ.get("EI_API_KEY")
    if not api_key:
        return

    try:
        with open(image_path, "rb") as image_file:
            requests.post(
                "https://ingestion.edgeimpulse.com/api/training/files",
                headers={
                    "x-api-key": api_key,
                    "x-label": label,
                },
                files={
                    "data": (
                        image_path.name,
                        image_file,
                        "image/jpeg",
                    )
                },
                timeout=15,
            ).raise_for_status()
    except (OSError, requests.RequestException) as error:
        print(f"[edge-impulse] upload failed for {image_path.name}: {error}")


@app.post("/api/clarifications/{cid}/label")
async def label_clarification(cid: int, payload: dict):
    label = str(payload.get("label", "")).strip()

    if not label:
        return JSONResponse({"error": "label is required"}, status_code=400)

    with db() as conn:
        row = conn.execute(
            "SELECT image_path FROM clarifications WHERE id = ?",
            (cid,),
        ).fetchone()

        if not row:
            return JSONResponse(
                {"error": "clarification not found"},
                status_code=404,
            )

        conn.execute(
            """
            UPDATE clarifications
            SET human_label = ?
            WHERE id = ?
            """,
            (label, cid),
        )

    if label != IGNORED_LABEL:
        forward_to_edge_impulse(IMAGE_DIR / row["image_path"], label)

    return {"ok": True}


DASHBOARD = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#0f3b63">
  <title>Bin-ary Sort</title>

  <style>
    :root {
      --navy: #123b63;
      --navy-dark: #0b2946;
      --navy-soft: #eaf2f8;
      --green: #12945f;
      --green-dark: #08744a;
      --green-soft: #e8f6ef;
      --lime: #67ae32;
      --orange: #d9653b;
      --orange-soft: #fff1eb;
      --text: #173451;
      --muted: #6a7a8e;
      --line: #dce6e2;
      --page: #f5faf8;
      --card: rgba(255, 255, 255, 0.94);
      --shadow: 0 18px 44px rgba(20, 59, 82, 0.10);
      --radius: 24px;
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      min-height: 100vh;
      color: var(--text);
      font-family:
        Inter,
        ui-sans-serif,
        system-ui,
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        sans-serif;
      background:
        radial-gradient(circle at top left, rgba(18, 148, 95, 0.08), transparent 33rem),
        radial-gradient(circle at top right, rgba(18, 59, 99, 0.08), transparent 31rem),
        var(--page);
    }

    button {
      font: inherit;
    }

    .topbar {
      position: sticky;
      top: 0;
      z-index: 20;
      border-bottom: 1px solid rgba(18, 59, 99, 0.08);
      background: rgba(255, 255, 255, 0.88);
      backdrop-filter: blur(18px);
    }

    .topbar-inner {
      max-width: 1480px;
      margin: 0 auto;
      padding: 18px 28px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 24px;
    }

    .brand {
      display: flex;
      align-items: center;
      min-width: 0;
      gap: 14px;
    }

    .brand img {
      width: 62px;
      height: 62px;
      object-fit: contain;
      border-radius: 16px;
      background: white;
    }

    .brand h1 {
      margin: 0;
      font-size: clamp(1.55rem, 2.6vw, 2.25rem);
      line-height: 1;
      letter-spacing: -0.04em;
      color: var(--navy);
    }

    .brand p {
      margin: 7px 0 0;
      color: var(--muted);
      font-size: 0.96rem;
      font-weight: 650;
    }

    .connection-pill {
      display: inline-flex;
      align-items: center;
      gap: 9px;
      flex-shrink: 0;
      padding: 10px 16px;
      border-radius: 999px;
      color: var(--green-dark);
      background: var(--green-soft);
      font-size: 0.9rem;
      font-weight: 800;
    }

    .connection-dot {
      width: 9px;
      height: 9px;
      border-radius: 50%;
      background: var(--green);
      box-shadow: 0 0 0 5px rgba(18, 148, 95, 0.12);
    }

    .shell {
      max-width: 1480px;
      margin: 0 auto;
      padding: 30px 28px 54px;
    }

    .hero-grid {
      display: grid;
      grid-template-columns: minmax(0, 1.45fr) minmax(330px, 0.75fr);
      gap: 22px;
    }

    .card {
      border: 1px solid rgba(18, 59, 99, 0.10);
      border-radius: var(--radius);
      background: var(--card);
      box-shadow: var(--shadow);
    }

    .card-pad {
      padding: 26px;
    }

    .eyebrow {
      margin: 0 0 5px;
      color: var(--green-dark);
      font-size: 0.78rem;
      font-weight: 900;
      letter-spacing: 0.12em;
      text-transform: uppercase;
    }

    .section-title {
      margin: 0;
      color: var(--navy);
      font-size: 1.65rem;
      letter-spacing: -0.035em;
    }

    .section-subtitle {
      margin: 8px 0 0;
      color: var(--muted);
      line-height: 1.55;
    }

    .stats-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 14px;
      margin-top: 22px;
    }

    .stat-card {
      padding: 18px;
      border: 1px solid var(--line);
      border-radius: 18px;
      background: linear-gradient(145deg, #ffffff, #f8fbfa);
    }

    .stat-label {
      color: var(--muted);
      font-size: 0.78rem;
      font-weight: 850;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }

    .stat-value {
      display: block;
      margin-top: 7px;
      color: var(--navy);
      font-size: 2rem;
      font-weight: 900;
      letter-spacing: -0.05em;
    }

    .stat-note {
      display: block;
      margin-top: 4px;
      color: var(--muted);
      font-size: 0.8rem;
    }

    .latest-panel {
      height: 100%;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      min-height: 300px;
      text-align: center;
    }

    .latest-icon {
      width: 84px;
      height: 84px;
      display: grid;
      place-items: center;
      border-radius: 24px;
      color: var(--green-dark);
      background: linear-gradient(145deg, var(--green-soft), #f7fffb);
      font-size: 2.25rem;
      font-weight: 900;
    }

    .latest-class {
      margin: 18px 0 4px;
      color: var(--navy);
      font-size: 2.3rem;
      font-weight: 900;
      letter-spacing: -0.05em;
      text-transform: capitalize;
    }

    .latest-meta {
      color: var(--muted);
      font-weight: 650;
    }

    .confidence-wrap {
      width: 100%;
      margin-top: 24px;
      text-align: left;
    }

    .confidence-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      color: var(--muted);
      font-size: 0.88rem;
      font-weight: 800;
    }

    .confidence-row strong {
      color: var(--navy);
    }

    .confidence-track {
      height: 11px;
      margin-top: 10px;
      overflow: hidden;
      border-radius: 999px;
      background: #e4ece8;
    }

    .confidence-bar {
      width: 0;
      height: 100%;
      border-radius: inherit;
      background: linear-gradient(90deg, var(--green), var(--lime));
      transition: width 350ms ease;
    }

    .content-grid {
      display: grid;
      grid-template-columns: minmax(0, 1.45fr) minmax(340px, 0.75fr);
      gap: 22px;
      margin-top: 22px;
    }

    .table-wrap {
      margin-top: 20px;
      overflow-x: auto;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      min-width: 680px;
    }

    th,
    td {
      padding: 14px 12px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: middle;
    }

    th {
      color: var(--muted);
      font-size: 0.76rem;
      font-weight: 900;
      letter-spacing: 0.06em;
      text-transform: uppercase;
    }

    td {
      font-size: 0.92rem;
    }

    tbody tr {
      transition: background 160ms ease;
    }

    tbody tr:hover {
      background: #f7fbf9;
    }

    .classification-badge {
      display: inline-flex;
      align-items: center;
      padding: 6px 10px;
      border-radius: 999px;
      color: var(--navy);
      background: var(--navy-soft);
      font-size: 0.8rem;
      font-weight: 850;
      text-transform: capitalize;
    }

    .classification-badge.flagged {
      color: #9a4329;
      background: var(--orange-soft);
    }

    .mono {
      font-variant-numeric: tabular-nums;
    }

    .empty {
      margin: 18px 0 0;
      padding: 30px 18px;
      border: 1px dashed #cbdad4;
      border-radius: 18px;
      color: var(--muted);
      background: #fbfdfc;
      text-align: center;
    }

    .pending-list {
      display: grid;
      gap: 16px;
      margin-top: 20px;
    }

    .clarification-card {
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: 19px;
      background: white;
    }

    .clarification-card img {
      display: block;
      width: 100%;
      height: 220px;
      object-fit: cover;
      background: #edf4f1;
    }

    .clarification-body {
      padding: 15px;
    }

    .clarification-meta {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 12px;
      color: var(--muted);
      font-size: 0.78rem;
      font-weight: 750;
    }

    .button-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }

    .action-button {
      min-height: 42px;
      padding: 9px 13px;
      border: 0;
      border-radius: 12px;
      color: white;
      background: var(--navy);
      cursor: pointer;
      font-size: 0.83rem;
      font-weight: 850;
      transition:
        transform 150ms ease,
        opacity 150ms ease;
    }

    .action-button:hover {
      transform: translateY(-1px);
      opacity: 0.92;
    }

    .action-button.secondary {
      color: var(--navy);
      background: var(--navy-soft);
    }

    .action-button.ignore {
      color: #983f27;
      background: var(--orange-soft);
    }

    .status-strip {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 14px;
      margin-top: 22px;
    }

    .status-card {
      display: flex;
      align-items: center;
      gap: 13px;
      padding: 17px;
      border: 1px solid rgba(18, 59, 99, 0.10);
      border-radius: 18px;
      background: rgba(255, 255, 255, 0.86);
    }

    .status-icon {
      width: 44px;
      height: 44px;
      display: grid;
      place-items: center;
      flex-shrink: 0;
      border-radius: 14px;
      background: var(--green-soft);
      font-size: 1.2rem;
    }

    .status-card span {
      display: block;
      color: var(--muted);
      font-size: 0.74rem;
      font-weight: 800;
    }

    .status-card strong {
      display: block;
      margin-top: 2px;
      color: var(--navy);
      font-size: 0.92rem;
    }

    .error-banner {
      display: none;
      margin-bottom: 18px;
      padding: 13px 16px;
      border: 1px solid #efc9ba;
      border-radius: 14px;
      color: #8c3f27;
      background: var(--orange-soft);
      font-weight: 750;
    }

    .error-banner.show {
      display: block;
    }

    @media (max-width: 1050px) {
      .hero-grid,
      .content-grid {
        grid-template-columns: 1fr;
      }

      .stats-grid,
      .status-strip {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .latest-panel {
        min-height: 260px;
      }
    }

    @media (max-width: 650px) {
      .topbar-inner {
        padding: 14px 16px;
      }

      .brand img {
        width: 52px;
        height: 52px;
      }

      .brand p {
        display: none;
      }

      .connection-pill {
        padding: 9px 11px;
        font-size: 0.76rem;
      }

      .shell {
        padding: 18px 14px 40px;
      }

      .card-pad {
        padding: 20px;
      }

      .stats-grid,
      .status-strip {
        grid-template-columns: 1fr 1fr;
      }

      .stat-card {
        padding: 15px;
      }

      .stat-value {
        font-size: 1.65rem;
      }

      .clarification-card img {
        height: 250px;
      }
    }

    @media (max-width: 430px) {
      .connection-pill span:last-child {
        display: none;
      }

      .stats-grid,
      .status-strip {
        grid-template-columns: 1fr;
      }

      .brand h1 {
        font-size: 1.45rem;
      }
    }
  </style>
</head>

<body>
  <header class="topbar">
    <div class="topbar-inner">
      <div class="brand">
        <img src="/logo.png" alt="Bin-ary Sort logo">
        <div>
          <h1>Bin-ary Sort</h1>
          <p>Edge AI waste classification dashboard</p>
        </div>
      </div>

      <div class="connection-pill" id="connectionPill">
        <span class="connection-dot"></span>
        <span id="connectionText">Server connected</span>
      </div>
    </div>
  </header>

  <main class="shell">
    <div class="error-banner" id="errorBanner">
      The dashboard could not retrieve the latest server data.
    </div>

    <section class="hero-grid">
      <div class="card card-pad">
        <p class="eyebrow">System overview</p>
        <h2 class="section-title">Classification activity</h2>
        <p class="section-subtitle">
          Live results received from connected edge devices.
        </p>

        <div class="stats-grid">
          <div class="stat-card">
            <span class="stat-label">Total results</span>
            <strong class="stat-value" id="total">0</strong>
            <span class="stat-note">all classifications</span>
          </div>

          <div class="stat-card">
            <span class="stat-label">Flagged</span>
            <strong class="stat-value" id="flagged">0</strong>
            <span class="stat-note">needs review</span>
          </div>

          <div class="stat-card">
            <span class="stat-label">Pending</span>
            <strong class="stat-value" id="pendingCount">0</strong>
            <span class="stat-note">unlabeled images</span>
          </div>

          <div class="stat-card">
            <span class="stat-label">Review rate</span>
            <strong class="stat-value" id="reviewRate">0%</strong>
            <span class="stat-note">flagged of total</span>
          </div>
        </div>
      </div>

      <div class="card card-pad">
        <div class="latest-panel">
          <p class="eyebrow">Latest classification</p>
          <div class="latest-icon" id="latestIcon">?</div>
          <div class="latest-class" id="latestClass">Waiting</div>
          <div class="latest-meta" id="latestMeta">
            Waiting for the first result
          </div>

          <div class="confidence-wrap">
            <div class="confidence-row">
              <span>Model confidence</span>
              <strong id="latestConfidence">--</strong>
            </div>
            <div class="confidence-track">
              <div class="confidence-bar" id="confidenceBar"></div>
            </div>
          </div>
        </div>
      </div>
    </section>

    <section class="status-strip">
      <div class="status-card">
        <div class="status-icon">🌐</div>
        <div>
          <span>Web server</span>
          <strong id="serverStatus">Online</strong>
        </div>
      </div>

      <div class="status-card">
        <div class="status-icon">📡</div>
        <div>
          <span>Latest device</span>
          <strong id="latestDevice">No data</strong>
        </div>
      </div>

      <div class="status-card">
        <div class="status-icon">🧠</div>
        <div>
          <span>Model version</span>
          <strong id="latestModel">No data</strong>
        </div>
      </div>

      <div class="status-card">
        <div class="status-icon">⏱️</div>
        <div>
          <span>Last update</span>
          <strong id="lastUpdate">Waiting</strong>
        </div>
      </div>
    </section>

    <section class="content-grid">
      <div class="card card-pad">
        <p class="eyebrow">Classification history</p>
        <h2 class="section-title">Recent results</h2>
        <p class="section-subtitle">
          The 50 most recent classifications stored by the server.
        </p>

        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>Device</th>
                <th>Prediction</th>
                <th>Confidence</th>
                <th>Frame time</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody id="rows"></tbody>
          </table>

          <div class="empty" id="historyEmpty">
            No classification events have been received yet.
          </div>
        </div>
      </div>

      <aside class="card card-pad">
        <p class="eyebrow">Human feedback</p>
        <h2 class="section-title">Needs review</h2>
        <p class="section-subtitle">
          Confirm the correct label or ignore unusable images.
        </p>

        <div class="pending-list" id="pending"></div>
      </aside>
    </section>
  </main>

  <script>
    const totalElement = document.getElementById("total");
    const flaggedElement = document.getElementById("flagged");
    const pendingCountElement = document.getElementById("pendingCount");
    const reviewRateElement = document.getElementById("reviewRate");
    const rowsElement = document.getElementById("rows");
    const pendingElement = document.getElementById("pending");
    const historyEmptyElement = document.getElementById("historyEmpty");
    const latestIconElement = document.getElementById("latestIcon");
    const latestClassElement = document.getElementById("latestClass");
    const latestMetaElement = document.getElementById("latestMeta");
    const latestConfidenceElement = document.getElementById("latestConfidence");
    const confidenceBarElement = document.getElementById("confidenceBar");
    const latestDeviceElement = document.getElementById("latestDevice");
    const latestModelElement = document.getElementById("latestModel");
    const lastUpdateElement = document.getElementById("lastUpdate");
    const connectionTextElement = document.getElementById("connectionText");
    const errorBannerElement = document.getElementById("errorBanner");

    function escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }

    function safePredictions(rawPredictions) {
      try {
        const parsed = JSON.parse(rawPredictions || "[]");
        return Array.isArray(parsed) ? parsed : [];
      } catch {
        return [];
      }
    }

    function formatTimestamp(timestamp) {
      if (!timestamp) return "Unknown";

      const date = new Date(timestamp);
      if (Number.isNaN(date.getTime())) {
        return timestamp;
      }

      return date.toLocaleString([], {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
        second: "2-digit"
      });
    }

    function formatConfidence(value) {
      const numericValue = Number(value);
      if (!Number.isFinite(numericValue)) return "--";
      return `${(numericValue * 100).toFixed(1)}%`;
    }

    function formatMilliseconds(value) {
      const numericValue = Number(value);
      if (!Number.isFinite(numericValue)) return "--";
      return `${numericValue.toFixed(0)} ms`;
    }

    function updateLatest(event) {
      if (!event) {
        latestIconElement.textContent = "?";
        latestClassElement.textContent = "Waiting";
        latestMetaElement.textContent = "Waiting for the first result";
        latestConfidenceElement.textContent = "--";
        confidenceBarElement.style.width = "0%";
        latestDeviceElement.textContent = "No data";
        latestModelElement.textContent = "No data";
        return;
      }

      const prediction = safePredictions(event.predictions)[0] || {};
      const predictedClass = prediction.class || "Unknown";
      const confidence = Number(prediction.confidence || 0);
      const normalizedClass = String(predictedClass).toLowerCase();

      latestIconElement.textContent =
        normalizedClass.includes("recycl") ? "♻" :
        normalizedClass.includes("trash") ? "🗑" :
        "?";

      latestClassElement.textContent = predictedClass;
      latestMetaElement.textContent =
        `${escapeHtml(event.device_id || "Unknown device")} • ${formatTimestamp(event.timestamp)}`;
      latestConfidenceElement.textContent = formatConfidence(confidence);
      confidenceBarElement.style.width =
        `${Math.max(0, Math.min(100, confidence * 100))}%`;
      latestDeviceElement.textContent = event.device_id || "Unknown";
      latestModelElement.textContent = event.model_version || "Unknown";
    }

    function renderRows(events) {
      historyEmptyElement.style.display = events.length ? "none" : "block";

      rowsElement.innerHTML = events.map(event => {
        const prediction = safePredictions(event.predictions)[0] || {};
        const isFlagged = Boolean(event.needs_clarification);

        return `
          <tr>
            <td class="mono">${escapeHtml(formatTimestamp(event.timestamp))}</td>
            <td>${escapeHtml(event.device_id || "Unknown")}</td>
            <td>
              <span class="classification-badge ${isFlagged ? "flagged" : ""}">
                ${escapeHtml(prediction.class || "Unknown")}
              </span>
            </td>
            <td class="mono">${escapeHtml(formatConfidence(prediction.confidence))}</td>
            <td class="mono">${escapeHtml(formatMilliseconds(event.ms_frame))}</td>
            <td>${isFlagged ? "Review" : "Accepted"}</td>
          </tr>
        `;
      }).join("");
    }

    function renderPending(pendingItems) {
      if (!pendingItems.length) {
        pendingElement.innerHTML = `
          <div class="empty">
            Nothing is waiting for review.
          </div>
        `;
        return;
      }

      pendingElement.innerHTML = pendingItems.map(item => {
        const predictions = safePredictions(item.predictions);

        const predictionButtons = predictions.map(prediction => `
          <button
            class="action-button"
            onclick='submitLabel(${Number(item.id)}, ${JSON.stringify(String(prediction.class || ""))})'
          >
            ${escapeHtml(prediction.class || "Unknown")}
            ${escapeHtml(formatConfidence(prediction.confidence))}
          </button>
        `).join("");

        return `
          <article class="clarification-card">
            <img
              src="/clarifications/${Number(item.id)}/image"
              alt="Image awaiting classification review"
              loading="lazy"
            >

            <div class="clarification-body">
              <div class="clarification-meta">
                <span>${escapeHtml(item.device_id || "Unknown device")}</span>
                <span>${escapeHtml(formatTimestamp(item.timestamp))}</span>
              </div>

              <div class="button-row">
                ${predictionButtons}

                <button
                  class="action-button secondary"
                  onclick="submitCustomLabel(${Number(item.id)})"
                >
                  Other
                </button>

                <button
                  class="action-button ignore"
                  onclick="submitLabel(${Number(item.id)}, 'ignored')"
                >
                  Ignore
                </button>
              </div>
            </div>
          </article>
        `;
      }).join("");
    }

    async function refresh() {
      try {
        const response = await fetch("/api/recent", {
          cache: "no-store"
        });

        if (!response.ok) {
          throw new Error(`Server returned ${response.status}`);
        }

        const data = await response.json();
        const events = Array.isArray(data.events) ? data.events : [];
        const pendingItems = Array.isArray(data.pending) ? data.pending : [];
        const total = Number(data.total || 0);
        const flagged = Number(data.flagged || 0);

        totalElement.textContent = total.toLocaleString();
        flaggedElement.textContent = flagged.toLocaleString();
        pendingCountElement.textContent = pendingItems.length.toLocaleString();
        reviewRateElement.textContent =
          total > 0 ? `${((flagged / total) * 100).toFixed(1)}%` : "0%";

        updateLatest(events[0]);
        renderRows(events);
        renderPending(pendingItems);

        lastUpdateElement.textContent = new Date().toLocaleTimeString([], {
          hour: "numeric",
          minute: "2-digit",
          second: "2-digit"
        });

        connectionTextElement.textContent = "Server connected";
        errorBannerElement.classList.remove("show");
      } catch (error) {
        console.error(error);
        connectionTextElement.textContent = "Connection issue";
        errorBannerElement.classList.add("show");
      }
    }

    async function submitLabel(id, label) {
      if (!label) return;

      try {
        const response = await fetch(`/api/clarifications/${id}/label`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json"
          },
          body: JSON.stringify({ label })
        });

        if (!response.ok) {
          throw new Error(`Unable to save label: ${response.status}`);
        }

        await refresh();
      } catch (error) {
        console.error(error);
        alert("The label could not be saved. Please try again.");
      }
    }

    function submitCustomLabel(id) {
      const customLabel = prompt("Enter the correct label:");
      if (customLabel && customLabel.trim()) {
        submitLabel(id, customLabel.trim());
      }
    }

    refresh();
    setInterval(refresh, 3000);
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(DASHBOARD)
