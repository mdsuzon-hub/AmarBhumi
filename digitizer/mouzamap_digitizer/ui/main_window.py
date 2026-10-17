"""Main application window: wires views, panels, tools and background tasks together."""
from __future__ import annotations

import logging
import os
from typing import Callable, List, Optional

import numpy as np
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QAction, QActionGroup, QKeySequence
from PySide6.QtWidgets import (QApplication, QComboBox, QDockWidget, QFileDialog, QHBoxLayout, QLabel,
                               QMainWindow, QMessageBox, QProgressBar, QPushButton, QScrollArea, QSplitter,
                               QTabWidget, QToolBar, QVBoxLayout, QWidget)
from shapely.geometry import Point, Polygon, box

from core import export_manager as em
from core.image_preprocessing import apply_prep, estimate_skew_angle
from core.pdf_loader import (WARN_SOURCE_PIXELS, SourceLoadError, is_image, is_pdf, load_source,
                             open_pdf_info)
from core.pipeline import PipelineResult, run_detection
from core.polygon_editing import EditError
from core.polygon_validation import repair_geometry
from core.progress import OperationCancelled, Progress
from core.project_manager import (PROJECT_EXTENSION, ProjectError, load_project, save_intermediate_binary,
                                  save_project)
from core.settings import PrepParams
from core.version import APP_NAME, APP_VERSION, COORDINATE_NOTE
from models.parcel import VALIDATION_STATUSES
from models.project import Project
from ui.canvas import EditorView, ProcessedView, Tool, mask_to_overlay_qimage, numpy_to_qimage
from ui.dialogs import ExportDialog, OpenSourceDialog, PrepareDialog
from ui.parcel_table import ParcelInfoPanel, ParcelTable, ParcelTableModel
from ui.settings_panel import SettingsPanel, StylePanel
from ui.workers import TaskThread

log = logging.getLogger(__name__)

SOURCE_FILTER = "Maps (*.pdf *.png *.jpg *.jpeg *.tif *.tiff);;PDF (*.pdf);;Images (*.png *.jpg *.jpeg *.tif *.tiff)"


class MainWindow(QMainWindow):
    def __init__(self, sync_tasks: bool = False) -> None:
        super().__init__()
        self.sync_tasks = sync_tasks          # run tasks inline (used by tests/headless scripts)
        self.project = Project()
        self.source: Optional[np.ndarray] = None
        self.working: Optional[np.ndarray] = None
        self.detection = None
        self._task: Optional[TaskThread] = None
        self._last_dir = os.path.expanduser("~")
        self._quiet = False                   # suppress modal dialogs (tests)

        self._build_views()
        self._build_docks()
        self._build_actions()
        self._build_menus_toolbars()
        self._build_statusbar()
        self._wire()
        self._bind_store()
        self._update_actions()
        self.resize(1500, 900)
        self.setWindowTitle(APP_NAME)

    # ------------------------------------------------------------------ construction
    def _build_views(self) -> None:
        self.editor = EditorView()
        self.processed = ProcessedView()
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(QLabel("<b>Original map + parcel overlay</b>  (editing view)"))
        ll.addWidget(self.editor, 1)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        head.addWidget(QLabel("<b>Processed binary image</b>"))
        self.binary_choice = QComboBox()
        self.binary_choice.addItem("Boundaries (after gap closing)", "boundaries")
        self.binary_choice.addItem("Thresholded ink", "binary")
        head.addWidget(self.binary_choice)
        head.addStretch(1)
        rl.addLayout(head)
        rl.addWidget(self.processed, 1)
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.addWidget(left)
        self.splitter.addWidget(right)
        self.setCentralWidget(self.splitter)

    def _build_docks(self) -> None:
        self.settings = SettingsPanel()
        self.style_panel = StylePanel()
        tabs = QTabWidget()
        for panel, title in ((self.settings, "Pipeline"), (self.style_panel, "Display")):
            sc = QScrollArea()
            sc.setWidgetResizable(True)
            sc.setWidget(panel)
            tabs.addTab(sc, title)
        self.left_dock = QDockWidget("Settings", self)
        self.left_dock.setWidget(tabs)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.left_dock)
        self.left_dock.setMinimumWidth(310)

        self.table_model = ParcelTableModel(self.project.store, self.settings.validation_params)
        self.table = ParcelTable(self.table_model)
        self.info = ParcelInfoPanel()
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(self.info)
        v.addWidget(QLabel("<b>Parcels</b> – double-click Dag no., Status or Notes to edit; "
                           "double-click other cells to zoom"))
        v.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.btn_merge = QPushButton("Merge")
        self.btn_delete = QPushButton("Delete")
        self.btn_valid = QPushButton("✓ Valid")
        self.btn_uncertain = QPushButton("? Uncertain")
        self.btn_rejected = QPushButton("✗ Reject")
        for b in (self.btn_merge, self.btn_delete, self.btn_valid, self.btn_uncertain, self.btn_rejected):
            row.addWidget(b)
        v.addLayout(row)
        self.right_dock = QDockWidget("Parcels", self)
        self.right_dock.setWidget(w)
        self.addDockWidget(Qt.RightDockWidgetArea, self.right_dock)
        self.right_dock.setMinimumWidth(520)

    def _act(self, text: str, slot: Callable, shortcut=None, tip: str = "", checkable: bool = False) -> QAction:
        a = QAction(text, self)
        a.setCheckable(checkable)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        if tip:
            a.setToolTip(tip)
            a.setStatusTip(tip)
        a.triggered.connect(slot)
        return a

    def _build_actions(self) -> None:
        A = self._act
        self.a_open = A("Open map (PDF/image)…", self.open_source, "Ctrl+O")
        self.a_open_project = A("Open project…", self.open_project_dialog, "Ctrl+Shift+O")
        self.a_save = A("Save project", lambda: self.save_project(False), "Ctrl+S")
        self.a_save_as = A("Save project as…", lambda: self.save_project(True), "Ctrl+Shift+S")
        self.a_export = A("Export parcels…", self.export_dialog, "Ctrl+E")
        self.a_export_img = A("Export working image (PNG)…", self.export_working_image,
                              tip="The exact pixel grid the exported polygons refer to - use it for georeferencing")
        self.a_import = A("Import polygons (GeoJSON/GPKG/SHP/CSV)…", self.import_polygons)
        self.a_save_masks = A("Save intermediate masks…", self.save_intermediate)
        self.a_quit = A("Quit", self.close, "Ctrl+Q")

        self.a_undo = A("Undo", lambda: self.project.store.undo(), "Ctrl+Z")
        self.a_redo = A("Redo", lambda: self.project.store.redo(), "Ctrl+Shift+Z")
        self.a_redo.setShortcuts([QKeySequence("Ctrl+Shift+Z"), QKeySequence("Ctrl+Y")])
        self.a_delete = A("Delete selected", self.delete_selected, "Delete")
        self.a_merge = A("Merge selected", self.merge_selected, "Ctrl+M",
                         "Merge two or more adjacent selected polygons")
        self.a_repair = A("Repair geometry", self.repair_selected, tip="Fix self-intersections of selected polygons")
        self.a_select_uncertain = A("Select all uncertain", lambda: self.editor.select_ids(
            [p.parcel_id for p in self.project.store.parcels if p.validation_status == "uncertain"]), "Ctrl+U",
            "Select every polygon flagged uncertain (small, thin or odd shapes) for quick review or rejection")
        self.a_valid = A("Mark valid", lambda: self.mark("valid"), "1")
        self.a_uncertain = A("Mark uncertain", lambda: self.mark("uncertain"), "2")
        self.a_rejected = A("Mark rejected", lambda: self.mark("rejected"), "3")
        self.a_unreviewed = A("Mark unreviewed", lambda: self.mark("unreviewed"), "0")
        self.a_select_all = A("Select all", lambda: self.editor.select_ids(
            [p.parcel_id for p in self.project.store.parcels]), "Ctrl+A")

        self.a_prepare = A("Crop / rotate…", self.prepare_image)
        self.a_deskew = A("Auto-deskew", self.auto_deskew)
        self.a_run = A("Detect parcels", self.run_detection, "F5")
        self.a_reprocess = A("Reprocess selected area…", self.start_reprocess)
        self.a_cancel = A("Cancel task", self.cancel_task, "Ctrl+.")

        self.a_fit = A("Fit to window", self._fit_all, "Ctrl+0")
        self.a_actual = A("Zoom 100%", lambda: self.editor.zoom_actual(), "Ctrl+1")
        self.a_sync = A("Synchronise the two views", lambda *_: None, checkable=True)
        self.a_sync.setChecked(True)

        group = QActionGroup(self)
        self.tool_actions = {}
        for tool, text, key, tip in (
            (Tool.PAN, "Pan", "H", "Drag to pan (middle mouse button or Space also pans)"),
            (Tool.SELECT, "Select", "S", "Click a polygon; Ctrl-click or drag a box for several"),
            (Tool.EDIT, "Edit vertices", "E", "Select one polygon, drag its vertices, double-click an edge to add a "
                                               "vertex, right-click a vertex to delete it"),
            (Tool.SPLIT, "Split", "L", "Click points along the cut line, double-click (or Enter) to split the "
                                       "selected polygon; Backspace removes the last point"),
            (Tool.DRAW, "Draw polygon", "P", "Click the corners, double-click (or Enter) to finish"),
            (Tool.ROI, "Reprocess area", "R", "Drag a rectangle to re-detect parcels in that area"),
        ):
            a = A(text, lambda checked=False, t=tool: self.set_tool(t), key, tip, checkable=True)
            group.addAction(a)
            self.tool_actions[tool] = a
        self.tool_actions[Tool.SELECT].setChecked(True)

        self.a_about = A("About", self.about)
        self.a_georef_help = A("Georeferencing in QGIS…", self.georef_help)

    def _build_menus_toolbars(self) -> None:
        m = self.menuBar()
        f = m.addMenu("&File")
        for a in (self.a_open, self.a_open_project, None, self.a_save, self.a_save_as, None, self.a_import,
                  self.a_export, self.a_export_img, self.a_save_masks, None, self.a_quit):
            f.addSeparator() if a is None else f.addAction(a)
        e = m.addMenu("&Edit")
        for a in (self.a_undo, self.a_redo, None, self.a_select_all, self.a_select_uncertain, self.a_delete, self.a_merge, self.a_repair,
                  None, self.a_valid, self.a_uncertain, self.a_rejected, self.a_unreviewed):
            e.addSeparator() if a is None else e.addAction(a)
        i = m.addMenu("&Image")
        i.addAction(self.a_prepare)
        i.addAction(self.a_deskew)
        p = m.addMenu("&Process")
        for a in (self.a_run, self.a_reprocess, self.a_cancel):
            p.addAction(a)
        v = m.addMenu("&View")
        for a in (self.a_fit, self.a_actual, self.a_sync, None):
            v.addSeparator() if a is None else v.addAction(a)
        v.addAction(self.left_dock.toggleViewAction())
        v.addAction(self.right_dock.toggleViewAction())
        h = m.addMenu("&Help")
        h.addAction(self.a_georef_help)
        h.addAction(self.a_about)

        tb = QToolBar("Tools")
        tb.setMovable(False)
        self.addToolBar(tb)
        for a in (self.a_open, self.a_save, None):
            tb.addSeparator() if a is None else tb.addAction(a)
        for t in Tool:
            tb.addAction(self.tool_actions[t])
        tb.addSeparator()
        for a in (self.a_undo, self.a_redo, self.a_merge, self.a_delete, None, self.a_run, self.a_fit, self.a_export):
            tb.addSeparator() if a is None else tb.addAction(a)

    def _build_statusbar(self) -> None:
        sb = self.statusBar()
        self.count_label = QLabel("No parcels")
        self.cursor_label = QLabel("")
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(220)
        self.progress.setRange(0, 100)
        self.progress.hide()
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.hide()
        sb.addPermanentWidget(self.cursor_label)
        sb.addPermanentWidget(self.progress)
        sb.addPermanentWidget(self.cancel_btn)
        sb.addPermanentWidget(self.count_label)

    def _wire(self) -> None:
        self.editor.selection_changed.connect(self._on_editor_selection)
        self.editor.geometry_edited.connect(self._on_geometry_edited)
        self.editor.vertex_insert_requested.connect(lambda pid, x, y: self._edit(
            lambda: self.project.store.add_vertex(pid, (x, y))))
        self.editor.vertex_delete_requested.connect(lambda pid, r, v: self._edit(
            lambda: self.project.store.remove_vertex(pid, r, v)))
        self.editor.split_drawn.connect(self._on_split)
        self.editor.polygon_drawn.connect(self._on_polygon_drawn)
        self.editor.roi_selected.connect(self._on_roi)
        self.editor.cursor_moved.connect(lambda x, y: self.cursor_label.setText(f"x {x:.1f}  y {y:.1f} px   "))
        self.processed.cursor_moved.connect(lambda x, y: self.cursor_label.setText(f"x {x:.1f}  y {y:.1f} px   "))
        self.editor.view_changed.connect(lambda: self._sync_from(self.editor, self.processed))
        self.processed.view_changed.connect(lambda: self._sync_from(self.processed, self.editor))
        self.table.ids_selected.connect(self._on_table_selection)
        self.table.zoom_requested.connect(self.editor.zoom_to_parcel)
        self.style_panel.style_changed.connect(self._on_style)
        self.settings.run_requested.connect(self.run_detection)
        self.settings.reprocess_requested.connect(self.start_reprocess)
        self.settings.prepare_requested.connect(self.prepare_image)
        self.settings.deskew_requested.connect(self.auto_deskew)
        self.settings.params_changed.connect(self._params_changed)
        self.binary_choice.currentIndexChanged.connect(lambda *_: self._show_processed())
        self.cancel_btn.clicked.connect(self.cancel_task)
        self.btn_merge.clicked.connect(self.merge_selected)
        self.btn_delete.clicked.connect(self.delete_selected)
        self.btn_valid.clicked.connect(lambda: self.mark("valid"))
        self.btn_uncertain.clicked.connect(lambda: self.mark("uncertain"))
        self.btn_rejected.clicked.connect(lambda: self.mark("rejected"))

    # ------------------------------------------------------------------ helpers
    def _msg(self, kind: str, title: str, text: str) -> None:
        log.log(logging.ERROR if kind == "critical" else logging.INFO, "%s: %s", title, text)
        if self._quiet:
            self.statusBar().showMessage(f"{title}: {text}", 8000)
            return
        getattr(QMessageBox, kind)(self, title, text)

    def _ask(self, title: str, text: str, default_yes: bool = True) -> bool:
        if self._quiet:
            return default_yes
        r = QMessageBox.question(self, title, text, QMessageBox.Yes | QMessageBox.No,
                                 QMessageBox.Yes if default_yes else QMessageBox.No)
        return r == QMessageBox.Yes

    def _fit_all(self) -> None:
        self.editor.fit()
        if self.a_sync.isChecked():
            self.processed.auto_fit = False
            self._sync_from(self.editor, self.processed)
        else:
            self.processed.fit()

    def _sync_from(self, src, dst) -> None:
        if self.a_sync.isChecked() and src.has_image and dst.has_image:
            dst.apply_view(*src.view_state())

    def _mark_dirty(self) -> None:
        self.project.dirty = True
        self._update_title()

    def _update_title(self) -> None:
        self.setWindowTitle(f"{APP_NAME} — {self.project.title}{' *' if self.project.dirty else ''}")

    def _params_changed(self) -> None:
        self.project.detect = self.settings.detection_params()
        self.project.validation = self.settings.validation_params()
        self._mark_dirty()

    def _on_style(self, style) -> None:
        self.editor.set_style(style)
        self.processed.set_outlines_visible(style.show_polygons)

    def set_tool(self, tool: Tool) -> None:
        self.editor.set_tool(tool)
        self.tool_actions[tool].setChecked(True)
        if tool == Tool.ROI:
            self.statusBar().showMessage("Drag a rectangle over the area to re-detect.", 6000)

    # ------------------------------------------------------------------ background tasks
    def _begin_busy(self, title: str) -> None:
        self.statusBar().showMessage(title)
        self.progress.setValue(0)
        self.progress.show()
        self.cancel_btn.setVisible(not self.sync_tasks)
        self.settings.set_busy(True)
        self._busy = True
        self._update_actions()

    def _end_busy(self) -> None:
        self._busy = False
        self._task = None
        self.progress.hide()
        self.cancel_btn.hide()
        self.settings.set_busy(False)
        self._update_actions()

    def _on_progress(self, pct: int, msg: str) -> None:
        self.progress.setValue(pct)
        if msg:
            self.statusBar().showMessage(msg)

    _busy = False

    def _run_task(self, fn: Callable[[Progress], object], on_result: Callable, title: str,
                  on_fail: Optional[Callable[[str], None]] = None) -> None:
        if self._task is not None or self._busy:
            self._msg("information", "Busy", "Another task is still running.")
            return
        self._begin_busy(title)

        def done(res):
            self._end_busy()
            try:
                on_result(res)
            except Exception as exc:  # noqa: BLE001
                log.exception("Result handling failed")
                self._msg("critical", "Error", str(exc))

        def failed(text: str):
            self._end_busy()
            (on_fail or (lambda t: self._msg("critical", "Error", t)))(text)

        def cancelled():
            self._end_busy()
            self.statusBar().showMessage("Cancelled.", 5000)

        if self.sync_tasks:
            self._task = None
            try:
                res = fn(Progress(self._on_progress))
            except OperationCancelled:
                cancelled()
                return
            except Exception as exc:  # noqa: BLE001
                failed(str(exc) or type(exc).__name__)
                return
            done(res)
            return
        t = TaskThread(fn, self)
        t.progress.connect(self._on_progress)
        t.result.connect(done)
        t.error.connect(failed)
        t.cancelled.connect(cancelled)
        t.finished.connect(t.deleteLater)
        self._task = t
        t.start()

    def cancel_task(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self.statusBar().showMessage("Cancelling…")

    # ------------------------------------------------------------------ store binding & display
    def _bind_store(self) -> None:
        self.project.store.subscribe(self._on_store_changed)
        self.table_model.set_store(self.project.store)
        self.editor.sync_parcels(self.project.store.parcels)
        self.processed.sync_parcels(self.project.store.parcels)
        self._on_store_changed(mark=False)

    def _on_store_changed(self, mark: bool = True) -> None:
        parcels = self.project.store.parcels
        self.editor.sync_parcels(parcels)
        self.processed.sync_parcels(parcels)
        self.table_model.refresh()
        self.table.select_ids(self.editor.selected_ids())
        self._update_info()
        self._update_actions()
        self._update_counts()
        if mark:
            self._mark_dirty()

    def _update_counts(self) -> None:
        ps = self.project.store.parcels
        if not ps:
            self.count_label.setText("No parcels")
            return
        n = {s: sum(1 for p in ps if p.validation_status == s) for s in VALIDATION_STATUSES}
        bad = sum(1 for p in ps if self.table_model.is_invalid(p))
        self.count_label.setText(f"{len(ps)} polygons  |  valid {n['valid']}  uncertain {n['uncertain']}  "
                                 f"unreviewed {n['unreviewed']}  rejected {n['rejected']}  |  invalid geometry {bad}")

    def _update_info(self) -> None:
        store = self.project.store
        parcels = [store.get(i) for i in self.editor.selected_ids() if store.has(i)]
        self.info.show_parcels(parcels, self.table_model.issues)

    def _update_actions(self) -> None:
        store = self.project.store
        has_img = self.working is not None
        busy = self._busy
        sel = self.editor.selected_ids()
        self.a_undo.setEnabled(store.can_undo)
        self.a_redo.setEnabled(store.can_redo)
        self.a_undo.setText("Undo " + store.undo_label if store.can_undo else "Undo")
        self.a_redo.setText("Redo " + store.redo_label if store.can_redo else "Redo")
        self.a_merge.setEnabled(len(sel) >= 2)
        self.btn_merge.setEnabled(len(sel) >= 2)
        for w in (self.a_delete, self.btn_delete, self.a_repair, self.a_valid, self.a_uncertain, self.a_rejected,
                  self.a_unreviewed, self.btn_valid, self.btn_uncertain, self.btn_rejected):
            w.setEnabled(bool(sel))
        for w in (self.a_run, self.a_reprocess, self.a_prepare, self.a_deskew, self.a_export_img):
            w.setEnabled(has_img and not busy)
        for w in (self.a_export, self.a_save, self.a_save_as, self.a_import):
            w.setEnabled(has_img)
        self.a_save_masks.setEnabled(self.detection is not None)
        self.a_cancel.setEnabled(busy and not self.sync_tasks)

    def _show_working(self) -> None:
        self.editor.clear_all()
        self.processed.clear_all()
        self.editor.set_image(numpy_to_qimage(self.working))
        h, w = self.working.shape[:2]
        self.processed.set_image(numpy_to_qimage(np.full((h, w), 255, np.uint8)))
        self.editor.sync_parcels(self.project.store.parcels)
        self.processed.sync_parcels(self.project.store.parcels)
        self.editor.set_style(self.style_panel.style)

    def _show_processed(self) -> None:
        if self.detection is None or self.working is None:
            return
        key = self.binary_choice.currentData()
        mask = self.detection.boundaries_full() if key == "boundaries" else self.detection.binary_full()
        self.processed.set_image(numpy_to_qimage(255 - mask), keep_view=True)
        self.processed.sync_parcels(self.project.store.parcels)
        self._sync_from(self.editor, self.processed)

    def _show_detection(self) -> None:
        self._show_processed()
        self.editor.set_lines_overlay(mask_to_overlay_qimage(self.detection.boundaries_full(), (255, 0, 0, 255)))
        self.editor.set_style(self.style_panel.style)
        self._update_actions()

    # ------------------------------------------------------------------ opening
    def _confirm_discard(self) -> bool:
        if not self.project.dirty or not len(self.project.store) and self.working is None:
            return True
        if self._quiet:
            return True
        r = QMessageBox.question(self, "Unsaved changes", "Save the current project before continuing?",
                                 QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        if r == QMessageBox.Cancel:
            return False
        if r == QMessageBox.Save:
            return self.save_project(False)
        return True

    def open_source(self, path: Optional[str] = None, page: int = 1, dpi: int = 300) -> None:
        if not self._confirm_discard():
            return
        if not path:
            path, _ = QFileDialog.getOpenFileName(self, "Open scanned map", self._last_dir, SOURCE_FILTER)
            if not path:
                return
            self._last_dir = os.path.dirname(path)
            if is_pdf(path):
                try:
                    info = open_pdf_info(path)
                except SourceLoadError as exc:
                    self._msg("critical", "Cannot open PDF", str(exc))
                    return
                dlg = OpenSourceDialog(info, self)
                if dlg.exec() != OpenSourceDialog.Accepted:
                    return
                page, dpi = dlg.values()
        kind = "pdf" if is_pdf(path) else "image"
        if kind == "image" and not is_image(path):
            self._msg("critical", "Unsupported file", f"Cannot open '{os.path.basename(path)}'.")
            return
        proj = Project(path, kind, page, dpi)
        self._load(proj, f"Loading {os.path.basename(path)}")

    def open_project_dialog(self) -> None:
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open project", self._last_dir,
                                              f"MouzaMap project (*{PROJECT_EXTENSION});;All files (*)")
        if path:
            self.open_project(path)

    def open_project(self, path: str) -> None:
        try:
            proj = load_project(path)
        except ProjectError as exc:
            self._msg("critical", "Cannot open project", str(exc))
            return
        self._last_dir = os.path.dirname(path)
        self._load(proj, f"Opening {os.path.basename(path)}", from_file=True)

    def _load(self, proj: Project, title: str, from_file: bool = False) -> None:
        """Render/load the source, apply the stored crop/rotation, then switch to `proj`."""
        def work(prog: Progress):
            src = load_source(proj.source_path, proj.source_page, proj.dpi, prog.sub(0, 80))
            working, matrix = apply_prep(src, proj.prep, prog.sub(80, 100))
            return src, working, matrix

        def ok(res):
            src, working, matrix = res
            h, w = src.shape[:2]
            if from_file and proj.source_size not in ((0, 0), (w, h)):
                self._msg("warning", "Source size differs",
                          f"The source now renders at {w}×{h} px but the project was made at "
                          f"{proj.source_size[0]}×{proj.source_size[1]} px. Polygons may not line up.")
            if w * h > WARN_SOURCE_PIXELS:
                self._msg("warning", "Very large image",
                          f"{w}×{h} px ({w * h / 1e6:.0f} MP). Display and editing may be slow; "
                          "consider a lower DPI.")
            proj.set_geometry_state((w, h), (working.shape[1], working.shape[0]), matrix)
            self.project, self.source, self.working, self.detection = proj, src, working, None
            self.settings.set_params(proj.detect, proj.validation)
            self._bind_store()
            self._show_working()
            self._fit_all()
            proj.dirty = False
            self._update_title()
            self._update_actions()
            self.statusBar().showMessage(f"Loaded {w}×{h} px ({len(proj.store)} polygons)", 6000)

        def fail(text: str):
            if from_file and not self._quiet and os.path.basename(proj.source_path):
                if self._ask("Source not found", f"{text}\n\nLocate the source file now?"):
                    new, _ = QFileDialog.getOpenFileName(self, "Locate source map", self._last_dir, SOURCE_FILTER)
                    if new:
                        proj.source_path = new
                        self._load(proj, title, from_file=True)
                        return
            self._msg("critical", "Cannot load map", text)

        self._run_task(work, ok, title, fail)

    # ------------------------------------------------------------------ image preparation
    def prepare_image(self) -> None:
        if self.source is None:
            return
        dlg = PrepareDialog(self.source, self.project.prep, self)
        if dlg.exec() == PrepareDialog.Accepted:
            self.apply_prep(dlg.prep())

    def auto_deskew(self) -> None:
        if self.source is None:
            return
        angle = estimate_skew_angle(self.source)
        if abs(angle) < 0.05:
            self._msg("information", "Auto-deskew", "No significant skew detected.")
            return
        if self._ask("Auto-deskew", f"Rotate by {angle:+.2f}° counter-clockwise?\n(The crop will be reset.)"):
            self.apply_prep(PrepParams(rotation_deg=angle, crop=None))

    def apply_prep(self, prep: PrepParams) -> None:
        if prep == self.project.prep:
            return
        old = self.project.source_to_working.copy()
        src = self.source

        def ok(res):
            working, matrix = res
            self.working = working
            self.project.prep = prep
            self.project.set_geometry_state(self.project.source_size, (working.shape[1], working.shape[0]), matrix)
            self.detection = None
            self._show_working()
            if len(self.project.store):     # polygons follow the image through the same transform
                self.project.store.transform_all(matrix @ np.linalg.inv(old), "Crop/rotate image")
            self._fit_all()
            self._mark_dirty()
            self.statusBar().showMessage("Image prepared – run detection again to refresh the binary view.", 6000)

        self._run_task(lambda prog: apply_prep(src, prep, prog), ok, "Preparing image")

    # ------------------------------------------------------------------ detection
    def _has_manual_work(self) -> bool:
        return any(p.extraction_method != "automatic" or p.dag_number or
                   p.validation_status in ("valid", "rejected") or (p.notes and not p.notes.startswith("auto:"))
                   for p in self.project.store.parcels)

    def run_detection(self, roi: Optional[Polygon] = None) -> None:
        if self.working is None:
            self._msg("information", "No map", "Open a PDF or image first.")
            return
        if roi is None and len(self.project.store) and self._has_manual_work():
            if not self._ask("Replace parcels?", "Detection replaces ALL current parcels, including manual edits, "
                             "dag numbers and review marks.\nYou can undo this with Ctrl+Z. Continue?", False):
                return
        params, vparams = self.settings.detection_params(), self.settings.validation_params()
        self.project.detect, self.project.validation = params, vparams
        rgb = self.working
        self._run_task(lambda prog: run_detection(rgb, params, vparams, prog),
                       lambda res: self._detection_done(res, roi), "Detecting parcels")

    def _detection_done(self, res: PipelineResult, roi: Optional[Polygon]) -> None:
        self.detection = res.detection
        self._show_detection()
        if roi is None:
            self.project.store.replace_all(res.items)
            msg = f"{len(res.items)} parcels detected."
        else:
            removed, added = self.project.store.replace_in_region(roi, res.items)
            msg = f"Area reprocessed: {removed} replaced by {added} parcels (manual edits kept)."
        flagged = sum(1 for _, a in res.items if a["validation_status"] == "uncertain")
        if flagged:
            msg += f"  {flagged} flagged uncertain."
        self.statusBar().showMessage(msg, 10000)
        if res.warnings and not res.items:
            self._msg("warning", "Nothing detected", "\n".join(res.warnings))

    def start_reprocess(self) -> None:
        if self.working is None:
            return
        self.set_tool(Tool.ROI)

    def _on_roi(self, rect: QRectF) -> None:
        self.set_tool(Tool.SELECT)
        self.run_detection(box(rect.left(), rect.top(), rect.right(), rect.bottom()))

    # ------------------------------------------------------------------ editing
    def _edit(self, fn: Callable[[], object]) -> None:
        try:
            fn()
        except EditError as exc:
            self._msg("warning", "Cannot edit", str(exc))
            self.editor.sync_parcels(self.project.store.parcels)   # drop any dragged preview
        except KeyError:
            pass

    def _on_geometry_edited(self, pid: str, geom: Polygon) -> None:
        self._edit(lambda: self.project.store.set_geometry(pid, geom, "Move vertices"))
        if not geom.is_valid:
            self.statusBar().showMessage("Polygon is now self-intersecting (shown dashed magenta). "
                                         "Fix the vertices or use Edit ▸ Repair geometry.", 10000)

    def _on_split(self, pts) -> None:
        sel = self.editor.selected_ids()
        pid = sel[0] if len(sel) == 1 else self.editor.parcel_at(*pts[0])
        if pid is None:
            self._msg("information", "Split", "Select the polygon to split (or start the line inside it).")
            return
        self._edit(lambda: self.project.store.split(pid, pts))

    def _on_polygon_drawn(self, pts) -> None:
        self._edit(lambda: self.project.store.add_polygon(Polygon(pts)))

    def selected(self) -> List[str]:
        return self.editor.selected_ids()

    def delete_selected(self) -> None:
        ids = self.selected()
        if ids:
            self.project.store.delete(ids)

    def merge_selected(self) -> None:
        ids = self.selected()
        if len(ids) >= 2:
            self._edit(lambda: self.project.store.merge(ids))

    def repair_selected(self) -> None:
        for pid in self.selected():
            if not self.project.store.get(pid).geometry.is_valid:
                self._edit(lambda pid=pid: self.project.store.repair(pid))

    def mark(self, status: str) -> None:
        ids = self.selected()
        if ids:
            self.project.store.set_attrs(ids, validation_status=status)

    def _on_editor_selection(self, ids: list) -> None:
        self.table.select_ids(ids)
        self._update_info()
        self._update_actions()

    def _on_table_selection(self, ids: list) -> None:
        if set(ids) != set(self.editor.selected_ids()):
            self.editor.select_ids(ids)
            if len(ids) == 1:
                it = self.editor._items.get(ids[0])
                if it is not None and not self.editor.viewport().rect().contains(
                        self.editor.mapFromScene(it.sceneBoundingRect().center())):
                    self.editor.centerOn(it)
        self._update_info()
        self._update_actions()

    # ------------------------------------------------------------------ saving / export
    def save_project(self, save_as: bool = False) -> bool:
        proj = self.project
        if self.working is None:
            return False
        path = proj.file_path
        if save_as or not path:
            base = os.path.splitext(os.path.basename(proj.source_path))[0] or "project"
            path, _ = QFileDialog.getSaveFileName(self, "Save project", os.path.join(self._last_dir, base + PROJECT_EXTENSION),
                                                  f"MouzaMap project (*{PROJECT_EXTENSION})")
            if not path:
                return False
            if not path.endswith(PROJECT_EXTENSION):
                path += PROJECT_EXTENSION
            self._last_dir = os.path.dirname(path)
        proj.detect, proj.validation = self.settings.detection_params(), self.settings.validation_params()
        try:
            save_project(proj, path)
        except ProjectError as exc:
            self._msg("critical", "Cannot save", str(exc))
            return False
        self._update_title()
        self.statusBar().showMessage(f"Saved {path}", 5000)
        return True

    def export_dialog(self) -> None:
        if not len(self.project.store):
            self._msg("information", "Export", "There are no parcels to export yet.")
            return
        dlg = ExportDialog(self.project.export, self)
        if dlg.exec() != ExportDialog.Accepted:
            return
        fmt, opt = dlg.choice()
        base = os.path.splitext(os.path.basename(self.project.source_path))[0] or "parcels"
        filters = {"gpkg": "GeoPackage (*.gpkg)", "geojson": "GeoJSON (*.geojson)",
                   "shp": "Shapefile (*.shp)", "csv": "CSV (*.csv)", "png": "PNG (*.png)"}
        suffix = {"png": "_preview"}.get(fmt, "")
        path, _ = QFileDialog.getSaveFileName(self, "Export parcels", os.path.join(self._last_dir, f"{base}{suffix}.{fmt}"),
                                              filters[fmt])
        if path:
            self.export_to(fmt, path, opt)

    def export_to(self, fmt: str, path: str, opt) -> Optional[em.ExportBundle]:
        self._last_dir = os.path.dirname(path)
        self.project.export = opt
        try:
            if fmt == "png":
                em.export_png_preview(self.project, self.working, path, include_rejected=opt.include_rejected)
                bundle = None
            else:
                fn = {"gpkg": em.export_geopackage, "geojson": em.export_geojson,
                      "shp": em.export_shapefile, "csv": em.export_csv}[fmt]
                bundle = fn(self.project, path, opt)
        except em.ExportError as exc:
            self._msg("critical", "Export failed", str(exc))
            return None
        text = f"Exported to {path}\n\n" + ("" if fmt == "png" else COORDINATE_NOTE)
        if bundle and bundle.warnings:
            text += "\n\nNotes:\n• " + "\n• ".join(bundle.warnings)
        self._msg("information", "Export complete", text)
        return bundle

    def export_working_image(self) -> None:
        if self.working is None:
            return
        base = os.path.splitext(os.path.basename(self.project.source_path))[0] or "map"
        path, _ = QFileDialog.getSaveFileName(self, "Export working image", os.path.join(self._last_dir, base + "_working.png"),
                                              "PNG (*.png)")
        if path:
            try:
                em.export_working_image(self.working, path)
                self.statusBar().showMessage(f"Saved {path}", 6000)
            except em.ExportError as exc:
                self._msg("critical", "Export failed", str(exc))

    def import_polygons(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import polygons", self._last_dir,
                                              "Polygons (*.geojson *.gpkg *.shp *.csv)")
        if path:
            self.import_from(path)

    def import_from(self, path: str, replace: bool = False) -> int:
        try:
            res = em.import_parcels(path, self.project)
        except em.ExportError as exc:
            self._msg("critical", "Import failed", str(exc))
            return 0
        keep = ("dag_number", "validation_status", "notes")
        items = []
        for geom, attrs in res.items:
            a = {k: str(attrs[k]) for k in keep if k in attrs and attrs[k] is not None}
            if a.get("validation_status") not in VALIDATION_STATUSES:
                a.pop("validation_status", None)
            a["extraction_method"] = "manually edited"
            items.append((geom, a))
        if replace:
            self.project.store.replace_all(items, "Import polygons")
            return len(items)
        n = self.project.store.add_polygons(items)
        self.statusBar().showMessage(f"Imported {n} polygons.", 6000)
        return n

    def save_intermediate(self) -> None:
        if self.detection is None:
            return
        if not self.project.file_path and not self.save_project(True):
            return
        try:
            a = save_intermediate_binary(self.project.file_path, "binary", self.detection.binary_full())
            b = save_intermediate_binary(self.project.file_path, "boundaries", self.detection.boundaries_full())
        except ProjectError as exc:
            self._msg("critical", "Cannot save", str(exc))
            return
        self._msg("information", "Intermediate masks saved", f"{a}\n{b}")

    # ------------------------------------------------------------------ help / close
    def about(self) -> None:
        self._msg("information", f"About {APP_NAME}",
                  f"{APP_NAME} {APP_VERSION}\n\nExtracts land-parcel polygons from scanned mouza maps.\n"
                  "Extraction is a starting point: always review and correct the result.\n\n" + COORDINATE_NOTE)

    def georef_help(self) -> None:
        self._msg("information", "Georeferencing later in QGIS",
                  "1. File ▸ Export working image… (the exact pixel grid of the polygons).\n"
                  "2. File ▸ Export parcels… (GeoPackage recommended).\n"
                  "3. In QGIS, georeference the exported image with the Georeferencer using your lat/lon points.\n"
                  "4. Run scripts/georeference_polygons.py with the georeferenced GeoTIFF (or a CSV of control "
                  "points) to transform the polygons into the same CRS.\n\nSee README.md for the full procedure.")

    def closeEvent(self, event) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task.wait(3000)
        if self._confirm_discard():
            event.accept()
        else:
            event.ignore()
