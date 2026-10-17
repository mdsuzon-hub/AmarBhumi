import csv

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.transform import Affine
from shapely.geometry import box

from core.export_manager import export_geopackage, export_geojson, prepare_export
from core.settings import ExportOptions
from scripts.georeference_polygons import georeference, main
from tests.synthetic import irregular_map

A = (2.0, 0.0, 500000.0, 0.0, -2.0, 2600000.0)      # 2 m per pixel, y down -> northing falls


def expected_bounds(proj):
    xs, ys = [], []
    for p in proj.store.parcels:
        x0, y0, x1, y1 = p.geometry.bounds
        for x, y in ((x0, y0), (x1, y1)):
            xs.append(A[0] * x + A[2]); ys.append(A[4] * y + A[5])
    return min(xs), min(ys), max(xs), max(ys)


def test_gcp_workflow_roundtrip(make_proj, tmp_path):
    img, _ = irregular_map()
    proj = make_proj(img)
    src = tmp_path / "pixels.gpkg"
    export_geopackage(proj, str(src))                              # y-flipped pixel export
    h, w = img.shape[:2]
    with open(tmp_path / "g.csv", "w", newline="") as fh:
        wr = csv.writer(fh); wr.writerow(["px", "py", "x", "y"])
        for px, py in ((0, 0), (w, 0), (0, h), (w, h), (w / 2, h / 3)):
            wr.writerow([px, py, A[0] * px + A[2], A[4] * py + A[5]])
    out = tmp_path / "geo.gpkg"
    assert main([str(src), str(out), "--gcps", str(tmp_path / "g.csv"), "--crs", "EPSG:32646"]) == 0
    gdf = gpd.read_file(out, layer="parcels")
    assert gdf.crs.to_epsg() == 32646 and len(gdf) == len(proj.store)
    assert gdf.total_bounds == pytest.approx(expected_bounds(proj), abs=1e-3)
    assert set(gdf.parcel_id) == {p.parcel_id for p in proj.store.parcels}


def test_raster_workflow_and_size_mismatch(make_proj, tmp_path):
    img, _ = irregular_map()
    proj = make_proj(img)
    src = tmp_path / "pixels.geojson"
    export_geojson(proj, str(src))
    h, w = img.shape[:2]
    def tif(name, width, height):
        p = tmp_path / name
        with rasterio.open(p, "w", driver="GTiff", width=width, height=height, count=1, dtype="uint8",
                           crs="EPSG:32646", transform=Affine(*A)) as d:
            d.write(np.zeros((1, height, width), np.uint8))
        return str(p)
    rep = georeference(str(src), str(tmp_path / "o.gpkg"), raster=tif("ok.tif", w, h))
    assert rep["crs"] == "EPSG:32646"
    gdf = gpd.read_file(tmp_path / "o.gpkg", layer="parcels")
    assert gdf.total_bounds == pytest.approx(expected_bounds(proj), abs=1e-3)
    from core.georeference import GeoreferenceError
    with pytest.raises(GeoreferenceError, match="Raster is"):
        georeference(str(src), str(tmp_path / "o2.gpkg"), raster=tif("bad.tif", w + 5, h))


def test_cli_reports_errors(tmp_path, make_proj, capsys):
    img, _ = irregular_map(rows=1, cols=2)
    src = tmp_path / "p.gpkg"
    export_geopackage(make_proj(img), str(src))
    assert main([str(src), str(tmp_path / "o.gpkg")]) == 2
    assert "either --raster or --gcps" in capsys.readouterr().err
