"""Base data: items, groups, prices, excel import/export, persons, warehouses, chart of accounts, cost centers."""
import io
from flask import render_template, request, redirect, url_for, flash, abort, jsonify, send_file
from core import (app, q, x, v1, db, next_no, to_int, uid, price_at, buy_at, stock_rows, add_moves, default_wh, warehouses_list,
                  money, pk, pkr, can, PERSON_KINDS, safe_back)
from jalali import today_j, norm_date


def _vat_in():
    v = str(request.form.get("vat_rate", "")).replace("٫", ".").strip()
    try:
        return None if v == "" else min(max(float(v), 0.0), 100.0)
    except ValueError:
        return None


def new_code(table, start):
    """next numeric code: max numeric code + 1 (at least `start`)"""
    m = v1(f"SELECT MAX(code::bigint) FROM {table} WHERE code ~ '^[0-9]{{1,15}}$'")
    return str(max(int(m or 0) + 1, start))


def item_code(supplier_id=None):
    """supplier's item: <supplier code>-001, -002 ... ; no supplier: numeric 1001, 1002 ..."""
    sc = v1("SELECT code FROM persons WHERE id=?", (supplier_id,)) if supplier_id else None
    if not sc:
        return new_code("items", 1001)
    n = 0
    for r in q("SELECT code FROM items WHERE code LIKE ?", (sc + "-%",)):
        tail = r["code"][len(sc) + 1:]
        if tail.isdigit():
            n = max(n, int(tail))
    while True:
        n += 1
        c = f"{sc}-{n:03d}"
        if not v1("SELECT COUNT(*) FROM items WHERE code=?", (c,)):
            return c


def suppliers():
    return q("SELECT id, code, name FROM persons WHERE is_supplier=1 ORDER BY code")


def supplier_by(code="", name=""):
    """find a supplier by code or exact name; create it if only the name is new"""
    if code:
        sid = v1("SELECT id FROM persons WHERE code=?", (code,))
        if sid:
            x("UPDATE persons SET is_supplier=1 WHERE id=?", (sid,))
            return sid
    if name:
        sid = v1("SELECT id FROM persons WHERE name=? ORDER BY is_supplier DESC, id LIMIT 1", (name,))
        if sid:
            x("UPDATE persons SET is_supplier=1 WHERE id=?", (sid,))
            return sid
        return x("INSERT INTO persons(code,name,is_supplier,active,created) VALUES(?,?,1,1,?)", (code or new_person_code("supplier"), name, today_j()))
    return None


def recode_items(supplier_id):
    """give every item of a supplier a code with the supplier's prefix (ids never change, documents keep working)"""
    sc = v1("SELECT code FROM persons WHERE id=?", (supplier_id,))
    n = 0
    for it in q("SELECT id, code FROM items WHERE supplier_id=? ORDER BY code", (supplier_id,)):
        if it["code"].startswith(sc + "-"):
            continue
        x("UPDATE items SET code=? WHERE id=?", (item_code(supplier_id), it["id"]))
        n += 1
    return n


def group_by_name(name, code=""):
    """find a group by code or exact name; create it (auto code) if missing"""
    if code:
        gid = v1("SELECT id FROM item_groups WHERE code=?", (code,))
        if gid:
            return gid
    gid = v1("SELECT id FROM item_groups WHERE name=? ORDER BY id LIMIT 1", (name,)) if name else 0
    return gid or x("INSERT INTO item_groups(code,name) VALUES(?,?)", (code or new_code("item_groups", 10), name or code))


def _groups():
    return q("SELECT * FROM item_groups ORDER BY code")


# ================= items =================
@app.route("/items", methods=["GET", "POST"])
def items():
    if request.method == "POST":
        code, name = request.form.get("code", "").strip(), request.form.get("name", "").strip()
        unit = request.form.get("unit", "").strip() or "باکس"
        pack_unit = request.form.get("pack_unit", "").strip() or "کارتن"
        pack = max(to_int(request.form.get("pack"), 1), 1)
        init_qty = max(to_int(request.form.get("init_ctn")), 0) * pack + max(to_int(request.form.get("init_box")), 0)
        price, buy = max(to_int(request.form.get("price")), 0), max(to_int(request.form.get("buy")), 0)
        date = norm_date(request.form.get("date")) or today_j()
        wh = to_int(request.form.get("wh_id")) or default_wh()
        gid = to_int(request.form.get("group_id")) or None
        sid = to_int(request.form.get("supplier_id")) or None
        code = code or item_code(sid)
        if not name:
            flash("نام کالا لازم است.", "bad")
        elif q("SELECT 1 FROM items WHERE code=?", (code,), one=True):
            flash("این کد کالا قبلا ثبت شده است.", "bad")
        else:
            iid = x("INSERT INTO items(code,name,group_id,unit,pack,pack_unit,barcode,vat_rate,supplier_id) VALUES(?,?,?,?,?,?,?,?,?)",
                    (code, name, gid, unit, pack, pack_unit, request.form.get("barcode", "").strip() or None, _vat_in(), sid))
            if price or buy:
                x("INSERT INTO prices(item_id,date,price,buy) VALUES(?,?,?,?)", (iid, date, price, buy))
            msg = f"کالا «{name}» با کد {code} ثبت شد."
            if init_qty > 0:
                from inventory import make_receipt
                no, _ = make_receipt(date, wh, "opening", "موجودی اولیه", [(iid, init_qty, buy)])
                msg += f" رسید موجودی اولیه شماره {no} صادر شد: {pk(init_qty, pack, unit, pack_unit)}."
            db().commit()
            flash(msg, "ok")
        return redirect(url_for("items"))
    wh = to_int(request.args.get("wh")) or None
    gid = to_int(request.args.get("group")) or None
    sup = to_int(request.args.get("supplier")) or None
    rows = stock_rows(wh_id=wh)
    if gid:
        rows = [r for r in rows if r["group_id"] == gid]
    if sup:
        rows = [r for r in rows if r["supplier_id"] == sup]
    if request.args.get("inactive") != "1":
        rows = [r for r in rows if r["active"]]
    return render_template("items.html", rows=rows, groups=_groups(), whs=warehouses_list(), wh=wh, gid=gid, sups=suppliers(), sup=sup)


@app.route("/items/<int:iid>", methods=["GET", "POST"])
def item_edit(iid):
    it = q("SELECT * FROM items WHERE id=?", (iid,), one=True) or abort(404)
    if request.method == "POST":
        name, code = request.form.get("name", "").strip(), request.form.get("code", "").strip() or it["code"]
        sid = to_int(request.form.get("supplier_id")) or None
        if request.form.get("recode") and sid and not code.startswith((v1("SELECT code FROM persons WHERE id=?", (sid,)) or "") + "-"):
            code = item_code(sid)
        if code != it["code"] and q("SELECT 1 FROM items WHERE code=? AND id<>?", (code, iid), one=True):
            flash("این کد کالا برای کالای دیگری ثبت شده است.", "bad")
        elif name:
            x("""UPDATE items SET code=?, name=?, group_id=?, unit=?, pack=?, pack_unit=?, barcode=?, active=?, note=?, vat_rate=?, supplier_id=?
                 WHERE id=?""",
              (code, name, to_int(request.form.get("group_id")) or None, request.form.get("unit", "").strip() or "باکس",
               max(to_int(request.form.get("pack"), it["pack"]), 1), request.form.get("pack_unit", "").strip() or "کارتن",
               request.form.get("barcode", "").strip() or None, 1 if request.form.get("active") else 0,
               request.form.get("note", "").strip() or None, _vat_in(), sid, iid))
            db().commit()
            flash("ویرایش ذخیره شد.", "ok")
        return redirect(url_for("item_edit", iid=iid))
    hist = q("SELECT * FROM prices WHERE item_id=? ORDER BY date DESC, id DESC", (iid,))
    row = next((r for r in stock_rows() if r["id"] == iid), None)
    by_wh = q("""SELECT w.name, COALESCE(SUM(m.qty),0) st FROM warehouses w LEFT JOIN stock_moves m ON m.wh_id=w.id AND m.item_id=?
                 GROUP BY w.id, w.name, w.is_default, w.code ORDER BY w.is_default DESC, w.code""", (iid,))
    return render_template("item_edit.html", it=it, hist=hist, stock=row["st"], price=price_at(iid, today_j()),
                           buy=buy_at(iid, today_j()), row=row, groups=_groups(), by_wh=by_wh, sups=suppliers())


@app.route("/prices", methods=["GET", "POST"])
def prices():
    if request.method == "POST":
        iid = to_int(request.form.get("item_id"))
        date = norm_date(request.form.get("date"))
        price = to_int(request.form.get("price")) if str(request.form.get("price", "")).strip() else (price_at(iid, date) if iid and date else 0)
        buy = to_int(request.form.get("buy")) if str(request.form.get("buy", "")).strip() else (int(buy_at(iid, date) or 0) if iid and date else 0)
        if not iid or not date or price < 0 or buy < 0 or (price == 0 and buy == 0):
            flash("کالا، تاریخ معتبر و حداقل یکی از نرخ خرید یا فروش لازم است.", "bad")
        else:
            x("INSERT INTO prices(item_id,date,price,buy) VALUES(?,?,?,?)", (iid, date, price, buy))
            db().commit()
            msg = f"نرخ ثبت شد: خرید {money(buy)} / فروش {money(price)} ریال"
            if buy and price:
                msg += f" (سود واحد {money(price - buy)} ریال، {round((price - buy) * 100.0 / price, 1)}% از فروش)"
            flash(msg, "ok")
        return redirect(safe_back(request.form.get("back"), url_for("prices")))
    rows = q("SELECT p.*, i.code, i.name FROM prices p JOIN items i ON i.id=p.item_id ORDER BY p.date DESC, p.id DESC LIMIT 300")
    return render_template("prices.html", rows=rows, items=stock_rows(), groups=_groups())


@app.post("/prices/<int:pid>/delete")
def price_delete(pid):
    x("DELETE FROM prices WHERE id=?", (pid,))
    db().commit()
    flash("نرخ حذف شد.", "ok")
    return redirect(safe_back(request.form.get("back"), url_for("prices")))


@app.post("/items/bulk-price")
def items_bulk_price():
    gid = to_int(request.form.get("group_id")) or None
    target = request.form.get("target", "price")
    mode = request.form.get("mode", "pct")
    val = float(str(request.form.get("value", "0")).replace(",", "") or 0)
    rnd = max(to_int(request.form.get("round"), 1), 1)
    date = norm_date(request.form.get("date")) or today_j()
    if not val:
        flash("مقدار تغییر را وارد کنید.", "bad")
        return redirect(url_for("prices"))
    n = 0
    for r in stock_rows(date):
        if gid and r["group_id"] != gid:
            continue
        p, b = int(r["price"] or 0), int(r["buy"] or 0)

        def ch(v):
            if not v:
                return v
            nv = v * (1 + val / 100.0) if mode == "pct" else v + val
            return max(int(round(nv / rnd)) * rnd, 0)
        np_, nb = (ch(p) if target in ("price", "both") else p), (ch(b) if target in ("buy", "both") else b)
        if (np_, nb) != (p, b):
            x("INSERT INTO prices(item_id,date,price,buy) VALUES(?,?,?,?)", (r["id"], date, np_, nb))
            n += 1
    db().commit()
    flash(f"نرخ {n} کالا از تاریخ {date} تغییر کرد.", "ok")
    return redirect(url_for("prices"))


# ---------- item groups
@app.route("/groups", methods=["GET", "POST"])
def groups():
    if request.method == "POST":
        gid = to_int(request.form.get("id"))
        code, name = request.form.get("code", "").strip(), request.form.get("name", "").strip()
        if not gid and not code:
            code = new_code("item_groups", 10)
        if not code or not name:
            flash("کد و نام گروه لازم است.", "bad")
        elif q("SELECT 1 FROM item_groups WHERE code=? AND id<>?", (code, gid), one=True):
            flash("این کد گروه وجود دارد.", "bad")
        elif gid:
            x("UPDATE item_groups SET code=?, name=?, parent_id=?, active=? WHERE id=?",
              (code, name, to_int(request.form.get("parent_id")) or None, 1 if request.form.get("active") else 0, gid))
            db().commit(); flash("گروه ذخیره شد.", "ok")
        else:
            x("INSERT INTO item_groups(code,name,parent_id) VALUES(?,?,?)", (code, name, to_int(request.form.get("parent_id")) or None))
            db().commit(); flash(f"گروه «{name}» ثبت شد.", "ok")
        return redirect(url_for("groups"))
    rows = q("""SELECT g.*, p.name parent_name, (SELECT COUNT(*) FROM items WHERE group_id=g.id) n
                FROM item_groups g LEFT JOIN item_groups p ON p.id=g.parent_id ORDER BY g.code""")
    return render_template("groups.html", rows=rows)


# ---------- excel
XL_COLS = ["کد کالا", "نام کالا", "کد تامین‌کننده", "نام تامین‌کننده", "کد گروه", "نام گروه", "واحد فروش", "واحد بسته‌بندی", "تعداد در بسته",
           "نرخ خرید", "نرخ فروش", "موجودی اولیه (واحد فروش)", "بارکد", "فعال (1/0)"]
# carton-based columns (template); old box-based columns are still accepted
XL_ALIAS = {"تعداد باکس در کارتن": "تعداد در بسته", "نرخ خرید هر باکس": "نرخ خرید", "نرخ فروش هر باکس": "نرخ فروش"}
XL_CTN = ["نرخ خرید هر کارتن", "نرخ فروش هر کارتن", "موجودی اولیه (کارتن)", "موجودی اولیه (باکس)", "موجودی واقعی (کارتن)", "موجودی واقعی (باکس)"]
# template for new items: no codes, the system assigns item and group codes
TPL_COLS = ["نام کالا", "نام تامین‌کننده", "نام گروه", "واحد فروش", "واحد بسته‌بندی", "تعداد باکس در کارتن", "نرخ خرید هر کارتن", "نرخ فروش هر کارتن",
            "موجودی اولیه (کارتن)", "موجودی اولیه (باکس)", "بارکد"]


def _xl(rows, title, extra_cols=(), cols=None):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    wb = Workbook()
    ws = wb.active
    ws.title = title
    ws.sheet_view.rightToLeft = True
    cols = (cols or XL_COLS) + list(extra_cols)
    ws.append(cols)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF", name="Vazirmatn")
        c.fill = PatternFill("solid", fgColor="1E3A8A")
        c.alignment = Alignment(horizontal="center", wrap_text=True)
    for r in rows:
        ws.append(r)
    widths = {"کد کالا": 14, "نام کالا": 34, "کد تامین‌کننده": 12, "نام تامین‌کننده": 22, "کد گروه": 10, "نام گروه": 18, "بارکد": 16, "فعال (1/0)": 8}
    for i, c in enumerate(cols, 1):
        w = widths.get(c, 14)
        ws.column_dimensions[ws.cell(1, i).column_letter].width = w
    ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


@app.route("/items/template.xlsx")
def items_template():
    buf = _xl([["نمونه: شامپو ۲۰۰ میل", "پخش نمونه", "بهداشتی", "باکس", "کارتن", 12, 7200000, 10200000, 40, 0, ""]], "کالاها", cols=TPL_COLS)
    return send_file(buf, as_attachment=True, download_name="items_template.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/items/export.xlsx")
def items_export():
    rows = []
    gmap = {g_["id"]: g_ for g_ in _groups()}
    for r in stock_rows():
        g_ = gmap.get(r["group_id"])
        rows.append([r["code"], r["name"], r["sup_code"] or "", r["sup_name"] or "", g_["code"] if g_ else "", g_["name"] if g_ else "", r["unit"], r["pack_unit"], r["pack"],
                     int(r["buy"] or 0), int(r["price"] or 0), None, r["barcode"] or "", r["active"], int(r["st"])])
    buf = _xl(rows, "کالاها", ["موجودی فعلی (فقط نمایش)", "موجودی واقعی (کارتن)", "موجودی واقعی (باکس)"])
    return send_file(buf, as_attachment=True, download_name=f"items_{today_j().replace('/', '')}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


@app.route("/items/import", methods=["GET", "POST"])
def items_import():
    result = None
    if request.method == "POST":
        f = request.files.get("file")
        date = norm_date(request.form.get("date")) or today_j()
        wh = to_int(request.form.get("wh_id")) or default_wh()
        if not f or not f.filename:
            flash("فایل اکسل را انتخاب کنید.", "bad")
            return redirect(url_for("items_import"))
        try:
            from openpyxl import load_workbook
            ws = load_workbook(io.BytesIO(f.read()), data_only=True).worksheets[0]
        except Exception:
            flash("فایل اکسل معتبر نیست (xlsx).", "bad")
            return redirect(url_for("items_import"))
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            flash("فایل خالی است.", "bad")
            return redirect(url_for("items_import"))
        head = [_cell(h) for h in rows[0]]
        idx = {c: head.index(c) for c in XL_COLS + XL_CTN if c in head}
        for a, c in XL_ALIAS.items():
            if a in head and c not in idx:
                idx[c] = head.index(a)
        if "نام کالا" not in idx:
            flash("ستون «نام کالا» در ردیف اول لازم است. فایل نمونه را دانلود کنید.", "bad")
            return redirect(url_for("items_import"))

        def get(r, c):
            i = idx.get(c)
            return _cell(r[i]) if i is not None and i < len(r) else ""
        errors, plan, seen = [], [], set()
        for n, r in enumerate(rows[1:], 2):
            if not any(_cell(v) for v in r):
                continue
            code, name = get(r, "کد کالا"), get(r, "نام کالا")
            if not code and not name:
                errors.append((n, "نام کالا خالی است")); continue
            key = ("c", code) if code else ("n", name)
            if key in seen:
                errors.append((n, f"کد {code} تکراری در فایل" if code else f"نام «{name}» تکراری در فایل")); continue
            seen.add(key)
            nums = {}
            for c in ("تعداد در بسته", "نرخ خرید", "نرخ فروش", "موجودی اولیه (واحد فروش)") + tuple(XL_CTN):
                raw = get(r, c)
                v = to_int(raw, None) if raw else None
                if raw and (v is None or v < 0):
                    errors.append((n, f"«{c}» عدد معتبر نیست: {raw}")); break
                nums[c] = v
            else:
                if code:
                    ex = q("SELECT * FROM items WHERE code=?", (code,), one=True)
                else:
                    ex = q("SELECT * FROM items WHERE name=? ORDER BY id LIMIT 1", (name,), one=True)
                if not ex and not name:
                    errors.append((n, f"نام کالای جدید {code} خالی است")); continue
                plan.append((code, name, ex, (get(r, "کد تامین‌کننده"), get(r, "نام تامین‌کننده")), get(r, "کد گروه"), get(r, "نام گروه"), get(r, "واحد فروش"), get(r, "واحد بسته‌بندی"),
                             nums, get(r, "بارکد"), get(r, "فعال (1/0)")))
        if errors:
            result = {"errors": errors}
        else:
            created = updated = priced = 0
            opening, counted = [], {}
            for code, name, ex, sup, gcode, gname, unit, punit, nums, barcode, active in plan:
                gid = group_by_name(gname, gcode) if (gcode or gname) else None
                sid = supplier_by(*sup) if (sup[0] or sup[1]) else None
                pack = nums["تعداد در بسته"]
                k = pack or (ex["pack"] if ex else 1) or 1
                for col, ccol in (("نرخ خرید", "نرخ خرید هر کارتن"), ("نرخ فروش", "نرخ فروش هر کارتن")):
                    if nums[col] is None and nums[ccol] is not None:      # carton price -> box price
                        nums[col] = round(nums[ccol] / k)
                if nums["موجودی اولیه (کارتن)"] or nums["موجودی اولیه (باکس)"]:
                    nums["موجودی اولیه (واحد فروش)"] = (nums["موجودی اولیه (واحد فروش)"] or 0) + (nums["موجودی اولیه (کارتن)"] or 0) * k + (nums["موجودی اولیه (باکس)"] or 0)
                if ex:
                    x("""UPDATE items SET name=COALESCE(NULLIF(?,''),name), group_id=COALESCE(?,group_id), unit=COALESCE(NULLIF(?,''),unit),
                         pack_unit=COALESCE(NULLIF(?,''),pack_unit), pack=COALESCE(?,pack), barcode=COALESCE(NULLIF(?,''),barcode),
                         active=COALESCE(?,active), supplier_id=COALESCE(?,supplier_id) WHERE id=?""",
                      (name, gid, unit, punit, pack if pack else None, barcode, int(active) if active in ("0", "1") else None, sid, ex["id"]))
                    iid = ex["id"]; updated += 1
                else:
                    iid = x("INSERT INTO items(code,name,group_id,unit,pack,pack_unit,barcode,active,supplier_id) VALUES(?,?,?,?,?,?,?,?,?)",
                            (code or item_code(sid), name, gid, unit or "باکس", pack or 1, punit or "کارتن", barcode or None,
                             int(active) if active in ("0", "1") else 1, sid))
                    created += 1
                b, p = nums["نرخ خرید"], nums["نرخ فروش"]
                if b is not None or p is not None:
                    cb, cp = int(buy_at(iid, date) or 0), price_at(iid, date)
                    nb, np_ = (b if b is not None else cb), (p if p is not None else cp)
                    if (nb, np_) != (cb, cp):
                        x("INSERT INTO prices(item_id,date,price,buy) VALUES(?,?,?,?)", (iid, date, np_, nb))
                        priced += 1
                if nums["موجودی واقعی (کارتن)"] is not None or nums["موجودی واقعی (باکس)"] is not None:
                    counted[iid] = (nums["موجودی واقعی (کارتن)"] or 0) * k + (nums["موجودی واقعی (باکس)"] or 0)
                oq = nums["موجودی اولیه (واحد فروش)"]
                if oq:
                    opening.append((iid, oq, b if b is not None else int(buy_at(iid, date) or 0)))
            rno = None
            if opening:
                from inventory import make_receipt
                rno, _ = make_receipt(date, wh, "opening", "ورود گروهی از اکسل", opening)
            ano = adj = None
            if counted:
                from inventory import stock_count
                ano, _, adj = stock_count(date, wh, counted, "انبارگردانی از اکسل")
            db().commit()
            result = {"created": created, "updated": updated, "priced": priced, "rno": rno, "opening": len(opening), "ano": ano, "adj": adj}
    return render_template("items_import.html", result=result, whs=warehouses_list())


# ================= persons =================
KIND_FLAG = {"customer": "is_customer", "supplier": "is_supplier", "visitor": "is_visitor", "employee": "is_employee", "other": "is_other"}
KIND_PREFIX = {"customer": "C", "supplier": "S", "visitor": "V", "employee": "E", "other": "P"}


def new_person_code(kind):
    pre = KIND_PREFIX.get(kind, "P")
    while True:
        n = next_no("PER-" + pre)
        code = f"{pre}{n}"
        if not v1("SELECT COUNT(*) FROM persons WHERE code=?", (code,)):
            return code


def _person_form(pid=None):
    f = request.form
    flags = {k: 1 if f.get(k) else 0 for k in KIND_FLAG.values()}
    if not any(flags.values()):
        raise ValueError("حداقل یک نوع (مشتری، تامین‌کننده، ویزیتور...) انتخاب کنید.")
    name = f.get("name", "").strip()
    if not name:
        raise ValueError("نام لازم است.")
    code = f.get("code", "").strip()
    if not code:
        first = next(k for k, col in KIND_FLAG.items() if flags[col])
        code = new_person_code(first)
    if q("SELECT 1 FROM persons WHERE code=? AND id<>?", (code, pid or 0), one=True):
        raise ValueError("این کد برای شخص دیگری ثبت شده است.")
    return dict(code=code, name=name, **flags, visitor_id=to_int(f.get("visitor_id")) or None,
                phone=f.get("phone", "").strip() or None, mobile=f.get("mobile", "").strip() or None,
                address=f.get("address", "").strip() or None, national_id=f.get("national_id", "").strip() or None,
                economic_code=f.get("economic_code", "").strip() or None, note=f.get("note", "").strip() or None,
                credit_limit=max(to_int(f.get("credit_limit")), 0), credit_days=max(to_int(f.get("credit_days")), 0), active=1 if f.get("active", "1") else 0,
                ptype="legal" if f.get("ptype") == "legal" else "real", reg_no=f.get("reg_no", "").strip() or None,
                postal_code=f.get("postal_code", "").strip() or None)


def visitors_list():
    return q("SELECT id, code, name FROM persons WHERE is_visitor=1 AND active=1 ORDER BY code")


from core import CUST_BAL, SUP_BAL
BAL_SQL = CUST_BAL.format(p="p.id")
PUR_SQL = SUP_BAL.format(p="p.id")


@app.route("/persons", methods=["GET", "POST"])
def persons():
    kind = request.args.get("kind", "")
    if request.method == "POST":
        try:
            d = _person_form()
            cols = ",".join(d)
            pid = x(f"INSERT INTO persons({cols},created) VALUES({','.join('?' * len(d))},?)", (*d.values(), today_j()))
            db().commit()
            flash(f"«{d['name']}» با کد {d['code']} ثبت شد.", "ok")
            return redirect(url_for("person", pid=pid))
        except ValueError as e:
            db().rollback()
            flash(str(e), "bad")
        return redirect(url_for("persons", kind=kind))
    sql = f"""SELECT p.*, v.name vname, {BAL_SQL} AS receivable, {PUR_SQL} AS purchased FROM persons p
              LEFT JOIN persons v ON v.id=p.visitor_id WHERE 1=1"""
    args = []
    if kind in KIND_FLAG:
        sql += f" AND p.{KIND_FLAG[kind]}=1"
    s = request.args.get("q", "").strip()
    if s:
        sql += " AND (p.code ILIKE ? OR p.name ILIKE ? OR p.mobile ILIKE ? OR p.phone ILIKE ?)"
        args += [f"%{s}%"] * 4
    vf = to_int(request.args.get("visitor"))
    if vf:
        sql += " AND p.visitor_id=?"; args.append(vf)
    rows = q(sql + " ORDER BY p.code LIMIT 1000", args)
    counts = {k: v1(f"SELECT COUNT(*) FROM persons WHERE {c}=1") for k, c in KIND_FLAG.items()}
    return render_template("persons.html", rows=rows, kind=kind, counts=counts, visitors=visitors_list(), vf=vf)


@app.route("/persons/<int:pid>", methods=["GET", "POST"])
def person(pid):
    p = q("SELECT * FROM persons WHERE id=?", (pid,), one=True) or abort(404)
    if request.method == "POST":
        try:
            d = _person_form(pid)
            if p["is_visitor"] and not d["is_visitor"] and v1("SELECT COUNT(*) FROM consignments WHERE visitor_id=? AND status='open'", (pid,)):
                raise ValueError("این ویزیتور امانی باز دارد؛ نمی‌توان نوع ویزیتور را برداشت.")
            x(f"UPDATE persons SET {', '.join(k + '=?' for k in d)} WHERE id=?", (*d.values(), pid))
            db().commit()
            flash("ذخیره شد.", "ok")
        except ValueError as e:
            db().rollback()
            flash(str(e), "bad")
        return redirect(url_for("person", pid=pid))
    from core import INV_SQL, CONS_SQL
    st = {
        "receivable": v1(f"SELECT {BAL_SQL} FROM persons p WHERE p.id=?", (pid,)),
        "purchased": v1(f"SELECT {PUR_SQL} FROM persons p WHERE p.id=?", (pid,)),
        "visitor_sales": v1("SELECT SUM(total*sg) FROM v_si WHERE visitor_id=?", (pid,)),
        "open_amani": v1("SELECT SUM(l.qty) FROM cons_lines l JOIN consignments c ON c.id=l.cons_id WHERE c.visitor_id=? AND c.status='open'", (pid,)),
    }
    invs = q(INV_SQL + " WHERE s.customer_id=? OR s.visitor_id=? ORDER BY s.date DESC, s.no DESC LIMIT 30", (pid, pid))
    cons = q(CONS_SQL + " WHERE c.visitor_id=? ORDER BY c.date DESC, c.no DESC LIMIT 15", (pid,)) if p["is_visitor"] else []
    purs = q("SELECT * FROM v_pu u WHERE u.supplier_id=? ORDER BY u.date DESC, u.no DESC LIMIT 20", (pid,)) if p["is_supplier"] else []
    sitems = [r for r in stock_rows() if r["supplier_id"] == pid] if p["is_supplier"] else []
    custs = q(f"SELECT p.id, p.code, p.name, p.mobile, {BAL_SQL} AS receivable FROM persons p WHERE p.visitor_id=? ORDER BY p.code", (pid,)) \
        if p["is_visitor"] else []
    return render_template("person.html", p=p, st=st, invs=invs, cons=cons, purs=purs, custs=custs, visitors=visitors_list(), sitems=sitems)


@app.post("/persons/<int:pid>/recode-items")
def supplier_recode(pid):
    n = recode_items(pid)
    db().commit()
    flash(f"کد {n} کالا با پیشوند کد تامین‌کننده بازسازی شد." if n else "کد همه کالاهای این تامین‌کننده درست است.", "ok")
    return redirect(url_for("person", pid=pid))


@app.route("/api/persons", methods=["GET", "POST"])
def api_persons():
    """GET ?q=&kind=customer|supplier|visitor ; POST json {name, mobile, kind, visitor_id} -> quick add"""
    if request.method == "POST":
        d = request.get_json(silent=True) or {}
        name = str(d.get("name", "")).strip()
        kind = d.get("kind") if d.get("kind") in KIND_FLAG else "customer"
        if not name:
            return jsonify({"error": "نام لازم است."}), 400
        code = new_person_code(kind)
        pid = x(f"INSERT INTO persons(code,name,{KIND_FLAG[kind]},mobile,visitor_id,created) VALUES(?,?,1,?,?,?)",
                (code, name, str(d.get("mobile", "")).strip() or None, to_int(d.get("visitor_id")) or None, today_j()))
        db().commit()
        return jsonify({"id": pid, "code": code, "name": name})
    kind = request.args.get("kind", "")
    s = request.args.get("q", "").strip()
    sql, a = "SELECT id, code, name, mobile, visitor_id FROM persons WHERE active=1", []
    if kind in KIND_FLAG:
        sql += f" AND {KIND_FLAG[kind]}=1"
    if s:
        sql += " AND (code ILIKE ? OR name ILIKE ? OR mobile ILIKE ?)"; a += [f"%{s}%"] * 3
    return jsonify([dict(r) for r in q(sql + " ORDER BY code LIMIT 30", a)])


# ================= warehouses / accounts / cost centers =================
@app.route("/warehouses", methods=["GET", "POST"])
def warehouses():
    if request.method == "POST":
        wid = to_int(request.form.get("id"))
        code, name = request.form.get("code", "").strip(), request.form.get("name", "").strip()
        dflt = 1 if request.form.get("is_default") else 0
        active = 1 if request.form.get("active", "1") else 0
        if not code or not name:
            flash("کد و نام انبار لازم است.", "bad")
        elif q("SELECT 1 FROM warehouses WHERE code=? AND id<>?", (code, wid), one=True):
            flash("این کد انبار وجود دارد.", "bad")
        else:
            if dflt:
                x("UPDATE warehouses SET is_default=0")
            if wid:
                x("UPDATE warehouses SET code=?, name=?, keeper=?, address=?, is_default=?, active=? WHERE id=?",
                  (code, name, request.form.get("keeper"), request.form.get("address"), dflt, active, wid))
            else:
                x("INSERT INTO warehouses(code,name,keeper,address,is_default) VALUES(?,?,?,?,?)",
                  (code, name, request.form.get("keeper"), request.form.get("address"), dflt))
            if not v1("SELECT COUNT(*) FROM warehouses WHERE is_default=1 AND active=1"):
                x("UPDATE warehouses SET is_default=1 WHERE id=(SELECT id FROM warehouses WHERE active=1 ORDER BY id LIMIT 1)")
            db().commit()
            flash("انبار ذخیره شد.", "ok")
        return redirect(url_for("warehouses"))
    rows = q("""SELECT w.*, COALESCE((SELECT SUM(qty) FROM stock_moves WHERE wh_id=w.id),0) st,
                (SELECT COUNT(DISTINCT item_id) FROM stock_moves m WHERE wh_id=w.id) n_items FROM warehouses w ORDER BY w.is_default DESC, w.code""")
    return render_template("warehouses.html", rows=rows)


@app.route("/accounts", methods=["GET", "POST"])
def accounts():
    if request.method == "POST":
        aid = to_int(request.form.get("id"))
        name = request.form.get("name", "").strip()
        if aid:
            a = q("SELECT * FROM accounts WHERE id=?", (aid,), one=True) or abort(404)
            if name:
                x("UPDATE accounts SET name=?, active=? WHERE id=?", (name, 1 if request.form.get("active") else 0, aid))
                db().commit(); flash("حساب ذخیره شد.", "ok")
        else:
            parent = q("SELECT * FROM accounts WHERE id=?", (to_int(request.form.get("parent_id")),), one=True)
            code = request.form.get("code", "").strip()
            if not name or not code:
                flash("کد و نام حساب لازم است.", "bad")
            elif parent and not code.startswith(parent["code"]):
                flash(f"کد حساب باید با کد حساب بالادست ({parent['code']}) شروع شود.", "bad")
            elif parent and parent["level"] >= 3:
                flash("زیر حساب معین نمی‌توان حساب تعریف کرد (تفصیلی‌ها: اشخاص، بانک، صندوق، مرکز هزینه).", "bad")
            elif q("SELECT 1 FROM accounts WHERE code=?", (code,), one=True):
                flash("این کد حساب وجود دارد.", "bad")
            else:
                x("INSERT INTO accounts(code,name,level,parent_id,nature) VALUES(?,?,?,?,?)",
                  (code, name, (parent["level"] + 1) if parent else 1, parent["id"] if parent else None,
                   request.form.get("nature") or (parent["nature"] if parent else "-")))
                db().commit(); flash(f"حساب {code} - {name} ثبت شد.", "ok")
        return redirect(url_for("accounts"))
    return render_template("accounts.html", rows=q("SELECT * FROM accounts ORDER BY code"))


@app.route("/cost-centers", methods=["GET", "POST"])
def cost_centers():
    if request.method == "POST":
        cid = to_int(request.form.get("id"))
        code, name = request.form.get("code", "").strip(), request.form.get("name", "").strip()
        if not code or not name:
            flash("کد و نام لازم است.", "bad")
        elif q("SELECT 1 FROM cost_centers WHERE code=? AND id<>?", (code, cid), one=True):
            flash("این کد وجود دارد.", "bad")
        elif cid:
            x("UPDATE cost_centers SET code=?, name=?, active=? WHERE id=?", (code, name, 1 if request.form.get("active") else 0, cid))
            db().commit(); flash("ذخیره شد.", "ok")
        else:
            x("INSERT INTO cost_centers(code,name) VALUES(?,?)", (code, name))
            db().commit(); flash("مرکز هزینه ثبت شد.", "ok")
        return redirect(url_for("cost_centers"))
    return render_template("cost_centers.html", rows=q("SELECT * FROM cost_centers ORDER BY code"))
