"""Standard deletion of base data (only when unused, with a list of where it is used)."""
from flask import render_template, request, redirect, url_for, flash, abort, session
from werkzeug.security import check_password_hash
from core import app, q, x, v1, db, to_int, safe_back, del_moves
import gl

# kind -> (table, label, [(count sql, what)], list endpoint)
USAGE = {
    "person": ("persons", "شخص", [
        ("SELECT COUNT(*) FROM sales_invoices WHERE customer_id=? OR visitor_id=?", "فاکتور فروش"),
        ("SELECT COUNT(*) FROM purchases WHERE supplier_id=?", "فاکتور خرید"),
        ("SELECT COUNT(*) FROM consignments WHERE visitor_id=?", "فاکتور امانی"),
        ("SELECT COUNT(*) FROM tr_docs WHERE person_id=? OR visitor_id=?", "سند دریافت/پرداخت"),
        ("SELECT COUNT(*) FROM cheques WHERE person_id=? OR holder_id=?", "چک"),
        ("SELECT COUNT(*) FROM voucher_lines WHERE person_id=?", "ردیف سند حسابداری"),
        ("SELECT COUNT(*) FROM items WHERE supplier_id=?", "کالا")], "persons"),
    "item": ("items", "کالا", [
        ("SELECT COUNT(DISTINCT inv_id) FROM sales_lines WHERE item_id=?", "فاکتور فروش"),
        ("SELECT COUNT(DISTINCT pur_id) FROM purchase_lines WHERE item_id=?", "فاکتور خرید"),
        ("SELECT COUNT(DISTINCT cons_id) FROM cons_lines WHERE item_id=?", "فاکتور امانی"),
        ("SELECT COUNT(DISTINCT tr_id) FROM transfer_lines WHERE item_id=?", "حواله انبار"),
        ("""SELECT COUNT(DISTINCT l.receipt_id) FROM receipt_lines l JOIN receipts r ON r.id=l.receipt_id
            WHERE l.item_id=? AND r.kind<>'opening'""", "رسید انبار")], "items"),
    "warehouse": ("warehouses", "انبار", [("SELECT COUNT(*) FROM stock_moves WHERE wh_id=?", "گردش کالا")], "warehouses"),
    "group": ("item_groups", "گروه کالا", [("SELECT COUNT(*) FROM items WHERE group_id=?", "کالا"),
                                          ("SELECT COUNT(*) FROM item_groups WHERE parent_id=?", "زیرگروه")], "groups"),
    "cc": ("cost_centers", "مرکز هزینه", [("SELECT COUNT(*) FROM voucher_lines WHERE cc_id=?", "ردیف سند حسابداری"),
                                         ("SELECT COUNT(*) FROM tr_docs WHERE cc_id=?", "سند خزانه")], "cost_centers"),
    "bank": ("banks", "حساب بانکی", [("SELECT COUNT(*) FROM tr_lines WHERE bank_id=?", "ردیف خزانه"),
                                    ("SELECT COUNT(*) FROM tr_docs WHERE from_bank=? OR to_bank=?", "انتقال وجه"),
                                    ("SELECT COUNT(*) FROM cheques WHERE bank_id=?", "چک"),
                                    ("SELECT COUNT(*) FROM cheque_events WHERE bank_id=?", "عملیات چک"),
                                    ("SELECT COUNT(*) FROM voucher_lines WHERE bank_id=?", "ردیف سند حسابداری")], "cash_banks"),
    "box": ("cash_boxes", "صندوق", [("SELECT COUNT(*) FROM tr_lines WHERE box_id=?", "ردیف خزانه"),
                                   ("SELECT COUNT(*) FROM tr_docs WHERE from_box=? OR to_box=?", "انتقال وجه"),
                                   ("SELECT COUNT(*) FROM cheque_events WHERE box_id=?", "عملیات چک"),
                                   ("SELECT COUNT(*) FROM voucher_lines WHERE box_id=?", "ردیف سند حسابداری")], "cash_banks"),
    "account": ("accounts", "حساب", [("SELECT COUNT(*) FROM voucher_lines WHERE account_id=?", "ردیف سند حسابداری"),
                                    ("SELECT COUNT(*) FROM tr_docs WHERE acc_id=?", "سند خزانه"),
                                    ("SELECT COUNT(*) FROM accounts WHERE parent_id=?", "زیرحساب")], "accounts"),
}
DEL_PERM = {"person": "persons", "item": "items", "group": "items", "warehouse": "setup", "cc": "setup", "account": "setup",
            "bank": "treasury", "box": "treasury"}


def usage(kind, i):
    out = []
    for sql, what in USAGE[kind][2]:
        n = v1(sql, (i,) * sql.count("?"))
        if n:
            out.append(f"{n:,} {what}")
    return out


@app.post("/delete/<kind>/<int:i>")
def master_delete(kind, i):
    if kind not in USAGE:
        abort(404)
    from core import can
    if not can(DEL_PERM[kind], "users"):
        abort(403)
    tbl, label, _, back = USAGE[kind]
    r = q(f"SELECT * FROM {tbl} WHERE id=?", (i,), one=True) or abort(404)
    nxt = safe_back(request.form.get("next"), url_for(back))
    name = r.get("name") or r.get("code")
    used = usage(kind, i)
    if kind == "account" and r["sys_key"]:
        used.insert(0, "حساب سیستمی برنامه")
    if kind == "warehouse" and v1("SELECT COUNT(*) FROM warehouses") <= 1:
        used.insert(0, "تنها انبار برنامه")
    if used:
        hint = "" if kind == "account" and r["sys_key"] else " — ابتدا آن اسناد را حذف کنید یا آن را «غیرفعال» کنید."
        flash(f"حذف {label} «{name}» ممکن نیست؛ استفاده شده در: {'، '.join(used)}{hint}", "bad")
        return redirect(nxt)
    if kind == "item":       # opening-stock receipts made by the item form go with the item
        rids = [z["receipt_id"] for z in q("SELECT DISTINCT receipt_id FROM receipt_lines WHERE item_id=?", (i,))]
        x("DELETE FROM stock_moves WHERE item_id=? AND doc_type='RES'", (i,))
        x("DELETE FROM receipt_lines WHERE item_id=?", (i,))
        for rid in rids:
            if not v1("SELECT COUNT(*) FROM receipt_lines WHERE receipt_id=?", (rid,)):
                del_moves("RES", rid)
                x("DELETE FROM receipts WHERE id=?", (rid,))
            gl.post_receipt(rid)
        if v1("SELECT COUNT(*) FROM stock_moves WHERE item_id=?", (i,)):
            db().rollback()
            flash(f"حذف کالا «{name}» ممکن نیست؛ گردش انبار دارد.", "bad")
            return redirect(nxt)
    if kind == "person":
        x("UPDATE persons SET visitor_id=NULL WHERE visitor_id=?", (i,))
    if kind in ("bank", "box") and r["is_default"]:
        x(f"UPDATE {tbl} SET is_default=1 WHERE id=(SELECT id FROM {tbl} WHERE id<>? AND active=1 ORDER BY id LIMIT 1)", (i,))
    if kind == "warehouse" and r["is_default"]:
        x("UPDATE warehouses SET is_default=1 WHERE id=(SELECT id FROM warehouses WHERE id<>? ORDER BY id LIMIT 1)", (i,))
    x(f"DELETE FROM {tbl} WHERE id=?", (i,))
    db().commit()
    flash(f"{label} «{name}» حذف شد.", "ok")
    return redirect(safe_back(request.form.get("after"), url_for(back)) if request.form.get("after") else url_for(back))


# ---------------- admin: wipe test data (TEMPORARY — remove after the user's cleanup)
DOC_TABLES = ["voucher_lines", "vouchers", "cheque_events", "tr_lines", "tr_docs", "cheques", "stock_moves", "sales_lines", "sales_invoices",
              "cons_lines", "consignments", "transfer_lines", "transfers", "purchase_lines", "purchases", "receipt_lines", "receipts"]
BASE_TABLES = ["prices", "items", "item_groups", "persons"]


@app.route("/system/reset", methods=["GET", "POST"])
def data_reset():
    counts = {t: v1(f"SELECT COUNT(*) FROM {t}") for t in DOC_TABLES + BASE_TABLES}
    if request.method == "POST":
        F = request.form
        mode = F.get("mode") if F.get("mode") in ("docs", "all") else "docs"
        u = q("SELECT * FROM users WHERE id=?", (session.get("uid"),), one=True)
        if F.get("confirm", "").strip() != "حذف همه" or F.get("ok") != "1":
            flash("عبارت «حذف همه» را دقیق وارد و تیک تایید را بزنید.", "bad")
        elif not u or not check_password_hash(u["pw"], F.get("password", "")):
            flash("رمز عبور درست نیست.", "bad")
        else:
            tables = DOC_TABLES + (BASE_TABLES if mode == "all" else [])
            x("TRUNCATE " + ", ".join(tables) + " RESTART IDENTITY CASCADE")
            if mode == "all":
                x("DELETE FROM counters")
            else:
                x("DELETE FROM counters WHERE kind NOT LIKE 'PER-%'")
            db().commit()
            flash("اسناد آزمایشی پاک شد" + (" و اشخاص، کالاها و گروه‌ها هم حذف شدند." if mode == "all" else "؛ اشخاص و کالاها باقی ماندند."), "ok")
            return redirect(url_for("dashboard"))
    return render_template("data_reset.html", counts=counts)
