"""Sales trend, item-group movement, ABC analysis, stock cover / reorder, price changes, inactive customers, sales matrix."""
from flask import request, url_for
from core import q, v1, to_int, period, stock_rows, pkr, warehouses_list
from jalali import today_j, add_days, diff_days, week_start, year_start, MONTHS
from reports import rep, link, _visitors, _customers, _pname, _whname, receivable_until
import export

# line amount after its share of the invoice's final discount, without VAT
NET = "(l.qty*l.price-l.disc) * COALESCE(1 - s.discount::float / NULLIF(s.gross-s.ldisc,0), 1)"


def groups_list():
    """item groups with their full path, for the group filter"""
    G = {r["id"]: r for r in q("SELECT id, code, name, parent_id FROM item_groups ORDER BY code")}

    def path(i, seen=()):
        r = G[i]
        return (path(r["parent_id"], seen + (i,)) + " › " if r["parent_id"] in G and r["parent_id"] not in seen else "") + r["name"]
    return [{"id": i, "code": r["code"], "label": f"{r['code']} - {path(i)}"} for i, r in G.items()]


def group_ids(gid):
    """the group and all its sub-groups"""
    if not gid:
        return None
    kids, out, todo = {}, set(), [gid]
    for r in q("SELECT id, parent_id FROM item_groups"):
        kids.setdefault(r["parent_id"], []).append(r["id"])
    while todo:
        i = todo.pop()
        if i not in out:
            out.add(i); todo += kids.get(i, [])
    return list(out)


def _gname(gid):
    r = q("SELECT code, name FROM item_groups WHERE id=?", (gid,), one=True) if gid else None
    return f"{r['code']} - {r['name']}" if r else None


def _sups():
    return q("SELECT id, code, name FROM persons WHERE is_supplier=1 ORDER BY code")


def _flt(w, a, vid=None, cid=None, gids=None, sid=None):
    """filters on sales lines (l, s) joined to items i"""
    if vid:
        w += " AND s.visitor_id=?"; a.append(vid)
    if cid:
        w += " AND s.customer_id=?"; a.append(cid)
    if gids:
        w += " AND i.group_id = ANY(?)"; a.append(gids)
    if sid:
        w += " AND i.supplier_id=?"; a.append(sid)
    return w, a


def _sub(f, t, vid, cid, gid, sid, wid=None):
    return (f"از {f} تا {t}" + (f" | ویزیتور: {_pname(vid)}" if vid else "") + (f" | مشتری: {_pname(cid)}" if cid else "")
            + (f" | گروه: {_gname(gid)}" if gid else "") + (f" | تامین‌کننده: {_pname(sid)}" if sid else "") + (f" | انبار: {_whname(wid)}" if wid else ""))


def _pct(a, b):
    return round((a - b) * 100.0 / abs(b), 1) if b else None


def _mg(s, c):
    return round((s - c) * 100.0 / s, 1) if s else None


# ================= sales trend =================
def _buckets(f, t, grain):
    out, d = [], f
    if grain == "day":
        while d <= t:
            out.append((d, d)); d = add_days(d, 1)
    elif grain == "week":
        d = week_start(f)
        while d <= t:
            out.append((d, f"هفته {d}")); d = add_days(d, 7)
    else:
        y, m = int(f[:4]), int(f[5:7])
        while f"{y}/{m:02d}" <= t[:7]:
            out.append((f"{y}/{m:02d}", f"{MONTHS[m - 1]} {y}"))
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _bkey(d, grain):
    return d if grain == "day" else week_start(d) if grain == "week" else d[:7]


@rep("/reports/trend", "rep_trend", "rep_sales")
def rep_trend():
    """روند فروش روزانه / هفتگی / ماهانه با سود، رشد نسبت به دوره قبل، میانگین متحرک و تجمعی"""
    grain = request.args.get("grain") if request.args.get("grain") in ("day", "week", "month") else "month"
    f, t = period(default_from=year_start(today_j()) if grain == "month" else add_days(today_j(), -29 if grain == "day" else -83))
    vid, cid = to_int(request.args.get("visitor")), to_int(request.args.get("customer"))
    gid, sid = to_int(request.args.get("group")), to_int(request.args.get("supplier"))
    w, a = _flt(" WHERE s.date BETWEEN ? AND ?", [f, t], vid, cid, group_ids(gid), sid)
    rs = q(f"""SELECT s.date d, s.id sid, s.kind, s.customer_id c, SUM(l.qty*s.sg) qty, SUM(l.qty*l.price*s.sg) gross,
               SUM({NET}*s.sg) net, SUM(l.qty*l.cost*s.sg) cogs FROM sales_lines l JOIN v_si s ON s.id=l.inv_id JOIN items i ON i.id=l.item_id
               {w} GROUP BY s.date, s.id, s.kind, s.customer_id""", a)
    B = {k: {"inv": set(), "cust": set(), "qty": 0, "gross": 0, "ret": 0, "net": 0, "cogs": 0} for k, _ in _buckets(f, t, grain)}
    for r in rs:
        b = B.setdefault(_bkey(r["d"], grain), {"inv": set(), "cust": set(), "qty": 0, "gross": 0, "ret": 0, "net": 0, "cogs": 0})
        if r["kind"] == "return":
            b["ret"] -= r["net"]
        else:
            b["inv"].add(r["sid"]); b["cust"].add(r["c"]); b["gross"] += r["net"]
        b["qty"] += r["qty"]; b["net"] += r["net"]; b["cogs"] += r["cogs"]
    labels = dict(_buckets(f, t, grain))
    rows, prev, cum, hist, T = [], None, 0, [], {"inv": 0, "qty": 0, "gross": 0, "ret": 0, "net": 0, "cogs": 0}
    for k in sorted(B):
        b = B[k]
        p = b["net"] - b["cogs"]
        cum += b["net"]; hist.append(b["net"])
        ma = sum(hist[-3:]) / len(hist[-3:])
        n = len(b["inv"])
        rows.append([labels.get(k, k), n, len(b["cust"]), b["qty"], b["gross"], b["ret"], b["net"], b["cogs"], p, _mg(b["net"], b["cogs"]),
                     round(b["gross"] / n) if n else 0, _pct(b["net"], prev) if prev is not None else None, round(ma), cum])
        prev = b["net"]
        for z in ("qty", "gross", "ret", "net", "cogs"):
            T[z] += b[z]
        T["inv"] += n
    nb = len(rows) or 1
    best = max(rows, key=lambda r: r[6]) if rows else None
    half = len(rows) // 2
    first, last = (sum(r[6] for r in rows[:half]), sum(r[6] for r in rows[-half:])) if half else (0, 0)
    lab = {"day": "روز", "week": "هفته", "month": "ماه"}[grain]
    spec = {"title": f"روند فروش {({'day': 'روزانه', 'week': 'هفتگی', 'month': 'ماهانه'}[grain])}", "sub": _sub(f, t, vid, cid, gid, sid),
            "filters": ["period", "visitor", "customer", "group", "supplier"],
            "selects": [("grain", [("day", "روزانه"), ("week", "هفتگی"), ("month", "ماهانه")], grain)],
            "kpis": [("فروش خالص دوره", T["net"], "money"), ("سود ناخالص", T["net"] - T["cogs"], "money"), ("حاشیه سود", _mg(T["net"], T["cogs"]), "pct"),
                     (f"میانگین فروش هر {lab}", round(T["net"] / nb), "money"), (f"بهترین {lab}", best[0] if best and best[6] else "—", "text"),
                     ("رشد نیمه دوم نسبت به نیمه اول", _pct(last, first) if half else None, "pct")],
            "charts": [{"id": "tr", "type": "line", "title": "فروش خالص، سود و میانگین متحرک ۳ دوره", "labels": [r[0] for r in rows],
                        "sets": [("فروش خالص", [r[6] for r in rows]), ("سود ناخالص", [r[8] for r in rows]), ("میانگین متحرک", [r[12] for r in rows])]},
                       {"id": "tq", "type": "bar", "title": "تعداد فاکتور و مشتری", "labels": [r[0] for r in rows],
                        "sets": [("فاکتور", [r[1] for r in rows]), ("مشتری", [r[2] for r in rows])]}],
            "sections": [{"cols": [(lab, "text"), ("تعداد فاکتور", "num"), ("تعداد مشتری", "num"), ("تعداد فروش (باکس)", "num"), ("فروش", "money"),
                                   ("برگشت از فروش", "money"), ("فروش خالص", "money"), ("بهای تمام‌شده", "money"), ("سود ناخالص", "money"), ("حاشیه %", "pct"),
                                   ("میانگین هر فاکتور", "money"), ("رشد نسبت به قبل %", "pct"), ("میانگین متحرک ۳ دوره", "money"), ("فروش تجمعی", "money")],
                          "rows": rows, "total": ["جمع", T["inv"], "", T["qty"], T["gross"], T["ret"], T["net"], T["cogs"], T["net"] - T["cogs"],
                                                  _mg(T["net"], T["cogs"]), round(T["gross"] / T["inv"]) if T["inv"] else 0, "", "", T["net"]]}],
            "note": "مبالغ بدون مالیات و پس از تخفیف ردیف و سهم تخفیف نهایی فاکتور. بهای تمام‌شده = میانگین موزون. فیلتر گروه زیرگروه‌ها را هم شامل می‌شود."}
    return export.out(spec, visitors=_visitors(), customers=_customers(), groups=groups_list(), suppliers=_sups())


# ================= item / group movement =================
IN_T, OUT_T = ("RES", "PUR", "SRT", "MRJ"), ("FCT", "AMN")


def _movement(f, t, wid, gids, sid):
    """per item: opening, in/out by type, closing (qty) and closing value at weighted-average cost, sales and profit in the period"""
    its = [r for r in stock_rows(wh_id=wid) if (not gids or r["group_id"] in gids) and (not sid or r["supplier_id"] == sid)]
    wf, wa = (" AND wh_id=?", [wid]) if wid else ("", [])
    op = {r["item_id"]: r["s"] for r in q(f"SELECT item_id, SUM(qty) s FROM stock_moves WHERE date < ?{wf} GROUP BY item_id", [f] + wa)}
    cl = {r["item_id"]: r["s"] for r in q(f"SELECT item_id, SUM(qty) s FROM stock_moves WHERE date <= ?{wf} GROUP BY item_id", [t] + wa)}
    mv = {}
    for r in q(f"""SELECT item_id, doc_type, SUM(GREATEST(qty,0)) i, SUM(GREATEST(-qty,0)) o FROM stock_moves WHERE date BETWEEN ? AND ?{wf}
                   GROUP BY item_id, doc_type""", [f, t] + wa):
        mv.setdefault(r["item_id"], {})[r["doc_type"]] = (r["i"], r["o"])
    # company value at period end (outgoing moves carry the average cost, so value = sum qty*cost)
    val = {r["item_id"]: (r["q"], r["v"]) for r in q("""SELECT item_id, SUM(qty) q, SUM(qty*cost) v FROM stock_moves
                                                         WHERE doc_type<>'TRF' AND date <= ? GROUP BY item_id""", (t,))}
    w_, a_ = " WHERE s.date BETWEEN ? AND ?", [f, t]
    if wid:
        w_ += " AND s.wh_id=?"; a_.append(wid)
    sl = {r["item_id"]: r for r in q(f"""SELECT l.item_id, SUM(l.qty*s.sg) qty, SUM({NET}*s.sg) net, SUM(l.qty*l.cost*s.sg) cogs
                                         FROM sales_lines l JOIN v_si s ON s.id=l.inv_id {w_} GROUP BY l.item_id""", a_)}
    out = []
    for r in its:
        m = mv.get(r["id"], {})
        g = lambda ts, k: sum(m.get(x_, (0, 0))[k] for x_ in ts)
        pin = m.get("PUR", (0, 0))
        trf = m.get("TRF", (0, 0))
        res = m.get("RES", (0, 0))
        o_, c_ = op.get(r["id"], 0), cl.get(r["id"], 0)
        qv, vv = val.get(r["id"], (0, 0))
        unit = vv / qv if qv > 0 else (r["vc"] or 0)
        s = sl.get(r["id"]) or {"qty": 0, "net": 0, "cogs": 0}
        out.append(dict(r, opening=o_, closing=c_, buy=pin[0], pret=pin[1], res_in=res[0], res_out=res[1], trf_in=trf[0], trf_out=trf[1],
                        sale=m.get("FCT", (0, 0))[1], sret=m.get("SRT", (0, 0))[0], amn=m.get("AMN", (0, 0))[1], mrj=m.get("MRJ", (0, 0))[0],
                        tin=g(IN_T, 0) + trf[0], tout=g(OUT_T, 1) + pin[1] + res[1] + trf[1], unit=unit, value=round(c_ * unit),
                        sqty=s["qty"], snet=s["net"], profit=s["net"] - s["cogs"]))
    return out


@rep("/reports/groups", "rep_groups", "rep_stock")
def rep_groups():
    """گردش گروه کالا: موجودی اول دوره، وارده، صادره، پایان دوره، ارزش، فروش و سود هر گروه"""
    f, t = period(default_from=year_start(today_j()))
    wid, gid, sid = to_int(request.args.get("wh")) or None, to_int(request.args.get("group")), to_int(request.args.get("supplier"))
    G = {r["id"]: r for r in q("SELECT id, code, name FROM item_groups")}
    S = {}
    for r in _movement(f, t, wid, group_ids(gid), sid):
        k = r["group_id"]
        s = S.setdefault(k, {"n": 0, "opening": 0, "tin": 0, "tout": 0, "closing": 0, "value": 0, "sqty": 0, "snet": 0, "profit": 0})
        s["n"] += 1
        for z in ("opening", "tin", "tout", "closing", "value", "sqty", "snet", "profit"):
            s[z] += r[z]
    TS = sum(s["snet"] for s in S.values())
    rows = []
    for k, s in sorted(S.items(), key=lambda kv: -kv[1]["snet"]):
        g = G.get(k)
        args = {"from": f, "to": t, "group": k or "", "wh": wid or "", "supplier": sid or ""}
        rows.append([g["code"] if g else "-", link(g["name"] if g else "بدون گروه", url_for("rep_items", **args)), s["n"], s["opening"], s["tin"], s["tout"],
                     s["closing"], s["value"], s["sqty"], s["snet"], s["profit"], _mg(s["snet"], s["snet"] - s["profit"]),
                     round(s["snet"] * 100.0 / TS, 1) if TS else None])
    tot = ["جمع", ""] + [sum(r[i] for r in rows) for i in range(2, 11)]
    tot += [_mg(tot[9], tot[9] - tot[10]), 100.0 if TS else None]
    spec = {"title": "گردش گروه کالا", "sub": _sub(f, t, None, None, gid, sid, wid), "filters": ["period", "group", "supplier", "wh"],
            "kpis": [("ارزش موجودی پایان دوره", tot[7], "money"), ("فروش خالص", tot[9], "money"), ("سود ناخالص", tot[10], "money"), ("حاشیه سود", tot[11], "pct")],
            "charts": [{"id": "gs", "type": "bar", "horizontal": True, "title": "فروش و سود گروه‌ها", "labels": [(G[k]["name"] if k in G else "بدون گروه") for k, _ in sorted(S.items(), key=lambda kv: -kv[1]["snet"])][:15],
                        "sets": [("فروش خالص", [r[9] for r in rows][:15]), ("سود", [r[10] for r in rows][:15])]}] if rows else [],
            "sections": [{"cols": [("کد", "text"), ("گروه (ریز کالاها)", "text"), ("تعداد کالا", "num"), ("موجودی اول دوره", "num"), ("وارده", "num"), ("صادره", "num"),
                                   ("موجودی پایان دوره", "num"), ("ارزش پایان دوره (میانگین موزون)", "money"), ("فروش خالص (تعداد)", "num"),
                                   ("فروش خالص (ریال)", "money"), ("سود ناخالص", "money"), ("حاشیه %", "pct"), ("سهم از فروش %", "pct")],
                          "rows": rows, "total": tot}],
            "note": "وارده: رسید، خرید، برگشت از فروش، مرجوعی امانی" + (" و حواله ورودی" if wid else "") + " • صادره: فروش، تحویل امانی، برگشت از خرید، کسری انبارگردانی"
                    + (" و حواله خروجی" if wid else "") + ". روی نام گروه بزنید تا گردش کالا به کالای آن گروه باز شود."}
    return export.out(spec, groups=groups_list(), suppliers=_sups(), whs=warehouses_list(False))


@rep("/reports/items", "rep_items", "rep_stock")
def rep_items():
    """گردش کالا به کالا (با فیلتر گروه / تامین‌کننده): اول دوره، وارده و صادره به تفکیک، پایان دوره، ارزش، فروش و سود"""
    f, t = period()
    wid, gid, sid = to_int(request.args.get("wh")) or None, to_int(request.args.get("group")), to_int(request.args.get("supplier"))
    data = []
    for r in _movement(f, t, wid, group_ids(gid), sid):
        if not any(r[z] for z in ("opening", "tin", "tout", "closing", "sqty")):
            continue
        data.append([r["code"], link(r["name"], url_for("rep_kardex", item=r["id"], wh=wid or "", **{"from": f, "to": t})), r["group_name"] or "",
                     r["opening"], r["res_in"] - r["res_out"], r["buy"] - r["pret"], r["trf_in"] - r["trf_out"], -r["amn"], r["mrj"], -r["sale"], r["sret"],
                     r["closing"], pkr(r["closing"], r), r["unit"], r["value"], r["sqty"], r["snet"], r["profit"]])
    spec = {"title": "گردش کالا به کالا" + (f" — گروه {_gname(gid)}" if gid else ""), "sub": _sub(f, t, None, None, gid, sid, wid),
            "filters": ["period", "group", "supplier", "wh"],
            "kpis": [("تعداد کالا", len(data), "num"), ("ارزش پایان دوره", sum(d[14] for d in data), "money"), ("فروش خالص", sum(d[16] for d in data), "money"),
                     ("سود ناخالص", sum(d[17] for d in data), "money")],
            "sections": [{"cols": [("کد", "text"), ("کالا", "text"), ("گروه", "text"), ("اول دوره", "num"), ("رسید / انبارگردانی (±)", "num"), ("خرید خالص", "num"),
                                   ("حواله (±)", "num"), ("تحویل امانی", "num"), ("مرجوعی امانی", "num"), ("فروش مستقیم", "num"), ("برگشت از فروش", "num"),
                                   ("پایان دوره", "num"), ("پایان دوره (کارتن)", "text"), ("بهای واحد (میانگین موزون)", "money"), ("ارزش پایان دوره", "money"),
                                   ("کل فروش خالص (تعداد)", "num"), ("فروش خالص (ریال)", "money"), ("سود ناخالص", "money")],
                          "rows": data, "total": ["جمع", "", ""] + [sum(r[i] for r in data) for i in range(3, 12)] + ["", ""] +
                                                 [sum(r[i] for r in data) for i in range(14, 18)]}],
            "note": "اول دوره + همه ستون‌های گردش = پایان دوره. «کل فروش خالص» شامل فروش مستقیم و فاکتورهای امانی تسویه‌شده است."}
    return export.out(spec, groups=groups_list(), suppliers=_sups(), whs=warehouses_list(False))


# ================= ABC =================
@rep("/reports/abc", "rep_abc", "rep_sales")
def rep_abc():
    """تحلیل ABC کالاها بر اساس فروش یا سود"""
    f, t = period(default_from=year_start(today_j()))
    gid, sid = to_int(request.args.get("group")), to_int(request.args.get("supplier"))
    by = "profit" if request.args.get("by") == "profit" else "sales"
    w, a = _flt(" WHERE s.date BETWEEN ? AND ?", [f, t], gids=group_ids(gid), sid=sid)
    rs = q(f"""SELECT i.id, i.code, i.name, i.pack, i.unit, i.pack_unit, gr.name gname, SUM(l.qty*s.sg) qty, SUM({NET}*s.sg) net,
               SUM(l.qty*l.cost*s.sg) cogs, COUNT(DISTINCT s.customer_id) nc FROM sales_lines l JOIN v_si s ON s.id=l.inv_id JOIN items i ON i.id=l.item_id
               LEFT JOIN item_groups gr ON gr.id=i.group_id {w} GROUP BY i.id, i.code, i.name, i.pack, i.unit, i.pack_unit, gr.name""", a)
    key = (lambda r: r["net"] - r["cogs"]) if by == "profit" else (lambda r: r["net"])
    rs = sorted(rs, key=lambda r: -key(r))
    T = sum(max(key(r), 0) for r in rs)
    cum, rows, C = 0, [], {"A": [0, 0], "B": [0, 0], "C": [0, 0]}
    for n, r in enumerate(rs, 1):
        v = key(r)
        cum += max(v, 0)
        cp = cum * 100.0 / T if T else 100
        cls = "A" if cp - max(v, 0) * 100.0 / (T or 1) < 80 else "B" if cp - max(v, 0) * 100.0 / (T or 1) < 95 else "C"
        if v <= 0:
            cls = "C"
        C[cls][0] += 1; C[cls][1] += v
        rows.append([n, r["code"], r["name"], r["gname"] or "", r["qty"], pkr(r["qty"], r), r["nc"], r["net"], r["net"] - r["cogs"], _mg(r["net"], r["cogs"]),
                     round(v * 100.0 / T, 1) if T else None, round(cp, 1), {"v": cls, "cls": "bad" if cls == "C" else None}])
    lab = "سود" if by == "profit" else "فروش"
    spec = {"title": f"تحلیل ABC کالاها بر اساس {lab}", "sub": _sub(f, t, None, None, gid, sid), "filters": ["period", "group", "supplier"],
            "selects": [("by", [("sales", "بر اساس فروش"), ("profit", "بر اساس سود")], by)],
            "kpis": [(f"گروه A ({C['A'][0]} کالا)", C["A"][1], "money"), (f"گروه B ({C['B'][0]} کالا)", C["B"][1], "money"),
                     (f"گروه C ({C['C'][0]} کالا)", C["C"][1], "money"), ("تعداد کالای فروخته‌شده", len(rows), "num")],
            "sections": [{"cols": [("رتبه", "num"), ("کد", "text"), ("کالا", "text"), ("گروه", "text"), ("تعداد", "num"), ("تعداد (کارتن)", "text"), ("تعداد مشتری", "num"),
                                   ("فروش خالص", "money"), ("سود ناخالص", "money"), ("حاشیه %", "pct"), ("سهم %", "pct"), ("سهم تجمعی %", "pct"), ("کلاس", "text")],
                          "rows": rows, "total": ["", "جمع", "", "", sum(r[4] for r in rows), "", "", sum(r[7] for r in rows), sum(r[8] for r in rows),
                                                  _mg(sum(r[7] for r in rows), sum(r[7] - r[8] for r in rows)), 100.0 if T else None, "", ""]}],
            "note": "A = کالاهایی که ۸۰٪ اول " + lab + " را می‌سازند (مهم‌ترین، همیشه موجود باشند)، B = ۱۵٪ بعدی، C = ۵٪ آخر و کالاهای زیان‌ده."}
    return export.out(spec, groups=groups_list(), suppliers=_sups())


# ================= stock cover / reorder =================
@rep("/reports/cover", "rep_cover", "rep_stock")
def rep_cover():
    """پوشش موجودی و پیشنهاد سفارش: میانگین فروش روزانه، روز پوشش، نقطه سفارش"""
    t = today_j()
    days = max(to_int(request.args.get("days"), 30), 1)
    target = max(to_int(request.args.get("target"), 30), 1)
    gid, sid = to_int(request.args.get("group")), to_int(request.args.get("supplier"))
    f = add_days(t, -days + 1)
    gids = group_ids(gid)
    sold = {r["item_id"]: r["n"] for r in q("""SELECT item_id, -SUM(qty) n FROM stock_moves WHERE doc_type IN ('FCT','SRT','AMN','MRJ')
                                               AND date BETWEEN ? AND ? GROUP BY item_id""", (f, t))}
    rows = []
    for r in stock_rows():
        if (gids and r["group_id"] not in gids) or (sid and r["supplier_id"] != sid) or not r["active"]:
            continue
        s = max(sold.get(r["id"], 0), 0)
        if not s and r["st"] <= 0:
            continue
        avg = s / days
        have = r["st"] + r["open_amani"]
        cover = round(have / avg) if avg > 0 else None
        need = max(round(avg * target - have), 0)
        k = r["pack"] or 1
        need_ctn = -(-need // k) if need else 0
        st_ = "کسری" if have <= 0 and s else "سفارش فوری" if cover is not None and cover < 7 else "سفارش" if cover is not None and cover < target else \
            "بدون فروش" if not s else "کافی"
        rows.append([r["code"], r["name"], r["sup_name"] or "", r["st"], r["open_amani"], pkr(have, r), s, f"{avg:,.1f}", cover,
                     {"v": st_, "cls": "bad" if st_ in ("کسری", "سفارش فوری") else None}, need, need_ctn, need_ctn * k * (r["buy"] or r["vc"] or 0)])
    order = {"کسری": 0, "سفارش فوری": 1, "سفارش": 2, "کافی": 3, "بدون فروش": 4}
    rows.sort(key=lambda r: (order[r[9]["v"]], r[8] if r[8] is not None else 10 ** 9))
    spec = {"title": "پوشش موجودی و پیشنهاد سفارش", "sub": f"میانگین فروش {days} روز اخیر ({f} تا {t}) | پوشش هدف {target} روز" +
            (f" | گروه: {_gname(gid)}" if gid else "") + (f" | تامین‌کننده: {_pname(sid)}" if sid else ""),
            "filters": ["group", "supplier"], "inputs": [("days", "روزهای مبنا", days), ("target", "پوشش هدف (روز)", target)],
            "kpis": [("کالای نیازمند سفارش", sum(1 for r in rows if r[10] > 0), "num"), ("سفارش فوری / کسری", sum(1 for r in rows if r[9]["cls"]), "num"),
                     ("مبلغ تقریبی سفارش پیشنهادی", sum(r[12] for r in rows), "money")],
            "sections": [{"cols": [("کد", "text"), ("کالا", "text"), ("تامین‌کننده", "text"), ("موجودی انبار", "num"), ("امانی نزد ویزیتور", "num"),
                                   ("جمع (کارتن)", "text"), ("فروش دوره مبنا", "num"), ("میانگین فروش روزانه", "text"), ("روز پوشش", "num"), ("وضعیت", "text"),
                                   ("پیشنهاد سفارش (باکس)", "num"), ("پیشنهاد سفارش (کارتن)", "num"), ("مبلغ تقریبی (نرخ خرید)", "money")],
                          "rows": rows, "total": ["جمع", "", "", sum(r[3] for r in rows), sum(r[4] for r in rows), "", sum(r[6] for r in rows), "", "", "",
                                                  sum(r[10] for r in rows), sum(r[11] for r in rows), sum(r[12] for r in rows)]}],
            "note": "روز پوشش = (موجودی انبار + امانی) ÷ میانگین فروش روزانه. پیشنهاد سفارش = کسری تا پوشش هدف، گرد به کارتن کامل."}
    return export.out(spec, groups=groups_list(), suppliers=_sups())


# ================= price changes =================
@rep("/reports/price-changes", "rep_price_changes", "rep_stock")
def rep_price_changes():
    """تغییر نرخ خرید و فروش کالاها در بازه و اثر آن بر حاشیه سود"""
    f, t = period(default_from=add_days(today_j(), -89))
    gid, sid = to_int(request.args.get("group")), to_int(request.args.get("supplier"))
    gids = group_ids(gid)

    def last(col, d, inclusive):
        op = "<=" if inclusive else "<"
        return {r["item_id"]: r["v"] for r in q(f"""SELECT DISTINCT ON (item_id) item_id, {col} v FROM prices WHERE {col} IS NOT NULL AND date {op} ?
                                                   ORDER BY item_id, date DESC, id DESC""", (d,))}
    p0, p1, b0, b1 = last("price", f, False), last("price", t, True), last("buy", f, False), last("buy", t, True)
    pu = {}
    for r in q("""SELECT l.item_id, p.date, (l.qty*l.price-l.disc)::float/NULLIF(l.qty,0) u FROM purchase_lines l JOIN purchases p ON p.id=l.pur_id
                  WHERE p.kind='buy' AND l.qty>0 AND p.date<=? ORDER BY p.date, l.id""", (t,)):
        z = pu.setdefault(r["item_id"], [None, None])
        if r["date"] < f:
            z[0] = r["u"]
        z[1] = r["u"]
    nch = {r["item_id"]: r["n"] for r in q("SELECT item_id, COUNT(*) n FROM prices WHERE date BETWEEN ? AND ? GROUP BY item_id", (f, t))}
    rows = []
    for r in stock_rows(t):
        if (gids and r["group_id"] not in gids) or (sid and r["supplier_id"] != sid):
            continue
        i = r["id"]
        so, sn = p0.get(i), p1.get(i)
        bo = pu.get(i, [None])[0] if pu.get(i, [None])[0] is not None else b0.get(i)
        bn = pu.get(i, [None, None])[1] if pu.get(i, [None, None])[1] is not None else b1.get(i)
        if (so == sn or so is None) and (bo == bn or bo is None) and not nch.get(i):
            continue
        avg = r["avg_cost"] or 0
        rows.append([r["code"], link(r["name"], url_for("item_edit", iid=i)), so or 0, sn or 0, _pct(sn or 0, so) if so else None, round(bo or 0), round(bn or 0),
                     _pct(bn or 0, bo) if bo else None, round(avg), (sn or 0) - avg, _mg(sn or 0, avg), r["st"], round(r["st"] * ((bn or 0) - avg)) if bn else 0])
    spec = {"title": "تغییرات نرخ خرید و فروش", "sub": _sub(f, t, None, None, gid, sid), "filters": ["period", "group", "supplier"],
            "kpis": [("کالاهای با تغییر نرخ", len(rows), "num"), ("میانگین تغییر نرخ فروش", round(sum(r[4] for r in rows if r[4] is not None) / max(sum(1 for r in rows if r[4] is not None), 1), 1), "pct"),
                     ("میانگین تغییر نرخ خرید", round(sum(r[7] for r in rows if r[7] is not None) / max(sum(1 for r in rows if r[7] is not None), 1), 1), "pct")],
            "sections": [{"cols": [("کد", "text"), ("کالا", "text"), ("نرخ فروش قبل", "money"), ("نرخ فروش فعلی", "money"), ("تغییر فروش %", "pct"),
                                   ("نرخ خرید قبل", "money"), ("آخرین نرخ خرید", "money"), ("تغییر خرید %", "pct"), ("بهای میانگین موزون فعلی", "money"),
                                   ("سود هر واحد با نرخ فعلی", "money"), ("حاشیه %", "pct"), ("موجودی", "num"), ("سود نگهداری موجودی (به نرخ خرید جدید)", "money")],
                          "rows": rows}],
            "note": "نرخ خرید از آخرین فاکتور خرید (یا لیست قیمت). سود هر واحد = نرخ فروش فعلی − میانگین موزون (نه آخرین نرخ خرید). "
                    "«سود نگهداری» = ارزش موجودی به آخرین نرخ خرید منهای میانگین موزون؛ سودی که با گران شدن خرید، روی موجودی قبلی نصیب شما می‌شود."}
    return export.out(spec, groups=groups_list(), suppliers=_sups())


# ================= inactive customers =================
@rep("/reports/inactive", "rep_inactive", "rep_sales")
def rep_inactive():
    """مشتریانی که مدتی خرید نکرده‌اند"""
    t = today_j()
    days = max(to_int(request.args.get("days"), 30), 1)
    vid = to_int(request.args.get("visitor"))
    w, a = ("AND p.visitor_id=?", [vid]) if vid else ("", [])
    y0 = add_days(t, -365)
    rs = q(f"""SELECT p.id, p.code, p.name, p.mobile, v.name vname, MAX(s.date) last, COUNT(s.id) FILTER (WHERE s.date>=? AND s.kind<>'return') n12,
               COALESCE(SUM(s.net_total*s.sg) FILTER (WHERE s.date>=?),0) a12 FROM persons p LEFT JOIN persons v ON v.id=p.visitor_id
               LEFT JOIN v_si s ON s.customer_id=p.id WHERE p.is_customer=1 AND p.active=1 {w}
               GROUP BY p.id, p.code, p.name, p.mobile, v.name""", [y0, y0] + a)
    rows = []
    for r in rs:
        dd = diff_days(r["last"], t) if r["last"] else None
        if dd is not None and dd < days:
            continue
        rows.append([r["code"], link(r["name"], url_for("person", pid=r["id"])), r["vname"] or "", r["mobile"] or "",
                     r["last"] or "هرگز", dd, r["n12"], r["a12"], round(r["a12"] / 12), receivable_until(t, r["id"])])
    rows.sort(key=lambda r: (-(r[7] or 0)))
    spec = {"title": "مشتریان غیرفعال", "sub": f"بیش از {days} روز بدون خرید تا {t}" + (f" | ویزیتور: {_pname(vid)}" if vid else ""),
            "filters": ["visitor"], "inputs": [("days", "روز بدون خرید", days)],
            "kpis": [("تعداد مشتری غیرفعال", len(rows), "num"), ("خرید ۱۲ ماه اخیر آن‌ها", sum(r[7] for r in rows), "money"),
                     ("مانده بدهی آن‌ها", sum(r[9] for r in rows), "money")],
            "sections": [{"cols": [("کد", "text"), ("مشتری", "text"), ("ویزیتور", "text"), ("موبایل", "text"), ("آخرین خرید", "text"), ("روز بدون خرید", "num"),
                                   ("فاکتور ۱۲ ماه", "num"), ("خرید ۱۲ ماه (بدون مالیات)", "money"), ("میانگین ماهانه", "money"), ("مانده بدهی", "money")],
                          "rows": rows, "total": ["جمع", "", "", "", "", "", sum(r[6] for r in rows), sum(r[7] for r in rows), "", sum(r[9] for r in rows)]}],
            "note": "مرتب بر اساس خرید ۱۲ ماه اخیر: مشتریان پرخریدی که قطع شده‌اند اول آمده‌اند؛ برای پیگیری ویزیتور."}
    return export.out(spec, visitors=_visitors())


# ================= sales matrix =================
@rep("/reports/matrix", "rep_matrix", "rep_sales")
def rep_matrix():
    """جدول فروش: ردیف ویزیتور / مشتری، ستون گروه کالا / تامین‌کننده"""
    f, t = period()
    rk = request.args.get("rows") if request.args.get("rows") in ("visitor", "customer", "group") else "visitor"
    ck = request.args.get("cols") if request.args.get("cols") in ("group", "supplier", "visitor") else "group"
    if ck == rk:
        ck = "group" if rk != "group" else "visitor"
    val = request.args.get("val") if request.args.get("val") in ("amount", "qty", "profit") else "amount"
    D = {"visitor": ("COALESCE(v.name,'بدون ویزیتور')", "LEFT JOIN persons v ON v.id=s.visitor_id", "ویزیتور"),
         "customer": ("c.code || ' - ' || c.name", "JOIN persons c ON c.id=s.customer_id", "مشتری"),
         "group": ("COALESCE(gr.name,'بدون گروه')", "LEFT JOIN item_groups gr ON gr.id=i.group_id", "گروه کالا"),
         "supplier": ("COALESCE(sp.name,'بدون تامین‌کننده')", "LEFT JOIN persons sp ON sp.id=i.supplier_id", "تامین‌کننده")}
    expr = {"amount": f"{NET}*s.sg", "qty": "l.qty*s.sg", "profit": f"({NET} - l.qty*l.cost)*s.sg"}[val]
    rs = q(f"""SELECT {D[rk][0]} r, {D[ck][0]} c, SUM({expr}) a FROM sales_lines l JOIN v_si s ON s.id=l.inv_id JOIN items i ON i.id=l.item_id
               {D[rk][1]} {D[ck][1] if D[ck][1] != D[rk][1] else ''} WHERE s.date BETWEEN ? AND ? GROUP BY 1, 2""", (f, t))
    M, CT = {}, {}
    for r in rs:
        M.setdefault(r["r"], {})[r["c"]] = r["a"]
        CT[r["c"]] = CT.get(r["c"], 0) + r["a"]
    cols = sorted(CT, key=lambda c: -CT[c])[:20]
    rows = [[k] + [M[k].get(c, 0) for c in cols] + [sum(M[k].values())] for k in sorted(M, key=lambda k: -sum(M[k].values()))]
    kind = "num" if val == "qty" else "money"
    spec = {"title": f"جدول فروش {D[rk][2]} × {D[ck][2]}", "sub": f"از {f} تا {t}", "filters": ["period"],
            "selects": [("rows", [("visitor", "ردیف: ویزیتور"), ("customer", "ردیف: مشتری"), ("group", "ردیف: گروه کالا")], rk),
                        ("cols", [("group", "ستون: گروه کالا"), ("supplier", "ستون: تامین‌کننده"), ("visitor", "ستون: ویزیتور")], ck),
                        ("val", [("amount", "فروش خالص"), ("qty", "تعداد"), ("profit", "سود ناخالص")], val)],
            "sections": [{"cols": [(D[rk][2], "text")] + [(c, kind) for c in cols] + [("جمع", kind)], "rows": rows,
                          "total": ["جمع"] + [CT[c] for c in cols] + [sum(CT.values())]}],
            "note": "حداکثر ۲۰ ستون پرفروش نمایش داده می‌شود؛ ستون «جمع» همه را شامل است."}
    return export.out(spec)
