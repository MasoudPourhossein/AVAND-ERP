import os, datetime, secrets, time
from urllib.parse import urlparse
from flask import Flask, g, request, redirect, url_for, flash, abort, session
from werkzeug.middleware.proxy_fix import ProxyFix
from jalali import today_j, month_start_j, norm_date
import dbl as D
from dbl import q, v1, x

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("AVAND_DATA", os.path.join(BASE, "..", "data"))
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
FONT_DIR = os.path.join(BASE, "static", "fonts")
DEFAULT_TITLE = os.environ.get("AVAND_COMPANY", "آوند ERP")
LOGO_PATH = os.path.join(DATA_DIR, "logo.png")
POWERED = "Powered by Masoud Pourhossein"
os.makedirs(BACKUP_DIR, exist_ok=True)


def _secret():
    if os.environ.get("AVAND_SECRET"):
        return os.environ["AVAND_SECRET"]
    p = os.path.join(DATA_DIR, ".secret_key")
    try:
        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32))
    except FileExistsError:
        pass
    for _ in range(50):
        k = open(p).read().strip()
        if k:
            return k
        time.sleep(0.1)
    raise RuntimeError("secret key file is empty")


app = Flask(__name__)
app.secret_key = _secret()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.environ.get("AVAND_HTTPS") == "1",
                  PERMANENT_SESSION_LIFETIME=datetime.timedelta(hours=12), MAX_CONTENT_LENGTH=512 * 1024 * 1024)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.teardown_appcontext(D.close)


class _DBShim:
    commit = staticmethod(D.commit)
    rollback = staticmethod(D.rollback)


def db():
    return _DBShim


DOC = {"RES": "رسید انبار", "PUR": "فاکتور خرید", "PRT": "برگشت از خرید", "TRF": "حواله بین انبار", "AMN": "فاکتور امانی",
       "FCT": "فاکتور فروش", "SRT": "برگشت از فروش", "MRJ": "برگه مرجوعی", "DRY": "سند دریافت"}
PAY_TERMS = ["نقدی", "نسیه", "چک", "کارت‌خوان", "اقساطی", "نقد و نسیه"]
METHOD = {"pos": "کارت‌خوان (پوز)", "cash": "نقد", "bank": "واریز / حواله بانکی", "cheque": "چک"}
PERSON_KINDS = {"customer": "مشتری", "supplier": "تامین‌کننده", "visitor": "ویزیتور", "employee": "کارمند", "other": "سایر"}

# ---------------- permissions
PERMS = {
    "amani": "صدور فاکتور امانی",
    "settle": "تسویه امانی و صدور فاکتور قطعی",
    "sale": "فاکتور فروش مستقیم",
    "payment": "ثبت دریافتی روی فاکتور فروش",
    "treasury": "خزانه: دریافت، پرداخت، چک، بانک و صندوق",
    "accounting": "حسابداری: سند دستی و دفاتر",
    "purchase": "خرید از تامین‌کننده",
    "receipt": "رسید انبار",
    "transfer": "حواله بین انبار",
    "items": "کالا، گروه، نرخ و ورود اکسل",
    "persons": "اشخاص (مشتری، ویزیتور، تامین‌کننده...)",
    "setup": "انبارها، کدینگ حساب و مراکز هزینه",
    "rep_stock": "گزارش‌های انبار",
    "rep_sales": "گزارش‌ها و آمار فروش",
    "rep_fin": "گزارش‌های مالی",
    "void": "ویرایش، حذف و ابطال اسناد",
    "users": "مدیریت کاربران، تنظیمات و پشتیبان",
}
PRESETS = {
    "admin": ("مدیر کل", list(PERMS)),
    "sales": ("کاربر فروش", ["amani", "settle", "sale", "payment", "persons", "rep_sales", "rep_stock"]),
    "stock": ("انباردار", ["receipt", "transfer", "purchase", "rep_stock"]),
    "acct": ("حسابدار", ["payment", "treasury", "accounting", "purchase", "persons", "rep_sales", "rep_fin", "rep_stock"]),
    "viewer": ("فقط مشاهده گزارش", ["rep_stock", "rep_sales", "rep_fin"]),
}
LEGACY_PERM = {"base": ["items", "persons", "setup"]}

# endpoint -> (GET: any-of perms or None=any user, POST: any-of perms)
ACCESS = {
    "dashboard": (None, None), "reports": (None, None), "password": (None, None), "logout": (None, None),
    "api_search": (None, None), "api_stock": (["rep_stock"], []), "api_persons": (None, ["persons", "settle", "sale", "amani"]),
    "items": (["items", "rep_stock"], ["items"]), "item_edit": (["items", "rep_stock"], ["items"]),
    "items_import": (["items"], ["items"]), "items_template": (["items"], []), "items_export": (["items", "rep_stock"], []),
    "items_bulk_price": (["items"], ["items"]), "groups": (["items"], ["items"]),
    "prices": (["items"], ["items"]), "price_delete": (["items"], ["items"]),
    "persons": (["persons", "rep_fin", "rep_sales"], ["persons"]), "person": (["persons", "rep_fin", "rep_sales"], ["persons"]),
    "warehouses": (["setup", "rep_stock"], ["setup"]), "accounts": (["setup", "rep_fin"], ["setup"]),
    "cost_centers": (["setup", "rep_fin"], ["setup"]),
    "receipts": (["receipt", "rep_stock"], []), "receipt_view": (["receipt", "rep_stock"], []),
    "receipt_new": (["receipt"], ["receipt"]), "receipt_delete": (["void"], ["void"]),
    "purchases": (["purchase", "rep_stock", "rep_fin"], []), "purchase_new": (["purchase"], ["purchase"]),
    "purchase_view": (["purchase", "rep_stock", "rep_fin"], []), "purchase_delete": (["void"], ["void"]),
    "transfers": (["transfer", "rep_stock"], []), "transfer_new": (["transfer"], ["transfer"]),
    "transfer_view": (["transfer", "rep_stock"], []), "transfer_delete": (["void"], ["void"]),
    "amani_list": (["amani", "settle", "rep_sales"], []), "amani_view": (["amani", "settle", "rep_sales"], []),
    "amani_new": (["amani"], ["amani"]), "amani_settle": (["settle"], ["settle"]),
    "amani_delete": (["void"], ["void"]), "print_amani": (["amani", "settle", "rep_sales"], []),
    "print_return": (["settle", "rep_sales", "rep_stock"], []),
    "sale_new": (["sale"], ["sale"]), "sale_return_new": (["sale"], ["sale"]), "sale_edit": (["void"], ["void"]),
    "purchase_edit": (["void"], ["void"]),
    "invoices": (["payment", "settle", "sale", "rep_sales", "rep_fin"], []),
    "invoice": (["payment", "settle", "sale", "rep_sales", "rep_fin"], ["payment"]),
    "print_invoice": (["payment", "settle", "sale", "rep_sales", "rep_fin"], []),
    "invoice_remove": (["users"], ["users"]), "payment_delete": (["void"], ["void"]),
    "users": (["users"], ["users"]), "user_edit": (["users"], ["users"]), "user_delete": (["users"], ["users"]),
    "backup": (["users"], []), "backups": (["users"], ["users"]), "backup_file": (["users"], []),
    "backup_delete": (["users"], ["users"]), "supplier_recode": (["items"], ["items"]), "backup_restore": (["users"], ["users"]), "settings": (["users"], ["users"]),
    "tr_list": (["treasury", "rep_fin"], []), "tr_new": (["treasury"], ["treasury"]), "tr_view": (["treasury", "rep_fin", "payment"], []),
    "tr_delete": (["void"], ["void"]), "inv_receive": (["payment", "treasury"], ["payment", "treasury"]),
    "cheques": (["treasury", "rep_fin"], []), "cheque_view": (["treasury", "rep_fin"], ["treasury"]),
    "cash_banks": (["treasury", "setup", "rep_fin"], ["treasury", "setup"]), "api_open_invoices": (["treasury", "payment"], []),
    "api_person_balance": (["treasury", "payment", "rep_fin", "persons"], []),
    "vouchers": (["accounting", "rep_fin"], []), "voucher_view": (["accounting", "rep_fin"], []), "voucher_new": (["accounting"], ["accounting"]),
    "voucher_edit": (["accounting"], ["accounting"]), "voucher_delete": (["void"], ["void"]), "repost": (["users"], ["users"]),
    "master_delete": ([], ["persons", "items", "setup", "treasury", "users"])
}


def perms_of(u):
    return set(filter(None, (u["perms"] or "").split(","))) if u else set()


def can(*ps):
    u = g.get("user")
    return bool(u) and bool(perms_of(u) & set(ps))


def register_report(endpoint, perm):
    ACCESS[endpoint] = ([perm], [])


def next_no(kind):
    x("INSERT INTO counters(kind,last) VALUES(?,1000) ON CONFLICT (kind) DO NOTHING", (kind,))
    return v1("UPDATE counters SET last=last+1 WHERE kind=? RETURNING last", (kind,))


# ---------------- schema + seed
CHART = [  # code, name, level, parent, nature, sys_key
    ("1", "دارایی‌های جاری", 1, None, "D", None),
    ("11", "موجودی نقد و بانک", 2, "1", "D", None),
    ("1101", "صندوق", 3, "11", "D", "cash"), ("1102", "بانک‌ها", 3, "11", "D", "bank"), ("1103", "تنخواه‌گردان", 3, "11", "D", "petty"),
    ("12", "حساب‌ها و اسناد دریافتنی تجاری", 2, "1", "D", None),
    ("1201", "حساب‌های دریافتنی (مشتریان)", 3, "12", "D", "receivable"), ("1202", "اسناد دریافتنی (چک)", 3, "12", "D", "cheque_recv"),
    ("1203", "اسناد در جریان وصول", 3, "12", "D", "cheque_bank"),
    ("13", "موجودی کالا", 2, "1", "D", None),
    ("1301", "موجودی کالای انبار", 3, "13", "D", "inventory"), ("1302", "کالای امانی نزد ویزیتورها", 3, "13", "D", "consign"),
    ("14", "پیش‌پرداخت‌ها", 2, "1", "D", None), ("1401", "پیش‌پرداخت خرید", 3, "14", "D", "prepay_buy"),
    ("15", "سایر حساب‌های دریافتنی", 2, "1", "D", None), ("1501", "مساعده و وام کارکنان", 3, "15", "D", "emp_recv"),
    ("2", "دارایی‌های غیرجاری", 1, None, "D", None),
    ("21", "دارایی‌های ثابت", 2, "2", "D", None), ("2101", "اثاثه و تجهیزات", 3, "21", "D", None), ("2102", "وسایط نقلیه", 3, "21", "D", None),
    ("3", "بدهی‌های جاری", 1, None, "C", None),
    ("31", "حساب‌ها و اسناد پرداختنی تجاری", 2, "3", "C", None),
    ("3101", "حساب‌های پرداختنی (تامین‌کنندگان)", 3, "31", "C", "payable"), ("3102", "اسناد پرداختنی (چک)", 3, "31", "C", "cheque_pay"),
    ("32", "سایر حساب‌های پرداختنی", 2, "3", "C", None),
    ("3201", "حقوق و دستمزد پرداختنی", 3, "32", "C", "salary_pay"), ("3202", "مالیات بر ارزش افزوده", 3, "32", "C", "vat"),
    ("33", "پیش‌دریافت‌ها", 2, "3", "C", None), ("3301", "پیش‌دریافت فروش", 3, "33", "C", "prepay_sale"),
    ("5", "حقوق مالکانه", 1, None, "C", None),
    ("51", "سرمایه", 2, "5", "C", None), ("5101", "سرمایه", 3, "51", "C", "capital"),
    ("52", "سود و زیان", 2, "5", "C", None), ("5201", "سود و زیان انباشته", 3, "52", "C", "retained"),
    ("5202", "حساب جاری شرکا", 3, "52", "C", "partners"),
    ("6", "درآمدها", 1, None, "C", None),
    ("61", "فروش", 2, "6", "C", None), ("6101", "فروش کالا", 3, "61", "C", "sales"),
    ("6102", "برگشت از فروش", 3, "61", "D", "sales_ret"), ("6103", "تخفیفات فروش", 3, "61", "D", "sales_disc"),
    ("62", "سایر درآمدها", 2, "6", "C", None), ("6201", "سایر درآمدها", 3, "62", "C", "other_income"),
    ("7", "بهای تمام‌شده", 1, None, "D", None),
    ("71", "بهای تمام‌شده کالای فروش‌رفته", 2, "7", "D", None), ("7101", "بهای تمام‌شده کالای فروش‌رفته", 3, "71", "D", "cogs"),
    ("8", "هزینه‌ها", 1, None, "D", None),
    ("81", "هزینه‌های فروش و توزیع", 2, "8", "D", None), ("8101", "پورسانت ویزیتورها", 3, "81", "D", "commission"),
    ("8102", "هزینه حمل و نقل", 3, "81", "D", None),
    ("82", "هزینه‌های اداری و عمومی", 2, "8", "D", None), ("8201", "حقوق و دستمزد", 3, "82", "D", "salary"),
    ("8202", "اجاره", 3, "82", "D", None), ("8203", "آب، برق، گاز و تلفن", 3, "82", "D", None),
    ("8204", "کارمزد بانکی", 3, "82", "D", "bank_fee"),
    ("83", "سایر هزینه‌ها", 2, "8", "D", None), ("8301", "کسری و اضافات انبار", 3, "83", "D", "stock_diff"),
]
SCHEMA_VERSION = 3


def init_db():
    with app.app_context():
        sql = open(os.path.join(BASE, "schema.sql"), encoding="utf-8").read()
        c = D.conn()
        c.exec_driver_sql("SELECT pg_advisory_lock(424243)")
        try:
            if v1("SELECT COUNT(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relname='payments' AND c.relkind='r' AND n.nspname='public'"):
                c.exec_driver_sql("ALTER TABLE payments RENAME TO payments_legacy")
            for stmt in [s for s in sql.split(";") if s.strip()]:
                c.exec_driver_sql(stmt)
            if not v1("SELECT COUNT(*) FROM cash_boxes"):
                x("INSERT INTO cash_boxes(code,name,is_default) VALUES('C01','صندوق مرکزی',1)")
            if not v1("SELECT COUNT(*) FROM banks"):
                x("INSERT INTO banks(code,name,is_pos,is_default) VALUES('B01','حساب بانکی / کارت‌خوان',1,1)")
            if not v1("SELECT COUNT(*) FROM warehouses"):
                x("INSERT INTO warehouses(code,name,is_default) VALUES('W01','انبار مرکزی',1)")
            ids = {r["code"]: r["id"] for r in q("SELECT id, code FROM accounts")}
            for code, name, lvl, parent, nat, key in CHART:   # seed / complete the standard chart (keeps user edits)
                if key and v1("SELECT COUNT(*) FROM accounts WHERE sys_key=?", (key,)):
                    continue
                if code in ids:
                    if key:
                        x("UPDATE accounts SET sys_key=? WHERE id=? AND sys_key IS NULL", (key, ids[code]))
                    continue
                ids[code] = x("INSERT INTO accounts(code,name,level,parent_id,nature,sys_key) VALUES(?,?,?,?,?,?)",
                              (code, name, lvl, ids.get(parent), nat, key))
            if v1("SELECT COUNT(*) FROM pg_class WHERE relname='payments_legacy'"):
                box, bank = default_box(), default_bank()
                for p in q("""SELECT p.*, s.customer_id, s.visitor_id FROM payments_legacy p JOIN sales_invoices s ON s.id=p.inv_id ORDER BY p.id"""):
                    did = x("INSERT INTO tr_docs(no,kind,date,person_id,inv_id,visitor_id,created_by) VALUES(?,'recv',?,?,?,?,?)",
                            (p["no"], p["date"], p["customer_id"], p["inv_id"], p["visitor_id"], p["created_by"]))
                    x("INSERT INTO tr_lines(doc_id,method,amount,box_id,bank_id,ref) VALUES(?,?,?,?,?,?)",
                      (did, p["method"], p["amount"], box if p["method"] == "cash" else None, bank if p["method"] != "cash" else None, p["ref"]))
                c.exec_driver_sql("DROP TABLE payments_legacy")
            x("""INSERT INTO counters(kind,last) SELECT 'TRD', COALESCE(MAX(no),1000) FROM tr_docs
                 ON CONFLICT(kind) DO UPDATE SET last=GREATEST(counters.last, excluded.last)""")
            ver = v1("SELECT MAX(version) FROM schema_info") or 0
            if ver < SCHEMA_VERSION:
                if ver:
                    import gl
                    gl.repost_all()
                x("INSERT INTO schema_info(version) VALUES(?)", (SCHEMA_VERSION,))
            # legacy permission names
            for r in q("SELECT id, perms FROM users WHERE perms LIKE '%base%'"):
                ps = set(filter(None, r["perms"].split(",")))
                for old, new in LEGACY_PERM.items():
                    if old in ps:
                        ps.discard(old); ps.update(new)
                if "users" in ps:
                    ps = set(PERMS)
                x("UPDATE users SET perms=? WHERE id=?", (",".join(sorted(ps)), r["id"]))
            for r in q("SELECT id, perms FROM users WHERE perms LIKE '%users%'"):   # admins get every (new) permission
                ps = set(filter(None, r["perms"].split(",")))
                if ps != set(PERMS) and "users" in ps:
                    x("UPDATE users SET perms=? WHERE id=?", (",".join(sorted(set(PERMS))), r["id"]))
            D.commit()
        except Exception:
            D.rollback()
            raise
        finally:
            c.exec_driver_sql("SELECT pg_advisory_unlock(424243)")
            D.commit()
            D.close()


def setting(key, default=None):
    r = q("SELECT value FROM settings WHERE key=?", (key,), one=True)
    return r["value"] if r and r["value"] is not None else default


def set_setting(key, value):
    x("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def company():
    return setting("title", DEFAULT_TITLE)


def logo_file():
    return LOGO_PATH if os.path.exists(LOGO_PATH) else None


def to_int(v, default=0):
    try:
        return int(str(v).replace(",", "").replace("٬", "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")))
    except (TypeError, ValueError):
        return default


def uid():
    return g.user["id"] if g.get("user") else None


def uname(i):
    r = q("SELECT COALESCE(NULLIF(fullname,''),username) n FROM users WHERE id=?", (i,), one=True) if i else None
    return r["n"] if r else None


def default_box():
    return v1("SELECT id FROM cash_boxes WHERE active=1 ORDER BY is_default DESC, id LIMIT 1") or None


def default_bank():
    return v1("SELECT id FROM banks WHERE active=1 ORDER BY is_default DESC, id LIMIT 1") or None


def default_wh():
    return v1("SELECT id FROM warehouses WHERE is_default=1 AND active=1 ORDER BY id LIMIT 1") or v1("SELECT id FROM warehouses ORDER BY id LIMIT 1")


def warehouses_list(active_only=True):
    return q("SELECT * FROM warehouses" + (" WHERE active=1" if active_only else "") + " ORDER BY is_default DESC, code")


def price_at(item_id, date):
    r = q("SELECT price FROM prices WHERE item_id=? AND date<=? AND price>0 ORDER BY date DESC, id DESC LIMIT 1", (item_id, date), one=True)
    return r["price"] if r else 0


AVG_COST = ("COALESCE((SELECT SUM(qty*cost)/NULLIF(SUM(qty),0) FROM stock_moves WHERE item_id=i.id AND qty>0 AND cost>0 "
            "AND doc_type IN ('RES','PUR')),0)")
BUY_AT = "(SELECT buy FROM prices WHERE item_id={i} AND date<={d} AND buy>0 ORDER BY date DESC,id DESC LIMIT 1)"


def buy_at(item_id, date):
    """purchase price effective at date; fallback: weighted average of receipt/purchase costs"""
    r = q("SELECT buy FROM prices WHERE item_id=? AND date<=? AND buy>0 ORDER BY date DESC, id DESC LIMIT 1", (item_id, date), one=True)
    if r:
        return r["buy"]
    return v1("""SELECT SUM(qty*cost)/NULLIF(SUM(qty),0) FROM stock_moves WHERE item_id=? AND qty>0 AND cost>0
                 AND doc_type IN ('RES','PUR')""", (item_id,))


def stock_rows(date=None, wh_id=None):
    """per item: st (stock in warehouse(s)), open_amani, price, cost, buy"""
    date = date or today_j()
    wf, wa = ("AND wh_id=?", [wh_id]) if wh_id else ("", [])
    return q(f"""
      SELECT i.*, gr.name AS group_name, sp.code AS sup_code, sp.name AS sup_name,
       COALESCE((SELECT SUM(qty) FROM stock_moves WHERE item_id=i.id {wf}),0) AS st,
       COALESCE((SELECT SUM(cl.qty) FROM cons_lines cl JOIN consignments c ON c.id=cl.cons_id
                 WHERE cl.item_id=i.id AND c.status='open' {wf.replace('wh_id', 'c.wh_id')}),0) AS open_amani,
       COALESCE((SELECT price FROM prices WHERE item_id=i.id AND date<=? AND price>0 ORDER BY date DESC,id DESC LIMIT 1),0) AS price,
       COALESCE({BUY_AT.format(i="i.id", d="?")},{AVG_COST}) AS cost,
       COALESCE({BUY_AT.format(i="i.id", d="?")},0) AS buy
      FROM items i LEFT JOIN item_groups gr ON gr.id=i.group_id LEFT JOIN persons sp ON sp.id=i.supplier_id ORDER BY i.code""", wa + wa + [date, date, date])


def stock_of(item_id, wh_id):
    return v1("SELECT SUM(qty) FROM stock_moves WHERE item_id=? AND wh_id=?", (item_id, wh_id))


def add_moves(doc_type, doc_id, doc_no, date, rows):
    """rows: (item_id, wh_id, qty_signed, cost)"""
    for item_id, wh_id, qty, cost in rows:
        if qty:
            x("INSERT INTO stock_moves(date,item_id,wh_id,qty,cost,doc_type,doc_id,doc_no) VALUES(?,?,?,?,?,?,?,?)",
              (date, item_id, wh_id, qty, cost or 0, doc_type, doc_id, doc_no))


def del_moves(doc_type, doc_id):
    x("DELETE FROM stock_moves WHERE doc_type=? AND doc_id=?", (doc_type, doc_id))


def check_stock(needs, wh_id):
    """needs: {item_id: qty}. raises ValueError if any item is short in wh"""
    for i, qt in needs.items():
        s = stock_of(i, wh_id)
        if qt > s:
            it = q("SELECT name, pack, unit, pack_unit FROM items WHERE id=?", (i,), one=True)
            raise ValueError(f"موجودی «{it['name']}» در این انبار کافی نیست (موجودی: {pkr(s, it)}).")


INV_SQL = """
 SELECT s.*, s.no AS final_no, s.date AS settle_date, cu.name cname, cu.code ccode, v.name vname, v.code vcode, w.name whname,
  s.qty_total AS sold_qty,
  COALESCE((SELECT SUM(amount) FROM payments WHERE inv_id=s.id AND method='pos'),0) AS pos,
  COALESCE((SELECT SUM(amount) FROM payments WHERE inv_id=s.id AND method='cash'),0) AS cash,
  COALESCE((SELECT SUM(amount) FROM payments WHERE inv_id=s.id AND method='bank'),0) AS bank,
  COALESCE((SELECT SUM(amount) FROM payments WHERE inv_id=s.id AND method='cheque'),0) AS chq,
  COALESCE((SELECT SUM(amount) FROM payments WHERE inv_id=s.id),0) AS paid,
  c.no AS cons_no, c.return_no, r.no AS ret_of_no
 FROM v_si s JOIN persons cu ON cu.id=s.customer_id LEFT JOIN persons v ON v.id=s.visitor_id
 JOIN warehouses w ON w.id=s.wh_id LEFT JOIN consignments c ON c.id=s.cons_id LEFT JOIN sales_invoices r ON r.id=s.ret_of
"""
CONS_SQL = """
 SELECT c.*, v.name vname, v.code vcode, w.name whname,
  COALESCE((SELECT SUM(qty) FROM cons_lines WHERE cons_id=c.id),0) qty,
  COALESCE((SELECT SUM(returned) FROM cons_lines WHERE cons_id=c.id),0) ret_qty,
  COALESCE((SELECT SUM(qty-returned) FROM cons_lines WHERE cons_id=c.id),0) sold_qty,
  COALESCE((SELECT SUM(qty*price) FROM cons_lines WHERE cons_id=c.id),0) amani_value,
  COALESCE((SELECT SUM((qty-returned)*price) FROM cons_lines WHERE cons_id=c.id),0) sold_value
 FROM consignments c JOIN persons v ON v.id=c.visitor_id JOIN warehouses w ON w.id=c.wh_id
"""
# receivable balance helpers: invoice total minus payments
# signed receivable effect of each sales doc (returns negative)
CUST_BAL = "(SELECT COALESCE(SUM(debit-credit),0) FROM voucher_lines WHERE person_id={p})"
SUP_BAL = "COALESCE((SELECT SUM(total*sg) FROM v_pu u WHERE u.supplier_id={p}),0)"   # purchased (not balance)


def vat_default():
    return float(setting("vat_rate", "10") or 0) if setting("vat_on", "0") == "1" else 0.0


def vat_of(item_vat_rate):
    """effective VAT % for an item row value (None = company default)"""
    if setting("vat_on", "0") != "1":
        return 0.0
    return float(item_vat_rate) if item_vat_rate is not None else vat_default()


SELLER_KEYS = [("s_economic", "کد اقتصادی"), ("s_national", "شناسه ملی"), ("s_reg", "شماره ثبت"), ("s_postal", "کد پستی"),
               ("s_phone", "تلفن"), ("s_address", "نشانی")]


def seller_info():
    return {k: setting(k, "") for k, _ in SELLER_KEYS}


_ONES = ["", "یک", "دو", "سه", "چهار", "پنج", "شش", "هفت", "هشت", "نه"]
_TEENS = ["ده", "یازده", "دوازده", "سیزده", "چهارده", "پانزده", "شانزده", "هفده", "هجده", "نوزده"]
_TENS = ["", "", "بیست", "سی", "چهل", "پنجاه", "شصت", "هفتاد", "هشتاد", "نود"]
_HUND = ["", "یکصد", "دویست", "سیصد", "چهارصد", "پانصد", "ششصد", "هفتصد", "هشتصد", "نهصد"]
_SCALE = ["", "هزار", "میلیون", "میلیارد", "هزار میلیارد", "میلیون میلیارد"]


def _three(n):
    h, r = divmod(n, 100)
    parts = [_HUND[h]] if h else []
    if 10 <= r < 20:
        parts.append(_TEENS[r - 10])
    else:
        t, o = divmod(r, 10)
        if t:
            parts.append(_TENS[t])
        if o:
            parts.append(_ONES[o])
    return " و ".join(parts)


def num_words(n):
    """Persian words for an integer: 1250000 -> یک میلیون و دویست و پنجاه هزار"""
    n = int(round(n or 0))
    if n == 0:
        return "صفر"
    neg, n = n < 0, abs(n)
    groups, i = [], 0
    while n:
        n, g = divmod(n, 1000)
        if g:
            groups.append(f"{_three(g)} {_SCALE[i]}".strip())
        i += 1
    return ("منفی " if neg else "") + " و ".join(reversed(groups))


def inv_paid(inv_id):
    return v1("SELECT SUM(amount) FROM payments WHERE inv_id=?", (inv_id,))


def inv_status(r):
    if r.get("kind") == "return":
        return "برگشت از فروش", "warn"
    diff = r["total"] - r["paid"]
    if diff == 0:
        return "تسویه کامل", "ok"
    return ("کسری دریافت", "bad") if diff > 0 else ("دریافت اضافه", "warn")


def period(default_from=None):
    f = norm_date(request.args.get("from")) or default_from or month_start_j()
    t = norm_date(request.args.get("to")) or today_j()
    if f > t:
        f, t = t, f
    g.period = (f, t)
    return f, t


def money(v):
    try:
        return "{:,}".format(int(round(v or 0)))
    except (TypeError, ValueError):
        return v


app.add_template_filter(money, "m")


def pk(qty, pack=1, unit="باکس", pack_unit="کارتن"):
    """505 باکس (10 کارتن + 5)"""
    qty = int(qty or 0)
    pack = int(pack or 1)
    base = f"{qty:,} {unit or ''}".strip()
    if pack <= 1 or qty == 0:
        return base
    c, b = divmod(abs(qty), pack)
    sign = "-" if qty < 0 else ""
    part = f"{sign}{c:,} {pack_unit or 'کارتن'}" + (f" + {b:,}" if b else "")
    return f"{base} ({part})"


def pkr(qty, r):
    return pk(qty, r["pack"], r["unit"], r["pack_unit"])


app.add_template_global(pk, "pk")
app.add_template_global(pkr, "pkr")


def xurl(fmt_):
    a = request.args.to_dict()
    a["fmt"] = fmt_
    return url_for(request.endpoint, **(request.view_args or {}), **a)


def safe_back(u, default):
    return u if u and u.startswith("/") and not u.startswith("//") else default


@app.context_processor
def inject():
    u = g.get("user")
    return {"today": today_j(), "DOC": DOC, "METHOD": METHOD, "PERSON_KINDS": PERSON_KINDS, "inv_status": inv_status, "user": u,
            "PERMS": PERMS, "PRESETS": PRESETS, "PAY_TERMS": PAY_TERMS, "num_words": num_words, "can": can, "COMPANY": company(), "HAS_LOGO": bool(logo_file()),
            "LOGO_V": int(os.path.getmtime(LOGO_PATH)) if logo_file() else 0, "POWERED": POWERED, "xurl": xurl}


# ---------------- request guard
PUBLIC = {"static", "login", "setup", "logo", "health"}


@app.route("/health")
def health():
    v1("SELECT 1")
    return "ok"


@app.before_request
def guard():
    if request.method == "POST":
        o = request.headers.get("Origin")
        if o and o != "null" and urlparse(o).netloc != request.host:
            abort(403)
    ep = request.endpoint
    if ep in PUBLIC or ep is None:
        return
    if not q("SELECT 1 FROM users LIMIT 1", one=True):
        return redirect(url_for("setup"))
    u = q("SELECT * FROM users WHERE id=? AND active=1 AND deleted=0", (session.get("uid"),), one=True) if session.get("uid") else None
    if not u:
        session.clear()
        return redirect(url_for("login", next=request.full_path if request.method == "GET" else None))
    g.user = u
    get_p, post_p = ACCESS.get(ep, (["users"], ["users"]))
    need = get_p if request.method == "GET" else post_p
    if need is not None and not (perms_of(u) & set(need)):
        flash("شما به این بخش دسترسی ندارید.", "bad")
        return redirect(url_for("dashboard") if ep != "dashboard" else url_for("password"))
    if request.method == "POST":
        D.lock()
