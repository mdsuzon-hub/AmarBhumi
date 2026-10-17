"""Enclosed-region extraction: boundary mask -> parcel polygons.

Method
------
1. Label the non-ink pixels with 4-connectivity (so 8-connected pen lines are watertight).
2. Classify regions: tiny regions (text counters, dust) are *absorbed*; regions touching
   the image border or covering too much of the image are *excluded* (outside/background);
   the rest are parcels.
3. Every ink pixel is assigned to its nearest non-absorbed region (distance transform with
   labels). Adjacent parcels therefore meet exactly on the centre-line of the drawn
   boundary instead of being shrunk by half a line width.
4. The label raster is polygonised exactly with rasterio (shared edges are identical),
   and simplified with a coverage-aware simplifier so neighbours keep sharing boundaries.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List

import cv2
import numpy as np
import shapely
from rasterio import features
from rasterio.transform import IDENTITY
from shapely import affinity
from shapely.geometry import Polygon, shape

from core.boundary_detection import DetectionResult
from core.polygon_validation import check_geometry
from core.progress import Progress
from core.settings import DetectionParams, ValidationParams

log = logging.getLogger(__name__)


@dataclass
class ExtractedPolygon:
    geometry: Polygon
    flags: List[str] = field(default_factory=list)   # e.g. ["multipart"]


@dataclass
class ExtractionResult:
    polygons: List[ExtractedPolygon]
    warnings: List[str]


def label_regions(boundaries: np.ndarray, border_mode: str = "exclude_touching"):
    """4-connected labelling of non-ink space. Returns (n, labels, stats)."""
    ink = boundaries
    if border_mode == "close_border":
        ink = boundaries.copy()
        ink[0, :] = ink[-1, :] = 255
        ink[:, 0] = ink[:, -1] = 255
    free = (ink == 0).astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(free, connectivity=4)
    return n, labels, stats


def partition_by_nearest_region(labels: np.ndarray, source_mask_lut: np.ndarray) -> np.ndarray:
    """Assign every pixel to the nearest 'source' region.

    labels:          int32 region labels (0 = ink).
    source_mask_lut: bool array indexed by label; True = region keeps its pixels and also
                     claims neighbouring ink/absorbed pixels.
    Returns an int32 array of region labels covering every pixel (sources only).
    """
    is_source = source_mask_lut[labels]
    src = np.where(is_source, 0, 255).astype(np.uint8)
    if not is_source.any():
        return np.zeros_like(labels)
    _, pix_labels = cv2.distanceTransformWithLabels(
        src, cv2.DIST_L2, cv2.DIST_MASK_5, labelType=cv2.DIST_LABEL_PIXEL)
    # DIST_LABEL_PIXEL numbers the zero pixels 1..N in raster-scan order.
    region_of_pixel = np.concatenate([[0], labels[is_source]]).astype(np.int32)
    out = region_of_pixel[pix_labels]
    return out


def _simplify_independent(polys: List[Polygon], tol: float) -> List[Polygon]:
    return [g.simplify(tol, preserve_topology=True) for g in polys]


def simplify_topological(polys: List[Polygon], tol: float) -> List[Polygon]:
    """Douglas-Peucker simplification that keeps shared boundaries shared.

    The boundaries of all polygons are noded and merged into arcs running between
    junctions; each arc is simplified exactly once (end points, i.e. junctions, are
    preserved) and the polygons are rebuilt from the simplified arcs. Any polygon whose
    rebuilt shape looks wrong falls back to plain per-polygon simplification.
    """
    if tol <= 0 or not polys:
        return polys
    fallback = _simplify_independent(polys, tol)
    try:
        noded = shapely.union_all([g.boundary for g in polys])
        merged = shapely.line_merge(noded)
        arcs = list(getattr(merged, "geoms", [merged]))
        simple = [a.simplify(tol, preserve_topology=False) for a in arcs]
        simple = [a for a in simple if not a.is_empty and a.length > 0]
        faces = list(shapely.polygonize([shapely.union_all(simple)]).geoms)
        if not faces:
            return fallback
        tree = shapely.STRtree(faces)
        out: List[Polygon] = []
        for orig, fb in zip(polys, fallback):
            hits = tree.query(orig.representative_point(), predicate="intersects")
            cand = None
            for i in hits:
                f = faces[int(i)]
                if f.is_valid and abs(f.area - orig.area) <= 0.25 * orig.area + 4.0:
                    cand = f
                    break
            out.append(cand if cand is not None else fb)
        return out
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Topological simplification failed (%s); simplifying polygons independently", exc)
        return fallback


def extract_polygons(det: DetectionResult, params: DetectionParams,
                     progress: Progress | None = None) -> ExtractionResult:
    """Turn a DetectionResult into parcel polygons in working-image pixel coordinates."""
    p = progress or Progress()
    warnings: List[str] = []
    sx, sy = det.scale_x, det.scale_y
    s = (sx + sy) / 2.0
    h, w = det.boundaries.shape[:2]

    p.report(5, "Labelling enclosed regions")
    n, labels, stats = label_regions(det.boundaries, params.border_mode)
    area = stats[:, cv2.CC_STAT_AREA]
    x0, y0 = stats[:, cv2.CC_STAT_LEFT], stats[:, cv2.CC_STAT_TOP]
    x1, y1 = x0 + stats[:, cv2.CC_STAT_WIDTH], y0 + stats[:, cv2.CC_STAT_HEIGHT]

    min_area = max(1.0, params.min_parcel_area * s * s)
    touches = (x0 == 0) | (y0 == 0) | (x1 == w) | (y1 == h)
    huge = area > params.max_area_fraction * h * w
    is_small = area < min_area
    exclude = (huge | (touches if params.border_mode == "exclude_touching" else False)) & ~is_small
    is_parcel = ~is_small & ~exclude
    is_parcel[0] = exclude[0] = False
    is_source = is_parcel | exclude
    is_source[0] = False
    log.info("Regions: %d total, %d parcels, %d excluded, %d absorbed",
             n - 1, int(is_parcel.sum()), int(exclude.sum()), int((is_small[1:]).sum()))
    if not is_parcel.any():
        warnings.append("No enclosed regions found. Try a larger gap-closing kernel, a different "
                        "threshold, or crop the map so its outer frame is closed.")
        return ExtractionResult([], warnings)

    p.report(25, "Assigning boundary pixels to neighbouring regions")
    owner = partition_by_nearest_region(labels, is_source)
    del labels
    owner[~is_parcel[owner]] = 0           # excluded regions leave holes in the label map
    p.report(55, "Tracing polygons")
    ids, polys, flags = [], [], []
    best: dict = {}
    for geom, value in features.shapes(owner, mask=owner > 0, connectivity=4, transform=IDENTITY):
        rid = int(value)
        poly = shape(geom)
        if rid not in best:
            best[rid] = [poly]
        else:
            best[rid].append(poly)
    for rid in sorted(best):
        parts = best[rid]
        main = max(parts, key=lambda g: g.area)
        ids.append(rid)
        polys.append(main)
        flags.append(["multipart"] if len(parts) > 1 else [])
    del owner

    p.report(80, "Simplifying boundaries")
    tol = params.simplify_tolerance * s
    polys = simplify_topological(polys, tol)

    inv_sx, inv_sy = 1.0 / sx, 1.0 / sy
    out: List[ExtractedPolygon] = []
    for g, f in zip(polys, flags):
        if sx != 1.0 or sy != 1.0:
            g = affinity.scale(g, inv_sx, inv_sy, origin=(0, 0))
        if not isinstance(g, Polygon) or g.is_empty:
            continue
        out.append(ExtractedPolygon(g, f))
    # stable reading order: top-to-bottom, left-to-right by centroid
    out.sort(key=lambda e: (round(e.geometry.centroid.y / 50.0), e.geometry.centroid.x))
    p.report(100, f"{len(out)} polygons extracted")
    if any("multipart" in e.flags for e in out):
        warnings.append("Some regions were split in several pieces; only the largest piece was kept.")
    return ExtractionResult(out, warnings)


def candidate_issue_note(poly: Polygon, flags: List[str], vparams: ValidationParams):
    """Run validation on a fresh candidate: returns (status, note)."""
    from core.polygon_validation import Issue, auto_note, auto_status
    issues = check_geometry(poly, vparams)
    if "multipart" in flags:
        issues.append(Issue("multipart", "Region had several pieces; largest kept", "warning"))
    return auto_status(issues), auto_note(issues)
