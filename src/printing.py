"""Standard Iranian-style invoice print (sales / sales return / purchase / purchase return) — screen, print and PDF."""
from flask import render_template, request, abort, send_file
import io
from core import q, INV_SQL, company, seller_info, logo_file, FONT_DIR, num_words, uname, can, pk
from jalali import today_j

PTYPE = {"real": "حقیقی", "legal": "حقوقی"}


def _person(pid):
    p = q("SELECT * FROM persons WHERE id=?", (pid,), one=True)
    return {"name": p["name"], "code": p["code"], "ptype": PTYPE.get(p["ptype"], ""), "national_id": p["national_id"] or "",
            "economic_code": p["economic_code"] or "", "reg_no": p["reg_no"] or "", "postal_code": p["postal_code"] or "",
            "phone": p["mobile"] or p["phone"] or "", "address": p["address"] or ""}


def _company():
    s = seller_info()
    return {"name": company(), "code": "", "ptype": "حقوقی", "national_id": s["s_national"], "economic_code": s["s_economic"],
            "reg_no": s["s_reg"], "postal_code": s["s_postal"], "phone": s["s_phone"], "address": s["s_address"]}


def _lines(table, fk, did):
    out = []
    for l in q(f"""SELECT l.*, i.code, i.name, i.unit, i.pack, i.pack_unit FROM {table} l JOIN items i ON i.id=l.item_id
                   WHERE l.{fk}=? ORDER BY l.id""", (did,)):
        g = l["qty"] * l["price"]
        out.append({"code": l["code"], "name": l["name"], "qty": l["qty"], "unit": l["unit"],
                    "pack_t": pk(l["qty"], l["pack"], l["unit"], l["pack_unit"]) if (l["pack"] or 1) > 1 else "",
                    "price": l["price"], "gross": g, "disc": l["disc"], "net": g - l["disc"], "vat_rate": l["vat_rate"], "vat": l["vat"],
                    "total": g - l["disc"] + l["vat"]})
    return out


def _render(d):
    if request.args.get("fmt") == "pdf":
        try:
            from weasyprint import HTML
        except Exception:
            abort(500)
        html = render_template("std_invoice.html", d=d, pdf=True, font_dir=FONT_DIR, logo=logo_file(), now=today_j())
        data = HTML(string=html).write_pdf()
        return send_file(io.BytesIO(data), as_attachment=True, download_name=f"invoice_{d['no']}.pdf", mimetype="application/pdf")
    return render_template("std_invoice.html", d=d, pdf=False, now=today_j())


def sale_print(iid):
    s = q(INV_SQL + " WHERE s.id=?", (iid,), one=True) or abort(404)
    ret = s["kind"] == "return"
    meta = [("انبار", s["whname"])]
    if s["vname"]:
        meta.append(("ویزیتور", s["vname"]))
    if s["cons_no"]:
        meta.append(("عطف به امانی", s["cons_no"]))
    if s["ret_of_no"]:
        meta.append(("برگشت فاکتور", s["ret_of_no"]))
    if s["ref_no"]:
        meta.append(("شماره سفارش", s["ref_no"]))
    if s["pay_terms"]:
        meta.append(("نحوه پرداخت", s["pay_terms"]))
    if s["due_date"]:
        meta.append(("سررسید", s["due_date"]))
    lines = _lines("sales_lines", "inv_id", iid)
    paid = s["paid"]
    d = {"kind": "sale", "id": iid, "no": s["no"], "date": s["date"], "note": s["note"], "meta": meta, "user": uname(s["created_by"]),
         "title": "صورتحساب برگشت از فروش" if ret else "صورتحساب فروش کالا و خدمات",
         "seller": _person(s["customer_id"]) if ret else _company(), "buyer": _company() if ret else _person(s["customer_id"]),
         "seller_label": "مشخصات فروشنده (مرجوع‌کننده)" if ret else "مشخصات فروشنده", "buyer_label": "مشخصات خریدار",
         "lines": lines, "gross": s["gross"], "ldisc": s["ldisc"], "vat": s["vat_total"], "hdisc": s["discount"], "total": s["total"],
         "words": num_words(s["total"]), "paid": None if ret else (s["pos"] + s["bank"], s["cash"] + s["chq"], s["total"] - paid),
         "back": f"/invoices/{iid}", "edit": f"/sales/{iid}/edit" if can("void") and s["kind"] != "consign" else None,
         "ret": f"/sales/return/new?of={iid}" if can("sale") and not ret else None, "sign": ("مهر و امضای فروشنده", "مهر و امضای خریدار")}
    return _render(d)


def purchase_print(pid):
    u = q("""SELECT u.*, w.name whname FROM v_pu u JOIN warehouses w ON w.id=u.wh_id WHERE u.id=?""", (pid,), one=True) or abort(404)
    ret = u["kind"] == "return"
    meta = [("انبار", u["whname"])]
    if u["ref_no"]:
        meta.append(("شماره فاکتور تامین‌کننده", u["ref_no"]))
    if u["pay_terms"]:
        meta.append(("نحوه پرداخت", u["pay_terms"]))
    if u["due_date"]:
        meta.append(("سررسید", u["due_date"]))
    d = {"kind": "purchase", "id": pid, "no": u["no"], "date": u["date"], "note": u["note"], "meta": meta, "user": uname(u["created_by"]),
         "title": "صورتحساب برگشت از خرید" if ret else "صورتحساب خرید کالا و خدمات",
         "seller": _company() if ret else _person(u["supplier_id"]), "buyer": _person(u["supplier_id"]) if ret else _company(),
         "seller_label": "مشخصات فروشنده", "buyer_label": "مشخصات خریدار",
         "lines": _lines("purchase_lines", "pur_id", pid), "gross": u["gross"], "ldisc": u["ldisc"], "vat": u["vat_total"],
         "hdisc": u["discount"], "total": u["total"], "words": num_words(u["total"]), "paid": None,
         "back": "/purchases", "edit": f"/purchases/{pid}/edit" if can("void") else None, "ret": None,
         "delete": f"/purchases/{pid}/delete" if can("void") else None, "sign": ("مهر و امضای فروشنده", "مهر و امضای خریدار (انباردار)")}
    return _render(d)
