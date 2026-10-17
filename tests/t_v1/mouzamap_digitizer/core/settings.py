"""Configuration dataclasses for the processing pipeline, validation and export.

Every threshold, kernel size, minimum area and simplification tolerance used by the
pipeline lives here so that it can be edited in the GUI and saved in the project file.
Pixel-size parameters are expressed in *working image* pixels; when a very large image is
internally downscaled for detection, the values are scaled automatically.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Optional, Tuple, Type, TypeVar

T = TypeVar("T")


def _from_dict(cls: Type[T], data: Optional[dict]) -> T:
    """Build a dataclass from a dict, ignoring unknown keys (forward compatibility)."""
    if not data:
        return cls()
    names = {f.name for f in dataclasses.fields(cls)}  # type: ignore[arg-type]
    return cls(**{k: v for k, v in data.items() if k in names})  # type: ignore[call-arg]


@dataclass
class PrepParams:
    """Geometric preparation applied to the source image (rotation, then crop).

    rotation_deg: counter-clockwise rotation in degrees (OpenCV convention).
    crop: (x, y, w, h) in the coordinate frame of the *rotated* canvas, or None.
    """

    rotation_deg: float = 0.0
    crop: Optional[Tuple[int, int, int, int]] = None

    def to_dict(self) -> dict:
        return {"rotation_deg": self.rotation_deg, "crop": list(self.crop) if self.crop else None}

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "PrepParams":
        p = _from_dict(cls, data)
        if p.crop is not None:
            p.crop = tuple(int(v) for v in p.crop)  # type: ignore[assignment]
        return p


@dataclass
class DetectionParams:
    """Boundary detection and region extraction settings."""

    # --- photometric adjustment (applied to the detection copy only) ---
    contrast_alpha: float = 1.0       # gain (1.0 = unchanged)
    brightness_beta: int = 0          # offset
    clahe: bool = False               # local contrast equalisation
    clahe_clip: float = 2.0
    blur_ksize: int = 3               # Gaussian pre-blur (odd; 1 disables)

    # --- binarisation ---
    threshold_method: str = "adaptive"  # "adaptive" or "otsu"
    adaptive_block_size: int = 51       # odd, neighbourhood size
    adaptive_c: int = 12                # constant subtracted from local mean

    # --- noise removal ---
    min_noise_area: int = 12            # ink blobs smaller than this (px^2) are deleted

    # --- line detection ---
    line_filter: bool = False           # keep only ink connected to long straight strokes
    line_min_length: int = 60           # px, length of the oriented line probes

    text_max_extent: int = 120          # px; isolated ink clusters smaller than this (digits, symbols) are dropped
                                        # (0 = off). Keep below the size of the smallest real boundary.

    # --- gap closing ---
    close_ksize: int = 5                # elliptical kernel diameter; bridges gaps <= ~k-1 px
    close_iterations: int = 1

    # --- region selection ---
    min_parcel_area: int = 200          # px^2; smaller enclosed regions are absorbed
    max_area_fraction: float = 0.30     # regions larger than this share of the image are skipped
    border_mode: str = "exclude_touching"  # or "close_border"

    # --- geometry ---
    simplify_tolerance: float = 1.5     # px, topology-preserving simplification
    max_detect_pixels: int = 25_000_000  # larger images are downscaled for detection only

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "DetectionParams":
        return _from_dict(cls, data)


@dataclass
class ValidationParams:
    """Thresholds used to flag suspicious polygons."""

    suspicious_area: float = 400.0     # px^2: smaller parcels are flagged as uncertain
    max_aspect_ratio: float = 20.0     # long thin shapes (roads, canals) are flagged
    min_solidity: float = 0.50         # area / convex-hull area
    duplicate_iou: float = 0.90        # overlap ratio above which two polygons are duplicates

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "ValidationParams":
        return _from_dict(cls, data)


@dataclass
class ExportOptions:
    """How parcels are written out.

    coordinate_space: "working" (the cropped/rotated image shown in the app) or
                      "source" (the originally rendered page/image).
    flip_y:           write y = image_height - y_pixel so that GIS software draws the map
                      the right way up. Recorded in the export metadata.
    crs / affine:     OPTIONAL. Only used when a real georeferencing transform is supplied
                      (affine = (a, b, c, d, e, f): x' = a*px + b*py + c, y' = d*px + e*py + f).
                      A CRS without a transform is rejected.
    """

    include_rejected: bool = False
    flip_y: bool = True
    coordinate_space: str = "working"
    repair_invalid: bool = True
    crs: Optional[str] = None
    affine: Optional[Tuple[float, float, float, float, float, float]] = None

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["affine"] = list(self.affine) if self.affine else None
        return d

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "ExportOptions":
        o = _from_dict(cls, data)
        if o.affine is not None:
            o.affine = tuple(float(v) for v in o.affine)  # type: ignore[assignment]
        return o
