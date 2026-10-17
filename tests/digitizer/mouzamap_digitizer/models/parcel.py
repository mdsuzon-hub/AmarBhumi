"""Parcel data model.

A Parcel is an immutable value object: every edit produces a new Parcel via
`dataclasses.replace`. That makes undo/redo snapshots cheap (they only hold references)
and lets the GUI detect changes by object identity.

Geometry is in working-image pixel coordinates (origin top-left, y down).
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Dict, List, Tuple

from shapely.geometry import Polygon

VALIDATION_STATUSES: Tuple[str, ...] = ("unreviewed", "valid", "uncertain", "rejected")
EXTRACTION_METHODS: Tuple[str, ...] = ("automatic", "manually edited")

Coord = Tuple[float, float]


def ring_coords(ring) -> List[Coord]:
    """Open ring (no repeated closing vertex) as a list of (x, y)."""
    coords = [(float(x), float(y)) for x, y in ring.coords]
    if len(coords) > 1 and coords[0] == coords[-1]:
        coords.pop()
    return coords


@dataclass(frozen=True)
class Parcel:
    parcel_id: str
    geometry: Polygon
    dag_number: str = ""
    extraction_method: str = "automatic"
    validation_status: str = "unreviewed"
    source_page: int = 1
    notes: str = ""

    # ---- derived attributes -------------------------------------------------
    @property
    def pixel_area(self) -> float:
        """Area in image pixels squared (NOT a real-world area)."""
        return float(self.geometry.area)

    @property
    def vertex_count(self) -> int:
        """Number of distinct vertices over the exterior and all interior rings."""
        g = self.geometry
        if g.is_empty:
            return 0
        return len(g.exterior.coords) - 1 + sum(len(r.coords) - 1 for r in g.interiors)

    def with_changes(self, **changes) -> "Parcel":
        return replace(self, **changes)

    def attributes(self) -> Dict[str, object]:
        """The attribute set written to exports (spec section 7)."""
        return {
            "parcel_id": self.parcel_id,
            "dag_number": self.dag_number,
            "pixel_area": round(self.pixel_area, 3),
            "vertex_count": self.vertex_count,
            "extraction_method": self.extraction_method,
            "validation_status": self.validation_status,
            "source_page": int(self.source_page),
            "notes": self.notes,
        }

    # ---- (de)serialisation --------------------------------------------------
    def to_dict(self) -> dict:
        g = self.geometry
        return {
            "parcel_id": self.parcel_id,
            "dag_number": self.dag_number,
            "extraction_method": self.extraction_method,
            "validation_status": self.validation_status,
            "source_page": self.source_page,
            "notes": self.notes,
            "exterior": [list(c) for c in ring_coords(g.exterior)],
            "holes": [[list(c) for c in ring_coords(r)] for r in g.interiors],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Parcel":
        geom = Polygon([tuple(c) for c in d["exterior"]], [[tuple(c) for c in h] for h in d.get("holes", [])])
        return cls(
            parcel_id=str(d["parcel_id"]),
            geometry=geom,
            dag_number=str(d.get("dag_number", "")),
            extraction_method=d.get("extraction_method", "automatic"),
            validation_status=d.get("validation_status", "unreviewed"),
            source_page=int(d.get("source_page", 1)),
            notes=str(d.get("notes", "")),
        )
