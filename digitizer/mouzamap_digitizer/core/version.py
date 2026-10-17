"""Application metadata."""

APP_NAME = "MouzaMap Digitizer"
APP_VERSION = "0.1.0"
PROJECT_FORMAT = "mouzamap-project"
PROJECT_FORMAT_VERSION = 1

COORDINATE_NOTE = (
    "Coordinates are IMAGE PIXEL coordinates, NOT geographic coordinates. "
    "Origin is the top-left corner of the image; one unit equals one image pixel; "
    "vertices lie on pixel corners (continuous coordinates). "
    "No CRS is assigned. Georeference these polygons before using them as map data."
)
