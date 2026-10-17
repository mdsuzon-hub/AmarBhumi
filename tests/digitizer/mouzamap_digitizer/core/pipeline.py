"""High-level pipeline entry points shared by the GUI, tests and scripts."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

import numpy as np
from shapely.geometry import Polygon

from core.boundary_detection import DetectionResult, detect_boundaries
from core.polygon_extraction import candidate_issue_note, extract_polygons
from core.progress import Progress
from core.settings import DetectionParams, ValidationParams

Item = Tuple[Polygon, Dict[str, object]]


@dataclass
class PipelineResult:
    detection: DetectionResult
    items: List[Item]          # (geometry, attributes) ready for ParcelStore
    warnings: List[str]


def run_detection(rgb: np.ndarray, params: DetectionParams, vparams: ValidationParams,
                  progress: Progress | None = None) -> PipelineResult:
    """Detect boundaries, extract enclosed regions and flag suspicious ones."""
    p = progress or Progress()
    det = detect_boundaries(rgb, params, p.sub(0, 45))
    ext = extract_polygons(det, params, p.sub(45, 95))
    p.report(97, "Validating polygons")
    items: List[Item] = []
    for e in ext.polygons:
        status, note = candidate_issue_note(e.geometry, e.flags, vparams)
        items.append((e.geometry, {"extraction_method": "automatic",
                                   "validation_status": status, "notes": note}))
    p.report(100, "Done")
    return PipelineResult(det, items, ext.warnings)
