"""Georeferencing data model and helpers.

The digitizer itself never invents geographic coordinates. This module only provides the
*data model* (ground control points) and the maths needed once the user has supplied real
reference points, so that future GUI versions - or the bundled
`scripts/georeference_polygons.py` - can transform pixel polygons into a real CRS.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Iterable, Sequence, Tuple

import numpy as np
from shapely import affinity
from shapely.geometry.base import BaseGeometry

Affine6 = Tuple[float, float, float, float, float, float]


class GeoreferenceError(Exception):
    """Raised when a transform cannot be computed from the supplied points."""


@dataclass(frozen=True)
class GroundControlPoint:
    """A user-supplied tie point between image pixels and a real-world location.

    px, py: image pixel coordinates in the project's working-image system (y down).
    lat, lon: geographic position in decimal degrees (EPSG:4326), entered by the user.
    """

    px: float
    py: float
    lat: float
    lon: float
    label: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "GroundControlPoint":
        return cls(float(d["px"]), float(d["py"]), float(d["lat"]), float(d["lon"]), str(d.get("label", "")))


def fit_affine(points: Sequence[Tuple[float, float, float, float]]) -> Tuple[Affine6, float]:
    """Least-squares affine fit from (px, py, X, Y) tuples.

    Returns ((a, b, c, d, e, f), rmse) with X = a*px + b*py + c and Y = d*px + e*py + f.
    Needs at least three non-collinear points. For tie points given as lon/lat in degrees,
    pass X=lon, Y=lat; for projected coordinates pass easting/northing.
    """
    if len(points) < 3:
        raise GeoreferenceError("At least 3 control points are required for an affine transform.")
    arr = np.asarray(points, dtype=float)
    A = np.column_stack([arr[:, 0], arr[:, 1], np.ones(len(arr))])
    if np.linalg.matrix_rank(A) < 3:
        raise GeoreferenceError("Control points are collinear; cannot fit a transform.")
    cx, *_ = np.linalg.lstsq(A, arr[:, 2], rcond=None)
    cy, *_ = np.linalg.lstsq(A, arr[:, 3], rcond=None)
    pred = np.column_stack([A @ cx, A @ cy])
    rmse = float(np.sqrt(np.mean(np.sum((pred - arr[:, 2:4]) ** 2, axis=1))))
    return (float(cx[0]), float(cx[1]), float(cx[2]), float(cy[0]), float(cy[1]), float(cy[2])), rmse


def fit_affine_from_gcps(gcps: Iterable[GroundControlPoint]) -> Tuple[Affine6, float]:
    """Fit pixel -> (lon, lat) from GroundControlPoint objects."""
    return fit_affine([(g.px, g.py, g.lon, g.lat) for g in gcps])


def apply_affine(geom: BaseGeometry, affine: Affine6) -> BaseGeometry:
    """Apply (a, b, c, d, e, f) to a Shapely geometry."""
    a, b, c, d, e, f = affine
    return affinity.affine_transform(geom, [a, b, d, e, c, f])


def invert_affine(affine: Affine6) -> Affine6:
    a, b, c, d, e, f = affine
    det = a * e - b * d
    if abs(det) < 1e-18:
        raise GeoreferenceError("Transform is singular and cannot be inverted.")
    ia, ib = e / det, -b / det
    id_, ie = -d / det, a / det
    return (ia, ib, -(ia * c + ib * f), id_, ie, -(id_ * c + ie * f))
