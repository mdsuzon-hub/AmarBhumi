#!/usr/bin/env python3
"""Generate a synthetic mouza-style sample map (PDF + PNG) with a title block, legend and frame.

Usage:  python scripts/make_sample_map.py [output_dir]
Outputs sample_mouza_map.pdf (2 pages: clean + noisy/skewed) and sample_mouza_map.png (page 1).
The data is synthetic - it only imitates the *look* of a cadastral sheet (irregular quadrilateral
plots, shared boundaries, plot numbers, a road, title/legend outside the frame).
"""
from __future__ import annotations

import os
import sys

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz

PAGE_W, PAGE_H = 3508, 2480      # A4 landscape at 300 DPI
FRAME = (260, 330, 2560, 1900)   # x, y, w, h of the map frame on page 1 (page pixels)
ROWS, COLS = 6, 8


def draw_map(seed: int = 7):
    rng = np.random.default_rng(seed)
    img = np.full((PAGE_H, PAGE_W, 3), 244, np.uint8)
    fx, fy, fw, fh = FRAME
    ink = (35, 35, 40)
    cw, ch = fw / COLS, fh / ROWS
    pts = np.zeros((ROWS + 1, COLS + 1, 2), np.int32)
    for j in range(ROWS + 1):
        for i in range(COLS + 1):
            jx = 0 if i in (0, COLS) else rng.integers(-int(cw * .22), int(cw * .22))
            jy = 0 if j in (0, ROWS) else rng.integers(-int(ch * .22), int(ch * .22))
            pts[j, i] = (fx + i * cw + jx, fy + j * ch + jy)
    for j in range(ROWS + 1):
        for i in range(COLS):
            cv2.line(img, tuple(pts[j, i]), tuple(pts[j, i + 1]), ink, 4, cv2.LINE_AA)
    for j in range(ROWS):
        for i in range(COLS + 1):
            cv2.line(img, tuple(pts[j, i]), tuple(pts[j + 1, i]), ink, 4, cv2.LINE_AA)
    truth = {}
    for j in range(ROWS):
        for i in range(COLS):
            quad = np.array([pts[j, i], pts[j, i + 1], pts[j + 1, i + 1], pts[j + 1, i]], float)
            c = quad.mean(axis=0)
            num = j * COLS + i + 1
            truth[num] = quad
            cv2.putText(img, str(num), (int(c[0]) - 30, int(c[1]) + 15), cv2.FONT_HERSHEY_SIMPLEX, 1.6,
                        (20, 20, 120), 3, cv2.LINE_AA)
    # outer frame (double line) and title block / legend outside it
    cv2.rectangle(img, (fx - 25, fy - 25), (fx + fw + 25, fy + fh + 25), ink, 6, cv2.LINE_AA)
    cv2.putText(img, "MOUZA SAMPLE  -  CADASTRAL SHEET 1", (fx, 190), cv2.FONT_HERSHEY_SIMPLEX, 2.4, ink, 5, cv2.LINE_AA)
    cv2.putText(img, "Synthetic test data  -  scale 16 in = 1 mile", (fx, 260), cv2.FONT_HERSHEY_SIMPLEX, 1.4, ink, 3, cv2.LINE_AA)
    lx = fx + fw + 120
    cv2.rectangle(img, (lx, fy), (lx + 520, fy + 640), ink, 4, cv2.LINE_AA)
    cv2.putText(img, "LEGEND", (lx + 150, fy + 70), cv2.FONT_HERSHEY_SIMPLEX, 1.6, ink, 4, cv2.LINE_AA)
    for k, label in enumerate(("Plot boundary", "Plot number", "Frame", "Road (see map)")):
        cv2.putText(img, label, (lx + 40, fy + 170 + k * 100), cv2.FONT_HERSHEY_SIMPLEX, 1.2, ink, 3, cv2.LINE_AA)
    return img, truth


def scan_effects(img, seed=3, sigma=7.0, tilt=0.0):
    rng = np.random.default_rng(seed)
    out = img.astype(np.float32)
    yy, xx = np.mgrid[0:out.shape[0], 0:out.shape[1]]
    out *= (0.93 + 0.07 * np.cos(xx / 1500.0) * np.cos(yy / 1100.0))[..., None]   # uneven lighting
    out += rng.normal(0, sigma, out.shape[:2])[..., None]
    mask = rng.random(out.shape[:2])
    out[mask < 0.0008] = 0
    out = np.clip(out, 0, 255).astype(np.uint8)
    out = cv2.GaussianBlur(out, (3, 3), 0)
    if tilt:
        m = cv2.getRotationMatrix2D((out.shape[1] / 2, out.shape[0] / 2), tilt, 1.0)
        out = cv2.warpAffine(out, m, (out.shape[1], out.shape[0]), borderValue=(238, 238, 238))
    return out


def main() -> None:
    out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "samples")
    os.makedirs(out_dir, exist_ok=True)
    clean, _ = draw_map()
    page1 = scan_effects(clean, sigma=2.0)
    page2 = scan_effects(clean, seed=11, sigma=9.0, tilt=1.4)
    png = os.path.join(out_dir, "sample_mouza_map.png")
    cv2.imwrite(png, page1)
    doc = fitz.open()
    for img, ext in ((page1, ".png"), (page2, ".jpg")):
        ok, buf = cv2.imencode(ext, img, [cv2.IMWRITE_JPEG_QUALITY, 85] if ext == ".jpg" else [])
        page = doc.new_page(width=PAGE_W * 72 / 300, height=PAGE_H * 72 / 300)
        page.insert_image(page.rect, stream=buf.tobytes())
    pdf = os.path.join(out_dir, "sample_mouza_map.pdf")
    doc.save(pdf, deflate=True)
    print("wrote", pdf, "and", png)


if __name__ == "__main__":
    main()
