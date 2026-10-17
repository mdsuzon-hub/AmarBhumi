#!/usr/bin/env python3
"""
map_view.py
-----------
Reads map.json (bbox + dags), renders an interactive satellite map
using Folium + Leaflet-Extras (Esri World Imagery) with MarkerCluster
for efficient rendering of large point sets.

Output: map_view.html  (open in any browser)

Usage:
    python map_view.py
"""

import folium
from folium.plugins import MarkerCluster
import json
import os
import sys

# ------------------------------------------------------------------
# Config
# ------------------------------------------------------------------
JSON_FILE = "map.json"
OUT_FILE  = "map_view.html"

# ------------------------------------------------------------------
# 1. Load JSON
# ------------------------------------------------------------------
if not os.path.exists(JSON_FILE):
    sys.exit(f"❌ {JSON_FILE} not found in {os.getcwd()}")

with open(JSON_FILE, "r", encoding="utf-8") as f:
    try:
        data = json.load(f)
    except json.JSONDecodeError as e:
        sys.exit(f"❌ Invalid JSON in {JSON_FILE}: {e}")

# ------------------------------------------------------------------
# 2. Validate structure
# ------------------------------------------------------------------
if "bbox" not in data or "dags" not in data:
    sys.exit("❌ JSON must contain 'bbox' and 'dags' keys")

if not isinstance(data["bbox"], list) or len(data["bbox"]) != 4:
    sys.exit("❌ 'bbox' must be [minLng, minLat, maxLng, maxLat]")

min_lng, min_lat, max_lng, max_lat = data["bbox"]
center_lat = (min_lat + max_lat) / 2
center_lng = (min_lng + max_lng) / 2

print(f"✅ Loaded {len(data['dags'])} points from {JSON_FILE}")
print(f"   bbox: {data['bbox']}")

# ------------------------------------------------------------------
# 3. Base map: Esri World Imagery (satellite)
# ------------------------------------------------------------------
m = folium.Map(
    location=[center_lat, center_lng],
    zoom_start=17,
    tiles=(
        "https://server.arcgisonline.com/ArcGIS/rest/services/"
        "World_Imagery/MapServer/tile/{z}/{y}/{x}"
    ),
    attr="Tiles &copy; Esri &mdash; Source: Esri, Maxar, Earthstar Geographics",
    max_zoom=19,
)

# Labels overlay (street / place names over satellite)
folium.TileLayer(
    tiles=(
        "https://server.arcgisonline.com/ArcGIS/rest/services/"
        "Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}"
    ),
    attr="Esri Reference",
    name="Labels",
    overlay=True,
    control=True,
).add_to(m)

# ------------------------------------------------------------------
# 4. Points inside MarkerCluster (efficient for large datasets)
# ------------------------------------------------------------------
cluster = MarkerCluster(name="DAG Points", show=True).add_to(m)

valid = 0
skipped = 0
for pt in data["dags"]:
    if not isinstance(pt, dict) or "lat" not in pt or "lng" not in pt:
        print(f"⚠️  Skipping malformed point: {pt}")
        skipped += 1
        continue

    try:
        lat = float(pt["lat"])
        lng = float(pt["lng"])
    except (TypeError, ValueError):
        print(f"⚠️  Skipping non-numeric point: {pt}")
        skipped += 1
        continue

    folium.CircleMarker(
        location=[lat, lng],
        radius=6,
        color="#ff4444",
        weight=2,
        fill=True,
        fill_color="#ff4444",
        fill_opacity=0.85,
        popup=folium.Popup(
            f"<b>ID:</b> {pt.get('id', '?')}<br>"
            f"<b>DAG:</b> {pt.get('dag', '?')}<br>"
            f"<b>Lat:</b> {lat:.6f}<br>"
            f"<b>Lng:</b> {lng:.6f}",
            max_width=220,
        ),
        tooltip=f"ID {pt.get('id', '?')}",
    ).add_to(cluster)
    valid += 1

print(f"   rendered {valid} points, skipped {skipped}")

# ------------------------------------------------------------------
# 5. BBox rectangle for reference
# ------------------------------------------------------------------
folium.Rectangle(
    bounds=[[min_lat, min_lng], [max_lat, max_lng]],
    color="#00ccff",
    weight=2,
    fill=True,
    fill_opacity=0.05,
    popup="Bounding Box",
).add_to(m)

# Fit view to data extent
m.fit_bounds([[min_lat, min_lng], [max_lat, max_lng]])

# ------------------------------------------------------------------
# 6. Layer control + save
# ------------------------------------------------------------------
folium.LayerControl(collapsed=False).add_to(m)
m.save(OUT_FILE)

print(f"✅ Map written to {OUT_FILE}")
print(f"   Open: file://{os.path.abspath(OUT_FILE)}")
print(f"   Or serve: python -m http.server 8000  →  http://localhost:8000/{OUT_FILE}")