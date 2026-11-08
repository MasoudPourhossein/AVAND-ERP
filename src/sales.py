"""Sales: consignment (امانی) to visitors, settlement into per-customer invoices, direct sales invoices, payments."""
from flask import render_template, request, redirect, url_for, flash, abort, session
from core import (app, q, x, v1, db, next_no, to_int, uid, uname, price_at, buy_at, add_moves, del_moves, check_stock, default_wh,
                  INV_SQL, CONS_SQL, inv_paid, period, money, pkr, DOC, METHOD, can)
from inventory import parse_lines, form_ctx, doc_out, wh_valid, item_lines
from jalali import today_j, norm_date


def _visitors():
    return q("SELECT id, code, name, visitor_id FROM persons WHERE is_visitor=1 AND active=1 ORDER BY code")


def _customers():
    return q("SELECT id, code, name, visitor_id FROM persons WHERE is_customer=1 AND active=1 ORDER BY code")


def cons_lines(cid):
    return item_lines("cons_lines", "cons_id", cid)


def sale_lines(iid):
    return item_lines("sales_lines", "inv_id", iid)


def create_invoice(kind, date, customer, visitor, wh, lines, discount=0, note=None, cons_id=None):
    """lines: (item_id, qty, price). Direct invoices move stock out; consignment invoices don't (already issued)."""
    no = next_no("FCT")
    iid = x("""INSERT INTO sales_invoices(no,kind,date,customer_id,visitor_id,wh_id,cons_id,discount,note,created_by)
               VALUES(?,?,?,?,?,?,?,?,?,?)""", (no, kind, date, customer, visitor, wh, cons_id, discount, note, uid()))
    for i, qt, p in lines:
        x("INSERT INTO sales_lines(inv_id,item_id,qty,price,cost) VALUES(?,?,?,?,?)", (iid, i, qt, p, buy_at(i, date) or 0))
    if kind == "direct":
        add_moves("FCT", iid, no, date, [(i, wh, -qt, 0) for i, qt, _ in lines])
    return no, iid


# ================= consignment =================
@app.route("/amani")
def amani_list():
    st = request.args.get("status", "open")
    f, t = period()
    sql, args = CONS_SQL + " WHERE 1=1", []
    if st in ("open", "settled"):
        sql += " AND c.status=?"; args.append(st)
    if st != "open":
        sql += " AND c.date BETWEEN ? AND ?"; args += [f, t]
    vid = to_int(request.args.get("visitor"))
    if vid:
        sql += " AND c.visitor_id=?"; args.append(vid)
    last = session.pop("last_cons", None)
    last = q(CONS_SQL + " WHERE c.id=? AND c.status='settled'", (last,), one=True) if last else None
    last_invs = q(INV_SQL + " WHERE s.cons_id=? ORDER BY s.no", (last["id"],)) if last else []
    return render_template("amani_list.html", rows=q(sql + " ORDER BY c.date DESC, c.no DESC", args), st=st, f=f, t=t,
                           last=last, last_invs=last_invs, visitors=_visitors(), vid=vid)


@app.route("/amani/new", methods=["GET", "POST"])
def amani_new():
    vs = _visitors()
    if request.method == "POST":
        try:
            date, wh = norm_date(request.form.get("date")), to_int(request.form.get("wh_id"))
            vid = to_int(request.form.get("party_id"))
            if not date:
                raise ValueError("تاریخ نامعتبر است.")
            if not wh_valid(wh):
                raise ValueError("انبار نامعتبر است.")
            if not q("SELECT 1 FROM persons WHERE id=? AND is_visitor=1", (vid,), one=True):
                raise ValueError("ویزیتور را انتخاب کنید.")
            lines = parse_lines()
            check_stock({i: qt for i, qt, _ in lines}, wh)
            no = next_no("AMN")
            cid = x("INSERT INTO consignments(no,date,visitor_id,wh_id,note,created_by) VALUES(?,?,?,?,?,?)",
                    (no, date, vid, wh, request.form.get("note"), uid()))
            for i, qt, _ in lines:
                x("INSERT INTO cons_lines(cons_id,item_id,qty,price) VALUES(?,?,?,?)", (cid, i, qt, price_at(i, date)))
            add_moves("AMN", cid, no, date, [(i, wh, -qt, 0) for i, qt, _ in lines])
            db().commit()
            flash(f"فاکتور امانی شماره {no} صادر شد.", "ok")
            return redirect(url_for("amani_view", cid=cid))
        except ValueError as e:
            db().rollback()
            flash(str(e), "bad")
    return render_template("doc_form.html", **form_ctx("amani", parties=vs))


def _amani_doc(cid):
    c = q(CONS_SQL + " WHERE c.id=?", (cid,), one=True) or abort(404)
    lines = cons_lines(cid)
    invs = q(INV_SQL + " WHERE s.cons_id=? ORDER BY s.no", (cid,))
    doc = {"title": DOC["AMN"], "no": c["no"], "date": c["date"], "party": f"{c['vname']} ({c['vcode']})", "note": c["note"],
           "status": "امانی باز" if c["status"] == "open" else "تسویه شد" + (f" — فاکتورها: {'، '.join(str(i['no']) for i in invs)}" if invs else ""),
           "extra": f"انبار: {c['whname']}",
           "cols": ["کد کالا", "نام کالا", "تعداد تحویلی", "نرخ واحد (ریال)", "ارزش (ریال)"],
           "rows": [[l["code"], l["name"], pkr(l["qty"], l), money(l["price"]), money(l["qty"] * l["price"])] for l in lines],
           "foot": [("جمع ارزش امانی (ریال)", money(c["amani_value"]))], "sign": ["تحویل‌دهنده", "ویزیتور"], "user": uname(c["created_by"])}
    return c, doc, invs


@app.route("/amani/<int:cid>")
def amani_view(cid):
    c, doc, invs = _amani_doc(cid)
    return doc_out(doc, c=c, invs=invs, back=url_for("amani_list"), pdf_url=url_for("amani_view", cid=cid, fmt="pdf"),
                   del_url=url_for("amani_delete", cid=cid) if c["status"] == "open" else None)


@app.route("/print/amani/<int:cid>")
def print_amani(cid):
    c, doc, _ = _amani_doc(cid)
    return doc_out(doc, printing=True)


@app.post("/amani/<int:cid>/delete")
def amani_delete(cid):
    c = q("SELECT * FROM consignments WHERE id=?", (cid,), one=True) or abort(404)
    if c["status"] != "open":
        flash("فقط فاکتور امانی باز قابل حذف است.", "bad")
        return redirect(url_for("amani_view", cid=cid))
    del_moves("AMN", cid)
    x("DELETE FROM consignments WHERE id=?", (cid,))
    db().commit()
    flash(f"فاکتور امانی {c['no']} حذف شد و کالا به انبار برگشت.", "ok")
    return redirect(url_for("amani_list"))


@app.route("/amani/<int:cid>/settle", methods=["GET", "POST"])
def amani_settle(cid):
    c = q(CONS_SQL + " WHERE c.id=?", (cid,), one=True) or abort(404)
    if c["status"] != "open":
        return redirect(url_for("amani_view", cid=cid))
    lines = cons_lines(cid)
    custs = q("""SELECT id, code, name, visitor_id FROM persons WHERE is_customer=1 AND active=1
                 ORDER BY (visitor_id=?) DESC, code""", (c["visitor_id"],))
    if request.method == "POST":
        f = request.form
        try:
            date = norm_date(f.get("settle_date"))
            if not date:
                raise ValueError("تاریخ نامعتبر است.")
            if date < c["date"]:
                raise ValueError("تاریخ قطعی نمی‌تواند قبل از تاریخ امانی باشد.")
            L = {}
            for l in lines:
                k = l["pack"] or 1
                ret = to_int(f.get(f"retc_{l['id']}")) * k + to_int(f.get(f"ret_{l['id']}"))
                pr = to_int(f.get(f"price_{l['id']}"), l["price"])
                if ret < 0 or ret > l["qty"]:
                    raise ValueError(f"مرجوعی «{l['name']}» باید بین ۰ و {l['qty']} باشد.")
                if pr < 0:
                    raise ValueError("نرخ نامعتبر است.")
                L[l["id"]] = {"l": l, "ret": ret, "sold": l["qty"] - ret, "price": pr, "left": l["qty"] - ret}
            # customer blocks
            blocks = []
            for b in f.getlist("blk"):
                cust = to_int(f.get(f"cust_{b}"))
                rows = []
                for lid, d in L.items():
                    k = d["l"]["pack"] or 1
                    qt = to_int(f.get(f"q_{b}_{lid}_c")) * k + to_int(f.get(f"q_{b}_{lid}_b"))
                    if qt < 0:
                        raise ValueError("تعداد منفی مجاز نیست.")
                    if qt:
                        d["left"] -= qt
                        if d["left"] < 0:
                            raise ValueError(f"جمع تخصیص «{d['l']['name']}» به مشتریان از فروش آن ({d['sold']}) بیشتر است.")
                        rows.append((d["l"]["item_id"], qt, d["price"]))
                if rows:
                    if not q("SELECT 1 FROM persons WHERE id=? AND is_customer=1", (cust,), one=True):
                        raise ValueError("برای هر فاکتور مشتری را انتخاب کنید.")
                    blocks.append((cust, rows))
            rest = [(d["l"]["item_id"], d["left"], d["price"]) for d in L.values() if d["left"] > 0]
            if rest:
                rc = to_int(f.get("rest_cust")) or c["visitor_id"]
                if rc == c["visitor_id"]:
                    x("UPDATE persons SET is_customer=1 WHERE id=?", (rc,))
                elif not q("SELECT 1 FROM persons WHERE id=? AND is_customer=1", (rc,), one=True):
                    raise ValueError("مشتری باقیمانده نامعتبر است.")
                blocks.append((rc, rest))
            # merge blocks of the same customer
            merged = {}
            for cust, rows in blocks:
                m = merged.setdefault(cust, {})
                for i, qt, p in rows:
                    m[i] = (m.get(i, (0, p))[0] + qt, p)
            nos = []
            for cust, m in merged.items():
                no, _ = create_invoice("consign", date, cust, c["visitor_id"], c["wh_id"], [(i, qp[0], qp[1]) for i, qp in m.items()],
                                       note=f"عطف به امانی {c['no']}", cons_id=cid)
                nos.append(no)
            rno = next_no("MRJ") if any(d["ret"] for d in L.values()) else None
            for lid, d in L.items():
                x("UPDATE cons_lines SET returned=?, price=? WHERE id=?", (d["ret"], d["price"], lid))
            add_moves("MRJ", cid, rno, date, [(d["l"]["item_id"], c["wh_id"], d["ret"], 0) for d in L.values() if d["ret"]])
            x("UPDATE consignments SET status='settled', settle_date=?, return_no=?, settled_by=? WHERE id=?", (date, rno, uid(), cid))
            db().commit()
            msg = f"امانی {c['no']} تسویه شد."
            if nos:
                msg += f" فاکتور فروش: {'، '.join(map(str, nos))}."
            if rno:
                msg += f" برگه مرجوعی {rno} ثبت و کالا به انبار برگشت."
            flash(msg, "ok")
            session["last_cons"] = cid
            return redirect(url_for("amani_list", status="open"))
        except ValueError as e:
            db().rollback()
            flash(str(e), "bad")
    return render_template("settle.html", c=c, lines=lines, custs=custs)


@app.route("/print/return/<int:cid>")
def print_return(cid):
    c = q(CONS_SQL + " WHERE c.id=?", (cid,), one=True) or abort(404)
    if not c["return_no"]:
        abort(404)
    lines = [l for l in cons_lines(cid) if l["returned"] > 0]
    doc = {"title": DOC["MRJ"], "no": c["return_no"], "date": c["settle_date"], "party": f"{c['vname']} ({c['vcode']})",
           "extra": f"عطف به فاکتور امانی {c['no']} | انبار: {c['whname']}",
           "cols": ["کد کالا", "نام کالا", "تحویلی", "مرجوعی"],
           "rows": [[l["code"], l["name"], pkr(l["qty"], l), pkr(l["returned"], l)] for l in lines],
           "foot": [("جمع مرجوعی", money(c["ret_qty"]))], "sign": ["انباردار", "ویزیتور"], "user": uname(c["settled_by"])}
    return doc_out(doc, printing=True, pdf_url=url_for("print_return", cid=cid, fmt="pdf"))


# ================= direct sales =================
@app.route("/sales/new", methods=["GET", "POST"])
def sale_new():
    if request.method == "POST":
        try:
            date, wh = norm_date(request.form.get("date")), to_int(request.form.get("wh_id"))
            cust, vis = to_int(request.form.get("party_id")), to_int(request.form.get("visitor_id")) or None
            if not date:
                raise ValueError("تاریخ نامعتبر است.")
            if not wh_valid(wh):
                raise ValueError("انبار نامعتبر است.")
            if not q("SELECT 1 FROM persons WHERE id=? AND is_customer=1", (cust,), one=True):
                raise ValueError("مشتری را انتخاب کنید.")
            if vis and not q("SELECT 1 FROM persons WHERE id=? AND is_visitor=1", (vis,), one=True):
                raise ValueError("ویزیتور نامعتبر است.")
            lines = parse_lines(True)
            disc = max(to_int(request.form.get("discount")), 0)
            if disc > sum(qt * p for _, qt, p in lines):
                raise ValueError("تخفیف از جمع فاکتور بیشتر است.")
            check_stock({i: qt for i, qt, _ in lines}, wh)
            no, iid = create_invoice("direct", date, cust, vis, wh, lines, disc, request.form.get("note"))
            db().commit()
            flash(f"فاکتور فروش شماره {no} صادر شد.", "ok")
            return redirect(url_for("invoice", iid=iid))
        except ValueError as e:
            db().rollback()
            flash(str(e), "bad")
    return render_template("doc_form.html", **form_ctx("sale", parties=_customers(), visitors=_visitors()))


# ================= invoices & payments =================
@app.route("/invoices")
def invoices():
    f, t = period()
    vid, cid = to_int(request.args.get("visitor")), to_int(request.args.get("customer"))
    only = request.args.get("only", "")
    sql, args = INV_SQL + " WHERE s.date BETWEEN ? AND ?", [f, t]
    if vid:
        sql += " AND s.visitor_id=?"; args.append(vid)
    if cid:
        sql += " AND s.customer_id=?"; args.append(cid)
    rows = q(sql + " ORDER BY s.date DESC, s.no DESC", args)
    if only == "diff":
        rows = [r for r in rows if r["total"] != r["pos"] + r["cash"]]
    return render_template("invoices.html", rows=rows, f=f, t=t, vid=vid, cid=cid, only=only, visitors=_visitors(), customers=_customers())


@app.route("/invoices/<int:iid>", methods=["GET", "POST"])
def invoice(iid):
    s = q(INV_SQL + " WHERE s.id=?", (iid,), one=True) or abort(404)
    if request.method == "POST":
        amount, method = to_int(request.form.get("amount")), request.form.get("method")
        date = norm_date(request.form.get("date")) or today_j()
        if amount <= 0 or method not in METHOD:
            flash("مبلغ و نوع دریافت معتبر لازم است.", "bad")
        elif date < s["date"]:
            flash("تاریخ دریافت نمی‌تواند قبل از تاریخ فاکتور باشد.", "bad")
        else:
            no = next_no("DRY")
            x("INSERT INTO payments(no,inv_id,date,method,amount,ref,created_by) VALUES(?,?,?,?,?,?,?)",
              (no, iid, date, method, amount, request.form.get("ref"), uid()))
            db().commit()
            flash(f"سند دریافت شماره {no} ثبت شد.", "ok")
        return redirect(url_for("invoice", iid=iid))
    pays = q("""SELECT p.*, COALESCE(NULLIF(u.fullname,''),u.username) uname FROM payments p LEFT JOIN users u ON u.id=p.created_by
                WHERE inv_id=? ORDER BY date, p.id""", (iid,))
    return render_template("invoice.html", c=s, lines=sale_lines(iid), pays=pays)


@app.post("/payments/<int:pid>/delete")
def payment_delete(pid):
    p = q("SELECT * FROM payments WHERE id=?", (pid,), one=True) or abort(404)
    x("DELETE FROM payments WHERE id=?", (pid,))
    db().commit()
    flash(f"سند دریافت {p['no']} حذف شد.", "ok")
    return redirect(url_for("invoice", iid=p["inv_id"]))


@app.route("/print/invoice/<int:iid>")
def print_invoice(iid):
    s = q(INV_SQL + " WHERE s.id=?", (iid,), one=True) or abort(404)
    lines = sale_lines(iid)
    gross = sum(l["qty"] * l["price"] for l in lines)
    diff = s["total"] - s["pos"] - s["cash"]
    extra = f"انبار: {s['whname']}" + (f" | ویزیتور: {s['vname']}" if s["vname"] else "") + (f" | عطف به امانی {s['cons_no']}" if s["cons_no"] else "")
    doc = {"title": DOC["FCT"], "no": s["no"], "date": s["date"], "party": f"{s['cname']} ({s['ccode']})", "party_label": "مشتری",
           "extra": extra, "note": s["note"] if s["kind"] == "direct" else None,
           "cols": ["کد کالا", "نام کالا", "تعداد", "نرخ واحد (ریال)", "مبلغ (ریال)"],
           "rows": [[l["code"], l["name"], pkr(l["qty"], l), money(l["price"]), money(l["qty"] * l["price"])] for l in lines],
           "foot": [("جمع (ریال)", money(gross))] + ([("تخفیف", money(s["discount"]))] if s["discount"] else []) +
                   [("مبلغ فاکتور (ریال)", money(s["total"])), ("دریافتی پوز", money(s["pos"])), ("دریافتی نقد", money(s["cash"])),
                    ("مانده (ریال)", money(diff))],
           "sign": ["فروشنده", "خریدار"], "user": uname(s["created_by"])}
    return doc_out(doc, printing=True, pdf_url=url_for("print_invoice", iid=iid, fmt="pdf"))


@app.route("/invoices/<int:iid>/remove", methods=["GET", "POST"])
def invoice_remove(iid):
    """admin only, 2 warnings. direct: delete invoice (stock back). consign: unsettle whole consignment or delete it."""
    s = q(INV_SQL + " WHERE s.id=?", (iid,), one=True) or abort(404)
    cid = s["cons_id"]
    group = q(INV_SQL + " WHERE s.cons_id=? ORDER BY s.no", (cid,)) if cid else [s]
    ids = [r["id"] for r in group]
    pays = q("SELECT * FROM payments WHERE inv_id = ANY(?)", (ids,))
    actions = ("unsettle", "delete") if cid else ("delete",)
    action = request.values.get("action") if request.values.get("action") in actions else actions[0]
    step = 1
    if request.method == "POST":
        if request.form.get("step") == "1":
            if request.form.get("ok1") != "1":
                flash("برای ادامه، تیک هشدار اول را بزنید.", "bad")
            else:
                session["rm_inv"] = iid
                step = 2
        elif request.form.get("step") == "2":
            if session.get("rm_inv") != iid:
                flash("هشدار اول تایید نشده است. دوباره شروع کنید.", "bad")
            elif request.form.get("confirm", "").strip() != str(s["no"]) or request.form.get("ok2") != "1":
                flash("شماره فاکتور درست وارد نشده یا هشدار دوم تایید نشده است.", "bad")
                step = 2
            else:
                session.pop("rm_inv", None)
                x("DELETE FROM payments WHERE inv_id = ANY(?)", (ids,))
                for i in ids:
                    del_moves("FCT", i)
                x("DELETE FROM sales_invoices WHERE id = ANY(?)", (ids,))
                if cid:
                    del_moves("MRJ", cid)
                    if action == "delete":
                        del_moves("AMN", cid)
                        x("DELETE FROM consignments WHERE id=?", (cid,))
                        db().commit()
                        flash(f"امانی {s['cons_no']} و {len(ids)} فاکتور آن کامل حذف شد؛ کالا به انبار برگشت.", "ok")
                        return redirect(url_for("invoices"))
                    x("UPDATE cons_lines SET returned=0 WHERE cons_id=?", (cid,))
                    x("UPDATE consignments SET status='open', settle_date=NULL, return_no=NULL, settled_by=NULL WHERE id=?", (cid,))
                    db().commit()
                    flash(f"{len(ids)} فاکتور باطل شد و سند به امانی باز {s['cons_no']} برگشت.", "ok")
                    return redirect(url_for("amani_view", cid=cid))
                db().commit()
                flash(f"فاکتور {s['no']} حذف شد و کالا به انبار برگشت.", "ok")
                return redirect(url_for("invoices"))
    return render_template("invoice_remove.html", c=s, group=group, pays=pays, action=action, actions=actions, step=step)
