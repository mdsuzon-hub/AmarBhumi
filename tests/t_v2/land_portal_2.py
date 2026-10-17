#!/usr/bin/env python3
"""
ভূমি সুরক্ষা · Bhumi Suraksha  —  Bangladesh Land Portal (MVP)
================================================================
    pip install flask
    python bhumi_portal.py              # http://127.0.0.1:5000

* Bangla (primary) + English UI, green theme, light/dark toggle, responsive.
* Two roles, NO login in this MVP (toggle in the header):
    - General user : landing page, land map, pre-purchase verification, mutation tracking
    - Admin        : dashboard, fraud alerts, smart e-mutation workflow, data upload
* Map data is loaded dynamically from  ./data/<place>_<sheet>_land.json  and
  ./data/<place>_<sheet>_plot.json   (e.g. gazipur_001_land.json, gazipur_001_plot.json).
  Drop new files in that folder (or upload as admin) and they appear automatically.
  Demo files are generated on first run if ./data is empty.
* Registry / mutation / case / khas data is DUMMY and generated deterministically per plot,
  so it works for any land/plot JSON you add.
* Free services used from the browser: OpenStreetMap Nominatim (place search + reverse
  geocoding), Esri / OSM / OpenTopoMap / CARTO tiles, Leaflet.

NOTE: the admin toggle is NOT security. Add real authentication before production.
"""
import argparse, glob, hashlib, itertools, json, math, os, random, re, threading, webbrowser
from datetime import date, timedelta
from flask import Flask, Response, jsonify, request

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
NAME_RE = re.compile(r"^(.+)_([^_]+)_(land|plot)\.json$", re.I)
TODAY = date.today()
app = Flask(__name__)

PLACES = {  # slug: (bangla, (lat, lon))
    "gazipur": ("গাজীপুর", (24.0023, 90.4264)), "dhaka": ("ঢাকা", (23.8103, 90.4125)),
    "barishal": ("বরিশাল", (22.7010, 90.3535)), "sylhet": ("সিলেট", (24.8949, 91.8687)),
    "rajshahi": ("রাজশাহী", (24.3745, 88.6042)), "khulna": ("খুলনা", (22.8456, 89.5403)),
    "chattogram": ("চট্টগ্রাম", (22.3569, 91.7832)), "rangpur": ("রংপুর", (25.7439, 89.2752)),
    "mymensingh": ("ময়মনসিংহ", (24.7471, 90.4203)), "cumilla": ("কুমিল্লা", (23.4607, 91.1809)),
    "bogura": ("বগুড়া", (24.8465, 89.3773)),
}


# =============================================================== geometry
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


def geom_bbox(g):
    polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
    pts = [pt for poly in polys for pt in poly[0]]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return [min(xs), min(ys), max(xs), max(ys)]


# =============================================================== data files
BN_D = str.maketrans("0123456789", "০১২৩৪৫৬৭৮৯")
bn = lambda x: str(x).translate(BN_D)
dstr = lambda days: (TODAY - timedelta(days=days)).isoformat()
dt = lambda s: date.fromisoformat(s)


def make_demo():
    """Create demo land/plot JSON files if the data folder has none."""
    os.makedirs(DATA, exist_ok=True)
    if glob.glob(os.path.join(DATA, "*_land.json")) or glob.glob(os.path.join(DATA, "*_plot.json")):
        return
    classes = [("Settlement", ["Homestead", "Rural Settlement"]), ("Agriculture", ["Cropland", "Orchard"]),
               ("Commercial Area", ["Shop", "Market"]), ("Water Body", ["Pond"]), ("Vacant Land", ["Fallow"])]
    for place, sheet, rows, cols in [("gazipur", "001", 3, 5), ("gazipur", "002", 3, 4), ("dhaka", "001", 3, 4),
                                     ("barishal", "001", 3, 4), ("sylhet", "001", 3, 4)]:
        lat, lon = PLACES[place][1]
        rnd = random.Random(place + sheet)
        plots, lands, gid = [], [], 1
        w, h = 0.00055, 0.00040
        wkt = lambda pts: "POLYGON((%s))" % ", ".join(f"{x:.7f} {y:.7f}" for x, y in pts + [pts[0]])
        for r in range(rows):
            for c in range(cols):
                x0, y0 = lon + (c - cols / 2) * w, lat + (r - rows / 2) * h
                j = lambda: rnd.uniform(-0.00005, 0.00005)
                p = [(x0 + j(), y0 + j()), (x0 + w * .94 + j(), y0 + j()),
                     (x0 + w * .94 + j(), y0 + h * .92 + j()), (x0 + j(), y0 + h * .92 + j())]
                no = r * cols + c + 1
                base = {"m_code": f"{rnd.randint(3000, 3999)}", "plot_no_en": str(no), "label": bn(no),
                        "jl_no_en": str(rnd.randint(10, 60)), "sht_no_en": sheet}
                plots.append({"gid": gid, **base, "geom": wkt(p)})
                if rnd.random() < .5:
                    mb = ((p[0][0] + p[1][0]) / 2, (p[0][1] + p[1][1]) / 2)
                    mt = ((p[3][0] + p[2][0]) / 2, (p[3][1] + p[2][1]) / 2)
                    shapes = [[p[0], mb, mt, p[3]], [mb, p[1], p[2], mt]]
                else:
                    shapes = [p]
                for s in shapes:
                    mc = rnd.choice(classes)
                    lands.append({"gid": len(lands) + 1, **base, "maj_class": mc[0],
                                  "sub_class": rnd.choice(mc[1]), "geom": wkt(s)})
                gid += 1
        for kind, key, items in (("plot", "plots", plots), ("land", "landuse", lands)):
            with open(os.path.join(DATA, f"{place}_{sheet}_{kind}.json"), "w", encoding="utf-8") as f:
                json.dump({key: items}, f, ensure_ascii=False, indent=1)


def scan():
    out = {}
    if os.path.isdir(DATA):
        for f in sorted(os.listdir(DATA)):
            m = NAME_RE.match(f)
            if m:
                e = out.setdefault(f"{m[1].lower()}_{m[2].lower()}",
                                   {"place": m[1].lower(), "sheet": m[2], "land": None, "plot": None})
                e[m[3].lower()] = os.path.join(DATA, f)
    return out


def signature():
    return tuple((p, os.path.getmtime(p)) for e in scan().values() for p in (e["land"], e["plot"]) if p)


_fc = {}


def load_file(path):
    mt = os.path.getmtime(path)
    if path in _fc and _fc[path][0] == mt:
        return _fc[path][1]
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    items = raw if isinstance(raw, list) else [i for v in raw.values() if isinstance(v, list) for i in v]
    feats, bb = [], [1e9, 1e9, -1e9, -1e9]
    for it in items:
        if not isinstance(it, dict) or not it.get("geom"):
            continue
        g = parse_wkt(it["geom"])
        if not g:
            continue
        props = {k: v for k, v in it.items() if k != "geom"}
        props["area_sqm"] = round(geom_area(g), 2)
        b = geom_bbox(g)
        bb = [min(bb[0], b[0]), min(bb[1], b[1]), max(bb[2], b[2]), max(bb[3], b[3])]
        feats.append({"type": "Feature", "properties": props, "geometry": g})
    fc = {"type": "FeatureCollection", "features": feats, "bbox": bb if feats else None}
    _fc[path] = (mt, fc)
    return fc


EMPTY = {"type": "FeatureCollection", "features": [], "bbox": None}


def load_ds(ds):
    e = scan().get(ds)
    if not e:
        return None
    return (load_file(e["land"]) if e["land"] else EMPTY), (load_file(e["plot"]) if e["plot"] else EMPTY)


def plot_features(ds):
    """Plot features; falls back to grouping land parts by plot number."""
    land, plot = load_ds(ds)
    if plot["features"]:
        return plot["features"]
    g = {}
    for f in land["features"]:
        p = f["properties"]
        k = p.get("plot_no_en")
        if k is None:
            continue
        if k in g:
            g[k]["properties"]["area_sqm"] += p["area_sqm"]
        else:
            g[k] = {"properties": dict(p), "geometry": f["geometry"]}
    return list(g.values())


# =============================================================== dummy registry
NAMES = [("মোঃ আব্দুল করিম", "Md. Abdul Karim"), ("রহিমা বেগম", "Rahima Begum"), ("মোঃ জাহাঙ্গীর আলম", "Md. Jahangir Alam"),
         ("ফাতেমা খাতুন", "Fatema Khatun"), ("মোঃ শফিকুল ইসলাম", "Md. Shafiqul Islam"), ("নাসরিন আক্তার", "Nasrin Akter"),
         ("আবুল কালাম", "Abul Kalam"), ("সালমা বেগম", "Salma Begum"), ("মোঃ হাসান মাহমুদ", "Md. Hasan Mahmud"),
         ("তানভীর আহমেদ", "Tanvir Ahmed"), ("সুমন চন্দ্র দাস", "Suman Chandra Das"), ("মিজানুর রহমান", "Mizanur Rahman"),
         ("শারমিন সুলতানা", "Sharmin Sultana"), ("মোঃ রফিকুল ইসলাম", "Md. Rafiqul Islam"), ("কামরুন নাহার", "Kamrun Nahar"),
         ("আনোয়ার হোসেন", "Anwar Hossain")]
KHAS = {"private": ("Private (raiyati) land", "ব্যক্তিমালিকানাধীন (রায়তি) জমি"), "khas": ("Government khas land", "সরকারি খাস জমি"),
        "acq": ("Under government acquisition", "সরকারি অধিগ্রহণাধীন জমি"), "vested": ("Vested (enemy) property", "অর্পিত সম্পত্তি")}
CASES = [("Title suit", "স্বত্ব মোকদ্দমা"), ("Partition suit", "বণ্টন মোকদ্দমা"), ("Injunction", "নিষেধাজ্ঞা মামলা")]
FL = {
    "forged": ("critical", "Suspected forged deed", "সন্দেহজনক জাল দলিল"),
    "double": ("critical", "Same plot sold to multiple buyers", "একই জমি একাধিক ক্রেতার কাছে বিক্রি"),
    "khas": ("critical", "Khas / acquired / vested land", "খাস / অধিগ্রহণ / অর্পিত সম্পত্তি"),
    "rapid": ("high", "Suspiciously rapid transfers", "সন্দেহজনক দ্রুত হস্তান্তর"),
    "mismatch": ("high", "Registration vs mutation record mismatch", "নিবন্ধন ও নামজারি রেকর্ডের অমিল"),
    "unmut": ("medium", "Deed not mutated / mutation delayed", "দলিল অনুযায়ী নামজারি হয়নি / বিলম্বিত"),
    "case": ("medium", "Pending court case", "আদালতে মামলা বিচারাধীন"),
    "pending": ("low", "Mutation in progress", "নামজারি প্রক্রিয়াধীন"),
}
PUB = {
    "forged": ("Some deed records failed verification. Contact the AC (Land) office before paying anything.", "কিছু দলিলের তথ্য যাচাইয়ে ব্যর্থ। টাকা দেওয়ার আগে এসি (ল্যান্ড) অফিসে যোগাযোগ করুন।"),
    "double": ("Conflicting registrations exist for this plot. Do not buy before the AC (Land) office clears it.", "এই জমির বিপরীতে পরস্পরবিরোধী নিবন্ধন আছে। এসি (ল্যান্ড) অফিসের ছাড়পত্র ছাড়া কিনবেন না।"),
    "khas": ("Land is not freely transferable private property.", "জমিটি অবাধে হস্তান্তরযোগ্য ব্যক্তিমালিকানাধীন নয়।"),
    "rapid": ("Several ownership transfers happened in a very short time.", "খুব অল্প সময়ে একাধিকবার মালিকানা হস্তান্তর হয়েছে।"),
    "mismatch": ("Deed and mutation (khatian) records do not match.", "দলিল ও নামজারি (খতিয়ান) রেকর্ড মিলছে না।"),
    "unmut": ("A registered deed has not been reflected in the khatian yet.", "নিবন্ধিত দলিল এখনো খতিয়ানে প্রতিফলিত হয়নি।"),
    "case": ("A court case is pending on this land.", "এই জমির উপর মামলা বিচারাধীন।"),
    "pending": ("A mutation application is still being processed.", "একটি নামজারি আবেদন প্রক্রিয়াধীন।"),
}
CRIT_CHECKS = {"deed_valid", "nid_match", "no_double", "not_khas"}
OVR = {}  # admin overrides of mutation workflow  case_no -> {status, stage, done}


def make_record(ds, place, p, bb):
    key = f"{ds}:{p.get('plot_no_en')}"
    r = random.Random(int(hashlib.md5(key.encode()).hexdigest()[:10], 16))
    area = round(float(p.get("area_sqm", 0)) / 40.4686, 2)
    ppl = [{"en": n[1], "bn": n[0], "nid": str(r.randint(10 ** 9, 10 ** 10 - 1))} for n in r.sample(NAMES, 6)]
    pbn = PLACES.get(place, (place.title(),))[0]
    office = {"en": f"{place.title()} Sub-Registry Office", "bn": f"{pbn} সাব-রেজিস্ট্রি অফিস"}
    deeds, muts, cases = [], [], []

    def deed(s, b, D, valid=True, nid=True):
        dd = {"id": f"REG-{(TODAY - timedelta(days=D)).year}-{r.randint(10000, 99999)}", "date": dstr(D), "seller": s,
              "buyer": b, "area": area, "serial_valid": valid, "nid_match": nid, "office": office}
        deeds.append(dd)
        return dd

    def mut(dd, D, status, stage=5, rec_owner=None, area_rec=None):
        applied = max(D - r.randint(3, 10), 1)
        done = dstr(max(applied - r.randint(15, 40), 0)) if status == "completed" else None
        muts.append({"case_no": f"MUT-{(TODAY - timedelta(days=applied)).year}-{r.randint(10000, 99999)}",
                     "deed_id": dd["id"], "status": status, "stage": 5 if done else stage, "applied": dstr(applied),
                     "done": done, "owner_recorded": dd["buyer"] if rec_owner is None else rec_owner,
                     "area_recorded": dd["area"] if area_rec is None else area_rec})

    st = {"cur": 0, "nxt": 1}

    def chain(n):
        D = r.randint(1000, 3000)
        for _ in range(n):
            dd = deed(st["cur"], st["nxt"], D)
            mut(dd, D, "completed")
            st["cur"], st["nxt"] = st["nxt"], st["nxt"] + 1
            D -= r.randint(150, 300)

    sc = r.choices(["clean", "pending", "rapid", "double", "forged", "mismatch", "case"], [50, 15, 8, 8, 6, 7, 6])[0]
    if sc in ("clean", "case"):
        chain(r.choice([0, 1, 2]) if sc == "clean" else r.choice([0, 1]))
    elif sc == "pending":
        chain(r.choice([0, 1]))
        D = r.randint(12, 70)
        mut(deed(st["cur"], st["nxt"], D), D, "in_progress", r.randint(1, 4))
    elif sc == "rapid":
        a = deed(0, 1, 80); mut(a, 80, "completed")
        b = deed(1, 2, 50); mut(b, 50, "in_progress", 2)
        deed(2, 3, 20)
    elif sc == "double":
        D1 = r.randint(40, 100)
        a = deed(0, 1, D1); mut(a, D1, "in_progress", 3)
        D2 = D1 - r.randint(5, 25)
        b = deed(0, 2, D2); mut(b, D2, "in_progress", 1)
    elif sc == "forged":
        chain(r.choice([0, 1]))
        D, bad = r.randint(20, 60), r.choice(["serial", "nid"])
        mut(deed(st["cur"], st["nxt"], D, valid=bad != "serial", nid=bad != "nid"), D, "in_progress", 1)
    elif sc == "mismatch":
        chain(r.choice([0, 1]))
        D = r.randint(120, 300)
        dd = deed(st["cur"], st["nxt"], D)
        if r.random() < .5:
            mut(dd, D, "completed", rec_owner=st["nxt"] + 1)
        else:
            mut(dd, D, "completed", area_rec=round(dd["area"] * r.uniform(.6, .9), 2))
    khas = r.choices(["private", "khas", "acq", "vested"], [91, 3, 3, 3])[0]
    if khas != "private" and not deeds:
        D = r.randint(60, 400)
        mut(deed(0, 1, D), D, "completed")
    if sc == "case" or r.random() < .03:
        c = r.choice(CASES)
        cases.append({"no": f"CS-{r.randint(100, 999)}/{TODAY.year - r.randint(0, 3)}", "type": {"en": c[0], "bn": c[1]},
                      "court": {"en": f"{place.title()} Senior Assistant Judge Court", "bn": f"{pbn} সিনিয়র সহকারী জজ আদালত"},
                      "filed": dstr(r.randint(60, 900))})
    return {"key": key, "ds": ds, "place": place, "plot_no": str(p.get("plot_no_en")), "label": p.get("label"),
            "khatian": str(r.randint(100, 999)), "mouza": p.get("m_code"), "jl": p.get("jl_no_en"), "sheet": p.get("sht_no_en"),
            "area_dec": area, "area_sqm": p.get("area_sqm"), "people": ppl, "owner0": 0, "khas": khas, "cases": cases,
            "deeds": deeds, "muts": muts, "scenario": sc, "center": [(bb[1] + bb[3]) / 2, (bb[0] + bb[2]) / 2]}


_reg = {"sig": None, "recs": {}}


def registry():
    sig = signature()
    if _reg["sig"] != sig:
        recs = {}
        for ds, e in scan().items():
            for f in plot_features(ds):
                rec = make_record(ds, e["place"], f["properties"], geom_bbox(f["geometry"]))
                recs[rec["key"]] = rec
        _reg.update(sig=sig, recs=recs)
    return _reg["recs"]


def eff_muts(rec):
    out = []
    for m in rec["muts"]:
        m = dict(m)
        m.update(OVR.get(m["case_no"], {}))
        out.append(m)
    return out


def record_owner(rec):
    o = rec["owner0"]
    for m in sorted([m for m in eff_muts(rec) if m["status"] == "completed"], key=lambda m: m.get("done") or ""):
        o = m["owner_recorded"]
    return o


def mk(code, en, bnt):
    sev, te, tb = FL[code]
    return {"code": code, "sev": sev, "title": {"en": te, "bn": tb}, "detail": {"en": en, "bn": bnt},
            "public": {"en": PUB[code][0], "bn": PUB[code][1]}}


def analyze(rec):
    """Rule engine: forged deeds, double sales, rapid transfers, registry vs mutation mismatch, khas sale ..."""
    fl, P, D, M = [], rec["people"], rec["deeds"], eff_muts(rec)
    n = lambda i: P[i]
    for dd in D:
        if not (dd["serial_valid"] and dd["nid_match"]):
            e, b = (("registry serial not found", "নিবন্ধন সিরিয়াল রেজিস্ট্রিতে পাওয়া যায়নি") if not dd["serial_valid"]
                    else ("seller NID does not match record owner", "বিক্রেতার এনআইডি রেকর্ডের মালিকের সাথে মেলেনি"))
            fl.append(mk("forged", f"Deed {dd['id']}: {e}.", f"দলিল {dd['id']}: {b}।"))
    for a, b in itertools.combinations(D, 2):
        if a["seller"] == b["seller"] and a["buyer"] != b["buyer"] and abs((dt(a["date"]) - dt(b["date"])).days) <= 120:
            fl.append(mk("double", f"{n(a['seller'])['en']} sold the plot to {n(a['buyer'])['en']} ({a['date']}) and {n(b['buyer'])['en']} ({b['date']}).",
                         f"{n(a['seller'])['bn']} জমিটি {n(a['buyer'])['bn']} ({a['date']}) এবং {n(b['buyer'])['bn']} ({b['date']}) কে বিক্রি করেছেন।"))
            break
    recent = [x for x in D if (TODAY - dt(x["date"])).days <= 90]
    if len(recent) >= 3:
        fl.append(mk("rapid", f"{len(recent)} ownership transfers registered within 90 days.", f"৯০ দিনের মধ্যে {bn(len(recent))}টি মালিকানা হস্তান্তর নিবন্ধিত হয়েছে।"))
    dmap = {d["id"]: d for d in D}
    for m in M:
        dd = dmap.get(m["deed_id"])
        if dd and m["status"] == "completed":
            if m["owner_recorded"] != dd["buyer"]:
                fl.append(mk("mismatch", f"Deed {dd['id']} buyer is {n(dd['buyer'])['en']} but khatian was updated to {n(m['owner_recorded'])['en']}.",
                             f"দলিল {dd['id']} এর ক্রেতা {n(dd['buyer'])['bn']}, কিন্তু খতিয়ান হয়েছে {n(m['owner_recorded'])['bn']} এর নামে।"))
            elif abs(m["area_recorded"] - dd["area"]) > max(.5, dd["area"] * .01):
                fl.append(mk("mismatch", f"Deed area {dd['area']} dec vs mutation area {m['area_recorded']} dec.",
                             f"দলিলে জমির পরিমাণ {bn(dd['area'])} শতক, নামজারিতে {bn(m['area_recorded'])} শতক।"))
        if m["status"] in ("in_progress", "on_hold"):
            age = (TODAY - dt(m["applied"])).days
            fl.append(mk("unmut" if age > 45 else "pending", f"Case {m['case_no']} open for {age} days (stage {m['stage'] + 1}/6).",
                         f"মামলা {m['case_no']} {bn(age)} দিন ধরে চলমান (ধাপ {bn(m['stage'] + 1)}/৬)।"))
    for dd in D:
        if not any(m["deed_id"] == dd["id"] for m in M):
            age = (TODAY - dt(dd["date"])).days
            fl.append(mk("unmut" if age > 45 else "pending", f"Deed {dd['id']} registered {age} days ago with no mutation application.",
                         f"দলিল {dd['id']} {bn(age)} দিন আগে নিবন্ধিত, কিন্তু নামজারির আবেদন নেই।"))
    if rec["khas"] != "private":
        k = KHAS[rec["khas"]]
        fl.append(mk("khas", f"Land class: {k[0]}." + (f" {len(D)} sale deed(s) registered on it." if D else ""),
                     f"জমির শ্রেণি: {k[1]}।" + (f" এর উপর {bn(len(D))}টি বিক্রয় দলিল নিবন্ধিত।" if D else "")))
    for c in rec["cases"]:
        fl.append(mk("case", f"{c['type']['en']} {c['no']} pending.", f"{c['type']['bn']} {c['no']} বিচারাধীন।"))
    return fl


def mut_checks(rec, m, flags):
    codes = {f["code"] for f in flags}
    dd = next((d for d in rec["deeds"] if d["id"] == m["deed_id"]), None)
    owner = rec["owner0"]
    for x in sorted([x for x in eff_muts(rec) if x["status"] == "completed" and x["case_no"] != m["case_no"]], key=lambda x: x["done"] or ""):
        if dd and x["done"] and x["done"] <= dd["date"]:
            owner = x["owner_recorded"]
    return [{"code": "deed_valid", "ok": bool(dd and dd["serial_valid"])}, {"code": "nid_match", "ok": bool(dd and dd["nid_match"])},
            {"code": "seller_owner", "ok": bool(dd and dd["seller"] == owner)},
            {"code": "area_match", "ok": bool(dd and abs(m["area_recorded"] - dd["area"]) <= max(.5, dd["area"] * .01))},
            {"code": "no_case", "ok": not rec["cases"]}, {"code": "not_khas", "ok": rec["khas"] == "private"},
            {"code": "no_double", "ok": "double" not in codes}]


SEV_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1}


def verdict(fl):
    return "danger" if any(f["sev"] in ("critical", "high") for f in fl) else ("caution" if fl else "safe")


def report(rec, admin):
    fl = analyze(rec)
    codes = {f["code"] for f in fl}
    P = rec["people"]
    own = P[record_owner(rec)]
    nid = own["nid"] if admin else "•••• •••• " + own["nid"][-4:]
    flags = [{"code": f["code"], "sev": f["sev"], "title": f["title"], "detail": f["detail"] if admin else f["public"]} for f in fl]
    flags.sort(key=lambda f: -SEV_RANK[f["sev"]])
    muts = []
    for m in eff_muts(rec):
        dd = next((d for d in rec["deeds"] if d["id"] == m["deed_id"]), None)
        muts.append({**m, "applicant": P[dd["buyer"]] if dd else None, "checks": mut_checks(rec, m, fl) if admin else None})
    return {"key": rec["key"], "ds": rec["ds"], "plot_no": rec["plot_no"], "label": rec["label"], "khatian": rec["khatian"],
            "mouza": rec["mouza"], "jl": rec["jl"], "sheet": rec["sheet"], "area_dec": rec["area_dec"], "area_sqm": rec["area_sqm"],
            "place": rec["place"], "center": rec["center"],
            "owner": {"en": own["en"], "bn": own["bn"], "nid": nid},
            "khas": {"code": rec["khas"], "en": KHAS[rec["khas"]][0], "bn": KHAS[rec["khas"]][1]},
            "verdict": verdict(fl), "flags": flags,
            "checklist": [{"code": "owner", "ok": not codes & {"mismatch", "forged", "double"}}, {"code": "mut", "ok": not codes & {"unmut", "pending"}},
                          {"code": "case", "ok": "case" not in codes}, {"code": "khas", "ok": "khas" not in codes},
                          {"code": "fraud", "ok": not codes & {"forged", "double", "rapid"}}],
            "deeds": [{**d, "seller": P[d["seller"]], "buyer": P[d["buyer"]], "verified": d["serial_valid"] and d["nid_match"]} for d in rec["deeds"]],
            "muts": [{k: v for k, v in m.items() if k != "owner_recorded"} for m in muts], "cases": rec["cases"]}


def is_admin():
    return request.headers.get("X-Role") == "admin"


def admin_only():
    return None if is_admin() else (jsonify(error="admin only"), 403)


# =============================================================== API
@app.route("/api/datasets")
def api_datasets():
    out = []
    for ds, e in scan().items():
        feats = plot_features(ds)
        land, plot = load_ds(ds)
        bb = plot["bbox"] or land["bbox"] or ([feats[0] and geom_bbox(feats[0]["geometry"])][0] if feats else None)
        if not bb:
            continue
        out.append({"id": ds, "place": e["place"], "place_bn": PLACES.get(e["place"], (e["place"].title(),))[0], "sheet": e["sheet"],
                    "plots": len(feats), "parts": len(land["features"]), "area_dec": round(sum(f["properties"]["area_sqm"] for f in feats) / 40.4686, 1),
                    "center": [(bb[1] + bb[3]) / 2, (bb[0] + bb[2]) / 2], "bbox": bb,
                    "files": [os.path.basename(p) for p in (e["land"], e["plot"]) if p]})
    return jsonify(out)


@app.route("/api/dataset/<ds>")
def api_dataset(ds):
    d = load_ds(ds)
    if not d:
        return jsonify(error="not found"), 404
    return jsonify(id=ds, land=d[0], plot=d[1])


@app.route("/api/version")
def api_version():
    return jsonify(version=[[os.path.basename(p), m] for p, m in signature()])


@app.route("/api/find")
def api_find():
    q, ds = (request.args.get("q") or "").strip().lower(), request.args.get("ds") or ""
    res = []
    for rec in registry().values():
        if ds and rec["ds"] != ds:
            continue
        hit = q in (rec["plot_no"].lower(), rec["khatian"].lower())
        if is_admin() and q and len(q) > 2:
            hit = hit or any(q in p["en"].lower() or q in p["bn"] for p in rec["people"][:1])
        if hit:
            res.append({"ds": rec["ds"], "plot_no": rec["plot_no"], "khatian": rec["khatian"], "place": rec["place"],
                        "sheet": rec["sheet"], "area_dec": rec["area_dec"]})
    return jsonify(res[:30])


@app.route("/api/verify")
def api_verify():
    rec = registry().get(f"{request.args.get('ds')}:{request.args.get('plot')}")
    return (jsonify(report(rec, is_admin())) if rec else (jsonify(error="plot not found"), 404))


@app.route("/api/track/<case>")
def api_track(case):
    case = case.strip().upper()
    for rec in registry().values():
        for m in eff_muts(rec):
            if m["case_no"] == case:
                fl = analyze(rec)
                ap = next((rec["people"][d["buyer"]] for d in rec["deeds"] if d["id"] == m["deed_id"]), None)
                return jsonify(case_no=case, ds=rec["ds"], plot_no=rec["plot_no"], khatian=rec["khatian"], status=m["status"],
                               stage=m["stage"], applied=m["applied"], done=m["done"], deed_id=m["deed_id"], applicant=ap,
                               checks=mut_checks(rec, m, fl))
    return jsonify(error="case not found"), 404


@app.route("/api/samples")
def api_samples():
    pl, seen, cases = [], set(), []
    for rec in registry().values():
        if rec["scenario"] not in seen and rec["scenario"] != "case":
            seen.add(rec["scenario"])
            pl.append({"ds": rec["ds"], "plot": rec["plot_no"], "kind": rec["scenario"]})
        if len(cases) < 3 and rec["muts"] and rec["muts"][-1]["status"] == "in_progress":
            cases.append(rec["muts"][-1]["case_no"])
    return jsonify(plots=pl[:5], cases=cases)


@app.route("/api/admin/risk/<ds>")
def api_risk(ds):
    if (r := admin_only()):
        return r
    out = {}
    for rec in registry().values():
        if rec["ds"] == ds:
            fl = analyze(rec)
            if fl:
                out[rec["plot_no"]] = {"sev": max((f["sev"] for f in fl), key=SEV_RANK.get), "codes": [f["code"] for f in fl]}
    return jsonify(out)


@app.route("/api/admin/summary")
def api_summary():
    if (r := admin_only()):
        return r
    by_code, by_sev, per_ds, stages = {}, {}, {}, [0] * 5
    status = {"completed": 0, "in_progress": 0, "on_hold": 0, "rejected": 0}
    days, flagged, crit = [], 0, 0
    for rec in registry().values():
        fl = [f for f in analyze(rec) if f["code"] != "pending"]
        d = per_ds.setdefault(rec["ds"], {"ds": rec["ds"], "plots": 0, "flagged": 0, "critical": 0})
        d["plots"] += 1
        for f in fl:
            by_code[f["code"]] = by_code.get(f["code"], 0) + 1
            by_sev[f["sev"]] = by_sev.get(f["sev"], 0) + 1
        if fl:
            flagged += 1; d["flagged"] += 1
        if any(f["sev"] == "critical" for f in fl):
            crit += 1; d["critical"] += 1
        for m in eff_muts(rec):
            status[m["status"]] += 1
            if m["status"] == "in_progress":
                stages[min(m["stage"], 4)] += 1
            if m["status"] == "completed" and m["done"]:
                days.append((dt(m["done"]) - dt(m["applied"])).days)
    return jsonify(plots=len(registry()), flagged=flagged, critical=crit, by_code=by_code, by_sev=by_sev, status=status, stages=stages,
                   avg_days=round(sum(days) / len(days), 1) if days else 0, per_ds=list(per_ds.values()))


@app.route("/api/admin/alerts")
def api_alerts():
    if (r := admin_only()):
        return r
    rows = []
    for rec in registry().values():
        for f in analyze(rec):
            if f["code"] != "pending":
                rows.append({"ds": rec["ds"], "plot": rec["plot_no"], "khatian": rec["khatian"], "code": f["code"], "sev": f["sev"],
                             "title": f["title"], "detail": f["detail"], "area": rec["area_dec"]})
    rows.sort(key=lambda x: -SEV_RANK[x["sev"]])
    return jsonify(rows[:600])


@app.route("/api/admin/mutations")
def api_mutations():
    if (r := admin_only()):
        return r
    rows = []
    for rec in registry().values():
        fl = analyze(rec)
        for m in eff_muts(rec):
            ch = mut_checks(rec, m, fl)
            ap = next((rec["people"][d["buyer"]] for d in rec["deeds"] if d["id"] == m["deed_id"]), None)
            rows.append({"case_no": m["case_no"], "ds": rec["ds"], "plot": rec["plot_no"], "deed_id": m["deed_id"], "status": m["status"],
                         "stage": m["stage"], "applied": m["applied"], "applicant": ap, "checks": ch,
                         "blocked": any(not c["ok"] and c["code"] in CRIT_CHECKS for c in ch)})
    rows.sort(key=lambda x: x["applied"], reverse=True)
    return jsonify(rows)


def find_mut(case):
    for rec in registry().values():
        for m in eff_muts(rec):
            if m["case_no"] == case:
                return rec, m
    return None, None


def advance(rec, m):
    """Move one stage forward unless a critical automated check fails."""
    ch = mut_checks(rec, m, analyze(rec))
    if any(not c["ok"] and c["code"] in CRIT_CHECKS for c in ch):
        return False
    st = m["stage"] + 1
    OVR[m["case_no"]] = {"status": "completed", "stage": 5, "done": TODAY.isoformat()} if st >= 5 else {"stage": st, "status": "in_progress"}
    return True


@app.route("/api/admin/mutation/<case>/<act>", methods=["POST"])
def api_mut_act(case, act):
    if (r := admin_only()):
        return r
    rec, m = find_mut(case)
    if not m:
        return jsonify(error="case not found"), 404
    if m["status"] in ("completed", "rejected"):
        return jsonify(error="case already closed"), 409
    if act == "advance":
        if not advance(rec, m):
            return jsonify(error="blocked", blockers=[c["code"] for c in mut_checks(rec, m, analyze(rec)) if not c["ok"] and c["code"] in CRIT_CHECKS]), 409
    elif act in ("reject", "hold", "resume"):
        OVR.setdefault(case, {}).update(status={"reject": "rejected", "hold": "on_hold", "resume": "in_progress"}[act])
    else:
        return jsonify(error="bad action"), 400
    return jsonify(ok=True)


@app.route("/api/admin/automate", methods=["POST"])
def api_automate():
    if (r := admin_only()):
        return r
    done = held = 0
    for rec in list(registry().values()):
        for m in eff_muts(rec):
            if m["status"] == "in_progress":
                if advance(rec, m):
                    done += 1
                else:
                    held += 1
    return jsonify(advanced=done, flagged=held)


@app.route("/api/admin/upload", methods=["POST"])
def api_upload():
    if (r := admin_only()):
        return r
    name = os.path.basename(request.args.get("name", ""))
    if not NAME_RE.match(name):
        return jsonify(error="File name must look like  gazipur_001_land.json  or  gazipur_001_plot.json"), 400
    try:
        data = json.loads(request.get_data(as_text=True))
    except Exception as e:
        return jsonify(error=f"Invalid JSON: {e}"), 400
    os.makedirs(DATA, exist_ok=True)
    with open(os.path.join(DATA, name), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    return jsonify(ok=True)


@app.route("/")
def index():
    return Response(PAGE, mimetype="text/html")


# =============================================================== Front-end
PAGE = r"""<!doctype html>
<html lang="bn" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ভূমি সুরক্ষা · Bhumi Suraksha</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Hind+Siliguri:wght@400;500;600;700&family=Tiro+Bangla:ital@0;1&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
:root{--bg:#f2f7f1;--sf:#fff;--sf2:#e8f2ea;--tx:#10281c;--mut:#587063;--ln:#d4e3d8;--g:#0f7a4d;--g2:#17a468;--gd:#06301f;--gl:#d7f0df;--onp:#fff;--sand:#efe3b8;
 --red:#d62839;--amb:#c98512;--blu:#2f6fdd;--sh:0 14px 34px -16px rgba(6,48,31,.35);color-scheme:light}
[data-theme=dark]{--bg:#06120c;--sf:#0d1f16;--sf2:#14291f;--tx:#e3f3ea;--mut:#8db09c;--ln:#1e3a2b;--g:#2ecc88;--g2:#1fae70;--gd:#031a10;--gl:#113424;--onp:#04210f;--sand:#3a3316;
 --red:#ff5d6b;--amb:#f0b13b;--blu:#6aa0ff;--sh:0 14px 34px -16px rgba(0,0,0,.8);color-scheme:dark}
*{box-sizing:border-box}html,body{margin:0;min-height:100%}
body{font-family:'Hind Siliguri',system-ui,'Segoe UI',sans-serif;background:var(--bg);color:var(--tx);font-size:16px;line-height:1.55}
h1,h2,h3{font-family:'Tiro Bangla','Hind Siliguri',serif;font-weight:400;line-height:1.2;margin:0}
a{color:var(--g);text-decoration:none}button,input,select{font:inherit;color:inherit}
:focus-visible{outline:3px solid var(--g2);outline-offset:2px}
.wrap{max-width:1180px;margin:0 auto;padding:0 20px}
/* header */
.nav{position:sticky;top:0;z-index:2000;background:color-mix(in srgb,var(--sf) 88%,transparent);backdrop-filter:blur(14px);border-bottom:1px solid var(--ln)}
.nav-in{display:flex;align-items:center;gap:14px;height:64px;max-width:1400px;margin:0 auto;padding:0 18px}
.brand{display:flex;align-items:center;gap:10px;color:var(--tx)}
.logo{width:40px;height:40px;border-radius:12px;background:var(--gd);display:grid;place-items:center;position:relative;color:#fff;font-family:'Tiro Bangla',serif;font-size:21px}
.logo:after{content:"";position:absolute;right:-3px;top:-3px;width:11px;height:11px;border-radius:50%;background:var(--red);border:2px solid var(--sf)}
.brand b{display:block;font-size:17px;line-height:1.1}.brand small{color:var(--mut);font-size:11.5px}
.links{display:flex;gap:2px;margin-left:12px;flex:1;overflow-x:auto;scrollbar-width:none}
.links a{padding:8px 13px;border-radius:10px;color:var(--mut);font-weight:500;white-space:nowrap}
.links a:hover{background:var(--sf2);color:var(--tx)}.links a.on{background:var(--gl);color:var(--g)}
.links .sep{width:1px;background:var(--ln);margin:6px 8px}
.tools{display:flex;gap:8px}
.ib{height:38px;min-width:38px;padding:0 12px;border-radius:10px;border:1px solid var(--ln);background:var(--sf);cursor:pointer;white-space:nowrap}
.ib:hover{border-color:var(--g)}.ib.adm{background:var(--gd);color:#fff;border-color:var(--gd)}
#burger{display:none}
@media(max-width:860px){#burger{display:block}.links{position:fixed;inset:64px 0 auto 0;flex-direction:column;background:var(--sf);padding:10px;margin:0;border-bottom:1px solid var(--ln);display:none}
 .links.open{display:flex}.links .sep{display:none}.brand small{display:none}}
/* generic */
.btn{border:1px solid var(--ln);background:var(--sf);padding:.6rem 1.05rem;border-radius:12px;cursor:pointer;font-weight:500;display:inline-flex;align-items:center;gap:6px}
.btn:hover{border-color:var(--g)}.btn.pri{background:var(--g);color:var(--onp);border-color:transparent}.btn.pri:hover{filter:brightness(1.08)}
.btn.sm{padding:.3rem .7rem;font-size:13px;border-radius:9px}.btn.danger{color:var(--red)}
.card{background:var(--sf);border:1px solid var(--ln);border-radius:18px;box-shadow:var(--sh)}
input,select{background:var(--bg);border:1px solid var(--ln);border-radius:12px;padding:.62rem .8rem;width:100%;min-width:0}
label{display:block;font-size:13px;color:var(--mut);font-weight:500}label input,label select{margin-top:4px;color:var(--tx)}
.pill{display:inline-block;padding:3px 12px;border-radius:99px;font-size:13px;background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.25)}
.bdg{display:inline-block;padding:2px 10px;border-radius:99px;font-size:12px;font-weight:600;white-space:nowrap}
.critical,.bad{background:color-mix(in srgb,var(--red) 16%,transparent);color:var(--red)}
.high{background:color-mix(in srgb,#e8731a 18%,transparent);color:#d35f06}
.medium,.warn{background:color-mix(in srgb,var(--amb) 18%,transparent);color:var(--amb)}
.low,.info{background:color-mix(in srgb,var(--blu) 15%,transparent);color:var(--blu)}.ok{background:var(--gl);color:var(--g)}
.tw{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:14px}
th{text-align:left;color:var(--mut);font-weight:500;padding:10px 12px;border-bottom:1px solid var(--ln);white-space:nowrap}
td{padding:11px 12px;border-bottom:1px solid var(--ln);vertical-align:middle}tr.cl{cursor:pointer}tr.cl:hover td{background:var(--sf2)}
.page{padding:30px 0 60px}.page>.wrap>h2{font-size:30px}.page .lead{color:var(--mut);margin:6px 0 22px;max-width:70ch}
.grid{display:grid;gap:16px}.g2{grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}.g4{grid-template-columns:repeat(auto-fit,minmax(200px,1fr))}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px;align-items:center}
.chip{padding:5px 12px;border-radius:99px;border:1px dashed var(--ln);background:var(--sf);cursor:pointer;font-size:13px}.chip:hover{border-color:var(--g);color:var(--g)}
/* hero */
.hero{background:radial-gradient(900px 400px at 85% -10%,rgba(46,204,136,.28),transparent 60%),radial-gradient(600px 300px at 0 100%,rgba(239,227,184,.12),transparent 60%),var(--gd);color:#e9f7ee;padding:56px 0 70px}
.hero-in{display:grid;grid-template-columns:1.25fr .75fr;gap:40px;align-items:center}
.hero h1{font-size:clamp(34px,5vw,56px);margin:16px 0 12px}.hero p{font-size:18px;color:#bfdccb;max-width:56ch;margin:0 0 26px}
.finder{padding:18px;color:var(--tx)}.fgrid{display:grid;grid-template-columns:1fr 1fr 1fr auto;gap:10px;align-items:end}
.or{display:flex;align-items:center;gap:10px;color:var(--mut);font-size:13px;margin:14px 0}.or:before,.or:after{content:"";flex:1;height:1px;background:var(--ln)}
.gs{display:flex;gap:10px}.hero .chips{color:#bfdccb}.hero .chip{background:rgba(255,255,255,.08);border-color:rgba(255,255,255,.25);color:#e9f7ee}
.art{position:relative}.art svg{width:100%;height:auto;border-radius:22px;box-shadow:0 30px 60px -20px rgba(0,0,0,.6)}
.art .tag{position:absolute;background:var(--sf);color:var(--tx);padding:8px 14px;border-radius:14px;box-shadow:var(--sh);font-size:13px;font-weight:600}
@media(max-width:900px){.hero-in{grid-template-columns:1fr}.art{display:none}.fgrid{grid-template-columns:1fr 1fr}.fgrid button{grid-column:1/-1}}
.stats{margin-top:-34px;position:relative}.stats .grid{padding:6px}
.stat{padding:18px 20px}.stat b{font-family:'Tiro Bangla',serif;font-size:32px;font-weight:400;color:var(--g);display:block;line-height:1.1}.stat span{color:var(--mut);font-size:14px}
.feat{padding:22px;display:flex;flex-direction:column;gap:8px;color:var(--tx);transition:transform .2s,border-color .2s}.feat:hover{transform:translateY(-3px);border-color:var(--g)}
.feat i{font-style:normal;width:46px;height:46px;border-radius:14px;background:var(--gl);display:grid;place-items:center;font-size:23px}.feat h3{font-size:21px}.feat p{margin:0;color:var(--mut);font-size:14.5px}
.sec{padding:44px 0}.sec h2{font-size:30px;margin-bottom:18px}
.steps{counter-reset:s;display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:0;position:relative}
.step{padding:6px 18px 6px 0;position:relative}.step:before{counter-increment:s;content:counter(s,bengali);width:34px;height:34px;border-radius:50%;background:var(--g);color:var(--onp);display:grid;place-items:center;font-weight:700;margin-bottom:10px}
.step b{display:block}.step span{color:var(--mut);font-size:14px}
footer{background:var(--gd);color:#a9c9b7;padding:30px 0;font-size:14px}footer b{color:#fff}
/* map */
.mapwrap{position:relative;height:calc(100vh - 64px);height:calc(100dvh - 64px)}#map{position:absolute;inset:0;z-index:1}
#panel{position:absolute;z-index:1000;top:12px;left:12px;bottom:12px;width:350px;background:color-mix(in srgb,var(--sf) 94%,transparent);backdrop-filter:blur(12px);border:1px solid var(--ln);border-radius:20px;box-shadow:var(--sh);display:flex;flex-direction:column;overflow:hidden;transition:transform .25s}
#panel.hide{transform:translateX(-370px)}.ph{padding:14px 16px 8px}.ph b{font-family:'Tiro Bangla',serif;font-size:19px;font-weight:400}.ph small{display:block;color:var(--mut)}
.pb{overflow:auto;padding:0 16px 16px;flex:1}.pb h4{margin:16px 0 8px;font-size:13px;color:var(--mut);font-weight:600}
.pb .two{display:grid;grid-template-columns:1fr 1fr;gap:8px}.mini{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin:10px 0}.mini div{background:var(--sf2);border-radius:12px;padding:8px;text-align:center}.mini b{display:block;font-size:17px}.mini span{font-size:11px;color:var(--mut)}
.row{display:flex;align-items:center;gap:8px;padding:6px 6px;border-radius:9px;font-size:13.5px;cursor:pointer}.row:hover{background:var(--sf2)}.row .n{margin-left:auto;color:var(--mut);font-size:12px;white-space:nowrap}
.sw{width:14px;height:14px;border-radius:4px;flex:none;border:1px solid rgba(0,0,0,.25)}.plots{max-height:170px;overflow:auto;margin-top:6px}
.geores{margin-top:6px;border:1px solid var(--ln);border-radius:12px;overflow:hidden}.geores div{padding:8px 10px;font-size:13px;cursor:pointer;border-bottom:1px solid var(--ln)}.geores div:hover{background:var(--sf2)}.geores div:last-child{border:0}
#ptoggle{position:absolute;z-index:1001;top:20px;left:372px;width:40px;height:40px;border-radius:12px;border:1px solid var(--ln);background:var(--sf);cursor:pointer;transition:left .25s}
#panel.hide+#ptoggle{left:12px}
.drawer{position:absolute;z-index:1100;top:12px;right:12px;bottom:12px;width:430px;max-width:calc(100% - 24px);background:var(--sf);border:1px solid var(--ln);border-radius:20px;box-shadow:var(--sh);overflow:auto;padding:18px;transform:translateX(110%);transition:transform .28s}
.drawer.open{transform:none}.drawer .x{position:absolute;right:12px;top:12px;z-index:2}
@media(max-width:700px){#panel{right:12px;width:auto;bottom:auto;max-height:58%}#ptoggle{left:auto;right:12px;top:12px}#panel.hide+#ptoggle{left:auto}#panel.hide{transform:translateY(-120%)}.drawer{top:auto;height:78%;width:auto;left:12px;transform:translateY(110%)}}
.leaflet-popup-content-wrapper,.leaflet-popup-tip{background:var(--sf);color:var(--tx)}.leaflet-popup-content{font-family:inherit}
.pop h4{margin:0 0 6px;font-size:16px}.pop table{font-size:13px}.pop td{padding:2px 8px 2px 0;border:0}.pop td:first-child{color:var(--mut)}
.plot-lbl{background:rgba(0,0,0,.68);color:#fff;border:0;border-radius:6px;padding:1px 6px;font-weight:600;box-shadow:none}.plot-lbl:before{display:none}.nolabels .plot-lbl{display:none}
/* report */
.rep{display:flex;flex-direction:column;gap:14px}.rep h3{font-size:22px}
.vd{display:flex;gap:14px;align-items:center;padding:16px;border-radius:16px}.vd .ic{width:54px;height:54px;border-radius:50%;display:grid;place-items:center;font-size:26px;font-weight:700;flex:none;color:#fff}
.vd.safe{background:var(--gl)}.vd.safe .ic{background:var(--g)}.vd.caution{background:color-mix(in srgb,var(--amb) 16%,var(--sf))}.vd.caution .ic{background:var(--amb)}.vd.danger{background:color-mix(in srgb,var(--red) 14%,var(--sf))}.vd.danger .ic{background:var(--red)}
.vd b{font-size:18px;display:block}.vd span{font-size:13.5px;color:var(--mut)}
.kv{display:grid;grid-template-columns:repeat(2,1fr);gap:8px 14px}.kv div{font-size:14px}.kv small{display:block;color:var(--mut);font-size:12px}
.cl{list-style:none;margin:0;padding:0;display:grid;gap:6px}.cl li{display:flex;gap:10px;align-items:center;font-size:14.5px}.cl li i{font-style:normal;width:22px;height:22px;border-radius:50%;display:grid;place-items:center;font-size:12px;color:#fff;flex:none}
.cl .y i{background:var(--g)}.cl .n i{background:var(--red)}
.flag{padding:10px 12px;border-radius:12px;border:1px solid var(--ln);font-size:14px}.flag b{display:block}.flag p{margin:2px 0 0;color:var(--mut);font-size:13px}
.tl{position:relative;padding-left:22px;display:grid;gap:12px}.tl:before{content:"";position:absolute;left:7px;top:6px;bottom:6px;width:2px;background:var(--ln)}
.ev{position:relative;font-size:14px}.ev:before{content:"";position:absolute;left:-21px;top:6px;width:12px;height:12px;border-radius:50%;background:var(--g);border:2px solid var(--sf)}.ev.bad:before{background:var(--red)}.ev small{color:var(--mut);display:block}
.stp{display:flex;gap:0;overflow-x:auto;padding:8px 0}.stp div{flex:1;min-width:92px;text-align:center;font-size:12.5px;position:relative;color:var(--mut)}
.stp div:before{content:"";display:block;width:26px;height:26px;border-radius:50%;margin:0 auto 6px;background:var(--sf2);border:2px solid var(--ln);position:relative;z-index:1}
.stp div:after{content:"";position:absolute;top:13px;left:calc(50% + 14px);right:calc(-50% + 14px);height:2px;background:var(--ln)}.stp div:last-child:after{display:none}
.stp .d{color:var(--tx)}.stp .d:before{background:var(--g);border-color:var(--g)}.stp .d:after{background:var(--g)}.stp .c{color:var(--g);font-weight:700}.stp .c:before{background:var(--sf);border-color:var(--g);box-shadow:0 0 0 4px var(--gl)}
.seg{display:flex;gap:3px;min-width:90px}.seg i{flex:1;height:7px;border-radius:4px;background:var(--ln)}.seg i.on{background:var(--g2)}
.bar{display:grid;grid-template-columns:150px 1fr 36px;gap:10px;align-items:center;font-size:13.5px;margin:7px 0}.bar u{height:12px;border-radius:6px;background:var(--sf2);position:relative;overflow:hidden;text-decoration:none}.bar u i{position:absolute;inset:0 auto 0 0;border-radius:6px}
.kpi{padding:18px}.kpi b{font-family:'Tiro Bangla',serif;font-size:34px;font-weight:400;display:block;line-height:1.1}.kpi span{color:var(--mut);font-size:14px}
.donut{width:150px;height:150px;border-radius:50%;display:grid;place-items:center;margin:auto}.donut div{width:98px;height:98px;border-radius:50%;background:var(--sf);display:grid;place-items:center;text-align:center;font-size:12px;color:var(--mut)}.donut b{display:block;font-size:24px;color:var(--tx)}
.modal{position:fixed;inset:0;z-index:3000;background:rgba(0,0,0,.55);display:grid;place-items:center;padding:16px}.mbox{background:var(--sf);border-radius:22px;max-width:620px;width:100%;max-height:90vh;overflow:auto;padding:22px;position:relative}.mbox .x{position:absolute;right:14px;top:14px}
#toast{position:fixed;z-index:4000;left:50%;bottom:24px;transform:translateX(-50%);background:var(--gd);color:#fff;padding:10px 18px;border-radius:12px;display:none;max-width:90%;box-shadow:var(--sh)}
.filters{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px}.filters>*{flex:1;min-width:150px}.empty{padding:34px;text-align:center;color:var(--mut)}
@media(prefers-reduced-motion:reduce){*{transition:none!important}}
</style>
</head>
<body>
<header class="nav"><div class="nav-in" id="chrome"></div></header>
<main id="app"></main>
<div id="toast"></div>
<script>
/* ============================================================ i18n */
const D={
brand:['Bhumi Suraksha','ভূমি সুরক্ষা'],tag:['Digital land services · Bangladesh','ডিজিটাল ভূমি সেবা · বাংলাদেশ'],
n_home:['Home','হোম'],n_map:['Land map','ভূমি মানচিত্র'],n_verify:['Verify before you buy','কেনার আগে যাচাই'],n_track:['Track mutation','নামজারি ট্র্যাক'],
n_dash:['Dashboard','ড্যাশবোর্ড'],n_alerts:['Fraud alerts','জালিয়াতি সতর্কতা'],n_mut:['e-Mutation workflow','ই-নামজারি'],n_data:['Map data','ম্যাপ ডেটা'],
r_user:['General user','সাধারণ ব্যবহারকারী'],r_admin:['Admin mode','অ্যাডমিন মোড'],need_admin:['Switch to Admin mode to open this page.','এই পাতা দেখতে অ্যাডমিন মোডে যান।'],
pill:['MVP · demo data','এমভিপি · ডেমো ডেটা'],h1:['Find your land. Verify before you buy.','আপনার জমি খুঁজুন। কেনার আগে যাচাই করুন।'],
h1p:['Locate any plot on the map, see its ownership, mutation history, pending cases and khas status — before you pay a taka.','মানচিত্রে যেকোনো দাগ খুঁজুন, মালিকানা, নামজারির ইতিহাস, চলমান মামলা ও খাস জমির অবস্থা দেখুন — এক টাকা দেওয়ার আগেই।'],
f_place:['District / place','জেলা / এলাকা'],f_sheet:['Map sheet','শিট'],f_plot:['Plot (dag) no.','দাগ নং'],f_plot_ph:['e.g. 7','যেমন ৭'],f_go:['Find on map','মানচিত্রে দেখুন'],
or:['or search a place','অথবা এলাকা খুঁজুন'],geo_ph:['Village, union, upazila or landmark…','গ্রাম, ইউনিয়ন, উপজেলা বা পরিচিত স্থান…'],search:['Search','খুঁজুন'],try:['Try a demo plot:','ডেমো দাগ দেখুন:'],
st_places:['Districts covered','জেলা'],st_sheets:['Map sheets','ম্যাপ শিট'],st_plots:['Plots indexed','দাগ'],st_area:['Decimal mapped','শতক ম্যাপকৃত'],
ft1:['Land map','ভূমি মানচিত্র'],ft1p:['Satellite and street maps with plot boundaries, land use and area in decimal and acre.','স্যাটেলাইট ও সড়ক মানচিত্রে দাগের সীমানা, ভূমির ব্যবহার এবং শতক/একরে পরিমাণ।'],
ft2:['Check before you buy','কেনার আগে যাচাই'],ft2p:['Owner, mutation history, pending cases and khas or acquisition status in one report.','মালিক, নামজারির ইতিহাস, মামলা এবং খাস/অধিগ্রহণ অবস্থা এক রিপোর্টে।'],
ft3:['Track your mutation','নামজারি ট্র্যাক'],ft3p:['Follow your e-mutation case from deed registration to the updated khatian.','দলিল নিবন্ধন থেকে হালনাগাদ খতিয়ান পর্যন্ত আপনার নামজারি মামলা অনুসরণ করুন।'],
ft4:['Fraud detection (admin)','জালিয়াতি শনাক্তকরণ (অ্যাডমিন)'],ft4p:['Flags forged deeds, double sales, rapid transfers and registry–mutation mismatches.','জাল দলিল, একই জমি দুবার বিক্রি, দ্রুত হস্তান্তর ও নিবন্ধন–নামজারির অমিল চিহ্নিত করে।'],
how:['How it works','কীভাবে কাজ করে'],hw1:['Find the plot','দাগ খুঁজুন'],hw1s:['Pick district and sheet, or search a place on the map.','জেলা ও শিট বাছুন, অথবা মানচিত্রে এলাকা খুঁজুন।'],
hw2:['Run the check','যাচাই করুন'],hw2s:['We compare deed, mutation, case and khas records.','আমরা দলিল, নামজারি, মামলা ও খাস রেকর্ড মিলিয়ে দেখি।'],
hw3:['Read the verdict','ফলাফল দেখুন'],hw3s:['Safe, caution or high risk — with the reasons.','নিরাপদ, সতর্কতা বা উচ্চ ঝুঁকি — কারণসহ।'],hw4:['Confirm at the office','অফিসে নিশ্চিত করুন'],hw4s:['Always confirm with the AC (Land) office before paying.','টাকা দেওয়ার আগে এসি (ল্যান্ড) অফিসে নিশ্চিত হোন।'],
foot:['Demo data only — not a legal record. Always confirm with the local land office.','শুধু ডেমো ডেটা — আইনি রেকর্ড নয়। স্থানীয় ভূমি অফিসে নিশ্চিত হোন।'],
sheet:['Sheet','শিট'],plots:['plots','দাগ'],all_sheets:['All sheets','সব শিট'],dec:['dec','শতক'],acre:['acre','একর'],sqm:['m²','বর্গমিটার'],
loc_me:['📍 My location','📍 আমার অবস্থান'],geo_none:['No place found','কোনো স্থান পাওয়া যায়নি'],geo_near:['Land data loaded for this area','এই এলাকার ভূমি ডেটা লোড হয়েছে'],geo_nodata:['No land sheet near this place yet','এই স্থানের কাছে এখনো কোনো ভূমি শিট নেই'],
find_plot:['Find a plot','দাগ খুঁজুন'],plot_ph:['Plot no…','দাগ নং…'],classes:['Land-use classes','ভূমির শ্রেণি'],opacity:['Land-use opacity','স্বচ্ছতা'],lbls:['Show plot labels','দাগ নম্বর দেখান'],risk:['Show risk overlay (admin)','ঝুঁকি স্তর দেখান (অ্যাডমিন)'],
s_plots:['Plots','দাগ'],s_parts:['Parts','অংশ'],s_dec:['Decimal','শতক'],plot:['Plot','দাগ'],mouza:['Mouza code','মৌজা কোড'],khatian:['Khatian','খতিয়ান'],jl:['JL no.','জেএল নং'],area:['Area','পরিমাণ'],
verify_btn:['Verify this plot','এই দাগ যাচাই করুন'],full:['Open full report','পূর্ণ রিপোর্ট'],address:['Address (OpenStreetMap)','ঠিকানা (ওপেনস্ট্রিটম্যাপ)'],subclass:['Sub class','উপ-শ্রেণি'],
v_h:['Check before you buy','কেনার আগে যাচাই'],v_p:['Enter a plot or khatian number to see the ownership record, history and risks for that land.','জমির মালিকানা, ইতিহাস ও ঝুঁকি দেখতে দাগ বা খতিয়ান নম্বর দিন।'],
v_q:['Plot or khatian no.','দাগ বা খতিয়ান নং'],v_go:['Verify land','জমি যাচাই করুন'],v_none:['No plot matched. Check the number and the sheet.','কোনো দাগ মেলেনি। নম্বর ও শিট যাচাই করুন।'],v_pick:['Several matches — pick one','একাধিক ফল — একটি বাছুন'],
v_safe:['No red flags found','কোনো ঝুঁকির চিহ্ন পাওয়া যায়নি'],v_caution:['Proceed with caution','সতর্কতার সাথে এগোন'],v_danger:['High risk — do not pay yet','উচ্চ ঝুঁকি — এখনই টাকা দেবেন না'],
v_safe_s:['Records are consistent. Still confirm at the AC (Land) office.','রেকর্ড সামঞ্জস্যপূর্ণ। তবুও এসি (ল্যান্ড) অফিসে নিশ্চিত হোন।'],v_caution_s:['Some items need your attention before buying.','কেনার আগে কিছু বিষয় দেখা দরকার।'],v_danger_s:['Serious issues found. Consult the AC (Land) office or a lawyer first.','গুরুতর সমস্যা পাওয়া গেছে। আগে এসি (ল্যান্ড) অফিস বা আইনজীবীর পরামর্শ নিন।'],
owner:['Record owner','রেকর্ডীয় মালিক'],nid:['NID','এনআইডি'],khas_st:['Land class','জমির শ্রেণি'],
c_owner:['Ownership record is consistent','মালিকানা রেকর্ড সামঞ্জস্যপূর্ণ'],c_mut:['Mutation is up to date','নামজারি হালনাগাদ'],c_case:['No pending court case','কোনো মামলা বিচারাধীন নয়'],c_khas:['Not khas / acquired / vested','খাস / অধিগ্রহণ / অর্পিত নয়'],c_fraud:['No fraud pattern detected','জালিয়াতির ধরন পাওয়া যায়নি'],
flags:['Findings','পর্যবেক্ষণ'],hist:['History','ইতিহাস'],ev_deed:['Deed registered','দলিল নিবন্ধিত'],ev_mut:['Mutation case','নামজারি মামলা'],ev_case:['Court case','আদালতের মামলা'],
unver:['unverified','যাচাই হয়নি'],ver:['verified','যাচাইকৃত'],print:['Print','প্রিন্ট'],
st_completed:['Completed','সম্পন্ন'],st_in_progress:['In progress','চলমান'],st_on_hold:['On hold','স্থগিত'],st_rejected:['Rejected','বাতিল'],
s0:['Application','আবেদন'],s1:['Deed check','দলিল যাচাই'],s2:['Field inspection','মাঠ পরিদর্শন'],s3:['Hearing','শুনানি'],s4:['AC (Land) approval','এসি (ল্যান্ড) অনুমোদন'],s5:['Khatian issued','খতিয়ান ইস্যু'],
t_h:['Track your mutation','নামজারি ট্র্যাক করুন'],t_p:['Enter your mutation case number to see where it stands.','আপনার নামজারি মামলার নম্বর দিন, কোন ধাপে আছে দেখুন।'],t_q:['Case no. (e.g. MUT-2026-12345)','মামলা নং (যেমন MUT-2026-12345)'],t_go:['Track case','মামলা দেখুন'],t_none:['Case not found.','মামলা পাওয়া যায়নি।'],
applicant:['Applicant','আবেদনকারী'],applied:['Applied on','আবেদনের তারিখ'],deed:['Deed no.','দলিল নং'],auto:['Automated checks','স্বয়ংক্রিয় যাচাই'],
k_deed_valid:['Deed serial found in registry','দলিলের সিরিয়াল রেজিস্ট্রিতে আছে'],k_nid_match:['Seller NID matches record','বিক্রেতার এনআইডি রেকর্ডের সাথে মিলছে'],k_seller_owner:['Seller was the record owner','বিক্রেতা রেকর্ডীয় মালিক ছিলেন'],k_area_match:['Deed and mutation area match','দলিল ও নামজারির পরিমাণ মিলছে'],k_no_case:['No pending court case','কোনো মামলা নেই'],k_not_khas:['Land is private (not khas)','জমি ব্যক্তিমালিকানাধীন'],k_no_double:['No conflicting sale','কোনো বিরোধপূর্ণ বিক্রয় নেই'],
a_dash:['Admin dashboard','অ্যাডমিন ড্যাশবোর্ড'],a_dash_p:['Live view of fraud signals and the e-mutation pipeline across all loaded map sheets.','সব লোডকৃত ম্যাপ শিট জুড়ে জালিয়াতির সংকেত ও ই-নামজারি পাইপলাইনের লাইভ চিত্র।'],
k_plots:['Plots monitored','পর্যবেক্ষিত দাগ'],k_flag:['Plots with alerts','সতর্কতাযুক্ত দাগ'],k_crit:['Critical plots','জটিল দাগ'],k_open:['Mutations in progress','চলমান নামজারি'],k_avg:['Avg. days to complete','গড় সম্পন্নের দিন'],
by_type:['Alerts by type','ধরন অনুযায়ী সতর্কতা'],by_sev:['By severity','তীব্রতা'],pipe:['Mutation pipeline','নামজারি পাইপলাইন'],per_ds:['By map sheet','শিট অনুযায়ী'],
sev_critical:['Critical','জটিল'],sev_high:['High','উচ্চ'],sev_medium:['Medium','মাঝারি'],sev_low:['Low','নিম্ন'],all:['All','সব'],
al_h:['Fraud & anomaly alerts','জালিয়াতি ও অসঙ্গতি সতর্কতা'],al_p:['Rule engine over deeds, mutations, cases and land class. Click a row to inspect.','দলিল, নামজারি, মামলা ও জমির শ্রেণির উপর নিয়মভিত্তিক বিশ্লেষণ। বিস্তারিত দেখতে সারিতে ক্লিক করুন।'],
type:['Type','ধরন'],sev:['Severity','তীব্রতা'],detail:['Detail','বিবরণ'],on_map:['Open on map','মানচিত্রে দেখুন'],search_ph:['Search plot, khatian, owner…','দাগ, খতিয়ান, মালিক খুঁজুন…'],
m_h:['Smart e-Mutation workflow','স্মার্ট ই-নামজারি ওয়ার্কফ্লো'],m_p:['Deed registration is linked to mutation. Seven automated checks run on every case; clean cases move forward without manual work.','দলিল নিবন্ধন নামজারির সাথে যুক্ত। প্রতিটি মামলায় সাতটি স্বয়ংক্রিয় যাচাই চলে; ত্রুটিহীন মামলা হাতে-কলমে ছাড়াই এগিয়ে যায়।'],
run_auto:['⚡ Run automation','⚡ স্বয়ংক্রিয় প্রক্রিয়া চালান'],case:['Case','মামলা'],progress:['Progress','অগ্রগতি'],checks:['Checks','যাচাই'],status:['Status','অবস্থা'],actions:['Actions','কাজ'],
advance:['Advance','এগিয়ে দিন'],hold:['Hold','স্থগিত'],resume:['Resume','চালু'],reject:['Reject','বাতিল'],blocked:['Blocked by automated check — needs officer review','স্বয়ংক্রিয় যাচাইয়ে আটকেছে — কর্মকর্তার পর্যালোচনা দরকার'],
auto_res:['advanced','এগিয়েছে'],auto_held:['held for review','পর্যালোচনায় আটকে'],done:['Done','হয়েছে'],
d_h:['Map data','ম্যাপ ডেটা'],d_p:['Sheets are discovered from files named place_sheet_land.json and place_sheet_plot.json in the data folder.','ডেটা ফোল্ডারে place_sheet_land.json ও place_sheet_plot.json নামের ফাইল থেকে শিট স্বয়ংক্রিয়ভাবে যুক্ত হয়।'],
d_up:['Upload a file','ফাইল আপলোড'],d_up_s:['e.g. gazipur_003_land.json','যেমন gazipur_003_land.json'],files:['Files','ফাইল'],
sale:['Sale','বিক্রয়'],loading:['Loading…','লোড হচ্ছে…'],refreshed:['Map data updated','ম্যাপ ডেটা হালনাগাদ হয়েছে'],err:['Something went wrong','কিছু ভুল হয়েছে'],
};
const S={lang:localStorage.lang||'bn',theme:localStorage.theme||(matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light'),role:localStorage.role||'user',datasets:[],cache:{},map:null,view:{c:[23.7,90.35],z:7},ver:''};
const $=(s,r=document)=>r.querySelector(s),$$=(s,r=document)=>[...r.querySelectorAll(s)];
const bnI=S.lang==='bn';
const t=k=>D[k]?D[k][S.lang==='bn'?1:0]:k, tr=o=>o?(S.lang==='bn'?o.bn:o.en):'';
const dg=s=>S.lang==='bn'?String(s).replace(/\d/g,d=>'০১২৩৪৫৬৭৮৯'[d]):String(s);
const nf=(n,d=2)=>Number(n).toLocaleString(S.lang==='bn'?'bn-BD':'en-US',{maximumFractionDigits:d});
const fd=s=>s?new Date(s).toLocaleDateString(S.lang==='bn'?'bn-BD':'en-GB',{day:'numeric',month:'short',year:'numeric'}):'–';
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const dec=m=>m/40.4686;
const CLS={'Settlement':['বসতি','#f4a261'],'Agriculture':['কৃষি','#8ab17d'],'Commercial Area':['বাণিজ্যিক','#e63946'],'Water Body':['জলাশয়','#38bdf8'],'Vacant Land':['পতিত জমি','#d6c28a'],'Road':['সড়ক','#9ca3af'],'Industrial Area':['শিল্প','#8b5cf6'],'Forest':['বন','#15803d']};
const clsName=c=>S.lang==='bn'&&CLS[c]?CLS[c][0]:c, clsCol=c=>CLS[c]?CLS[c][1]:`hsl(${[...(c||'?')].reduce((a,ch)=>(a*31+ch.charCodeAt(0))>>>0,7)%360} 60% 50%)`;
const SEVC={critical:'#d62839',high:'#e8731a',medium:'#d9961a',low:'#2f6fdd'};
function toast(m){const e=$('#toast');e.textContent=m;e.style.display='block';clearTimeout(toast.t);toast.t=setTimeout(()=>e.style.display='none',3200)}
async function api(p,o={}){o.headers={...(o.headers||{}),'X-Role':S.role};const r=await fetch(p,o);const j=await r.json().catch(()=>({}));if(!r.ok){const e=new Error(j.error||r.statusText);e.data=j;throw e}return j}
const placeName=p=>{const d=S.datasets.find(x=>x.place===p);return S.lang==='bn'&&d?d.place_bn:p.charAt(0).toUpperCase()+p.slice(1)};
const sevB=s=>`<span class="bdg ${s}">${t('sev_'+s)}</span>`,stB=s=>`<span class="bdg ${s==='completed'?'ok':s==='rejected'?'bad':s==='on_hold'?'warn':'info'}">${t('st_'+s)}</span>`;

/* ============================================================ chrome */
function chrome(){
 document.documentElement.dataset.theme=S.theme;document.documentElement.lang=S.lang;
 $('#chrome').innerHTML=`<a class="brand" href="#/"><span class="logo">ভূ</span><div><b>${t('brand')}</b><small>${t('tag')}</small></div></a>
 <nav class="links" id="links"></nav>
 <div class="tools"><button class="ib ${S.role==='admin'?'adm':''}" id="role" title="No login in MVP">${S.role==='admin'?'🛡️ '+t('r_admin'):'👤 '+t('r_user')}</button>
 <button class="ib" id="lang">${S.lang==='bn'?'EN':'বাং'}</button><button class="ib" id="theme" aria-label="theme">${S.theme==='dark'?'☀️':'🌙'}</button><button class="ib" id="burger">☰</button></div>`;
 $('#lang').onclick=()=>{S.lang=S.lang==='bn'?'en':'bn';localStorage.lang=S.lang;chrome();route()};
 $('#theme').onclick=()=>{S.theme=S.theme==='dark'?'light':'dark';localStorage.theme=S.theme;chrome();if(S.map)setBase()};
 $('#role').onclick=()=>{S.role=S.role==='admin'?'user':'admin';localStorage.role=S.role;chrome();if(S.role==='user'&&location.hash.startsWith('#/admin'))location.hash='#/';else route()};
 $('#burger').onclick=()=>$('#links').classList.toggle('open');
}
function nav(p){
 const u=[['/','n_home'],['/map','n_map'],['/verify','n_verify'],['/track','n_track']],a=[['/admin','n_dash'],['/admin/alerts','n_alerts'],['/admin/mutations','n_mut'],['/admin/data','n_data']];
 $('#links').innerHTML=u.map(x=>`<a href="#${x[0]}" class="${p===x[0]?'on':''}">${t(x[1])}</a>`).join('')+(S.role==='admin'?`<span class="sep"></span>`+a.map(x=>`<a href="#${x[0]}" class="${p===x[0]?'on':''}">${t(x[1])}</a>`).join(''):'');
 $$('#links a').forEach(l=>l.onclick=()=>$('#links').classList.remove('open'));
}

/* ============================================================ router */
function route(){
 if(S.map){S.view={c:S.map.getCenter(),z:S.map.getZoom()};S.map.remove();S.map=null}
 const [path,qs]=location.hash.slice(1).split('?'),q=Object.fromEntries(new URLSearchParams(qs||'')),p=path||'/';
 if(p.startsWith('/admin')&&S.role!=='admin'){toast(t('need_admin'));location.hash='#/';return}
 ({'/':home,'/map':mapView,'/verify':verifyView,'/track':trackView,'/admin':adminDash,'/admin/alerts':alertsView,'/admin/mutations':mutView,'/admin/data':dataView}[p]||home)(q);
 nav(p);window.scrollTo(0,0);
}
function fillSel(ps,ss,all,preset){
 const places=[...new Set(S.datasets.map(d=>d.place))];
 ps.innerHTML=places.map(p=>`<option value="${p}">${placeName(p)}</option>`).join('');
 const fs=()=>{ss.innerHTML=(all?`<option value="">${t('all_sheets')}</option>`:'')+S.datasets.filter(d=>d.place===ps.value).map(d=>`<option value="${d.id}">${t('sheet')} ${dg(d.sheet)} · ${dg(d.plots)} ${t('plots')}</option>`).join('')};
 ps.addEventListener('change',fs);if(preset)ps.value=preset.place;fs();if(preset)ss.value=preset.id;
}
const sampleChips=async(el,fn)=>{try{const s=await api('/api/samples');el.innerHTML=s.plots.map(x=>`<span class="chip" data-ds="${x.ds}" data-p="${x.plot}">${placeName(x.ds.split('_')[0])} ${t('plot')} ${dg(x.plot)}</span>`).join('');$$('.chip',el).forEach(c=>c.onclick=()=>fn(c.dataset.ds,c.dataset.p))}catch{}};

/* ============================================================ home */
function home(){
 const ds=S.datasets,tot={pl:new Set(ds.map(d=>d.place)).size,sh:ds.length,p:ds.reduce((a,d)=>a+d.plots,0),a:ds.reduce((a,d)=>a+d.area_dec,0)};
 $('#app').innerHTML=`<section class="hero"><div class="wrap hero-in"><div>
 <span class="pill">${t('pill')}</span><h1>${t('h1')}</h1><p>${t('h1p')}</p>
 <div class="finder card"><div class="fgrid"><label>${t('f_place')}<select id="hp"></select></label><label>${t('f_sheet')}<select id="hs"></select></label>
 <label>${t('f_plot')}<input id="hn" inputmode="numeric" placeholder="${t('f_plot_ph')}"></label><button class="btn pri" id="hgo">🗺️ ${t('f_go')}</button></div>
 <div class="or">${t('or')}</div><div class="gs"><input id="hq" placeholder="${t('geo_ph')}"><button class="btn" id="hqb">🔎 ${t('search')}</button></div></div>
 <div class="chips"><span>${t('try')}</span><span id="hch"></span></div></div>
 <div class="art"><svg viewBox="0 0 400 320" aria-hidden="true"><rect width="400" height="320" fill="#0b4a31"/><g stroke="#0a3a27" stroke-width="2">
 <path d="M10 20 150 10 170 110 20 120Z" fill="#2e7d4f"/><path d="M160 10 300 24 290 120 180 112Z" fill="#c9b66b"/><path d="M310 26 392 40 386 130 300 122Z" fill="#3b9d68"/>
 <path d="M18 132 170 124 182 226 30 236Z" fill="#8ab17d"/><path d="M186 124 290 132 300 230 196 226Z" fill="#2e7d4f" stroke="#ffd60a" stroke-width="4"/><path d="M302 134 386 142 380 238 312 232Z" fill="#c9b66b"/>
 <path d="M30 248 180 240 190 306 40 310Z" fill="#3b9d68"/><path d="M196 240 300 244 304 308 204 306Z" fill="#38a3c4"/><path d="M312 246 380 250 376 308 318 308Z" fill="#8ab17d"/></g>
 <circle cx="245" cy="178" r="9" fill="#d62839" stroke="#fff" stroke-width="3"/></svg>
 <span class="tag" style="left:-16px;top:40px">📍 ${t('plot')} ${dg(7)}</span><span class="tag" style="right:-10px;bottom:34px;color:var(--g)">✔ ${t('v_safe')}</span></div></div></section>
 <section class="stats"><div class="wrap"><div class="card grid g4" style="padding:6px">
 ${[[tot.pl,'st_places'],[tot.sh,'st_sheets'],[tot.p,'st_plots'],[Math.round(tot.a),'st_area']].map(x=>`<div class="stat"><b>${nf(x[0],0)}</b><span>${t(x[1])}</span></div>`).join('')}</div></div></section>
 <section class="sec"><div class="wrap grid g4">${[['#/map','🗺️','ft1'],['#/verify','🔍','ft2'],['#/track','🧾','ft3'],['#/admin','🛡️','ft4']].map(x=>`<a class="card feat" href="${x[0]}"><i>${x[1]}</i><h3>${t(x[2])}</h3><p>${t(x[2]+'p')}</p></a>`).join('')}</div></section>
 <section class="sec" style="padding-top:0"><div class="wrap"><h2>${t('how')}</h2><div class="steps">${[1,2,3,4].map(i=>`<div class="step"><b>${t('hw'+i)}</b><span>${t('hw'+i+'s')}</span></div>`).join('')}</div></div></section>
 <footer><div class="wrap"><b>${t('brand')}</b> — ${t('foot')}</div></footer>`;
 fillSel($('#hp'),$('#hs'),false);
 $('#hgo').onclick=()=>{const id=$('#hs').value,n=$('#hn').value.trim();location.hash=`#/map?ds=${id}${n?'&plot='+encodeURIComponent(n):''}`};
 const go=()=>{const v=$('#hq').value.trim();if(v)location.hash='#/map?q='+encodeURIComponent(v)};$('#hqb').onclick=go;$('#hq').onkeydown=e=>e.key==='Enter'&&go();
 sampleChips($('#hch'),(d,p)=>location.hash=`#/map?ds=${d}&plot=${p}`);
}

/* ============================================================ report */
function reportHTML(r,admin){
 const V=r.verdict,ico={safe:'✔',caution:'!',danger:'✖'}[V];
 const ev=[];
 r.deeds.forEach(d=>ev.push({date:d.date,bad:!d.verified,t:`📜 ${t('ev_deed')} · ${d.id}`,s:`${tr(d.seller)} → ${tr(d.buyer)} · ${nf(d.area)} ${t('dec')} · ${t('sale')}`,x:`${tr(d.office)} · ${d.verified?t('ver'):'⚠ '+t('unver')}`}));
 r.muts.forEach(m=>ev.push({date:m.applied,bad:m.status==='rejected',t:`🗂️ ${t('ev_mut')} · ${m.case_no}`,s:`${t('s'+Math.min(m.stage,5))} · ${t('st_'+m.status)}`,x:m.done?fd(m.done):''}));
 r.cases.forEach(c=>ev.push({date:c.filed,bad:true,t:`⚖️ ${t('ev_case')} · ${c.no}`,s:tr(c.type),x:tr(c.court)}));
 ev.sort((a,b)=>b.date.localeCompare(a.date));
 return `<div class="rep"><div><h3>${t('plot')} ${dg(r.plot_no)} <small style="font-family:inherit;color:var(--mut);font-size:14px">${placeName(r.place)} · ${t('sheet')} ${dg(r.sheet)}</small></h3></div>
 <div class="vd ${V}"><div class="ic">${ico}</div><div><b>${t('v_'+V)}</b><span>${t('v_'+V+'_s')}</span></div></div>
 <div class="card" style="padding:14px;box-shadow:none"><div class="kv">
 <div><small>${t('owner')}</small><b>${esc(tr(r.owner))}</b></div><div><small>${t('nid')}</small>${esc(r.owner.nid)}</div>
 <div><small>${t('khatian')}</small>${dg(r.khatian)}</div><div><small>${t('mouza')}</small>${dg(r.mouza||'–')}</div>
 <div><small>${t('area')}</small>${nf(r.area_dec)} ${t('dec')} · ${nf(r.area_dec/100,4)} ${t('acre')}</div><div><small>${t('khas_st')}</small><span class="bdg ${r.khas.code==='private'?'ok':'critical'}">${esc(tr(r.khas))}</span></div></div></div>
 <ul class="cl">${r.checklist.map(c=>`<li class="${c.ok?'y':'n'}"><i>${c.ok?'✓':'✕'}</i>${t('c_'+c.code)}</li>`).join('')}</ul>
 ${r.flags.length?`<div><b>${t('flags')}</b><div class="grid" style="margin-top:8px">${r.flags.map(f=>`<div class="flag">${sevB(f.sev)} <b style="display:inline;margin-left:4px">${tr(f.title)}</b><p>${esc(tr(f.detail))}</p></div>`).join('')}</div></div>`:''}
 <div><b>${t('hist')}</b><div class="tl" style="margin-top:10px">${ev.map(e=>`<div class="ev ${e.bad?'bad':''}"><small>${fd(e.date)}</small><b>${e.t}</b>${e.s}<small>${e.x}</small></div>`).join('')||'–'}</div></div></div>`;
}
async function showReport(ds,plot,into){
 into.innerHTML=`<div class="empty">${t('loading')}</div>`;
 try{const r=await api(`/api/verify?ds=${ds}&plot=${encodeURIComponent(plot)}`);into.innerHTML=reportHTML(r);return r}catch(e){into.innerHTML=`<div class="empty">${t('v_none')}</div>`}
}
function modal(html){const m=document.createElement('div');m.className='modal';m.innerHTML=`<div class="mbox"><button class="btn sm x">✕</button>${html}</div>`;m.onclick=e=>{if(e.target===m||e.target.classList.contains('x'))m.remove()};document.body.appendChild(m);return m}

/* ============================================================ verify */
function verifyView(q){
 $('#app').innerHTML=`<div class="page"><div class="wrap"><h2>${t('v_h')}</h2><p class="lead">${t('v_p')}</p>
 <div class="card" style="padding:18px"><div class="fgrid" style="grid-template-columns:1fr 1fr 1fr auto"><label>${t('f_place')}<select id="vp"></select></label><label>${t('f_sheet')}<select id="vs"></select></label>
 <label>${t('v_q')}<input id="vq" inputmode="numeric"></label><button class="btn pri" id="vgo">🔍 ${t('v_go')}</button></div><div class="chips"><span>${t('try')}</span><span id="vch"></span></div></div>
 <div id="vout" style="margin-top:20px;max-width:760px"></div></div></div>`;
 fillSel($('#vp'),$('#vs'),true);
 const out=$('#vout');
 const show=async(ds,p)=>{const r=await showReport(ds,p,out);if(r)out.innerHTML=`<div class="card" style="padding:20px">${out.innerHTML}<div class="chips"><a class="btn" href="#/map?ds=${ds}&plot=${p}">🗺️ ${t('on_map')}</a><button class="btn" onclick="print()">🖨️ ${t('print')}</button></div></div>`};
 const run=async()=>{const v=$('#vq').value.trim();if(!v)return;const ds=$('#vs').value;
  let res=[];try{res=await api(`/api/find?q=${encodeURIComponent(v)}&ds=${ds}`)}catch{}
  if(!res.length)out.innerHTML=`<div class="card empty">${t('v_none')}</div>`;
  else if(res.length===1)show(res[0].ds,res[0].plot_no);
  else{out.innerHTML=`<div class="card" style="padding:16px"><b>${t('v_pick')}</b><div class="chips">${res.map(x=>`<span class="chip" data-d="${x.ds}" data-p="${x.plot_no}">${placeName(x.place)} · ${t('sheet')} ${dg(x.sheet)} · ${t('plot')} ${dg(x.plot_no)}</span>`).join('')}</div></div>`;$$('.chip',out).forEach(c=>c.onclick=()=>show(c.dataset.d,c.dataset.p))}};
 $('#vgo').onclick=run;$('#vq').onkeydown=e=>e.key==='Enter'&&run();
 sampleChips($('#vch'),show);if(q.ds&&q.plot)show(q.ds,q.plot);
}

/* ============================================================ track */
function trackView(){
 $('#app').innerHTML=`<div class="page"><div class="wrap"><h2>${t('t_h')}</h2><p class="lead">${t('t_p')}</p>
 <div class="card" style="padding:18px;max-width:760px"><div class="gs"><input id="tq" placeholder="${t('t_q')}"><button class="btn pri" id="tgo">${t('t_go')}</button></div><div class="chips" id="tch"></div></div>
 <div id="tout" style="margin-top:20px;max-width:760px"></div></div></div>`;
 const run=async c=>{const out=$('#tout');try{const r=await api('/api/track/'+encodeURIComponent(c));
  out.innerHTML=`<div class="card" style="padding:20px"><div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap"><h3>${r.case_no}</h3>${stB(r.status)}</div>
  <div class="stp">${[0,1,2,3,4,5].map(i=>`<div class="${i<r.stage||r.status==='completed'?'d':i===r.stage?'c':''}">${t('s'+i)}</div>`).join('')}</div>
  <div class="kv" style="margin:12px 0"><div><small>${t('applicant')}</small>${r.applicant?esc(tr(r.applicant)):'–'}</div><div><small>${t('applied')}</small>${fd(r.applied)}</div><div><small>${t('plot')} / ${t('khatian')}</small>${dg(r.plot_no)} / ${dg(r.khatian)}</div><div><small>${t('deed')}</small>${r.deed_id}</div></div>
  <b>${t('auto')}</b><ul class="cl" style="margin-top:8px">${r.checks.map(c=>`<li class="${c.ok?'y':'n'}"><i>${c.ok?'✓':'!'}</i>${t('k_'+c.code)}</li>`).join('')}</ul>
  <div class="chips"><a class="btn" href="#/verify?ds=${r.ds}&plot=${r.plot_no}">🔍 ${t('n_verify')}</a></div></div>`}catch{out.innerHTML=`<div class="card empty">${t('t_none')}</div>`}};
 $('#tgo').onclick=()=>run($('#tq').value);$('#tq').onkeydown=e=>e.key==='Enter'&&run($('#tq').value);
 api('/api/samples').then(s=>{$('#tch').innerHTML=s.cases.map(c=>`<span class="chip">${c}</span>`).join('');$$('#tch .chip').forEach(c=>c.onclick=()=>{$('#tq').value=c.textContent;run(c.textContent)})});
}

/* ============================================================ map */
let DATA,RISK={},plotL={},selL,landG,plotG,ovG,baseL,curIds=[];
const esri='https://server.arcgisonline.com/ArcGIS/rest/services/';
function bases(){const sat=()=>L.tileLayer(esri+'World_Imagery/MapServer/tile/{z}/{y}/{x}',{maxNativeZoom:19,maxZoom:22,attribution:'Tiles © Esri'});
 return {'🛰️ Satellite + labels':L.layerGroup([sat(),L.tileLayer(esri+'Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}',{maxNativeZoom:19,maxZoom:22})]),'🛰️ Satellite':sat(),
 '🗺️ OpenStreetMap':L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxNativeZoom:19,maxZoom:22,attribution:'© OpenStreetMap contributors'}),
 '⛰️ Topographic':L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png',{maxNativeZoom:17,maxZoom:22,attribution:'© OpenTopoMap'}),
 '☀️ Light':L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png',{maxNativeZoom:20,maxZoom:22,attribution:'© OSM © CARTO'}),
 '🌙 Dark':L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',{maxNativeZoom:20,maxZoom:22,attribution:'© OSM © CARTO'})}}
function setBase(){}
const getDs=async id=>S.cache[id]||(S.cache[id]=await api('/api/dataset/'+id));
const selIds=()=>{const s=$('#ms').value;return s?[s]:S.datasets.filter(d=>d.place===$('#mp').value).map(d=>d.id)};

async function mapView(q){
 $('#app').innerHTML=`<div class="mapwrap"><div id="map"></div>
 <aside id="panel"><div class="ph"><b>${t('n_map')}</b><small id="sub"></small></div><div class="pb">
 <div class="two"><label>${t('f_place')}<select id="mp"></select></label><label>${t('f_sheet')}<select id="ms"></select></label></div>
 <h4>🔎 ${t('or')}</h4><div class="gs"><input id="mq" placeholder="${t('geo_ph')}"><button class="btn sm" id="mb">${t('search')}</button></div>
 <div class="geores" id="mres" hidden></div><button class="btn sm" id="locme" style="margin-top:8px">${t('loc_me')}</button>
 <div class="mini"><div><b id="s_p">–</b><span>${t('s_plots')}</span></div><div><b id="s_l">–</b><span>${t('s_parts')}</span></div><div><b id="s_a">–</b><span>${t('s_dec')}</span></div></div>
 <h4>${t('find_plot')}</h4><input id="pq" placeholder="${t('plot_ph')}"><div class="plots" id="plist"></div>
 <h4>${t('classes')}</h4><div id="legend"></div>
 <h4>${t('opacity')}</h4><input id="op" type="range" min="0" max="1" step=".05" value=".55" style="padding:0">
 <label class="row"><input type="checkbox" id="lbl" checked style="width:auto"> ${t('lbls')}</label>
 ${S.role==='admin'?`<label class="row"><input type="checkbox" id="rk" checked style="width:auto"> ${t('risk')}</label>`:''}
 </div></aside><button id="ptoggle" aria-label="panel">☰</button><div class="drawer" id="drawer"></div></div>`;
 const m=S.map=L.map('map',{zoomControl:false,maxZoom:22}).setView(S.view.c,S.view.z);
 L.control.zoom({position:'bottomright'}).addTo(m);L.control.scale({imperial:false}).addTo(m);
 const B=bases();B['🛰️ Satellite + labels'].addTo(m);
 m.createPane('landPane').style.zIndex=410;m.createPane('plotPane').style.zIndex=420;
 landG=L.layerGroup().addTo(m);plotG=L.layerGroup().addTo(m);ovG=L.layerGroup().addTo(m);
 L.control.layers(B,{[t('classes')]:landG,[t('s_plots')]:plotG},{position:'topright'}).addTo(m);
 DATA={land:{features:[]},plot:{features:[]}};RISK={};plotL={};selL=null;
 S.datasets.forEach(d=>L.circleMarker(d.center,{radius:9,color:'#fff',weight:2,fillColor:'#17a468',fillOpacity:.95}).bindTooltip(`${placeName(d.place)} · ${t('sheet')} ${dg(d.sheet)} · ${dg(d.plots)} ${t('plots')}`).on('click',()=>{$('#mp').value=d.place;$('#mp').dispatchEvent(new Event('change'));$('#ms').value=d.id;loadDs([d.id])}).addTo(ovG));
 const first=S.datasets.find(d=>d.id===q.ds);fillSel($('#mp'),$('#ms'),true,first);
 $('#mp').addEventListener('change',()=>loadDs(selIds()));$('#ms').onchange=()=>loadDs(selIds());
 $('#ptoggle').onclick=()=>$('#panel').classList.toggle('hide');
 $('#op').oninput=render;$('#lbl').onchange=labels;if($('#rk'))$('#rk').onchange=render;m.on('zoomend',labels);$('#pq').oninput=plist;
 $('#mb').onclick=()=>geo($('#mq').value);$('#mq').onkeydown=e=>e.key==='Enter'&&geo($('#mq').value);
 $('#locme').onclick=()=>navigator.geolocation?navigator.geolocation.getCurrentPosition(p=>goto(p.coords.latitude,p.coords.longitude),()=>toast(t('err'))):toast(t('err'));
 if(q.ds&&first){await loadDs([q.ds],true,q.plot)}else if(q.q){$('#mq').value=q.q;geo(q.q,true)}
 else if(S.datasets.length&&S.view.z<10){const b=L.latLngBounds(S.datasets.map(d=>d.center));m.fitBounds(b,{padding:[80,80],maxZoom:12});$('#sub').textContent=t('f_place')}
}
async function loadDs(ids,fit=true,focus){
 curIds=ids;const parts=await Promise.all(ids.map(getDs));
 const merge=k=>({type:'FeatureCollection',features:parts.flatMap(p=>p[k].features.map(f=>(f.properties.ds=p.id,f)))});
 DATA={land:merge('land'),plot:merge('plot')};RISK={};
 if(S.role==='admin')await Promise.all(ids.map(async id=>{try{const r=await api('/api/admin/risk/'+id);for(const k in r)RISK[id+':'+k]=r[k]}catch{}}));
 render();if(fit)fitData();if(focus)focusPlot(ids[0]+':'+focus);
}
const labelText=p=>dg(p.plot_no_en??p.label);
function plotPop(p,ds,l){
 const parts=DATA.land.features.filter(f=>f.properties.ds===ds&&f.properties.plot_no_en===p.plot_no_en);
 return `<div class="pop"><h4>${t('plot')} ${dg(p.plot_no_en)}</h4><table><tr><td>${t('mouza')}</td><td>${dg(p.m_code||'–')}</td></tr><tr><td>${t('jl')} / ${t('sheet')}</td><td>${dg(p.jl_no_en||'–')} / ${dg(p.sht_no_en||'–')}</td></tr>
 <tr><td>${t('area')}</td><td>${nf(dec(p.area_sqm))} ${t('dec')}<br>${nf(p.area_sqm)} ${t('sqm')} · ${nf(p.area_sqm/4046.86,4)} ${t('acre')}</td></tr>
 <tr><td>${t('address')}</td><td id="addr">…</td></tr></table>
 <div style="margin:6px 0">${parts.map(f=>`<span class="bdg" style="background:${clsCol(f.properties.maj_class)};color:#fff;margin:0 3px 3px 0">${clsName(f.properties.maj_class)}</span>`).join('')}</div>
 <button class="btn pri sm" onclick="vf('${ds}','${p.plot_no_en}')">🔍 ${t('verify_btn')}</button></div>`;
}
function render(){
 landG.clearLayers();plotG.clearLayers();plotL={};const op=+$('#op').value,hidden=S.hidden||(S.hidden=new Set()),risk=$('#rk')?$('#rk').checked:false;
 L.geoJSON(DATA.land,{pane:'landPane',filter:f=>!hidden.has(f.properties.maj_class),style:f=>({color:'#fff',weight:1,fillColor:clsCol(f.properties.maj_class),fillOpacity:op}),
  onEachFeature:(f,l)=>{l.bindPopup(`<div class="pop"><h4>${clsName(f.properties.maj_class)}</h4><table><tr><td>${t('subclass')}</td><td>${esc(f.properties.sub_class||'–')}</td></tr><tr><td>${t('plot')}</td><td>${dg(f.properties.plot_no_en)}</td></tr><tr><td>${t('area')}</td><td>${nf(dec(f.properties.area_sqm))} ${t('dec')}</td></tr></table></div>`);
   l.on('mouseover',()=>l.setStyle({weight:3}));l.on('mouseout',()=>l.setStyle({weight:1}))}}).addTo(landG);
 L.geoJSON(DATA.plot,{pane:'plotPane',style:f=>{const rk=risk&&RISK[f.properties.ds+':'+f.properties.plot_no_en];return{color:rk?SEVC[rk.sev]:'#ffd60a',weight:rk?3:2,fillColor:rk?SEVC[rk.sev]:'#ffd60a',fillOpacity:rk?.18:.02}},
  onEachFeature:(f,l)=>{const p=f.properties;plotL[p.ds+':'+p.plot_no_en]=l;l.bindPopup(()=>plotPop(p,p.ds),{maxWidth:330});
   l.bindTooltip(labelText(p),{permanent:true,direction:'center',className:'plot-lbl'});
   l.on('click',()=>hl(l));l.on('popupopen',()=>revGeo(l));}}).addTo(plotG);
 labels();side();
}
const hl=l=>{if(selL)selL.setStyle({weight:2});selL=l;l.setStyle({color:'#ff3b30',weight:4});l.bringToFront()};
async function revGeo(l){const c=l.getBounds().getCenter();try{const r=await(await fetch(`https://nominatim.openstreetmap.org/reverse?format=jsonv2&zoom=16&accept-language=${S.lang==='bn'?'bn,en':'en'}&lat=${c.lat}&lon=${c.lng}`)).json();const e=$('#addr');if(e)e.textContent=r.display_name?r.display_name.split(',').slice(0,4).join(', '):'–'}catch{const e=$('#addr');if(e)e.textContent='–'}}
function side(){
 const lf=DATA.land.features,pf=DATA.plot.features;
 $('#s_p').textContent=dg(pf.length);$('#s_l').textContent=dg(lf.length);$('#s_a').textContent=nf(dec((pf.length?pf:lf).reduce((a,f)=>a+f.properties.area_sqm,0)),1);
 $('#sub').textContent=curIds.length?curIds.map(i=>i.replace('_',' · ')).join(', '):t('f_place');
 const cls={};lf.forEach(f=>{const c=f.properties.maj_class||'?';(cls[c]??={n:0,a:0}).n++;cls[c].a+=f.properties.area_sqm});
 $('#legend').innerHTML=Object.entries(cls).map(([c,v])=>`<label class="row"><input type="checkbox" data-c="${esc(c)}" ${S.hidden.has(c)?'':'checked'} style="width:auto"><span class="sw" style="background:${clsCol(c)}"></span>${clsName(c)}<span class="n">${dg(v.n)} · ${nf(dec(v.a),1)} ${t('dec')}</span></label>`).join('');
 $$('#legend input').forEach(i=>i.onchange=()=>{i.checked?S.hidden.delete(i.dataset.c):S.hidden.add(i.dataset.c);render()});plist();
}
function plist(){
 const q=$('#pq').value.trim().toLowerCase();
 $('#plist').innerHTML=DATA.plot.features.filter(f=>!q||String(f.properties.plot_no_en).toLowerCase().includes(q)).map(f=>`<div class="row" data-k="${f.properties.ds}:${f.properties.plot_no_en}"><span class="sw" style="background:#ffd60a"></span>${t('plot')} ${dg(f.properties.plot_no_en)}<span class="n">${nf(dec(f.properties.area_sqm))} ${t('dec')}</span></div>`).join('');
 $$('#plist .row').forEach(r=>r.onclick=()=>focusPlot(r.dataset.k));
}
function focusPlot(k){const l=plotL[k];if(!l)return;S.map.fitBounds(l.getBounds(),{maxZoom:20,padding:[60,60]});hl(l);l.openPopup()}
function labels(){S.map&&S.map.getContainer().classList.toggle('nolabels',!$('#lbl').checked||S.map.getZoom()<17)}
function fitData(){const g=L.geoJSON([...DATA.plot.features,...DATA.land.features]);if(g.getLayers().length)S.map.fitBounds(g.getBounds(),{padding:[60,60]})}
window.vf=async(ds,p)=>{const d=$('#drawer');d.innerHTML=`<button class="btn sm x" onclick="this.parentNode.classList.remove('open')">✕</button>`;d.classList.add('open');
 const box=document.createElement('div');d.appendChild(box);const r=await showReport(ds,p,box);if(r)box.insertAdjacentHTML('beforeend',`<a class="btn sm" style="margin-top:12px" href="#/verify?ds=${ds}&plot=${p}">${t('full')}</a>`)};
const dist=(a,b)=>{const R=6371,x=(b[0]-a[0])*Math.PI/180,y=(b[1]-a[1])*Math.PI/180,h=Math.sin(x/2)**2+Math.cos(a[0]*Math.PI/180)*Math.cos(b[0]*Math.PI/180)*Math.sin(y/2)**2;return 2*R*Math.asin(Math.sqrt(h))};
async function goto(lat,lon){
 L.marker([lat,lon]).addTo(S.map);S.map.setView([lat,lon],16);
 const near=S.datasets.map(d=>({d,k:dist([lat,lon],d.center)})).sort((a,b)=>a.k-b.k)[0];
 if(near&&near.k<25){$('#mp').value=near.d.place;$('#mp').dispatchEvent(new Event('change'));$('#ms').value=near.d.id;await loadDs([near.d.id],true);toast(t('geo_near'))}else toast(t('geo_nodata'));
}
async function geo(q,auto){
 q=q.trim();if(!q)return;const box=$('#mres');
 try{const r=await(await fetch(`https://nominatim.openstreetmap.org/search?format=jsonv2&limit=5&countrycodes=bd&accept-language=${S.lang==='bn'?'bn,en':'en'}&q=${encodeURIComponent(q)}`)).json();
  if(!r.length){box.hidden=false;box.innerHTML=`<div>${t('geo_none')}</div>`;return}
  if(auto||r.length===1){box.hidden=true;return goto(+r[0].lat,+r[0].lon)}
  box.hidden=false;box.innerHTML=r.map((x,i)=>`<div data-i="${i}">📍 ${esc(x.display_name)}</div>`).join('');
  $$('div',box).forEach(d=>d.onclick=()=>{box.hidden=true;goto(+r[d.dataset.i].lat,+r[d.dataset.i].lon)})}catch{toast(t('err'))}
}

/* ============================================================ admin: dashboard */
async function adminDash(){
 $('#app').innerHTML=`<div class="page"><div class="wrap"><h2>${t('a_dash')}</h2><p class="lead">${t('a_dash_p')}</p><div id="dx" class="empty">${t('loading')}</div></div></div>`;
 const s=await api('/api/admin/summary'),mx=Math.max(1,...Object.values(s.by_code)),sv=['critical','high','medium','low'],tot=sv.reduce((a,k)=>a+(s.by_sev[k]||0),0)||1;let acc=0;
 const grad=sv.map(k=>{const a=acc/tot*100;acc+=(s.by_sev[k]||0);return `${SEVC[k]} ${a}% ${acc/tot*100}%`}).join(',');
 const codeCol={forged:'#d62839',double:'#d62839',khas:'#d62839',rapid:'#e8731a',mismatch:'#e8731a',unmut:'#d9961a',case:'#d9961a'};
 const FLN={forged:['Suspected forged deed','জাল দলিল'],double:['Same plot, multiple buyers','একই জমি একাধিক ক্রেতা'],khas:['Khas / acquired / vested','খাস / অধিগ্রহণ / অর্পিত'],rapid:['Rapid transfers','দ্রুত হস্তান্তর'],mismatch:['Registry–mutation mismatch','নিবন্ধন–নামজারি অমিল'],unmut:['Mutation missing / delayed','নামজারি হয়নি / বিলম্বিত'],case:['Pending court case','মামলা বিচারাধীন']};
 const sm=s.status;
 $('#dx').outerHTML=`<div class="grid g4">${[[s.plots,'k_plots'],[s.flagged,'k_flag'],[s.critical,'k_crit'],[sm.in_progress,'k_open'],[s.avg_days,'k_avg']].map(x=>`<div class="card kpi"><b>${nf(x[0],1)}</b><span>${t(x[1])}</span></div>`).join('')}</div>
 <div class="grid g2" style="margin-top:16px">
 <div class="card" style="padding:20px"><h3 style="font-size:20px;margin-bottom:10px">${t('by_type')}</h3>${Object.keys(FLN).map(k=>`<div class="bar"><span>${FLN[k][S.lang==='bn'?1:0]}</span><u><i style="width:${(s.by_code[k]||0)/mx*100}%;background:${codeCol[k]}"></i></u><b>${dg(s.by_code[k]||0)}</b></div>`).join('')}<div class="chips"><a class="btn sm" href="#/admin/alerts">${t('n_alerts')}</a></div></div>
 <div class="card" style="padding:20px"><h3 style="font-size:20px;margin-bottom:10px">${t('by_sev')}</h3><div class="donut" style="background:conic-gradient(${grad})"><div><span><b>${dg(tot===1&&!s.by_sev.critical?0:tot)}</b>${t('n_alerts')}</span></div></div>
 <div class="chips" style="justify-content:center">${sv.map(k=>`<span><span class="sw" style="display:inline-block;background:${SEVC[k]};vertical-align:middle"></span> ${t('sev_'+k)} ${dg(s.by_sev[k]||0)}</span>`).join('')}</div></div>
 <div class="card" style="padding:20px"><h3 style="font-size:20px;margin-bottom:10px">${t('pipe')}</h3>${[0,1,2,3,4].map(i=>`<div class="bar"><span>${t('s'+i)}</span><u><i style="width:${s.stages[i]/Math.max(1,...s.stages)*100}%;background:var(--g2)"></i></u><b>${dg(s.stages[i])}</b></div>`).join('')}
 <div class="chips">${stB('completed')} ${dg(sm.completed)} ${stB('on_hold')} ${dg(sm.on_hold)} ${stB('rejected')} ${dg(sm.rejected)}<a class="btn sm" href="#/admin/mutations">${t('n_mut')}</a></div></div>
 <div class="card" style="padding:20px"><h3 style="font-size:20px;margin-bottom:10px">${t('per_ds')}</h3><div class="tw"><table><tr><th>${t('sheet')}</th><th>${t('s_plots')}</th><th>${t('n_alerts')}</th><th>${t('k_crit')}</th></tr>${s.per_ds.map(d=>`<tr class="cl" onclick="location.hash='#/map?ds=${d.ds}'"><td>${placeName(d.ds.split('_')[0])} · ${dg(d.ds.split('_').pop())}</td><td>${dg(d.plots)}</td><td>${dg(d.flagged)}</td><td>${d.critical?`<span class="bdg critical">${dg(d.critical)}</span>`:'–'}</td></tr>`).join('')}</table></div></div></div>`;
}

/* ============================================================ admin: alerts */
async function alertsView(){
 $('#app').innerHTML=`<div class="page"><div class="wrap"><h2>${t('al_h')}</h2><p class="lead">${t('al_p')}</p>
 <div class="filters"><select id="fs"><option value="">${t('sev')}: ${t('all')}</option>${['critical','high','medium','low'].map(k=>`<option value="${k}">${t('sev_'+k)}</option>`).join('')}</select>
 <select id="fc"><option value="">${t('type')}: ${t('all')}</option>${['forged','double','khas','rapid','mismatch','unmut','case'].map(k=>`<option value="${k}">${k}</option>`).join('')}</select><input id="fq" placeholder="${t('search_ph')}"></div>
 <div class="card tw"><table id="at"></table></div></div></div>`;
 const rows=await api('/api/admin/alerts');
 const draw=()=>{const s=$('#fs').value,c=$('#fc').value,q=$('#fq').value.toLowerCase(),R=rows.filter(r=>(!s||r.sev===s)&&(!c||r.code===c)&&(!q||`${r.plot} ${r.khatian} ${r.detail.en} ${r.detail.bn} ${r.ds}`.toLowerCase().includes(q)));
  $('#at').innerHTML=`<tr><th>${t('sev')}</th><th>${t('type')}</th><th>${t('sheet')}</th><th>${t('plot')}</th><th>${t('detail')}</th></tr>`+(R.map((r,i)=>`<tr class="cl" data-i="${i}"><td>${sevB(r.sev)}</td><td><b>${tr(r.title)}</b></td><td>${placeName(r.ds.split('_')[0])} · ${dg(r.ds.split('_').pop())}</td><td>${dg(r.plot)}</td><td style="min-width:260px;color:var(--mut)">${esc(tr(r.detail))}</td></tr>`).join('')||`<tr><td colspan=5 class="empty">–</td></tr>`);
  $$('#at tr.cl').forEach(tr=>tr.onclick=()=>inspect(R[tr.dataset.i].ds,R[tr.dataset.i].plot))};
 ['fs','fc'].forEach(i=>$('#'+i).onchange=draw);$('#fq').oninput=draw;draw();
}
async function inspect(ds,plot){const m=modal('<div id="mb"></div>');const r=await showReport(ds,plot,$('#mb',m));if(r)$('#mb',m).insertAdjacentHTML('beforeend',`<div class="chips"><a class="btn" href="#/map?ds=${ds}&plot=${plot}" onclick="document.querySelector('.modal').remove()">🗺️ ${t('on_map')}</a></div>`)}

/* ============================================================ admin: mutations */
async function mutView(){
 $('#app').innerHTML=`<div class="page"><div class="wrap"><h2>${t('m_h')}</h2><p class="lead">${t('m_p')}</p>
 <div class="filters"><select id="ms2"><option value="">${t('status')}: ${t('all')}</option>${['in_progress','on_hold','completed','rejected'].map(k=>`<option value="${k}">${t('st_'+k)}</option>`).join('')}</select><input id="mq2" placeholder="${t('search_ph')}"><button class="btn pri" id="auto">${t('run_auto')}</button></div>
 <div class="card tw"><table id="mt"></table></div></div></div>`;
 let rows=await api('/api/admin/mutations');
 const draw=()=>{const s=$('#ms2').value,q=$('#mq2').value.toLowerCase(),R=rows.filter(r=>(!s||r.status===s)&&(!q||`${r.case_no} ${r.plot} ${r.deed_id} ${r.applicant?r.applicant.en+r.applicant.bn:''}`.toLowerCase().includes(q))).slice(0,150);
  $('#mt').innerHTML=`<tr><th>${t('case')}</th><th>${t('plot')}</th><th>${t('applicant')}</th><th>${t('progress')}</th><th>${t('checks')}</th><th>${t('status')}</th><th>${t('actions')}</th></tr>`+R.map(r=>{const ok=r.checks.filter(c=>c.ok).length,open=r.status==='in_progress'||r.status==='on_hold';
   return `<tr><td><a href="#" data-c="${r.case_no}" class="cd"><b>${r.case_no}</b></a><br><small style="color:var(--mut)">${fd(r.applied)}</small></td><td>${placeName(r.ds.split('_')[0])} · ${dg(r.plot)}</td><td>${r.applicant?esc(tr(r.applicant)):'–'}</td>
   <td><div class="seg" title="${t('s'+Math.min(r.stage,5))}">${[0,1,2,3,4,5].map(i=>`<i class="${i<=r.stage?'on':''}"></i>`).join('')}</div><small>${t('s'+Math.min(r.stage,5))}</small></td>
   <td><span class="bdg ${ok===7?'ok':r.blocked?'bad':'warn'}">${dg(ok)}/${dg(7)}</span></td><td>${stB(r.status)}</td>
   <td style="white-space:nowrap">${open?`<button class="btn sm pri" data-a="advance" data-c="${r.case_no}" ${r.status==='on_hold'?'disabled':''}>${t('advance')}</button> <button class="btn sm" data-a="${r.status==='on_hold'?'resume':'hold'}" data-c="${r.case_no}">${r.status==='on_hold'?t('resume'):t('hold')}</button> <button class="btn sm danger" data-a="reject" data-c="${r.case_no}">${t('reject')}</button>`:'–'}</td></tr>`}).join('');
  $$('#mt [data-a]').forEach(b=>b.onclick=async()=>{try{await api(`/api/admin/mutation/${b.dataset.c}/${b.dataset.a}`,{method:'POST'});toast(t('done'))}catch(e){toast(e.data&&e.data.error==='blocked'?'⛔ '+t('blocked'):t('err'))}rows=await api('/api/admin/mutations');draw()});
  $$('#mt .cd').forEach(a=>a.onclick=e=>{e.preventDefault();const r=rows.find(x=>x.case_no===a.dataset.c);modal(`<h3>${r.case_no}</h3><div class="stp">${[0,1,2,3,4,5].map(i=>`<div class="${i<r.stage||r.status==='completed'?'d':i===r.stage?'c':''}">${t('s'+i)}</div>`).join('')}</div><b>${t('auto')}</b><ul class="cl" style="margin-top:8px">${r.checks.map(c=>`<li class="${c.ok?'y':'n'}"><i>${c.ok?'✓':'✕'}</i>${t('k_'+c.code)}</li>`).join('')}</ul>${r.blocked?`<p class="bdg bad" style="margin-top:12px">${t('blocked')}</p>`:''}<div class="chips"><button class="btn" onclick="document.querySelector('.modal').remove();inspect('${r.ds}','${r.plot}')">🔍 ${t('n_verify')}</button></div>`)})};
 $('#ms2').onchange=draw;$('#mq2').oninput=draw;draw();
 $('#auto').onclick=async()=>{const r=await api('/api/admin/automate',{method:'POST'});toast(`⚡ ${dg(r.advanced)} ${t('auto_res')} · ${dg(r.flagged)} ${t('auto_held')}`);rows=await api('/api/admin/mutations');draw()};
}

/* ============================================================ admin: data */
async function dataView(){
 $('#app').innerHTML=`<div class="page"><div class="wrap"><h2>${t('d_h')}</h2><p class="lead">${t('d_p')}</p><div class="grid g2">
 <div class="card tw"><table><tr><th>${t('sheet')}</th><th>${t('s_plots')}</th><th>${t('s_parts')}</th><th>${t('s_dec')}</th><th>${t('files')}</th></tr>${S.datasets.map(d=>`<tr class="cl" onclick="location.hash='#/map?ds=${d.id}'"><td><b>${placeName(d.place)} · ${dg(d.sheet)}</b></td><td>${dg(d.plots)}</td><td>${dg(d.parts)}</td><td>${nf(d.area_dec,1)}</td><td><small>${d.files.join('<br>')}</small></td></tr>`).join('')}</table></div>
 <div class="card" style="padding:20px;align-self:start"><h3 style="font-size:20px">${t('d_up')}</h3><p style="color:var(--mut);font-size:14px">${t('d_up_s')}</p><input type="file" id="upf" accept=".json" multiple></div></div></div></div>`;
 $('#upf').onchange=async e=>{for(const f of e.target.files){try{await api('/api/admin/upload?name='+encodeURIComponent(f.name),{method:'POST',body:await f.text()});toast('✔ '+f.name)}catch(x){toast(x.message)}}await loadDatasets();dataView()};
}

/* ============================================================ boot */
async function loadDatasets(){S.datasets=await api('/api/datasets');S.cache={}}
setInterval(async()=>{try{const v=JSON.stringify((await api('/api/version')).version);if(S.ver&&v!==S.ver){await loadDatasets();toast(t('refreshed'));if(!S.map)route()}S.ver=v}catch{}},5000);
addEventListener('hashchange',route);
(async()=>{chrome();try{await loadDatasets();S.ver=JSON.stringify((await api('/api/version')).version)}catch{}route()})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    a = ap.parse_args()
    make_demo()
    threading.Timer(1.0, lambda: webbrowser.open(f"http://{a.host}:{a.port}")).start()
    app.run(host=a.host, port=a.port, debug=False)