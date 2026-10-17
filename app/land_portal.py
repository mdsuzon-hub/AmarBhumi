#!/usr/bin/env python3
"""
আমার ভূমি / AmarBhumi  —  Bangladesh Land Portal  (Flask + Leaflet)  MVP
===========================================================================
    pip install flask
    python land_portal.py                 # http://127.0.0.1:5000
    python land_portal.py --port 8000 --no-browser

TWO ISOLATED DATA MODES (header switch, X-Data-Mode sent by the front-end)
    dataset : data/<district>_<sheet>_land.json / _plot.json  (+ legacy land.json / plot.json)
              If nothing exists, 3 demo datasets are generated on first start.
    api     : data/api/api_<div>_<dist>_<thana>_<mouza>_{plot,land,meta}.json
              Areas are fetched step by step from the government map service through the
              /api/bd/* proxy, then SAVED under a dynamic id. After that every feature
              (map, plot drawer, search, verify, tracking, fraud, workflow, admin) works on
              them exactly like on dataset mode. If no API area is saved yet, API mode falls
              back to the demo data (and says so).
    Each mode has its own caches, case ids (MUT-… / API-…) and admin views.

    Government service URL can be overridden with BD_MAP_URL.

Roles (MVP, no login):  User = public pages.  Admin = switch in the header -> #/admin
Ownership, cases, fraud alerts and mutation workflow are DUMMY data (deterministic).
"""
import argparse
import hashlib
import hmac
import json
import math
import os
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zlib
from datetime import date, timedelta

from flask import Flask, Response, jsonify, redirect, request, has_request_context, send_file

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
API_DIR = os.path.join(DATA_DIR, "api")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(API_DIR, exist_ok=True)
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 80 * 1024 * 1024
TODAY = date.today()
CUSTOM_FILE = os.path.join(DATA_DIR, "admin_custom.json")
STATE = {"fraud": {}, "wf": {}, "rev": {"dataset": 0, "api": 0},
         "custom": {m: {"alerts": {}, "cases": {}} for m in ("dataset", "api")}}
ADMIN_KEY = os.environ.get("BD_ADMIN_KEY", "")
HARDEN = {"on": True}


def _load_custom():
    """Manually added fraud alerts / mutation cases survive restarts (kept per data mode)."""
    try:
        with open(CUSTOM_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        for m in ("dataset", "api"):
            for k in ("alerts", "cases"):
                STATE["custom"][m][k] = dict((raw.get(m) or {}).get(k) or {})
    except (OSError, ValueError, AttributeError):
        pass


def _touch(mode):
    STATE["rev"][mode] += 1
    tmp = CUSTOM_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(STATE["custom"], f, ensure_ascii=False)
    os.replace(tmp, CUSTOM_FILE)


_load_custom()

BD_MAP_URL = os.environ.get("BD_MAP_URL", "https://map.land.gov.bd")
_bd_session = {"token": None, "cookies": None}
_bd_lock = threading.Lock()

BN_DIGITS = "০১২৩৪৫৬৭৮৯"
BN_DIST = {
    "dhaka": "ঢাকা", "gazipur": "গাজীপুর", "cumilla": "কুমিল্লা", "comilla": "কুমিল্লা", "barishal": "বরিশাল",
    "barisal": "বরিশাল", "jhalokathi": "ঝালকাঠি", "chattogram": "চট্টগ্রাম", "chittagong": "চট্টগ্রাম",
    "khulna": "খুলনা", "rajshahi": "রাজশাহী", "sylhet": "সিলেট", "rangpur": "রংপুর", "mymensingh": "ময়মনসিংহ",
    "narayanganj": "নারায়ণগঞ্জ", "tangail": "টাঙ্গাইল", "bogura": "বগুড়া", "bogra": "বগুড়া", "jashore": "যশোর",
    "cox_s_bazar": "কক্সবাজার", "coxsbazar": "কক্সবাজার", "narsingdi": "নরসিংদী", "manikganj": "মানিকগঞ্জ",
    "munshiganj": "মুন্সিগঞ্জ", "faridpur": "ফরিদপুর", "pabna": "পাবনা", "dinajpur": "দিনাজপুর",
    "noakhali": "নোয়াখালী", "feni": "ফেনী", "default": "ডিফল্ট",
}


def bn_digits(s):
    return "".join(BN_DIGITS[int(c)] if c.isdigit() else c for c in str(s))


def cur_mode():
    """'api' or 'dataset' — taken from the request, defaulting to API mode."""
    if not has_request_context():
        return "api"
    m = request.headers.get("X-Data-Mode") or request.args.get("mode") or ""
    return "dataset" if m == "dataset" else "api"


# ----------------------------------------------------------------- government map service proxy
def _bd_open():
    try:
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
        page = opener.open(BD_MAP_URL + "/", timeout=15).read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"Government map service unreachable: {exc}") from exc
    match = re.search(r'<meta\s+name=["\']csrf-token["\']\s+content=["\']([^"\']+)', page, re.I)
    if not match:
        raise RuntimeError("Government map service did not provide a CSRF token")
    _bd_session.update(token=match.group(1), cookies=opener)


def _bd_request(path, fields):
    """POST to the government map service using a fresh Laravel CSRF session (one automatic retry on 419)."""
    with _bd_lock:
        for attempt in (0, 1):
            if not _bd_session["token"] or not _bd_session["cookies"]:
                _bd_open()
            opener, token = _bd_session["cookies"], _bd_session["token"]
            data = dict(fields)
            data["_token"] = token
            req = urllib.request.Request(
                BD_MAP_URL + path,
                data=urllib.parse.urlencode(data).encode("utf-8"),
                headers={"Accept": "application/json", "X-CSRF-TOKEN": token, "X-Requested-With": "XMLHttpRequest"},
                method="POST",
            )
            try:
                with opener.open(req, timeout=60) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code in (419, 401, 403):
                    _bd_session.update(token=None, cookies=None)
                    if attempt == 0:
                        continue
                raise RuntimeError(f"Government map service returned HTTP {exc.code}") from exc
            except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
                raise RuntimeError(f"Government map service request failed: {exc}") from exc


# ----------------------------------------------------------------- WKT -> GeoJSON
def parse_wkt(wkt):
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
            stack[-1].extend([float(v) for v in p.split()[:2]] for p in t.split(",") if p.strip())
    return {"type": "MultiPolygon" if kind == "MULTIPOLYGON" else "Polygon", "coordinates": stack[0][0]}


def ring_area(ring):
    lat0 = math.radians(sum(p[1] for p in ring) / len(ring))
    kx, ky = 111320 * math.cos(lat0), 110574
    s = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        s += (x1 * kx) * (y2 * ky) - (x2 * kx) * (y1 * ky)
    return abs(s) / 2


def geom_area(g):
    polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
    return sum(ring_area(p[0]) - sum(ring_area(h) for h in p[1:]) for p in polys)


def flat(c):
    if c and isinstance(c[0], (int, float)):
        yield c
        return
    for x in c:
        yield from flat(x)


def centroid_of(g):
    pts = list(flat(g["coordinates"]))
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def _pt_in_ring(x, y, ring):
    inside, j = False, len(ring) - 1
    for i in range(len(ring)):
        xi, yi, xj, yj = ring[i][0], ring[i][1], ring[j][0], ring[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-18) + xi:
            inside = not inside
        j = i
    return inside


def _outer_rings(g):
    if g["type"] == "MultiPolygon":
        return [p[0] for p in g["coordinates"] if p]
    return [g["coordinates"][0]] if g["coordinates"] else []


def assign_parts(plots, lands):
    """Land-use rows without a plot number get the plot that contains their centroid."""
    if not plots or not lands or len(plots) * len(lands) > 6_000_000:
        return
    boxes = []
    for p in plots:
        g = parse_wkt(p.get("geom"))
        if not g:
            continue
        rings = _outer_rings(g)
        pts = [q for r in rings for q in r]
        if pts:
            xs, ys = [q[0] for q in pts], [q[1] for q in pts]
            boxes.append((min(xs), min(ys), max(xs), max(ys), rings, p["plot_no_en"]))
    for l in lands:
        if l.get("plot_no_en"):
            continue
        g = parse_wkt(l.get("geom"))
        if not g:
            continue
        try:
            cx, cy = centroid_of(g)
        except ZeroDivisionError:
            continue
        for x0, y0, x1, y1, rings, no in boxes:
            if x0 <= cx <= x1 and y0 <= cy <= y1 and any(_pt_in_ring(cx, cy, r) for r in rings):
                l["plot_no_en"] = no
                l.setdefault("label", bn_digits(no))
                break


# ----------------------------------------------------------------- dataset discovery / loading (mode aware)
NAME_RE = re.compile(r"^(?P<id>[A-Za-z0-9][A-Za-z0-9_\-]*)_(?P<kind>land|plot)\.json$", re.I)
SPLIT_RE = re.compile(r"^(?P<d>.+?)[_-](?P<s>\d+)$")


def _scan(folders):
    found = {}
    for folder in folders:
        if not os.path.isdir(folder):
            continue
        for fn in sorted(os.listdir(folder)):
            m = NAME_RE.match(fn)
            if m:
                found.setdefault(m["id"].lower(), {}).setdefault(m["kind"].lower(), os.path.join(folder, fn))
            elif folder == BASE and fn.lower() in ("land.json", "plot.json"):
                found.setdefault("default", {}).setdefault(fn[:4].lower(), os.path.join(folder, fn))
    return found


def discover():
    if cur_mode() == "api":
        found = _scan([API_DIR])
        if found:
            return found  # nothing saved yet -> fall back to demo data below
    return _scan([DATA_DIR, BASE])


_meta = {}


def api_meta(ds):
    p = os.path.join(API_DIR, f"{ds}_meta.json")
    try:
        mt = os.path.getmtime(p)
        c = _meta.get(p)
        if c and c[0] == mt:
            return c[1]
        with open(p, encoding="utf-8") as f:
            m = json.load(f)
        _meta[p] = (mt, m)
        return m
    except (OSError, ValueError):
        return None


def label_of(ds):
    m = api_meta(ds)
    if m:
        bn = m.get("district_bn") or m.get("district_en") or ""
        en = m.get("district_en") or m.get("district_bn") or ""
        return "api" + str(m.get("district", "")), str(m.get("mouza_code") or m.get("mouza", "")), bn, en
    mm = SPLIT_RE.match(ds)
    d, s = (mm["d"], mm["s"]) if mm else (ds, "")
    en = d.replace("_", " ").replace("-", " ").title()
    return d.lower(), s, BN_DIST.get(d.lower().replace("-", "_"), en), en


_cache = {}


def load_file(path, ds):
    mtime = os.path.getmtime(path)
    c = _cache.get(path)
    if c and c[0] == mtime:
        return c[1]
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    items = raw if isinstance(raw, list) else [x for v in raw.values() if isinstance(v, list) for x in v]
    feats = []
    for it in items:
        if not isinstance(it, dict) or not it.get("geom"):
            continue
        g = parse_wkt(it["geom"])
        if not g:
            continue
        props = {k: v for k, v in it.items() if k != "geom"}
        props["area_sqm"] = round(geom_area(g), 2)
        props["_ds"] = ds
        feats.append({"type": "Feature", "properties": props, "geometry": g})
    fc = {"type": "FeatureCollection", "features": feats}
    _cache[path] = (mtime, fc)
    return fc


def fc_of(feats):
    return {"type": "FeatureCollection", "features": feats}


def signature():
    parts = [cur_mode()]
    for ds, files in sorted(discover().items()):
        for k, p in sorted(files.items()):
            try:
                st = os.stat(p)
                parts.append(f"{ds}{k}{st.st_mtime_ns}{st.st_size}")
            except OSError:
                pass
        mp = os.path.join(API_DIR, f"{ds}_meta.json")
        if os.path.exists(mp):
            parts.append(f"{ds}meta{os.stat(mp).st_mtime_ns}")
    return hashlib.md5("|".join(parts).encode()).hexdigest()[:12]


_sum = {}


def summaries():
    mode, sig = cur_mode(), signature()
    c = _sum.get(mode)
    if c and c[0] == sig:
        return c[1]
    out = []
    for ds, files in sorted(discover().items()):
        try:
            land = load_file(files["land"], ds)["features"] if "land" in files else []
            plot = load_file(files["plot"], ds)["features"] if "plot" in files else []
        except Exception:
            continue
        base = plot or land
        pts = [p for f in base for p in flat(f["geometry"]["coordinates"])]
        bb = center = None
        if pts:
            lngs, lats = [p[0] for p in pts], [p[1] for p in pts]
            bb = [min(lats), min(lngs), max(lats), max(lngs)]
            center = [(bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2]
        d, s, bn, en = label_of(ds)
        m = api_meta(ds) or {}
        out.append({
            "id": ds, "district": d, "sheet": s, "district_bn": bn, "district_en": en,
            "plots": len(plot), "parts": len(land),
            "area_sqm": round(sum(f["properties"]["area_sqm"] for f in (plot or land)), 1),
            "bbox": bb, "center": center,
            "source": "api" if m else "demo", "fallback": mode == "api" and not m,
            "mouza_bn": m.get("mouza_bn"), "mouza_en": m.get("mouza_en"),
        })
    _sum[mode] = (sig, out)
    return out


# ----------------------------------------------------------------- plot index + dummy records
_idx = {}


def index():
    mode, sig = cur_mode(), signature()
    c = _idx.get(mode)
    if c and c["sig"] == sig:
        return c
    plots, parts = {}, {}
    for ds, files in sorted(discover().items()):
        try:
            if "plot" in files:
                for i, f in enumerate(load_file(files["plot"], ds)["features"]):
                    p = f["properties"]
                    no = str(p.get("plot_no_en", p.get("label", i)))
                    c2 = centroid_of(f["geometry"])
                    plots[(ds, no)] = {"ds": ds, "idx": i, "props": p, "geom": f["geometry"], "lat": c2[1], "lng": c2[0]}
            if "land" in files:
                for f in load_file(files["land"], ds)["features"]:
                    p = f["properties"]
                    parts.setdefault((ds, str(p.get("plot_no_en", ""))), []).append(
                        {"maj": p.get("maj_class"), "sub": p.get("sub_class"), "area_sqm": p["area_sqm"]})
        except Exception:
            continue
    _idx[mode] = {"sig": sig, "plots": plots, "parts": parts}
    return _idx[mode]


NAMES = [("আব্দুর রহিম", "Abdur Rahim"), ("মোহাম্মদ কামাল", "Mohammad Kamal"), ("রহিমা বেগম", "Rahima Begum"),
         ("শামীম আহমেদ", "Shamim Ahmed"), ("নাসরিন আক্তার", "Nasrin Akter"), ("জাহিদ হাসান", "Jahid Hasan"),
         ("ফারজানা ইয়াসমিন", "Farzana Yasmin"), ("সুলতান মিয়া", "Sultan Mia"), ("মিজানুর রহমান", "Mizanur Rahman"),
         ("সালমা খাতুন", "Salma Khatun"), ("তারেক আজিজ", "Tareq Aziz"), ("লুৎফর রহমান", "Lutfor Rahman"),
         ("হাসান মাহমুদ", "Hasan Mahmud"), ("রোকসানা পারভীন", "Roksana Parvin"), ("আনোয়ার হোসেন", "Anwar Hossain"),
         ("কবির উদ্দিন", "Kabir Uddin")]
SCEN = ["clean", "clean", "pending", "clean", "clean", "khas", "clean", "double_sale", "clean", "clean",
        "acq", "clean", "fake_deed", "clean", "clean", "rapid", "clean", "mismatch", "clean", "clean"]
FRAUD_TYPES = ("fake_deed", "double_sale", "rapid", "mismatch")
FORUM = {"civil_suit": "civil_court", "injunction": "civil_court", "mortgage": "bank", "mutation_appeal": "ac_land"}
STAGES = ["registration", "auto_check", "union_office", "ac_land", "approved", "khatian"]
ADVICE = {
    "fake_deed": ("দলিলটি জাল হওয়ার সন্দেহ আছে — লেনদেন করবেন না এবং সাব-রেজিস্ট্রি অফিসে মূল দলিল যাচাই করুন।",
                  "The deed is suspected to be forged — do not transact; verify the original deed at the sub-registry office."),
    "double_sale": ("একই জমি একাধিকবার বিক্রির প্রমাণ আছে — আগে মালিকানা বিরোধ নিষ্পত্তি করুন।",
                    "Evidence of the same land being sold more than once — resolve the ownership dispute first."),
    "rapid": ("অল্প সময়ে বারবার হস্তান্তর হয়েছে — ক্রয়ের আগে প্রতিটি হস্তান্তরের বৈধতা যাচাই করুন।",
              "Repeated transfers in a short time — verify the legitimacy of every transfer before buying."),
    "mismatch": ("দলিল ও নামজারি রেকর্ডে অমিল আছে — সংশোধনের পর লেনদেন করুন।",
                 "Deed and mutation records do not match — transact only after correction."),
    "pending": ("চলমান মামলা/বন্ধক আছে — নিষ্পত্তি না হওয়া পর্যন্ত লেনদেন স্থগিত রাখুন।",
                "Pending case/mortgage exists — hold the transaction until it is settled."),
    "khas": ("এটি সরকারি খাস/অর্পিত সম্পত্তি — ক্রয়-বিক্রয় আইনসম্মত নয়।",
             "This is government khas/vested property — buying or selling is not lawful."),
    "acq": ("জমিটি অধিগ্রহণের আওতায় — ক্ষতিপূরণ ও প্রকল্পের অবস্থা ডিসি অফিসে যাচাই করুন।",
            "The land is under acquisition — check compensation and project status at the DC office."),
    "generic": ("লেনদেনের আগে ইউনিয়ন ভূমি অফিস থেকে হালনাগাদ খতিয়ান ও পর্চা সংগ্রহ করুন।",
                "Before transacting, collect an up-to-date khatian and porcha from the union land office."),
}


def person(rng):
    n = rng.choice(NAMES)
    return {"bn": n[0], "en": n[1]}


def scenario(ds, idx):
    return SCEN[(idx + zlib.crc32(ds.encode())) % len(SCEN)]


def _ev(bn, en):
    return {"bn": bn, "en": en}


def alert_evidence(typ, rng, area_dec):
    if typ == "fake_deed":
        return [_ev("দলিলের সিরিয়াল নম্বর নিবন্ধন সার্ভারের রেকর্ডে পাওয়া যায়নি", "Deed serial number not found in the registration server"),
                _ev("সাব-রেজিস্ট্রারের ডিজিটাল স্বাক্ষর যাচাই ব্যর্থ", "Sub-registrar digital signature verification failed"),
                _ev("দাতার এনআইডি রেকর্ডভুক্ত মালিকের সাথে মেলেনি", "Donor NID does not match the recorded owner")]
    if typ == "double_sale":
        gap, a, b = rng.randint(2, 12), rng.randint(1000, 9999), rng.randint(1000, 9999)
        return [_ev(f"{gap} দিনের ব্যবধানে একই দাগে 2টি বিক্রয় দলিল নিবন্ধিত", f"Two sale deeds registered on the same plot {gap} days apart"),
                _ev(f"দলিল নং {a} ও {b} — ভিন্ন দুই ক্রেতা", f"Deed nos. {a} and {b} — two different buyers"),
                _ev("দুই ক্রেতার নামজারি আবেদন একসাথে অপেক্ষমাণ", "Two buyers' mutation applications pending at once")]
    if typ == "rapid":
        return [_ev("90 দিনে 4 বার মালিকানা পরিবর্তন", "Ownership changed 4 times within 90 days"),
                _ev("প্রতিটি দলিলে ঘোষিত মূল্য বাজারদরের তুলনায় অস্বাভাবিক", "Declared value in each deed is abnormal versus market rate"),
                _ev("একই দলিল লেখক একাধিক হস্তান্তরে জড়িত", "The same deed writer appears in multiple transfers")]
    a = round(area_dec, 1)
    b = round(a * rng.uniform(0.6, 0.85), 1)
    return [_ev(f"দলিলে জমির পরিমাণ {a} শতাংশ, নামজারি রেকর্ডে {b} শতাংশ", f"Deed area {a} decimal vs mutation record {b} decimal"),
            _ev("দলিলের দাগ ও খতিয়ান নম্বর রেকর্ডের সাথে অমিল", "Plot and khatian numbers in the deed differ from records"),
            _ev("নিবন্ধনের পর নামজারির আবেদন করা হয়নি", "No mutation application filed after registration")]


_der = {}


def derived():
    idx, mode = index(), cur_mode()
    dsig = f"{idx['sig']}:{STATE['rev'][mode]}"
    c = _der.get(mode)
    if c and c["sig"] == dsig:
        return c
    alerts, by_plot = {}, {}
    for key, pl in idx["plots"].items():
        scen = scenario(pl["ds"], pl["idx"])
        if scen not in FRAUD_TYPES or len(alerts) >= 200:
            continue
        ds, no = key
        rng = random.Random(f"al:{ds}:{no}")
        d, s, bn, en = label_of(ds)
        aid = f"FR-{zlib.crc32(f'{ds}:{no}'.encode()) % 90000 + 10000}"
        area_dec = pl["props"]["area_sqm"] / 40.4686
        a = {"id": aid, "type": scen, "severity": "high" if scen in ("fake_deed", "double_sale") else "medium",
             "ds": ds, "plot_no": no, "label": pl["props"].get("label", no), "sheet": s,
             "district": {"bn": bn, "en": en}, "confidence": rng.randint(72, 98),
             "detected": (TODAY - timedelta(days=rng.randint(1, 20))).isoformat(),
             "default_status": "investigating" if rng.random() < 0.3 else "new",
             "evidence": alert_evidence(scen, rng, area_dec)}
        alerts[aid] = a
        by_plot.setdefault(key, []).append(a)
    for aid, a in STATE["custom"][mode]["alerts"].items():  # manually added by admin
        if (a["ds"], a["plot_no"]) in idx["plots"]:
            alerts[aid] = a
            by_plot.setdefault((a["ds"], a["plot_no"]), []).append(a)
    cases, n = {}, 0
    prefix = "API" if mode == "api" else "MUT"
    for key, pl in idx["plots"].items():
        if pl["idx"] % 2 or n >= 60:
            continue
        n += 1
        ds, no = key
        scen = scenario(ds, pl["idx"])
        rng = random.Random(f"wf:{ds}:{no}")
        ft = {a["type"] for a in by_plot.get(key, [])}
        stage = rng.choices(range(6), weights=[2, 2, 3, 3, 2, 2])[0]
        checks = wf_checks(scen, ft)
        bad = any(v != "pass" for k, v in checks if k != "fee_paid")
        if bad:
            stage = rng.choice([1, 2])
        status = "completed" if stage == 5 else "flagged" if bad else "delayed" if stage in (2, 3) and rng.random() < 0.25 else "ok"
        created = TODAY - timedelta(days=rng.randint(stage * 5 + 3, stage * 7 + 12))
        tl, d0 = [], created
        for s_i in range(stage + 1):
            tl.append({"date": d0.isoformat(), "key": STAGES[s_i]})
            d0 = min(TODAY, d0 + timedelta(days=rng.randint(2, 6)))
        d, s, bn, en = label_of(ds)
        h = zlib.crc32(f"case:{ds}:{no}".encode()) % 90000 + 10000
        cid = f"{prefix}-{TODAY.year}-{h:05d}"
        while cid in cases:  # keep ids stable per plot, resolve rare collisions
            h += 1
            cid = f"{prefix}-{TODAY.year}-{h:05d}"
        cases[cid] = {
            "id": cid, "ds": ds, "plot_no": no, "label": pl["props"].get("label", no), "sheet": s,
            "district": {"bn": bn, "en": en}, "buyer": person(rng), "seller": person(rng),
            "deed_no": f"{rng.randint(1000, 9999)}/{created.year}", "deed_date": created.isoformat(),
            "office": {"bn": f"{bn} সাব-রেজিস্ট্রি অফিস", "en": f"{en} Sub-Registry Office"},
            "created": created.isoformat(), "sla_due": (created + timedelta(days=28)).isoformat(),
            "stage": stage, "status": status, "timeline": tl,
            "checks": [{"key": k, "level": v if stage >= 1 else "queued"} for k, v in checks],
            "raw": [[k, v] for k, v in checks],
        }
    for cid, c in STATE["custom"][mode]["cases"].items():
        if (c["ds"], c["plot_no"]) in idx["plots"]:
            cases[cid] = c
    _der[mode] = {"sig": dsig, "alerts": alerts, "cases": cases, "by_plot": by_plot}
    return _der[mode]


def wf_checks(scen, ft):
    return [("deed_verified", "fail" if "fake_deed" in ft else "pass"),
            ("no_double_sale", "fail" if "double_sale" in ft else "pass"),
            ("area_match", "fail" if "mismatch" in ft else "pass"),
            ("no_pending_case", "warn" if scen == "pending" else "pass"),
            ("khas_check", "fail" if scen == "khas" else "warn" if scen == "acq" else "pass"),
            ("velocity", "warn" if "rapid" in ft else "pass"),
            ("fee_paid", "pass")]


def alert_view(a):
    v = dict(a)
    v["status"] = STATE["fraud"].get(a["id"], a["default_status"])
    return v


def case_view(c):
    v = dict(c)
    st = STATE["wf"].get(c["id"])
    if st:
        v.update(stage=st["stage"], status=st["status"], timeline=c["timeline"] + st["log"])
    if v.get("raw"):  # automated checks become visible once the case leaves "registration"
        v["checks"] = [{"key": k, "level": lv if v["stage"] >= 1 else "queued"} for k, lv in v["raw"]]
    v["days_left"] = (date.fromisoformat(c["sla_due"]) - TODAY).days
    return v


def record(key):
    pl = index()["plots"].get(key)
    if not pl:
        return None
    ds, no = key
    p = pl["props"]
    rng = random.Random(f"{ds}:{no}")
    owner = person(rng)  # first draw: api_search relies on it
    father = person(rng)
    scen = scenario(ds, pl["idx"])
    d, s, bn, en = label_of(ds)
    n = 4 if scen == "rapid" else rng.randint(2, 3)
    if scen == "rapid":
        dates = [TODAY - timedelta(days=x) for x in (88, 61, 33, 9)]
    else:
        dates, cur = [], TODAY - timedelta(days=rng.randint(60, 700))
        for _ in range(n):
            dates.append(cur)
            cur -= timedelta(days=rng.randint(150, 600))
        dates.reverse()
    chain = [person(rng) for _ in range(n)] + [owner]
    hist = []
    for i, dt in enumerate(dates):
        hist.append({"date": dt.isoformat(), "type": "sale" if scen == "rapid" else rng.choice(["sale", "sale", "inherit", "gift", "partition"]),
                     "from": chain[i], "to": chain[i + 1], "deed_no": f"{rng.randint(1000, 9999)}/{dt.year}",
                     "case_no": f"{rng.randint(100, 999)}/{dt.year}", "status": "approved"})
    hist.reverse()
    pending = []
    if scen == "pending":
        for typ in rng.sample(list(FORUM), rng.randint(1, 2)):
            pending.append({"type": typ, "forum": FORUM[typ], "ref": f"{rng.randint(10, 899)}/{TODAY.year - rng.randint(0, 2)}",
                            "since": (TODAY - timedelta(days=rng.randint(30, 500))).isoformat()})
    khas = rng.choice(["khas", "khas", "vested"]) if scen == "khas" else "none"
    acq = rng.choice(["proposed", "notified"]) if scen == "acq" else "none"
    flags = [alert_view(a) for a in derived()["by_plot"].get(key, [])]
    ft = {f["type"] for f in flags}
    checks = [
        ("ownership_verified", "fail" if "fake_deed" in ft else "warn" if "double_sale" in ft else "pass"),
        ("mutation_current", "fail" if "double_sale" in ft else "warn" if "mismatch" in ft else "pass"),
        ("pending_cases", "warn" if pending else "pass"),
        ("khas_vested", "pass" if khas == "none" else "fail"),
        ("acquisition", {"none": "pass", "proposed": "warn", "notified": "fail"}[acq]),
        ("fraud_scan", "fail" if ft & {"fake_deed", "double_sale"} else "warn" if ft else "pass"),
    ]
    score = 3 + rng.randint(0, 6) + sum({"fake_deed": 55, "double_sale": 50, "rapid": 30, "mismatch": 25}[t] for t in ft)
    score += min(40, 20 * len(pending)) + (70 if khas != "none" else 0) + {"none": 0, "proposed": 25, "notified": 45}[acq]
    score = min(100, score)
    advice = [ADVICE[t] for t in ft] + ([ADVICE["pending"]] if pending else []) + ([ADVICE["khas"]] if khas != "none" else []) \
        + ([ADVICE["acq"]] if acq != "none" else []) + [ADVICE["generic"]]
    area = p["area_sqm"]
    return {
        "ds": ds, "plot_no": no, "label": p.get("label", no), "sheet": s, "district": {"bn": bn, "en": en},
        "mouza": p.get("mouza") or f"মৌজা {p.get('m_code', '')}", "m_code": p.get("m_code"),
        "jl": p.get("jl_no_en"), "sht": p.get("sht_no_en"), "area_sqm": area,
        "owner": owner, "father": father, "nid": f"{rng.randint(1970, 2002)}{'•' * 9}{rng.randint(10, 99)}",
        "khatian": rng.randint(100, 2999), "dag": no, "share": "1/1" if n < 3 else "1/2",
        "history": hist, "pending": pending, "khas": khas, "acq": acq, "flags": flags,
        "checks": [{"key": k, "level": v} for k, v in checks], "score": score,
        "verdict": "safe" if score < 25 else "caution" if score < 55 else "danger",
        "advice": [{"bn": a[0], "en": a[1]} for a in advice],
        "parts": index()["parts"].get((ds, no), []), "geometry": pl["geom"], "center": [pl["lat"], pl["lng"]],
        "source": "api" if api_meta(ds) else "demo",
    }


# ----------------------------------------------------------------- API
def err(msg, code=400):
    return jsonify(error=msg), code


@app.before_request
def _admin_guard():
    if ADMIN_KEY and request.path.startswith("/api/admin/"):
        if not hmac.compare_digest(request.headers.get("X-Admin-Key", ""), ADMIN_KEY):
            return err("admin key required / অ্যাডমিন কী প্রয়োজন", 401)


@app.after_request
def _sec_headers(r):
    r.headers.setdefault("X-Content-Type-Options", "nosniff")
    r.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    r.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return r


@app.route("/api/datasets")
def api_datasets():
    return jsonify(summaries())


def _num(*vals):
    return all(re.fullmatch(r"\d+", v or "") for v in vals)


@app.route("/api/bd/divisions")
def api_bd_divisions():
    try:
        return jsonify(_bd_request("/getAdminByCode", {"level": "forDiv", "column": "division_n"}))
    except RuntimeError as exc:
        return err(str(exc), 502)


@app.route("/api/bd/districts")
def api_bd_districts():
    division = request.args.get("division_code", "").strip()
    if not _num(division):
        return err("division_code must be numeric")
    try:
        return jsonify(_bd_request("/getAdminByCode", {"level": "forDist", "division_code": division}))
    except RuntimeError as exc:
        return err(str(exc), 502)


@app.route("/api/bd/thanas")
def api_bd_thanas():
    division, district = (request.args.get(k, "").strip() for k in ("division_code", "district_code"))
    if not _num(division, district):
        return err("division_code and district_code must be numeric")
    try:
        return jsonify(_bd_request("/getAdminByCode", {"level": "forThana", "division_code": division, "district_code": district}))
    except RuntimeError as exc:
        return err(str(exc), 502)


@app.route("/api/bd/mouzas")
def api_bd_mouzas():
    division, district, thana = (request.args.get(k, "").strip() for k in ("division_code", "district_code", "thana_code"))
    if not _num(division, district, thana):
        return err("division_code, district_code and thana_code must be numeric")
    try:
        return jsonify(_bd_request("/getAdminByCode", {
            "level": "forMauza", "division_code": division, "district_code": district, "thana_code": thana}))
    except RuntimeError as exc:
        return err(str(exc), 502)


@app.route("/api/bd/map-data")
def api_bd_map_data():
    level = request.args.get("level", "").strip()
    table_name = request.args.get("tableName", "").strip()
    allowed_levels = {"forDiv", "forDist", "forThana", "forMauza"}
    if level not in allowed_levels or not re.fullmatch(r"[A-Za-z0-9_]+", table_name):
        return err("invalid level or tableName")
    fields = {"level": level, "tableName": table_name}
    for name in ("divVal", "distVal", "thanaVal", "mauzaVal", "plotOrLanduse"):
        value = request.args.get(name)
        if value:
            fields[name] = value
    if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", str(value)) for key, value in fields.items()
           if key not in ("level", "tableName", "plotOrLanduse")):
        return err("map values contain invalid characters")
    try:
        return jsonify(_bd_request("/getGeomByCode", fields))
    except RuntimeError as exc:
        return err(str(exc), 502)


def _norm_rows(rows, meta, is_plot):
    out = []
    for i, r in enumerate(rows or []):
        if not isinstance(r, dict) or not r.get("geom"):
            continue
        r = dict(r)
        no = r.get("plot_no_en") or r.get("plot_no") or (r.get("label") if is_plot else None)
        if no is None and is_plot:
            no = i + 1
        if no is not None and str(no) != "":
            r["plot_no_en"] = str(no)
            r.setdefault("label", bn_digits(no))
        if is_plot:
            r.setdefault("mouza", meta.get("mouza_bn") or meta.get("mouza_en") or "")
        r.setdefault("m_code", meta.get("mouza_code", ""))
        out.append(r)
    return out


@app.route("/api/bd/register", methods=["POST"])
def api_bd_register():
    """Save a government-API area under a dynamic id so every feature can use it like a dataset."""
    b = request.get_json(silent=True) or {}
    meta = b.get("meta") or {}
    codes = [re.sub(r"[^A-Za-z0-9]", "", str(meta.get(k, ""))) for k in ("division", "district", "thana", "mouza")]
    if not all(codes):
        return err("division, district, thana and mouza are required")
    ds = ("api_" + "_".join(codes)).lower()
    plots, lands = _norm_rows(b.get("plot"), meta, True), _norm_rows(b.get("land"), meta, False)
    if not plots:
        return err("no plot geometry returned by the government service / সরকারি সার্ভিস থেকে দাগ পাওয়া যায়নি", 422)
    assign_parts(plots, lands)
    keep = {k: ("" if v is None else str(v)[:200]) for k, v in meta.items()}
    keep["boundaries"] = {k: v for k, v in (b.get("boundaries") or {}).items()
                          if k in ("district", "thana") and isinstance(v, str)}
    keep["saved"] = TODAY.isoformat()

    def dump(name, obj):
        with open(os.path.join(API_DIR, name), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
    dump(f"{ds}_meta.json", keep)
    dump(f"{ds}_plot.json", {"plots": plots})
    dump(f"{ds}_land.json", {"landuse": lands})
    return jsonify(ok=True, id=ds, plots=len(plots), parts=len(lands))


@app.route("/api/admin/dataset/<ds>", methods=["DELETE"])
def api_admin_dataset_delete(ds):
    ds = ds.lower()
    if not re.fullmatch(r"api_[a-z0-9_]+", ds) or not api_meta(ds):
        return err("only saved API areas can be removed", 404)
    for k in ("plot", "land", "meta"):
        try:
            os.remove(os.path.join(API_DIR, f"{ds}_{k}.json"))
        except OSError:
            pass
    return jsonify(ok=True)


@app.route("/api/version")
def api_version():
    return jsonify(sig=signature())


@app.route("/api/stats")
def api_stats():
    S, dr = summaries(), derived()
    plots = sum(s["plots"] for s in S)
    return jsonify(datasets=len(S), plots=plots, checks=plots * 6, alerts=len(dr["alerts"]), cases=len(dr["cases"]),
                   sample_cases=[{"id": c["id"], "status": case_view(c)["status"]} for c in list(dr["cases"].values())[:6]])


@app.route("/api/data")
def api_data():
    try:
        files, land, plot = discover(), [], []
        ids = [i for i in request.args.get("ids", "").split(",") if i in files]
        for i in ids:
            if "land" in files[i]:
                land += load_file(files[i]["land"], i)["features"]
            if "plot" in files[i]:
                plot += load_file(files[i]["plot"], i)["features"]
        bounds = [{"ds": i, **((api_meta(i) or {}).get("boundaries") or {})} for i in ids if api_meta(i)]
        return jsonify(land=fc_of(land), plot=fc_of(plot), ids=ids, bounds=bounds, sig=signature())
    except Exception as e:
        return err(str(e), 500)


@app.route("/api/plots/<ds>")
def api_plots(ds):
    return jsonify([{"plot_no": k[1], "label": v["props"].get("label", k[1])}
                    for k, v in index()["plots"].items() if k[0] == ds.lower()])


@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip().lower()
    if not q:
        return jsonify(results=[])
    res = []
    for key, pl in index()["plots"].items():
        p = pl["props"]
        no = key[1].lower()
        owner = person(random.Random(f"{key[0]}:{key[1]}"))
        hay = f"{no} {p.get('label', '')} {p.get('m_code', '')} {p.get('mouza', '')} {owner['bn']} {owner['en']}".lower()
        if q in hay:
            d, s, bn, en = label_of(key[0])
            res.append({"ds": key[0], "plot_no": key[1], "label": p.get("label", key[1]), "sheet": s,
                        "district": {"bn": bn, "en": en}, "owner": owner, "lat": pl["lat"], "lng": pl["lng"],
                        "area_sqm": p["area_sqm"], "rank": 0 if no == q else 1 if no.startswith(q) else 2})
    res.sort(key=lambda r: r["rank"])
    return jsonify(results=res[:40])


@app.route("/api/verify/<ds>/<plot_no>")
def api_verify(ds, plot_no):
    r = record((ds.lower(), plot_no))
    return jsonify(r) if r else err("plot not found / দাগ পাওয়া যায়নি", 404)


@app.route("/api/track/<cid>")
def api_track(cid):
    c = derived()["cases"].get(cid.strip().upper())
    return jsonify(case_view(c)) if c else err("case not found / কেস পাওয়া যায়নি", 404)


# ---- admin (MVP: no authentication)
@app.route("/api/admin/overview")
def api_admin_overview():
    dr = derived()
    alerts = [alert_view(a) for a in dr["alerts"].values()]
    cases = [case_view(c) for c in dr["cases"].values()]
    done = [c for c in cases if c["status"] == "completed"]
    avg = round(sum((date.fromisoformat(c["timeline"][-1]["date"]) - date.fromisoformat(c["created"])).days for c in done) / len(done)) if done else 0
    return jsonify(
        open_alerts=sum(a["status"] in ("new", "investigating") for a in alerts),
        high=sum(a["severity"] == "high" and a["status"] in ("new", "investigating") for a in alerts),
        in_progress=sum(c["status"] in ("ok", "delayed") for c in cases),
        flagged=sum(c["status"] == "flagged" for c in cases), avg_days=avg,
        by_type={t: sum(a["type"] == t for a in alerts) for t in FRAUD_TYPES},
        by_stage=[sum(c["stage"] == i for c in cases) for i in range(6)],
        recent=sorted(alerts, key=lambda a: a["detected"], reverse=True)[:5])


@app.route("/api/admin/fraud")
def api_admin_fraud():
    return jsonify(alerts=sorted((alert_view(a) for a in derived()["alerts"].values()), key=lambda a: a["detected"], reverse=True))


@app.route("/api/admin/fraud/<aid>", methods=["POST"])
def api_admin_fraud_set(aid):
    st = (request.get_json(silent=True) or {}).get("status")
    if aid not in derived()["alerts"] or st not in ("new", "investigating", "resolved", "false_positive"):
        return err("bad request")
    STATE["fraud"][aid] = st
    return jsonify(alert=alert_view(derived()["alerts"][aid]))


@app.route("/api/admin/workflow")
def api_admin_wf():
    return jsonify(cases=[case_view(c) for c in derived()["cases"].values()])


@app.route("/api/admin/workflow/<cid>", methods=["POST"])
def api_admin_wf_set(cid):
    c = derived()["cases"].get(cid)
    act = (request.get_json(silent=True) or {}).get("action")
    if not c or act not in ("advance", "hold", "release", "reject"):
        return err("bad request")
    st = STATE["wf"].setdefault(cid, {"stage": c["stage"], "status": c["status"], "log": []})
    if act == "advance":
        if st["status"] in ("flagged", "rejected", "completed"):
            return err("blocked: flagged/closed — release first / আটকানো বা বন্ধ — আগে ছাড় দিন", 409)
        st["stage"] = min(5, st["stage"] + 1)
        st["status"] = "completed" if st["stage"] == 5 else "ok"
        if st["stage"] == 1 and any(lv != "pass" for k, lv in (c.get("raw") or []) if k != "fee_paid"):
            st["status"] = "flagged"  # automated checks failed -> needs an officer
        st["log"].append({"date": TODAY.isoformat(), "key": STAGES[st["stage"]]})
    else:
        st["status"] = {"hold": "flagged", "release": "ok", "reject": "rejected"}[act]
        st["log"].append({"date": TODAY.isoformat(), "key": act})
    return jsonify(case=case_view(c))


def _plot_key(b):
    key = (str(b.get("ds", "")).strip().lower(), str(b.get("plot_no", "")).strip())
    return key if key in index()["plots"] else None


def _free_id(fmt, exists):
    n = zlib.crc32(str(time.time_ns()).encode()) % 90000 + 10000
    while exists(fmt(n)):
        n = n + 1 if n < 99999 else 10000
    return fmt(n)


@app.route("/api/admin/fraud", methods=["POST"])
def api_admin_fraud_create():
    """Admin adds a fraud alert on any plot of the active data mode."""
    b = request.get_json(silent=True) or {}
    key, typ = _plot_key(b), b.get("type")
    if not key:
        return err("plot not found in this dataset / এই ডেটাসেটে দাগ পাওয়া যায়নি", 404)
    if typ not in FRAUD_TYPES:
        return err("invalid fraud type / জালিয়াতির ধরন সঠিক নয়")
    mode, (ds, no) = cur_mode(), key
    pl = index()["plots"][key]
    sev = b.get("severity")
    if sev not in ("high", "medium"):
        sev = "high" if typ in ("fake_deed", "double_sale") else "medium"
    try:
        conf = max(1, min(99, int(b.get("confidence", 85))))
    except (TypeError, ValueError):
        conf = 85
    note = str(b.get("note") or "").strip()[:300]
    taken, store = derived()["alerts"], STATE["custom"][mode]["alerts"]
    aid = _free_id(lambda n: f"FR-C{n}", lambda i: i in taken or i in store)
    evidence = alert_evidence(typ, random.Random(aid), pl["props"]["area_sqm"] / 40.4686)
    if note:
        evidence.append(_ev(note, note))
    d, s, bn, en = label_of(ds)
    store[aid] = {"id": aid, "type": typ, "severity": sev, "ds": ds, "plot_no": no,
                  "label": pl["props"].get("label", no), "sheet": s, "district": {"bn": bn, "en": en},
                  "confidence": conf, "detected": TODAY.isoformat(), "default_status": "new",
                  "evidence": evidence, "custom": True, "note": note}
    _touch(mode)
    return jsonify(ok=True, alert=alert_view(store[aid]))


@app.route("/api/admin/fraud/<aid>", methods=["DELETE"])
def api_admin_fraud_delete(aid):
    mode = cur_mode()
    if aid not in STATE["custom"][mode]["alerts"]:
        return err("only manually added alerts can be removed / শুধু ম্যানুয়ালি যোগ করা সতর্কতা মোছা যায়", 404)
    STATE["custom"][mode]["alerts"].pop(aid)
    STATE["fraud"].pop(aid, None)
    _touch(mode)
    return jsonify(ok=True)


@app.route("/api/admin/workflow", methods=["POST"])
def api_admin_wf_create():
    """Admin opens an e-Mutation case for any plot of the active data mode (starts at 'registration')."""
    b = request.get_json(silent=True) or {}
    key = _plot_key(b)
    if not key:
        return err("plot not found in this dataset / এই ডেটাসেটে দাগ পাওয়া যায়নি", 404)
    mode, (ds, no) = cur_mode(), key
    pl, dr = index()["plots"][key], derived()
    store = STATE["custom"][mode]["cases"]
    prefix = "API" if mode == "api" else "MUT"
    cid = _free_id(lambda n: f"{prefix}-{TODAY.year}-{n:05d}", lambda i: i in dr["cases"] or i in store)
    rng = random.Random(cid)

    def who(v):
        v = str(v or "").strip()[:80]
        return {"bn": v, "en": v} if v else person(rng)

    try:
        dd = date.fromisoformat(str(b.get("deed_date") or "")[:10])
    except ValueError:
        dd = TODAY
    dd = min(dd, TODAY)
    deed_no = str(b.get("deed_no") or "").strip()[:30] or f"{rng.randint(1000, 9999)}/{dd.year}"
    ft = {a["type"] for a in dr["by_plot"].get(key, [])}
    raw = [[k, v] for k, v in wf_checks(scenario(ds, pl["idx"]), ft)]
    d, s, bn, en = label_of(ds)
    store[cid] = {
        "id": cid, "ds": ds, "plot_no": no, "label": pl["props"].get("label", no), "sheet": s,
        "district": {"bn": bn, "en": en}, "buyer": who(b.get("buyer")), "seller": who(b.get("seller")),
        "deed_no": deed_no, "deed_date": dd.isoformat(),
        "office": {"bn": f"{bn} সাব-রেজিস্ট্রি অফিস", "en": f"{en} Sub-Registry Office"},
        "created": TODAY.isoformat(), "sla_due": (TODAY + timedelta(days=28)).isoformat(),
        "stage": 0, "status": "ok", "timeline": [{"date": TODAY.isoformat(), "key": "registration"}],
        "checks": [{"key": k, "level": "queued"} for k, _ in raw], "raw": raw, "custom": True,
    }
    _touch(mode)
    return jsonify(ok=True, case=case_view(store[cid]))


@app.route("/api/admin/workflow/<cid>", methods=["DELETE"])
def api_admin_wf_delete(cid):
    mode = cur_mode()
    if cid not in STATE["custom"][mode]["cases"]:
        return err("only manually added cases can be removed / শুধু ম্যানুয়ালি যোগ করা কেস মোছা যায়", 404)
    STATE["custom"][mode]["cases"].pop(cid)
    STATE["wf"].pop(cid, None)
    _touch(mode)
    return jsonify(ok=True)


HARDEN_HTML = r"""
<style>body.locked,body.locked *{-webkit-user-select:none;user-select:none}body.locked input,body.locked select,body.locked textarea,body.locked .sel{-webkit-user-select:text;user-select:text}img,svg{-webkit-user-drag:none}</style>
<script src="https://cdn.jsdelivr.net/npm/disable-devtool@0.3.7/disable-devtool.min.js"></script>
<script>
(function(){
  var stop=function(e){e.preventDefault();e.stopPropagation();return false};
  var lock=function(){try{document.body.innerHTML='<div style="font:600 18px/1.8 system-ui,sans-serif;display:grid;place-items:center;min-height:100vh;text-align:center;padding:24px">Developer tools are not allowed on this page. Close them to continue.<br>ডেভেলপার টুল ব্যবহার করা যাবে না। বন্ধ করে চালিয়ে যান।</div>'}catch(e){}};
  document.addEventListener('DOMContentLoaded',function(){document.body.classList.add('locked')});
  addEventListener('contextmenu',stop,true);addEventListener('dragstart',stop,true);
  addEventListener('keydown',function(e){var c=e.ctrlKey||e.metaKey,k=e.code||'';
    if(k==='F12'||(c&&e.shiftKey&&/^Key[IJCKE]$/.test(k))||(c&&e.altKey&&/^Key[IJCU]$/.test(k))||(c&&/^Key[US]$/.test(k)))stop(e)},true);
  if(window.DisableDevtool)DisableDevtool({disableMenu:true,clearLog:true,interval:400,ondevtoolopen:function(){lock()},ondevtoolclose:function(){location.reload()}});
})();
</script>
"""


@app.route("/api/admin/upload/<ds>/<kind>", methods=["POST"])
def api_admin_upload(ds, kind):
    if cur_mode() == "api":
        return err("upload is for dataset mode; use the API loader / আপলোড শুধু ডেটাসেট মোডে")
    ds = ds.lower()
    if kind not in ("land", "plot") or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,60}", ds):
        return err("name must look like gazipur_001 / নাম হতে হবে gazipur_001 এর মতো")
    try:
        data = json.loads(request.get_data(as_text=True))
    except Exception as e:
        return err(f"Invalid JSON: {e}")
    with open(os.path.join(DATA_DIR, f"{ds}_{kind}.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    return jsonify(ok=True)


@app.route("/admin")
def admin_redirect():
    return redirect("/#/admin")


@app.route("/")
def index_page():
    return Response(PAGE.replace("<!--HARDEN-->", HARDEN_HTML if HARDEN["on"] else ""), mimetype="text/html")


@app.route("/assets/heto_1.jpeg")
def hero_image():
    return send_file(os.path.join(BASE, "src", "img", "heto_1.jpeg"), mimetype="image/jpeg")


@app.route("/assets/logo.png")
def logo_image():
    return send_file(os.path.join(BASE, "src", "img", "logo.png"), mimetype="image/png")


@app.route("/assets/logo_dark.png")
def logo_dark_image():
    return send_file(os.path.join(BASE, "src", "img", "logo_dark.png"), mimetype="image/png")


# ----------------------------------------------------------------- demo data
DEMO = [("gazipur_001", 24.0023, 90.4264, "গাজীপুর সদর"), ("dhaka_014", 23.8583, 90.2667, "সাভার"),
        ("cumilla_007", 23.4607, 91.1809, "কুমিল্লা সদর")]
CLASSES = [("Settlement", "Homestead"), ("Agriculture", "Paddy Field"), ("Commercial Area", "Shop"),
           ("Water Body", "Pond"), ("Vacant Land", "Fallow"), ("Industrial Area", "Garment Factory")]


def write_demo():
    if _scan([DATA_DIR, BASE]):
        return
    for ds, lat, lng, mouza in DEMO:
        rng = random.Random(ds)
        sheet = SPLIT_RE.match(ds)["s"]
        kx, ky = 111320 * math.cos(math.radians(lat)), 110574
        plots, lands, gid = [], [], 0
        w, h, gap = 34, 26, 4
        for r in range(4):
            for c in range(5):
                gid += 1
                x0 = (c * (w + gap) - 2.5 * (w + gap))
                y0 = (r * (h + gap + (10 if r >= 2 else 0)) - 2 * (h + gap))
                jit = lambda: rng.uniform(-1.2, 1.2)
                pts = [(x0 + jit(), y0 + jit()), (x0 + w + jit(), y0 + jit()), (x0 + w + jit(), y0 + h + jit()), (x0 + jit(), y0 + h + jit())]
                pts = [(lng + x / kx, lat + y / ky) for x, y in pts]
                wkt = lambda ring: "POLYGON((" + ", ".join(f"{x:.7f} {y:.7f}" for x, y in ring + [ring[0]]) + "))"
                no = str(120 + gid)
                m_code = str(3600 + SCEN.index("clean") + len(ds))
                base = {"m_code": m_code, "plot_no_en": no, "label": bn_digits(no)}
                plots.append({"gid": gid, **base, "jl_no_en": "12", "sht_no_en": sheet, "mouza": mouza, "geom": wkt(pts)})
                lerp = lambda a, b, t: (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
                if rng.random() < 0.5:
                    t = rng.uniform(0.35, 0.65)
                    m03, m12 = lerp(pts[0], pts[3], t), lerp(pts[1], pts[2], t)
                    rings = [[pts[0], pts[1], m12, m03], [m03, m12, pts[2], pts[3]]]
                else:
                    rings = [pts]
                for ring in rings:
                    maj, sub = rng.choices(CLASSES, weights=[5, 4, 2, 1, 2, 1])[0]
                    lands.append({"gid": len(lands) + 1, **base, "maj_class": maj, "sub_class": sub, "geom": wkt(ring)})
        for kind, key, rows in (("plot", "plots", plots), ("land", "landuse", lands)):
            with open(os.path.join(DATA_DIR, f"{ds}_{kind}.json"), "w", encoding="utf-8") as f:
                json.dump({key: rows}, f, ensure_ascii=False, indent=1)


# ----------------------------------------------------------------- Front-end
PAGE = r"""<!doctype html>
<html lang="bn" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>আমার ভূমি</title>
<script>try{document.documentElement.dataset.theme=localStorage.theme||(matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light');matchMedia('(prefers-color-scheme:dark)').addEventListener('change',e=>{if(!localStorage.theme)document.documentElement.dataset.theme=e.matches?'dark':'light'});}catch(e){}</script>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Hind+Siliguri:wght@400;500;600;700&family=Noto+Sans+Bengali:wght@400;500;600;700&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
:root{--nav:64px;--bg:#f3f8f4;--sf:#fff;--sf2:#eef5f0;--tx:#0f2218;--mu:#5a6f62;--ln:#dce8e0;--g:#0b7d4c;--g2:#16a96b;--gt:#e1f4e8;--on:#fff;
--wr:#b45309;--wrt:#fef3c7;--bd:#b91c1c;--bdt:#fee2e2;--sh:0 10px 30px rgba(8,50,30,.10);--glass:rgba(255,255,255,.86);--top-nav:rgba(255,255,255,.66)}
[data-theme=dark]{--bg:#07120c;--sf:#0e1d15;--sf2:#13271c;--tx:#e6f3ea;--mu:#8ba596;--ln:#1e3a2b;--g:#34d58f;--g2:#1fb673;--gt:#103223;--on:#04210f;
--wr:#fbbf24;--wrt:#3a2e0b;--bd:#f87171;--bdt:#3b1414;--sh:0 10px 30px rgba(0,0,0,.45);--glass:rgba(14,29,21,.88);--top-nav:rgba(14,29,21,.72)}
*{box-sizing:border-box}
html,body{margin:0;min-height:100%}
body{font-family:'Inter','Hind Siliguri','Noto Sans Bengali','Nirmala UI','Kalpurush','Vrinda',system-ui,sans-serif;background:var(--bg);color:var(--tx);line-height:1.55;font-size:16px}
a{color:inherit;text-decoration:none}
h1,h2,h3,h4{margin:0;line-height:1.25}
[hidden]{display:none!important}
.container{max-width:1160px;margin:0 auto;padding:0 20px}
/* nav */
.nav{position:sticky;top:0;z-index:2000;height:var(--nav);background:var(--top-nav);backdrop-filter:blur(14px);border-bottom:1px solid var(--ln)}
.nav-in{height:100%;max-width:1280px;margin:0 auto;padding:0 18px;display:flex;align-items:center;gap:14px}
.brand{display:flex;align-items:center;gap:10px;font-weight:700;font-size:19px;color:var(--g);flex-shrink:0}
.logo{display:block;width:156px;height:50px;object-fit:contain;object-position:left center}
[data-theme=dark] .logo-light{display:none!important}
html:not([data-theme=dark]) .logo-dark{display:none!important}
.nav-links{display:flex;align-items:center;gap:4px;margin-left:18px;flex:1;min-width:0}
.nav-links a{padding:8px 13px;border-radius:10px;color:var(--mu);font-weight:500;font-size:15px;white-space:nowrap;flex-shrink:0}
.nav-links a:hover{background:var(--sf2);color:var(--tx)}.nav-links a.on{background:var(--gt);color:var(--g)}
.tools{display:flex;gap:8px;align-items:center}
.seg{display:inline-flex;background:var(--sf2);border:1px solid var(--ln);border-radius:11px;padding:3px}
.seg button{border:0;background:none;color:var(--mu);padding:5px 11px;border-radius:8px;font:inherit;font-size:13px;font-weight:600;cursor:pointer}
.nav-links button{white-space:nowrap}
.seg button.on{background:var(--g);color:var(--on)}
.icon{width:38px;height:38px;border-radius:11px;border:1px solid var(--ln);background:var(--sf);color:var(--tx);cursor:pointer;font-size:17px}
.burger{display:inline-flex;align-items:center;justify-content:center}
#links:not(.open){display:none}
#links.open{display:flex}
#roleSeg{margin-left:auto}
/* buttons / forms */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;padding:11px 18px;border-radius:12px;border:1px solid transparent;background:var(--g);color:var(--on);font:inherit;font-weight:600;font-size:15px;cursor:pointer;transition:.15s}
.btn:hover{filter:brightness(1.08);transform:translateY(-1px)}
.btn.alt{background:var(--sf);color:var(--tx);border-color:var(--ln)}.btn.sm{padding:7px 12px;font-size:13px;border-radius:10px}
.btn.danger{background:var(--bd);color:#fff}
.btnrow{display:flex;gap:8px;flex-wrap:wrap}
input,select{font:inherit;color:var(--tx);background:var(--sf2);border:1px solid var(--ln);border-radius:11px;padding:10px 12px;width:100%;outline:none;min-width:0}
input:focus,select:focus{border-color:var(--g2);box-shadow:0 0 0 3px color-mix(in srgb,var(--g2) 22%,transparent)}
select:disabled{opacity:.6}
label.f{display:block;font-size:13px;color:var(--mu);font-weight:500}label.f>*:last-child{margin-top:5px}
.card{background:var(--sf);border:1px solid var(--ln);border-radius:18px;padding:20px;box-shadow:var(--sh)}
.badge{display:inline-block;padding:2px 10px;border-radius:99px;font-size:12px;font-weight:600;background:var(--sf2);color:var(--mu);white-space:nowrap}
.badge.ok,.badge.pass,.badge.safe,.badge.completed,.badge.resolved{background:var(--gt);color:var(--g)}
.badge.warn,.badge.caution,.badge.medium,.badge.delayed,.badge.investigating{background:var(--wrt);color:var(--wr)}
.badge.fail,.badge.danger,.badge.high,.badge.flagged,.badge.rejected,.badge.new{background:var(--bdt);color:var(--bd)}
/* home */
.hero{position:relative;isolation:isolate;background:linear-gradient(90deg,color-mix(in srgb,var(--bg) 96%,transparent) 0%,color-mix(in srgb,var(--bg) 82%,transparent) 48%,color-mix(in srgb,var(--bg) 28%,transparent) 100%),url('/assets/heto_1.jpeg') center/cover no-repeat;padding:72px 0 54px;min-height:calc(100vh - var(--nav))}
.hero:after{content:'';position:absolute;inset:0;z-index:-1;background:linear-gradient(135deg,color-mix(in srgb,var(--g) 18%,transparent),transparent 55%,color-mix(in srgb,var(--g2) 12%,transparent))}
.hero-in{display:grid;grid-template-columns:minmax(0,720px);gap:36px;align-items:center}
.hero-in>div:first-child{position:relative;z-index:1}
.pill{display:inline-block;background:var(--sf);border:1px solid var(--ln);color:var(--g);padding:5px 14px;border-radius:99px;font-size:13px;font-weight:600}
.hero h1{font-size:clamp(30px,4.4vw,50px);margin:16px 0 12px;font-weight:700}
.hero p{color:var(--mu);font-size:18px;margin:0 0 22px;max-width:560px}
.searchcard{display:grid;gap:14px}
.row3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px}.row2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.or{display:flex;align-items:center;gap:10px;color:var(--mu);font-size:12px}.or:before,.or:after{content:'';flex:1;height:1px;background:var(--ln)}
.placebox{display:flex;gap:8px}
.hero-art{position:relative;min-height:330px}
.hero-image{display:block;width:100%;height:auto;aspect-ratio:1373 / 784;object-fit:cover;border:1px solid var(--ln);border-radius:24px;box-shadow:var(--sh);filter:saturate(.9)}
.mock{display:grid;grid-template-columns:repeat(4,1fr);gap:7px;transform:rotate(-4deg);padding:16px;background:var(--sf);border:1px solid var(--ln);border-radius:24px;box-shadow:var(--sh)}
.mock i{display:block;aspect-ratio:1.15;border-radius:9px;border:2px solid #ffd60a;background:var(--g2);opacity:.85}
.mock i:nth-child(3n){background:#f4a261}.mock i:nth-child(5n){background:#8ab17d}.mock i:nth-child(7n){background:#38bdf8}.mock i:nth-child(4){background:#e63946}
.float{position:absolute;background:var(--sf);border:1px solid var(--ln);border-radius:14px;padding:10px 14px;font-weight:600;font-size:14px;box-shadow:var(--sh)}
.f1{left:-14px;bottom:44px;color:var(--g)}.f2{right:-6px;top:18px;color:var(--bd)}
.stats{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin:0 auto;padding:42px 20px 18px}
.stat{background:var(--sf);border:1px solid var(--ln);border-radius:16px;padding:16px;text-align:center}
.stat b{display:block;font-size:26px;color:var(--g)}.stat span{font-size:13px;color:var(--mu)}
.home-reveal{opacity:0;transform:translateY(28px);transition:opacity .7s ease,transform .7s ease}
.home-reveal.is-visible{opacity:1;transform:none}
.sec{padding:46px 0}.sec h2{font-size:clamp(24px,3vw,32px);margin-bottom:8px}.sec>.container>p{color:var(--mu);margin:0 0 26px}
.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:16px}.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}
.feat{cursor:pointer;transition:.2s}.feat:hover{transform:translateY(-4px);border-color:var(--g2)}
.feat .ic{width:46px;height:46px;border-radius:14px;background:var(--gt);display:grid;place-items:center;font-size:23px;margin-bottom:12px}
.feat h3{font-size:18px;margin-bottom:6px}.feat p{margin:0;color:var(--mu);font-size:14.5px}
.band{background:linear-gradient(135deg,var(--g),var(--g2));color:var(--on);border-radius:24px;padding:34px}
.band h2{margin-bottom:18px}.band .grid4>div{background:rgba(255,255,255,.14);border-radius:16px;padding:16px}
.band h4{font-size:16px;margin-bottom:5px}.band p{margin:0;font-size:14px;opacity:.92}
.step{display:flex;gap:14px}.step .n{width:40px;height:40px;border-radius:50%;background:var(--g);color:var(--on);display:grid;place-items:center;font-weight:700;flex:none}
.step h4{margin-bottom:3px}.step p{margin:0;color:var(--mu);font-size:14.5px}
footer{border-top:1px solid var(--ln);padding:26px 0;color:var(--mu);font-size:14px;text-align:center}
.note{background:var(--wrt);color:var(--wr);border-radius:12px;padding:10px 14px;font-size:13.5px;display:inline-block}
/* page heads */
.phead{padding:30px 0 18px}.phead h1{font-size:30px}.phead p{color:var(--mu);margin:6px 0 0}
.formrow{display:grid;grid-template-columns:1fr 1fr 1fr auto;gap:10px;align-items:end}
.mode-label{font-size:11px;color:var(--mu);margin-right:4px}.api-fields{margin-top:10px}
/* verify */
.vbanner{display:flex;gap:22px;align-items:center;border-radius:22px;padding:22px 26px;margin:20px 0;border:1px solid var(--ln)}
.vbanner.safe{background:var(--gt)}.vbanner.caution{background:var(--wrt)}.vbanner.danger{background:var(--bdt)}
.vbanner h2{font-size:26px;margin-bottom:4px}.vbanner p{margin:0 0 10px;color:var(--mu)}
.gauge{--gc:var(--g);width:118px;height:118px;border-radius:50%;flex:none;display:grid;place-content:center;text-align:center;position:relative;background:conic-gradient(var(--gc) calc(var(--p)*1%),var(--ln) 0)}
.caution .gauge{--gc:var(--wr)}.danger .gauge{--gc:var(--bd)}
.gauge:before{content:'';position:absolute;inset:11px;border-radius:50%;background:var(--sf)}
.gauge b,.gauge small{position:relative}.gauge b{font-size:30px;line-height:1}.gauge small{font-size:11px;color:var(--mu)}
.vgrid{display:grid;grid-template-columns:repeat(2,1fr);gap:16px}.vgrid .wide{grid-column:1/-1}
.card h3{font-size:16px;margin-bottom:12px}
.ck{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px dashed var(--ln)}.ck:last-child{border:0}
.ck i{width:26px;height:26px;border-radius:50%;display:grid;place-items:center;font-style:normal;font-weight:700;font-size:14px;flex:none}
.ck.pass i{background:var(--gt);color:var(--g)}.ck.warn i{background:var(--wrt);color:var(--wr)}.ck.fail i{background:var(--bdt);color:var(--bd)}.ck.queued i{background:var(--sf2);color:var(--mu)}
.ck span{flex:1}.ck em{font-style:normal;font-size:12px;color:var(--mu)}
dl.kv{display:grid;grid-template-columns:auto 1fr;gap:6px 14px;margin:0;font-size:14.5px}dl.kv dt{color:var(--mu)}dl.kv dd{margin:0;font-weight:500}
#vmap{height:210px;border-radius:14px;overflow:hidden;margin-bottom:10px}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.tag{display:inline-block;padding:2px 10px;border-radius:99px;color:#fff;font-size:12px}
.tl{position:relative;padding-left:22px;margin:0;list-style:none}.tl:before{content:'';position:absolute;left:6px;top:6px;bottom:6px;width:2px;background:var(--ln)}
.tl li{position:relative;padding:0 0 14px}.tl li:before{content:'';position:absolute;left:-21px;top:6px;width:10px;height:10px;border-radius:50%;background:var(--g2);box-shadow:0 0 0 3px var(--gt)}
.tl small{color:var(--mu);display:block}
.empty{color:var(--mu);text-align:center;padding:18px}
.item{padding:10px 12px;border-radius:12px;background:var(--sf2);margin-bottom:8px;font-size:14px}
ul.adv{margin:0;padding-left:20px}ul.adv li{margin-bottom:6px}
/* stepper */
.stepper{display:flex;gap:4px;margin:6px 0 4px;overflow-x:auto;padding-bottom:4px}
.stepper div{flex:1;min-width:92px;text-align:center;font-size:12px;color:var(--mu)}
.stepper div:before{content:'';display:block;height:6px;border-radius:4px;background:var(--ln);margin-bottom:6px}
.stepper .done:before{background:var(--g2)}.stepper .cur:before{background:var(--g);box-shadow:0 0 0 3px var(--gt)}.stepper .cur.flagged:before{background:var(--bd);box-shadow:0 0 0 3px var(--bdt)}
.stepper .done,.stepper .cur{color:var(--tx);font-weight:600}
/* admin */
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:18px}
.tabs button{border:1px solid var(--ln);background:var(--sf);color:var(--mu);padding:9px 16px;border-radius:12px;font:inherit;font-weight:600;cursor:pointer}
.tabs button.on{background:var(--g);color:var(--on);border-color:var(--g)}
.kpis{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:16px}
.kpi{background:var(--sf);border:1px solid var(--ln);border-radius:16px;padding:16px}.kpi b{font-size:28px;color:var(--g);display:block}.kpi span{font-size:13px;color:var(--mu)}
.bar{display:grid;grid-template-columns:150px 1fr 34px;gap:10px;align-items:center;margin:9px 0;font-size:13.5px}
.bar div{height:10px;background:var(--sf2);border-radius:6px;overflow:hidden}.bar i{display:block;height:100%;background:linear-gradient(90deg,var(--g),var(--g2));border-radius:6px}
.tbl{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:14px}
th{text-align:left;color:var(--mu);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.04em;padding:10px 12px;border-bottom:1px solid var(--ln);white-space:nowrap}
td{padding:11px 12px;border-bottom:1px solid var(--ln);vertical-align:middle}tbody tr.r{cursor:pointer}tbody tr.r:hover{background:var(--sf2)}
td select{padding:6px 8px;width:auto;font-size:13px}
.filters{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px;align-items:center}.filters select{width:auto}
.fchip{border:1px solid var(--ln);background:var(--sf);color:var(--mu);padding:6px 13px;border-radius:99px;font:inherit;font-size:13px;cursor:pointer}.fchip.on{background:var(--gt);color:var(--g);border-color:var(--g2);font-weight:600}
.board{display:grid;grid-auto-flow:column;grid-auto-columns:minmax(230px,1fr);gap:12px;overflow-x:auto;padding-bottom:10px}
.col{background:var(--sf2);border:1px solid var(--ln);border-radius:16px;padding:12px;min-height:200px}
.col h4{font-size:13px;margin-bottom:10px;display:flex;justify-content:space-between;color:var(--mu)}
.kcard{background:var(--sf);border:1px solid var(--ln);border-left:4px solid var(--g2);border-radius:12px;padding:10px 12px;margin-bottom:8px;cursor:pointer;font-size:13.5px}
.kcard.flagged,.kcard.rejected{border-left-color:var(--bd)}.kcard.delayed{border-left-color:var(--wr)}.kcard:hover{box-shadow:var(--sh)}
.kcard b{display:block}.dots{display:flex;gap:4px;margin-top:6px}.dots i{width:9px;height:9px;border-radius:50%;background:var(--g2)}.dots i.warn{background:var(--wr)}.dots i.fail{background:var(--bd)}.dots i.queued{background:var(--ln)}
dialog{border:1px solid var(--ln);border-radius:22px;background:var(--sf);color:var(--tx);padding:24px;width:min(640px,94vw);max-height:88vh;box-shadow:0 30px 80px rgba(0,0,0,.4)}
dialog::backdrop{background:rgba(0,0,0,.55)}.dhead{display:flex;justify-content:space-between;gap:10px;align-items:start;margin-bottom:12px}
.two{display:grid;grid-template-columns:1fr 1fr;gap:16px}
/* map */
#v-map{position:fixed;top:var(--nav);left:0;right:0;bottom:0}
#map{position:absolute;inset:0;background:var(--sf2)}
#panel{position:absolute;z-index:1000;top:12px;left:12px;bottom:12px;width:340px;background:var(--glass);backdrop-filter:blur(12px);border:1px solid var(--ln);border-radius:18px;box-shadow:var(--sh);display:flex;flex-direction:column;overflow:hidden;transition:transform .25s}
#panel.hide{transform:translateX(-360px)}
#ptoggle{position:absolute;z-index:1001;top:20px;left:362px;transition:left .25s}#panel.hide+#ptoggle{left:12px}
.ph{padding:14px 16px 6px}.ph b{font-size:17px;display:block}.ph small{color:var(--mu)}
.pb{overflow:auto;padding:0 16px 16px;flex:1}.pb h4{font-size:11.5px;text-transform:uppercase;letter-spacing:.06em;color:var(--mu);margin:16px 0 8px}
.pb input[type=search]{margin-bottom:8px}
.stats4{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:10px 0}.stats4 div{background:var(--sf2);border-radius:12px;padding:8px 10px}.stats4 b{display:block;font-size:17px;color:var(--g)}.stats4 span{font-size:11px;color:var(--mu)}
.plist{max-height:200px;overflow:auto}
.rw{display:flex;align-items:center;gap:8px;padding:6px 6px;border-radius:9px;font-size:13px;cursor:pointer;text-align:left}.rw:hover{background:var(--sf2)}
.rw input{width:auto;margin:0;padding:0;flex:none;accent-color:var(--g2);cursor:pointer}
.rw .lbl{flex:1;min-width:0;text-align:left}
.sw{width:14px;height:14px;border-radius:4px;flex:none;border:1px solid rgba(0,0,0,.2)}.rw .n{margin-left:auto;color:var(--mu);font-size:12px;white-space:nowrap}
.chk{display:flex;align-items:center;gap:8px;padding:4px 2px;font-size:13.5px;cursor:pointer;text-align:left}.chk input{width:auto;margin:0;accent-color:var(--g2)}
#legend{display:flex;flex-direction:column;gap:2px;text-align:left}
input[type=checkbox],input[type=radio]{width:auto;margin:0;padding:0;accent-color:var(--g2);cursor:pointer}
.pb input[type=range]{padding:0;accent-color:var(--g2)}
.chip{border:1px solid var(--ln);background:var(--sf);color:var(--tx);padding:5px 10px;border-radius:99px;font:inherit;font-size:12px;cursor:pointer}.chip.on{background:var(--g);color:var(--on);border-color:var(--g)}
.fabs{position:absolute;z-index:1000;right:12px;bottom:112px}
#drawer{position:absolute;z-index:1000;top:12px;right:12px;width:340px;max-height:calc(100% - 24px);overflow:auto;background:var(--glass);backdrop-filter:blur(12px);border:1px solid var(--ln);border-radius:18px;box-shadow:var(--sh);padding:16px}
#drawer h3{font-size:19px}#drawer .x{float:right}.dload{padding:20px;text-align:center;color:var(--mu)}
.plot-lbl{background:rgba(0,0,0,.65);color:#fff;border:0;border-radius:6px;padding:1px 6px;font-weight:600;box-shadow:none}.plot-lbl:before{display:none}.nolabels .plot-lbl{display:none}
.ovpin{transform:translate(-50%,-100%);background:var(--g);color:var(--on);border-radius:12px;padding:5px 10px;font-size:12px;white-space:nowrap;box-shadow:0 6px 16px rgba(0,0,0,.35);cursor:pointer;text-align:center;font-family:'Hind Siliguri',sans-serif}
.ovpin b{display:block;font-size:13px}.ovpin:after{content:'';position:absolute;left:50%;bottom:-6px;margin-left:-6px;border:6px solid transparent;border-bottom:0;border-top-color:var(--g)}
#toast{position:fixed;z-index:5000;right:16px;bottom:18px;background:#0b1f15;color:#fff;padding:10px 16px;border-radius:12px;font-size:14px;display:none;max-width:90vw;box-shadow:var(--sh)}
body.map-mode footer{display:none}
/* api loader popup */
#loader{position:fixed;inset:0;z-index:6000;background:rgba(0,0,0,.55);display:grid;place-items:center}
#loader .card{width:min(420px,92vw)}.pbar{height:8px;background:var(--sf2);border-radius:6px;overflow:hidden;margin:12px 0}
.pbar i{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--g),var(--g2));transition:width .3s}

@media(max-width:960px){.hero{background-position:58% center}.hero-in{grid-template-columns:1fr}.hero-art{display:none}.stats{grid-template-columns:repeat(3,1fr)}.grid4{grid-template-columns:repeat(2,1fr)}.grid3{grid-template-columns:1fr}.kpis{grid-template-columns:repeat(3,1fr)}.formrow{grid-template-columns:1fr 1fr}}
@media(max-width:760px){.burger{display:block}.nav-links{display:none;position:absolute;top:var(--nav);left:0;right:0;background:var(--sf);border-bottom:1px solid var(--ln);flex-direction:column;align-items:stretch;padding:10px;margin:0}
.nav-links.open{display:flex}#roleSeg{margin:6px 0 0;align-self:flex-start}.brand span:last-child{font-size:17px}
.row3{grid-template-columns:1fr}.stats{grid-template-columns:repeat(2,1fr)}.grid4{grid-template-columns:1fr}.vgrid,.two{grid-template-columns:1fr}.vbanner{flex-direction:column;text-align:center}.kpis{grid-template-columns:repeat(2,1fr)}.formrow{grid-template-columns:1fr}.bar{grid-template-columns:110px 1fr 30px}
#panel{width:auto;right:12px;bottom:auto;max-height:46%}#panel.hide{transform:translateY(-120%)}#ptoggle{left:auto;right:12px;top:auto;bottom:12px}#panel.hide+#ptoggle{left:auto}
#drawer{top:auto;left:12px;right:12px;bottom:12px;width:auto;max-height:50%}.fabs{bottom:auto;top:calc(46% + 24px)}}
@media print{.nav,.btnrow,#toast,footer,.tabs{display:none!important}.view:not(#v-verify){display:none!important}body{background:#fff}}

/* ===== v2: typography, icons, responsive admin ===== */
body{text-rendering:optimizeLegibility;-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;font-kerning:normal;font-feature-settings:"kern" 1,"liga" 1,"locl" 1;overflow-wrap:break-word;-webkit-text-size-adjust:100%}
h1,h2,h3,h4{line-height:1.35}
input,select,textarea,button{font-family:inherit;line-height:1.5}
textarea{font:inherit;color:var(--tx);background:var(--sf2);border:1px solid var(--ln);border-radius:11px;padding:10px 12px;width:100%;outline:none;resize:vertical;min-height:84px}
textarea:focus{border-color:var(--g2);box-shadow:0 0 0 3px color-mix(in srgb,var(--g2) 22%,transparent)}
html[lang=bn] body{font-family:'Noto Sans Bengali','Hind Siliguri','Inter','Nirmala UI','Kalpurush','Vrinda',system-ui,sans-serif;line-height:1.75;font-variant-numeric:normal}
html[lang=bn] *{letter-spacing:0!important;text-transform:none!important}
html[lang=bn] .badge,html[lang=bn] .tag,html[lang=bn] .chip,html[lang=bn] .fchip{line-height:1.7;padding-top:3px;padding-bottom:3px}
html[lang=bn] .btn,html[lang=bn] .seg button{line-height:1.6}
html[lang=bn] th{font-size:13px}
html[lang=bn] .stat b,html[lang=bn] .stat span,html[lang=bn] .step .n{font-family:'Noto Sans Bengali','Hind Siliguri','Nirmala UI','Kalpurush','Vrinda',system-ui,sans-serif;font-variant-numeric:normal}
html[lang=bn] .stat span{line-height:1.5}
html[lang=bn] .step .n{font-size:18px;line-height:1;font-weight:700}
.ico{flex:none;vertical-align:-3px;display:inline-block}
[data-ic]{display:inline-flex;align-items:center;justify-content:center;line-height:0}
.icon{display:inline-grid;place-items:center;padding:0;transition:.15s}.icon:hover{background:var(--sf2)}
.logo{color:var(--on)}.ck i svg{display:block}.feat .ic{color:var(--g)}
.float{display:flex;align-items:center;gap:8px}
.toolbar{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:14px}.toolbar .filters{margin:0}
.fgrid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.fgrid .full{grid-column:1/-1}
.formrow.up{grid-template-columns:1fr 1fr 1fr auto}
.modeSeg{display:inline-flex}.nav-links .modeSeg{display:none}
dialog{overflow:auto}
a:focus-visible,button:focus-visible,.rw:focus-visible{outline:2px solid var(--g2);outline-offset:2px}
@keyframes spin{to{transform:rotate(360deg)}}
#ldSteps{list-style:none;margin:0 0 12px;padding:0}#ldSteps li{padding:5px 0;color:var(--mu)}
#ldSteps li:before{content:'';display:inline-block;width:11px;height:11px;border-radius:50%;border:2px solid var(--ln);margin-right:10px;vertical-align:middle}
#ldSteps li.run{color:var(--tx);font-weight:600}#ldSteps li.run:before{border-color:var(--g2);border-top-color:transparent;animation:spin .8s linear infinite}
#ldSteps li.ok:before{background:var(--g);border-color:var(--g)}#ldSteps li.warn:before{background:var(--wr);border-color:var(--wr)}#ldSteps li.fail:before{background:var(--bd);border-color:var(--bd)}
@media(max-width:760px){
 .nav-in{padding:0 12px;position:relative}.tools .modeSeg,.tools .mode-label{display:none}
 .nav-links{position:absolute;top:calc(var(--nav) - 1px);left:12px;right:12px;margin:0;padding:10px;display:none;flex-direction:column;align-items:stretch;gap:2px;background:var(--glass);border:1px solid var(--ln);border-radius:0 0 14px 14px;box-shadow:var(--sh);backdrop-filter:blur(14px)}
 #links.open{display:flex}.nav-links a{width:100%}
 .nav-links .modeSeg{display:inline-flex;align-self:flex-start;margin:6px 0 0}
 .tabs{flex-wrap:nowrap;overflow-x:auto;padding-bottom:4px;-webkit-overflow-scrolling:touch}.tabs button{white-space:nowrap}
 .rt thead{display:none}.rt,.rt tbody{display:block}
 .rt tr.r{display:grid;grid-template-columns:1fr 1fr;gap:8px 12px;padding:12px;border-bottom:1px solid var(--ln)}
 .rt td{border:0;padding:0;display:block;min-width:0}
 .rt td[data-l]:before{content:attr(data-l);display:block;font-size:11.5px;color:var(--mu)}
 .rt tr.det{display:block;padding:0 12px 12px;border-bottom:1px solid var(--ln)}.rt tr.det td{display:block}
 .rt td select{width:100%}
 .board{grid-auto-columns:minmax(80vw,1fr);scroll-snap-type:x mandatory}.col{scroll-snap-align:start}
 .formrow,.formrow.up,.fgrid{grid-template-columns:1fr}
 .phead h1{font-size:24px}.card{padding:16px}.toolbar .btn{width:100%}
}
@media(max-width:560px){
 dialog{width:100%;max-width:100%;margin:auto 0 0;border-radius:22px 22px 0 0;max-height:92vh}
 .container{padding:0 14px}.hero{padding:34px 0 24px}.brand span:last-child{font-size:16px}.logo{width:136px;height:46px}
}
@media(min-width:1400px){.container{max-width:1280px}}
/* primary navigation layout */
:root{--side-nav:240px}
.nav{position:sticky}
.nav-in{max-width:none}
.nav-links{position:fixed;z-index:1990;top:var(--nav);left:0;bottom:0;width:var(--side-nav);margin:0;padding:18px 12px;display:flex;flex-direction:column;align-items:stretch;gap:4px;background:var(--glass);border-right:1px solid var(--ln);box-shadow:var(--sh);overflow-y:auto;transition:transform .25s}
.nav-links a{display:flex;align-items:center;min-height:42px;padding:9px 13px}
.nav-links .modeSeg{display:none}
.nav-links #roleSeg{display:none}
.side-nav-collapsed .nav-links{transform:translateX(-100%)}
main{margin-left:var(--side-nav);transition:margin-left .25s}
.side-nav-collapsed main{margin-left:0}
.avatar{width:34px;height:34px;border-radius:50%;display:grid;place-items:center;background:var(--gt);color:var(--g);font-size:13px;font-weight:700}
.tools{margin-left:auto;min-width:0}
.tools .seg{flex-shrink:0}
.tools button{white-space:nowrap}
#roleSeg{margin-left:0}
.burger{display:inline-flex}
#links:not(.open){display:flex}
#links.open{display:flex}
#v-map{left:var(--side-nav);transition:left .25s}
.side-nav-collapsed #v-map{left:0}
@media(max-width:760px){
 .nav-in{padding:0 12px}
 .nav-links{top:var(--nav);left:0;right:auto;width:min(280px,86vw);padding:14px 12px;border-radius:0;border-right:1px solid var(--ln)}
 .side-nav-collapsed .nav-links{transform:translateX(-100%)}
 main{margin-left:0}
 #v-map{left:0}
 .brand span:last-child{font-size:17px}
}
@media(max-width:1100px) and (min-width:761px){
 .brand span:last-child,.mode-label{display:none}
 .nav-in{gap:8px}
}
</style>
<!--HARDEN-->
</head>
<body>
<header class="nav"><div class="nav-in">
  <a class="brand" href="#/" aria-label="AmarBhumi"><img class="logo logo-light" src="/assets/logo.png" alt="AmarBhumi আমার ভূমি"><img class="logo logo-dark" src="/assets/logo_dark.png" alt="AmarBhumi আমার ভূমি"></a>
  <div class="tools">
    <span class="avatar" aria-hidden="true">U</span>
    <div class="seg" id="roleSeg"><button data-role="user" data-i18n="role_user"></button><button data-role="admin" data-i18n="role_admin"></button></div>
    <span class="mode-label" data-i18n="mode_label"></span><div class="seg modeSeg"><button data-mode="dataset" data-i18n="mode_dataset"></button><button data-mode="api" data-i18n="mode_api"></button></div>
    <div class="seg" id="langSeg"><button data-lang="bn">বাং</button><button data-lang="en">EN</button></div>
    <button class="icon" id="themeBtn" title="Theme" aria-label="Theme"><i data-ic="contrast" data-s="18"></i></button>
    <button class="icon burger" id="burger" aria-label="Menu" aria-expanded="true"><i data-ic="menu" data-s="18"></i></button>
  </div>
</div></header>

<nav class="nav-links open" id="links" aria-label="Primary navigation">
    <a href="#/" data-r="home" data-i18n="nav_home"></a>
    <a href="#/map" data-r="map" data-i18n="nav_map"></a>
    <a href="#/verify" data-r="verify" data-i18n="nav_verify"></a>
    <a href="#/track" data-r="track" data-i18n="nav_track"></a>
    <a href="#/admin" data-r="admin" id="navAdmin" data-i18n="nav_admin"></a>
</nav>

<main>
<section class="view" id="v-home">
  <div class="hero"><div class="container hero-in">
    <div>
      <span class="pill" data-i18n="hero_badge"></span>
      <h1 data-i18n="hero_h"></h1><p data-i18n="hero_p"></p>
      <div class="card searchcard">
        <div id="datasetSearch">
          <div class="row3">
          <label class="f"><span data-i18n="f_district"></span><select id="h_dist"></select></label>
          <label class="f"><span data-i18n="f_sheet"></span><select id="h_sheet"></select></label>
          <label class="f"><span data-i18n="f_plot"></span><input id="h_plot" list="h_plots"></label>
          </div><datalist id="h_plots"></datalist>
        </div>
        <div id="apiSearch" hidden>
          <div class="row3 api-fields">
            <label class="f"><span data-i18n="f_division"></span><select id="h_division"></select></label>
            <label class="f"><span data-i18n="f_district"></span><select id="h_adist"></select></label>
            <label class="f"><span data-i18n="f_thana"></span><select id="h_thana"></select></label>
          </div>
          <div class="row2 api-fields">
            <label class="f"><span data-i18n="f_mouza"></span><select id="h_mouza"></select></label>
            <label class="f"><span data-i18n="f_plot"></span><input id="h_aplot"></label>
          </div>
        </div>
        <div class="btnrow"><button class="btn" id="h_map" data-i18n="btn_map"></button><button class="btn alt" id="h_ver" data-i18n="btn_verify"></button></div>
        <div class="or"><span data-i18n="or_place"></span></div>
        <div class="placebox"><input id="h_place" data-ph="place_ph"><button class="btn" id="h_go" aria-label="Search"><i data-ic="pin" data-s="18"></i></button></div>
      </div>
    </div>
  </div></div>
  <div class="container stats home-reveal" id="h_stats"></div>
  <div class="sec home-reveal"><div class="container"><h2 data-i18n="feat_h"></h2><p data-i18n="feat_p"></p>
    <div class="grid4">
      <div class="card feat" data-go="#/map"><div class="ic"><i data-ic="map" data-s="24"></i></div><h3 data-i18n="feat_map_t"></h3><p data-i18n="feat_map_d"></p></div>
      <div class="card feat" data-go="#/verify"><div class="ic"><i data-ic="search" data-s="24"></i></div><h3 data-i18n="feat_ver_t"></h3><p data-i18n="feat_ver_d"></p></div>
      <div class="card feat" data-go="#/track"><div class="ic"><i data-ic="file" data-s="24"></i></div><h3 data-i18n="feat_mut_t"></h3><p data-i18n="feat_mut_d"></p></div>
      <div class="card feat" data-go="#/admin"><div class="ic"><i data-ic="shield" data-s="24"></i></div><h3 data-i18n="feat_fraud_t"></h3><p data-i18n="feat_fraud_d"></p></div>
    </div></div></div>
  <div class="container home-reveal"><div class="band"><h2 data-i18n="fraud_h"></h2><div class="grid4">
    <div><h4 data-i18n="ft_fake_deed"></h4><p data-i18n="ftd_fake_deed"></p></div>
    <div><h4 data-i18n="ft_double_sale"></h4><p data-i18n="ftd_double_sale"></p></div>
    <div><h4 data-i18n="ft_rapid"></h4><p data-i18n="ftd_rapid"></p></div>
    <div><h4 data-i18n="ft_mismatch"></h4><p data-i18n="ftd_mismatch"></p></div></div></div></div>
  <div class="sec home-reveal"><div class="container"><h2 data-i18n="steps_h"></h2><p></p><div class="grid3">
    <div class="step"><span class="n">১</span><div><h4 data-i18n="st1_t"></h4><p data-i18n="st1_d"></p></div></div>
    <div class="step"><span class="n">২</span><div><h4 data-i18n="st2_t"></h4><p data-i18n="st2_d"></p></div></div>
    <div class="step"><span class="n">৩</span><div><h4 data-i18n="st3_t"></h4><p data-i18n="st3_d"></p></div></div></div>
    <p style="margin-top:26px"><span class="note" data-i18n="demo_note"></span></p></div></div>
  <footer><div class="container"><span data-i18n="brand"></span> · <span data-i18n="tagline"></span></div></footer>
</section>

<section class="view" id="v-map" hidden>
  <div id="map"></div>
  <aside id="panel">
    <div class="ph"><b data-i18n="nav_map"></b><small id="sub"></small></div>
    <div class="pb">
      <h4 data-i18n="m_nav"></h4>
      <div id="m_dsNav"><div class="row2"><select id="m_dist"></select><select id="m_sheet"></select></div></div>
      <div id="m_apiNav" hidden>
        <select id="m_division"></select><select id="m_adist" style="margin-top:6px"></select>
        <select id="m_thana" style="margin-top:6px"></select><select id="m_mouza" style="margin-top:6px"></select>
        <button class="btn sm" id="m_apiload" style="margin:8px 0;width:100%" data-i18n="api_load"></button>
        <h4 data-i18n="api_saved"></h4><div id="m_saved" class="plist"></div>
        <div id="m_fb" class="note" hidden data-i18n="api_fb"></div>
      </div>
      <div class="btnrow" style="margin-top:8px"><button class="btn sm alt" id="m_all" data-i18n="m_overview"></button><button class="btn sm alt" id="fit" data-i18n="m_fit"></button></div>
      <h4 data-i18n="m_search"></h4>
      <input id="q" type="search" data-ph="m_search_ph"><input id="place" type="search" data-ph="m_place_ph">
      <div id="placeRes" class="plist"></div>
      <div class="stats4" id="mstats"></div>
      <h4 data-i18n="m_plots"></h4><div class="plist" id="plist"></div>
      <h4 data-i18n="m_classes"></h4><div id="legend"></div>
      <h4 data-i18n="m_layers"></h4>
      <label class="chk"><input type="checkbox" id="lyLand" checked><span data-i18n="m_ly_land"></span></label>
      <label class="chk"><input type="checkbox" id="lyPlot" checked><span data-i18n="m_ly_plot"></span></label>
      <label class="chk"><input type="checkbox" id="lbl" checked><span data-i18n="m_labels"></span></label>
      <label class="chk"><input type="checkbox" id="autoload" checked><span data-i18n="m_autoload"></span></label>
      <label class="chk"><input type="checkbox" id="auto" checked><span data-i18n="m_auto"></span></label>
      <h4 data-i18n="m_opacity"></h4><input id="op" type="range" min="0" max="1" step="0.05" value="0.55">
      <h4 data-i18n="m_base"></h4><div class="chips" id="bases"></div>
    </div>
  </aside>
  <button class="icon" id="ptoggle" aria-label="Panel"><i data-ic="layers" data-s="18"></i></button>
  <div class="fabs"><button class="icon" id="locate" title="My location" aria-label="My location"><i data-ic="locate" data-s="18"></i></button></div>
  <div id="drawer" hidden></div>
</section>

<section class="view" id="v-verify" hidden><div class="container">
  <div class="phead"><h1 data-i18n="v_title"></h1><p data-i18n="v_sub"></p></div>
  <div class="card"><div class="formrow">
    <label class="f"><span data-i18n="f_district"></span><select id="v_dist"></select></label>
    <label class="f"><span data-i18n="f_sheet"></span><select id="v_sheet"></select></label>
    <label class="f"><span data-i18n="f_plot"></span><input id="v_plot" list="v_plots"></label>
    <button class="btn" id="v_go" data-i18n="v_btn"></button></div><datalist id="v_plots"></datalist></div>
  <div class="card" id="v_apiBox" hidden style="margin-top:12px"></div>
  <div id="v_out"></div><p style="margin:26px 0"><span class="note" data-i18n="demo_note"></span></p>
</div></section>

<section class="view" id="v-track" hidden><div class="container">
  <div class="phead"><h1 data-i18n="tr_title"></h1><p data-i18n="tr_sub"></p></div>
  <div class="card"><div class="placebox"><input id="tr_in" data-ph="tr_ph"><button class="btn" id="tr_go" data-i18n="tr_btn"></button></div>
    <div class="chips" id="tr_samples" style="margin-top:12px"></div></div>
  <div id="tr_out" style="margin-top:16px"></div><p style="margin:26px 0"><span class="note" data-i18n="demo_note"></span></p>
</div></section>

<section class="view" id="v-admin" hidden><div class="container" id="a_root"></div></section>
</main>
<div id="loader" hidden><div class="card"><h3 id="ldTitle"></h3><div class="pbar"><i id="ldBar"></i></div><ul id="ldSteps"></ul><button class="btn sm alt" id="ldClose" hidden data-i18n="close"></button></div></div>
<div id="toast"></div><dialog id="dlg"></dialog>

<script>
const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
const BN='০১২৩৪৫৬৭৮৯';
const IC={
leaf:'<path d="M11 20A7 7 0 0 1 9.8 6.1C15.5 5 17 4.48 19 2c1 2 2 4.18 2 8 0 5.5-4.78 10-10 10Z"/><path d="M2 21c0-3 1.85-5.36 5.08-6C9.5 14.52 12 13 13 12"/>',
map:'<polygon points="3 6 9 3 15 6 21 3 21 18 15 21 9 18 3 21"/><line x1="9" x2="9" y1="3" y2="18"/><line x1="15" x2="15" y1="6" y2="21"/>',
search:'<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
shield:'<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="m9 12 2 2 4-4"/>',
file:'<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M10 9H8"/><path d="M16 13H8"/><path d="M16 17H8"/>',
pin:'<path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z"/><circle cx="12" cy="10" r="3"/>',
check:'<path d="M20 6 9 17l-5-5"/>',
okc:'<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><path d="m9 11 3 3L22 4"/>',
alert:'<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
x:'<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
printer:'<path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><path d="M6 9V3h12v6"/><rect x="6" y="14" width="12" height="8" rx="1"/>',
link:'<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
globe:'<circle cx="12" cy="12" r="10"/><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/><path d="M2 12h20"/>',
lock:'<rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
menu:'<path d="M4 12h16"/><path d="M4 6h16"/><path d="M4 18h16"/>',
contrast:'<circle cx="12" cy="12" r="10"/><path d="M12 18a6 6 0 0 0 0-12v12z"/>',
play:'<polygon points="6 3 20 12 6 21 6 3"/>',
pause:'<rect x="14" y="4" width="4" height="16" rx="1"/><rect x="6" y="4" width="4" height="16" rx="1"/>',
trash:'<path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/>',
plus:'<path d="M5 12h14"/><path d="M12 5v14"/>',
locate:'<line x1="2" x2="5" y1="12" y2="12"/><line x1="19" x2="22" y1="12" y2="12"/><line x1="12" x2="12" y1="2" y2="5"/><line x1="12" x2="12" y1="19" y2="22"/><circle cx="12" cy="12" r="7"/><circle cx="12" cy="12" r="3"/>',
layers:'<path d="m12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83Z"/><path d="m22 17.65-9.17 4.16a2 2 0 0 1-1.66 0L2 17.65"/><path d="m22 12.65-9.17 4.16a2 2 0 0 1-1.66 0L2 12.65"/>',
clipboard:'<rect width="8" height="4" x="8" y="2" rx="1" ry="1"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><path d="M12 11h4"/><path d="M12 16h4"/><path d="M8 11h.01"/><path d="M8 16h.01"/>',
clock:'<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
ban:'<circle cx="12" cy="12" r="10"/><path d="m4.9 4.9 14.2 14.2"/>'};
const ico=(n,s=18)=>`<svg class="ico" width="${s}" height="${s}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${IC[n]||''}</svg>`;
let lang=localStorage.lang||'bn',role=localStorage.role||'user',dataMode=localStorage.dataMode==='dataset'?'dataset':'api';
/* ---------- translations [bn, en] ---------- */
const T={
brand:["আমার ভূমি","AmarBhumi"],tagline:["বাংলাদেশ ভূমি তথ্য ও যাচাই","Bangladesh land info & verification"],
mode_label:["ডেটা উৎস","Data source"],mode_dataset:["ডেমো ডেটা","Dataset"],mode_api:["সরকারি API","Government API"],
nav_home:["হোম","Home"],nav_map:["ভূমি মানচিত্র","Land map"],nav_verify:["কেনার আগে যাচাই","Pre-purchase check"],nav_track:["নামজারি ট্র্যাকিং","Mutation tracking"],nav_admin:["অ্যাডমিন টুলস","Admin tools"],
role_user:["ব্যবহারকারী","User"],role_admin:["অ্যাডমিন","Admin"],
hero_badge:["ডিজিটাল ভূমি সেবা • বাংলাদেশ","Digital Land Services • Bangladesh"],
hero_h:["আপনার জমি খুঁজুন, মানচিত্রে দেখুন, নিশ্চিন্তে কিনুন","Find your land, see it on the map, buy with confidence"],
hero_p:["জেলা ও শিট বেছে দাগ নম্বর দিন — মালিকানা, নামজারির ইতিহাস, মামলা ও খাস জমির তথ্য এক জায়গায়।","Pick a district & sheet, enter the plot number — ownership, mutation history, cases and khas status in one place."],
f_district:["জেলা","District"],f_sheet:["শিট","Sheet"],f_plot:["দাগ নম্বর","Plot no."],sel_district:["জেলা নির্বাচন করুন","Select district"],sel_sheet:["শিট নির্বাচন করুন","Select sheet"],all_sheets:["সব শিট","All sheets"],
f_division:["বিভাগ","Division"],f_thana:["থানা / উপজেলা","Thana / Upazila"],f_mouza:["মৌজা","Mouza"],sel_division:["বিভাগ নির্বাচন করুন","Select division"],sel_thana:["থানা নির্বাচন করুন","Select thana"],sel_mouza:["মৌজা নির্বাচন করুন","Select mouza"],
btn_map:["মানচিত্রে দেখুন","View on map"],btn_verify:["কেনার আগে যাচাই","Verify before buying"],or_place:["অথবা এলাকা খুঁজুন","or search an area"],place_ph:["গ্রাম, ইউনিয়ন বা স্থানের নাম…","Village, union or place name…"],
need_dist:["আগে জেলা নির্বাচন করুন","Select a district first"],need_plot:["শিট ও দাগ নম্বর দিন","Select sheet and enter plot no."],need_api_area:["বিভাগ, জেলা, থানা ও মৌজা নির্বাচন করুন","Select division, district, thana and mouza"],
mock_safe:["মালিকানা যাচাইকৃত","Ownership verified"],mock_alert:["জাল দলিল সন্দেহ","Forged deed suspected"],
stat_ds:["ডেটাসেট (শিট)","Datasets (sheets)"],stat_plots:["দাগ / প্লট","Plots"],stat_checks:["স্বয়ংক্রিয় যাচাই","Automated checks"],stat_alerts:["জালিয়াতি সতর্কতা","Fraud alerts"],stat_cases:["নামজারি কেস","Mutation cases"],
feat_h:["এক প্ল্যাটফর্মে সব ভূমি সেবা","All land services on one platform"],feat_p:["নাগরিক ও কর্মকর্তা — উভয়ের জন্য স্বচ্ছ, দ্রুত ও নিরাপদ।","Transparent, fast and safe — for citizens and officers alike."],
feat_map_t:["ইন্টারঅ্যাকটিভ ভূমি মানচিত্র","Interactive land map"],feat_map_d:["স্যাটেলাইট মানচিত্রে দাগ, ভূমি ব্যবহার ও আয়তন দেখুন।","See plots, land use and area on satellite maps."],
feat_ver_t:["নাগরিক প্রি-পারচেজ যাচাই","Citizen pre-purchase check"],feat_ver_d:["মালিকানা, নামজারি, মামলা ও খাস/অধিগ্রহণ অবস্থা যাচাই করুন।","Verify ownership, mutation, cases and khas/acquisition status."],
feat_mut_t:["স্মার্ট ই-নামজারি","Smart e-Mutation"],feat_mut_d:["দলিল নিবন্ধন থেকে খতিয়ান পর্যন্ত কেসের অবস্থা অনুসরণ করুন।","Track a case from deed registration to khatian."],
feat_fraud_t:["জালিয়াতি শনাক্তকরণ","Fraud detection"],feat_fraud_d:["জাল দলিল, দ্বৈত বিক্রয় ও সন্দেহজনক হস্তান্তর ধরুন (অ্যাডমিন)।","Catch forged deeds, double sales and suspicious transfers (admin)."],
fraud_h:["জালিয়াতি শনাক্তকরণ ইঞ্জিন","Fraud detection engine"],
ft_fake_deed:["জাল দলিল","Forged deed"],ft_double_sale:["একই জমি একাধিক ক্রেতার কাছে বিক্রি","Same land sold to multiple buyers"],ft_rapid:["সন্দেহজনক দ্রুত হস্তান্তর","Suspicious rapid transfer"],ft_mismatch:["নিবন্ধন ও নামজারি রেকর্ডের অমিল","Registry vs mutation mismatch"],
ftd_fake_deed:["সিরিয়াল, সিল ও স্বাক্ষর নিবন্ধন সার্ভারের সাথে মিলিয়ে দেখা হয়।","Serial, seal and signature are cross-checked with the registry server."],
ftd_double_sale:["একই দাগে অল্প সময়ে একাধিক বিক্রয় দলিল ধরা পড়ে।","Multiple sale deeds on one plot in a short time are flagged."],
ftd_rapid:["অল্প সময়ে বারবার মালিকানা বদল হলে সতর্কতা আসে।","Frequent ownership changes in a short period raise an alert."],
ftd_mismatch:["দলিল, খতিয়ান ও নামজারির পরিমাণ/নম্বর অমিল শনাক্ত হয়।","Mismatch of area/numbers across deed, khatian and mutation is detected."],
steps_h:["কীভাবে কাজ করে","How it works"],st1_t:["জমি খুঁজুন","Find the land"],st1_d:["জেলা, শিট ও দাগ নম্বর বা স্থানের নাম দিন।","Enter district, sheet & plot no., or a place name."],
st2_t:["মানচিত্রে দেখুন","See it on the map"],st2_d:["সীমানা, ভূমি ব্যবহার ও আয়তন (শতাংশ/একর) দেখুন।","See boundary, land use and area (decimal/acre)."],
st3_t:["যাচাই করে কিনুন","Verify, then buy"],st3_d:["ঝুঁকি স্কোর ও পরামর্শসহ রিপোর্ট নিন।","Get a report with risk score and advice."],
demo_note:["ডেমো তথ্য — এই MVP-তে দেখানো মালিকানা, মামলা ও জালিয়াতির সব তথ্য কাল্পনিক।","Demo data — all ownership, case and fraud data in this MVP is fictional."],
m_nav:["নেভিগেশন","Navigate"],m_overview:["সব এলাকা","All areas"],m_fit:["ডেটায় ফোকাস","Fit to data"],m_search:["অনুসন্ধান","Search"],m_search_ph:["দাগ নম্বর / মৌজা / মালিকের নাম","Plot no. / mouza / owner"],m_place_ph:["স্থান খুঁজুন (OpenStreetMap)","Find a place (OpenStreetMap)"],
m_plots:["দাগ তালিকা","Plots"],m_classes:["ভূমির ধরন","Land-use classes"],m_layers:["লেয়ার","Layers"],m_ly_land:["ভূমি ব্যবহার","Land use"],m_ly_plot:["দাগের সীমানা","Plot boundaries"],m_labels:["দাগ নম্বর লেবেল","Plot labels"],
m_autoload:["জুম করলে স্বয়ংক্রিয় লোড","Auto-load on zoom"],m_auto:["ফাইল বদলালে অটো-রিফ্রেশ","Auto-refresh on file change"],m_opacity:["স্বচ্ছতা","Opacity"],m_base:["বেস ম্যাপ","Base map"],
m_total:["মোট দাগ","Plots"],m_parts:["ভূমি অংশ","Land parts"],m_area:["মোট আয়তন (শতাংশ)","Total (decimal)"],m_cls:["ধরন","Classes"],
m_click_ds:["লোড করতে ক্লিক করুন","Click to load"],m_none:["কোনো ডেটা নেই","No data"],m_refreshed:["ডেটা হালনাগাদ হয়েছে","Data refreshed"],m_loaded:["লোড হয়েছে","loaded"],m_nodata:["কিছু পাওয়া যায়নি","Nothing found"],
m_locating:["অবস্থান খোঁজা হচ্ছে…","Locating…"],m_here:["আপনার অবস্থান","You are here"],m_place_hit:["এলাকায় ডেটাসেট পাওয়া গেছে — লোড হচ্ছে","Dataset found nearby — loading"],
bm_sat:["স্যাটেলাইট","Satellite"],bm_satl:["স্যাটেলাইট + লেবেল","Satellite + labels"],bm_osm:["ওএসএম","OSM"],bm_topo:["ভূসংস্থান","Topo"],bm_light:["হালকা","Light"],bm_dark:["গাঢ়","Dark"],
c_Settlement:["বসতবাড়ি","Settlement"],c_Agriculture:["কৃষি","Agriculture"],c_Agricultural_Land:["কৃষি","Agricultural"],c_Commercial_Area:["বাণিজ্যিক","Commercial"],c_Water_Body:["জলাশয়","Water body"],c_Waterbody:["জলাশয়","Water body"],c_Educational_Institution:["শিক্ষাপ্রতিষ্ঠান","Educational institution"],c_Recreational_Place:["বিনোদনমূলক স্থান","Recreational space"],c_Road:["সড়ক","Road"],c_Religious_Place:["ধর্মীয় স্থান","Religious Place"],c_Industrial_Area:["শিল্প এলাকা","Industrial"],c_Forest:["বন","Forest"],c_Administrative_Area:["প্রশাসনিক এলাকা","Administrative Area"],c_Vacant_Land:["পতিত জমি","Vacant land"],c_Miscellaneous_Landuse:["বিবিধ","Miscellaneous land"],
s_Homestead:["বাড়ি","Homestead"],s_Paddy_Field:["ধানি জমি","Paddy field"],s_Shop:["দোকান","Shop"],s_Pond:["পুকুর","Pond"],s_Fallow:["পতিত","Fallow"],s_Garment_Factory:["পোশাক কারখানা","Garment factory"],
u_dec:["শতাংশ","decimal"],u_acre:["একর","acre"],u_sqm:["বর্গমিটার","sq.m"],
d_owner:["মালিক","Owner"],d_risk:["ঝুঁকি","Risk"],d_report:["পূর্ণ যাচাই রিপোর্ট","Full verification report"],d_share:["লিংক কপি","Copy link"],d_gmaps:["গুগল ম্যাপে খুলুন","Open in Google Maps"],d_copied:["লিংক কপি হয়েছে","Link copied"],d_addr:["ঠিকানা","Address"],
loading:["লোড হচ্ছে…","Loading…"],close:["বন্ধ","Close"],open_map:["মানচিত্রে দেখুন","View on map"],err:["ত্রুটি","Error"],days:["দিন","days"],
v_title:["কেনার আগে যাচাই করুন","Check before you buy"],v_sub:["জমি কেনার আগে মালিকানা, নামজারি ইতিহাস, চলমান মামলা এবং খাস/অধিগ্রহণ অবস্থা জেনে নিন।","Know the ownership, mutation history, pending cases and khas/acquisition status before you buy."],
v_btn:["যাচাই করুন","Verify"],v_plot:["দাগ","Plot"],v_score:["ঝুঁকি","risk"],v_print:["প্রিন্ট","Print"],v_status:["যাচাই ফলাফল","Verification results"],v_owner_h:["মালিকানা ও রেকর্ড","Ownership & record"],
v_owner:["বর্তমান মালিক","Current owner"],v_father:["পিতা/স্বামী","Father/Spouse"],v_nid:["এনআইডি","NID"],v_khatian:["খতিয়ান নং","Khatian no."],v_dag:["দাগ নং","Dag no."],v_mouza:["মৌজা","Mouza"],v_share:["অংশ","Share"],v_area:["আয়তন","Area"],
v_location:["অবস্থান ও ভূমি ব্যবহার","Location & land use"],v_flags:["জালিয়াতি সতর্কতা","Fraud flags"],v_noflags:["কোনো জালিয়াতির সংকেত নেই","No fraud signals"],v_pending:["মামলা, দায় ও বিশেষ অবস্থা","Cases, liabilities & special status"],
v_nopending:["কোনো চলমান মামলা বা বন্ধক নেই","No pending case or mortgage"],v_hist:["নামজারি ও মালিকানা ইতিহাস","Mutation & ownership history"],v_advice:["পরামর্শ","Advice"],v_since:["থেকে","since"],v_deed:["দলিল","Deed"],v_case:["নামজারি কেস","Mutation case"],
v_verdict_safe:["তুলনামূলক নিরাপদ","Relatively safe"],v_verdict_caution:["সতর্কতার সাথে এগোন","Proceed with caution"],v_verdict_danger:["উচ্চ ঝুঁকি — কিনবেন না","High risk — do not buy"],v_notfound:["দাগ পাওয়া যায়নি","Plot not found"],
ck_ownership_verified:["মালিকানা যাচাই","Ownership verified"],ck_mutation_current:["নামজারি হালনাগাদ","Mutation up to date"],ck_pending_cases:["মামলা/বন্ধক নেই","No cases / mortgage"],ck_khas_vested:["খাস/অর্পিত নয়","Not khas / vested"],ck_acquisition:["অধিগ্রহণ নেই","No acquisition"],ck_fraud_scan:["জালিয়াতি স্ক্যান","Fraud scan"],
wk_deed_verified:["দলিল যাচাই","Deed verified"],wk_no_double_sale:["দ্বৈত বিক্রয় নেই","No double sale"],wk_area_match:["পরিমাণ মিল","Area match"],wk_no_pending_case:["মামলা নেই","No pending case"],wk_khas_check:["খাস/অধিগ্রহণ যাচাই","Khas / acquisition"],wk_velocity:["হস্তান্তরের গতি","Transfer velocity"],wk_fee_paid:["ফি পরিশোধ","Fees paid"],
lv_pass:["উত্তীর্ণ","Pass"],lv_warn:["সতর্ক","Warn"],lv_fail:["ব্যর্থ","Fail"],lv_queued:["অপেক্ষমাণ","Queued"],
h_sale:["বিক্রয়","Sale"],h_inherit:["উত্তরাধিকার","Inheritance"],h_gift:["হেবা/দান","Gift"],h_partition:["বণ্টন","Partition"],
p_civil_suit:["দেওয়ানি মামলা","Civil suit"],p_injunction:["নিষেধাজ্ঞা","Injunction"],p_mortgage:["ব্যাংক বন্ধক","Bank mortgage"],p_mutation_appeal:["নামজারি আপিল","Mutation appeal"],
fo_civil_court:["দেওয়ানি আদালত","Civil court"],fo_ac_land:["এসি (ল্যান্ড) অফিস","AC (Land) office"],fo_bank:["ব্যাংক","Bank"],
kh_none:["খাস/অর্পিত নয়","Not khas / vested"],kh_khas:["সরকারি খাস জমি","Government khas land"],kh_vested:["অর্পিত সম্পত্তি তালিকাভুক্ত","Listed as vested property"],
aq_none:["অধিগ্রহণ প্রস্তাব নেই","No acquisition proposal"],aq_proposed:["অধিগ্রহণ প্রস্তাবিত","Acquisition proposed"],aq_notified:["অধিগ্রহণ নোটিশ জারি","Acquisition notice issued"],
sev_high:["উচ্চ","High"],sev_medium:["মধ্যম","Medium"],
st_new:["নতুন","New"],st_investigating:["তদন্তাধীন","Investigating"],st_resolved:["নিষ্পন্ন","Resolved"],st_false_positive:["ভুল সতর্কতা","False positive"],
sg_registration:["দলিল নিবন্ধন","Deed registered"],sg_auto_check:["স্বয়ংক্রিয় যাচাই","Auto checks"],sg_union_office:["ইউনিয়ন ভূমি অফিস","Union land office"],sg_ac_land:["এসি (ল্যান্ড) শুনানি","AC (Land) hearing"],sg_approved:["নামজারি অনুমোদন","Mutation approved"],sg_khatian:["খতিয়ান ও ডিসিআর প্রদান","Khatian & DCR issued"],
ev_hold:["ম্যানুয়ালি আটকানো হয়েছে","Put on hold manually"],ev_release:["অ্যাডমিন ছাড় দিয়েছে","Released by admin"],ev_reject:["প্রত্যাখ্যান করা হয়েছে","Rejected"],
cs_ok:["চলমান","On track"],cs_flagged:["আটকানো (সতর্কতা)","On hold (flagged)"],cs_delayed:["বিলম্বিত","Delayed"],cs_completed:["সম্পন্ন","Completed"],cs_rejected:["প্রত্যাখ্যাত","Rejected"],
tr_title:["নামজারি কেস ট্র্যাকিং","Mutation case tracking"],tr_sub:["দলিল নিবন্ধন থেকে খতিয়ান প্রাপ্তি পর্যন্ত আপনার কেসের বর্তমান অবস্থা দেখুন।","Follow your case from deed registration to khatian."],
tr_ph:["কেস নম্বর (যেমন MUT-2026-12345)","Case no. (e.g. MUT-2026-12345)"],tr_btn:["অনুসন্ধান","Track"],tr_samples:["নমুনা কেস","Sample cases"],tr_notfound:["এই নম্বরে কোনো কেস পাওয়া যায়নি","No case found for this number"],
tr_buyer:["ক্রেতা/আবেদনকারী","Buyer / applicant"],tr_seller:["বিক্রেতা","Seller"],tr_deed:["দলিল নং","Deed no."],tr_office:["সাব-রেজিস্ট্রি অফিস","Sub-registry office"],tr_sla:["নির্ধারিত সময়সীমা","Service deadline"],tr_left:["দিন বাকি","days left"],tr_over:["দিন অতিক্রান্ত","days overdue"],tr_timeline:["ঘটনাপ্রবাহ","Timeline"],tr_checks:["স্বয়ংক্রিয় যাচাই","Automated checks"],tr_plot:["দাগ","Plot"],
a_title:["অ্যাডমিন টুলস","Admin tools"],a_note:["MVP: লগইন নেই — অ্যাডমিন টুল সবার জন্য উন্মুক্ত (শুধু ডেমো)","MVP: no login — admin tools are open (demo only)"],
a_overview:["সারসংক্ষেপ","Overview"],a_fraud:["জালিয়াতি সতর্কতা","Fraud alerts"],a_workflow:["ই-নামজারি ওয়ার্কফ্লো","e-Mutation workflow"],a_data:["ডেটা ম্যানেজার","Data manager"],
a_open:["খোলা সতর্কতা","Open alerts"],a_high:["উচ্চ ঝুঁকি","High severity"],a_prog:["চলমান কেস","Cases in progress"],a_flag:["আটকানো কেস","Flagged cases"],a_avg:["গড় সময় (দিন)","Avg. days"],
a_bytype:["ধরন অনুযায়ী সতর্কতা","Alerts by type"],a_funnel:["কেসের ধাপ","Case stages"],a_recent:["সাম্প্রতিক সতর্কতা","Recent alerts"],a_all:["সব","All"],
th_id:["আইডি","ID"],th_type:["ধরন","Type"],th_sev:["তীব্রতা","Severity"],th_plot:["দাগ","Plot"],th_conf:["আস্থা","Confidence"],th_date:["তারিখ","Detected"],th_status:["অবস্থা","Status"],th_ds:["ডেটাসেট","Dataset"],th_plots:["দাগ","Plots"],th_parts:["অংশ","Parts"],th_area:["আয়তন (শতাংশ)","Area (decimal)"],
a_evidence:["প্রমাণ / সংকেত","Evidence / signals"],a_advance:["পরবর্তী ধাপে","Advance"],a_hold:["আটকান","Hold"],a_release:["ছাড় দিন","Release"],a_reject:["প্রত্যাখ্যান","Reject"],a_updated:["হালনাগাদ হয়েছে","Updated"],
a_upload_h:["নতুন ডেটাসেট আপলোড","Upload dataset"],a_upload_help:["নাম দিন জেলা_শিট আকারে (যেমন gazipur_002), তারপর land ও plot JSON বেছে নিন। ফাইল data/ ফোল্ডারে জমা হবে এবং মানচিত্রে স্বয়ংক্রিয় যোগ হবে।","Name it district_sheet (e.g. gazipur_002), then choose land and plot JSON. Files are saved in data/ and appear on the map automatically."],
a_name:["ডেটাসেটের নাম","Dataset name"],a_land_f:["land.json ফাইল","land.json file"],a_plot_f:["plot.json ফাইল","plot.json file"],a_upload_btn:["আপলোড","Upload"],a_uploaded:["আপলোড সম্পন্ন","Uploaded"],a_pick:["অন্তত একটি ফাইল বেছে নিন","Choose at least one file"],
a_gate_t:["অ্যাডমিন মোড বন্ধ আছে","Admin mode is off"],a_gate_d:["অ্যাডমিন টুল দেখতে ওপরের সুইচ থেকে অ্যাডমিন বেছে নিন। (MVP-তে লগইন নেই)","Switch to Admin using the toggle above to see admin tools. (No login in the MVP)"],a_enable:["অ্যাডমিন মোড চালু করুন","Enable admin mode"],
ld_title:["সরকারি API থেকে এলাকা লোড হচ্ছে","Loading area from government API"],ld_dist:["জেলার সীমানা","District boundary"],ld_thana:["থানার সীমানা","Thana boundary"],ld_plots:["দাগের ডেটা","Plot data"],ld_land:["ভূমি ব্যবহারের ডেটা","Land-use data"],ld_save:["সংরক্ষণ ও সূচক তৈরি","Saving & indexing"],ld_cached:["সংরক্ষিত এলাকা থেকে লোড হয়েছে","Loaded from saved area"],ld_nocode:["এই মৌজার ম্যাপ কোড নেই","This mouza has no map table code"],api_load:["API থেকে লোড করুন","Load from API"],api_saved:["সংরক্ষিত API এলাকা","Saved API areas"],api_fb:["API মোডে এখনো কোনো এলাকা লোড হয়নি — ডেমো ডেটা দেখানো হচ্ছে।","No API area loaded yet — showing demo data."],a_api_h:["সরকারি API থেকে এলাকা যোগ করুন","Add area from government API"],a_reload:["রিফ্রেশ","Refresh"],a_deleted:["মুছে ফেলা হয়েছে","Removed"],a_confirm:["এই সংরক্ষিত এলাকা মুছবেন?","Remove this saved area?"],th_src:["উৎস","Source"],src_api:["API","API"],src_demo:["ডেমো","Demo"],a_new_fraud:["নতুন জালিয়াতি সতর্কতা","New fraud alert"],a_new_case:["নতুন নামজারি কেস","New mutation case"],a_add_alert:["সতর্কতা যোগ","Add alert"],a_add_case:["কেস যোগ","Add case"],a_ds:["ডেটাসেট / মৌজা","Dataset / mouza"],a_ftype:["জালিয়াতির ধরন","Fraud type"],a_sev:["তীব্রতা","Severity"],a_sev_auto:["স্বয়ংক্রিয়","Automatic"],a_conf:["আস্থা (%)","Confidence (%)"],a_note_l:["মন্তব্য (ঐচ্ছিক)","Note (optional)"],a_buyer:["ক্রেতা / আবেদনকারী","Buyer / applicant"],a_seller:["বিক্রেতা","Seller"],a_deed_no:["দলিল নং (ঐচ্ছিক)","Deed no. (optional)"],a_deed_date:["দলিলের তারিখ","Deed date"],a_create:["তৈরি করুন","Create"],a_cancel:["বাতিল","Cancel"],a_created:["সফলভাবে যোগ হয়েছে","Added successfully"],a_delete:["মুছুন","Delete"],a_custom:["ম্যানুয়াল","Manual"],a_confirm_del:["এটি মুছে ফেলবেন?","Delete this?"],a_key:["অ্যাডমিন কী দিন","Enter the admin key"],a_need_plot:["ডেটাসেট ও দাগ নম্বর দিন","Choose a dataset and plot no."],a_plot_hint:["এই ডেটাসেটের দাগ থেকে বেছে নিন","Pick from this dataset's plots"],a_mode_scope:["এটি শুধু বর্তমান ডেটা মোডে যোগ হবে","This is added to the current data mode only"],a_noplots:["এই মোডে কোনো ডেটাসেট নেই","No dataset in this mode"]
};
const t=k=>(T[k]||[k,k])[lang==='bn'?0:1];
const D=v=>String(v??'').replace(/\d/g,d=>lang==='bn'?BN[d]:d);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const X=s=>D(esc(s));
const nf=(n,d=2)=>D(Number(n).toLocaleString('en-IN',{maximumFractionDigits:d}));
const dec=m=>m/40.4686;
const nm=o=>o?(o[lang]||o.bn):'';
const dn=d=>lang==='bn'?d.district_bn:d.district_en;
const fd=s=>{try{return D(new Date(s).toLocaleDateString(lang==='bn'?'bn-BD':'en-GB',{day:'numeric',month:'short',year:'numeric'}))}catch(e){return s}};
const key=s=>String(s||'').replace(/ /g,'_');
const cls=c=>T['c_'+key(c)]?t('c_'+key(c)):(c||'—');
const sub=c=>T['s_'+key(c)]?t('s_'+key(c)):(c||'—');
const toast=m=>{const e=$('#toast');e.textContent=m;e.style.display='block';clearTimeout(toast.h);toast.h=setTimeout(()=>e.style.display='none',2800)};
/* every request carries the active data mode -> server keeps the two modes isolated */
const api=async(u,o={})=>{const h={...(o.headers||{}),'X-Data-Mode':dataMode};try{const k=sessionStorage.getItem('adminKey');if(k)h['X-Admin-Key']=k}catch(e){}
  const r=await fetch(u,{...o,headers:h});let j={};try{j=await r.json()}catch(e){}
  if(r.status===401&&u.startsWith('/api/admin')&&!o._retry){const k=prompt(t('a_key'));if(k){try{sessionStorage.setItem('adminKey',k)}catch(e){}return api(u,{...o,_retry:1})}}
  if(!r.ok)throw Object.assign(new Error(j.error||r.status),{status:r.status});return j};
const PALETTE={'Commercial Area':'#e63946','Settlement':'#f4a261','Agriculture':'#8ab17d','Water Body':'#38bdf8','Road':'#9ca3af','Industrial Area':'#8b5cf6','Forest':'#15803d','Vacant Land':'#d6c28a'};
const colorOf=c=>PALETTE[c]||`hsl(${[...(c||'?')].reduce((a,ch)=>a*31+ch.charCodeAt(0)>>>0,7)%360} 65% 50%)`;
const STAGES=['registration','auto_check','union_office','ac_land','approved','khatian'];
const FT=['fake_deed','double_sale','rapid','mismatch'];
let DSL=[],STATS={},VR=null,TC=null,cur={path:'/',q:new URLSearchParams()};
const goto=h=>{if(location.hash===h)route(false);else location.hash=h};

/* ---------- datasets / selectors ---------- */
const districts=()=>{const m=new Map();DSL.forEach(d=>m.set(d.district,d));return[...m.values()]};
const sheetsOf=d=>DSL.filter(x=>x.district===d);
const shl=d=>d.source==='api'?((lang==='bn'?d.mouza_bn:d.mouza_en)||X(d.sheet||d.id)):t('f_sheet')+' '+X(d.sheet||d.id);
function fillDist(el,val){el.innerHTML=`<option value="">${t('sel_district')}</option>`+districts().map(d=>`<option value="${esc(d.district)}">${esc(dn(d))}</option>`).join('');el.value=districts().some(d=>d.district===val)?val:''}
function fillSheet(el,dist,val,all){const sh=sheetsOf(dist);
  el.innerHTML=(all?`<option value="*">${t('all_sheets')}</option>`:`<option value="">${t('sel_sheet')}</option>`)+sh.map(d=>`<option value="${esc(d.id)}">${shl(d)}</option>`).join('');
  el.value=[...el.options].some(o=>o.value===val)?val:(!all&&sh.length===1?sh[0].id:(all?'*':''))}
function cascade(dEl,sEl,all,cb){const dv=dEl.value,sv=sEl.value;fillDist(dEl,dv);fillSheet(sEl,dEl.value,sv,all);
  dEl.onchange=()=>{fillSheet(sEl,dEl.value,'',all);cb&&cb('d')};sEl.onchange=()=>cb&&cb('s')}
const fillPlots=(el,ds)=>{el.innerHTML='';if(ds&&ds!=='*')api('/api/plots/'+encodeURIComponent(ds)).then(a=>el.innerHTML=a.map(p=>`<option value="${esc(p.plot_no)}">${esc(p.label)}</option>`).join('')).catch(()=>{})};
const bbOf=d=>[[d.bbox[0],d.bbox[1]],[d.bbox[2],d.bbox[3]]];

/* ---------- government API cascading selects (division > district > thana > mouza) ---------- */
const optionText=x=>lang==='bn'?(x.name_bn||x.name_en||x.name||''):(x.name_en||x.name_bn||x.name||'');
const fillApiSelect=(el,items,ph)=>{el._items=items;el._ph=ph;el.innerHTML=`<option value="">${t(ph)}</option>`+items.map(x=>`<option value="${esc(x.code)}">${esc(optionText(x))}</option>`).join('')};
const BDC={},sel=(p,n)=>$('#'+p+n);
async function fillOpts(el,url,ph){
  if(BDC[url])return fillApiSelect(el,BDC[url],ph);
  el.disabled=true;el.innerHTML=`<option value="">${t('loading')}</option>`;let items=[];
  try{items=await api(url);if(!Array.isArray(items))items=[];BDC[url]=items}catch(e){toast(e.message)}
  fillApiSelect(el,items,ph);el.disabled=false}
function bindApi(p){
  const [dv,di,th,mz]=['division','adist','thana','mouza'].map(n=>sel(p,n));if(!dv||dv._bound)return;dv._bound=1;
  const clr=(...els)=>els.forEach(e=>fillApiSelect(e,[],e===di?'sel_district':e===th?'sel_thana':'sel_mouza'));
  dv.onchange=()=>{clr(di,th,mz);if(dv.value)fillOpts(di,'/api/bd/districts?division_code='+dv.value,'sel_district')};
  di.onchange=()=>{clr(th,mz);if(di.value)fillOpts(th,`/api/bd/thanas?division_code=${dv.value}&district_code=${di.value}`,'sel_thana')};
  th.onchange=()=>{clr(mz);if(th.value)fillOpts(mz,`/api/bd/mouzas?division_code=${dv.value}&district_code=${di.value}&thana_code=${th.value}`,'sel_mouza')};
  fillOpts(dv,'/api/bd/divisions','sel_division');clr(di,th,mz)}
const apiBoxHTML=p=>`<div class="row3 api-fields">${[['division','f_division'],['adist','f_district'],['thana','f_thana']].map(([n,k])=>`<label class="f"><span>${t(k)}</span><select id="${p}${n}"></select></label>`).join('')}</div><div class="row2 api-fields" style="align-items:end"><label class="f"><span>${t('f_mouza')}</span><select id="${p}mouza"></select></label><div class="btnrow"><button class="btn" id="${p}apiload">${t('api_load')}</button></div></div>`;
function apiSel(p){const g=n=>sel(p,n),it=n=>(g(n)._items||[]).find(x=>String(x.code)===g(n).value)||{};
  const [dv,di,th,mz]=['division','adist','thana','mouza'].map(it);
  return{division:g('division').value,district:g('adist').value,thana:g('thana').value,mouza:g('mouza').value,mcode:mz.m_code,
    meta:{division_bn:dv.name_bn,division_en:dv.name_en,district_bn:di.name_bn,district_en:di.name_en,thana_bn:th.name_bn,thana_en:th.name_en,mouza_bn:mz.name_bn,mouza_en:mz.name_en,mouza_code:mz.m_code}}}
const apiId=s=>'api_'+[s.division,s.district,s.thana,s.mouza].map(c=>c.replace(/[^A-Za-z0-9]/g,'')).join('_').toLowerCase();

/* ---------- loader popup (step by step) ---------- */
function loaderOpen(steps){$('#ldTitle').textContent=t('ld_title');$('#ldSteps').innerHTML=steps.map(k=>`<li>${t(k)}</li>`).join('');$('#ldBar').style.width='0';$('#ldClose').hidden=true;$('#loader').hidden=false}
function LD(i,st){$('#ldSteps').children[i].className=st;$('#ldBar').style.width=((i+(st==='run'?.4:1))/$('#ldSteps').children.length*100)+'%'}
const loaderClose=()=>$('#loader').hidden=true;
$('#ldClose').onclick=loaderClose;
async function refreshData(){[DSL,STATS]=await Promise.all([api('/api/datasets'),api('/api/stats')]);sigSeen=(await api('/api/version')).sig}

/* load one mouza from the government API, step by step, then SAVE it under a dynamic dataset id */
async function loadApiArea(p,force){
  const s=apiSel(p);
  if(!s.division||!s.district||!s.thana||!s.mouza){toast(t('need_api_area'));return null}
  if(!s.mcode){toast(t('ld_nocode'));return null}
  const id=apiId(s);
  if(!force&&DSL.some(d=>d.id===id&&d.source==='api')){toast(t('ld_cached'));return id}
  loaderOpen(['ld_dist','ld_thana','ld_plots','ld_land','ld_save']);
  const tbl=s.mcode.toLowerCase(),mz=`&mauzaVal=${encodeURIComponent(s.mouza)}`;
  const q=(lvl,tb,x)=>api(`/api/bd/map-data?level=${lvl}&tableName=${encodeURIComponent(tb)}${x}`);
  const run=async(i,fn,opt)=>{LD(i,'run');try{const r=await fn();LD(i,'ok');return r}catch(e){LD(i,opt?'warn':'fail');if(!opt)throw e}};
  try{
    const bd={};
    bd.district=(await run(0,()=>q('forDist','gis_boundary_district',`&divVal=${s.division}&distVal=${s.district}`),1))?.geom;
    bd.thana=(await run(1,()=>q('forThana','gis_boundary_upazila',`&thanaVal=${s.thana}`),1))?.geom;
    const pr=await run(2,()=>q('forMauza',tbl,mz+'&plotOrLanduse=plot'));
    const lr=await run(3,()=>q('forMauza',tbl,mz+'&plotOrLanduse=landuse'),1);
    const r=await run(4,()=>api('/api/bd/register',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({meta:{division:s.division,district:s.district,thana:s.thana,mouza:s.mouza,...s.meta},plot:(pr&&pr.plots)||[],land:(lr&&lr.landuse)||[],boundaries:bd})}));
    await refreshData();loaderClose();return r.id
  }catch(e){$('#ldTitle').textContent=t('err')+': '+e.message;$('#ldClose').hidden=false;return null}}

/* ---------- mode switch: fully isolated state ---------- */
function applyModeUI(){$$('.modeSeg button').forEach(b=>b.classList.toggle('on',b.dataset.mode===dataMode));$('#datasetSearch').hidden=dataMode==='api';$('#apiSearch').hidden=dataMode!=='api';if(dataMode==='api')bindApi('h_')}
async function switchMode(m){dataMode=m;localStorage.dataMode=m;applyModeUI();
  try{await refreshData()}catch(e){toast(e.message)}
  loaded=[];DATA=null;curSel=null;VR=null;TC=null;FA=[];WC=[];hiddenCls.clear();
  $('#v_out').innerHTML='';$('#tr_out').innerHTML='';$('#v_apiBox').innerHTML='';
  if(mapReady){landG.clearLayers();plotG.clearLayers();ovG.clearLayers();apiG.clearLayers();$('#drawer').hidden=true;plotLayers={};selLayer=null}
  const h=location.hash.split('?')[0];if(location.hash&&h!==location.hash)location.hash=h;else route(false)}

/* ---------- static i18n ---------- */
function applyStatic(){document.documentElement.lang=lang;$$('[data-ic]').forEach(e=>e.innerHTML=ico(e.dataset.ic,+e.dataset.s||18));document.title=t('brand')+' — '+t('tagline');
  $$('[data-i18n]').forEach(e=>e.textContent=t(e.dataset.i18n));$$('[data-ph]').forEach(e=>e.placeholder=t(e.dataset.ph));
  $$('#langSeg button').forEach(b=>b.classList.toggle('on',b.dataset.lang===lang));$$('#roleSeg button').forEach(b=>b.classList.toggle('on',b.dataset.role===role));
  applyModeUI();$$('select').forEach(e=>{if(e._items&&e._ph){const v=e.value;fillApiSelect(e,e._items,e._ph);e.value=v}});
  $('#navAdmin').style.display=role==='admin'?'':'none'}
$('#langSeg').onclick=e=>{const b=e.target.closest('button');if(!b)return;lang=b.dataset.lang;localStorage.lang=lang;applyStatic();route(true)};
$$('.modeSeg').forEach(sg=>sg.onclick=e=>{const b=e.target.closest('button');if(b&&b.dataset.mode!==dataMode)switchMode(b.dataset.mode)});
$('#roleSeg').onclick=e=>{const b=e.target.closest('button');if(!b)return;role=b.dataset.role;localStorage.role=role;applyStatic();
  if(role==='admin'&&cur.path.indexOf('/admin')!==0)location.hash='#/admin';else if(role==='user'&&cur.path.indexOf('/admin')===0)location.hash='#/';else route(true)};
function syncThemeLogo(theme){const dark=(theme||document.documentElement.dataset.theme)==='dark';const s=document.querySelector('.brand > img:only-child');if(s)s.src=dark?'/assets/logo_dark.png':'/assets/logo.png'}
$('#themeBtn').onclick=()=>{const n=document.documentElement.dataset.theme==='dark'?'light':'dark';document.documentElement.dataset.theme=n;localStorage.theme=n;syncThemeLogo(n)};
syncThemeLogo();
$('#burger').onclick=()=>{const collapsed=document.body.classList.toggle('side-nav-collapsed');$('#burger').setAttribute('aria-expanded',String(!collapsed))};
$$('[data-go]').forEach(e=>e.onclick=()=>{const g=e.dataset.go;if(g==='#/admin'&&role!=='admin'){role='admin';localStorage.role=role;applyStatic()}location.hash=g});

/* ---------- router ---------- */
function route(keep){
  const h=location.hash.slice(1)||'/',[path,qs]=h.split('?');cur={path,q:new URLSearchParams(qs||'')};
  const v=path.startsWith('/map')?'map':path.startsWith('/verify')?'verify':path.startsWith('/track')?'track':path.startsWith('/admin')?'admin':'home';
  $$('.view').forEach(e=>e.hidden=e.id!=='v-'+v);document.body.classList.toggle('map-mode',v==='map');
  $$('.nav-links a').forEach(a=>a.classList.toggle('on',a.dataset.r===v));
  if(v==='home')renderHome();else if(v==='map')showMap(cur.q,keep);else if(v==='verify')renderVerify(keep);else if(v==='track')renderTrack(keep);else renderAdmin();
  if(!keep&&v!=='map')scrollTo(0,0)}
addEventListener('hashchange',()=>route(false));

/* ---------- home ---------- */
function renderHome(){
  $('#h_stats').innerHTML=[['stat_ds',STATS.datasets],['stat_plots',STATS.plots],['stat_checks',STATS.checks],['stat_alerts',STATS.alerts],['stat_cases',STATS.cases]].map(([k,v])=>`<div class="stat"><b>${nf(v||0,0)}</b><span>${t(k)}</span></div>`).join('');
  const reveal=$$('.home-reveal');
  if('IntersectionObserver' in window){const observer=new IntersectionObserver(entries=>entries.forEach(entry=>{if(entry.isIntersecting){entry.target.classList.add('is-visible');observer.unobserve(entry.target)}}),{threshold:.12});reveal.forEach(e=>observer.observe(e))}
  else reveal.forEach(e=>e.classList.add('is-visible'));
  applyModeUI();
  cascade($('#h_dist'),$('#h_sheet'),false,w=>{if(w==='s')fillPlots($('#h_plots'),$('#h_sheet').value)});fillPlots($('#h_plots'),$('#h_sheet').value)}
$('#h_map').onclick=async()=>{
  if(dataMode==='api'){const id=await loadApiArea('h_');if(id){const p=$('#h_aplot').value.trim();goto('#/map?ds='+id+(p?'&plot='+encodeURIComponent(p):''))}return}
  const d=$('#h_dist').value,s=$('#h_sheet').value,p=$('#h_plot').value.trim();if(!d)return toast(t('need_dist'));
  const ids=s?[s]:sheetsOf(d).map(x=>x.id);goto(`#/map?ds=${ids.join(',')}${p&&ids.length===1?'&plot='+encodeURIComponent(p):''}`)};
$('#h_ver').onclick=async()=>{
  if(dataMode==='api'){const p=$('#h_aplot').value.trim();if(!p)return toast(t('need_plot'));
    const id=await loadApiArea('h_');if(id)goto(`#/verify?ds=${id}&plot=${encodeURIComponent(p)}`);return}
  const s=$('#h_sheet').value,p=$('#h_plot').value.trim();if(!s||!p)return toast(t('need_plot'));goto(`#/verify?ds=${s}&plot=${encodeURIComponent(p)}`)};
const goPlace=()=>{const v=$('#h_place').value.trim();if(v)goto('#/map?place='+encodeURIComponent(v))};
$('#h_go').onclick=goPlace;$('#h_place').onkeydown=e=>{if(e.key==='Enter')goPlace()};

/* ---------- map ---------- */
let map,landG,plotG,apiG,ovG,baseLayer,baseKey='satl',loaded=[],DATA=null,plotLayers={},selLayer=null,curSel=null,hiddenCls=new Set(),mapReady=false,pinMark=null,busy=false,searchT;
const esri='https://server.arcgisonline.com/ArcGIS/rest/services/';
const BASES={
 sat:()=>L.tileLayer(esri+'World_Imagery/MapServer/tile/{z}/{y}/{x}',{maxNativeZoom:19,maxZoom:22,attribution:'Tiles © Esri'}),
 satl:()=>L.layerGroup([BASES.sat(),L.tileLayer(esri+'Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}',{maxNativeZoom:19,maxZoom:22})]),
 osm:()=>L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxNativeZoom:19,maxZoom:22,attribution:'© OpenStreetMap contributors'}),
 topo:()=>L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png',{maxNativeZoom:17,maxZoom:22,attribution:'© OpenTopoMap, OSM'}),
 light:()=>L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png',{maxNativeZoom:20,maxZoom:22,attribution:'© OSM, © CARTO'}),
 dark:()=>L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',{maxNativeZoom:20,maxZoom:22,attribution:'© OSM, © CARTO'})};
function setBase(k){baseKey=k;if(baseLayer)map.removeLayer(baseLayer);baseLayer=BASES[k]().addTo(map);baseLayer.eachLayer?baseLayer.eachLayer(l=>l.setZIndex&&l.setZIndex(1)):0;renderBases()}
function renderBases(){$('#bases').innerHTML=Object.keys(BASES).map(k=>`<button class="chip ${k===baseKey?'on':''}" data-k="${k}">${t('bm_'+k)}</button>`).join('');$$('#bases .chip').forEach(b=>b.onclick=()=>setBase(b.dataset.k))}
function initMap(){
  if(mapReady)return;mapReady=true;
  map=L.map('map',{zoomControl:false,maxZoom:22}).setView([23.7,90.35],7);
  L.control.zoom({position:'bottomright'}).addTo(map);L.control.scale({imperial:false,position:'bottomleft'}).addTo(map);
  map.createPane('landPane').style.zIndex=410;map.createPane('plotPane').style.zIndex=420;map.createPane('boundPane').style.zIndex=405;
  apiG=L.layerGroup().addTo(map);landG=L.layerGroup().addTo(map);plotG=L.layerGroup().addTo(map);ovG=L.layerGroup().addTo(map);setBase('satl');
  $('#lyLand').onchange=e=>e.target.checked?landG.addTo(map):map.removeLayer(landG);
  $('#lyPlot').onchange=e=>e.target.checked?plotG.addTo(map):map.removeLayer(plotG);
  $('#lbl').onchange=updateLabels;map.on('zoomend',updateLabels);map.on('moveend',autoLoad);
  $('#op').oninput=()=>{const o=+$('#op').value;landG.eachLayer(g=>g.setStyle&&g.setStyle({fillOpacity:o}))};
  $('#ptoggle').onclick=()=>$('#panel').classList.toggle('hide');
  $('#fit').onclick=()=>fitData();$('#m_all').onclick=()=>{loadIds([]);fitAll()};
  $('#q').oninput=()=>{clearTimeout(searchT);searchT=setTimeout(renderList,220)};
  const pl=()=>{const v=$('#place').value.trim();if(v)geocode(v)};$('#place').onkeydown=e=>{if(e.key==='Enter')pl()};
  $('#locate').onclick=locateMe;
  $('#m_apiload').onclick=async()=>{const id=await loadApiArea('m_');if(id)await loadIds([...new Set([...loaded,id])])};
  setInterval(poll,4000)}
function updateLabels(){map.getContainer().classList.toggle('nolabels',!$('#lbl').checked||map.getZoom()<17)}
function fitData(){if(!DATA)return fitAll();const g=L.geoJSON([...DATA.plot.features,...DATA.land.features]);if(g.getLayers().length)map.fitBounds(g.getBounds(),{padding:[50,50]})}
function fitAll(){const b=DSL.filter(d=>d.bbox).flatMap(d=>bbOf(d));if(b.length)map.fitBounds(b,{padding:[60,60],maxZoom:12})}
function mapUrl(plot){if(!cur.path.startsWith('/map'))return;history.replaceState(null,'','#/map'+(loaded.length?`?ds=${loaded.join(',')}${plot?'&plot='+encodeURIComponent(plot):''}`:''))}
function wktGeo(wkt){
  const m=/^\s*(MULTIPOLYGON|POLYGON)\s*(\(.*\))\s*$/is.exec(wkt||'');if(!m)return null;
  const stack=[[]],tokens=m[2].match(/\(|\)|[^()]+/g)||[];
  for(const token of tokens){
    if(token==='(')stack.push([]);
    else if(token===')'){if(stack.length<2)return null;const node=stack.pop();stack[stack.length-1].push(node)}
    else stack[stack.length-1].push(...token.split(',').map(p=>p.trim().split(/\s+/).slice(0,2).map(Number)).filter(p=>p.length===2&&p.every(Number.isFinite)));
  }
  if(stack.length!==1||!stack[0].length)return null;
  return {type:m[1].toUpperCase()==='POLYGON'?'Polygon':'MultiPolygon',coordinates:stack[0][0]};
}
async function showMap(q,keep){
  initMap();setTimeout(()=>map.invalidateSize(),60);
  const isApi=dataMode==='api';$('#m_dsNav').hidden=isApi;$('#m_apiNav').hidden=!isApi;
  if(isApi)bindApi('m_');
  else cascade($('#m_dist'),$('#m_sheet'),true,w=>{const d=$('#m_dist').value,s=$('#m_sheet').value;if(!d)return loadIds([]);loadIds(s&&s!=='*'?[s]:sheetsOf(d).map(x=>x.id))});
  renderBases();sidebar();
  if(keep){if(curSel)showDrawer(curSel[0],curSel[1]);return}
  const ds=(q.get('ds')||'').split(',').filter(i=>DSL.some(d=>d.id===i));
  if(q.get('place'))return geocode(q.get('place'));
  if(ds.length){if(ds.join()!==loaded.join())await loadIds(ds);const p=q.get('plot');
    if(p){const f=DATA&&DATA.plot.features.find(f=>String(f.properties.plot_no_en)===p);if(f)selectPlot(f.properties._ds,p,true);else toast(t('v_notfound'))}}
  else if(!loaded.length){drawOverview();fitAll()}}
async function loadIds(ids,o={}){
  const fit=o.fit!==false;
  if(!ids.length){loaded=[];DATA=null;curSel=null;$('#drawer').hidden=true;renderData();mapUrl();return}
  try{DATA=await api('/api/data?ids='+ids.join(','));loaded=DATA.ids;renderData();if(fit)fitData();if(o.quiet)toast(t('m_refreshed'));mapUrl(curSel&&curSel[1])}catch(e){toast(e.message)}}
function renderData(){
  landG.clearLayers();plotG.clearLayers();apiG.clearLayers();plotLayers={};selLayer=null;
  if(DATA){const op=+$('#op').value,many=DATA.plot.features.length>1500;
    (DATA.bounds||[]).forEach(b=>[['district','#2563eb',2],['thana','#00e5ff',3]].forEach(([k,c,w])=>{const g=b[k]&&wktGeo(b[k]);if(g)L.geoJSON(g,{pane:'boundPane',interactive:false,style:{color:c,weight:w,fillOpacity:.03}}).addTo(apiG)}));
    L.geoJSON(DATA.land,{pane:'landPane',interactive:false,filter:f=>!hiddenCls.has(f.properties.maj_class),style:f=>({color:'#fff',weight:1,fillColor:colorOf(f.properties.maj_class),fillOpacity:op})}).addTo(landG);
    L.geoJSON(DATA.plot,{pane:'plotPane',style:()=>({color:'#ffd60a',weight:2,fillColor:'#ffd60a',fillOpacity:.04}),onEachFeature:(f,l)=>{
      const p=f.properties;plotLayers[p._ds+'|'+p.plot_no_en]=l;
      l.bindTooltip(String(p.label??p.plot_no_en),{permanent:!many,sticky:many,direction:'center',className:'plot-lbl'});
      l.on('click',()=>selectPlot(p._ds,String(p.plot_no_en),false));
      l.on('mouseover',()=>l!==selLayer&&l.setStyle({weight:3}));l.on('mouseout',()=>l!==selLayer&&l.setStyle({weight:2}))}}).addTo(plotG);
    if(curSel&&plotLayers[curSel.join('|')])highlight(plotLayers[curSel.join('|')])}
  updateLabels();drawOverview();sidebar()}
function drawOverview(){ovG.clearLayers();DSL.filter(d=>d.center&&!loaded.includes(d.id)).forEach(d=>{
  const m=L.marker(d.center,{icon:L.divIcon({className:'',iconSize:[0,0],html:`<div class="ovpin"><b>${esc(dn(d))}</b>${shl(d)} · ${nf(d.plots,0)}</div>`})});
  m.on('click',async()=>{await loadIds([...loaded,d.id],{fit:false});map.fitBounds(bbOf(d),{padding:[40,40]})});m.addTo(ovG)})}
async function autoLoad(){
  if(busy||!$('#autoload').checked||map.getZoom()<15)return;const b=map.getBounds();
  const hit=DSL.filter(d=>d.bbox&&!loaded.includes(d.id)&&b.intersects(bbOf(d)));if(!hit.length)return;
  busy=true;await loadIds([...loaded,...hit.map(d=>d.id)],{fit:false});busy=false}
function highlight(l){if(selLayer)selLayer.setStyle({color:'#ffd60a',weight:2});selLayer=l;l.setStyle({color:'#ff3b30',weight:4});l.bringToFront()}
async function selectPlot(ds,no,zoom){
  if(!plotLayers[ds+'|'+no])await loadIds([...new Set([...loaded,ds])],{fit:false});
  const l=plotLayers[ds+'|'+no];curSel=[ds,no];
  if(l){highlight(l);if(zoom)map.fitBounds(l.getBounds(),{maxZoom:20,padding:[70,70]})}
  mapUrl(no);showDrawer(ds,no);if(innerWidth<760)$('#panel').classList.add('hide')}
function sidebar(){
  const lf=DATA?DATA.land.features:[],pf=DATA?DATA.plot.features:[];
  const total=(pf.length?pf:lf).reduce((a,f)=>a+f.properties.area_sqm,0),cs={};
  lf.forEach(f=>{const c=f.properties.maj_class||'?';(cs[c]??={n:0,a:0}).n++;cs[c].a+=f.properties.area_sqm});
  $('#sub').textContent=(loaded.length?`${loaded.length} ${t('m_loaded')} · ${new Date().toLocaleTimeString(lang==='bn'?'bn-BD':'en-GB')}`:t('m_click_ds'))+(dataMode==='api'?' · API':'');
  $('#mstats').innerHTML=[[pf.length,'m_total'],[lf.length,'m_parts'],[dec(total),'m_area'],[Object.keys(cs).length,'m_cls']].map(([v,k],i)=>`<div><b>${nf(v,i==2?1:0)}</b><span>${t(k)}</span></div>`).join('');
  $('#legend').innerHTML=Object.entries(cs).map(([c,v])=>`<label class="rw"><input type="checkbox" data-c="${esc(c)}" ${hiddenCls.has(c)?'':'checked'}><span class="sw" style="background:${colorOf(c)}"></span><span class="lbl">${esc(cls(c))}</span><span class="n">${nf(v.n,0)} · ${nf(dec(v.a),1)}</span></label>`).join('')||`<small>${t('m_none')}</small>`;
  $$('#legend input').forEach(i=>i.onchange=()=>{i.checked?hiddenCls.delete(i.dataset.c):hiddenCls.add(i.dataset.c);renderData()});
  renderList();renderSaved()}
function renderSaved(){if(dataMode!=='api')return;const a=DSL.filter(d=>d.source==='api');$('#m_fb').hidden=!DSL.some(d=>d.fallback);
  $('#m_saved').innerHTML=a.map(d=>`<div class="rw" data-id="${esc(d.id)}">${ico('pin',15)} ${esc(dn(d))} · ${esc(shl(d))}<span class="n">${nf(d.plots,0)}</span></div>`).join('')||`<small>${t('m_none')}</small>`;
  $$('#m_saved .rw').forEach(e=>e.onclick=()=>loadIds([...new Set([...loaded,e.dataset.id])]).then(()=>{const d=DSL.find(x=>x.id===e.dataset.id);d&&d.bbox&&map.fitBounds(bbOf(d),{padding:[40,40]})}))}
async function renderList(){
  const q=$('#q').value.trim(),el=$('#plist');let rows=[];
  if(q){try{rows=(await api('/api/search?q='+encodeURIComponent(q))).results}catch(e){}}
  else if(DATA)rows=DATA.plot.features.slice(0,300).map(f=>({ds:f.properties._ds,plot_no:String(f.properties.plot_no_en),label:f.properties.label,area_sqm:f.properties.area_sqm,sheet:(DSL.find(d=>d.id===f.properties._ds)||{}).sheet}));
  el.innerHTML=rows.map((r,i)=>`<div class="rw" data-i="${i}"><span class="sw" style="background:#ffd60a"></span>${t('v_plot')} ${X(r.label)}${r.district?` <small>· ${esc(nm(r.district))} ${X(r.sheet)}</small>`:''}${r.owner?` <small>· ${esc(nm(r.owner))}</small>`:''}<span class="n">${nf(dec(r.area_sqm))} ${t('u_dec')}</span></div>`).join('')||`<small>${q?t('m_nodata'):t('m_none')}</small>`;
  $$('#plist .rw').forEach(e=>e.onclick=()=>{const r=rows[+e.dataset.i];selectPlot(r.ds,String(r.plot_no),true)})}
async function showDrawer(ds,no){
  const dr=$('#drawer');dr.hidden=false;dr.innerHTML=`<div class="dload">${t('loading')}</div>`;
  try{const r=await api(`/api/verify/${encodeURIComponent(ds)}/${encodeURIComponent(no)}`);if(!curSel||curSel[0]!==ds||curSel[1]!==no)return;
    const vt={safe:'v_verdict_safe',caution:'v_verdict_caution',danger:'v_verdict_danger'}[r.verdict];
    dr.innerHTML=`<button class="icon x" id="dx" style="width:32px;height:32px">${ico('x',15)}</button><h3>${t('v_plot')} ${X(r.label)}</h3>
    <p style="margin:2px 0 10px;color:var(--mu);font-size:14px">${esc(nm(r.district))} · ${X(r.mouza)}${r.source==='api'?'':' · '+t('f_sheet')+' '+X(r.sheet)}</p>
    <span class="badge ${r.verdict}">${t(vt)} · ${nf(r.score,0)}</span> <span class="badge ${r.source==='api'?'ok':''}">${t('src_'+r.source)}</span>
    <dl class="kv" style="margin:12px 0"><dt>${t('d_owner')}</dt><dd>${esc(nm(r.owner))}</dd>
    <dt>${t('v_area')}</dt><dd>${nf(dec(r.area_sqm))} ${t('u_dec')}<br><small style="color:var(--mu)">${nf(r.area_sqm,0)} ${t('u_sqm')} · ${nf(r.area_sqm/4046.86,4)} ${t('u_acre')}</small></dd>
    <dt>${t('d_addr')}</dt><dd id="addr">…</dd></dl>
    <div class="chips" style="margin-bottom:12px">${r.parts.map(p=>`<span class="tag" style="background:${colorOf(p.maj)}">${esc(cls(p.maj))} · ${esc(sub(p.sub))} · ${nf(dec(p.area_sqm),1)}</span>`).join('')}</div>
    <div class="btnrow"><a class="btn sm" href="#/verify?ds=${encodeURIComponent(ds)}&plot=${encodeURIComponent(no)}">${ico('search',15)} ${t('d_report')}</a>
    <button class="btn sm alt" id="dshare">${ico('link',15)} ${t('d_share')}</button>
    <a class="btn sm alt" target="_blank" rel="noopener" href="https://www.google.com/maps?q=${r.center[0]},${r.center[1]}">${ico('globe',15)} ${t('d_gmaps')}</a></div>${role==='admin'?`<div class="btnrow" style="margin-top:8px"><button class="btn sm danger" id="dna">${ico('alert',15)} ${t('a_add_alert')}</button><button class="btn sm alt" id="dnc">${ico('clipboard',15)} ${t('a_add_case')}</button></div>`:''}`;
    if(role==='admin'){$('#dna').onclick=()=>openCreate('fraud',{ds,plot:no});$('#dnc').onclick=()=>openCreate('case',{ds,plot:no})}
    $('#dx').onclick=()=>{dr.hidden=true;curSel=null;if(selLayer){selLayer.setStyle({color:'#ffd60a',weight:2});selLayer=null}mapUrl()};
    $('#dshare').onclick=()=>{navigator.clipboard?.writeText(location.href).then(()=>toast(t('d_copied')),()=>prompt('',location.href))};
    fetch(`https://nominatim.openstreetmap.org/reverse?format=jsonv2&zoom=17&accept-language=${lang==='bn'?'bn,en':'en'}&lat=${r.center[0]}&lon=${r.center[1]}`).then(x=>x.json()).then(j=>{const a=$('#addr');if(a)a.textContent=j.display_name||'—'}).catch(()=>{const a=$('#addr');if(a)a.textContent='—'})
  }catch(e){dr.innerHTML=`<div class="dload">${esc(e.message)}</div>`}}
async function geocode(q){
  $('#place').value=q;const el=$('#placeRes');el.innerHTML=`<small>${t('loading')}</small>`;
  try{const r=await (await fetch(`https://nominatim.openstreetmap.org/search?format=jsonv2&countrycodes=bd&limit=6&accept-language=${lang==='bn'?'bn,en':'en'}&q=${encodeURIComponent(q)}`)).json();
    el.innerHTML=r.map((x,i)=>`<div class="rw" data-i="${i}">${ico('pin',15)} <span>${esc(x.display_name.split(',').slice(0,3).join(','))}</span></div>`).join('')||`<small>${t('m_nodata')}</small>`;
    const go=x=>{const lat=+x.lat,lng=+x.lon;if(pinMark)map.removeLayer(pinMark);pinMark=L.marker([lat,lng]).addTo(map).bindPopup(esc(x.display_name)).openPopup();
      const near=DSL.filter(d=>d.center).map(d=>({d,m:map.distance([lat,lng],d.center)})).sort((a,b)=>a.m-b.m)[0];
      if(near&&near.m<4000){toast(t('m_place_hit'));loadIds([...new Set([...loaded,near.d.id])],{fit:false}).then(()=>map.fitBounds(bbOf(near.d),{padding:[40,40]}))}
      else if(x.boundingbox){const b=x.boundingbox.map(Number);map.fitBounds([[b[0],b[2]],[b[1],b[3]]],{maxZoom:17})}else map.setView([lat,lng],16)};
    $$('#placeRes .rw').forEach(e=>e.onclick=()=>go(r[+e.dataset.i]));if(r.length===1||cur.q.get('place'))go(r[0])
  }catch(e){el.innerHTML=`<small>${t('err')}: ${esc(e.message)}</small>`}}
function locateMe(){if(!navigator.geolocation)return;toast(t('m_locating'));
  navigator.geolocation.getCurrentPosition(p=>{const ll=[p.coords.latitude,p.coords.longitude];if(pinMark)map.removeLayer(pinMark);pinMark=L.circleMarker(ll,{radius:9,color:'#fff',weight:3,fillColor:'#2563eb',fillOpacity:1}).addTo(map).bindPopup(t('m_here')).openPopup();map.setView(ll,17)},e=>toast(e.message),{enableHighAccuracy:true,timeout:10000})}
let sigSeen=null;
async function poll(){if(!mapReady||!$('#auto').checked||$('#loader').hidden===false)return;const mode=dataMode;
  try{const s=(await api('/api/version')).sig;if(mode!==dataMode)return;
    if(sigSeen&&s!==sigSeen){DSL=await api('/api/datasets');STATS=await api('/api/stats');
      cascade($('#m_dist'),$('#m_sheet'),true,()=>{});
      if(loaded.length)await loadIds(loaded.filter(i=>DSL.some(d=>d.id===i)),{fit:false,quiet:true});else{drawOverview();sidebar()}}
    sigSeen=s}catch(e){}}

/* ---------- verify ---------- */
let vmap=null;
function renderVerify(keep){
  const ab=$('#v_apiBox');ab.hidden=dataMode!=='api';
  if(dataMode==='api'){if(!ab.firstChild||keep){ab.innerHTML=apiBoxHTML('v_');bindApi('v_');
    $('#v_apiload').onclick=async()=>{const id=await loadApiArea('v_');if(!id)return;const d=DSL.find(x=>x.id===id);if(!d)return;
      fillDist($('#v_dist'),d.district);fillSheet($('#v_sheet'),d.district,id,false);fillPlots($('#v_plots'),id)}}}
  else ab.innerHTML='';
  cascade($('#v_dist'),$('#v_sheet'),false,w=>{if(w==='s')fillPlots($('#v_plots'),$('#v_sheet').value)});
  const q=cur.q,ds=q.get('ds'),p=q.get('plot');
  if(!keep&&ds&&p){const d=DSL.find(x=>x.id===ds);if(d){$('#v_dist').value=d.district;fillSheet($('#v_sheet'),d.district,ds,false);$('#v_sheet').value=ds;fillPlots($('#v_plots'),ds);$('#v_plot').value=p;runVerify(ds,p);return}}
  if(keep&&VR)drawReport();else if(!VR)$('#v_out').innerHTML=''}
$('#v_go').onclick=()=>{const s=$('#v_sheet').value,p=$('#v_plot').value.trim();if(!s||!p)return toast(t('need_plot'));runVerify(s,p)};
async function runVerify(ds,p){$('#v_out').innerHTML=`<div class="empty">${t('loading')}</div>`;
  try{VR=await api(`/api/verify/${encodeURIComponent(ds)}/${encodeURIComponent(p)}`);drawReport()}catch(e){VR=null;$('#v_out').innerHTML=`<div class="card empty">${t('v_notfound')}</div>`}}
function drawReport(){const r=VR;if(!r)return;
  const vt={safe:'v_verdict_safe',caution:'v_verdict_caution',danger:'v_verdict_danger'}[r.verdict],ic={pass:ico('check',14),warn:ico('alert',14),fail:ico('x',14),queued:ico('clock',14)};
  const hist=r.history.map(h=>`<li><b>${t('h_'+h.type)}</b> — ${esc(nm(h.from))} → ${esc(nm(h.to))}<small>${fd(h.date)} · ${t('v_deed')} ${X(h.deed_no)} · ${t('v_case')} ${X(h.case_no)}</small></li>`).join('');
  $('#v_out').innerHTML=`
  <div class="vbanner ${r.verdict}"><div class="gauge" style="--p:${r.score}"><b>${nf(r.score,0)}</b><small>${t('v_score')}</small></div>
    <div><h2>${t(vt)}</h2><p>${t('v_plot')} ${X(r.label)} · ${esc(nm(r.district))} · ${X(r.mouza)}${r.source==='api'?'':' · '+t('f_sheet')+' '+X(r.sheet)}</p>
    <div class="btnrow"><button class="btn sm alt" onclick="print()">${ico('printer',15)} ${t('v_print')}</button><a class="btn sm alt" href="#/map?ds=${encodeURIComponent(r.ds)}&plot=${encodeURIComponent(r.plot_no)}">${ico('map',15)} ${t('open_map')}</a></div></div></div>
  <div class="vgrid">
   <div class="card"><h3>${t('v_status')}</h3>${r.checks.map(c=>`<div class="ck ${c.level}"><i>${ic[c.level]}</i><span>${t('ck_'+c.key)}</span><em>${t('lv_'+c.level)}</em></div>`).join('')}</div>
   <div class="card"><h3>${t('v_owner_h')}</h3><dl class="kv">
     <dt>${t('v_owner')}</dt><dd>${esc(nm(r.owner))}</dd><dt>${t('v_father')}</dt><dd>${esc(nm(r.father))}</dd><dt>${t('v_nid')}</dt><dd>${X(r.nid)}</dd>
     <dt>${t('v_khatian')}</dt><dd>${X(r.khatian)}</dd><dt>${t('v_dag')}</dt><dd>${X(r.dag)}</dd><dt>${t('v_mouza')}</dt><dd>${X(r.mouza)} (${X(r.m_code)})</dd><dt>${t('v_share')}</dt><dd>${X(r.share)}</dd>
     <dt>${t('v_area')}</dt><dd>${nf(dec(r.area_sqm))} ${t('u_dec')} · ${nf(r.area_sqm/4046.86,4)} ${t('u_acre')}</dd></dl></div>
   <div class="card"><h3>${t('v_location')}</h3><div id="vmap"></div><div class="chips">${r.parts.map(p=>`<span class="tag" style="background:${colorOf(p.maj)}">${esc(cls(p.maj))} · ${esc(sub(p.sub))} · ${nf(dec(p.area_sqm),1)} ${t('u_dec')}</span>`).join('')}</div></div>
   <div class="card"><h3>${t('v_flags')}</h3>${r.flags.length?r.flags.map(f=>`<div class="item"><span class="badge ${f.severity}">${t('sev_'+f.severity)}</span> <b>${t('ft_'+f.type)}</b><ul class="adv" style="margin-top:6px">${f.evidence.map(e=>`<li>${X(nm(e))}</li>`).join('')}</ul></div>`).join(''):`<div class="empty">${ico('check',15)} ${t('v_noflags')}</div>`}</div>
   <div class="card wide"><h3>${t('v_pending')}</h3>
     ${r.pending.length?r.pending.map(p=>`<div class="item"><span class="badge warn">${t('p_'+p.type)}</span> ${X(p.ref)} · ${t('fo_'+p.forum)} · ${t('v_since')} ${fd(p.since)}</div>`).join(''):`<div class="item">${ico('check',15)} ${t('v_nopending')}</div>`}
     <div class="item"><span class="badge ${r.khas==='none'?'pass':'fail'}">${t('kh_'+r.khas)}</span> <span class="badge ${r.acq==='none'?'pass':r.acq==='proposed'?'warn':'fail'}">${t('aq_'+r.acq)}</span></div></div>
   <div class="card wide"><h3>${t('v_hist')}</h3><ul class="tl">${hist}</ul></div>
   <div class="card wide"><h3>${t('v_advice')}</h3><ul class="adv">${r.advice.map(a=>`<li>${X(nm(a))}</li>`).join('')}</ul></div>
  </div>`;
  if(vmap){vmap.remove();vmap=null}
  vmap=L.map('vmap',{zoomControl:false,attributionControl:false});L.tileLayer(esri+'World_Imagery/MapServer/tile/{z}/{y}/{x}',{maxZoom:21,maxNativeZoom:19}).addTo(vmap);
  const g=L.geoJSON(r.geometry,{style:{color:'#ffd60a',weight:3,fillOpacity:.18}}).addTo(vmap);vmap.fitBounds(g.getBounds(),{maxZoom:19,padding:[24,24]})}

/* ---------- tracking ---------- */
function renderTrack(keep){
  $('#tr_samples').innerHTML=`<small style="color:var(--mu);align-self:center">${t('tr_samples')}:</small>`+(STATS.sample_cases||[]).map(c=>`<button class="chip" data-id="${c.id}">${c.id}</button>`).join('');
  $$('#tr_samples .chip').forEach(b=>b.onclick=()=>{$('#tr_in').value=b.dataset.id;runTrack(b.dataset.id)});
  const id=cur.q.get('id');if(!keep&&id){$('#tr_in').value=id;runTrack(id)}else if(keep&&TC)drawTrack()}
$('#tr_go').onclick=()=>{const v=$('#tr_in').value.trim();if(v)runTrack(v)};$('#tr_in').onkeydown=e=>{if(e.key==='Enter')$('#tr_go').click()};
async function runTrack(id){try{TC=await api('/api/track/'+encodeURIComponent(id));drawTrack()}catch(e){TC=null;$('#tr_out').innerHTML=`<div class="card empty">${t('tr_notfound')}</div>`}}
function caseBody(c){
  const ic={pass:ico('check',14),warn:ico('alert',14),fail:ico('x',14),queued:ico('clock',14)};
  const steps=STAGES.map((s,i)=>`<div class="${i<c.stage||c.status==='completed'?'done':i===c.stage?'cur '+(c.status==='flagged'||c.status==='rejected'?'flagged':''):''}">${t('sg_'+s)}</div>`).join('');
  const dl=c.days_left,sla=c.status==='completed'?'':` · <b style="color:${dl<0?'var(--bd)':'var(--g)'}">${nf(Math.abs(dl),0)} ${dl<0?t('tr_over'):t('tr_left')}</b>`;
  return `<div class="stepper">${steps}</div>
  <div class="two" style="margin-top:14px"><div><h3 style="font-size:15px;margin-bottom:8px">${t('tr_checks')}</h3>${c.checks.map(k=>`<div class="ck ${k.level}"><i>${ic[k.level]}</i><span>${t('wk_'+k.key)}</span><em>${t('lv_'+k.level)}</em></div>`).join('')}</div>
  <div><h3 style="font-size:15px;margin-bottom:8px">${t('tr_timeline')}</h3><ul class="tl">${c.timeline.map(e=>`<li><b>${T['sg_'+e.key]?t('sg_'+e.key):t('ev_'+e.key)}</b><small>${fd(e.date)}</small></li>`).join('')}</ul></div></div>
  <p style="color:var(--mu);font-size:13.5px;margin:6px 0 0">${t('tr_sla')}: ${fd(c.sla_due)}${sla}</p>`}
function drawTrack(){const c=TC;if(!c)return;
  $('#tr_out').innerHTML=`<div class="card"><div class="dhead"><div><h2 style="font-size:22px">${c.id}</h2><p style="margin:2px 0 0;color:var(--mu)">${t('tr_plot')} ${X(c.label)} · ${esc(nm(c.district))} · ${t('f_sheet')} ${X(c.sheet)}</p></div><span class="badge ${c.status}">${t('cs_'+c.status)}</span></div>
  <dl class="kv" style="margin-bottom:16px"><dt>${t('tr_buyer')}</dt><dd>${esc(nm(c.buyer))}</dd><dt>${t('tr_seller')}</dt><dd>${esc(nm(c.seller))}</dd><dt>${t('tr_deed')}</dt><dd>${X(c.deed_no)} · ${fd(c.deed_date)}</dd><dt>${t('tr_office')}</dt><dd>${esc(nm(c.office))}</dd></dl>
  ${caseBody(c)}<div class="btnrow" style="margin-top:14px"><a class="btn sm alt" href="#/verify?ds=${encodeURIComponent(c.ds)}&plot=${encodeURIComponent(c.plot_no)}">${ico('search',15)} ${t('d_report')}</a><a class="btn sm alt" href="#/map?ds=${encodeURIComponent(c.ds)}&plot=${encodeURIComponent(c.plot_no)}">${ico('map',15)} ${t('open_map')}</a></div></div>`}

/* ---------- admin ---------- */
let atab='overview',FA=[],fType='',fStat='',WC=[];
function renderAdmin(){
  const root=$('#a_root');
  if(role!=='admin'){root.innerHTML=`<div class="phead"><div class="card empty" style="padding:40px"><h2>${ico('lock',22)} ${t('a_gate_t')}</h2><p>${t('a_gate_d')}</p><button class="btn" id="a_en">${t('a_enable')}</button></div></div>`;
    $('#a_en').onclick=()=>{role='admin';localStorage.role=role;applyStatic();renderAdmin()};return}
  root.innerHTML=`<div class="phead"><h1>${t('a_title')} <span class="badge ${dataMode==='api'?'ok':''}" style="vertical-align:middle">${dataMode==='api'?t('mode_api'):t('mode_dataset')}</span></h1><p><span class="note">${t('a_note')}</span></p></div>
  <div class="tabs">${[['overview','a_overview'],['fraud','a_fraud'],['workflow','a_workflow'],['data','a_data']].map(([k,l])=>`<button data-tab="${k}" class="${k===atab?'on':''}">${t(l)}</button>`).join('')}</div><div id="a_body"><div class="empty">${t('loading')}</div></div>`;
  $$('.tabs button').forEach(b=>b.onclick=()=>{atab=b.dataset.tab;renderAdmin()});
  ({overview:aOverview,fraud:aFraud,workflow:aWorkflow,data:aData})[atab]().catch(e=>$('#a_body').innerHTML=`<div class="card empty">${esc(e.message)}</div>`)}
const bars=(rows)=>{const m=Math.max(1,...rows.map(r=>r[1]));return rows.map(([l,v])=>`<div class="bar"><span>${l}</span><div><i style="width:${v/m*100}%"></i></div><b>${nf(v,0)}</b></div>`).join('')};
async function aOverview(){const o=await api('/api/admin/overview');
  $('#a_body').innerHTML=`<div class="kpis">${[['a_open',o.open_alerts],['a_high',o.high],['a_prog',o.in_progress],['a_flag',o.flagged],['a_avg',o.avg_days]].map(([k,v])=>`<div class="kpi"><b>${nf(v,0)}</b><span>${t(k)}</span></div>`).join('')}</div>
  <div class="two"><div class="card"><h3>${t('a_bytype')}</h3>${bars(FT.map(f=>[t('ft_'+f),o.by_type[f]||0]))}</div>
  <div class="card"><h3>${t('a_funnel')}</h3>${bars(STAGES.map((s,i)=>[t('sg_'+s),o.by_stage[i]]))}</div></div>
  <div class="card" style="margin-top:16px"><h3>${t('a_recent')}</h3>${o.recent.map(a=>`<div class="item"><span class="badge ${a.severity}">${t('sev_'+a.severity)}</span> <b>${t('ft_'+a.type)}</b> · ${a.id} · ${t('v_plot')} ${X(a.label)} · ${esc(nm(a.district))} <span class="badge ${a.status}" style="float:right">${t('st_'+a.status)}</span></div>`).join('')||`<div class="empty">${t('m_none')}</div>`}</div>`}
const STS=['new','investigating','resolved','false_positive'];
async function aFraud(){FA=(await api('/api/admin/fraud')).alerts;drawFraud()}
function drawFraud(){
  const rows=FA.filter(a=>(!fType||a.type===fType)&&(!fStat||a.status===fStat));
  const body=rows.map(a=>`<tr class="r" data-id="${a.id}">
    <td data-l="${t('th_id')}">${a.id}${a.custom?` <span class="badge">${t('a_custom')}</span>`:''}</td>
    <td data-l="${t('th_type')}"><b>${t('ft_'+a.type)}</b></td>
    <td data-l="${t('th_sev')}"><span class="badge ${a.severity}">${t('sev_'+a.severity)}</span></td>
    <td data-l="${t('th_plot')}">${X(a.label)} · ${esc(nm(a.district))} ${X(a.sheet)}</td>
    <td data-l="${t('th_conf')}">${nf(a.confidence,0)}%</td>
    <td data-l="${t('th_date')}">${fd(a.detected)}</td>
    <td data-l="${t('th_status')}"><select data-s="${a.id}">${STS.map(s=>`<option value="${s}" ${a.status===s?'selected':''}>${t('st_'+s)}</option>`).join('')}</select></td></tr>
  <tr class="det" hidden><td colspan="7"><b>${t('a_evidence')}</b><ul class="adv">${a.evidence.map(e=>`<li>${X(nm(e))}</li>`).join('')}</ul>
    <div class="btnrow"><a class="btn sm alt" href="#/map?ds=${a.ds}&plot=${encodeURIComponent(a.plot_no)}">${ico('map',15)} ${t('open_map')}</a><a class="btn sm alt" href="#/verify?ds=${a.ds}&plot=${encodeURIComponent(a.plot_no)}">${ico('search',15)} ${t('d_report')}</a>${a.custom?`<button class="btn sm danger" data-delf="${a.id}">${ico('trash',15)} ${t('a_delete')}</button>`:''}</div></td></tr>`).join('');
  $('#a_body').innerHTML=`<div class="toolbar"><div class="filters"><button class="fchip ${fType?'':'on'}" data-t="">${t('a_all')}</button>${FT.map(f=>`<button class="fchip ${fType===f?'on':''}" data-t="${f}">${t('ft_'+f)}</button>`).join('')}
  <select id="fs"><option value="">${t('th_status')}: ${t('a_all')}</option>${STS.map(s=>`<option value="${s}" ${fStat===s?'selected':''}>${t('st_'+s)}</option>`).join('')}</select></div>
  <button class="btn" id="a_newf">${ico('plus',16)} ${t('a_new_fraud')}</button></div>
  <div class="card tbl" style="padding:6px"><table class="rt"><thead><tr><th>${t('th_id')}</th><th>${t('th_type')}</th><th>${t('th_sev')}</th><th>${t('th_plot')}</th><th>${t('th_conf')}</th><th>${t('th_date')}</th><th>${t('th_status')}</th></tr></thead><tbody>${body||`<tr><td colspan="7" class="empty">${t('m_none')}</td></tr>`}</tbody></table></div>`;
  $('#a_newf').onclick=()=>openCreate('fraud');
  $$('.filters .fchip').forEach(b=>b.onclick=()=>{fType=b.dataset.t;drawFraud()});$('#fs').onchange=e=>{fStat=e.target.value;drawFraud()};
  $$('tr.r').forEach(r=>r.onclick=e=>{if(e.target.closest('select'))return;r.nextElementSibling.hidden=!r.nextElementSibling.hidden});
  $$('td select').forEach(sl=>sl.onchange=async()=>{try{const j=await api('/api/admin/fraud/'+sl.dataset.s,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:sl.value})});FA=FA.map(a=>a.id===j.alert.id?j.alert:a);toast(t('a_updated'))}catch(e){toast(e.message)}});
  $$('[data-delf]').forEach(b=>b.onclick=async()=>{if(!confirm(t('a_confirm_del')))return;
    try{await api('/api/admin/fraud/'+b.dataset.delf,{method:'DELETE'});FA=FA.filter(a=>a.id!==b.dataset.delf);STATS=await api('/api/stats');toast(t('a_deleted'));drawFraud()}catch(e){toast(e.message)}})}
async function aWorkflow(){WC=(await api('/api/admin/workflow')).cases;drawBoard()}
function drawBoard(){
  $('#a_body').innerHTML=`<div class="toolbar" style="justify-content:flex-end"><button class="btn" id="a_newc">${ico('plus',16)} ${t('a_new_case')}</button></div>
  <div class="board">${STAGES.map((s,i)=>{const cs=WC.filter(c=>c.stage===i);return `<div class="col"><h4><span>${t('sg_'+s)}</span><span>${nf(cs.length,0)}</span></h4>${cs.map(c=>`<div class="kcard ${c.status}" data-id="${c.id}"><b>${c.id}${c.custom?` <span class="badge">${t('a_custom')}</span>`:''}</b>${t('v_plot')} ${X(c.label)} · ${esc(nm(c.district))}<br><small>${esc(nm(c.buyer))}</small><div class="dots">${c.checks.map(k=>`<i class="${k.level}"></i>`).join('')}</div><div style="margin-top:6px"><span class="badge ${c.status}">${t('cs_'+c.status)}</span></div></div>`).join('')}</div>`}).join('')}</div>`;
  $('#a_newc').onclick=()=>openCreate('case');
  $$('.kcard').forEach(k=>k.onclick=()=>openCase(k.dataset.id))}
function openCase(id){const c=WC.find(x=>x.id===id);if(!c)return;const d=$('#dlg');
  d.innerHTML=`<div class="dhead"><div><h2 style="font-size:20px">${c.id} <span class="badge ${c.status}">${t('cs_'+c.status)}</span>${c.custom?` <span class="badge">${t('a_custom')}</span>`:''}</h2><p style="margin:2px 0 0;color:var(--mu)">${t('tr_plot')} ${X(c.label)} · ${esc(nm(c.district))} · ${esc(nm(c.buyer))} ← ${esc(nm(c.seller))}</p></div><button class="icon" id="dc" aria-label="${t('close')}">${ico('x')}</button></div>
  ${caseBody(c)}<div class="btnrow" style="margin-top:16px"><button class="btn sm" data-a="advance">${ico('play',14)} ${t('a_advance')}</button><button class="btn sm alt" data-a="hold">${ico('pause',14)} ${t('a_hold')}</button><button class="btn sm alt" data-a="release">${ico('okc',15)} ${t('a_release')}</button><button class="btn sm danger" data-a="reject">${ico('ban',15)} ${t('a_reject')}</button>
  <a class="btn sm alt" href="#/verify?ds=${encodeURIComponent(c.ds)}&plot=${encodeURIComponent(c.plot_no)}" onclick="$('#dlg').close()">${ico('search',15)} ${t('d_report')}</a>${c.custom?`<button class="btn sm danger" id="dcdel">${ico('trash',15)} ${t('a_delete')}</button>`:''}</div>`;
  $('#dc').onclick=()=>d.close();
  if(c.custom)$('#dcdel').onclick=async()=>{if(!confirm(t('a_confirm_del')))return;
    try{await api('/api/admin/workflow/'+c.id,{method:'DELETE'});WC=WC.filter(x=>x.id!==c.id);STATS=await api('/api/stats');d.close();toast(t('a_deleted'));drawBoard()}catch(e){toast(e.message)}};
  $$('#dlg [data-a]').forEach(b=>b.onclick=async()=>{try{const j=await api('/api/admin/workflow/'+c.id,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:b.dataset.a})});
    WC=WC.map(x=>x.id===c.id?j.case:x);drawBoard();openCase(c.id)}catch(e){toast(e.message)}});
  if(!d.open)d.showModal()}
/* add a fraud alert / mutation case for any plot of the ACTIVE data mode */
function openCreate(kind,pre={}){
  if(!DSL.length)return toast(t('a_noplots'));
  const d=$('#dlg'),isF=kind==='fraud',ds0=pre.ds&&DSL.some(x=>x.id===pre.ds)?pre.ds:DSL[0].id,today=new Date().toISOString().slice(0,10);
  const fields=isF?`<label class="f"><span>${t('a_ftype')}</span><select id="n_type">${FT.map(f=>`<option value="${f}">${t('ft_'+f)}</option>`).join('')}</select></label>
    <label class="f"><span>${t('a_sev')}</span><select id="n_sev"><option value="">${t('a_sev_auto')}</option><option value="high">${t('sev_high')}</option><option value="medium">${t('sev_medium')}</option></select></label>
    <label class="f"><span>${t('a_conf')}</span><input id="n_conf" type="number" min="1" max="99" value="85" inputmode="numeric"></label>
    <label class="f full"><span>${t('a_note_l')}</span><textarea id="n_note" rows="3" maxlength="300"></textarea></label>`
  :`<label class="f"><span>${t('a_buyer')}</span><input id="n_buyer" maxlength="80" autocomplete="off"></label>
    <label class="f"><span>${t('a_seller')}</span><input id="n_seller" maxlength="80" autocomplete="off"></label>
    <label class="f"><span>${t('a_deed_no')}</span><input id="n_deed" maxlength="30" autocomplete="off"></label>
    <label class="f"><span>${t('a_deed_date')}</span><input id="n_date" type="date" value="${today}" max="${today}"></label>`;
  d.innerHTML=`<div class="dhead"><div><h2 style="font-size:20px">${isF?t('a_new_fraud'):t('a_new_case')}</h2><p style="margin:2px 0 0;color:var(--mu);font-size:13.5px">${t('a_mode_scope')} · <b>${dataMode==='api'?t('mode_api'):t('mode_dataset')}</b></p></div><button class="icon" id="dc" aria-label="${t('close')}">${ico('x')}</button></div>
  <div class="fgrid"><label class="f full"><span>${t('a_ds')}</span><select id="n_ds">${DSL.map(x=>`<option value="${esc(x.id)}" ${x.id===ds0?'selected':''}>${esc(dn(x))} · ${esc(shl(x))} (${nf(x.plots,0)})</option>`).join('')}</select></label>
  <label class="f full"><span>${t('f_plot')}</span><input id="n_plot" list="n_plots" autocomplete="off" value="${esc(pre.plot||'')}" placeholder="${esc(t('a_plot_hint'))}"><datalist id="n_plots"></datalist></label>${fields}</div>
  <div class="btnrow" style="margin-top:16px;justify-content:flex-end"><button class="btn alt" id="n_cancel">${t('a_cancel')}</button><button class="btn" id="n_go">${ico('plus',16)} ${t('a_create')}</button></div>`;
  const fp=()=>fillPlots($('#n_plots'),$('#n_ds').value);fp();$('#n_ds').onchange=()=>{$('#n_plot').value='';fp()};
  $('#dc').onclick=$('#n_cancel').onclick=()=>d.close();
  $('#n_go').onclick=async()=>{const ds=$('#n_ds').value,plot_no=$('#n_plot').value.trim();if(!ds||!plot_no)return toast(t('a_need_plot'));
    const body=isF?{ds,plot_no,type:$('#n_type').value,severity:$('#n_sev').value,confidence:+$('#n_conf').value||85,note:$('#n_note').value}
      :{ds,plot_no,buyer:$('#n_buyer').value,seller:$('#n_seller').value,deed_no:$('#n_deed').value,deed_date:$('#n_date').value};
    $('#n_go').disabled=true;
    try{await api(isF?'/api/admin/fraud':'/api/admin/workflow',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      d.close();toast(t('a_created'));STATS=await api('/api/stats');
      if(cur.path.startsWith('/admin')){atab=isF?'fraud':'workflow';renderAdmin()}}
    catch(e){toast(e.message);$('#n_go').disabled=false}};
  if(!d.open)d.showModal()}
async function aData(){const L=await api('/api/datasets'),isApi=dataMode==='api';
  $('#a_body').innerHTML=`${L.some(d=>d.fallback)?`<p><span class="note">${t('api_fb')}</span></p>`:''}
  <div class="card tbl" style="padding:6px"><table class="rt"><thead><tr><th>${t('th_ds')}</th><th>${t('th_src')}</th><th>${t('f_district')}</th><th>${t('f_sheet')} / ${t('f_mouza')}</th><th>${t('th_plots')}</th><th>${t('th_parts')}</th><th>${t('th_area')}</th><th></th></tr></thead><tbody>
  ${L.map(d=>`<tr class="r"><td data-l="${t('th_ds')}"><code>${esc(d.id)}</code></td><td data-l="${t('th_src')}"><span class="badge ${d.source==='api'?'ok':''}">${t('src_'+d.source)}</span></td><td data-l="${t('f_district')}">${esc(dn(d))}</td><td data-l="${t('f_mouza')}">${d.source==='api'?esc(shl(d)):X(d.sheet)}</td><td data-l="${t('th_plots')}">${nf(d.plots,0)}</td><td data-l="${t('th_parts')}">${nf(d.parts,0)}</td><td data-l="${t('th_area')}">${nf(dec(d.area_sqm),1)}</td>
  <td><div class="btnrow"><a class="btn sm alt" href="#/map?ds=${esc(d.id)}" aria-label="${t('open_map')}">${ico('map',15)}</a><button class="btn sm alt" data-af="${esc(d.id)}">${ico('alert',15)} ${t('a_add_alert')}</button><button class="btn sm alt" data-ac="${esc(d.id)}">${ico('clipboard',15)} ${t('a_add_case')}</button>${d.source==='api'?`<button class="btn sm danger" data-del="${esc(d.id)}" aria-label="${t('a_delete')}">${ico('trash',15)}</button>`:''}</div></td></tr>`).join('')}</tbody></table></div>
  ${isApi?`<div class="card" style="margin-top:16px"><h3>${t('a_api_h')}</h3>${apiBoxHTML('a_')}<div class="btnrow" style="margin-top:8px"><button class="btn alt" id="a_apire">${ico('refresh',16)} ${t('a_reload')}</button></div></div>`
  :`<div class="card" style="margin-top:16px"><h3>${t('a_upload_h')}</h3><p style="color:var(--mu);font-size:14px;margin-top:0">${t('a_upload_help')}</p>
  <div class="formrow up"><label class="f"><span>${t('a_name')}</span><input id="u_name" placeholder="gazipur_002"></label>
  <label class="f"><span>${t('a_land_f')}</span><input type="file" id="u_land" accept=".json"></label><label class="f"><span>${t('a_plot_f')}</span><input type="file" id="u_plot" accept=".json"></label>
  <button class="btn" id="u_go">${t('a_upload_btn')}</button></div></div>`}`;
  $$('[data-af]').forEach(b=>b.onclick=()=>openCreate('fraud',{ds:b.dataset.af}));
  $$('[data-ac]').forEach(b=>b.onclick=()=>openCreate('case',{ds:b.dataset.ac}));
  $$('[data-del]').forEach(b=>b.onclick=async()=>{if(!confirm(t('a_confirm')))return;
    try{await api('/api/admin/dataset/'+b.dataset.del,{method:'DELETE'});await refreshData();toast(t('a_deleted'));aData()}catch(e){toast(e.message)}});
  if(isApi){bindApi('a_');const go=async f=>{const id=await loadApiArea('a_',f);if(id)aData()};$('#a_apiload').onclick=()=>go(false);$('#a_apire').onclick=()=>go(true)}
  else $('#u_go').onclick=async()=>{const n=$('#u_name').value.trim().toLowerCase(),fs=['land','plot'].map(k=>[k,$('#u_'+k).files[0]]).filter(x=>x[1]);
    if(!fs.length)return toast(t('a_pick'));try{for(const[k,f]of fs)await api(`/api/admin/upload/${encodeURIComponent(n)}/${k}`,{method:'POST',body:await f.text()});toast(t('a_uploaded'));await refreshData();aData()}catch(e){toast(e.message)}}}

/* ---------- boot ---------- */
(async()=>{applyStatic();
  try{await refreshData()}catch(e){toast(e.message)}
  route(false)})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--dev", action="store_true", help="disable the devtools deterrent while developing")
    a = ap.parse_args()
    HARDEN["on"] = not a.dev
    write_demo()
    print(f"আমার ভূমি  ->  http://{a.host}:{a.port}   (data folder: {DATA_DIR}, API areas: {API_DIR})")
    if not a.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{a.host}:{a.port}")).start()
    app.run(host=a.host, port=a.port, debug=False, threaded=True)