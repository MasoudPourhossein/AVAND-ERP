"""Stock documents: receipts (رسید), purchases (خرید/برگشت از خرید), transfers (حواله بین انبار)."""
import io
from flask import send_file, render_template, request, redirect, url_for, flash, abort
from core import qty_by_item
from core import (app, q, x, v1, db, next_no, to_int, uid, uname, stock_rows, add_moves, del_moves, check_stock, default_wh,
                  warehouses_list, period, money, pkr, DOC, can)
from jalali import today_j, norm_date
import export


def parse_lines(with_price=False, full=False):
    """-> [(item_id, qty, price)] or with full=True [(item_id, qty, price, disc, vat_rate)]"""
    ids = request.form.getlist("item_id")
    ctns, boxes = request.form.getlist("ctn"), request.form.getlist("box")
    if not ctns:
        ctns, boxes = [0] * len(ids), request.form.getlist("qty")
    prices = request.form.getlist("price") if with_price else []
    prices += [0] * (len(ids) - len(prices))
    discs, vats = request.form.getlist("disc"), request.form.getlist("vat")
    discs += [0] * (len(ids) - len(discs)); vats += [""] * (len(ids) - len(vats))
    packs = {r["id"]: r["pack"] or 1 for r in q("SELECT id, pack FROM items")}
    lines, seen = [], set()
    for i, ct, bx, pr, ds, vr in zip(ids, ctns, boxes, prices, discs, vats):
        i, ct, bx, pr, ds = to_int(i), to_int(ct), to_int(bx), to_int(pr), to_int(ds)
        try:
            vr = float(str(vr).replace("٫", ".").strip() or 0)
        except ValueError:
            raise ValueError("نرخ مالیات نامعتبر است.")
        if not i:
            continue
        if ct < 0 or bx < 0 or pr < 0 or ds < 0 or not (0 <= vr <= 100):
            raise ValueError("عدد منفی مجاز نیست.")
        qt = ct * packs.get(i, 1) + bx
        if qt <= 0:
            continue
        if i in seen and request.form.get("allow_dup") != "1":
            nm = v1("SELECT name FROM items WHERE id=?", (i,))
            raise ValueError(f"کالای «{nm}» در چند ردیف آمده است؛ اگر عمدی است، تکرار را در همان ردیف تایید کنید.")
        seen.add(i)
        if ds > qt * pr:
            raise ValueError("تخفیف یک ردیف از مبلغ آن بیشتر است.")
        lines.append((i, qt, pr, ds, vr) if full else (i, qt, pr))
    if not lines:
        raise ValueError("حداقل یک ردیف کالا با تعداد معتبر وارد کنید.")
    return lines


def stock_map():
    out = {}
    for r in q("SELECT item_id, wh_id, SUM(qty) s FROM stock_moves GROUP BY item_id, wh_id"):
        out.setdefault(r["item_id"], {})[r["wh_id"]] = r["s"]
    return out


def form_ctx(mode, parties=(), visitors=(), doc=None, init_lines=(), own_moves=None):
    from core import vat_default, setting
    sm = stock_map()
    for (i, w), qv in (own_moves or {}).items():   # editing: this document's own outflow is available again
        sm.setdefault(i, {})[w] = sm.get(i, {}).get(w, 0) - qv
    if request.method == "POST" and request.form.getlist("item_id"):
        init_lines = posted_lines()
    return dict(mode=mode, items=stock_rows(), whs=warehouses_list(), wh=default_wh(), stock_map=sm,
                parties=parties, visitors=visitors, doc=doc, init_lines=list(init_lines),
                sups=q("SELECT id, code, name FROM persons WHERE is_supplier=1 ORDER BY code") if mode == "receipt" else [],
                vat_on=setting("vat_on", "0") == "1", vat_def=vat_default())


def posted_lines():
    F = request.form
    cols = {k: F.getlist(k) for k in ("item_id", "ctn", "box", "price", "disc", "vat")}
    out = []
    for n, i in enumerate(cols["item_id"]):
        g_ = lambda k: cols[k][n] if n < len(cols[k]) else ""
        if to_int(i):
            out.append({"id": to_int(i), "ctn": to_int(g_("ctn")) or "", "box": to_int(g_("box")) or "", "price": to_int(g_("price")),
                        "disc": to_int(g_("disc")), "vat": g_("vat") or 0})
    return out


def line_vat(qt, pr, ds, vr):
    return int(round((qt * pr - ds) * vr / 100.0))


def doc_init_lines(table, fk, did):
    out = []
    for l in q(f"SELECT l.*, i.pack FROM {table} l JOIN items i ON i.id=l.item_id WHERE l.{fk}=? ORDER BY l.id", (did,)):
        k = l["pack"] or 1
        c, b = divmod(l["qty"], k) if k > 1 else (0, l["qty"])
        out.append({"id": l["item_id"], "ctn": c, "box": b, "price": l["price"], "disc": l.get("disc", 0), "vat": l.get("vat_rate", 0)})
    return out


def doc_out(doc, **kw):
    if request.args.get("fmt") == "pdf":
        return export.doc_pdf(doc)
    return render_template("doc_view.html", doc=doc, **kw)


def wh_valid(wid):
    return bool(wid and q("SELECT 1 FROM warehouses WHERE id=? AND active=1", (wid,), one=True))


def item_lines(table, fk, did):
    return q(f"SELECT l.*, i.code, i.name, i.unit, i.pack, i.pack_unit FROM {table} l JOIN items i ON i.id=l.item_id WHERE l.{fk}=? ORDER BY l.id", (did,))


# ================= receipts =================
def make_receipt(date, wh, kind, note, lines, credit=None, person_id=None):
    """lines: (item_id, qty, unit_cost) -> (no, id). credit: None | 'items' (each item's supplier) | 'person' (person_id)"""
    credit = credit if credit in ("items", "person") else None
    if credit == "person" and not person_id:
        credit = None
    no = next_no("RES")
    rid = x("INSERT INTO receipts(no,date,wh_id,kind,note,created_by,credit,person_id) VALUES(?,?,?,?,?,?,?,?)",
            (no, date, wh, kind, note, uid(), credit, person_id if credit == "person" else None))
    for i, qt, c in lines:
        x("INSERT INTO receipt_lines(receipt_id,item_id,qty,unit_cost) VALUES(?,?,?,?)", (rid, i, qt, c or 0))
    add_moves("RES", rid, no, date, [(i, wh, qt, c) for i, qt, c in lines])
    import gl
    gl.post_receipt(rid)
    return no, rid


@app.route("/receipts")
def receipts():
    f, t = period()
    rows = q("""SELECT r.*, w.name whname, COALESCE(SUM(l.qty),0) qty, COALESCE(SUM(l.qty*l.unit_cost),0) val, COUNT(l.id) n FROM receipts r
                JOIN warehouses w ON w.id=r.wh_id LEFT JOIN receipt_lines l ON l.receipt_id=r.id WHERE r.date BETWEEN ? AND ?
                GROUP BY r.id, w.name ORDER BY r.date DESC, r.no DESC""", (f, t))
    kinds = {"opening": "موجودی اولیه", "adjust": "انبارگردانی"}
    x_ = export.list_out("رسیدهای انبار", f"از {f} تا {t}", [("شماره", "text"), ("تاریخ", "text"), ("انبار", "text"), ("نوع", "text"), ("توضیح", "text"),
                         ("تعداد اقلام", "num"), ("جمع تعداد", "num"), ("ارزش (ریال)", "money")],
                         [[r["no"], r["date"], r["whname"], kinds.get(r["kind"], "ورود متفرقه"), r["note"] or "", r["n"], r["qty"], r["val"]] for r in rows],
                         ["جمع", "", "", "", "", sum(r["n"] for r in rows), sum(r["qty"] for r in rows), sum(r["val"] for r in rows)])
    if x_:
        return x_
    return render_template("receipts.html", rows=rows, f=f, t=t)


@app.route("/receipts/new", methods=["GET", "POST"])
def receipt_new():
    if request.method == "POST":
        try:
            date, wh = norm_date(request.form.get("date")), to_int(request.form.get("wh_id"))
            if not date:
                raise ValueError("تاریخ نامعتبر است.")
            if not wh_valid(wh):
                raise ValueError("انبار نامعتبر است.")
            no, rid = make_receipt(date, wh, "in", request.form.get("note"), parse_lines(True), credit=request.form.get("credit") or None,
                                   person_id=to_int(request.form.get("credit_person")) or None)
            db().commit()
            flash(f"رسید انبار شماره {no} ثبت شد.", "ok")
            return redirect(url_for("receipt_view", rid=rid))
        except ValueError as e:
            db().rollback()
            flash(str(e), "bad")
    return render_template("doc_form.html", **form_ctx("receipt"))


def stock_count(date, wh, counted, note):
    """counted: {item_id: real qty (boxes)} -> adjustment receipt (kind 'adjust') with the differences, or None"""
    lines = []
    for i, real in counted.items():
        cur = v1("SELECT SUM(qty) FROM stock_moves WHERE item_id=? AND wh_id=?", (i, wh))
        if real != cur:
            from core import buy_at
            lines.append((i, real - cur, int(buy_at(i, date) or 0)))
    if not lines:
        return None, None, 0
    no, rid = make_receipt(date, wh, "adjust", note or "انبارگردانی", lines)
    return no, rid, len(lines)


@app.route("/stock-count", methods=["GET", "POST"])
def stock_count_page():
    wh = to_int(request.values.get("wh")) or default_wh()
    if not wh_valid(wh):
        wh = default_wh()
    F = request.form
    if request.method == "POST":
        date = norm_date(F.get("date"))
        try:
            if not date:
                raise ValueError("تاریخ نامعتبر است.")
            counted = {}
            for iid in F.getlist("item"):
                c, b = F.get(f"c_{iid}", "").strip(), F.get(f"b_{iid}", "").strip()
                if c == "" and b == "":
                    continue
                k = v1("SELECT pack FROM items WHERE id=?", (to_int(iid),)) or 1
                if to_int(c) < 0 or to_int(b) < 0:
                    raise ValueError("تعداد شمارش منفی نمی‌شود.")
                counted[to_int(iid)] = to_int(c) * k + to_int(b)
            no, rid, n = stock_count(date, wh, counted, (F.get("note") or "").strip())
            db().commit()
            if rid:
                flash(f"انبارگردانی ثبت شد: اصلاح موجودی {n} کالا با سند شماره {no}.", "ok")
                return redirect(url_for("receipt_view", rid=rid))
            flash("موجودی شمارش‌شده با سیستم یکی است؛ سندی لازم نبود.", "ok")
        except ValueError as e:
            db().rollback()
            flash(str(e), "bad")
        return redirect(url_for("stock_count_page", wh=wh))
    rows = [r for r in stock_rows(wh_id=wh) if r["active"] or r["st"]]
    sup, gid = to_int(request.args.get("supplier")), to_int(request.args.get("group"))
    if sup:
        rows = [r for r in rows if r["supplier_id"] == sup]
    if gid:
        rows = [r for r in rows if r["group_id"] == gid]
    from masters import suppliers, _groups
    return render_template("stock_count.html", rows=rows, wh=wh, whs=warehouses_list(), sups=suppliers(), groups=_groups(), sup=sup, gid=gid)


@app.route("/receipts/<int:rid>")
def receipt_view(rid):
    r = q("SELECT r.*, w.name whname FROM receipts r JOIN warehouses w ON w.id=r.wh_id WHERE r.id=?", (rid,), one=True) or abort(404)
    lines = item_lines("receipt_lines", "receipt_id", rid)
    credit = None
    if r["kind"] != "adjust":
        credit = {"mode": r["credit"] or "", "person_id": r["person_id"],
                  "pname": v1("SELECT name FROM persons WHERE id=?", (r["person_id"],)) if r["person_id"] else None,
                  "sups": q("SELECT id, code, name FROM persons WHERE is_supplier=1 ORDER BY code"),
                  "url": url_for("receipt_credit", rid=rid), "opening": r["kind"] == "opening"}
    doc = {"title": "اصلاح موجودی (انبارگردانی)" if r["kind"] == "adjust" else DOC["RES"] + (" (موجودی اولیه)" if r["kind"] == "opening" else ""), "no": r["no"], "date": r["date"], "note": r["note"],
           "extra": f"انبار: {r['whname']}",
           "cols": ["کد کالا", "نام کالا", "تعداد", "نرخ خرید واحد", "مبلغ"],
           "rows": [[l["code"], l["name"], pkr(l["qty"], l), money(l["unit_cost"]), money(l["qty"] * l["unit_cost"])] for l in lines],
           "foot": [("جمع مبلغ (ریال)", money(sum(l["qty"] * l["unit_cost"] for l in lines)))],
           "sign": ["تحویل‌دهنده", "انباردار"], "user": uname(r["created_by"])}
    return doc_out(doc, back=url_for("receipts"), del_url=url_for("receipt_delete", rid=rid), pdf_url=url_for("receipt_view", rid=rid, fmt="pdf"),
                   credit=credit)


@app.post("/receipts/<int:rid>/credit")
def receipt_credit(rid):
    """set who the received stock is owed to (capital / each item's supplier / one person) and repost"""
    r = q("SELECT * FROM receipts WHERE id=?", (rid,), one=True) or abort(404)
    mode = request.form.get("mode") if request.form.get("mode") in ("items", "person") else None
    pid = to_int(request.form.get("person_id")) or None
    if mode == "person" and not pid:
        flash("تامین‌کننده را انتخاب کنید.", "bad")
        return redirect(url_for("receipt_view", rid=rid))
    x("UPDATE receipts SET credit=?, person_id=? WHERE id=?", (mode, pid if mode == "person" else None, rid))
    import gl
    gl.post_receipt(rid)
    db().commit()
    flash("حساب طرف رسید اصلاح شد" + (" — تامین‌کننده(ها) بستانکار شدند." if mode else "."), "ok")
    return redirect(url_for("receipt_view", rid=rid))


def _can_remove_moves(doc_type, did):
    """removing a document's moves must not make any item negative in any warehouse"""
    for m in q("SELECT item_id, wh_id, SUM(qty) qty FROM stock_moves WHERE doc_type=? AND doc_id=? GROUP BY item_id, wh_id", (doc_type, did)):
        if m["qty"] > 0 and v1("SELECT SUM(qty) FROM stock_moves WHERE item_id=? AND wh_id=?", (m["item_id"], m["wh_id"])) - m["qty"] < 0:
            return False
    return True


@app.post("/receipts/<int:rid>/delete")
def receipt_delete(rid):
    if not _can_remove_moves("RES", rid):
        flash("حذف ممکن نیست: موجودی کالا در انبار منفی می‌شود.", "bad")
        return redirect(url_for("receipt_view", rid=rid))
    del_moves("RES", rid)
    x("DELETE FROM receipts WHERE id=?", (rid,))
    import gl
    gl.unpost("RES", rid)
    db().commit()
    flash("رسید حذف شد.", "ok")
    return redirect(url_for("receipts"))


# ================= purchases =================
PUR_LIST = """SELECT u.*, s.name sname, s.code scode, w.name whname, u.qty_total qty
  FROM v_pu u JOIN persons s ON s.id=u.supplier_id JOIN warehouses w ON w.id=u.wh_id"""


@app.route("/purchases")
def purchases():
    f, t = period()
    sid = to_int(request.args.get("supplier"))
    sql, a = PUR_LIST + " WHERE u.date BETWEEN ? AND ?", [f, t]
    if sid:
        sql += " AND u.supplier_id=?"; a.append(sid)
    rows = q(sql + " ORDER BY u.date DESC, u.no DESC", a)
    sups = q("SELECT id, code, name FROM persons WHERE is_supplier=1 ORDER BY code")
    R = [[r["no"], "برگشت از خرید" if r["kind"] == "return" else "خرید", r["date"], r["sname"], r["whname"], r["ref_no"] or "", r["qty"],
          r["net_total"] * r["sg"], r["vat_total"] * r["sg"], r["total"] * r["sg"]] for r in rows]
    x_ = export.list_out("فاکتورهای خرید", f"از {f} تا {t}", [("شماره", "text"), ("نوع", "text"), ("تاریخ", "text"), ("تامین‌کننده", "text"),
                         ("انبار", "text"), ("ش. فاکتور تامین‌کننده", "text"), ("تعداد", "num"), ("مبلغ بدون مالیات", "money"), ("مالیات", "money"),
                         ("جمع (ریال)", "money")], R, ["جمع", "", "", "", "", "", sum(r[6] for r in R), sum(r[7] for r in R), sum(r[8] for r in R), sum(r[9] for r in R)])
    if x_:
        return x_
    return render_template("purchases.html", rows=rows, f=f, t=t, sid=sid, sups=sups)


def _suppliers():
    return q("SELECT id, code, name, visitor_id, credit_days FROM persons WHERE is_supplier=1 AND active=1 ORDER BY code")


def _save_purchase(pid=None):
    """create (pid=None) or replace a purchase invoice from the posted form -> (no, id)"""
    date, wh = norm_date(request.form.get("date")), to_int(request.form.get("wh_id"))
    sid = to_int(request.form.get("party_id"))
    kind = "return" if request.form.get("kind") == "return" else "buy"
    if not date:
        raise ValueError("تاریخ نامعتبر است.")
    if not wh_valid(wh):
        raise ValueError("انبار نامعتبر است.")
    if not q("SELECT 1 FROM persons WHERE id=? AND is_supplier=1", (sid,), one=True):
        raise ValueError("تامین‌کننده را انتخاب کنید.")
    lines = parse_lines(True, full=True)
    disc = max(to_int(request.form.get("discount")), 0)
    if disc > sum(qt * p - d for _, qt, p, d, _v in lines):
        raise ValueError("تخفیف از جمع فاکتور بیشتر است.")
    for it in q("""SELECT i.id, i.name, i.supplier_id, p.name sname FROM items i LEFT JOIN persons p ON p.id=i.supplier_id
                    WHERE i.id = ANY(?)""", ([l[0] for l in lines],)):
        if it["supplier_id"] and it["supplier_id"] != sid:
            raise ValueError(f"کالای «{it['name']}» متعلق به تامین‌کننده «{it['sname']}» است.")
    x("UPDATE items SET supplier_id=? WHERE id = ANY(?) AND supplier_id IS NULL", (sid, [l[0] for l in lines]))
    due, auto = norm_date(request.form.get("due_date")), 0
    if not due and kind == "buy":
        from core import due_from_items
        due = due_from_items(date, lines, sid)[0]
        auto = 1 if due else 0
    hdr = (kind, date, sid, wh, request.form.get("ref_no", "").strip() or None, disc, request.form.get("note"),
           due, request.form.get("pay_terms") or None, auto)
    from core import stock_snapshot, worse_negative
    before = stock_snapshot({(i, wh) for i, *_ in lines} |
                            {(m["item_id"], m["wh_id"]) for m in q("SELECT item_id, wh_id FROM stock_moves WHERE doc_type='PUR' AND doc_id=?", (pid or 0,))})
    if pid:
        old = q("SELECT * FROM purchases WHERE id=?", (pid,), one=True)
        del_moves("PUR", pid)
        x("DELETE FROM purchase_lines WHERE pur_id=?", (pid,))
        x("UPDATE purchases SET kind=?, date=?, supplier_id=?, wh_id=?, ref_no=?, discount=?, note=?, due_date=?, pay_terms=?, due_auto=?, updated_by=? WHERE id=?",
          hdr + (uid(), pid))
        no = old["no"]
    else:
        no = next_no("PUR")
        pid = x("""INSERT INTO purchases(no,kind,date,supplier_id,wh_id,ref_no,discount,note,due_date,pay_terms,due_auto,created_by)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (no,) + hdr + (uid(),))
    if kind == "return":
        check_stock(qty_by_item(lines), wh)
    for i, qt, p, d, vr in lines:
        x("INSERT INTO purchase_lines(pur_id,item_id,qty,price,disc,vat_rate,vat) VALUES(?,?,?,?,?,?,?)", (pid, i, qt, p, d, vr, line_vat(qt, p, d, vr)))
    sign = -1 if kind == "return" else 1
    add_moves("PUR", pid, no, date, [(i, wh, sign * qt, (qt * p - d) / qt if qt else p) for i, qt, p, d, _v in lines])
    bad = worse_negative(before)
    if bad:
        raise ValueError("با این سند موجودی این کالاها منفی می‌شود: " + "، ".join(bad))
    import gl
    gl.post_purchase(pid)
    return no, pid


def _no_negative(doc_type, did):
    for m in q("SELECT DISTINCT item_id, wh_id FROM stock_moves WHERE doc_type=? AND doc_id=?", (doc_type, did)):
        if v1("SELECT SUM(qty) FROM stock_moves WHERE item_id=? AND wh_id=?", (m["item_id"], m["wh_id"])) < 0:
            return False
    return True


@app.route("/purchases/new", methods=["GET", "POST"])
def purchase_new():
    if request.method == "POST" and request.files.get("xfile"):
        try:
            doc, lines = purchase_from_excel(request.files["xfile"])
            flash(f"{len(lines)} ردیف از اکسل خوانده شد؛ بازبینی کنید و «ثبت» را بزنید.", "ok")
            return render_template("doc_form.html", **form_ctx("purchase", parties=_suppliers(), doc=doc, init_lines=lines))
        except ValueError as e:
            flash(str(e), "bad")
            return redirect(url_for("purchase_new"))
    if request.method == "POST":
        try:
            no, pid = _save_purchase()
            db().commit()
            flash(f"{'برگشت از خرید' if request.form.get('kind') == 'return' else 'فاکتور خرید'} شماره {no} ثبت شد.", "ok")
            return redirect(url_for("purchase_view", pid=pid))
        except ValueError as e:
            db().rollback()
            flash(str(e), "bad")
    return render_template("doc_form.html", **form_ctx("purchase", parties=_suppliers()))


PX_COLS = ["کد کالا", "نام کالا", "باکس در کارتن", "تعداد کارتن", "تعداد باکس", "نرخ خرید هر باکس", "نرخ خرید هر کارتن", "تخفیف ردیف (ریال)", "مالیات %",
           "موجودی فعلی (فقط نمایش)"]


@app.route("/purchases/excel")
def purchase_excel():
    from core import default_wh
    sid = to_int(request.args.get("supplier"))
    sups = q("""SELECT p.id, p.code, p.name, (SELECT COUNT(*) FROM items WHERE supplier_id=p.id AND active=1) n
                FROM persons p WHERE p.is_supplier=1 AND p.active=1 ORDER BY p.code""")
    items = [r for r in stock_rows() if r["active"] and r["supplier_id"] == sid] if sid else []
    return render_template("purchase_excel.html", sups=sups, sid=sid, items=items, whs=warehouses_list(), wh=default_wh())


@app.route("/purchases/template.xlsx")
def purchase_template():
    """ready purchase sheet: all items of a supplier (or all items) with last purchase prices"""
    from masters import _xl
    from core import vat_default
    sid = to_int(request.args.get("supplier"))
    rows = [r for r in stock_rows() if r["active"] and (not sid or r["supplier_id"] == sid)]
    vd = vat_default()
    data = [[r["code"], r["name"], r["pack"] or 1, None, None, int(r["cost"] or 0), int(r["cost"] or 0) * (r["pack"] or 1) if (r["pack"] or 1) > 1 else None, None,
             r["vat_rate"] if r["vat_rate"] is not None else vd, pkr(r["st"], r)] for r in rows]
    name = v1("SELECT name FROM persons WHERE id=?", (sid,)) if sid else "همه"
    buf = _xl(data, "خرید", cols=PX_COLS)
    return send_file(buf, as_attachment=True, download_name=f"purchase_{(name or '').replace(' ', '_')}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def purchase_from_excel(f):
    """-> (doc, init_lines) for the purchase form (nothing is saved)"""
    from openpyxl import load_workbook
    from masters import _cell, pick_price
    try:
        ws = load_workbook(io.BytesIO(f.read()), data_only=True).worksheets[0]
    except Exception:
        raise ValueError("فایل اکسل معتبر نیست (xlsx).")
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise ValueError("فایل خالی است.")
    head = [_cell(h) for h in rows[0]]
    if "کد کالا" not in head:
        raise ValueError("ستون «کد کالا» لازم است؛ «قالب اکسل خرید» را دانلود کنید.")
    ix = {c: head.index(c) for c in PX_COLS if c in head}
    num = lambda r, c: to_int(str(_cell(r[ix[c]])).replace(",", "")) if c in ix and ix[c] < len(r) and _cell(r[ix[c]]) != "" else None
    items = {r["code"]: r for r in stock_rows()}
    lines, bad = [], []
    for n, r in enumerate(rows[1:], 2):
        code = _cell(r[ix["کد کالا"]]) if ix["کد کالا"] < len(r) else ""
        if not code:
            continue
        it = items.get(code)
        if not it:
            bad.append(f"ردیف {n}: کد {code}")
            continue
        k = it["pack"] or 1
        c, b = num(r, "تعداد کارتن") or 0, num(r, "تعداد باکس") or 0
        if c <= 0 and b <= 0:
            continue
        price = pick_price(num(r, "نرخ خرید هر باکس"), num(r, "نرخ خرید هر کارتن"), k, int(it["cost"] or 0))
        vat = _cell(r[ix["مالیات %"]]) if "مالیات %" in ix and ix["مالیات %"] < len(r) else ""
        lines.append({"id": it["id"], "ctn": c if k > 1 else "", "box": b + (c * k if k <= 1 else 0) or "", "price": price if price is not None else int(it["cost"] or 0),
                      "disc": num(r, "تخفیف ردیف (ریال)") or 0, "vat": vat or 0})
    if bad:
        raise ValueError("این کدها پیدا نشد: " + "، ".join(bad[:10]))
    if not lines:
        raise ValueError("هیچ ردیفی با تعداد پر نشده است.")
    doc = {"supplier_id": to_int(request.form.get("party_id")) or None, "wh_id": to_int(request.form.get("wh_id")) or None,
           "date": norm_date(request.form.get("date") or "") or None, "kind": "buy"}
    return doc, lines


@app.route("/purchases/<int:pid>/edit", methods=["GET", "POST"])
def purchase_edit(pid):
    p = q("SELECT * FROM purchases WHERE id=?", (pid,), one=True) or abort(404)
    if request.method == "POST":
        try:
            no, _ = _save_purchase(pid)
            db().commit()
            flash(f"فاکتور خرید {no} ویرایش شد.", "ok")
            return redirect(url_for("purchase_view", pid=pid))
        except ValueError as e:
            db().rollback()
            flash(str(e), "bad")
    own = {}
    if p["kind"] == "return":
        for m in q("SELECT item_id, wh_id, SUM(qty) s FROM stock_moves WHERE doc_type='PUR' AND doc_id=? GROUP BY 1,2", (pid,)):
            own[(m["item_id"], m["wh_id"])] = m["s"]
    return render_template("doc_form.html", **form_ctx("purchase", parties=_suppliers(), doc=p,
                                                       init_lines=doc_init_lines("purchase_lines", "pur_id", pid), own_moves=own))


@app.route("/purchases/<int:pid>")
def purchase_view(pid):
    from printing import purchase_print
    return purchase_print(pid)


@app.post("/purchases/<int:pid>/delete")
def purchase_delete(pid):
    if not _can_remove_moves("PUR", pid):
        flash("حذف ممکن نیست: موجودی کالا در انبار منفی می‌شود.", "bad")
        return redirect(url_for("purchase_view", pid=pid))
    del_moves("PUR", pid)
    x("UPDATE tr_docs SET pur_id=NULL WHERE pur_id=?", (pid,))
    x("DELETE FROM purchases WHERE id=?", (pid,))
    import gl
    gl.unpost("PUR", pid)
    db().commit()
    flash("فاکتور خرید حذف شد.", "ok")
    return redirect(url_for("purchases"))


# ================= transfers =================
TR_LIST = """SELECT t.*, a.name from_name, b.name to_name, (SELECT COALESCE(SUM(qty),0) FROM transfer_lines WHERE tr_id=t.id) qty
  FROM transfers t JOIN warehouses a ON a.id=t.from_wh JOIN warehouses b ON b.id=t.to_wh"""


@app.route("/transfers")
def transfers():
    f, t = period()
    rows = q(TR_LIST + " WHERE t.date BETWEEN ? AND ? ORDER BY t.date DESC, t.no DESC", (f, t))
    x_ = export.list_out("حواله‌های بین انبار", f"از {f} تا {t}", [("شماره", "text"), ("تاریخ", "text"), ("از انبار", "text"), ("به انبار", "text"),
                         ("توضیح", "text"), ("تعداد", "num")], [[r["no"], r["date"], r["from_name"], r["to_name"], r["note"] or "", r["qty"]] for r in rows],
                         ["جمع", "", "", "", "", sum(r["qty"] for r in rows)])
    if x_:
        return x_
    return render_template("transfers.html", rows=rows, f=f, t=t)


@app.route("/transfers/new", methods=["GET", "POST"])
def transfer_new():
    if request.method == "POST":
        try:
            date = norm_date(request.form.get("date"))
            a, b = to_int(request.form.get("from_wh")), to_int(request.form.get("to_wh"))
            if not date:
                raise ValueError("تاریخ نامعتبر است.")
            if not wh_valid(a) or not wh_valid(b):
                raise ValueError("انبار نامعتبر است.")
            if a == b:
                raise ValueError("انبار مبدا و مقصد یکی است.")
            lines = parse_lines()
            check_stock(qty_by_item(lines), a)
            no = next_no("TRF")
            tid = x("INSERT INTO transfers(no,date,from_wh,to_wh,note,created_by) VALUES(?,?,?,?,?,?)", (no, date, a, b, request.form.get("note"), uid()))
            for i, qt, _ in lines:
                x("INSERT INTO transfer_lines(tr_id,item_id,qty) VALUES(?,?,?)", (tid, i, qt))
            from core import buy_at
            add_moves("TRF", tid, no, date, [m for i, qt, _ in lines for m in ((i, a, -qt, 0), (i, b, qt, 0))])
            db().commit()
            flash(f"حواله انبار شماره {no} ثبت شد.", "ok")
            return redirect(url_for("transfer_view", tid=tid))
        except ValueError as e:
            db().rollback()
            flash(str(e), "bad")
    return render_template("doc_form.html", **form_ctx("transfer"))


@app.route("/transfers/<int:tid>")
def transfer_view(tid):
    t = q(TR_LIST + " WHERE t.id=?", (tid,), one=True) or abort(404)
    lines = item_lines("transfer_lines", "tr_id", tid)
    doc = {"title": DOC["TRF"], "no": t["no"], "date": t["date"], "note": t["note"],
           "extra": f"از انبار: {t['from_name']} ← به انبار: {t['to_name']}",
           "cols": ["کد کالا", "نام کالا", "تعداد"], "rows": [[l["code"], l["name"], pkr(l["qty"], l)] for l in lines],
           "foot": [("جمع تعداد", money(t["qty"]))], "sign": ["انباردار مبدا", "انباردار مقصد"], "user": uname(t["created_by"])}
    return doc_out(doc, back=url_for("transfers"), del_url=url_for("transfer_delete", tid=tid), pdf_url=url_for("transfer_view", tid=tid, fmt="pdf"))


@app.post("/transfers/<int:tid>/delete")
def transfer_delete(tid):
    if not _can_remove_moves("TRF", tid):
        flash("حذف ممکن نیست: موجودی کالا در انبار مقصد منفی می‌شود.", "bad")
        return redirect(url_for("transfer_view", tid=tid))
    del_moves("TRF", tid)
    x("DELETE FROM transfers WHERE id=?", (tid,))
    db().commit()
    flash("حواله حذف شد.", "ok")
    return redirect(url_for("transfers"))
