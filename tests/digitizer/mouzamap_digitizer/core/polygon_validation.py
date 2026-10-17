"""Polygon validation: invalid geometry, tiny regions, odd shapes and duplicates."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence

import numpy as np
import shapely
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.validation import explain_validity

from core.settings import ValidationParams
from models.parcel import Parcel


@dataclass(frozen=True)
class Issue:
    code: str       # invalid_geometry | empty | too_small | elongated | low_solidity | duplicate | multipart
    message: str
    severity: str   # "error" or "warning"


def is_invalid(geom: Optional[BaseGeometry]) -> bool:
    return geom is None or geom.is_empty or not geom.is_valid


def repair_geometry(geom: BaseGeometry) -> Optional[Polygon]:
    """Return a valid Polygon (largest piece) or None if nothing usable remains."""
    if geom is None or geom.is_empty:
        return None
    fixed = geom if geom.is_valid else shapely.make_valid(geom)
    polys: List[Polygon] = []
    stack = [fixed]
    while stack:
        g = stack.pop()
        if isinstance(g, Polygon):
            polys.append(g)
        elif hasattr(g, "geoms"):
            stack.extend(g.geoms)
    polys = [p for p in polys if p.area > 0]
    if not polys:
        return None
    return max(polys, key=lambda p: p.area)


def _aspect_ratio(poly: Polygon) -> float:
    rect = poly.minimum_rotated_rectangle
    if rect.is_empty or not isinstance(rect, Polygon):
        return float("inf")
    xs, ys = rect.exterior.coords.xy
    a = float(np.hypot(xs[1] - xs[0], ys[1] - ys[0]))
    b = float(np.hypot(xs[2] - xs[1], ys[2] - ys[1]))
    lo, hi = min(a, b), max(a, b)
    return hi / lo if lo > 1e-9 else float("inf")


def check_geometry(geom: BaseGeometry, params: ValidationParams) -> List[Issue]:
    """Per-polygon checks (does not include duplicate detection)."""
    issues: List[Issue] = []
    if geom is None or geom.is_empty:
        return [Issue("empty", "Empty geometry", "error")]
    if not geom.is_valid:
        issues.append(Issue("invalid_geometry", f"Invalid geometry: {explain_validity(geom)}", "error"))
        return issues  # shape metrics are unreliable on invalid rings
    if isinstance(geom, MultiPolygon):
        issues.append(Issue("multipart", "Geometry has several parts", "warning"))
        return issues
    if geom.area < params.suspicious_area:
        issues.append(Issue("too_small", f"Very small region ({geom.area:.0f} px²)", "warning"))
    if _aspect_ratio(geom) > params.max_aspect_ratio:
        issues.append(Issue("elongated", "Long thin shape (road, canal or sliver?)", "warning"))
    hull = geom.convex_hull.area
    if hull > 0 and geom.area / hull < params.min_solidity:
        issues.append(Issue("low_solidity", "Highly concave shape", "warning"))
    return issues


def find_duplicates(parcels: Sequence[Parcel], iou_threshold: float) -> Dict[str, List[str]]:
    """Map parcel_id -> ids of other parcels overlapping it with IoU >= threshold."""
    result: Dict[str, List[str]] = {}
    geoms = [p.geometry for p in parcels]
    valid_idx = [i for i, g in enumerate(geoms) if not is_invalid(g)]
    if len(valid_idx) < 2:
        return result
    tree = shapely.STRtree([geoms[i] for i in valid_idx])
    left, right = tree.query([geoms[i] for i in valid_idx], predicate="intersects")
    for a, b in zip(left, right):
        if a >= b:
            continue
        ga, gb = geoms[valid_idx[a]], geoms[valid_idx[b]]
        inter = ga.intersection(gb).area
        union = ga.area + gb.area - inter
        if union > 0 and inter / union >= iou_threshold:
            ia, ib = parcels[valid_idx[a]].parcel_id, parcels[valid_idx[b]].parcel_id
            result.setdefault(ia, []).append(ib)
            result.setdefault(ib, []).append(ia)
    return result


def auto_status(issues: Iterable[Issue]) -> str:
    """Status assigned to freshly extracted parcels: suspicious ones start as 'uncertain'."""
    return "uncertain" if any(True for _ in issues) else "unreviewed"


def auto_note(issues: Iterable[Issue]) -> str:
    msgs = [i.message for i in issues]
    return ("auto: " + "; ".join(msgs)) if msgs else ""
