"""Boundary (ink) detection: grayscale -> threshold -> clean-up -> gap closing.

All size parameters are given in working-image pixels. For very large images the whole
detection runs on a downscaled copy (see DetectionParams.max_detect_pixels) and the sizes
are scaled accordingly; polygon coordinates are scaled back by polygon_extraction.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Tuple

import cv2
import numpy as np

from core.progress import Progress
from core.settings import DetectionParams

log = logging.getLogger(__name__)


@dataclass
class DetectionResult:
    """Binary products of the detection stage (255 = ink/boundary), at detection scale."""

    binary: np.ndarray          # thresholded + de-noised ink
    boundaries: np.ndarray      # after line filter and gap closing (used for region finding)
    scale_x: float              # detection_px = working_px * scale_x
    scale_y: float
    image_shape: Tuple[int, int]  # (h, w) of the working image

    def _full(self, arr: np.ndarray) -> np.ndarray:
        h, w = self.image_shape
        if arr.shape[:2] == (h, w):
            return arr
        return cv2.resize(arr, (w, h), interpolation=cv2.INTER_NEAREST)

    def binary_full(self) -> np.ndarray:
        """Thresholded ink at working-image size (for display)."""
        return self._full(self.binary)

    def boundaries_full(self) -> np.ndarray:
        """Closed boundary mask at working-image size (for display)."""
        return self._full(self.boundaries)


def _odd(n: float, minimum: int = 1) -> int:
    n = max(minimum, int(round(n)))
    return n if n % 2 == 1 else n + 1


def detection_scale(h: int, w: int, max_pixels: int) -> float:
    return 1.0 if h * w <= max_pixels else float(np.sqrt(max_pixels / float(h * w)))


def to_gray(rgb: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)


def enhance_gray(gray: np.ndarray, p: DetectionParams) -> np.ndarray:
    """Contrast/brightness, optional CLAHE and Gaussian blur."""
    out = gray
    if p.contrast_alpha != 1.0 or p.brightness_beta != 0:
        out = cv2.convertScaleAbs(out, alpha=p.contrast_alpha, beta=p.brightness_beta)
    if p.clahe:
        out = cv2.createCLAHE(clipLimit=max(0.1, p.clahe_clip), tileGridSize=(8, 8)).apply(out)
    if p.blur_ksize > 1:
        k = _odd(p.blur_ksize)
        out = cv2.GaussianBlur(out, (k, k), 0)
    return out


def binarize(gray: np.ndarray, p: DetectionParams, scale: float = 1.0) -> np.ndarray:
    """Ink = 255. Adaptive (local) or Otsu (global) thresholding."""
    if p.threshold_method == "otsu":
        _, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
        return ink
    block = _odd(p.adaptive_block_size * scale, 3)
    return cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                 cv2.THRESH_BINARY_INV, block, p.adaptive_c)


def remove_small_components(ink: np.ndarray, min_area: float) -> np.ndarray:
    """Delete ink blobs below `min_area` px² (scan dust, speckle)."""
    if min_area <= 1:
        return ink
    n, labels, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    keep = stats[:, cv2.CC_STAT_AREA] >= min_area
    keep[0] = False
    return (keep[labels].astype(np.uint8)) * 255


def keep_line_components(ink: np.ndarray, min_length: int) -> np.ndarray:
    """Keep only ink components that contain a long straight stroke.

    Oriented morphological openings (0/45/90/135 degrees) act as line detectors: whatever
    survives an opening with a `min_length` line element is a stroke at least that long.
    Components without such a stroke (digits, hatching, handwriting) are dropped.
    """
    L = max(3, int(min_length))
    seeds = np.zeros_like(ink)
    horiz = cv2.getStructuringElement(cv2.MORPH_RECT, (L, 1))
    vert = cv2.getStructuringElement(cv2.MORPH_RECT, (1, L))
    d = max(3, int(L * 0.7))
    diag1 = np.eye(d, dtype=np.uint8)
    diag2 = np.fliplr(diag1).copy()
    for kernel in (horiz, vert, diag1, diag2):
        seeds |= cv2.morphologyEx(ink, cv2.MORPH_OPEN, kernel)
    n, labels = cv2.connectedComponents(ink, connectivity=8)
    keep = np.zeros(n, dtype=bool)
    keep[np.unique(labels[seeds > 0])] = True
    keep[0] = False
    return (keep[labels].astype(np.uint8)) * 255


def remove_small_extent_components(ink: np.ndarray, max_extent: float) -> np.ndarray:
    """Delete ink clusters whose bounding box is smaller than `max_extent` px in both
    directions - plot numbers, symbols and other text that is not connected to a boundary.
    (Their enclosed loops, e.g. inside 0, 6, 8, 9, would otherwise become tiny false parcels.)"""
    if max_extent <= 1:
        return ink
    n, labels, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    extent = np.maximum(stats[:, cv2.CC_STAT_WIDTH], stats[:, cv2.CC_STAT_HEIGHT])
    keep = extent >= max_extent
    keep[0] = False
    return (keep[labels].astype(np.uint8)) * 255


def close_gaps(ink: np.ndarray, ksize: float, iterations: int) -> np.ndarray:
    """Morphological closing with an elliptical kernel to reconnect broken boundaries."""
    k = int(round(ksize))
    if k <= 1 or iterations < 1:
        return ink
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    return cv2.morphologyEx(ink, cv2.MORPH_CLOSE, kernel, iterations=iterations)


def detect_boundaries(rgb: np.ndarray, params: DetectionParams,
                      progress: Optional[Progress] = None) -> DetectionResult:
    """Run the photometric/morphological part of the pipeline on a working image."""
    p = progress or Progress()
    h, w = rgb.shape[:2]
    scale = detection_scale(h, w, params.max_detect_pixels)

    p.report(2, "Converting to grayscale")
    gray = to_gray(rgb)
    sx = sy = 1.0
    if scale < 1.0:
        nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
        log.info("Detection runs on a downscaled %dx%d copy of the %dx%d image", nw, nh, w, h)
        gray = cv2.resize(gray, (nw, nh), interpolation=cv2.INTER_AREA)
        sx, sy = nw / w, nh / h
    s = (sx + sy) / 2.0

    p.report(10, "Enhancing contrast")
    gray = enhance_gray(gray, params)

    p.report(25, "Thresholding")
    ink = binarize(gray, params, s)
    del gray

    p.report(45, "Removing noise")
    ink = remove_small_components(ink, params.min_noise_area * s * s)
    binary = ink

    if params.line_filter:
        p.report(60, "Detecting boundary lines")
        ink = keep_line_components(ink, max(3, params.line_min_length * s))

    p.report(80, "Closing gaps in boundaries")
    boundaries = close_gaps(ink, params.close_ksize * s, params.close_iterations)
    if params.text_max_extent > 0:
        p.report(92, "Removing text and symbols")
        boundaries = remove_small_extent_components(boundaries, params.text_max_extent * s)
    p.report(100, "Boundaries detected")
    return DetectionResult(binary=binary, boundaries=boundaries, scale_x=sx, scale_y=sy,
                           image_shape=(h, w))
