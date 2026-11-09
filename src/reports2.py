"""More reports: due-date statements (customers / visitors / suppliers), cash-flow forecast, debtors & credit limits,
monthly sales, idle stock, VAT (seasonal)."""
from flask import request, url_for
from core import q, v1, to_int, period, stock_rows
from jalali import today_j, add_days, diff_days, norm_date, year_start, weekday, WEEKDAYS, MONTHS
from reports import rep, link, _visitors, _customers, _pname
import export
import gl

BUCKETS = [(31, 10 ** 6, "سررسید گذشته بیش از ۳۰ روز"), (1, 30, "سررسید گذشته ۱ تا ۳۰ روز"), (-7, 0, "امروز تا ۷ روز آینده"),
           (-30, -8, "۸ تا ۳۰ روز آینده"), (-10 ** 6, -31, "بیش از ۳۰ روز آینده")]


def bucket(overdue):
    return next(i for i, b in enumerate(BUCKETS) if b[0] <= overdue <= b[1])


def open_items(side="recv", t=None, pid=None):
    """open (unpaid) documents per person by FIFO: the person's ledger balance is assigned to the newest documents.
    side recv: customers (receivable, sales invoices) | pay: suppliers (payable, purchase invoices).
    -> list of dicts: person, pname, pcode, visitor_id, vname, doc_id, no, date, due, total, open, overdue"""
    t = t or today_j()
    key = "receivable" if side == "recv" else "payable"
    sg = 1 if side == "recv" else -1
    w, a = "", [gl.acc(key), t]
    if pid:
        w, a = " AND vl.person_id=?", a + [pid]
    bals = q(f"""SELECT vl.person_id p, SUM(vl.debit-vl.credit)*{sg} b FROM voucher_lines vl JOIN vouchers v ON v.id=vl.voucher_id
                 WHERE vl.account_id=? AND v.date<=? AND vl.person_id IS NOT NULL{w} GROUP BY vl.person_id HAVING SUM(vl.debit-vl.credit)*{sg} > 0""", a)
    if not bals:
        return []
    ids = [b["p"] for b in bals]
    P = {p["id"]: p for p in q("""SELECT p.id, p.code, p.name, p.credit_days, p.visitor_id, v.name vname FROM persons p
                                   LEFT JOIN persons v ON v.id=p.visitor_id WHERE p.id = ANY(?)""", (ids,))}
    if side == "recv":
        docs = q("""SELECT s.id, s.no, s.date, s.due_date, s.total, s.customer_id p, s.visitor_id, v.name vname FROM v_si s
                    LEFT JOIN persons v ON v.id=s.visitor_id WHERE s.kind<>'return' AND s.date<=? AND s.customer_id = ANY(?)""", (t, ids))
    else:
        docs = q("""SELECT u.id, u.no, u.date, u.due_date, u.total, u.supplier_id p, NULL visitor_id, NULL vname FROM v_pu u
                    WHERE u.kind<>'return' AND u.date<=? AND u.supplier_id = ANY(?)""", (t, ids))
    by = {}
    for d in docs:
        p = P[d["p"]]
        due = d["due_date"] or (add_days(d["date"], p["credit_days"]) if p["credit_days"] else d["date"])
        by.setdefault(d["p"], []).append(dict(d, due=due))
    out = []
    for b in bals:
        p, left = P[b["p"]], b["b"]
        for d in sorted(by.get(b["p"], []), key=lambda d: (d["due"], d["date"], d["no"]), reverse=True):
            if left <= 0:
                break
            o = min(left, d["total"])
            left -= o
            out.append({"person": p["id"], "pcode": p["code"], "pname": p["name"], "visitor_id": d["visitor_id"] or p["visitor_id"],
                        "vname": d["vname"] or p["vname"] or "", "doc_id": d["id"], "no": d["no"], "date": d["date"], "due": d["due"],
                        "total": d["total"], "open": o, "overdue": diff_days(d["due"], t)})
        if left > 0:   # opening balance, bounced cheques, manual entries... with no invoice: due now
            out.append({"person": p["id"], "pcode": p["code"], "pname": p["name"], "visitor_id": p["visitor_id"], "vname": p["vname"] or "",
                        "doc_id": None, "no": None, "date": "", "due": t, "total": left, "open": left, "overdue": 0})
    return sorted(out, key=lambda r: (r["due"], r["pcode"]))


def _doc_link(side, r):
    if not r["doc_id"]:
        return {"v": "مانده بدون فاکتور (مانده قبلی / چک برگشتی / سایر)"}
    return link(f"{'فاکتور فروش' if side == 'recv' else 'فاکتور خرید'} {r['no']}",
                url_for("invoice", iid=r["doc_id"]) if side == "recv" else url_for("purchase_view", pid=r["doc_id"]))


def _status(ov):
    return {"v": f"{ov} روز گذشته", "cls": "bad"} if ov > 0 else ("امروز" if ov == 0 else f"{-ov} روز مانده")


@rep("/reports/fin/due", "fin_due", "rep_fin")
def fin_due():
    """صورت‌حساب بر اساس سررسید: کل / هر مشتری / هر ویزیتور / تامین‌کنندگان"""
    t = norm_date(request.args.get("to") or "") or today_j()
    as_ = request.args.get("as") if request.args.get("as") in ("all", "customer", "visitor", "supplier") else "all"
    side = "pay" if as_ == "supplier" else "recv"
    pid = to_int(request.args.get("person"))
    if as_ == "customer":
        opts = _customers()
    elif as_ == "visitor":
        opts = _visitors()
    elif as_ == "supplier":
        opts = q("SELECT id, code, name FROM persons WHERE is_supplier=1 ORDER BY code")
    else:
        opts = []
    if as_ in ("customer", "visitor") and not pid and opts:
        pid = opts[0]["id"]
    items = open_items(side, t, pid if as_ in ("customer", "supplier") else None)
    if as_ == "visitor":
        items = [r for r in items if r["visitor_id"] == pid]
    # cheques not yet cashed (received) / not yet paid (issued)
    cw, ca = (" WHERE c.kind='recv' AND c.status IN ('in_box','in_bank')", []) if side == "recv" else (" WHERE c.kind='pay' AND c.status='issued'", [])
    if pid and as_ in ("customer", "supplier"):
        cw += " AND c.person_id=?"; ca.append(pid)
    if as_ == "visitor":
        cw += " AND p.visitor_id=?"; ca.append(pid)
    chq = q(f"""SELECT c.*, p.code pcode, p.name pname, b.name bname FROM cheques c LEFT JOIN persons p ON p.id=c.person_id
                LEFT JOIN banks b ON b.id=c.bank_id {cw} ORDER BY c.due_date, c.id""", ca)
    B = [0] * len(BUCKETS)
    for r in items:
        B[bucket(r["overdue"])] += r["open"]
    cum, det = 0, []
    for r in items:
        cum += r["open"]
        det.append([r["due"], WEEKDAYS[weekday(r["due"])] if r["due"] else "", _status(r["overdue"]),
                    link(f"{r['pcode']} - {r['pname']}", url_for("fin_statement", person=r["person"])), r["vname"], _doc_link(side, r),
                    r["date"], r["total"], r["open"], cum])
    secs = []
    if as_ != "customer" and as_ != "supplier" or not pid:
        S = {}
        for r in items:
            s = S.setdefault(r["person"], [r["pcode"], link(r["pname"], url_for("fin_due", **{"as": "supplier" if side == "pay" else "customer",
                                                                                              "person": r["person"], "to": t})),
                                           r["vname"]] + [0] * (len(BUCKETS) + 1) + [""])
            s[3 + bucket(r["overdue"])] += r["open"]; s[3 + len(BUCKETS)] += r["open"]
            if not s[-1] or r["due"] < s[-1]:
                s[-1] = r["due"]
        rows = sorted(S.values(), key=lambda s: -s[3 + len(BUCKETS)])
        secs.append({"title": "خلاصه به تفکیک " + ("تامین‌کننده" if side == "pay" else "مشتری"),
                     "cols": [("کد", "text"), ("طرف حساب", "text"), ("ویزیتور", "text")] + [(b[2], "money") for b in BUCKETS] +
                             [("جمع مانده", "money"), ("قدیمی‌ترین سررسید", "text")],
                     "rows": rows, "total": ["جمع", "", ""] + [sum(r[3 + i] for r in rows) for i in range(len(BUCKETS) + 1)] + [""]})
    if as_ == "all":
        V = {}
        for r in items:
            k = r["vname"] or "بدون ویزیتور"
            v = V.setdefault(k, [k, r["visitor_id"]] + [0] * (len(BUCKETS) + 1))
            v[2 + bucket(r["overdue"])] += r["open"]; v[2 + len(BUCKETS)] += r["open"]
        vrows = [[link(v[0], url_for("fin_due", **{"as": "visitor", "person": v[1], "to": t})) if v[1] else v[0]] + v[2:]
                 for v in sorted(V.values(), key=lambda v: -v[-1])]
        secs.append({"title": "خلاصه به تفکیک ویزیتور", "cols": [("ویزیتور", "text")] + [(b[2], "money") for b in BUCKETS] + [("جمع", "money")],
                     "rows": vrows, "total": ["جمع"] + [sum(r[1 + i] for r in vrows) for i in range(len(BUCKETS) + 1)]})
    secs.append({"title": "ریز فاکتورهای باز به ترتیب سررسید", "cols": [("سررسید", "text"), ("روز", "text"), ("وضعیت", "text"),
                ("طرف حساب", "text"), ("ویزیتور", "text"), ("سند", "text"), ("تاریخ فاکتور", "text"), ("مبلغ فاکتور", "money"),
                ("مانده باز", "money"), ("جمع تجمعی", "money")], "rows": det,
                 "total": ["جمع", "", "", "", "", "", "", "", sum(r["open"] for r in items), ""]})
    secs.append({"title": "چک‌های " + ("دریافتی وصول‌نشده (نزد صندوق / در جریان وصول)" if side == "recv" else "پرداختی پاس‌نشده") + " به ترتیب سررسید",
                 "cols": [("سررسید", "text"), ("وضعیت", "text"), ("طرف حساب", "text"), ("سریال", "text"), ("بانک", "text"), ("مبلغ", "money")],
                 "rows": [[c["due_date"], _status(diff_days(c["due_date"], t)), c["pname"] or "", link(c["serial"] or f"#{c['id']}", url_for("cheque_view", cid=c["id"])),
                           c["bank_name"] or c["bname"] or "", c["amount"]] for c in chq],
                 "total": ["جمع", "", "", "", "", sum(c["amount"] for c in chq)]})
    who = {"all": "همه مشتریان", "customer": f"مشتری: {_pname(pid) or ''}", "visitor": f"ویزیتور: {_pname(pid) or ''}",
           "supplier": f"تامین‌کننده: {_pname(pid) or 'همه'}"}[as_]
    spec = {"title": "صورت‌حساب بر اساس سررسید — " + who, "sub": f"مانده‌ها به تاریخ {t} — سررسید فاکتور = سررسید ثبت‌شده، یا تاریخ فاکتور + مهلت پرداخت شخص",
            "filters": [], "inputs": [("to", "به تاریخ", t)],
            "selects": [("as", [("all", "کل مشتریان"), ("customer", "هر مشتری"), ("visitor", "هر ویزیتور"), ("supplier", "تامین‌کنندگان")], as_)] +
                       ([("person", ([("", "همه")] if as_ == "supplier" else []) + [(str(o["id"]), f"{o['code']} - {o['name']}") for o in opts], str(pid or ""))] if opts else []),
            "kpis": [("سررسید گذشته", B[0] + B[1], "money"), ("تا ۷ روز آینده", B[2], "money"), ("بعد از ۷ روز", B[3] + B[4], "money"),
                     ("چک‌های " + ("وصول‌نشده" if side == "recv" else "پاس‌نشده"), sum(c["amount"] for c in chq), "money")],
            "sections": secs,
            "note": "مانده هر شخص از دفتر کل است و به جدیدترین فاکتورها نسبت داده می‌شود (روش FIFO)؛ دریافتی‌های علی‌الحساب هم قدیمی‌ترین فاکتورها را تسویه می‌کنند."}
    return export.out(spec)


@rep("/reports/fin/cashflow", "fin_cashflow", "rep_fin")
def fin_cashflow():
    """پیش‌بینی جریان نقد: سررسید مطالبات، چک‌های دریافتی، بدهی تامین‌کنندگان و چک‌های پرداختی"""
    t = today_j()
    days = max(min(to_int(request.args.get("days")) or 60, 365), 7)
    end = add_days(t, days)
    cash = v1("SELECT SUM(debit-credit) FROM voucher_lines WHERE account_id IN (?,?)", (gl.acc("cash"), gl.acc("bank")))
    rows = {}

    def put(d, col, amt):
        d = max(d, t)
        if d > end:
            return
        r = rows.setdefault(d, [0, 0, 0, 0])
        r[col] += amt
    for r in open_items("recv", t):
        put(r["due"], 0, r["open"])
    for c in q("SELECT due_date, amount FROM cheques WHERE kind='recv' AND status IN ('in_box','in_bank')"):
        put(c["due_date"], 1, c["amount"])
    for r in open_items("pay", t):
        put(r["due"], 2, r["open"])
    for c in q("SELECT due_date, amount FROM cheques WHERE kind='pay' AND status='issued'"):
        put(c["due_date"], 3, c["amount"])
    bal, data = cash, []
    for d in sorted(rows):
        a, b, c, e = rows[d]
        bal += a + b - c - e
        data.append([d + (" (و قبل)" if d == t else ""), WEEKDAYS[weekday(d)], a, b, a + b, c, e, c + e, a + b - c - e, bal])
    T = [sum(r[i] for r in data) for i in range(2, 9)]
    spec = {"title": "پیش‌بینی جریان نقدی (سررسیدها)", "sub": f"از امروز {t} تا {end} — موجودی فعلی صندوق و بانک: {cash:,}",
            "filters": [], "selects": [("days", [(str(n), f"{n} روز آینده") for n in (7, 30, 60, 90, 180, 365)], str(days))],
            "kpis": [("موجودی نقد و بانک", cash, "money"), ("ورودی پیش‌بینی", T[2], "money"), ("خروجی پیش‌بینی", T[5], "money"),
                     ("مانده پایان دوره", cash + T[6], "money")],
            "sections": [{"cols": [("تاریخ", "text"), ("روز", "text"), ("سررسید فاکتورهای فروش", "money"), ("چک‌های دریافتی", "money"),
                                   ("جمع ورودی", "money"), ("بدهی تامین‌کنندگان", "money"), ("چک‌های پرداختی", "money"), ("جمع خروجی", "money"),
                                   ("خالص روز", "money"), ("مانده پیش‌بینی", "money")], "rows": data, "total": ["جمع", ""] + T + [cash + T[6]]}],
            "note": "سررسیدهای گذشته در ردیف امروز آمده‌اند. سررسید فاکتور بدون تاریخ سررسید = تاریخ فاکتور + مهلت پرداخت شخص."}
    return export.out(spec)


@rep("/reports/fin/debtors", "fin_debtors", "rep_fin")
def fin_debtors():
    """مانده اشخاص، سقف اعتبار، آخرین فاکتور و آخرین دریافت"""
    vid = to_int(request.args.get("visitor"))
    side = request.args.get("side") if request.args.get("side") in ("dr", "cr", "all") else "dr"
    rows = q("""SELECT p.id, p.code, p.name, p.mobile, p.credit_limit, v.name vname, p.visitor_id,
                COALESCE((SELECT SUM(debit-credit) FROM voucher_lines WHERE person_id=p.id),0) bal,
                (SELECT MAX(date) FROM sales_invoices WHERE customer_id=p.id AND kind<>'return') last_inv,
                (SELECT MAX(d.date) FROM tr_docs d WHERE d.person_id=p.id AND d.kind='recv') last_recv
                FROM persons p LEFT JOIN persons v ON v.id=p.visitor_id ORDER BY p.code""")
    t = today_j()
    if vid:
        rows = [r for r in rows if r["visitor_id"] == vid]
    rows = [r for r in rows if (side == "dr" and r["bal"] > 0) or (side == "cr" and r["bal"] < 0) or (side == "all" and r["bal"])]
    rows.sort(key=lambda r: -abs(r["bal"]))
    data = [[r["code"], link(r["name"], url_for("fin_statement", person=r["id"])), r["vname"] or "", r["mobile"] or "",
             r["bal"] if r["bal"] > 0 else 0, -r["bal"] if r["bal"] < 0 else 0, r["credit_limit"] or "",
             {"v": "بیش از سقف", "cls": "bad"} if r["credit_limit"] and r["bal"] > r["credit_limit"] else "",
             r["last_inv"] or "", r["last_recv"] or "", diff_days(r["last_recv"], t) if r["last_recv"] else ""] for r in rows]
    spec = {"title": "مانده اشخاص و سقف اعتبار", "sub": f"به تاریخ {t}" + (f" | ویزیتور: {_pname(vid)}" if vid else ""), "filters": ["visitor"],
            "selects": [("side", [("dr", "بدهکاران"), ("cr", "بستانکاران"), ("all", "همه")], side)],
            "kpis": [("جمع بدهکاران", sum(r[4] for r in data), "money"), ("جمع بستانکاران", sum(r[5] for r in data), "money"),
                     ("تعداد بیش از سقف اعتبار", sum(1 for r in data if r[7]), "num")],
            "sections": [{"cols": [("کد", "text"), ("نام", "text"), ("ویزیتور", "text"), ("موبایل", "text"), ("بدهکار", "money"), ("بستانکار", "money"),
                                   ("سقف اعتبار", "money"), ("وضعیت", "text"), ("آخرین فاکتور", "text"), ("آخرین دریافت", "text"), ("روز از آخرین دریافت", "num")],
                          "rows": data, "total": ["جمع", "", "", "", sum(r[4] for r in data), sum(r[5] for r in data), "", "", "", "", ""]}]}
    return export.out(spec, visitors=_visitors())


@rep("/reports/monthly", "rep_monthly", "rep_sales")
def rep_monthly():
    """فروش ماهانه یک سال به تفکیک ویزیتور / مشتری / گروه کالا / کالا"""
    y = to_int(request.args.get("year")) or int(today_j()[:4])
    by = request.args.get("by") if request.args.get("by") in ("visitor", "customer", "group", "item", "supplier") else "visitor"
    val = request.args.get("val") if request.args.get("val") in ("amount", "qty") else "amount"
    key = {"visitor": ("COALESCE(v.code,'-')", "COALESCE(v.name,'بدون ویزیتور')", "LEFT JOIN persons v ON v.id=s.visitor_id"),
           "customer": ("c.code", "c.name", "JOIN persons c ON c.id=s.customer_id"),
           "group": ("COALESCE(g.code,'-')", "COALESCE(g.name,'بدون گروه')", "JOIN items i ON i.id=l.item_id LEFT JOIN item_groups g ON g.id=i.group_id"),
           "item": ("i.code", "i.name", "JOIN items i ON i.id=l.item_id"),
           "supplier": ("COALESCE(sp.code,'-')", "COALESCE(sp.name,'بدون تامین‌کننده')", "JOIN items i ON i.id=l.item_id LEFT JOIN persons sp ON sp.id=i.supplier_id")}[by]
    expr = "(l.qty*l.price-l.disc)*s.sg" if val == "amount" else "l.qty*s.sg"
    rs = q(f"""SELECT {key[0]} k, {key[1]} n, CAST(SUBSTRING(s.date,6,2) AS INT) m, SUM({expr}) a
               FROM sales_lines l JOIN v_si s ON s.id=l.inv_id {key[2]} WHERE s.date LIKE ? GROUP BY 1,2,3""", (f"{y}/%",))
    R = {}
    for r in rs:
        R.setdefault((r["k"], r["n"]), [0] * 12)[r["m"] - 1] += r["a"]
    rows = [[k[0], k[1]] + v + [sum(v)] for k, v in sorted(R.items(), key=lambda kv: -sum(kv[1]))]
    tot = ["جمع", ""] + [sum(r[2 + i] for r in rows) for i in range(13)]
    kind = "money" if val == "amount" else "num"
    lab = {"visitor": "ویزیتور", "customer": "مشتری", "group": "گروه کالا", "item": "کالا", "supplier": "تامین‌کننده کالا"}[by]
    spec = {"title": f"فروش ماهانه سال {y} به تفکیک {lab}", "sub": "مبلغ = فروش پس از تخفیف ردیف، بدون مالیات (برگشت‌ها کسر شده)" if val == "amount" else "تعداد به واحد فروش",
            "filters": [], "selects": [("year", [(str(i), str(i)) for i in range(int(today_j()[:4]), int(today_j()[:4]) - 6, -1)], str(y)),
                                       ("by", [("visitor", "ویزیتور"), ("customer", "مشتری"), ("group", "گروه کالا"), ("item", "کالا"), ("supplier", "تامین‌کننده کالا")], by),
                                       ("val", [("amount", "مبلغ"), ("qty", "تعداد")], val)],
            "charts": [{"id": "cm", "type": "bar", "title": "جمع ماهانه", "labels": MONTHS, "sets": [("فروش", tot[2:14])]}],
            "sections": [{"cols": [("کد", "text"), (lab, "text")] + [(m, kind) for m in (MONTHS)] + [("جمع سال", kind)],
                          "rows": rows, "total": tot}]}
    return export.out(spec)


@rep("/reports/idle", "rep_idle", "rep_stock")
def rep_idle():
    """کالاهای موجود بدون فروش در بازه"""
    f, t = period(default_from=add_days(today_j(), -60))
    sold = {r["item_id"]: r for r in q("""SELECT l.item_id, SUM(l.qty*s.sg) qty FROM sales_lines l JOIN v_si s ON s.id=l.inv_id
                                           WHERE s.date BETWEEN ? AND ? GROUP BY l.item_id""", (f, t))}
    last = {r["item_id"]: r["d"] for r in q("SELECT l.item_id, MAX(s.date) d FROM sales_lines l JOIN sales_invoices s ON s.id=l.inv_id GROUP BY l.item_id")}
    rows = []
    for r in stock_rows():
        if r["st"] <= 0 or (sold.get(r["id"]) and sold[r["id"]]["qty"] > 0):
            continue
        rows.append([r["code"], link(r["name"], url_for("item_edit", iid=r["id"])), r["group_name"] or "", r["st"], int(r["st"] * (r["vc"] or 0)),
                     last.get(r["id"], "هرگز"), diff_days(last[r["id"]], t) if r["id"] in last else ""])
    rows.sort(key=lambda r: -r[4])
    spec = {"title": "کالاهای بدون فروش (راکد)", "sub": f"موجودی دارند ولی از {f} تا {t} فروش نداشته‌اند", "filters": ["period"],
            "kpis": [("تعداد کالا", len(rows), "num"), ("ارزش موجودی راکد", sum(r[4] for r in rows), "money")],
            "sections": [{"cols": [("کد", "text"), ("کالا", "text"), ("گروه", "text"), ("موجودی (واحد)", "num"), ("ارزش (بهای خرید)", "money"),
                                   ("آخرین فروش", "text"), ("روز از آخرین فروش", "num")], "rows": rows,
                          "total": ["جمع", "", "", sum(r[3] for r in rows), sum(r[4] for r in rows), "", ""]}]}
    return export.out(spec)


@rep("/reports/fin/vat", "fin_vat", "rep_fin")
def fin_vat():
    """گزارش فصلی مالیات بر ارزش افزوده: فاکتورهای فروش و خرید با مشخصات طرف معامله"""
    t0 = today_j()
    season = (int(t0[5:7]) - 1) // 3
    f0 = f"{t0[:4]}/{season * 3 + 1:02d}/01"
    f, t = period(default_from=f0)

    def lst(view, pcol, label, ep, arg):
        rs = q(f"""SELECT x.*, p.name pname, p.national_id, p.economic_code, p.ptype FROM {view} x JOIN persons p ON p.id=x.{pcol}
                   WHERE x.date BETWEEN ? AND ? ORDER BY x.date, x.no""", (f, t))
        rows = [[link(r["no"], url_for(ep, **{arg: r["id"]})), r["date"], label(r), r["pname"], r["national_id"] or "", r["economic_code"] or "",
                 r["net_total"] * r["sg"], r["vat_total"] * r["sg"], r["total"] * r["sg"]] for r in rs]
        return rows, {"cols": [("شماره", "text"), ("تاریخ", "text"), ("نوع", "text"), ("طرف معامله", "text"), ("شناسه / کد ملی", "text"),
                               ("کد اقتصادی", "text"), ("مبلغ بدون مالیات", "money"), ("مالیات و عوارض", "money"), ("جمع", "money")],
                      "rows": rows, "total": ["جمع", "", "", "", "", "", sum(r[6] for r in rows), sum(r[7] for r in rows), sum(r[8] for r in rows)]}
    sr, ss = lst("v_si", "customer_id", lambda r: "برگشت از فروش" if r["kind"] == "return" else "فروش", "invoice", "iid")
    pr, ps = lst("v_pu", "supplier_id", lambda r: "برگشت از خرید" if r["kind"] == "return" else "خرید", "purchase_view", "pid")
    sv, pv = sum(r[7] for r in sr), sum(r[7] for r in pr)
    ss["title"], ps["title"] = "فاکتورهای فروش", "فاکتورهای خرید"
    spec = {"title": "گزارش مالیات بر ارزش افزوده", "sub": f"از {f} تا {t}", "filters": ["period"],
            "kpis": [("فروش بدون مالیات", sum(r[6] for r in sr), "money"), ("مالیات فروش", sv, "money"), ("مالیات خرید", pv, "money"),
                     ("مالیات پرداختنی دوره", sv - pv, "money")],
            "sections": [ss, ps], "note": "پیش‌فرض بازه: فصل جاری. کد اقتصادی و شناسه ملی از اطلاعات اشخاص خوانده می‌شود."}
    return export.out(spec)
