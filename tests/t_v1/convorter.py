#!/usr/bin/env python3
"""
Map Vectorizer - hand-drawn / scanned cadastral map  ->  plot polygon JSON

Install:   pip install flask opencv-python-headless numpy pymupdf
Run:       python map_vectorizer.py          (opens http://127.0.0.1:5000)

Workflow
 1. Upload JPG / PNG / PDF map.
 2. Drag the 3 yellow corner handles (NW, NE, SW) + the red centre handle so the
    image sits on the satellite basemap (supports rotate / scale / skew).
    Use the opacity slider to see through it.
 3. Pick the line type (dark ink / adaptive / any colour / red / blue). Tick
    "Highlight detected lines" to see exactly what will be treated as boundary.
    Raise "Close gaps" if hand-drawn lines have small breaks.
 4. "Detect plots" -> every closed region becomes a polygon, numbered in reading
    order. Fix leftovers: click a polygon to edit number / class / delete it,
    or use "Click-to-add" to flood-fill a missed plot.
 5. Fill JL / sheet / m_code prefix, press "Export JSON".
"""
import json
import sys
import threading
import webbrowser

import cv2
import numpy as np
from flask import Flask, Response, jsonify, request

app = Flask(__name__)
STATE = {"img": None}
MAX_DIM = 4000
BN = str.maketrans("0123456789", "০১২৩৪৫৬৭৮৯")


# ----------------------------------------------------------------- image utils
def load_upload(f, page=0):
    data = f.read()
    if f.filename.lower().endswith(".pdf"):
        import fitz  # pymupdf
        doc = fitz.open(stream=data, filetype="pdf")
        p = doc[min(page, len(doc) - 1)]
        scale = min(4.0, MAX_DIM / max(p.rect.width, p.rect.height))
        pix = p.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        arr = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)
        img = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR if pix.n == 3 else cv2.COLOR_GRAY2BGR)
    else:
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Cannot read file")
    m = max(img.shape[:2])
    if m > MAX_DIM:
        img = cv2.resize(img, None, fx=MAX_DIM / m, fy=MAX_DIM / m, interpolation=cv2.INTER_AREA)
    return img


def line_mask(img, mode, thr):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, s, v = cv2.split(cv2.cvtColor(img, cv2.COLOR_BGR2HSV))
    if mode == "adaptive":
        m = cv2.adaptiveThreshold(cv2.GaussianBlur(gray, (3, 3), 0), 255,
                                  cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 41, 12)
    elif mode == "color":
        m = ((s > 70) & (v > 40)) | (gray < thr)
    elif mode == "red":
        m = ((h < 10) | (h > 170)) & (s > 70) & (v > 50)
    elif mode == "blue":
        m = (h > 95) & (h < 135) & (s > 70) & (v > 40)
    else:
        m = gray < thr
    return (m > 0).astype(np.uint8) * 255


def find_regions(img, p, seed=None):
    H, W = img.shape[:2]
    lines = line_mask(img, p["mode"], p["thr"])
    gap = int(p["gap"])
    k = None
    if gap > 0:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * gap + 1, 2 * gap + 1))
        lines = cv2.dilate(lines, k)
    n, lab, st, _ = cv2.connectedComponentsWithStats(cv2.bitwise_not(lines), connectivity=4)

    if seed:
        sx, sy = int(seed[0]), int(seed[1])
        if not (0 <= sx < W and 0 <= sy < H) or lab[sy, sx] == 0:
            return []
        ids = [int(lab[sy, sx])]
    else:
        ids = range(1, n)

    out = []
    for i in ids:
        x, y, w, h, a = st[i]
        if a > p["max_frac"] * W * H:
            continue
        if not seed:
            if a < p["min_area"]:
                continue
            if x == 0 or y == 0 or x + w >= W or y + h >= H:   # page background
                continue
        pad = gap + 2
        x0, y0 = max(x - pad, 0), max(y - pad, 0)
        x1, y1 = min(x + w + pad, W), min(y + h + pad, H)
        m = (lab[y0:y1, x0:x1] == i).astype(np.uint8)
        if k is not None:
            m = cv2.dilate(m, k)          # give back the area eaten by line thickening
        cs, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        if not cs:
            continue
        c = max(cs, key=cv2.contourArea)
        ap = cv2.approxPolyDP(c, p["eps"], True)
        if len(ap) < 3:
            continue
        pts = (ap[:, 0, :] + np.array([x0, y0])).tolist()
        cy = float(np.mean([q[1] for q in pts]))
        cx = float(np.mean([q[0] for q in pts]))
        out.append((cy, cx, pts))

    band = H / 25.0   # reading order: top->bottom rows, left->right inside a row
    out.sort(key=lambda t: (round(t[0] / band), t[1]))
    return [t[2] for t in out]


# ---------------------------------------------------------------------- routes
@app.post("/upload")
def upload():
    try:
        STATE["img"] = load_upload(request.files["file"], int(request.form.get("page", 0)))
    except Exception as e:
        return jsonify(error=str(e)), 400
    h, w = STATE["img"].shape[:2]
    return jsonify(w=w, h=h)


@app.get("/image")
def image():
    ok, buf = cv2.imencode(".jpg", STATE["img"], [cv2.IMWRITE_JPEG_QUALITY, 92])
    return Response(buf.tobytes(), mimetype="image/jpeg")


@app.get("/mask")
def mask():
    img = STATE["img"]
    m = line_mask(img, request.args.get("mode", "dark"), int(request.args.get("thr", 140)))
    rgba = np.zeros((*m.shape, 4), np.uint8)
    rgba[m > 0] = (0, 0, 255, 230)  # BGRA red
    ok, buf = cv2.imencode(".png", rgba)
    return Response(buf.tobytes(), mimetype="image/png")


@app.post("/detect")
def detect():
    d = request.json
    p = dict(mode=d["mode"], thr=int(d["thr"]), gap=int(d["gap"]),
             min_area=int(d["min_area"]), max_frac=float(d["max_frac"]), eps=float(d["eps"]))
    return jsonify(polys=find_regions(STATE["img"], p, d.get("seed")))


@app.post("/export")
def export():
    d = request.json
    meta = d["meta"]
    code = f'{meta["prefix"]}_{meta["jl"]}_{meta["sht"]}_{meta["survey"]}'
    plots, land = [], []
    for idx, f in enumerate(d["features"]):
        no = str(f["no"]).strip()
        num = int(no) if no.isdigit() else None
        gid = num if num is not None else 900000 + idx
        ring = [list(pt) for pt in f["ring"]]
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        wkt = "MULTIPOLYGON(((" + ",".join(f"{x} {y}" for x, y in ring) + ")))"
        label = no.translate(BN)
        plots.append({"gid": gid, "m_code": code, "label": label,
                      "plot_no_en": num if num is not None else no,
                      "jl_no_en": meta["jl"], "sht_no_en": meta["sht"], "geom": wkt})
        land.append({"gid": gid, "m_code": code, "label": label,
                     "maj_class": f["maj"], "sub_class": f["sub"],
                     "plot_no_en": num if num is not None else no, "geom": wkt})
    return Response(json.dumps({"plot": plots, "landuse": land}, indent=4, ensure_ascii=True),
                    mimetype="application/json")


@app.get("/")
def index():
    return Response(PAGE, mimetype="text/html")


# ------------------------------------------------------------------------ page
PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>Map Vectorizer</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
*{box-sizing:border-box}body{margin:0;display:flex;height:100vh;font:13px system-ui,sans-serif}
#side{width:320px;padding:10px;overflow:auto;background:#f4f4f5;border-right:1px solid #ccc}
#map{flex:1}h4{margin:12px 0 4px;font-size:12px;text-transform:uppercase;color:#555}
label{display:block;margin:4px 0}input,select,button{font:inherit;padding:4px;max-width:100%}
input[type=text],input[type=number],select{width:100%}input[type=range]{width:100%}
button{cursor:pointer;margin:2px 0;width:100%}button.p{background:#1d4ed8;color:#fff;border:0;padding:7px;border-radius:4px}
.row{display:flex;gap:6px}.row>*{flex:1}.h{width:16px;height:16px;border-radius:50%;background:#ff0;border:2px solid #000}
.h.c{background:#f33}.pl{background:#fff9;border:0;box-shadow:none;font-weight:600;padding:0 2px}
#stat{color:#444;margin-top:6px}
</style></head><body>
<div id="side">
<h4>1 · Map file</h4>
<input type="file" id="file" accept=".jpg,.jpeg,.png,.pdf"><label>PDF page <input type="number" id="page" value="1" min="1"></label>
<div class="row"><input type="text" id="goto" placeholder="lat,lng e.g. 23.2724,89.8311"><button id="go" style="flex:0 0 50px">Go</button></div>
<h4>2 · Align</h4>
<label>Image opacity <input type="range" id="op" min="0" max="1" step="0.02" value="0.6"></label>
<label><input type="checkbox" id="showImg" checked> Show image</label>
<label><input type="checkbox" id="handles" checked> Show alignment handles</label>
<h4>3 · Line detection</h4>
<label>Line type <select id="mode"><option value="dark">Dark ink (threshold)</option><option value="adaptive">Adaptive (uneven scans)</option><option value="color">Any coloured / dark</option><option value="red">Red lines</option><option value="blue">Blue lines</option></select></label>
<label>Darkness threshold <input type="range" id="thr" min="40" max="240" value="140"></label>
<label>Close gaps (px) <input type="range" id="gap" min="0" max="12" value="2"></label>
<label>Min plot area (px²) <input type="number" id="minA" value="400"></label>
<label>Simplify (px) <input type="number" id="eps" value="1.5" step="0.5"></label>
<label><input type="checkbox" id="hl"> Highlight detected lines</label>
<h4>4 · Plots</h4>
<label>Start plot no <input type="number" id="start" value="1"></label>
<button class="p" id="detect">Detect plots</button>
<label><input type="checkbox" id="add"> Click-to-add plot (flood fill)</label>
<button id="renum">Renumber in order</button><button id="clear">Clear all</button>
<div id="stat">No plots</div>
<div id="selbox" style="display:none;background:#fff;padding:8px;border-radius:4px;margin-top:6px">
<b>Selected plot</b><label>Plot no <input type="text" id="sno"></label>
<label>Major class <input type="text" id="smaj"></label><label>Sub class <input type="text" id="ssub"></label>
<div class="row"><button id="apply">Apply</button><button id="del">Delete</button></div></div>
<h4>5 · Export</h4>
<div class="row"><label>m_code prefix<input type="text" id="prefix" value="DHA_GOP_MUK"></label><label>Survey<input type="text" id="survey" value="RS"></label></div>
<div class="row"><label>JL no<input type="text" id="jl" value="025"></label><label>Sheet no<input type="text" id="sht" value="002"></label></div>
<label>Default major class <input type="text" id="dmaj" value="Settlement"></label>
<label>Default sub class <input type="text" id="dsub" value="Rural Settlement with Homestead Vegetation"></label>
<button class="p" id="export">Export JSON</button>
</div><div id="map"></div>
<script>
const $=id=>document.getElementById(id);
const map=L.map('map',{zoomAnimation:false,maxZoom:22}).setView([23.2724,89.8311],18);
const sat=L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',{maxNativeZoom:19,maxZoom:22,attribution:'Esri'}).addTo(map);
const osm=L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxNativeZoom:19,maxZoom:22,attribution:'&copy; OpenStreetMap'});
L.control.layers({Satellite:sat,OSM:osm}).addTo(map);
const pane=map.createPane('imgpane');pane.style.zIndex=250;pane.style.pointerEvents='none';
function mk(){const e=document.createElement('img');e.style.cssText='position:absolute;left:0;top:0;transform-origin:0 0;max-width:none;display:none';pane.appendChild(e);return e}
const imgEl=mk(),maskEl=mk();
let W=0,H=0,C=null,feats=[],sel=-1,mk4={},group=L.featureGroup().addTo(map);

function layoutImg(){
  if(!C)return;
  const p=k=>map.latLngToLayerPoint(C[k]),a=p('nw'),b=p('ne'),c=p('sw');
  const m=`matrix(${(b.x-a.x)/W},${(b.y-a.y)/W},${(c.x-a.x)/H},${(c.y-a.y)/H},${a.x},${a.y})`;
  imgEl.style.transform=maskEl.style.transform=m;
}
map.on('zoomend viewreset',()=>{layoutImg()});
const px2ll=(x,y)=>{const u=x/W,v=y/H;return[C.nw.lng+u*(C.ne.lng-C.nw.lng)+v*(C.sw.lng-C.nw.lng),C.nw.lat+u*(C.ne.lat-C.nw.lat)+v*(C.sw.lat-C.nw.lat)]};
function ll2px(ll){const ax=C.ne.lng-C.nw.lng,ay=C.ne.lat-C.nw.lat,bx=C.sw.lng-C.nw.lng,by=C.sw.lat-C.nw.lat,dx=ll.lng-C.nw.lng,dy=ll.lat-C.nw.lat,det=ax*by-ay*bx;return[(dx*by-dy*bx)/det*W,(ax*dy-ay*dx)/det*H]}
const centerLL=()=>L.latLng((C.ne.lat+C.sw.lat)/2,(C.ne.lng+C.sw.lng)/2);

function makeHandles(){
  Object.values(mk4).forEach(m=>m.remove());mk4={};
  const icon=c=>L.divIcon({className:'',html:`<div class="h ${c}"></div>`,iconSize:[16,16],iconAnchor:[8,8]});
  for(const k of ['nw','ne','sw']){
    const m=L.marker(C[k],{draggable:true,icon:icon(''),zIndexOffset:1000}).addTo(map);
    m.on('drag',e=>{C[k]=e.latlng;mk4.c.setLatLng(centerLL());layoutImg()});
    m.on('dragend',redraw);mk4[k]=m;
  }
  const c=L.marker(centerLL(),{draggable:true,icon:icon('c'),zIndexOffset:1001}).addTo(map);
  c.on('drag',e=>{const o=centerLL(),dl=e.latlng.lat-o.lat,dg=e.latlng.lng-o.lng;
    for(const k of ['nw','ne','sw']){C[k]=L.latLng(C[k].lat+dl,C[k].lng+dg);mk4[k].setLatLng(C[k])}layoutImg()});
  c.on('dragend',redraw);mk4.c=c;toggleHandles();
}
function toggleHandles(){Object.values(mk4).forEach(m=>{m.getElement().style.display=$('handles').checked?'':'none'})}
$('handles').onchange=toggleHandles;

$('file').onchange=async e=>{
  const f=e.target.files[0];if(!f)return;
  const fd=new FormData();fd.append('file',f);fd.append('page',+$('page').value-1);
  const r=await fetch('/upload',{method:'POST',body:fd}),j=await r.json();
  if(j.error)return alert(j.error);
  W=j.w;H=j.h;const t=Date.now();
  imgEl.style.width=maskEl.style.width=W+'px';imgEl.style.height=maskEl.style.height=H+'px';
  imgEl.src='/image?'+t;imgEl.style.display='';updateMask();
  if(!C){const s=map.getSize(),w=s.x*.6,h=w*H/W,cx=s.x/2,cy=s.y/2,cl=(x,y)=>map.containerPointToLatLng([x,y]);
    C={nw:cl(cx-w/2,cy-h/2),ne:cl(cx+w/2,cy-h/2),sw:cl(cx-w/2,cy+h/2)};makeHandles()}
  layoutImg();feats=[];redraw();
};
$('op').oninput=()=>imgEl.style.opacity=$('op').value;imgEl.style.opacity=.6;
$('showImg').onchange=()=>imgEl.style.visibility=$('showImg').checked?'visible':'hidden';
let tm;function updateMask(){clearTimeout(tm);tm=setTimeout(()=>{if(!W)return;
  maskEl.style.display=$('hl').checked?'':'none';
  if($('hl').checked)maskEl.src=`/mask?mode=${$('mode').value}&thr=${$('thr').value}&t=${Date.now()}`},250)}
['hl','mode','thr'].forEach(i=>$(i).oninput=updateMask);
$('go').onclick=()=>{const [a,b]=$('goto').value.split(',').map(Number);if(!isNaN(a)&&!isNaN(b))map.setView([a,b],19)};

async function detect(seed){
  const body={mode:$('mode').value,thr:+$('thr').value,gap:+$('gap').value,min_area:+$('minA').value,max_frac:.5,eps:+$('eps').value};
  if(seed)body.seed=seed;
  const r=await fetch('/detect',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  return (await r.json()).polys;
}
const blank=px=>({px,no:'',maj:$('dmaj').value,sub:$('dsub').value});
function number(){feats.forEach((f,i)=>f.no=String(+$('start').value+i))}
$('detect').onclick=async()=>{
  if(!W)return alert('Upload a map first');
  $('stat').textContent='Detecting…';
  feats=(await detect()).map(blank);number();sel=-1;redraw();
};
$('renum').onclick=()=>{number();redraw()};
$('clear').onclick=()=>{feats=[];sel=-1;redraw()};
map.on('click',async e=>{
  if(!W||!C)return;
  if(!$('add').checked){sel=-1;redraw();return}
  const [x,y]=ll2px(e.latlng),polys=await detect([x,y]);
  if(polys.length){const f=blank(polys[0]);const nums=feats.map(f=>+f.no).filter(n=>!isNaN(n));
    f.no=String((nums.length?Math.max(...nums):+$('start').value-1)+1);feats.push(f);sel=feats.length-1;redraw()}
});
function redraw(){
  group.clearLayers();
  feats.forEach((f,i)=>{
    const ll=f.px.map(p=>{const q=px2ll(p[0],p[1]);return[q[1],q[0]]});
    const pl=L.polygon(ll,{color:i===sel?'#f00':'#00e5ff',weight:i===sel?3:2,fillOpacity:i===sel?.35:.12,bubblingMouseEvents:false});
    pl.bindTooltip(f.no,{permanent:true,direction:'center',className:'pl'});
    pl.on('click',()=>{sel=i;redraw()});group.addLayer(pl);
  });
  $('stat').textContent=feats.length?feats.length+' plots':'No plots';
  const f=feats[sel];$('selbox').style.display=f?'':'none';
  if(f){$('sno').value=f.no;$('smaj').value=f.maj;$('ssub').value=f.sub}
}
$('apply').onclick=()=>{const f=feats[sel];if(!f)return;f.no=$('sno').value;f.maj=$('smaj').value;f.sub=$('ssub').value;redraw()};
$('del').onclick=delSel;function delSel(){if(sel>=0){feats.splice(sel,1);sel=-1;redraw()}}
document.addEventListener('keydown',e=>{if(e.key==='Delete'&&!/INPUT/.test(document.activeElement.tagName))delSel()});

$('export').onclick=async()=>{
  if(!feats.length)return alert('No plots');
  const features=feats.map(f=>({no:f.no,maj:f.maj,sub:f.sub,ring:f.px.map(p=>px2ll(p[0],p[1]))}));
  const meta={prefix:$('prefix').value,survey:$('survey').value,jl:$('jl').value,sht:$('sht').value};
  const r=await fetch('/export',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({features,meta})});
  const a=document.createElement('a');a.href=URL.createObjectURL(await r.blob());a.download=`${meta.prefix}_${meta.jl}_${meta.sht}.json`;a.click();
};
</script></body></html>"""

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
    threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    app.run(port=port, debug=False)