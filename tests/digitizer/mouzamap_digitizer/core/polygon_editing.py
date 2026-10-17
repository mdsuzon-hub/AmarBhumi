"""Polygon editing operations and the undo/redo-capable ParcelStore.

Geometry helpers are pure functions on Shapely polygons. `ParcelStore` owns the parcel
list; every mutation is recorded as a snapshot (parcels are immutable, so a snapshot is
just a tuple of references) which gives unlimited-feeling undo/redo at negligible cost.
The store has no GUI dependency; the GUI subscribes with `store.subscribe(callback)`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
from shapely import affinity
from shapely.geometry import LineString, MultiPolygon, Point, Polygon
from shapely.ops import split, unary_union

from core.polygon_validation import repair_geometry
from models.parcel import Coord, Parcel, ring_coords

log = logging.getLogger(__name__)

HISTORY_LIMIT = 200


class EditError(Exception):
    """Raised with a user-readable message when an edit cannot be performed."""


# --------------------------------------------------------------------------- geometry helpers
def polygon_rings(poly: Polygon) -> List[List[Coord]]:
    """[exterior, hole1, hole2, ...] as open coordinate lists."""
    return [ring_coords(poly.exterior)] + [ring_coords(r) for r in poly.interiors]


def polygon_from_rings(rings: Sequence[Sequence[Coord]]) -> Polygon:
    if not rings or len(rings[0]) < 3:
        raise EditError("A polygon needs at least 3 vertices.")
    return Polygon(rings[0], [r for r in rings[1:] if len(r) >= 3])


def _closest_segment(ring: Sequence[Coord], pt: Coord) -> Tuple[int, float, Coord]:
    """Index of the segment (i -> i+1) closest to pt, its distance and the projected point."""
    best = (0, float("inf"), ring[0])
    p = np.asarray(pt, float)
    n = len(ring)
    for i in range(n):
        a, b = np.asarray(ring[i], float), np.asarray(ring[(i + 1) % n], float)
        ab = b - a
        denom = float(ab @ ab)
        t = 0.0 if denom == 0 else max(0.0, min(1.0, float((p - a) @ ab) / denom))
        proj = a + t * ab
        d = float(np.hypot(*(p - proj)))
        if d < best[1]:
            best = (i, d, (float(proj[0]), float(proj[1])))
    return best


def nearest_edge_distance(poly: Polygon, pt: Coord) -> float:
    return min(_closest_segment(r, pt)[1] for r in polygon_rings(poly))


def insert_vertex(poly: Polygon, pt: Coord) -> Polygon:
    """Insert a vertex on the edge nearest to `pt` (the point is projected onto the edge)."""
    rings = polygon_rings(poly)
    best_ring, best = 0, (0, float("inf"), (0.0, 0.0))
    for ri, ring in enumerate(rings):
        cand = _closest_segment(ring, pt)
        if cand[1] < best[1]:
            best_ring, best = ri, cand
    seg, _, proj = best
    rings[best_ring].insert(seg + 1, proj)
    return polygon_from_rings(rings)


def delete_vertex(poly: Polygon, ring_index: int, vertex_index: int) -> Polygon:
    rings = polygon_rings(poly)
    ring = rings[ring_index]
    if len(ring) <= 3:
        raise EditError("Cannot delete a vertex: a ring needs at least 3 vertices.")
    del ring[vertex_index]
    return polygon_from_rings(rings)


def _extend_line(line: LineString, length: float) -> LineString:
    """Extend both ends of a polyline straight outwards so it fully crosses a polygon."""
    pts = list(line.coords)

    def push(a, b):
        v = np.array(b, float) - np.array(a, float)
        n = float(np.hypot(*v))
        if n == 0:
            return b
        return tuple(np.array(b, float) + v / n * length)

    pts[0] = push(pts[1], pts[0])
    pts[-1] = push(pts[-2], pts[-1])
    return LineString(pts)


def split_polygon(poly: Polygon, line_pts: Sequence[Coord]) -> List[Polygon]:
    """Split a polygon with a polyline (its ends are extended to cross the polygon)."""
    pts = [tuple(p) for p in line_pts]
    if len(pts) < 2 or all(p == pts[0] for p in pts):
        raise EditError("Draw a split line with at least two distinct points.")
    minx, miny, maxx, maxy = poly.bounds
    reach = 2.0 * float(np.hypot(maxx - minx, maxy - miny)) + 10.0
    cutter = _extend_line(LineString(pts), reach)
    pieces = [g for g in split(poly, cutter).geoms if isinstance(g, Polygon) and g.area > 1e-6]
    if len(pieces) < 2:
        raise EditError("The split line does not cut through the selected polygon.")
    return pieces


def merge_polygons(polys: Sequence[Polygon]) -> Polygon:
    """Union of adjacent polygons. Tiny gaps (<~1px) between them are bridged."""
    if len(polys) < 2:
        raise EditError("Select at least two polygons to merge.")
    union = unary_union(list(polys))
    if isinstance(union, Polygon):
        return union
    bridged = unary_union([p.buffer(1.0, join_style=2) for p in polys]).buffer(-1.0, join_style=2)
    if isinstance(bridged, Polygon):
        return bridged
    raise EditError("The selected polygons are not adjacent, so they cannot be merged into one.")


# --------------------------------------------------------------------------- store
@dataclass(frozen=True)
class _Snapshot:
    parcels: Tuple[Parcel, ...]
    next_id: int
    label: str


Observer = Callable[[], None]


class ParcelStore:
    """Ordered collection of parcels with undo/redo and change notification."""

    def __init__(self, parcels: Iterable[Parcel] = (), next_id: int = 1, source_page: int = 1) -> None:
        self._parcels: Tuple[Parcel, ...] = tuple(parcels)
        self._next_id = max(next_id, self._max_numeric_id() + 1)
        self.source_page = source_page
        self._undo: List[_Snapshot] = []
        self._redo: List[_Snapshot] = []
        self._observers: List[Observer] = []

    # ---- basics ----
    @property
    def parcels(self) -> Tuple[Parcel, ...]:
        return self._parcels

    @property
    def next_id(self) -> int:
        return self._next_id

    def __len__(self) -> int:
        return len(self._parcels)

    def get(self, parcel_id: str) -> Parcel:
        for p in self._parcels:
            if p.parcel_id == parcel_id:
                return p
        raise KeyError(parcel_id)

    def has(self, parcel_id: str) -> bool:
        return any(p.parcel_id == parcel_id for p in self._parcels)

    def subscribe(self, callback: Observer) -> None:
        self._observers.append(callback)

    def _notify(self) -> None:
        for cb in list(self._observers):
            cb()

    def _max_numeric_id(self) -> int:
        best = 0
        for p in self._parcels:
            digits = "".join(ch for ch in p.parcel_id if ch.isdigit())
            if digits:
                best = max(best, int(digits))
        return best

    def _new_id(self) -> str:
        pid = f"P{self._next_id:04d}"
        self._next_id += 1
        return pid

    # ---- history ----
    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    @property
    def undo_label(self) -> str:
        return self._undo[-1].label if self._undo else ""

    @property
    def redo_label(self) -> str:
        return self._redo[-1].label if self._redo else ""

    def _record(self, label: str) -> None:
        self._undo.append(_Snapshot(self._parcels, self._next_id, label))
        del self._undo[:-HISTORY_LIMIT]
        self._redo.clear()

    def undo(self) -> None:
        if not self._undo:
            return
        snap = self._undo.pop()
        self._redo.append(_Snapshot(self._parcels, self._next_id, snap.label))
        self._parcels, self._next_id = snap.parcels, snap.next_id
        self._notify()

    def redo(self) -> None:
        if not self._redo:
            return
        snap = self._redo.pop()
        self._undo.append(_Snapshot(self._parcels, self._next_id, snap.label))
        self._parcels, self._next_id = snap.parcels, snap.next_id
        self._notify()

    def clear_history(self) -> None:
        self._undo.clear()
        self._redo.clear()

    # ---- mutations ----
    def _make(self, geom: Polygon, **attrs) -> Parcel:
        attrs.setdefault("source_page", self.source_page)
        return Parcel(parcel_id=self._new_id(), geometry=geom, **attrs)

    def replace_all(self, items: Iterable[Tuple[Polygon, dict]], label: str = "Detect parcels") -> None:
        """Replace every parcel with freshly extracted (geometry, attributes) items."""
        self._record(label)
        self._next_id = 1
        self._parcels = tuple(self._make(g, **a) for g, a in items)
        self._notify()

    def replace_in_region(self, region: Polygon, items: Iterable[Tuple[Polygon, dict]],
                          keep_manual: bool = True, label: str = "Reprocess area") -> Tuple[int, int]:
        """Replace automatic parcels whose centroid lies inside `region` with new items.

        Manually edited parcels are kept (and new candidates overlapping them are dropped).
        Returns (removed, added).
        """
        self._record(label)
        kept: List[Parcel] = []
        removed = 0
        for p in self._parcels:
            inside = region.contains(p.geometry.representative_point())
            manual = p.extraction_method != "automatic"
            if inside and not (keep_manual and manual):
                removed += 1
            else:
                kept.append(p)
        protected = [p.geometry for p in kept if p.extraction_method != "automatic"
                     and region.intersects(p.geometry)]
        added: List[Parcel] = []
        for g, a in items:
            if not region.contains(g.representative_point()):
                continue
            if any(g.intersection(m).area > 0.5 * g.area for m in protected):
                continue
            added.append(self._make(g, **a))
        self._parcels = tuple(kept + added)
        self._notify()
        return removed, len(added)

    def add_polygon(self, geom: Polygon, **attrs) -> Parcel:
        geom = self._valid_or_raise(geom)
        self._record("Add polygon")
        attrs.setdefault("extraction_method", "manually edited")
        parcel = self._make(geom, **attrs)
        self._parcels += (parcel,)
        self._notify()
        return parcel

    def add_polygons(self, items: Iterable[Tuple[Polygon, dict]], label: str = "Import polygons") -> int:
        """Append several (geometry, attributes) items as one undoable step. Returns count added."""
        new = []
        for geom, attrs in items:
            try:
                geom = self._valid_or_raise(geom)
            except EditError:
                continue
            new.append((geom, attrs))
        if not new:
            return 0
        self._record(label)
        self._parcels += tuple(self._make(g, **a) for g, a in new)
        self._notify()
        return len(new)

    def delete(self, ids: Iterable[str]) -> int:
        idset = set(ids)
        remaining = tuple(p for p in self._parcels if p.parcel_id not in idset)
        removed = len(self._parcels) - len(remaining)
        if removed:
            self._record(f"Delete {removed} polygon(s)")
            self._parcels = remaining
            self._notify()
        return removed

    def set_geometry(self, parcel_id: str, geom: Polygon, label: str = "Edit polygon") -> Parcel:
        old = self.get(parcel_id)
        self._record(label)
        new = old.with_changes(geometry=geom, extraction_method="manually edited")
        self._replace(old, new)
        self._notify()
        return new

    def add_vertex(self, parcel_id: str, pt: Coord) -> Parcel:
        return self.set_geometry(parcel_id, insert_vertex(self.get(parcel_id).geometry, pt), "Add vertex")

    def remove_vertex(self, parcel_id: str, ring_index: int, vertex_index: int) -> Parcel:
        geom = delete_vertex(self.get(parcel_id).geometry, ring_index, vertex_index)
        return self.set_geometry(parcel_id, geom, "Delete vertex")

    def set_attrs(self, ids: Iterable[str], **attrs) -> None:
        """Change attribute fields (dag_number, notes, validation_status, ...) on parcels."""
        idset = set(ids)
        changed = [p for p in self._parcels if p.parcel_id in idset
                   and any(getattr(p, k) != v for k, v in attrs.items())]
        if not changed:
            return
        self._record("Edit attributes")
        lut = {p.parcel_id: p.with_changes(**attrs) for p in changed}
        self._parcels = tuple(lut.get(p.parcel_id, p) for p in self._parcels)
        self._notify()

    def merge(self, ids: Sequence[str]) -> Parcel:
        parcels = [self.get(i) for i in ids]
        merged_geom = merge_polygons([p.geometry for p in parcels])
        self._record(f"Merge {len(parcels)} polygons")
        first = max(parcels, key=lambda p: p.pixel_area)
        dags = [p.dag_number for p in parcels if p.dag_number]
        notes = " | ".join(n for n in dict.fromkeys(p.notes for p in parcels) if n)
        status = "uncertain" if any(p.validation_status == "uncertain" for p in parcels) else "unreviewed"
        new = first.with_changes(geometry=merged_geom, extraction_method="manually edited",
                                 dag_number=dags[0] if dags else "", notes=notes,
                                 validation_status=status)
        idset = set(ids)
        pos = min(i for i, p in enumerate(self._parcels) if p.parcel_id in idset)
        rest = [p for p in self._parcels if p.parcel_id not in idset]
        rest.insert(min(pos, len(rest)), new)
        self._parcels = tuple(rest)
        self._notify()
        return new

    def split(self, parcel_id: str, line_pts: Sequence[Coord]) -> List[Parcel]:
        old = self.get(parcel_id)
        pieces = split_polygon(old.geometry, line_pts)
        self._record("Split polygon")
        pieces.sort(key=lambda g: -g.area)
        created: List[Parcel] = []
        for k, g in enumerate(pieces):
            if k == 0:   # largest piece keeps the identity and dag number
                created.append(old.with_changes(geometry=g, extraction_method="manually edited"))
            else:
                created.append(Parcel(self._new_id(), g, extraction_method="manually edited",
                                      validation_status="unreviewed", source_page=old.source_page,
                                      notes=f"split from {old.parcel_id}"))
        out: List[Parcel] = []
        for p in self._parcels:
            out.extend(created if p.parcel_id == parcel_id else [p])
        self._parcels = tuple(out)
        self._notify()
        return created

    def transform_all(self, matrix3: np.ndarray, label: str = "Transform polygons") -> None:
        """Apply a 3x3 affine to every parcel (used when crop/rotation of the image changes)."""
        m = np.asarray(matrix3, float)
        params = [m[0, 0], m[0, 1], m[1, 0], m[1, 1], m[0, 2], m[1, 2]]
        self._record(label)
        self._parcels = tuple(p.with_changes(geometry=affinity.affine_transform(p.geometry, params))
                              for p in self._parcels)
        self._notify()

    def repair(self, parcel_id: str) -> Parcel:
        old = self.get(parcel_id)
        fixed = repair_geometry(old.geometry)
        if fixed is None:
            raise EditError("This polygon cannot be repaired automatically.")
        return self.set_geometry(parcel_id, fixed, "Repair geometry")

    # ---- internals ----
    def _replace(self, old: Parcel, new: Parcel) -> None:
        self._parcels = tuple(new if p is old else p for p in self._parcels)

    @staticmethod
    def _valid_or_raise(geom: Polygon) -> Polygon:
        if geom.is_empty:
            raise EditError("The polygon is empty.")
        if not geom.is_valid:
            geom = repair_geometry(geom)
            if geom is None:
                raise EditError("The drawn polygon is invalid (self-intersecting).")
        if geom.area <= 0:
            raise EditError("The polygon has no area.")
        return geom

    def load(self, parcels: Iterable[Parcel], next_id: int) -> None:
        """Replace content without recording history (used when opening a project)."""
        self._parcels = tuple(parcels)
        self._next_id = max(next_id, self._max_numeric_id() + 1)
        self.clear_history()
        self._notify()
