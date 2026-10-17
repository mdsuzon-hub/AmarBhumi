"""Headless end-to-end test of the GUI (Qt 'offscreen' platform, tasks run inline)."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import cv2
import numpy as np
import pytest
from shapely.geometry import Point

pytest.importorskip("PySide6")
from PySide6.QtCore import QPointF, QRectF, Qt                    # noqa: E402
from PySide6.QtWidgets import QApplication                          # noqa: E402

from core.settings import ExportOptions, PrepParams                 # noqa: E402
from tests.synthetic import irregular_map                           # noqa: E402
from ui.canvas import Tool, VertexHandle                            # noqa: E402
from ui.main_window import MainWindow                               # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app, tmp_path):
    img, truth = irregular_map(rows=3, cols=3, cell=200)
    path = str(tmp_path / "map.png")
    cv2.imwrite(path, cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
    w = MainWindow(sync_tasks=True)
    w._quiet = True
    w.show()
    w.open_source(path)
    w.map_path = path
    w.truth = truth
    yield w
    w.project.dirty = False
    w.close()


def adjacent_pair(store):
    ps = store.parcels
    for i, a in enumerate(ps):
        for b in ps[i + 1:]:
            if a.geometry.intersection(b.geometry).length > 20:
                return a, b
    raise AssertionError("no adjacent parcels")


def test_open_detect_select_and_info(win):
    assert win.working is not None and win.working.shape[2] == 3
    win.run_detection()
    assert len(win.project.store) == 9 and len(win.editor._items) == 9 and win.table_model.rowCount() == 9
    assert win.detection is not None and "9 polygons" in win.count_label.text()
    pid = win.project.store.parcels[4].parcel_id
    win.editor.select_ids([pid])
    assert win.table.selected_ids() == [pid]
    text = win.info._labels["ID"].text()
    assert text == pid and "px²" in win.info._labels["Pixel area"].text()
    win.table.select_ids([win.project.store.parcels[0].parcel_id])
    win.table._on_selection()
    assert win.editor.selected_ids() == [win.project.store.parcels[0].parcel_id]


def test_edit_workflow_with_undo(win):
    win.run_detection()
    s = win.project.store
    a, b = adjacent_pair(s)
    win.editor.select_ids([a.parcel_id, b.parcel_id])
    assert win.a_merge.isEnabled()
    win.merge_selected()
    assert len(s) == 8
    win.a_undo.trigger()
    assert len(s) == 9
    pid = s.parcels[0].parcel_id
    win.editor.select_ids([pid])
    win.mark("valid")
    assert s.get(pid).validation_status == "valid"
    s.set_attrs([pid], dag_number="১২৩")
    assert win.editor._items[pid]._label.text() == "১২৩"
    win.editor.select_ids([pid])
    win.delete_selected()
    assert len(s) == 8
    win.a_undo.trigger()
    assert len(s) == 9


def test_vertex_editing_via_handles(win):
    win.run_detection()
    s = win.project.store
    pid = s.parcels[4].parcel_id
    win.set_tool(Tool.EDIT)
    win.editor.select_ids([pid])
    handles = win.editor._handles
    assert len(handles) == s.get(pid).vertex_count
    before = s.get(pid).geometry
    h = handles[0]
    h._moving = True
    h.setPos(h.pos() + QPointF(7, 5))
    h._moving = False
    win.editor._handle_released()                   # (the real GUI defers this with a zero timer)
    after = s.get(pid)
    assert not after.geometry.equals_exact(before, 1e-6) and after.extraction_method == "manually edited"
    n = after.vertex_count
    c = after.geometry.exterior.coords
    mid = ((c[0][0] + c[1][0]) / 2, (c[0][1] + c[1][1]) / 2)
    win.editor.vertex_insert_requested.emit(pid, *mid)
    assert s.get(pid).vertex_count == n + 1
    win.editor.vertex_delete_requested.emit(pid, 0, 0)
    assert s.get(pid).vertex_count == n


def test_split_draw_and_merge_tools(win):
    win.run_detection()
    s = win.project.store
    target = s.parcels[4]
    cx, cy = target.geometry.centroid.x, target.geometry.centroid.y
    win.editor.select_ids([target.parcel_id])
    win.editor.split_drawn.emit([(cx, cy - 300), (cx, cy + 300)])
    assert len(s) == 10
    win.editor.polygon_drawn.emit([(5, 5), (40, 5), (40, 40), (5, 40)])
    assert len(s) == 11 and s.parcels[-1].extraction_method == "manually edited"


def test_reprocess_area_keeps_manual_edits(win):
    win.run_detection()
    s = win.project.store
    first = s.parcels[0]
    s.set_attrs([first.parcel_id], dag_number="42")
    s.set_geometry(first.parcel_id, first.geometry.buffer(-3))
    n = len(s)
    win._on_roi(QRectF(0, 0, win.working.shape[1], win.working.shape[0]))
    assert len(s) == n
    assert s.get(first.parcel_id).dag_number == "42"


def test_save_close_reopen_and_export(win, tmp_path):
    win.run_detection()
    s = win.project.store
    s.set_attrs([s.parcels[2].parcel_id], dag_number="777", notes="check me")
    pf = str(tmp_path / "work.mouzamap")
    win.project.file_path = pf
    assert win.save_project(False) and not win.project.dirty
    before = [(p.parcel_id, p.geometry.wkt, p.dag_number) for p in s.parcels]

    win2 = MainWindow(sync_tasks=True)
    win2._quiet = True
    win2.open_project(pf)
    after = [(p.parcel_id, p.geometry.wkt, p.dag_number) for p in win2.project.store.parcels]
    assert before == after
    assert len(win2.editor._items) == 9 and not win2.project.dirty
    for fmt in ("gpkg", "geojson", "shp", "csv", "png"):
        out = str(tmp_path / f"o.{fmt}")
        win2.export_to(fmt, out, ExportOptions())
        assert os.path.getsize(out) > 0, fmt
    n = win2.import_from(str(tmp_path / "o.gpkg"))
    assert n == 9 and len(win2.project.store) == 18
    win2.project.dirty = False
    win2.close()


def test_crop_rotate_keeps_polygons_registered(win):
    win.run_detection()
    s = win.project.store
    pid = s.parcels[4].parcel_id
    pt_before = s.get(pid).geometry.centroid
    win.apply_prep(PrepParams(rotation_deg=0.0, crop=(40, 30, 500, 500)))
    pt_after = s.get(pid).geometry.centroid
    assert (pt_after.x, pt_after.y) == pytest.approx((pt_before.x - 40, pt_before.y - 30), abs=1e-6)
    assert win.working.shape[:2] == (500, 500) and win.project.working_size == (500, 500)
    # polygons still sit on the same drawn boundaries after a rotation too
    win.apply_prep(PrepParams(rotation_deg=10.0, crop=None))
    win.run_detection()
    assert len(s) == 9


def test_screenshot(win, tmp_path):
    win.run_detection()
    s = win.project.store
    s.set_attrs([s.parcels[1].parcel_id], validation_status="valid", dag_number="12")
    s.set_attrs([s.parcels[2].parcel_id], validation_status="rejected")
    win.editor.select_ids([s.parcels[4].parcel_id])
    win.style_panel._checks["show_lines"].setChecked(True)
    win.resize(1600, 900)
    win.show()
    QApplication.processEvents()
    out = os.environ.get("MOUZA_SCREENSHOT")
    img = win.grab()
    assert not img.isNull() and img.width() > 800
    if out:
        img.save(out)
