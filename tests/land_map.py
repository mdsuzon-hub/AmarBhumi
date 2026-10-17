#!/usr/bin/env python3
"""
Land & Plot Map Viewer  (Flask + Leaflet)
-----------------------------------------
Put this file in the same folder as  land.json  and  plot.json, then run:

    pip install flask
    python land_map.py            # opens http://127.0.0.1:5000

Features
  * Multiple free base maps: Satellite, Satellite+Labels, OSM, Topo, Light, Dark
  * Land-use layer (coloured by maj_class) + Plot boundary layer (with plot labels)
  * Click a polygon -> info popup (plot no, class, area in sq.m / decimal / acre ...)
  * Filter classes, search plot no, opacity slider, plot list (click to zoom)
  * Dynamic: editing land.json / plot.json auto-refreshes the map,
    or upload new JSON files from the sidebar.
"""
import argparse
import json
import math
import os
import re
import threading
import webbrowser

from flask import Flask, Response, jsonify, request

BASE = os.path.dirname(os.path.abspath(__file__))
FILES = {"land": os.path.join(BASE, "land.json"), "plot": os.path.join(BASE, "plot.json")}
app = Flask(__name__)


# ----------------------------------------------------------------- WKT -> GeoJSON
def parse_wkt(wkt):
    """Parse POLYGON / MULTIPOLYGON WKT into a GeoJSON geometry dict."""
    m = re.match(r"^\s*(MULTIPOLYGON|POLYGON)\s*(\(.*\))\s*$", wkt or "", re.I | re.S)
    if not m:
        return None
    kind, body = m.group(1).upper(), m.group(2)
    stack = [[]]
    for t in re.findall(r"\(|\)|[^()]+", body):
        if t == "(":
            stack.append([])
        elif t == ")":
            node = stack.pop()
            stack[-1].append(node)
        else:
            stack[-1].extend(
                [float(v) for v in p.split()[:2]] for p in t.split(",") if p.strip()
            )
    coords = stack[0][0]
    return {"type": "MultiPolygon" if kind == "MULTIPOLYGON" else "Polygon", "coordinates": coords}


def ring_area(ring):
    """Approx. planar area (m^2) of a lon/lat ring (fine for small parcels)."""
    lat0 = math.radians(sum(p[1] for p in ring) / len(ring))
    kx, ky = 111320 * math.cos(lat0), 110574
    s = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        s += (x1 * kx) * (y2 * ky) - (x2 * kx) * (y1 * ky)
    return abs(s) / 2


def geom_area(g):
    polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
    total = 0.0
    for poly in polys:
        total += ring_area(poly[0]) - sum(ring_area(h) for h in poly[1:])
    return total


# ----------------------------------------------------------------- loading
_cache = {}


def load(kind):
    path = FILES[kind]
    if not os.path.exists(path):
        return {"type": "FeatureCollection", "features": []}
    mtime = os.path.getmtime(path)
    if kind in _cache and _cache[kind][0] == mtime:
        return _cache[kind][1]
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    items = []
    if isinstance(raw, list):
        items = raw
    else:  # take every list inside the dict (landuse / plots ...)
        for v in raw.values():
            if isinstance(v, list):
                items += v
    feats = []
    for it in items:
        if not isinstance(it, dict) or not it.get("geom"):
            continue
        g = parse_wkt(it["geom"])
        if not g:
            continue
        props = {k: v for k, v in it.items() if k != "geom"}
        props["area_sqm"] = round(geom_area(g), 2)
        feats.append({"type": "Feature", "properties": props, "geometry": g})
    fc = {"type": "FeatureCollection", "features": feats}
    _cache[kind] = (mtime, fc)
    return fc


def version():
    return [os.path.getmtime(p) if os.path.exists(p) else 0 for p in FILES.values()]


# ----------------------------------------------------------------- API
@app.route("/api/data")
def api_data():
    try:
        return jsonify(land=load("land"), plot=load("plot"), version=version())
    except Exception as e:  # bad JSON etc.
        return jsonify(error=str(e)), 500


@app.route("/api/version")
def api_version():
    return jsonify(version=version())


@app.route("/api/upload/<kind>", methods=["POST"])
def api_upload(kind):
    if kind not in FILES:
        return jsonify(error="unknown kind"), 404
    try:
        data = json.loads(request.get_data(as_text=True))
    except Exception as e:
        return jsonify(error=f"Invalid JSON: {e}"), 400
    with open(FILES[kind], "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    _cache.pop(kind, None)
    return jsonify(ok=True)


@app.route("/")
def index():
    return Response(PAGE, mimetype="text/html")


# ----------------------------------------------------------------- Front-end
PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Land &amp; Plot Map</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
:root{--bg:rgba(255,255,255,.92);--fg:#1d2433;--mut:#6b7488;--line:#e3e7ef;--acc:#2563eb;--card:#f4f6fb}
@media (prefers-color-scheme:dark){:root{--bg:rgba(24,28,38,.93);--fg:#e8ecf5;--mut:#98a2b8;--line:#2c3446;--acc:#60a5fa;--card:#222838}}
*{box-sizing:border-box}
html,body{height:100%;margin:0;font-family:Inter,system-ui,Segoe UI,Roboto,sans-serif;color:var(--fg)}
#map{position:absolute;inset:0}
#panel{position:absolute;z-index:1000;top:12px;left:12px;bottom:12px;width:330px;background:var(--bg);
  backdrop-filter:blur(10px);border:1px solid var(--line);border-radius:16px;box-shadow:0 8px 30px rgba(0,0,0,.18);
  display:flex;flex-direction:column;overflow:hidden;transition:transform .25s}
#panel.hide{transform:translateX(-350px)}
#toggle{position:absolute;z-index:1001;top:20px;left:352px;width:36px;height:36px;border-radius:10px;border:1px solid var(--line);
  background:var(--bg);color:var(--fg);cursor:pointer;font-size:18px;transition:left .25s}
#panel.hide + #toggle{left:12px}
header{padding:16px 16px 10px}
h1{font-size:17px;margin:0 0 2px}
header small{color:var(--mut)}
.body{overflow:auto;padding:0 16px 16px;flex:1}
.stats{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:8px 0 14px}
.stat{background:var(--card);border-radius:12px;padding:10px}
.stat b{display:block;font-size:18px}.stat span{font-size:11px;color:var(--mut)}
h3{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin:16px 0 8px}
input[type=search]{width:100%;padding:9px 12px;border-radius:10px;border:1px solid var(--line);background:var(--card);color:var(--fg);outline:none}
input[type=search]:focus{border-color:var(--acc)}
input[type=range]{width:100%}
.row{display:flex;align-items:center;gap:8px;padding:6px 4px;border-radius:8px;font-size:13px;cursor:pointer}
.row:hover{background:var(--card)}
.sw{width:14px;height:14px;border-radius:4px;flex:none;border:1px solid rgba(0,0,0,.2)}
.row .n{margin-left:auto;color:var(--mut);font-size:12px;white-space:nowrap}
.btn{display:inline-block;padding:8px 12px;border-radius:10px;background:var(--acc);color:#fff;font-size:12px;cursor:pointer;border:0;margin:0 6px 6px 0}
.btn.alt{background:var(--card);color:var(--fg);border:1px solid var(--line)}
.plots{max-height:190px;overflow:auto;margin-top:8px}
.chip{display:inline-flex;gap:6px;align-items:center;font-size:12px;color:var(--mut)}
.plot-lbl{background:rgba(0,0,0,.65);color:#fff;border:0;border-radius:6px;padding:1px 6px;font-weight:600;box-shadow:none}
.plot-lbl:before{display:none}
.nolabels .plot-lbl{display:none}
.pop h4{margin:0 0 6px;font-size:15px}
.pop table{border-collapse:collapse;font-size:12.5px}
.pop td{padding:2px 8px 2px 0;vertical-align:top}.pop td:first-child{color:#6b7488}
.pop .tag{display:inline-block;padding:1px 8px;border-radius:99px;color:#fff;font-size:11px;margin:2px 2px 0 0}
#toast{position:absolute;z-index:1200;right:14px;top:14px;background:#111c;color:#fff;padding:8px 14px;border-radius:10px;font-size:13px;display:none}
@media(max-width:640px){#panel{width:calc(100% - 24px);bottom:auto;max-height:60%}#toggle{left:auto;right:12px;top:12px}#panel.hide+#toggle{left:auto}}
</style>
</head>
<body>
<div id="map"></div>
<div id="panel">
  <header><h1>🗺️ Land &amp; Plot Map</h1><small id="sub">Loading…</small></header>
  <div class="body">
    <div class="stats">
      <div class="stat"><b id="s_plots">–</b><span>Plots</span></div>
      <div class="stat"><b id="s_parts">–</b><span>Land-use parts</span></div>
      <div class="stat"><b id="s_area">–</b><span>Total area (decimal)</span></div>
      <div class="stat"><b id="s_cls">–</b><span>Classes</span></div>
    </div>

    <input id="q" type="search" placeholder="Search plot no / label…">
    <div class="plots" id="plotlist"></div>

    <h3>Land-use classes</h3>
    <div id="legend"></div>

    <h3>Land-use opacity</h3>
    <input id="op" type="range" min="0" max="1" step="0.05" value="0.55">
    <label class="row"><input type="checkbox" id="lbl" checked> Show plot labels</label>
    <label class="row"><input type="checkbox" id="auto" checked> Auto-refresh when files change</label>

    <h3>Data</h3>
    <label class="btn">Upload land.json<input type="file" id="up_land" accept=".json" hidden></label>
    <label class="btn">Upload plot.json<input type="file" id="up_plot" accept=".json" hidden></label>
    <button class="btn alt" id="fit">Fit to data</button>
  </div>
</div>
<button id="toggle" title="Toggle panel">☰</button>
<div id="toast"></div>

<script>
const $ = s => document.querySelector(s);
const map = L.map('map', {zoomControl:false, maxZoom:22}).setView([22.334, 90.05], 17);
L.control.zoom({position:'bottomright'}).addTo(map);
L.control.scale({imperial:false}).addTo(map);

/* ---------- base maps (all free, no API key) ---------- */
const esri = 'https://server.arcgisonline.com/ArcGIS/rest/services/';
const sat = () => L.tileLayer(esri+'World_Imagery/MapServer/tile/{z}/{y}/{x}', {maxNativeZoom:19, maxZoom:22, attribution:'Tiles © Esri'});
const base = {
  '🛰️ Satellite': sat(),
  '🛰️ Satellite + Labels': L.layerGroup([sat(),
      L.tileLayer(esri+'Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}', {maxNativeZoom:19, maxZoom:22})]),
  '🗺️ OpenStreetMap': L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {maxNativeZoom:19, maxZoom:22, attribution:'© OpenStreetMap contributors'}),
  '⛰️ Topographic': L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png', {maxNativeZoom:17, maxZoom:22, attribution:'© OpenTopoMap, OSM'}),
  '☀️ Light': L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', {maxNativeZoom:20, maxZoom:22, attribution:'© OSM, © CARTO'}),
  '🌙 Dark': L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {maxNativeZoom:20, maxZoom:22, attribution:'© OSM, © CARTO'}),
};
base['🛰️ Satellite + Labels'].addTo(map);

map.createPane('landPane').style.zIndex = 410;
map.createPane('plotPane').style.zIndex = 420;
const landGroup = L.layerGroup().addTo(map), plotGroup = L.layerGroup().addTo(map);
L.control.layers(base, {'Land use': landGroup, 'Plot boundaries': plotGroup}, {collapsed:true}).addTo(map);

/* ---------- helpers ---------- */
const PALETTE = {'Commercial Area':'#e63946','Settlement':'#f4a261','Agriculture':'#8ab17d','Water Body':'#38bdf8',
  'Road':'#9ca3af','Industrial Area':'#8b5cf6','Forest':'#15803d','Vacant Land':'#d6c28a'};
const colorOf = c => PALETTE[c] || `hsl(${[...(c||'?')].reduce((a,ch)=>a*31+ch.charCodeAt(0)>>>0,7)%360} 65% 50%)`;
const dec = m => m / 40.4686;                       // sq.m -> decimal (শতাংশ)
const fmt = (n, d=2) => Number(n).toLocaleString(undefined, {maximumFractionDigits:d});
const toast = t => { const e=$('#toast'); e.textContent=t; e.style.display='block'; clearTimeout(toast.t); toast.t=setTimeout(()=>e.style.display='none',2500); };
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const areaRows = a => `<tr><td>Area</td><td>${fmt(a)} m² · ${fmt(dec(a))} decimal · ${fmt(a/4046.86,4)} acre</td></tr>`;

let DATA = null, hidden = new Set(), plotLayers = {}, selected = null, ver = null;

/* ---------- render ---------- */
function partsOf(p) {
  return DATA.land.features.filter(f => f.properties.m_code === p.m_code && f.properties.plot_no_en === p.plot_no_en);
}
function plotPopup(p) {
  const parts = partsOf(p);
  const tags = parts.map(f => `<span class="tag" style="background:${colorOf(f.properties.maj_class)}">${esc(f.properties.maj_class)}</span>`).join('');
  const rows = parts.map(f => `<tr><td>${esc(f.properties.sub_class)}</td><td>${fmt(dec(f.properties.area_sqm))} decimal</td></tr>`).join('');
  return `<div class="pop"><h4>Plot ${esc(p.plot_no_en)} <small>(${esc(p.label)})</small></h4>
    <table><tr><td>Mouza code</td><td>${esc(p.m_code)}</td></tr>
    <tr><td>JL / Sheet</td><td>${esc(p.jl_no_en)} / ${esc(p.sht_no_en)}</td></tr>${areaRows(p.area_sqm)}</table>
    ${parts.length ? `<div style="margin-top:6px">${tags}</div><table style="margin-top:4px">${rows}</table>` : ''}</div>`;
}
function landPopup(p) {
  return `<div class="pop"><h4>${esc(p.maj_class)}</h4><table>
    <tr><td>Sub class</td><td>${esc(p.sub_class)}</td></tr>
    <tr><td>Plot no</td><td>${esc(p.plot_no_en)} (${esc(p.label)})</td></tr>
    <tr><td>Mouza code</td><td>${esc(p.m_code)}</td></tr>${areaRows(p.area_sqm)}</table></div>`;
}

function render() {
  landGroup.clearLayers(); plotGroup.clearLayers(); plotLayers = {};
  const op = +$('#op').value;
  L.geoJSON(DATA.land, {
    pane:'landPane',
    filter: f => !hidden.has(f.properties.maj_class),
    style: f => ({color:'#fff', weight:1, fillColor:colorOf(f.properties.maj_class), fillOpacity:op}),
    onEachFeature: (f, l) => {
      l.bindPopup(landPopup(f.properties));
      l.on('mouseover', () => l.setStyle({weight:3}));
      l.on('mouseout',  () => l.setStyle({weight:1}));
    }
  }).addTo(landGroup);

  L.geoJSON(DATA.plot, {
    pane:'plotPane',
    style: () => ({color:'#ffd60a', weight:2, fillColor:'#ffd60a', fillOpacity:0.02}),
    onEachFeature: (f, l) => {
      const p = f.properties;
      plotLayers[p.gid ?? p.plot_no_en] = l;
      l.bindPopup(() => plotPopup(p), {maxWidth:340});
      l.bindTooltip(String(p.plot_no_en ?? p.label), {permanent:true, direction:'center', className:'plot-lbl'});
      l.on('click', () => highlight(l));
    }
  }).addTo(plotGroup);
  updateLabels(); sidebar();
}

function highlight(l) {
  if (selected) selected.setStyle({color:'#ffd60a', weight:2});
  selected = l; l.setStyle({color:'#ff3b30', weight:4}); l.bringToFront();
}

function sidebar() {
  const lf = DATA.land.features, pf = DATA.plot.features;
  const total = (pf.length ? pf : lf).reduce((a,f) => a + f.properties.area_sqm, 0);
  $('#s_plots').textContent = pf.length;
  $('#s_parts').textContent = lf.length;
  $('#s_area').textContent = fmt(dec(total), 1);
  const cls = {};
  lf.forEach(f => { const c = f.properties.maj_class || 'Unknown'; (cls[c] ??= {n:0, a:0}).n++; cls[c].a += f.properties.area_sqm; });
  $('#s_cls').textContent = Object.keys(cls).length;
  $('#sub').textContent = `${DATA.plot.features.length} plots · updated ${new Date().toLocaleTimeString()}`;
  $('#legend').innerHTML = Object.entries(cls).map(([c, v]) =>
    `<label class="row"><input type="checkbox" data-c="${esc(c)}" ${hidden.has(c)?'':'checked'}>
     <span class="sw" style="background:${colorOf(c)}"></span>${esc(c)}
     <span class="n">${v.n} · ${fmt(dec(v.a),1)} dec</span></label>`).join('');
  document.querySelectorAll('#legend input').forEach(i => i.onchange = () => {
    i.checked ? hidden.delete(i.dataset.c) : hidden.add(i.dataset.c); render();
  });
  list();
}

function list() {
  const q = $('#q').value.trim().toLowerCase();
  $('#plotlist').innerHTML = DATA.plot.features
    .filter(f => !q || `${f.properties.plot_no_en} ${f.properties.label}`.toLowerCase().includes(q))
    .map(f => `<div class="row" data-id="${f.properties.gid ?? f.properties.plot_no_en}">
      <span class="sw" style="background:#ffd60a"></span>Plot ${esc(f.properties.plot_no_en)} (${esc(f.properties.label)})
      <span class="n">${fmt(dec(f.properties.area_sqm))} dec</span></div>`).join('') || '<small>No plots</small>';
  document.querySelectorAll('#plotlist .row').forEach(r => r.onclick = () => {
    const l = plotLayers[r.dataset.id]; if (!l) return;
    map.fitBounds(l.getBounds(), {maxZoom:20, padding:[40,40]}); highlight(l); l.openPopup();
  });
}

function updateLabels() {
  map.getContainer().classList.toggle('nolabels', !$('#lbl').checked || map.getZoom() < 17);
}
function fit() {
  const g = L.geoJSON([...DATA.plot.features, ...DATA.land.features]);
  if (g.getLayers().length) map.fitBounds(g.getBounds(), {padding:[50,50]});
}

/* ---------- data loading / live refresh ---------- */
async function load(first) {
  try {
    const r = await fetch('/api/data'); const d = await r.json();
    if (d.error) throw new Error(d.error);
    DATA = d; ver = JSON.stringify(d.version); render();
    if (first) fit(); else toast('Data refreshed ✔');
  } catch (e) { toast('Load error: ' + e.message); }
}
setInterval(async () => {
  if (!$('#auto').checked) return;
  try { const v = JSON.stringify((await (await fetch('/api/version')).json()).version); if (ver && v !== ver) load(false); } catch {}
}, 3000);

['land','plot'].forEach(k => $('#up_'+k).onchange = async e => {
  const f = e.target.files[0]; if (!f) return;
  const r = await fetch('/api/upload/'+k, {method:'POST', body: await f.text()});
  const j = await r.json(); j.error ? toast(j.error) : load(false); e.target.value = '';
});

$('#q').oninput = list;
$('#op').oninput = render;
$('#lbl').onchange = updateLabels;
$('#fit').onclick = fit;
$('#toggle').onclick = () => $('#panel').classList.toggle('hide');
map.on('zoomend', updateLabels);
load(true);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    a = ap.parse_args()
    threading.Timer(1.0, lambda: webbrowser.open(f"http://{a.host}:{a.port}")).start()
    app.run(host=a.host, port=a.port, debug=False)
