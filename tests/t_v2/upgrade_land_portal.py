#!/usr/bin/env python3
"""
Upgrade script for  land_portal.py  (Bhumi Dorpon)

    python upgrade_land_portal.py land_portal.py            ->  writes land_portal_v2.py
    python land_portal_v2.py                                 (production-like: devtools deterrent ON)
    python land_portal_v2.py --dev                           (devtools deterrent OFF while you develop)
    BD_ADMIN_KEY=my-secret python land_portal_v2.py          (admin API requires that key)

What it adds
  1. Admin can ADD fraud alerts and e-Mutation cases for any plot of any dataset, in BOTH data modes
     (kept separate per mode, saved to data/admin_custom.json, can be deleted again).
     Entry points: Fraud tab, Workflow tab, Data manager (per dataset row) and the map plot drawer.
  2. Real SVG icons (Lucide style) instead of emoji.
  3. Bangla typography fixes + fully responsive admin (table -> cards, kanban snap, bottom-sheet dialogs).
  4. Devtools deterrent (disable-devtool + key/context-menu blocking), optional admin key, security headers.
"""
import re
import sys
import pathlib

src = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "land_portal.py")
dst = pathlib.Path(sys.argv[2] if len(sys.argv) > 2 else src.with_name(src.stem + "_v2.py"))
s = src.read_text(encoding="utf-8")


def rep(old, new, count=1):
    global s
    n = s.count(old)
    if n != count:
        sys.exit(f"ANCHOR MISMATCH ({n} found, {count} expected):\n  {old[:90]!r}\nNothing was written.")
    s = s.replace(old, new)


def rex(pattern, new, count=1, flags=0):
    global s
    s, n = re.subn(pattern, lambda m: new, s, flags=flags)
    if n != count:
        sys.exit(f"REGEX MISMATCH ({n} found, {count} expected):\n  {pattern[:90]!r}\nNothing was written.")


# =====================================================================================  BACKEND
rep("import hashlib\n", "import hashlib\nimport hmac\n")
rep("import threading\n", "import threading\nimport time\n")

rep('STATE = {"fraud": {}, "wf": {}}  # in-memory admin actions (MVP)', r'''CUSTOM_FILE = os.path.join(DATA_DIR, "admin_custom.json")
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


_load_custom()''')

# ---- derived(): cache signature includes admin revisions, merge manual alerts / cases
rep('''    idx, mode = index(), cur_mode()
    c = _der.get(mode)
    if c and c["sig"] == idx["sig"]:
        return c
''', '''    idx, mode = index(), cur_mode()
    dsig = f"{idx['sig']}:{STATE['rev'][mode]}"
    c = _der.get(mode)
    if c and c["sig"] == dsig:
        return c
''')
rep("    cases, n = {}, 0\n", '''    for aid, a in STATE["custom"][mode]["alerts"].items():  # manually added by admin
        if (a["ds"], a["plot_no"]) in idx["plots"]:
            alerts[aid] = a
            by_plot.setdefault((a["ds"], a["plot_no"]), []).append(a)
    cases, n = {}, 0
''')
rep('''            "checks": [{"key": k, "level": v if stage >= 1 else "queued"} for k, v in checks],
''', '''            "checks": [{"key": k, "level": v if stage >= 1 else "queued"} for k, v in checks],
            "raw": [[k, v] for k, v in checks],
''')
rep('    _der[mode] = {"sig": idx["sig"], "alerts": alerts, "cases": cases, "by_plot": by_plot}',
    '''    for cid, c in STATE["custom"][mode]["cases"].items():
        if (c["ds"], c["plot_no"]) in idx["plots"]:
            cases[cid] = c
    _der[mode] = {"sig": dsig, "alerts": alerts, "cases": cases, "by_plot": by_plot}''')

rep('    v["days_left"] = (date.fromisoformat(c["sla_due"]) - TODAY).days\n',
    '''    if v.get("raw"):  # automated checks become visible once the case leaves "registration"
        v["checks"] = [{"key": k, "level": lv if v["stage"] >= 1 else "queued"} for k, lv in v["raw"]]
    v["days_left"] = (date.fromisoformat(c["sla_due"]) - TODAY).days
''')

rep('''def err(msg, code=400):
    return jsonify(error=msg), code
''', '''def err(msg, code=400):
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
''')

rep('        st["status"] = "completed" if st["stage"] == 5 else "ok"\n',
    '''        st["status"] = "completed" if st["stage"] == 5 else "ok"
        if st["stage"] == 1 and any(lv != "pass" for k, lv in (c.get("raw") or []) if k != "fee_paid"):
            st["status"] = "flagged"  # automated checks failed -> needs an officer
''')

rep('@app.route("/api/admin/upload/<ds>/<kind>", methods=["POST"])', r'''def _plot_key(b):
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


@app.route("/api/admin/upload/<ds>/<kind>", methods=["POST"])''')

rep('    return Response(PAGE, mimetype="text/html")',
    '    return Response(PAGE.replace("<!--HARDEN-->", HARDEN_HTML if HARDEN["on"] else ""), mimetype="text/html")')
rep('    ap.add_argument("--no-browser", action="store_true")\n',
    '    ap.add_argument("--no-browser", action="store_true")\n'
    '    ap.add_argument("--dev", action="store_true", help="disable the devtools deterrent while developing")\n')
rep("    write_demo()\n", '    HARDEN["on"] = not a.dev\n    write_demo()\n')

# =====================================================================================  FRONT-END: head / fonts / css
rep("family=Hind+Siliguri:wght@400;500;600;700&family=Inter:wght@400;500;600;700&display=swap",
    "family=Hind+Siliguri:wght@400;500;600;700&family=Noto+Sans+Bengali:wght@400;500;600;700&family=Inter:wght@400;500;600;700&display=swap")
rep("font-family:'Hind Siliguri','Inter',system-ui,sans-serif",
    "font-family:'Inter','Hind Siliguri','Noto Sans Bengali','Nirmala UI','Kalpurush','Vrinda',system-ui,sans-serif")
rex(r"#ldSteps\{.*?li\.fail:before\{[^}]*\}", "", flags=re.S)

EXTRA_CSS = r'''
/* ===== v2: typography, icons, responsive admin ===== */
body{text-rendering:optimizeLegibility;-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;font-kerning:normal;font-feature-settings:"kern" 1,"liga" 1,"locl" 1;overflow-wrap:break-word;-webkit-text-size-adjust:100%}
h1,h2,h3,h4{line-height:1.35}
input,select,textarea,button{font-family:inherit;line-height:1.5}
textarea{font:inherit;color:var(--tx);background:var(--sf2);border:1px solid var(--ln);border-radius:11px;padding:10px 12px;width:100%;outline:none;resize:vertical;min-height:84px}
textarea:focus{border-color:var(--g2);box-shadow:0 0 0 3px color-mix(in srgb,var(--g2) 22%,transparent)}
html[lang=bn] body{line-height:1.75}
html[lang=bn] *{letter-spacing:0!important;text-transform:none!important}
html[lang=bn] .badge,html[lang=bn] .tag,html[lang=bn] .chip,html[lang=bn] .fchip{line-height:1.7;padding-top:3px;padding-bottom:3px}
html[lang=bn] .btn,html[lang=bn] .seg button{line-height:1.6}
html[lang=bn] th{font-size:13px}
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
 .nav-in{padding:0 12px}.tools .modeSeg,.tools .mode-label{display:none}
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
 .container{padding:0 14px}.hero{padding:34px 0 24px}.brand span:last-child{font-size:16px}
}
@media(min-width:1400px){.container{max-width:1280px}}
'''
rep("</style>\n</head>", EXTRA_CSS + "</style>\n<!--HARDEN-->\n</head>")

# =====================================================================================  FRONT-END: HTML icons
def ic_html(n, sz=18):
    return f'<i data-ic="{n}" data-s="{sz}"></i>'


rex(r'<span class="logo">[^<]*</span>', f'<span class="logo">{ic_html("leaf", 20)}</span>')
rex(r'<button class="icon" id="themeBtn" title="Theme">[^<]*</button>',
    f'<button class="icon" id="themeBtn" title="Theme" aria-label="Theme">{ic_html("contrast")}</button>')
rex(r'<button class="icon burger" id="burger">[^<]*</button>',
    f'<button class="icon burger" id="burger" aria-label="Menu">{ic_html("menu")}</button>')
rex(r'<button class="btn" id="h_go">[^<]*</button>', f'<button class="btn" id="h_go" aria-label="Search">{ic_html("pin")}</button>')
rex(r'<div class="float f1">[^<]*<span data-i18n="mock_safe"></span></div><div class="float f2">[^<]*<span data-i18n="mock_alert"></span></div>',
    f'<div class="float f1">{ic_html("shield")} <span data-i18n="mock_safe"></span></div>'
    f'<div class="float f2">{ic_html("alert")} <span data-i18n="mock_alert"></span></div>')
rex(r'<button class="icon" id="ptoggle">[^<]*</button>', f'<button class="icon" id="ptoggle" aria-label="Panel">{ic_html("layers")}</button>')
rex(r'<button class="icon" id="locate" title="My location">[^<]*</button>',
    f'<button class="icon" id="locate" title="My location" aria-label="My location">{ic_html("locate")}</button>')
_feat = iter(["map", "search", "file", "shield"])
rex(r'<div class="ic">[^<]*</div>', "@@FEAT@@", count=4)
while "@@FEAT@@" in s:
    s = s.replace("@@FEAT@@", f'<div class="ic">{ic_html(next(_feat), 24)}</div>', 1)

MODESEG = '<div class="seg modeSeg"><button data-mode="dataset" data-i18n="mode_dataset"></button><button data-mode="api" data-i18n="mode_api"></button></div>'
rep('<div class="seg" id="modeSeg"><button data-mode="dataset" data-i18n="mode_dataset"></button><button data-mode="api" data-i18n="mode_api"></button></div>', MODESEG)
rep('<div class="seg" id="roleSeg">', MODESEG + '\n    <div class="seg" id="roleSeg">')

# =====================================================================================  FRONT-END: JS
ICONS_JS = r'''const IC={
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
'''
rep("let lang=localStorage.lang||'bn',", ICONS_JS + "let lang=localStorage.lang||'bn',")
rep("function applyStatic(){document.documentElement.lang=lang;",
    "function applyStatic(){document.documentElement.lang=lang;$$('[data-ic]').forEach(e=>e.innerHTML=ico(e.dataset.ic,+e.dataset.s||18));")

rex(r"^const api=async.*$", r'''const api=async(u,o={})=>{const h={...(o.headers||{}),'X-Data-Mode':dataMode};try{const k=sessionStorage.getItem('adminKey');if(k)h['X-Admin-Key']=k}catch(e){}
  const r=await fetch(u,{...o,headers:h});let j={};try{j=await r.json()}catch(e){}
  if(r.status===401&&u.startsWith('/api/admin')&&!o._retry){const k=prompt(t('a_key'));if(k){try{sessionStorage.setItem('adminKey',k)}catch(e){}return api(u,{...o,_retry:1})}}
  if(!r.ok)throw Object.assign(new Error(j.error||r.status),{status:r.status});return j};''', flags=re.M)

NEW_T = ('src_demo:["ডেমো","Demo"],'
 'a_new_fraud:["নতুন জালিয়াতি সতর্কতা","New fraud alert"],a_new_case:["নতুন নামজারি কেস","New mutation case"],'
 'a_add_alert:["সতর্কতা যোগ","Add alert"],a_add_case:["কেস যোগ","Add case"],a_ds:["ডেটাসেট / মৌজা","Dataset / mouza"],'
 'a_ftype:["জালিয়াতির ধরন","Fraud type"],a_sev:["তীব্রতা","Severity"],a_sev_auto:["স্বয়ংক্রিয়","Automatic"],'
 'a_conf:["আস্থা (%)","Confidence (%)"],a_note_l:["মন্তব্য (ঐচ্ছিক)","Note (optional)"],'
 'a_buyer:["ক্রেতা / আবেদনকারী","Buyer / applicant"],a_seller:["বিক্রেতা","Seller"],a_deed_no:["দলিল নং (ঐচ্ছিক)","Deed no. (optional)"],'
 'a_deed_date:["দলিলের তারিখ","Deed date"],a_create:["তৈরি করুন","Create"],a_cancel:["বাতিল","Cancel"],'
 'a_created:["সফলভাবে যোগ হয়েছে","Added successfully"],a_delete:["মুছুন","Delete"],a_custom:["ম্যানুয়াল","Manual"],'
 'a_confirm_del:["এটি মুছে ফেলবেন?","Delete this?"],a_key:["অ্যাডমিন কী দিন","Enter the admin key"],'
 'a_need_plot:["ডেটাসেট ও দাগ নম্বর দিন","Choose a dataset and plot no."],a_plot_hint:["এই ডেটাসেটের দাগ থেকে বেছে নিন","Pick from this dataset\'s plots"],'
 'a_mode_scope:["এটি শুধু বর্তমান ডেটা মোডে যোগ হবে","This is added to the current data mode only"],a_noplots:["এই মোডে কোনো ডেটাসেট নেই","No dataset in this mode"]')
rex(r'src_demo:\["[^"]*","Demo"\]', NEW_T)
rex(r'm_refreshed:\["[^"]*","Data refreshed [^"]*"\]', 'm_refreshed:["ডেটা হালনাগাদ হয়েছে","Data refreshed"]')

rep("$$('#modeSeg button')", "$$('.modeSeg button')")
rep("$('#modeSeg').onclick=e=>{const b=e.target.closest('button');if(b&&b.dataset.mode!==dataMode)switchMode(b.dataset.mode)};",
    "$$('.modeSeg').forEach(sg=>sg.onclick=e=>{const b=e.target.closest('button');if(b&&b.dataset.mode!==dataMode)switchMode(b.dataset.mode)});")

# admin shortcuts in the map plot drawer
rep("${t('d_gmaps')}</a></div>`;",
    "${t('d_gmaps')}</a></div>${role==='admin'?`<div class=\"btnrow\" style=\"margin-top:8px\"><button class=\"btn sm danger\" id=\"dna\">${ico('alert',15)} ${t('a_add_alert')}</button><button class=\"btn sm alt\" id=\"dnc\">${ico('clipboard',15)} ${t('a_add_case')}</button></div>`:''}`;")
rep("$('#dx').onclick=()=>{dr.hidden=true;",
    "if(role==='admin'){$('#dna').onclick=()=>openCreate('fraud',{ds,plot:no});$('#dnc').onclick=()=>openCreate('case',{ds,plot:no})}\n    $('#dx').onclick=()=>{dr.hidden=true;")

ADMIN_TAIL = r'''const STS=['new','investigating','resolved','false_positive'];
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

'''
rex(r"async function aFraud\(\)\{.*?(?=/\* ---------- boot)", ADMIN_TAIL, flags=re.S)

# =====================================================================================  emoji -> SVG icon (everything that is left)
rep("{pass:'✓',warn:'!',fail:'✕',queued:'…'}", "{pass:ico('check',14),warn:ico('alert',14),fail:ico('x',14),queued:ico('clock',14)}", count=2)
EMO = {'🔎': ('search', 15), '🗺': ('map', 15), '🔗': ('link', 15), '🌐': ('globe', 15), '🖨': ('printer', 15),
       '📍': ('pin', 15), '🔒': ('lock', 22), '▶': ('play', 14), '⏸': ('pause', 14), '✔': ('okc', 15),
       '✕': ('x', 15), '✓': ('check', 15)}
for ch, (name, size) in EMO.items():
    s = re.sub(re.escape(ch) + "[\ufe0e\ufe0f]?", lambda m, n=name, z=size: "${ico('%s',%d)}" % (n, z), s)

left = [(m.group(), s[max(0, m.start() - 40):m.end() + 20].replace("\n", " "))
        for m in re.finditer("[\u2600-\u27bf\u2b00-\u2bff\u23e9-\u23fa\u2630\U0001f300-\U0001faff]", s)]
dst.write_text(s, encoding="utf-8")
print(f"OK  ->  {dst}")
if left:
    print("WARNING: emoji/symbols still present (replace by hand):")
    for ch, ctx in left:
        print("   ", ch, "...", ctx)
print("Run:  python", dst.name, "  (add --dev while developing)")