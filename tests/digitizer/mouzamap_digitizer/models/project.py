"""Project model: everything needed to resume work later."""
from __future__ import annotations

from typing import List, Optional, Tuple

import numpy as np

from core.georeference import GroundControlPoint
from core.polygon_editing import ParcelStore
from core.settings import DetectionParams, ExportOptions, PrepParams, ValidationParams


class Project:
    """In-memory project state (the GUI edits this; project_manager serialises it)."""

    def __init__(self, source_path: str = "", source_kind: str = "image", source_page: int = 1,
                 dpi: int = 300) -> None:
        self.source_path = source_path
        self.source_kind = source_kind           # "pdf" or "image"
        self.source_page = source_page           # 1-based
        self.dpi = dpi
        self.prep = PrepParams()
        self.detect = DetectionParams()
        self.validation = ValidationParams()
        self.export = ExportOptions()
        self.store = ParcelStore(source_page=source_page)
        self.georef_points: List[GroundControlPoint] = []   # reserved for georeferencing
        self.source_size: Tuple[int, int] = (0, 0)           # (w, h) of the rendered source
        self.working_size: Tuple[int, int] = (0, 0)          # (w, h) after rotate/crop
        self.source_to_working: np.ndarray = np.eye(3)
        self.file_path: Optional[str] = None
        self.dirty = False

    # ---- coordinate-space helpers ----
    @property
    def working_to_source(self) -> np.ndarray:
        return np.linalg.inv(self.source_to_working)

    def set_geometry_state(self, source_size: Tuple[int, int], working_size: Tuple[int, int],
                           matrix: np.ndarray) -> None:
        self.source_size = (int(source_size[0]), int(source_size[1]))
        self.working_size = (int(working_size[0]), int(working_size[1]))
        self.source_to_working = np.asarray(matrix, float)

    @property
    def title(self) -> str:
        import os
        return os.path.basename(self.file_path) if self.file_path else "Untitled"
