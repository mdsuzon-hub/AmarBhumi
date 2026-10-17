import cv2
import numpy as np
import pytest

from core.image_preprocessing import (apply_prep, build_transform, estimate_skew_angle, rotated_preview)
from core.settings import PrepParams
from tests.synthetic import grid_map


def marker_image():
    img = np.full((300, 500, 3), 255, np.uint8)
    img[100:110, 200:210] = 0
    img.flags.writeable = False
    return img


def centroid_of_dark(img):
    ys, xs = np.nonzero(img[..., 0] < 128)
    return xs.mean() + 0.5, ys.mean() + 0.5


def test_crop_only_is_a_view_and_matrix_is_exact():
    src = marker_image()
    out, m = apply_prep(src, PrepParams(crop=(150, 50, 200, 150)))
    assert np.shares_memory(out, src), "crop must not copy pixel data"
    assert out.shape[:2] == (150, 200)
    px = m @ np.array([205.0, 105.0, 1.0])
    assert np.allclose(centroid_of_dark(out), px[:2], atol=0.01)


def test_no_prep_returns_source_itself():
    src = marker_image()
    out, m = apply_prep(src, PrepParams())
    assert out is src and np.allclose(m, np.eye(3))


@pytest.mark.parametrize("angle", [12.0, -7.5, 90.0])
def test_rotation_matrix_maps_source_points_to_working_pixels(angle):
    src = marker_image()
    out, m = apply_prep(src, PrepParams(rotation_deg=angle))
    px = m @ np.array([205.0, 105.0, 1.0])
    assert np.allclose(centroid_of_dark(out), px[:2], atol=1.2)


def test_rotate_then_crop_matrix():
    src = marker_image()
    prep = PrepParams(rotation_deg=20.0, crop=(100, 80, 300, 250))
    out, m = apply_prep(src, prep)
    assert out.shape[:2] == (250, 300)
    px = m @ np.array([205.0, 105.0, 1.0])
    assert np.allclose(centroid_of_dark(out), px[:2], atol=1.2)
    m2, size = build_transform(src.shape[:2], prep)
    assert np.allclose(m, m2) and size == (300, 250)


def test_source_is_never_modified():
    src = marker_image()
    before = src.copy()
    apply_prep(src, PrepParams(rotation_deg=15, crop=(10, 10, 200, 200)))
    assert np.array_equal(src, before)
    with pytest.raises(ValueError):
        src[0, 0, 0] = 5


@pytest.mark.parametrize("tilt", [-4.0, 2.5, 6.0])
def test_skew_estimate_recovers_known_tilt(tilt):
    img, _ = grid_map(rows=3, cols=4, cell=200, labels=False)
    tilted, _ = rotated_preview(img, tilt)             # rotate counter-clockwise by `tilt`
    est = estimate_skew_angle(tilted)
    assert est == pytest.approx(-tilt, abs=0.4)         # correction is the opposite rotation
    fixed, _ = apply_prep(np.ascontiguousarray(tilted), PrepParams(rotation_deg=est))
    assert abs(estimate_skew_angle(np.ascontiguousarray(fixed))) < 0.4


def test_crop_is_clamped():
    src = marker_image()
    out, _ = apply_prep(src, PrepParams(crop=(-50, -50, 9999, 9999)))
    assert out.shape[:2] == (300, 500)
