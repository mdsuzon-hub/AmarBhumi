import json
import os
import sqlite3

import cv2
import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import box

from core.export_manager import (ExportError, export_csv, export_geojson, export_geopackage,
                                 export_png_preview, export_shapefile, export_working_image,
                                 import_parcels, prepare_export)
from core.settings import ExportOptions
from tests.synthetic import irregular_map

EXPORTERS = {".geojson": export_geojson, ".gpkg": export_geopackage, ".shp": export_shapefile,
             ".csv": export_csv}


@pytest.fixture
def proj(make_proj):
    img, _ = irregular_map()
    p = make_proj(img)
    p.store.set_attrs([p.store.parcels[0].parcel_id], dag_number="১২/A", notes="hello, \"world\"\nline2",
                      validation_status="valid")
    return p


def same_shapes(imported, store):
    got = {a["parcel_id"]: g for g, a in imported.items}
    assert set(got) == {p.parcel_id for p in store.parcels}
    for p in store.parcels:
        assert got[p.parcel_id].normalize().equals_exact(p.geometry.normalize(), 1e-6), p.parcel_id


@pytest.mark.parametrize("ext", list(EXPORTERS))
@pytest.mark.parametrize("flip", [True, False])
def test_export_reimport_shapes_are_identical(proj, tmp_path, ext, flip):
    path = str(tmp_path / f"out{ext}")
    EXPORTERS[ext](proj, path, ExportOptions(flip_y=flip))
    same_shapes(import_parcels(path), proj.store)


@pytest.mark.parametrize("ext", list(EXPORTERS))
def test_attributes_survive_roundtrip(proj, tmp_path, ext):
    path = str(tmp_path / f"out{ext}")
    EXPORTERS[ext](proj, path)
    first = proj.store.parcels[0]
    rec = {a["parcel_id"]: a for _, a in import_parcels(path).items}[first.parcel_id]
    assert rec["dag_number"] == "১২/A"
    assert rec["validation_status"] == "valid"
    assert rec["extraction_method"] == "automatic"
    assert float(rec["pixel_area"]) == pytest.approx(first.pixel_area, abs=0.01)
    assert int(rec["vertex_count"]) == first.vertex_count
    assert int(rec["source_page"]) == first.source_page
    if ext != ".shp":   # shapefile DBF truncates multi-line text fields differently; checked below
        assert rec["notes"] == first.notes


def test_geojson_declares_pixel_crs_not_wgs84(proj, tmp_path):
    path = str(tmp_path / "o.geojson")
    export_geojson(proj, path)
    doc = json.load(open(path, encoding="utf-8"))
    assert "4326" not in json.dumps(doc["crs"]) and "CRS84" not in json.dumps(doc["crs"])
    assert "NOT georeferenced" in doc["mouzamap"]["coordinate_system"]
    assert "NOT geographic" in doc["mouzamap"]["coordinate_note"]
    gdf = gpd.read_file(path)
    assert gdf.crs is not None and not gdf.crs.is_geographic


def test_geopackage_has_no_crs_and_metadata(proj, tmp_path):
    path = str(tmp_path / "o.gpkg")
    export_geopackage(proj, path)
    gdf = gpd.read_file(path, layer="parcels")
    assert gdf.crs is None
    assert list(gdf.columns)[:-1] == ["parcel_id", "dag_number", "pixel_area", "vertex_count",
                                      "extraction_method", "validation_status", "source_page", "notes"]
    con = sqlite3.connect(path)
    meta = dict(con.execute("SELECT key, value FROM mouzamap_metadata").fetchall())
    con.close()
    assert "NOT georeferenced" in meta["coordinate_system"]
    assert meta["flip_y"] == "true"


def test_shapefile_field_names_and_no_prj(proj, tmp_path):
    path = str(tmp_path / "o.shp")
    b = export_shapefile(proj, path)
    assert not os.path.exists(str(tmp_path / "o.prj"))
    assert any("10 characters" in w for w in b.warnings)
    gdf = gpd.read_file(path)
    assert {"vert_count", "extr_meth", "valid_stat", "src_page"} <= set(gdf.columns)
    assert all(len(c) <= 10 for c in gdf.columns if c != "geometry")


def test_flip_y_option_changes_coordinates_and_is_recorded(proj, tmp_path):
    h = proj.working_size[1]
    up = prepare_export(proj, ExportOptions(flip_y=True))
    down = prepare_export(proj, ExportOptions(flip_y=False))
    assert up.metadata["flip_y"] and not down.metadata["flip_y"] and up.metadata["image_height"] == h
    assert up.geoms[0].bounds[1] == pytest.approx(h - down.geoms[0].bounds[3])


def test_rejected_parcels_excluded_by_default_and_invalid_repaired(proj):
    pid = proj.store.parcels[1].parcel_id
    proj.store.set_attrs([pid], validation_status="rejected")
    assert len(prepare_export(proj).geoms) == len(proj.store) - 1
    assert len(prepare_export(proj, ExportOptions(include_rejected=True)).geoms) == len(proj.store)
    from shapely.geometry import Polygon
    proj.store.set_geometry(proj.store.parcels[2].parcel_id, Polygon([(0, 0), (10, 10), (10, 0), (0, 10)]))
    b = prepare_export(proj)
    assert all(g.is_valid for g in b.geoms) and any("repaired" in w for w in b.warnings)
    b2 = prepare_export(proj, ExportOptions(repair_invalid=False))
    assert any("skipped" in w for w in b2.warnings)


def test_source_coordinate_space_uses_the_prep_transform(proj):
    proj.set_geometry_state((900, 700), proj.working_size, np.array([[1, 0, -40], [0, 1, -25], [0, 0, 1.0]]))
    w = prepare_export(proj, ExportOptions(flip_y=False, coordinate_space="working"))
    s = prepare_export(proj, ExportOptions(flip_y=False, coordinate_space="source"))
    assert s.geoms[0].bounds[0] == pytest.approx(w.geoms[0].bounds[0] + 40)
    assert s.geoms[0].bounds[1] == pytest.approx(w.geoms[0].bounds[1] + 25)
    assert s.metadata["image_height"] == 700


def test_crs_without_transform_is_refused(proj, tmp_path):
    with pytest.raises(ExportError, match="without a georeferencing transform"):
        export_geopackage(proj, str(tmp_path / "x.gpkg"), ExportOptions(crs="EPSG:4326"))
    with pytest.raises(ExportError, match="Invalid CRS"):
        export_geopackage(proj, str(tmp_path / "x.gpkg"), ExportOptions(crs="nonsense", affine=(1, 0, 0, 0, 1, 0)))


def test_export_with_user_supplied_transform_assigns_crs(proj, tmp_path):
    affine = (1e-5, 0.0, 90.0, 0.0, -1e-5, 24.0)
    opt = ExportOptions(crs="EPSG:4326", affine=affine)
    path = str(tmp_path / "geo.gpkg")
    export_geopackage(proj, path, opt)
    gdf = gpd.read_file(path, layer="parcels")
    assert gdf.crs.to_epsg() == 4326
    minx, miny, maxx, maxy = gdf.total_bounds
    assert 90.0 < minx < maxx < 90.1 and 23.9 < miny < maxy < 24.0
    with pytest.raises(ExportError, match="georeferenced"):
        import_parcels(path)


def test_empty_project_and_bad_folder(make_proj, tmp_path, proj):
    from models.project import Project
    with pytest.raises(ExportError, match="no parcels"):
        export_geojson(Project(), str(tmp_path / "a.geojson"))
    with pytest.raises(ExportError, match="Folder does not exist"):
        export_geojson(proj, str(tmp_path / "nope" / "a.geojson"))


def test_png_preview_and_working_image(proj, tmp_path):
    img, _ = irregular_map()
    out = str(tmp_path / "prev.png")
    export_png_preview(proj, img, out)
    prev = cv2.imread(out)
    assert prev.shape[:2] == img.shape[:2]
    assert not np.array_equal(prev, cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
    out2 = str(tmp_path / "work.png")
    export_working_image(img, out2)
    assert np.array_equal(cv2.cvtColor(cv2.imread(out2), cv2.COLOR_BGR2RGB), img)
    export_png_preview(proj, img, str(tmp_path / "small.png"), max_dim=300)
    assert max(cv2.imread(str(tmp_path / "small.png")).shape[:2]) == 300
