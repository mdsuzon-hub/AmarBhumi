from shapely.geometry import Polygon, box

from core.polygon_validation import (check_geometry, find_duplicates, is_invalid, repair_geometry)
from core.settings import ValidationParams
from models.parcel import Parcel

V = ValidationParams()


def codes(g):
    return {i.code for i in check_geometry(g, V)}


def test_invalid_bowtie_detected_and_repaired():
    bow = Polygon([(0, 0), (10, 10), (10, 0), (0, 10)])
    assert is_invalid(bow) and "invalid_geometry" in codes(bow)
    fixed = repair_geometry(bow)
    assert fixed is not None and fixed.is_valid and fixed.area > 0


def test_small_elongated_and_concave_shapes_are_flagged():
    assert "too_small" in codes(box(0, 0, 10, 10))
    assert "elongated" in codes(box(0, 0, 1000, 20))
    ell = Polygon([(0, 0), (100, 0), (100, 10), (10, 10), (10, 100), (0, 100)])
    assert "low_solidity" in codes(ell)
    assert codes(box(0, 0, 100, 100)) == set()


def test_duplicates_found_by_iou():
    ps = [Parcel("A", box(0, 0, 100, 100)), Parcel("B", box(1, 0, 101, 100)), Parcel("C", box(300, 0, 400, 100))]
    dups = find_duplicates(ps, 0.9)
    assert dups == {"A": ["B"], "B": ["A"]}
    assert find_duplicates(ps, 0.999) == {}
