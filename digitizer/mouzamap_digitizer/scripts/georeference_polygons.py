#!/usr/bin/env python3
"""Turn exported pixel-coordinate polygons into real georeferenced polygons.

The digitizer never invents coordinates; run this ONLY when you have real reference data.
Two ways to provide the transformation:

 A) --raster georeferenced.tif
    Georeference the PNG written by "File > Export working image" in QGIS (Georeferencer,
    output = GeoTIFF, transformation Linear or Polynomial 1 - both are affine). The affine
    transform and CRS are read from that GeoTIFF and applied to the polygons.

 B) --gcps points.csv --crs EPSG:xxxx
    A CSV with header  px,py,x,y  (image pixel coordinates in the digitizer's working image,
    y pointing DOWN as in the app, and the real-world X/Y in the CRS given by --crs; the
    columns may also be named lon,lat). At least 3 non-collinear points; a least-squares affine
    transform is fitted and its RMSE reported. Prefer a projected CRS (e.g. UTM 46N, EPSG:32646)
    over lon/lat for the reference coordinates when the area is large.

Examples:
  python scripts/georeference_polygons.py parcels.gpkg out.gpkg --raster map_working_georef.tif
  python scripts/georeference_polygons.py parcels.geojson out.gpkg --gcps gcps.csv --crs EPSG:32646
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from typing import List, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import export_manager as em                     # noqa: E402
from core.georeference import GeoreferenceError, fit_affine   # noqa: E402
from core.settings import ExportOptions                    # noqa: E402
from models.parcel import Parcel                           # noqa: E402
from models.project import Project                         # noqa: E402


def read_gcps(path: str) -> List[Tuple[float, float, float, float]]:
    pts = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            row = {k.strip().lower(): v for k, v in row.items()}
            x = row.get("x", row.get("lon"))
            y = row.get("y", row.get("lat"))
            if None in (row.get("px"), row.get("py"), x, y):
                raise GeoreferenceError("GCP CSV needs columns px,py,x,y (or px,py,lon,lat).")
            pts.append((float(row["px"]), float(row["py"]), float(x), float(y)))
    return pts


def georeference(polygons_path: str, out_path: str, raster: str | None = None,
                 gcps: str | None = None, crs: str | None = None) -> dict:
    """Programmatic entry point. Returns a small report dict."""
    imported = em.import_parcels(polygons_path)           # pixel coordinates, y-down, as in the app
    meta = imported.metadata
    if raster:
        import rasterio
        with rasterio.open(raster) as src:
            if src.crs is None:
                raise GeoreferenceError(f"{raster} has no CRS - it is not georeferenced.")
            t = src.transform
            if t.is_identity:
                raise GeoreferenceError(
                    f"{raster} carries no affine geotransform (it was probably warped with a thin-plate/"
                    "polynomial>1 transformation or stores only GCPs). Re-run the QGIS Georeferencer with "
                    "'Linear', 'Helmert' or 'Polynomial 1', or use --gcps.")
            affine = (t.a, t.b, t.c, t.d, t.e, t.f)
            out_crs = src.crs.to_string()
            if meta and (src.width, src.height) != (meta.get("image_width"), meta.get("image_height")):
                raise GeoreferenceError(
                    f"Raster is {src.width}x{src.height} px but the polygons were exported for a "
                    f"{meta.get('image_width')}x{meta.get('image_height')} px image. Use the image "
                    "from 'Export working image' (or export polygons in the matching coordinate space).")
        report = {"method": "raster", "crs": out_crs}
    elif gcps:
        if not crs:
            raise GeoreferenceError("--crs is required with --gcps (the CRS of the X/Y columns).")
        affine, rmse = fit_affine(read_gcps(gcps))
        out_crs = crs
        report = {"method": "gcps", "crs": crs, "rmse": rmse}
    else:
        raise GeoreferenceError("Give either --raster or --gcps.")

    proj = Project(meta.get("source_path", "") if meta else "")
    proj.set_geometry_state((int(meta.get("image_width", 0)), int(meta.get("image_height", 0))),
                            (int(meta.get("image_width", 0)), int(meta.get("image_height", 0))),
                            __import__("numpy").eye(3))
    parcels = []
    for i, (geom, a) in enumerate(imported.items, 1):
        parcels.append(Parcel(str(a.get("parcel_id", f"P{i:04d}")), geom, str(a.get("dag_number") or ""),
                              str(a.get("extraction_method", "manually edited")),
                              str(a.get("validation_status", "unreviewed")),
                              int(a.get("source_page", 1) or 1), str(a.get("notes") or "")))
    proj.store.load(parcels, len(parcels) + 1)
    opt = ExportOptions(include_rejected=True, flip_y=False, crs=out_crs, affine=affine)
    ext = os.path.splitext(out_path)[1].lower()
    fn = {".gpkg": em.export_geopackage, ".geojson": em.export_geojson, ".shp": em.export_shapefile,
          ".csv": em.export_csv}.get(ext)
    if fn is None:
        raise GeoreferenceError("Output must end with .gpkg, .geojson or .shp")
    fn(proj, out_path, opt)
    report.update(affine=affine, count=len(parcels), output=out_path)
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("polygons", help="parcels exported by the digitizer (.gpkg/.geojson/.shp/.csv)")
    ap.add_argument("output", help="georeferenced output (.gpkg/.geojson/.shp)")
    ap.add_argument("--raster", help="GeoTIFF made by georeferencing the exported working image")
    ap.add_argument("--gcps", help="CSV with px,py,x,y control points")
    ap.add_argument("--crs", help="CRS of the X/Y columns of the GCP CSV, e.g. EPSG:32646")
    args = ap.parse_args(argv)
    try:
        rep = georeference(args.polygons, args.output, args.raster, args.gcps, args.crs)
    except (GeoreferenceError, em.ExportError) as exc:
        print("error:", exc, file=sys.stderr)
        return 2
    print(f"Wrote {rep['count']} polygons to {rep['output']} in {rep['crs']} ({rep['method']})")
    if "rmse" in rep:
        print(f"Fit RMSE: {rep['rmse']:.6g} (CRS units) - check this against your point accuracy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
