"""Geometric image preparation: deskew estimation, rotation and cropping.

The source array is never modified. `apply_prep` returns either the source itself, a
cropped *view* of it (no copy) or a freshly warped array, together with the 3x3 matrix
that maps source pixel coordinates to working-image pixel coordinates, so the relationship
between polygons and the original image is always recoverable.
"""
from __future__ import annotations

import math
from typing import Optional, Tuple

import cv2
import numpy as np

from core.progress import Progress
from core.settings import PrepParams

Matrix3 = np.ndarray


def rotation_canvas(shape_hw: Tuple[int, int], angle_deg: float) -> Tuple[Matrix3, Tuple[int, int]]:
    """Matrix (3x3) rotating about the image centre onto an expanded canvas, and its (w, h)."""
    h, w = shape_hw
    if abs(angle_deg) < 1e-9:
        return np.eye(3), (w, h)
    c = (w / 2.0, h / 2.0)
    m = cv2.getRotationMatrix2D(c, angle_deg, 1.0)
    cos, sin = abs(m[0, 0]), abs(m[0, 1])
    nw = int(math.ceil(h * sin + w * cos))
    nh = int(math.ceil(h * cos + w * sin))
    m[0, 2] += nw / 2.0 - c[0]
    m[1, 2] += nh / 2.0 - c[1]
    return np.vstack([m, [0.0, 0.0, 1.0]]), (nw, nh)


def clamp_crop(crop: Optional[Tuple[int, int, int, int]], canvas_wh: Tuple[int, int]
               ) -> Tuple[int, int, int, int]:
    cw, ch = canvas_wh
    if crop is None:
        return 0, 0, cw, ch
    x, y, w, h = crop
    x0, y0 = max(0, min(cw - 1, int(x))), max(0, min(ch - 1, int(y)))
    x1, y1 = max(x0 + 1, min(cw, int(x + w))), max(y0 + 1, min(ch, int(y + h)))
    return x0, y0, x1 - x0, y1 - y0


def build_transform(src_shape_hw: Tuple[int, int], prep: PrepParams
                    ) -> Tuple[Matrix3, Tuple[int, int]]:
    """3x3 matrix source->working and the working image size (w, h)."""
    rot, canvas = rotation_canvas(src_shape_hw, prep.rotation_deg)
    x, y, w, h = clamp_crop(prep.crop, canvas)
    shift = np.array([[1.0, 0.0, -x], [0.0, 1.0, -y], [0.0, 0.0, 1.0]])
    return shift @ rot, (w, h)


def apply_prep(src: np.ndarray, prep: PrepParams, progress: Optional[Progress] = None
               ) -> Tuple[np.ndarray, Matrix3]:
    """Rotate then crop. Returns (working_rgb, source_to_working_3x3)."""
    progress = progress or Progress()
    matrix, (w, h) = build_transform(src.shape[:2], prep)
    if abs(prep.rotation_deg) < 1e-9:
        progress.report(100, "Cropping")
        x, y = int(round(-matrix[0, 2])), int(round(-matrix[1, 2]))
        if (x, y, w, h) == (0, 0, src.shape[1], src.shape[0]):
            return src, matrix
        return src[y:y + h, x:x + w], matrix  # view, no copy
    progress.report(30, "Rotating")
    out = cv2.warpAffine(src, matrix[:2], (w, h), flags=cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255))
    progress.report(100, "Rotated")
    out.flags.writeable = False
    return out, matrix


def make_thumbnail(rgb: np.ndarray, max_dim: int = 1600) -> Tuple[np.ndarray, float]:
    """Downscaled copy and the scale factor (thumb = source * scale)."""
    h, w = rgb.shape[:2]
    scale = min(1.0, max_dim / float(max(h, w)))
    if scale >= 1.0:
        return np.ascontiguousarray(rgb), 1.0
    thumb = cv2.resize(rgb, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
    return thumb, scale


def rotated_preview(thumb: np.ndarray, angle_deg: float) -> Tuple[np.ndarray, Matrix3]:
    """Rotate a thumbnail with the exact same canvas rule as the full-size pipeline."""
    m, (w, h) = rotation_canvas(thumb.shape[:2], angle_deg)
    if abs(angle_deg) < 1e-9:
        return thumb, m
    out = cv2.warpAffine(thumb, m[:2], (w, h), flags=cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255))
    return out, m


def estimate_skew_angle(rgb: np.ndarray, max_dim: int = 1600) -> float:
    """Estimate the rotation (degrees, counter-clockwise, OpenCV convention) that makes the
    dominant lines horizontal/vertical. Returns 0.0 if no reliable estimate is possible.

    Uses Hough line segments on the thresholded drawing; angles are folded into
    (-45, 45] and a length-weighted median is taken.
    """
    thumb, _ = make_thumbnail(rgb, max_dim)
    gray = cv2.cvtColor(thumb, cv2.COLOR_RGB2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    _, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    min_len = int(0.12 * max(thumb.shape[:2]))
    lines = cv2.HoughLinesP(ink, 1, np.pi / 720, threshold=80, minLineLength=min_len, maxLineGap=8)
    if lines is None or len(lines) < 3:
        return 0.0
    l = np.asarray(lines).reshape(-1, 4).astype(float)   # (N,1,4) in OpenCV 4, (N,4) in OpenCV 5
    dx, dy = l[:, 2] - l[:, 0], l[:, 3] - l[:, 1]
    angles = np.degrees(np.arctan2(dy, dx))
    angles = (angles + 45.0) % 90.0 - 45.0
    weights = np.hypot(dx, dy)
    order = np.argsort(angles)
    cum = np.cumsum(weights[order])
    median = float(angles[order][np.searchsorted(cum, cum[-1] / 2.0)])
    keep = np.abs(angles - median) <= 3.0
    if keep.sum() < 3:
        return 0.0
    est = float(np.average(angles[keep], weights=weights[keep]))
    return round(est, 2)
