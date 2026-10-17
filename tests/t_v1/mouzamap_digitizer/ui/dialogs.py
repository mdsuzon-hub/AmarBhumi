"""Modal dialogs: open source (page/DPI), crop & rotate, export options."""
from __future__ import annotations

from typing import Optional, Tuple

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPen, QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
                               QGraphicsRectItem, QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel,
                               QPushButton, QSpinBox, QVBoxLayout)

from core.image_preprocessing import estimate_skew_angle, make_thumbnail, rotated_preview
from core.pdf_loader import WARN_SOURCE_PIXELS, PdfInfo, render_pdf_thumbnail, rendered_size
from core.settings import ExportOptions, PrepParams
from ui.canvas import numpy_to_qimage

EXPORT_FORMATS = [
    ("GeoPackage (.gpkg)", "gpkg"),
    ("GeoJSON (.geojson)", "geojson"),
    ("Shapefile (.shp)", "shp"),
    ("CSV with vertices (.csv)", "csv"),
    ("PNG preview over the map (.png)", "png"),
]


class OpenSourceDialog(QDialog):
    """Choose the PDF page and render resolution."""

    def __init__(self, info: PdfInfo, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Open PDF")
        self.info = info
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.page = QSpinBox()
        self.page.setRange(1, info.page_count)
        form.addRow(f"Page (1–{info.page_count})", self.page)
        self.dpi = QComboBox()
        self.dpi.setEditable(True)
        self.dpi.addItems(["150", "200", "300", "400", "600"])
        self.dpi.setCurrentText("300")
        form.addRow("Render DPI (300–600 recommended)", self.dpi)
        lay.addLayout(form)
        self.size_label = QLabel()
        lay.addWidget(self.size_label)
        self.thumb = QLabel(alignment=Qt.AlignCenter)
        self.thumb.setMinimumSize(300, 300)
        lay.addWidget(self.thumb)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.page.valueChanged.connect(self._update)
        self.dpi.currentTextChanged.connect(self._update)
        self._update()

    def _dpi_value(self) -> int:
        try:
            return max(72, min(1200, int(float(self.dpi.currentText()))))
        except ValueError:
            return 300

    def _update(self, *_):
        w, h = rendered_size(self.info, self.page.value(), self._dpi_value())
        mp = w * h / 1e6
        warn = w * h > WARN_SOURCE_PIXELS
        self.size_label.setText(f"Image size: {w} × {h} px ({mp:.0f} MP)" +
                                ("  ⚠ very large – consider a lower DPI" if warn else ""))
        self.size_label.setStyleSheet("color:#c00000;" if warn else "")
        try:
            t = render_pdf_thumbnail(self.info.path, self.page.value(), 360)
            self.thumb.setPixmap(QPixmap.fromImage(numpy_to_qimage(t)).copy())
        except Exception:
            self.thumb.setText("(preview unavailable)")

    def values(self) -> Tuple[int, int]:
        return self.page.value(), self._dpi_value()


class _CropView(QGraphicsView):
    """Thumbnail view where the user drags a crop rectangle."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setMinimumSize(640, 480)
        self.setBackgroundBrush(QBrush(QColor("#3a3d41")))
        self._pix = None
        self._rect: Optional[QGraphicsRectItem] = None
        self._start: Optional[QPointF] = None

    def set_pixmap(self, pm: QPixmap) -> None:
        keep = self.crop_rect()
        self.scene().clear()
        self._rect = None
        self._pix = self.scene().addPixmap(pm)
        self.scene().setSceneRect(QRectF(pm.rect()))
        self.fitInView(self.scene().sceneRect(), Qt.KeepAspectRatio)
        if keep is not None:
            self.set_crop_rect(keep)

    def crop_rect(self) -> Optional[QRectF]:
        return self._rect.rect() if self._rect is not None else None

    def set_crop_rect(self, r: Optional[QRectF]) -> None:
        if self._rect is not None:
            self.scene().removeItem(self._rect)
            self._rect = None
        if r is not None and r.width() > 1:
            pen = QPen(QColor("#00e5ff"), 2)
            pen.setCosmetic(True)
            self._rect = self.scene().addRect(r, pen, QBrush(QColor(0, 229, 255, 40)))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._pix is not None:
            self.fitInView(self.scene().sceneRect(), Qt.KeepAspectRatio)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self._pix is not None:
            self._start = self.mapToScene(e.position().toPoint())
            self.set_crop_rect(QRectF(self._start, self._start))
            self._rect = self._rect or None
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._start is not None:
            p = self.mapToScene(e.position().toPoint())
            r = QRectF(self._start, p).normalized().intersected(self.scene().sceneRect())
            if self._rect is None:
                self.set_crop_rect(r)
            else:
                self._rect.setRect(r)
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._start = None
        if self._rect is not None and (self._rect.rect().width() < 8 or self._rect.rect().height() < 8):
            self.set_crop_rect(None)
        super().mouseReleaseEvent(e)


class PrepareDialog(QDialog):
    """Rotate/deskew the source and drag a crop rectangle that excludes titles and legends."""

    def __init__(self, source_rgb: np.ndarray, prep: PrepParams, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Crop and rotate")
        self._src = source_rgb
        self._thumb, self._scale = make_thumbnail(source_rgb, 1400)
        self._canvas_scale = 1.0
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Drag on the image to select the map area (titles, legends and margins outside it "
                             "are excluded). Rotation is applied first, then the crop."))
        self.view = _CropView()
        lay.addWidget(self.view, 1)
        row = QHBoxLayout()
        self.angle = QDoubleSpinBox()
        self.angle.setRange(-180, 180)
        self.angle.setDecimals(2)
        self.angle.setSingleStep(0.1)
        self.angle.setSuffix("° (counter-clockwise)")
        self.angle.setValue(prep.rotation_deg)
        row.addWidget(QLabel("Rotation"))
        row.addWidget(self.angle)
        deskew = QPushButton("Auto-deskew")
        deskew.clicked.connect(self._auto)
        clear = QPushButton("Clear crop")
        clear.clicked.connect(lambda: self.view.set_crop_rect(None))
        row.addWidget(deskew)
        row.addWidget(clear)
        row.addStretch(1)
        lay.addLayout(row)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.resize(900, 760)
        self._refresh_preview()
        if prep.crop:
            x, y, w, h = prep.crop
            s = self._scale
            self.view.set_crop_rect(QRectF(x * s, y * s, w * s, h * s))
        self.angle.valueChanged.connect(self._on_angle)
        self._last_angle = self.angle.value()

    def _refresh_preview(self) -> None:
        img, _ = rotated_preview(self._thumb, self.angle.value())
        self.view.set_pixmap(QPixmap.fromImage(numpy_to_qimage(img)).copy())

    def _on_angle(self, v: float) -> None:
        self.view.set_crop_rect(None)       # a crop drawn for another angle would be meaningless
        self._refresh_preview()

    def _auto(self) -> None:
        self.angle.setValue(estimate_skew_angle(self._src))

    def prep(self) -> PrepParams:
        r = self.view.crop_rect()
        crop = None
        if r is not None:
            s = self._scale
            crop = (int(round(r.x() / s)), int(round(r.y() / s)), int(round(r.width() / s)), int(round(r.height() / s)))
        return PrepParams(rotation_deg=float(self.angle.value()), crop=crop)


class ExportDialog(QDialog):
    """Choose export format and coordinate options."""

    def __init__(self, options: ExportOptions, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export parcels")
        lay = QVBoxLayout(self)
        note = QLabel("<b>Coordinates are image pixels, not geographic coordinates.</b><br>"
                      "No CRS is assigned. Georeference the polygons later in QGIS (see README).")
        note.setWordWrap(True)
        lay.addWidget(note)
        form = QFormLayout()
        self.format = QComboBox()
        for label, key in EXPORT_FORMATS:
            self.format.addItem(label, key)
        form.addRow("Format", self.format)
        self.space = QComboBox()
        self.space.addItem("Working image (cropped/rotated, as shown)", "working")
        self.space.addItem("Original source image", "source")
        self.space.setCurrentIndex(0 if options.coordinate_space == "working" else 1)
        form.addRow("Pixel coordinates relative to", self.space)
        lay.addLayout(form)
        self.flip = QCheckBox("Flip Y axis (y up) so GIS software shows the map upright")
        self.flip.setChecked(options.flip_y)
        self.rejected = QCheckBox("Include parcels marked 'rejected'")
        self.rejected.setChecked(options.include_rejected)
        self.repair = QCheckBox("Repair invalid geometries before export")
        self.repair.setChecked(options.repair_invalid)
        for c in (self.flip, self.rejected, self.repair):
            lay.addWidget(c)
        self.format.currentIndexChanged.connect(self._toggle)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self._toggle()

    def _toggle(self) -> None:
        png = self.format.currentData() == "png"
        for w in (self.space, self.flip, self.repair):
            w.setEnabled(not png)

    def choice(self) -> Tuple[str, ExportOptions]:
        opt = ExportOptions(include_rejected=self.rejected.isChecked(), flip_y=self.flip.isChecked(),
                            coordinate_space=self.space.currentData(), repair_invalid=self.repair.isChecked())
        return self.format.currentData(), opt
