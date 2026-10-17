"""Export (and re-import) of parcels: GeoJSON, GeoPackage, Shapefile, CSV and PNG preview.

Coordinates are written as *image pixel coordinates* unless the caller supplies a real
georeferencing transform together with a CRS (ExportOptions.affine + .crs). No CRS is ever
assigned otherwise: GeoJSON declares an explicit *local engineering* CRS ("Image pixel coordinates", so GDAL/QGIS
do not fall back to the RFC 7946 default of WGS84), GeoPackage layers have an undefined SRS and
Shapefiles get no .prj. Every export also records its coordinate conventions as metadata.
"""
from __future__ import annotations

import csv
import dataclasses
import datetime as _dt
import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np
from shapely import affinity, wkt as shapely_wkt
from shapely.geometry import MultiPolygon, Polygon, mapping, shape
from shapely.geometry.polygon import orient

from core.georeference import apply_affine
from core.polygon_validation import is_invalid, repair_geometry
from core.settings import ExportOptions
from core.version import APP_NAME, APP_VERSION, COORDINATE_NOTE
from models.parcel import Parcel, ring_coords
from models.project import Project

log = logging.getLogger(__name__)

ATTRIBUTE_ORDER = ["parcel_id", "dag_number", "pixel_area", "vertex_count",
                   "extraction_method", "validation_status", "source_page", "notes"]
# Shapefile field names are limited to 10 characters.
SHP_FIELD_MAP = {"parcel_id": "parcel_id", "dag_number": "dag_number", "pixel_area": "pixel_area",
                 "vertex_count": "vert_count", "extraction_method": "extr_meth",
                 "validation_status": "valid_stat", "source_page": "src_page", "notes": "notes"}
SHP_REVERSE = {v: k for k, v in SHP_FIELD_MAP.items()}
METADATA_LAYER = "mouzamap_metadata"
# GeoJSON readers (GDAL/QGIS) assume WGS84 when no usable CRS is declared - a "crs": null is
# ignored. To keep pixel coordinates from being read as longitude/latitude, un-georeferenced
# GeoJSON declares an explicit local *engineering* CRS instead.
LOCAL_PIXEL_CRS_WKT = ('LOCAL_CS["Image pixel coordinates (not georeferenced)",'
                       'LOCAL_DATUM["Image pixel grid",0],UNIT["pixel",1],AXIS["X",EAST],AXIS["Y",NORTH]]')

STATUS_RGB = {"unreviewed": (40, 110, 255), "valid": (30, 170, 60),
              "uncertain": (255, 160, 0), "rejected": (150, 150, 150)}


class ExportError(Exception):
    """Raised with a user-readable message when an export/import cannot be completed."""


@dataclass
class ExportBundle:
    geoms: List[Polygon]
    attrs: List[Dict[str, object]]
    metadata: dict
    crs: Optional[str]
    warnings: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------- preparation
def _matrix_params(m: np.ndarray) -> List[float]:
    return [m[0, 0], m[0, 1], m[1, 0], m[1, 1], m[0, 2], m[1, 2]]


def prepare_export(project: Project, options: Optional[ExportOptions] = None) -> ExportBundle:
    """Select, repair and transform parcels according to the export options."""
    opt = options or project.export
    warnings: List[str] = []
    if not len(project.store):
        raise ExportError("There are no parcels to export.")

    if opt.crs and not opt.affine:
        raise ExportError("A CRS was given without a georeferencing transform. Polygons are in image "
                          "pixels; refusing to label them with a CRS. Provide a transform or no CRS.")
    crs_text: Optional[str] = None
    if opt.crs:
        try:
            from pyproj import CRS
            crs_text = CRS.from_user_input(opt.crs).to_string()
        except Exception as exc:
            raise ExportError(f"Invalid CRS '{opt.crs}': {exc}") from exc

    if opt.coordinate_space not in ("working", "source"):
        raise ExportError("coordinate_space must be 'working' or 'source'.")
    to_source = opt.coordinate_space == "source"
    size = project.source_size if to_source else project.working_size
    height = float(size[1])
    if opt.flip_y and not opt.affine and height <= 0:
        raise ExportError("Image size is unknown; cannot flip the y axis.")

    geoms: List[Polygon] = []
    attrs: List[Dict[str, object]] = []
    repaired = skipped = rejected = 0
    for parcel in project.store.parcels:
        if parcel.validation_status == "rejected" and not opt.include_rejected:
            rejected += 1
            continue
        g = parcel.geometry
        if is_invalid(g):
            fixed = repair_geometry(g) if opt.repair_invalid else None
            if fixed is None:
                skipped += 1
                continue
            g, repaired = fixed, repaired + 1
        a = parcel.attributes()
        a["pixel_area"] = round(float(g.area), 3)
        if to_source:
            g = affinity.affine_transform(g, _matrix_params(project.working_to_source))
        if opt.affine:
            g = apply_affine(g, opt.affine)
        elif opt.flip_y:
            g = affinity.affine_transform(g, [1, 0, 0, -1, 0, height])
        if not g.is_valid:
            g = repair_geometry(g) or g
        geoms.append(orient(g, 1.0) if isinstance(g, Polygon) else g)
        attrs.append(a)

    if not geoms:
        raise ExportError("There are no parcels to export (all were rejected, invalid or none exist).")
    if rejected:
        warnings.append(f"{rejected} rejected parcel(s) were not exported.")
    if repaired:
        warnings.append(f"{repaired} invalid geometr{'y' if repaired == 1 else 'ies'} repaired before export.")
    if skipped:
        warnings.append(f"{skipped} invalid parcel(s) skipped (repair disabled or impossible).")

    georef = bool(opt.affine)
    metadata = {
        "tool": f"{APP_NAME} {APP_VERSION}",
        "exported_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "coordinate_system": (f"georeferenced via user-supplied affine transform, CRS {crs_text}" if georef
                              else "local image pixel coordinates (NOT georeferenced, no CRS)"),
        "coordinate_note": COORDINATE_NOTE if not georef else
        "Coordinates were transformed with a user-supplied affine transform.",
        "coordinate_space": opt.coordinate_space,
        "units": "map units of CRS" if georef else "pixels",
        "origin": "n/a" if georef else ("bottom-left (y axis flipped: y = image_height - y_pixel)"
                                          if opt.flip_y else "top-left (y axis points down)"),
        "flip_y": bool(opt.flip_y and not georef),
        "image_width": int(size[0]), "image_height": int(size[1]),
        "source_path": project.source_path, "source_page": project.source_page, "dpi": project.dpi,
        "rotation_deg": project.prep.rotation_deg,
        "crop_xywh": list(project.prep.crop) if project.prep.crop else None,
        "source_to_working_matrix": project.source_to_working.tolist(),
        "crs": crs_text,
        "affine": list(opt.affine) if opt.affine else None,
        "parcel_count": len(geoms),
    }
    return ExportBundle(geoms, attrs, metadata, crs_text, warnings)


def _check_path(path: str, ext: str) -> str:
    folder = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(folder):
        raise ExportError(f"Folder does not exist: {folder}")
    base, cur = os.path.splitext(path)
    return path if cur.lower() == ext else path + ext


def _write_sidecar(path: str, bundle: ExportBundle) -> None:
    with open(path + ".mouzamap.json", "w", encoding="utf-8") as fh:
        json.dump(bundle.metadata, fh, indent=1, ensure_ascii=False)


# --------------------------------------------------------------------------- writers
def export_geojson(project: Project, path: str, options: Optional[ExportOptions] = None) -> ExportBundle:
    path = _check_path(path, ".geojson")
    b = prepare_export(project, options)
    crs_member = {"type": "name", "properties": {"name": LOCAL_PIXEL_CRS_WKT}}
    if b.crs:
        try:
            from pyproj import CRS
            epsg = CRS.from_user_input(b.crs).to_epsg()
        except Exception:
            epsg = None
        if epsg:
            crs_member = {"type": "name", "properties": {"name": f"urn:ogc:def:crs:EPSG::{epsg}"}}
        else:
            b.warnings.append("CRS has no EPSG code; GeoJSON written without a crs member.")
    doc = {
        "type": "FeatureCollection",
        "name": "parcels",
        "crs": crs_member,   # local pixel (engineering) CRS unless the user georeferenced the data
        "mouzamap": b.metadata,
        "features": [{"type": "Feature", "properties": a, "geometry": mapping(g)}
                     for g, a in zip(b.geoms, b.attrs)],
    }
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False)
    except OSError as exc:
        raise ExportError(f"Cannot write {path}: {exc}") from exc
    return b


def _geodataframe(bundle: ExportBundle, rename: Optional[Dict[str, str]] = None):
    import geopandas as gpd
    cols = {k: [a[k] for a in bundle.attrs] for k in ATTRIBUTE_ORDER}
    if rename:
        cols = {rename[k]: v for k, v in cols.items()}
    return gpd.GeoDataFrame(cols, geometry=bundle.geoms, crs=bundle.crs)


def export_geopackage(project: Project, path: str, options: Optional[ExportOptions] = None) -> ExportBundle:
    path = _check_path(path, ".gpkg")
    b = prepare_export(project, options)
    try:
        import pandas as pd
        import pyogrio
        if os.path.exists(path):
            os.remove(path)
        _geodataframe(b).to_file(path, driver="GPKG", layer="parcels", engine="pyogrio")
        meta = pd.DataFrame({"key": list(b.metadata.keys()),
                             "value": [json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
                                       for v in b.metadata.values()]})
        pyogrio.write_dataframe(meta, path, layer=METADATA_LAYER, driver="GPKG", append=True)
    except Exception as exc:
        raise ExportError(f"Cannot write GeoPackage {path}: {exc}") from exc
    return b


def export_shapefile(project: Project, path: str, options: Optional[ExportOptions] = None) -> ExportBundle:
    path = _check_path(path, ".shp")
    b = prepare_export(project, options)
    if not b.crs:
        b.warnings.append("Shapefile has no .prj: coordinates are image pixels, not a CRS.")
    long_notes = sum(1 for a in b.attrs if len(str(a["notes"])) > 254)
    if long_notes:
        b.warnings.append(f"{long_notes} note(s) longer than 254 characters were truncated (Shapefile limit).")
    b.warnings.append("Shapefile field names are limited to 10 characters: vertex_count->vert_count, "
                      "extraction_method->extr_meth, validation_status->valid_stat, source_page->src_page.")
    try:
        _geodataframe(b, SHP_FIELD_MAP).to_file(path, driver="ESRI Shapefile", encoding="UTF-8",
                                                engine="pyogrio")
        _write_sidecar(path, b)
    except Exception as exc:
        raise ExportError(f"Cannot write Shapefile {path}: {exc}") from exc
    return b


def export_csv(project: Project, path: str, options: Optional[ExportOptions] = None) -> ExportBundle:
    path = _check_path(path, ".csv")
    b = prepare_export(project, options)
    try:
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(ATTRIBUTE_ORDER + ["hole_count", "exterior_vertices", "hole_vertices", "wkt"])
            for g, a in zip(b.geoms, b.attrs):
                ext = ring_coords(g.exterior)
                holes = [ring_coords(r) for r in g.interiors]
                w.writerow([a[k] for k in ATTRIBUTE_ORDER] + [
                    len(holes), json.dumps(ext), json.dumps(holes), g.wkt])
        _write_sidecar(path, b)
    except OSError as exc:
        raise ExportError(f"Cannot write {path}: {exc}") from exc
    return b


# --------------------------------------------------------------------------- PNG preview
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/noto/NotoSansBengali-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSerifBengali-Regular.ttf",
    "/usr/share/fonts/truetype/lohit-bengali/Lohit-Bengali.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def _unicode_font(size: int):
    try:
        from PIL import ImageFont
    except ImportError:
        return None
    for f in _FONT_CANDIDATES:
        if os.path.exists(f):
            try:
                return ImageFont.truetype(f, size)
            except OSError:
                continue
    return None


def _draw_label(img: np.ndarray, text: str, xy: Tuple[int, int], height_px: int) -> None:
    """Draw text centred at xy. ASCII via OpenCV, other scripts via Pillow when a font exists."""
    x, y = xy
    if text.isascii():
        scale = max(0.3, height_px / 28.0)
        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
        org = (x - tw // 2, y + th // 2)
        cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), 3, cv2.LINE_AA)
        cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), 1, cv2.LINE_AA)
        return
    font = _unicode_font(max(10, height_px))
    if font is None:
        return
    from PIL import Image, ImageDraw
    l, t, r, btm = font.getbbox(text)
    w, h = r - l + 6, btm - t + 6
    patch = Image.new("L", (w, h), 0)
    ImageDraw.Draw(patch).text((3 - l, 3 - t), text, fill=255, font=font)
    mask = np.asarray(patch, dtype=np.float32)[..., None] / 255.0
    x0, y0 = x - w // 2, y - h // 2
    H, W = img.shape[:2]
    if x0 < 0 or y0 < 0 or x0 + w > W or y0 + h > H:
        return
    region = img[y0:y0 + h, x0:x0 + w].astype(np.float32)
    img[y0:y0 + h, x0:x0 + w] = (region * (1 - mask) + np.array((0, 0, 0), np.float32) * mask).astype(np.uint8)


def export_png_preview(project: Project, working_rgb: np.ndarray, path: str,
                       include_rejected: bool = True, labels: bool = True,
                       max_dim: int = 6000) -> None:
    """PNG of the working image with parcel outlines coloured by validation status."""
    path = _check_path(path, ".png")
    if not len(project.store):
        raise ExportError("There are no parcels to draw.")
    h, w = working_rgb.shape[:2]
    scale = min(1.0, max_dim / float(max(h, w)))
    base = (cv2.resize(working_rgb, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
            if scale < 1.0 else working_rgb.copy())
    overlay = base.copy()
    drawn: List[Tuple[Parcel, List[np.ndarray]]] = []
    for p in project.store.parcels:
        if p.geometry.is_empty or (p.validation_status == "rejected" and not include_rejected):
            continue
        rings = [np.round(np.array(r, float) * scale).astype(np.int32)
                 for r in [ring_coords(p.geometry.exterior)] + [ring_coords(i) for i in p.geometry.interiors]]
        cv2.fillPoly(overlay, rings, STATUS_RGB.get(p.validation_status, (40, 110, 255)))
        drawn.append((p, rings))
    cv2.addWeighted(overlay, 0.25, base, 0.75, 0, dst=base)
    thick = max(1, int(round(2 * max(1.0, base.shape[1] / 3000.0))))
    for p, rings in drawn:
        cv2.polylines(base, rings, True, STATUS_RGB.get(p.validation_status, (40, 110, 255)), thick, cv2.LINE_AA)
    if labels:
        for p, _ in drawn:
            pt = p.geometry.representative_point()
            text = p.dag_number or p.parcel_id
            side = max(12.0, float(np.sqrt(p.geometry.area)) * scale * 0.25)
            _draw_label(base, text, (int(pt.x * scale), int(pt.y * scale)), int(min(side, 40)))
    ok = cv2.imwrite(path, cv2.cvtColor(base, cv2.COLOR_RGB2BGR))
    if not ok:
        raise ExportError(f"Cannot write {path}")


def export_working_image(working_rgb: np.ndarray, path: str) -> None:
    """Write the working image - the exact pixel grid the exported polygons refer to."""
    path = _check_path(path, ".png")
    if not cv2.imwrite(path, cv2.cvtColor(np.ascontiguousarray(working_rgb), cv2.COLOR_RGB2BGR)):
        raise ExportError(f"Cannot write {path}")


# --------------------------------------------------------------------------- import
@dataclass
class ImportResult:
    items: List[Tuple[Polygon, Dict[str, object]]]
    metadata: dict


def _read_metadata(path: str) -> dict:
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".geojson":
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh).get("mouzamap", {})
        if ext == ".gpkg":
            con = sqlite3.connect(path)
            try:
                rows = con.execute(f'SELECT key, value FROM "{METADATA_LAYER}"').fetchall()
            finally:
                con.close()
            meta = {}
            for k, v in rows:
                try:
                    meta[k] = json.loads(v)
                except (TypeError, ValueError):
                    meta[k] = v
            return meta
        side = path + ".mouzamap.json"
        if os.path.exists(side):
            with open(side, "r", encoding="utf-8") as fh:
                return json.load(fh)
    except Exception as exc:
        log.warning("Could not read export metadata from %s: %s", path, exc)
    return {}


def _polys(geom) -> List[Polygon]:
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    return []


def import_parcels(path: str, project: Optional[Project] = None) -> ImportResult:
    """Read parcels back from a file written by this tool, undoing the y-flip recorded in
    its metadata (and mapping from source to working space if `project` is given)."""
    if not os.path.isfile(path):
        raise ExportError(f"File not found: {path}")
    ext = os.path.splitext(path)[1].lower()
    meta = _read_metadata(path)
    if meta.get("crs"):
        raise ExportError("This file is georeferenced; it cannot be mapped back to image pixels.")
    rows: List[Tuple[Polygon, Dict[str, object]]] = []
    try:
        if ext == ".csv":
            with open(path, newline="", encoding="utf-8-sig") as fh:
                for r in csv.DictReader(fh):
                    geom = shapely_wkt.loads(r["wkt"])
                    rows.append((geom, {k: r[k] for k in ATTRIBUTE_ORDER if k in r}))
        elif ext == ".geojson":
            with open(path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
            for f in doc.get("features", []):
                rows.append((shape(f["geometry"]), dict(f.get("properties") or {})))
        elif ext in (".gpkg", ".shp"):
            import geopandas as gpd
            gdf = gpd.read_file(path, layer="parcels", engine="pyogrio") if ext == ".gpkg" \
                else gpd.read_file(path, engine="pyogrio")
            for _, row in gdf.iterrows():
                attrs = {(SHP_REVERSE.get(k, k) if ext == ".shp" else k): row[k]
                         for k in gdf.columns if k != "geometry"}
                rows.append((row.geometry, attrs))
        else:
            raise ExportError(f"Unsupported file type: {ext}")
    except ExportError:
        raise
    except Exception as exc:
        raise ExportError(f"Cannot read {path}: {exc}") from exc

    flip = bool(meta.get("flip_y"))
    height = float(meta.get("image_height", 0))
    if flip and height <= 0:
        raise ExportError("File was exported with a flipped y axis but lacks the image height metadata.")
    items: List[Tuple[Polygon, Dict[str, object]]] = []
    for geom, attrs in rows:
        for poly in _polys(geom):
            if flip:
                poly = affinity.affine_transform(poly, [1, 0, 0, -1, 0, height])
            if meta.get("coordinate_space") == "source" and project is not None:
                poly = affinity.affine_transform(poly, _matrix_params(project.source_to_working))
            clean = {k: (v.item() if hasattr(v, "item") else v) for k, v in attrs.items()}
            items.append((poly, clean))
    return ImportResult(items, meta)
