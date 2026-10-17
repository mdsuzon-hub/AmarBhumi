"""Loading scanned maps from PDF (PyMuPDF) or raster images (PNG/JPEG/TIFF).

Images are returned as read-only RGB uint8 arrays (H, W, 3). They are marked read-only so
that no part of the pipeline can modify the original source pixels.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import List, Optional, Tuple

import cv2
import numpy as np

try:  # PyMuPDF >= 1.24 exposes `pymupdf`; older versions only `fitz`
    import pymupdf as fitz  # type: ignore
except ImportError:  # pragma: no cover
    import fitz  # type: ignore

from core.progress import Progress

log = logging.getLogger(__name__)

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".tif", ".tiff")
PDF_EXTENSIONS = (".pdf",)
MAX_SOURCE_PIXELS = 400_000_000   # hard refusal (memory)
WARN_SOURCE_PIXELS = 120_000_000  # GUI warns above this


class SourceLoadError(Exception):
    """Raised with a user-readable message when a PDF page or image cannot be loaded."""


@dataclass(frozen=True)
class PdfInfo:
    path: str
    page_count: int
    page_sizes_pt: Tuple[Tuple[float, float], ...]


def is_pdf(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in PDF_EXTENSIONS


def is_image(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in IMAGE_EXTENSIONS


def open_pdf_info(path: str) -> PdfInfo:
    """Read page count and page sizes (points) without rendering."""
    try:
        with fitz.open(path) as doc:
            if doc.needs_pass:
                raise SourceLoadError("The PDF is password protected.")
            sizes = tuple((float(p.rect.width), float(p.rect.height)) for p in doc)
    except SourceLoadError:
        raise
    except Exception as exc:
        raise SourceLoadError(f"Cannot open PDF '{path}': {exc}") from exc
    if not sizes:
        raise SourceLoadError("The PDF contains no pages.")
    return PdfInfo(path, len(sizes), sizes)


def rendered_size(info: PdfInfo, page_number: int, dpi: int) -> Tuple[int, int]:
    """Pixel size (w, h) a page would have at `dpi`. page_number is 1-based."""
    w, h = info.page_sizes_pt[page_number - 1]
    return int(round(w * dpi / 72.0)), int(round(h * dpi / 72.0))


def _readonly(arr: np.ndarray) -> np.ndarray:
    arr.flags.writeable = False
    return arr


def render_pdf_page(path: str, page_number: int, dpi: int = 300,
                    progress: Optional[Progress] = None) -> np.ndarray:
    """Render a 1-based PDF page to an RGB array at the requested DPI."""
    progress = progress or Progress()
    if not 72 <= dpi <= 1200:
        raise SourceLoadError(f"DPI {dpi} is outside the supported range 72-1200.")
    try:
        progress.report(5, "Opening PDF")
        with fitz.open(path) as doc:
            if not 1 <= page_number <= len(doc):
                raise SourceLoadError(f"Page {page_number} does not exist (PDF has {len(doc)} pages).")
            page = doc[page_number - 1]
            w, h = int(page.rect.width * dpi / 72), int(page.rect.height * dpi / 72)
            if w * h > MAX_SOURCE_PIXELS:
                raise SourceLoadError(
                    f"Rendering at {dpi} DPI would create a {w}x{h} image "
                    f"({w * h / 1e6:.0f} MP). Choose a lower DPI.")
            progress.report(20, f"Rendering page {page_number} at {dpi} DPI")
            zoom = dpi / 72.0
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csRGB,
                                  alpha=False, annots=False)
            arr = np.frombuffer(pix.samples, dtype=np.uint8)
            if pix.stride != pix.width * 3:
                arr = arr.reshape(pix.height, pix.stride)[:, : pix.width * 3]
            arr = arr.reshape(pix.height, pix.width, 3)
    except SourceLoadError:
        raise
    except MemoryError as exc:
        raise SourceLoadError("Not enough memory to render this page; try a lower DPI.") from exc
    except Exception as exc:
        raise SourceLoadError(f"Cannot render page {page_number} of '{os.path.basename(path)}': {exc}") from exc
    progress.report(100, "Page rendered")
    return _readonly(np.ascontiguousarray(arr))


def render_pdf_thumbnail(path: str, page_number: int, max_dim: int = 420) -> np.ndarray:
    """Small RGB preview of a PDF page."""
    with fitz.open(path) as doc:
        page = doc[page_number - 1]
        zoom = max_dim / max(page.rect.width, page.rect.height)
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csRGB, alpha=False)
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.stride)[:, : pix.width * 3]
        return np.ascontiguousarray(arr.reshape(pix.height, pix.width, 3))


def load_image_file(path: str, progress: Optional[Progress] = None) -> np.ndarray:
    """Load PNG/JPEG/TIFF as RGB (first frame only for multi-page TIFF)."""
    progress = progress or Progress()
    progress.report(10, "Reading image")
    if not os.path.isfile(path):
        raise SourceLoadError(f"File not found: {path}")
    img = cv2.imread(path, cv2.IMREAD_COLOR)
    if img is None:
        raise SourceLoadError(f"Cannot decode image '{os.path.basename(path)}' (unsupported or corrupt).")
    h, w = img.shape[:2]
    if w * h > MAX_SOURCE_PIXELS:
        raise SourceLoadError(f"Image is {w}x{h} ({w * h / 1e6:.0f} MP) - too large to process.")
    cv2.cvtColor(img, cv2.COLOR_BGR2RGB, dst=img)  # in place: no second full-size buffer
    progress.report(100, "Image loaded")
    return _readonly(img)


def load_source(path: str, page_number: int = 1, dpi: int = 300,
                progress: Optional[Progress] = None) -> np.ndarray:
    """Load a PDF page or image file according to the file extension."""
    if is_pdf(path):
        return render_pdf_page(path, page_number, dpi, progress)
    if is_image(path):
        return load_image_file(path, progress)
    raise SourceLoadError(f"Unsupported file type: {os.path.splitext(path)[1] or path}")
