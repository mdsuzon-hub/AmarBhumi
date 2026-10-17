import os

import cv2
import numpy as np
import pytest

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz

from core.pdf_loader import (SourceLoadError, load_image_file, load_source, open_pdf_info,
                             render_pdf_page, render_pdf_thumbnail, rendered_size)
from tests.synthetic import grid_map


def make_pdf(path, pages=3):
    doc = fitz.open()
    for i in range(pages):
        img, _ = grid_map(rows=1 + i, cols=2, labels=False)
        ok, png = cv2.imencode(".png", img)
        page = doc.new_page(width=300, height=300)     # points
        page.insert_image(page.rect, stream=png.tobytes())
    doc.save(path)
    doc.close()


def test_multi_page_pdf_info_and_page_selection(tmp_path):
    pdf = str(tmp_path / "maps.pdf")
    make_pdf(pdf, 3)
    info = open_pdf_info(pdf)
    assert info.page_count == 3
    assert rendered_size(info, 2, 144) == (600, 600)
    p1, p3 = render_pdf_page(pdf, 1, 144), render_pdf_page(pdf, 3, 144)
    assert p1.shape == (600, 600, 3) and p1.dtype == np.uint8
    assert not np.array_equal(p1, p3), "different pages must render differently"
    thumb = render_pdf_thumbnail(pdf, 2, 120)
    assert max(thumb.shape[:2]) == 120


def test_render_dpi_is_configurable(tmp_path):
    pdf = str(tmp_path / "m.pdf")
    make_pdf(pdf, 1)
    assert render_pdf_page(pdf, 1, 72).shape[:2] == (300, 300)
    assert render_pdf_page(pdf, 1, 300).shape[:2] == (1250, 1250)


def test_rendered_page_is_read_only(tmp_path):
    pdf = str(tmp_path / "m.pdf")
    make_pdf(pdf, 1)
    arr = render_pdf_page(pdf, 1, 100)
    with pytest.raises(ValueError):
        arr[0, 0, 0] = 1


def test_clear_errors(tmp_path):
    pdf = str(tmp_path / "m.pdf")
    make_pdf(pdf, 2)
    with pytest.raises(SourceLoadError, match="Page 5"):
        render_pdf_page(pdf, 5, 100)
    with pytest.raises(SourceLoadError, match="DPI"):
        render_pdf_page(pdf, 1, 20)
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")
    with pytest.raises(SourceLoadError, match="Cannot open PDF"):
        open_pdf_info(str(bad))
    with pytest.raises(SourceLoadError, match="not found"):
        load_image_file(str(tmp_path / "missing.png"))
    junk = tmp_path / "junk.png"
    junk.write_bytes(b"xx")
    with pytest.raises(SourceLoadError, match="Cannot decode"):
        load_image_file(str(junk))
    with pytest.raises(SourceLoadError, match="Unsupported"):
        load_source(str(tmp_path / "a.docx"))


@pytest.mark.parametrize("ext", [".png", ".jpg", ".tif"])
def test_import_png_jpeg_tiff_as_rgb(tmp_path, ext):
    img, _ = grid_map(labels=False)
    img[10:20, 10:20] = (255, 0, 0)         # pure red patch (RGB)
    path = str(tmp_path / f"m{ext}")
    cv2.imwrite(path, cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
    arr = load_image_file(path)
    assert arr.shape == img.shape
    assert arr[15, 15, 0] > 200 and arr[15, 15, 2] < 60, "channels must be RGB"
    assert not arr.flags.writeable
