"""Synthetic cadastral-style test maps with known ground-truth parcels."""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

import cv2
import numpy as np
from shapely import affinity
from shapely.geometry import Polygon


def blank(w: int, h: int, bg: int = 255) -> np.ndarray:
    return np.full((h, w, 3), bg, np.uint8)


def grid_map(rows: int = 3, cols: int = 4, cell: int = 180, margin: int = 60,
             line_w: int = 3, ink: int = 0, bg: int = 255, labels: bool = True
             ) -> Tuple[np.ndarray, List[Polygon]]:
    """Rectangular parcels sharing boundaries inside a closed frame."""
    w, h = cols * cell + 2 * margin, rows * cell + 2 * margin
    img = blank(w, h, bg)
    xs = [margin + i * cell for i in range(cols + 1)]
    ys = [margin + j * cell for j in range(rows + 1)]
    for x in xs:
        cv2.line(img, (x, ys[0]), (x, ys[-1]), (ink,) * 3, line_w)
    for y in ys:
        cv2.line(img, (xs[0], y), (xs[-1], y), (ink,) * 3, line_w)
    truth = []
    for j in range(rows):
        for i in range(cols):
            box = Polygon([(xs[i] + .5, ys[j] + .5), (xs[i + 1] + .5, ys[j] + .5),
                           (xs[i + 1] + .5, ys[j + 1] + .5), (xs[i] + .5, ys[j + 1] + .5)])
            truth.append(box)
            if labels:
                cv2.putText(img, str(j * cols + i + 1), (xs[i] + cell // 3, ys[j] + cell // 2),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.9, (ink,) * 3, 2, cv2.LINE_AA)
    return img, truth


def irregular_map(rows: int = 3, cols: int = 4, cell: int = 170, margin: int = 70,
                  jitter: int = 28, line_w: int = 3, seed: int = 3, ink: int = 0,
                  bg: int = 255, labels: bool = True) -> Tuple[np.ndarray, List[Polygon]]:
    """Quadrilateral parcels from a jittered lattice (non-rectangular, shared edges)."""
    rng = np.random.default_rng(seed)
    w, h = cols * cell + 2 * margin, rows * cell + 2 * margin
    img = blank(w, h, bg)
    pts = np.zeros((rows + 1, cols + 1, 2), int)
    for j in range(rows + 1):
        for i in range(cols + 1):
            jx = 0 if i in (0, cols) else rng.integers(-jitter, jitter + 1)
            jy = 0 if j in (0, rows) else rng.integers(-jitter, jitter + 1)
            pts[j, i] = (margin + i * cell + jx, margin + j * cell + jy)
    for j in range(rows + 1):
        for i in range(cols):
            cv2.line(img, tuple(pts[j, i]), tuple(pts[j, i + 1]), (ink,) * 3, line_w)
    for j in range(rows):
        for i in range(cols + 1):
            cv2.line(img, tuple(pts[j, i]), tuple(pts[j + 1, i]), (ink,) * 3, line_w)
    truth = []
    for j in range(rows):
        for i in range(cols):
            quad = [pts[j, i], pts[j, i + 1], pts[j + 1, i + 1], pts[j + 1, i]]
            poly = affinity.translate(Polygon([tuple(map(float, q)) for q in quad]), 0.5, 0.5)
            truth.append(poly)
            if labels:
                c = poly.centroid
                cv2.putText(img, str(j * cols + i + 1), (int(c.x) - 12, int(c.y) + 8),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (ink,) * 3, 2, cv2.LINE_AA)
    return img, truth


def make_noisy(img: np.ndarray, sigma: float = 8.0, speckle: float = 0.002,
               seed: int = 1, blur: bool = True) -> np.ndarray:
    """Simulate a faint, noisy scan."""
    rng = np.random.default_rng(seed)
    out = img.astype(np.float32)
    out += rng.normal(0, sigma, out.shape[:2])[..., None]
    mask = rng.random(out.shape[:2])
    out[mask < speckle / 2] = 0
    out[mask > 1 - speckle / 2] = 255
    out = np.clip(out, 0, 255).astype(np.uint8)
    if blur:
        out = cv2.GaussianBlur(out, (3, 3), 0)
    return out


def match_ious(detected: Sequence[Polygon], truth: Sequence[Polygon]) -> List[float]:
    """Best IoU achieved for each ground-truth polygon."""
    res = []
    for t in truth:
        best = 0.0
        for d in detected:
            if not d.intersects(t):
                continue
            inter = d.intersection(t).area
            best = max(best, inter / (d.area + t.area - inter))
        res.append(best)
    return res
