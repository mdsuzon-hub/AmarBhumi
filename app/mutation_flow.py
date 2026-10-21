#!/usr/bin/env python3
"""
mutation_flow.py  —  Deed registration + the ONE e-Mutation workflow for land_portal.py
=======================================================================================
Install (in land_portal.py, right before `if __name__ == "__main__":`):

    import mutation_flow
    PAGE = mutation_flow.install(app, PAGE, dict(data_dir=DATA_DIR, record=record, err=err,
                                                  bn_digits=bn_digits, index=index))

What this module owns (everything lives in data/mutation_flow.json, uploads in data/mutation_files/):
    Step 0  Deed registration   user requests online -> admin (sub-registrar) approves -> registered deed (Bangla A4)
    Step 1  Application         online / offline (paper) -> applicant + office copy
    Step 2  Verification        automated checks + officer review (failed checks can be WAIVED by the admin)
    Step 3  Field inspection    Kanungo / surveyor report
    Step 4  Notice              previous owner + neighbours, objection period
    Step 5  Hearing + AC review
    Step 6  Mutation order
    Step 7  Final fee           demo payment (bKash / Nagad / Rocket / card / bank) -> khatian + DCR

The legacy case board of land_portal.py (/api/admin/workflow, /api/track) is no longer used by the UI:
    * nav, "#/track" and the admin "workflow" tab are redirected to this module
    * /api/stats and /api/admin/overview are re-pointed to this module's data
Routes: /api/mut/* (signed-in users)   /api/admin/mut/* (admin only, protected by the app's guard)
"""
import base64
import json
import os
import random
import re
import threading
import zlib
from datetime import date, datetime, timedelta

from flask import jsonify, request, send_file, session

FEE_ITEMS = [("application", 100), ("notice", 50), ("mutation", 1000), ("dcr", 20)]  # total 1,170
DEED_TYPES = ("sale", "gift", "inheritance")
DOC_KEYS = ("deed", "prev_khatian", "ldt", "nid", "photo", "signature", "warish", "decree")
PAY_METHODS = ("bkash", "nagad", "rocket", "card", "bank")
MAX_FILE = 2 * 1024 * 1024
EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "application/pdf": "pdf"}
SLA_DAYS = 28
EST = {1: 2, 2: 5, 3: 10, 4: 5, 5: 2, 6: 3}  # planned working days per stage (sum 27 < SLA 28)
DEMO_OTP = "123456"

# name, father, NID, address, mobile, demo user id
DEMO_P = [
    ("করিম উদ্দিন", "আব্দুল জব্বার", "1985123456789", "বাড়ি ১২, কাউলতিয়া, গাজীপুর সদর", "01711223344", "user1"),
    ("সুমাইয়া আক্তার", "মো. হারুন অর রশিদ", "1992456789012", "বাসা ৭, মুক্তিযোদ্ধা সড়ক, সাভার", "01819334455", "user2"),
    ("তানভীর হাসান", "আবুল কালাম", "1989765432109", "গ্রাম: দক্ষিণ চর, কুমিল্লা সদর", "01911445566", "user3"),
    ("নাজমুল হক", "মোশারফ হোসেন", "1980234567890", "মহল্লা: পূর্ব বাজার, গাজীপুর", "01611556677", None),
    ("রুবিনা ইয়াসমিন", "আনোয়ার হোসেন", "1995345678901", "গ্রাম: বড়বাড়ী, সাভার", "01511667788", None),
    ("মাহমুদুল হাসান", "নূর মোহাম্মদ", "1987567890123", "ওয়ার্ড ৪, কুমিল্লা সদর", "01311778899", None),
]
# (upto, person, basis, age_days, stage_age_days, flags)
#   upto: 0 draft, 1 submitted, 2 verified, 3 inspected, 4 notice issued, 5 notice closed, 6 reviewed, 7 order, 8 paid
DEMO_SPECS = [
    (0, 0, "sale", 1, 0, ()), (1, 0, "sale", 1, 1, ()), (1, 1, "gift", 4, 4, ("noldt",)),
    (2, 1, "sale", 7, 3, ()), (3, 2, "sale", 10, 2, ()), (4, 0, "inheritance", 9, 4, ()),
    (4, 2, "sale", 15, 6, ("obj",)), (5, 1, "sale", 20, 3, ("obj", "hear")), (6, 0, "gift", 22, 2, ()),
    (7, 0, "sale", 25, 1, ()), (8, 1, "sale", 30, 0, ()), (2, 2, "sale", 12, 6, ("hold",)),
    (4, 1, "sale", 35, 12, ()), (1, 0, "gift", 5, 5, ("return",)), (1, 2, "sale", 6, 6, ("reject",)),
]


class ActErr(ValueError):
    def __init__(self, msg, code=400, **extra):
        super().__init__(msg)
        self.code, self.extra = code, extra


def _sniff(raw):
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw[:5] == b"%PDF-":
        return "application/pdf"
    return None


def _luhn(n):
    s, alt = 0, False
    for ch in reversed(n):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        s, alt = s + d, not alt
    return s % 10 == 0


def check_pay(method, f):
    """Validate the demo payment form; returns a masked account label (full numbers are never stored)."""
    f = f if isinstance(f, dict) else {}
    dg = lambda v: re.sub(r"\D", "", str(v or ""))
    if method in ("bkash", "nagad", "rocket"):
        mob = re.sub(r"[\s-]", "", str(f.get("mobile") or ""))
        if not re.fullmatch(r"(\+?88)?01[3-9]\d{8}", mob):
            raise ValueError("valid wallet mobile number required / সঠিক মোবাইল নম্বর দিন")
        if not re.fullmatch(r"\d{4,5}", dg(f.get("pin"))):
            raise ValueError("PIN must be 4-5 digits / পিন ৪-৫ সংখ্যার হতে হবে")
        if dg(f.get("otp")) != DEMO_OTP:
            raise ValueError("wrong OTP (demo OTP is %s) / ওটিপি ভুল (ডেমো ওটিপি %s)" % (DEMO_OTP, DEMO_OTP))
        return "••••" + mob[-4:]
    if method == "card":
        n = dg(f.get("number"))
        if not (13 <= len(n) <= 19 and _luhn(n)):
            raise ValueError("invalid card number (demo: 4242 4242 4242 4242) / কার্ড নম্বর সঠিক নয়")
        mt = re.fullmatch(r"(\d{2})\s*/\s*(\d{2})", str(f.get("exp") or "").strip())
        if not mt or not 1 <= int(mt[1]) <= 12 or (2000 + int(mt[2]), int(mt[1])) < (date.today().year, date.today().month):
            raise ValueError("card expiry invalid or past (MM/YY) / কার্ডের মেয়াদ সঠিক নয়")
        if not re.fullmatch(r"\d{3,4}", dg(f.get("cvv"))):
            raise ValueError("CVV must be 3-4 digits / সিভিভি ৩-৪ সংখ্যার")
        if not str(f.get("name") or "").strip():
            raise ValueError("card holder name required / কার্ডধারীর নাম দিন")
        return "•••• " + n[-4:]
    bank, acc = str(f.get("bank") or "").strip()[:60], dg(f.get("account"))
    if not bank or not 8 <= len(acc) <= 17 or not str(f.get("holder") or "").strip():
        raise ValueError("bank name, account no. (8-17 digits) and holder name required / ব্যাংক, অ্যাকাউন্ট নম্বর ও নাম দিন")
    return "%s ••%s" % (bank, acc[-4:])


def install(app, page, ctx):
    data_dir = ctx["data_dir"]
    store = os.path.join(data_dir, "mutation_flow.json")
    files_dir = os.path.join(data_dir, "mutation_files")
    os.makedirs(files_dir, exist_ok=True)
    lock = threading.RLock()
    tl_ = threading.local()  # seeding actor — thread-local so it can never leak into another request
    db = {"seq": {}, "deeds": {}, "apps": {}, "notes": [], "seeded": False}
    err, bd, record = ctx["err"], ctx["bn_digits"], ctx["record"]
    index_fn = ctx.get("index")

    # ------------------------------------------------------------------ storage
    try:
        with open(store, encoding="utf-8") as f:
            raw = json.load(f)
        for k in db:
            if k in raw:
                db[k] = raw[k]
    except (OSError, ValueError):
        pass

    def save():
        tmp = store + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False)
        os.replace(tmp, store)

    def now():
        return datetime.now().isoformat(timespec="seconds")

    def today():
        return date.today().isoformat()

    def seq(kind, yearly=True):
        k = f"{kind}:{date.today().year}" if yearly else kind
        db["seq"][k] = int(db["seq"].get(k, 0)) + 1
        return db["seq"][k]

    def user():
        return getattr(tl_, "u", None) or session.get("u") or {}

    def is_admin():
        return user().get("role") == "admin"

    def mine(o):
        return is_admin() or bool(o.get("uid") and o.get("uid") == user().get("id"))

    def ev(key, note=""):
        return {"ts": now(), "key": key, "by": user().get("name"), "note": note}

    def notify(to, ref, kind, bn, en):
        if not to:
            return
        db["notes"].append({"id": seq("note", False), "to": to, "ref": ref, "kind": kind,
                            "bn": bn, "en": en, "ts": now(), "read": False})
        del db["notes"][:-600]

    def tell(a, bn, en):
        notify(a.get("uid"), a["id"], "app", bn, en)

    # ------------------------------------------------------------------ helpers
    def snap(rec):
        s = {k: rec[k] for k in ("ds", "plot_no", "label", "sheet", "district", "mouza", "m_code", "jl",
                                 "area_sqm", "khatian", "center")}
        s["parts"] = rec["parts"]
        return s

    def pre_checks(rec):
        if not rec:
            return []
        ft = [f["type"] for f in rec["flags"]]
        fraud = any(x in ("fake_deed", "double_sale") for x in ft)
        return [["not_khas", "pass" if rec["khas"] == "none" else "fail", "" if rec["khas"] == "none" else rec["khas"]],
                ["no_acq", {"none": "pass", "proposed": "warn", "notified": "fail"}[rec["acq"]], "" if rec["acq"] == "none" else rec["acq"]],
                ["no_case", "warn" if rec["pending"] else "pass", ", ".join(p["type"] for p in rec["pending"])],
                ["no_fraud", "fail" if fraud else "warn" if rec["flags"] else "pass", ", ".join(ft)]]

    def party(v, need=True):
        v = v if isinstance(v, dict) else {}
        p = {"name": str(v.get("name") or "").strip()[:80], "father": str(v.get("father") or "").strip()[:80],
             "nid": re.sub(r"\D", "", str(v.get("nid") or ""))[:17], "address": str(v.get("address") or "").strip()[:160]}
        if need and not p["name"]:
            raise ValueError("name required / নাম দিন")
        return p

    def get_rec(ds, no):
        return record((str(ds or "").strip().lower(), str(no or "").strip()))

    # ------------------------------------------------------------------ deeds
    def deed_fields(b):
        typ = b.get("type")
        if typ not in DEED_TYPES:
            raise ValueError("invalid deed type / দলিলের ধরন সঠিক নয়")
        rec = get_rec(b.get("ds"), b.get("plot_no"))
        if not rec:
            raise LookupError("plot not found / দাগ পাওয়া যায়নি")
        if rec["khas"] != "none":
            raise ValueError("khas / vested land cannot be registered / খাস বা অর্পিত জমির দলিল নিবন্ধন হয় না")
        if rec["acq"] == "notified":
            raise ValueError("land under acquisition notice cannot be registered / অধিগ্রহণ নোটিশভুক্ত জমির দলিল নিবন্ধন হয় না")
        plot_dec = rec["area_sqm"] / 40.4686
        try:
            area = round(float(b.get("area_dec")), 4)
        except (TypeError, ValueError):
            raise ValueError("area required / জমির পরিমাণ দিন")
        if area <= 0 or area > plot_dec + 0.01:
            raise ValueError("area must be between 0 and %.2f decimal / পরিমাণ ০ থেকে %.2f শতাংশের মধ্যে হতে হবে" % (plot_dec, plot_dec))
        try:
            value = max(0, int(float(b.get("value") or 0)))
        except (TypeError, ValueError):
            value = 0
        if typ == "sale" and value <= 0:
            raise ValueError("sale price required / বিক্রয় মূল্য দিন")
        seller, buyer = party(b.get("seller")), party(b.get("buyer"))
        wit = [str(w).strip()[:80] for w in (b.get("witnesses") or []) if str(w).strip()][:2]
        if typ != "inheritance" and len(wit) < 2:
            raise ValueError("two witnesses required / দুইজন সাক্ষীর নাম দিন")
        bounds = {k: str((b.get("boundaries") or {}).get(k) or "").strip()[:80] for k in ("north", "south", "east", "west")}
        try:
            dd = date.fromisoformat(str(b.get("deed_date") or "")[:10])
        except ValueError:
            dd = date.today()
        return {"type": typ, "land": snap(rec), "area_dec": area, "value": value, "seller": seller, "buyer": buyer,
                "witnesses": wit, "boundaries": bounds, "deed_date": min(dd, date.today()).isoformat(),
                "note": str(b.get("note") or "").strip()[:300]}

    def deed_view(d):
        v = dict(d)
        v["checks"] = pre_checks(get_rec(d["land"]["ds"], d["land"]["plot_no"]))
        v["app_id"] = next((a["id"] for a in db["apps"].values() if a.get("deed_id") == d["id"] and a["status"] != "rejected"), None)
        v["plot_area_dec"] = round(d["land"]["area_sqm"] / 40.4686, 2)
        return v

    def new_deed(f, uid, channel):
        did = "DR-%d-%05d" % (date.today().year, seq("deed"))
        d = dict(f, id=did, uid=uid, channel=channel, status="submitted", created=now(), registered=None, reason="",
                 timeline=[ev("submitted")])
        db["deeds"][did] = d
        notify("admin", did, "deed", "নতুন দলিল নিবন্ধন আবেদন: %s" % did, "New deed registration request: %s" % did)
        return d

    def deed_act(d, act, b):
        if d["status"] != "submitted":
            raise ActErr("only requests under review can be processed / শুধু যাচাইাধীন আবেদন প্রক্রিয়া করা যায়", 409)
        did, reason = d["id"], str(b.get("reason") or "").strip()[:300]
        if act == "approve":
            rec = get_rec(d["land"]["ds"], d["land"]["plot_no"])
            if rec and (rec["khas"] != "none" or rec["acq"] == "notified"):
                raise ActErr("blocked: khas/vested or acquisition notice / খাস, অর্পিত বা অধিগ্রহণভুক্ত জমি", 409)
            n = seq("deedno")
            dist = d["land"]["district"]
            d["deed_no"] = "%d/%d" % (n, date.today().year)
            d["registered"] = {
                "date": today(), "serial": n,
                "office": {"bn": "%s সাব-রেজিস্ট্রি অফিস" % dist["bn"], "en": "%s Sub-Registry Office" % dist["en"]},
                "book": str(b.get("book") or 1)[:10], "volume": str(b.get("volume") or (n // 200 + 1))[:10],
                "page": str(b.get("page") or n * 3)[:10], "registrar": user().get("name")}
            d["status"], d["reason"] = "approved", ""
            d["timeline"].append(ev("approved", reason))
            notify(d.get("uid"), did, "deed",
                   "আপনার দলিল নিবন্ধিত হয়েছে (দলিল নং %s) — অনলাইন কপি ডাউনলোড করুন।" % bd(d["deed_no"]),
                   "Your deed is registered (deed no. %s) — download the online copy." % d["deed_no"])
        elif act in ("return", "reject"):
            if not reason:
                raise ActErr("reason required / কারণ লিখুন")
            d["status"] = "returned" if act == "return" else "rejected"
            d["reason"] = reason
            d["timeline"].append(ev("returned" if act == "return" else "rejected", reason))
            notify(d.get("uid"), did, "deed",
                   ("আপনার দলিল আবেদন সংশোধনের জন্য ফেরত: " if act == "return" else "আপনার দলিল আবেদন প্রত্যাখ্যাত: ") + reason,
                   ("Your deed request was returned: " if act == "return" else "Your deed request was rejected: ") + reason)
        else:
            raise ActErr("unknown action")

    @app.route("/api/mut/deeds", methods=["GET", "POST"])
    def mut_deeds():
        if request.method == "GET":
            boot()
            rows = [deed_view(d) for d in db["deeds"].values() if mine(d)]
            rows.sort(key=lambda d: d["created"], reverse=True)
            return jsonify(deeds=rows)
        try:
            f = deed_fields(request.get_json(silent=True) or {})
        except (ValueError, LookupError) as e:
            return err(str(e), 404 if isinstance(e, LookupError) else 400)
        with lock:
            adm = is_admin()
            d = new_deed(f, None if adm else user().get("id"), "offline" if adm else "online")
            save()
        return jsonify(ok=True, deed=deed_view(d))

    @app.route("/api/mut/deeds/<did>", methods=["GET", "PUT"])
    def mut_deed(did):
        d = db["deeds"].get(did)
        if not d:
            return err("deed request not found / দলিল আবেদন পাওয়া যায়নি", 404)
        if not mine(d):
            return err("forbidden", 403)
        if request.method == "GET":
            return jsonify(deed=deed_view(d))
        if d["status"] != "returned":
            return err("only returned requests can be edited / শুধু ফেরত আবেদন সংশোধন করা যায়", 409)
        try:
            f = deed_fields(request.get_json(silent=True) or {})
        except (ValueError, LookupError) as e:
            return err(str(e), 404 if isinstance(e, LookupError) else 400)
        with lock:
            d.update(f)
            d["status"], d["reason"] = "submitted", ""
            d["timeline"].append(ev("resubmitted"))
            notify("admin", did, "deed", "দলিল আবেদন সংশোধন করে পুনরায় জমা: %s" % did, "Deed request resubmitted: %s" % did)
            save()
        return jsonify(ok=True, deed=deed_view(d))

    @app.route("/api/admin/mut/deeds/<did>/<act>", methods=["POST"])
    def mut_deed_act(did, act):
        b = request.get_json(silent=True) or {}
        with lock:
            d = db["deeds"].get(did)
            if not d:
                return err("deed request not found / দলিল আবেদন পাওয়া যায়নি", 404)
            try:
                deed_act(d, act, b)
            except ActErr as e:
                return err(str(e), e.code)
            save()
        return jsonify(ok=True, deed=deed_view(d))

    # ------------------------------------------------------------------ applications
    def required_docs(a):
        r = ["prev_khatian", "ldt", "nid", "photo", "signature"]
        if a["basis"] == "inheritance":
            r.append("warish")
        if a.get("court_ordered"):
            r.append("decree")
        if not a.get("deed_id"):
            r.insert(0, "deed")
        return r

    def applicant(b):
        a = b.get("applicant") if isinstance(b.get("applicant"), dict) else {}
        p = party(a)
        if not p["father"] or not p["address"]:
            raise ValueError("father's name and address required / পিতার নাম ও ঠিকানা দিন")
        if not re.fullmatch(r"\d{10}|\d{13}|\d{17}", p["nid"]):
            raise ValueError("NID must be 10, 13 or 17 digits / এনআইডি ১০, ১৩ বা ১৭ সংখ্যার হতে হবে")
        mob = re.sub(r"[\s-]", "", str(a.get("mobile") or ""))
        if not re.fullmatch(r"(\+?88)?01[3-9]\d{8}", mob):
            raise ValueError("valid mobile number required / সঠিক মোবাইল নম্বর দিন")
        p["mobile"], p["email"] = mob, str(a.get("email") or "").strip()[:80]
        return p

    def app_checks(a, rec):
        """[[key, level, info]] — level is pass / warn / fail / waived (admin override)."""
        d = db["deeds"].get(a.get("deed_id") or "")
        miss = [k for k in required_docs(a) if k not in a["docs"]]
        plot_dec = a["land"]["area_sqm"] / 40.4686
        c = [["docs_complete", "pass" if not miss else "fail", ", ".join(miss)],
             ["deed_registered", "pass" if (d and d["status"] == "approved") or a.get("external") else "fail",
              (d or {}).get("deed_no") or (a.get("external") or {}).get("no", "")],
             ["area_match", "pass" if 0 < a["area_dec"] <= plot_dec + 0.01 else "fail", "%.2f / %.2f" % (a["area_dec"], plot_dec)]]
        c += pre_checks(rec)
        h = zlib.crc32(("tax:%s:%s" % (a["land"]["ds"], a["land"]["plot_no"])).encode()) % 10
        c.append(["tax_paid", "fail" if not a["ldt"]["receipt"] else "pass" if h < 8 else "warn",
                  "%s / %s" % (a["ldt"]["receipt"] or "—", a["ldt"]["paid_till"] or "—")])
        c.append(["nid_ok", "pass" if re.fullmatch(r"\d{10}|\d{13}|\d{17}", a["applicant"]["nid"]) else "fail", ""])
        seller = (d or {}).get("seller") or (a.get("external") or {}).get("seller") or {}
        buyer = (d or {}).get("buyer") or {}
        same = lambda x, y: str(x).strip().lower() == str(y).strip().lower()
        c.append(["applicant_match", "pass" if (not buyer or same(buyer.get("name", ""), a["applicant"]["name"])) else "warn",
                  "%s / %s" % (buyer.get("name", "—"), a["applicant"]["name"])])
        owner = rec["owner"] if rec else None
        chain = a["basis"] == "inheritance" or not owner or not seller.get("name") or any(same(seller["name"], owner[k]) for k in ("bn", "en"))
        c.append(["owner_chain", "pass" if chain else "warn", "%s / %s" % (seller.get("name", "—"), owner["bn"] if owner else "—")])
        for row in c:
            w = (a.get("waivers") or {}).get(row[0])
            if w and row[1] in ("fail", "warn"):
                row[1] = "waived"
                row[2] = (row[2] + " · " if row[2] else "") + w["reason"]
        return c

    def fee_block(a):
        paid = a.get("fees_paid") or {}
        return {"items": [{"key": k, "amount": v, "paid": k in paid} for k, v in FEE_ITEMS],
                "total": sum(v for _, v in FEE_ITEMS), "remaining": sum(v for k, v in FEE_ITEMS if k not in paid)}

    def sla_left(a):
        """Days to the service deadline (negative = overdue). The clock is paused while the case is on hold."""
        if not a.get("sla_due"):
            return None
        due = date.fromisoformat(a["sla_due"])
        if a.get("held_at"):
            due += timedelta(days=(date.today() - date.fromisoformat(a["held_at"][:10])).days)
        return (due - date.today()).days

    def eta(a):
        """Dynamic estimate: planned days of the remaining stages + whatever the current stage still needs."""
        if a["status"] in ("completed", "rejected", "draft", "returned") or a["stage"] < 1:
            return None
        st, td = a["stage"], date.today()
        per = dict(EST)
        n, h = a.get("notice"), a.get("hearing")
        if n:
            per[3] = n["days"]
            if n["objections"]:
                per[4] = EST[4] + 7
        since = date.fromisoformat((a.get("stage_since") or a.get("submitted") or now())[:10])
        elapsed = (td - since).days
        left = per.get(st, 0) - elapsed
        if st == 3 and n and not n["closed"]:
            left = (date.fromisoformat(n["deadline"]) - td).days
        if st == 4 and h and h.get("resolved") is None:
            left = (date.fromisoformat(h["date"]) - td).days + 1
        overdue = max(-left, 0)
        remaining = max(left, 0) + sum(per[s] for s in range(st + 1, 7))
        end = td + timedelta(days=remaining)
        sl = sla_left(a)
        due = td + timedelta(days=sl) if sl is not None else None
        state = "delayed" if (sl is not None and sl < 0) or overdue > 0 else "at_risk" if due and end > due else "on_track"
        return {"remaining": remaining, "end": end.isoformat(), "overdue": overdue, "elapsed": elapsed,
                "budget": per.get(st, 0), "state": state}

    def app_view(a, brief=False):
        v = dict(a)
        v["required"] = required_docs(a)
        v["fees"] = fee_block(a)
        v["sla_left"] = sla_left(a)
        v["eta"] = eta(a)
        d = db["deeds"].get(a.get("deed_id") or "")
        v["deed_no"] = (d or {}).get("deed_no") or (a.get("external") or {}).get("no", "")
        if not brief:
            v["checks"] = app_checks(a, get_rec(a["land"]["ds"], a["land"]["plot_no"]))
            v["deed"] = deed_view(d) if d else None
        return v

    def new_app(b, adm, uid, channel):
        applicant_ = applicant(b)
        did, d, ext = str(b.get("deed_id") or ""), None, None
        if did:
            d = db["deeds"].get(did)
            if not d or d["status"] != "approved":
                raise ActErr("a registered deed is required / নিবন্ধিত দলিল ছাড়া নামজারি শুরু করা যায় না", 422)
            if not adm and d.get("uid") != uid:
                raise ActErr("forbidden", 403)
            if any(x.get("deed_id") == did and x["status"] != "rejected" for x in db["apps"].values()):
                raise ActErr("an application already exists for this deed / এই দলিলের জন্য আবেদন আছে", 409)
            basis, area, rec = d["type"], d["area_dec"], get_rec(d["land"]["ds"], d["land"]["plot_no"])
        else:
            if not adm:
                raise ActErr("a registered deed is required / নিবন্ধিত দলিল ছাড়া নামজারি শুরু করা যায় না", 422)
            e = b.get("external") if isinstance(b.get("external"), dict) else {}
            rec = get_rec(e.get("ds"), e.get("plot_no"))
            if not rec:
                raise ActErr("plot not found / দাগ পাওয়া যায়নি", 404)
            if e.get("type") not in DEED_TYPES or not str(e.get("deed_no") or "").strip():
                raise ActErr("deed type and deed no. required / দলিলের ধরন ও নম্বর দিন")
            try:
                area = round(float(e.get("area_dec")), 4)
                dd = date.fromisoformat(str(e.get("deed_date") or "")[:10])
            except (TypeError, ValueError):
                raise ActErr("area and deed date required / পরিমাণ ও দলিলের তারিখ দিন")
            if area <= 0 or area > rec["area_sqm"] / 40.4686 + 0.01:
                raise ActErr("area out of range / পরিমাণ সীমার বাইরে")
            basis = e["type"]
            ext = {"no": str(e["deed_no"]).strip()[:30], "date": dd.isoformat(), "type": basis,
                   "seller": party(e.get("seller")), "area_dec": area}
        if not rec:
            raise ActErr("plot not found / দাগ পাওয়া যায়নি", 404)
        aid = "MA-%d-%05d" % (date.today().year, seq("app"))
        a = {"id": aid, "uid": (d.get("uid") if d else None) if adm else uid, "channel": channel,
             "deed_id": did or None, "external": ext, "basis": basis, "court_ordered": bool(b.get("court_ordered")),
             "land": snap(rec), "area_dec": area, "applicant": applicant_,
             "prev_khatian": str(b.get("prev_khatian") or "").strip()[:30],
             "ldt": {"receipt": str(b.get("ldt_receipt") or "").strip()[:30], "paid_till": str(b.get("ldt_paid_till") or "").strip()[:9]},
             "docs": {}, "stage": 0, "status": "draft", "created": now(), "submitted": None, "sla_due": None,
             "reason": "", "timeline": [ev("created")]}
        db["apps"][aid] = a
        return a

    @app.route("/api/mut/apps", methods=["GET", "POST"])
    def mut_apps():
        if request.method == "GET":
            boot()
            rows = [app_view(a, True) for a in db["apps"].values() if mine(a)]
            rows.sort(key=lambda a: a["created"], reverse=True)
            return jsonify(apps=rows)
        adm = is_admin()
        with lock:
            try:
                a = new_app(request.get_json(silent=True) or {}, adm, user().get("id"), "offline" if adm else "online")
            except ActErr as e:
                return err(str(e), e.code)
            except ValueError as e:
                return err(str(e))
            save()
        return jsonify(ok=True, app=app_view(a))

    @app.route("/api/mut/apps/<aid>", methods=["GET", "PUT"])
    def mut_app(aid):
        a = db["apps"].get(aid)
        if not a:
            return err("application not found / আবেদন পাওয়া যায়নি", 404)
        if not mine(a):
            return err("forbidden", 403)
        if request.method == "GET":
            return jsonify(app=app_view(a))
        if a["status"] not in ("draft", "returned"):
            return err("not editable now / এখন সংশোধন করা যাবে না", 409)
        b = request.get_json(silent=True) or {}
        try:
            ap = applicant(b)
        except ValueError as e:
            return err(str(e))
        with lock:
            a["applicant"] = ap
            a["court_ordered"] = bool(b.get("court_ordered"))
            a["prev_khatian"] = str(b.get("prev_khatian") or "").strip()[:30]
            a["ldt"] = {"receipt": str(b.get("ldt_receipt") or "").strip()[:30], "paid_till": str(b.get("ldt_paid_till") or "").strip()[:9]}
            save()
        return jsonify(ok=True, app=app_view(a))

    @app.route("/api/mut/apps/<aid>/doc/<key>", methods=["POST", "DELETE"])
    def mut_doc(aid, key):
        a = db["apps"].get(aid)
        if not a or key not in DOC_KEYS:
            return err("not found", 404)
        if not mine(a):
            return err("forbidden", 403)
        adm = is_admin()
        if not (a["status"] in ("draft", "returned") or (adm and a["status"] not in ("completed", "rejected") and a["stage"] <= 3)):
            return err("documents are locked at this stage / এই ধাপে কাগজপত্র বদলানো যায় না", 409)
        folder = os.path.join(files_dir, aid)

        def wipe():
            if os.path.isdir(folder):
                for fn in os.listdir(folder):
                    if fn.split(".")[0] == key:
                        try:
                            os.remove(os.path.join(folder, fn))
                        except OSError:
                            pass
        with lock:
            if request.method == "DELETE":
                wipe()
                a["docs"].pop(key, None)
                save()
                return jsonify(ok=True)
            b = request.get_json(silent=True) or {}
            if b.get("paper"):
                if not adm:
                    return err("admin only / শুধু অ্যাডমিন", 403)
                wipe()
                a["docs"][key] = {"paper": True, "name": "paper", "uploaded": now()}
                save()
                return jsonify(ok=True)
            data = str(b.get("data") or "")
            if data[:60].startswith("data:") and "," in data[:200]:
                data = data.split(",", 1)[1]
            try:
                raw = base64.b64decode(data)
            except Exception:
                return err("invalid file data")
            if not raw or len(raw) > MAX_FILE:
                return err("file must be under 2 MB / ফাইল ২ এমবির কম হতে হবে", 413)
            mime = _sniff(raw)
            if not mime or (key in ("photo", "signature") and mime == "application/pdf"):
                return err("unsupported file type (JPG, PNG, WEBP or PDF) / ফাইলের ধরন সমর্থিত নয়")
            os.makedirs(folder, exist_ok=True)
            wipe()
            with open(os.path.join(folder, "%s.%s" % (key, EXT[mime])), "wb") as f:
                f.write(raw)
            a["docs"][key] = {"name": os.path.basename(str(b.get("name") or key))[:80], "mime": mime, "ext": EXT[mime],
                              "size": len(raw), "uploaded": now()}
            save()
        return jsonify(ok=True)

    @app.route("/api/mut/apps/<aid>/file/<key>")
    def mut_file(aid, key):
        a = db["apps"].get(aid)
        meta = a and a["docs"].get(key)
        if not a or not mine(a):
            return err("forbidden", 403)
        if not meta or meta.get("paper"):
            return err("not found", 404)
        path = os.path.join(files_dir, aid, "%s.%s" % (key, meta["ext"]))
        if not os.path.isfile(path):
            return err("not found", 404)
        return send_file(path, mimetype=meta["mime"], max_age=0)

    def do_submit(a):
        if a["status"] not in ("draft", "returned"):
            raise ActErr("already submitted / ইতোমধ্যে জমা দেওয়া হয়েছে", 409)
        miss = [k for k in required_docs(a) if k not in a["docs"]]
        if miss:
            raise ActErr("documents missing / কাগজপত্র অসম্পূর্ণ", 422, missing=miss)
        aid, first = a["id"], a["submitted"] is None
        a.update(stage=1, status="ok", reason="", submitted=a["submitted"] or now(), stage_since=now(),
                 sla_due=(date.today() + timedelta(days=SLA_DAYS)).isoformat())
        if first:
            txn = "TXN%08d" % (zlib.crc32(aid.encode()) % 10**8)
            a["fees_paid"] = {"application": {"amount": 100, "txn": txn, "date": today(),
                                              "method": "cash" if a["channel"] == "offline" else "demo"}}
            a["timeline"] += [ev("application"), ev("verification")]
        else:
            a["timeline"] += [ev("resubmitted"), ev("verification")]
        tell(a, "আপনার নামজারি আবেদন (%s) জমা হয়েছে — নথি যাচাই চলছে। আবেদন ফি ৳১০০ গৃহীত।" % bd(aid),
             "Your mutation application (%s) is submitted — verification is in progress. Application fee ৳100 received." % aid)
        notify("admin", aid, "app", "নতুন নামজারি আবেদন জমা: %s" % aid, "New mutation application submitted: %s" % aid)

    @app.route("/api/mut/apps/<aid>/submit", methods=["POST"])
    def mut_submit(aid):
        a = db["apps"].get(aid)
        if not a:
            return err("application not found / আবেদন পাওয়া যায়নি", 404)
        if not mine(a):
            return err("forbidden", 403)
        with lock:
            try:
                do_submit(a)
            except ActErr as e:
                if e.extra:
                    return jsonify(error=str(e), **e.extra), e.code
                return err(str(e), e.code)
            save()
        return jsonify(ok=True, app=app_view(a))

    def complete_payment(a, method, txn, account=""):
        paid = dict(a.get("fees_paid") or {})
        rem = [(k, v) for k, v in FEE_ITEMS if k not in paid]
        for k, v in rem:
            paid[k] = {"amount": v, "txn": txn, "date": today(), "method": method}
        a["fees_paid"] = paid
        a["payment"] = {"amount": sum(v for _, v in rem), "method": method, "txn": txn, "date": today(), "account": account}
        y, n = date.today().year, zlib.crc32(("kh:" + a["id"]).encode())
        a["khatian"] = {"khatian_no": n % 9000 + 1000, "dcr_no": "DCR-%d-%d" % (y, n % 900000 + 100000), "issued": today()}
        a["stage"], a["status"] = 7, "completed"
        a["timeline"] += [ev("paid"), ev("khatian")]
        tell(a, "ফি পরিশোধ সম্পন্ন — নামজারি খতিয়ান ও ডিসিআর প্রস্তুত। এখনই ডাউনলোড করুন।",
             "Fee payment complete — your mutation khatian and DCR are ready. Download now.")
        notify("admin", a["id"], "app", "নামজারি সম্পন্ন, খতিয়ান জারি: %s" % a["id"], "Mutation completed, khatian issued: %s" % a["id"])

    @app.route("/api/mut/apps/<aid>/pay", methods=["POST"])
    def mut_pay(aid):
        a = db["apps"].get(aid)
        if not a or not a.get("uid") or a["uid"] != user().get("id"):
            return err("forbidden", 403)
        b = request.get_json(silent=True) or {}
        method = b.get("method")
        if method not in PAY_METHODS:
            return err("choose a payment method / পেমেন্ট পদ্ধতি বেছে নিন")
        try:
            acct = check_pay(method, b.get("fields"))
        except ValueError as e:
            return err(str(e), 422)
        with lock:
            if a["stage"] != 6 or a["status"] != "awaiting_payment":
                return err("payment is not due / এখন ফি পরিশোধের সময় নয়", 409)
            complete_payment(a, method, "TXN%s%04d" % (datetime.now().strftime("%y%m%d%H%M%S"), zlib.crc32(aid.encode()) % 10000), acct)
            save()
        return jsonify(ok=True, app=app_view(a))

    @app.route("/api/mut/apps/<aid>/special/<sid>/reply", methods=["POST"])
    def mut_special_reply(aid, sid):
        a = db["apps"].get(aid)
        if not a or not mine(a) or is_admin():
            return err("forbidden", 403)
        text = str((request.get_json(silent=True) or {}).get("text") or "").strip()[:500]
        if not text:
            return err("reply required / উত্তর লিখুন")
        with lock:
            sn = next((x for x in a.get("special_notices", []) if x["id"] == sid), None)
            if not sn:
                return err("notice not found", 404)
            if sn["closed"]:
                return err("notice is closed / নোটিশ বন্ধ", 409)
            sn["replies"].append({"by": user().get("name"), "text": text, "ts": now()})
            a["timeline"].append(ev("replied", text))
            notify("admin", aid, "app", "বিশেষ নোটিশের উত্তর দাখিল: %s" % aid, "Reply to special notice: %s" % aid)
            save()
        return jsonify(ok=True, app=app_view(a))

    @app.route("/api/mut/track/<xid>")
    def mut_track(xid):
        """The single tracking endpoint: application id, deed id, deed no., notice no., DCR no."""
        boot()
        q = xid.strip()
        o = db["apps"].get(q.upper()) or db["deeds"].get(q.upper())
        if not o:
            o = next((x for x in db["apps"].values() if q.upper() in (
                (x.get("notice") or {}).get("no", ""), (x.get("khatian") or {}).get("dcr_no", ""))), None) \
                or next((x for x in db["deeds"].values() if q == x.get("deed_no")), None)
        if not o:
            return err("not found / পাওয়া যায়নি", 404)
        isapp = o["id"] in db["apps"]
        out = dict(kind="app" if isapp else "deed", id=o["id"], status=o["status"], stage=o.get("stage"), channel=o["channel"],
                   created=o["created"], land={"label": o["land"]["label"], "district": o["land"]["district"], "mouza": o["land"]["mouza"],
                                              "ds": o["land"]["ds"], "plot_no": o["land"]["plot_no"]},
                   timeline=[{"ts": e["ts"], "key": e["key"]} for e in o["timeline"]], mine=mine(o))
        if isapp:
            out.update(basis=o["basis"], eta=eta(o), sla_left=sla_left(o), sla_due=o.get("sla_due"))
        else:
            out.update(type=o["type"], deed_no=o.get("deed_no"))
        return jsonify(out)

    @app.route("/api/mut/demo")
    def mut_demo():
        """One-click tracking samples (a spread of stages/statuses)."""
        boot()
        seen, apps = set(), []
        for a in sorted(db["apps"].values(), key=lambda x: (x["status"], x["stage"])):
            k = (a["status"], a["stage"])
            if k not in seen and len(apps) < 10:
                seen.add(k)
                apps.append({"id": a["id"], "status": a["status"], "stage": a["stage"]})
        deeds, seen = [], set()
        for d in db["deeds"].values():
            if d["status"] not in seen and len(deeds) < 4:
                seen.add(d["status"])
                deeds.append({"id": d["id"], "status": d["status"]})
        return jsonify(apps=apps, deeds=deeds)

    # ------------------------------------------------------------------ admin processing
    def wrong():
        return ActErr("not allowed at this stage / এই ধাপে সম্ভব নয়", 409)

    def app_act(a, act, b):
        aid = a["id"]
        if a["status"] in ("completed", "rejected"):
            raise ActErr("case is closed / কেস বন্ধ", 409)
        old_stage, st, status = a["stage"], a["stage"], a["status"]
        reason = str(b.get("reason") or "").strip()[:300]
        note = str(b.get("note") or "").strip()[:300]
        if act == "hold":
            if not reason:
                raise ActErr("reason required / কারণ লিখুন")
            if status == "flagged":
                raise ActErr("already on hold", 409)
            a["held_from"], a["status"], a["reason"], a["held_at"] = status, "flagged", reason, now()
            a["timeline"].append(ev("held", reason))
            tell(a, "আপনার আবেদন সাময়িক স্থগিত (সময়ের ঘড়ি বন্ধ): " + reason, "Your application is on hold (SLA clock paused): " + reason)
        elif act == "release":
            if status != "flagged":
                raise ActErr("not on hold", 409)
            days = (date.today() - date.fromisoformat(a["held_at"][:10])).days if a.get("held_at") else 0
            if days and a.get("sla_due"):
                a["sla_due"] = (date.fromisoformat(a["sla_due"]) + timedelta(days=days)).isoformat()
            if days and a.get("stage_since"):
                a["stage_since"] = (date.fromisoformat(a["stage_since"][:10]) + timedelta(days=days)).isoformat()
            a.pop("held_at", None)
            a["status"], a["reason"] = a.pop("held_from", "ok"), ""
            a["timeline"].append(ev("released", note or reason))
            tell(a, "স্থগিতাদেশ প্রত্যাহার — প্রক্রিয়া চলছে।", "Hold lifted — processing continues.")
        elif act == "reject":
            if not reason:
                raise ActErr("reason required / কারণ লিখুন")
            a["status"], a["reason"] = "rejected", reason
            a["timeline"].append(ev("rejected", reason))
            tell(a, "আপনার নামজারি আবেদন প্রত্যাখ্যাত: " + reason, "Your mutation application was rejected: " + reason)
        elif act in ("waive", "unwaive"):
            key = str(b.get("key") or "")
            if st < 1:
                raise wrong()
            if key not in [r[0] for r in app_checks(a, get_rec(a["land"]["ds"], a["land"]["plot_no"]))]:
                raise ActErr("unknown check")
            if act == "waive":
                if not reason:
                    raise ActErr("reason required / কারণ লিখুন")
                a.setdefault("waivers", {})[key] = {"by": user().get("name"), "date": today(), "reason": reason}
                a["timeline"].append(ev("waived", "%s: %s" % (key, reason)))
                tell(a, "একটি স্বয়ংক্রিয় যাচাই কর্মকর্তা কর্তৃক অব্যাহতি পেয়েছে: " + reason, "An automated check was waived by the officer: " + reason)
            else:
                a.get("waivers", {}).pop(key, None)
                a["timeline"].append(ev("unwaived", key))
        elif act == "special_notice":
            to = b.get("to")
            subject, message = str(b.get("subject") or "").strip()[:120], str(b.get("message") or "").strip()[:600]
            if to not in ("applicant", "previous_owner", "neighbors", "other") or not subject or not message:
                raise ActErr("recipient, subject and message required / প্রাপক, বিষয় ও বক্তব্য দিন")
            try:
                days = max(1, min(30, int(b.get("days") or 7)))
            except (TypeError, ValueError):
                days = 7
            d = db["deeds"].get(a.get("deed_id") or "")
            prev = ((d or {}).get("seller") or (a.get("external") or {}).get("seller") or {}).get("name", "")
            names = {"applicant": [a["applicant"]["name"]], "previous_owner": [prev] if prev else [],
                     "neighbors": (a.get("inspection") or {}).get("neighbors", []), "other": [str(b.get("other") or "").strip()[:80]]}[to]
            n = len(a.get("special_notices", [])) + 1
            a.setdefault("special_notices", []).append({
                "id": "SN-%s-%d" % (aid, n), "to": to, "names": [x for x in names if x], "subject": subject, "message": message,
                "check": str(b.get("check") or "")[:30], "issued": today(), "days": days,
                "deadline": (date.today() + timedelta(days=days)).isoformat(), "by": user().get("name"), "replies": [], "closed": False})
            a["timeline"].append(ev("special_notice", subject))
            if to == "applicant":
                tell(a, "বিশেষ নোটিশ: %s — %s দিনের মধ্যে সাড়া দিন।" % (subject, bd(days)), "Special notice: %s — please respond within %d day(s)." % (subject, days))
        elif act == "close_special":
            sn = next((x for x in a.get("special_notices", []) if x["id"] == b.get("sid")), None)
            if not sn:
                raise ActErr("notice not found", 404)
            sn["closed"] = True
            a["timeline"].append(ev("special_closed", sn["subject"]))
        elif act == "return":
            if st < 1 or st > 4:
                raise wrong()
            if not reason:
                raise ActErr("reason required / কারণ লিখুন")
            a["status"], a["stage"], a["reason"] = "returned", 0, reason
            a.pop("held_at", None)
            a["timeline"].append(ev("returned", reason))
            tell(a, "আবেদন সংশোধনের জন্য ফেরত: " + reason + " — সংশোধন করে পুনরায় জমা দিন।",
                 "Application returned for correction: " + reason + " — fix and resubmit.")
        elif status == "flagged":
            raise ActErr("on hold — release first / স্থগিত — আগে ছাড় দিন", 409)
        elif status in ("draft", "returned"):
            raise ActErr("waiting for the applicant / আবেদনকারীর অপেক্ষায়", 409)
        elif act == "verify":
            if st != 1:
                raise wrong()
            checks = app_checks(a, get_rec(a["land"]["ds"], a["land"]["plot_no"]))
            failed = [k for k, v, _i in checks if v == "fail"]
            if failed:
                raise ActErr("failed checks — waive, return or reject: %s / ব্যর্থ যাচাই — অব্যাহতি, ফেরত বা প্রত্যাখ্যান করুন: %s"
                             % (", ".join(failed), ", ".join(failed)), 409)
            a["verification"] = {"by": user().get("name"), "date": today(), "note": note, "checks": checks}
            a["stage"] = 2
            a["timeline"] += [ev("verified", note), ev("inspection")]
            tell(a, "নথি ও রেকর্ড যাচাই সম্পন্ন — মাঠ পর্যায়ে তদন্ত হবে।", "Documents and records verified — field inspection will follow.")
        elif act == "inspect":
            if st != 2:
                raise wrong()
            insp, poss = str(b.get("inspector") or "").strip()[:80], b.get("possession")
            if not insp or poss not in ("full", "partial", "none", "disputed"):
                raise ActErr("inspector and possession required / তদন্তকারী ও দখলের অবস্থা দিন")
            try:
                found = round(float(b.get("area_found")), 4)
            except (TypeError, ValueError):
                raise ActErr("area found required / মাঠে প্রাপ্ত পরিমাণ দিন")
            try:
                idt = date.fromisoformat(str(b.get("date") or "")[:10])
            except ValueError:
                idt = date.today()
            bounds = {k: str((b.get("boundaries") or {}).get(k) or "").strip()[:100] for k in ("north", "south", "east", "west")}
            nb = [str(x).strip()[:80] for x in (b.get("neighbors") or []) if str(x).strip()][:8]
            claimed = a["area_dec"]
            a["inspection"] = {"inspector": insp, "date": idt.isoformat(), "possession": poss, "area_found": found,
                               "area_claimed": claimed, "area_match": claimed > 0 and abs(found - claimed) / claimed <= 0.05,
                               "boundaries": bounds, "neighbors": nb, "remarks": str(b.get("remarks") or "").strip()[:400]}
            a["stage"] = 3
            a["timeline"] += [ev("inspected"), ev("notice")]
            tell(a, "তদন্ত প্রতিবেদন প্রস্তুত — সংশ্লিষ্ট পক্ষদের নোটিশ জারি করা হবে।", "Inspection report filed — notice will be issued to interested parties.")
        elif act == "issue_notice":
            if st != 3 or a.get("notice"):
                raise wrong()
            try:
                days = max(7, min(60, int(b.get("days") or 10)))
            except (TypeError, ValueError):
                days = 10
            d = db["deeds"].get(a.get("deed_id") or "")
            prev = (d or {}).get("seller") or (a.get("external") or {}).get("seller") or {}
            rcp = []
            if prev.get("name"):
                rcp.append({"role": "previous_owner", "name": prev["name"], "address": prev.get("address", "")})
            for n in (a.get("inspection") or {}).get("neighbors", []):
                rcp.append({"role": "neighbor", "name": n, "address": ""})
            for n in (b.get("extra") or [])[:6]:
                if str(n).strip():
                    rcp.append({"role": "other", "name": str(n).strip()[:80], "address": ""})
            dl = date.today() + timedelta(days=days)
            a["notice"] = {"no": "NT-" + aid, "issued": today(), "deadline": dl.isoformat(), "days": days,
                           "recipients": rcp, "objections": [], "closed": False}
            a["timeline"].append(ev("notice_issued"))
            tell(a, "নোটিশ জারি হয়েছে — আপত্তি দাখিলের শেষ তারিখ %s।" % bd(dl.isoformat()), "Notice issued — objection deadline %s." % dl.isoformat())
        elif act == "objection":
            n = a.get("notice")
            by, text = str(b.get("by") or "").strip()[:80], str(b.get("text") or "").strip()[:500]
            if st != 3 or not n or n["closed"]:
                raise wrong()
            if not by or not text:
                raise ActErr("objector and statement required / আপত্তিকারী ও বক্তব্য দিন")
            n["objections"].append({"by": by, "text": text, "date": today()})
            a["timeline"].append(ev("objection", "%s: %s" % (by, text)))
            tell(a, "আপনার আবেদনের বিরুদ্ধে আপত্তি দাখিল হয়েছে — শুনানির তারিখ জানানো হবে।",
                 "An objection was filed against your application — the hearing date will be notified.")
        elif act == "close_notice":
            n = a.get("notice")
            if st != 3 or not n or n["closed"]:
                raise wrong()
            n["closed"] = True
            a["stage"] = 4
            a["timeline"].append(ev("hearing"))
            if n["objections"]:
                tell(a, "আপত্তির সময়সীমা শেষ — আপত্তি থাকায় শুনানি অনুষ্ঠিত হবে।", "Objection period over — a hearing will be held because objections exist.")
            else:
                tell(a, "আপত্তির সময়সীমা শেষ — কোনো আপত্তি নেই; এসি (ল্যান্ড) পর্যালোচনা চলছে।",
                     "Objection period over — no objections; AC (Land) review is under way.")
        elif act == "schedule_hearing":
            n = a.get("notice")
            if st != 4 or not (n and n["objections"]):
                raise wrong()
            try:
                hd = date.fromisoformat(str(b.get("date") or "")[:10])
            except ValueError:
                raise ActErr("hearing date required / শুনানির তারিখ দিন")
            if hd < date.today():
                raise ActErr("hearing date cannot be in the past / শুনানির তারিখ অতীত হতে পারে না")
            place = str(b.get("place") or "").strip()[:120] or "%s এসি (ল্যান্ড) অফিস" % a["land"]["district"]["bn"]
            tm = str(b.get("time") or "11:00")[:5]
            a["hearing"] = {"date": hd.isoformat(), "time": tm, "place": place, "outcome": "", "resolved": None}
            a["timeline"].append(ev("hearing_scheduled", "%s %s, %s" % (hd.isoformat(), tm, place)))
            tell(a, "শুনানির তারিখ: %s, সময়: %s, স্থান: %s।" % (bd(hd.isoformat()), bd(tm), place), "Hearing on %s at %s, venue: %s." % (hd.isoformat(), tm, place))
        elif act == "record_hearing":
            h, outcome = a.get("hearing"), str(b.get("outcome") or "").strip()[:500]
            if st != 4 or not h:
                raise wrong()
            if not outcome:
                raise ActErr("outcome required / শুনানির ফলাফল লিখুন")
            h.update(outcome=outcome, resolved=bool(b.get("resolved")), held_on=today())
            a["timeline"].append(ev("hearing_held", outcome))
            tell(a, "শুনানি সম্পন্ন হয়েছে।", "The hearing has been held.")
        elif act == "review":
            if st != 4:
                raise wrong()
            decision, remarks = b.get("decision"), str(b.get("remarks") or "").strip()[:400]
            flags = {k: bool(b.get(k)) for k in ("authentic", "chain", "inspection_ok", "legal_ok")}
            rv = dict(flags, remarks=remarks, decision=decision, by=user().get("name"), date=today())
            if decision == "reject":
                if not remarks:
                    raise ActErr("reason required / কারণ লিখুন")
                a["review"], a["status"], a["reason"] = rv, "rejected", remarks
                a["timeline"].append(ev("rejected", remarks))
                tell(a, "এসি (ল্যান্ড) পর্যালোচনায় আবেদন প্রত্যাখ্যাত: " + remarks, "Rejected at AC (Land) review: " + remarks)
            elif decision == "approve":
                if not all(flags.values()):
                    raise ActErr("confirm all four review points / চারটি বিষয়ই নিশ্চিত করুন")
                n, h = a["notice"], a.get("hearing")
                if n["objections"] and not (h and h.get("resolved")):
                    raise ActErr("hold the hearing and resolve objections first / আগে শুনানি করে আপত্তি নিষ্পত্তি করুন", 409)
                a["review"], a["stage"] = rv, 5
                a["timeline"] += [ev("reviewed", remarks), ev("order")]
                tell(a, "এসি (ল্যান্ড) পর্যালোচনা সম্পন্ন — নামজারি আদেশের অপেক্ষায়।", "AC (Land) review complete — awaiting the mutation order.")
            else:
                raise ActErr("decision required")
        elif act == "issue_order":
            if st != 5:
                raise wrong()
            n = seq("order")
            a["order"] = {"no": "%d/%d" % (n, date.today().year), "date": today(), "by": user().get("name")}
            rem = fee_block(a)["remaining"]
            a["stage"], a["status"] = 6, "awaiting_payment"
            a["timeline"] += [ev("order_issued"), ev("payment")]
            tell(a, "নামজারি আদেশ জারি হয়েছে (আদেশ নং %s) — অনুগ্রহ করে অবশিষ্ট ফি ৳%s পরিশোধ করুন।" % (bd(a["order"]["no"]), bd(rem)),
                 "Mutation order issued (order no. %s) — please pay the remaining fee ৳%s." % (a["order"]["no"], rem))
        elif act == "mark_paid":
            rc = str(b.get("receipt") or "").strip()[:30]
            if st != 6 or status != "awaiting_payment":
                raise wrong()
            if not rc:
                raise ActErr("receipt no. required / রশিদ নম্বর দিন")
            complete_payment(a, "office", rc, "অফিস কাউন্টার")
        else:
            raise ActErr("unknown action")
        if a["stage"] != old_stage:
            a["stage_since"] = now()

    @app.route("/api/admin/mut/apps/<aid>/<act>", methods=["POST"])
    def mut_app_act(aid, act):
        b = request.get_json(silent=True) or {}
        with lock:
            a = db["apps"].get(aid)
            if not a:
                return err("application not found / আবেদন পাওয়া যায়নি", 404)
            try:
                app_act(a, act, b)
            except ActErr as e:
                return err(str(e), e.code)
            save()
        return jsonify(ok=True, app=app_view(a))

    # ------------------------------------------------------------------ demo data
    def shift(a, age, sage):
        dl_ = timedelta(days=age)
        back = lambda s: (datetime.fromisoformat(s) - dl_).isoformat(timespec="seconds")
        for e in a["timeline"]:
            e["ts"] = back(e["ts"])
        a["created"] = back(a["created"])
        if a.get("submitted"):
            a["submitted"] = back(a["submitted"])
            a["sla_due"] = (date.fromisoformat(a["submitted"][:10]) + timedelta(days=SLA_DAYS)).isoformat()
        a["stage_since"] = (datetime.now() - timedelta(days=sage)).isoformat(timespec="seconds")
        n = a.get("notice")
        if n:
            iss = date.today() - timedelta(days=sage)
            n["issued"], n["deadline"] = iss.isoformat(), (iss + timedelta(days=n["days"])).isoformat()

    def seed_demo():
        if not index_fn:
            return 0
        keys = []
        for k in index_fn()["plots"]:
            r = get_rec(*k)
            if r and r["khas"] == "none" and r["acq"] != "notified" and not any(f["type"] in ("fake_deed", "double_sale") for f in r["flags"]):
                keys.append(k)
            if len(keys) >= 24:
                break
        if not keys:
            return 0
        tl_.u = {"id": "admin1", "role": "admin", "name": {"bn": "রফিকুল ইসলাম", "en": "Rafiqul Islam"}}
        made = 0
        try:
            def mk_deed(i, basis, pi, age):
                ds, no = keys[i % len(keys)]
                rec, p, rng = get_rec(ds, no), DEMO_P[pi], random.Random("seed%d" % i)
                area = round(rec["area_sqm"] / 40.4686 * rng.uniform(0.3, 0.7), 2)
                f = {"type": basis, "land": snap(rec), "area_dec": area,
                     "value": int(area * rng.randint(60, 140) * 1000) if basis == "sale" else 0,
                     "seller": {"name": rec["owner"]["bn"], "father": rec["father"]["bn"], "nid": "", "address": "পূর্বের মালিক"},
                     "buyer": {"name": p[0], "father": p[1], "nid": p[2], "address": p[3]},
                     "witnesses": [DEMO_P[3][0], DEMO_P[4][0]],
                     "boundaries": {"north": "রাস্তা", "south": "খাল", "east": "মো. ফজলুর রহমানের জমি", "west": "আব্দুল মান্নানের জমি"},
                     "deed_date": (date.today() - timedelta(days=age + 3)).isoformat(), "note": ""}
                return new_deed(f, p[5], "online"), rec, area, p, rng

            for i, (upto, pi, basis, age, sage, fl) in enumerate(DEMO_SPECS):
                d, rec, area, p, rng = mk_deed(i, basis, pi, age)
                deed_act(d, "approve", {})
                a = new_app({"deed_id": d["id"], "applicant": {"name": p[0], "father": p[1], "nid": p[2], "mobile": p[4], "email": "", "address": p[3]},
                             "prev_khatian": str(rec["khatian"]),
                             "ldt_receipt": "" if "noldt" in fl else "LDT-%d" % rng.randint(10000, 99999),
                             "ldt_paid_till": str(date.today().year - 1)}, True, None, "online")
                if upto >= 1:
                    for k in required_docs(a):
                        a["docs"][k] = {"paper": True, "name": "paper", "uploaded": now()}
                    do_submit(a)
                st_ = lambda name, body=None: app_act(a, name, body or {})
                if upto >= 2:
                    st_("verify", {"note": "নথি ও রেকর্ড যাচাই সম্পন্ন"})
                if upto >= 3:
                    st_("inspect", {"inspector": "মো. শফিকুল ইসলাম (কানুনগো)", "date": today(), "possession": "full", "area_found": area,
                                    "boundaries": {"north": "রাস্তা", "south": "খাল", "east": "ফজলুর রহমান", "west": "আব্দুল মান্নান"},
                                    "neighbors": ["ফজলুর রহমান", "আব্দুল মান্নান"], "remarks": "জমি আবেদনকারীর দখলে, সীমানা স্পষ্ট।"})
                if upto >= 4:
                    st_("issue_notice", {"days": 10, "extra": []})
                    if "obj" in fl:
                        st_("objection", {"by": DEMO_P[3][0], "text": "জমির পশ্চিম সীমানা নিয়ে আমার আপত্তি আছে।"})
                if upto >= 5:
                    st_("close_notice")
                    if "obj" in fl:
                        st_("schedule_hearing", {"date": (date.today() + timedelta(days=3)).isoformat(), "time": "11:00", "place": ""})
                if upto >= 6:
                    if "obj" in fl:
                        st_("record_hearing", {"outcome": "আপত্তিকারী আপত্তি প্রত্যাহার করেছেন।", "resolved": True})
                    st_("review", {"decision": "approve", "authentic": True, "chain": True, "inspection_ok": True, "legal_ok": True, "remarks": "সব কাগজপত্র সঠিক।"})
                if upto >= 7:
                    st_("issue_order")
                if upto >= 8:
                    st_("mark_paid", {"receipt": "RC-%d" % rng.randint(100000, 999999)})
                if "hold" in fl:
                    st_("hold", {"reason": "প্রতিবেশীর লিখিত অভিযোগ যাচাইয়ের অপেক্ষায়"})
                    a["held_at"] = (datetime.now() - timedelta(days=2)).isoformat(timespec="seconds")
                if "return" in fl:
                    st_("return", {"reason": "পূর্বের খতিয়ানের কপি অস্পষ্ট — আবার আপলোড করুন"})
                if "reject" in fl:
                    st_("reject", {"reason": "দলিলের তথ্য ও রেকর্ডে গুরুতর অমিল"})
                if a["submitted"]:
                    shift(a, age, sage)
                made += 1
            for j, (kind, pi) in enumerate([("submitted", 0), ("submitted", 1), ("returned", 2), ("rejected", 1)]):
                d = mk_deed(len(DEMO_SPECS) + j, "sale", pi, 2 + j)[0]
                if kind == "returned":
                    deed_act(d, "return", {"reason": "সাক্ষীর তথ্য অসম্পূর্ণ — সংশোধন করে পুনরায় জমা দিন"})
                elif kind == "rejected":
                    deed_act(d, "reject", {"reason": "দলিলের তথ্য রেকর্ডের সাথে অমিল"})
            for n in db["notes"]:
                n["read"] = True
        finally:
            tl_.u = None
        db["seeded"] = True
        save()
        return made

    def boot():
        with lock:
            if not db["seeded"] and not db["apps"] and not db["deeds"]:
                seed_demo()

    @app.route("/api/admin/mut/seed", methods=["POST"])
    def mut_seed():
        with lock:
            if len(db["apps"]) >= 60:
                return err("enough demo data already / যথেষ্ট ডেমো ডেটা আছে", 409)
            n = seed_demo()
        return jsonify(ok=True, created=n) if n else err("no suitable plots — load an area first / উপযুক্ত দাগ নেই", 422)

    # ------------------------------------------------------------------ notifications
    @app.route("/api/mut/notifications")
    def mut_notes():
        to = "admin" if is_admin() else user().get("id")
        rows = [n for n in db["notes"] if n["to"] == to]
        return jsonify(notes=rows[-40:][::-1], unread=sum(1 for n in rows if not n["read"]))

    @app.route("/api/mut/notifications/read", methods=["POST"])
    def mut_notes_read():
        to = "admin" if is_admin() else user().get("id")
        nid = (request.get_json(silent=True) or {}).get("id")
        with lock:
            for n in db["notes"]:
                if n["to"] == to and (nid is None or n["id"] == nid):
                    n["read"] = True
            save()
        return jsonify(ok=True)

    # ------------------------------------------------------------------ re-point legacy endpoints to the single database
    old_stats, old_over = app.view_functions.get("api_stats"), app.view_functions.get("api_admin_overview")

    def stats_v2():
        j = old_stats().get_json()
        j["cases"] = len(db["apps"])
        j["sample_cases"] = [{"id": a["id"], "status": a["status"]} for a in list(db["apps"].values())[:6]]
        return jsonify(j)

    def overview_v2():
        j = old_over().get_json()
        apps = list(db["apps"].values())
        done = [a for a in apps if a["status"] == "completed" and a.get("submitted")]
        span = lambda a: (datetime.fromisoformat(a["timeline"][-1]["ts"]) - datetime.fromisoformat(a["submitted"])).days
        j["in_progress"] = sum(a["status"] in ("ok", "awaiting_payment") for a in apps)
        j["flagged"] = sum(a["status"] == "flagged" for a in apps)
        j["avg_days"] = round(sum(max(0, span(a)) for a in done) / len(done)) if done else 0
        buckets = [0] * 6
        for a in apps:
            if a["status"] != "rejected":
                buckets[{0: 0, 1: 1, 2: 2, 3: 3, 4: 3, 5: 4, 6: 4, 7: 5}[a["stage"]]] += 1
        j["by_stage"] = buckets
        return jsonify(j)

    if old_stats:
        app.view_functions["api_stats"] = stats_v2
    if old_over:
        app.view_functions["api_admin_overview"] = overview_v2

    return page.replace("</body>", "<style>" + MF_CSS + "</style>" + MF_HTML + "<script>" + MF_JS + "</script></body>")


# ======================================================================================= front-end
MF_CSS = r"""
.icon{position:relative}
.mf-dot{position:absolute;top:6px;right:7px;width:9px;height:9px;border-radius:50%;background:var(--bd);border:2px solid var(--sf)}
.mf-notes{width:min(380px,92vw)}.mf-n{padding:9px 10px;border-radius:10px;cursor:pointer;font-size:13.5px;line-height:1.55}.mf-n:hover{background:var(--sf2)}.mf-n.u{background:var(--gt)}.mf-n small{display:block;color:var(--mu)}
.tabs button[data-tab=workflow]{display:none}
.ck{flex-wrap:wrap}.ck .btn{margin-left:2px}.ck.waived i{background:var(--gt);color:var(--g)}
.mf-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:14px}
.mf-card{cursor:pointer;display:grid;gap:7px;transition:.15s;align-content:start}.mf-card:hover{transform:translateY(-2px);border-color:var(--g2)}
.mf-row{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}
.mf-t{font-weight:600}.mf-prog{height:6px;border-radius:4px;background:var(--ln);overflow:hidden}.mf-prog i{display:block;height:100%;background:linear-gradient(90deg,var(--g),var(--g2))}
.mf-info{display:flex;gap:14px;align-items:flex-start;background:var(--gt);border-color:var(--g2);margin-bottom:16px}.mf-info p{margin:4px 0 0;color:var(--mu);font-size:14.5px}
.mf-tb{margin-bottom:14px;display:grid;gap:12px}
.mf-search{position:relative;flex:1;min-width:200px}.mf-search svg{position:absolute;left:12px;top:50%;transform:translateY(-50%);color:var(--mu)}.mf-search input{padding-left:36px}
.mf-flt{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;align-items:end}
.mf-fl select{padding:8px 10px;font-size:13.5px}
.mf-sub{font-size:12.5px;color:var(--mu)}
.mf-pg{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin-top:14px}
th.srt{cursor:pointer;user-select:none}th.srt:hover{color:var(--g)}
.mf-eta{font-size:12.5px;display:flex;gap:5px;align-items:center;color:var(--mu)}
.mf-eta.delayed,.neg{color:var(--bd)!important}.mf-eta.at_risk,.mf-eta.paused{color:var(--wr)}.mf-eta.on_track{color:var(--g)}
p.mf-eta{font-size:14px;margin:10px 0 0}
.mf-etag{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin:12px 0}
.mf-etag div{background:var(--sf2);border-radius:12px;padding:10px 12px}.mf-etag b{display:block;font-size:20px}.mf-etag span{font-size:12px;color:var(--mu)}
.mf-stack{display:flex;height:12px;border-radius:6px;overflow:hidden;margin:8px 0 12px}.mf-stack i{display:block;height:100%}
.mf-map{height:250px;border-radius:14px;overflow:hidden;margin-bottom:10px;z-index:0}
.mf-reply{background:var(--sf2);border-radius:10px;padding:7px 10px;margin-top:6px;font-size:13.5px}
.mf-board .kcard{cursor:pointer}.mf-board .kcard[draggable=true]{cursor:grab}.mf-board .col.over{outline:2px dashed var(--g2)}
.mf-dlg{width:min(940px,96vw)}.mf-two{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:14px}
.mf-dtabs{margin:14px 0 0;overflow-x:auto;flex-wrap:nowrap}.mf-dtabs button{white-space:nowrap}
.mf-docrow{display:flex;gap:10px;align-items:center;padding:9px 0;border-bottom:1px dashed var(--ln);flex-wrap:wrap}.mf-docrow:last-child{border:0}
.mf-next{border-radius:14px;padding:12px 14px;background:var(--wrt);color:var(--wr);font-size:14.5px;margin:12px 0}.mf-next.g{background:var(--gt);color:var(--g)}.mf-next.r{background:var(--bdt);color:var(--bd)}
.mf-panel{border:1px solid var(--g2);border-radius:16px;padding:14px;margin-top:14px;background:color-mix(in srgb,var(--gt) 40%,var(--sf))}
.mf-panel h3{margin-bottom:10px}.mf-panel .fgrid{margin-bottom:10px}.mf-panel textarea{min-height:64px}
.mf-fee{display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px dashed var(--ln)}.mf-pay{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px;margin:12px 0}
.mf-pay label{border:1px solid var(--ln);border-radius:12px;padding:10px;display:flex;gap:8px;align-items:center;cursor:pointer;font-weight:600}.mf-pay label.on{border-color:var(--g);background:var(--gt)}
.mf-hint{background:var(--wrt);color:var(--wr);border-radius:12px;padding:9px 12px;font-size:13px;margin:10px 0}
.mf-chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
@media(max-width:760px){.mf-two{grid-template-columns:1fr}.mf-grid{grid-template-columns:1fr}.mfvl{display:none}.mf-board{grid-auto-columns:minmax(78vw,1fr)}}
@media print{
 .pd-copy{text-align:right;font-weight:700;border:1px solid #000;display:inline-block;padding:1px 10px;float:right;margin-top:4px}
 .pd-photo{width:30mm;height:36mm;border:1px solid #000;display:grid;place-items:center;font-size:8pt;overflow:hidden}.pd-photo img{width:100%;height:100%;object-fit:cover}
 .pd-sigimg{height:12mm;display:block;margin:0 auto 2px}
 .pd-sign.s3{grid-template-columns:repeat(3,1fr);gap:18px}
 .pd-two{display:grid;grid-template-columns:1fr auto;gap:14px;align-items:start}
 .pd-pg{break-after:page;height:0}
 .pd-stub{border-top:2px dashed #000;margin-top:14px;padding-top:5px;font-size:9pt;break-inside:avoid}.pd-stub .in{display:grid;grid-template-columns:1fr auto;gap:12px;align-items:center}.pd-stub svg{height:14mm;width:auto}
 .pd-title{text-align:center;font-size:15pt;font-weight:700;margin:8px 0 6px;text-decoration:underline}
 .pd-cl{margin:0 0 4px;padding-left:0;list-style:none}.pd-cl li{margin-bottom:5px;text-align:justify}
 .pd-seal{display:inline-block;border:2px double #000;border-radius:50%;padding:10px 14px;font-weight:700;font-size:9pt;text-align:center;transform:rotate(-8deg)}
}
"""

MF_HTML = r"""
<section class="view" id="v-mut" hidden><div class="container" id="mf_root"></div></section>
<dialog id="mfDlg" class="mf-dlg"></dialog>
"""

MF_JS = r"""
(function(){
if(window.__mf)return;window.__mf=1;
Object.assign(IC,{
bell:'<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/>',
download:'<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" x2="12" y1="15" y2="3"/>',
upload:'<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" x2="12" y1="3" y2="15"/>',
table:'<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M3 9h18"/><path d="M3 15h18"/><path d="M9 3v18"/>',
grid:'<rect width="7" height="7" x="3" y="3" rx="1"/><rect width="7" height="7" x="14" y="3" rx="1"/><rect width="7" height="7" x="14" y="14" rx="1"/><rect width="7" height="7" x="3" y="14" rx="1"/>',
kanban:'<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M8 7v7"/><path d="M12 7v4"/><path d="M16 7v9"/>',
wand:'<path d="m21.64 3.64-1.28-1.28a1.21 1.21 0 0 0-1.72 0L2.36 18.64a1.21 1.21 0 0 0 0 1.72l1.28 1.28a1.2 1.2 0 0 0 1.72 0L21.64 5.36a1.2 1.2 0 0 0 0-1.72"/><path d="m14 7 3 3"/><path d="M5 6v4"/><path d="M19 14v4"/><path d="M10 2v2"/><path d="M7 8H3"/><path d="M21 16h-4"/><path d="M11 3H9"/>',
send:'<path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/>',
card:'<rect width="20" height="14" x="2" y="5" rx="2"/><line x1="2" x2="22" y1="10" y2="10"/>',
unlock:'<rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 9.9-1"/>'});
const BNd='০১২৩৪৫৬৭৮৯';
const B=v=>String(v??'').replace(/\d/g,d=>BNd[d]);
const E=v=>B(esc(v));
const MN=['জানুয়ারি','ফেব্রুয়ারি','মার্চ','এপ্রিল','মে','জুন','জুলাই','আগস্ট','সেপ্টেম্বর','অক্টোবর','নভেম্বর','ডিসেম্বর'];
const bdate=s=>{if(!s)return '—';const d=new Date(String(s).length<=10?s+'T00:00:00':s);return isNaN(d)?'—':B(d.getDate())+' '+MN[d.getMonth()]+' '+B(d.getFullYear())};
const ftime=s=>{try{return new Date(s).toLocaleString(lang==='bn'?'bn-BD':'en-GB',{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'})}catch(e){return s}};
const W1='শূন্য,এক,দুই,তিন,চার,পাঁচ,ছয়,সাত,আট,নয়,দশ,এগারো,বারো,তেরো,চৌদ্দ,পনেরো,ষোলো,সতেরো,আঠারো,ঊনিশ,বিশ,একুশ,বাইশ,তেইশ,চব্বিশ,পঁচিশ,ছাব্বিশ,সাতাশ,আটাশ,ঊনত্রিশ,ত্রিশ,একত্রিশ,বত্রিশ,তেত্রিশ,চৌত্রিশ,পঁয়ত্রিশ,ছত্রিশ,সাঁইত্রিশ,আটত্রিশ,ঊনচল্লিশ,চল্লিশ,একচল্লিশ,বিয়াল্লিশ,তেতাল্লিশ,চুয়াল্লিশ,পঁয়তাল্লিশ,ছেচল্লিশ,সাতচল্লিশ,আটচল্লিশ,ঊনপঞ্চাশ,পঞ্চাশ,একান্ন,বাহান্ন,তিপ্পান্ন,চুয়ান্ন,পঞ্চান্ন,ছাপ্পান্ন,সাতান্ন,আটান্ন,ঊনষাট,ষাট,একষট্টি,বাষট্টি,তেষট্টি,চৌষট্টি,পঁয়ষট্টি,ছেষট্টি,সাতষট্টি,আটষট্টি,ঊনসত্তর,সত্তর,একাত্তর,বাহাত্তর,তিয়াত্তর,চুয়াত্তর,পঁচাত্তর,ছিয়াত্তর,সাতাত্তর,আটাত্তর,ঊনআশি,আশি,একাশি,বিরাশি,তিরাশি,চুরাশি,পঁচাশি,ছিয়াশি,সাতাশি,অষ্টাশি,ঊননব্বই,নব্বই,একানব্বই,বিরানব্বই,তিরানব্বই,চুরানব্বই,পঁচানব্বই,ছিয়ানব্বই,সাতানব্বই,আটানব্বই,নিরানব্বই'.split(',');
const bnWords=n=>{n=Math.floor(Number(n)||0);if(n===0)return W1[0];const p=[];const cr=Math.floor(n/1e7);n%=1e7;const lk=Math.floor(n/1e5);n%=1e5;const hz=Math.floor(n/1e3);n%=1e3;const sh=Math.floor(n/100);n%=100;
 if(cr)p.push((cr>99?bnWords(cr):W1[cr])+' কোটি');if(lk)p.push(W1[lk]+' লক্ষ');if(hz)p.push(W1[hz]+' হাজার');if(sh)p.push(W1[sh]+'শত');if(n)p.push(W1[n]);return p.join(' ')};

/* ---------- strings [bn,en] ---------- */
const MF={
ng:["দলিল ও নামজারি","Deed & mutation"],ng_desk:["দলিল ও নামজারি ডেস্ক","Deed & mutation desk"],
nav_deeds:["দলিল নিবন্ধন","Deed registration"],nav_apps:["নামজারি আবেদন","Mutation applications"],nav_trk:["ট্র্যাকিং","Tracking"],nav_ddeeds:["দলিল ডেস্ক","Deed desk"],nav_dapps:["নামজারি ডেস্ক","Mutation desk"],
title:["দলিল নিবন্ধন ও নামজারি","Deed registration & mutation"],sub:["অনলাইনে দলিল নিবন্ধনের আবেদন করুন, অনুমোদনের পর দলিল ডাউনলোড করুন, তারপর নামজারির আবেদন করে খতিয়ান পর্যন্ত প্রতিটি ধাপ অনুসরণ করুন।","Request deed registration online, download the approved deed, then apply for mutation and follow every step up to the khatian."],
admtitle:["দলিল ও নামজারি ডেস্ক (অ্যাডমিন)","Deed & mutation desk (admin)"],admsub:["দলিল যাচাই ও অনুমোদন, নামজারির প্রতিটি ধাপ প্রক্রিয়া এবং অফলাইন আবেদন গ্রহণ।","Verify and approve deeds, process every mutation step and take offline applications."],
tab_deeds:["দলিল নিবন্ধন","Deed registration"],tab_apps:["নামজারি আবেদন","Mutation applications"],tab_track:["ট্র্যাক করুন","Track"],
s0_t:["ধাপ ০: দলিল নিবন্ধন (নামজারির আগে)","Step 0: Deed registration (before mutation)"],s0_d:["জমি ক্রয়, দান বা উত্তরাধিকারসূত্রে প্রাপ্ত হলে বিক্রয় দলিল / দান দলিল / ওয়ারিশ বণ্টননামা নিবন্ধিত হয়। নিবন্ধিত দলিল ছাড়া নামজারি শুরু করা যায় না।","Land is bought, gifted or inherited; a sale deed, gift deed or inheritance deed is registered. Without a registered deed, mutation cannot start."],
new_deed:["নতুন দলিল আবেদন","New deed request"],new_app:["নতুন নামজারি আবেদন","New mutation application"],new_off:["অফলাইন আবেদন গ্রহণ","Offline intake"],
none:["কিছু নেই","Nothing here"],all:["সব","All"],need_deed:["আগে একটি নিবন্ধিত দলিল দরকার (ধাপ ০)","You need a registered deed first (Step 0)"],
ds_submitted:["যাচাই চলছে","Under review"],ds_approved:["নিবন্ধিত","Registered"],ds_returned:["সংশোধনের জন্য ফেরত","Returned"],ds_rejected:["প্রত্যাখ্যাত","Rejected"],
as_draft:["খসড়া","Draft"],as_ok:["চলমান","In progress"],as_flagged:["স্থগিত","On hold"],as_returned:["সংশোধনের জন্য ফেরত","Returned"],as_rejected:["প্রত্যাখ্যাত","Rejected"],as_awaiting_payment:["ফি পরিশোধের অপেক্ষায়","Awaiting payment"],as_completed:["সম্পন্ন","Completed"],
ty_sale:["বিক্রয় দলিল","Sale deed"],ty_gift:["দান (হেবা) দলিল","Gift deed"],ty_inheritance:["ওয়ারিশ বণ্টননামা","Inheritance deed"],
plot:["দাগ","Plot"],deed_no:["দলিল নং","Deed no."],online:["অনলাইন","Online"],offline:["অফলাইন","Offline"],
sp_deed:["দলিল নিবন্ধন","Deed registered"],sp_application:["আবেদন","Application"],sp_verification:["নথি যাচাই","Verification"],sp_inspection:["মাঠ তদন্ত","Field inspection"],sp_notice:["নোটিশ","Notice"],sp_hearing:["শুনানি ও এসি পর্যালোচনা","Hearing & AC review"],sp_order:["নামজারি আদেশ","Mutation order"],sp_payment:["চূড়ান্ত ফি","Final fee"],sp_khatian:["খতিয়ান ও ডিসিআর","Khatian & DCR"],
dsp_submitted:["আবেদন জমা","Submitted"],dsp_review:["সাব-রেজিস্ট্রার যাচাই","Registrar review"],dsp_registered:["নিবন্ধিত","Registered"],
dc_deed:["নিবন্ধিত দলিলের কপি","Registered deed copy"],dc_prev_khatian:["পূর্বের খতিয়ান","Previous khatian"],dc_ldt:["হালনাগাদ ভূমি উন্নয়ন কর রশিদ","Up-to-date land development tax receipt"],dc_nid:["আবেদনকারীর এনআইডি","Applicant's NID"],dc_photo:["পাসপোর্ট সাইজ ছবি","Passport-size photo"],dc_signature:["স্বাক্ষর","Signature"],dc_warish:["ওয়ারিশ সনদ","Warish certificate"],dc_decree:["আদালতের ডিক্রি","Court decree"],
auto_deed:["নিবন্ধিত দলিল — সিস্টেম থেকে স্বয়ংক্রিয়ভাবে সংযুক্ত","Registered deed — attached automatically from the system"],paper:["কাগজে জমা (অফিসে)","Paper copy received at office"],required:["প্রয়োজন","Required"],upload:["আপলোড","Upload"],replace:["বদলান","Replace"],paper_btn:["কাগজে পেয়েছি","Paper received"],toobig:["ফাইল ১.৫ এমবির কম হতে হবে","File must be under 1.5 MB"],
ck_docs_complete:["সব কাগজপত্র জমা","All documents submitted"],ck_deed_registered:["দলিল নিবন্ধিত","Deed registered"],ck_area_match:["জমির পরিমাণ রেকর্ডের মধ্যে","Area within record"],ck_not_khas:["খাস/অর্পিত নয়","Not khas / vested"],ck_no_acq:["অধিগ্রহণের আওতায় নয়","Not under acquisition"],ck_no_case:["চলমান মামলা নেই","No pending court case"],ck_no_fraud:["জালিয়াতির সংকেত নেই","No fraud signals"],ck_tax_paid:["ভূমি উন্নয়ন কর পরিশোধিত","Land development tax paid"],ck_nid_ok:["এনআইডি বৈধ","NID format valid"],ck_applicant_match:["আবেদনকারী = দলিলের গ্রহীতা","Applicant matches deed buyer"],ck_owner_chain:["বিক্রেতা = রেকর্ডীয় মালিক","Seller matches record owner"],
lv_waived:["অব্যাহতিপ্রাপ্ত","Waived"],
ev_created:["খসড়া তৈরি","Draft created"],ev_submitted:["জমা দেওয়া হয়েছে","Submitted"],ev_resubmitted:["সংশোধন করে পুনরায় জমা","Resubmitted"],ev_approved:["অনুমোদিত ও নিবন্ধিত","Approved & registered"],ev_returned:["সংশোধনের জন্য ফেরত","Returned for correction"],ev_rejected:["প্রত্যাখ্যাত","Rejected"],ev_held:["স্থগিত","Put on hold"],ev_released:["স্থগিতাদেশ প্রত্যাহার","Released"],ev_application:["আবেদন জমা","Application submitted"],ev_verification:["নথি যাচাই শুরু","Verification started"],ev_verified:["নথি যাচাই সম্পন্ন","Documents verified"],ev_inspection:["মাঠ তদন্ত শুরু","Field inspection started"],ev_inspected:["তদন্ত প্রতিবেদন প্রস্তুত","Inspection report filed"],ev_notice:["নোটিশ ধাপ শুরু","Notice stage started"],ev_notice_issued:["নোটিশ জারি","Notice issued"],ev_objection:["আপত্তি দাখিল","Objection filed"],ev_hearing:["শুনানি ও এসি পর্যালোচনা শুরু","Hearing & AC review started"],ev_hearing_scheduled:["শুনানি নির্ধারিত","Hearing scheduled"],ev_hearing_held:["শুনানি সম্পন্ন","Hearing held"],ev_reviewed:["এসি (ল্যান্ড) পর্যালোচনা সম্পন্ন","AC (Land) review done"],ev_order:["আদেশের অপেক্ষা","Awaiting order"],ev_order_issued:["নামজারি আদেশ জারি","Mutation order issued"],ev_payment:["ফি পরিশোধের অপেক্ষা","Awaiting payment"],ev_paid:["ফি পরিশোধিত","Fee paid"],ev_khatian:["খতিয়ান ও ডিসিআর প্রস্তুত","Khatian & DCR issued"],
ev_waived:["যাচাই থেকে অব্যাহতি","Check waived"],ev_unwaived:["অব্যাহতি প্রত্যাহার","Waiver revoked"],ev_special_notice:["বিশেষ নোটিশ জারি","Special notice issued"],ev_special_closed:["বিশেষ নোটিশ নিষ্পন্ন","Special notice closed"],ev_replied:["নোটিশের উত্তর","Notice reply"],
nx_draft:["কাগজপত্র আপলোড করে আবেদন জমা দিন।","Upload the documents and submit your application."],nx_1:["নথি ও সরকারি রেকর্ড যাচাই চলছে।","Your documents and government records are being verified."],nx_2:["ভূমি অফিসের কর্মকর্তা মাঠ পর্যায়ে তদন্ত করবেন।","A land-office officer will inspect the land."],nx_3:["পূর্বের মালিক ও প্রতিবেশীদের নোটিশ দেওয়া হচ্ছে; আপত্তির সময়সীমা চলছে।","Notice goes to the previous owner and neighbours; the objection period is running."],nx_4:["এসি (ল্যান্ড) ফাইল পর্যালোচনা করছেন (আপত্তি থাকলে শুনানি হবে)।","The AC (Land) is reviewing the file (hearing if objections exist)."],nx_5:["নামজারি আদেশ জারির অপেক্ষায়।","Waiting for the mutation order."],nx_6:["আদেশ হয়েছে — অনলাইনে অবশিষ্ট ফি পরিশোধ করুন।","Order issued — pay the remaining fee online."],nx_7:["সম্পন্ন! খতিয়ান ও ডিসিআর ডাউনলোড করুন।","Done! Download your khatian and DCR."],
on_hold:["স্থগিত — কারণ","On hold — reason"],wait_app:["আবেদনকারীর সংশোধনের অপেক্ষায়।","Waiting for the applicant."],ret_reason:["ফেরতের কারণ","Reason returned"],rej_reason:["প্রত্যাখ্যানের কারণ","Reason rejected"],
f_type:["দলিলের ধরন","Deed type"],f_ds:["ডেটাসেট / মৌজা","Dataset / mouza"],f_plot:["দাগ নম্বর","Plot no."],f_area:["হস্তান্তরিত জমি (শতাংশ)","Area transferred (decimal)"],f_value:["মূল্য (টাকা)","Consideration (BDT)"],f_date:["দলিলের তারিখ","Deed date"],f_name:["নাম","Name"],f_father:["পিতা/স্বামীর নাম","Father / spouse"],f_nid:["এনআইডি নম্বর","NID no."],f_addr:["ঠিকানা","Address"],f_w1:["সাক্ষী ১","Witness 1"],f_w2:["সাক্ষী ২","Witness 2"],f_n:["উত্তরে","North"],f_s:["দক্ষিণে","South"],f_e:["পূর্বে","East"],f_w:["পশ্চিমে","West"],f_bd:["চৌহদ্দি (ঐচ্ছিক)","Boundaries (optional)"],f_note:["মন্তব্য","Remarks"],f_mobile:["মোবাইল","Mobile"],f_email:["ইমেইল","Email"],f_court:["আদালতের আদেশে নামজারি","Court-ordered mutation"],f_pk:["পূর্বের খতিয়ান নং","Previous khatian no."],f_ldt:["ভূমি উন্নয়ন কর রশিদ নং","Land development tax receipt no."],f_ldty:["কর পরিশোধিত সন","Tax paid up to (year)"],f_deed:["নিবন্ধিত দলিল","Registered deed"],f_ext:["সিস্টেমের বাইরের (কাগজের) দলিল","External (paper) deed"],f_dno:["দলিল নং","Deed no."],f_seller_n:["বিক্রেতা/দাতার নাম","Seller / donor name"],
sec_land:["ভূমির বিবরণ","Land details"],sec_seller:["পক্ষ ১","Party 1"],sec_buyer:["পক্ষ ২","Party 2"],sec_wit:["সাক্ষী","Witnesses"],sec_app:["আবেদনকারী","Applicant"],sec_docs:["কাগজপত্র","Documents"],sec_checks:["স্বয়ংক্রিয় যাচাই","Automated checks"],sec_pre:["নিবন্ধনের আগে যাচাই","Registry pre-checks"],sec_tl:["ঘটনাপ্রবাহ","Timeline"],sec_fee:["ফি","Fees"],
land_info:["জেলা","District"],land_mouza:["মৌজা","Mouza"],land_khatian:["খতিয়ান","Khatian"],land_area:["দাগের আয়তন","Plot area"],dec:["শতাংশ","decimal"],
save:["জমা দিন","Submit"],resub:["সংশোধন করে জমা","Resubmit"],edit:["সংশোধন","Edit"],cancel:["বাতিল","Cancel"],create:["তৈরি করুন","Create"],open:["খুলুন","Open"],
dl_deed:["নিবন্ধিত দলিল ডাউনলোড (PDF)","Download registered deed (PDF)"],start_mut:["নামজারির আবেদন করুন","Apply for mutation"],view_app:["আবেদন দেখুন","View application"],
dl_app_u:["আবেদনকারীর কপি","Applicant copy"],dl_app_o:["অফিস কপি","Office copy"],dl_insp:["তদন্ত প্রতিবেদন","Inspection report"],dl_notice:["নোটিশ","Notice"],dl_order:["নামজারি আদেশ","Mutation order"],dl_khatian:["নামজারি খতিয়ান ও ডিসিআর ডাউনলোড","Download mutation khatian & DCR"],
submit_app:["আবেদন জমা দিন","Submit application"],pay_now:["ফি পরিশোধ করুন","Pay fee"],pay_demo:["ডেমো পেমেন্ট — প্রকৃত অর্থ কাটা হবে না","Demo payment — no real money is charged"],pay_ok:["পরিশোধ করুন","Pay"],paid:["পরিশোধিত","Paid"],due:["বাকি","Due"],total:["মোট","Total"],
fee_application:["আবেদন ফি","Application fee"],fee_notice:["নোটিশ ও ডাক খরচ","Notice & postage"],fee_mutation:["নামজারি ফি","Mutation fee"],fee_dcr:["ডিসিআর ফি","DCR fee"],
pm_bkash:["বিকাশ","bKash"],pm_nagad:["নগদ","Nagad"],pm_rocket:["রকেট","Rocket"],pm_card:["কার্ড","Card"],pm_bank:["ব্যাংক","Bank"],
pay_mobile:["ওয়ালেট মোবাইল নম্বর","Wallet mobile number"],pay_pin:["পিন","PIN"],pay_otp:["ওটিপি (ডেমো: ১২৩৪৫৬)","OTP (demo: 123456)"],pay_card:["কার্ড নম্বর","Card number"],pay_name:["কার্ডধারীর নাম","Card holder"],pay_exp:["মেয়াদ (MM/YY)","Expiry (MM/YY)"],pay_cvv:["সিভিভি","CVV"],pay_bank:["ব্যাংকের নাম","Bank name"],pay_acc:["অ্যাকাউন্ট নম্বর","Account number"],pay_holder:["অ্যাকাউন্টধারীর নাম","Account holder"],
pay_fill:["ডেমো তথ্য দিয়ে পূরণ","Fill with demo data"],pay_processing:["নিরাপদ পেমেন্ট প্রক্রিয়াধীন…","Processing secure payment…"],pay_success:["পেমেন্ট সফল","Payment successful"],pay_receipt:["লেনদেন নং","Transaction"],pay_acct:["অ্যাকাউন্ট","Account"],
track_ph:["আইডি, দলিল নং বা নোটিশ নং লিখুন","Enter ID, deed no. or notice no."],track_btn:["ট্র্যাক","Track"],nf:["পাওয়া যায়নি","Not found"],
reason_ph:["কারণ / মন্তব্য (স্থগিত, ফেরত, প্রত্যাখ্যান, অব্যাহতিতে বাধ্যতামূলক)","Reason / remark (required for hold, return, reject, waive)"],need_reason:["কারণ লিখুন","Enter a reason"],done:["সম্পন্ন","Done"],
a_hold:["স্থগিত","Hold"],a_release:["ছাড় দিন","Release"],a_return:["সংশোধনে ফেরত","Return"],a_reject:["প্রত্যাখ্যান","Reject"],a_approve:["অনুমোদন ও নিবন্ধন","Approve & register"],
ap_verify_t:["ধাপ ১: নথি ও রেকর্ড যাচাই","Step 1: Verify documents & records"],ap_verify:["যাচাই সম্পন্ন — তদন্তে পাঠান","Verified — send for inspection"],ap_note:["যাচাই মন্তব্য","Verification note"],
ap_insp_t:["ধাপ ২: মাঠ তদন্ত (কানুনগো/সার্ভেয়ার)","Step 2: Field inspection (Kanungo / surveyor)"],ap_insp:["তদন্তকারীর নাম","Inspector name"],ap_idate:["তদন্তের তারিখ","Inspection date"],ap_poss:["দখলের অবস্থা","Possession"],po_full:["সম্পূর্ণ দখলে","In full possession"],po_partial:["আংশিক দখলে","Partial possession"],po_none:["দখলে নেই","Not in possession"],po_disputed:["বিরোধপূর্ণ","Disputed"],ap_found:["মাঠে প্রাপ্ত পরিমাণ (শতাংশ)","Area found on site (decimal)"],ap_nb:["প্রতিবেশী জমির মালিক (প্রতি লাইনে একজন)","Neighbouring owners (one per line)"],ap_rem:["তদন্ত মন্তব্য","Inspection remarks"],ap_insp_go:["প্রতিবেদন জমা দিন","File inspection report"],
ap_not_t:["ধাপ ৩: সংশ্লিষ্ট পক্ষদের নোটিশ","Step 3: Notice to interested parties"],ap_days:["আপত্তির সময় (দিন)","Objection period (days)"],ap_extra:["অতিরিক্ত প্রাপক (প্রতি লাইনে একজন)","Extra recipients (one per line)"],ap_issue:["নোটিশ জারি করুন","Issue notice"],ap_obj_by:["আপত্তিকারী","Objector"],ap_obj_txt:["আপত্তির বিবরণ","Objection statement"],ap_obj_add:["আপত্তি নথিভুক্ত","Record objection"],ap_close:["সময়সীমা শেষ — এসি পর্যালোচনায় পাঠান","Close period — send to AC review"],
ap_hear_t:["ধাপ ৪: শুনানি ও এসি (ল্যান্ড) পর্যালোচনা","Step 4: Hearing & AC (Land) review"],ap_hd:["শুনানির তারিখ","Hearing date"],ap_ht:["সময়","Time"],ap_hp:["স্থান","Venue"],ap_hsched:["শুনানি নির্ধারণ","Schedule hearing"],ap_ho:["শুনানির ফলাফল","Hearing outcome"],ap_hr:["আপত্তি নিষ্পত্তি হয়েছে","Objections resolved"],ap_hrec:["শুনানি নথিভুক্ত","Record hearing"],
rv1:["দলিলের সত্যতা যাচাই করা হয়েছে","Deed authenticity verified"],rv2:["মালিকানার ধারাবাহিকতা (ওনারশিপ চেইন) ঠিক আছে","Ownership chain is in order"],rv3:["তদন্ত প্রতিবেদন সন্তোষজনক","Inspection report is satisfactory"],rv4:["কোনো আইনি জটিলতা নেই","No legal issues"],ap_rr:["পর্যালোচনা মন্তব্য","Review remarks"],ap_rapp:["অনুমোদন — আদেশের জন্য","Approve — proceed to order"],ap_rrej:["প্রত্যাখ্যান","Reject"],
ap_ord_t:["ধাপ ৫: নামজারি আদেশ","Step 5: Mutation order"],ap_ord:["নামজারি আদেশ জারি করুন","Issue mutation order"],ap_pay_t:["ধাপ ৬: চূড়ান্ত ফি","Step 6: Final fee"],ap_rc:["অফিস রশিদ নং","Office receipt no."],ap_markpaid:["অফিসে পরিশোধিত — খতিয়ান জারি করুন","Paid at office — issue khatian"],ap_pay_wait:["আবেদনকারীর অনলাইন পরিশোধের অপেক্ষা, অথবা অফিসে নগদ গ্রহণ করে নিচে নিশ্চিত করুন।","Waiting for the applicant to pay online, or confirm an office payment below."],
r_insp:["তদন্ত প্রতিবেদন","Inspection report"],r_notice:["নোটিশ","Notice"],r_hear:["শুনানি","Hearing"],r_order:["আদেশ","Order"],r_pay:["পরিশোধ","Payment"],r_kh:["খতিয়ান","Khatian"],
k_insp:["তদন্তকারী","Inspector"],k_date:["তারিখ","Date"],k_possession:["দখল","Possession"],k_area:["আয়তন (দাবি / প্রাপ্ত)","Area (claimed / found)"],k_match:["মিলেছে","Matches"],k_nomatch:["মেলেনি","Does not match"],k_neigh:["প্রতিবেশী","Neighbours"],k_deadline:["আপত্তির শেষ তারিখ","Objection deadline"],k_recip:["প্রাপক","Recipients"],k_obj:["আপত্তি","Objections"],k_order_no:["আদেশ নং","Order no."],k_kh:["খতিয়ান নং","Khatian no."],k_dcr:["ডিসিআর নং","DCR no."],k_txn:["লেনদেন নং","Transaction"],
pr_owner:["পূর্বের মালিক","Previous owner"],pr_neighbor:["প্রতিবেশী","Neighbour"],pr_other:["অন্যান্য","Other"],
bell:["বিজ্ঞপ্তি","Notifications"],markall:["সব পড়া হয়েছে","Mark all read"],nonotes:["কোনো বিজ্ঞপ্তি নেই","No notifications"],
kpi_pd:["যাচাইাধীন দলিল","Deeds to review"],kpi_act:["কর্মের অপেক্ষায় আবেদন","Applications to act on"],kpi_pay:["ফি অপেক্ষায়","Awaiting payment"],kpi_done:["সম্পন্ন","Completed"],kpi_delay:["বিলম্বিত","Delayed"],
/* views, search, filters */
vw_table:["টেবিল","Table"],vw_card:["কার্ড","Cards"],vw_board:["বোর্ড","Board"],search_ph:["খুঁজুন: আইডি, নাম, দাগ, মৌজা, দলিল নং…","Search ID, name, plot, mouza, deed no…"],sort_by:["সাজান","Sort by"],per_page:["প্রতি পৃষ্ঠায়","Per page"],reset:["রিসেট","Reset"],res_n:["ফলাফল","results"],
c_id:["আইডি","ID"],c_type:["ধরন","Type"],c_land:["ভূমি","Land"],c_parties:["পক্ষসমূহ","Parties"],c_area:["পরিমাণ","Area"],c_status:["অবস্থা","Status"],c_created:["তারিখ","Date"],c_applicant:["আবেদনকারী","Applicant"],c_basis:["ভিত্তি","Basis"],c_stage:["ধাপ","Stage"],c_eta:["আনুমানিক সময়","Estimate"],c_district:["জেলা","District"],c_channel:["মাধ্যম","Channel"],c_delay:["সময়সূচি","Schedule"],
dl_on_track:["সময়মতো","On track"],dl_at_risk:["ঝুঁকিতে","At risk"],dl_delayed:["বিলম্বিত","Delayed"],dl_paused:["স্থগিত","Paused"],
bd_draft:["খসড়া / ফেরত","Draft / returned"],bd_closed:["প্রত্যাখ্যাত","Rejected"],bd_hint:["কার্ড টেনে পরবর্তী ধাপের কলামে ছাড়ুন — সংশ্লিষ্ট ধাপের প্যানেল খুলবে।","Drag a card to the next column — the matching step panel opens."],bd_next:["ধাপটি সম্পন্ন করে এগিয়ে নিন","Complete this step to move the case forward"],bd_only_next:["শুধু পরবর্তী ধাপে নেওয়া যায়","Only the next stage is allowed"],
/* detail */
dt_ov:["সারসংক্ষেপ","Overview"],dt_land:["ভূমি ও মানচিত্র","Land & map"],dt_docs:["কাগজপত্র","Documents"],dt_ver:["যাচাই প্রতিবেদন","Verification"],dt_tl:["ঘটনাপ্রবাহ","Timeline"],
lt_class:["ভূমির সম্পূর্ণ শ্রেণিবিন্যাস","Full land classification"],lt_use:["ব্যবহার","Use"],lt_share:["অংশ","Share"],lt_total:["মোট","Total"],lt_map:["অবস্থান","Location"],lt_risk:["রেজিস্ট্রি ঝুঁকি","Registry risk"],lt_viewmap:["মানচিত্রে দেখুন","View on map"],lt_gm:["গুগল ম্যাপ","Google Maps"],lt_cls:["শ্রেণি","Class"],lt_flags:["জালিয়াতি সতর্কতা","Fraud flags"],lt_pend:["চলমান মামলা","Pending cases"],lt_noparts:["শ্রেণির তথ্য নেই","No classification data"],
eta_t:["আনুমানিক সময় ও সময়সূচি","Estimated time & schedule"],eta_rem:["আনুমানিক বাকি (দিন)","Est. days left"],eta_est:["সম্ভাব্য সমাপ্তি","Estimated finish"],eta_sla:["সেবার সময়সীমা (দিন)","Deadline (days)"],eta_step:["বর্তমান ধাপ (কাটানো / বরাদ্দ)","This step (spent / planned)"],eta_done:["মোট সময় লেগেছে","Completed in"],eta_days:["দিন","days"],
vr_title:["পূর্ণ যাচাই প্রতিবেদন","Full verification report"],vr_sum:["সারসংক্ষেপ","Summary"],vr_waive:["অব্যাহতি","Waive"],vr_unwaive:["প্রত্যাহার","Revoke"],vr_notice:["নোটিশ","Notice"],vr_print:["প্রতিবেদন প্রিন্ট","Print report"],vr_registry:["রেজিস্ট্রি ঝুঁকি বিশ্লেষণ","Registry risk analysis"],vr_chain:["মালিকানার ধারা","Ownership chain"],vr_advice:["পরামর্শ","Advice"],vr_hint:["কারণ লিখে যাচাইয়ের পাশের ‘অব্যাহতি’ বোতাম চাপুন; সব ব্যর্থ যাচাই অব্যাহতিপ্রাপ্ত হলে ‘যাচাই সম্পন্ন’ চাপুন।","Enter a reason, press Waive next to a check, then press ‘Verified’ once every failed check is waived."],vr_reason_ph:["অব্যাহতির কারণ / মন্তব্য","Reason / remark for the waiver"],vr_proceed:["যাচাই সম্পন্ন — তদন্তে পাঠান","Verified — send for inspection"],vr_waive_first:["আগে সব ব্যর্থ যাচাই অব্যাহতি দিন","Waive every failed check first"],
sn_t:["বিশেষ নোটিশ","Special notices"],sn_new:["বিশেষ নোটিশ পাঠান","Send special notice"],sn_to:["প্রাপক","Recipient"],sn_applicant:["আবেদনকারী","Applicant"],sn_previous_owner:["পূর্বের মালিক","Previous owner"],sn_neighbors:["প্রতিবেশী","Neighbours"],sn_other:["অন্য কেউ","Other"],sn_other_n:["প্রাপকের নাম","Recipient name"],sn_sub:["বিষয়","Subject"],sn_msg:["বক্তব্য","Message"],sn_days:["সাড়া দেওয়ার সময় (দিন)","Days to respond"],sn_send:["নোটিশ পাঠান","Send notice"],sn_open:["চলমান","Open"],sn_closed:["নিষ্পন্ন","Closed"],sn_deadline:["শেষ তারিখ","Deadline"],sn_reply:["উত্তর দিন","Reply"],sn_reply_ph:["আপনার উত্তর…","Your reply…"],sn_close:["নিষ্পন্ন করুন","Close"],sn_sent:["নোটিশ পাঠানো হয়েছে","Notice sent"],
demo_fill:["ডেমো তথ্য","Demo data"],demo_docs:["ডেমো কাগজ সংযুক্ত করুন","Attach demo documents"],demo_load:["ডেমো কেস লোড করুন","Load demo cases"],demo_chip:["ডেমো ট্র্যাকিং লিংক — এক ক্লিকে দেখুন","Demo tracking links — one click"],demo_hint:["ফর্মের ঘরে টাইপ করলে ডেমো তথ্যের পরামর্শ আসবে; নাম বেছে নিলে বাকি ঘর নিজে পূরণ হবে।","Typing in a field suggests demo values; picking a name fills the other fields."],demo_loaded:["ডেমো কেস যোগ হয়েছে","Demo cases added"],
tk_by:["আইডি, দলিল নং, নোটিশ নং বা ডিসিআর নং দিয়ে খুঁজুন","Search by ID, deed no., notice no. or DCR no."]
};
const m=k=>(MF[k]||[k,k])[lang==='bn'?0:1];
const PL={sale:['বিক্রেতা','ক্রেতা'],gift:['দাতা','গ্রহীতা'],inheritance:['মৃত মালিক','ওয়ারিশ/গ্রহীতা']};
const PLE={sale:['Seller','Buyer'],gift:['Donor','Donee'],inheritance:['Deceased owner','Heir']};
const pl=(ty,i)=>(lang==='bn'?PL:PLE)[ty][i];
const DW={sale:'সাফ কবলা দলিল',gift:'হেবা (দান) দলিল',inheritance:'ওয়ারিশ বণ্টননামা দলিল'};
const STEPS=['deed','application','verification','inspection','notice','hearing','order','payment','khatian'];
const FEEN={application:'আবেদন ফি',notice:'নোটিশ ও ডাক খরচ',mutation:'নামজারি ফি',dcr:'ডিসিআর ফি'};
const J=(u,b,method)=>api(u,{method:method||(b?'POST':'GET'),headers:{'Content-Type':'application/json'},body:b?JSON.stringify(b):undefined});
const $v=id=>{const e=document.getElementById(id);return e?String(e.value||'').trim():''};
const $c=id=>{const e=document.getElementById(id);return !!(e&&e.checked)};
const LSg=(k,d)=>{try{const v=localStorage.getItem('mf_'+k);return v===null?d:JSON.parse(v)}catch(e){return d}};
const LSs=(k,v)=>{try{localStorage.setItem('mf_'+k,JSON.stringify(v))}catch(e){}};
const bc=s=>({returned:'warn',awaiting_payment:'warn',submitted:'warn',draft:'',approved:'ok',ok:'ok'})[s]??s;
const DB={on_track:'ok',at_risk:'warn',delayed:'fail',paused:'warn'};
const dlg=()=>document.getElementById('mfDlg');
const mask=n=>n?('•'.repeat(Math.max(0,String(n).length-4))+String(n).slice(-4)):'—';
const clsBn=c=>(T['c_'+key(c)]||[c||'—'])[0];
const dc=v=>Number(v).toFixed(2);
const todayIso=()=>new Date().toISOString().slice(0,10);
const rnd=a=>a[Math.floor(Math.random()*a.length)];
const norm=s=>String(s??'').replace(/[০-৯]/g,d=>BNd.indexOf(d)).toLowerCase();
const lvl=l=>T['lv_'+l]?t('lv_'+l):m('lv_'+l);

/* ---------- demo people + suggestion lists ---------- */
const DP=[['করিম উদ্দিন','আব্দুল জব্বার','1985123456789','বাড়ি ১২, কাউলতিয়া, গাজীপুর সদর','01711223344'],['সুমাইয়া আক্তার','মো. হারুন অর রশিদ','1992456789012','বাসা ৭, মুক্তিযোদ্ধা সড়ক, সাভার','01819334455'],['তানভীর হাসান','আবুল কালাম','1989765432109','গ্রাম: দক্ষিণ চর, কুমিল্লা সদর','01911445566'],['নাজমুল হক','মোশারফ হোসেন','1980234567890','মহল্লা: পূর্ব বাজার, গাজীপুর','01611556677'],['রুবিনা ইয়াসমিন','আনোয়ার হোসেন','1995345678901','গ্রাম: বড়বাড়ী, সাভার','01511667788'],['মাহমুদুল হাসান','নূর মোহাম্মদ','1987567890123','ওয়ার্ড ৪, কুমিল্লা সদর','01311778899']];
const BANKS=['Dutch-Bangla Bank','Islami Bank Bangladesh','Sonali Bank','BRAC Bank','City Bank'];
(function(){const o=(id,a)=>`<datalist id="${id}">${a.map(v=>`<option value="${esc(v)}">`).join('')}</datalist>`,c=i=>DP.map(p=>p[i]);
 const h=document.createElement('div');h.hidden=true;
 h.innerHTML=o('dl_n',c(0))+o('dl_f',c(1))+o('dl_i',c(2))+o('dl_a',c(3))+o('dl_m',c(4))+o('dl_bank',BANKS)+o('dl_ins',['মো. শফিকুল ইসলাম (কানুনগো)','মোছা. নাসরিন সুলতানা (সার্ভেয়ার)','আব্দুল করিম (ইউনিয়ন ভূমি সহকারী)'])+o('dl_pk',['1204','2231','845','3017'])+o('dl_ldt',['LDT-48213','LDT-90177','LDT-55302'])+o('dl_dno',['1432/'+new Date().getFullYear(),'2087/'+new Date().getFullYear()])+o('dl_hp',['এসি (ল্যান্ড) অফিস','উপজেলা ভূমি অফিস']);
 document.body.appendChild(h)})();
const LST={n:'dl_n',f:'dl_f',i:'dl_i',a:'dl_a',m:'dl_m'};
function bindPerson(p){const n=document.getElementById(p+'n');if(!n)return;n.addEventListener('change',()=>{const x=DP.find(r=>r[0]===n.value.trim());if(!x)return;[['f',1],['i',2],['a',3],['m',4]].forEach(([s,i])=>{const e=document.getElementById(p+s);if(e&&!e.value)e.value=x[i]})})}
const setv=(id,v)=>{const e=document.getElementById(id);if(e)e.value=v};

/* ---------- state ---------- */
const ST={tab:'deeds',deeds:[],apps:[],admin:false};
const VS={deeds:{q:'',f:{},sort:'created',dir:-1,page:1,size:LSg('size_deeds',10),view:LSg('view_deeds','card')},apps:{q:'',f:{},sort:'created',dir:-1,page:1,size:LSg('size_apps',10),view:LSg('view_apps','card')}};
const DT={id:null,tab:'ov'};let LM=null;
function show(html){const d=dlg();d.innerHTML=html;if(!d.open)d.showModal();const x=d.querySelector('[data-x]');if(x)x.onclick=()=>d.close()}
function askReason(title,val){return new Promise(res=>{const d=document.createElement('dialog');d.style.width='min(460px,94vw)';document.body.appendChild(d);
  const close=v=>{try{d.close()}catch(e){}if(d.isConnected)d.remove();res(v)};
  d.innerHTML=`<div class="dhead"><div><h2 style="font-size:19px">${title}</h2></div><button class="icon" data-x aria-label="${t('close')}">${ico('x')}</button></div>
   <label class="f"><span>${m('vr_reason_ph')}</span><textarea id="mfAskTxt" rows="3" maxlength="300">${esc(val||'')}</textarea></label>
   <div class="btnrow" style="margin-top:14px;justify-content:flex-end"><button class="btn alt" data-cancel>${m('cancel')}</button><button class="btn" data-ok>${ico('check',15)} ${m('vr_waive')}</button></div>`;
  const txt=d.querySelector('#mfAskTxt'),ok=d.querySelector('[data-ok]');
  d.querySelector('[data-x]').onclick=()=>close('');d.querySelector('[data-cancel]').onclick=()=>close('');
  ok.onclick=()=>{const v=txt.value.trim();if(!v){toast(m('need_reason'));txt.focus();return}close(v)};
  txt.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();ok.click()}});
  d.addEventListener('cancel',e=>{e.preventDefault();close('')});
  d.showModal();setTimeout(()=>txt.focus(),30)})}
dlg().addEventListener('close',()=>{if(LM){LM.remove();LM=null}});
const head=(ti,su)=>`<div class="dhead"><div><h2 style="font-size:20px">${ti}</h2>${su?`<p style="margin:2px 0 0;color:var(--mu);font-size:13.5px">${su}</p>`:''}</div><button class="icon" data-x aria-label="${t('close')}">${ico('x')}</button></div>`;
const kv=rows=>`<dl class="kv">${rows.filter(r=>r[1]!==undefined&&r[1]!=='').map(([a,b])=>`<dt>${a}</dt><dd>${b}</dd>`).join('')}</dl>`;
const lvIc={pass:ico('check',14),warn:ico('alert',14),fail:ico('x',14),queued:ico('clock',14),waived:ico('okc',14)};
const chkHTML=(cs,adm)=>cs.map(([k,l,info])=>`<div class="ck ${l}"><i>${lvIc[l]}</i><span>${m('ck_'+k)}${info?`<small class="mf-sub" style="display:block">${esc(info)}</small>`:''}</span>${adm&&(l==='fail'||l==='warn')?`<button class="btn sm alt" data-waive="${k}">${ico('unlock',13)} ${m('vr_waive')}</button><button class="btn sm alt" data-snk="${k}">${ico('send',13)} ${m('vr_notice')}</button>`:''}${adm&&l==='waived'?`<button class="btn sm alt" data-unwaive="${k}">${m('vr_unwaive')}</button>`:''}<em>${lvl(l)}</em></div>`).join('');
const tlHTML=tl=>`<ul class="tl">${tl.map(e=>`<li><b>${m('ev_'+e.key)}</b><small>${ftime(e.ts)}${e.by?' · '+esc(nm(e.by)):''}</small>${e.note?`<small>${esc(e.note)}</small>`:''}</li>`).join('')}</ul>`;
const landKV=(l,extra=[])=>kv([[m('land_info'),esc(nm(l.district))],[m('land_mouza'),X(l.mouza)+' ('+X(l.m_code)+')'],['JL',X(l.jl||'—')],[m('plot'),X(l.label)],[m('land_khatian'),X(l.khatian)],[m('land_area'),nf(dec(l.area_sqm))+' '+t('u_dec')],...extra]);
const partyKV=p=>kv([[m('f_name'),esc(p.name)],[m('f_father'),esc(p.father||'—')],[m('f_nid'),X(mask(p.nid))],[m('f_addr'),esc(p.address||'—')]]);
function dStepper(d){const i=d.status==='approved'?3:1,bad=['returned','rejected'].includes(d.status);return `<div class="stepper">${['submitted','review','registered'].map((s,k)=>`<div class="${k<i?'done':k===i?'cur'+(bad?' flagged':''):''}">${m('dsp_'+s)}</div>`).join('')}</div>`}
function aStepper(a){const cur=a.stage+1,done=a.status==='completed',bad=['flagged','rejected','returned'].includes(a.status);return `<div class="stepper">${STEPS.map((s,i)=>`<div class="${i<cur||done?'done':i===cur?'cur'+(bad?' flagged':''):''}">${m('sp_'+STEPS[i])}</div>`).join('')}</div>`}
const tabsBar=(list,cur)=>`<div class="tabs mf-dtabs">${list.map(([k,l])=>`<button data-dtab="${k}" class="${k===cur?'on':''}">${l}</button>`).join('')}</div>`;
const pane=(k,cur,html)=>`<div data-pane="${k}" ${k===cur?'':'hidden'}>${html}</div>`;
function wireTabs(rec){const D=dlg();const go=k=>{D.querySelectorAll('[data-dtab]').forEach(b=>b.classList.toggle('on',b.dataset.dtab===k));D.querySelectorAll('[data-pane]').forEach(p=>p.hidden=p.dataset.pane!==k);DT.tab=k;if(k==='land')setTimeout(()=>initLandMap(rec),40)};
  D.querySelectorAll('[data-dtab]').forEach(b=>b.onclick=()=>go(b.dataset.dtab));if(DT.tab==='land')go('land')}
const viewMap=l=>{dlg().close();location.hash='#/map?ds='+encodeURIComponent(l.ds)+'&plot='+encodeURIComponent(l.plot_no)};

/* ---------- ETA / delay ---------- */
const dState=a=>['completed','rejected','draft','returned'].includes(a.status)?'':a.status==='flagged'?'paused':(a.eta?a.eta.state:'');
function etaTxt(a){const bn=lang==='bn',e=a.eta;
 if(a.status==='completed')return bn?'সম্পন্ন':'Completed';if(a.status==='rejected')return bn?'প্রত্যাখ্যাত':'Rejected';
 if(['draft','returned'].includes(a.status))return bn?'আবেদনকারীর অপেক্ষায়':'Waiting for the applicant';if(!e)return '—';
 if(a.status==='flagged')return bn?'স্থগিত — সময়ের ঘড়ি বন্ধ':'On hold — clock paused';
 if(a.sla_left<0)return bn?`সময়সীমা ${D(-a.sla_left)} দিন অতিক্রান্ত`:`${-a.sla_left} day(s) past the deadline`;
 if(e.overdue>0)return bn?`ধাপটি ${D(e.overdue)} দিন পিছিয়ে — আরও ~${D(e.remaining)} দিন লাগবে`:`This step is ${e.overdue} day(s) behind — ~${e.remaining} more day(s)`;
 return bn?`আনুমানিক ${D(e.remaining)} দিন বাকি · ${fd(e.end)}`:`~${e.remaining} day(s) left · by ${fd(e.end)}`}
function etaCard(a){
 if(a.status==='completed'){const s=a.submitted?Math.max(0,Math.round((new Date(a.timeline[a.timeline.length-1].ts)-new Date(a.submitted))/864e5)):0;return `<div class="card"><h3>${ico('clock',15)} ${m('eta_t')}</h3><div class="mf-next g">${m('eta_done')} <b>${D(s)}</b> ${m('eta_days')}</div></div>`}
 if(!a.eta&&a.status!=='flagged')return `<div class="card"><h3>${ico('clock',15)} ${m('eta_t')}</h3><p class="mf-sub">${etaTxt(a)}</p></div>`;
 const e=a.eta||{remaining:0,end:todayIso(),elapsed:0,budget:0,overdue:0},ds=dState(a),sl=a.sla_left;
 return `<div class="card"><div class="mf-row"><h3 style="margin:0">${ico('clock',15)} ${m('eta_t')}</h3>${ds?`<span class="badge ${DB[ds]}">${m('dl_'+ds)}</span>`:''}</div>
 <div class="mf-etag"><div><b>${D(e.remaining)}</b><span>${m('eta_rem')}</span></div><div><b>${fd(e.end)}</b><span>${m('eta_est')}</span></div>
 <div><b class="${sl<0?'neg':''}">${sl<0?'−':''}${D(Math.abs(sl))}</b><span>${m('eta_sla')} · ${fd(a.sla_due)}</span></div>
 <div><b class="${e.overdue?'neg':''}">${D(e.elapsed)} / ${D(e.budget)}</b><span>${m('eta_step')}</span></div></div>
 <div class="mf-prog"><i style="width:${Math.min(100,(a.stage+1)/8*100)}%"></i></div><p class="mf-eta ${ds}">${etaTxt(a)}</p></div>`}

/* ---------- routing / page ---------- */
const _route=route;
route=function(keep){
  const h=location.hash.slice(1)||'/';
  if(ME&&h.startsWith('/track')){const id=new URLSearchParams(h.split('?')[1]||'').get('id');location.replace('#/'+(role==='admin'?'mutadmin':'mutation')+'?tab=track'+(id?'&id='+encodeURIComponent(id):''));return}
  if(ME&&h.startsWith('/admin')&&/tab=workflow/.test(h)){location.replace('#/mutadmin?tab=apps&view=board');return}
  if(ME&&(h.startsWith('/mutation')||h.startsWith('/mutadmin'))){
    const [path,qs]=h.split('?');cur={path,q:new URLSearchParams(qs||'')};
    if(path.startsWith('/mutadmin')&&role!=='admin'){location.hash='#/mutation';return}
    $$('.view').forEach(e=>e.hidden=e.id!=='v-mut');document.body.classList.remove('map-mode','auth-mode');
    mfRender(path.startsWith('/mutadmin'));if(!keep)scrollTo(0,0);return}
  return _route(keep)};
function hlNav(){const p=(location.hash.slice(1)||'/').split('?')[0];if(p.startsWith('/mut'))$$('.nav-links a').forEach(a=>a.classList.toggle('on',a.dataset.h==='#'+p+'?tab='+ST.tab))}
renderNav=function(){const el=$('#links');if(!el)return;if(!ME){el.innerHTML='';return}
  const g=(lab,items)=>`<div class="ngrp">${lab}</div>`+items.map(([h,r,i,l])=>`<a href="${h}" data-r="${r}" data-h="${h}">${ico(i,18)}<span>${l}</span></a>`).join('');
  el.innerHTML=g(t('ng_services'),[['#/','home','home',t('nav_home')],['#/map','map','map',t('nav_map')],['#/verify','verify','search',t('nav_verify')]])
   +g(m('ng'),[['#/mutation?tab=deeds','mut','file',m('nav_deeds')],['#/mutation?tab=apps','mut','clipboard',m('nav_apps')],['#/mutation?tab=track','mut','search',m('nav_trk')]])
   +(role==='admin'?g(t('ng_admin'),[['#/admin?tab=overview','admin','dash',t('a_overview')],['#/admin?tab=fraud','admin','shield',t('a_fraud')],['#/admin?tab=data','admin','layers',t('a_data')]])
     +g(m('ng_desk'),[['#/mutadmin?tab=deeds','mut','file',m('nav_ddeeds')],['#/mutadmin?tab=apps','mut','clipboard',m('nav_dapps')],['#/mutadmin?tab=track','mut','search',m('nav_trk')]]):'')
   +`<div class="nfoot"><span class="avatar">${avatarOf(ME)}</span><span><b>${esc(nm(ME.name))}</b><span class="badge ${role==='admin'?'ok':''}">${t('role_'+role)}</span></span></div>`;
  hlNav()};
const _ru=renderUser;
renderUser=function(){_ru();const w=document.getElementById('bellWrap');if(w)w.hidden=!ME;if(ME){if(NN.uid!==ME.id){NN={rows:[],unread:0,max:0,first:true,uid:ME.id}}loadNotes()}};
document.querySelectorAll('[data-go="#/track"]').forEach(e=>e.dataset.go='#/mutation?tab=track');
const _oc=openCreate;
openCreate=function(kind,pre){if(kind==='case'){const q=new URLSearchParams({tab:'apps',new:'1'});if(pre&&pre.ds)q.set('ds',pre.ds);if(pre&&pre.plot)q.set('plot',pre.plot);const d=$('#dlg');if(d&&d.open)d.close();location.hash='#/mutadmin?'+q;return}return _oc(kind,pre)};

async function mfLoad(){const [d,a]=await Promise.all([J('/api/mut/deeds'),J('/api/mut/apps')]);ST.deeds=d.deeds;ST.apps=a.apps}
async function mfRender(admin){
  ST.admin=admin;const q=cur.q;if(q.get('tab'))ST.tab=q.get('tab');
  if(q.get('view'))['deeds','apps'].forEach(k=>VS[k].view=q.get('view'));
  const open=q.get('open');if(open)ST.tab=open.startsWith('DR-')?'deeds':'apps';if(q.get('new'))ST.tab='apps';
  $('#mf_root').innerHTML=`<div class="phead"><h1>${m(admin?'admtitle':'title')}</h1><p>${m(admin?'admsub':'sub')}</p></div><div class="tabs" id="mf_tabs"></div><div id="mf_body"><div class="empty">${t('loading')}</div></div>`;
  try{await mfLoad()}catch(e){$('#mf_body').innerHTML=`<div class="card empty">${esc(e.message)}</div>`;return}
  drawTabs();drawBody();
  if(q.get('new')){history.replaceState(null,'','#'+cur.path+'?tab=apps');openAppForm(null,{ds:q.get('ds'),plot:q.get('plot')})}
  if(open){history.replaceState(null,'','#'+cur.path+'?tab='+ST.tab);open.startsWith('DR-')?openDeed(open):openApp(open)}}
function drawTabs(){
  const nd=ST.deeds.filter(d=>d.status==='submitted').length,na=ST.apps.filter(a=>a.status==='ok').length;
  $('#mf_tabs').innerHTML=[['deeds','tab_deeds',nd],['apps','tab_apps',na],['track','tab_track',0]].map(([k,l,n])=>`<button data-t="${k}" class="${ST.tab===k?'on':''}">${m(l)}${ST.admin&&n?` <span class="badge new">${nf(n,0)}</span>`:''}</button>`).join('');
  $$('#mf_tabs button').forEach(b=>b.onclick=()=>{ST.tab=b.dataset.t;history.replaceState(null,'','#'+cur.path+'?tab='+ST.tab);drawTabs();drawBody()});hlNav()}
const drawBody=()=>({deeds:()=>drawList('deeds'),apps:()=>drawList('apps'),track:drawTrackTab})[ST.tab]();
const kpis=()=>ST.admin?`<div class="kpis">${[['kpi_pd',ST.deeds.filter(d=>d.status==='submitted').length],['kpi_act',ST.apps.filter(a=>a.status==='ok').length],['kpi_delay',ST.apps.filter(a=>dState(a)==='delayed').length],['kpi_pay',ST.apps.filter(a=>a.status==='awaiting_payment').length],['kpi_done',ST.apps.filter(a=>a.status==='completed').length]].map(([k,v])=>`<div class="kpi"><b>${nf(v,0)}</b><span>${m(k)}</span></div>`).join('')}</div>`:'';

/* ---------- list engine: table / cards / board, search, filters, sort, pagination ---------- */
const uniq=(rows,fn)=>[...new Map(rows.map(r=>[fn(r).en,fn(r)])).values()];
const FD={status:{l:'c_status',o:()=>['submitted','approved','returned','rejected'].map(v=>[v,m('ds_'+v)]),t:(r,v)=>r.status===v},
 type:{l:'c_type',o:()=>['sale','gift','inheritance'].map(v=>[v,m('ty_'+v)]),t:(r,v)=>r.type===v},
 channel:{l:'c_channel',o:()=>[['online',m('online')],['offline',m('offline')]],t:(r,v)=>r.channel===v},
 district:{l:'c_district',o:rows=>uniq(rows,r=>r.land.district).map(d=>[d.en,nm(d)]),t:(r,v)=>r.land.district.en===v}};
const FA_={status:{l:'c_status',o:()=>['draft','ok','flagged','returned','awaiting_payment','completed','rejected'].map(v=>[v,m('as_'+v)]),t:(r,v)=>r.status===v},
 delay:{l:'c_delay',o:()=>['on_track','at_risk','delayed','paused'].map(v=>[v,m('dl_'+v)]),t:(r,v)=>dState(r)===v},
 stage:{l:'c_stage',o:()=>[1,2,3,4,5,6,7].map(v=>[String(v),m('sp_'+STEPS[v+1])]),t:(r,v)=>String(r.stage)===v&&!['rejected','draft','returned'].includes(r.status)},
 basis:{l:'c_basis',o:()=>['sale','gift','inheritance'].map(v=>[v,m('ty_'+v)]),t:(r,v)=>r.basis===v},
 channel:FD.channel,district:FD.district};
const SORTS={deeds:{created:d=>d.created,id:d=>d.id,type:d=>d.type,area:d=>d.area_dec,status:d=>d.status,district:d=>d.land.district.en},
 apps:{created:a=>a.created,id:a=>a.id,applicant:a=>a.applicant.name,area:a=>a.area_dec,stage:a=>a.stage,status:a=>a.status,eta:a=>a.eta?a.eta.remaining:9999,district:a=>a.land.district.en}};
const HAY={deeds:d=>[d.id,d.deed_no,MF['ty_'+d.type].join(' '),d.land.label,d.land.plot_no,d.land.district.bn,d.land.district.en,d.land.mouza,d.seller.name,d.buyer.name,MF['ds_'+d.status].join(' ')].join(' '),
 apps:a=>[a.id,a.deed_no,a.deed_id,a.applicant.name,a.applicant.mobile,MF['ty_'+a.basis].join(' '),a.land.label,a.land.plot_no,a.land.district.bn,a.land.district.en,a.land.mouza,MF['as_'+a.status].join(' '),MF['sp_'+STEPS[a.stage+1]].join(' ')].join(' ')};
const COLS={deeds:[['id','id'],['type','type'],['land','district'],['parties',null],['area','area'],['status','status'],['created','created']],
 apps:[['id','id'],['applicant','applicant'],['land','district'],['basis',null],['stage','stage'],['eta','eta'],['status','status'],['created','created']]};
const landCell=r=>`${m('plot')} ${X(r.land.label)}<small class="mf-sub" style="display:block">${esc(nm(r.land.district))} · ${X(r.land.mouza)}</small>`;
const CELL={deeds:{id:d=>`<b>${esc(d.id)}</b>${d.deed_no?`<small class="mf-sub" style="display:block">${m('deed_no')} ${X(d.deed_no)}</small>`:''}`,type:d=>m('ty_'+d.type),land:landCell,parties:d=>`${esc(d.seller.name)} → ${esc(d.buyer.name)}`,area:d=>nf(d.area_dec)+' '+t('u_dec'),status:d=>`<span class="badge ${bc(d.status)}">${m('ds_'+d.status)}</span>`,created:d=>fd(d.created)},
 apps:{id:a=>`<b>${esc(a.id)}</b><small class="mf-sub" style="display:block">${a.channel==='offline'?m('offline'):m('online')}</small>`,applicant:a=>`${esc(a.applicant.name)}<small class="mf-sub" style="display:block">${nf(a.area_dec)} ${t('u_dec')}</small>`,land:landCell,basis:a=>m('ty_'+a.basis),stage:a=>m('sp_'+STEPS[a.stage+1]),
  eta:a=>{const s=dState(a);return `${s?`<span class="badge ${DB[s]}">${m('dl_'+s)}</span>`:''}<small class="mf-eta ${s}" style="display:block">${etaTxt(a)}</small>`},status:a=>`<span class="badge ${bc(a.status)}">${m('as_'+a.status)}</span>`,created:a=>fd(a.created)}};
const mapBtn=(r,txt)=>`<button class="btn sm alt" data-map="${esc(r.land.ds)}|${esc(r.land.plot_no)}" title="${t('open_map')}">${ico('map',14)}${txt?' '+t('open_map'):''}</button>`;
function applyF(kind){const S=VS[kind],fl=kind==='deeds'?FD:FA_,toks=norm(S.q).split(/\s+/).filter(Boolean);
  const rows=ST[kind].filter(r=>{if(toks.length){const h=norm(HAY[kind](r));if(!toks.every(x=>h.includes(x)))return false}return Object.entries(S.f).every(([k,v])=>!v||fl[k].t(r,v))});
  const sv=SORTS[kind][S.sort]||SORTS[kind].created;
  rows.sort((x,y)=>{const a=sv(x),b=sv(y);return (typeof a==='number'&&typeof b==='number'?a-b:String(a).localeCompare(String(b)))*S.dir});return rows}
function drawList(kind){
  const S=VS[kind],isD=kind==='deeds',fl=isD?FD:FA_,rows=ST[kind];
  $('#mf_body').innerHTML=`${kpis()}${isD?`<div class="card mf-info">${ico('file',22)}<div><b>${m('s0_t')}</b><p>${m('s0_d')}</p></div></div>`:''}
  <div class="card mf-tb"><div class="mf-row" style="flex-wrap:wrap">
   <div class="mf-search">${ico('search',16)}<input id="mf_q" type="search" list="mf_sug" autocomplete="off" placeholder="${esc(m('search_ph'))}" value="${esc(S.q)}"><datalist id="mf_sug"></datalist></div>
   <div class="seg" id="mf_vw">${['table','card','board'].map(v=>`<button data-v="${v}" class="${S.view===v?'on':''}" title="${m('vw_'+v)}">${ico({table:'table',card:'grid',board:'kanban'}[v],16)} <span class="mfvl">${m('vw_'+v)}</span></button>`).join('')}</div>
   <button class="btn" id="mf_new">${ico('plus',16)} ${isD?m('new_deed'):ST.admin?m('new_off'):m('new_app')}</button>${ST.admin?`<button class="btn alt" id="mf_seed">${ico('wand',16)} ${m('demo_load')}</button>`:''}</div>
  <div class="mf-flt">${Object.entries(fl).map(([k,f])=>`<label class="f mf-fl"><span>${m(f.l)}</span><select data-fk="${k}"><option value="">${m('all')}</option>${f.o(rows).map(([v,l])=>`<option value="${esc(v)}" ${S.f[k]===v?'selected':''}>${esc(l)}</option>`).join('')}</select></label>`).join('')}
   <label class="f mf-fl"><span>${m('sort_by')}</span><select id="mf_sort">${Object.keys(SORTS[kind]).map(k=>`<option value="${k}" ${S.sort===k?'selected':''}>${m('c_'+k)}</option>`).join('')}</select></label>
   <button class="btn sm alt" id="mf_dir" title="↑↓">${S.dir>0?'▲ A→Z':'▼ Z→A'}</button>
   <label class="f mf-fl" id="mf_sizew" ${S.view==='table'?'':'hidden'}><span>${m('per_page')}</span><select id="mf_size">${[10,25,50,100].map(n=>`<option value="${n}" ${S.size===n?'selected':''}>${nf(n,0)}</option>`).join('')}</select></label>
   <button class="btn sm alt" id="mf_reset">${m('reset')}</button></div><small class="mf-sub" id="mf_cnt"></small></div><div id="mf_lb"></div>`;
  const sug=[...new Set(rows.flatMap(r=>isD?[r.id,r.seller.name,r.buyer.name,r.land.label]:[r.id,r.applicant.name,r.land.label]))].slice(0,80);
  $('#mf_sug').innerHTML=sug.map(v=>`<option value="${esc(v)}">`).join('');
  $('#mf_q').oninput=e=>{S.q=e.target.value;S.page=1;drawListBody(kind)};
  $$('[data-fk]').forEach(s=>s.onchange=()=>{S.f[s.dataset.fk]=s.value;S.page=1;drawListBody(kind)});
  $('#mf_sort').onchange=e=>{S.sort=e.target.value;drawListBody(kind)};
  $('#mf_dir').onclick=()=>{S.dir*=-1;drawList(kind)};
  $('#mf_size').onchange=e=>{S.size=+e.target.value;LSs('size_'+kind,S.size);S.page=1;drawListBody(kind)};
  $('#mf_reset').onclick=()=>{Object.assign(S,{q:'',f:{},sort:'created',dir:-1,page:1});drawList(kind)};
  $$('#mf_vw button').forEach(b=>b.onclick=()=>{S.view=b.dataset.v;LSs('view_'+kind,S.view);S.page=1;drawList(kind)});
  $('#mf_new').onclick=()=>isD?openDeedForm(null):openAppForm(null);
  const sd=$('#mf_seed');if(sd)sd.onclick=async()=>{sd.disabled=true;try{await J('/api/admin/mut/seed',{});toast(m('demo_loaded'));await mfLoad();drawTabs();drawList(kind)}catch(e){toast(e.message);sd.disabled=false}};
  drawListBody(kind)}
function pagerHTML(S,total,size){const pages=Math.max(1,Math.ceil(total/size));S.page=Math.min(Math.max(1,S.page),pages);
  const from=total?(S.page-1)*size+1:0,to=Math.min(total,S.page*size),nums=[];for(let p=1;p<=pages;p++)if(p===1||p===pages||Math.abs(p-S.page)<=1)nums.push(p);
  const out=[];nums.forEach((p,i)=>{if(i&&p-nums[i-1]>1)out.push('…');out.push(p)});
  return `<div class="mf-pg"><small class="mf-sub">${D(from)}–${D(to)} / ${D(total)}</small><div class="btnrow"><button class="btn sm alt" data-pg="${S.page-1}" ${S.page<=1?'disabled':''}>‹</button>${out.map(p=>p==='…'?'<span>…</span>':`<button class="btn sm ${p===S.page?'':'alt'}" data-pg="${p}">${D(p)}</button>`).join('')}<button class="btn sm alt" data-pg="${S.page+1}" ${S.page>=pages?'disabled':''}>›</button></div></div>`}
function tableHTML(kind,rows){const S=VS[kind],cols=COLS[kind];
  return `<div class="card tbl" style="padding:6px"><table class="rt"><thead><tr>${cols.map(([c,sk])=>`<th ${sk?`data-sort="${sk}" class="srt"`:''}>${m('c_'+c)}${sk&&S.sort===sk?` ${S.dir>0?'▲':'▼'}`:''}</th>`).join('')}<th></th></tr></thead><tbody>${rows.map(r=>`<tr class="r" data-open="${r.id}">${cols.map(([c])=>`<td data-l="${m('c_'+c)}">${CELL[kind][c](r)}</td>`).join('')}<td>${mapBtn(r)}</td></tr>`).join('')||`<tr><td colspan="9" class="empty">${m('none')}</td></tr>`}</tbody></table></div>`}
function cardHTML(kind,r){
  if(kind==='deeds')return `<div class="card mf-card" data-open="${r.id}"><div class="mf-row"><b>${esc(r.id)}</b><span class="badge ${bc(r.status)}">${m('ds_'+r.status)}</span></div>
   <div class="mf-t">${m('ty_'+r.type)} · ${m('plot')} ${X(r.land.label)}</div><small class="mf-sub">${esc(nm(r.land.district))} · ${X(r.land.mouza)}</small>
   <small>${esc(r.seller.name)} → ${esc(r.buyer.name)}</small><small class="mf-sub">${nf(r.area_dec)} ${t('u_dec')}${r.value?' · ৳ '+nf(r.value,0):''}</small>
   <div class="mf-row"><small class="mf-sub">${fd(r.created)} · ${r.channel==='offline'?m('offline'):m('online')}${r.deed_no?' · '+m('deed_no')+' '+X(r.deed_no):''}</small>${mapBtn(r,1)}</div></div>`;
  const s=dState(r);
  return `<div class="card mf-card" data-open="${r.id}"><div class="mf-row"><b>${esc(r.id)}</b><span class="badge ${bc(r.status)}">${m('as_'+r.status)}</span></div>
   <div class="mf-t">${m('ty_'+r.basis)} · ${m('plot')} ${X(r.land.label)}</div><small class="mf-sub">${esc(nm(r.land.district))} · ${X(r.land.mouza)}</small>
   <small>${esc(r.applicant.name)} · ${nf(r.area_dec)} ${t('u_dec')}</small><div class="mf-prog"><i style="width:${(r.status==='completed'?8:r.stage+1)/8*100}%"></i></div>
   <div class="mf-row"><small class="mf-sub">${m('sp_'+STEPS[r.stage+1])}</small>${s?`<span class="badge ${DB[s]}">${m('dl_'+s)}</span>`:''}</div>
   <small class="mf-eta ${s}">${ico('clock',13)} ${etaTxt(r)}</small><div class="mf-row"><small class="mf-sub">${fd(r.created)} · ${r.channel==='offline'?m('offline'):m('online')}</small>${mapBtn(r,1)}</div></div>`}
function boardCols(kind){
  if(kind==='deeds')return ['submitted','approved','returned','rejected'].map(s=>({id:s,l:m('ds_'+s),test:r=>r.status===s}));
  const act=r=>!['completed','rejected','draft','returned'].includes(r.status);
  return [{id:'0',l:m('bd_draft'),test:r=>['draft','returned'].includes(r.status)},...[1,2,3,4,5,6].map(i=>({id:String(i),l:m('sp_'+STEPS[i+1]),test:r=>act(r)&&r.stage===i})),{id:'7',l:m('as_completed'),test:r=>r.status==='completed'},{id:'x',l:m('bd_closed'),test:r=>r.status==='rejected'}]}
function boardCard(kind,r){const s=kind==='apps'?dState(r):'';
  return `<div class="kcard ${s==='delayed'?'delayed':r.status}" data-open="${r.id}" data-id="${r.id}" ${ST.admin?'draggable="true"':''}><b>${esc(r.id)}</b>${m('plot')} ${X(r.land.label)} · ${esc(nm(r.land.district))}<br><small>${kind==='deeds'?esc(r.seller.name)+' → '+esc(r.buyer.name):esc(r.applicant.name)}</small>
   ${kind==='apps'?`<div style="margin-top:6px"><span class="badge ${bc(r.status)}">${m('as_'+r.status)}</span> ${s?`<span class="badge ${DB[s]}">${m('dl_'+s)}</span>`:''}</div><small class="mf-eta ${s}" style="margin-top:4px">${etaTxt(r)}</small>`:`<div style="margin-top:6px"><span class="badge ${bc(r.status)}">${m('ds_'+r.status)}</span></div>`}</div>`}
function boardHTML(kind,rows){return `<div class="board mf-board">${boardCols(kind).map(c=>{const cs=rows.filter(c.test);return `<div class="col" data-col="${c.id}"><h4><span>${c.l}</span><span>${nf(cs.length,0)}</span></h4>${cs.map(r=>boardCard(kind,r)).join('')||`<small class="mf-sub">${m('none')}</small>`}</div>`}).join('')}</div>${ST.admin?`<p class="mf-sub">${m('bd_hint')}</p>`:''}`}
function dropCard(kind,id,col){
  if(kind==='deeds'){const d=ST.deeds.find(x=>x.id===id);if(!d||d.status!=='submitted'||col==='submitted')return toast(m('bd_only_next'));return openDeed(id)}
  const a=ST.apps.find(x=>x.id===id);if(!a)return;if(col==='x')return openApp(id);
  if(+col===a.stage+1&&a.status==='ok'){toast(m('bd_next'));return openApp(id)}toast(m('bd_only_next'))}
function drawListBody(kind){
  const S=VS[kind],rows=applyF(kind),el=$('#mf_lb');$('#mf_cnt').textContent=`${D(rows.length)} / ${D(ST[kind].length)} ${m('res_n')}`;$('#mf_sizew').hidden=S.view!=='table';
  if(S.view==='board')el.innerHTML=boardHTML(kind,rows);
  else{const size=S.view==='card'?10:S.size,pg=pagerHTML(S,rows.length,size),sl=rows.slice((S.page-1)*size,S.page*size);
    el.innerHTML=(S.view==='card'?`<div class="mf-grid">${sl.map(r=>cardHTML(kind,r)).join('')||`<div class="card empty">${m('none')}</div>`}</div>`:tableHTML(kind,sl))+pg}
  el.onclick=e=>{const mp=e.target.closest('[data-map]');if(mp){e.stopPropagation();const [ds,no]=mp.dataset.map.split('|');location.hash='#/map?ds='+encodeURIComponent(ds)+'&plot='+encodeURIComponent(no);return}
    const pg=e.target.closest('[data-pg]');if(pg){if(!pg.disabled){S.page=+pg.dataset.pg;drawListBody(kind)}return}
    const th=e.target.closest('[data-sort]');if(th){const k=th.dataset.sort;if(S.sort===k)S.dir*=-1;else{S.sort=k;S.dir=1}drawList(kind);return}
    const o=e.target.closest('[data-open]');if(o)kind==='deeds'?openDeed(o.dataset.open):openApp(o.dataset.open)};
  el.ondragstart=e=>{const c=e.target.closest('.kcard');if(c)e.dataTransfer.setData('text/plain',c.dataset.id)};
  el.ondragover=e=>{const c=e.target.closest('.col');if(c){e.preventDefault();c.classList.add('over')}};
  el.ondragleave=e=>{const c=e.target.closest('.col');if(c)c.classList.remove('over')};
  el.ondrop=e=>{const c=e.target.closest('.col');if(!c)return;e.preventDefault();c.classList.remove('over');dropCard(kind,e.dataTransfer.getData('text/plain'),c.dataset.col)}}

/* ---------- tracking (single, for deeds and mutations) ---------- */
async function drawTrackTab(){
  $('#mf_body').innerHTML=`<div class="card"><p class="mf-sub" style="margin:0 0 8px">${m('tk_by')}</p><div class="placebox"><input id="tk_in" list="tk_dl" autocomplete="off" placeholder="${esc(m('track_ph'))}"><datalist id="tk_dl"></datalist><button class="btn" id="tk_go">${m('track_btn')}</button></div>
   <div style="margin-top:12px"><b style="font-size:13.5px">${ico('wand',14)} ${m('demo_chip')}</b><div class="mf-chips" id="tk_chips"><small class="mf-sub">${t('loading')}</small></div></div></div><div id="tk_out" style="margin-top:14px"></div>`;
  const go=async v=>{v=(v||$v('tk_in')).trim();if(!v)return;setv('tk_in',v);try{const r=await J('/api/mut/track/'+encodeURIComponent(v));
    const isA=r.kind==='app',st=isA?aStepper({stage:r.stage,status:r.status}):dStepper({status:r.status}),s=isA?dState(r):'';
    $('#tk_out').innerHTML=`<div class="card"><div class="mf-row"><h3 style="margin:0">${esc(r.id)}</h3><span class="badge ${bc(r.status)}">${m((isA?'as_':'ds_')+r.status)}</span>${s?`<span class="badge ${DB[s]}">${m('dl_'+s)}</span>`:''}</div>
     <p class="mf-sub" style="margin:4px 0">${isA?m('ty_'+r.basis):m('ty_'+r.type)} · ${m('plot')} ${X(r.land.label)} · ${esc(nm(r.land.district))} · ${X(r.land.mouza)} · ${r.channel==='offline'?m('offline'):m('online')}</p>${st}
     ${isA?`<p class="mf-eta ${s}">${ico('clock',14)} ${etaTxt(r)}${r.sla_due?` · ${m('eta_sla')}: ${fd(r.sla_due)}`:''}</p>`:''}${tlHTML(r.timeline)}
     <div class="btnrow"><button class="btn sm alt" data-map="${esc(r.land.ds)}|${esc(r.land.plot_no)}">${ico('map',14)} ${t('open_map')}</button>${r.mine?`<button class="btn sm" id="tk_open">${m('open')}</button>`:''}</div></div>`;
    const o=$('#tk_open');if(o)o.onclick=()=>isA?openApp(r.id):openDeed(r.id);
    const mb=$('#tk_out [data-map]');if(mb)mb.onclick=()=>{location.hash='#/map?ds='+encodeURIComponent(r.land.ds)+'&plot='+encodeURIComponent(r.land.plot_no)}}catch(e){$('#tk_out').innerHTML=`<div class="card empty">${m('nf')}</div>`}};
  $('#tk_go').onclick=()=>go();$('#tk_in').onkeydown=e=>{if(e.key==='Enter')go()};
  try{const d=await J('/api/mut/demo');const all=[...d.apps.map(a=>[a.id,m('as_'+a.status)+(a.status==='ok'||a.status==='flagged'?' · '+m('sp_'+STEPS[a.stage+1]):'')]),...d.deeds.map(x=>[x.id,m('ds_'+x.status)])];
    $('#tk_chips').innerHTML=all.map(([id,l])=>`<button class="chip" data-id="${id}">${id} · ${esc(l)}</button>`).join('')||`<small class="mf-sub">${m('none')}</small>`;
    $('#tk_dl').innerHTML=all.map(([id])=>`<option value="${id}">`).join('');$$('#tk_chips .chip').forEach(b=>b.onclick=()=>go(b.dataset.id))}catch(e){$('#tk_chips').innerHTML=''}
  const q=cur.q.get('id');if(q)go(q)}
async function refresh(then){await mfLoad();drawTabs();if(ST.tab!=='track')drawBody();if(then)then()}

/* ---------- land: full classification + interactive mini map ---------- */
function initLandMap(rec){const el=document.getElementById('lt_map');if(!el||!rec||!window.L)return;if(LM){LM.remove();LM=null}
  LM=L.map(el,{attributionControl:false});L.tileLayer(esri+'World_Imagery/MapServer/tile/{z}/{y}/{x}',{maxZoom:21,maxNativeZoom:19}).addTo(LM);
  const g=L.geoJSON(rec.geometry,{style:{color:'#ffd60a',weight:3,fillOpacity:.2}}).addTo(LM);LM.fitBounds(g.getBounds(),{padding:[20,20],maxZoom:20});setTimeout(()=>LM&&LM.invalidateSize(),80)}
function landPane(l,areaDec,rec){
  const parts=l.parts||[],tot=parts.reduce((s,p)=>s+p.area_sqm,0)||l.area_sqm;
  const risk=rec?`<div class="mf-row" style="margin:8px 0"><span class="badge ${rec.verdict}">${t('v_verdict_'+rec.verdict)} · ${nf(rec.score,0)}</span><span class="badge ${rec.khas==='none'?'pass':'fail'}">${t('kh_'+rec.khas)}</span><span class="badge ${rec.acq==='none'?'pass':rec.acq==='proposed'?'warn':'fail'}">${t('aq_'+rec.acq)}</span></div>
   ${kv([[m('lt_flags'),nf(rec.flags.length,0)],[m('lt_pend'),nf(rec.pending.length,0)]])}`:'';
  return `<div class="mf-two"><div class="card"><h3>${m('sec_land')}</h3>${landKV(l,[[m('f_area'),nf(areaDec)+' '+t('u_dec')+' ('+nf(areaDec/100,4)+' '+t('u_acre')+')']])}${risk}</div>
   <div class="card"><h3>${m('lt_map')}</h3><div id="lt_map" class="mf-map"></div><div class="btnrow"><button class="btn sm" data-vmap>${ico('map',14)} ${m('lt_viewmap')}</button><a class="btn sm alt" target="_blank" rel="noopener" href="https://www.google.com/maps?q=${(l.center||[0,0])[0]},${(l.center||[0,0])[1]}">${ico('globe',14)} ${m('lt_gm')}</a></div></div></div>
  <div class="card" style="margin-top:14px"><h3>${m('lt_class')}</h3>${parts.length?`<div class="mf-stack">${parts.map(p=>`<i style="width:${p.area_sqm/tot*100}%;background:${colorOf(p.maj)}" title="${esc(cls(p.maj))}"></i>`).join('')}</div>
   <table class="rt"><thead><tr><th>${m('lt_cls')}</th><th>${m('lt_use')}</th><th>${m('c_area')} (${t('u_dec')})</th><th>${m('lt_share')}</th></tr></thead><tbody>${parts.map(p=>`<tr class="r"><td data-l="${m('lt_cls')}"><span class="sw" style="display:inline-block;width:12px;height:12px;border-radius:3px;background:${colorOf(p.maj)};margin-right:6px"></span>${esc(cls(p.maj))}</td><td data-l="${m('lt_use')}">${esc(sub(p.sub))}</td><td data-l="${m('c_area')}">${nf(dec(p.area_sqm))}</td><td data-l="${m('lt_share')}">${nf(p.area_sqm/tot*100,1)}%</td></tr>`).join('')}<tr class="r"><td colspan="2"><b>${m('lt_total')}</b></td><td><b>${nf(dec(tot))}</b></td><td><b>${nf(100,0)}%</b></td></tr></tbody></table>`:`<p class="mf-sub">${m('lt_noparts')}</p>`}</div>`}

/* ---------- demo-assisted forms ---------- */
const P3=(pre,lab,v)=>`<div class="full"><b>${lab}</b></div><label class="f"><span>${m('f_name')}</span><input id="${pre}n" list="dl_n" value="${esc(v.name||'')}"></label><label class="f"><span>${m('f_father')}</span><input id="${pre}f" list="dl_f" value="${esc(v.father||'')}"></label><label class="f"><span>${m('f_nid')}</span><input id="${pre}i" list="dl_i" inputmode="numeric" value="${esc(v.nid||'')}"></label><label class="f"><span>${m('f_addr')}</span><input id="${pre}a" list="dl_a" value="${esc(v.address||'')}"></label>`;
async function openDeedForm(d){
  const adm=ST.admin,me=ME||{},isE=!!d;
  const dsOpts=DSL.map(x=>`<option value="${esc(x.id)}" ${isE&&d.land.ds===x.id?'selected':''}>${esc(dn(x))} · ${esc(shl(x))}</option>`).join('');
  const bdv=isE?d.boundaries:{},w=isE?d.witnesses:[];
  show(head(isE?m('edit')+' · '+d.id:m('new_deed'))+`<div class="mf-row" style="margin-bottom:10px"><small class="mf-sub">${m('demo_hint')}</small><button class="btn sm alt" id="df_demo">${ico('wand',15)} ${m('demo_fill')}</button></div><div class="fgrid">
   <label class="f"><span>${m('f_type')}</span><select id="df_type">${['sale','gift','inheritance'].map(k=>`<option value="${k}" ${isE&&d.type===k?'selected':''}>${m('ty_'+k)}</option>`).join('')}</select></label>
   <label class="f"><span>${m('f_date')}</span><input id="df_date" type="date" max="${todayIso()}" value="${isE?d.deed_date:todayIso()}"></label>
   <label class="f"><span>${m('f_ds')}</span><select id="df_ds">${dsOpts}</select></label>
   <label class="f"><span>${m('f_plot')}</span><input id="df_plot" list="df_plots" autocomplete="off" value="${isE?esc(d.land.plot_no):''}"><datalist id="df_plots"></datalist></label>
   <div class="full card" id="df_land" style="background:var(--sf2);box-shadow:none;padding:12px" hidden></div>
   <label class="f"><span>${m('f_area')}</span><input id="df_area" type="number" step="0.01" min="0" value="${isE?d.area_dec:''}"></label>
   <label class="f"><span>${m('f_value')}</span><input id="df_val" type="number" min="0" value="${isE?d.value:''}"></label>
   <div class="full fgrid" id="df_pa" style="padding:0">${P3('dp_','',isE?d.seller:{})}</div><div class="full fgrid" id="df_pb" style="padding:0">${P3('db_','',isE?d.buyer:{name:adm?'':nm(me.name)})}</div>
   <label class="f"><span>${m('f_w1')}</span><input id="df_w1" list="dl_n" value="${esc(w[0]||'')}"></label><label class="f"><span>${m('f_w2')}</span><input id="df_w2" list="dl_n" value="${esc(w[1]||'')}"></label>
   <div class="full"><b>${m('f_bd')}</b></div>${['north','south','east','west'].map((k,i)=>`<label class="f"><span>${m('f_'+'nsew'[i])}</span><input id="df_b${i}" value="${esc(bdv[k]||'')}"></label>`).join('')}
   <label class="f full"><span>${m('f_note')}</span><textarea id="df_note" maxlength="300">${isE?esc(d.note):''}</textarea></label></div>
   <div class="btnrow" style="margin-top:14px;justify-content:flex-end"><button class="btn alt" data-x>${m('cancel')}</button><button class="btn" id="df_go">${isE?m('resub'):m('save')}</button></div>`);
  bindPerson('dp_');bindPerson('db_');
  const relabel=()=>{const ty=$v('df_type');$('#df_pa b').textContent=pl(ty,0);$('#df_pb b').textContent=pl(ty,1)};
  const info=async()=>{const ds=$v('df_ds'),no=$v('df_plot');if(!ds||!no)return null;try{const r=await api(`/api/verify/${encodeURIComponent(ds)}/${encodeURIComponent(no)}`);
    const b=$('#df_land');b.hidden=false;b.innerHTML=`${landKV({district:r.district,mouza:r.mouza,m_code:r.m_code,jl:r.jl,label:r.label,khatian:r.khatian,area_sqm:r.area_sqm})}${r.khas!=='none'?`<span class="badge fail">${t('kh_'+r.khas)}</span>`:''}`;
    if(!$v('df_area'))$('#df_area').value=dec(r.area_sqm).toFixed(2);if(!isE&&!$v('dp_n')){$('#dp_n').value=nm(r.owner);$('#dp_f').value=nm(r.father)}return r}catch(e){$('#df_land').hidden=true;return null}};
  $('#df_type').onchange=relabel;$('#df_ds').onchange=()=>{$('#df_plot').value='';fillPlots($('#df_plots'),$v('df_ds'))};$('#df_plot').onchange=info;relabel();fillPlots($('#df_plots'),$v('df_ds'));if(isE)info();
  $('#df_demo').onclick=async()=>{const ds=$v('df_ds');let plots=[];try{plots=await api('/api/plots/'+encodeURIComponent(ds))}catch(e){}
    let r=null;for(let i=0;i<10&&plots.length;i++){const p=rnd(plots);try{const x=await api(`/api/verify/${encodeURIComponent(ds)}/${encodeURIComponent(p.plot_no)}`);if(x.khas==='none'&&x.acq!=='notified'){r=x;break}}catch(e){}}
    if(!r)return toast(m('nf'));setv('df_plot',r.plot_no);setv('df_area','');setv('dp_n','');setv('dp_f','');await info();
    const area=Math.max(0.5,dec(r.area_sqm)*0.5);setv('df_area',area.toFixed(2));const buy=rnd(DP),w=DP.filter(p=>p!==buy);
    setv('df_val',$v('df_type')==='sale'?Math.round(area*(60+Math.random()*80))*1000:'');setv('dp_i','');setv('dp_a','গ্রাম: '+nm(r.district)+' সদর');
    setv('db_n',buy[0]);setv('db_f',buy[1]);setv('db_i',buy[2]);setv('db_a',buy[3]);setv('df_w1',w[0][0]);setv('df_w2',w[1][0]);
    ['রাস্তা','খাল','ফজলুর রহমানের জমি','আব্দুল মান্নানের জমি'].forEach((v,i)=>setv('df_b'+i,v));setv('df_note','ডেমো তথ্য দিয়ে পূরণকৃত আবেদন')};
  $('#df_go').onclick=async()=>{const pp=p=>({name:$v(p+'n'),father:$v(p+'f'),nid:$v(p+'i'),address:$v(p+'a')});
    const body={type:$v('df_type'),ds:$v('df_ds'),plot_no:$v('df_plot'),area_dec:$v('df_area'),value:$v('df_val'),deed_date:$v('df_date'),seller:pp('dp_'),buyer:pp('db_'),witnesses:[$v('df_w1'),$v('df_w2')],boundaries:{north:$v('df_b0'),south:$v('df_b1'),east:$v('df_b2'),west:$v('df_b3')},note:$v('df_note')};
    $('#df_go').disabled=true;try{const r=await J(isE?'/api/mut/deeds/'+d.id:'/api/mut/deeds',body,isE?'PUT':'POST');await refresh();openDeed(r.deed.id)}catch(e){toast(e.message);$('#df_go').disabled=false}}}
async function openDeed(id){
  let d;try{d=(await J('/api/mut/deeds/'+encodeURIComponent(id))).deed}catch(e){return toast(e.message)}
  const rec=await api(`/api/verify/${encodeURIComponent(d.land.ds)}/${encodeURIComponent(d.land.plot_no)}`).catch(()=>null);
  if(DT.id!==id){DT.id=id;DT.tab='ov'}
  const adm=ST.admin,r=d.registered;
  const ov=(d.reason&&['returned','rejected'].includes(d.status)?`<div class="mf-next r"><b>${m(d.status==='returned'?'ret_reason':'rej_reason')}:</b> ${esc(d.reason)}</div>`:'')
   +(d.status==='approved'?`<div class="mf-next g"><b>${m('deed_no')} ${X(d.deed_no)}</b> · ${bdate(r.date)} · ${esc(nm(r.office))}</div>`:'')
   +`<div class="mf-two"><div class="card"><h3>${m('sec_land')}</h3>${landKV(d.land,[[m('f_area'),nf(d.area_dec)+' '+t('u_dec')],[m('f_value'),d.value?'৳ '+nf(d.value,0):'—'],[m('f_date'),fd(d.deed_date)]])}</div>
   <div class="card"><h3>${pl(d.type,0)}</h3>${partyKV(d.seller)}<h3 style="margin-top:12px">${pl(d.type,1)}</h3>${partyKV(d.buyer)}</div></div>
   <div class="card" style="margin-top:14px"><h3>${m('sec_pre')}</h3>${chkHTML(d.checks)}</div>`;
  show(head(`${esc(d.id)} <span class="badge ${bc(d.status)}">${m('ds_'+d.status)}</span>`,`${m('ty_'+d.type)} · ${esc(nm(d.land.district))}`)+dStepper(d)
  +`<div class="btnrow" style="margin-top:8px">${d.status==='approved'?`<button class="btn" id="d_dl">${ico('download',16)} ${m('dl_deed')}</button>`:''}
   ${d.status==='approved'&&!d.app_id&&!adm?`<button class="btn alt" id="d_mut">${ico('file',16)} ${m('start_mut')}</button>`:''}${d.app_id?`<button class="btn alt" id="d_va">${m('view_app')}</button>`:''}
   ${d.status==='returned'&&!adm?`<button class="btn alt" id="d_ed">${ico('file',16)} ${m('edit')}</button>`:''}<button class="btn sm alt" data-vmap>${ico('map',15)} ${m('lt_viewmap')}</button></div>`
  +tabsBar([['ov',m('dt_ov')],['land',m('dt_land')],['tl',m('dt_tl')]],DT.tab)+pane('ov',DT.tab,ov)+pane('land',DT.tab,landPane(d.land,d.area_dec,rec))+pane('tl',DT.tab,`<div class="card" style="margin-top:14px">${tlHTML(d.timeline)}</div>`)
  +(adm&&d.status==='submitted'?`<div class="mf-panel"><h3>${m('a_approve')}</h3><div class="fgrid"><label class="f"><span>বই নং</span><input id="dp_book" placeholder="1"></label><label class="f"><span>ভলিউম</span><input id="dp_vol"></label><label class="f"><span>পৃষ্ঠা</span><input id="dp_page"></label></div>
   <input id="dp_reason" placeholder="${esc(m('reason_ph'))}"><div class="btnrow" style="margin-top:10px"><button class="btn sm" data-da="approve">${ico('check',15)} ${m('a_approve')}</button><button class="btn sm alt" data-da="return" data-need>${m('a_return')}</button><button class="btn sm danger" data-da="reject" data-need>${m('a_reject')}</button></div></div>`:''));
  const D_=dlg(),g=s=>D_.querySelector(s);wireTabs(rec);D_.querySelectorAll('[data-vmap]').forEach(b=>b.onclick=()=>viewMap(d.land));
  if(g('#d_dl'))g('#d_dl').onclick=()=>pdDeed(d);if(g('#d_mut'))g('#d_mut').onclick=()=>openAppForm(d);if(g('#d_va'))g('#d_va').onclick=()=>openApp(d.app_id);if(g('#d_ed'))g('#d_ed').onclick=()=>openDeedForm(d);
  D_.querySelectorAll('[data-da]').forEach(b=>b.onclick=async()=>{const act=b.dataset.da,reason=$v('dp_reason');if(b.hasAttribute('data-need')&&!reason)return toast(m('need_reason'));
    try{await J('/api/admin/mut/deeds/'+d.id+'/'+act,{reason,book:$v('dp_book'),volume:$v('dp_vol'),page:$v('dp_page')});toast(m('done'));await refresh();openDeed(d.id)}catch(e){toast(e.message)}})}
function openAppForm(deed,pre){pre=pre||{};
  const adm=ST.admin,me=ME||{},ok=ST.deeds.filter(d=>d.status==='approved'&&!d.app_id);
  if(!ok.length&&!adm)return toast(m('need_deed'));
  const opts=ok.map(d=>`<option value="${d.id}" ${deed&&deed.id===d.id?'selected':''}>${esc(d.id)} · ${m('ty_'+d.type)} · ${X(d.deed_no||'')} · ${X(d.land.label)}</option>`).join('');
  const pe=deed?deed.buyer:{name:adm?'':nm(me.name)},useExt=adm&&!deed&&(pre.ds||!ok.length);
  show(head(adm?m('new_off'):m('new_app'),adm?m('online')+' / '+m('offline'):'')+`<div class="mf-row" style="margin-bottom:10px"><small class="mf-sub">${m('demo_hint')}</small><button class="btn sm alt" id="af_demo">${ico('wand',15)} ${m('demo_fill')}</button></div><div class="fgrid">
   <label class="f full"><span>${m('f_deed')}</span><select id="af_deed">${opts}${adm?`<option value="" ${useExt||!ok.length?'selected':''}>${m('f_ext')}</option>`:''}</select></label>
   <div class="full fgrid" id="af_ext" hidden>
    <label class="f"><span>${m('f_ds')}</span><select id="ex_ds">${DSL.map(x=>`<option value="${esc(x.id)}" ${pre.ds===x.id?'selected':''}>${esc(dn(x))} · ${esc(shl(x))}</option>`).join('')}</select></label>
    <label class="f"><span>${m('f_plot')}</span><input id="ex_plot" list="ex_plots" value="${esc(pre.plot||'')}"><datalist id="ex_plots"></datalist></label>
    <label class="f"><span>${m('f_type')}</span><select id="ex_type">${['sale','gift','inheritance'].map(k=>`<option value="${k}">${m('ty_'+k)}</option>`).join('')}</select></label>
    <label class="f"><span>${m('f_dno')}</span><input id="ex_no" list="dl_dno"></label><label class="f"><span>${m('f_date')}</span><input id="ex_date" type="date" max="${todayIso()}"></label>
    <label class="f"><span>${m('f_area')}</span><input id="ex_area" type="number" step="0.01"></label><label class="f full"><span>${m('f_seller_n')}</span><input id="ex_seller" list="dl_n"></label></div>
   <div class="full"><b>${m('sec_app')}</b></div>
   <label class="f"><span>${m('f_name')}</span><input id="ap_n" list="dl_n" value="${esc(pe.name||'')}"></label><label class="f"><span>${m('f_father')}</span><input id="ap_f" list="dl_f" value="${esc(pe.father||'')}"></label>
   <label class="f"><span>${m('f_nid')}</span><input id="ap_i" list="dl_i" inputmode="numeric" value="${esc(pe.nid||'')}"></label><label class="f"><span>${m('f_mobile')}</span><input id="ap_m" list="dl_m" inputmode="tel" placeholder="01XXXXXXXXX"></label>
   <label class="f"><span>${m('f_email')}</span><input id="ap_e" value="${esc(adm?'':me.email||'')}"></label><label class="f"><span>${m('f_addr')}</span><input id="ap_a" list="dl_a" value="${esc(pe.address||'')}"></label>
   <label class="f"><span>${m('f_pk')}</span><input id="ap_pk" list="dl_pk"></label><label class="f"><span>${m('f_ldt')}</span><input id="ap_ldt" list="dl_ldt"></label>
   <label class="f"><span>${m('f_ldty')}</span><input id="ap_ldty" inputmode="numeric" placeholder="${new Date().getFullYear()}"></label>
   <label class="chk full"><input type="checkbox" id="ap_court"><span>${m('f_court')}</span></label></div>
   <div class="btnrow" style="margin-top:14px;justify-content:flex-end"><button class="btn alt" data-x>${m('cancel')}</button><button class="btn" id="af_go">${m('create')}</button></div>`);
  bindPerson('ap_');const tg=()=>{$('#af_ext').hidden=!!$v('af_deed')};$('#af_deed').onchange=tg;tg();
  if(adm){$('#ex_ds').onchange=()=>fillPlots($('#ex_plots'),$v('ex_ds'));fillPlots($('#ex_plots'),$v('ex_ds'))}
  $('#af_demo').onclick=async()=>{const p=rnd(DP);setv('ap_n',p[0]);setv('ap_f',p[1]);setv('ap_i',p[2]);setv('ap_m',p[4]);setv('ap_a',p[3]);setv('ap_e','');setv('ap_pk',String(1000+Math.floor(Math.random()*3000)));setv('ap_ldt','LDT-'+(10000+Math.floor(Math.random()*89999)));setv('ap_ldty',new Date().getFullYear()-1);
    if(adm&&!$v('af_deed')){const ds=$v('ex_ds');let plots=[];try{plots=await api('/api/plots/'+encodeURIComponent(ds))}catch(e){}
      for(let i=0;i<10&&plots.length;i++){const pp=rnd(plots);try{const x=await api(`/api/verify/${encodeURIComponent(ds)}/${encodeURIComponent(pp.plot_no)}`);if(x.khas==='none'&&x.acq!=='notified'){setv('ex_plot',pp.plot_no);setv('ex_area',Math.max(0.5,dec(x.area_sqm)*0.4).toFixed(2));setv('ex_seller',nm(x.owner));break}}catch(e){}}
      setv('ex_no',(1000+Math.floor(Math.random()*8000))+'/'+new Date().getFullYear());setv('ex_date',new Date(Date.now()-864e6).toISOString().slice(0,10))}};
  $('#af_go').onclick=async()=>{const body={deed_id:$v('af_deed'),applicant:{name:$v('ap_n'),father:$v('ap_f'),nid:$v('ap_i'),mobile:$v('ap_m'),email:$v('ap_e'),address:$v('ap_a')},court_ordered:$c('ap_court'),prev_khatian:$v('ap_pk'),ldt_receipt:$v('ap_ldt'),ldt_paid_till:$v('ap_ldty')};
    if(!body.deed_id)body.external={ds:$v('ex_ds'),plot_no:$v('ex_plot'),type:$v('ex_type'),deed_no:$v('ex_no'),deed_date:$v('ex_date'),area_dec:$v('ex_area'),seller:{name:$v('ex_seller')}};
    $('#af_go').disabled=true;try{const r=await J('/api/mut/apps',body);await refresh();openApp(r.app.id)}catch(e){toast(e.message);$('#af_go').disabled=false}}}

/* ---------- application detail ---------- */
function pickFile(aid,k){const i=document.createElement('input');i.type='file';i.accept=(k==='photo'||k==='signature')?'image/*':'image/*,application/pdf';
  i.onchange=()=>{const f=i.files[0];if(!f)return;if(f.size>1.5*1024*1024)return toast(m('toobig'));const r=new FileReader();
    r.onload=async()=>{try{await J(`/api/mut/apps/${aid}/doc/${k}`,{name:f.name,data:r.result});openApp(aid)}catch(e){toast(e.message)}};r.readAsDataURL(f)};i.click()}
function demoPng(label,kind){const c=document.createElement('canvas');c.width=480;c.height=kind==='photo'?600:300;const g=c.getContext('2d');
  g.fillStyle='#f4f8f5';g.fillRect(0,0,c.width,c.height);g.strokeStyle='#0b7d4c';g.lineWidth=6;g.strokeRect(6,6,c.width-12,c.height-12);g.fillStyle='#0b7d4c';g.textAlign='center';
  if(kind==='photo'){g.beginPath();g.arc(240,230,90,0,7);g.fill();g.fillRect(120,340,240,170)}
  else if(kind==='signature'){g.strokeStyle='#123';g.lineWidth=4;g.beginPath();g.moveTo(40,170);for(let i=0;i<8;i++)g.quadraticCurveTo(80+i*50,70+(i%2)*140,100+i*48,170);g.stroke()}
  g.fillStyle='#0b7d4c';g.font='bold 24px sans-serif';g.fillText(label,c.width/2,c.height-44);g.font='15px sans-serif';g.fillText('DEMO DOCUMENT',c.width/2,c.height-20);return c.toDataURL('image/png')}
async function demoDocs(a){const need=a.required.filter(k=>!a.docs[k]&&(k!=='deed'||!a.deed_id));
  for(const k of need){try{await J(`/api/mut/apps/${a.id}/doc/${k}`,{name:'demo-'+k+'.png',data:demoPng(m('dc_'+k),k)})}catch(e){toast(e.message);break}}openApp(a.id,'docs')}
function nextMsg(a){if(a.status==='completed')return['g',m('nx_7')];if(a.status==='rejected')return['r',m('rej_reason')+': '+esc(a.reason)];if(a.status==='returned')return['r',m('ret_reason')+': '+esc(a.reason)];if(a.status==='flagged')return['r',m('on_hold')+': '+esc(a.reason)];if(a.status==='draft')return['',m('nx_draft')];return[a.status==='awaiting_payment'?'':'g',m('nx_'+a.stage)]}
function spCard(a,adm,own){const L=a.special_notices||[];if(!L.length)return '';
  return `<div class="card" style="margin-top:14px"><h3>${ico('send',15)} ${m('sn_t')}</h3>${L.map(s=>{const od=!s.closed&&s.deadline<todayIso();return `<div class="item"><div class="mf-row"><b>${esc(s.subject)}</b><span class="badge ${s.closed?'ok':od?'fail':'warn'}">${s.closed?m('sn_closed'):od?m('dl_delayed'):m('sn_open')}</span></div><div>${esc(s.message)}</div>
   <small class="mf-sub">${m('sn_to')}: ${m('sn_'+s.to)}${s.names.length?' ('+esc(s.names.join(', '))+')':''} · ${m('sn_deadline')}: ${fd(s.deadline)}</small>${s.replies.map(r=>`<div class="mf-reply"><small>${esc(nm(r.by))} · ${ftime(r.ts)}</small><br>${esc(r.text)}</div>`).join('')}
   <div class="btnrow" style="margin-top:8px">${own&&!s.closed?`<input data-rp="${s.id}" placeholder="${esc(m('sn_reply_ph'))}" style="flex:1;min-width:160px"><button class="btn sm" data-reply="${s.id}">${m('sn_reply')}</button>`:''}${adm?`<button class="btn sm alt" data-snp="${s.id}">${ico('printer',14)}</button>${!s.closed?`<button class="btn sm alt" data-snc="${s.id}">${m('sn_close')}</button>`:''}`:''}</div></div>`}).join('')}</div>`}
function docsPane(a,adm,canDoc){const dd=a.docs;
  return `<div class="card" style="margin-top:14px"><div class="mf-row"><h3 style="margin:0">${m('sec_docs')}</h3>${canDoc?`<button class="btn sm alt" id="a_demodocs">${ico('wand',14)} ${m('demo_docs')}</button>`:''}</div>${(a.deed_id?`<div class="mf-docrow"><span class="badge ok">${ico('check',13)}</span><span style="flex:1"><b>${m('dc_deed')}</b><small class="mf-sub" style="display:block">${m('auto_deed')}</small></span></div>`:'')
   +a.required.filter(k=>k!=='deed'||!a.deed_id).map(k=>{const x=dd[k];return `<div class="mf-docrow"><span class="badge ${x?'ok':'fail'}">${x?ico('check',13):ico('x',13)}</span><span style="flex:1;min-width:150px"><b>${m('dc_'+k)}</b><small class="mf-sub" style="display:block">${x?(x.paper?m('paper'):esc(x.name)):m('required')}</small></span>
    ${x&&!x.paper?`<a class="btn sm alt" target="_blank" rel="noopener" href="/api/mut/apps/${a.id}/file/${k}">${ico('search',14)}</a>`:''}
    ${canDoc?`<button class="btn sm alt" data-up="${k}">${ico('upload',14)} ${x?m('replace'):m('upload')}</button>${adm&&!x?`<button class="btn sm alt" data-paper="${k}">${m('paper_btn')}</button>`:''}${x?`<button class="btn sm alt" data-rm="${k}">${ico('trash',14)}</button>`:''}`:''}</div>`}).join('')}</div>`}
function verPane(a,rec){const cs=a.checks,n=l=>cs.filter(c=>c[1]===l).length;
  const reg=rec?`<div class="mf-two"><div class="card"><h3>${m('vr_registry')}</h3><div class="mf-row" style="margin-bottom:8px"><span class="badge ${rec.verdict}">${t('v_verdict_'+rec.verdict)} · ${nf(rec.score,0)}/${nf(100,0)}</span><span class="badge ${rec.khas==='none'?'pass':'fail'}">${t('kh_'+rec.khas)}</span><span class="badge ${rec.acq==='none'?'pass':rec.acq==='proposed'?'warn':'fail'}">${t('aq_'+rec.acq)}</span></div>
    ${kv([[t('v_owner'),esc(nm(rec.owner))],[t('v_father'),esc(nm(rec.father))],[t('v_khatian'),X(rec.khatian)],[t('v_share'),X(rec.share)]])}
    ${rec.pending.map(p=>`<div class="item"><span class="badge warn">${t('p_'+p.type)}</span> ${X(p.ref)} · ${t('fo_'+p.forum)}</div>`).join('')}
    ${rec.flags.map(f=>`<div class="item"><span class="badge ${f.severity}">${t('sev_'+f.severity)}</span> <b>${t('ft_'+f.type)}</b><ul class="adv" style="margin-top:6px">${f.evidence.map(e=>`<li>${X(nm(e))}</li>`).join('')}</ul></div>`).join('')}</div>
   <div class="card"><h3>${m('vr_chain')}</h3><ul class="tl">${rec.history.map(h=>`<li><b>${t('h_'+h.type)}</b> — ${esc(nm(h.from))} → ${esc(nm(h.to))}<small>${fd(h.date)} · ${t('v_deed')} ${X(h.deed_no)}</small></li>`).join('')}</ul></div></div>
   <div class="card" style="margin-top:14px"><h3>${m('vr_advice')}</h3><ul class="adv">${rec.advice.map(x=>`<li>${X(nm(x))}</li>`).join('')}</ul></div>`:'';
  return `<div class="card" style="margin-top:14px"><div class="mf-row"><h3 style="margin:0">${m('vr_title')}</h3><div class="btnrow"><button class="btn sm alt" data-snk="">${ico('send',14)} ${m('sn_new')}</button><button class="btn sm alt" id="a_pdver">${ico('printer',14)} ${m('vr_print')}</button></div></div>
   <div class="mf-row" style="margin:10px 0;gap:8px;flex-wrap:wrap"><input id="vr_reason" placeholder="${esc(m('vr_reason_ph'))}" style="flex:1;min-width:220px">
    ${a.stage===1?`<button class="btn sm" data-vproceed ${n('fail')?'disabled':''}>${ico('check',14)} ${m('vr_proceed')}</button>`:''}${a.stage===1&&n('fail')?`<span class="mf-sub">${m('vr_waive_first')}</span>`:''}</div>
   <div class="mf-row" style="margin:10px 0"><span class="badge pass">${t('lv_pass')} ${nf(n('pass'),0)}</span><span class="badge warn">${t('lv_warn')} ${nf(n('warn'),0)}</span><span class="badge fail">${t('lv_fail')} ${nf(n('fail'),0)}</span><span class="badge ok">${m('lv_waived')} ${nf(n('waived'),0)}</span></div>${chkHTML(cs,true)}<p class="mf-sub" style="margin:8px 0 0">${m('vr_hint')}</p></div>${reg}`}
async function openApp(id,tab){
  let a;try{a=(await J('/api/mut/apps/'+encodeURIComponent(id))).app}catch(e){return toast(e.message)}
  const rec=await api(`/api/verify/${encodeURIComponent(a.land.ds)}/${encodeURIComponent(a.land.plot_no)}`).catch(()=>null);
  if(DT.id!==id){DT.id=id;DT.tab='ov'}if(tab)DT.tab=tab;const adm=ST.admin;if(DT.tab==='ver'&&!adm)DT.tab='ov';
  const canDoc=['draft','returned'].includes(a.status)||(adm&&!['completed','rejected'].includes(a.status)&&a.stage<=3),own=!adm&&a.uid===(ME&&ME.id);
  const [nc,nt]=nextMsg(a),I=a.inspection,N=a.notice,H=a.hearing,O=a.order,K=a.khatian;
  const fees=`<div class="card"><h3>${m('sec_fee')}</h3>${a.fees.items.map(f=>`<div class="mf-fee"><span>${m('fee_'+f.key)}</span><span>৳${nf(f.amount,0)} <small class="badge ${f.paid?'ok':'warn'}">${f.paid?m('paid'):m('due')}</small></span></div>`).join('')}<div class="mf-fee"><b>${m('due')}</b><b>৳${nf(a.fees.remaining,0)}</b></div>${a.payment?`<small class="mf-sub">${m('pay_receipt')}: ${esc(a.payment.txn)} · ${m('pm_'+a.payment.method)==='pm_'+a.payment.method?esc(a.payment.method):m('pm_'+a.payment.method)} ${esc(a.payment.account||'')}</small>`:''}</div>`;
  const results=[I&&`<div class="card"><h3>${m('r_insp')}</h3>${kv([[m('k_insp'),esc(I.inspector)],[m('k_date'),fd(I.date)],[m('k_possession'),m('po_'+I.possession)],[m('k_area'),nf(I.area_claimed)+' / '+nf(I.area_found)+' · '+(I.area_match?m('k_match'):m('k_nomatch'))],[m('k_neigh'),esc(I.neighbors.join(', '))||'—'],[m('f_note'),esc(I.remarks)]])}</div>`,
   N&&`<div class="card"><h3>${m('r_notice')}</h3>${kv([['No.',esc(N.no)],[m('k_date'),fd(N.issued)],[m('k_deadline'),fd(N.deadline)],[m('k_recip'),N.recipients.map(r=>esc(r.name)+' ('+m('pr_'+r.role)+')').join('<br>')],[m('k_obj'),N.objections.length?N.objections.map(o=>esc(o.by)+': '+esc(o.text)).join('<br>'):'—']])}</div>`,
   H&&`<div class="card"><h3>${m('r_hear')}</h3>${kv([[m('k_date'),fd(H.date)+' '+X(H.time)],[m('ap_hp'),esc(H.place)],[m('ap_ho'),esc(H.outcome)||'—']])}</div>`,
   O&&`<div class="card"><h3>${m('r_order')}</h3>${kv([[m('k_order_no'),X(O.no)],[m('k_date'),fd(O.date)]])}</div>`,
   K&&`<div class="card"><h3>${m('r_kh')}</h3>${kv([[m('k_kh'),X(K.khatian_no)],[m('k_dcr'),X(K.dcr_no)],[m('k_date'),fd(K.issued)]])}</div>`].filter(Boolean).join('');
  const ov=`<div class="mf-two">${etaCard(a)}${fees}</div>${adm?'':`<div class="card" style="margin-top:14px"><h3>${m('sec_checks')}</h3>${chkHTML(a.checks)}</div>`}${spCard(a,adm,own)}${results?`<div class="mf-two">${results}</div>`:''}
   <div class="card" style="margin-top:14px"><h3>${m('sec_app')}</h3>${partyKV(a.applicant)}${kv([[m('f_mobile'),X(a.applicant.mobile)],[m('f_pk'),X(a.prev_khatian||'—')],[m('f_ldt'),X(a.ldt.receipt||'—')],[m('c_channel'),a.channel==='offline'?m('offline'):m('online')]])}</div>`;
  show(head(`${esc(a.id)} <span class="badge ${bc(a.status)}">${m('as_'+a.status)}</span>`,`${m('ty_'+a.basis)} · ${m('plot')} ${X(a.land.label)} · ${esc(nm(a.land.district))} · ${esc(a.applicant.name)}`)+aStepper(a)
  +`<div class="mf-next ${nc}">${nt}</div>
   <div class="btnrow">${a.khatian?`<button class="btn" data-pd="khatian">${ico('download',16)} ${m('dl_khatian')}</button>`:''}<button class="btn sm alt" data-vmap>${ico('map',15)} ${m('lt_viewmap')}</button><button class="btn sm alt" data-pd="app_u">${ico('printer',15)} ${m('dl_app_u')}</button>${adm?`<button class="btn sm alt" data-pd="app_o">${ico('printer',15)} ${m('dl_app_o')}</button>`:''}${I?`<button class="btn sm alt" data-pd="insp">${ico('printer',15)} ${m('dl_insp')}</button>`:''}${N?`<button class="btn sm alt" data-pd="notice">${ico('printer',15)} ${m('dl_notice')}</button>`:''}${O?`<button class="btn sm alt" data-pd="order">${ico('printer',15)} ${m('dl_order')}</button>`:''}</div>`
  +tabsBar([['ov',m('dt_ov')],['land',m('dt_land')],['docs',m('dt_docs')],...(adm?[['ver',m('dt_ver')]]:[]),['tl',m('dt_tl')]],DT.tab)
  +pane('ov',DT.tab,ov)+pane('land',DT.tab,landPane(a.land,a.area_dec,rec))+pane('docs',DT.tab,docsPane(a,adm,canDoc))+(adm?pane('ver',DT.tab,verPane(a,rec)):'')+pane('tl',DT.tab,`<div class="card" style="margin-top:14px">${tlHTML(a.timeline)}</div>`)
  +`<div class="btnrow" style="margin-top:14px">${['draft','returned'].includes(a.status)?`<button class="btn" id="a_submit">${ico('check',16)} ${m('submit_app')}</button>`:''}${own&&a.status==='awaiting_payment'?`<button class="btn" id="a_pay">${ico('card',16)} ${m('pay_now')} · ৳${nf(a.fees.remaining,0)}</button>`:''}</div>`+(adm?adminPanel(a):''));
  const D_=dlg();wireTabs(rec);
  D_.querySelectorAll('[data-vmap]').forEach(b=>b.onclick=()=>viewMap(a.land));
  D_.querySelectorAll('[data-up]').forEach(b=>b.onclick=()=>pickFile(a.id,b.dataset.up));
  D_.querySelectorAll('[data-rm]').forEach(b=>b.onclick=async()=>{try{await J(`/api/mut/apps/${a.id}/doc/${b.dataset.rm}`,null,'DELETE');openApp(a.id)}catch(e){toast(e.message)}});
  D_.querySelectorAll('[data-paper]').forEach(b=>b.onclick=async()=>{try{await J(`/api/mut/apps/${a.id}/doc/${b.dataset.paper}`,{paper:true});openApp(a.id)}catch(e){toast(e.message)}});
  const dd_=D_.querySelector('#a_demodocs');if(dd_)dd_.onclick=()=>{dd_.disabled=true;demoDocs(a)};
  D_.querySelectorAll('[data-pd]').forEach(b=>b.onclick=()=>({khatian:pdKhatian,app_u:x=>pdApp(x,'u'),app_o:x=>pdApp(x,'o'),insp:pdInsp,notice:pdNotice,order:pdOrder})[b.dataset.pd](a));
  const pv=D_.querySelector('#a_pdver');if(pv)pv.onclick=()=>pdVerify(a,rec);
  const sb=D_.querySelector('#a_submit');if(sb)sb.onclick=async()=>{sb.disabled=true;try{await J(`/api/mut/apps/${a.id}/submit`,{});toast(m('done'));await refresh();openApp(a.id)}catch(e){toast(e.message);sb.disabled=false}};
  const pb=D_.querySelector('#a_pay');if(pb)pb.onclick=()=>openPay(a);
  D_.querySelectorAll('[data-reply]').forEach(b=>b.onclick=async()=>{const text=(D_.querySelector(`[data-rp="${b.dataset.reply}"]`)||{}).value||'';if(!text.trim())return;try{await J(`/api/mut/apps/${a.id}/special/${b.dataset.reply}/reply`,{text});toast(m('done'));openApp(a.id)}catch(e){toast(e.message)}});
  D_.querySelectorAll('[data-snp]').forEach(b=>b.onclick=()=>pdSpecial(a,(a.special_notices||[]).find(s=>s.id===b.dataset.snp)));
  D_.querySelectorAll('[data-snc]').forEach(b=>b.onclick=async()=>{try{await J('/api/admin/mut/apps/'+a.id+'/close_special',{sid:b.dataset.snc});openApp(a.id)}catch(e){toast(e.message)}});
  const needR=async()=>{let r=$v('vr_reason')||$v('ap_reason');if(!r)r=await askReason(m('vr_waive'),'');
    if(!r)return '';const v=document.getElementById('vr_reason');if(v&&!v.value)v.value=r;const p=document.getElementById('ap_reason');if(p&&!p.value)p.value=r;return r};
  D_.querySelectorAll('[data-waive]').forEach(b=>b.onclick=async()=>{const reason=await needR();if(!reason)return;b.disabled=true;try{await J('/api/admin/mut/apps/'+a.id+'/waive',{key:b.dataset.waive,reason});toast(m('done'));await refresh();openApp(a.id)}catch(e){toast(e.message);b.disabled=false}});
  D_.querySelectorAll('[data-unwaive]').forEach(b=>b.onclick=async()=>{try{await J('/api/admin/mut/apps/'+a.id+'/unwaive',{key:b.dataset.unwaive});await refresh();openApp(a.id)}catch(e){toast(e.message)}});
  D_.querySelectorAll('[data-vproceed]').forEach(b=>b.onclick=async()=>{const note=$v('vr_reason')||$v('ap_note');b.disabled=true;try{await J('/api/admin/mut/apps/'+a.id+'/verify',{note});toast(m('done'));await refresh();openApp(a.id)}catch(e){toast(e.message);b.disabled=false}});
  D_.querySelectorAll('[data-snk]').forEach(b=>b.onclick=()=>openSpecial(a,b.dataset.snk));
  bindAdmin(a)}

/* ---------- special notice ---------- */
const SNT={tax_paid:[['হালনাগাদ ভূমি উন্নয়ন কর রশিদ জমা দিন','আপনার দাখিলকৃত ভূমি উন্নয়ন কর রশিদের তথ্য যাচাইয়ে অসঙ্গতি পাওয়া গেছে। নির্ধারিত সময়ের মধ্যে হালনাগাদ রশিদ জমা দিন।'],['Submit an up-to-date land development tax receipt','The tax receipt details you provided could not be verified. Please submit a current receipt within the deadline.']],
 docs_complete:[['অসম্পূর্ণ কাগজপত্র জমা দিন','আপনার আবেদনে প্রয়োজনীয় কাগজপত্র অসম্পূর্ণ। অনুগ্রহ করে অনুপস্থিত কাগজ জমা দিন।'],['Submit the missing documents','Required documents are missing from your application. Please upload them.']],
 area_match:[['জমির পরিমাণের অমিল ব্যাখ্যা করুন','আবেদিত জমির পরিমাণ রেকর্ডের সাথে মিলছে না। প্রমাণসহ ব্যাখ্যা দিন।'],['Explain the area mismatch','The claimed area does not match the record. Please explain with evidence.']],
 owner_chain:[['মালিকানার ধারাবাহিকতা প্রমাণ করুন','বিক্রেতার নাম রেকর্ডীয় মালিকের সাথে মিলছে না। পূর্ববর্তী দলিল বা সনদ জমা দিন।'],['Prove the chain of ownership','The seller does not match the record owner. Submit the previous deeds or certificates.']],
 applicant_match:[['আবেদনকারীর পরিচয় নিশ্চিত করুন','আবেদনকারী ও দলিলের গ্রহীতার নাম এক নয়। ব্যাখ্যা বা সংশোধনী দলিল দিন।'],['Confirm the applicant identity','The applicant and the deed buyer differ. Provide an explanation or a correction deed.']],
 no_case:[['চলমান মামলার তথ্য দিন','জমিতে চলমান মামলা শনাক্ত হয়েছে। মামলার অবস্থা ও নথি জমা দিন।'],['Provide details of the pending case','A pending case was found on this land. Submit its status and papers.']]};
function openSpecial(a,key){const tp=SNT[key]||[[`যাচাই সংক্রান্ত নোটিশ`,`আপনার আবেদনের একটি যাচাই বিষয়ে ব্যাখ্যা বা অতিরিক্ত কাগজ প্রয়োজন।`],[`Verification notice`,`A verification item on your application needs an explanation or extra papers.`]],i=lang==='bn'?0:1;
  show(head(m('sn_new'),a.id)+`<div class="fgrid"><label class="f"><span>${m('sn_to')}</span><select id="sn_to">${['applicant','previous_owner','neighbors','other'].map(k=>`<option value="${k}">${m('sn_'+k)}</option>`).join('')}</select></label>
   <label class="f"><span>${m('sn_days')}</span><input id="sn_days" type="number" min="1" max="30" value="7"></label><label class="f full" id="sn_ow" hidden><span>${m('sn_other_n')}</span><input id="sn_other"></label>
   <label class="f full"><span>${m('sn_sub')}</span><input id="sn_sub" value="${esc(tp[i][0])}"></label><label class="f full"><span>${m('sn_msg')}</span><textarea id="sn_msg">${esc(tp[i][1])}</textarea></label></div>
   <div class="btnrow" style="margin-top:14px;justify-content:flex-end"><button class="btn alt" id="sn_back">${m('cancel')}</button><button class="btn" id="sn_go">${ico('send',15)} ${m('sn_send')}</button></div>`);
  $('#sn_to').onchange=()=>{$('#sn_ow').hidden=$v('sn_to')!=='other'};$('#sn_back').onclick=()=>openApp(a.id,'ver');
  $('#sn_go').onclick=async()=>{try{await J('/api/admin/mut/apps/'+a.id+'/special_notice',{to:$v('sn_to'),days:$v('sn_days'),other:$v('sn_other'),subject:$v('sn_sub'),message:$v('sn_msg'),check:key||''});toast(m('sn_sent'));await refresh();openApp(a.id,'ov')}catch(e){toast(e.message)}}}

/* ---------- demo payment ---------- */
const WALLET=['bkash','nagad','rocket'];
function payFields(pm){
  const F=(n,l,ph='',ty='text',lst='')=>`<label class="f"><span>${m(l)}</span><input name="${n}" type="${ty}" ${lst?`list="${lst}"`:''} placeholder="${esc(ph)}" autocomplete="off"></label>`;
  if(WALLET.includes(pm))return `<div class="fgrid">${F('mobile','pay_mobile','01XXXXXXXXX','tel','dl_m')}${F('pin','pay_pin','• • • • •','password')}${F('otp','pay_otp','123456','text')}</div>`;
  if(pm==='card')return `<div class="fgrid">${F('number','pay_card','4242 4242 4242 4242')}${F('name','pay_name')}${F('exp','pay_exp','12/29')}${F('cvv','pay_cvv','123','password')}</div>`;
  return `<div class="fgrid">${F('bank','pay_bank','','text','dl_bank')}${F('account','pay_acc','1234567890123')}${F('holder','pay_holder')}</div>`}
function payDemo(pm){const nmx=(ME&&ME.name&&(ME.name.en||ME.name.bn))||'Demo User',yy=String((new Date().getFullYear()+3)%100).padStart(2,'0'),p=rnd(DP);
  return WALLET.includes(pm)?{mobile:p[4],pin:'12345',otp:'123456'}:pm==='card'?{number:'4242 4242 4242 4242',name:nmx,exp:'12/'+yy,cvv:'123'}:{bank:rnd(BANKS),account:'1234567890123',holder:nmx}}
function openPay(a){let pm='bkash';
  show(head(m('pay_now'),a.id)+`<div class="card">${a.fees.items.map(f=>`<div class="mf-fee"><span>${m('fee_'+f.key)}</span><span>৳${nf(f.amount,0)} · <small class="badge ${f.paid?'ok':'warn'}">${f.paid?m('paid'):m('due')}</small></span></div>`).join('')}<div class="mf-fee"><b>${m('due')}</b><b>৳${nf(a.fees.remaining,0)}</b></div></div>
  <div class="mf-pay" id="pm_list">${PAYM.map(k=>`<label class="${k===pm?'on':''}"><input type="radio" name="pm" value="${k}" ${k===pm?'checked':''}>${m('pm_'+k)}</label>`).join('')}</div>
  <div id="pf"></div><div class="mf-row" style="margin:10px 0"><p class="mf-sub" style="margin:0">${m('pay_demo')}</p><button class="btn sm alt" id="pay_fill">${ico('wand',14)} ${m('pay_fill')}</button></div>
  <div class="btnrow" style="justify-content:flex-end"><button class="btn alt" id="pay_back">${m('cancel')}</button><button class="btn" id="pay_go">${ico('lock',15)} ${m('pay_ok')} ৳${nf(a.fees.remaining,0)}</button></div>`);
  const draw=()=>{$('#pf').innerHTML=payFields(pm);$$('#pm_list label').forEach(l=>l.classList.toggle('on',l.querySelector('input').value===pm))};
  $$('#pm_list input').forEach(r=>r.onchange=()=>{pm=r.value;draw()});draw();
  $('#pay_back').onclick=()=>openApp(a.id);
  $('#pay_fill').onclick=()=>{const d=payDemo(pm);Object.entries(d).forEach(([k,v])=>{const e=document.querySelector(`#pf [name=${k}]`);if(e)e.value=v})};
  $('#pay_go').onclick=async()=>{const fields={};$$('#pf [name]').forEach(e=>fields[e.name]=e.value);const btn=$('#pay_go');btn.disabled=true;btn.innerHTML=`<span class="spin"></span> ${m('pay_processing')}`;
    await new Promise(r=>setTimeout(r,1300));
    try{const r=await J(`/api/mut/apps/${a.id}/pay`,{method:pm,fields});await refresh();const p=r.app.payment;
      show(head(m('pay_success'),r.app.id)+`<div class="mf-next g">${ico('check',16)} ${m('pay_success')} · ৳${nf(p.amount,0)}</div><div class="card">${kv([[m('pay_receipt'),esc(p.txn)],[m('pm_'+p.method).length?m('c_type'):'',m('pm_'+p.method)],[m('pay_acct'),esc(p.account||'—')],[m('k_date'),fd(p.date)],[m('k_kh'),X(r.app.khatian.khatian_no)],[m('k_dcr'),X(r.app.khatian.dcr_no)]])}</div>
       <div class="btnrow" style="margin-top:14px"><button class="btn" id="ps_dl">${ico('download',16)} ${m('dl_khatian')}</button><button class="btn alt" id="ps_view">${m('view_app')}</button></div>`);
      $('#ps_dl').onclick=()=>pdKhatian(r.app);$('#ps_view').onclick=()=>openApp(r.app.id)}
    catch(e){toast(e.message);btn.disabled=false;btn.innerHTML=`${ico('lock',15)} ${m('pay_ok')} ৳${nf(a.fees.remaining,0)}`}}}
const PAYM=['bkash','nagad','rocket','card','bank'];

/* ---------- admin stage panel ---------- */
function adminPanel(a){
  const st=a.stage,s=a.status;if(['completed','rejected'].includes(s))return '';
  const gen=`<div style="margin-top:12px"><input id="ap_reason" placeholder="${esc(m('reason_ph'))}"><div class="btnrow" style="margin-top:8px">${s==='flagged'?`<button class="btn sm alt" data-do="release">${m('a_release')}</button>`:`<button class="btn sm alt" data-do="hold" data-need>${ico('pause',13)} ${m('a_hold')}</button>`}${st>=1&&st<=4&&s!=='flagged'?`<button class="btn sm alt" data-do="return" data-need>${m('a_return')}</button>`:''}<button class="btn sm danger" data-do="reject" data-need>${m('a_reject')}</button><button class="btn sm alt" data-snk="">${ico('send',13)} ${m('sn_new')}</button></div></div>`;
  const F=(id,l,v='',ty='text',lst='')=>`<label class="f"><span>${l}</span><input id="${id}" type="${ty}" ${lst?`list="${lst}"`:''} value="${esc(v)}"></label>`;
  const Tx=(id,l)=>`<label class="f full"><span>${l}</span><textarea id="${id}"></textarea></label>`;
  let h='';const today=todayIso();
  if(s==='flagged'||['draft','returned'].includes(s))h=`<p class="mu">${s==='flagged'?m('on_hold')+': '+esc(a.reason):m('wait_app')}</p>`;
  else if(st===1)h=`<h3>${m('ap_verify_t')}</h3>${Tx('ap_note',m('ap_note'))}<button class="btn sm" data-do="verify">${ico('check',15)} ${m('ap_verify')}</button>`;
  else if(st===2)h=`<h3>${m('ap_insp_t')}</h3><div class="fgrid">${F('ap_insp',m('ap_insp'),'','text','dl_ins')}${F('ap_idate',m('ap_idate'),today,'date')}
    <label class="f"><span>${m('ap_poss')}</span><select id="ap_poss">${['full','partial','none','disputed'].map(k=>`<option value="${k}">${m('po_'+k)}</option>`).join('')}</select></label>${F('ap_found',m('ap_found'),a.area_dec,'number')}
    ${['n','s','e','w'].map(k=>F('ap_'+k,m('f_'+k))).join('')}${Tx('ap_nb',m('ap_nb'))}${Tx('ap_rem',m('ap_rem'))}</div><button class="btn sm" data-do="inspect">${m('ap_insp_go')}</button>`;
  else if(st===3&&!a.notice)h=`<h3>${m('ap_not_t')}</h3><div class="fgrid">${F('ap_days',m('ap_days'),10,'number')}${Tx('ap_extra',m('ap_extra'))}</div><button class="btn sm" data-do="issue_notice">${m('ap_issue')}</button>`;
  else if(st===3)h=`<h3>${m('ap_not_t')}</h3><p class="mu">${m('k_deadline')}: ${fd(a.notice.deadline)}</p><div class="fgrid">${F('ap_oby',m('ap_obj_by'),'','text','dl_n')}${Tx('ap_otext',m('ap_obj_txt'))}</div><div class="btnrow"><button class="btn sm alt" data-do="objection">${m('ap_obj_add')}</button><button class="btn sm" data-do="close_notice">${m('ap_close')}</button></div>`;
  else if(st===4){const hr=a.notice&&a.notice.objections.length,H=a.hearing;
    h=`<h3>${m('ap_hear_t')}</h3>`+(hr&&!H?`<div class="fgrid">${F('ap_hd',m('ap_hd'),'','date')}${F('ap_ht',m('ap_ht'),'11:00','time')}${F('ap_hp',m('ap_hp'),'','text','dl_hp')}</div><button class="btn sm alt" data-do="schedule_hearing">${m('ap_hsched')}</button>`:'')
    +(hr&&H&&H.resolved===null?`<div class="fgrid">${Tx('ap_ho',m('ap_ho'))}<label class="chk full"><input type="checkbox" id="ap_hr"><span>${m('ap_hr')}</span></label></div><button class="btn sm alt" data-do="record_hearing">${m('ap_hrec')}</button>`:'')
    +`<div style="margin-top:12px">${[1,2,3,4].map(i=>`<label class="chk"><input type="checkbox" id="ap_r${i}"><span>${m('rv'+i)}</span></label>`).join('')}${Tx('ap_rr',m('ap_rr'))}</div><div class="btnrow"><button class="btn sm" data-do="review_approve">${m('ap_rapp')}</button><button class="btn sm danger" data-do="review_reject">${m('ap_rrej')}</button></div>`}
  else if(st===5)h=`<h3>${m('ap_ord_t')}</h3><button class="btn sm" data-do="issue_order">${m('ap_ord')}</button>`;
  else if(st===6)h=`<h3>${m('ap_pay_t')}</h3><p class="mu">${m('ap_pay_wait')}</p><div class="fgrid">${F('ap_rc',m('ap_rc'))}</div><button class="btn sm" data-do="mark_paid">${m('ap_markpaid')}</button>`;
  return `<div class="mf-panel"><div class="mf-row"><b>${m('dt_ver')} / ${m('sec_checks')}</b><button class="btn sm alt" id="ap_demo">${ico('wand',13)} ${m('demo_fill')}</button></div>${h}${gen}</div>`}
const PAY={verify:()=>({note:$v('ap_note')}),inspect:()=>({inspector:$v('ap_insp'),date:$v('ap_idate'),possession:$v('ap_poss'),area_found:$v('ap_found'),boundaries:{north:$v('ap_n'),south:$v('ap_s'),east:$v('ap_e'),west:$v('ap_w')},neighbors:$v('ap_nb').split('\n'),remarks:$v('ap_rem')}),
 issue_notice:()=>({days:$v('ap_days'),extra:$v('ap_extra').split('\n')}),objection:()=>({by:$v('ap_oby'),text:$v('ap_otext')}),close_notice:()=>({}),
 schedule_hearing:()=>({date:$v('ap_hd'),time:$v('ap_ht'),place:$v('ap_hp')}),record_hearing:()=>({outcome:$v('ap_ho'),resolved:$c('ap_hr')}),
 review_approve:()=>({decision:'approve',authentic:$c('ap_r1'),chain:$c('ap_r2'),inspection_ok:$c('ap_r3'),legal_ok:$c('ap_r4'),remarks:$v('ap_rr')}),review_reject:()=>({decision:'reject',remarks:$v('ap_rr')}),
 issue_order:()=>({}),mark_paid:()=>({receipt:$v('ap_rc')}),hold:()=>({}),release:()=>({}),return:()=>({}),reject:()=>({})};
function demoPanel(a){const tc=(id,v)=>{const e=document.getElementById(id);if(e)e.checked=v},d=new Date(Date.now()+3*864e5).toISOString().slice(0,10);
  setv('ap_note','নথি ও সরকারি রেকর্ড যাচাই করা হয়েছে।');setv('ap_insp','মো. শফিকুল ইসলাম (কানুনগো)');setv('ap_found',a.area_dec);['রাস্তা','খাল','ফজলুর রহমানের জমি','আব্দুল মান্নানের জমি'].forEach((v,i)=>setv('ap_'+'nsew'[i],v));
  setv('ap_nb','ফজলুর রহমান\nআব্দুল মান্নান');setv('ap_rem','জমি আবেদনকারীর দখলে; সীমানা স্পষ্ট।');setv('ap_days','10');setv('ap_oby',DP[3][0]);setv('ap_otext','জমির পশ্চিম সীমানা নিয়ে আমার আপত্তি আছে।');
  setv('ap_hd',d);setv('ap_hp','এসি (ল্যান্ড) অফিস');setv('ap_ho','আপত্তিকারী আপত্তি প্রত্যাহার করেছেন।');tc('ap_hr',true);[1,2,3,4].forEach(i=>tc('ap_r'+i,true));setv('ap_rr','সব কাগজপত্র ও তদন্ত প্রতিবেদন সঠিক।');setv('ap_rc','RC-'+(100000+Math.floor(Math.random()*899999)))}
function bindAdmin(a){const D_=dlg(),dm=D_.querySelector('#ap_demo');if(dm)dm.onclick=()=>demoPanel(a);
  D_.querySelectorAll('[data-do]').forEach(b=>b.onclick=async()=>{const k=b.dataset.do,reason=$v('ap_reason');
  if(b.hasAttribute('data-need')&&!reason)return toast(m('need_reason'));
  const body=Object.assign({reason},PAY[k]());b.disabled=true;
  try{await J('/api/admin/mut/apps/'+a.id+'/'+(k.startsWith('review')?'review':k),body);toast(m('done'));await refresh();openApp(a.id)}catch(e){toast(e.message);b.disabled=false}})}

/* ---------- Bangla A4 documents (print -> Save as PDF) ---------- */
const kvT=rows=>`<table class="pd-kv">${rows.map(([a,b])=>`<tr><th>${a}</th><td>${b}</td></tr>`).join('')}</table>`;
const tbT=(h,rows)=>`<table><thead><tr>${h.map(x=>`<th>${x}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${r.map(x=>`<td>${x}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
const sec=(n,h,b)=>`<section class="pd-sec"><h3>${B(n)}। ${h}</h3>${b}</section>`;
const landUrl=l=>location.origin+'/#/verify?ds='+encodeURIComponent(l.ds)+'&plot='+encodeURIComponent(l.plot_no);
const areaTxt=dcm=>`${B(dc(dcm))} শতাংশ (${B((dcm/100).toFixed(4))} একর)`;
const DISC='দ্রষ্টব্য: এটি “আমার ভূমি” ডেমো প্ল্যাটফর্মে প্রস্তুতকৃত তথ্যভিত্তিক নথি; প্রকৃত সরকারি দলিল, খতিয়ান বা আদালতগ্রাহ্য প্রমাণ নহে। QR কোডটি জমির অবস্থান ও যাচাই পাতায় নিয়া যায়; বারকোডটি ট্র্যাকিং নম্বর।';
function mfDoc(o){return `<header class="pd-head"><div><img src="/assets/logo.png" alt="" style="height:44px"><h1>${o.title}</h1><p>${o.sub||''}</p></div><div class="pd-qr">${qrSvg(o.url)}<span>জমির QR — স্ক্যান করুন</span></div></header>
 ${o.copy?`<div class="pd-copy">${o.copy}</div>`:''}<div class="pd-meta">${o.rows.map(([k,v])=>`<div><b>${k}</b>${v}</div>`).join('')}</div>${o.body}
 ${o.signs?`<div class="pd-sign s3">${o.signs.map(x=>`<div>${x}</div>`).join('')}</div>`:''}<div class="pd-bar">${barSvg(o.bar)}</div>
 ${o.stub?`<div class="pd-stub"><span>✂ ${o.stub[0]}</span><div class="in"><div>${o.stub[1]}</div><div>${barSvg(o.bar)}</div></div></div>`:''}<div class="pd-disc">${DISC}</div>`}
async function mfPrint(title,html){const pd=$('#printDoc');pd.innerHTML=html;const old=document.title;document.title=title;
  await Promise.all([...pd.querySelectorAll('img')].map(i=>i.complete?1:new Promise(r=>{i.onload=i.onerror=r})));
  try{await document.fonts.ready}catch(e){}
  const back=()=>{document.title=old;removeEventListener('afterprint',back)};addEventListener('afterprint',back);print()}
const schedule=(l,area,bd)=>kvT([['জেলা',E(l.district.bn)],['মৌজা (জেএল নং)',`${E(l.mouza)} (${E(l.jl||'—')})`],['শিট নং',E(l.sheet||'—')],['খতিয়ান নং',E(l.khatian)],['দাগ নং',E(l.label)],['শ্রেণি',E(clsBn((l.parts[0]||{}).maj))],['দাগের মোট জমি',areaTxt(dec(l.area_sqm))],['হস্তান্তরিত/আবেদিত জমি',`<b>${areaTxt(area)}</b>`],
  ['চৌহদ্দি',bd&&(bd.north||bd.south||bd.east||bd.west)?`উত্তরে: ${E(bd.north||'—')}; দক্ষিণে: ${E(bd.south||'—')}; পূর্বে: ${E(bd.east||'—')}; পশ্চিমে: ${E(bd.west||'—')}`:'দাগের মানচিত্রসীমা অনুযায়ী']]);
const pTxt=(p,r)=>`<b>${E(p.name)}</b>, পিতা/স্বামী: ${E(p.father||'—')}, জাতীয় পরিচয়পত্র নং ${B(mask(p.nid))}, ঠিকানা: ${E(p.address||'—')} (অতঃপর “${r}” বলিয়া উল্লিখিত)`;
function pdDeed(d){
  const L=d.land,r=d.registered||{},[A,Bn]=PL[d.type],ty=d.type;
  const val=d.value?`<b>৳ ${B(Number(d.value).toLocaleString('en-IN'))}</b> (কথায়: ${bnWords(d.value)} টাকা মাত্র)`:'';
  const decl=`${A} অঙ্গীকার ও ঘোষণা করিতেছেন যে উক্ত সম্পত্তি ইতঃপূর্বে অন্য কাহারও নিকট বিক্রয়, দান, বন্ধক বা অন্য কোনোভাবে হস্তান্তর করা হয় নাই; ইহা সরকারি খাস বা অর্পিত নহে, অধিগ্রহণের আওতাভুক্ত নহে এবং কোনো মামলা-মোকদ্দমা বা দায়-দেনা নাই।`;
  const cl={sale:[`যেহেতু ${A} নিম্ন তফসিলে বর্ণিত সম্পত্তির বৈধ মালিক ও দখলকার এবং উক্ত সম্পত্তি সম্পূর্ণ নিষ্কণ্টক, নির্ঝঞ্ঝাট ও সর্বপ্রকার দায়মুক্ত।`,`যেহেতু ${A} নিজ প্রয়োজনে উক্ত সম্পত্তি বিক্রয়ে ইচ্ছুক এবং ${Bn} ক্রয়ে সম্মত হওয়ায় অদ্য ${val} মূল্যে ${A} উক্ত সম্পত্তি ${Bn}র অনুকূলে চিরস্থায়ীভাবে বিক্রয় ও হস্তান্তর করিলেন এবং মূল্য সম্পূর্ণ বুঝিয়া পাইলেন।`,decl,`এই দলিল নিবন্ধনের পর ${Bn} নিজ নামে নামজারি (খারিজ) করিয়া ভূমি উন্নয়ন কর পরিশোধপূর্বক উক্ত সম্পত্তি বংশানুক্রমে ভোগদখল করিতে পারিবেন; ${A} বা তাঁহার ওয়ারিশগণের কোনো দাবি থাকিবে না।`,`উপরিউক্ত বর্ণনায় কোনো তথ্য অসত্য প্রমাণিত হইলে ${A} আইনানুযায়ী দায়ী থাকিবেন।`],
   gift:[`${A} তাঁহার নিজ স্বত্ব ও দখলীয় নিম্ন তফসিলের সম্পত্তি স্বেচ্ছায়, সুস্থ মস্তিষ্কে ও কাহারও প্ররোচনা ব্যতীত ${Bn}র অনুকূলে হেবা (দান) করিলেন।`,`${Bn} উক্ত দান গ্রহণ করিলেন এবং সম্পত্তির দখল বুঝিয়া পাইলেন।${val?' দানকৃত সম্পত্তির আনুমানিক মূল্য '+val+'।':''}`,decl,`এই দলিল নিবন্ধনের পর ${Bn} নিজ নামে নামজারি করিয়া ভূমি উন্নয়ন কর পরিশোধপূর্বক সম্পত্তি ভোগদখল করিতে পারিবেন।`,`উপরিউক্ত বর্ণনায় কোনো তথ্য অসত্য প্রমাণিত হইলে ${A} আইনানুযায়ী দায়ী থাকিবেন।`],
   inheritance:[`${A} নিম্ন তফসিলের সম্পত্তির মালিক ছিলেন এবং তাঁহার মৃত্যুর পর ওয়ারিশান সনদ অনুযায়ী উক্ত সম্পত্তি তাঁহার ওয়ারিশগণের মধ্যে বর্তাইয়াছে।`,`ওয়ারিশগণ নিজেদের মধ্যে আপোষে সম্মত হইয়া নিম্ন তফসিলের ${areaTxt(d.area_dec)} জমি ${E(d.buyer.name)}-এর অংশে বণ্টন করিয়া দিলেন এবং তিনি উহা বুঝিয়া পাইলেন।`,`সকল ওয়ারিশ অঙ্গীকার করিতেছেন যে বণ্টিত অংশ সম্পর্কে তাঁহাদের আর কোনো দাবি নাই এবং সম্পত্তি দায়মুক্ত।`,`এই দলিল নিবন্ধনের পর ${Bn} নিজ নামে নামজারি করিয়া ভূমি উন্নয়ন কর পরিশোধপূর্বক ভোগদখল করিতে পারিবেন।`,`উপরিউক্ত বর্ণনায় কোনো তথ্য অসত্য প্রমাণিত হইলে সংশ্লিষ্ট ওয়ারিশগণ আইনানুযায়ী দায়ী থাকিবেন।`]}[ty];
  const body=`<div class="pd-title">${DW[ty]}</div><section class="pd-sec"><p class="pd-decl" style="text-indent:0">অদ্য ${bdate(d.deed_date)} খ্রিষ্টাব্দ তারিখে ${pTxt(d.seller,A)} এবং ${pTxt(d.buyer,Bn)}-এর মধ্যে এই ${DW[ty]} নিম্নলিখিত শর্তে সম্পাদিত হইল:</p><ol class="pd-cl">${cl.map((x,i)=>`<li>${B(i+1)}। ${x}</li>`).join('')}</ol></section>
   ${sec(1,'সম্পত্তির তফসিল',schedule(L,d.area_dec,d.boundaries))}${sec(2,'সাক্ষীগণ',tbT(['ক্রমিক','সাক্ষীর নাম','স্বাক্ষর'],(d.witnesses.length?d.witnesses:['','']).map((w,i)=>[B(i+1),E(w)||'&nbsp;','&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;'])))}`;
  mfPrint(d.id+' '+DW[ty],mfDoc({title:DW[ty],sub:`আমার ভূমি — অনলাইন নিবন্ধিত দলিল (ডেমো)`,url:landUrl(L),bar:d.id,
   rows:[['দলিল নং',`${E(d.deed_no)}`],['দলিলের ধরন',DW[ty]],['রেজিস্ট্রি অফিস',E(r.office?r.office.bn:'—')],['বই / ভলিউম / পৃষ্ঠা',`${B(r.book||'—')} / ${B(r.volume||'—')} / ${B(r.page||'—')}`],['নিবন্ধনের তারিখ',bdate(r.date)],['আবেদন (ট্র্যাকিং) নং',E(d.id)]],
   body,signs:[`${A}র স্বাক্ষর`,`${Bn}র স্বাক্ষর`,`সাব-রেজিস্ট্রার (ডিজিটাল অনুমোদন)<br>${E(r.registrar?r.registrar.bn:'')}`],
   stub:['ভূমি অফিস সংগ্রহ অংশ — এখানে কাটুন',`দলিল নং ${E(d.deed_no)} · ${E(L.district.bn)}<br>মৌজা ${E(L.mouza)} · দাগ ${E(L.label)} · ${areaTxt(d.area_dec)}<br>${A}: ${E(d.seller.name)} → ${Bn}: ${E(d.buyer.name)}`]}))}
const imgBox=(a,k,cls)=>a.docs[k]&&!a.docs[k].paper&&a.docs[k].mime!=='application/pdf'?`<img src="/api/mut/apps/${a.id}/file/${k}" alt="">`:'<span>ছবি/স্বাক্ষর<br>(কাগজে সংযুক্ত)</span>';
const RESBN={pass:'উত্তীর্ণ',warn:'সতর্কতা',fail:'ব্যর্থ',waived:'অব্যাহতিপ্রাপ্ত'};
function pdApp(a,copy){
  const L=a.land,ap=a.applicant,d=a.deed||{},ext=a.external,BA={sale:'ক্রয়',gift:'দান (হেবা)',inheritance:'উত্তরাধিকার (ওয়ারিশ)'}[a.basis]+(a.court_ordered?' + আদালতের আদেশ':'');
  const docs=Object.keys(a.docs).length||a.required.length?a.required.map(k=>[`${m_dc(k)}`,a.docs[k]?(a.docs[k].paper?'কাগজে জমা':'সংযুক্ত (ফাইল)'):'<b>জমা হয় নাই</b>',a.docs[k]&&!a.docs[k].paper?E(a.docs[k].name):'—']):[];
  const body=`<div class="pd-title">নামজারি (খারিজ) আবেদনপত্র</div>
  ${sec(1,'আবেদনকারীর তথ্য',`<div class="pd-two"><div>${kvT([['নাম',E(ap.name)],['পিতা/স্বামী',E(ap.father)],['জাতীয় পরিচয়পত্র নং',B(mask(ap.nid))],['মোবাইল',B(ap.mobile)],['ইমেইল',E(ap.email||'—')],['ঠিকানা',E(ap.address)]])}</div><div class="pd-photo">${imgBox(a,'photo')}</div></div>`)}
  ${sec(2,'নামজারির ভিত্তি ও দলিল',kvT([['নামজারির ভিত্তি',BA],['দলিল নং ও তারিখ',a.deed_id?`${E(d.deed_no)} (${bdate((d.registered||{}).date)})`:`${E(ext.no)} (${bdate(ext.date)}) — কাগজের দলিল`],['পূর্বের খতিয়ান নং',E(a.prev_khatian||'—')],['ভূমি উন্নয়ন কর রশিদ নং / সন',`${E(a.ldt.receipt||'—')} / ${E(a.ldt.paid_till||'—')}`]]))}
  ${sec(3,'ভূমির তফসিল',schedule(L,a.area_dec,d.boundaries))}
  ${sec(4,'সংযুক্ত কাগজপত্র',tbT(['কাগজ','অবস্থা','ফাইল'],[...(a.deed_id?[['নিবন্ধিত দলিলের কপি','সিস্টেম থেকে সংযুক্ত','—']]:[]),...docs]))}
  ${sec(5,'স্বয়ংক্রিয় যাচাই (জমাদানের সময়)',tbT(['যাচাইয়ের বিষয়','ফলাফল'],a.checks.map(([k,l])=>[m_ck(k),RESBN[l]])))}
  ${sec(6,'ফি',tbT(['ফি','টাকা','অবস্থা'],a.fees.items.map(f=>[FEEN[f.key],B(f.amount),f.paid?'পরিশোধিত':'আদেশের পর প্রদেয়']).concat([['মোট',`<b>${B(a.fees.total)}</b>`,'']])))}
  <p class="pd-decl">আমি এই মর্মে ঘোষণা করিতেছি যে এই আবেদনে প্রদত্ত সকল তথ্য ও সংযুক্ত কাগজপত্র সত্য ও সঠিক। কোনো তথ্য অসত্য প্রমাণিত হইলে আবেদন বাতিলসহ আইনানুযায়ী ব্যবস্থা গ্রহণে আমার কোনো আপত্তি থাকিবে না।</p>`;
  mfPrint(a.id+' নামজারি আবেদন',mfDoc({title:'নামজারি আবেদনপত্র',sub:'আমার ভূমি — ই-নামজারি (ডেমো)',url:landUrl(L),bar:a.id,copy:copy==='o'?'অফিস কপি':'আবেদনকারীর কপি',
   rows:[['আবেদন (ট্র্যাকিং) নং',E(a.id)],['আবেদনের তারিখ',bdate((a.submitted||a.created).slice(0,10))],['মাধ্যম',a.channel==='offline'?'অফলাইন (অফিসে)':'অনলাইন'],['জেলা',E(L.district.bn)],['দাগ নং',E(L.label)],['সেবার সময়সীমা',a.sla_due?bdate(a.sla_due):'—']],
   body,signs:[`আবেদনকারীর স্বাক্ষর<br>${a.docs.signature&&!a.docs.signature.paper&&a.docs.signature.mime!=='application/pdf'?`<img class="pd-sigimg" src="/api/mut/apps/${a.id}/file/signature" alt="">`:''}`,'গ্রহণকারী কর্মকর্তার স্বাক্ষর ও সিল','তারিখ'],
   stub:['ভূমি অফিস সংগ্রহ অংশ — এখানে কাটুন',`আবেদন নং ${E(a.id)} · ${E(L.district.bn)} · দাগ ${E(L.label)}<br>আবেদনকারী: ${E(ap.name)} · মোবাইল ${B(ap.mobile)}`]}))}
const m_dc=k=>(MF['dc_'+k]||[k])[0],m_ck=k=>(MF['ck_'+k]||[k])[0];
function pdInsp(a){const I=a.inspection,L=a.land,PO={full:'সম্পূর্ণ দখলে',partial:'আংশিক দখলে',none:'দখলে নাই',disputed:'বিরোধপূর্ণ'};
  mfPrint(a.id+' তদন্ত প্রতিবেদন',mfDoc({title:'মাঠ তদন্ত প্রতিবেদন (তদন্ত / Tauzi)',sub:'নামজারি কেস — ভূমি অফিস',url:landUrl(L),bar:a.id,rows:[['কেস নং',E(a.id)],['তদন্তের তারিখ',bdate(I.date)],['তদন্তকারী',E(I.inspector)]],
   body:sec(1,'ভূমির পরিচিতি',schedule(L,a.area_dec,null))+sec(2,'তদন্তের ফলাফল',kvT([['প্রকৃত দখল',PO[I.possession]],['আবেদিত জমি',areaTxt(I.area_claimed)],['মাঠে প্রাপ্ত জমি',areaTxt(I.area_found)],['পরিমাণ মিল',I.area_match?'মিলিয়াছে (±৫% সীমার মধ্যে)':'<b>মিলে নাই</b>'],['মন্তব্য',E(I.remarks||'—')]]))
   +sec(3,'চৌহদ্দি ও প্রতিবেশী',kvT([['উত্তরে',E(I.boundaries.north||'—')],['দক্ষিণে',E(I.boundaries.south||'—')],['পূর্বে',E(I.boundaries.east||'—')],['পশ্চিমে',E(I.boundaries.west||'—')],['প্রতিবেশী জমির মালিক',E(I.neighbors.join(', ')||'—')]])),
   signs:['তদন্তকারী কর্মকর্তা (কানুনগো/সার্ভেয়ার)','ইউনিয়ন ভূমি সহকারী কর্মকর্তা','সহকারী কমিশনার (ভূমি)']}))}
function pdNotice(a){const N=a.notice,L=a.land,H=a.hearing,RL={previous_owner:'পূর্বের মালিক',neighbor:'প্রতিবেশী জমির মালিক',other:'অন্যান্য'};
  mfPrint(a.id+' নোটিশ',mfDoc({title:'নামজারি সংক্রান্ত নোটিশ (আপত্তি আহ্বান)',sub:'সহকারী কমিশনার (ভূমি)-এর কার্যালয় — ডেমো',url:landUrl(L),bar:N.no,rows:[['নোটিশ নং',E(N.no)],['কেস নং',E(a.id)],['জারির তারিখ',bdate(N.issued)]],
   body:`<section class="pd-sec"><h3>প্রাপক</h3>${tbT(['ক্রমিক','নাম','পরিচয়','ঠিকানা'],N.recipients.map((r,i)=>[B(i+1),E(r.name),RL[r.role],E(r.address||'—')]))}</section>
   <section class="pd-sec"><p class="pd-decl">এতদ্বারা জানানো যাইতেছে যে নিম্ন তফসিলের জমি ${E(a.applicant.name)} (পিতা/স্বামী: ${E(a.applicant.father)})-এর নামে নামজারির আবেদন (কেস নং ${E(a.id)}) গৃহীত হইয়াছে। উক্ত নামজারি সম্পর্কে কাহারও কোনো আপত্তি থাকিলে এই নোটিশ জারির ${B(N.days)} দিনের মধ্যে, অর্থাৎ <b>${bdate(N.deadline)}</b> তারিখের মধ্যে, লিখিতভাবে সংশ্লিষ্ট ভূমি অফিসে দাখিল করিতে হইবে। আপত্তি পাওয়া গেলে শুনানির তারিখ পৃথকভাবে জানানো হইবে; নির্ধারিত সময়ে আপত্তি দাখিল না হইলে নামজারি প্রক্রিয়া চলমান থাকিবে।${H?` <br><b>শুনানি:</b> ${bdate(H.date)}, সময় ${B(H.time)}, স্থান: ${E(H.place)}।`:''}</p></section>
   ${sec(1,'ভূমির তফসিল',schedule(L,a.area_dec,null))}`,signs:['','','সহকারী কমিশনার (ভূমি)<br>(ডিজিটাল স্বাক্ষর)']}))}
function pdOrder(a){const O=a.order,L=a.land,R=a.review||{};
  mfPrint(a.id+' নামজারি আদেশ',mfDoc({title:'নামজারি আদেশ',sub:'সহকারী কমিশনার (ভূমি)-এর কার্যালয় — ডেমো',url:landUrl(L),bar:a.id,rows:[['আদেশ নং',E(O.no)],['আদেশের তারিখ',bdate(O.date)],['কেস নং',E(a.id)]],
   body:sec(1,'আবেদনকারী',kvT([['নাম',E(a.applicant.name)],['পিতা/স্বামী',E(a.applicant.father)],['ঠিকানা',E(a.applicant.address)]]))+sec(2,'ভূমির তফসিল',schedule(L,a.area_dec,(a.deed||{}).boundaries))
   +sec(3,'পর্যবেক্ষণ',`<ol class="pd-cl"><li>১। দলিলের সত্যতা যাচাই করা হইয়াছে।</li><li>২। মালিকানার ধারাবাহিকতা (ওনারশিপ চেইন) ঠিক আছে।</li><li>৩। মাঠ তদন্ত প্রতিবেদন সন্তোষজনক।</li><li>৪। নোটিশ জারির পর আপত্তি ${(a.notice&&a.notice.objections.length)?'শুনানিতে নিষ্পত্তি হইয়াছে':'পাওয়া যায় নাই'}।</li>${R.remarks?`<li>৫। মন্তব্য: ${E(R.remarks)}</li>`:''}</ol>`)
   +`<p class="pd-decl"><b>আদেশ:</b> উপরোক্ত বিবেচনায় নামজারি মঞ্জুর করা হইল। আবেদনকারীর নামে নতুন খতিয়ান সৃজন করা হউক এবং অবশিষ্ট ফি পরিশোধের পর খতিয়ান ও ডিসিআর প্রদান করা হউক।</p>`,signs:['','','সহকারী কমিশনার (ভূমি)<br>(ডিজিটাল স্বাক্ষর)']}))}
function pdKhatian(a){const K=a.khatian,L=a.land,ap=a.applicant,O=a.order||{},P=a.payment||{},bds=(a.deed||{}).boundaries||{},I=a.inspection;
  const full=Math.abs(dec(L.area_sqm)-a.area_dec)<0.01;
  const b1=`<div class="pd-title">নামজারি খতিয়ান</div>`+sec(1,'ভূমির অবস্থান',kvT([['জেলা',E(L.district.bn)],['মৌজা (জেএল নং)',`${E(L.mouza)} (${E(L.jl||'—')})`],['শিট নং',E(L.sheet||'—')],['নতুন খতিয়ান নং',`<b>${B(K.khatian_no)}</b>`],['পূর্বের খতিয়ান নং',E(a.prev_khatian||L.khatian)]]))
   +sec(2,'মালিকের বিবরণ',kvT([['মালিকের নাম',`<b>${E(ap.name)}</b>`],['পিতা/স্বামী',E(ap.father)],['জাতীয় পরিচয়পত্র নং',B(mask(ap.nid))],['ঠিকানা',E(ap.address)],['মালিকানার অংশ',full?'১/১ (সম্পূর্ণ)':`${areaTxt(a.area_dec)} (অবশিষ্ট জমি পূর্বের মালিকের নামে বহাল)`]]))
   +sec(3,'জমির বিবরণ',tbT(['দাগ নং','শ্রেণি','দাগের মোট জমি (শতাংশ)','এই খতিয়ানে জমি (শতাংশ)','একর'],[[E(L.label),E(clsBn((L.parts[0]||{}).maj)),B(dc(dec(L.area_sqm))),`<b>${B(dc(a.area_dec))}</b>`,B((a.area_dec/100).toFixed(4))]]))
   +sec(4,'চৌহদ্দি',kvT([['উত্তরে',E((I&&I.boundaries.north)||bds.north||'—')],['দক্ষিণে',E((I&&I.boundaries.south)||bds.south||'—')],['পূর্বে',E((I&&I.boundaries.east)||bds.east||'—')],['পশ্চিমে',E((I&&I.boundaries.west)||bds.west||'—')]]))
   +sec(5,'নামজারির সূত্র',kvT([['নামজারি কেস নং',E(a.id)],['নামজারির ভিত্তি',{sale:'ক্রয় (বিক্রয় দলিল)',gift:'দান (হেবা দলিল)',inheritance:'উত্তরাধিকার (ওয়ারিশ)'}[a.basis]],['দলিল নং',a.deed_id?E((a.deed||{}).deed_no):E((a.external||{}).no)],['নামজারি আদেশ নং ও তারিখ',`${E(O.no)} — ${bdate(O.date)}`],['খতিয়ান জারির তারিখ',bdate(K.issued)],['ডিসিআর নং',E(K.dcr_no)]]));
  const kh=mfDoc({title:'নামজারি খতিয়ান (সৃজিত রেকর্ড)',sub:'আমার ভূমি — ডিজিটাল খতিয়ান (ডেমো)',url:landUrl(L),bar:a.id,rows:[['খতিয়ান নং',B(K.khatian_no)],['কেস নং',E(a.id)],['জারির তারিখ',bdate(K.issued)]],body:b1,signs:['','<span class="pd-seal">ডিজিটাল সিল<br>এসি (ল্যান্ড)</span>','সহকারী কমিশনার (ভূমি)<br>(ডিজিটাল স্বাক্ষর)']});
  const dcr=mfDoc({title:'ডিসিআর — ডুপ্লিকেট ক্যাশ রিসিপ্ট',sub:'নামজারি ফি পরিশোধের রশিদ — ভবিষ্যতে ভূমি উন্নয়ন কর পরিশোধে সংরক্ষণ করুন',url:landUrl(L),bar:K.dcr_no,rows:[['ডিসিআর নং',E(K.dcr_no)],['খতিয়ান নং',B(K.khatian_no)],['তারিখ',bdate(P.date||K.issued)]],
   body:sec(1,'পরিশোধের বিবরণ',kvT([['মালিক',E(ap.name)],['দাগ নং',E(L.label)],['পরিশোধ পদ্ধতি',E({bkash:'বিকাশ',nagad:'নগদ',rocket:'রকেট',card:'কার্ড',bank:'ব্যাংক',office:'অফিসে নগদ',demo:'অনলাইন',cash:'নগদ'}[P.method]||P.method||'—')],['অ্যাকাউন্ট',E(P.account||'—')],['লেনদেন / রশিদ নং',E(P.txn||'—')]]))
   +sec(2,'ফি বিবরণী',tbT(['ফি','টাকা','অবস্থা'],a.fees.items.map(f=>[FEEN[f.key],B(f.amount),'পরিশোধিত']).concat([['মোট',`<b>${B(a.fees.total)}</b>`,'']])))
   +`<p class="pd-decl">উপরোক্ত ফি সম্পূর্ণ পরিশোধিত হইয়াছে। ভবিষ্যতে ভূমি উন্নয়ন কর পরিশোধের সময় এই ডিসিআর ও খতিয়ান নম্বর উল্লেখ করিতে হইবে।</p>`,signs:['','','আদায়কারী কর্মকর্তা (ডিজিটাল)']});
  mfPrint(a.id+' নামজারি খতিয়ান ও ডিসিআর',kh+'<div class="pd-pg"></div>'+dcr)}
function pdVerify(a,rec){const L=a.land,cs=a.checks,hb=x=>(T['h_'+x]||[x])[0];
  const body=sec(1,'ভূমির পরিচিতি',schedule(L,a.area_dec,null))
   +sec(2,'স্বয়ংক্রিয় যাচাইয়ের ফলাফল',tbT(['যাচাইয়ের বিষয়','ফলাফল','তথ্য'],cs.map(([k,l,i])=>[m_ck(k),`<b>${RESBN[l]}</b>`,E(i||'—')])))
   +(Object.keys(a.waivers||{}).length?sec(3,'অব্যাহতি',tbT(['বিষয়','কারণ','তারিখ'],Object.entries(a.waivers).map(([k,w])=>[m_ck(k),E(w.reason),bdate(w.date)]))):'')
   +(rec?sec(4,'রেজিস্ট্রি ঝুঁকি বিশ্লেষণ',kvT([['ঝুঁকি স্কোর',`<b>${B(rec.score)} / ${B(100)}</b> — ${(T['v_verdict_'+rec.verdict]||[''])[0]}`],['খাস/অর্পিত',(T['kh_'+rec.khas]||[''])[0]],['অধিগ্রহণ',(T['aq_'+rec.acq]||[''])[0]],['চলমান মামলা',B(rec.pending.length)],['জালিয়াতি সংকেত',rec.flags.length?rec.flags.map(f=>(T['ft_'+f.type]||[f.type])[0]).join(', '):'নাই']]))
    +sec(5,'মালিকানার ধারাবাহিক ইতিহাস',tbT(['তারিখ','ধরন','হস্তান্তরকারী → গ্রহীতা','দলিল নং'],rec.history.map(h=>[bdate(h.date),hb(h.type),`${E(h.from.bn)} → ${E(h.to.bn)}`,E(h.deed_no)]))):'');
  mfPrint(a.id+' যাচাই প্রতিবেদন',mfDoc({title:'পূর্ণ যাচাই প্রতিবেদন',sub:'নামজারি কেস — কর্মকর্তার জন্য (ডেমো)',url:landUrl(L),bar:a.id,rows:[['কেস নং',E(a.id)],['প্রস্তুতের তারিখ',bdate(todayIso())],['প্রস্তুতকারী',E(ME?nm(ME.name):'—')]],body,signs:['যাচাইকারী কর্মকর্তা','ইউনিয়ন ভূমি সহকারী কর্মকর্তা','সহকারী কমিশনার (ভূমি)']}))}
function pdSpecial(a,s){if(!s)return;const L=a.land,TO={applicant:'আবেদনকারী',previous_owner:'পূর্বের মালিক',neighbors:'প্রতিবেশী',other:'অন্যান্য'};
  mfPrint(s.id+' বিশেষ নোটিশ',mfDoc({title:'বিশেষ নোটিশ',sub:'সহকারী কমিশনার (ভূমি)-এর কার্যালয় — ডেমো',url:landUrl(L),bar:s.id,rows:[['নোটিশ নং',E(s.id)],['কেস নং',E(a.id)],['জারির তারিখ',bdate(s.issued)]],
   body:`<section class="pd-sec"><h3>প্রাপক</h3>${kvT([['পরিচয়',TO[s.to]],['নাম',E(s.names.join(', ')||'—')],['সাড়া দেওয়ার শেষ তারিখ',`<b>${bdate(s.deadline)}</b>`]])}</section><section class="pd-sec"><h3>বিষয়: ${E(s.subject)}</h3><p class="pd-decl">${E(s.message)}</p><p class="pd-decl">নির্ধারিত সময়ের মধ্যে সাড়া না দিলে বিদ্যমান তথ্যের ভিত্তিতে নামজারি প্রক্রিয়া এগিয়ে নেওয়া হইবে অথবা আবেদন বাতিল হইতে পারে।</p></section>${sec(1,'ভূমির তফসিল',schedule(L,a.area_dec,null))}`,signs:['','','সহকারী কমিশনার (ভূমি)<br>(ডিজিটাল স্বাক্ষর)']}))}

/* ---------- notification bell ---------- */
let NN={rows:[],unread:0,max:0,first:true,uid:null};
(function(){const w=document.createElement('div');w.className='uwrap';w.id='bellWrap';w.hidden=true;
  w.innerHTML=`<button class="icon" id="bellBtn" aria-label="${m('bell')}">${ico('bell',18)}<span class="mf-dot" id="bellDot" hidden></span></button><div class="umenu mf-notes" id="bellMenu" hidden></div>`;
  const tools=document.querySelector('.tools');tools.insertBefore(w,document.getElementById('themeBtn'));
  $('#bellBtn').onclick=e=>{e.stopPropagation();const mn=$('#bellMenu');mn.hidden=!mn.hidden;drawBell()};
  document.addEventListener('click',e=>{if(!e.target.closest('#bellWrap'))$('#bellMenu').hidden=true});
  $('#bellMenu').onclick=async e=>{if(e.target.closest('#nAll')){await J('/api/mut/notifications/read',{}).catch(()=>{});loadNotes();return}
    const it=e.target.closest('[data-n]');if(!it)return;const n=NN.rows.find(x=>String(x.id)===it.dataset.n);if(!n)return;
    await J('/api/mut/notifications/read',{id:n.id}).catch(()=>{});$('#bellMenu').hidden=true;loadNotes();
    const h='#/'+(role==='admin'?'mutadmin':'mutation')+'?open='+encodeURIComponent(n.ref);if(location.hash===h)route(false);else location.hash=h};
  setInterval(()=>{if(ME)loadNotes()},20000)})();
async function loadNotes(){if(!ME)return;try{const j=await J('/api/mut/notifications'),mx=Math.max(0,...j.notes.map(n=>n.id));
  if(!NN.first&&mx>NN.max){const n=j.notes.find(x=>x.id===mx);if(n)toast((lang==='bn'?n.bn:n.en).slice(0,140))}
  NN.first=false;NN.max=Math.max(NN.max,mx);NN.rows=j.notes;NN.unread=j.unread;drawBell()}catch(e){}}
function drawBell(){const d=$('#bellDot');if(d)d.hidden=!NN.unread;const mn=$('#bellMenu');if(!mn||mn.hidden)return;
  mn.innerHTML=`<div class="mf-row" style="padding:4px 8px"><b>${m('bell')}</b>${NN.unread?`<button class="btn sm alt" id="nAll">${m('markall')}</button>`:''}</div>`+(NN.rows.map(n=>`<div class="mf-n ${n.read?'':'u'}" data-n="${n.id}">${esc(lang==='bn'?n.bn:n.en)}<small>${esc(n.ref)} · ${ftime(n.ts)}</small></div>`).join('')||`<div class="empty">${m('nonotes')}</div>`)}
if(ME){renderNav();renderUser()}
})();
"""
