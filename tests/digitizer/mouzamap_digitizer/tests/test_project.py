import json
import os

import numpy as np
import pytest
from shapely.geometry import box

from core.georeference import GroundControlPoint
from core.project_manager import ProjectError, load_project, save_project
from core.settings import PrepParams
from models.project import Project
from tests.synthetic import irregular_map


def test_save_and_reload_preserves_everything(tmp_path, make_proj):
    img, _ = irregular_map()
    proj = make_proj(img, path=str(tmp_path / "map.png"))
    first = proj.store.parcels[0].parcel_id
    proj.store.set_attrs([first], dag_number="২৭/১", notes="নোট ✓", validation_status="valid")
    proj.store.set_geometry(proj.store.parcels[1].parcel_id, box(0, 0, 50, 40))
    proj.detect.close_ksize = 9
    proj.detect.threshold_method = "otsu"
    proj.prep = PrepParams(rotation_deg=1.5, crop=(5, 6, 300, 200))
    proj.set_geometry_state((800, 600), (300, 200), np.array([[1, 0, -5], [0, 1, -6], [0, 0, 1.0]]))
    proj.export.flip_y = False
    proj.georef_points = [GroundControlPoint(10, 20, 23.7, 90.4, "corner")]
    path = str(tmp_path / "p.mouzamap")
    save_project(proj, path)
    assert not proj.dirty and os.path.exists(path)

    back = load_project(path)
    assert back.source_path == proj.source_path and back.source_page == proj.source_page
    assert back.detect == proj.detect and back.prep == proj.prep and back.export == proj.export
    assert back.working_size == (300, 200) and np.allclose(back.source_to_working, proj.source_to_working)
    assert back.georef_points == proj.georef_points
    assert len(back.store) == len(proj.store)
    for a, b in zip(proj.store.parcels, back.store.parcels):
        assert a.parcel_id == b.parcel_id and a.geometry.equals_exact(b.geometry, 1e-9)
        assert (a.dag_number, a.notes, a.validation_status, a.extraction_method, a.source_page) == \
               (b.dag_number, b.notes, b.validation_status, b.extraction_method, b.source_page)
    assert back.store.next_id == proj.store.next_id
    assert back.store.parcels[0].dag_number == "২৭/১"


def test_project_declares_it_is_not_georeferenced(tmp_path, make_proj):
    img, _ = irregular_map(rows=1, cols=2)
    path = str(tmp_path / "p.mouzamap")
    save_project(make_proj(img), path)
    data = json.load(open(path, encoding="utf-8"))
    assert data["georeferencing"]["status"] == "not georeferenced"
    assert "EPSG" not in json.dumps(data["georeferencing"])


def test_relative_source_path_survives_moving_the_folder(tmp_path):
    a = tmp_path / "a"; a.mkdir()
    (a / "map.png").write_bytes(b"x")
    proj = Project(str(a / "map.png"))
    save_project(proj, str(a / "p.mouzamap"))
    b = tmp_path / "b"
    os.rename(a, b)
    assert load_project(str(b / "p.mouzamap")).source_path == str(b / "map.png")


def test_errors_are_reported_clearly(tmp_path):
    bad = tmp_path / "bad.mouzamap"
    bad.write_text("{not json")
    with pytest.raises(ProjectError, match="Cannot read"):
        load_project(str(bad))
    other = tmp_path / "other.mouzamap"
    other.write_text('{"format": "something-else"}')
    with pytest.raises(ProjectError, match="not a MouzaMap"):
        load_project(str(other))
    future = tmp_path / "f.mouzamap"
    future.write_text('{"format": "mouzamap-project", "version": 99}')
    with pytest.raises(ProjectError, match="newer"):
        load_project(str(future))
    with pytest.raises(ProjectError):
        save_project(Project("x"), str(tmp_path / "nofolder" / "p.mouzamap"))
