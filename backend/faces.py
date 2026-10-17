"""CPU face pipeline using OpenCV's built-in YuNet detector + SFace recognizer.

Chosen for a modest, GPU-less NAS: both ship inside opencv-contrib and run fine on
CPU, the model files are tiny (YuNet ~345 KB, SFace ~37 MB), and there's no heavy
dependency tree (no onnxruntime / insightface / scipy build). SFace yields a 128-d
embedding; we L2-normalise it so cosine similarity is a dot product. OpenCV's own
guidance puts the "same person" cosine threshold around 0.363.

Models are fetched once to the persistent /config volume on first use.
"""
from __future__ import annotations

import os
import threading
import urllib.request
from pathlib import Path

import cv2
import numpy as np

# Cap OpenCV's CPU threads so a background face scan can't saturate every core and
# starve interactive recording playback (transcode) on a modest NAS.
try:
    cv2.setNumThreads(max(1, (os.cpu_count() or 2) // 2))
except Exception:
    pass

MODELS_DIR = Path(os.environ.get("FACE_MODELS_DIR", "/config/faces/models"))
YUNET_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/"
             "face_detection_yunet/face_detection_yunet_2023mar.onnx")
SFACE_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/"
             "face_recognition_sface/face_recognition_sface_2021dec.onnx")

_lock = threading.Lock()
_detector = None
_recognizer = None


def _fetch(url: str, dest: Path) -> Path:
    if not dest.exists() or dest.stat().st_size == 0:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        urllib.request.urlretrieve(url, tmp)
        tmp.replace(dest)
    return dest


def _load():
    global _detector, _recognizer
    if _detector is None or _recognizer is None:
        with _lock:
            if _detector is None:
                y = _fetch(YUNET_URL, MODELS_DIR / "yunet.onnx")
                _detector = cv2.FaceDetectorYN.create(str(y), "", (320, 320),
                                                      score_threshold=0.7)
            if _recognizer is None:
                s = _fetch(SFACE_URL, MODELS_DIR / "sface.onnx")
                _recognizer = cv2.FaceRecognizerSF.create(str(s), "")
    return _detector, _recognizer


def warm() -> bool:
    _load()
    return True


def detect(img_bgr: "np.ndarray", min_score: float = 0.75, det_max: int = 1280) -> list[dict]:
    """Return [{bbox:[x1,y1,x2,y2], score, embedding(np.float32[128])}] for an image.

    YuNet is unreliable on very large frames (it misses faces at 4K), so we run
    detection on a copy scaled down to `det_max` on the long side, then map the
    boxes + landmarks back to full resolution and align/embed on the original for
    a crisp crop and a good-quality embedding.
    """
    det, rec = _load()
    h, w = img_bgr.shape[:2]
    scale = min(1.0, det_max / max(h, w))
    small = cv2.resize(img_bgr, (round(w * scale), round(h * scale))) if scale < 1.0 else img_bgr
    sh, sw = small.shape[:2]
    det.setInputSize((sw, sh))
    _, faces = det.detect(small)
    out: list[dict] = []
    if faces is None:
        return out
    for row in faces:
        score = float(row[-1])
        if score < min_score:
            continue
        full = row.copy()
        full[0:14] = row[0:14] / scale          # bbox(4) + 5 landmarks(10) -> full res
        aligned = rec.alignCrop(img_bgr, full)
        feat = rec.feature(aligned).flatten().astype(np.float32)
        n = np.linalg.norm(feat)
        if n == 0:
            continue
        feat = feat / n
        x, y, bw, bh = full[0], full[1], full[2], full[3]
        out.append({
            "bbox": [int(x), int(y), int(x + bw), int(y + bh)],
            "score": score,
            "embedding": feat,
        })
    return out
