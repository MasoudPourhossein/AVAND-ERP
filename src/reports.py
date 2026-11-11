from flask import render_template, request, url_for, jsonify
from core import (app, q, v1, to_int, stock_rows, INV_SQL, CONS_SQL, period, can, register_report, METHOD, pkr, warehouses_list)
from jalali import (today_j, add_days, diff_days, week_start, month_start, year_start, shift_month, shift_year,
                    month_end, weekday, parts, WEEKDAYS, MONTHS)
import export

SL = "FROM sales_lines l JOIN v_si s ON s.id=l.inv_id"
DOC_LABEL = {"SRT": "برگشت از فروش", "RES": "رسید انبار", "PUR": "فاکتور خرید", "PRT": "برگشت از خرید", "TRF": "حواله انبار", "AMN": "حواله امانی",
             "MRJ": "برگشت مرجوعی", "FCT": "فاکتور فروش"}


def link(v, href, cls=None):
    return {"v": v, "href": href, "cls": cls}


def pct(cur, prev):
    return None if not prev or cur is None else (cur - prev) * 100.0 / abs(prev)


def _f(col, val, w, a):
    if val:
        w += f" AND {col}=?"; a.append(val)
    return w, a


PAY_COLS = """COALESCE(SUM(CASE WHEN p.method='pos' THEN p.amount END),0) pos, COALESCE(SUM(CASE WHEN p.method='cash' THEN p.amount END),0) cash,
  COALESCE(SUM(CASE WHEN p.method='bank' THEN p.amount END),0) bank, COALESCE(SUM(CASE WHEN p.method='cheque' THEN p.amount END),0) chq,
  COALESCE(SUM(p.amount),0) coll"""


# ---------------- aggregates
def sales_agg(f, t, vid=None, cid=None):
    """amount = net sales excl. VAT (returns deducted); total = invoices incl. VAT"""
    w, a = " WHERE s.date BETWEEN ? AND ?", [f, t]
    w, a = _f("s.visitor_id", vid, w, a)
    w, a = _f("s.customer_id", cid, w, a)
    r = q(f"""SELECT COALESCE(SUM(gross*sg),0) gross, COALESCE(SUM((ldisc+discount)*sg),0) disc, COALESCE(SUM(net_total*sg),0) amount,
              COALESCE(SUM(vat_total*sg),0) vat, COALESCE(SUM(total*sg),0) total, COALESCE(SUM(qty_total*sg),0) qty,
              COALESCE(SUM(cogs_total*sg),0) cogs, COUNT(*) FILTER (WHERE kind<>'return') inv,
              COALESCE(SUM(net_total) FILTER (WHERE kind='return'),0) sret FROM v_si s {w}""", a, one=True)
    cw, ca = " WHERE c.status='settled' AND c.settle_date BETWEEN ? AND ?", [f, t]
    cw, ca = _f("c.visitor_id", vid, cw, ca)
    rr = q(f"SELECT COALESCE(SUM(l.returned),0) ret, COALESCE(SUM(l.returned*l.price),0) ret_val FROM cons_lines l JOIN consignments c ON c.id=l.cons_id {cw}",
           ca, one=True) if not cid else {"ret": 0, "ret_val": 0}
    pw, pa = " WHERE p.date BETWEEN ? AND ?", [f, t]
    pw, pa = _f("p.visitor_id", vid, pw, pa)
    pw, pa = _f("p.person_id", cid, pw, pa)
    p = q(f"SELECT {PAY_COLS} FROM payments p {pw}", pa, one=True)
    d = dict(r)
    d.update(ret=rr["ret"], ret_val=rr["ret_val"], pos=p["pos"], cash=p["cash"], bank=p["bank"], chq=p["chq"], coll=p["coll"], profit=r["amount"] - r["cogs"])
    return d


def daily_series(f, t):
    rows = {r["d"]: r for r in q("""SELECT date d, SUM(net_total*sg) amt, SUM(qty_total*sg) qty, COUNT(*) FILTER (WHERE kind<>'return') inv
                                    FROM v_si WHERE date BETWEEN ? AND ? GROUP BY date""", (f, t))}
    rets = {r["d"]: r["x"] for r in q("""SELECT c.settle_date d, SUM(l.returned) x FROM cons_lines l JOIN consignments c ON c.id=l.cons_id
                                          WHERE c.status='settled' AND c.settle_date BETWEEN ? AND ? GROUP BY c.settle_date""", (f, t))}
    pays = {r["d"]: r["s"] for r in q("SELECT date d, SUM(amount) s FROM payments WHERE date BETWEEN ? AND ? GROUP BY date", (f, t))}
    out, d = [], f
    while d <= t:
        r = rows.get(d)
        out.append({"d": d, "amount": r["amt"] if r else 0, "qty": r["qty"] if r else 0, "ret": rets.get(d, 0),
                    "inv": r["inv"] if r else 0, "coll": pays.get(d, 0)})
        d = add_days(d, 1)
    return out


def _gl_bal(key, t, pid=None, before=False):
    import gl
    op = "<" if before else "<="
    w, a = ("", []) if not pid else (" AND vl.person_id=?", [pid])
    return v1(f"""SELECT SUM(vl.debit-vl.credit) FROM voucher_lines vl JOIN vouchers v ON v.id=vl.voucher_id
                  WHERE vl.account_id=? AND v.date {op} ?{w}""", [gl.acc(key), t] + a)


def receivable_until(t, pid=None, as_="customer", before=False):
    """customer / total: GL receivable account. visitor: invoices of his customers minus receipts registered for him."""
    if as_ != "visitor" or not pid:
        return _gl_bal("receivable", t, pid, before)
    op = "<" if before else "<="
    inv = v1(f"SELECT SUM(total*sg) FROM v_si s WHERE s.date {op} ? AND s.visitor_id=?", [t, pid])
    pay = v1(f"SELECT SUM(p.amount) FROM payments p WHERE p.date {op} ? AND p.visitor_id=?", [t, pid])
    return inv - pay


def payable_until(t, pid=None, before=False):
    """+ = we owe the supplier"""
    return -_gl_bal("payable", t, pid, before)


def compare_periods():
    t = today_j()
    ws, ms, ys = week_start(t), month_start(t), year_start(t)
    return [
        ("امروز", t, t, "دیروز", add_days(t, -1), add_days(t, -1)),
        ("این هفته", ws, t, "هفته گذشته", add_days(ws, -7), add_days(t, -7)),
        ("این ماه", ms, t, "ماه گذشته", shift_month(ms, -1), shift_month(t, -1)),
        ("امسال", ys, t, "سال گذشته", shift_year(ys, -1), shift_year(t, -1)),
    ]


def unpaid_invoices(t=None):
    t = t or today_j()
    out = []
    for r in q(INV_SQL + " WHERE s.kind<>'return' ORDER BY s.date, s.no"):
        rem = r["rem"]
        if rem > 0:
            out.append({"id": r["id"], "no": r["no"], "date": r["date"], "c": r["cname"], "v": r["vname"] or "", "vcode": r["vcode"],
                        "ccode": r["ccode"], "total": r["total"], "paid": r["paid"], "rem": rem, "age": diff_days(r["date"], t)})
    return out


# ---------------- dashboard
@app.route("/")
def dashboard():
    t = today_j()
    ctx = {"cmp": [], "k": {}}
    if can("rep_sales", "rep_fin"):
        for lab, f1, t1, plab, f0, t0 in compare_periods()[:3]:
            cur, prev = sales_agg(f1, t1), sales_agg(f0, t0)
            ctx["cmp"].append({"label": lab, "plabel": plab, "cur": cur, "prev": prev, "ch": pct(cur["amount"], prev["amount"])})
        ser = daily_series(add_days(t, -13), t)
        ctx["chart"] = {"labels": [d["d"][5:] for d in ser], "data": [d["amount"] for d in ser]}
    if can("rep_fin"):
        ctx["k"]["receivable"] = receivable_until(t)
        ctx["k"]["payable"] = payable_until(t)
        ctx["k"]["coll_today"] = sales_agg(t, t)["coll"]
    if can("rep_stock"):
        st = stock_rows()
        ctx["k"]["stock_val"] = sum(r["st"] * r["vc"] for r in st)
        ctx["k"]["open_qty"] = sum(r["open_amani"] for r in st)
        ctx["low"] = [r for r in st if r["st"] <= 0 and r["active"]]
        ctx["live"] = [{"code": r["code"], "name": r["name"], "id": r["id"], "s": r["st"], "a": r["open_amani"], "row": r} for r in st if r["active"]]
    if can("rep_fin", "payment"):
        ctx["unpaid"] = unpaid_invoices(t)
        ctx["unpaid_sum"] = sum(u["rem"] for u in ctx["unpaid"])
    if can("amani", "settle", "rep_sales"):
        ctx["open_list"] = q(CONS_SQL + " WHERE c.status='open' ORDER BY c.date, c.no LIMIT 8")
        ctx["k"]["open_docs"] = v1("SELECT COUNT(*) FROM consignments WHERE status='open'")
    return render_template("dashboard.html", **ctx)


@app.route("/api/stock")
def api_stock():
    out = []
    for r in stock_rows():
        if not r["active"]:
            continue
        out.append({"code": r["code"], "name": r["name"], "id": r["id"], "stock": r["st"], "amani": r["open_amani"],
                    "stock_t": pkr(r["st"], r), "amani_t": pkr(r["open_amani"], r), "total_t": pkr(r["st"] + r["open_amani"], r)})
    return jsonify(out)


@app.route("/reports")
def reports():
    return render_template("reports.html")


def rep(rule, endpoint, perm):
    def deco(fn):
        register_report(endpoint, perm)
        app.add_url_rule(rule, endpoint, fn)
        return fn
    return deco


def _visitors():
    return q("SELECT id, code, name FROM persons WHERE is_visitor=1 ORDER BY code")


def _customers():
    return q("SELECT id, code, name FROM persons WHERE is_customer=1 ORDER BY code")


def _items():
    return q("SELECT id, code, name FROM items ORDER BY code")


def _pname(pid):
    r = q("SELECT name FROM persons WHERE id=?", (pid,), one=True) if pid else None
    return r["name"] if r else None


def _whname(wid):
    r = q("SELECT name FROM warehouses WHERE id=?", (wid,), one=True) if wid else None
    return r["name"] if r else None


# ================= SALES =================
@rep("/reports/stats", "rep_stats", "rep_sales")
def rep_stats():
    t = today_j()
    f, tt = period(default_from=add_days(t, -29))
    metrics = [("فروش (ریال)", "amount", "money"), ("تعداد فروش", "qty", "num"), ("تعداد فاکتور", "inv", "num"),
               ("مرجوعی (تعداد)", "ret", "num"), ("دریافتی (ریال)", "coll", "money"), ("سود ناخالص (ریال)", "profit", "money")]
    blocks = [(lab, plab, sales_agg(f1, t1), sales_agg(f0, t0), f1, t1, f0, t0) for lab, f1, t1, plab, f0, t0 in compare_periods()]
    cmp_rows = []
    for mlab, key, kind in metrics:
        row = [mlab]
        for b in blocks:
            row += [b[2][key], b[3][key], pct(b[2][key], b[3][key])]
        cmp_rows.append(row)
    cmp_cols = [("شاخص", "text")]
    for b in blocks:
        cmp_cols += [(b[0], "money"), (b[1], "money"), ("تغییر %", "pct")]
    ser = daily_series(f, tt)
    daily = [[d["d"], WEEKDAYS[weekday(d["d"])], d["inv"], d["qty"], d["amount"], d["ret"], d["coll"]] for d in ser]
    ws0 = week_start(t)
    weeks = []
    for k in range(11, -1, -1):
        a = add_days(ws0, -7 * k); b = min(add_days(a, 6), t)
        s = sales_agg(a, b)
        weeks.append([f"{a} تا {b}", s["inv"], s["qty"], s["amount"], s["coll"]])
    y = parts(t)[0]
    months = []
    for m in range(1, 13):
        a = "%04d/%02d/01" % (y, m); b = month_end(a)
        cur = sales_agg(a, b)["amount"] if a <= t else None
        prev = sales_agg(shift_year(a, -1), shift_year(b, -1))["amount"]
        months.append([MONTHS[m - 1], cur, prev, pct(cur, prev) if cur is not None else None])
    kp = []
    for b in blocks:
        kp.append((f"فروش {b[0]}", b[2]["amount"], "money"))
        kp.append((f"تغییر نسبت به {b[1]}", pct(b[2]["amount"], b[3]["amount"]), "pct"))
    spec = {"title": "آمار و مقایسه فروش", "sub": f"ریز روزانه از {f} تا {tt}", "filters": ["period"], "kpis": kp, "cards": blocks,
            "charts": [
                {"id": "c1", "type": "bar", "title": "فروش روزانه (ریال)", "labels": [d["d"][5:] for d in ser], "sets": [("فروش", [d["amount"] for d in ser])]},
                {"id": "c2", "type": "bar", "title": "فروش ۱۲ هفته اخیر (ریال)", "labels": [w[0][5:10] for w in weeks], "sets": [("فروش", [w[3] for w in weeks])]},
                {"id": "c3", "type": "line", "title": f"فروش ماهانه {y} در مقایسه با {y - 1}", "labels": MONTHS,
                 "sets": [(str(y), [m[1] for m in months]), (str(y - 1), [m[2] for m in months])]}],
            "sections": [
                {"title": "مقایسه دوره‌ها", "cols": cmp_cols, "rows": cmp_rows},
                {"title": "فروش روزانه", "cols": [("تاریخ", "text"), ("روز", "text"), ("فاکتور", "num"), ("تعداد فروش", "num"), ("فروش (ریال)", "money"),
                                                  ("مرجوعی", "num"), ("دریافتی (ریال)", "money")],
                 "rows": daily, "total": ["جمع", ""] + [sum(d[i] for d in daily) for i in range(2, 7)]},
                {"title": "فروش هفتگی", "cols": [("هفته", "text"), ("فاکتور", "num"), ("تعداد فروش", "num"), ("فروش (ریال)", "money"), ("دریافتی (ریال)", "money")], "rows": weeks},
                {"title": "فروش ماهانه", "cols": [("ماه", "text"), (f"فروش {y}", "money"), (f"فروش {y - 1}", "money"), ("تغییر %", "pct")], "rows": months}]}
    return export.out(spec, template="stats.html")


@rep("/reports/sales", "rep_sales", "rep_sales")
def rep_sales():
    f, t = period()
    vid, cid, wid = to_int(request.args.get("visitor")), to_int(request.args.get("customer")), to_int(request.args.get("wh"))
    w, a = " WHERE s.date BETWEEN ? AND ?", [f, t]
    w, a = _f("s.visitor_id", vid, w, a)
    w, a = _f("s.customer_id", cid, w, a)
    w, a = _f("s.wh_id", wid, w, a)
    rows = q(f"""SELECT s.id sid, s.no, s.date, s.kind, cu.name cname, v.name vname, i.code, i.name, i.pack, i.unit, i.pack_unit, l.qty*s.sg qty, l.price,
                 (l.qty*l.price-l.disc)*s.sg amount, l.vat*s.sg vat
                 {SL} JOIN persons cu ON cu.id=s.customer_id LEFT JOIN persons v ON v.id=s.visitor_id JOIN items i ON i.id=l.item_id
                 {w} ORDER BY s.date, s.no, i.code""", a)
    data = [[link(r["no"], url_for("invoice", iid=r["sid"]), "bad" if r["kind"] == "return" else None), r["date"], r["cname"], r["vname"] or "",
             r["code"], r["name"], r["qty"], pkr(r["qty"], r), r["price"], r["amount"], r["vat"]] for r in rows]
    disc = v1(f"SELECT SUM(s.discount*s.sg) FROM v_si s {w}", a)
    sub = f"از {f} تا {t}" + (f" | ویزیتور: {_pname(vid)}" if vid else "") + (f" | مشتری: {_pname(cid)}" if cid else "") + (f" | انبار: {_whname(wid)}" if wid else "")
    spec = {"title": "گزارش ریز فروش", "sub": sub, "filters": ["period", "visitor", "customer", "wh"],
            "kpis": [("جمع اقلام (پس از تخفیف ردیف)", sum(r["amount"] for r in rows), "money"), ("تخفیف نهایی", disc, "money"),
                     ("فروش خالص", sum(r["amount"] for r in rows) - disc, "money"), ("مالیات ارزش افزوده", sum(r["vat"] for r in rows), "money")],
            "sections": [{"cols": [("فاکتور", "text"), ("تاریخ", "text"), ("مشتری", "text"), ("ویزیتور", "text"), ("کد", "text"), ("کالا", "text"),
                                   ("تعداد", "num"), ("تعداد (کارتن)", "text"), ("نرخ", "money"), ("مبلغ پس از تخفیف", "money"), ("مالیات", "money")],
                          "rows": data, "total": ["جمع", "", "", "", "", "", sum(r["qty"] for r in rows), "", "", sum(r["amount"] for r in rows),
                                                  sum(r["vat"] for r in rows)]}],
            "note": "برگشت از فروش با علامت منفی آمده است."}
    return export.out(spec, visitors=_visitors(), customers=_customers(), whs=warehouses_list(False))


@rep("/reports/returns", "rep_returns", "rep_sales")
def rep_returns():
    f, t = period()
    vid = to_int(request.args.get("visitor"))
    w, a = "", [f, t]
    w, a = _f("c.visitor_id", vid, w, a)
    rows = q(f"""SELECT c.id cid, c.return_no, c.settle_date, v.name vname, i.code, i.name, l.qty, l.returned, l.returned*l.price val
      FROM cons_lines l JOIN consignments c ON c.id=l.cons_id JOIN persons v ON v.id=c.visitor_id JOIN items i ON i.id=l.item_id
      WHERE c.status='settled' AND l.returned>0 AND c.settle_date BETWEEN ? AND ?{w} ORDER BY c.settle_date, c.return_no, i.code""", a)
    data = [[link(r["return_no"], url_for("print_return", cid=r["cid"])), r["settle_date"], r["vname"], r["code"], r["name"], r["qty"], r["returned"],
             f"{round(r['returned'] * 100.0 / r['qty'], 1)}%" if r["qty"] else "", r["val"]] for r in rows]
    spec = {"title": "گزارش مرجوعی", "sub": f"از {f} تا {t}" + (f" | ویزیتور: {_pname(vid)}" if vid else ""), "filters": ["period", "visitor"],
            "sections": [{"cols": [("برگه مرجوعی", "text"), ("تاریخ", "text"), ("ویزیتور", "text"), ("کد", "text"), ("کالا", "text"),
                                   ("تحویلی", "num"), ("مرجوعی", "num"), ("درصد مرجوعی", "text"), ("ارزش مرجوعی (ریال)", "money")],
                          "rows": data, "total": ["جمع", "", "", "", "", sum(r["qty"] for r in rows), sum(r["returned"] for r in rows), "",
                                                  sum(r["val"] for r in rows)]}]}
    return export.out(spec, visitors=_visitors())


@rep("/reports/visitors", "rep_visitors", "rep_sales")
def rep_visitors():
    f, t = period()
    rows = []
    for v in _visitors():
        s = sales_agg(f, t, vid=v["id"])
        issued = v1("SELECT SUM(l.qty) FROM cons_lines l JOIN consignments c ON c.id=l.cons_id WHERE c.visitor_id=? AND c.date BETWEEN ? AND ?", (v["id"], f, t))
        open_qty = v1("SELECT SUM(l.qty) FROM cons_lines l JOIN consignments c ON c.id=l.cons_id WHERE c.visitor_id=? AND c.status='open'", (v["id"],))
        ncust = v1("SELECT COUNT(DISTINCT customer_id) FROM sales_invoices WHERE visitor_id=? AND date BETWEEN ? AND ?", (v["id"], f, t))
        ret_pct = round(s["ret"] * 100.0 / (s["qty"] + s["ret"]), 1) if (s["qty"] + s["ret"]) else 0
        rows.append([v["code"], link(v["name"], url_for("person", pid=v["id"])), issued, s["ret"], f"{ret_pct}%", s["qty"], s["inv"], ncust, s["amount"],
                     s["pos"] + s["bank"], s["cash"] + s["chq"], receivable_until(t, v["id"], "visitor"), open_qty])
    rows.sort(key=lambda r: -r[8])
    tot = ["جمع", ""] + [sum(r[i] for r in rows) for i in (2, 3)] + [""] + [sum(r[i] for r in rows) for i in range(5, 13)]
    spec = {"title": "عملکرد ویزیتورها", "sub": f"از {f} تا {t}", "filters": ["period"],
            "charts": [{"id": "cv", "type": "bar", "horizontal": True, "title": "فروش هر ویزیتور (ریال)", "labels": [export.cell_v(r[1]) for r in rows],
                        "sets": [("فروش", [r[8] for r in rows])]}],
            "sections": [{"cols": [("کد", "text"), ("ویزیتور", "text"), ("تحویل امانی", "num"), ("مرجوعی", "num"), ("% مرجوعی", "text"),
                                   ("فروش (تعداد)", "num"), ("فاکتور", "num"), ("مشتری", "num"), ("فروش (ریال)", "money"), ("وصولی پوز/بانک", "money"), ("وصولی نقد/چک", "money"),
                                   ("مانده مطالبات مشتریانش", "money"), ("امانی باز", "num")], "rows": rows, "total": tot}]}
    return export.out(spec)


@rep("/reports/customers", "rep_customers", "rep_sales")
def rep_customers():
    f, t = period()
    vid = to_int(request.args.get("visitor"))
    w, a = " WHERE s.date BETWEEN ? AND ?", [f, t]
    w, a = _f("s.visitor_id", vid, w, a)
    rows = q(f"""SELECT cu.id, cu.code, cu.name, v.name vname, COUNT(*) FILTER (WHERE s.kind<>'return') inv, SUM(s.qty_total*s.sg) qty,
                 SUM(s.net_total*s.sg) net FROM v_si s JOIN persons cu ON cu.id=s.customer_id LEFT JOIN persons v ON v.id=cu.visitor_id {w}
                 GROUP BY cu.id, cu.code, cu.name, v.name ORDER BY net DESC""", a)
    data = [[r["code"], link(r["name"], url_for("fin_statement", person=r["id"], **{"as": "customer"})), r["vname"] or "", r["inv"], r["qty"],
             r["net"], receivable_until(t, r["id"])] for r in rows]
    spec = {"title": "فروش به تفکیک مشتری", "sub": f"از {f} تا {t}" + (f" | ویزیتور: {_pname(vid)}" if vid else ""), "filters": ["period", "visitor"],
            "sections": [{"cols": [("کد", "text"), ("مشتری", "text"), ("ویزیتور مسئول", "text"), ("فاکتور", "num"), ("تعداد", "num"), ("فروش خالص (بدون مالیات)", "money"),
                                   ("مانده فعلی", "money")], "rows": data, "total": ["جمع", "", ""] + [sum(r[i] for r in data) for i in range(3, 7)]}]}
    return export.out(spec, visitors=_visitors())


# ================= STOCK =================
@rep("/reports/stock", "rep_stock", "rep_stock")
def rep_stock():
    wid = to_int(request.args.get("wh")) or None
    rows, T = [], [0] * 6
    for r in stock_rows(wh_id=wid):
        s = r["st"]
        if not r["active"] and not s:
            continue
        vals = [s, r["open_amani"], s + r["open_amani"], s * r["vc"], r["open_amani"] * r["vc"], s * r["price"]]
        T = [a + b for a, b in zip(T, vals)]
        rows.append([r["code"], link(r["name"], url_for("rep_kardex", item=r["id"], wh=wid or "")), r["group_name"] or "", s, pkr(s, r), r["open_amani"],
                     pkr(r["open_amani"], r), s + r["open_amani"], r["vc"], r["price"], vals[3], vals[4], vals[5]])
    spec = {"title": "موجودی و ارزش انبار", "sub": f"تاریخ {today_j()}" + (f" | انبار: {_whname(wid)}" if wid else " | همه انبارها"), "filters": ["wh"],
            "kpis": [("موجودی انبار (واحد)", T[0], "num"), ("امانی نزد ویزیتورها (واحد)", T[1], "num"),
                     ("ارزش انبار (میانگین موزون)", T[3], "money"), ("ارزش امانی (میانگین موزون)", T[4], "money"), ("ارزش انبار به نرخ فروش", T[5], "money")],
            "sections": [{"cols": [("کد", "text"), ("کالا", "text"), ("گروه", "text"), ("موجودی انبار", "num"), ("انبار (کارتن)", "text"),
                                   ("امانی نزد ویزیتور", "num"), ("امانی (کارتن)", "text"), ("جمع موجودی", "num"), ("بهای واحد (میانگین موزون)", "money"),
                                   ("نرخ فروش واحد", "money"), ("ارزش انبار (میانگین موزون)", "money"), ("ارزش امانی (میانگین موزون)", "money"), ("ارزش انبار (نرخ فروش)", "money")],
                          "rows": rows, "total": ["جمع", "", "", T[0], "", T[1], "", T[2], "", "", T[3], T[4], T[5]]}]}
    return export.out(spec, whs=warehouses_list(False))


def _doc_link(m):
    t, d, no = m["doc_type"], m["doc_id"], m["doc_no"]
    url = {"RES": ("receipt_view", "rid"), "PUR": ("purchase_view", "pid"), "TRF": ("transfer_view", "tid"), "AMN": ("amani_view", "cid"),
           "MRJ": ("print_return", "cid"), "FCT": ("invoice", "iid"), "SRT": ("invoice", "iid")}.get(t)
    return link(no or "", url_for(url[0], **{url[1]: d}) if url else None)


@rep("/reports/kardex", "rep_kardex", "rep_stock")
def rep_kardex():
    f, t = period(default_from=year_start(today_j()))
    iid, wid = to_int(request.args.get("item")), to_int(request.args.get("wh")) or None
    items = _items()
    if not iid and items:
        iid = items[0]["id"]
    it = q("SELECT * FROM items WHERE id=?", (iid,), one=True)
    rows, tin, tout, bal = [], 0, 0, 0
    if it:
        w, a = " WHERE m.item_id=?", [iid]
        w, a = _f("m.wh_id", wid, w, a)
        bal = v1(f"SELECT SUM(m.qty) FROM stock_moves m {w} AND m.date < ?", a + [f])
        rows.append(["", "مانده از قبل", "", "", "", "", bal, pkr(bal, it)])
        for m in q(f"""SELECT m.*, w.name whname,
                        CASE m.doc_type WHEN 'PUR' THEN (SELECT p.name FROM purchases u JOIN persons p ON p.id=u.supplier_id WHERE u.id=m.doc_id)
                          WHEN 'AMN' THEN (SELECT p.name FROM consignments c JOIN persons p ON p.id=c.visitor_id WHERE c.id=m.doc_id)
                          WHEN 'MRJ' THEN (SELECT p.name FROM consignments c JOIN persons p ON p.id=c.visitor_id WHERE c.id=m.doc_id)
                          WHEN 'FCT' THEN (SELECT p.name FROM sales_invoices s JOIN persons p ON p.id=s.customer_id WHERE s.id=m.doc_id)
                          WHEN 'SRT' THEN (SELECT p.name FROM sales_invoices s JOIN persons p ON p.id=s.customer_id WHERE s.id=m.doc_id)
                          WHEN 'RES' THEN (SELECT note FROM receipts WHERE id=m.doc_id) ELSE '' END AS party,
                        (SELECT kind FROM purchases WHERE id=m.doc_id AND m.doc_type='PUR') AS pkind
                        FROM stock_moves m JOIN warehouses w ON w.id=m.wh_id {w} AND m.date BETWEEN ? AND ? ORDER BY m.date, m.id""", a + [f, t]):
            bal += m["qty"]
            tin += max(m["qty"], 0); tout += max(-m["qty"], 0)
            lab = DOC_LABEL["PRT"] if m["pkind"] == "return" else DOC_LABEL.get(m["doc_type"], m["doc_type"])
            rows.append([m["date"], lab, _doc_link(m), m["party"] or "", m["whname"], m["qty"] if m["qty"] > 0 else "", -m["qty"] if m["qty"] < 0 else "", bal, pkr(bal, it)])
        rows[0] = ["", "مانده از قبل", "", "", "", "", "", rows[0][6], rows[0][7]]
    spec = {"title": f"کاردکس کالا: {it['code']} - {it['name']}" if it else "کاردکس کالا",
            "sub": f"از {f} تا {t}" + (f" | انبار: {_whname(wid)}" if wid else " | همه انبارها"), "filters": ["period", "item", "wh"],
            "sections": [{"cols": [("تاریخ", "text"), ("شرح", "text"), ("شماره سند", "text"), ("طرف حساب / توضیح", "text"), ("انبار", "text"),
                                   ("وارده", "num"), ("صادره", "num"), ("مانده", "num"), ("مانده (کارتن)", "text")],
                          "rows": rows, "total": ["جمع دوره", "", "", "", "", tin, tout, bal, ""]}]}
    return export.out(spec, items=items, item_id=iid, whs=warehouses_list(False))


@rep("/reports/amani", "rep_amani", "rep_stock")
def rep_amani():
    vid = to_int(request.args.get("visitor"))
    w, a = ("AND c.visitor_id=?", [vid]) if vid else ("", [])
    by_item = q(f"""SELECT i.code, i.name, i.unit, i.pack, i.pack_unit, SUM(l.qty) qty, SUM(l.qty*l.price) val FROM cons_lines l JOIN consignments c ON c.id=l.cons_id
                    JOIN items i ON i.id=l.item_id WHERE c.status='open' {w} GROUP BY i.id ORDER BY i.code""", a)
    by_v = q(f"""SELECT v.code, v.name, COUNT(DISTINCT c.id) docs, MIN(c.date) oldest, SUM(l.qty) qty, SUM(l.qty*l.price) val FROM cons_lines l
                 JOIN consignments c ON c.id=l.cons_id JOIN persons v ON v.id=c.visitor_id WHERE c.status='open' {w} GROUP BY v.id ORDER BY v.code""", a)
    docs = q(CONS_SQL + f" WHERE c.status='open' {w} ORDER BY c.date, c.no", a)
    t = today_j()
    spec = {"title": "کالای امانی نزد ویزیتورها", "sub": f"تاریخ {t}" + (f" | ویزیتور: {_pname(vid)}" if vid else ""), "filters": ["visitor"],
            "kpis": [("تعداد کل امانی", sum(r["qty"] for r in by_item), "num"), ("ارزش امانی (ریال)", sum(r["val"] for r in by_item), "money"),
                     ("فاکتور امانی باز", len(docs), "num")],
            "sections": [
                {"title": "به تفکیک ویزیتور", "cols": [("کد", "text"), ("ویزیتور", "text"), ("سند باز", "num"), ("قدیمی‌ترین", "text"), ("تعداد", "num"), ("ارزش (ریال)", "money")],
                 "rows": [[r["code"], r["name"], r["docs"], r["oldest"], r["qty"], r["val"]] for r in by_v],
                 "total": ["جمع", "", sum(r["docs"] for r in by_v), "", sum(r["qty"] for r in by_v), sum(r["val"] for r in by_v)]},
                {"title": "به تفکیک کالا", "cols": [("کد", "text"), ("کالا", "text"), ("تعداد", "num"), ("تعداد (کارتن)", "text"), ("ارزش (ریال)", "money")],
                 "rows": [[r["code"], r["name"], r["qty"], pkr(r["qty"], r), r["val"]] for r in by_item],
                 "total": ["جمع", "", sum(r["qty"] for r in by_item), "", sum(r["val"] for r in by_item)]},
                {"title": "فاکتورهای امانی باز", "cols": [("شماره", "text"), ("تاریخ", "text"), ("روز گذشته", "num"), ("ویزیتور", "text"), ("انبار", "text"),
                                                         ("تعداد", "num"), ("ارزش (ریال)", "money")],
                 "rows": [[link(r["no"], url_for("amani_view", cid=r["id"])), r["date"], diff_days(r["date"], t), r["vname"], r["whname"], r["qty"], r["amani_value"]] for r in docs]}]}
    return export.out(spec, visitors=_visitors())


@rep("/reports/purchases", "rep_purchases", "rep_stock")
def rep_purchases():
    f, t = period()
    sid = to_int(request.args.get("supplier"))
    w, a = " WHERE u.date BETWEEN ? AND ?", [f, t]
    w, a = _f("u.supplier_id", sid, w, a)
    rows = q(f"""SELECT u.id, u.no, u.kind, u.date, s.name sname, i.code, i.name, i.pack, i.unit, i.pack_unit, l.qty, l.price, l.qty*l.price-l.disc amount, l.vat
                 FROM purchase_lines l JOIN purchases u ON u.id=l.pur_id JOIN persons s ON s.id=u.supplier_id JOIN items i ON i.id=l.item_id
                 {w} ORDER BY u.date, u.no, i.code""", a)
    sg = lambda r: -1 if r["kind"] == "return" else 1
    data = [[link(r["no"], url_for("purchase_view", pid=r["id"])), "برگشت" if r["kind"] == "return" else "خرید", r["date"], r["sname"], r["code"], r["name"],
             sg(r) * r["qty"], pkr(r["qty"], r), r["price"], sg(r) * r["amount"], sg(r) * r["vat"]] for r in rows]
    disc = v1(f"SELECT SUM(CASE WHEN u.kind='return' THEN -u.discount ELSE u.discount END) FROM purchases u {w}", a)
    spec = {"title": "گزارش خرید", "sub": f"از {f} تا {t}" + (f" | تامین‌کننده: {_pname(sid)}" if sid else ""), "filters": ["period", "supplier"],
            "kpis": [("جمع اقلام (پس از تخفیف ردیف)", sum(d[9] for d in data), "money"), ("تخفیف نهایی", disc, "money"),
                     ("خرید خالص", sum(d[9] for d in data) - disc, "money"), ("مالیات خرید", sum(d[10] for d in data), "money")],
            "sections": [{"cols": [("شماره", "text"), ("نوع", "text"), ("تاریخ", "text"), ("تامین‌کننده", "text"), ("کد", "text"), ("کالا", "text"),
                                   ("تعداد", "num"), ("تعداد (کارتن)", "text"), ("نرخ", "money"), ("مبلغ پس از تخفیف", "money"), ("مالیات", "money")],
                          "rows": data, "total": ["جمع", "", "", "", "", "", sum(d[6] for d in data), "", "", sum(d[9] for d in data), sum(d[10] for d in data)]}]}
    return export.out(spec, suppliers=q("SELECT id, code, name FROM persons WHERE is_supplier=1 ORDER BY code"))


# ================= FINANCIAL =================
@rep("/reports/fin/summary", "fin_summary", "rep_fin")
def fin_summary():
    f, t = period()
    s = sales_agg(f, t)
    st = stock_rows()
    inv_cost = sum(r["st"] * r["vc"] for r in st)
    amani_cost = sum(r["open_amani"] * r["vc"] for r in st)
    r0, r1 = receivable_until(f, before=True), receivable_until(t)
    p0, p1 = payable_until(f, before=True), payable_until(t)
    purchases = v1("SELECT SUM(total*sg) FROM v_pu WHERE date BETWEEN ? AND ?", (f, t))
    margin = round(s["profit"] * 100.0 / s["amount"], 1) if s["amount"] else 0
    pv = q("SELECT COALESCE(SUM(vat_total*sg),0) v, COALESCE(SUM(net_total*sg),0) n FROM v_pu WHERE date BETWEEN ? AND ?", (f, t), one=True)
    rows1 = [["فروش ناخالص (پس از کسر برگشت از فروش)", s["gross"]], ["کسر می‌شود: تخفیفات فروش", -s["disc"]], ["فروش خالص", s["amount"]],
             ["کسر می‌شود: بهای تمام‌شده کالای فروش‌رفته", -s["cogs"]], ["سود ناخالص", s["profit"]], [f"حاشیه سود ناخالص: {margin}%", None],
             ["(اطلاعات) ارزش کالای مرجوعی امانی در دوره", s["ret_val"]]]
    rows2 = [["مانده مطالبات از مشتریان ابتدای دوره", r0], ["اضافه می‌شود: فروش خالص دوره", s["amount"]],
             ["اضافه می‌شود: مالیات ارزش افزوده فروش", s["vat"]],
             ["کسر می‌شود: وصولی کارت‌خوان و بانک", -(s["pos"] + s["bank"])], ["کسر می‌شود: وصولی نقد", -s["cash"]], ["کسر می‌شود: چک‌های دریافتی", -s["chq"]],
             ["سایر (برگشت چک، اصلاحی، …)", r1 - r0 - s["amount"] - s["vat"] + s["coll"]], ["مانده مطالبات پایان دوره", r1]]
    paid = v1("""SELECT SUM(l.amount) FROM tr_lines l JOIN tr_docs d ON d.id=l.doc_id JOIN persons p ON p.id=d.person_id
                 WHERE d.kind='pay' AND p.is_supplier=1 AND d.acc_id IS NULL AND d.date BETWEEN ? AND ?""", (f, t))
    rows3 = [["مانده بدهی به تامین‌کنندگان ابتدای دوره", p0], ["اضافه می‌شود: خرید خالص دوره (با مالیات)", purchases],
             ["کسر می‌شود: پرداخت‌ها", -paid], ["سایر", p1 - p0 - purchases + paid], ["مانده بدهی پایان دوره", p1]]
    rows5 = [["مالیات و عوارض فروش (دریافتی از مشتریان)", s["vat"]], ["کسر می‌شود: مالیات و عوارض خرید", -pv["v"]],
             ["مالیات ارزش افزوده پرداختنی دوره", s["vat"] - pv["v"]]]
    rows4 = [["موجودی کالای انبارها (بهای خرید)", inv_cost], ["کالای امانی نزد ویزیتورها (بهای خرید)", amani_cost], ["جمع موجودی کالا", inv_cost + amani_cost]]
    spec = {"title": "خلاصه وضعیت مالی", "sub": f"از {f} تا {t}", "filters": ["period"],
            "kpis": [("فروش خالص", s["amount"], "money"), ("سود ناخالص", s["profit"], "money"), ("وصولی دوره", s["coll"], "money"),
                     ("مانده مطالبات", r1, "money"), ("بدهی به تامین‌کنندگان", p1, "money")],
            "sections": [{"title": "صورت سود ناخالص", "cols": [("شرح", "text"), ("مبلغ (ریال)", "money")], "rows": rows1},
                         {"title": "حساب‌های دریافتنی (مشتریان)", "cols": [("شرح", "text"), ("مبلغ (ریال)", "money")], "rows": rows2},
                         {"title": "حساب‌های پرداختنی (تامین‌کنندگان)", "cols": [("شرح", "text"), ("مبلغ (ریال)", "money")], "rows": rows3},
                         {"title": "مالیات و عوارض ارزش افزوده", "cols": [("شرح", "text"), ("مبلغ (ریال)", "money")], "rows": rows5},
                         {"title": "موجودی کالا", "cols": [("شرح", "text"), ("مبلغ (ریال)", "money")], "rows": rows4}],
            "note": "بهای تمام‌شده = تعداد فروش × نرخ خرید کالا در تاریخ فاکتور (ثبت‌شده روی فاکتور). ارزش موجودی به نرخ خرید فعلی."}
    return export.out(spec)


@rep("/reports/fin/trial", "fin_trial", "rep_fin")
def fin_trial():
    """balance of every person over all accounts (receivable, payable, salary, cheques bounced...)"""
    f, t = period()
    groups = [("is_customer", "مشتریان"), ("is_visitor", "ویزیتورها"), ("is_supplier", "تامین‌کنندگان"), ("is_employee", "کارکنان"), ("is_other", "سایر اشخاص")]
    S = {r["p"]: r for r in q("""SELECT vl.person_id p, SUM(CASE WHEN v.date<? THEN vl.debit-vl.credit ELSE 0 END) op,
                                 SUM(CASE WHEN v.date BETWEEN ? AND ? THEN vl.debit ELSE 0 END) dr, SUM(CASE WHEN v.date BETWEEN ? AND ? THEN vl.credit ELSE 0 END) cr
                                 FROM voucher_lines vl JOIN vouchers v ON v.id=vl.voucher_id WHERE vl.person_id IS NOT NULL AND v.date<=?
                                 GROUP BY vl.person_id""", (f, f, t, f, t, t))}
    seen, secs = set(), []
    cols = [("کد", "text"), ("نام", "text"), ("مانده ابتدای دوره", "money"), ("گردش بدهکار", "money"), ("گردش بستانکار", "money"),
            ("مانده بدهکار", "money"), ("مانده بستانکار", "money"), ("تشخیص", "text")]
    for flag, title in groups:
        rows = []
        for p in q(f"SELECT id, code, name FROM persons WHERE {flag}=1 ORDER BY code"):
            if p["id"] in seen or p["id"] not in S:
                continue
            seen.add(p["id"])
            r = S[p["id"]]
            cl = r["op"] + r["dr"] - r["cr"]
            rows.append([p["code"], link(p["name"], url_for("fin_statement", person=p["id"], **{"from": f, "to": t})), r["op"], r["dr"], r["cr"],
                         cl if cl > 0 else 0, -cl if cl < 0 else 0, "بدهکار" if cl > 0 else ("بستانکار" if cl < 0 else "—")])
        if rows:
            secs.append({"title": title, "cols": cols, "rows": rows, "total": ["جمع", ""] + [sum(r[i] for r in rows) for i in range(2, 7)] + [""]})
    spec = {"title": "تراز اشخاص", "sub": f"از {f} تا {t}", "filters": ["period"], "sections": secs or [{"cols": cols, "rows": []}],
            "note": "مانده از دفتر کل: فاکتورها، دریافت/پرداخت‌ها، چک‌های برگشتی و اسناد دستی."}
    return export.out(spec)


@rep("/reports/fin/statement", "fin_statement", "rep_fin")
def fin_statement():
    """صورت‌حساب: person (from the ledger, every account) | as=visitor: all invoices of his customers + receipts registered for him"""
    from accounting import src_link
    f, t = period(default_from=year_start(today_j()))
    as_ = "visitor" if request.args.get("as") == "visitor" else "person"
    ps = q("SELECT id, code, name FROM persons WHERE is_visitor=1 ORDER BY code") if as_ == "visitor" else \
        q("SELECT id, code, name FROM persons ORDER BY is_customer DESC, code")
    pid = to_int(request.args.get("person")) or (ps[0]["id"] if ps else 0)
    if as_ == "visitor":
        ent = []
        bal = receivable_until(f, pid, "visitor", before=True)
        for r in q(INV_SQL + " WHERE s.visitor_id=? AND s.date BETWEEN ? AND ? ORDER BY s.date, s.no", (pid, f, t)):
            ret = r["kind"] == "return"
            ent.append((r["date"], 0, link(f"{'برگشت از فروش' if ret else 'فاکتور فروش'} {r['no']}", url_for("invoice", iid=r["id"])), r["cname"],
                        f"{r['sold_qty']:,} واحد" + (f" / امانی {r['cons_no']}" if r["cons_no"] else ""), 0 if ret else r["total"], r["total"] if ret else 0))
        for p in q("""SELECT p.*, cu.name cname, s.no ino FROM payments p LEFT JOIN persons cu ON cu.id=p.person_id LEFT JOIN sales_invoices s ON s.id=p.inv_id
                      WHERE p.visitor_id=? AND p.date BETWEEN ? AND ?""", (pid, f, t)):
            ent.append((p["date"], 1, link(f"سند دریافت {p['no']}", url_for("tr_view", did=p["doc_id"])), p["cname"] or "",
                        f"{METHOD.get(p['method'], p['method'])}" + (f" - فاکتور {p['ino']}" if p["ino"] else " - علی‌الحساب") + f" {p['ref'] or ''}", 0, p["amount"]))
        ent.sort(key=lambda e: (e[0], e[1]))
        rows = [["", "مانده از قبل", "", "", "", "", bal, "بد" if bal > 0 else ("بس" if bal < 0 else "—")]]
        dr = cr = 0
        for e in ent:
            bal += e[5] - e[6]; dr += e[5]; cr += e[6]
            rows.append([e[0], e[2], e[3], e[4], e[5] or "", e[6] or "", bal, "بد" if bal > 0 else ("بس" if bal < 0 else "—")])
        c3, title = "مشتری", "صورت‌حساب ویزیتور (همه مشتریان)"
    else:
        bal = v1("SELECT SUM(vl.debit-vl.credit) FROM voucher_lines vl JOIN vouchers v ON v.id=vl.voucher_id WHERE vl.person_id=? AND v.date<?", (pid, f))
        rows = [["", "مانده از قبل", "", "", "", "", bal, "بد" if bal > 0 else ("بس" if bal < 0 else "—")]]
        dr = cr = 0
        for l in q("""SELECT vl.*, v.date, v.no vno, v.descr vdescr, v.src_type, v.src_id, a.name aname FROM voucher_lines vl
                      JOIN vouchers v ON v.id=vl.voucher_id JOIN accounts a ON a.id=vl.account_id
                      WHERE vl.person_id=? AND v.date BETWEEN ? AND ? ORDER BY v.date, v.src_type='TRD', v.no, vl.id""", (pid, f, t)):
            bal += l["debit"] - l["credit"]; dr += l["debit"]; cr += l["credit"]
            rows.append([l["date"], link(l["vdescr"] or l["descr"] or "", src_link(l["src_type"], l["src_id"]) or url_for("voucher_view", vid=l["voucher_id"])),
                         l["aname"], l["descr"] or "", l["debit"] or "", l["credit"] or "", bal, "بد" if bal > 0 else ("بس" if bal < 0 else "—")])
        c3, title = "حساب", "صورت‌حساب"
    spec = {"title": f"{title}: {_pname(pid) or ''}", "sub": f"از {f} تا {t}", "filters": ["period", "person"],
            "sections": [{"cols": [("تاریخ", "text"), ("سند", "text"), (c3, "text"), ("شرح", "text"), ("بدهکار", "money"), ("بستانکار", "money"),
                                   ("مانده", "money"), ("تشخیص", "text")],
                          "rows": rows, "total": ["جمع دوره", "", "", "", dr, cr, bal, ""]}],
            "note": "بد = شخص به ما بدهکار است • بس = ما به شخص بدهکاریم."}
    return export.out(spec, persons=ps, person_id=pid, as_=as_ if as_ == "visitor" else "")


@rep("/reports/fin/aging", "fin_aging", "rep_fin")
def fin_aging():
    t = today_j()
    vid = to_int(request.args.get("visitor"))
    B = [(0, 7, "۰ تا ۷ روز"), (8, 30, "۸ تا ۳۰ روز"), (31, 60, "۳۱ تا ۶۰ روز"), (61, 90, "۶۱ تا ۹۰ روز"), (91, 10 ** 6, "بیش از ۹۰ روز")]
    det, summ = [], {}
    for u in unpaid_invoices(t):
        if vid and u["v"] != _pname(vid):
            continue
        bi = next(i for i, b in enumerate(B) if b[0] <= u["age"] <= b[1])
        det.append([link(u["no"], url_for("invoice", iid=u["id"])), u["date"], u["c"], u["v"], u["age"], u["total"], u["paid"], u["rem"], B[bi][2]])
        s = summ.setdefault((u["ccode"], u["c"], u["v"]), [0] * 6)
        s[bi] += u["rem"]; s[5] += u["rem"]
    rows = [[k[0], k[1], k[2]] + v for k, v in sorted(summ.items())]
    tot = ["جمع", "", ""] + [sum(r[i] for r in rows) for i in range(3, 9)]
    spec = {"title": "گزارش سنی مطالبات", "sub": f"به تاریخ {t}" + (f" | ویزیتور: {_pname(vid)}" if vid else ""), "filters": ["visitor"],
            "kpis": [(b[2], tot[3 + i] if rows else 0, "money") for i, b in enumerate(B)] + [("جمع مطالبات", tot[8] if rows else 0, "money")],
            "sections": [{"title": "خلاصه به تفکیک مشتری", "cols": [("کد", "text"), ("مشتری", "text"), ("ویزیتور", "text")] + [(b[2], "money") for b in B] + [("جمع", "money")],
                          "rows": rows, "total": tot if rows else None},
                         {"title": "ریز فاکتورهای باز", "cols": [("فاکتور", "text"), ("تاریخ", "text"), ("مشتری", "text"), ("ویزیتور", "text"), ("سن (روز)", "num"),
                                                                ("مبلغ فاکتور", "money"), ("دریافتی", "money"), ("مانده", "money"), ("دوره", "text")],
                          "rows": det, "total": ["جمع", "", "", "", "", sum(d[5] for d in det), sum(d[6] for d in det), sum(d[7] for d in det), ""] if det else None}]}
    return export.out(spec, visitors=_visitors())


PAY_DET = """SELECT p.*, s.no ino, s.id sid, cu.name cname, v.name vname, v.id vid, v.code vcode, COALESCE(NULLIF(u.fullname,''),u.username) un
  FROM payments p LEFT JOIN sales_invoices s ON s.id=p.inv_id LEFT JOIN persons cu ON cu.id=p.person_id LEFT JOIN persons v ON v.id=p.visitor_id
  LEFT JOIN users u ON u.id=p.created_by"""


def _ino(d):
    return link(d["ino"], url_for("invoice", iid=d["sid"])) if d["sid"] else "علی‌الحساب"


@rep("/reports/fin/cashbook", "fin_cashbook", "rep_fin")
def fin_cashbook():
    f, t = period()
    method = request.args.get("method", "")
    days = q(f"SELECT p.date d, {PAY_COLS}, COUNT(*) n FROM payments p WHERE p.date BETWEEN ? AND ? GROUP BY p.date ORDER BY p.date", (f, t))
    det = q(PAY_DET + " WHERE p.date BETWEEN ? AND ? ORDER BY p.date, p.no", (f, t))
    if method in METHOD:
        det = [d for d in det if d["method"] == method]
    K = ("pos", "cash", "bank", "chq", "coll")
    spec = {"title": "دفتر دریافت روزانه (صندوق، کارت‌خوان، بانک، چک)", "sub": f"از {f} تا {t}", "filters": ["period", "method"],
            "kpis": [("دریافت پوز", sum(d["pos"] for d in days), "money"), ("دریافت نقد", sum(d["cash"] for d in days), "money"),
                     ("بانک و چک", sum(d["bank"] + d["chq"] for d in days), "money"), ("جمع دریافت", sum(d["coll"] for d in days), "money")],
            "sections": [{"title": "خلاصه روزانه", "cols": [("تاریخ", "text"), ("روز", "text"), ("تعداد", "num"), ("پوز", "money"), ("نقد", "money"),
                                                           ("بانکی", "money"), ("چک", "money"), ("جمع", "money")],
                          "rows": [[d["d"], WEEKDAYS[weekday(d["d"])], d["n"]] + [d[k] for k in K] for d in days],
                          "total": ["جمع", "", sum(d["n"] for d in days)] + [sum(d[k] for d in days) for k in K]},
                         {"title": "ریز اسناد دریافت", "cols": [("سند", "text"), ("تاریخ", "text"), ("مشتری", "text"), ("ویزیتور", "text"), ("فاکتور", "text"),
                                                               ("نوع", "text"), ("پیگیری", "text"), ("کاربر", "text"), ("مبلغ", "money")],
                          "rows": [[link(d["no"], url_for("tr_view", did=d["doc_id"])), d["date"], d["cname"] or "", d["vname"] or "", _ino(d), METHOD[d["method"]],
                                    d["ref"] or "", d["un"] or "", d["amount"]] for d in det],
                          "total": ["جمع", "", "", "", "", "", "", "", sum(d["amount"] for d in det)]}]}
    return export.out(spec)


@rep("/reports/fin/daily", "fin_daily", "rep_fin")
def fin_daily():
    f, t = period(default_from=today_j())
    vid = to_int(request.args.get("visitor"))
    w, a = "", [f, t]
    w, a = _f("p.visitor_id", vid, w, a)
    rows = q(f"""SELECT p.date d, v.id vid, COALESCE(v.code,'-') code, COALESCE(v.name,'بدون ویزیتور') name, COUNT(*) n,
                 SUM(CASE WHEN p.method IN ('pos','bank') THEN p.amount ELSE 0 END) pos, SUM(CASE WHEN p.method IN ('cash','cheque') THEN p.amount ELSE 0 END) cash
                 FROM payments p LEFT JOIN persons v ON v.id=p.visitor_id
                 WHERE p.date BETWEEN ? AND ?{w} GROUP BY p.date, v.id, v.code, v.name ORDER BY p.date DESC, code""", a)
    data, day, sub = [], None, None

    def flush():
        if sub and sub["k"] > 1:
            data.append([f"جمع {day}", "", sub["n"], sub["pos"], sub["cash"], sub["pos"] + sub["cash"]])
    for r in rows:
        if r["d"] != day:
            flush()
            day, sub = r["d"], {"n": 0, "pos": 0, "cash": 0, "k": 0}
        sub["n"] += r["n"]; sub["pos"] += r["pos"]; sub["cash"] += r["cash"]; sub["k"] += 1
        data.append([f"{r['d']} {WEEKDAYS[weekday(r['d'])]}", link(f"{r['code']} - {r['name']}", url_for("fin_statement", person=r["vid"], **{"as": "visitor"}) if r["vid"] else None),
                     r["n"], r["pos"], r["cash"], r["pos"] + r["cash"]])
    flush()
    by_v = {}
    for r in rows:
        b = by_v.setdefault((r["code"], r["name"]), [0, 0, 0, 0])
        b[0] += r["n"]; b[1] += r["pos"]; b[2] += r["cash"]; b[3] += r["pos"] + r["cash"]
    summ = [[k[0], k[1]] + v for k, v in sorted(by_v.items(), key=lambda kv: -kv[1][3])]
    TP, TC = sum(r["pos"] for r in rows), sum(r["cash"] for r in rows)
    spec = {"title": "جمع دریافتی روزانه به تفکیک ویزیتور", "sub": f"از {f} تا {t}" + (f" | ویزیتور: {_pname(vid)}" if vid else ""),
            "filters": ["period", "visitor"],
            "kpis": [("پوز و بانک", TP, "money"), ("نقد و چک", TC, "money"), ("جمع دریافت", TP + TC, "money")],
            "charts": [{"id": "cd", "type": "bar", "horizontal": True, "title": "دریافتی هر ویزیتور (ریال)", "labels": [r[1] for r in summ],
                        "sets": [("پوز و بانک", [r[3] for r in summ]), ("نقد و چک", [r[4] for r in summ])]}] if summ else [],
            "sections": [{"title": "به تفکیک ویزیتور", "cols": [("کد", "text"), ("ویزیتور", "text"), ("تعداد سند", "num"), ("پوز و بانک", "money"), ("نقد و چک", "money"), ("جمع", "money")],
                          "rows": summ, "total": ["جمع", "", sum(r[2] for r in summ), TP, TC, TP + TC]},
                         {"title": "روز به روز", "cols": [("تاریخ", "text"), ("ویزیتور", "text"), ("تعداد سند", "num"), ("پوز", "money"), ("نقد", "money"), ("جمع", "money")],
                          "rows": data, "total": ["جمع", "", sum(r["n"] for r in rows), TP, TC, TP + TC]}]}
    return export.out(spec, visitors=_visitors())


@rep("/reports/fin/profit", "fin_profit", "rep_fin")
def fin_profit():
    """سود و زیان فروش با بهای تمام‌شده میانگین موزون: به تفکیک فاکتور، کالا، مشتری، ویزیتور، گروه، تامین‌کننده، ماه، روز"""
    import gl
    f, t = period(default_from=year_start(today_j()))
    BY = {"invoice": ("فاکتور", "s.no::text", "s.date || ' — ' || c.name", "JOIN persons c ON c.id=s.customer_id", "MIN(s.date), MIN(s.no)"),
          "item": ("کالا", "i.code", "i.name", "JOIN items i ON i.id=l.item_id", "sales DESC"),
          "customer": ("مشتری", "c.code", "c.name", "JOIN persons c ON c.id=s.customer_id", "sales DESC"),
          "visitor": ("ویزیتور", "COALESCE(v.code,'-')", "COALESCE(v.name,'بدون ویزیتور')", "LEFT JOIN persons v ON v.id=s.visitor_id", "sales DESC"),
          "group": ("گروه کالا", "COALESCE(gr.code,'-')", "COALESCE(gr.name,'بدون گروه')", "JOIN items i ON i.id=l.item_id LEFT JOIN item_groups gr ON gr.id=i.group_id", "sales DESC"),
          "supplier": ("تامین‌کننده کالا", "COALESCE(sp.code,'-')", "COALESCE(sp.name,'بدون تامین‌کننده')", "JOIN items i ON i.id=l.item_id LEFT JOIN persons sp ON sp.id=i.supplier_id", "sales DESC"),
          "month": ("ماه", "SUBSTRING(s.date,1,7)", "SUBSTRING(s.date,1,7)", "", "1"),
          "day": ("روز", "s.date", "s.date", "", "1")}
    by = request.args.get("by") if request.args.get("by") in BY else "item"
    lab, kx, nx, join, order = BY[by]
    vid, cid = to_int(request.args.get("visitor")), to_int(request.args.get("customer"))
    w, a = " WHERE s.date BETWEEN ? AND ?", [f, t]
    if vid:
        w += " AND s.visitor_id=?"; a.append(vid)
    if cid:
        w += " AND s.customer_id=?"; a.append(cid)
    # line amount after its share of the invoice's final discount, without VAT
    net = "(l.qty*l.price-l.disc) * COALESCE(1 - s.discount::float / NULLIF(s.gross-s.ldisc,0), 1)"
    rows = q(f"""SELECT {kx} k, {nx} n, MIN(s.id) sid, COUNT(DISTINCT s.id) FILTER (WHERE s.kind<>'return') ninv, SUM(l.qty*s.sg) qty,
                 SUM({net}*s.sg) sales, SUM(l.qty*l.cost*s.sg) cogs {SL} {join} {w} GROUP BY 1, 2 ORDER BY {order}""", a)
    S, C = sum(r["sales"] for r in rows), sum(r["cogs"] for r in rows)
    P_ = S - C
    exp = -v1("""SELECT SUM(vl.credit-vl.debit) FROM voucher_lines vl JOIN vouchers v ON v.id=vl.voucher_id JOIN accounts ac ON ac.id=vl.account_id
                 WHERE ac.code LIKE '8%' AND v.date BETWEEN ? AND ?""", (f, t)) if not (vid or cid) else 0

    def mg(s_, c_):
        return round((s_ - c_) * 100.0 / s_, 1) if s_ else None
    cum, data = 0, []
    for r in rows:
        p = r["sales"] - r["cogs"]
        cum += p
        name = link(r["n"], url_for("invoice", iid=r["sid"])) if by == "invoice" else r["n"]
        row = [r["k"], name, r["ninv"], r["qty"], r["sales"], r["cogs"], p, mg(r["sales"], r["cogs"]), round(p * 100.0 / P_, 1) if P_ else None]
        if by in ("month", "day", "invoice"):
            row.append(cum)
        data.append(row)
    cols = [("کد" if by not in ("month", "day") else lab, "text"), (lab if by not in ("month", "day") else "", "text"), ("تعداد فاکتور", "num"),
            ("تعداد فروش (باکس)", "num"), ("فروش خالص (بدون مالیات)", "money"), ("بهای تمام‌شده (میانگین موزون)", "money"), ("سود ناخالص", "money"),
            ("حاشیه سود %", "pct"), ("سهم از سود %", "pct")] + ([("سود تجمعی", "money")] if by in ("month", "day", "invoice") else [])
    total = ["جمع", "", sum(r["ninv"] for r in rows), sum(r["qty"] for r in rows), S, C, P_, mg(S, C), 100.0 if P_ else None] + ([P_] if by in ("month", "day", "invoice") else [])
    charts = []
    if by in ("month", "day") and rows:
        charts = [{"id": "pf", "type": "bar", "title": "فروش و سود", "labels": [r["k"] for r in rows],
                   "sets": [("فروش خالص", [r["sales"] for r in rows]), ("سود ناخالص", [r["sales"] - r["cogs"] for r in rows])]}]
    kp = [("فروش خالص", S, "money"), ("بهای تمام‌شده", C, "money"), ("سود ناخالص", P_, "money"), ("حاشیه سود", mg(S, C), "pct")]
    if not (vid or cid):
        kp = kp[:3] + [("هزینه‌ها (دفتر کل)", exp, "money"), ("سود خالص", P_ - exp, "money")]
    spec = {"title": f"سود و زیان فروش — به تفکیک {lab}", "sub": f"از {f} تا {t}" + (f" | ویزیتور: {_pname(vid)}" if vid else "") + (f" | مشتری: {_pname(cid)}" if cid else ""),
            "filters": ["period", "visitor", "customer"],
            "selects": [("by", [(k, "به تفکیک " + v[0]) for k, v in BY.items()], by)],
            "kpis": kp, "charts": charts,
            "sections": [{"cols": cols, "rows": data, "total": total}],
            "note": "بهای تمام‌شده هر فروش = میانگین موزون متحرک خرید کالا در لحظه فروش (همه خریدها و برگشت‌ها به ترتیب تاریخ). "
                    "فروش خالص = مبلغ پس از تخفیف ردیف و سهم تخفیف نهایی فاکتور، بدون مالیات؛ برگشت از فروش کسر شده است."}
    return export.out(spec, visitors=_visitors(), customers=_customers())


@rep("/reports/fin/payments", "fin_payments", "rep_fin")
def fin_payments():
    f, t = period()
    vid, cid = to_int(request.args.get("visitor")), to_int(request.args.get("customer"))
    method = request.args.get("method", "")
    w, a = " WHERE p.date BETWEEN ? AND ?", [f, t]
    w, a = _f("p.visitor_id", vid, w, a)
    w, a = _f("p.person_id", cid, w, a)
    if method in METHOD:
        w += " AND p.method=?"; a.append(method)
    rows = q(PAY_DET + w + " ORDER BY p.date, p.no", a)
    spec = {"title": "گزارش دریافتی‌ها", "sub": f"از {f} تا {t}", "filters": ["period", "visitor", "customer", "method"],
            "sections": [{"cols": [("سند", "text"), ("تاریخ", "text"), ("مشتری", "text"), ("ویزیتور", "text"), ("فاکتور", "text"), ("نوع", "text"),
                                   ("پیگیری", "text"), ("مبلغ", "money")],
                          "rows": [[link(p["no"], url_for("tr_view", did=p["doc_id"])), p["date"], p["cname"] or "", p["vname"] or "", _ino(p), METHOD[p["method"]],
                                    p["ref"] or "", p["amount"]] for p in rows],
                          "total": ["جمع", "", "", "", "", "", "", sum(p["amount"] for p in rows)]}]}
    return export.out(spec, visitors=_visitors(), customers=_customers())
