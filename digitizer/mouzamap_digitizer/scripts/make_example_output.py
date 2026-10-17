#!/usr/bin/env python3
"""Run the pipeline on the sample PDF and write example exports to samples/example_output/.

This is also a minimal example of using the core API without the GUI.
"""
from __future__ import annotations

import os
import sys

import numpy as np
from shapely.geometry import Polygon

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import export_manager as em                       # noqa: E402
from core.image_preprocessing import apply_prep              # noqa: E402
from core.pdf_loader import render_pdf_page                  # noqa: E402
from core.pipeline import run_detection                      # noqa: E402
from core.project_manager import save_project                # noqa: E402
from core.settings import ExportOptions, PrepParams          # noqa: E402
from models.project import Project                           # noqa: E402
from scripts.make_sample_map import FRAME, draw_map          # noqa: E402


def main() -> None:
    samples = os.path.join(ROOT, "samples")
    pdf = os.path.join(samples, "sample_mouza_map.pdf")
    out = os.path.join(samples, "example_output")
    os.makedirs(out, exist_ok=True)

    proj = Project(pdf, "pdf", source_page=1, dpi=300)
    src = render_pdf_page(pdf, 1, 300)
    fx, fy, fw, fh = FRAME
    proj.prep = PrepParams(0.0, (fx - 60, fy - 60, fw + 120, fh + 120))     # crop to the frame
    working, matrix = apply_prep(src, proj.prep)
    proj.set_geometry_state((src.shape[1], src.shape[0]), (working.shape[1], working.shape[0]), matrix)
    proj.detect.close_ksize = 7
    res = run_detection(working, proj.detect, proj.validation)
    proj.store.replace_all(res.items)

    # Fill in dag numbers by matching each polygon with the printed plot numbers (ground truth).
    _, truth = draw_map()
    offset = np.array([fx - 60, fy - 60], float)
    marks = {n: Polygon(q - offset).representative_point() for n, q in truth.items()}
    for p in proj.store.parcels:
        for n, pt in marks.items():
            if p.geometry.contains(pt):
                proj.store.set_attrs([p.parcel_id], dag_number=str(n))
    print(f"{len(proj.store)} parcels; warnings: {res.warnings}")

    opt = ExportOptions()
    em.export_geopackage(proj, os.path.join(out, "sample_parcels.gpkg"), opt)
    em.export_geojson(proj, os.path.join(out, "sample_parcels.geojson"), opt)
    em.export_shapefile(proj, os.path.join(out, "sample_parcels.shp"), opt)
    em.export_csv(proj, os.path.join(out, "sample_parcels.csv"), opt)
    em.export_png_preview(proj, working, os.path.join(out, "sample_preview.png"), max_dim=2400)
    em.export_working_image(working, os.path.join(out, "sample_working_image.png"))
    proj.store.clear_history()
    save_project(proj, os.path.join(out, "sample.mouzamap"))
    print("wrote example output to", out)


if __name__ == "__main__":
    main()
