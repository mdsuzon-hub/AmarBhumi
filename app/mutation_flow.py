#!/usr/bin/env python3
"""
mutation_flow.py  —  Deed registration + full e-Mutation workflow for land_portal.py
====================================================================================
Install (in land_portal.py, right before `if __name__ == "__main__":`):

    import mutation_flow
    PAGE = mutation_flow.install(app, PAGE, dict(data_dir=DATA_DIR, record=record, err=err, bn_digits=bn_digits))

Flow
    Step 0  Deed registration   user requests online -> admin (sub-registrar) verifies/approves
                                -> user downloads the registered deed (Bangla A4, QR + barcode + land-office stub)
    Step 1  Mutation application user applies with documents (online) / admin takes it offline (paper)
                                -> applicant copy + office copy (A4 form, barcode)
    Step 2  Verification        automated + officer checks (docs, khas/vested, acquisition, case, tax ...)
    Step 3  Field inspection    Kanungo / surveyor report (possession, boundaries, neighbours, area)
    Step 4  Notice              notice to previous owner + neighbours, objection period
    Step 5  Hearing + AC review hearing if objections, then AC Land review (4-point checklist)
    Step 6  Mutation order      AC Land issues the order  -> applicant is notified to pay
    Step 7  Final fee           online (demo gateway) or office receipt -> khatian + DCR generated
    Every step sends an in-app notification (bell icon). Every document is a structured Bangla A4 print/PDF.

Storage:  data/mutation_flow.json   (records)   data/mutation_files/<app-id>/<doc>.<ext>   (uploads)
Routes :  /api/mut/*  (any signed-in user)      /api/admin/mut/*  (admin only, protected by the app's guard)
"""
import base64
import json
import os
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


def install(app, page, ctx):
    data_dir = ctx["data_dir"]
    store = os.path.join(data_dir, "mutation_flow.json")
    files_dir = os.path.join(data_dir, "mutation_files")
    os.makedirs(files_dir, exist_ok=True)
    lock = threading.RLock()
    db = {"seq": {}, "deeds": {}, "apps": {}, "notes": []}
    err = ctx["err"]
    bd = ctx["bn_digits"]
    record = ctx["record"]

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
        return session.get("u") or {}

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
        fraud = any(f["type"] in ("fake_deed", "double_sale") for f in rec["flags"])
        return [["not_khas", "pass" if rec["khas"] == "none" else "fail"],
                ["no_acq", {"none": "pass", "proposed": "warn", "notified": "fail"}[rec["acq"]]],
                ["no_case", "warn" if rec["pending"] else "pass"],
                ["no_fraud", "fail" if fraud else "warn" if rec["flags"] else "pass"]]

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

    @app.route("/api/mut/deeds", methods=["GET", "POST"])
    def mut_deeds():
        if request.method == "GET":
            rows = [deed_view(d) for d in db["deeds"].values() if mine(d)]
            rows.sort(key=lambda d: d["created"], reverse=True)
            return jsonify(deeds=rows)
        try:
            f = deed_fields(request.get_json(silent=True) or {})
        except LookupError as e:
            return err(str(e), 404)
        except ValueError as e:
            return err(str(e))
        with lock:
            did = "DR-%d-%05d" % (date.today().year, seq("deed"))
            adm = is_admin()
            d = dict(f, id=did, uid=None if adm else user().get("id"), channel="offline" if adm else "online",
                     status="submitted", created=now(), registered=None, reason="", timeline=[ev("submitted")])
            db["deeds"][did] = d
            notify("admin", did, "deed", "নতুন দলিল নিবন্ধন আবেদন: %s" % did, "New deed registration request: %s" % did)
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
        except LookupError as e:
            return err(str(e), 404)
        except ValueError as e:
            return err(str(e))
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
            if d["status"] != "submitted":
                return err("only requests under review can be processed / শুধু যাচাইাধীন আবেদন প্রক্রিয়া করা যায়", 409)
            reason = str(b.get("reason") or "").strip()[:300]
            if act == "approve":
                rec = get_rec(d["land"]["ds"], d["land"]["plot_no"])
                if rec and (rec["khas"] != "none" or rec["acq"] == "notified"):
                    return err("blocked: khas/vested or acquisition notice / খাস, অর্পিত বা অধিগ্রহণভুক্ত জমি", 409)
                n, y = seq("deedno"), date.today().year
                dist = d["land"]["district"]
                d["deed_no"] = "%d/%d" % (n, y)
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
                    return err("reason required / কারণ লিখুন")
                d["status"] = "returned" if act == "return" else "rejected"
                d["reason"] = reason
                d["timeline"].append(ev("returned" if act == "return" else "rejected", reason))
                notify(d.get("uid"), did, "deed",
                       ("আপনার দলিল আবেদন সংশোধনের জন্য ফেরত: " if act == "return" else "আপনার দলিল আবেদন প্রত্যাখ্যাত: ") + reason,
                       ("Your deed request was returned: " if act == "return" else "Your deed request was rejected: ") + reason)
            else:
                return err("unknown action")
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
        d = db["deeds"].get(a.get("deed_id") or "")
        miss = [k for k in required_docs(a) if k not in a["docs"]]
        c = [["docs_complete", "pass" if not miss else "fail"],
             ["deed_registered", "pass" if (d and d["status"] == "approved") or a.get("external") else "fail"],
             ["area_match", "pass" if 0 < a["area_dec"] <= a["land"]["area_sqm"] / 40.4686 + 0.01 else "fail"]]
        c += pre_checks(rec)
        h = zlib.crc32(("tax:%s:%s" % (a["land"]["ds"], a["land"]["plot_no"])).encode()) % 10
        c.append(["tax_paid", "fail" if not a["ldt"]["receipt"] else "pass" if h < 8 else "warn"])
        c.append(["nid_ok", "pass" if re.fullmatch(r"\d{10}|\d{13}|\d{17}", a["applicant"]["nid"]) else "fail"])
        seller = (d or {}).get("seller") or (a.get("external") or {}).get("seller") or {}
        buyer = (d or {}).get("buyer") or {}
        same = lambda x, y: str(x).strip().lower() == str(y).strip().lower()
        c.append(["applicant_match", "pass" if (not buyer or same(buyer.get("name", ""), a["applicant"]["name"])) else "warn"])
        owner = rec["owner"] if rec else None
        chain = a["basis"] == "inheritance" or not owner or not seller.get("name") or any(same(seller["name"], owner[k]) for k in ("bn", "en"))
        c.append(["owner_chain", "pass" if chain else "warn"])
        return c

    def fee_block(a):
        paid = a.get("fees_paid") or {}
        return {"items": [{"key": k, "amount": v, "paid": k in paid} for k, v in FEE_ITEMS],
                "total": sum(v for _, v in FEE_ITEMS), "remaining": sum(v for k, v in FEE_ITEMS if k not in paid)}

    def app_view(a, brief=False):
        v = dict(a)
        v["required"] = required_docs(a)
        v["fees"] = fee_block(a)
        v["days_left"] = (date.fromisoformat(a["sla_due"]) - date.today()).days if a.get("sla_due") else None
        if not brief:
            v["checks"] = app_checks(a, get_rec(a["land"]["ds"], a["land"]["plot_no"]))
            d = db["deeds"].get(a.get("deed_id") or "")
            v["deed"] = deed_view(d) if d else None
        return v

    @app.route("/api/mut/apps", methods=["GET", "POST"])
    def mut_apps():
        if request.method == "GET":
            rows = [app_view(a, True) for a in db["apps"].values() if mine(a)]
            rows.sort(key=lambda a: a["created"], reverse=True)
            return jsonify(apps=rows)
        b = request.get_json(silent=True) or {}
        adm, u = is_admin(), user()
        try:
            ap = applicant(b)
        except ValueError as e:
            return err(str(e))
        did, d, ext = str(b.get("deed_id") or ""), None, None
        if did:
            d = db["deeds"].get(did)
            if not d or d["status"] != "approved":
                return err("a registered deed is required / নিবন্ধিত দলিল ছাড়া নামজারি শুরু করা যায় না", 422)
            if not adm and d.get("uid") != u.get("id"):
                return err("forbidden", 403)
            if any(x.get("deed_id") == did and x["status"] != "rejected" for x in db["apps"].values()):
                return err("an application already exists for this deed / এই দলিলের জন্য আবেদন আছে", 409)
            basis, area, rec = d["type"], d["area_dec"], get_rec(d["land"]["ds"], d["land"]["plot_no"])
        else:
            if not adm:
                return err("a registered deed is required / নিবন্ধিত দলিল ছাড়া নামজারি শুরু করা যায় না", 422)
            e = b.get("external") if isinstance(b.get("external"), dict) else {}
            rec = get_rec(e.get("ds"), e.get("plot_no"))
            if not rec:
                return err("plot not found / দাগ পাওয়া যায়নি", 404)
            if e.get("type") not in DEED_TYPES or not str(e.get("deed_no") or "").strip():
                return err("deed type and deed no. required / দলিলের ধরন ও নম্বর দিন")
            try:
                area = round(float(e.get("area_dec")), 4)
                dd = date.fromisoformat(str(e.get("deed_date") or "")[:10])
            except (TypeError, ValueError):
                return err("area and deed date required / পরিমাণ ও দলিলের তারিখ দিন")
            if area <= 0 or area > rec["area_sqm"] / 40.4686 + 0.01:
                return err("area out of range / পরিমাণ সীমার বাইরে")
            try:
                sel = party(e.get("seller"))
            except ValueError as ex:
                return err(str(ex))
            basis = e["type"]
            ext = {"no": str(e["deed_no"]).strip()[:30], "date": dd.isoformat(), "type": basis, "seller": sel, "area_dec": area}
        if not rec:
            return err("plot not found / দাগ পাওয়া যায়নি", 404)
        with lock:
            aid = "MA-%d-%05d" % (date.today().year, seq("app"))
            a = {"id": aid, "uid": (d.get("uid") if d else None) if adm else u.get("id"),
                 "channel": "offline" if adm else "online", "deed_id": did or None, "external": ext, "basis": basis,
                 "court_ordered": bool(b.get("court_ordered")), "land": snap(rec), "area_dec": area, "applicant": ap,
                 "prev_khatian": str(b.get("prev_khatian") or "").strip()[:30],
                 "ldt": {"receipt": str(b.get("ldt_receipt") or "").strip()[:30], "paid_till": str(b.get("ldt_paid_till") or "").strip()[:9]},
                 "docs": {}, "stage": 0, "status": "draft", "created": now(), "submitted": None, "sla_due": None,
                 "reason": "", "timeline": [ev("created")]}
            db["apps"][aid] = a
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

    @app.route("/api/mut/apps/<aid>/submit", methods=["POST"])
    def mut_submit(aid):
        a = db["apps"].get(aid)
        if not a:
            return err("application not found / আবেদন পাওয়া যায়নি", 404)
        if not mine(a):
            return err("forbidden", 403)
        with lock:
            if a["status"] not in ("draft", "returned"):
                return err("already submitted / ইতোমধ্যে জমা দেওয়া হয়েছে", 409)
            miss = [k for k in required_docs(a) if k not in a["docs"]]
            if miss:
                return jsonify(error="documents missing / কাগজপত্র অসম্পূর্ণ", missing=miss), 422
            first = a["submitted"] is None
            a.update(stage=1, status="ok", reason="", submitted=a["submitted"] or now(),
                     sla_due=(date.today() + timedelta(days=28)).isoformat())
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
            save()
        return jsonify(ok=True, app=app_view(a))

    def complete_payment(a, method, txn):
        paid = dict(a.get("fees_paid") or {})
        rem = [(k, v) for k, v in FEE_ITEMS if k not in paid]
        for k, v in rem:
            paid[k] = {"amount": v, "txn": txn, "date": today(), "method": method}
        a["fees_paid"] = paid
        a["payment"] = {"amount": sum(v for _, v in rem), "method": method, "txn": txn, "date": today()}
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
        method = (request.get_json(silent=True) or {}).get("method")
        if method not in PAY_METHODS:
            return err("choose a payment method / পেমেন্ট পদ্ধতি বেছে নিন")
        with lock:
            if a["stage"] != 6 or a["status"] != "awaiting_payment":
                return err("payment is not due / এখন ফি পরিশোধের সময় নয়", 409)
            complete_payment(a, method, "TXN%s%04d" % (datetime.now().strftime("%y%m%d%H%M%S"), zlib.crc32(aid.encode()) % 10000))
            save()
        return jsonify(ok=True, app=app_view(a))

    @app.route("/api/mut/track/<xid>")
    def mut_track(xid):
        xid = xid.strip().upper()
        o = db["apps"].get(xid) or db["deeds"].get(xid)
        if not o:
            return err("not found / পাওয়া যায়নি", 404)
        return jsonify(kind="app" if xid in db["apps"] else "deed", id=o["id"], status=o["status"], stage=o.get("stage"),
                       land={"label": o["land"]["label"], "district": o["land"]["district"], "mouza": o["land"]["mouza"]},
                       timeline=[{"ts": e["ts"], "key": e["key"]} for e in o["timeline"]], mine=mine(o))

    # ------------------------------------------------------------------ admin processing
    @app.route("/api/admin/mut/apps/<aid>/<act>", methods=["POST"])
    def mut_app_act(aid, act):
        b = request.get_json(silent=True) or {}
        wrong = lambda: err("not allowed at this stage / এই ধাপে সম্ভব নয়", 409)
        with lock:
            a = db["apps"].get(aid)
            if not a:
                return err("application not found / আবেদন পাওয়া যায়নি", 404)
            if a["status"] in ("completed", "rejected"):
                return err("case is closed / কেস বন্ধ", 409)
            st, status = a["stage"], a["status"]
            reason = str(b.get("reason") or "").strip()[:300]
            note = str(b.get("note") or "").strip()[:300]
            if act == "hold":
                if not reason:
                    return err("reason required / কারণ লিখুন")
                if status == "flagged":
                    return err("already on hold", 409)
                a["held_from"], a["status"], a["reason"] = status, "flagged", reason
                a["timeline"].append(ev("held", reason))
                tell(a, "আপনার আবেদন সাময়িক স্থগিত: " + reason, "Your application is on hold: " + reason)
            elif act == "release":
                if status != "flagged":
                    return err("not on hold", 409)
                a["status"], a["reason"] = a.pop("held_from", "ok"), ""
                a["timeline"].append(ev("released"))
                tell(a, "স্থগিতাদেশ প্রত্যাহার — প্রক্রিয়া চলছে।", "Hold lifted — processing continues.")
            elif act == "reject":
                if not reason:
                    return err("reason required / কারণ লিখুন")
                a["status"], a["reason"] = "rejected", reason
                a["timeline"].append(ev("rejected", reason))
                tell(a, "আপনার নামজারি আবেদন প্রত্যাখ্যাত: " + reason, "Your mutation application was rejected: " + reason)
            elif act == "return":
                if st < 1 or st > 4:
                    return wrong()
                if not reason:
                    return err("reason required / কারণ লিখুন")
                a["status"], a["stage"], a["reason"] = "returned", 0, reason
                a["timeline"].append(ev("returned", reason))
                tell(a, "আবেদন সংশোধনের জন্য ফেরত: " + reason + " — সংশোধন করে পুনরায় জমা দিন।",
                     "Application returned for correction: " + reason + " — fix and resubmit.")
            elif status == "flagged":
                return err("on hold — release first / স্থগিত — আগে ছাড় দিন", 409)
            elif status in ("draft", "returned"):
                return err("waiting for the applicant / আবেদনকারীর অপেক্ষায়", 409)
            elif act == "verify":
                if st != 1:
                    return wrong()
                checks = app_checks(a, get_rec(a["land"]["ds"], a["land"]["plot_no"]))
                if any(v == "fail" for _, v in checks):
                    return err("failed checks — return or reject / ব্যর্থ যাচাই — ফেরত বা প্রত্যাখ্যান করুন", 409)
                a["verification"] = {"by": user().get("name"), "date": today(), "note": note, "checks": checks}
                a["stage"] = 2
                a["timeline"] += [ev("verified", note), ev("inspection")]
                tell(a, "নথি ও রেকর্ড যাচাই সম্পন্ন — মাঠ পর্যায়ে তদন্ত হবে।", "Documents and records verified — field inspection will follow.")
            elif act == "inspect":
                if st != 2:
                    return wrong()
                insp = str(b.get("inspector") or "").strip()[:80]
                poss = b.get("possession")
                if not insp or poss not in ("full", "partial", "none", "disputed"):
                    return err("inspector and possession required / তদন্তকারী ও দখলের অবস্থা দিন")
                try:
                    found = round(float(b.get("area_found")), 4)
                except (TypeError, ValueError):
                    return err("area found required / মাঠে প্রাপ্ত পরিমাণ দিন")
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
                    return wrong()
                try:
                    days = max(7, min(60, int(b.get("days") or 15)))
                except (TypeError, ValueError):
                    days = 15
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
                tell(a, "নোটিশ জারি হয়েছে — আপত্তি দাখিলের শেষ তারিখ %s।" % bd(dl.isoformat()),
                     "Notice issued — objection deadline %s." % dl.isoformat())
            elif act == "objection":
                n = a.get("notice")
                by, text = str(b.get("by") or "").strip()[:80], str(b.get("text") or "").strip()[:500]
                if st != 3 or not n or n["closed"]:
                    return wrong()
                if not by or not text:
                    return err("objector and statement required / আপত্তিকারী ও বক্তব্য দিন")
                n["objections"].append({"by": by, "text": text, "date": today()})
                a["timeline"].append(ev("objection", "%s: %s" % (by, text)))
                tell(a, "আপনার আবেদনের বিরুদ্ধে আপত্তি দাখিল হয়েছে — শুনানির তারিখ জানানো হবে।",
                     "An objection was filed against your application — the hearing date will be notified.")
            elif act == "close_notice":
                n = a.get("notice")
                if st != 3 or not n or n["closed"]:
                    return wrong()
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
                    return wrong()
                try:
                    hd = date.fromisoformat(str(b.get("date") or "")[:10])
                except ValueError:
                    return err("hearing date required / শুনানির তারিখ দিন")
                if hd < date.today():
                    return err("hearing date cannot be in the past / শুনানির তারিখ অতীত হতে পারে না")
                place = str(b.get("place") or "").strip()[:120] or "%s এসি (ল্যান্ড) অফিস" % a["land"]["district"]["bn"]
                tm = str(b.get("time") or "11:00")[:5]
                a["hearing"] = {"date": hd.isoformat(), "time": tm, "place": place, "outcome": "", "resolved": None}
                a["timeline"].append(ev("hearing_scheduled", "%s %s, %s" % (hd.isoformat(), tm, place)))
                tell(a, "শুনানির তারিখ: %s, সময়: %s, স্থান: %s।" % (bd(hd.isoformat()), bd(tm), place),
                     "Hearing on %s at %s, venue: %s." % (hd.isoformat(), tm, place))
            elif act == "record_hearing":
                h = a.get("hearing")
                outcome = str(b.get("outcome") or "").strip()[:500]
                if st != 4 or not h:
                    return wrong()
                if not outcome:
                    return err("outcome required / শুনানির ফলাফল লিখুন")
                h.update(outcome=outcome, resolved=bool(b.get("resolved")), held_on=today())
                a["timeline"].append(ev("hearing_held", outcome))
                tell(a, "শুনানি সম্পন্ন হয়েছে।", "The hearing has been held.")
            elif act == "review":
                if st != 4:
                    return wrong()
                decision, remarks = b.get("decision"), str(b.get("remarks") or "").strip()[:400]
                flags = {k: bool(b.get(k)) for k in ("authentic", "chain", "inspection_ok", "legal_ok")}
                rv = dict(flags, remarks=remarks, decision=decision, by=user().get("name"), date=today())
                if decision == "reject":
                    if not remarks:
                        return err("reason required / কারণ লিখুন")
                    a["review"], a["status"], a["reason"] = rv, "rejected", remarks
                    a["timeline"].append(ev("rejected", remarks))
                    tell(a, "এসি (ল্যান্ড) পর্যালোচনায় আবেদন প্রত্যাখ্যাত: " + remarks, "Rejected at AC (Land) review: " + remarks)
                elif decision == "approve":
                    if not all(flags.values()):
                        return err("confirm all four review points / চারটি বিষয়ই নিশ্চিত করুন")
                    n, h = a["notice"], a.get("hearing")
                    if n["objections"] and not (h and h.get("resolved")):
                        return err("hold the hearing and resolve objections first / আগে শুনানি করে আপত্তি নিষ্পত্তি করুন", 409)
                    a["review"], a["stage"] = rv, 5
                    a["timeline"] += [ev("reviewed", remarks), ev("order")]
                    tell(a, "এসি (ল্যান্ড) পর্যালোচনা সম্পন্ন — নামজারি আদেশের অপেক্ষায়।", "AC (Land) review complete — awaiting the mutation order.")
                else:
                    return err("decision required")
            elif act == "issue_order":
                if st != 5:
                    return wrong()
                n, y = seq("order"), date.today().year
                a["order"] = {"no": "%d/%d" % (n, y), "date": today(), "by": user().get("name")}
                rem = fee_block(a)["remaining"]
                a["stage"], a["status"] = 6, "awaiting_payment"
                a["timeline"] += [ev("order_issued"), ev("payment")]
                tell(a, "নামজারি আদেশ জারি হয়েছে (আদেশ নং %s) — অনুগ্রহ করে অবশিষ্ট ফি ৳%s পরিশোধ করুন।" % (bd(a["order"]["no"]), bd(rem)),
                     "Mutation order issued (order no. %s) — please pay the remaining fee ৳%s." % (a["order"]["no"], rem))
            elif act == "mark_paid":
                rc = str(b.get("receipt") or "").strip()[:30]
                if st != 6 or status != "awaiting_payment":
                    return wrong()
                if not rc:
                    return err("receipt no. required / রশিদ নম্বর দিন")
                complete_payment(a, "office", rc)
            else:
                return err("unknown action")
            save()
        return jsonify(ok=True, app=app_view(a))

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

    return page.replace("</body>", "<style>" + MF_CSS + "</style>" + MF_HTML + "<script>" + MF_JS + "</script></body>")


# ======================================================================================= front-end
MF_CSS = r"""
.icon{position:relative}
.mf-dot{position:absolute;top:6px;right:7px;width:9px;height:9px;border-radius:50%;background:var(--bd);border:2px solid var(--sf)}
.mf-notes{width:min(380px,92vw)}.mf-n{padding:9px 10px;border-radius:10px;cursor:pointer;font-size:13.5px;line-height:1.55}.mf-n:hover{background:var(--sf2)}.mf-n.u{background:var(--gt)}.mf-n small{display:block;color:var(--mu)}
.mf-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:14px}
.mf-card{cursor:pointer;display:grid;gap:7px;transition:.15s}.mf-card:hover{transform:translateY(-2px);border-color:var(--g2)}
.mf-row{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}
.mf-t{font-weight:600}.mf-prog{height:6px;border-radius:4px;background:var(--ln);overflow:hidden}.mf-prog i{display:block;height:100%;background:linear-gradient(90deg,var(--g),var(--g2))}
.mf-info{display:flex;gap:14px;align-items:flex-start;background:var(--gt);border-color:var(--g2);margin-bottom:16px}.mf-info p{margin:4px 0 0;color:var(--mu);font-size:14.5px}
.mf-dlg{width:min(900px,96vw)}.mf-two{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:14px}
.mf-docrow{display:flex;gap:10px;align-items:center;padding:9px 0;border-bottom:1px dashed var(--ln);flex-wrap:wrap}.mf-docrow:last-child{border:0}
.mf-next{border-radius:14px;padding:12px 14px;background:var(--wrt);color:var(--wr);font-size:14.5px;margin:12px 0}.mf-next.g{background:var(--gt);color:var(--g)}.mf-next.r{background:var(--bdt);color:var(--bd)}
.mf-panel{border:1px solid var(--g2);border-radius:16px;padding:14px;margin-top:14px;background:color-mix(in srgb,var(--gt) 40%,var(--sf))}
.mf-panel h3{margin-bottom:10px}.mf-panel .fgrid{margin-bottom:10px}.mf-panel textarea{min-height:64px}
.mf-fee{display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px dashed var(--ln)}.mf-pay{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px;margin:12px 0}
.mf-pay label{border:1px solid var(--ln);border-radius:12px;padding:10px;display:flex;gap:8px;align-items:center;cursor:pointer;font-weight:600}
.mf-sub{font-size:12.5px;color:var(--mu)}
@media(max-width:760px){.mf-two{grid-template-columns:1fr}.mf-grid{grid-template-columns:1fr}}
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
upload:'<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" x2="12" y1="3" y2="15"/>'});
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
ng:["দলিল ও নামজারি","Deed & mutation"],nav_mut:["দলিল ও নামজারি","Deed & mutation"],nav_mutadm:["দলিল ও নামজারি প্রক্রিয়া","Deed & mutation desk"],
title:["দলিল নিবন্ধন ও নামজারি","Deed registration & mutation"],sub:["অনলাইনে দলিল নিবন্ধনের আবেদন করুন, অনুমোদনের পর দলিল ডাউনলোড করুন, তারপর নামজারির আবেদন করে খতিয়ান পর্যন্ত প্রতিটি ধাপ অনুসরণ করুন।","Request deed registration online, download the approved deed, then apply for mutation and follow every step up to the khatian."],
admtitle:["দলিল ও নামজারি প্রক্রিয়া (অ্যাডমিন)","Deed & mutation desk (admin)"],admsub:["দলিল যাচাই ও অনুমোদন, নামজারির প্রতিটি ধাপ প্রক্রিয়া এবং অফলাইন আবেদন গ্রহণ।","Verify and approve deeds, process every mutation step and take offline applications."],
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
ev_created:["খসড়া তৈরি","Draft created"],ev_submitted:["জমা দেওয়া হয়েছে","Submitted"],ev_resubmitted:["সংশোধন করে পুনরায় জমা","Resubmitted"],ev_approved:["অনুমোদিত ও নিবন্ধিত","Approved & registered"],ev_returned:["সংশোধনের জন্য ফেরত","Returned for correction"],ev_rejected:["প্রত্যাখ্যাত","Rejected"],ev_held:["স্থগিত","Put on hold"],ev_released:["স্থগিতাদেশ প্রত্যাহার","Released"],ev_application:["আবেদন জমা","Application submitted"],ev_verification:["নথি যাচাই শুরু","Verification started"],ev_verified:["নথি যাচাই সম্পন্ন","Documents verified"],ev_inspection:["মাঠ তদন্ত শুরু","Field inspection started"],ev_inspected:["তদন্ত প্রতিবেদন প্রস্তুত","Inspection report filed"],ev_notice:["নোটিশ ধাপ শুরু","Notice stage started"],ev_notice_issued:["নোটিশ জারি","Notice issued"],ev_objection:["আপত্তি দাখিল","Objection filed"],ev_hearing:["শুনানি ও এসি পর্যালোচনা শুরু","Hearing & AC review started"],ev_hearing_scheduled:["শুনানি নির্ধারিত","Hearing scheduled"],ev_hearing_held:["শুনানি সম্পন্ন","Hearing held"],ev_reviewed:["এসি (ল্যান্ড) পর্যালোচনা সম্পন্ন","AC (Land) review done"],ev_order:["আদেশের অপেক্ষা","Awaiting order"],ev_order_issued:["নামজারি আদেশ জারি","Mutation order issued"],ev_payment:["ফি পরিশোধের অপেক্ষা","Awaiting payment"],ev_paid:["ফি পরিশোধিত","Fee paid"],ev_khatian:["খতিয়ান ও ডিসিআর প্রস্তুত","Khatian & DCR issued"],
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
track_ph:["আইডি লিখুন (MA-… বা DR-…)","Enter ID (MA-… or DR-…)"],track_btn:["ট্র্যাক","Track"],nf:["পাওয়া যায়নি","Not found"],
reason_ph:["কারণ / মন্তব্য (স্থগিত, ফেরত, প্রত্যাখ্যানে বাধ্যতামূলক)","Reason / remark (required for hold, return, reject)"],need_reason:["কারণ লিখুন","Enter a reason"],done:["সম্পন্ন","Done"],
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
q_deed:["দলিল","Deed"],q_basis:["নামজারির ভিত্তি","Basis"],q_ch:["মাধ্যম","Channel"],
kpi_pd:["যাচাইাধীন দলিল","Deeds to review"],kpi_act:["কর্মের অপেক্ষায় আবেদন","Applications to act on"],kpi_pay:["ফি অপেক্ষায়","Awaiting payment"],kpi_done:["সম্পন্ন","Completed"]
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
const bc=s=>({returned:'warn',awaiting_payment:'warn',submitted:'warn',draft:'',approved:'ok',ok:'ok'})[s]??s;
const dlg=()=>document.getElementById('mfDlg');
const mask=n=>n?('•'.repeat(Math.max(0,String(n).length-4))+String(n).slice(-4)):'—';
const clsBn=c=>(T['c_'+key(c)]||[c||'—'])[0];
const dc=v=>Number(v).toFixed(2);
let ST={tab:'deeds',deeds:[],apps:[],fd:'',fa:'',stage:'',admin:false};
function show(html){const d=dlg();d.innerHTML=html;if(!d.open)d.showModal();const x=d.querySelector('[data-x]');if(x)x.onclick=()=>d.close()}
const head=(ti,su)=>`<div class="dhead"><div><h2 style="font-size:20px">${ti}</h2>${su?`<p style="margin:2px 0 0;color:var(--mu);font-size:13.5px">${su}</p>`:''}</div><button class="icon" data-x aria-label="${t('close')}">${ico('x')}</button></div>`;
const kv=rows=>`<dl class="kv">${rows.filter(r=>r[1]!==undefined&&r[1]!=='').map(([a,b])=>`<dt>${a}</dt><dd>${b}</dd>`).join('')}</dl>`;
const lvIc={pass:ico('check',14),warn:ico('alert',14),fail:ico('x',14),queued:ico('clock',14)};
const chkHTML=cs=>cs.map(([k,l])=>`<div class="ck ${l}"><i>${lvIc[l]}</i><span>${m('ck_'+k)}</span><em>${t('lv_'+l)}</em></div>`).join('');
const tlHTML=tl=>`<ul class="tl">${tl.map(e=>`<li><b>${m('ev_'+e.key)}</b><small>${ftime(e.ts)}${e.by?' · '+esc(nm(e.by)):''}</small>${e.note?`<small>${esc(e.note)}</small>`:''}</li>`).join('')}</ul>`;
const landKV=(l,extra=[])=>kv([[m('land_info'),esc(nm(l.district))],[m('land_mouza'),X(l.mouza)+' ('+X(l.m_code)+')'],['JL',X(l.jl||'—')],[m('plot'),X(l.label)],[m('land_khatian'),X(l.khatian)],[m('land_area'),nf(dec(l.area_sqm))+' '+t('u_dec')],...extra]);
const partyKV=p=>kv([[m('f_name'),esc(p.name)],[m('f_father'),esc(p.father||'—')],[m('f_nid'),X(mask(p.nid))],[m('f_addr'),esc(p.address||'—')]]);
function dStepper(d){const i=d.status==='approved'?3:1,bad=['returned','rejected'].includes(d.status);return `<div class="stepper">${['submitted','review','registered'].map((s,k)=>`<div class="${k<i?'done':k===i?'cur'+(bad?' flagged':''):''}">${m('dsp_'+s)}</div>`).join('')}</div>`}
function aStepper(a){const cur=a.stage+1,done=a.status==='completed',bad=['flagged','rejected','returned'].includes(a.status);return `<div class="stepper">${STEPS.map((s,i)=>`<div class="${i<cur||done?'done':i===cur?'cur'+(bad?' flagged':''):''}">${m('sp_'+s)}</div>`).join('')}</div>`}

/* ---------- routing / page ---------- */
const _route=route;
route=function(keep){
  const h=location.hash.slice(1)||'/';
  if(ME&&(h.startsWith('/mutation')||h.startsWith('/mutadmin'))){
    const [path,qs]=h.split('?');cur={path,q:new URLSearchParams(qs||'')};
    if(path.startsWith('/mutadmin')&&role!=='admin'){location.hash='#/mutation';return}
    $$('.view').forEach(e=>e.hidden=e.id!=='v-mut');document.body.classList.remove('map-mode','auth-mode');
    $$('.nav-links a').forEach(a=>a.classList.toggle('on',a.dataset.h===(path.startsWith('/mutadmin')?'#/mutadmin':'#/mutation')));
    mfRender(path.startsWith('/mutadmin'));if(!keep)scrollTo(0,0);return}
  return _route(keep)};
const _rn=renderNav;
renderNav=function(){_rn();const el=$('#links');if(!ME||!el)return;const f=el.querySelector('.nfoot');if(!f)return;
  f.insertAdjacentHTML('beforebegin',`<div class="ngrp">${m('ng')}</div><a href="#/mutation" data-r="mut" data-h="#/mutation">${ico('file',18)}<span>${m('nav_mut')}</span></a>`+(role==='admin'?`<a href="#/mutadmin" data-r="mut" data-h="#/mutadmin">${ico('clipboard',18)}<span>${m('nav_mutadm')}</span></a>`:''));
  $$('.nav-links a[data-r=mut]').forEach(a=>a.classList.toggle('on',location.hash.startsWith(a.dataset.h)))};
const _ru=renderUser;
renderUser=function(){_ru();const w=document.getElementById('bellWrap');if(w)w.hidden=!ME;if(ME){if(NN.uid!==ME.id){NN={rows:[],unread:0,max:0,first:true,uid:ME.id}}loadNotes()}};
const goHome=document.querySelector('[data-go="#/track"]');if(goHome)goHome.dataset.go='#/mutation';

async function mfLoad(){const [d,a]=await Promise.all([J('/api/mut/deeds'),J('/api/mut/apps')]);ST.deeds=d.deeds;ST.apps=a.apps}
async function mfRender(admin){
  ST.admin=admin;const q=cur.q;if(q.get('tab'))ST.tab=q.get('tab');
  const open=q.get('open');if(open)ST.tab=open.startsWith('DR-')?'deeds':'apps';
  $('#mf_root').innerHTML=`<div class="phead"><h1>${m(admin?'admtitle':'title')}</h1><p>${m(admin?'admsub':'sub')}</p></div><div class="tabs" id="mf_tabs"></div><div id="mf_body"><div class="empty">${t('loading')}</div></div>`;
  try{await mfLoad()}catch(e){$('#mf_body').innerHTML=`<div class="card empty">${esc(e.message)}</div>`;return}
  drawTabs();drawBody();
  if(open){history.replaceState(null,'','#'+cur.path);open.startsWith('DR-')?openDeed(open):openApp(open)}}
function drawTabs(){
  const nd=ST.deeds.filter(d=>d.status==='submitted').length,na=ST.apps.filter(a=>a.status==='ok').length;
  $('#mf_tabs').innerHTML=[['deeds','tab_deeds',nd],['apps','tab_apps',na],['track','tab_track',0]].map(([k,l,n])=>`<button data-t="${k}" class="${ST.tab===k?'on':''}">${m(l)}${ST.admin&&n?` <span class="badge new">${nf(n,0)}</span>`:''}</button>`).join('');
  $$('#mf_tabs button').forEach(b=>b.onclick=()=>{ST.tab=b.dataset.t;drawTabs();drawBody()})}
const drawBody=()=>({deeds:drawDeeds,apps:drawApps,track:drawTrackTab})[ST.tab]();
const kpis=()=>ST.admin?`<div class="kpis">${[['kpi_pd',ST.deeds.filter(d=>d.status==='submitted').length],['kpi_act',ST.apps.filter(a=>a.status==='ok').length],['kpi_pay',ST.apps.filter(a=>a.status==='awaiting_payment').length],['kpi_done',ST.apps.filter(a=>a.status==='completed').length]].map(([k,v])=>`<div class="kpi"><b>${nf(v,0)}</b><span>${m(k)}</span></div>`).join('')}</div>`:'';
const chips=(list,cur,pre)=>list.map(s=>`<button class="fchip ${cur===s?'on':''}" data-f="${s}">${s?m(pre+s):m('all')}</button>`).join('');

function drawDeeds(){
  const rows=ST.deeds.filter(d=>!ST.fd||d.status===ST.fd);
  $('#mf_body').innerHTML=`${kpis()}<div class="card mf-info">${ico('file',22)}<div><b>${m('s0_t')}</b><p>${m('s0_d')}</p></div></div>
  <div class="toolbar"><div class="filters">${chips(['','submitted','approved','returned','rejected'],ST.fd,'ds_')}</div><button class="btn" id="mf_nd">${ico('plus',16)} ${m('new_deed')}</button></div>
  <div class="mf-grid">${rows.map(d=>`<div class="card mf-card" data-d="${d.id}"><div class="mf-row"><b>${esc(d.id)}</b><span class="badge ${bc(d.status)}">${m('ds_'+d.status)}</span></div>
   <div class="mf-t">${m('ty_'+d.type)} · ${m('plot')} ${X(d.land.label)}</div><small class="mf-sub">${esc(nm(d.land.district))} · ${X(d.land.mouza)}</small>
   <small>${esc(d.seller.name)} → ${esc(d.buyer.name)}</small><div class="mf-row"><small class="mf-sub">${fd(d.created)}${d.channel==='offline'?' · '+m('offline'):''}</small>${d.deed_no?`<small><b>${m('deed_no')} ${X(d.deed_no)}</b></small>`:''}</div></div>`).join('')||`<div class="card empty">${m('none')}</div>`}</div>`;
  $$('.filters .fchip').forEach(b=>b.onclick=()=>{ST.fd=b.dataset.f;drawDeeds()});
  $$('[data-d]').forEach(c=>c.onclick=()=>openDeed(c.dataset.d));$('#mf_nd').onclick=()=>openDeedForm(null)}
function drawApps(){
  const rows=ST.apps.filter(a=>(!ST.fa||a.status===ST.fa)&&(ST.stage===''||String(a.stage)===ST.stage));
  $('#mf_body').innerHTML=`${kpis()}<div class="toolbar"><div class="filters">${chips(['','draft','ok','flagged','returned','awaiting_payment','completed','rejected'],ST.fa,'as_')}
   ${ST.admin?`<select id="mf_stage"><option value="">${m('sp_application')}…</option>${STEPS.slice(1).map((s,i)=>`<option value="${i+1}" ${ST.stage===String(i+1)?'selected':''}>${m('sp_'+s)}</option>`).join('')}</select>`:''}</div>
   <button class="btn" id="mf_na">${ico('plus',16)} ${ST.admin?m('new_off'):m('new_app')}</button></div>
  <div class="mf-grid">${rows.map(a=>`<div class="card mf-card" data-a="${a.id}"><div class="mf-row"><b>${esc(a.id)}</b><span class="badge ${bc(a.status)}">${m('as_'+a.status)}</span></div>
   <div class="mf-t">${m('ty_'+a.basis)} · ${m('plot')} ${X(a.land.label)}</div><small class="mf-sub">${esc(nm(a.land.district))} · ${esc(a.applicant.name)}</small>
   <div class="mf-prog"><i style="width:${(a.status==='completed'?8:a.stage+1)/8*100}%"></i></div>
   <div class="mf-row"><small class="mf-sub">${m('sp_'+STEPS[a.stage+1])}</small><small class="mf-sub">${a.channel==='offline'?m('offline'):m('online')}</small></div></div>`).join('')||`<div class="card empty">${m('none')}</div>`}</div>`;
  $$('.filters .fchip').forEach(b=>b.onclick=()=>{ST.fa=b.dataset.f;drawApps()});const sg=$('#mf_stage');if(sg)sg.onchange=()=>{ST.stage=sg.value;drawApps()};
  $$('[data-a]').forEach(c=>c.onclick=()=>openApp(c.dataset.a));$('#mf_na').onclick=()=>openAppForm(null)}
function drawTrackTab(){
  $('#mf_body').innerHTML=`<div class="card"><div class="placebox"><input id="tk_in" placeholder="${esc(m('track_ph'))}"><button class="btn" id="tk_go">${m('track_btn')}</button></div></div><div id="tk_out" style="margin-top:14px"></div>`;
  const go=async()=>{const id=$v('tk_in');if(!id)return;try{const r=await J('/api/mut/track/'+encodeURIComponent(id));
    const st=r.kind==='app'?aStepper({stage:r.stage,status:r.status}):dStepper({status:r.status});
    $('#tk_out').innerHTML=`<div class="card"><div class="mf-row"><h3 style="margin:0">${esc(r.id)}</h3><span class="badge ${bc(r.status)}">${m((r.kind==='app'?'as_':'ds_')+r.status)}</span></div>
     <p class="mu" style="margin:4px 0">${m('plot')} ${X(r.land.label)} · ${esc(nm(r.land.district))} · ${X(r.land.mouza)}</p>${st}${tlHTML(r.timeline)}
     ${r.mine?`<button class="btn sm" id="tk_open">${m('open')}</button>`:''}</div>`;
    const o=$('#tk_open');if(o)o.onclick=()=>r.kind==='app'?openApp(r.id):openDeed(r.id)}catch(e){$('#tk_out').innerHTML=`<div class="card empty">${m('nf')}</div>`}};
  $('#tk_go').onclick=go;$('#tk_in').onkeydown=e=>{if(e.key==='Enter')go()}}
async function refresh(then){await mfLoad();drawTabs();drawBody();if(then)then()}

/* ---------- deed form / detail ---------- */
async function openDeedForm(d){
  const adm=ST.admin,me=ME||{},isE=!!d;
  const dsOpts=DSL.map(x=>`<option value="${esc(x.id)}" ${isE&&d.land.ds===x.id?'selected':''}>${esc(dn(x))} · ${esc(shl(x))}</option>`).join('');
  const P=(pre,lab,v)=>`<div class="full"><b>${lab}</b></div><label class="f"><span>${m('f_name')}</span><input id="${pre}n" value="${esc(v.name||'')}"></label><label class="f"><span>${m('f_father')}</span><input id="${pre}f" value="${esc(v.father||'')}"></label><label class="f"><span>${m('f_nid')}</span><input id="${pre}i" inputmode="numeric" value="${esc(v.nid||'')}"></label><label class="f"><span>${m('f_addr')}</span><input id="${pre}a" value="${esc(v.address||'')}"></label>`;
  const bdv=isE?d.boundaries:{},w=isE?d.witnesses:[];
  show(head(isE?m('edit')+' · '+d.id:m('new_deed'))+`<div class="fgrid">
   <label class="f"><span>${m('f_type')}</span><select id="df_type">${['sale','gift','inheritance'].map(k=>`<option value="${k}" ${isE&&d.type===k?'selected':''}>${m('ty_'+k)}</option>`).join('')}</select></label>
   <label class="f"><span>${m('f_date')}</span><input id="df_date" type="date" max="${new Date().toISOString().slice(0,10)}" value="${isE?d.deed_date:new Date().toISOString().slice(0,10)}"></label>
   <label class="f"><span>${m('f_ds')}</span><select id="df_ds">${dsOpts}</select></label>
   <label class="f"><span>${m('f_plot')}</span><input id="df_plot" list="df_plots" autocomplete="off" value="${isE?esc(d.land.plot_no):''}"><datalist id="df_plots"></datalist></label>
   <div class="full card" id="df_land" style="background:var(--sf2);box-shadow:none;padding:12px" hidden></div>
   <label class="f"><span>${m('f_area')}</span><input id="df_area" type="number" step="0.01" min="0" value="${isE?d.area_dec:''}"></label>
   <label class="f"><span>${m('f_value')}</span><input id="df_val" type="number" min="0" value="${isE?d.value:''}"></label>
   <div class="full" id="df_pa">${P('dp_','',isE?d.seller:{})}</div><div class="full" id="df_pb">${P('db_','',isE?d.buyer:{name:adm?'':nm(me.name)})}</div>
   <label class="f"><span>${m('f_w1')}</span><input id="df_w1" value="${esc(w[0]||'')}"></label><label class="f"><span>${m('f_w2')}</span><input id="df_w2" value="${esc(w[1]||'')}"></label>
   <div class="full"><b>${m('f_bd')}</b></div>
   ${['north','south','east','west'].map((k,i)=>`<label class="f"><span>${m('f_'+'nsew'[i])}</span><input id="df_b${i}" value="${esc(bdv[k]||'')}"></label>`).join('')}
   <label class="f full"><span>${m('f_note')}</span><textarea id="df_note" maxlength="300">${isE?esc(d.note):''}</textarea></label></div>
   <div class="btnrow" style="margin-top:14px;justify-content:flex-end"><button class="btn alt" data-x>${m('cancel')}</button><button class="btn" id="df_go">${isE?m('resub'):m('save')}</button></div>`);
  const relabel=()=>{const ty=$v('df_type');$('#df_pa b').textContent=pl(ty,0);$('#df_pb b').textContent=pl(ty,1)};
  const info=async()=>{const ds=$v('df_ds'),no=$v('df_plot');if(!ds||!no)return;try{const r=await api(`/api/verify/${encodeURIComponent(ds)}/${encodeURIComponent(no)}`);
    const b=$('#df_land');b.hidden=false;b.innerHTML=`${landKV({district:r.district,mouza:r.mouza,m_code:r.m_code,jl:r.jl,label:r.label,khatian:r.khatian,area_sqm:r.area_sqm})}${r.khas!=='none'?`<span class="badge fail">${t('kh_'+r.khas)}</span>`:''}`;
    if(!$v('df_area'))$('#df_area').value=dec(r.area_sqm).toFixed(2);if(!isE&&!$v('dp_n')){$('#dp_n').value=nm(r.owner);$('#dp_f').value=nm(r.father)}}catch(e){$('#df_land').hidden=true}};
  $('#df_type').onchange=relabel;$('#df_ds').onchange=()=>{$('#df_plot').value='';fillPlots($('#df_plots'),$v('df_ds'))};$('#df_plot').onchange=info;relabel();fillPlots($('#df_plots'),$v('df_ds'));if(isE)info();
  $('#df_go').onclick=async()=>{const pp=p=>({name:$v(p+'n'),father:$v(p+'f'),nid:$v(p+'i'),address:$v(p+'a')});
    const body={type:$v('df_type'),ds:$v('df_ds'),plot_no:$v('df_plot'),area_dec:$v('df_area'),value:$v('df_val'),deed_date:$v('df_date'),seller:pp('dp_'),buyer:pp('db_'),witnesses:[$v('df_w1'),$v('df_w2')],boundaries:{north:$v('df_b0'),south:$v('df_b1'),east:$v('df_b2'),west:$v('df_b3')},note:$v('df_note')};
    $('#df_go').disabled=true;try{const r=await J(isE?'/api/mut/deeds/'+d.id:'/api/mut/deeds',body,isE?'PUT':'POST');await refresh();openDeed(r.deed.id)}catch(e){toast(e.message);$('#df_go').disabled=false}}}
async function openDeed(id){
  let d;try{d=(await J('/api/mut/deeds/'+encodeURIComponent(id))).deed}catch(e){return toast(e.message)}
  const adm=ST.admin,r=d.registered;
  show(head(`${esc(d.id)} <span class="badge ${bc(d.status)}">${m('ds_'+d.status)}</span>`,`${m('ty_'+d.type)} · ${esc(nm(d.land.district))}`)+dStepper(d)
  +(d.reason&&['returned','rejected'].includes(d.status)?`<div class="mf-next r"><b>${m(d.status==='returned'?'ret_reason':'rej_reason')}:</b> ${esc(d.reason)}</div>`:'')
  +(d.status==='approved'?`<div class="mf-next g"><b>${m('deed_no')} ${X(d.deed_no)}</b> · ${bdate(r.date)} · ${esc(nm(r.office))}</div>`:'')
  +`<div class="mf-two"><div class="card"><h3>${m('sec_land')}</h3>${landKV(d.land,[[m('f_area'),nf(d.area_dec)+' '+t('u_dec')],[m('f_value'),d.value?'৳ '+nf(d.value,0):'—'],[m('f_date'),fd(d.deed_date)]])}</div>
   <div class="card"><h3>${pl(d.type,0)}</h3>${partyKV(d.seller)}<h3 style="margin-top:12px">${pl(d.type,1)}</h3>${partyKV(d.buyer)}</div></div>
   <div class="mf-two"><div class="card"><h3>${m('sec_pre')}</h3>${chkHTML(d.checks)}</div><div class="card"><h3>${m('sec_tl')}</h3>${tlHTML(d.timeline)}</div></div>
   <div class="btnrow" style="margin-top:14px">${d.status==='approved'?`<button class="btn" id="d_dl">${ico('download',16)} ${m('dl_deed')}</button>`:''}
   ${d.status==='approved'&&!d.app_id&&!adm?`<button class="btn alt" id="d_mut">${ico('file',16)} ${m('start_mut')}</button>`:''}${d.app_id?`<button class="btn alt" id="d_va">${m('view_app')}</button>`:''}
   ${d.status==='returned'&&!adm?`<button class="btn alt" id="d_ed">${ico('file',16)} ${m('edit')}</button>`:''}</div>`
  +(adm&&d.status==='submitted'?`<div class="mf-panel"><h3>${m('a_approve')}</h3><div class="fgrid"><label class="f"><span>বই নং</span><input id="dp_book" placeholder="1"></label><label class="f"><span>ভলিউম</span><input id="dp_vol"></label><label class="f"><span>পৃষ্ঠা</span><input id="dp_page"></label></div>
   <input id="dp_reason" placeholder="${esc(m('reason_ph'))}"><div class="btnrow" style="margin-top:10px"><button class="btn sm" data-da="approve">${ico('check',15)} ${m('a_approve')}</button><button class="btn sm alt" data-da="return" data-need>${m('a_return')}</button><button class="btn sm danger" data-da="reject" data-need>${m('a_reject')}</button></div></div>`:''));
  const D=dlg();const g=s=>D.querySelector(s);
  if(g('#d_dl'))g('#d_dl').onclick=()=>pdDeed(d);if(g('#d_mut'))g('#d_mut').onclick=()=>openAppForm(d);if(g('#d_va'))g('#d_va').onclick=()=>openApp(d.app_id);if(g('#d_ed'))g('#d_ed').onclick=()=>openDeedForm(d);
  D.querySelectorAll('[data-da]').forEach(b=>b.onclick=async()=>{const act=b.dataset.da,reason=$v('dp_reason');if(b.hasAttribute('data-need')&&!reason)return toast(m('need_reason'));
    try{await J('/api/admin/mut/deeds/'+d.id+'/'+act,{reason,book:$v('dp_book'),volume:$v('dp_vol'),page:$v('dp_page')});toast(m('done'));await refresh();openDeed(d.id)}catch(e){toast(e.message)}})}

/* ---------- application form ---------- */
function openAppForm(deed){
  const adm=ST.admin,me=ME||{},ok=ST.deeds.filter(d=>d.status==='approved'&&!d.app_id);
  if(!ok.length&&!adm)return toast(m('need_deed'));
  const opts=ok.map(d=>`<option value="${d.id}" ${deed&&deed.id===d.id?'selected':''}>${esc(d.id)} · ${m('ty_'+d.type)} · ${X(d.deed_no||'')} · ${X(d.land.label)}</option>`).join('');
  const pre=deed?deed.buyer:{name:adm?'':nm(me.name)};
  show(head(adm?m('new_off'):m('new_app'),adm?m('online')+' / '+m('offline'):'')+`<div class="fgrid">
   <label class="f full"><span>${m('f_deed')}</span><select id="af_deed">${opts}${adm?`<option value="" ${ok.length?'':'selected'}>${m('f_ext')}</option>`:''}</select></label>
   <div class="full fgrid" id="af_ext" hidden>
    <label class="f"><span>${m('f_ds')}</span><select id="ex_ds">${DSL.map(x=>`<option value="${esc(x.id)}">${esc(dn(x))} · ${esc(shl(x))}</option>`).join('')}</select></label>
    <label class="f"><span>${m('f_plot')}</span><input id="ex_plot" list="ex_plots"><datalist id="ex_plots"></datalist></label>
    <label class="f"><span>${m('f_type')}</span><select id="ex_type">${['sale','gift','inheritance'].map(k=>`<option value="${k}">${m('ty_'+k)}</option>`).join('')}</select></label>
    <label class="f"><span>${m('f_dno')}</span><input id="ex_no"></label><label class="f"><span>${m('f_date')}</span><input id="ex_date" type="date"></label>
    <label class="f"><span>${m('f_area')}</span><input id="ex_area" type="number" step="0.01"></label><label class="f full"><span>${m('f_seller_n')}</span><input id="ex_seller"></label></div>
   <div class="full"><b>${m('sec_app')}</b></div>
   <label class="f"><span>${m('f_name')}</span><input id="ap_n" value="${esc(pre.name||'')}"></label><label class="f"><span>${m('f_father')}</span><input id="ap_f" value="${esc(pre.father||'')}"></label>
   <label class="f"><span>${m('f_nid')}</span><input id="ap_i" inputmode="numeric" value="${esc(pre.nid||'')}"></label><label class="f"><span>${m('f_mobile')}</span><input id="ap_m" inputmode="tel" placeholder="01XXXXXXXXX"></label>
   <label class="f"><span>${m('f_email')}</span><input id="ap_e" value="${esc(adm?'':me.email||'')}"></label><label class="f"><span>${m('f_addr')}</span><input id="ap_a" value="${esc(pre.address||'')}"></label>
   <label class="f"><span>${m('f_pk')}</span><input id="ap_pk"></label><label class="f"><span>${m('f_ldt')}</span><input id="ap_ldt"></label>
   <label class="f"><span>${m('f_ldty')}</span><input id="ap_ldty" inputmode="numeric" placeholder="${new Date().getFullYear()}"></label>
   <label class="chk full"><input type="checkbox" id="ap_court"><span>${m('f_court')}</span></label></div>
   <div class="btnrow" style="margin-top:14px;justify-content:flex-end"><button class="btn alt" data-x>${m('cancel')}</button><button class="btn" id="af_go">${m('create')}</button></div>`);
  const tg=()=>{$('#af_ext').hidden=!!$v('af_deed')};$('#af_deed').onchange=tg;tg();
  if(adm){$('#ex_ds').onchange=()=>fillPlots($('#ex_plots'),$v('ex_ds'));fillPlots($('#ex_plots'),$v('ex_ds'))}
  $('#af_go').onclick=async()=>{const body={deed_id:$v('af_deed'),applicant:{name:$v('ap_n'),father:$v('ap_f'),nid:$v('ap_i'),mobile:$v('ap_m'),email:$v('ap_e'),address:$v('ap_a')},court_ordered:$c('ap_court'),prev_khatian:$v('ap_pk'),ldt_receipt:$v('ap_ldt'),ldt_paid_till:$v('ap_ldty')};
    if(!body.deed_id)body.external={ds:$v('ex_ds'),plot_no:$v('ex_plot'),type:$v('ex_type'),deed_no:$v('ex_no'),deed_date:$v('ex_date'),area_dec:$v('ex_area'),seller:{name:$v('ex_seller')}};
    $('#af_go').disabled=true;try{const r=await J('/api/mut/apps',body);await refresh();openApp(r.app.id)}catch(e){toast(e.message);$('#af_go').disabled=false}}}

/* ---------- application detail ---------- */
function pick(aid,k){const i=document.createElement('input');i.type='file';i.accept=(k==='photo'||k==='signature')?'image/*':'image/*,application/pdf';
  i.onchange=()=>{const f=i.files[0];if(!f)return;if(f.size>1.5*1024*1024)return toast(m('toobig'));const r=new FileReader();
    r.onload=async()=>{try{await J(`/api/mut/apps/${aid}/doc/${k}`,{name:f.name,data:r.result});openApp(aid)}catch(e){toast(e.message)}};r.readAsDataURL(f)};i.click()}
function nextMsg(a){if(a.status==='completed')return['g',m('nx_7')];if(a.status==='rejected')return['r',m('rej_reason')+': '+esc(a.reason)];if(a.status==='returned')return['r',m('ret_reason')+': '+esc(a.reason)];if(a.status==='flagged')return['r',m('on_hold')+': '+esc(a.reason)];if(a.status==='draft')return['',m('nx_draft')];return[a.status==='awaiting_payment'?'':'g',m('nx_'+a.stage)]}
async function openApp(id){
  let a;try{a=(await J('/api/mut/apps/'+encodeURIComponent(id))).app}catch(e){return toast(e.message)}
  const adm=ST.admin,canDoc=['draft','returned'].includes(a.status)||(adm&&!['completed','rejected'].includes(a.status)&&a.stage<=3),own=!adm&&a.uid===(ME&&ME.id);
  const [nc,nt]=nextMsg(a),dd=a.docs;
  const docRows=(a.deed_id?`<div class="mf-docrow"><span class="badge ok">${ico('check',13)}</span><span style="flex:1"><b>${m('dc_deed')}</b><small class="mf-sub" style="display:block">${m('auto_deed')}</small></span></div>`:'')
   +a.required.filter(k=>k!=='deed'||!a.deed_id).map(k=>{const x=dd[k];return `<div class="mf-docrow"><span class="badge ${x?'ok':'fail'}">${x?ico('check',13):ico('x',13)}</span><span style="flex:1;min-width:150px"><b>${m('dc_'+k)}</b><small class="mf-sub" style="display:block">${x?(x.paper?m('paper'):esc(x.name)):m('required')}</small></span>
    ${x&&!x.paper?`<a class="btn sm alt" target="_blank" rel="noopener" href="/api/mut/apps/${a.id}/file/${k}">${ico('search',14)}</a>`:''}
    ${canDoc?`<button class="btn sm alt" data-up="${k}">${ico('upload',14)} ${x?m('replace'):m('upload')}</button>${adm&&!x?`<button class="btn sm alt" data-paper="${k}">${m('paper_btn')}</button>`:''}${x?`<button class="btn sm alt" data-rm="${k}">${ico('trash',14)}</button>`:''}`:''}</div>`}).join('');
  const I=a.inspection,N=a.notice,H=a.hearing,O=a.order,K=a.khatian;
  const results=[I&&`<div class="card"><h3>${m('r_insp')}</h3>${kv([[m('k_insp'),esc(I.inspector)],[m('k_date'),fd(I.date)],[m('k_possession'),m('po_'+I.possession)],[m('k_area'),nf(I.area_claimed)+' / '+nf(I.area_found)+' · '+(I.area_match?m('k_match'):m('k_nomatch'))],[m('k_neigh'),esc(I.neighbors.join(', '))||'—'],[m('f_note'),esc(I.remarks)]])}</div>`,
   N&&`<div class="card"><h3>${m('r_notice')}</h3>${kv([['No.',esc(N.no)],[m('k_date'),fd(N.issued)],[m('k_deadline'),fd(N.deadline)],[m('k_recip'),N.recipients.map(r=>esc(r.name)+' ('+m('pr_'+r.role)+')').join('<br>')],[m('k_obj'),N.objections.length?N.objections.map(o=>esc(o.by)+': '+esc(o.text)).join('<br>'):'—']])}</div>`,
   H&&`<div class="card"><h3>${m('r_hear')}</h3>${kv([[m('k_date'),fd(H.date)+' '+X(H.time)],[m('ap_hp'),esc(H.place)],[m('ap_ho'),esc(H.outcome)||'—']])}</div>`,
   O&&`<div class="card"><h3>${m('r_order')}</h3>${kv([[m('k_order_no'),X(O.no)],[m('k_date'),fd(O.date)]])}</div>`,
   K&&`<div class="card"><h3>${m('r_kh')}</h3>${kv([[m('k_kh'),X(K.khatian_no)],[m('k_dcr'),X(K.dcr_no)],[m('k_date'),fd(K.issued)],[m('k_txn'),esc((a.payment||{}).txn||'')]])}</div>`].filter(Boolean).join('');
  show(head(`${esc(a.id)} <span class="badge ${bc(a.status)}">${m('as_'+a.status)}</span>`,`${m('ty_'+a.basis)} · ${m('plot')} ${X(a.land.label)} · ${esc(nm(a.land.district))} · ${esc(a.applicant.name)}`)+aStepper(a)
  +`<div class="mf-next ${nc}">${nt}</div>
   <div class="btnrow">${a.khatian?`<button class="btn" data-pd="khatian">${ico('download',16)} ${m('dl_khatian')}</button>`:''}<button class="btn sm alt" data-pd="app_u">${ico('printer',15)} ${m('dl_app_u')}</button>${adm?`<button class="btn sm alt" data-pd="app_o">${ico('printer',15)} ${m('dl_app_o')}</button>`:''}${I?`<button class="btn sm alt" data-pd="insp">${ico('printer',15)} ${m('dl_insp')}</button>`:''}${N?`<button class="btn sm alt" data-pd="notice">${ico('printer',15)} ${m('dl_notice')}</button>`:''}${O?`<button class="btn sm alt" data-pd="order">${ico('printer',15)} ${m('dl_order')}</button>`:''}</div>
   <div class="mf-two"><div class="card"><h3>${m('sec_docs')}</h3>${docRows}</div><div class="card"><h3>${m('sec_checks')}</h3>${chkHTML(a.checks)}</div></div>
   ${results?`<div class="mf-two">${results}</div>`:''}
   <div class="mf-two"><div class="card"><h3>${m('sec_app')}</h3>${partyKV(a.applicant)}${kv([[m('f_mobile'),X(a.applicant.mobile)],[m('f_pk'),X(a.prev_khatian||'—')],[m('f_ldt'),X(a.ldt.receipt||'—')],[m('q_ch'),a.channel==='offline'?m('offline'):m('online')]])}</div><div class="card"><h3>${m('sec_tl')}</h3>${tlHTML(a.timeline)}</div></div>
   <div class="btnrow" style="margin-top:14px">${['draft','returned'].includes(a.status)?`<button class="btn" id="a_submit">${ico('check',16)} ${m('submit_app')}</button>`:''}${own&&a.status==='awaiting_payment'?`<button class="btn" id="a_pay">${m('pay_now')} · ৳${nf(a.fees.remaining,0)}</button>`:''}</div>`
  +(adm?adminPanel(a):''));
  const D=dlg();
  D.querySelectorAll('[data-up]').forEach(b=>b.onclick=()=>pick(a.id,b.dataset.up));
  D.querySelectorAll('[data-rm]').forEach(b=>b.onclick=async()=>{try{await J(`/api/mut/apps/${a.id}/doc/${b.dataset.rm}`,null,'DELETE');openApp(a.id)}catch(e){toast(e.message)}});
  D.querySelectorAll('[data-paper]').forEach(b=>b.onclick=async()=>{try{await J(`/api/mut/apps/${a.id}/doc/${b.dataset.paper}`,{paper:true});openApp(a.id)}catch(e){toast(e.message)}});
  D.querySelectorAll('[data-pd]').forEach(b=>b.onclick=()=>({khatian:pdKhatian,app_u:x=>pdApp(x,'u'),app_o:x=>pdApp(x,'o'),insp:pdInsp,notice:pdNotice,order:pdOrder})[b.dataset.pd](a));
  const sb=D.querySelector('#a_submit');if(sb)sb.onclick=async()=>{sb.disabled=true;try{await J(`/api/mut/apps/${a.id}/submit`,{});toast(m('done'));await refresh();openApp(a.id)}catch(e){toast(e.message);sb.disabled=false}};
  const pb=D.querySelector('#a_pay');if(pb)pb.onclick=()=>openPay(a);
  bindAdmin(a)}
function openPay(a){
  show(head(m('pay_now'),a.id)+`<div class="card">${a.fees.items.map(f=>`<div class="mf-fee"><span>${m('fee_'+f.key)}</span><span>৳${nf(f.amount,0)} · <small class="badge ${f.paid?'ok':'warn'}">${f.paid?m('paid'):m('due')}</small></span></div>`).join('')}<div class="mf-fee"><b>${m('due')}</b><b>৳${nf(a.fees.remaining,0)}</b></div></div>
  <div class="mf-pay">${['bkash','nagad','rocket','card','bank'].map((k,i)=>`<label><input type="radio" name="pm" value="${k}" ${i===0?'checked':''}>${m('pm_'+k)}</label>`).join('')}</div><p class="mf-sub">${m('pay_demo')}</p>
  <div class="btnrow" style="justify-content:flex-end"><button class="btn alt" data-x>${m('cancel')}</button><button class="btn" id="pay_go">${m('pay_ok')} ৳${nf(a.fees.remaining,0)}</button></div>`);
  $('#pay_go').onclick=async()=>{const pm=(document.querySelector('input[name=pm]:checked')||{}).value;$('#pay_go').disabled=true;try{await J(`/api/mut/apps/${a.id}/pay`,{method:pm});toast(m('done'));await refresh();openApp(a.id)}catch(e){toast(e.message);$('#pay_go').disabled=false}}}

/* ---------- admin stage panel ---------- */
function adminPanel(a){
  const st=a.stage,s=a.status;if(['completed','rejected'].includes(s))return '';
  const gen=`<div style="margin-top:12px"><input id="ap_reason" placeholder="${esc(m('reason_ph'))}"><div class="btnrow" style="margin-top:8px">${s==='flagged'?`<button class="btn sm alt" data-do="release">${m('a_release')}</button>`:`<button class="btn sm alt" data-do="hold" data-need>${m('a_hold')}</button>`}${st>=1&&st<=4&&s!=='flagged'?`<button class="btn sm alt" data-do="return" data-need>${m('a_return')}</button>`:''}<button class="btn sm danger" data-do="reject" data-need>${m('a_reject')}</button></div></div>`;
  const F=(id,l,v='',ty='text')=>`<label class="f"><span>${l}</span><input id="${id}" type="${ty}" value="${esc(v)}"></label>`;
  const Tx=(id,l)=>`<label class="f full"><span>${l}</span><textarea id="${id}"></textarea></label>`;
  let h='';const today=new Date().toISOString().slice(0,10);
  if(s==='flagged'||['draft','returned'].includes(s))h=`<p class="mu">${s==='flagged'?m('on_hold')+': '+esc(a.reason):m('wait_app')}</p>`;
  else if(st===1)h=`<h3>${m('ap_verify_t')}</h3>${Tx('ap_note',m('ap_note'))}<button class="btn sm" data-do="verify">${ico('check',15)} ${m('ap_verify')}</button>`;
  else if(st===2)h=`<h3>${m('ap_insp_t')}</h3><div class="fgrid">${F('ap_insp',m('ap_insp'))}${F('ap_idate',m('ap_idate'),today,'date')}
    <label class="f"><span>${m('ap_poss')}</span><select id="ap_poss">${['full','partial','none','disputed'].map(k=>`<option value="${k}">${m('po_'+k)}</option>`).join('')}</select></label>${F('ap_found',m('ap_found'),a.area_dec,'number')}
    ${['n','s','e','w'].map((k,i)=>F('ap_'+k,m('f_'+k))).join('')}${Tx('ap_nb',m('ap_nb'))}${Tx('ap_rem',m('ap_rem'))}</div><button class="btn sm" data-do="inspect">${m('ap_insp_go')}</button>`;
  else if(st===3&&!a.notice)h=`<h3>${m('ap_not_t')}</h3><div class="fgrid">${F('ap_days',m('ap_days'),15,'number')}${Tx('ap_extra',m('ap_extra'))}</div><button class="btn sm" data-do="issue_notice">${m('ap_issue')}</button>`;
  else if(st===3)h=`<h3>${m('ap_not_t')}</h3><p class="mu">${m('k_deadline')}: ${fd(a.notice.deadline)}</p><div class="fgrid">${F('ap_oby',m('ap_obj_by'))}${Tx('ap_otext',m('ap_obj_txt'))}</div><div class="btnrow"><button class="btn sm alt" data-do="objection">${m('ap_obj_add')}</button><button class="btn sm" data-do="close_notice">${m('ap_close')}</button></div>`;
  else if(st===4){const hr=a.notice&&a.notice.objections.length,H=a.hearing;
    h=`<h3>${m('ap_hear_t')}</h3>`+(hr&&!H?`<div class="fgrid">${F('ap_hd',m('ap_hd'),'','date')}${F('ap_ht',m('ap_ht'),'11:00','time')}${F('ap_hp',m('ap_hp'))}</div><button class="btn sm alt" data-do="schedule_hearing">${m('ap_hsched')}</button>`:'')
    +(hr&&H&&H.resolved===null?`<div class="fgrid">${Tx('ap_ho',m('ap_ho'))}<label class="chk full"><input type="checkbox" id="ap_hr"><span>${m('ap_hr')}</span></label></div><button class="btn sm alt" data-do="record_hearing">${m('ap_hrec')}</button>`:'')
    +`<div style="margin-top:12px">${[1,2,3,4].map(i=>`<label class="chk"><input type="checkbox" id="ap_r${i}"><span>${m('rv'+i)}</span></label>`).join('')}${Tx('ap_rr',m('ap_rr'))}</div><div class="btnrow"><button class="btn sm" data-do="review_approve">${m('ap_rapp')}</button><button class="btn sm danger" data-do="review_reject">${m('ap_rrej')}</button></div>`}
  else if(st===5)h=`<h3>${m('ap_ord_t')}</h3><button class="btn sm" data-do="issue_order">${m('ap_ord')}</button>`;
  else if(st===6)h=`<h3>${m('ap_pay_t')}</h3><p class="mu">${m('ap_pay_wait')}</p><div class="fgrid">${F('ap_rc',m('ap_rc'))}</div><button class="btn sm" data-do="mark_paid">${m('ap_markpaid')}</button>`;
  return `<div class="mf-panel">${h}${gen}</div>`}
const PAY={verify:()=>({note:$v('ap_note')}),inspect:()=>({inspector:$v('ap_insp'),date:$v('ap_idate'),possession:$v('ap_poss'),area_found:$v('ap_found'),boundaries:{north:$v('ap_n'),south:$v('ap_s'),east:$v('ap_e'),west:$v('ap_w')},neighbors:$v('ap_nb').split('\n'),remarks:$v('ap_rem')}),
 issue_notice:()=>({days:$v('ap_days'),extra:$v('ap_extra').split('\n')}),objection:()=>({by:$v('ap_oby'),text:$v('ap_otext')}),close_notice:()=>({}),
 schedule_hearing:()=>({date:$v('ap_hd'),time:$v('ap_ht'),place:$v('ap_hp')}),record_hearing:()=>({outcome:$v('ap_ho'),resolved:$c('ap_hr')}),
 review_approve:()=>({decision:'approve',authentic:$c('ap_r1'),chain:$c('ap_r2'),inspection_ok:$c('ap_r3'),legal_ok:$c('ap_r4'),remarks:$v('ap_rr')}),review_reject:()=>({decision:'reject',remarks:$v('ap_rr')}),
 issue_order:()=>({}),mark_paid:()=>({receipt:$v('ap_rc')}),hold:()=>({}),release:()=>({}),return:()=>({}),reject:()=>({})};
function bindAdmin(a){dlg().querySelectorAll('[data-do]').forEach(b=>b.onclick=async()=>{const k=b.dataset.do,reason=$v('ap_reason');
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
function pdApp(a,copy){
  const L=a.land,ap=a.applicant,d=a.deed||{},ext=a.external,BA={sale:'ক্রয়',gift:'দান (হেবা)',inheritance:'উত্তরাধিকার (ওয়ারিশ)'}[a.basis]+(a.court_ordered?' + আদালতের আদেশ':'');
  const docs=Object.keys(a.docs).length||a.required.length?a.required.map(k=>[`${m_dc(k)}`,a.docs[k]?(a.docs[k].paper?'কাগজে জমা':'সংযুক্ত (ফাইল)'):'<b>জমা হয় নাই</b>',a.docs[k]&&!a.docs[k].paper?E(a.docs[k].name):'—']):[];
  const body=`<div class="pd-title">নামজারি (খারিজ) আবেদনপত্র</div>
  ${sec(1,'আবেদনকারীর তথ্য',`<div class="pd-two"><div>${kvT([['নাম',E(ap.name)],['পিতা/স্বামী',E(ap.father)],['জাতীয় পরিচয়পত্র নং',B(mask(ap.nid))],['মোবাইল',B(ap.mobile)],['ইমেইল',E(ap.email||'—')],['ঠিকানা',E(ap.address)]])}</div><div class="pd-photo">${imgBox(a,'photo')}</div></div>`)}
  ${sec(2,'নামজারির ভিত্তি ও দলিল',kvT([['নামজারির ভিত্তি',BA],['দলিল নং ও তারিখ',a.deed_id?`${E(d.deed_no)} (${bdate((d.registered||{}).date)})`:`${E(ext.no)} (${bdate(ext.date)}) — কাগজের দলিল`],['পূর্বের খতিয়ান নং',E(a.prev_khatian||'—')],['ভূমি উন্নয়ন কর রশিদ নং / সন',`${E(a.ldt.receipt||'—')} / ${E(a.ldt.paid_till||'—')}`]]))}
  ${sec(3,'ভূমির তফসিল',schedule(L,a.area_dec,d.boundaries))}
  ${sec(4,'সংযুক্ত কাগজপত্র',tbT(['কাগজ','অবস্থা','ফাইল'],[...(a.deed_id?[['নিবন্ধিত দলিলের কপি','সিস্টেম থেকে সংযুক্ত','—']]:[]),...docs]))}
  ${sec(5,'স্বয়ংক্রিয় যাচাই (জমাদানের সময়)',tbT(['যাচাইয়ের বিষয়','ফলাফল'],a.checks.map(([k,l])=>[m_ck(k),{pass:'উত্তীর্ণ',warn:'সতর্কতা',fail:'ব্যর্থ'}[l]])))}
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
   body:sec(1,'পরিশোধের বিবরণ',kvT([['মালিক',E(ap.name)],['দাগ নং',E(L.label)],['পরিশোধ পদ্ধতি',E({bkash:'বিকাশ',nagad:'নগদ',rocket:'রকেট',card:'কার্ড',bank:'ব্যাংক',office:'অফিসে নগদ',demo:'অনলাইন',cash:'নগদ'}[P.method]||P.method||'—')],['লেনদেন / রশিদ নং',E(P.txn||'—')]]))
   +sec(2,'ফি বিবরণী',tbT(['ফি','টাকা','অবস্থা'],a.fees.items.map(f=>[FEEN[f.key],B(f.amount),'পরিশোধিত']).concat([['মোট',`<b>${B(a.fees.total)}</b>`,'']])))
   +`<p class="pd-decl">উপরোক্ত ফি সম্পূর্ণ পরিশোধিত হইয়াছে। ভবিষ্যতে ভূমি উন্নয়ন কর পরিশোধের সময় এই ডিসিআর ও খতিয়ান নম্বর উল্লেখ করিতে হইবে।</p>`,signs:['','','আদায়কারী কর্মকর্তা (ডিজিটাল)']});
  mfPrint(a.id+' নামজারি খতিয়ান ও ডিসিআর',kh+'<div class="pd-pg"></div>'+dcr)}

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
