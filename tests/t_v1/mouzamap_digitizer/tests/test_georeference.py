import pytest
from shapely.geometry import box

from core.georeference import (GeoreferenceError, GroundControlPoint, apply_affine, fit_affine,
                               fit_affine_from_gcps, invert_affine)


def test_fit_recovers_known_affine_and_inverts():
    true = (0.00001, 0.000002, 90.4, -0.000001, -0.00001, 23.8)   # x' = a px + b py + c ...
    pts = [(px, py, true[0] * px + true[1] * py + true[2], true[3] * px + true[4] * py + true[5])
           for px, py in [(0, 0), (1000, 0), (0, 800), (1000, 800), (400, 300)]]
    est, rmse = fit_affine(pts)
    assert rmse < 1e-9
    assert est == pytest.approx(true, rel=1e-6)
    inv = invert_affine(est)
    g = apply_affine(apply_affine(box(10, 10, 50, 40), est), inv)
    assert g.equals_exact(box(10, 10, 50, 40), 1e-6)


def test_gcps_need_three_non_collinear_points():
    with pytest.raises(GeoreferenceError):
        fit_affine([(0, 0, 1, 1), (1, 1, 2, 2)])
    with pytest.raises(GeoreferenceError):
        fit_affine([(0, 0, 1, 1), (1, 1, 2, 2), (2, 2, 3, 3)])
    gcps = [GroundControlPoint(0, 0, 23.0, 90.0), GroundControlPoint(100, 0, 23.0, 90.1),
            GroundControlPoint(0, 100, 22.9, 90.0)]
    (a, b, c, d, e, f), rmse = fit_affine_from_gcps(gcps)
    assert (c, f) == pytest.approx((90.0, 23.0)) and rmse < 1e-9
