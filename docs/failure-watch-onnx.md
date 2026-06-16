# AI failure-watch — ONNX detector setup (operator one-time)

This is the one-time pinning step for the V5 spaghetti-detection inference
model. The agent ships in **stub mode** by default (every sample returns
p=0.0 — wire path is live, nothing fires). To turn on the real detector you
need to (1) export the upstream `.pt` to ONNX once, (2) host the `.onnx`
somewhere reachable from the Pi, (3) set two environment variables.

After this is done, every enrolled hub running agent ≥ v0.35.0 will pull the
model on next restart, verify its sha256, and start sending real
inference samples.

---

## Source model

| Field | Value |
|---|---|
| Repo | [`ApatheticWithoutTheA/3D-Print-Failure-Detector`](https://huggingface.co/ApatheticWithoutTheA/3D-Print-Failure-Detector) |
| License | MIT |
| File | `yolov11-3d-print-failure-detection` (extension-less Ultralytics `.pt`) |
| Size | 5,471,635 B (~5.2 MB compressed) |
| Architecture | YOLOv11s, 3 classes: spaghetti, stringing, zits |
| Accuracy (mAP@50-95) | spaghetti 0.82, stringing 0.60, zits 0.45 |

## One-time export to ONNX

Do this **once**, locally (any x86 box with Python is fine — no Pi needed):

```bash
# 1. Fresh venv (you don't want ultralytics in your global env)
python3 -m venv /tmp/yolo-export
source /tmp/yolo-export/bin/activate

# 2. Install ultralytics (pulls torch — ~2 GB)
pip install ultralytics

# 3. Download the upstream .pt
curl -L -o yolov11-failure-detector.pt \
  https://huggingface.co/ApatheticWithoutTheA/3D-Print-Failure-Detector/resolve/main/yolov11-3d-print-failure-detection

# 4. Export to ONNX. **DO NOT change `imgsz` — the agent's preprocess pins
#    640 (see makeros_hub/printers/yolo_preprocess.INPUT_SIZE). A different
#    export size will be rejected by the decoder's anchor-count check.**
yolo export model=yolov11-failure-detector.pt format=onnx imgsz=640 opset=12

# This writes `yolov11-failure-detector.onnx` (~10-12 MB).

# 5. Compute its sha256 — you'll need this for the env var
sha256sum yolov11-failure-detector.onnx
```

## Host the `.onnx`

Upload `yolov11-failure-detector.onnx` somewhere the Pi can HTTPS-fetch it.
Options (any works):

- Supabase Storage public bucket (recommended — same infra as `workspace-images`)
- Cloudflare R2 + custom domain
- GitHub release attached binary on this repo

The URL must serve the raw bytes (no HTML wrapper). Test with `curl -I`.

## Env reference

| Env var | Default | When to override |
|---|---|---|
| `MAKEROS_HUB_MODEL_URL` | (empty — stub mode) | Pin your hosted ONNX URL. Required to activate. |
| `MAKEROS_HUB_MODEL_SHA256` | (empty — stub mode) | Required. The `sha256sum` of the URL's content. Mismatch → fall back to stub. |
| `MAKEROS_HUB_MODEL_CACHE_DIR` | `/var/lib/makeros-hub/models` | Custom cache path (e.g. external SSD on a Pi). |
| `MAKEROS_HUB_DETECTOR_INTRA_OP_THREADS` | `2` | Reduce to 1 on a Pi 4 with the matcher or vprinter under load. |
| `MAKEROS_HUB_DETECTOR_INTER_OP_THREADS` | `1` | Rarely worth changing (ORT SEQUENTIAL mode ignores it). |
| `MAKEROS_HUB_DETECTOR_DOWNLOAD_TIMEOUT_SEC` | `15` | Bump on a hub with slow upstream (boots through a captive portal). |
| `MAKEROS_HUB_DETECTOR_DOWNLOAD_RETRIES` | `1` | Set `0` to fail fast at boot. |
| `MAKEROS_HUB_CLASS_WEIGHTS` | `1.0,0.7,0.5` | Rebalance spaghetti/stringing/zits without a release. Comma-separated floats. |

## Set the env vars

The detector activates when both vars are set on the Pi.

### Option A — per-hub via systemd drop-in (one-off testing)

```bash
sudo systemctl edit makeros-hub
# Paste:
[Service]
Environment="MAKEROS_HUB_MODEL_URL=https://<your-mirror>/yolov11-failure-detector.onnx"
Environment="MAKEROS_HUB_MODEL_SHA256=<sha256 from step 5>"

sudo systemctl restart makeros-hub
```

### Option B — every hub (recommended for production)

Hardcode the values in `makeros_hub/printers/onnx_detector.py` (`MODEL_URL`
+ `MODEL_SHA256` constants) and ship as the agent's next release. Every OTA
update pulls the new constants automatically.

## Verify

After restart, `journalctl -u makeros-hub | grep detector`:

- **Working**: `detector: model downloaded to /var/lib/makeros-hub/models/<sha>.onnx` → `detector: ready (model=...)`
- **Stub fallback** (MODEL_URL empty): `detector: MODEL_URL/MODEL_SHA256 unset; falling back to stub`
- **Stub fallback** (deps missing): `detector: onnxruntime/Pillow/numpy not in venv; falling back to stub`
- **Download failed**: `detector: model download/verify failed; falling back to stub` — check URL, sha, Pi internet

In `/admin/3dprinting/failure-watch`, you should start seeing samples with
non-zero smoothed-p values during an active print on a camera-enabled
printer (vs all-0 with the stub). Tune the workspace sensitivity preset
(low/medium/high) once enough samples are recorded to calibrate.

## Performance

| Pi 5 (4 cores @ 2.4 GHz) | Expected latency / frame |
|---|---|
| YOLOv11s FP32 (default) | ~300-500 ms |
| YOLOv11s INT8 (quantized) | ~150 ms |

INT8 quantization is a follow-up — `onnxruntime.quantization.quantize_dynamic`
on the exported `.onnx` produces a smaller, faster model with negligible
accuracy loss for our scalar use case.

The agent runs inference at the camera-capture cadence (every 30s while
printing), so even 500ms is well under the heartbeat budget.

## Caching

The agent caches the downloaded `.onnx` at
`/var/lib/makeros-hub/models/<sha256>.onnx`. The cache:

- Survives venv recreation on OTA (state vs code separation).
- Is content-addressed by sha — a future model upgrade gets a new path,
  the old file can be GC'd by hand or left in place.
- Self-heals: a corrupted/tampered cached file is detected on boot
  (sha mismatch) and re-downloaded.

## Soft-deactivate

To disable inference without rolling back the agent, clear `MODEL_URL`
(empty string) and restart — the framework reverts to stub mode and
samples land as p=0. The workspace can also flip the
`print.ai_failure_watch.enabled` feature off on the cloud side, which
drops all incoming samples regardless of what the agent is sending.
