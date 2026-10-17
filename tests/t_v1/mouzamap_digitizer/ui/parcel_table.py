"""Editable parcel list (Qt model/view) and the parcel information panel."""
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import (QAbstractTableModel, QItemSelection, QItemSelectionModel, QModelIndex,
                            QSortFilterProxyModel, Qt, Signal)
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFormLayout, QGroupBox, QLabel,
                               QStyledItemDelegate, QTableView, QWidget)

from core.polygon_editing import ParcelStore
from core.polygon_validation import check_geometry, find_duplicates
from core.settings import ValidationParams
from models.parcel import VALIDATION_STATUSES, Parcel

COLUMNS: List[Tuple[str, str]] = [
    ("ID", "parcel_id"), ("Dag no.", "dag_number"), ("Area (px²)", "pixel_area"),
    ("Vertices", "vertex_count"), ("Method", "extraction_method"), ("Status", "validation_status"),
    ("Page", "source_page"), ("Notes", "notes"), ("Geometry", "check"),
]
EDITABLE = {"dag_number", "notes", "validation_status"}
TINT = {"valid": "#d8f3df", "uncertain": "#ffe9c2", "rejected": "#e4e4e4", "unreviewed": None}


class ParcelTableModel(QAbstractTableModel):
    def __init__(self, store: ParcelStore, vparams: Callable[[], ValidationParams], parent=None) -> None:
        super().__init__(parent)
        self._store = store
        self._vparams = vparams
        self._parcels: Tuple[Parcel, ...] = ()
        self._cache: Dict[str, Tuple[Parcel, List]] = {}
        self._dups: Dict[str, List[str]] = {}
        self.refresh()

    def set_store(self, store: ParcelStore) -> None:
        self._store = store
        self._cache.clear()
        self.refresh()

    def refresh(self) -> None:
        self.beginResetModel()
        self._parcels = self._store.parcels
        vp = self._vparams()
        self._dups = find_duplicates(self._parcels, vp.duplicate_iou)
        fresh: Dict[str, Tuple[Parcel, List]] = {}
        for p in self._parcels:
            hit = self._cache.get(p.parcel_id)
            fresh[p.parcel_id] = hit if hit and hit[0] is p else (p, check_geometry(p.geometry, vp))
        self._cache = fresh
        self.endResetModel()

    def issues(self, p: Parcel) -> List[str]:
        items = [i.message for i in self._cache.get(p.parcel_id, (p, []))[1]]
        if p.parcel_id in self._dups:
            items.append("Duplicate of " + ", ".join(self._dups[p.parcel_id]))
        return items

    def is_invalid(self, p: Parcel) -> bool:
        return any(i.code in ("invalid_geometry", "empty") for i in self._cache.get(p.parcel_id, (p, []))[1])

    def parcel_at(self, row: int) -> Parcel:
        return self._parcels[row]

    # ---- Qt model API ----
    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._parcels)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return COLUMNS[section][0]
        return None

    def _raw(self, p: Parcel, key: str):
        if key == "check":
            return "; ".join(self.issues(p)) or "OK"
        if key in ("pixel_area",):
            return round(p.pixel_area, 1)
        if key == "vertex_count":
            return p.vertex_count
        return getattr(p, key)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        p = self._parcels[index.row()]
        key = COLUMNS[index.column()][1]
        if role in (Qt.DisplayRole, Qt.EditRole):
            return self._raw(p, key)
        if role == Qt.UserRole:
            return self._raw(p, key)
        if role == Qt.BackgroundRole:
            if key == "check" and self.is_invalid(p):
                return QColor("#ffc4ec")
            tint = TINT.get(p.validation_status)
            return QColor(tint) if tint else None
        if role == Qt.ToolTipRole and key in ("check", "notes"):
            return str(self._raw(p, key))
        if role == Qt.TextAlignmentRole and key in ("pixel_area", "vertex_count", "source_page"):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        return None

    def flags(self, index):
        f = super().flags(index)
        if COLUMNS[index.column()][1] in EDITABLE:
            f |= Qt.ItemIsEditable
        return f

    def setData(self, index, value, role=Qt.EditRole):
        if role != Qt.EditRole or not index.isValid():
            return False
        key = COLUMNS[index.column()][1]
        if key not in EDITABLE:
            return False
        if key == "validation_status" and value not in VALIDATION_STATUSES:
            return False
        self._store.set_attrs([self._parcels[index.row()].parcel_id], **{key: str(value)})
        return True


class StatusDelegate(QStyledItemDelegate):
    def createEditor(self, parent, option, index):
        box = QComboBox(parent)
        box.addItems(VALIDATION_STATUSES)
        box.activated.connect(lambda _=0, b=box: (self.commitData.emit(b), self.closeEditor.emit(b)))
        return box

    def setEditorData(self, editor, index):
        editor.setCurrentText(str(index.data(Qt.EditRole)))

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentText(), Qt.EditRole)


class ParcelTable(QTableView):
    """Table with sorting; emits `ids_selected` when the user selects rows."""

    ids_selected = Signal(list)
    zoom_requested = Signal(str)

    def __init__(self, model: ParcelTableModel, parent=None) -> None:
        super().__init__(parent)
        self.source = model
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(model)
        self.proxy.setSortRole(Qt.UserRole)
        self.setModel(self.proxy)
        self.setSortingEnabled(True)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setAlternatingRowColors(False)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(22)
        self.horizontalHeader().setStretchLastSection(False)
        self.setItemDelegateForColumn([c[1] for c in COLUMNS].index("validation_status"), StatusDelegate(self))
        self.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self._block = False
        self.selectionModel().selectionChanged.connect(self._on_selection)
        self.doubleClicked.connect(self._on_double)
        for i, w in enumerate((62, 80, 78, 64, 100, 90, 44, 140, 180)):
            self.setColumnWidth(i, w)

    def _on_double(self, index):
        key = COLUMNS[index.column()][1]
        if key not in EDITABLE:
            p = self.source.parcel_at(self.proxy.mapToSource(index).row())
            self.zoom_requested.emit(p.parcel_id)

    def selected_ids(self) -> List[str]:
        rows = {self.proxy.mapToSource(i).row() for i in self.selectionModel().selectedRows()}
        return [self.source.parcel_at(r).parcel_id for r in sorted(rows)]

    def _on_selection(self, *_):
        if not self._block:
            self.ids_selected.emit(self.selected_ids())

    def select_ids(self, ids: Sequence[str]) -> None:
        wanted = set(ids)
        self._block = True
        try:
            sel = QItemSelection()
            first = None
            for r in range(self.source.rowCount()):
                if self.source.parcel_at(r).parcel_id in wanted:
                    pi = self.proxy.mapFromSource(self.source.index(r, 0))
                    sel.select(pi, self.proxy.index(pi.row(), self.proxy.columnCount() - 1))
                    first = first or pi
            self.selectionModel().select(sel, QItemSelectionModel.ClearAndSelect)
            if first is not None:
                self.scrollTo(first)
        finally:
            self._block = False


class ParcelInfoPanel(QGroupBox):
    """Shows the key facts of the clicked polygon (ID, pixel area, vertices, status)."""

    def __init__(self, parent=None) -> None:
        super().__init__("Selected polygon", parent)
        form = QFormLayout(self)
        self._labels = {k: QLabel("–") for k in ("ID", "Dag number", "Pixel area", "Vertices", "Status", "Checks")}
        self._labels["Checks"].setWordWrap(True)
        for k, w in self._labels.items():
            form.addRow(k + ":", w)

    def show_parcels(self, parcels: Sequence[Parcel], issues: Callable[[Parcel], List[str]]) -> None:
        L = self._labels
        if not parcels:
            for w in L.values():
                w.setText("–")
        elif len(parcels) == 1:
            p = parcels[0]
            L["ID"].setText(p.parcel_id)
            L["Dag number"].setText(p.dag_number or "–")
            L["Pixel area"].setText(f"{p.pixel_area:,.0f} px²   (not a real-world area)")
            L["Vertices"].setText(str(p.vertex_count))
            L["Status"].setText(p.validation_status)
            L["Checks"].setText("; ".join(issues(p)) or "OK")
        else:
            L["ID"].setText(f"{len(parcels)} polygons selected")
            L["Dag number"].setText("–")
            L["Pixel area"].setText(f"{sum(p.pixel_area for p in parcels):,.0f} px² total")
            L["Vertices"].setText(str(sum(p.vertex_count for p in parcels)))
            L["Status"].setText(", ".join(sorted({p.validation_status for p in parcels})))
            L["Checks"].setText("–")
