"""Graphics views: the interactive map editor and the read-only processed-image viewer."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QBrush, QColor, QCursor, QImage, QKeyEvent, QPainter, QPainterPath, QPen,
                           QPixmap, QPolygonF)
from PySide6.QtWidgets import (QGraphicsEllipseItem, QGraphicsItem, QGraphicsPathItem,
                               QGraphicsPixmapItem, QGraphicsRectItem, QGraphicsScene,
                               QGraphicsSimpleTextItem, QGraphicsView, QStyle, QStyleOptionGraphicsItem)
from shapely.geometry import Point, Polygon

from core.polygon_editing import nearest_edge_distance, polygon_from_rings, polygon_rings
from core.polygon_validation import is_invalid
from models.parcel import Parcel

log = logging.getLogger(__name__)

STATUS_COLORS = {
    "unreviewed": "#2f6fff",
    "valid": "#1fae4b",
    "uncertain": "#ff9f00",
    "rejected": "#8c8c8c",
}


class Tool(Enum):
    PAN = "pan"
    SELECT = "select"
    EDIT = "edit"
    SPLIT = "split"
    DRAW = "draw"
    ROI = "roi"


@dataclass
class ViewStyle:
    """User-adjustable appearance of the overlays."""

    show_image: bool = True
    show_lines: bool = False
    show_polygons: bool = True
    show_labels: bool = True
    fill_opacity: int = 25          # percent
    line_width: float = 2.0         # screen pixels (cosmetic pen)
    colors: Dict[str, str] = field(default_factory=lambda: dict(STATUS_COLORS))


# --------------------------------------------------------------------------- helpers
def numpy_to_qimage(arr: np.ndarray) -> QImage:
    """Wrap an RGB (H,W,3) or gray (H,W) uint8 array. The returned QImage references `arr`."""
    arr = np.ascontiguousarray(arr)
    h, w = arr.shape[:2]
    if arr.ndim == 3:
        img = QImage(arr.data, w, h, arr.strides[0], QImage.Format_RGB888)
    else:
        img = QImage(arr.data, w, h, arr.strides[0], QImage.Format_Grayscale8)
    img._keepalive = arr  # type: ignore[attr-defined]
    return img


def mask_to_overlay_qimage(mask: np.ndarray, rgba: Tuple[int, int, int, int]) -> QImage:
    """Indexed8 image: 0 -> transparent, 1 -> rgba. (1 byte/pixel before Qt converts it.)"""
    idx = np.ascontiguousarray((mask > 0).astype(np.uint8))
    h, w = idx.shape
    img = QImage(idx.data, w, h, idx.strides[0], QImage.Format_Indexed8)
    img.setColorTable([0x00000000, QColor(*rgba).rgba()])
    img._keepalive = idx  # type: ignore[attr-defined]
    return img


def parcel_path(geom: Polygon) -> QPainterPath:
    """Painter path with holes (use with the odd-even fill rule)."""
    path = QPainterPath()
    path.setFillRule(Qt.OddEvenFill)
    for ring in polygon_rings(geom):
        poly = QPolygonF([QPointF(x, y) for x, y in ring])
        path.addPolygon(poly)
        path.closeSubpath()
    return path


# --------------------------------------------------------------------------- items
class ParcelItem(QGraphicsPathItem):
    """A selectable parcel polygon with a status-coloured fill."""

    def __init__(self, parcel: Parcel, style: ViewStyle) -> None:
        super().__init__()
        self.parcel = parcel
        self._style = style
        self._invalid = False
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setAcceptHoverEvents(True)
        self._label = QGraphicsSimpleTextItem(self)
        self._label.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
        self._label.setAcceptedMouseButtons(Qt.NoButton)
        self._label.setZValue(1)
        self.update_parcel(parcel)

    def update_parcel(self, parcel: Parcel) -> None:
        self.parcel = parcel
        self.setPath(parcel_path(parcel.geometry))
        self.setZValue(2.0 + 1.0 / (1.0 + max(parcel.pixel_area, 0.0) ** 0.5))
        self._invalid = is_invalid(parcel.geometry)
        self.setToolTip(f"{parcel.parcel_id}  dag: {parcel.dag_number or '-'}\n"
                        f"{parcel.pixel_area:.0f} px²  ·  {parcel.vertex_count} vertices\n"
                        f"status: {parcel.validation_status}" + ("\nINVALID GEOMETRY" if self._invalid else ""))
        self._label.setText(parcel.dag_number)
        try:
            pt = parcel.geometry.representative_point()
            self._label.setPos(pt.x, pt.y)
        except Exception:  # empty geometry
            pass
        self.restyle(self._style)

    def restyle(self, style: ViewStyle) -> None:
        self._style = style
        color = QColor(style.colors.get(self.parcel.validation_status, "#2f6fff"))
        selected = self.isSelected()
        pen_color = QColor("#ffee00") if selected else color
        pen = QPen(pen_color, style.line_width * (1.8 if selected else 1.0))
        pen.setCosmetic(True)
        if self._invalid:
            pen.setColor(QColor("#ff00cc"))
            pen.setStyle(Qt.DashLine)
        elif self.parcel.validation_status == "rejected":
            pen.setStyle(Qt.DotLine)
        self.setPen(pen)
        fill = QColor(color)
        fill.setAlpha(int(255 * (style.fill_opacity + (25 if selected else 0)) / 100))
        self.setBrush(QBrush(fill))
        self.setVisible(style.show_polygons)
        self._label.setVisible(style.show_labels and bool(self.parcel.dag_number))
        self._label.setBrush(QBrush(QColor("black")))

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemSelectedHasChanged:
            self.restyle(self._style)
        return super().itemChange(change, value)

    def paint(self, painter, option, widget=None):
        opt = QStyleOptionGraphicsItem(option)
        opt.state &= ~QStyle.State_Selected      # no dotted selection rectangle
        super().paint(painter, opt, widget)

    def preview_geometry(self, geom: Polygon) -> None:
        self.setPath(parcel_path(geom))


class VertexHandle(QGraphicsEllipseItem):
    """Draggable vertex marker; right-click deletes the vertex."""

    def __init__(self, owner: "EditorView", ring: int, index: int, x: float, y: float) -> None:
        super().__init__(-4.5, -4.5, 9, 9)
        self.owner, self.ring, self.index = owner, ring, index
        self.setPos(x, y)
        self.setFlags(QGraphicsItem.ItemIsMovable | QGraphicsItem.ItemSendsGeometryChanges |
                      QGraphicsItem.ItemIgnoresTransformations)
        self.setBrush(QBrush(QColor("#ffffff")))
        pen = QPen(QColor("#d00000"), 2)
        pen.setCosmetic(True)
        self.setPen(pen)
        self.setZValue(20)
        self.setCursor(Qt.SizeAllCursor)
        self._moving = False

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged and self._moving:
            self.owner._handle_moved()
        return super().itemChange(change, value)

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            event.accept()
            ring, idx = self.ring, self.index
            QTimer.singleShot(0, lambda: self.owner._request_vertex_delete(ring, idx))
            return
        self._moving = True
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if self._moving:
            self._moving = False
            QTimer.singleShot(0, self.owner._handle_released)


# --------------------------------------------------------------------------- views
class ImageView(QGraphicsView):
    """Zoom (wheel), pan (middle button / Space+drag / Pan tool), fit and view synchronisation."""

    view_changed = Signal()
    cursor_moved = Signal(float, float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setBackgroundBrush(QBrush(QColor("#3a3d41")))
        self.setMouseTracking(True)
        self.setViewportUpdateMode(QGraphicsView.SmartViewportUpdate)
        self._pix: Optional[QGraphicsPixmapItem] = None
        self._image_rect = QRectF()
        self._pan_last: Optional[QPointF] = None
        self._space = False
        self._syncing = False
        self.auto_fit = False
        self.horizontalScrollBar().valueChanged.connect(self._emit_view_changed)
        self.verticalScrollBar().valueChanged.connect(self._emit_view_changed)

    # ---- image ----
    def set_image(self, qimage: Optional[QImage], keep_view: bool = False) -> None:
        if self._pix is not None:
            self.scene().removeItem(self._pix)
            self._pix = None
        if qimage is None:
            self._image_rect = QRectF()
            return
        pix = QPixmap.fromImage(qimage)
        self._pix = self.scene().addPixmap(pix)
        self._pix.setZValue(0)
        self._image_rect = QRectF(0, 0, pix.width(), pix.height())
        m = max(self._image_rect.width(), self._image_rect.height()) * 0.25
        self.scene().setSceneRect(self._image_rect.adjusted(-m, -m, m, m))
        if not keep_view:
            self.fit()

    @property
    def has_image(self) -> bool:
        return self._pix is not None

    def set_image_visible(self, visible: bool) -> None:
        if self._pix is not None:
            self._pix.setVisible(visible)

    def fit(self) -> None:
        if not self._image_rect.isEmpty():
            self.fitInView(self._image_rect, Qt.KeepAspectRatio)
            self.auto_fit = True
            self._emit_view_changed()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.auto_fit and not self._image_rect.isEmpty():
            self.fitInView(self._image_rect, Qt.KeepAspectRatio)
            self._emit_view_changed()

    def zoom_actual(self) -> None:
        self.resetTransform()
        self._emit_view_changed()

    def scale_factor(self) -> float:
        return self.transform().m11()

    # ---- view sync ----
    def _emit_view_changed(self, *_):
        if not self._syncing:
            self.view_changed.emit()

    def view_state(self) -> Tuple[QPointF, float]:
        return self.mapToScene(self.viewport().rect().center()), self.scale_factor()

    def apply_view(self, center: QPointF, scale: float) -> None:
        self._syncing = True
        try:
            self.resetTransform()
            self.scale(scale, scale)
            self.centerOn(center)
        finally:
            self._syncing = False

    # ---- interaction ----
    def wheelEvent(self, event):
        if self._pix is None:
            return
        self.auto_fit = False
        factor = 1.25 ** (event.angleDelta().y() / 120.0)
        new = self.scale_factor() * factor
        if 0.01 <= new <= 80:
            self.scale(factor, factor)
            self._emit_view_changed()

    def _wants_pan(self, event) -> bool:
        return (event.button() == Qt.MiddleButton or
                (event.button() == Qt.LeftButton and (self._space or self._pan_tool_active())))

    def _pan_tool_active(self) -> bool:
        return False

    def mousePressEvent(self, event):
        if self._wants_pan(event):
            self.auto_fit = False
            self._pan_last = event.position()
            self.viewport().setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        p = self.mapToScene(event.position().toPoint())
        self.cursor_moved.emit(p.x(), p.y())
        if self._pan_last is not None:
            d = event.position() - self._pan_last
            self._pan_last = event.position()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - int(d.x()))
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - int(d.y()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._pan_last is not None:
            self._pan_last = None
            self.viewport().unsetCursor()
            self._restore_cursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _restore_cursor(self) -> None:
        pass

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key_Space and not event.isAutoRepeat():
            self._space = True
            self.viewport().setCursor(Qt.OpenHandCursor)
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key_Space and not event.isAutoRepeat():
            self._space = False
            self.viewport().unsetCursor()
            self._restore_cursor()
            return
        super().keyReleaseEvent(event)


class EditorView(ImageView):
    """Original map + detected lines + editable parcel polygons."""

    selection_changed = Signal(list)                 # list[str] parcel ids
    geometry_edited = Signal(str, object)            # parcel id, shapely Polygon
    vertex_insert_requested = Signal(str, float, float)
    vertex_delete_requested = Signal(str, int, int)
    split_drawn = Signal(list)                       # list[(x, y)]
    polygon_drawn = Signal(list)
    roi_selected = Signal(QRectF)
    escape_pressed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.style = ViewStyle()
        self.tool = Tool.SELECT
        self._items: Dict[str, ParcelItem] = {}
        self._lines: Optional[QGraphicsPixmapItem] = None
        self._handles: List[VertexHandle] = []
        self._edit_id: Optional[str] = None
        self._poly_pts: List[Tuple[float, float]] = []
        self._tool_item: Optional[QGraphicsPathItem] = None
        self._roi_start: Optional[QPointF] = None
        self._roi_item: Optional[QGraphicsRectItem] = None
        self._emit_selection = True
        self.scene().selectionChanged.connect(self._on_scene_selection)
        self.set_tool(Tool.SELECT)

    # ---- tools ----
    def _pan_tool_active(self) -> bool:
        return self.tool == Tool.PAN

    def set_tool(self, tool: Tool) -> None:
        self._cancel_tool()
        self.tool = tool
        self.setDragMode(QGraphicsView.RubberBandDrag if tool in (Tool.SELECT, Tool.EDIT)
                         else QGraphicsView.NoDrag)
        self._restore_cursor()
        self._rebuild_handles()

    def _restore_cursor(self) -> None:
        cursors = {Tool.PAN: Qt.OpenHandCursor, Tool.SELECT: Qt.ArrowCursor, Tool.EDIT: Qt.ArrowCursor,
                   Tool.SPLIT: Qt.CrossCursor, Tool.DRAW: Qt.CrossCursor, Tool.ROI: Qt.CrossCursor}
        self.viewport().setCursor(cursors[self.tool])

    def _cancel_tool(self) -> None:
        self._poly_pts = []
        for attr in ("_tool_item", "_roi_item"):
            item = getattr(self, attr)
            if item is not None:
                self.scene().removeItem(item)
                setattr(self, attr, None)
        self._roi_start = None

    # ---- image layers ----
    def set_image(self, qimage, keep_view: bool = False) -> None:
        self.set_lines_overlay(None)
        super().set_image(qimage, keep_view)

    def set_lines_overlay(self, qimage: Optional[QImage]) -> None:
        if self._lines is not None:
            self.scene().removeItem(self._lines)
            self._lines = None
        if qimage is not None:
            self._lines = self.scene().addPixmap(QPixmap.fromImage(qimage))
            self._lines.setZValue(1)
            self._lines.setOpacity(0.8)
            self._lines.setVisible(self.style.show_lines)

    def clear_all(self) -> None:
        for it in list(self._items.values()):
            self.scene().removeItem(it)
        self._items.clear()
        self._clear_handles()
        self.set_lines_overlay(None)

    # ---- parcels ----
    def set_style(self, style: ViewStyle) -> None:
        self.style = style
        self.set_image_visible(style.show_image)
        if self._lines is not None:
            self._lines.setVisible(style.show_lines)
        for it in self._items.values():
            it.restyle(style)

    def sync_parcels(self, parcels: Sequence[Parcel]) -> None:
        """Incrementally bring the scene in line with the store (by object identity)."""
        before = set(self.selected_ids())
        self._emit_selection = False
        try:
            seen = set()
            for p in parcels:
                seen.add(p.parcel_id)
                it = self._items.get(p.parcel_id)
                if it is None:
                    it = ParcelItem(p, self.style)
                    self.scene().addItem(it)
                    self._items[p.parcel_id] = it
                elif it.parcel is not p:
                    it.update_parcel(p)
            for pid in [k for k in self._items if k not in seen]:
                self.scene().removeItem(self._items.pop(pid))
        finally:
            self._emit_selection = True
        self._rebuild_handles()
        if set(self.selected_ids()) != before:
            self.selection_changed.emit(self.selected_ids())

    def selected_ids(self) -> List[str]:
        return [it.parcel.parcel_id for it in self.scene().selectedItems() if isinstance(it, ParcelItem)]

    def select_ids(self, ids: Sequence[str]) -> None:
        self._emit_selection = False
        try:
            self.scene().clearSelection()
            for pid in ids:
                if pid in self._items:
                    self._items[pid].setSelected(True)
        finally:
            self._emit_selection = True
        self._rebuild_handles()
        self.selection_changed.emit(self.selected_ids())

    def zoom_to_parcel(self, pid: str) -> None:
        it = self._items.get(pid)
        if it is not None:
            r = it.sceneBoundingRect()
            m = max(r.width(), r.height()) * 0.6 + 20
            self.fitInView(r.adjusted(-m, -m, m, m), Qt.KeepAspectRatio)
            self._emit_view_changed()

    def parcel_at(self, x: float, y: float) -> Optional[str]:
        pt = Point(x, y)
        best, best_area = None, float("inf")
        for pid, it in self._items.items():
            g = it.parcel.geometry
            if not g.is_empty and g.contains(pt) and g.area < best_area:
                best, best_area = pid, g.area
        return best

    def _on_scene_selection(self) -> None:
        if not self._emit_selection:
            return
        self._rebuild_handles()
        self.selection_changed.emit(self.selected_ids())

    # ---- vertex editing ----
    def _clear_handles(self) -> None:
        for h in self._handles:
            self.scene().removeItem(h)
        self._handles = []
        self._edit_id = None

    def _rebuild_handles(self) -> None:
        self._clear_handles()
        if self.tool != Tool.EDIT:
            return
        ids = self.selected_ids()
        if len(ids) != 1:
            return
        it = self._items.get(ids[0])
        if it is None:
            return
        self._edit_id = ids[0]
        for ri, ring in enumerate(polygon_rings(it.parcel.geometry)):
            for vi, (x, y) in enumerate(ring):
                h = VertexHandle(self, ri, vi, x, y)
                self.scene().addItem(h)
                self._handles.append(h)

    def _current_geometry_from_handles(self) -> Optional[Polygon]:
        rings: Dict[int, List[Tuple[int, Tuple[float, float]]]] = {}
        for h in self._handles:
            rings.setdefault(h.ring, []).append((h.index, (h.pos().x(), h.pos().y())))
        try:
            return polygon_from_rings([[c for _, c in sorted(rings[r])] for r in sorted(rings)])
        except Exception:
            return None

    def _handle_moved(self) -> None:
        geom = self._current_geometry_from_handles()
        it = self._items.get(self._edit_id or "")
        if geom is not None and it is not None:
            it.preview_geometry(geom)

    def _handle_released(self) -> None:
        geom = self._current_geometry_from_handles()
        pid = self._edit_id
        if geom is not None and pid is not None and pid in self._items:
            if not geom.equals_exact(self._items[pid].parcel.geometry, 1e-9):
                self.geometry_edited.emit(pid, geom)
                return
        self._rebuild_handles()

    def _request_vertex_delete(self, ring: int, index: int) -> None:
        if self._edit_id:
            self.vertex_delete_requested.emit(self._edit_id, ring, index)

    # ---- mouse / keyboard ----
    def mousePressEvent(self, event):
        if self._wants_pan(event):
            return super().mousePressEvent(event)
        p = self.mapToScene(event.position().toPoint())
        if self.tool in (Tool.SPLIT, Tool.DRAW) and event.button() == Qt.LeftButton:
            self._poly_pts.append((p.x(), p.y()))
            self._update_tool_path(p)
            event.accept()
            return
        if self.tool == Tool.ROI and event.button() == Qt.LeftButton:
            self._roi_start = p
            self._roi_item = self.scene().addRect(QRectF(p, p), self._tool_pen(), QBrush(QColor(255, 255, 255, 30)))
            self._roi_item.setZValue(30)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        p = self.mapToScene(event.position().toPoint())
        if self._poly_pts and self.tool in (Tool.SPLIT, Tool.DRAW):
            self._update_tool_path(p)
        if self._roi_start is not None and self._roi_item is not None:
            self._roi_item.setRect(QRectF(self._roi_start, p).normalized())
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._roi_start is not None and event.button() == Qt.LeftButton:
            rect = self._roi_item.rect() if self._roi_item else QRectF()
            self._cancel_tool()
            if rect.width() > 5 and rect.height() > 5:
                self.roi_selected.emit(rect)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() != Qt.LeftButton:
            return super().mouseDoubleClickEvent(event)
        p = self.mapToScene(event.position().toPoint())
        if self.tool in (Tool.SPLIT, Tool.DRAW):
            self._finish_polyline()
            event.accept()
            return
        if self.tool == Tool.EDIT and self._edit_id and self._edit_id in self._items:
            geom = self._items[self._edit_id].parcel.geometry
            if nearest_edge_distance(geom, (p.x(), p.y())) <= 10.0 / max(self.scale_factor(), 1e-6):
                self.vertex_insert_requested.emit(self._edit_id, p.x(), p.y())
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and self.tool in (Tool.SPLIT, Tool.DRAW):
            self._finish_polyline()
            return
        if event.key() == Qt.Key_Escape:
            self._cancel_tool()
            self.escape_pressed.emit()
            return
        if event.key() == Qt.Key_Backspace and self._poly_pts:
            self._poly_pts.pop()
            self._update_tool_path(None)
            return
        super().keyPressEvent(event)

    def _tool_pen(self) -> QPen:
        pen = QPen(QColor("#00e5ff"), 2, Qt.DashLine)
        pen.setCosmetic(True)
        return pen

    def _update_tool_path(self, cursor: Optional[QPointF]) -> None:
        path = QPainterPath()
        if self._poly_pts:
            path.moveTo(*self._poly_pts[0])
            for pt in self._poly_pts[1:]:
                path.lineTo(*pt)
            if cursor is not None:
                path.lineTo(cursor)
            if self.tool == Tool.DRAW and len(self._poly_pts) > 1:
                path.lineTo(*self._poly_pts[0])
        if self._tool_item is None:
            self._tool_item = self.scene().addPath(path, self._tool_pen())
            self._tool_item.setZValue(30)
        else:
            self._tool_item.setPath(path)

    def _finish_polyline(self) -> None:
        pts = list(self._poly_pts)
        tool = self.tool
        self._cancel_tool()
        if tool == Tool.SPLIT and len(pts) >= 2:
            self.split_drawn.emit(pts)
        elif tool == Tool.DRAW and len(pts) >= 3:
            self.polygon_drawn.emit(pts)


class ProcessedView(ImageView):
    """Read-only binary/processed image with thin parcel outlines."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._outlines: Dict[str, QGraphicsPathItem] = {}
        self._parcels: Dict[str, Parcel] = {}
        self.show_outlines = True

    def sync_parcels(self, parcels: Sequence[Parcel]) -> None:
        seen = set()
        pen = QPen(QColor("#ff4040"), 1.2)
        pen.setCosmetic(True)
        for p in parcels:
            seen.add(p.parcel_id)
            if self._parcels.get(p.parcel_id) is p:
                continue
            old = self._outlines.pop(p.parcel_id, None)
            if old is not None:
                self.scene().removeItem(old)
            item = self.scene().addPath(parcel_path(p.geometry), pen)
            item.setZValue(2)
            item.setVisible(self.show_outlines)
            self._outlines[p.parcel_id] = item
            self._parcels[p.parcel_id] = p
        for pid in [k for k in self._outlines if k not in seen]:
            self.scene().removeItem(self._outlines.pop(pid))
            self._parcels.pop(pid, None)

    def set_outlines_visible(self, visible: bool) -> None:
        self.show_outlines = visible
        for it in self._outlines.values():
            it.setVisible(visible)

    def clear_all(self) -> None:
        for it in self._outlines.values():
            self.scene().removeItem(it)
        self._outlines.clear()
        self._parcels.clear()
