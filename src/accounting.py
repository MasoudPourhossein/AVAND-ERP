"""Accounting: vouchers (auto + manual), journal, ledgers (kol / moein / tafsili), trial balance, P&L, balance sheet, cost centers."""
from flask import render_template, request, redirect, url_for, flash, abort
from core import app, q, x, v1, db, next_no, to_int, uid, uname, period, can
from jalali import today_j, norm_date, year_start
from reports import rep, link
import export
import gl

KIND_LABEL = {"auto": "خودکار", "manual": "دستی"}


def src_link(src_type, src_id):
    if not src_type or not src_id:
        return None
    if src_type == "FCT":
        return url_for("invoice", iid=src_id)
    if src_type == "PUR":
        return url_for("purchase_view", pid=src_id)
    if src_type == "RES":
        return url_for("receipt_view", rid=src_id)
    if src_type in ("AMN", "MRJ"):
        return url_for("amani_view", cid=src_id)
    if src_type == "TRD":
        return url_for("tr_view", did=src_id)
    if src_type == "CHQ":
        c = v1("SELECT cheque_id FROM cheque_events WHERE id=?", (src_id,))
        return url_for("cheque_view", cid=c) if c else None
    return None


def tafsili_name(r):
    return r.get("pname") or (("بانک " + r["bname"]) if r.get("bname") else "") or (("صندوق " + r["xname"]) if r.get("xname") else "") or ""


VL_SQL = """SELECT vl.*, a.code acode, a.name aname, p.name pname, b.name bname, xb.name xname, cc.name ccname
  FROM voucher_lines vl JOIN accounts a ON a.id=vl.account_id LEFT JOIN persons p ON p.id=vl.person_id LEFT JOIN banks b ON b.id=vl.bank_id
  LEFT JOIN cash_boxes xb ON xb.id=vl.box_id LEFT JOIN cost_centers cc ON cc.id=vl.cc_id"""


# ---------------- vouchers
@app.route("/vouchers")
def vouchers():
    f, t = period()
    kind = request.args.get("kind", "")
    w, a = " WHERE v.date BETWEEN ? AND ?", [f, t]
    if kind in KIND_LABEL:
        w += " AND v.kind=?"; a.append(kind)
    rows = q(f"""SELECT v.*, (SELECT SUM(debit) FROM voucher_lines WHERE voucher_id=v.id) amt FROM vouchers v {w}
                 ORDER BY v.date DESC, v.no DESC LIMIT 3000""", a)
    R = [[link(r["no"], url_for("voucher_view", vid=r["id"])), r["date"], KIND_LABEL[r["kind"]], gl.SRC_LABEL.get(r["src_type"], r["src_type"] or ""),
          link(r["descr"] or "", src_link(r["src_type"], r["src_id"])), r["amt"]] for r in rows]
    spec = {"title": "اسناد حسابداری", "sub": f"از {f} تا {t}", "filters": ["period"],
            "selects": [("kind", [("", "همه اسناد"), ("auto", "خودکار (از عملیات)"), ("manual", "دستی")], kind)],
            "actions": [(url_for("voucher_new"), "سند دستی جدید", "g")] if can("accounting") else [],
            "sections": [{"cols": [("شماره", "text"), ("تاریخ", "text"), ("نوع", "text"), ("منبع", "text"), ("شرح", "text"), ("مبلغ", "money")],
                          "rows": R, "total": ["جمع", "", "", "", "", sum(r[5] for r in R)]}]}
    return export.out(spec)


@app.route("/vouchers/<int:vid>")
def voucher_view(vid):
    v = q("SELECT * FROM vouchers WHERE id=?", (vid,), one=True) or abort(404)
    lines = q(VL_SQL + " WHERE vl.voucher_id=? ORDER BY vl.debit DESC, vl.id", (vid,))
    return render_template("voucher_view.html", v=v, lines=lines, src=src_link(v["src_type"], v["src_id"]), SRC=gl.SRC_LABEL, tname=tafsili_name, uname=uname)


def _save_manual(vid=None):
    F = request.form
    date = norm_date(F.get("date"))
    if not date:
        raise ValueError("تاریخ نامعتبر است.")
    rows = []
    accs = F.getlist("acc")
    for i, aid in enumerate(accs):
        g_ = lambda k: (F.getlist(k)[i] if i < len(F.getlist(k)) else "")
        dr, cr = to_int(g_("dr")), to_int(g_("cr"))
        if not to_int(aid) or (not dr and not cr):
            continue
        if dr and cr:
            raise ValueError("هر ردیف فقط بدهکار یا بستانکار باشد.")
        if v1("SELECT level FROM accounts WHERE id=?", (to_int(aid),)) != 3:
            raise ValueError("فقط حساب‌های معین قابل ثبت هستند.")
        rows.append({"acc": to_int(aid), "dr": dr, "cr": cr, "person": to_int(g_("person")) or None, "bank": to_int(g_("bank")) or None,
                     "box": to_int(g_("box")) or None, "cc": to_int(g_("cc")) or None, "descr": g_("ldescr").strip() or None})
    if len(rows) < 2:
        raise ValueError("سند حداقل دو ردیف لازم دارد.")
    td, tc = sum(r["dr"] for r in rows), sum(r["cr"] for r in rows)
    if td != tc:
        raise ValueError(f"سند تراز نیست: بدهکار {td:,} ≠ بستانکار {tc:,}")
    descr = (F.get("descr") or "").strip() or None
    if vid:
        x("UPDATE vouchers SET date=?, descr=? WHERE id=?", (date, descr, vid))
        x("DELETE FROM voucher_lines WHERE voucher_id=?", (vid,))
    else:
        vid = x("INSERT INTO vouchers(no,date,kind,src_type,descr,created_by) VALUES(?,?,'manual','MAN',?,?)", (next_no("VCH"), date, descr, uid()))
    for r in rows:
        x("INSERT INTO voucher_lines(voucher_id,account_id,person_id,bank_id,box_id,cc_id,debit,credit,descr) VALUES(?,?,?,?,?,?,?,?,?)",
          (vid, r["acc"], r["person"], r["bank"], r["box"], r["cc"], r["dr"], r["cr"], r["descr"]))
    return vid


def _form(v=None, lines=None):
    from treasury import persons_all, boxes, banks, leaf_accounts
    return render_template("voucher_form.html", v=v or {}, lines=[dict(l) for l in lines or []], accounts=leaf_accounts(), persons=persons_all(), boxes=boxes(), banks=banks(),
                           ccs=q("SELECT * FROM cost_centers WHERE active=1 ORDER BY code"))


@app.route("/vouchers/new", methods=["GET", "POST"])
def voucher_new():
    if request.method == "POST":
        try:
            vid = _save_manual()
            db().commit()
            flash("سند حسابداری ثبت شد.", "ok")
            return redirect(url_for("voucher_view", vid=vid))
        except ValueError as e:
            db().rollback()
            flash(str(e), "bad")
    return _form()


@app.route("/vouchers/<int:vid>/edit", methods=["GET", "POST"])
def voucher_edit(vid):
    v = q("SELECT * FROM vouchers WHERE id=?", (vid,), one=True) or abort(404)
    if v["kind"] != "manual":
        flash("اسناد خودکار از روی عملیات ساخته می‌شوند؛ سند مبدا را ویرایش کنید.", "bad")
        return redirect(url_for("voucher_view", vid=vid))
    if request.method == "POST":
        try:
            _save_manual(vid)
            db().commit()
            flash("سند ذخیره شد.", "ok")
            return redirect(url_for("voucher_view", vid=vid))
        except ValueError as e:
            db().rollback()
            flash(str(e), "bad")
    return _form(v, q("SELECT * FROM voucher_lines WHERE voucher_id=? ORDER BY id", (vid,)))


@app.post("/vouchers/<int:vid>/delete")
def voucher_delete(vid):
    v = q("SELECT * FROM vouchers WHERE id=?", (vid,), one=True) or abort(404)
    if v["kind"] != "manual":
        flash("سند خودکار قابل حذف نیست.", "bad")
        return redirect(url_for("voucher_view", vid=vid))
    x("DELETE FROM vouchers WHERE id=?", (vid,))
    db().commit()
    flash(f"سند {v['no']} حذف شد.", "ok")
    return redirect(url_for("vouchers"))


@app.post("/vouchers/repost")
def repost():
    n = gl.repost_all()
    db().commit()
    flash(f"اسناد خودکار حسابداری از روی {n} سند عملیاتی بازسازی شد.", "ok")
    return redirect(url_for("vouchers"))


# ---------------- GL reports
def _acc_tree():
    return q("SELECT id, code, name, level, parent_id, nature FROM accounts ORDER BY code")


def _desc(aid):
    """account id + all descendants"""
    ids, todo = [aid], [aid]
    kids = {}
    for a in _acc_tree():
        kids.setdefault(a["parent_id"], []).append(a["id"])
    while todo:
        n = todo.pop()
        for k in kids.get(n, []):
            ids.append(k); todo.append(k)
    return ids


def _sums(f, t):
    """leaf account -> (opening, dr, cr) ; opening = balance before f"""
    out = {}
    for r in q("""SELECT vl.account_id a, SUM(CASE WHEN v.date<? THEN vl.debit-vl.credit ELSE 0 END) op,
                  SUM(CASE WHEN v.date BETWEEN ? AND ? THEN vl.debit ELSE 0 END) dr, SUM(CASE WHEN v.date BETWEEN ? AND ? THEN vl.credit ELSE 0 END) cr
                  FROM voucher_lines vl JOIN vouchers v ON v.id=vl.voucher_id WHERE v.date<=? GROUP BY vl.account_id""", (f, f, t, f, t, t)):
        out[r["a"]] = (r["op"], r["dr"], r["cr"])
    return out


def _rollup(sums):
    accs = _acc_tree()
    byid = {a["id"]: dict(a, op=0, dr=0, cr=0) for a in accs}
    for aid, (op, dr, cr) in sums.items():
        n = byid.get(aid)
        while n:
            n["op"] += op; n["dr"] += dr; n["cr"] += cr
            n = byid.get(n["parent_id"])
    return [byid[a["id"]] for a in accs]


def _dc(v):
    return (v, 0) if v > 0 else (0, -v)


@rep("/reports/gl/trial", "gl_trial", "rep_fin")
def gl_trial():
    f, t = period(default_from=year_start(today_j()))
    lvl = to_int(request.args.get("level")) or 3
    rows = []
    T = [0] * 6
    for a in _rollup(_sums(f, t)):
        if a["level"] != lvl or not (a["op"] or a["dr"] or a["cr"]):
            continue
        cl = a["op"] + a["dr"] - a["cr"]
        r = [a["code"], link(a["name"], url_for("gl_ledger", acc=a["id"], **{"from": f, "to": t})), *_dc(a["op"]), a["dr"], a["cr"], *_dc(cl)]
        rows.append(r)
        for i in range(6):
            T[i] += r[2 + i]
    spec = {"title": "تراز آزمایشی", "sub": f"از {f} تا {t} — سطح {['', 'گروه', 'کل', 'معین'][lvl]}", "filters": ["period"],
            "selects": [("level", [("1", "سطح گروه"), ("2", "سطح کل"), ("3", "سطح معین")], str(lvl))],
            "kpis": [("گردش بدهکار", T[2], "money"), ("گردش بستانکار", T[3], "money"), ("اختلاف (باید صفر باشد)", T[2] - T[3], "money")],
            "sections": [{"cols": [("کد", "text"), ("حساب", "text"), ("مانده اول بدهکار", "money"), ("مانده اول بستانکار", "money"), ("گردش بدهکار", "money"),
                                   ("گردش بستانکار", "money"), ("مانده بدهکار", "money"), ("مانده بستانکار", "money")],
                          "rows": rows, "total": ["جمع", ""] + T}]}
    return export.out(spec)


def _ledger_rows(w, a, f, t, op_label="مانده از قبل"):
    op = v1(f"SELECT SUM(vl.debit-vl.credit) FROM voucher_lines vl JOIN vouchers v ON v.id=vl.voucher_id WHERE v.date<?{w}", [f] + a)
    lines = q(VL_SQL.replace("FROM voucher_lines vl", ", v.no vno, v.date, v.descr vdescr, v.src_type, v.src_id, v.id vid FROM voucher_lines vl JOIN vouchers v ON v.id=vl.voucher_id")
              + f" WHERE v.date BETWEEN ? AND ?{w} ORDER BY v.date, v.no, vl.id", [f, t] + a)
    bal, rows, dr, cr = op, [["", "", op_label, "", "", "", "", op, "بد" if op > 0 else ("بس" if op < 0 else "—")]], 0, 0
    for l in lines:
        bal += l["debit"] - l["credit"]; dr += l["debit"]; cr += l["credit"]
        rows.append([l["date"], link(l["vno"], url_for("voucher_view", vid=l["vid"])), link(l["descr"] or l["vdescr"] or "", src_link(l["src_type"], l["src_id"])),
                     f"{l['acode']} {l['aname']}", tafsili_name(l), l["debit"] or "", l["credit"] or "", bal, "بد" if bal > 0 else ("بس" if bal < 0 else "—")])
    cols = [("تاریخ", "text"), ("سند", "text"), ("شرح", "text"), ("حساب", "text"), ("تفصیلی", "text"), ("بدهکار", "money"), ("بستانکار", "money"),
            ("مانده", "money"), ("تشخیص", "text")]
    return {"cols": cols, "rows": rows, "total": ["جمع دوره", "", "", "", "", dr, cr, bal, ""]}, op, dr, cr, bal


@rep("/reports/gl/ledger", "gl_ledger", "rep_fin")
def gl_ledger():
    f, t = period(default_from=year_start(today_j()))
    accs = _acc_tree()
    aid = to_int(request.args.get("acc")) or (accs[0]["id"] if accs else 0)
    a = next((r for r in accs if r["id"] == aid), None) or abort(404)
    ids = _desc(aid)
    sec, op, dr, cr, bal = _ledger_rows(" AND vl.account_id = ANY(?)", [ids], f, t)
    spec = {"title": f"دفتر {['', 'گروه', 'کل', 'معین'][a['level']]}: {a['code']} - {a['name']}", "sub": f"از {f} تا {t}", "filters": ["period"],
            "selects": [("acc", [(str(r["id"]), f"{'— ' * (r['level'] - 1)}{r['code']} - {r['name']}") for r in accs], str(aid))],
            "kpis": [("مانده اول", op, "money"), ("گردش بدهکار", dr, "money"), ("گردش بستانکار", cr, "money"), ("مانده پایان", bal, "money")],
            "sections": [sec]}
    return export.out(spec)


@rep("/reports/gl/tafsili", "gl_tafsili", "rep_fin")
def gl_tafsili():
    f, t = period(default_from=year_start(today_j()))
    typ = request.args.get("t") if request.args.get("t") in ("person", "bank", "box", "cc") else "person"
    src = {"person": ("persons", "person_id", "شخص"), "bank": ("banks", "bank_id", "بانک"), "box": ("cash_boxes", "box_id", "صندوق"),
           "cc": ("cost_centers", "cc_id", "مرکز هزینه")}[typ]
    opts = q(f"SELECT id, code, name FROM {src[0]} ORDER BY code")
    i = to_int(request.args.get("id")) or (opts[0]["id"] if opts else 0)
    w, a = f" AND vl.{src[1]}=?", [i]
    if typ in ("bank", "box"):   # only money accounts (not cheques-in-transit etc.)
        w += " AND vl.account_id IN (?,?)"; a += [gl.acc("cash"), gl.acc("bank")]
    sec, op, dr, cr, bal = _ledger_rows(w, a, f, t)
    nm = next((o["name"] for o in opts if o["id"] == i), "")
    spec = {"title": f"دفتر تفصیلی {src[2]}: {nm}", "sub": f"از {f} تا {t}", "filters": ["period"],
            "selects": [("t", [("person", "اشخاص"), ("bank", "بانک‌ها"), ("box", "صندوق‌ها"), ("cc", "مراکز هزینه")], typ),
                        ("id", [(str(o["id"]), f"{o['code']} - {o['name']}") for o in opts], str(i))],
            "kpis": [("مانده اول", op, "money"), ("گردش بدهکار", dr, "money"), ("گردش بستانکار", cr, "money"), ("مانده پایان", bal, "money")],
            "sections": [sec]}
    return export.out(spec)


@rep("/reports/gl/journal", "gl_journal", "rep_fin")
def gl_journal():
    f, t = period(default_from=today_j())
    lines = q(VL_SQL.replace("FROM voucher_lines vl", ", v.no vno, v.date, v.descr vdescr, v.id vid FROM voucher_lines vl JOIN vouchers v ON v.id=vl.voucher_id")
              + " WHERE v.date BETWEEN ? AND ? ORDER BY v.date, v.no, vl.debit DESC, vl.id LIMIT 20000", (f, t))
    rows = [[l["date"], link(l["vno"], url_for("voucher_view", vid=l["vid"])), l["acode"], l["aname"], tafsili_name(l), l["descr"] or l["vdescr"] or "",
             l["debit"] or "", l["credit"] or ""] for l in lines]
    D, C = sum(l["debit"] for l in lines), sum(l["credit"] for l in lines)
    spec = {"title": "دفتر روزنامه", "sub": f"از {f} تا {t}", "filters": ["period"],
            "sections": [{"cols": [("تاریخ", "text"), ("سند", "text"), ("کد حساب", "text"), ("حساب", "text"), ("تفصیلی", "text"), ("شرح", "text"),
                                   ("بدهکار", "money"), ("بستانکار", "money")], "rows": rows, "total": ["جمع", "", "", "", "", "", D, C]}]}
    return export.out(spec)


def _bal_by_root(sums):
    out = {}
    for a in _rollup(sums):
        out[a["code"]] = a
    return out


@rep("/reports/gl/pl", "gl_pl", "rep_fin")
def gl_pl():
    f, t = period(default_from=year_start(today_j()))
    R = _rollup(_sums(f, t))
    rows, tot = [], {}
    for a in R:
        if a["code"][0] not in "678" or a["level"] == 1:
            continue
        mv = a["cr"] - a["dr"]   # + = income
        if a["level"] == 3 and mv:
            rows.append(["", a["code"], link(a["name"], url_for("gl_ledger", acc=a["id"], **{"from": f, "to": t})), mv])
        elif a["level"] == 2 and (a["dr"] or a["cr"]):
            rows.append([{"v": a["name"]}, a["code"], "", mv])
        tot[a["code"][0]] = tot.get(a["code"][0], 0) + (mv if a["level"] == 3 else 0)
    rev, cogs, exp = tot.get("6", 0), tot.get("7", 0), tot.get("8", 0)
    spec = {"title": "صورت سود و زیان", "sub": f"از {f} تا {t}", "filters": ["period"],
            "kpis": [("درآمد خالص", rev, "money"), ("بهای تمام‌شده", -cogs, "money"), ("سود ناخالص", rev + cogs, "money"), ("سود (زیان) خالص", rev + cogs + exp, "money")],
            "sections": [{"cols": [("سرفصل", "text"), ("کد", "text"), ("حساب", "text"), ("مبلغ (+ درآمد / − هزینه)", "money")], "rows": rows,
                          "total": ["سود (زیان) خالص", "", "", rev + cogs + exp]},
                         {"title": "خلاصه", "cols": [("شرح", "text"), ("مبلغ", "money")],
                          "rows": [["درآمدها (فروش خالص و سایر)", rev], ["بهای تمام‌شده کالای فروش‌رفته", cogs], ["سود ناخالص", rev + cogs],
                                   ["هزینه‌های عملیاتی و سایر", exp], ["سود (زیان) خالص دوره", rev + cogs + exp]]}]}
    return export.out(spec)


@rep("/reports/gl/bs", "gl_bs", "rep_fin")
def gl_bs():
    t = norm_date(request.args.get("to") or "") or today_j()
    R = _rollup(_sums("0000/00/00", t))
    secs = []
    profit = -sum(a["dr"] - a["cr"] for a in R if a["level"] == 1 and a["code"][0] in "678")
    for root, title, sg in (("1", "دارایی‌های جاری", 1), ("2", "دارایی‌های غیرجاری", 1), ("3", "بدهی‌های جاری", -1), ("5", "حقوق مالکانه", -1)):
        rows = []
        for a in R:
            if a["code"][0] != root or a["level"] != 3:
                continue
            b = sg * (a["dr"] - a["cr"])
            if b:
                rows.append([a["code"], link(a["name"], url_for("gl_ledger", acc=a["id"], to=t, **{"from": "1300/01/01"})), b])
        if root == "5" and profit:
            rows.append(["", "سود (زیان) دوره جاری (بسته نشده)", profit])
        secs.append({"title": title, "cols": [("کد", "text"), ("حساب", "text"), ("مبلغ", "money")], "rows": rows,
                     "total": ["جمع", "", sum(r[2] for r in rows)]})
    assets = sum(s["total"][2] for s in secs[:2])
    liab = sum(s["total"][2] for s in secs[2:])
    spec = {"title": "ترازنامه", "sub": f"به تاریخ {t}", "filters": [], "inputs": [("to", "تا تاریخ", t)],
            "kpis": [("جمع دارایی‌ها", assets, "money"), ("جمع بدهی‌ها و حقوق مالکانه", liab, "money"), ("اختلاف", assets - liab, "money")],
            "sections": secs}
    return export.out(spec)


@rep("/reports/gl/cc", "gl_cc", "rep_fin")
def gl_cc():
    f, t = period(default_from=year_start(today_j()))
    rows = q("""SELECT cc.code ccode, cc.name ccname, a.code, a.name, SUM(vl.debit) dr, SUM(vl.credit) cr
                FROM voucher_lines vl JOIN vouchers v ON v.id=vl.voucher_id JOIN cost_centers cc ON cc.id=vl.cc_id JOIN accounts a ON a.id=vl.account_id
                WHERE v.date BETWEEN ? AND ? GROUP BY 1,2,3,4 ORDER BY 1,3""", (f, t))
    R = [[r["ccode"], r["ccname"], r["code"], r["name"], r["dr"], r["cr"], r["dr"] - r["cr"]] for r in rows]
    spec = {"title": "گزارش مراکز هزینه", "sub": f"از {f} تا {t}", "filters": ["period"],
            "sections": [{"cols": [("کد مرکز", "text"), ("مرکز هزینه", "text"), ("کد حساب", "text"), ("حساب", "text"), ("بدهکار", "money"),
                                   ("بستانکار", "money"), ("خالص (بدهکار)", "money")], "rows": R,
                          "total": ["جمع", "", "", "", sum(r[4] for r in R), sum(r[5] for r in R), sum(r[6] for r in R)]}]}
    return export.out(spec)
