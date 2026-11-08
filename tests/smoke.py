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
print("SMOKE OK")
