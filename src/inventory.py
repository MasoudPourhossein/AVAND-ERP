"""Stock documents: receipts (رسید), purchases (خرید/برگشت از خرید), transfers (حواله بین انبار)."""
from flask import render_template, request, redirect, url_for, flash, abort
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
        if i in seen:
            raise ValueError("یک کالا دو بار در سند آمده است.")
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
def make_receipt(date, wh, kind, note, lines):
    """lines: (item_id, qty, unit_cost) -> (no, id)"""
    no = next_no("RES")
    rid = x("INSERT INTO receipts(no,date,wh_id,kind,note,created_by) VALUES(?,?,?,?,?,?)", (no, date, wh, kind, note, uid()))
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
            no, rid = make_receipt(date, wh, "in", request.form.get("note"), parse_lines(True))
            db().commit()
            flash(f"رسید انبار شماره {no} ثبت شد.", "ok")
            return redirect(url_for("receipt_view", rid=rid))
        except ValueError as e:
            db().rollback()
            flash(str(e), "bad")
    return render_template("doc_form.html", **form_ctx("receipt"))


@app.route("/receipts/<int:rid>")
def receipt_view(rid):
    r = q("SELECT r.*, w.name whname FROM receipts r JOIN warehouses w ON w.id=r.wh_id WHERE r.id=?", (rid,), one=True) or abort(404)
    lines = item_lines("receipt_lines", "receipt_id", rid)
    doc = {"title": DOC["RES"] + (" (موجودی اولیه)" if r["kind"] == "opening" else ""), "no": r["no"], "date": r["date"], "note": r["note"],
           "extra": f"انبار: {r['whname']}",
           "cols": ["کد کالا", "نام کالا", "تعداد", "نرخ خرید واحد", "مبلغ"],
           "rows": [[l["code"], l["name"], pkr(l["qty"], l), money(l["unit_cost"]), money(l["qty"] * l["unit_cost"])] for l in lines],
           "foot": [("جمع مبلغ (ریال)", money(sum(l["qty"] * l["unit_cost"] for l in lines)))],
           "sign": ["تحویل‌دهنده", "انباردار"], "user": uname(r["created_by"])}
    return doc_out(doc, back=url_for("receipts"), del_url=url_for("receipt_delete", rid=rid), pdf_url=url_for("receipt_view", rid=rid, fmt="pdf"))


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
    return render_template("purchases.html", rows=rows, f=f, t=t, sid=sid, sups=sups)


def _suppliers():
    return q("SELECT id, code, name, visitor_id FROM persons WHERE is_supplier=1 AND active=1 ORDER BY code")


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
    hdr = (kind, date, sid, wh, request.form.get("ref_no", "").strip() or None, disc, request.form.get("note"),
           norm_date(request.form.get("due_date")), request.form.get("pay_terms") or None)
    from core import stock_snapshot, worse_negative
    before = stock_snapshot({(i, wh) for i, *_ in lines} |
                            {(m["item_id"], m["wh_id"]) for m in q("SELECT item_id, wh_id FROM stock_moves WHERE doc_type='PUR' AND doc_id=?", (pid or 0,))})
    if pid:
        old = q("SELECT * FROM purchases WHERE id=?", (pid,), one=True)
        del_moves("PUR", pid)
        x("DELETE FROM purchase_lines WHERE pur_id=?", (pid,))
        x("UPDATE purchases SET kind=?, date=?, supplier_id=?, wh_id=?, ref_no=?, discount=?, note=?, due_date=?, pay_terms=?, updated_by=? WHERE id=?",
          hdr + (uid(), pid))
        no = old["no"]
    else:
        no = next_no("PUR")
        pid = x("""INSERT INTO purchases(no,kind,date,supplier_id,wh_id,ref_no,discount,note,due_date,pay_terms,created_by)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (no,) + hdr + (uid(),))
    if kind == "return":
        check_stock({i: qt for i, qt, *_ in lines}, wh)
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
            check_stock({i: qt for i, qt, _ in lines}, a)
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
