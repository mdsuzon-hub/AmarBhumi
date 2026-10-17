"""Polygon extraction on synthetic maps with known ground truth."""
import cv2
import numpy as np
import pytest
import shapely
from shapely.geometry import Polygon

from core.pipeline import run_detection
from core.polygon_editing import ParcelStore
from core.settings import DetectionParams, ValidationParams
from tests.synthetic import blank, grid_map, irregular_map, make_noisy, match_ious


def detect(img, **overrides):
    params = DetectionParams(**overrides)
    res = run_detection(img, params, ValidationParams())
    return [g for g, _ in res.items], res


def cut_vertical(img, x, y_start, length):
    """Erase exactly `length` rows of a vertical boundary (creates a gap of that size)."""
    img[y_start:y_start + length, x - 4:x + 5] = 255


def assert_matches(polys, truth, min_iou=0.95):
    ious = match_ious(polys, truth)
    assert min(ious) >= min_iou, f"worst IoU {min(ious):.3f}"


def test_clean_rectangular_grid():
    img, truth = grid_map()
    polys, res = detect(img)
    assert len(polys) == len(truth) == 12
    assert_matches(polys, truth, 0.98)
    assert not res.warnings


def test_irregular_quadrilaterals():
    img, truth = irregular_map()
    polys, _ = detect(img)
    assert len(polys) == len(truth)
    assert_matches(polys, truth, 0.97)
    assert all(len(g.exterior.coords) - 1 <= 12 for g in polys), "simplification should keep shapes light"


def test_adjacent_parcels_share_boundaries_without_gaps_or_overlaps():
    img, _ = irregular_map(rows=4, cols=5)
    polys, _ = detect(img)
    total = sum(g.area for g in polys)
    union = shapely.union_all(polys)
    assert union.area == pytest.approx(total, abs=1e-6), "polygons overlap"
    assert isinstance(union, Polygon), "gaps between neighbours (union is not one solid piece)"


def test_faint_noisy_scan():
    img, truth = grid_map(bg=235, ink=150)
    noisy = make_noisy(img, sigma=8, speckle=0.003)
    polys, _ = detect(noisy)
    assert len(polys) == len(truth)
    assert_matches(polys, truth, 0.97)


def test_otsu_on_clean_scan():
    img, truth = grid_map()
    polys, _ = detect(img, threshold_method="otsu")
    assert len(polys) == len(truth)


def test_small_gap_is_closed_by_morphology():
    img, truth = grid_map(labels=False)
    x = 60 + 2 * 180
    cut_vertical(img, x, 60 + 180 + 80, 4)
    polys, _ = detect(img, close_ksize=7)
    assert len(polys) == len(truth)
    polys_nc, _ = detect(img, close_ksize=1)
    assert len(polys_nc) == len(truth) - 1, "without closing the two parcels leak together"


def test_wider_gap_needs_bigger_kernel():
    img, truth = grid_map(labels=False)
    x = 60 + 2 * 180
    y0 = 60 + 180
    cut_vertical(img, x, y0 + 70, 12)
    assert len(detect(img, close_ksize=5)[0]) == len(truth) - 1
    assert len(detect(img, close_ksize=15)[0]) == len(truth)


def test_narrow_strip_parcels_are_found():
    img = blank(520, 300)
    for x in (40, 480):
        cv2.line(img, (x, 40), (x, 260), (0, 0, 0), 3)
    for y in (40, 260):
        cv2.line(img, (40, y), (480, y), (0, 0, 0), 3)
    cv2.line(img, (60, 40), (60, 260), (0, 0, 0), 3)          # 20 px strip at left
    cv2.line(img, (300, 40), (300, 260), (0, 0, 0), 3)
    polys, _ = detect(img, max_area_fraction=0.6)
    assert len(polys) == 3
    widths = sorted(g.bounds[2] - g.bounds[0] for g in polys)
    assert widths[0] == pytest.approx(20, abs=2)


def test_open_boundary_merges_and_split_tool_repairs_it():
    img, truth = grid_map(labels=False)
    x, y0 = 60 + 2 * 180, 60 + 180
    cut_vertical(img, x, y0 + 40, 100)
    polys, _ = detect(img)
    assert len(polys) == len(truth) - 1                              # two parcels leaked into one
    store = ParcelStore()
    store.replace_all([(g, {}) for g in polys])
    merged = max(store.parcels, key=lambda p: p.pixel_area)
    pieces = store.split(merged.parcel_id, [(x + 0.5, y0 - 20), (x + 0.5, y0 + 180 + 20)])
    assert len(pieces) == 2 and len(store) == len(truth)
    assert_matches([p.geometry for p in store.parcels], truth, 0.95)


def test_border_modes():
    img, truth = grid_map(labels=False)
    cropped = np.ascontiguousarray(img[:, 150:])      # first column of parcels is now open to the edge
    polys, _ = detect(cropped, border_mode="exclude_touching")
    assert len(polys) == len(truth) - 3               # they leak into the outside margin
    polys2, _ = detect(cropped, border_mode="close_border", max_area_fraction=0.2)
    assert len(polys2) == len(truth)                  # cut parcels are kept, closed by the image edge


def test_no_regions_gives_warning_not_crash():
    polys, res = detect(blank(200, 200))
    assert polys == [] and res.warnings


def test_downscaled_detection_scales_polygons_back():
    img, truth = grid_map(rows=2, cols=3, cell=300, line_w=6)
    polys, _ = detect(img, max_detect_pixels=120_000, close_ksize=5, min_parcel_area=500)
    assert len(polys) == len(truth)
    assert_matches(polys, truth, 0.95)
    h, w = img.shape[:2]
    assert max(g.bounds[2] for g in polys) > 0.8 * w, "coordinates must be in full-size pixels"


def test_line_filter_keeps_boundaries_and_drops_text():
    img, truth = grid_map()
    polys, _ = detect(img, line_filter=True, line_min_length=60)
    assert len(polys) == len(truth)
    assert_matches(polys, truth, 0.97)


def test_text_digits_do_not_create_parcels_or_holes():
    img, truth = grid_map(labels=True)
    polys, _ = detect(img)
    assert len(polys) == len(truth)
    assert all(len(g.interiors) == 0 for g in polys)


def test_large_digits_with_big_counters_are_not_parcels():
    img, truth = grid_map(labels=False)
    for k, (x, y) in enumerate([(90, 90), (300, 100), (480, 320), (110, 300)]):
        cv2.putText(img, "08" if k % 2 else "69", (x, y + 90), cv2.FONT_HERSHEY_SIMPLEX, 2.2, (0, 0, 0), 4, cv2.LINE_AA)
    polys, _ = detect(img, text_max_extent=0, min_parcel_area=100)
    assert len(polys) > len(truth), "sanity: without the text filter digit loops leak through"
    polys, _ = detect(img)
    assert len(polys) == len(truth)
    assert_matches(polys, truth, 0.97)


def test_suspicious_regions_are_flagged_uncertain():
    img = blank(500, 300)
    cv2.rectangle(img, (30, 30), (470, 270), (0, 0, 0), 3)
    cv2.line(img, (30, 60), (470, 60), (0, 0, 0), 3)          # long thin "road" strip, 28 px x 440 px
    cv2.line(img, (250, 60), (250, 270), (0, 0, 0), 3)
    cv2.rectangle(img, (100, 150), (110, 160), (0, 0, 0), 2)  # tiny 6px square parcel
    _, res = detect(img, min_parcel_area=10, text_max_extent=0)
    statuses = [a["validation_status"] for _, a in res.items]
    assert "uncertain" in statuses and "unreviewed" in statuses
    assert any("auto:" in a["notes"] for _, a in res.items)


def test_cancellation():
    from core.progress import CancelToken, OperationCancelled, Progress
    img, _ = grid_map()
    token = CancelToken()
    token.cancel()
    with pytest.raises(OperationCancelled):
        run_detection(img, DetectionParams(), ValidationParams(), Progress(None, token))
