import numpy as np
import pytest
from shapely.geometry import Polygon, box

from core.polygon_editing import (EditError, ParcelStore, delete_vertex, insert_vertex,
                                  merge_polygons, split_polygon)


def store_with(*geoms):
    s = ParcelStore()
    s.replace_all([(g, {}) for g in geoms])
    s.clear_history()
    return s


def test_split_polygon_with_short_line_still_cuts_through():
    parts = split_polygon(box(0, 0, 100, 50), [(50, 20), (50, 30)])    # line lies inside the polygon
    assert len(parts) == 2
    assert sorted(round(p.area) for p in parts) == [2500, 2500]


def test_split_with_polyline_and_failure_modes():
    parts = split_polygon(box(0, 0, 100, 100), [(0, 50), (50, 50), (100, 80)])
    assert len(parts) == 2 and sum(p.area for p in parts) == pytest.approx(10000)
    with pytest.raises(EditError):
        split_polygon(box(0, 0, 10, 10), [(100, 100), (200, 100)])
    with pytest.raises(EditError):
        split_polygon(box(0, 0, 10, 10), [(5, 5)])


def test_merge_adjacent_bridges_hairline_gap_and_rejects_distant():
    a, b = box(0, 0, 10, 10), box(10, 0, 20, 10)
    assert merge_polygons([a, b]).area == pytest.approx(200)
    assert merge_polygons([a, box(10.4, 0, 20, 10)]).geom_type == "Polygon"
    with pytest.raises(EditError):
        merge_polygons([a, box(50, 0, 60, 10)])


def test_insert_and_delete_vertex():
    sq = box(0, 0, 10, 10)
    p2 = insert_vertex(sq, (5, 0.4))
    assert len(p2.exterior.coords) - 1 == 5 and p2.area == pytest.approx(100)
    assert any(abs(x - 5) < 1e-9 and abs(y) < 1e-9 for x, y in p2.exterior.coords)
    p3 = delete_vertex(p2, 0, 1) if len(p2.exterior.coords) > 4 else p2
    assert len(p3.exterior.coords) - 1 == 4
    with pytest.raises(EditError):
        delete_vertex(Polygon([(0, 0), (1, 0), (0, 1)]), 0, 0)


def test_store_undo_redo_roundtrip():
    s = store_with(box(0, 0, 10, 10), box(10, 0, 20, 10))
    ids = [p.parcel_id for p in s.parcels]
    s.set_attrs([ids[0]], dag_number="123")
    s.merge(ids)
    assert len(s) == 1 and s.parcels[0].dag_number == "123"
    s.undo()
    assert len(s) == 2
    s.undo()
    assert s.get(ids[0]).dag_number == ""
    assert not s.can_undo
    s.redo(); s.redo()
    assert len(s) == 1 and s.can_undo and not s.can_redo


def test_new_edit_clears_redo_stack():
    s = store_with(box(0, 0, 10, 10))
    pid = s.parcels[0].parcel_id
    s.set_attrs([pid], notes="a")
    s.undo()
    assert s.can_redo
    s.set_attrs([pid], notes="b")
    assert not s.can_redo


def test_manual_edit_marks_extraction_method_and_ids_are_never_reused():
    s = store_with(box(0, 0, 10, 10), box(10, 0, 20, 10))
    first = s.parcels[0].parcel_id
    assert s.parcels[0].extraction_method == "automatic"
    s.set_geometry(first, box(0, 0, 12, 10))
    assert s.get(first).extraction_method == "manually edited"
    s.delete([s.parcels[1].parcel_id])
    new = s.add_polygon(box(30, 30, 40, 40))
    assert new.parcel_id not in ("P0001", "P0002") and new.extraction_method == "manually edited"
    s.undo()
    again = s.add_polygon(box(50, 50, 60, 60))
    assert again.parcel_id == new.parcel_id     # undo restores the id counter too


def test_split_keeps_largest_piece_identity_and_dag():
    s = store_with(box(0, 0, 100, 50))
    pid = s.parcels[0].parcel_id
    s.set_attrs([pid], dag_number="77")
    pieces = s.split(pid, [(30, -5), (30, 55)])
    assert len(s) == 2
    big = max(s.parcels, key=lambda p: p.pixel_area)
    assert big.parcel_id == pid and big.dag_number == "77"
    assert any("split from" in p.notes for p in s.parcels)


def test_replace_in_region_keeps_manual_parcels():
    s = store_with(box(0, 0, 10, 10), box(20, 0, 30, 10), box(100, 0, 110, 10))
    s.set_geometry(s.parcels[1].parcel_id, box(20, 0, 31, 10))        # manual
    removed, added = s.replace_in_region(box(-5, -5, 40, 20), [(box(0, 0, 10, 10), {}), (box(20, 0, 30, 10), {})])
    assert removed == 1 and added == 1       # manual one kept, its candidate dropped
    assert len(s) == 3


def test_transform_all_translates_polygons():
    s = store_with(box(0, 0, 10, 10))
    s.transform_all(np.array([[1, 0, 5], [0, 1, -3], [0, 0, 1.0]]))
    assert s.parcels[0].geometry.bounds == (5, -3, 15, 7)
    s.undo()
    assert s.parcels[0].geometry.bounds == (0, 0, 10, 10)


def test_observers_fire_on_every_change():
    s = store_with(box(0, 0, 10, 10))
    calls = []
    s.subscribe(lambda: calls.append(1))
    pid = s.parcels[0].parcel_id
    s.set_attrs([pid], notes="x"); s.undo(); s.redo()
    assert len(calls) == 3


def test_add_polygon_repairs_bowtie_or_rejects_empty():
    s = ParcelStore()
    bow = Polygon([(0, 0), (10, 10), (10, 0), (0, 10)])
    assert s.add_polygon(bow).geometry.is_valid
    with pytest.raises(EditError):
        s.add_polygon(Polygon())
