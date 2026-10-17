#!/usr/bin/env python3
"""
ভূমি দর্পণ / Bhumi Dorpon  —  Bangladesh Land Portal  (Flask + Leaflet)  MVP
===========================================================================
    pip install flask
    python land_portal.py                 # http://127.0.0.1:5000
    python land_portal.py --port 8000 --no-browser

Data files (auto-discovered, hot-reloaded):
    data/<district>_<sheet>_land.json     e.g. data/gazipur_001_land.json
    data/<district>_<sheet>_plot.json     e.g. data/gazipur_001_plot.json
  (files next to this script, and legacy land.json / plot.json, work too.)
  Records: {"geom": "POLYGON((lon lat, ...))", "plot_no_en": "123", "label": "১২৩",
            "m_code": "3601", "maj_class": "Settlement", "sub_class": "Homestead", ...}
  If no data exists, 3 demo datasets are generated on first start.
  Government map lookups are available through the server-side /api/bd/* proxy.
  The proxy obtains a fresh CSRF token automatically; set BD_MAP_URL to override
  the upstream service URL.

Roles (MVP, no login):  User = public pages.  Admin = switch in the header -> #/admin
Ownership, cases, fraud alerts and mutation workflow are DUMMY data (deterministic).
"""
import argparse
import hashlib
import json
import math
import os
import random
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zlib
from datetime import date, timedelta

from flask import Flask, Response, jsonify, redirect, request

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
os.makedirs(DATA_DIR, exist_ok=True)
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 80 * 1024 * 1024
TODAY = date.today()
STATE = {"fraud": {}, "wf": {}}  # in-memory admin actions (MVP)

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


def _bd_request(path, fields):
    """POST to the government map service using a fresh Laravel CSRF session."""
    with _bd_lock:
        if not _bd_session["token"] or not _bd_session["cookies"]:
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
            page = opener.open(BD_MAP_URL + "/", timeout=15).read().decode("utf-8", "replace")
            match = re.search(r'<meta\s+name=["\']csrf-token["\']\s+content=["\']([^"\']+)', page, re.I)
            if not match:
                raise RuntimeError("Government map service did not provide a CSRF token")
            _bd_session["token"] = match.group(1)
            _bd_session["cookies"] = opener
        opener = _bd_session["cookies"]
        data = dict(fields)
        data["_token"] = _bd_session["token"]
        req = urllib.request.Request(
            BD_MAP_URL + path,
            data=urllib.parse.urlencode(data).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "X-CSRF-TOKEN": _bd_session["token"],
                "X-Requested-With": "XMLHttpRequest",
            },
            method="POST",
        )
        try:
            with opener.open(req, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 419:
                _bd_session.update(token=None, cookies=None)
            raise RuntimeError(f"Government map service returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
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


# ----------------------------------------------------------------- dataset discovery / loading
NAME_RE = re.compile(r"^(?P<id>[A-Za-z0-9][A-Za-z0-9_\-]*)_(?P<kind>land|plot)\.json$", re.I)
SPLIT_RE = re.compile(r"^(?P<d>.+?)[_-](?P<s>\d+)$")


def discover():
    found = {}
    for folder in (DATA_DIR, BASE):
        for fn in sorted(os.listdir(folder)):
            m = NAME_RE.match(fn)
            if m:
                found.setdefault(m["id"].lower(), {}).setdefault(m["kind"].lower(), os.path.join(folder, fn))
            elif folder == BASE and fn.lower() in ("land.json", "plot.json"):
                found.setdefault("default", {}).setdefault(fn[:4].lower(), os.path.join(folder, fn))
    return found


def label_of(ds):
    m = SPLIT_RE.match(ds)
    d, s = (m["d"], m["s"]) if m else (ds, "")
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
    parts = []
    for ds, files in sorted(discover().items()):
        for k, p in sorted(files.items()):
            st = os.stat(p)
            parts.append(f"{ds}{k}{st.st_mtime_ns}{st.st_size}")
    return hashlib.md5("|".join(parts).encode()).hexdigest()[:12]


_sum = {"sig": None, "list": []}


def summaries():
    sig = signature()
    if _sum["sig"] == sig:
        return _sum["list"]
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
        out.append({
            "id": ds, "district": d, "sheet": s, "district_bn": bn, "district_en": en,
            "plots": len(plot), "parts": len(land),
            "area_sqm": round(sum(f["properties"]["area_sqm"] for f in (plot or land)), 1),
            "bbox": bb, "center": center,
        })
    _sum.update(sig=sig, list=out)
    return out


# ----------------------------------------------------------------- plot index + dummy records
_idx = {"sig": None, "plots": {}, "parts": {}}


def index():
    sig = signature()
    if _idx["sig"] == sig:
        return _idx
    plots, parts = {}, {}
    for ds, files in sorted(discover().items()):
        try:
            if "plot" in files:
                for i, f in enumerate(load_file(files["plot"], ds)["features"]):
                    p = f["properties"]
                    no = str(p.get("plot_no_en", p.get("label", i)))
                    c = centroid_of(f["geometry"])
                    plots[(ds, no)] = {"ds": ds, "idx": i, "props": p, "geom": f["geometry"], "lat": c[1], "lng": c[0]}
            if "land" in files:
                for f in load_file(files["land"], ds)["features"]:
                    p = f["properties"]
                    parts.setdefault((ds, str(p.get("plot_no_en", ""))), []).append(
                        {"maj": p.get("maj_class"), "sub": p.get("sub_class"), "area_sqm": p["area_sqm"]})
        except Exception:
            continue
    _idx.update(sig=sig, plots=plots, parts=parts)
    return _idx


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


_der = {"sig": None}


def derived():
    idx = index()
    if _der["sig"] == idx["sig"]:
        return _der
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
    cases, n = {}, 0
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
        cid = f"MUT-{TODAY.year}-{n:05d}"
        cases[cid] = {
            "id": cid, "ds": ds, "plot_no": no, "label": pl["props"].get("label", no), "sheet": s,
            "district": {"bn": bn, "en": en}, "buyer": person(rng), "seller": person(rng),
            "deed_no": f"{rng.randint(1000, 9999)}/{created.year}", "deed_date": created.isoformat(),
            "office": {"bn": f"{bn} সাব-রেজিস্ট্রি অফিস", "en": f"{en} Sub-Registry Office"},
            "created": created.isoformat(), "sla_due": (created + timedelta(days=28)).isoformat(),
            "stage": stage, "status": status, "timeline": tl,
            "checks": [{"key": k, "level": v if stage >= 1 else "queued"} for k, v in checks],
        }
    _der.update(sig=idx["sig"], alerts=alerts, cases=cases, by_plot=by_plot)
    return _der


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
    v["days_left"] = (date.fromisoformat(c["sla_due"]) - TODAY).days
    return v


def record(key):
    pl = index()["plots"].get(key)
    if not pl:
        return None
    ds, no = key
    p = pl["props"]
    rng = random.Random(f"{ds}:{no}")
    owner = person(rng)  # first draw: owner_of() relies on it
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
        "parts": index()["parts"].get(key, []), "geometry": pl["geom"], "center": [pl["lat"], pl["lng"]],
    }


# ----------------------------------------------------------------- API
def err(msg, code=400):
    return jsonify(error=msg), code


@app.route("/api/datasets")
def api_datasets():
    return jsonify(summaries())


@app.route("/api/bd/divisions")
def api_bd_divisions():
    try:
        return jsonify(_bd_request("/getAdminByCode", {
            "level": "forDiv",
            "column": "division_n",
        }))
    except RuntimeError as exc:
        return err(str(exc), 502)


@app.route("/api/bd/districts")
def api_bd_districts():
    division = request.args.get("division_code", "").strip()
    if not re.fullmatch(r"\d+", division):
        return err("division_code must be numeric")
    try:
        return jsonify(_bd_request("/getAdminByCode", {
            "level": "forDist",
            "division_code": division,
        }))
    except RuntimeError as exc:
        return err(str(exc), 502)


@app.route("/api/bd/thanas")
def api_bd_thanas():
    division, district = (request.args.get(k, "").strip() for k in ("division_code", "district_code"))
    if not re.fullmatch(r"\d+", division) or not re.fullmatch(r"\d+", district):
        return err("division_code and district_code must be numeric")
    try:
        return jsonify(_bd_request("/getAdminByCode", {
            "level": "forThana",
            "division_code": division,
            "district_code": district,
        }))
    except RuntimeError as exc:
        return err(str(exc), 502)


@app.route("/api/bd/mouzas")
def api_bd_mouzas():
    division, district, thana = (request.args.get(k, "").strip()
                                 for k in ("division_code", "district_code", "thana_code"))
    if not all(re.fullmatch(r"\d+", value) for value in (division, district, thana)):
        return err("division_code, district_code and thana_code must be numeric")
    try:
        return jsonify(_bd_request("/getAdminByCode", {
            "level": "forMauza",
            "division_code": division,
            "district_code": district,
            "thana_code": thana,
        }))
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
        return jsonify(land=fc_of(land), plot=fc_of(plot), ids=ids, sig=signature())
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
        hay = f"{no} {p.get('label', '')} {p.get('m_code', '')} {owner['bn']} {owner['en']}".lower()
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
        st["log"].append({"date": TODAY.isoformat(), "key": STAGES[st["stage"]]})
    else:
        st["status"] = {"hold": "flagged", "release": "ok", "reject": "rejected"}[act]
        st["log"].append({"date": TODAY.isoformat(), "key": act})
    return jsonify(case=case_view(c))


@app.route("/api/admin/upload/<ds>/<kind>", methods=["POST"])
def api_admin_upload(ds, kind):
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
    return Response(PAGE, mimetype="text/html")


# ----------------------------------------------------------------- demo data
DEMO = [("gazipur_001", 24.0023, 90.4264, "গাজীপুর সদর"), ("dhaka_014", 23.8583, 90.2667, "সাভার"),
        ("cumilla_007", 23.4607, 91.1809, "কুমিল্লা সদর")]
CLASSES = [("Settlement", "Homestead"), ("Agriculture", "Paddy Field"), ("Commercial Area", "Shop"),
           ("Water Body", "Pond"), ("Vacant Land", "Fallow"), ("Industrial Area", "Garment Factory")]


def write_demo():
    if discover():
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
<title>ভূমি দর্পণ</title>
<script>try{document.documentElement.dataset.theme=localStorage.theme||(matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light')}catch(e){}</script>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Hind+Siliguri:wght@400;500;600;700&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
:root{--nav:64px;--bg:#f3f8f4;--sf:#fff;--sf2:#eef5f0;--tx:#0f2218;--mu:#5a6f62;--ln:#dce8e0;--g:#0b7d4c;--g2:#16a96b;--gt:#e1f4e8;--on:#fff;
--wr:#b45309;--wrt:#fef3c7;--bd:#b91c1c;--bdt:#fee2e2;--sh:0 10px 30px rgba(8,50,30,.10);--glass:rgba(255,255,255,.86)}
[data-theme=dark]{--bg:#07120c;--sf:#0e1d15;--sf2:#13271c;--tx:#e6f3ea;--mu:#8ba596;--ln:#1e3a2b;--g:#34d58f;--g2:#1fb673;--gt:#103223;--on:#04210f;
--wr:#fbbf24;--wrt:#3a2e0b;--bd:#f87171;--bdt:#3b1414;--sh:0 10px 30px rgba(0,0,0,.45);--glass:rgba(14,29,21,.88)}
*{box-sizing:border-box}
html,body{margin:0;min-height:100%}
body{font-family:'Hind Siliguri','Inter',system-ui,sans-serif;background:var(--bg);color:var(--tx);line-height:1.55;font-size:16px}
a{color:inherit;text-decoration:none}
h1,h2,h3,h4{margin:0;line-height:1.25}
[hidden]{display:none!important}
.container{max-width:1160px;margin:0 auto;padding:0 20px}
/* nav */
.nav{position:sticky;top:0;z-index:2000;height:var(--nav);background:var(--glass);backdrop-filter:blur(14px);border-bottom:1px solid var(--ln)}
.nav-in{height:100%;max-width:1280px;margin:0 auto;padding:0 18px;display:flex;align-items:center;gap:14px}
.brand{display:flex;align-items:center;gap:10px;font-weight:700;font-size:19px;color:var(--g)}
.logo{width:36px;height:36px;border-radius:11px;background:linear-gradient(135deg,var(--g),var(--g2));display:grid;place-items:center;font-size:19px}
.nav-links{display:flex;align-items:center;gap:4px;margin-left:18px;flex:1}
.nav-links a{padding:8px 13px;border-radius:10px;color:var(--mu);font-weight:500;font-size:15px}
.nav-links a:hover{background:var(--sf2);color:var(--tx)}.nav-links a.on{background:var(--gt);color:var(--g)}
.tools{display:flex;gap:8px;align-items:center}
.seg{display:inline-flex;background:var(--sf2);border:1px solid var(--ln);border-radius:11px;padding:3px}
.seg button{border:0;background:none;color:var(--mu);padding:5px 11px;border-radius:8px;font:inherit;font-size:13px;font-weight:600;cursor:pointer}
.seg button.on{background:var(--g);color:var(--on)}
.icon{width:38px;height:38px;border-radius:11px;border:1px solid var(--ln);background:var(--sf);color:var(--tx);cursor:pointer;font-size:17px}
.burger{display:none}
#roleSeg{margin-left:auto}
/* buttons / forms */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;padding:11px 18px;border-radius:12px;border:1px solid transparent;background:var(--g);color:var(--on);font:inherit;font-weight:600;font-size:15px;cursor:pointer;transition:.15s}
.btn:hover{filter:brightness(1.08);transform:translateY(-1px)}
.btn.alt{background:var(--sf);color:var(--tx);border-color:var(--ln)}.btn.sm{padding:7px 12px;font-size:13px;border-radius:10px}
.btn.danger{background:var(--bd);color:#fff}
.btnrow{display:flex;gap:8px;flex-wrap:wrap}
input,select{font:inherit;color:var(--tx);background:var(--sf2);border:1px solid var(--ln);border-radius:11px;padding:10px 12px;width:100%;outline:none;min-width:0}
input:focus,select:focus{border-color:var(--g2);box-shadow:0 0 0 3px color-mix(in srgb,var(--g2) 22%,transparent)}
label.f{display:block;font-size:13px;color:var(--mu);font-weight:500}label.f>*:last-child{margin-top:5px}
.card{background:var(--sf);border:1px solid var(--ln);border-radius:18px;padding:20px;box-shadow:var(--sh)}
.badge{display:inline-block;padding:2px 10px;border-radius:99px;font-size:12px;font-weight:600;background:var(--sf2);color:var(--mu);white-space:nowrap}
.badge.ok,.badge.pass,.badge.safe,.badge.completed,.badge.resolved{background:var(--gt);color:var(--g)}
.badge.warn,.badge.caution,.badge.medium,.badge.delayed,.badge.investigating{background:var(--wrt);color:var(--wr)}
.badge.fail,.badge.danger,.badge.high,.badge.flagged,.badge.rejected,.badge.new{background:var(--bdt);color:var(--bd)}
/* home */
.hero{background:radial-gradient(1000px 500px at 85% -10%,color-mix(in srgb,var(--g2) 25%,transparent),transparent),linear-gradient(180deg,var(--gt),var(--bg));padding:54px 0 36px}
.hero-in{display:grid;grid-template-columns:1.15fr .85fr;gap:36px;align-items:center}
.pill{display:inline-block;background:var(--sf);border:1px solid var(--ln);color:var(--g);padding:5px 14px;border-radius:99px;font-size:13px;font-weight:600}
.hero h1{font-size:clamp(30px,4.4vw,50px);margin:16px 0 12px;font-weight:700}
.hero p{color:var(--mu);font-size:18px;margin:0 0 22px;max-width:560px}
.searchcard{display:grid;gap:14px}
.row3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px}.row2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.or{display:flex;align-items:center;gap:10px;color:var(--mu);font-size:12px}.or:before,.or:after{content:'';flex:1;height:1px;background:var(--ln)}
.placebox{display:flex;gap:8px}
.hero-art{position:relative;min-height:330px}
.mock{display:grid;grid-template-columns:repeat(4,1fr);gap:7px;transform:rotate(-4deg);padding:16px;background:var(--sf);border:1px solid var(--ln);border-radius:24px;box-shadow:var(--sh)}
.mock i{display:block;aspect-ratio:1.15;border-radius:9px;border:2px solid #ffd60a;background:var(--g2);opacity:.85}
.mock i:nth-child(3n){background:#f4a261}.mock i:nth-child(5n){background:#8ab17d}.mock i:nth-child(7n){background:#38bdf8}.mock i:nth-child(4){background:#e63946}
.float{position:absolute;background:var(--sf);border:1px solid var(--ln);border-radius:14px;padding:10px 14px;font-weight:600;font-size:14px;box-shadow:var(--sh)}
.f1{left:-14px;bottom:44px;color:var(--g)}.f2{right:-6px;top:18px;color:var(--bd)}
.stats{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-top:-8px;padding-top:6px}
.stat{background:var(--sf);border:1px solid var(--ln);border-radius:16px;padding:16px;text-align:center}
.stat b{display:block;font-size:26px;color:var(--g)}.stat span{font-size:13px;color:var(--mu)}
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
.note{background:var(--wrt);color:var(--wr);border-radius:12px;padding:10px 14px;font-size:13.5px}
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
.rw{display:flex;align-items:center;gap:8px;padding:6px 6px;border-radius:9px;font-size:13px;cursor:pointer}.rw:hover{background:var(--sf2)}
.sw{width:14px;height:14px;border-radius:4px;flex:none;border:1px solid rgba(0,0,0,.2)}.rw .n{margin-left:auto;color:var(--mu);font-size:12px;white-space:nowrap}
.chk{display:flex;align-items:center;gap:8px;padding:4px 2px;font-size:13.5px;cursor:pointer}.chk input{width:auto}
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
@media(max-width:960px){.hero-in{grid-template-columns:1fr}.hero-art{display:none}.stats{grid-template-columns:repeat(3,1fr)}.grid4{grid-template-columns:repeat(2,1fr)}.grid3{grid-template-columns:1fr}.kpis{grid-template-columns:repeat(3,1fr)}.formrow{grid-template-columns:1fr 1fr}}
@media(max-width:760px){.burger{display:block}.nav-links{display:none;position:absolute;top:var(--nav);left:0;right:0;background:var(--sf);border-bottom:1px solid var(--ln);flex-direction:column;align-items:stretch;padding:10px;margin:0}
.nav-links.open{display:flex}#roleSeg{margin:6px 0 0;align-self:flex-start}.brand span:last-child{font-size:17px}
.row3{grid-template-columns:1fr}.stats{grid-template-columns:repeat(2,1fr)}.grid4{grid-template-columns:1fr}.vgrid,.two{grid-template-columns:1fr}.vbanner{flex-direction:column;text-align:center}.kpis{grid-template-columns:repeat(2,1fr)}.formrow{grid-template-columns:1fr}.bar{grid-template-columns:110px 1fr 30px}
#panel{width:auto;right:12px;bottom:auto;max-height:46%}#panel.hide{transform:translateY(-120%)}#ptoggle{left:auto;right:12px;top:auto;bottom:12px}#panel.hide+#ptoggle{left:auto}
#drawer{top:auto;left:12px;right:12px;bottom:12px;width:auto;max-height:50%}.fabs{bottom:auto;top:calc(46% + 24px)}}
@media print{.nav,.btnrow,#toast,footer,.tabs{display:none!important}.view:not(#v-verify){display:none!important}body{background:#fff}}
</style>
</head>
<body>
<header class="nav"><div class="nav-in">
  <a class="brand" href="#/"><span class="logo">🌿</span><span data-i18n="brand"></span></a>
  <nav class="nav-links" id="links">
    <a href="#/" data-r="home" data-i18n="nav_home"></a>
    <a href="#/map" data-r="map" data-i18n="nav_map"></a>
    <a href="#/verify" data-r="verify" data-i18n="nav_verify"></a>
    <a href="#/track" data-r="track" data-i18n="nav_track"></a>
    <a href="#/admin" data-r="admin" id="navAdmin" data-i18n="nav_admin"></a>
    <div class="seg" id="roleSeg"><button data-role="user" data-i18n="role_user"></button><button data-role="admin" data-i18n="role_admin"></button></div>
  </nav>
  <div class="tools">
    <span class="mode-label" data-i18n="mode_label"></span><div class="seg" id="modeSeg"><button data-mode="dataset" data-i18n="mode_dataset"></button><button data-mode="api" data-i18n="mode_api"></button></div>
    <div class="seg" id="langSeg"><button data-lang="bn">বাং</button><button data-lang="en">EN</button></div>
    <button class="icon" id="themeBtn" title="Theme">🌓</button>
    <button class="icon burger" id="burger">☰</button>
  </div>
</div></header>

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
            <label class="f"><span data-i18n="f_district"></span><select id="h_api_dist"></select></label>
            <label class="f"><span data-i18n="f_thana"></span><select id="h_thana"></select></label>
          </div>
          <div class="row3 api-fields">
            <label class="f"><span data-i18n="f_mouza"></span><select id="h_mouza"></select></label>
          </div>
        </div>
        <div class="btnrow"><button class="btn" id="h_map" data-i18n="btn_map"></button><button class="btn alt" id="h_ver" data-i18n="btn_verify"></button></div>
        <div class="or"><span data-i18n="or_place"></span></div>
        <div class="placebox"><input id="h_place" data-ph="place_ph"><button class="btn" id="h_go">📍</button></div>
      </div>
    </div>
    <div class="hero-art"><div class="mock"><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i></div>
      <div class="float f1">✓ <span data-i18n="mock_safe"></span></div><div class="float f2">⚠ <span data-i18n="mock_alert"></span></div></div>
  </div></div>
  <div class="container stats" id="h_stats"></div>
  <div class="sec"><div class="container"><h2 data-i18n="feat_h"></h2><p data-i18n="feat_p"></p>
    <div class="grid4">
      <div class="card feat" data-go="#/map"><div class="ic">🗺️</div><h3 data-i18n="feat_map_t"></h3><p data-i18n="feat_map_d"></p></div>
      <div class="card feat" data-go="#/verify"><div class="ic">🔎</div><h3 data-i18n="feat_ver_t"></h3><p data-i18n="feat_ver_d"></p></div>
      <div class="card feat" data-go="#/track"><div class="ic">📑</div><h3 data-i18n="feat_mut_t"></h3><p data-i18n="feat_mut_d"></p></div>
      <div class="card feat" data-go="#/admin"><div class="ic">🛡️</div><h3 data-i18n="feat_fraud_t"></h3><p data-i18n="feat_fraud_d"></p></div>
    </div></div></div>
  <div class="container"><div class="band"><h2 data-i18n="fraud_h"></h2><div class="grid4">
    <div><h4 data-i18n="ft_fake_deed"></h4><p data-i18n="ftd_fake_deed"></p></div>
    <div><h4 data-i18n="ft_double_sale"></h4><p data-i18n="ftd_double_sale"></p></div>
    <div><h4 data-i18n="ft_rapid"></h4><p data-i18n="ftd_rapid"></p></div>
    <div><h4 data-i18n="ft_mismatch"></h4><p data-i18n="ftd_mismatch"></p></div></div></div></div>
  <div class="sec"><div class="container"><h2 data-i18n="steps_h"></h2><p></p><div class="grid3">
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
      <div class="row2"><select id="m_dist"></select><select id="m_sheet"></select></div>
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
  <button class="icon" id="ptoggle">☰</button>
  <div class="fabs"><button class="icon" id="locate" title="My location">📍</button></div>
  <div id="drawer" hidden></div>
</section>

<section class="view" id="v-verify" hidden><div class="container">
  <div class="phead"><h1 data-i18n="v_title"></h1><p data-i18n="v_sub"></p></div>
  <div class="card"><div class="formrow">
    <label class="f"><span data-i18n="f_district"></span><select id="v_dist"></select></label>
    <label class="f"><span data-i18n="f_sheet"></span><select id="v_sheet"></select></label>
    <label class="f"><span data-i18n="f_plot"></span><input id="v_plot" list="v_plots"></label>
    <button class="btn" id="v_go" data-i18n="v_btn"></button></div><datalist id="v_plots"></datalist></div>
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
<div id="toast"></div><dialog id="dlg"></dialog>

<script>
const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
const BN='০১২৩৪৫৬৭৮৯';
let lang=localStorage.lang||'bn',role=localStorage.role||'user',dataMode=localStorage.dataMode||'dataset';
/* ---------- translations [bn, en] ---------- */
const T={
brand:["ভূমি দর্পণ","Bhumi Dorpon"],tagline:["বাংলাদেশ ভূমি তথ্য ও যাচাই","Bangladesh land info & verification"],
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
m_click_ds:["লোড করতে ক্লিক করুন","Click to load"],m_none:["কোনো ডেটা নেই","No data"],m_refreshed:["ডেটা হালনাগাদ হয়েছে ✔","Data refreshed ✔"],m_loaded:["লোড হয়েছে","loaded"],m_nodata:["কিছু পাওয়া যায়নি","Nothing found"],
m_locating:["অবস্থান খোঁজা হচ্ছে…","Locating…"],m_here:["আপনার অবস্থান","You are here"],m_place_hit:["এলাকায় ডেটাসেট পাওয়া গেছে — লোড হচ্ছে","Dataset found nearby — loading"],
bm_sat:["স্যাটেলাইট","Satellite"],bm_satl:["স্যাটেলাইট + লেবেল","Satellite + labels"],bm_osm:["ওএসএম","OSM"],bm_topo:["ভূসংস্থান","Topo"],bm_light:["হালকা","Light"],bm_dark:["গাঢ়","Dark"],
c_Settlement:["বসতবাড়ি","Settlement"],c_Agriculture:["কৃষি","Agriculture"],c_Commercial_Area:["বাণিজ্যিক","Commercial"],c_Water_Body:["জলাশয়","Water body"],c_Road:["সড়ক","Road"],c_Industrial_Area:["শিল্প এলাকা","Industrial"],c_Forest:["বন","Forest"],c_Vacant_Land:["পতিত জমি","Vacant land"],
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
tr_ph:["কেস নম্বর (যেমন MUT-2026-00001)","Case no. (e.g. MUT-2026-00001)"],tr_btn:["অনুসন্ধান","Track"],tr_samples:["নমুনা কেস","Sample cases"],tr_notfound:["এই নম্বরে কোনো কেস পাওয়া যায়নি","No case found for this number"],
tr_buyer:["ক্রেতা/আবেদনকারী","Buyer / applicant"],tr_seller:["বিক্রেতা","Seller"],tr_deed:["দলিল নং","Deed no."],tr_office:["সাব-রেজিস্ট্রি অফিস","Sub-registry office"],tr_sla:["নির্ধারিত সময়সীমা","Service deadline"],tr_left:["দিন বাকি","days left"],tr_over:["দিন অতিক্রান্ত","days overdue"],tr_timeline:["ঘটনাপ্রবাহ","Timeline"],tr_checks:["স্বয়ংক্রিয় যাচাই","Automated checks"],tr_plot:["দাগ","Plot"],
a_title:["অ্যাডমিন টুলস","Admin tools"],a_note:["MVP: লগইন নেই — অ্যাডমিন টুল সবার জন্য উন্মুক্ত (শুধু ডেমো)","MVP: no login — admin tools are open (demo only)"],
a_overview:["সারসংক্ষেপ","Overview"],a_fraud:["জালিয়াতি সতর্কতা","Fraud alerts"],a_workflow:["ই-নামজারি ওয়ার্কফ্লো","e-Mutation workflow"],a_data:["ডেটা ম্যানেজার","Data manager"],
a_open:["খোলা সতর্কতা","Open alerts"],a_high:["উচ্চ ঝুঁকি","High severity"],a_prog:["চলমান কেস","Cases in progress"],a_flag:["আটকানো কেস","Flagged cases"],a_avg:["গড় সময় (দিন)","Avg. days"],
a_bytype:["ধরন অনুযায়ী সতর্কতা","Alerts by type"],a_funnel:["কেসের ধাপ","Case stages"],a_recent:["সাম্প্রতিক সতর্কতা","Recent alerts"],a_all:["সব","All"],
th_id:["আইডি","ID"],th_type:["ধরন","Type"],th_sev:["তীব্রতা","Severity"],th_plot:["দাগ","Plot"],th_conf:["আস্থা","Confidence"],th_date:["তারিখ","Detected"],th_status:["অবস্থা","Status"],th_ds:["ডেটাসেট","Dataset"],th_plots:["দাগ","Plots"],th_parts:["অংশ","Parts"],th_area:["আয়তন (শতাংশ)","Area (decimal)"],
a_evidence:["প্রমাণ / সংকেত","Evidence / signals"],a_advance:["পরবর্তী ধাপে","Advance"],a_hold:["আটকান","Hold"],a_release:["ছাড় দিন","Release"],a_reject:["প্রত্যাখ্যান","Reject"],a_updated:["হালনাগাদ হয়েছে","Updated"],
a_upload_h:["নতুন ডেটাসেট আপলোড","Upload dataset"],a_upload_help:["নাম দিন জেলা_শিট আকারে (যেমন gazipur_002), তারপর land ও plot JSON বেছে নিন। ফাইল data/ ফোল্ডারে জমা হবে এবং মানচিত্রে স্বয়ংক্রিয় যোগ হবে।","Name it district_sheet (e.g. gazipur_002), then choose land and plot JSON. Files are saved in data/ and appear on the map automatically."],
a_name:["ডেটাসেটের নাম","Dataset name"],a_land_f:["land.json ফাইল","land.json file"],a_plot_f:["plot.json ফাইল","plot.json file"],a_upload_btn:["আপলোড","Upload"],a_uploaded:["আপলোড সম্পন্ন","Uploaded"],a_pick:["অন্তত একটি ফাইল বেছে নিন","Choose at least one file"],
a_gate_t:["অ্যাডমিন মোড বন্ধ আছে","Admin mode is off"],a_gate_d:["অ্যাডমিন টুল দেখতে ওপরের সুইচ থেকে অ্যাডমিন বেছে নিন। (MVP-তে লগইন নেই)","Switch to Admin using the toggle above to see admin tools. (No login in the MVP)"],a_enable:["অ্যাডমিন মোড চালু করুন","Enable admin mode"]
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
const api=async(u,o)=>{const r=await fetch(u,o);let j={};try{j=await r.json()}catch(e){}if(!r.ok)throw Object.assign(new Error(j.error||r.status),{status:r.status});return j};
const PALETTE={'Commercial Area':'#e63946','Settlement':'#f4a261','Agriculture':'#8ab17d','Water Body':'#38bdf8','Road':'#9ca3af','Industrial Area':'#8b5cf6','Forest':'#15803d','Vacant Land':'#d6c28a'};
const colorOf=c=>PALETTE[c]||`hsl(${[...(c||'?')].reduce((a,ch)=>a*31+ch.charCodeAt(0)>>>0,7)%360} 65% 50%)`;
const STAGES=['registration','auto_check','union_office','ac_land','approved','khatian'];
const FT=['fake_deed','double_sale','rapid','mismatch'];
let DSL=[],STATS={},VR=null,TC=null,cur={path:'/',q:new URLSearchParams()},BD={divisions:[],districts:[],thanas:[],mouzas:[]};

/* ---------- datasets / selectors ---------- */
const districts=()=>{const m=new Map();DSL.forEach(d=>m.set(d.district,d));return[...m.values()]};
const sheetsOf=d=>DSL.filter(x=>x.district===d);
function fillDist(el,val){el.innerHTML=`<option value="">${t('sel_district')}</option>`+districts().map(d=>`<option value="${esc(d.district)}">${esc(dn(d))}</option>`).join('');el.value=districts().some(d=>d.district===val)?val:''}
function fillSheet(el,dist,val,all){const sh=sheetsOf(dist);
  el.innerHTML=(all?`<option value="*">${t('all_sheets')}</option>`:`<option value="">${t('sel_sheet')}</option>`)+sh.map(d=>`<option value="${esc(d.id)}">${t('f_sheet')} ${X(d.sheet||d.id)}</option>`).join('');
  el.value=[...el.options].some(o=>o.value===val)?val:(!all&&sh.length===1?sh[0].id:(all?'*':''))}
function cascade(dEl,sEl,all,cb){const dv=dEl.value,sv=sEl.value;fillDist(dEl,dv);fillSheet(sEl,dEl.value,sv,all);
  dEl.onchange=()=>{fillSheet(sEl,dEl.value,'',all);cb&&cb('d')};sEl.onchange=()=>cb&&cb('s')}
const fillPlots=(el,ds)=>{el.innerHTML='';if(ds&&ds!=='*')api('/api/plots/'+encodeURIComponent(ds)).then(a=>el.innerHTML=a.map(p=>`<option value="${esc(p.plot_no)}">${esc(p.label)}</option>`).join('')).catch(()=>{})};
const bbOf=d=>[[d.bbox[0],d.bbox[1]],[d.bbox[2],d.bbox[3]]];
const optionText=x=>lang==='bn'?(x.name_bn||x.name_en):(x.name_en||x.name_bn);
const fillApiSelect=(el,items,placeholder)=>{el.innerHTML=`<option value="">${t(placeholder)}</option>`+items.map(x=>`<option value="${esc(x.code)}">${esc(optionText(x))}</option>`).join('')};
async function loadApiDivisions(){if(!BD.divisions.length)BD.divisions=await api('/api/bd/divisions');fillApiSelect($('#h_division'),BD.divisions,'sel_division')}
async function loadApiDistricts(code){BD.districts=await api('/api/bd/districts?division_code='+encodeURIComponent(code));fillApiSelect($('#h_api_dist'),BD.districts,'sel_district');$('#h_thana').innerHTML=`<option value="">${t('sel_thana')}</option>`;$('#h_mouza').innerHTML=`<option value="">${t('sel_mouza')}</option>`}
async function loadApiThanas(division,code){BD.thanas=await api(`/api/bd/thanas?division_code=${encodeURIComponent(division)}&district_code=${encodeURIComponent(code)}`);fillApiSelect($('#h_thana'),BD.thanas,'sel_thana');$('#h_mouza').innerHTML=`<option value="">${t('sel_mouza')}</option>`}
async function loadApiMouzas(division,district,code){BD.mouzas=await api(`/api/bd/mouzas?division_code=${encodeURIComponent(division)}&district_code=${encodeURIComponent(district)}&thana_code=${encodeURIComponent(code)}`);fillApiSelect($('#h_mouza'),BD.mouzas,'sel_mouza')}
function setMode(mode){dataMode=mode;localStorage.dataMode=mode;$('#modeSeg button[data-mode="dataset"]').classList.toggle('on',mode==='dataset');$('#modeSeg button[data-mode="api"]').classList.toggle('on',mode==='api');$('#datasetSearch').hidden=mode!=='dataset';$('#apiSearch').hidden=mode!=='api';if(mode==='api')loadApiDivisions().catch(e=>toast(e.message))}

/* ---------- static i18n ---------- */
function applyStatic(){document.documentElement.lang=lang;document.title=t('brand')+' — '+t('tagline');
  $$('[data-i18n]').forEach(e=>e.textContent=t(e.dataset.i18n));$$('[data-ph]').forEach(e=>e.placeholder=t(e.dataset.ph));
  $$('#langSeg button').forEach(b=>b.classList.toggle('on',b.dataset.lang===lang));$$('#roleSeg button').forEach(b=>b.classList.toggle('on',b.dataset.role===role));
  $$('#modeSeg button').forEach(b=>b.classList.toggle('on',b.dataset.mode===dataMode));setMode(dataMode);
  $('#navAdmin').style.display=role==='admin'?'':'none'}
$('#langSeg').onclick=e=>{const b=e.target.closest('button');if(!b)return;lang=b.dataset.lang;localStorage.lang=lang;applyStatic();route(true)};
$('#modeSeg').onclick=e=>{const b=e.target.closest('button');if(b)setMode(b.dataset.mode)};
$('#roleSeg').onclick=e=>{const b=e.target.closest('button');if(!b)return;role=b.dataset.role;localStorage.role=role;applyStatic();
  if(role==='admin'&&cur.path.indexOf('/admin')!==0)location.hash='#/admin';else if(role==='user'&&cur.path.indexOf('/admin')===0)location.hash='#/';else route(true)};
$('#themeBtn').onclick=()=>{const n=document.documentElement.dataset.theme==='dark'?'light':'dark';document.documentElement.dataset.theme=n;localStorage.theme=n};
$('#burger').onclick=()=>$('#links').classList.toggle('open');
$$('[data-go]').forEach(e=>e.onclick=()=>{const g=e.dataset.go;if(g==='#/admin'&&role!=='admin'){role='admin';localStorage.role=role;applyStatic()}location.hash=g});

/* ---------- router ---------- */
function route(keep){
  const h=location.hash.slice(1)||'/',[path,qs]=h.split('?');cur={path,q:new URLSearchParams(qs||'')};
  const v=path.startsWith('/map')?'map':path.startsWith('/verify')?'verify':path.startsWith('/track')?'track':path.startsWith('/admin')?'admin':'home';
  $$('.view').forEach(e=>e.hidden=e.id!=='v-'+v);document.body.classList.toggle('map-mode',v==='map');
  $$('.nav-links a').forEach(a=>a.classList.toggle('on',a.dataset.r===v));$('#links').classList.remove('open');
  if(v==='home')renderHome();else if(v==='map')showMap(cur.q,keep);else if(v==='verify')renderVerify(keep);else if(v==='track')renderTrack(keep);else renderAdmin();
  if(!keep&&v!=='map')scrollTo(0,0)}
addEventListener('hashchange',()=>route(false));

/* ---------- home ---------- */
function renderHome(){
  $('#h_stats').innerHTML=[['stat_ds',STATS.datasets],['stat_plots',STATS.plots],['stat_checks',STATS.checks],['stat_alerts',STATS.alerts],['stat_cases',STATS.cases]].map(([k,v])=>`<div class="stat"><b>${nf(v||0,0)}</b><span>${t(k)}</span></div>`).join('');
  setMode(dataMode);
  cascade($('#h_dist'),$('#h_sheet'),false,w=>{if(w==='s')fillPlots($('#h_plots'),$('#h_sheet').value)});fillPlots($('#h_plots'),$('#h_sheet').value)}
$('#h_division').onchange=()=>{if($('#h_division').value)loadApiDistricts($('#h_division').value).catch(e=>toast(e.message))};
$('#h_api_dist').onchange=()=>{if($('#h_api_dist').value)loadApiThanas($('#h_division').value,$('#h_api_dist').value).catch(e=>toast(e.message))};
$('#h_thana').onchange=()=>{if($('#h_thana').value)loadApiMouzas($('#h_division').value,$('#h_api_dist').value,$('#h_thana').value).catch(e=>toast(e.message))};
$('#h_map').onclick=()=>{if(dataMode==='api'){const d=$('#h_division').value,di=$('#h_api_dist').value,tv=$('#h_thana').value,m=$('#h_mouza').value;if(!d||!di||!tv||!m)return toast(t('need_api_area'));location.hash=`#/map?api=1&division=${d}&district=${di}&thana=${tv}&mouza=${m}`;return}const d=$('#h_dist').value,s=$('#h_sheet').value,p=$('#h_plot').value.trim();if(!d)return toast(t('need_dist'));
  const ids=s?[s]:sheetsOf(d).map(x=>x.id);location.hash=`#/map?ds=${ids.join(',')}${p&&ids.length===1?'&plot='+encodeURIComponent(p):''}`};
$('#h_ver').onclick=()=>{if(dataMode==='api'){return $('#h_map').click()}const s=$('#h_sheet').value,p=$('#h_plot').value.trim();if(!s||!p)return toast(t('need_plot'));location.hash=`#/verify?ds=${s}&plot=${encodeURIComponent(p)}`};
const goPlace=()=>{const v=$('#h_place').value.trim();if(v)location.hash='#/map?place='+encodeURIComponent(v)};
$('#h_go').onclick=goPlace;$('#h_place').onkeydown=e=>{if(e.key==='Enter')goPlace()};

/* ---------- map ---------- */
let map,landG,plotG,apiG,ovG,baseLayer,baseKey='satl',loaded=[],DATA=null,API_DATA=null,plotLayers={},selLayer=null,curSel=null,hiddenCls=new Set(),mapReady=false,pinMark=null,busy=false,searchT;
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
  map.createPane('landPane').style.zIndex=410;map.createPane('plotPane').style.zIndex=420;
  landG=L.layerGroup().addTo(map);plotG=L.layerGroup().addTo(map);apiG=L.layerGroup().addTo(map);ovG=L.layerGroup().addTo(map);setBase('satl');
  $('#lyLand').onchange=e=>e.target.checked?landG.addTo(map):map.removeLayer(landG);
  $('#lyPlot').onchange=e=>e.target.checked?plotG.addTo(map):map.removeLayer(plotG);
  $('#lbl').onchange=updateLabels;map.on('zoomend',updateLabels);map.on('moveend',autoLoad);
  $('#op').oninput=()=>{const o=+$('#op').value;landG.eachLayer(g=>g.setStyle&&g.setStyle({fillOpacity:o}))};
  $('#ptoggle').onclick=()=>$('#panel').classList.toggle('hide');
  $('#fit').onclick=()=>fitData();$('#m_all').onclick=()=>{loadIds([]);fitAll()};
  $('#q').oninput=()=>{clearTimeout(searchT);searchT=setTimeout(renderList,220)};
  const pl=()=>{const v=$('#place').value.trim();if(v)geocode(v)};$('#place').onkeydown=e=>{if(e.key==='Enter')pl()};
  $('#locate').onclick=locateMe;
  setInterval(poll,4000)}
function updateLabels(){map.getContainer().classList.toggle('nolabels',!$('#lbl').checked||map.getZoom()<17)}
function fitData(){if(!DATA)return fitAll();const g=L.geoJSON([...DATA.plot.features,...DATA.land.features]);if(g.getLayers().length)map.fitBounds(g.getBounds(),{padding:[50,50]})}
function fitAll(){const b=DSL.filter(d=>d.bbox).flatMap(d=>bbOf(d));if(b.length)map.fitBounds(b,{padding:[60,60],maxZoom:12})}
function mapUrl(plot){history.replaceState(null,'','#/map'+(loaded.length?`?ds=${loaded.join(',')}${plot?'&plot='+encodeURIComponent(plot):''}`:''))}
function wktGeo(wkt){
  const m=/^\s*(MULTIPOLYGON|POLYGON)\s*(\(.*\))\s*$/is.exec(wkt||'');if(!m)return null;
  const stack=[[]],tokens=m[2].match(/\(|\)|[^()]+/g)||[];
  for(const token of tokens){
    if(token==='(')stack.push([]);
    else if(token===')'){if(stack.length<2)return null;const node=stack.pop();stack[stack.length-1].push(node)}
    else stack[stack.length-1].push(...token.split(',').map(p=>p.trim().split(/\s+/).slice(0,2).map(Number)).filter(p=>p.length===2&&p.every(Number.isFinite)));
  }
  if(stack.length!==1||!stack[0].length)return null;
  const kind=m[1].toUpperCase(),coordinates=stack[0][0];
  return {type:kind==='POLYGON'?'Polygon':'MultiPolygon',coordinates};
}
function apiFeatures(payload,key){const rows=payload&&payload[key]||[];return rows.map((row,i)=>{const geometry=wktGeo(row.geom);return geometry?{type:'Feature',properties:{...row,_api:true,_key:i},geometry}:null}).filter(Boolean)}
async function loadApiMap(q){
  const division=q.get('division'),district=q.get('district'),thana=q.get('thana'),mouza=q.get('mouza');
  if(!BD.divisions.some(x=>x.code===division))BD.divisions=await api('/api/bd/divisions');
  if(!BD.districts.some(x=>x.code===district))BD.districts=await api('/api/bd/districts?division_code='+encodeURIComponent(division));
  if(!BD.thanas.some(x=>x.code===thana))BD.thanas=await api(`/api/bd/thanas?division_code=${encodeURIComponent(division)}&district_code=${encodeURIComponent(district)}`);
  if(!BD.mouzas.some(x=>x.code===mouza))BD.mouzas=await api(`/api/bd/mouzas?division_code=${encodeURIComponent(division)}&district_code=${encodeURIComponent(district)}&thana_code=${encodeURIComponent(thana)}`);
  const boundary=await api(`/api/bd/map-data?level=forThana&tableName=gis_boundary_upazila&thanaVal=${encodeURIComponent(thana)}`);
  const divisionInfo=BD.divisions.find(x=>x.code===division),districtInfo=BD.districts.find(x=>x.code===district),thanaInfo=BD.thanas.find(x=>x.code===thana),mouzaInfo=BD.mouzas.find(x=>x.code===mouza);
  const mcode=mouzaInfo&&mouzaInfo.m_code;
  const [plots,land]=mcode?await Promise.all([
    api(`/api/bd/map-data?level=forMauza&tableName=${encodeURIComponent(mcode.toLowerCase())}&mauzaVal=${encodeURIComponent(mouza)}&plotOrLanduse=plot`),
    api(`/api/bd/map-data?level=forMauza&tableName=${encodeURIComponent(mcode.toLowerCase())}&mauzaVal=${encodeURIComponent(mouza)}&plotOrLanduse=landuse`)
  ]):[{},{}];
  API_DATA={boundary,plot:{type:'FeatureCollection',features:apiFeatures(plots,'plots')},land:{type:'FeatureCollection',features:apiFeatures(land,'landuse')},label:(mouzaInfo&&optionText(mouzaInfo))||'API area',division:divisionInfo&&optionText(divisionInfo),district:districtInfo&&optionText(districtInfo),thana:thanaInfo&&optionText(thanaInfo)};
  apiG.clearLayers();
  const layers=[];
  if(boundary&&boundary.geom){const g=wktGeo(boundary.geom);if(g)layers.push(L.geoJSON(g,{style:{color:'#00e5ff',weight:3,fillOpacity:.08}}))}
  L.geoJSON(API_DATA.land,{style:f=>({color:'#166534',weight:1,fillColor:colorOf(f.properties.maj_class),fillOpacity:.38})}).addTo(apiG);
  L.geoJSON(API_DATA.plot,{style:{color:'#ffd60a',weight:2,fillOpacity:.04},onEachFeature:(f,l)=>l.bindTooltip(String(f.properties.label||f.properties.plot_no_en||''),{permanent:true,className:'plot-lbl'})}).addTo(apiG);
  layers.forEach(l=>l.addTo(apiG));const all=apiG.getLayers();if(all.length){const group=L.featureGroup(all);map.fitBounds(group.getBounds(),{padding:[55,55],maxZoom:18})}
  $('#sub').textContent=`${API_DATA.label} · ${API_DATA.thana||''} · API`;
  $('#mstats').innerHTML=[[API_DATA.plot.features.length,'m_total'],[API_DATA.land.features.length,'m_parts'],['—','m_area'],[new Set(API_DATA.land.features.map(f=>f.properties.maj_class)).size,'m_cls']].map(([v,k])=>`<div><b>${typeof v==='number'?nf(v,0):v}</b><span>${t(k)}</span></div>`).join('');
  $('#legend').innerHTML=`<small>${esc(API_DATA.division||'')} · ${esc(API_DATA.district||'')} · ${esc(API_DATA.thana||'')} · ${esc(API_DATA.label||'')}</small>`;
}
async function showMap(q,keep){
  initMap();setTimeout(()=>map.invalidateSize(),60);
  if(q.get('api')==='1'){landG.clearLayers();plotG.clearLayers();ovG.clearLayers();loaded=[];DATA=null;$('#drawer').hidden=true;try{await loadApiMap(q)}catch(e){toast(e.message)}return}
  apiG.clearLayers();API_DATA=null;
  cascade($('#m_dist'),$('#m_sheet'),true,w=>{const d=$('#m_dist').value,s=$('#m_sheet').value;if(!d)return loadIds([]);loadIds(s&&s!=='*'?[s]:sheetsOf(d).map(x=>x.id))});
  renderBases();sidebar();
  if(keep){if(curSel)showDrawer(curSel[0],curSel[1]);return}
  const ds=(q.get('ds')||'').split(',').filter(i=>DSL.some(d=>d.id===i));
  if(q.get('place'))return geocode(q.get('place'));
  if(ds.length){if(ds.join()!==loaded.join())await loadIds(ds);const p=q.get('plot');
    if(p){const f=DATA&&DATA.plot.features.find(f=>String(f.properties.plot_no_en)===p);if(f)selectPlot(f.properties._ds,p,true)}}
  else if(!loaded.length){drawOverview();fitAll()}}
async function loadIds(ids,o={}){
  const fit=o.fit!==false;
  if(!ids.length){loaded=[];DATA=null;curSel=null;$('#drawer').hidden=true;renderData();mapUrl();return}
  try{DATA=await api('/api/data?ids='+ids.join(','));loaded=DATA.ids;renderData();if(fit)fitData();if(o.quiet)toast(t('m_refreshed'));mapUrl(curSel&&curSel[1])}catch(e){toast(e.message)}}
function renderData(){
  landG.clearLayers();plotG.clearLayers();plotLayers={};selLayer=null;
  if(DATA){const op=+$('#op').value;
    L.geoJSON(DATA.land,{pane:'landPane',interactive:false,filter:f=>!hiddenCls.has(f.properties.maj_class),style:f=>({color:'#fff',weight:1,fillColor:colorOf(f.properties.maj_class),fillOpacity:op})}).addTo(landG);
    L.geoJSON(DATA.plot,{pane:'plotPane',style:()=>({color:'#ffd60a',weight:2,fillColor:'#ffd60a',fillOpacity:.04}),onEachFeature:(f,l)=>{
      const p=f.properties;plotLayers[p._ds+'|'+p.plot_no_en]=l;
      l.bindTooltip(String(p.label??p.plot_no_en),{permanent:true,direction:'center',className:'plot-lbl'});
      l.on('click',()=>selectPlot(p._ds,String(p.plot_no_en),false));
      l.on('mouseover',()=>l!==selLayer&&l.setStyle({weight:3}));l.on('mouseout',()=>l!==selLayer&&l.setStyle({weight:2}))}}).addTo(plotG);
    if(curSel&&plotLayers[curSel.join('|')])highlight(plotLayers[curSel.join('|')])}
  updateLabels();drawOverview();sidebar()}
function drawOverview(){ovG.clearLayers();DSL.filter(d=>d.center&&!loaded.includes(d.id)).forEach(d=>{
  const m=L.marker(d.center,{icon:L.divIcon({className:'',iconSize:[0,0],html:`<div class="ovpin"><b>${esc(dn(d))}</b>${t('f_sheet')} ${X(d.sheet)} · ${nf(d.plots,0)}</div>`})});
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
  $('#sub').textContent=loaded.length?`${loaded.length} ${t('m_loaded')} · ${new Date().toLocaleTimeString(lang==='bn'?'bn-BD':'en-GB')}`:t('m_click_ds');
  $('#mstats').innerHTML=[[pf.length,'m_total'],[lf.length,'m_parts'],[dec(total),'m_area'],[Object.keys(cs).length,'m_cls']].map(([v,k],i)=>`<div><b>${nf(v,i==2?1:0)}</b><span>${t(k)}</span></div>`).join('');
  $('#legend').innerHTML=Object.entries(cs).map(([c,v])=>`<label class="rw"><input type="checkbox" data-c="${esc(c)}" ${hiddenCls.has(c)?'':'checked'}><span class="sw" style="background:${colorOf(c)}"></span>${esc(cls(c))}<span class="n">${nf(v.n,0)} · ${nf(dec(v.a),1)}</span></label>`).join('')||`<small>${t('m_none')}</small>`;
  $$('#legend input').forEach(i=>i.onchange=()=>{i.checked?hiddenCls.delete(i.dataset.c):hiddenCls.add(i.dataset.c);renderData()});
  renderList()}
async function renderList(){
  const q=$('#q').value.trim(),el=$('#plist');let rows=[];
  if(q){try{rows=(await api('/api/search?q='+encodeURIComponent(q))).results}catch(e){}}
  else if(DATA)rows=DATA.plot.features.slice(0,300).map(f=>({ds:f.properties._ds,plot_no:String(f.properties.plot_no_en),label:f.properties.label,area_sqm:f.properties.area_sqm,sheet:(DSL.find(d=>d.id===f.properties._ds)||{}).sheet}));
  const dd=id=>DSL.find(d=>d.id===id);
  el.innerHTML=rows.map((r,i)=>`<div class="rw" data-i="${i}"><span class="sw" style="background:#ffd60a"></span>${t('v_plot')} ${X(r.label)}${r.district?` <small>· ${esc(nm(r.district))} ${X(r.sheet)}</small>`:''}${r.owner?` <small>· ${esc(nm(r.owner))}</small>`:''}<span class="n">${nf(dec(r.area_sqm))} ${t('u_dec')}</span></div>`).join('')||`<small>${q?t('m_nodata'):t('m_none')}</small>`;
  $$('#plist .rw').forEach(e=>e.onclick=()=>{const r=rows[+e.dataset.i];selectPlot(r.ds,String(r.plot_no),true)})}
async function showDrawer(ds,no){
  const dr=$('#drawer');dr.hidden=false;dr.innerHTML=`<div class="dload">${t('loading')}</div>`;
  try{const r=await api(`/api/verify/${encodeURIComponent(ds)}/${encodeURIComponent(no)}`);if(!curSel||curSel[0]!==ds||curSel[1]!==no)return;
    const vt={safe:'v_verdict_safe',caution:'v_verdict_caution',danger:'v_verdict_danger'}[r.verdict];
    dr.innerHTML=`<button class="icon x" id="dx" style="width:32px;height:32px">✕</button><h3>${t('v_plot')} ${X(r.label)}</h3>
    <p style="margin:2px 0 10px;color:var(--mu);font-size:14px">${esc(nm(r.district))} · ${t('f_sheet')} ${X(r.sheet)} · ${X(r.mouza)}</p>
    <span class="badge ${r.verdict}">${t(vt)} · ${nf(r.score,0)}</span>
    <dl class="kv" style="margin:12px 0"><dt>${t('d_owner')}</dt><dd>${esc(nm(r.owner))}</dd>
    <dt>${t('v_area')}</dt><dd>${nf(dec(r.area_sqm))} ${t('u_dec')}<br><small style="color:var(--mu)">${nf(r.area_sqm,0)} ${t('u_sqm')} · ${nf(r.area_sqm/4046.86,4)} ${t('u_acre')}</small></dd>
    <dt>${t('d_addr')}</dt><dd id="addr">…</dd></dl>
    <div class="chips" style="margin-bottom:12px">${r.parts.map(p=>`<span class="tag" style="background:${colorOf(p.maj)}">${esc(cls(p.maj))} · ${esc(sub(p.sub))} · ${nf(dec(p.area_sqm),1)}</span>`).join('')}</div>
    <div class="btnrow"><a class="btn sm" href="#/verify?ds=${encodeURIComponent(ds)}&plot=${encodeURIComponent(no)}">🔎 ${t('d_report')}</a>
    <button class="btn sm alt" id="dshare">🔗 ${t('d_share')}</button>
    <a class="btn sm alt" target="_blank" rel="noopener" href="https://www.google.com/maps?q=${r.center[0]},${r.center[1]}">🌐 ${t('d_gmaps')}</a></div>`;
    $('#dx').onclick=()=>{dr.hidden=true;curSel=null;if(selLayer){selLayer.setStyle({color:'#ffd60a',weight:2});selLayer=null}mapUrl()};
    $('#dshare').onclick=()=>{navigator.clipboard?.writeText(location.href).then(()=>toast(t('d_copied')),()=>prompt('',location.href))};
    fetch(`https://nominatim.openstreetmap.org/reverse?format=jsonv2&zoom=17&accept-language=${lang==='bn'?'bn,en':'en'}&lat=${r.center[0]}&lon=${r.center[1]}`).then(x=>x.json()).then(j=>{const a=$('#addr');if(a)a.textContent=j.display_name||'—'}).catch(()=>{const a=$('#addr');if(a)a.textContent='—'})
  }catch(e){dr.innerHTML=`<div class="dload">${esc(e.message)}</div>`}}
async function geocode(q){
  $('#place').value=q;const el=$('#placeRes');el.innerHTML=`<small>${t('loading')}</small>`;
  try{const r=await (await fetch(`https://nominatim.openstreetmap.org/search?format=jsonv2&countrycodes=bd&limit=6&accept-language=${lang==='bn'?'bn,en':'en'}&q=${encodeURIComponent(q)}`)).json();
    el.innerHTML=r.map((x,i)=>`<div class="rw" data-i="${i}">📍 <span>${esc(x.display_name.split(',').slice(0,3).join(','))}</span></div>`).join('')||`<small>${t('m_nodata')}</small>`;
    const go=x=>{const lat=+x.lat,lng=+x.lon;if(pinMark)map.removeLayer(pinMark);pinMark=L.marker([lat,lng]).addTo(map).bindPopup(esc(x.display_name)).openPopup();
      const near=DSL.filter(d=>d.center).map(d=>({d,m:map.distance([lat,lng],d.center)})).sort((a,b)=>a.m-b.m)[0];
      if(near&&near.m<4000){toast(t('m_place_hit'));loadIds([...new Set([...loaded,near.d.id])],{fit:false}).then(()=>map.fitBounds(bbOf(near.d),{padding:[40,40]}))}
      else if(x.boundingbox){const b=x.boundingbox.map(Number);map.fitBounds([[b[0],b[2]],[b[1],b[3]]],{maxZoom:17})}else map.setView([lat,lng],16)};
    $$('#placeRes .rw').forEach(e=>e.onclick=()=>go(r[+e.dataset.i]));if(r.length===1||cur.q.get('place'))go(r[0])
  }catch(e){el.innerHTML=`<small>${t('err')}: ${esc(e.message)}</small>`}}
function locateMe(){if(!navigator.geolocation)return;toast(t('m_locating'));
  navigator.geolocation.getCurrentPosition(p=>{const ll=[p.coords.latitude,p.coords.longitude];if(pinMark)map.removeLayer(pinMark);pinMark=L.circleMarker(ll,{radius:9,color:'#fff',weight:3,fillColor:'#2563eb',fillOpacity:1}).addTo(map).bindPopup(t('m_here')).openPopup();map.setView(ll,17)},e=>toast(e.message),{enableHighAccuracy:true,timeout:10000})}
let sigSeen=null;
async function poll(){if(!$('#auto')||!$('#auto').checked)return;try{const s=(await api('/api/version')).sig;if(sigSeen&&s!==sigSeen){DSL=await api('/api/datasets');STATS=await api('/api/stats');
  cascade($('#m_dist'),$('#m_sheet'),true,()=>{});if(loaded.length)await loadIds(loaded.filter(i=>DSL.some(d=>d.id===i)),{fit:false,quiet:true});else drawOverview()}sigSeen=s}catch(e){}}

/* ---------- verify ---------- */
let vmap=null;
function renderVerify(keep){
  cascade($('#v_dist'),$('#v_sheet'),false,w=>{if(w==='s')fillPlots($('#v_plots'),$('#v_sheet').value)});
  const q=cur.q,ds=q.get('ds'),p=q.get('plot');
  if(!keep&&ds&&p){const d=DSL.find(x=>x.id===ds);if(d){$('#v_dist').value=d.district;fillSheet($('#v_sheet'),d.district,ds,false);$('#v_sheet').value=ds;fillPlots($('#v_plots'),ds);$('#v_plot').value=p;runVerify(ds,p);return}}
  if(keep&&VR)drawReport();else if(!VR)$('#v_out').innerHTML=''}
$('#v_go').onclick=()=>{const s=$('#v_sheet').value,p=$('#v_plot').value.trim();if(!s||!p)return toast(t('need_plot'));runVerify(s,p)};
async function runVerify(ds,p){$('#v_out').innerHTML=`<div class="empty">${t('loading')}</div>`;
  try{VR=await api(`/api/verify/${encodeURIComponent(ds)}/${encodeURIComponent(p)}`);drawReport()}catch(e){VR=null;$('#v_out').innerHTML=`<div class="card empty">${t('v_notfound')}</div>`}}
function drawReport(){const r=VR;if(!r)return;
  const vt={safe:'v_verdict_safe',caution:'v_verdict_caution',danger:'v_verdict_danger'}[r.verdict],ic={pass:'✓',warn:'!',fail:'✕',queued:'…'};
  const hist=r.history.map(h=>`<li><b>${t('h_'+h.type)}</b> — ${esc(nm(h.from))} → ${esc(nm(h.to))}<small>${fd(h.date)} · ${t('v_deed')} ${X(h.deed_no)} · ${t('v_case')} ${X(h.case_no)}</small></li>`).join('');
  $('#v_out').innerHTML=`
  <div class="vbanner ${r.verdict}"><div class="gauge" style="--p:${r.score}"><b>${nf(r.score,0)}</b><small>${t('v_score')}</small></div>
    <div><h2>${t(vt)}</h2><p>${t('v_plot')} ${X(r.label)} · ${esc(nm(r.district))} · ${t('f_sheet')} ${X(r.sheet)} · ${X(r.mouza)}</p>
    <div class="btnrow"><button class="btn sm alt" onclick="print()">🖨 ${t('v_print')}</button><a class="btn sm alt" href="#/map?ds=${encodeURIComponent(r.ds)}&plot=${encodeURIComponent(r.plot_no)}">🗺 ${t('open_map')}</a></div></div></div>
  <div class="vgrid">
   <div class="card"><h3>${t('v_status')}</h3>${r.checks.map(c=>`<div class="ck ${c.level}"><i>${ic[c.level]}</i><span>${t('ck_'+c.key)}</span><em>${t('lv_'+c.level)}</em></div>`).join('')}</div>
   <div class="card"><h3>${t('v_owner_h')}</h3><dl class="kv">
     <dt>${t('v_owner')}</dt><dd>${esc(nm(r.owner))}</dd><dt>${t('v_father')}</dt><dd>${esc(nm(r.father))}</dd><dt>${t('v_nid')}</dt><dd>${X(r.nid)}</dd>
     <dt>${t('v_khatian')}</dt><dd>${X(r.khatian)}</dd><dt>${t('v_dag')}</dt><dd>${X(r.dag)}</dd><dt>${t('v_mouza')}</dt><dd>${X(r.mouza)} (${X(r.m_code)})</dd><dt>${t('v_share')}</dt><dd>${X(r.share)}</dd>
     <dt>${t('v_area')}</dt><dd>${nf(dec(r.area_sqm))} ${t('u_dec')} · ${nf(r.area_sqm/4046.86,4)} ${t('u_acre')}</dd></dl></div>
   <div class="card"><h3>${t('v_location')}</h3><div id="vmap"></div><div class="chips">${r.parts.map(p=>`<span class="tag" style="background:${colorOf(p.maj)}">${esc(cls(p.maj))} · ${esc(sub(p.sub))} · ${nf(dec(p.area_sqm),1)} ${t('u_dec')}</span>`).join('')}</div></div>
   <div class="card"><h3>${t('v_flags')}</h3>${r.flags.length?r.flags.map(f=>`<div class="item"><span class="badge ${f.severity}">${t('sev_'+f.severity)}</span> <b>${t('ft_'+f.type)}</b><ul class="adv" style="margin-top:6px">${f.evidence.map(e=>`<li>${X(nm(e))}</li>`).join('')}</ul></div>`).join(''):`<div class="empty">✓ ${t('v_noflags')}</div>`}</div>
   <div class="card wide"><h3>${t('v_pending')}</h3>
     ${r.pending.length?r.pending.map(p=>`<div class="item"><span class="badge warn">${t('p_'+p.type)}</span> ${X(p.ref)} · ${t('fo_'+p.forum)} · ${t('v_since')} ${fd(p.since)}</div>`).join(''):`<div class="item">✓ ${t('v_nopending')}</div>`}
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
  const ic={pass:'✓',warn:'!',fail:'✕',queued:'…'};
  const steps=STAGES.map((s,i)=>`<div class="${i<c.stage||c.status==='completed'?'done':i===c.stage?'cur '+(c.status==='flagged'||c.status==='rejected'?'flagged':''):''}">${t('sg_'+s)}</div>`).join('');
  const dl=c.days_left,sla=c.status==='completed'?'':` · <b style="color:${dl<0?'var(--bd)':'var(--g)'}">${nf(Math.abs(dl),0)} ${dl<0?t('tr_over'):t('tr_left')}</b>`;
  return `<div class="stepper">${steps}</div>
  <div class="two" style="margin-top:14px"><div><h3 style="font-size:15px;margin-bottom:8px">${t('tr_checks')}</h3>${c.checks.map(k=>`<div class="ck ${k.level}"><i>${ic[k.level]}</i><span>${t('wk_'+k.key)}</span><em>${t('lv_'+k.level)}</em></div>`).join('')}</div>
  <div><h3 style="font-size:15px;margin-bottom:8px">${t('tr_timeline')}</h3><ul class="tl">${c.timeline.map(e=>`<li><b>${T['sg_'+e.key]?t('sg_'+e.key):t('ev_'+e.key)}</b><small>${fd(e.date)}</small></li>`).join('')}</ul></div></div>
  <p style="color:var(--mu);font-size:13.5px;margin:6px 0 0">${t('tr_sla')}: ${fd(c.sla_due)}${sla}</p>`}
function drawTrack(){const c=TC;if(!c)return;
  $('#tr_out').innerHTML=`<div class="card"><div class="dhead"><div><h2 style="font-size:22px">${c.id}</h2><p style="margin:2px 0 0;color:var(--mu)">${t('tr_plot')} ${X(c.label)} · ${esc(nm(c.district))} · ${t('f_sheet')} ${X(c.sheet)}</p></div><span class="badge ${c.status}">${t('cs_'+c.status)}</span></div>
  <dl class="kv" style="margin-bottom:16px"><dt>${t('tr_buyer')}</dt><dd>${esc(nm(c.buyer))}</dd><dt>${t('tr_seller')}</dt><dd>${esc(nm(c.seller))}</dd><dt>${t('tr_deed')}</dt><dd>${X(c.deed_no)} · ${fd(c.deed_date)}</dd><dt>${t('tr_office')}</dt><dd>${esc(nm(c.office))}</dd></dl>
  ${caseBody(c)}<div class="btnrow" style="margin-top:14px"><a class="btn sm alt" href="#/verify?ds=${encodeURIComponent(c.ds)}&plot=${encodeURIComponent(c.plot_no)}">🔎 ${t('d_report')}</a></div></div>`}

/* ---------- admin ---------- */
let atab='overview',FA=[],fType='',fStat='',WC=[];
function renderAdmin(){
  const root=$('#a_root');
  if(role!=='admin'){root.innerHTML=`<div class="phead"><div class="card empty" style="padding:40px"><h2>🔒 ${t('a_gate_t')}</h2><p>${t('a_gate_d')}</p><button class="btn" id="a_en">${t('a_enable')}</button></div></div>`;
    $('#a_en').onclick=()=>{role='admin';localStorage.role=role;applyStatic();renderAdmin()};return}
  root.innerHTML=`<div class="phead"><h1>${t('a_title')}</h1><p><span class="note">${t('a_note')}</span></p></div>
  <div class="tabs">${[['overview','a_overview'],['fraud','a_fraud'],['workflow','a_workflow'],['data','a_data']].map(([k,l])=>`<button data-tab="${k}" class="${k===atab?'on':''}">${t(l)}</button>`).join('')}</div><div id="a_body"><div class="empty">${t('loading')}</div></div>`;
  $$('.tabs button').forEach(b=>b.onclick=()=>{atab=b.dataset.tab;renderAdmin()});
  ({overview:aOverview,fraud:aFraud,workflow:aWorkflow,data:aData})[atab]().catch(e=>$('#a_body').innerHTML=`<div class="card empty">${esc(e.message)}</div>`)}
const bars=(rows)=>{const m=Math.max(1,...rows.map(r=>r[1]));return rows.map(([l,v])=>`<div class="bar"><span>${l}</span><div><i style="width:${v/m*100}%"></i></div><b>${nf(v,0)}</b></div>`).join('')};
async function aOverview(){const o=await api('/api/admin/overview');
  $('#a_body').innerHTML=`<div class="kpis">${[['a_open',o.open_alerts],['a_high',o.high],['a_prog',o.in_progress],['a_flag',o.flagged],['a_avg',o.avg_days]].map(([k,v])=>`<div class="kpi"><b>${nf(v,0)}</b><span>${t(k)}</span></div>`).join('')}</div>
  <div class="two"><div class="card"><h3>${t('a_bytype')}</h3>${bars(FT.map(f=>[t('ft_'+f),o.by_type[f]||0]))}</div>
  <div class="card"><h3>${t('a_funnel')}</h3>${bars(STAGES.map((s,i)=>[t('sg_'+s),o.by_stage[i]]))}</div></div>
  <div class="card" style="margin-top:16px"><h3>${t('a_recent')}</h3>${o.recent.map(a=>`<div class="item"><span class="badge ${a.severity}">${t('sev_'+a.severity)}</span> <b>${t('ft_'+a.type)}</b> · ${a.id} · ${t('v_plot')} ${X(a.label)} · ${esc(nm(a.district))} <span class="badge ${a.status}" style="float:right">${t('st_'+a.status)}</span></div>`).join('')}</div>`}
async function aFraud(){FA=(await api('/api/admin/fraud')).alerts;drawFraud()}
function drawFraud(){
  const rows=FA.filter(a=>(!fType||a.type===fType)&&(!fStat||a.status===fStat));
  $('#a_body').innerHTML=`<div class="filters"><button class="fchip ${fType?'':'on'}" data-t="">${t('a_all')}</button>${FT.map(f=>`<button class="fchip ${fType===f?'on':''}" data-t="${f}">${t('ft_'+f)}</button>`).join('')}
  <select id="fs"><option value="">${t('th_status')}: ${t('a_all')}</option>${['new','investigating','resolved','false_positive'].map(s=>`<option value="${s}" ${fStat===s?'selected':''}>${t('st_'+s)}</option>`).join('')}</select></div>
  <div class="card tbl" style="padding:6px"><table><thead><tr><th>${t('th_id')}</th><th>${t('th_type')}</th><th>${t('th_sev')}</th><th>${t('th_plot')}</th><th>${t('th_conf')}</th><th>${t('th_date')}</th><th>${t('th_status')}</th></tr></thead><tbody>
  ${rows.map(a=>`<tr class="r" data-id="${a.id}"><td>${a.id}</td><td><b>${t('ft_'+a.type)}</b></td><td><span class="badge ${a.severity}">${t('sev_'+a.severity)}</span></td><td>${X(a.label)} · ${esc(nm(a.district))} ${X(a.sheet)}</td><td>${nf(a.confidence,0)}%</td><td>${fd(a.detected)}</td>
  <td><select data-s="${a.id}">${['new','investigating','resolved','false_positive'].map(s=>`<option value="${s}" ${a.status===s?'selected':''}>${t('st_'+s)}</option>`).join('')}</select></td></tr>
  <tr class="det" hidden><td colspan="7"><b>${t('a_evidence')}</b><ul class="adv">${a.evidence.map(e=>`<li>${X(nm(e))}</li>`).join('')}</ul><div class="btnrow"><a class="btn sm alt" href="#/map?ds=${a.ds}&plot=${encodeURIComponent(a.plot_no)}">🗺 ${t('open_map')}</a><a class="btn sm alt" href="#/verify?ds=${a.ds}&plot=${encodeURIComponent(a.plot_no)}">🔎 ${t('d_report')}</a></div></td></tr>`).join('')}</tbody></table></div>`;
  $$('.filters .fchip').forEach(b=>b.onclick=()=>{fType=b.dataset.t;drawFraud()});$('#fs').onchange=e=>{fStat=e.target.value;drawFraud()};
  $$('tr.r').forEach(r=>r.onclick=e=>{if(e.target.closest('select'))return;r.nextElementSibling.hidden=!r.nextElementSibling.hidden});
  $$('td select').forEach(s=>s.onchange=async()=>{try{const j=await api('/api/admin/fraud/'+s.dataset.s,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:s.value})});FA=FA.map(a=>a.id===j.alert.id?j.alert:a);toast(t('a_updated'))}catch(e){toast(e.message)}})}
async function aWorkflow(){WC=(await api('/api/admin/workflow')).cases;drawBoard()}
function drawBoard(){
  $('#a_body').innerHTML=`<div class="board">${STAGES.map((s,i)=>{const cs=WC.filter(c=>c.stage===i);return `<div class="col"><h4><span>${t('sg_'+s)}</span><span>${nf(cs.length,0)}</span></h4>${cs.map(c=>`<div class="kcard ${c.status}" data-id="${c.id}"><b>${c.id}</b>${t('v_plot')} ${X(c.label)} · ${esc(nm(c.district))}<br><small>${esc(nm(c.buyer))}</small><div class="dots">${c.checks.map(k=>`<i class="${k.level}"></i>`).join('')}</div><div style="margin-top:6px"><span class="badge ${c.status}">${t('cs_'+c.status)}</span></div></div>`).join('')}</div>`}).join('')}</div>`;
  $$('.kcard').forEach(k=>k.onclick=()=>openCase(k.dataset.id))}
function openCase(id){const c=WC.find(x=>x.id===id);if(!c)return;const d=$('#dlg');
  d.innerHTML=`<div class="dhead"><div><h2 style="font-size:20px">${c.id} <span class="badge ${c.status}">${t('cs_'+c.status)}</span></h2><p style="margin:2px 0 0;color:var(--mu)">${t('tr_plot')} ${X(c.label)} · ${esc(nm(c.district))} · ${esc(nm(c.buyer))} ← ${esc(nm(c.seller))}</p></div><button class="icon" id="dc">✕</button></div>
  ${caseBody(c)}<div class="btnrow" style="margin-top:16px"><button class="btn sm" data-a="advance">▶ ${t('a_advance')}</button><button class="btn sm alt" data-a="hold">⏸ ${t('a_hold')}</button><button class="btn sm alt" data-a="release">✔ ${t('a_release')}</button><button class="btn sm danger" data-a="reject">✕ ${t('a_reject')}</button>
  <a class="btn sm alt" href="#/verify?ds=${encodeURIComponent(c.ds)}&plot=${encodeURIComponent(c.plot_no)}" onclick="$('#dlg').close()">🔎 ${t('d_report')}</a></div>`;
  $('#dc').onclick=()=>d.close();
  $$('#dlg [data-a]').forEach(b=>b.onclick=async()=>{try{const j=await api('/api/admin/workflow/'+c.id,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:b.dataset.a})});
    WC=WC.map(x=>x.id===c.id?j.case:x);drawBoard();openCase(c.id)}catch(e){toast(e.message)}});
  if(!d.open)d.showModal()}
async function aData(){const L=await api('/api/datasets');
  $('#a_body').innerHTML=`<div class="card tbl" style="padding:6px"><table><thead><tr><th>${t('th_ds')}</th><th>${t('f_district')}</th><th>${t('f_sheet')}</th><th>${t('th_plots')}</th><th>${t('th_parts')}</th><th>${t('th_area')}</th><th></th></tr></thead><tbody>
  ${L.map(d=>`<tr><td><code>${esc(d.id)}</code></td><td>${esc(dn(d))}</td><td>${X(d.sheet)}</td><td>${nf(d.plots,0)}</td><td>${nf(d.parts,0)}</td><td>${nf(dec(d.area_sqm),1)}</td><td><a class="btn sm alt" href="#/map?ds=${esc(d.id)}">🗺</a></td></tr>`).join('')}</tbody></table></div>
  <div class="card" style="margin-top:16px"><h3>${t('a_upload_h')}</h3><p style="color:var(--mu);font-size:14px;margin-top:0">${t('a_upload_help')}</p>
  <div class="formrow" style="grid-template-columns:1fr 1fr 1fr auto"><label class="f"><span>${t('a_name')}</span><input id="u_name" placeholder="gazipur_002"></label>
  <label class="f"><span>${t('a_land_f')}</span><input type="file" id="u_land" accept=".json"></label><label class="f"><span>${t('a_plot_f')}</span><input type="file" id="u_plot" accept=".json"></label>
  <button class="btn" id="u_go">${t('a_upload_btn')}</button></div></div>`;
  $('#u_go').onclick=async()=>{const n=$('#u_name').value.trim().toLowerCase(),fs=['land','plot'].map(k=>[k,$('#u_'+k).files[0]]).filter(x=>x[1]);
    if(!fs.length)return toast(t('a_pick'));try{for(const[k,f]of fs)await api(`/api/admin/upload/${encodeURIComponent(n)}/${k}`,{method:'POST',body:await f.text()});toast(t('a_uploaded'));
      DSL=await api('/api/datasets');STATS=await api('/api/stats');aData()}catch(e){toast(e.message)}}}

/* ---------- boot ---------- */
(async()=>{applyStatic();
  try{[DSL,STATS]=await Promise.all([api('/api/datasets'),api('/api/stats')]);sigSeen=(await api('/api/version')).sig}catch(e){toast(e.message)}
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
    a = ap.parse_args()
    write_demo()
    print(f"ভূমি দর্পণ  ->  http://{a.host}:{a.port}   (data folder: {DATA_DIR})")
    if not a.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{a.host}:{a.port}")).start()
    app.run(host=a.host, port=a.port, debug=False, threaded=True)