"""Person detection for the Visitors view (CPU, OpenCV DNN, no extra deps).

YOLOX-s from opencv_zoo (~35 MB, downloaded once into the same models dir as
the face models). Only COCO class 0 ("person") is used. One 640x640 inference
per frame, a fraction of a second on the NAS CPU, so a few frames per motion
event is cheap. Unlike face detection it finds people from behind, in profile,
under a cap or carrying a parcel, which is what "who came by" needs.
"""
from __future__ import annotations

import threading
import urllib.request
from pathlib import Path

import cv2
import numpy as np

from faces import MODELS_DIR

YOLOX_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/"
             "object_detection_yolox/object_detection_yolox_2022nov.onnx")
SIZE = 640
STRIDES = (8, 16, 32)

_net = None
_grids = None
_lock = threading.Lock()


def _load():
    global _net, _grids
    with _lock:
        if _net is None:
            p = MODELS_DIR / "yolox.onnx"
            if not p.exists() or p.stat().st_size < 1_000_000:
                MODELS_DIR.mkdir(parents=True, exist_ok=True)
                tmp = p.with_suffix(".part")
                urllib.request.urlretrieve(YOLOX_URL, tmp)
                tmp.replace(p)
            _net = cv2.dnn.readNet(str(p))
            grids, strides = [], []
            for s in STRIDES:
                n = SIZE // s
                xv, yv = np.meshgrid(np.arange(n), np.arange(n))
                grids.append(np.stack((xv, yv), 2).reshape(-1, 2))
                strides.append(np.full((n * n, 1), s))
            _grids = (np.concatenate(grids).astype(np.float32), np.concatenate(strides).astype(np.float32))
    return _net, _grids


def warm() -> bool:
    _load()
    return True


def detect(img_bgr: np.ndarray, min_score: float = 0.5) -> list[dict]:
    """[{bbox:[x1,y1,x2,y2], score}] for every person in a BGR image."""
    net, (grid, stride) = _load()
    h, w = img_bgr.shape[:2]
    r = min(SIZE / h, SIZE / w)
    pad = np.full((SIZE, SIZE, 3), 114, np.float32)
    pad[: int(h * r), : int(w * r)] = cv2.resize(img_bgr, (int(w * r), int(h * r)))
    with _lock:   # one inference at a time; cv2.dnn nets aren't thread-safe
        net.setInput(pad.transpose(2, 0, 1)[None])
        out = net.forward(net.getUnconnectedOutLayersNames())[0][0]
    xy = (out[:, :2] + grid) * stride
    wh = np.exp(out[:, 2:4]) * stride
    score = out[:, 4] * out[:, 5]          # objectness x P(person)
    keep = score >= min_score
    if not keep.any():
        return []
    xy, wh, score = xy[keep], wh[keep], score[keep]
    boxes = np.concatenate([xy - wh / 2, wh], 1) / r      # x, y, w, h in the original
    idx = cv2.dnn.NMSBoxes(boxes.tolist(), score.tolist(), min_score, 0.45)
    res = []
    for i in np.array(idx).flatten():
        x, y, bw, bh = boxes[i]
        res.append({"bbox": [max(0, int(x)), max(0, int(y)), min(w, int(x + bw)), min(h, int(y + bh))],
                    "score": float(score[i])})
    return res
