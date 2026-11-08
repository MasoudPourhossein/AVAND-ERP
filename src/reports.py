from flask import render_template, request, url_for, jsonify
from core import (app, q, v1, to_int, stock_rows, INV_SQL, CONS_SQL, period, can, register_report, METHOD, pkr, warehouses_list)
from jalali import (today_j, add_days, diff_days, week_start, month_start, year_start, shift_month, shift_year,
                    month_end, weekday, parts, WEEKDAYS, MONTHS)
import export

SL = "FROM sales_lines l JOIN sales_invoices s ON s.id=l.inv_id"
DOC_LABEL = {"RES": "رسید انبار", "PUR": "فاکتور خرید", "PRT": "برگشت از خرید", "TRF": "حواله انبار", "AMN": "حواله امانی",
             "MRJ": "برگشت مرجوعی", "FCT": "فاکتور فروش"}


def link(v, href, cls=None):
    return {"v": v, "href": href, "cls": cls}


def pct(cur, prev):
    return None if not prev or cur is None else (cur - prev) * 100.0 / abs(prev)


def _f(col, val, w, a):
    if val:
        w += f" AND {col}=?"; a.append(val)
    return w, a


# ---------------- aggregates
def sales_agg(f, t, vid=None, cid=None):
    w, a = " WHERE s.date BETWEEN ? AND ?", [f, t]
    w, a = _f("s.visitor_id", vid, w, a)
    w, a = _f("s.customer_id", cid, w, a)
    r = q(f"""SELECT COALESCE(SUM(l.qty*l.price),0) gross, COALESCE(SUM(l.qty),0) qty, COALESCE(SUM(l.qty*l.cost),0) cogs,
              COUNT(DISTINCT s.id) inv {SL} {w}""", a, one=True)
    disc = v1(f"SELECT SUM(s.discount) FROM sales_invoices s {w}", a)
    cw, ca = " WHERE c.status='settled' AND c.settle_date BETWEEN ? AND ?", [f, t]
    cw, ca = _f("c.visitor_id", vid, cw, ca)
    rr = q(f"SELECT COALESCE(SUM(l.returned),0) ret, COALESCE(SUM(l.returned*l.price),0) ret_val FROM cons_lines l JOIN consignments c ON c.id=l.cons_id {cw}",
           ca, one=True) if not cid else {"ret": 0, "ret_val": 0}
    pw, pa = " WHERE p.date BETWEEN ? AND ?", [f, t]
    pw, pa = _f("s.visitor_id", vid, pw, pa)
    pw, pa = _f("s.customer_id", cid, pw, pa)
    p = q(f"""SELECT COALESCE(SUM(CASE WHEN method='pos' THEN amount END),0) pos, COALESCE(SUM(CASE WHEN method='cash' THEN amount END),0) cash
              FROM payments p JOIN sales_invoices s ON s.id=p.inv_id {pw}""", pa, one=True)
    amount = r["gross"] - disc
    return {"gross": r["gross"], "disc": disc, "amount": amount, "qty": r["qty"], "cogs": r["cogs"], "inv": r["inv"],
            "ret": rr["ret"], "ret_val": rr["ret_val"], "pos": p["pos"], "cash": p["cash"], "coll": p["pos"] + p["cash"],
            "profit": amount - r["cogs"]}


def daily_series(f, t):
    rows = {r["d"]: r for r in q(f"SELECT s.date d, SUM(l.qty*l.price) g, SUM(l.qty) qty, COUNT(DISTINCT s.id) inv {SL} WHERE s.date BETWEEN ? AND ? GROUP BY s.date", (f, t))}
    disc = {r["d"]: r["x"] for r in q("SELECT date d, SUM(discount) x FROM sales_invoices WHERE date BETWEEN ? AND ? GROUP BY date", (f, t))}
    rets = {r["d"]: r["x"] for r in q("""SELECT c.settle_date d, SUM(l.returned) x FROM cons_lines l JOIN consignments c ON c.id=l.cons_id
                                          WHERE c.status='settled' AND c.settle_date BETWEEN ? AND ? GROUP BY c.settle_date""", (f, t))}
    pays = {r["d"]: r["s"] for r in q("SELECT date d, SUM(amount) s FROM payments WHERE date BETWEEN ? AND ? GROUP BY date", (f, t))}
    out, d = [], f
    while d <= t:
        r = rows.get(d)
        out.append({"d": d, "amount": (r["g"] if r else 0) - disc.get(d, 0), "qty": r["qty"] if r else 0, "ret": rets.get(d, 0),
                    "inv": r["inv"] if r else 0, "coll": pays.get(d, 0)})
        d = add_days(d, 1)
    return out


def receivable_until(t, pid=None, as_="customer", before=False):
    op = "<" if before else "<="
    w, a = "", []
    if pid:
        w = f" AND s.{'visitor_id' if as_ == 'visitor' else 'customer_id'}=?"; a = [pid]
    inv = v1(f"SELECT SUM(l.qty*l.price) {SL} WHERE s.date {op} ?{w}", [t] + a) - v1(f"SELECT SUM(s.discount) FROM sales_invoices s WHERE s.date {op} ?{w}", [t] + a)
    pay = v1(f"SELECT SUM(p.amount) FROM payments p JOIN sales_invoices s ON s.id=p.inv_id WHERE p.date {op} ?{w}", [t] + a)
    return inv - pay


def payable_until(t, pid=None, before=False):
    op = "<" if before else "<="
    w, a = ("", []) if not pid else (" AND u.supplier_id=?", [pid])
    return v1(f"""SELECT SUM(CASE WHEN u.kind='return' THEN -1 ELSE 1 END*((SELECT COALESCE(SUM(qty*price),0) FROM purchase_lines WHERE pur_id=u.id)-u.discount))
                  FROM purchases u WHERE u.date {op} ?{w}""", [t] + a)


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
    for r in q(INV_SQL + " ORDER BY s.date, s.no"):
        rem = r["total"] - r["pos"] - r["cash"]
        if rem > 0:
            out.append({"id": r["id"], "no": r["no"], "date": r["date"], "c": r["cname"], "v": r["vname"] or "", "vcode": r["vcode"],
                        "ccode": r["ccode"], "total": r["total"], "paid": r["pos"] + r["cash"], "rem": rem, "age": diff_days(r["date"], t)})
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
        ctx["k"]["stock_val"] = sum(r["st"] * r["cost"] for r in st)
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
    rows = q(f"""SELECT s.id sid, s.no, s.date, cu.name cname, v.name vname, i.code, i.name, i.pack, i.unit, i.pack_unit, l.qty, l.price, l.qty*l.price amount
                 {SL} JOIN persons cu ON cu.id=s.customer_id LEFT JOIN persons v ON v.id=s.visitor_id JOIN items i ON i.id=l.item_id
                 {w} ORDER BY s.date, s.no, i.code""", a)
    data = [[link(r["no"], url_for("invoice", iid=r["sid"])), r["date"], r["cname"], r["vname"] or "", r["code"], r["name"], r["qty"], pkr(r["qty"], r),
             r["price"], r["amount"]] for r in rows]
    disc = v1(f"SELECT SUM(s.discount) FROM sales_invoices s {w}", a)
    sub = f"از {f} تا {t}" + (f" | ویزیتور: {_pname(vid)}" if vid else "") + (f" | مشتری: {_pname(cid)}" if cid else "") + (f" | انبار: {_whname(wid)}" if wid else "")
    spec = {"title": "گزارش ریز فروش", "sub": sub, "filters": ["period", "visitor", "customer", "wh"],
            "kpis": [("جمع ناخالص", sum(r["amount"] for r in rows), "money"), ("تخفیف", disc, "money"), ("فروش خالص", sum(r["amount"] for r in rows) - disc, "money")],
            "sections": [{"cols": [("فاکتور", "text"), ("تاریخ", "text"), ("مشتری", "text"), ("ویزیتور", "text"), ("کد", "text"), ("کالا", "text"),
                                   ("تعداد", "num"), ("تعداد (کارتن)", "text"), ("نرخ", "money"), ("مبلغ (ریال)", "money")],
                          "rows": data, "total": ["جمع", "", "", "", "", "", sum(r["qty"] for r in rows), "", "", sum(r["amount"] for r in rows)]}]}
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
                     s["pos"], s["cash"], receivable_until(t, v["id"], "visitor"), open_qty])
    rows.sort(key=lambda r: -r[8])
    tot = ["جمع", ""] + [sum(r[i] for r in rows) for i in (2, 3)] + [""] + [sum(r[i] for r in rows) for i in range(5, 13)]
    spec = {"title": "عملکرد ویزیتورها", "sub": f"از {f} تا {t}", "filters": ["period"],
            "charts": [{"id": "cv", "type": "bar", "horizontal": True, "title": "فروش هر ویزیتور (ریال)", "labels": [export.cell_v(r[1]) for r in rows],
                        "sets": [("فروش", [r[8] for r in rows])]}],
            "sections": [{"cols": [("کد", "text"), ("ویزیتور", "text"), ("تحویل امانی", "num"), ("مرجوعی", "num"), ("% مرجوعی", "text"),
                                   ("فروش (تعداد)", "num"), ("فاکتور", "num"), ("مشتری", "num"), ("فروش (ریال)", "money"), ("پوز", "money"), ("نقد", "money"),
                                   ("مانده مطالبات مشتریانش", "money"), ("امانی باز", "num")], "rows": rows, "total": tot}]}
    return export.out(spec)


@rep("/reports/customers", "rep_customers", "rep_sales")
def rep_customers():
    f, t = period()
    vid = to_int(request.args.get("visitor"))
    w, a = " WHERE s.date BETWEEN ? AND ?", [f, t]
    w, a = _f("s.visitor_id", vid, w, a)
    rows = q(f"""SELECT cu.id, cu.code, cu.name, v.name vname, COUNT(DISTINCT s.id) inv, SUM(l.qty) qty, SUM(l.qty*l.price) gross
                 {SL} JOIN persons cu ON cu.id=s.customer_id LEFT JOIN persons v ON v.id=cu.visitor_id {w}
                 GROUP BY cu.id, cu.code, cu.name, v.name ORDER BY gross DESC""", a)
    data = []
    for r in rows:
        disc = v1("SELECT SUM(discount) FROM sales_invoices s WHERE s.customer_id=? AND s.date BETWEEN ? AND ?" + (" AND s.visitor_id=?" if vid else ""),
                  [r["id"], f, t] + ([vid] if vid else []))
        data.append([r["code"], link(r["name"], url_for("fin_statement", person=r["id"], **{"as": "customer"})), r["vname"] or "", r["inv"], r["qty"],
                     r["gross"] - disc, receivable_until(t, r["id"])])
    spec = {"title": "فروش به تفکیک مشتری", "sub": f"از {f} تا {t}" + (f" | ویزیتور: {_pname(vid)}" if vid else ""), "filters": ["period", "visitor"],
            "sections": [{"cols": [("کد", "text"), ("مشتری", "text"), ("ویزیتور مسئول", "text"), ("فاکتور", "num"), ("تعداد", "num"), ("فروش خالص", "money"),
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
        vals = [s, r["open_amani"], s + r["open_amani"], s * r["cost"], r["open_amani"] * r["cost"], s * r["price"]]
        T = [a + b for a, b in zip(T, vals)]
        rows.append([r["code"], link(r["name"], url_for("rep_kardex", item=r["id"], wh=wid or "")), r["group_name"] or "", s, pkr(s, r), r["open_amani"],
                     pkr(r["open_amani"], r), s + r["open_amani"], r["cost"], r["price"], vals[3], vals[4], vals[5]])
    spec = {"title": "موجودی و ارزش انبار", "sub": f"تاریخ {today_j()}" + (f" | انبار: {_whname(wid)}" if wid else " | همه انبارها"), "filters": ["wh"],
            "kpis": [("موجودی انبار (واحد)", T[0], "num"), ("امانی نزد ویزیتورها (واحد)", T[1], "num"),
                     ("ارزش انبار به بهای خرید", T[3], "money"), ("ارزش امانی به بهای خرید", T[4], "money"), ("ارزش انبار به نرخ فروش", T[5], "money")],
            "sections": [{"cols": [("کد", "text"), ("کالا", "text"), ("گروه", "text"), ("موجودی انبار", "num"), ("انبار (کارتن)", "text"),
                                   ("امانی نزد ویزیتور", "num"), ("امانی (کارتن)", "text"), ("جمع موجودی", "num"), ("نرخ خرید واحد", "money"),
                                   ("نرخ فروش واحد", "money"), ("ارزش انبار (بهای خرید)", "money"), ("ارزش امانی (بهای خرید)", "money"), ("ارزش انبار (نرخ فروش)", "money")],
                          "rows": rows, "total": ["جمع", "", "", T[0], "", T[1], "", T[2], "", "", T[3], T[4], T[5]]}]}
    return export.out(spec, whs=warehouses_list(False))


def _doc_link(m):
    t, d, no = m["doc_type"], m["doc_id"], m["doc_no"]
    url = {"RES": ("receipt_view", "rid"), "PUR": ("purchase_view", "pid"), "TRF": ("transfer_view", "tid"), "AMN": ("amani_view", "cid"),
           "MRJ": ("print_return", "cid"), "FCT": ("invoice", "iid")}.get(t)
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


@rep("/reports/items", "rep_items", "rep_stock")
def rep_items():
    f, t = period()
    wid = to_int(request.args.get("wh")) or None
    st = {r["id"]: r for r in stock_rows(wh_id=wid)}
    wf = " AND m.wh_id=?" if wid else ""
    mv = {}
    for r in q(f"""SELECT m.item_id, m.doc_type, SUM(m.qty) s FROM stock_moves m WHERE m.date BETWEEN ? AND ?{wf} GROUP BY m.item_id, m.doc_type""",
               [f, t] + ([wid] if wid else [])):
        mv.setdefault(r["item_id"], {})[r["doc_type"]] = r["s"]
    sales = {r["item_id"]: r for r in q(f"""SELECT l.item_id, SUM(l.qty) qty, SUM(l.qty*l.price) amt {SL} WHERE s.date BETWEEN ? AND ?
                                           {' AND s.wh_id=?' if wid else ''} GROUP BY l.item_id""", [f, t] + ([wid] if wid else []))}
    data = []
    for i in _items():
        m, sv, s_ = mv.get(i["id"], {}), sales.get(i["id"]), st[i["id"]]
        data.append([i["code"], link(i["name"], url_for("rep_kardex", item=i["id"], wh=wid or "", **{"from": f, "to": t})),
                     m.get("RES", 0), m.get("PUR", 0), m.get("TRF", 0), -m.get("AMN", 0), m.get("MRJ", 0), -m.get("FCT", 0),
                     sv["qty"] if sv else 0, sv["amt"] if sv else 0, s_["st"], s_["open_amani"]])
    data = [d for d in data if any(d[k] for k in range(2, 12))]
    spec = {"title": "گردش کالا به کالا", "sub": f"از {f} تا {t}" + (f" | انبار: {_whname(wid)}" if wid else ""), "filters": ["period", "wh"],
            "sections": [{"cols": [("کد", "text"), ("کالا", "text"), ("رسید", "num"), ("خرید خالص", "num"), ("حواله (±)", "num"), ("تحویل امانی", "num"),
                                   ("مرجوعی", "num"), ("فروش مستقیم", "num"), ("کل فروش (تعداد)", "num"), ("کل فروش (ریال)", "money"),
                                   ("موجودی فعلی", "num"), ("امانی باز", "num")],
                          "rows": data, "total": ["جمع", ""] + [sum(r[i] for r in data) for i in range(2, 12)]}]}
    return export.out(spec, whs=warehouses_list(False))


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
    rows = q(f"""SELECT u.id, u.no, u.kind, u.date, s.name sname, i.code, i.name, i.pack, i.unit, i.pack_unit, l.qty, l.price, l.qty*l.price amount
                 FROM purchase_lines l JOIN purchases u ON u.id=l.pur_id JOIN persons s ON s.id=u.supplier_id JOIN items i ON i.id=l.item_id
                 {w} ORDER BY u.date, u.no, i.code""", a)
    sg = lambda r: -1 if r["kind"] == "return" else 1
    data = [[link(r["no"], url_for("purchase_view", pid=r["id"])), "برگشت" if r["kind"] == "return" else "خرید", r["date"], r["sname"], r["code"], r["name"],
             sg(r) * r["qty"], pkr(r["qty"], r), r["price"], sg(r) * r["amount"]] for r in rows]
    disc = v1(f"SELECT SUM(CASE WHEN u.kind='return' THEN -u.discount ELSE u.discount END) FROM purchases u {w}", a)
    spec = {"title": "گزارش خرید", "sub": f"از {f} تا {t}" + (f" | تامین‌کننده: {_pname(sid)}" if sid else ""), "filters": ["period", "supplier"],
            "kpis": [("جمع خالص اقلام", sum(d[9] for d in data), "money"), ("تخفیف", disc, "money"), ("خرید خالص", sum(d[9] for d in data) - disc, "money")],
            "sections": [{"cols": [("شماره", "text"), ("نوع", "text"), ("تاریخ", "text"), ("تامین‌کننده", "text"), ("کد", "text"), ("کالا", "text"),
                                   ("تعداد", "num"), ("تعداد (کارتن)", "text"), ("نرخ", "money"), ("مبلغ", "money")],
                          "rows": data, "total": ["جمع", "", "", "", "", "", sum(d[6] for d in data), "", "", sum(d[9] for d in data)]}]}
    return export.out(spec, suppliers=q("SELECT id, code, name FROM persons WHERE is_supplier=1 ORDER BY code"))


# ================= FINANCIAL =================
@rep("/reports/fin/summary", "fin_summary", "rep_fin")
def fin_summary():
    f, t = period()
    s = sales_agg(f, t)
    st = stock_rows()
    inv_cost = sum(r["st"] * r["cost"] for r in st)
    amani_cost = sum(r["open_amani"] * r["cost"] for r in st)
    r0, r1 = receivable_until(f, before=True), receivable_until(t)
    p0, p1 = payable_until(f, before=True), payable_until(t)
    purchases = payable_until(t) - payable_until(f, before=True)
    margin = round(s["profit"] * 100.0 / s["amount"], 1) if s["amount"] else 0
    rows1 = [["فروش ناخالص", s["gross"]], ["کسر می‌شود: تخفیفات فروش", -s["disc"]], ["فروش خالص", s["amount"]],
             ["کسر می‌شود: بهای تمام‌شده کالای فروش‌رفته", -s["cogs"]], ["سود ناخالص", s["profit"]], [f"حاشیه سود ناخالص: {margin}%", None],
             ["(اطلاعات) ارزش کالای مرجوعی امانی در دوره", s["ret_val"]]]
    rows2 = [["مانده مطالبات از مشتریان ابتدای دوره", r0], ["اضافه می‌شود: فروش خالص دوره", s["amount"]],
             ["کسر می‌شود: وصولی کارت‌خوان (پوز)", -s["pos"]], ["کسر می‌شود: وصولی نقد", -s["cash"]], ["مانده مطالبات پایان دوره", r1]]
    rows3 = [["مانده بدهی به تامین‌کنندگان ابتدای دوره", p0], ["اضافه می‌شود: خرید خالص دوره", purchases],
             ["کسر می‌شود: پرداخت‌ها (فاز ۲ — خزانه)", 0], ["مانده بدهی پایان دوره", p1]]
    rows4 = [["موجودی کالای انبارها (بهای خرید)", inv_cost], ["کالای امانی نزد ویزیتورها (بهای خرید)", amani_cost], ["جمع موجودی کالا", inv_cost + amani_cost]]
    spec = {"title": "خلاصه وضعیت مالی", "sub": f"از {f} تا {t}", "filters": ["period"],
            "kpis": [("فروش خالص", s["amount"], "money"), ("سود ناخالص", s["profit"], "money"), ("وصولی دوره", s["coll"], "money"),
                     ("مانده مطالبات", r1, "money"), ("بدهی به تامین‌کنندگان", p1, "money")],
            "sections": [{"title": "صورت سود ناخالص", "cols": [("شرح", "text"), ("مبلغ (ریال)", "money")], "rows": rows1},
                         {"title": "حساب‌های دریافتنی (مشتریان)", "cols": [("شرح", "text"), ("مبلغ (ریال)", "money")], "rows": rows2},
                         {"title": "حساب‌های پرداختنی (تامین‌کنندگان)", "cols": [("شرح", "text"), ("مبلغ (ریال)", "money")], "rows": rows3},
                         {"title": "موجودی کالا", "cols": [("شرح", "text"), ("مبلغ (ریال)", "money")], "rows": rows4}],
            "note": "بهای تمام‌شده = تعداد فروش × نرخ خرید کالا در تاریخ فاکتور (ثبت‌شده روی فاکتور). ارزش موجودی به نرخ خرید فعلی."}
    return export.out(spec)


@rep("/reports/fin/trial", "fin_trial", "rep_fin")
def fin_trial():
    f, t = period()
    cust_rows, sup_rows = [], []
    for p in q("SELECT id, code, name FROM persons WHERE is_customer=1 ORDER BY code"):
        op = receivable_until(f, p["id"], before=True)
        dr = receivable_until(t, p["id"]) - op + v1("SELECT SUM(p.amount) FROM payments p JOIN sales_invoices s ON s.id=p.inv_id WHERE s.customer_id=? AND p.date BETWEEN ? AND ?", (p["id"], f, t))
        cr = v1("SELECT SUM(p.amount) FROM payments p JOIN sales_invoices s ON s.id=p.inv_id WHERE s.customer_id=? AND p.date BETWEEN ? AND ?", (p["id"], f, t))
        cl = op + dr - cr
        if op or dr or cr:
            cust_rows.append([p["code"], link(p["name"], url_for("fin_statement", person=p["id"], **{"as": "customer", "from": f, "to": t})), op, dr, cr,
                              cl if cl > 0 else 0, -cl if cl < 0 else 0, "بدهکار" if cl > 0 else ("بستانکار" if cl < 0 else "—")])
    for p in q("SELECT id, code, name FROM persons WHERE is_supplier=1 ORDER BY code"):
        op = payable_until(f, p["id"], before=True)
        mv = payable_until(t, p["id"]) - op
        cl = op + mv
        if op or mv:
            sup_rows.append([p["code"], link(p["name"], url_for("fin_statement", person=p["id"], **{"as": "supplier", "from": f, "to": t})), op,
                             -mv if mv < 0 else 0, mv if mv > 0 else 0, -cl if cl < 0 else 0, cl if cl > 0 else 0,
                             "بستانکار" if cl > 0 else ("بدهکار" if cl < 0 else "—")])
    cols = [("کد", "text"), ("نام", "text"), ("مانده ابتدای دوره", "money"), ("گردش بدهکار", "money"), ("گردش بستانکار", "money"),
            ("مانده بدهکار", "money"), ("مانده بستانکار", "money"), ("تشخیص", "text")]
    tot = lambda R: ["جمع", ""] + [sum(r[i] for r in R) for i in range(2, 7)] + [""]
    spec = {"title": "تراز اشخاص", "sub": f"از {f} تا {t}", "filters": ["period"],
            "sections": [{"title": "مشتریان (حساب‌های دریافتنی)", "cols": cols, "rows": cust_rows, "total": tot(cust_rows)},
                         {"title": "تامین‌کنندگان (حساب‌های پرداختنی)", "cols": cols, "rows": sup_rows, "total": tot(sup_rows)}]}
    return export.out(spec)


@rep("/reports/fin/statement", "fin_statement", "rep_fin")
def fin_statement():
    """صورت‌حساب: as=customer (فاکتورهای مشتری) | visitor (همه فاکتورهای صادره ویزیتور برای همه مشتریانش) | supplier"""
    f, t = period(default_from=year_start(today_j()))
    as_ = request.args.get("as") if request.args.get("as") in ("customer", "visitor", "supplier") else "customer"
    flag = {"customer": "is_customer", "visitor": "is_visitor", "supplier": "is_supplier"}[as_]
    ps = q(f"SELECT id, code, name FROM persons WHERE {flag}=1 ORDER BY code")
    pid = to_int(request.args.get("person")) or (ps[0]["id"] if ps else 0)
    ent = []
    if as_ == "supplier":
        bal = payable_until(f, pid, before=True)
        for u in q("""SELECT u.*, (SELECT COALESCE(SUM(qty*price),0) FROM purchase_lines WHERE pur_id=u.id)-u.discount total FROM purchases u
                      WHERE u.supplier_id=? AND u.date BETWEEN ? AND ? ORDER BY u.date, u.no""", (pid, f, t)):
            ret = u["kind"] == "return"
            ent.append((u["date"], link(f"{'برگشت از خرید' if ret else 'فاکتور خرید'} {u['no']}", url_for("purchase_view", pid=u["id"])),
                        u["ref_no"] or "", "", u["total"] if ret else 0, 0 if ret else u["total"]))
        rows = [["", "مانده از قبل", "", "", "", "", bal, "بس" if bal > 0 else ("بد" if bal < 0 else "—")]]
        dr = cr = 0
        for e in ent:
            bal += e[5] - e[4]; dr += e[4]; cr += e[5]
            rows.append([e[0], e[1], e[2], e[3], e[4] or "", e[5] or "", bal, "بس" if bal > 0 else ("بد" if bal < 0 else "—")])
        rows.append(["", "پرداخت‌ها به تامین‌کننده در فاز ۲ (خزانه) اضافه می‌شود", "", "", "", "", "", ""])
    else:
        col = "visitor_id" if as_ == "visitor" else "customer_id"
        bal = receivable_until(f, pid, as_, before=True)
        for r in q(INV_SQL + f" WHERE s.{col}=? AND s.date BETWEEN ? AND ? ORDER BY s.date, s.no", (pid, f, t)):
            ent.append((r["date"], 0, link(f"فاکتور فروش {r['no']}", url_for("invoice", iid=r["id"])),
                        r["cname"] if as_ == "visitor" else (r["vname"] or ""), f"{r['sold_qty']:,} واحد" + (f" / امانی {r['cons_no']}" if r["cons_no"] else ""),
                        r["total"], 0))
        for p in q(f"""SELECT p.*, s.no ino, s.id sid, cu.name cname FROM payments p JOIN sales_invoices s ON s.id=p.inv_id JOIN persons cu ON cu.id=s.customer_id
                       WHERE s.{col}=? AND p.date BETWEEN ? AND ?""", (pid, f, t)):
            ent.append((p["date"], 1, link(f"سند دریافت {p['no']}", url_for("invoice", iid=p["sid"])), p["cname"] if as_ == "visitor" else "",
                        f"{METHOD.get(p['method'], p['method'])} - فاکتور {p['ino']} {p['ref'] or ''}", 0, p["amount"]))
        ent.sort(key=lambda e: (e[0], e[1]))
        rows = [["", "مانده از قبل", "", "", "", "", bal, "بد" if bal > 0 else ("بس" if bal < 0 else "—")]]
        dr = cr = 0
        for e in ent:
            bal += e[5] - e[6]; dr += e[5]; cr += e[6]
            rows.append([e[0], e[2], e[3], e[4], e[5] or "", e[6] or "", bal, "بد" if bal > 0 else ("بس" if bal < 0 else "—")])
    title = {"customer": "صورت‌حساب مشتری", "visitor": "صورت‌حساب ویزیتور (همه مشتریان)", "supplier": "صورت‌حساب تامین‌کننده"}[as_]
    c3 = {"customer": "ویزیتور", "visitor": "مشتری", "supplier": "ش. فاکتور تامین‌کننده"}[as_]
    spec = {"title": f"{title}: {_pname(pid) or ''}", "sub": f"از {f} تا {t}", "filters": ["period", "person"],
            "sections": [{"cols": [("تاریخ", "text"), ("سند", "text"), (c3, "text"), ("شرح", "text"), ("بدهکار", "money"), ("بستانکار", "money"),
                                   ("مانده", "money"), ("تشخیص", "text")],
                          "rows": rows, "total": ["جمع دوره", "", "", "", dr, cr, bal, ""]}]}
    return export.out(spec, persons=ps, person_id=pid, as_=as_)


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
  FROM payments p JOIN sales_invoices s ON s.id=p.inv_id JOIN persons cu ON cu.id=s.customer_id LEFT JOIN persons v ON v.id=s.visitor_id
  LEFT JOIN users u ON u.id=p.created_by"""


@rep("/reports/fin/cashbook", "fin_cashbook", "rep_fin")
def fin_cashbook():
    f, t = period()
    method = request.args.get("method", "")
    days = q("""SELECT date d, SUM(CASE WHEN method='pos' THEN amount ELSE 0 END) pos, SUM(CASE WHEN method='cash' THEN amount ELSE 0 END) cash,
                COUNT(*) n FROM payments WHERE date BETWEEN ? AND ? GROUP BY date ORDER BY date""", (f, t))
    det = q(PAY_DET + " WHERE p.date BETWEEN ? AND ? ORDER BY p.date, p.no", (f, t))
    if method in METHOD:
        det = [d for d in det if d["method"] == method]
    spec = {"title": "دفتر دریافت روزانه (صندوق و کارت‌خوان)", "sub": f"از {f} تا {t}", "filters": ["period", "method"],
            "kpis": [("دریافت پوز", sum(d["pos"] for d in days), "money"), ("دریافت نقد", sum(d["cash"] for d in days), "money"),
                     ("جمع دریافت", sum(d["pos"] + d["cash"] for d in days), "money")],
            "sections": [{"title": "خلاصه روزانه", "cols": [("تاریخ", "text"), ("روز", "text"), ("تعداد سند", "num"), ("پوز", "money"), ("نقد", "money"), ("جمع", "money")],
                          "rows": [[d["d"], WEEKDAYS[weekday(d["d"])], d["n"], d["pos"], d["cash"], d["pos"] + d["cash"]] for d in days],
                          "total": ["جمع", "", sum(d["n"] for d in days), sum(d["pos"] for d in days), sum(d["cash"] for d in days), sum(d["pos"] + d["cash"] for d in days)]},
                         {"title": "ریز اسناد دریافت", "cols": [("سند", "text"), ("تاریخ", "text"), ("مشتری", "text"), ("ویزیتور", "text"), ("فاکتور", "text"),
                                                               ("نوع", "text"), ("پیگیری", "text"), ("کاربر", "text"), ("مبلغ", "money")],
                          "rows": [[d["no"], d["date"], d["cname"], d["vname"] or "", link(d["ino"], url_for("invoice", iid=d["sid"])), METHOD[d["method"]],
                                    d["ref"] or "", d["un"] or "", d["amount"]] for d in det],
                          "total": ["جمع", "", "", "", "", "", "", "", sum(d["amount"] for d in det)]}]}
    return export.out(spec)


@rep("/reports/fin/daily", "fin_daily", "rep_fin")
def fin_daily():
    f, t = period(default_from=today_j())
    vid = to_int(request.args.get("visitor"))
    w, a = "", [f, t]
    w, a = _f("s.visitor_id", vid, w, a)
    rows = q(f"""SELECT p.date d, v.id vid, COALESCE(v.code,'-') code, COALESCE(v.name,'بدون ویزیتور') name, COUNT(*) n,
                 SUM(CASE WHEN p.method='pos' THEN p.amount ELSE 0 END) pos, SUM(CASE WHEN p.method='cash' THEN p.amount ELSE 0 END) cash
                 FROM payments p JOIN sales_invoices s ON s.id=p.inv_id LEFT JOIN persons v ON v.id=s.visitor_id
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
            "kpis": [("دریافت پوز", TP, "money"), ("دریافت نقد", TC, "money"), ("جمع دریافت", TP + TC, "money")],
            "charts": [{"id": "cd", "type": "bar", "horizontal": True, "title": "دریافتی هر ویزیتور (ریال)", "labels": [r[1] for r in summ],
                        "sets": [("پوز", [r[3] for r in summ]), ("نقد", [r[4] for r in summ])]}] if summ else [],
            "sections": [{"title": "به تفکیک ویزیتور", "cols": [("کد", "text"), ("ویزیتور", "text"), ("تعداد سند", "num"), ("پوز", "money"), ("نقد", "money"), ("جمع", "money")],
                          "rows": summ, "total": ["جمع", "", sum(r[2] for r in summ), TP, TC, TP + TC]},
                         {"title": "روز به روز", "cols": [("تاریخ", "text"), ("ویزیتور", "text"), ("تعداد سند", "num"), ("پوز", "money"), ("نقد", "money"), ("جمع", "money")],
                          "rows": data, "total": ["جمع", "", sum(r["n"] for r in rows), TP, TC, TP + TC]}]}
    return export.out(spec, visitors=_visitors())


@rep("/reports/fin/profit", "fin_profit", "rep_fin")
def fin_profit():
    f, t = period()

    def grp(key_sql, name_sql, join=""):
        return q(f"""SELECT {key_sql} k, {name_sql} n, SUM(l.qty) qty, SUM(l.qty*l.price) sales, SUM(l.qty*l.cost) cogs
                     {SL} {join} WHERE s.date BETWEEN ? AND ? GROUP BY 1, 2 ORDER BY sales DESC""", (f, t))
    by_item = grp("i.code", "i.name", "JOIN items i ON i.id=l.item_id")
    by_v = grp("COALESCE(v.code,'-')", "COALESCE(v.name,'بدون ویزیتور')", "LEFT JOIN persons v ON v.id=s.visitor_id")
    by_c = grp("c.code", "c.name", "JOIN persons c ON c.id=s.customer_id")
    disc = v1("SELECT SUM(discount) FROM sales_invoices WHERE date BETWEEN ? AND ?", (f, t))

    def mg(s, c):
        return f"{round((s - c) * 100.0 / s, 1)}%" if s else "—"
    S, C = sum(r["sales"] for r in by_item), sum(r["cogs"] for r in by_item)

    def sec(title, lab, R):
        return {"title": title, "cols": [("کد", "text"), (lab, "text"), ("تعداد فروش", "num"), ("فروش", "money"), ("بهای تمام‌شده", "money"),
                                        ("سود ناخالص", "money"), ("حاشیه سود", "text")],
                "rows": [[r["k"], r["n"], r["qty"], r["sales"], r["cogs"], r["sales"] - r["cogs"], mg(r["sales"], r["cogs"])] for r in R],
                "total": ["جمع", "", sum(r["qty"] for r in R), S, C, S - C, mg(S, C)]}
    spec = {"title": "سود ناخالص فروش", "sub": f"از {f} تا {t}", "filters": ["period"],
            "kpis": [("فروش (قبل از تخفیف)", S, "money"), ("تخفیفات", disc, "money"), ("بهای تمام‌شده", C, "money"), ("سود ناخالص (پس از تخفیف)", S - disc - C, "money")],
            "sections": [sec("به تفکیک کالا", "کالا", by_item), sec("به تفکیک ویزیتور", "ویزیتور", by_v), sec("به تفکیک مشتری", "مشتری", by_c)],
            "note": "سود ناخالص = مبلغ فروش − (تعداد فروش × نرخ خرید کالا در تاریخ فاکتور). تخفیف کل فاکتورها در جمع نهایی کسر شده است."}
    return export.out(spec)


@rep("/reports/fin/payments", "fin_payments", "rep_fin")
def fin_payments():
    f, t = period()
    vid, cid = to_int(request.args.get("visitor")), to_int(request.args.get("customer"))
    method = request.args.get("method", "")
    w, a = " WHERE p.date BETWEEN ? AND ?", [f, t]
    w, a = _f("s.visitor_id", vid, w, a)
    w, a = _f("s.customer_id", cid, w, a)
    if method in METHOD:
        w += " AND p.method=?"; a.append(method)
    rows = q(PAY_DET + w + " ORDER BY p.date, p.no", a)
    spec = {"title": "گزارش دریافتی‌ها", "sub": f"از {f} تا {t}", "filters": ["period", "visitor", "customer", "method"],
            "sections": [{"cols": [("سند", "text"), ("تاریخ", "text"), ("مشتری", "text"), ("ویزیتور", "text"), ("فاکتور", "text"), ("نوع", "text"),
                                   ("پیگیری", "text"), ("مبلغ", "money")],
                          "rows": [[p["no"], p["date"], p["cname"], p["vname"] or "", link(p["ino"], url_for("invoice", iid=p["sid"])), METHOD[p["method"]],
                                    p["ref"] or "", p["amount"]] for p in rows],
                          "total": ["جمع", "", "", "", "", "", "", sum(p["amount"] for p in rows)]}]}
    return export.out(spec, visitors=_visitors(), customers=_customers())
