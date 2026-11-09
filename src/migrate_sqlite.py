"""One-time import of the old SQLite app (anbar.db) into AVAND ERP (PostgreSQL).
Usage: python migrate_sqlite.py /path/to/anbar.db [--force]
Runs only when the PostgreSQL database has no users (fresh install), unless --force."""
import sqlite3, sys
from core import app, init_db, q, x, v1, LEGACY_PERM, PRESETS, default_box, default_bank
import dbl as D

TABLE_SEQ = ["users", "items", "prices", "persons", "receipts", "receipt_lines", "consignments", "cons_lines", "sales_invoices",
             "sales_lines"]


def cols(sq, t):
    return {r[1] for r in sq.execute(f"PRAGMA table_info({t})")}


def main(path, force=False):
    init_db()
    sq = sqlite3.connect(path)
    sq.row_factory = sqlite3.Row
    with app.app_context():
        if v1("SELECT COUNT(*) FROM users") and not force:
            print("SKIP: database already has users (use --force on an empty database only).")
            return 0
        if v1("SELECT COUNT(*) FROM sales_invoices") or v1("SELECT COUNT(*) FROM items"):
            print("ABORT: target database already has data.")
            return 1
        wh = v1("SELECT id FROM warehouses WHERE is_default=1 ORDER BY id LIMIT 1")
        uc = cols(sq, "users")
        for u in sq.execute("SELECT * FROM users"):
            perms = set(filter(None, ((u["perms"] if "perms" in uc else "") or "").split(",")))
            if not perms:
                perms = set(PRESETS.get(u["role"], PRESETS["viewer"])[1])
            for old, new in LEGACY_PERM.items():
                if old in perms:
                    perms.discard(old); perms.update(new)
            if "users" in perms or u["role"] == "admin":
                perms = set(PRESETS["admin"][1])
            role = u["role"] if u["role"] in PRESETS else "viewer"
            x("INSERT INTO users(id,username,fullname,pw,role,perms,active,deleted,created) VALUES(?,?,?,?,?,?,?,?,?)",
              (u["id"], u["username"], u["fullname"], u["pw"], role, ",".join(sorted(perms)), u["active"],
               u["deleted"] if "deleted" in uc else 0, u["created"]))
        for s in sq.execute("SELECT * FROM settings") if "settings" in {r[0] for r in sq.execute("SELECT name FROM sqlite_master")} else []:
            x("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (s["key"], s["value"]))
        ic = cols(sq, "items")
        for i in sq.execute("SELECT * FROM items"):
            x("INSERT INTO items(id,code,name,unit,pack,pack_unit,active) VALUES(?,?,?,?,?,?,?)",
              (i["id"], i["code"], i["name"], i["unit"] or "باکس", i["pack"] if "pack" in ic else 1,
               i["pack_unit"] if "pack_unit" in ic else "کارتن", i["active"] if i["active"] is not None else 1))
        pc = cols(sq, "prices")
        for p in sq.execute("SELECT * FROM prices"):
            x("INSERT INTO prices(id,item_id,date,price,buy) VALUES(?,?,?,?,?)", (p["id"], p["item_id"], p["date"], p["price"] or 0,
                                                                                  p["buy"] if "buy" in pc else 0))
        for v in sq.execute("SELECT * FROM visitors"):
            x("INSERT INTO persons(id,code,name,is_visitor,is_customer,mobile,note,active) VALUES(?,?,?,1,1,?,?,?)",
              (v["id"], v["code"], v["name"], v["phone"], v["note"], v["active"] if v["active"] is not None else 1))
        rc, rlc = cols(sq, "receipts"), cols(sq, "receipt_lines")
        for r in sq.execute("SELECT * FROM receipts"):
            kind = "opening" if r["kind"] == "opening" else "in"
            x("INSERT INTO receipts(id,no,date,wh_id,kind,note,created_by) VALUES(?,?,?,?,?,?,?)",
              (r["id"], r["no"], r["date"], wh, kind, r["note"], r["created_by"] if "created_by" in rc else None))
            for l in sq.execute("SELECT * FROM receipt_lines WHERE receipt_id=?", (r["id"],)):
                cost = l["unit_cost"] if "unit_cost" in rlc else 0
                x("INSERT INTO receipt_lines(id,receipt_id,item_id,qty,unit_cost) VALUES(?,?,?,?,?)", (l["id"], r["id"], l["item_id"], l["qty"], cost or 0))
                x("INSERT INTO stock_moves(date,item_id,wh_id,qty,cost,doc_type,doc_id,doc_no) VALUES(?,?,?,?,?,'RES',?,?)",
                  (r["date"], l["item_id"], wh, l["qty"], cost or 0, r["id"], r["no"]))
        cc, clc = cols(sq, "consignments"), cols(sq, "cons_lines")
        inv_of = {}
        for c in sq.execute("SELECT * FROM consignments ORDER BY id"):
            x("""INSERT INTO consignments(id,no,date,visitor_id,wh_id,status,note,settle_date,return_no,created_by,settled_by)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
              (c["id"], c["no"], c["date"], c["visitor_id"], wh, c["status"], c["note"], c["settle_date"], c["return_no"],
               c["created_by"] if "created_by" in cc else None, c["settled_by"] if "settled_by" in cc else None))
            lines = sq.execute("SELECT * FROM cons_lines WHERE cons_id=? ORDER BY id", (c["id"],)).fetchall()
            for l in lines:
                x("INSERT INTO cons_lines(id,cons_id,item_id,qty,returned,price) VALUES(?,?,?,?,?,?)",
                  (l["id"], c["id"], l["item_id"], l["qty"], l["returned"], l["price"]))
                x("INSERT INTO stock_moves(date,item_id,wh_id,qty,doc_type,doc_id,doc_no) VALUES(?,?,?,?,'AMN',?,?)",
                  (c["date"], l["item_id"], wh, -l["qty"], c["id"], c["no"]))
                if c["status"] == "settled" and l["returned"]:
                    x("INSERT INTO stock_moves(date,item_id,wh_id,qty,doc_type,doc_id,doc_no) VALUES(?,?,?,?,'MRJ',?,?)",
                      (c["settle_date"], l["item_id"], wh, l["returned"], c["id"], c["return_no"]))
            if c["status"] == "settled" and c["final_no"]:
                iid = x("""INSERT INTO sales_invoices(no,kind,date,customer_id,visitor_id,wh_id,cons_id,note,created_by)
                           VALUES(?,'consign',?,?,?,?,?,?,?)""",
                        (c["final_no"], c["settle_date"], c["visitor_id"], c["visitor_id"], wh, c["id"], f"عطف به امانی {c['no']}",
                         c["settled_by"] if "settled_by" in cc else None))
                inv_of[c["id"]] = iid
                for l in lines:
                    sold = l["qty"] - l["returned"]
                    if sold > 0:
                        x("INSERT INTO sales_lines(inv_id,item_id,qty,price,cost) VALUES(?,?,?,?,?)",
                          (iid, l["item_id"], sold, l["price"], (l["cost"] if "cost" in clc else 0) or 0))
        pyc = cols(sq, "payments")
        skipped = 0
        for p in sq.execute("SELECT * FROM payments ORDER BY id"):
            iid = inv_of.get(p["cons_id"])
            if not iid:
                skipped += 1
                continue
            inv = q("SELECT customer_id, visitor_id FROM sales_invoices WHERE id=?", (iid,), one=True)
            m = p["method"] if p["method"] in ("cash", "pos", "bank") else "cash"
            did = x("INSERT INTO tr_docs(no,kind,date,person_id,inv_id,visitor_id,created_by) VALUES(?,'recv',?,?,?,?,?)",
                    (p["no"], p["date"], inv["customer_id"], iid, inv["visitor_id"], p["created_by"] if "created_by" in pyc else None))
            x("INSERT INTO tr_lines(doc_id,method,amount,box_id,bank_id,ref) VALUES(?,?,?,?,?,?)",
              (did, m, p["amount"], default_box() if m == "cash" else None, default_bank() if m != "cash" else None, p["ref"]))
        for k in sq.execute("SELECT * FROM counters"):
            x("INSERT INTO counters(kind,last) VALUES(?,?) ON CONFLICT(kind) DO UPDATE SET last=GREATEST(counters.last, excluded.last)", (k["kind"], k["last"]))
        x("""INSERT INTO counters(kind,last) SELECT 'TRD', COALESCE(MAX(no),1000) FROM tr_docs
             ON CONFLICT(kind) DO UPDATE SET last=GREATEST(counters.last, excluded.last)""")
        import gl
        import costing
        costing.recost_all()
        nd = gl.repost_all()
        for t in TABLE_SEQ:
            D.conn().exec_driver_sql(f"SELECT setval(pg_get_serial_sequence('{t}','id'), COALESCE((SELECT MAX(id) FROM {t}),0)+1, false)")
        D.commit()
        print(f"OK: users={v1('SELECT COUNT(*) FROM users')} items={v1('SELECT COUNT(*) FROM items')} persons={v1('SELECT COUNT(*) FROM persons')} "
              f"consignments={v1('SELECT COUNT(*) FROM consignments')} invoices={v1('SELECT COUNT(*) FROM sales_invoices')} "
              f"payments={v1('SELECT COUNT(*) FROM payments')} vouchers={v1('SELECT COUNT(*) FROM vouchers')} receipts={v1('SELECT COUNT(*) FROM receipts')}" + (f" skipped_payments={skipped}" if skipped else ""))
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(2)
    sys.exit(main(sys.argv[1], "--force" in sys.argv))
