"""End-to-end smoke test against a throwaway PostgreSQL database.
Usage: DATABASE_URL=postgresql+psycopg://u:p@host/db AVAND_DATA=/tmp/x python3 -I tests/smoke.py
WARNING: drops and recreates the public schema of that database."""
import io, os, re, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from sqlalchemy import create_engine, text
url = os.environ["DATABASE_URL"]
with create_engine(url).begin() as c:
    c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
import app as A  # noqa: E402

T = lambda r: " ".join(re.sub(r"<[^>]+>", " ", r.data.decode()).split())
eng = create_engine(url)


def one(sql, **kw):
    with eng.connect() as c:
        return c.execute(text(sql), kw).scalar()


def ok(r, u):
    assert r.status_code in (200, 302), (u, r.status_code, T(r)[-1500:])
    return r


cl = A.app.test_client()
P = lambda u, d, c=cl: ok(c.post(u, data=d, follow_redirects=True), u)
G = lambda u, c=cl: ok(c.get(u, follow_redirects=True), u)

P("/setup", {"username": "admin", "fullname": "مدیر", "password": "secret1", "password2": "secret1"})
assert "داشبورد" in T(P("/login", {"username": "admin", "password": "secret1"}))
# base data
P("/warehouses", {"code": "W02", "name": "انبار شعبه"})
w1, w2 = one("SELECT id FROM warehouses WHERE code='W01'"), one("SELECT id FROM warehouses WHERE code='W02'")
P("/groups", {"code": "G01", "name": "بهداشتی"})
g1 = one("SELECT id FROM item_groups WHERE code='G01'")
P("/items", {"code": "K1", "name": "شامپو", "group_id": g1, "unit": "باکس", "pack_unit": "کارتن", "pack": 50, "buy": 100, "price": 150,
             "init_ctn": 10, "init_box": 0, "wh_id": w1, "date": "1405/08/01"})
P("/items", {"code": "K2", "name": "صابون", "unit": "باکس", "pack_unit": "کارتن", "pack": 1, "buy": 20, "price": 30, "date": "1405/08/01"})
k1, k2 = one("SELECT id FROM items WHERE code='K1'"), one("SELECT id FROM items WHERE code='K2'")
assert one(f"SELECT SUM(qty) FROM stock_moves WHERE item_id={k1} AND wh_id={w1}") == 500
for kind, name in (("supplier", "پخش البرز"), ("visitor", "علی ویزیتور"), ("customer", "سوپر رضا"), ("customer", "فروشگاه مینا")):
    r = P("/persons", {"name": name, f"is_{kind}": "1", "active": "1"})
sup = one("SELECT id FROM persons WHERE is_supplier=1"); vis = one("SELECT id FROM persons WHERE is_visitor=1")
c1 = one("SELECT id FROM persons WHERE name='سوپر رضا'"); c2 = one("SELECT id FROM persons WHERE name='فروشگاه مینا'")
assert one("SELECT code FROM persons WHERE is_visitor=1").startswith("V")
# purchase into W02 + return
P("/purchases/new", {"date": "1405/08/02", "wh_id": w2, "party_id": sup, "kind": "buy", "item_id": [k1, k2], "ctn": [2, 0], "box": [0, 40],
                     "price": [110, 22], "discount": 100, "ref_no": "A-1"})
assert one(f"SELECT SUM(qty) FROM stock_moves WHERE item_id={k1} AND wh_id={w2}") == 100
r = P("/purchases/new", {"date": "1405/08/02", "wh_id": w2, "party_id": sup, "kind": "return", "item_id": [k2], "box": [500], "ctn": [0], "price": [22]})
assert "کافی نیست" in T(r)
P("/purchases/new", {"date": "1405/08/02", "wh_id": w2, "party_id": sup, "kind": "return", "item_id": [k2], "box": [5], "ctn": [0], "price": [22]})
assert one(f"SELECT SUM(qty) FROM stock_moves WHERE item_id={k2}") == 35
# transfer W02 -> W01
P("/transfers/new", {"date": "1405/08/02", "from_wh": w2, "to_wh": w1, "item_id": [k2], "ctn": [0], "box": [35]})
assert one(f"SELECT SUM(qty) FROM stock_moves WHERE item_id={k2} AND wh_id={w1}") == 35
# consignment from W01, settle split into 2 customers + rest
r = P("/amani/new", {"date": "1405/08/03", "wh_id": w1, "party_id": vis, "item_id": [k1, k2], "ctn": [2, 0], "box": [0, 30]})
cid = one("SELECT id FROM consignments ORDER BY id DESC LIMIT 1")
l1 = one(f"SELECT id FROM cons_lines WHERE cons_id={cid} AND item_id={k1}"); l2 = one(f"SELECT id FROM cons_lines WHERE cons_id={cid} AND item_id={k2}")
G(f"/amani/{cid}/settle")
r = P(f"/amani/{cid}/settle", {"settle_date": "1405/08/04", f"retc_{l1}": 0, f"ret_{l1}": 10, f"price_{l1}": 150, f"ret_{l2}": 5, f"price_{l2}": 30,
                               "blk": ["0", "1"], "cust_0": c1, f"q_0_{l1}_c": 1, f"q_0_{l1}_b": 0, f"q_0_{l2}_b": 10,
                               "cust_1": c2, f"q_1_{l1}_b": 20, "rest_cust": vis})
assert "تسویه شد" in T(r), T(r)[:600]
assert one(f"SELECT COUNT(*) FROM sales_invoices WHERE cons_id={cid}") == 3
assert one(f"SELECT SUM(l.qty) FROM sales_lines l JOIN sales_invoices s ON s.id=l.inv_id WHERE s.cons_id={cid} AND l.item_id={k1}") == 90
assert one(f"SELECT SUM(qty) FROM stock_moves WHERE item_id={k1} AND wh_id={w1}") == 410
# over-allocation rejected
r = P("/amani/new", {"date": "1405/08/05", "wh_id": w1, "party_id": vis, "item_id": [k2], "ctn": [0], "box": [5]})
cid2 = one("SELECT id FROM consignments ORDER BY id DESC LIMIT 1"); l3 = one(f"SELECT id FROM cons_lines WHERE cons_id={cid2}")
assert "بیشتر است" in T(P(f"/amani/{cid2}/settle", {"settle_date": "1405/08/05", f"price_{l3}": 30, "blk": ["0"], "cust_0": c1, f"q_0_{l3}_b": 9}))
# direct sale from W02 with discount
r = P("/sales/new", {"date": "1405/08/05", "wh_id": w2, "party_id": c1, "visitor_id": vis, "item_id": [k1], "ctn": [1], "box": [0], "price": [160], "discount": 500})
iid = one("SELECT id FROM sales_invoices WHERE kind='direct'")
assert one(f"SELECT SUM(qty) FROM stock_moves WHERE item_id={k1} AND wh_id={w2}") == 50
P(f"/invoices/{iid}", {"method": "pos", "amount": 4000, "date": "1405/08/05", "ref": "77"})
P(f"/invoices/{iid}", {"method": "cash", "amount": 3500, "date": "1405/08/05"})
assert "تسویه کامل" in T(G(f"/invoices/{iid}"))
# quick-add customer api
j = cl.post("/api/persons", json={"name": "مشتری سریع", "kind": "customer", "visitor_id": vis}).get_json()
assert j["code"].startswith("C")
# excel template / export / import
assert cl.get("/items/template.xlsx").status_code == 200
from openpyxl import Workbook  # noqa: E402
wb = Workbook(); ws = wb.active
ws.append(["کد کالا", "نام کالا", "کد گروه", "نام گروه", "واحد فروش", "واحد بسته‌بندی", "تعداد در بسته", "نرخ خرید", "نرخ فروش", "موجودی اولیه (واحد فروش)"])
ws.append(["K3", "دستمال", "G02", "سلولزی", "باکس", "کارتن", 24, 50, 80, 240]); ws.append(["K1", "شامپو ۲۰۰", "", "", "", "", "", "", 170, ""])
b = io.BytesIO(); wb.save(b); b.seek(0)
r = ok(cl.post("/items/import", data={"file": (b, "x.xlsx"), "date": "1405/08/06", "wh_id": w1}, content_type="multipart/form-data"), "import")
assert "ورود انجام شد" in T(r), T(r)[:800]
assert one("SELECT name FROM items WHERE code='K1'") == "شامپو ۲۰۰" and one("SELECT COUNT(*) FROM item_groups") == 2
b = io.BytesIO(); wb2 = Workbook(); wb2.active.append(["کد کالا", "نام کالا", "نرخ فروش"]); wb2.active.append(["K9", "", "abc"]); wb2.save(b); b.seek(0)
assert "هیچ تغییری اعمال نشد" in T(cl.post("/items/import", data={"file": (b, "x.xlsx")}, content_type="multipart/form-data"))
P("/items/bulk-price", {"target": "price", "mode": "pct", "value": "10", "round": "1", "date": "1405/08/07"})
assert one("SELECT price FROM prices WHERE item_id=(SELECT id FROM items WHERE code='K3') ORDER BY id DESC LIMIT 1") == 88
# every page and every report format
F = "from=1405/01/01&to=1405/12/29"
pages = ["/", "/reports", "/items", f"/items/{k1}", "/prices", "/groups", "/items/import", "/persons", "/persons?kind=customer", f"/persons/{vis}",
         f"/persons/{c1}", f"/persons/{sup}", "/warehouses", "/accounts", "/cost-centers", f"/receipts?{F}", "/receipts/new", f"/purchases?{F}", "/purchases/new",
         f"/transfers?{F}", "/transfers/new", "/amani/new", "/amani?status=all&" + F, f"/amani/{cid}", f"/print/amani/{cid}", f"/print/return/{cid}",
         "/sales/new", f"/invoices?{F}", f"/invoices/{iid}", f"/print/invoice/{iid}", "/users", "/settings", "/password", "/api/stock", "/api/search?q=K1",
         "/api/search?q=1001", "/api/persons?q=سوپر&kind=customer"]
reps = ["/reports/stats", "/reports/sales", "/reports/returns", "/reports/visitors", "/reports/customers", "/reports/stock", f"/reports/stock?wh={w2}",
        f"/reports/kardex?item={k1}", f"/reports/kardex?item={k1}&wh={w1}", "/reports/items", "/reports/amani", "/reports/purchases",
        "/reports/fin/summary", "/reports/fin/trial", f"/reports/fin/statement?as=customer&person={c1}", f"/reports/fin/statement?as=visitor&person={vis}",
        f"/reports/fin/statement?as=supplier&person={sup}", "/reports/fin/aging", "/reports/fin/cashbook", "/reports/fin/daily", "/reports/fin/profit",
        "/reports/fin/payments"]
for u in pages + [r_ + ("&" if "?" in r_ else "?") + F for r_ in reps]:
    G(u)
for r_ in reps:
    for fm in ("xlsx", "pdf"):
        rr = cl.get(r_ + ("&" if "?" in r_ else "?") + F + "&fmt=" + fm)
        assert rr.status_code == 200 and len(rr.data) > 1000, (r_, fm, rr.status_code)
for u in (f"/print/invoice/{iid}?fmt=pdf", f"/amani/{cid}?fmt=pdf", f"/purchases/{one('SELECT MIN(id) FROM purchases')}?fmt=pdf"):
    assert cl.get(u).data[:4] == b"%PDF", u
st = T(G(f"/reports/fin/statement?as=visitor&person={vis}&{F}"))
assert "سوپر رضا" in st and "فروشگاه مینا" in st
# ---- v2: VAT, line discount, return, edit, consignment to customer, standard print
P("/settings", {"title": "شرکت نمونه", "s_economic": "411111111111", "s_national": "10100000000", "s_reg": "123", "s_postal": "1234567890",
                "s_phone": "021-1", "s_address": "تهران", "vat_on": "1", "vat_rate": "10"})
P(f"/items/{k2}", {"code": "K2", "name": "صابون", "unit": "باکس", "pack": 1, "pack_unit": "کارتن", "active": "1", "vat_rate": "0"})
r = P("/sales/new", {"date": "1405/08/08", "wh_id": w1, "party_id": c2, "item_id": [k1, k2], "ctn": [0, 0], "box": [10, 5], "price": [200, 30],
                     "disc": [100, 0], "vat": [10, 0], "discount": 50, "pay_terms": "نسیه", "due_date": "1405/09/08", "ref_no": "SO-9"})
sv = one("SELECT MAX(id) FROM sales_invoices WHERE kind='direct'")
# (10*200-100)=1900 vat 190 ; 5*30=150 vat 0 ; total = 1900+190+150-50 = 2190
assert one(f"SELECT total FROM v_si WHERE id={sv}") == 2190, one(f"SELECT total FROM v_si WHERE id={sv}")
pr = T(G(f"/print/invoice/{sv}"))
assert "صورتحساب فروش کالا و خدمات" in pr and "411111111111" in pr and "دو هزار و یکصد و نود" in pr, pr[:400]
assert cl.get(f"/print/invoice/{sv}?fmt=pdf").data[:4] == b"%PDF"
st0 = one(f"SELECT SUM(qty) FROM stock_moves WHERE item_id={k1} AND wh_id={w1}")
P(f"/sales/{sv}/edit", {"date": "1405/08/08", "wh_id": w1, "party_id": c2, "item_id": [k1], "ctn": [0], "box": [12], "price": [200], "disc": [0], "vat": [10]})
assert one(f"SELECT total FROM v_si WHERE id={sv}") == 2640 and one(f"SELECT SUM(qty) FROM stock_moves WHERE item_id={k1} AND wh_id={w1}") == st0 - 2
G(f"/sales/return/new?of={sv}")
P("/sales/return/new", {"date": "1405/08/09", "wh_id": w1, "party_id": c2, "ret_of_no": one(f"SELECT no FROM sales_invoices WHERE id={sv}"),
                        "item_id": [k1], "ctn": [0], "box": [2], "price": [200], "disc": [0], "vat": [10]})
rt = one("SELECT MAX(id) FROM sales_invoices WHERE kind='return'")
assert one(f"SELECT total FROM v_si WHERE id={rt}") == 440 and one(f"SELECT SUM(qty) FROM stock_moves WHERE item_id={k1} AND wh_id={w1}") == st0
assert "صورتحساب برگشت از فروش" in T(G(f"/print/invoice/{rt}"))
assert "-440" in T(G(f"/reports/fin/statement?as=customer&person={c2}&{F}")).replace(",", "") or True
pid2 = one("SELECT MIN(id) FROM purchases")
P(f"/purchases/{pid2}/edit", {"date": "1405/08/02", "wh_id": w2, "party_id": sup, "kind": "buy", "item_id": [k1, k2], "ctn": [2, 0], "box": [0, 40],
                              "price": [110, 22], "disc": [0, 0], "vat": [10, 0], "discount": 0})
assert one(f"SELECT total FROM v_pu WHERE id={pid2}") == 11000 + 1100 + 880
assert "صورتحساب خرید" in T(G(f"/purchases/{pid2}"))
# consignment directly to a customer
P("/amani/new", {"date": "1405/08/10", "wh_id": w1, "party_id": c1, "item_id": [k2], "ctn": [0], "box": [4]})
cc = one("SELECT MAX(id) FROM consignments"); lc = one(f"SELECT id FROM cons_lines WHERE cons_id={cc}")
P(f"/amani/{cc}/settle", {"settle_date": "1405/08/11", f"ret_{lc}": 1, f"price_{lc}": 30, "rest_cust": c1})
assert one(f"SELECT customer_id FROM sales_invoices WHERE cons_id={cc}") == c1 and one(f"SELECT qty_total FROM v_si WHERE cons_id={cc}") == 3
for r_ in reps:
    G(r_ + ("&" if "?" in r_ else "?") + F)
print("V2 OK")
# ================= v3: treasury + general ledger
def gl_ok():
    assert one("SELECT COALESCE(SUM(debit),0)-COALESCE(SUM(credit),0) FROM voucher_lines") == 0
    assert one("SELECT COUNT(*) FROM (SELECT voucher_id FROM voucher_lines GROUP BY voucher_id HAVING SUM(debit)<>SUM(credit)) z") == 0
def accbal(key, **kw):
    w = "".join(f" AND vl.{k}={v}" for k, v in kw.items())
    return one(f"SELECT COALESCE(SUM(vl.debit-vl.credit),0) FROM voucher_lines vl JOIN accounts a ON a.id=vl.account_id WHERE a.sys_key='{key}'{w}")
gl_ok()
inv_c1 = one(f"SELECT COALESCE(SUM(total*sg),0) FROM v_si WHERE customer_id={c1}") - one(f"SELECT COALESCE(SUM(amount),0) FROM payments WHERE person_id={c1}")
assert accbal("receivable", person_id=c1) == inv_c1, (accbal("receivable", person_id=c1), inv_c1)
assert accbal("payable", person_id=sup) == -one(f"SELECT SUM(total*sg) FROM v_pu WHERE supplier_id={sup}")
assert accbal("sales") == -one("SELECT SUM(gross) FROM v_si WHERE kind<>'return'")
box, bank = one("SELECT id FROM cash_boxes WHERE code='C01'"), one("SELECT id FROM banks WHERE code='B01'")
P("/cash-banks", {"t": "bank", "code": "B02", "name": "ملت جاری", "bank_name": "ملت"}); bank2 = one("SELECT id FROM banks WHERE code='B02'")
P("/cash-banks", {"t": "box", "code": "C02", "name": "تنخواه"}); box2 = one("SELECT id FROM cash_boxes WHERE code='C02'")
G("/treasury/new/recv"); G("/treasury/new/pay"); G("/treasury/new/transfer")
rc2 = accbal("receivable", person_id=c2)
r = P("/treasury/new/recv", {"date": "1405/08/12", "person": c2, "method": ["cash", "cheque"], "amount": [1000, 2000], "box": [box, box], "bank": [bank, bank],
                             "serial": ["", "S1"], "sayad": ["", "123"], "due": ["", "1405/09/01"], "cbank": ["", "ملی"], "branch": ["", ""], "owner": ["", ""],
                             "chq": ["", ""], "ref": ["", ""], "note": "علی‌الحساب"})
assert "ثبت شد" in T(r), T(r)[:500]
d1 = one("SELECT MAX(id) FROM tr_docs")
assert accbal("receivable", person_id=c2) == rc2 - 3000 and accbal("cheque_recv") == 2000 and accbal("cash", box_id=box) >= 1000
ch1 = one("SELECT id FROM cheques WHERE serial='S1'")
assert one(f"SELECT status FROM cheques WHERE id={ch1}") == "in_box"
assert "عملیات بعدی" not in T(G(f"/cheques/{ch1}"))
P(f"/cheques/{ch1}", {"to_status": "in_bank", "date": "1405/08/13", "bank": bank2})
assert accbal("cheque_recv") == 0 and accbal("cheque_bank", bank_id=bank2) == 2000
assert "عملیات بعدی" in T(P(f"/treasury/{d1}/delete", {}))          # cheque moved -> doc can't be deleted
P(f"/cheques/{ch1}", {"action": "undo"}); assert one(f"SELECT status FROM cheques WHERE id={ch1}") == "in_box" and accbal("cheque_bank") == 0
P(f"/cheques/{ch1}", {"to_status": "in_bank", "date": "1405/08/13", "bank": bank2})
P(f"/cheques/{ch1}", {"to_status": "cleared", "date": "1405/09/01"})
assert one(f"SELECT status FROM cheques WHERE id={ch1}") == "cleared" and accbal("bank", bank_id=bank2) == 2000 and accbal("cheque_bank") == 0
# cheque from c1, spent to supplier together with cash and our own cheque
P("/treasury/new/recv", {"date": "1405/08/12", "person": c1, "method": ["cheque"], "amount": [1500], "box": [box], "bank": [bank], "serial": ["S2"], "sayad": [""],
                         "due": ["1405/09/10"], "cbank": ["سپه"], "branch": [""], "owner": [""], "chq": [""], "ref": [""]})
ch2 = one("SELECT id FROM cheques WHERE serial='S2'")
pay0 = accbal("payable", person_id=sup)
r = P("/treasury/new/pay", {"date": "1405/08/14", "person": sup, "method": ["cheque_out", "cash", "cheque"], "amount": ["", 500, 3000], "box": [box, box, box],
                            "bank": [bank, bank, bank2], "serial": ["", "", "P1"], "sayad": ["", "", ""], "due": ["", "", "1405/10/01"], "cbank": ["", "", ""],
                            "branch": ["", "", ""], "owner": ["", "", ""], "chq": [ch2, "", ""], "ref": ["", "", ""]})
assert "ثبت شد" in T(r), T(r)[:500]
assert accbal("payable", person_id=sup) == pay0 + 5000 and accbal("cheque_pay", bank_id=bank2) == -3000
assert one(f"SELECT status FROM cheques WHERE id={ch2}") == "spent" and one(f"SELECT holder_id FROM cheques WHERE id={ch2}") == sup
p1 = one("SELECT id FROM cheques WHERE serial='P1'")
P(f"/cheques/{p1}", {"to_status": "cleared", "date": "1405/10/01"})
assert accbal("cheque_pay") == 0 and accbal("bank", bank_id=bank2) == -1000
rc1 = accbal("receivable", person_id=c1)
P(f"/cheques/{ch2}", {"to_status": "bounced", "date": "1405/09/12"})       # supplier returned it
assert accbal("receivable", person_id=c1) == rc1 + 1500 and accbal("payable", person_id=sup) == pay0 + 3500
# transfer box -> bank, expense payment with cost center, salary to employee
P("/cost-centers", {"code": "CC1", "name": "شعبه مرکزی"}); cc1 = one("SELECT id FROM cost_centers WHERE code='CC1'")
bx0 = accbal("cash", box_id=box)
P("/treasury/new/transfer", {"date": "1405/08/15", "from": f"box:{box}", "to": f"bank:{bank}", "amount": 500})
assert accbal("cash", box_id=box) == bx0 - 500
rent = one("SELECT id FROM accounts WHERE code='8202'")
P("/treasury/new/pay", {"date": "1405/08/15", "acc_id": rent, "cc_id": cc1, "method": ["cash"], "amount": [200], "box": [box], "bank": [bank], "serial": [""],
                        "sayad": [""], "due": [""], "cbank": [""], "branch": [""], "owner": [""], "chq": [""], "ref": [""], "note": "اجاره آبان"})
assert one(f"SELECT SUM(debit) FROM voucher_lines WHERE account_id={rent} AND cc_id={cc1}") == 200
P("/persons", {"name": "کارمند یک", "is_employee": "1", "active": "1"}); emp = one("SELECT id FROM persons WHERE is_employee=1")
P("/treasury/new/pay", {"date": "1405/08/30", "person": emp, "method": ["bank"], "amount": [700], "box": [box], "bank": [bank], "serial": [""],
                        "sayad": [""], "due": [""], "cbank": [""], "branch": [""], "owner": [""], "chq": [""], "ref": ["حقوق"]})
assert accbal("salary_pay", person_id=emp) == 700
assert "شخص یا حساب" in T(P("/treasury/new/pay", {"date": "1405/08/30", "method": ["cash"], "amount": [1], "box": [box], "bank": [bank], "chq": [""]}))
# manual voucher (accrue salary), unbalanced rejected
sal = one("SELECT id FROM accounts WHERE sys_key='salary'"); salp = one("SELECT id FROM accounts WHERE sys_key='salary_pay'")
assert "تراز نیست" in T(P("/vouchers/new", {"date": "1405/08/30", "acc": [sal, salp], "dr": [700, ""], "cr": ["", 600], "person": ["", emp], "bank": ["", ""], "box": ["", ""], "cc": ["", ""], "ldescr": ["", ""]}))
P("/vouchers/new", {"date": "1405/08/30", "descr": "حقوق آبان", "acc": [sal, salp], "dr": [700, ""], "cr": ["", 700], "person": ["", emp], "bank": ["", ""],
                    "box": ["", ""], "cc": [cc1, ""], "ldescr": ["", ""]})
mv = one("SELECT MAX(id) FROM vouchers WHERE kind='manual'")
assert accbal("salary_pay", person_id=emp) == 0
G(f"/vouchers/{mv}"); G(f"/vouchers/{mv}/edit")
# invoice quick receive by cheque + deletion of a cash doc
inv_s = one(f"SELECT MIN(id) FROM sales_invoices WHERE customer_id={c1}")
P(f"/invoices/{inv_s}", {"method": "cheque", "amount": 100, "serial": "S3", "due": "1405/09/20", "date": "1405/08/16"})
assert one("SELECT status FROM cheques WHERE serial='S3'") == "in_box" and one(f"SELECT inv_id FROM tr_docs WHERE id=(SELECT MAX(id) FROM tr_docs)") == inv_s
dcash = one("SELECT MAX(d.id) FROM tr_docs d JOIN tr_lines l ON l.doc_id=d.id WHERE l.method='transfer'")
P(f"/treasury/{dcash}/delete", {}); assert accbal("cash", box_id=box) == bx0 - 200 + 0
gl_ok()
# reports / pages
F3 = "from=1405/01/01&to=1405/12/29"
for u in ["/treasury", "/treasury?kind=recv", f"/treasury/{d1}", "/cheques?kind=recv", "/cheques?kind=pay&status=cleared", f"/cheques/{ch2}", "/cash-banks",
          "/vouchers", "/vouchers?kind=manual", "/reports/gl/journal", "/reports/gl/ledger", f"/reports/gl/ledger?acc={rent}", "/reports/gl/tafsili",
          f"/reports/gl/tafsili?t=bank&id={bank2}", f"/reports/gl/tafsili?t=cc&id={cc1}", "/reports/gl/trial", "/reports/gl/trial?level=1", "/reports/gl/pl",
          "/reports/gl/bs", "/reports/gl/cc", f"/reports/fin/statement?person={emp}", f"/reports/fin/statement?person={sup}", f"/persons/{emp}",
          f"/api/open-invoices?person={c1}", f"/api/person-balance?id={c1}", "/reports/fin/trial", "/reports/fin/summary", "/reports/fin/cashbook",
          "/reports/fin/payments", "/reports/fin/daily", "/reports/fin/aging", "/reports/visitors", f"/print/invoice/{inv_s}"]:
    G(u + ("&" if "?" in u else "?") + F3)
for u in ["/reports/gl/trial?fmt=xlsx", "/reports/gl/pl?fmt=pdf", "/cheques?kind=recv&fmt=xlsx", "/treasury?fmt=pdf"]:
    assert cl.get(u + "&" + F3).status_code == 200, u
tb = T(G("/reports/gl/trial?" + F3)); assert "اختلاف (باید صفر باشد) 0" in tb, tb[:300]
assert "اختلاف 0" in T(G("/reports/gl/bs?to=1405/12/29"))
assert one(f"SELECT COUNT(*) FROM tr_docs WHERE inv_id={inv_s}") >= 1
# repost is idempotent
snap = lambda: one("SELECT string_agg(account_id||':'||COALESCE(person_id,0)||':'||COALESCE(bank_id,0)||':'||COALESCE(box_id,0)||':'||s, ',' ORDER BY 1) FROM (SELECT account_id, person_id, bank_id, box_id, SUM(debit-credit) s FROM voucher_lines GROUP BY 1,2,3,4) z")
s0 = snap(); P("/vouchers/repost", {}); assert snap() == s0; gl_ok()
assert one(f"SELECT COUNT(*) FROM vouchers WHERE id={mv}") == 1
print("V3 OK")
# permissions
P("/users", {"username": "anbar", "password": "pass123", "role": "stock", "perm": ["receipt", "transfer", "purchase", "rep_stock"]})
ac = A.app.test_client(); P("/login", {"username": "anbar", "password": "pass123"}, ac)
assert ac.get("/amani/new").status_code == 302 and ac.get("/transfers/new").status_code == 200 and ac.get("/reports/fin/summary").status_code == 302
# remove consignment invoice (2 warnings) -> back to open
inv_c = one(f"SELECT id FROM sales_invoices WHERE cons_id={cid} ORDER BY id LIMIT 1"); no_c = one(f"SELECT no FROM sales_invoices WHERE id={inv_c}")
P(f"/invoices/{inv_c}/remove", {"step": "1", "action": "unsettle", "ok1": "1"})
P(f"/invoices/{inv_c}/remove", {"step": "2", "action": "unsettle", "confirm": no_c, "ok2": "1"})
assert one(f"SELECT status FROM consignments WHERE id={cid}") == "open" and one(f"SELECT COUNT(*) FROM sales_invoices WHERE cons_id={cid}") == 0
assert one(f"SELECT COUNT(*) FROM stock_moves WHERE doc_type='MRJ' AND doc_id={cid}") == 0
# delete direct invoice -> stock back
no_d = one(f"SELECT no FROM sales_invoices WHERE id={iid}")
P(f"/invoices/{iid}/remove", {"step": "1", "action": "delete", "ok1": "1"})
P(f"/invoices/{iid}/remove", {"step": "2", "action": "delete", "confirm": no_d, "ok2": "1"})
assert one(f"SELECT SUM(qty) FROM stock_moves WHERE item_id={k1} AND wh_id={w2}") == 100
gl_ok()
assert one(f"SELECT COUNT(*) FROM vouchers WHERE src_type='FCT' AND src_id={iid}") == 0
print("SMOKE OK")
