# MouzaMap Digitizer

Desktop tool (Python 3.11+, PySide6) for Debian Linux that turns **scanned Bangladesh mouza / cadastral
map pages** into **one polygon per land parcel**, lets you correct the result by hand, and exports it for QGIS.

* Runs fully offline – no cloud services, no paid APIs.
* Polygons are stored in **image pixel coordinates**. The tool never guesses latitude/longitude;
  you georeference later in QGIS with your own reference points (see [Georeferencing](#georeferencing-later-in-qgis)).
* Automatic extraction is a **starting point, not a guarantee**. Hand-drawn maps with open boundaries,
  handwriting over lines, faded ink or overlapping text will need correction – that is what the
  editing tools, review statuses and "uncertain" flags are for.

---

## 1. Install (Debian 12/13, Ubuntu 22.04+)

```bash
cd mouzamap_digitizer
./install.sh            # apt libraries (sudo), .venv, packages, menu launcher
./run.sh                # or start "MouzaMap Digitizer" from the application menu
```

Options: `--no-apt` (skip system packages), `--no-desktop` (no menu entry), `--dev` (adds pytest),
`--uninstall`. Internet is needed only during installation (~300 MB of Python wheels).
The installer uses a virtual environment (`.venv`) so nothing is installed system-wide except the
standard Qt runtime libraries. Manual installation:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/python main.py
```

Try it with the bundled sample: `./run.sh samples/sample_mouza_map.pdf`.

## 2. Workflow

1. **File ▸ Open map** – choose a PDF (pick the page and render DPI, 300–600 recommended) or a PNG/JPEG/TIFF.
   The source is never modified; it is held read-only in memory.
2. **Crop / rotate…** – drag a rectangle around the map (exclude titles, legends, margins). Use
   *Auto-deskew* or enter an angle. Keep the map's outer frame inside the crop so edge parcels are closed.
3. **Pipeline tab** – adjust settings if needed, then **▶ Detect parcels** (F5). Detection runs in a
   background thread with a progress bar and **Cancel**.
4. **Review.** Left view = original + polygons, right view = processed binary image (views are
   synchronised). Click a polygon to see its ID, pixel area, vertex count and checks. Polygons
   flagged *uncertain* (tiny, very thin, very concave, …) are listed in amber; **Edit ▸ Select all
   uncertain** (Ctrl+U) lets you review or reject them in one go.
5. **Correct** with the tools below, type **dag numbers** in the parcel table, set statuses.
6. **File ▸ Save project** (Ctrl+S). Closing and reopening the project restores everything.
7. **File ▸ Export parcels…** – GeoPackage / GeoJSON / Shapefile / CSV / PNG preview.

### Tools (toolbar, menu and keys)

| Tool | Key | How |
|---|---|---|
| Pan | H | drag (middle mouse button or Space+drag always pans; wheel zooms) |
| Select | S | click; Ctrl-click or drag a box for several |
| Edit vertices | E | select one polygon → drag vertices; **double-click an edge** adds a vertex; **right-click a vertex** deletes it |
| Split | L | select polygon → click points along the cut line → double-click/Enter; Backspace removes last point; Esc cancels |
| Draw polygon | P | click corners → double-click/Enter (self-intersections are repaired automatically) |
| Reprocess area | R | drag a rectangle: automatic parcels inside are re-detected with the current settings; manually edited parcels are kept |

Other edit commands: **Merge** (Ctrl+M, adjacent polygons), **Delete** (Del), **Repair geometry**,
**Undo/Redo** (Ctrl+Z / Ctrl+Y, unlimited-feeling history), status keys **1** valid, **2** uncertain,
**3** rejected, **0** unreviewed. The *Display* tab toggles original/lines/polygons/labels and sets
colours, boundary thickness and fill opacity.

Status colours: blue unreviewed · green valid · amber uncertain · grey dotted rejected ·
**dashed magenta outline = invalid geometry** (e.g. self-intersecting after a vertex drag).

### Tuning the pipeline

| Symptom | Try |
|---|---|
| Neighbouring parcels merge into one (gap in a boundary) | raise **Closing kernel** (7, 9, 11…) or iterations; or fix with the **Split** tool |
| Different parcels merge because lines are very close | lower the closing kernel |
| Faint or uneven scan, boundaries missing | lower **Adaptive C**, raise contrast gain, enable CLAHE |
| Paper texture/specks become lines | raise **Adaptive C**, **Min. ink blob**, use Otsu on clean scans |
| Plot numbers or hatching create false regions | raise **Max. text/symbol size** (default 120 px), enable **Keep only line-like ink**, raise **Min. parcel area** |
| Parcels along the image edge are missing | include the map frame in the crop, or set *Image border* to `close_border` |
| Polygons too jagged / too smooth | **Simplify tolerance** (default 1.5 px; 0 keeps every pixel step) |
| Huge scans are slow | detection auto-downscales above *Max. detection pixels* (polygons are scaled back); or render at lower DPI |

All thresholds, kernel sizes, minimum areas and simplification tolerances are editable and are saved in
the project file.

## 3. Coordinates – read this before using the data

* Every polygon coordinate is an **image pixel coordinate of the *working image*** (the source after
  rotation and crop): origin at the top-left corner of the image, one unit = one pixel, vertices on pixel
  corners (e.g. `(0, 0)` is the top-left corner of the top-left pixel).
* The project file stores the 3×3 matrix `source_to_working_matrix`, so the link to the original rendered
  page is always recoverable; exports can be written relative to either image.
* `pixel_area` is in **pixels²**. No square metres, acres or decimals are computed anywhere – that is only
  meaningful after georeferencing.
* Exports carry no CRS (see below). `EPSG:4326` is never written.

### Polygon attributes

`parcel_id`, `dag_number` (manual), `pixel_area`, `vertex_count`, `extraction_method`
(`automatic` / `manually edited`), `validation_status` (`unreviewed` / `valid` / `uncertain` / `rejected`),
`source_page`, `notes`.

## 4. Exports

| Format | Notes |
|---|---|
| **GeoPackage** `.gpkg` (recommended) | layer `parcels` with *undefined* SRS + table `mouzamap_metadata` documenting the coordinate system, y-flip, image size, crop/rotation |
| **GeoJSON** `.geojson` | declares an explicit local *engineering* CRS "Image pixel coordinates (not georeferenced)" and a top-level `mouzamap` block with the limitations. (GDAL/QGIS assume WGS84 for GeoJSON without a CRS, which would misplace pixel values as degrees – hence the explicit local CRS.) |
| **Shapefile** `.shp` | no `.prj`; field names shortened to 10 characters (`vert_count`, `extr_meth`, `valid_stat`, `src_page`); notes truncated at 254 chars; sidecar `.mouzamap.json`; UTF-8 `.cpg` |
| **CSV** | attributes + `exterior_vertices` / `hole_vertices` (JSON) + `wkt`; sidecar `.mouzamap.json` |
| **PNG preview** | working image with outlines coloured by status and dag-number labels (Bengali digits are drawn when a Unicode font such as Noto Sans Bengali is installed) |
| **Working image** (File ▸ Export working image) | the exact raster the polygon coordinates refer to – you need it for georeferencing |

Options in the export dialog: **flip Y** (default on: GIS software draws y upwards, so the map appears upright
instead of mirrored; recorded in the metadata), coordinate space (working or original image), include
rejected parcels (default off), repair invalid geometries (default on). Re-importing (File ▸ Import polygons…)
undoes the flip automatically.

## 5. Georeferencing later in QGIS

You supply the reference points; the tool never invents coordinates.

**Open the polygons.** Layer ▸ Add Layer ▸ Add Vector Layer ▸ `…_parcels.gpkg`. The layer has no CRS (or a
local "Image pixel coordinates" CRS for GeoJSON) and appears upright with coordinates in pixel units. Do **not**
assign EPSG:4326 – the numbers are pixels.

**Method A – QGIS Georeferencer + script (recommended)**

1. In the digitizer: *Export working image* → `map_working.png`, and *Export parcels* → `parcels.gpkg`
   (keep the default *working image* coordinate space).
2. QGIS ▸ Layer ▸ Georeferencer ▸ open `map_working.png`. Add ≥ 4 ground control points (click a
   recognisable point on the image, enter its real X/Y or pick it on a reference map).
3. Transformation settings: type **Polynomial 1**, **Linear** or **Helmert** (all affine), choose the target CRS
   (a projected CRS such as UTM 46N / EPSG:32646 is recommended for Bangladesh; UTM 45N for the far west),
   output raster `map_working_georef.tif`, **Run**.
4. Transform the polygons with the same transformation:
   ```bash
   .venv/bin/python scripts/georeference_polygons.py parcels.gpkg parcels_georef.gpkg --raster map_working_georef.tif
   ```
   The output has the GeoTIFF's CRS and real coordinates; dag numbers and statuses are preserved.
   Polynomial 2/3 and thin-plate-spline warps are not affine and are rejected by the script.

**Method B – control points only (no raster step)**

Create `gcps.csv` with header `px,py,x,y` – `px,py` are pixel coordinates in the working image (y pointing
*down*, i.e. as shown in the digitizer's status bar), `x,y` the real coordinates in the CRS you name:

```bash
.venv/bin/python scripts/georeference_polygons.py parcels.gpkg parcels_georef.gpkg --gcps gcps.csv --crs EPSG:32646
```

A least-squares affine fit is computed and its RMSE reported. Always inspect the result over a reference
layer: scanned and hand-drawn maps are rarely perfectly affine, so check residuals at your control points.

*Data model for future versions:* the project file already contains a `georeferencing` section
(`control_points` with pixel x/y and lat/lon) and `core/georeference.py` implements the affine fit
(`GroundControlPoint`, `fit_affine`, `apply_affine`); `ExportOptions(crs=…, affine=…)` writes truly georeferenced
exports and **refuses a CRS without a transform**.

## 6. Project file

`*.mouzamap` is human-readable JSON: source path (absolute + relative), page, DPI, crop/rotation and the
source→working matrix, all processing settings, every polygon (exterior + holes) with attributes, the ID counter,
export options and the (empty) georeferencing section. Saves are atomic (temp file + rename). If the source
file has moved you are asked to locate it. *Process ▸ Save intermediate masks* writes the binary and boundary
masks as PNG next to the project (`<project>_intermediate/`).

## 7. How extraction works

1. grayscale → contrast/CLAHE → blur → adaptive or Otsu threshold (ink = 255)
2. remove specks → optional line filter (oriented morphological openings keep ink with long straight strokes)
   → morphological **closing** to bridge small gaps → remove isolated text/symbol clusters
3. 4-connected labelling of the enclosed (non-ink) regions; tiny regions (loops of digits, dust) are absorbed,
   regions touching the border or covering too much of the image are background
4. every ink pixel is handed to its nearest region (distance transform), so **neighbouring parcels meet on the
   centre-line of the drawn boundary** instead of being shrunk by half a line width
5. exact raster polygonisation (Rasterio) and **topology-preserving simplification** (shared boundaries are
   simplified once, so there are no gaps or overlaps between neighbours)
6. validation: invalid geometry, duplicates (IoU), very small, long-thin and very concave regions are flagged
   *uncertain* with an explanatory note

Images above 25 MP are processed on a downscaled copy and the polygons scaled back; the display keeps full
resolution (render at ≤ ~120 MP).

## 8. Tests

```bash
./install.sh --dev          # or: .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest  # 89 tests, ~6 s
```

Covers: clean, faint/noisy, adjacent, gapped, narrow/irregular and open-boundary synthetic maps with known
ground truth (IoU checks, no overlap/gap check), text filtering, multi-page PDFs, PNG/JPEG/TIFF import,
rotate/crop transform accuracy, split/merge/vertex/undo, validation, project save/load, export → re-import round
trips for every format (shapes identical), CRS safety rules, the georeferencing script, and a headless
end-to-end GUI run (`QT_QPA_PLATFORM=offscreen`).

## 9. Project layout

```
main.py                     entry point (logging to ~/.cache/mouzamap-digitizer/app.log)
core/                       no GUI imports - usable from scripts
  pdf_loader.py             PDF/image loading (read-only RGB arrays)
  image_preprocessing.py    rotate, crop, deskew estimate, source->working matrix
  boundary_detection.py     threshold, noise/line/text filters, gap closing
  polygon_extraction.py     enclosed regions -> simplified polygons
  polygon_validation.py     invalid / small / thin / duplicate checks, repair
  polygon_editing.py        split, merge, vertex ops, ParcelStore with undo/redo
  export_manager.py         gpkg / geojson / shp / csv / png, and re-import
  project_manager.py        .mouzamap files, intermediate results
  georeference.py           ground-control-point model and affine maths
  pipeline.py, settings.py, progress.py, version.py
models/                     Parcel (immutable), Project
ui/                         PySide6: main_window, canvas, parcel_table, settings_panel, dialogs, workers
scripts/                    make_sample_map.py, make_example_output.py, georeference_polygons.py
samples/                    sample_mouza_map.pdf/.png and example_output/ (all export formats)
tests/                      pytest suite and synthetic map generator
install.sh run.sh mouzamap-digitizer.desktop.in requirements*.txt resources/
```

## 10. Troubleshooting

* *"Could not load the Qt platform plugin xcb"* – run `./install.sh` (installs `libxcb-cursor0` and friends)
  or `sudo apt install libxcb-cursor0 libxkbcommon-x11-0`. Over SSH use `QT_QPA_PLATFORM=offscreen` (headless only).
* *Out of memory / slow* – lower the render DPI (300 DPI A3 ≈ 17 MP is comfortable); everything is kept in RAM.
* *PDF page cannot be rendered* – the error dialog names the page and cause; try another DPI.
* *Log file* – `~/.cache/mouzamap-digitizer/app.log`.

## 11. Known limits

* Multi-page TIFF: only the first frame is read (split pages first or convert to PDF).
* No OCR; dag numbers are typed by hand (Unicode, including Bengali digits, is supported).
* Display uses one full-resolution pixmap; very large images (> ~120 MP) may exceed what Qt/graphics memory
  can handle – use a lower DPI.
* Not every boundary can be recovered automatically: hand-drawn open boundaries, touching text and fading
  need the correction tools. Always review every polygon before using the data.
