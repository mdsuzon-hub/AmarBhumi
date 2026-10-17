"""Saving and loading project files (JSON) and intermediate results."""
from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Optional

import cv2
import numpy as np

from core.georeference import GroundControlPoint
from core.settings import DetectionParams, ExportOptions, PrepParams, ValidationParams
from core.version import APP_VERSION, PROJECT_FORMAT, PROJECT_FORMAT_VERSION
from models.parcel import Parcel
from models.project import Project

log = logging.getLogger(__name__)

PROJECT_EXTENSION = ".mouzamap"


class ProjectError(Exception):
    """Raised with a user-readable message when a project file cannot be read or written."""


def project_to_dict(project: Project, base_dir: Optional[str] = None) -> dict:
    src = project.source_path
    rel = None
    if base_dir and src:
        try:
            rel = os.path.relpath(src, base_dir)
        except ValueError:      # different drive (Windows)
            rel = None
    return {
        "format": PROJECT_FORMAT,
        "version": PROJECT_FORMAT_VERSION,
        "app_version": APP_VERSION,
        "source": {
            "path": src, "relative_path": rel, "kind": project.source_kind,
            "page": project.source_page, "dpi": project.dpi,
            "source_size": list(project.source_size),
        },
        "image_preparation": {
            **project.prep.to_dict(),
            "working_size": list(project.working_size),
            "source_to_working_matrix": project.source_to_working.tolist(),
            "note": "Polygon coordinates are in the working-image pixel system; "
                    "source_to_working_matrix maps source-image pixels to it.",
        },
        "detection": project.detect.to_dict(),
        "validation": project.validation.to_dict(),
        "export": project.export.to_dict(),
        "georeferencing": {
            "status": "not georeferenced",
            "control_points": [g.to_dict() for g in project.georef_points],
        },
        "parcels": {
            "next_id": project.store.next_id,
            "items": [p.to_dict() for p in project.store.parcels],
        },
    }


def save_project(project: Project, path: str) -> None:
    """Write atomically (temp file + rename) so a crash never corrupts an existing project."""
    path = os.path.abspath(path)
    data = project_to_dict(project, os.path.dirname(path))
    try:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError as exc:
        raise ProjectError(f"Cannot write project file '{path}': {exc}") from exc
    project.file_path = path
    project.dirty = False
    log.info("Project saved to %s (%d parcels)", path, len(project.store))


def load_project(path: str) -> Project:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectError(f"Cannot read project file '{path}': {exc}") from exc
    if data.get("format") != PROJECT_FORMAT:
        raise ProjectError("This file is not a MouzaMap Digitizer project.")
    if int(data.get("version", 0)) > PROJECT_FORMAT_VERSION:
        raise ProjectError("This project was created by a newer version of the application.")
    try:
        src = data["source"]
        source_path = src.get("path", "")
        rel = src.get("relative_path")
        if rel and not os.path.exists(source_path):
            candidate = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(path)), rel))
            if os.path.exists(candidate):
                source_path = candidate
        proj = Project(source_path, src.get("kind", "image"), int(src.get("page", 1)), int(src.get("dpi", 300)))
        prep = data.get("image_preparation", {})
        proj.prep = PrepParams.from_dict(prep)
        proj.set_geometry_state(tuple(src.get("source_size", (0, 0))),
                                tuple(prep.get("working_size", (0, 0))),
                                np.array(prep.get("source_to_working_matrix", np.eye(3).tolist()), float))
        proj.detect = DetectionParams.from_dict(data.get("detection"))
        proj.validation = ValidationParams.from_dict(data.get("validation"))
        proj.export = ExportOptions.from_dict(data.get("export"))
        proj.georef_points = [GroundControlPoint.from_dict(d)
                              for d in data.get("georeferencing", {}).get("control_points", [])]
        parcels = [Parcel.from_dict(d) for d in data.get("parcels", {}).get("items", [])]
        proj.store.source_page = proj.source_page
        proj.store.load(parcels, int(data.get("parcels", {}).get("next_id", 1)))
    except (KeyError, ValueError, TypeError) as exc:
        raise ProjectError(f"Project file is damaged or incomplete: {exc}") from exc
    proj.file_path = os.path.abspath(path)
    proj.dirty = False
    return proj


def save_intermediate_binary(project_path: str, name: str, mask: np.ndarray) -> str:
    """Save an intermediate mask (e.g. detected boundaries) next to the project file."""
    folder = os.path.splitext(project_path)[0] + "_intermediate"
    os.makedirs(folder, exist_ok=True)
    out = os.path.join(folder, f"{name}.png")
    if not cv2.imwrite(out, mask):
        raise ProjectError(f"Cannot write {out}")
    return out
