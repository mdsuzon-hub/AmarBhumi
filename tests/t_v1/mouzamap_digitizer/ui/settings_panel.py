"""Side panels: pipeline settings (all thresholds configurable) and display style."""
from __future__ import annotations

from typing import Dict, List, Tuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox,
                               QHBoxLayout, QLabel, QPushButton, QSlider, QSpinBox, QVBoxLayout, QWidget)

from core.settings import DetectionParams, ValidationParams
from ui.canvas import STATUS_COLORS, ViewStyle

# (attribute, label, kind, min, max, step, tooltip)  kind: int | float | bool | choice
DETECTION_FIELDS: List[Tuple[str, List[tuple]]] = [
    ("Image adjustment", [
        ("contrast_alpha", "Contrast (gain)", "float", 0.2, 4.0, 0.05, "Multiplies gray values. >1 increases contrast of faint scans."),
        ("brightness_beta", "Brightness (offset)", "int", -120, 120, 1, "Added to gray values."),
        ("clahe", "Local contrast (CLAHE)", "bool", 0, 0, 0, "Adaptive histogram equalisation; helps unevenly lit scans."),
        ("clahe_clip", "CLAHE clip limit", "float", 0.5, 10.0, 0.5, "Higher = stronger local contrast."),
        ("blur_ksize", "Pre-blur kernel", "int", 1, 15, 2, "Gaussian blur before thresholding (odd; 1 = off)."),
    ]),
    ("Thresholding", [
        ("threshold_method", "Method", "choice", ["adaptive", "otsu"], 0, 0, "Adaptive = local threshold (uneven scans). Otsu = one global threshold (clean scans)."),
        ("adaptive_block_size", "Adaptive block size", "int", 3, 501, 2, "Neighbourhood (px, odd) used to compute the local threshold. Should exceed the line width."),
        ("adaptive_c", "Adaptive C", "int", -50, 80, 1, "Subtracted from the local mean. Larger = only darker ink is kept."),
    ]),
    ("Noise & line detection", [
        ("min_noise_area", "Min. ink blob (px²)", "int", 0, 5000, 1, "Ink specks smaller than this are removed."),
        ("text_max_extent", "Max. text/symbol size (px)", "int", 0, 1000, 5, "Isolated ink clusters smaller than this in both directions (plot numbers, symbols) are removed so their loops do not become false parcels. 0 = off."),
        ("line_filter", "Keep only line-like ink", "bool", 0, 0, 0, "Drops ink components without a long straight stroke (digits, hatching, handwriting)."),
        ("line_min_length", "Min. line length (px)", "int", 5, 3000, 5, "Length of the oriented line probes used by the line filter."),
    ]),
    ("Gap closing", [
        ("close_ksize", "Closing kernel (px)", "int", 1, 99, 1, "Morphological closing diameter. Bridges gaps up to about this size in boundaries."),
        ("close_iterations", "Closing iterations", "int", 1, 10, 1, "Repeat closing to bridge larger gaps (also merges close parallel lines)."),
    ]),
    ("Region selection", [
        ("min_parcel_area", "Min. parcel area (px²)", "int", 1, 10_000_000, 10, "Enclosed regions smaller than this (text loops, dust) are absorbed into neighbours."),
        ("max_area_fraction", "Max. region (fraction of image)", "float", 0.05, 1.0, 0.05, "Larger regions are treated as background and skipped."),
        ("border_mode", "Image border", "choice", ["exclude_touching", "close_border"], 0, 0, "exclude_touching: regions open to the image edge are background. close_border: treat the edge as a boundary (keeps cut-off edge parcels)."),
    ]),
    ("Geometry", [
        ("simplify_tolerance", "Simplify tolerance (px)", "float", 0.0, 25.0, 0.1, "Douglas-Peucker tolerance. Shared boundaries stay shared. 0 = keep every pixel step."),
        ("max_detect_pixels", "Max. detection pixels", "int", 1_000_000, 400_000_000, 1_000_000, "Larger images are downscaled for detection only; polygons are scaled back."),
    ]),
]
VALIDATION_FIELDS: List[tuple] = [
    ("suspicious_area", "Suspicious area (px²)", "float", 0, 1e7, 50, "Smaller parcels are flagged as uncertain."),
    ("max_aspect_ratio", "Max. aspect ratio", "float", 1, 1000, 1, "Longer/thinner shapes (roads, canals) are flagged."),
    ("min_solidity", "Min. solidity", "float", 0, 1, 0.05, "Area / convex hull area. Lower = very concave shape."),
    ("duplicate_iou", "Duplicate overlap (IoU)", "float", 0.5, 1, 0.01, "Two polygons overlapping at least this much are duplicates."),
]


class _FormBuilder:
    def __init__(self, on_change) -> None:
        self.widgets: Dict[str, QWidget] = {}
        self._on_change = on_change

    def add(self, form: QFormLayout, spec: tuple) -> None:
        attr, label, kind, lo, hi, step, tip = spec
        if kind == "bool":
            w = QCheckBox()
            w.toggled.connect(self._on_change)
        elif kind == "choice":
            w = QComboBox()
            w.addItems(lo)
            w.currentIndexChanged.connect(self._on_change)
        elif kind == "int":
            w = QSpinBox()
            w.setRange(int(lo), int(hi))
            w.setSingleStep(int(step))
            w.valueChanged.connect(self._on_change)
        else:
            w = QDoubleSpinBox()
            w.setRange(float(lo), float(hi))
            w.setSingleStep(float(step))
            w.setDecimals(2)
            w.valueChanged.connect(self._on_change)
        w.setToolTip(tip)
        lbl = QLabel(label)
        lbl.setToolTip(tip)
        form.addRow(lbl, w)
        self.widgets[attr] = w

    def value(self, attr: str):
        w = self.widgets[attr]
        if isinstance(w, QCheckBox):
            return w.isChecked()
        if isinstance(w, QComboBox):
            return w.currentText()
        return w.value()

    def set_value(self, attr: str, v) -> None:
        w = self.widgets[attr]
        w.blockSignals(True)
        if isinstance(w, QCheckBox):
            w.setChecked(bool(v))
        elif isinstance(w, QComboBox):
            w.setCurrentText(str(v))
        else:
            w.setValue(v)
        w.blockSignals(False)


class SettingsPanel(QWidget):
    """All detection and validation parameters, editable."""

    params_changed = Signal()
    run_requested = Signal()
    reprocess_requested = Signal()
    prepare_requested = Signal()
    deskew_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        self.builder = _FormBuilder(lambda *_: self.params_changed.emit())

        row = QHBoxLayout()
        self.prepare_btn = QPushButton("Crop / rotate…")
        self.prepare_btn.setToolTip("Crop away titles/legends and rotate or auto-deskew the map")
        self.prepare_btn.clicked.connect(self.prepare_requested)
        self.deskew_btn = QPushButton("Auto-deskew")
        self.deskew_btn.clicked.connect(self.deskew_requested)
        row.addWidget(self.prepare_btn)
        row.addWidget(self.deskew_btn)
        lay.addLayout(row)

        self.run_btn = QPushButton("▶ Detect parcels")
        self.run_btn.setStyleSheet("font-weight: bold; padding: 6px;")
        self.run_btn.clicked.connect(self.run_requested)
        self.reprocess_btn = QPushButton("Reprocess selected area…")
        self.reprocess_btn.setToolTip("Draw a rectangle; automatic parcels inside it are re-detected with the current settings")
        self.reprocess_btn.clicked.connect(self.reprocess_requested)
        lay.addWidget(self.run_btn)
        lay.addWidget(self.reprocess_btn)

        for title, fields in DETECTION_FIELDS:
            box = QGroupBox(title)
            form = QFormLayout(box)
            for spec in fields:
                self.builder.add(form, spec)
            lay.addWidget(box)
        box = QGroupBox("Flagging of suspicious polygons")
        form = QFormLayout(box)
        for spec in VALIDATION_FIELDS:
            self.builder.add(form, spec)
        lay.addWidget(box)
        lay.addStretch(1)
        self.set_params(DetectionParams(), ValidationParams())

    def set_params(self, d: DetectionParams, v: ValidationParams) -> None:
        for attr in vars(d):
            if attr in self.builder.widgets:
                self.builder.set_value(attr, getattr(d, attr))
        for attr in vars(v):
            if attr in self.builder.widgets:
                self.builder.set_value(attr, getattr(v, attr))

    def detection_params(self) -> DetectionParams:
        d = DetectionParams()
        for attr in vars(d):
            if attr in self.builder.widgets:
                setattr(d, attr, self.builder.value(attr))
        return d

    def validation_params(self) -> ValidationParams:
        v = ValidationParams()
        for attr in vars(v):
            if attr in self.builder.widgets:
                setattr(v, attr, self.builder.value(attr))
        return v

    def set_busy(self, busy: bool) -> None:
        for b in (self.run_btn, self.reprocess_btn, self.prepare_btn, self.deskew_btn):
            b.setEnabled(not busy)


class StylePanel(QWidget):
    """Layer toggles, colours, boundary thickness and fill opacity."""

    style_changed = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.style = ViewStyle()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        box = QGroupBox("Layers")
        v = QVBoxLayout(box)
        self._checks: Dict[str, QCheckBox] = {}
        for attr, text in (("show_image", "Original image"), ("show_lines", "Detected lines (boundary mask)"),
                           ("show_polygons", "Parcel polygons"), ("show_labels", "Dag number labels")):
            c = QCheckBox(text)
            c.setChecked(getattr(self.style, attr))
            c.toggled.connect(lambda checked, a=attr: self._set(a, checked))
            v.addWidget(c)
            self._checks[attr] = c
        lay.addWidget(box)

        box2 = QGroupBox("Appearance")
        form = QFormLayout(box2)
        self.width = QDoubleSpinBox()
        self.width.setRange(0.5, 12)
        self.width.setSingleStep(0.5)
        self.width.setValue(self.style.line_width)
        self.width.valueChanged.connect(lambda x: self._set("line_width", x))
        form.addRow("Boundary thickness", self.width)
        self.fill = QSlider(Qt.Horizontal)
        self.fill.setRange(0, 80)
        self.fill.setValue(self.style.fill_opacity)
        self.fill.valueChanged.connect(lambda x: self._set("fill_opacity", x))
        form.addRow("Fill opacity", self.fill)
        for status in STATUS_COLORS:
            btn = QPushButton(status)
            btn.setStyleSheet(f"background:{self.style.colors[status]}; color:white; font-weight:bold;")
            btn.clicked.connect(lambda _=False, s=status, b=btn: self._pick(s, b))
            form.addRow("Colour", btn)
        lay.addWidget(box2)
        lay.addStretch(1)

    def _set(self, attr, value) -> None:
        setattr(self.style, attr, value)
        self.style_changed.emit(self.style)

    def _pick(self, status: str, btn: QPushButton) -> None:
        c = QColorDialog.getColor(QColor(self.style.colors[status]), self, f"Colour for '{status}'")
        if c.isValid():
            self.style.colors[status] = c.name()
            btn.setStyleSheet(f"background:{c.name()}; color:white; font-weight:bold;")
            self.style_changed.emit(self.style)
