"""General ledger: automatic vouchers for every document (double entry) + helpers.
Every posting function is idempotent: it deletes the document's previous auto voucher and writes a fresh one."""
from flask import g
from core import q, x, v1, next_no, uid, buy_at

SRC_LABEL = {"FCT": "فاکتور فروش", "SRT": "برگشت از فروش", "PUR": "فاکتور خرید", "PRT": "برگشت از خرید", "RES": "رسید انبار",
             "AMN": "امانی", "TRD": "سند خزانه", "CHQ": "عملیات چک", "MAN": "سند دستی"}


def acc(key):
    cache = g.setdefault("_acc", {})
    if key not in cache:
        cache[key] = v1("SELECT id FROM accounts WHERE sys_key=?", (key,))
        if not cache[key]:
            raise RuntimeError(f"حساب سیستمی «{key}» در کدینگ پیدا نشد.")
    return cache[key]


def unpost(src_type, src_id):
    x("DELETE FROM vouchers WHERE kind='auto' AND src_type=? AND src_id=?", (src_type, src_id))


def post(src_type, src_id, date, descr, lines, no=None):
    """lines: dicts {acc: sys_key or account id, dr, cr, person, bank, box, cc, descr}. Zero lines dropped."""
    unpost(src_type, src_id)
    rows = []
    for l in lines:
        dr, cr = int(round(l.get("dr") or 0)), int(round(l.get("cr") or 0))
        if dr < 0:
            dr, cr = 0, cr - dr
        if cr < 0:
            cr, dr = 0, dr - cr
        if dr == 0 and cr == 0:
            continue
        a = l["acc"] if isinstance(l["acc"], int) else acc(l["acc"])
        rows.append((a, l.get("person"), l.get("bank"), l.get("box"), l.get("cc"), dr, cr, l.get("descr")))
    if not rows:
        return None
    td, tc = sum(r[5] for r in rows), sum(r[6] for r in rows)
    if td != tc:
        raise ValueError(f"سند حسابداری تراز نیست ({td:,} ≠ {tc:,}).")
    vid = x("INSERT INTO vouchers(no,date,kind,src_type,src_id,descr,created_by) VALUES(?,?,'auto',?,?,?,?)",
            (no or next_no("VCH"), date, src_type, src_id, descr, uid()))
    for r in rows:
        x("INSERT INTO voucher_lines(voucher_id,account_id,person_id,bank_id,box_id,cc_id,debit,credit,descr) VALUES(?,?,?,?,?,?,?,?,?)",
          (vid,) + r)
    return vid


def party_acc(person_id, side="recv"):
    """default counter account for a person: customer/visitor -> receivable, supplier -> payable, employee -> salary/advance"""
    p = q("SELECT * FROM persons WHERE id=?", (person_id,), one=True)
    if not p:
        return acc("receivable")
    if p["is_customer"] or p["is_visitor"]:
        return acc("receivable")
    if p["is_supplier"]:
        return acc("payable")
    if p["is_employee"]:
        return acc("salary_pay")
    return acc("receivable")


# ---------------- documents
def post_sale(iid):
    s = q("SELECT * FROM v_si WHERE id=?", (iid,), one=True)
    if not s:
        return unpost("FCT", iid)
    ret = s["kind"] == "return"
    cust, cogs = s["customer_id"], int(round(s["cogs_total"] or 0))
    src_inv = "consign" if s["kind"] == "consign" else "inventory"
    who = q("SELECT name FROM persons WHERE id=?", (cust,), one=True)["name"]
    if not ret:
        lines = [{"acc": "receivable", "dr": s["total"], "person": cust, "descr": f"فاکتور فروش {s['no']}"},
                 {"acc": "sales", "cr": s["gross"]},
                 {"acc": "sales_disc", "dr": s["ldisc"] + s["discount"]},
                 {"acc": "vat", "cr": s["vat_total"]},
                 {"acc": "cogs", "dr": cogs}, {"acc": src_inv, "cr": cogs}]
        d = f"فاکتور فروش {s['no']} — {who}"
    else:
        lines = [{"acc": "sales_ret", "dr": s["net_total"]}, {"acc": "vat", "dr": s["vat_total"]},
                 {"acc": "receivable", "cr": s["total"], "person": cust, "descr": f"برگشت از فروش {s['no']}"},
                 {"acc": "inventory", "dr": cogs}, {"acc": "cogs", "cr": cogs}]
        d = f"برگشت از فروش {s['no']} — {who}"
    return post("FCT", iid, s["date"], d, lines)


def post_purchase(pid):
    u = q("SELECT * FROM v_pu WHERE id=?", (pid,), one=True)
    if not u:
        return unpost("PUR", pid)
    who = q("SELECT name FROM persons WHERE id=?", (u["supplier_id"],), one=True)["name"]
    sg = -1 if u["kind"] == "return" else 1
    lines = [{"acc": "inventory", "dr": sg * u["net_total"]}, {"acc": "vat", "dr": sg * u["vat_total"]},
             {"acc": "payable", "cr": sg * u["total"], "person": u["supplier_id"]}]
    return post("PUR", pid, u["date"], f"{'برگشت از خرید' if sg < 0 else 'فاکتور خرید'} {u['no']} — {who}", lines)


def post_receipt(rid):
    r = q("SELECT * FROM receipts WHERE id=?", (rid,), one=True)
    if not r:
        return unpost("RES", rid)
    val = v1("SELECT SUM(qty*unit_cost) FROM receipt_lines WHERE receipt_id=?", (rid,))
    counter = "capital" if r["kind"] == "opening" else "stock_diff"
    title = (f"اصلاح موجودی (انبارگردانی) {r['no']}" if r["kind"] == "adjust" else f"رسید انبار {r['no']}" + (" (موجودی اولیه)" if r["kind"] == "opening" else ""))
    lines = [{"acc": "inventory", "dr": val}]
    if r["kind"] != "adjust" and r["credit"] == "person" and r["person_id"]:      # stock we owe to one supplier
        lines.append({"acc": party_acc(r["person_id"]), "person": r["person_id"], "cr": val, "descr": title})
    elif r["kind"] != "adjust" and r["credit"] == "items":                         # stock we owe to each item's supplier
        for s in q("""SELECT i.supplier_id s, SUM(l.qty*l.unit_cost) v FROM receipt_lines l JOIN items i ON i.id=l.item_id
                      WHERE l.receipt_id=? GROUP BY i.supplier_id""", (rid,)):
            if s["s"]:
                lines.append({"acc": "payable", "person": s["s"], "cr": s["v"], "descr": title})
            else:
                lines.append({"acc": counter, "cr": s["v"]})
    else:
        lines.append({"acc": counter, "cr": val})
    return post("RES", rid, r["date"], title, lines)


def _cons_cost(cid, date, col):
    total = 0
    for l in q(f"SELECT item_id, {col} n FROM cons_lines WHERE cons_id=?", (cid,)):
        total += l["n"] * (buy_at(l["item_id"], date) or 0)
    return int(round(total))


def post_cons(cid):
    """goods with visitor/customer: inventory -> consignment account at cost (and back for returns)"""
    c = q("SELECT * FROM consignments WHERE id=?", (cid,), one=True)
    if not c:
        unpost("AMN", cid); unpost("MRJ", cid)
        return
    out = _cons_cost(cid, c["date"], "qty")
    post("AMN", cid, c["date"], f"امانی {c['no']}", [{"acc": "consign", "dr": out}, {"acc": "inventory", "cr": out}])
    if c["status"] == "settled" and c["return_no"]:
        back = _cons_cost(cid, c["date"], "returned")
        post("MRJ", cid, c["settle_date"], f"مرجوعی امانی {c['no']}", [{"acc": "inventory", "dr": back}, {"acc": "consign", "cr": back}])
    else:
        unpost("MRJ", cid)


def _place(method, box, bank, cheque=None, out=False):
    """ledger line target for a treasury line"""
    if method == "cash":
        return {"acc": "cash", "box": box}
    if method in ("pos", "bank"):
        return {"acc": "bank", "bank": bank}
    if method == "cheque":
        return {"acc": "cheque_pay", "bank": bank} if out else {"acc": "cheque_recv"}
    if method == "cheque_out":
        return {"acc": "cheque_recv"}
    raise ValueError(method)


def post_tr(did):
    d = q("SELECT * FROM tr_docs WHERE id=?", (did,), one=True)
    if not d:
        return unpost("TRD", did)
    lines = []
    tot = 0
    for l in q("SELECT * FROM tr_lines WHERE doc_id=?", (did,)):
        tot += l["amount"]
        if d["kind"] == "recv":
            lines.append(dict(_place(l["method"], l["box_id"], l["bank_id"]), dr=l["amount"]))
        elif d["kind"] == "pay":
            lines.append(dict(_place(l["method"], l["box_id"], l["bank_id"], out=True), cr=l["amount"]))
    if d["kind"] == "transfer":
        amt = v1("SELECT SUM(amount) FROM tr_lines WHERE doc_id=?", (did,))
        lines = [dict(_place("cash" if d["to_box"] else "bank", d["to_box"], d["to_bank"]), dr=amt),
                 dict(_place("cash" if d["from_box"] else "bank", d["from_box"], d["from_bank"]), cr=amt)]
        descr = f"انتقال وجه {d['no']}"
    else:
        a = d["acc_id"] or party_acc(d["person_id"])
        party = {"acc": a, "person": d["person_id"], "cc": d["cc_id"]}
        lines.append(dict(party, cr=tot) if d["kind"] == "recv" else dict(party, dr=tot))
        who = q("SELECT name FROM persons WHERE id=?", (d["person_id"],), one=True)["name"] if d["person_id"] else ""
        descr = f"{'دریافت' if d['kind'] == 'recv' else 'پرداخت'} {d['no']} {who}".strip() + (f" — {d['note']}" if d["note"] else "")
    return post("TRD", did, d["date"], descr, lines)


def post_cheque_event(eid):
    """status change of a cheque (not the initial registration, which is part of its treasury doc)"""
    e = q("""SELECT e.*, c.kind ckind, c.amount, c.person_id cperson, c.serial, c.bank_id cbank, c.holder_id FROM cheque_events e
             JOIN cheques c ON c.id=e.cheque_id WHERE e.id=?""", (eid,), one=True)
    if not e or e["tr_doc_id"]:      # registration / spending through a treasury doc is posted by that doc
        return unpost("CHQ", eid)
    amt, f_, t_ = e["amount"], e["from_status"], e["to_status"]
    lab = f"چک {e['serial'] or ''}".strip()
    L = []
    if e["ckind"] == "recv":
        dep = v1("""SELECT bank_id FROM cheque_events WHERE cheque_id=? AND to_status='in_bank' AND id<? ORDER BY id DESC LIMIT 1""",
                 (e["cheque_id"], eid)) or None
        if f_ == "spent" and t_ == "bounced":   # the person we gave it to returned it: claim back from the drawer
            return post("CHQ", eid, e["date"], f"{lab} — برگشتی (خرج‌شده)",
                        [{"acc": "receivable", "person": e["cperson"], "dr": amt},
                         {"acc": party_acc(e["person_id"]), "person": e["person_id"], "cr": amt}])
        src = {"in_box": {"acc": "cheque_recv"}, "in_bank": {"acc": "cheque_bank", "bank": dep}}.get(f_)
        if src is None:
            return None
        if t_ == "in_bank":
            L = [{"acc": "cheque_bank", "bank": e["bank_id"], "dr": amt}, dict(src, cr=amt)]
        elif t_ == "cleared":
            b_ = e["bank_id"] or (dep if f_ == "in_bank" else None)
            dst = {"acc": "bank", "bank": b_} if b_ else {"acc": "cash", "box": e["box_id"]}
            L = [dict(dst, dr=amt), dict(src, cr=amt)]
        elif t_ in ("bounced", "returned"):
            L = [{"acc": "receivable", "person": e["cperson"], "dr": amt}, dict(src, cr=amt)]
        elif t_ == "in_box":   # bounced from bank back to box
            L = [{"acc": "cheque_recv", "dr": amt}, dict(src, cr=amt)]
        elif t_ == "spent":
            L = [{"acc": party_acc(e["person_id"]), "person": e["person_id"], "dr": amt}, dict(src, cr=amt)]
    else:
        if t_ == "cleared":
            L = [{"acc": "cheque_pay", "bank": e["cbank"], "dr": amt}, {"acc": "bank", "bank": e["cbank"], "cr": amt}]
        elif t_ in ("bounced", "cancelled"):
            L = [{"acc": "cheque_pay", "bank": e["cbank"], "dr": amt}, {"acc": party_acc(e["cperson"]), "person": e["cperson"], "cr": amt}]
    return post("CHQ", eid, e["date"], f"{lab} — {STATUS_LABEL.get(t_, t_)}", L)


STATUS_LABEL = {"in_box": "نزد صندوق", "in_bank": "در جریان وصول (نزد بانک)", "cleared": "وصول / پاس شده", "bounced": "برگشتی",
                "returned": "عودت داده شده", "spent": "خرج شده (واگذار)", "issued": "صادر شده", "cancelled": "باطل شده"}


def repost_all():
    """rebuild every automatic voucher from documents (manual vouchers untouched), in date order"""
    x("DELETE FROM vouchers WHERE kind='auto'")
    docs = []
    for r in q("SELECT id, date FROM receipts"):
        docs.append((r["date"], 0, "RES", r["id"]))
    for r in q("SELECT id, date FROM purchases"):
        docs.append((r["date"], 1, "PUR", r["id"]))
    for r in q("SELECT id, date FROM consignments"):
        docs.append((r["date"], 2, "AMN", r["id"]))
    for r in q("SELECT id, date FROM sales_invoices"):
        docs.append((r["date"], 3, "FCT", r["id"]))
    for r in q("SELECT id, date FROM tr_docs"):
        docs.append((r["date"], 4, "TRD", r["id"]))
    for r in q("SELECT id, date FROM cheque_events"):
        docs.append((r["date"], 5, "CHQ", r["id"]))
    fn = {"RES": post_receipt, "PUR": post_purchase, "AMN": post_cons, "FCT": post_sale, "TRD": post_tr, "CHQ": post_cheque_event}
    for _, _, t, i in sorted(docs):
        fn[t](i)
    return len(docs)


def person_balance(pid, until=None, before=False):
    """+ = person owes us (debtor), - = we owe them (creditor)"""
    if until:
        return v1(f"SELECT SUM(vl.debit-vl.credit) FROM voucher_lines vl JOIN vouchers v ON v.id=vl.voucher_id WHERE vl.person_id=? AND v.date {'<' if before else '<='} ?",
                  (pid, until))
    return v1("SELECT SUM(debit-credit) FROM voucher_lines WHERE person_id=?", (pid,))
