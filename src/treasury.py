"""Treasury (خزانه): receive / pay documents for every person type, cash boxes, banks, cheques (received & issued), fund transfers."""
from flask import render_template, request, redirect, url_for, flash, abort, jsonify
from core import app, q, x, v1, db, next_no, to_int, uid, uname, INV_SQL, default_box, default_bank, can, METHOD
from jalali import today_j, norm_date
import gl

TR_KIND = {"recv": "دریافت", "pay": "پرداخت", "transfer": "انتقال وجه"}
TR_METHOD = {"cash": "نقد (صندوق)", "pos": "کارت‌خوان (پوز)", "bank": "واریز / حواله بانکی", "cheque": "چک",
             "cheque_out": "خرج چک دریافتی", "transfer": "انتقال"}
KIND_METHODS = {"recv": ["cash", "pos", "bank", "cheque"], "pay": ["cash", "bank", "cheque", "cheque_out"]}
CHQ_FLOW = {  # allowed next statuses
    "recv": {"in_box": ["in_bank", "cleared", "spent", "returned", "bounced"], "in_bank": ["cleared", "bounced", "in_box"],
             "bounced": ["returned"], "spent": ["bounced"]},
    "pay": {"issued": ["cleared", "bounced", "cancelled"]},
}
CHQ_ACTION = {"in_bank": "واگذاری به بانک (خواباندن به حساب)", "cleared": "وصول / پاس شد", "spent": "خرج چک (واگذاری به شخص)",
              "returned": "عودت به صاحب چک", "bounced": "برگشت خورد", "in_box": "برگشت از بانک به صندوق", "cancelled": "ابطال چک"}


def persons_all():
    return q("""SELECT id, code, name, is_customer, is_supplier, is_visitor, is_employee, visitor_id FROM persons WHERE active=1 ORDER BY code""")


def boxes(active=True):
    return q("SELECT * FROM cash_boxes" + (" WHERE active=1" if active else "") + " ORDER BY is_default DESC, code")


def banks(active=True):
    return q("SELECT * FROM banks" + (" WHERE active=1" if active else "") + " ORDER BY is_default DESC, code")


def leaf_accounts():
    return q("SELECT id, code, name, sys_key FROM accounts WHERE level=3 AND active=1 ORDER BY code")


def _visitor_of(pid, inv=None):
    if inv:
        return inv["visitor_id"]
    p = q("SELECT is_visitor, visitor_id FROM persons WHERE id=?", (pid,), one=True) if pid else None
    if not p:
        return None
    return pid if p["is_visitor"] else p["visitor_id"]


def save_doc(kind, date, person, lines, acc_id=None, cc_id=None, inv_id=None, pur_id=None, note=None, transfer=None):
    """lines: dicts {method, amount, box, bank, ref, cheque:{serial,sayad,bank_name,branch,account_no,owner,due}, cheque_id}
    transfer: (from_box, from_bank, to_box, to_bank). Returns (no, id)."""
    if not date:
        raise ValueError("تاریخ نامعتبر است.")
    inv = q("SELECT * FROM sales_invoices WHERE id=?", (inv_id,), one=True) if inv_id else None
    if inv and not person:
        person = inv["customer_id"]
    if kind in ("recv", "pay") and not person and not acc_id:
        raise ValueError("شخص یا حساب طرف مقابل را انتخاب کنید.")
    if not lines:
        raise ValueError("حداقل یک ردیف با مبلغ وارد کنید.")
    for l in lines:
        if l["amount"] <= 0 and l["method"] != "cheque_out":
            raise ValueError("مبلغ ردیف‌ها باید بیشتر از صفر باشد.")
        if kind in KIND_METHODS and l["method"] not in KIND_METHODS[kind]:
            raise ValueError("روش ردیف نامعتبر است.")
        if l["method"] == "cash" and not l.get("box"):
            raise ValueError("صندوق ردیف نقدی را انتخاب کنید.")
        if l["method"] in ("pos", "bank") and not l.get("bank"):
            raise ValueError("حساب بانکی ردیف را انتخاب کنید.")
        if l["method"] == "cheque":
            ch = l.get("cheque") or {}
            if not norm_date(ch.get("due") or ""):
                raise ValueError("تاریخ سررسید چک را وارد کنید.")
            if kind == "pay" and not l.get("bank"):
                raise ValueError("حساب بانکی چک پرداختی را انتخاب کنید.")
    tf = transfer or (None, None, None, None)
    if kind == "transfer":
        if not (tf[0] or tf[1]) or not (tf[2] or tf[3]) or tf[:2] == tf[2:]:
            raise ValueError("مبدا و مقصد انتقال را درست انتخاب کنید.")
    no = next_no("TRD")
    did = x("""INSERT INTO tr_docs(no,kind,date,person_id,acc_id,cc_id,inv_id,pur_id,visitor_id,note,created_by,from_box,from_bank,to_box,to_bank)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (no, kind, date, person or None, acc_id or None, cc_id or None, inv_id or None, pur_id or None,
             _visitor_of(person, inv) if kind == "recv" else None, note, uid()) + tuple(tf))
    pname = v1("SELECT name FROM persons WHERE id=?", (person,)) if person else None
    for l in lines:
        cid = None
        if l["method"] == "cheque":
            ch = l["cheque"]
            ck = "recv" if kind == "recv" else "pay"
            st = "in_box" if ck == "recv" else "issued"
            cid = x("""INSERT INTO cheques(kind,serial,sayad,bank_name,branch,account_no,owner,amount,issue_date,due_date,person_id,bank_id,status,note,created)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (ck, ch.get("serial"), ch.get("sayad"), ch.get("bank_name"), ch.get("branch"), ch.get("account_no"),
                     ch.get("owner") or (pname if ck == "recv" else None), l["amount"], date, norm_date(ch["due"]), person or None,
                     l.get("bank") if ck == "pay" else None, st, note, today_j()))
            x("INSERT INTO cheque_events(cheque_id,date,from_status,to_status,person_id,tr_doc_id,created_by) VALUES(?,?,?,?,?,?,?)",
              (cid, date, None, st, person or None, did, uid()))
        elif l["method"] == "cheque_out":
            c = q("SELECT * FROM cheques WHERE id=? AND kind='recv'", (l.get("cheque_id"),), one=True)
            if not c or c["status"] != "in_box":
                raise ValueError("چک انتخاب‌شده برای خرج کردن نزد صندوق نیست.")
            cid, l["amount"] = c["id"], c["amount"]
            x("UPDATE cheques SET status='spent', holder_id=? WHERE id=?", (person or None, cid))
            x("INSERT INTO cheque_events(cheque_id,date,from_status,to_status,person_id,tr_doc_id,created_by) VALUES(?,?,?,?,?,?,?)",
              (cid, date, "in_box", "spent", person or None, did, uid()))
        x("INSERT INTO tr_lines(doc_id,method,amount,box_id,bank_id,cheque_id,ref) VALUES(?,?,?,?,?,?,?)",
          (did, l["method"], l["amount"], l.get("box") if l["method"] == "cash" else None,
           l.get("bank") if l["method"] in ("pos", "bank") or (l["method"] == "cheque" and kind == "pay") else None, cid, l.get("ref") or None))
    gl.post_tr(did)
    return no, did


def delete_doc(did):
    d = q("SELECT * FROM tr_docs WHERE id=?", (did,), one=True) or abort(404)
    kill = []
    for l in q("SELECT * FROM tr_lines WHERE doc_id=? AND cheque_id IS NOT NULL", (did,)):
        c = q("SELECT * FROM cheques WHERE id=?", (l["cheque_id"],), one=True)
        nev = v1("SELECT COUNT(*) FROM cheque_events WHERE cheque_id=?", (c["id"],))
        if l["method"] == "cheque":
            if nev > 1:
                raise ValueError(f"چک {c['serial'] or c['id']} عملیات بعدی دارد (وضعیت: {gl.STATUS_LABEL.get(c['status'])}); ابتدا آن را برگردانید.")
            kill.append(c["id"])
        else:
            last = q("SELECT * FROM cheque_events WHERE cheque_id=? ORDER BY id DESC LIMIT 1", (c["id"],), one=True)
            if c["status"] != "spent" or last["tr_doc_id"] != did:
                raise ValueError(f"چک خرج‌شده {c['serial'] or c['id']} بعد از این سند تغییر وضعیت داده است.")
            x("UPDATE cheques SET status='in_box', holder_id=NULL WHERE id=?", (c["id"],))
    gl.unpost("TRD", did)
    x("DELETE FROM tr_docs WHERE id=?", (did,))
    for cid in kill:
        x("DELETE FROM cheques WHERE id=?", (cid,))
    return d


def _lines_from_form(kind, F):
    out = []
    ms = F.getlist("method")
    for i, m in enumerate(ms):
        g_ = lambda k: (F.getlist(k)[i] if i < len(F.getlist(k)) else "").strip()
        amt = to_int(g_("amount"))
        if m == "cheque_out":
            if to_int(g_("chq")):
                out.append({"method": m, "amount": 0, "cheque_id": to_int(g_("chq")), "ref": g_("ref")})
            continue
        if amt <= 0:
            continue
        out.append({"method": m, "amount": amt, "box": to_int(g_("box")) or None, "bank": to_int(g_("bank")) or None, "ref": g_("ref"),
                    "cheque": {"serial": g_("serial"), "sayad": g_("sayad"), "bank_name": g_("cbank"), "branch": g_("branch"),
                               "owner": g_("owner"), "due": g_("due")}})
    return out


def _pt(v):
    """'box:3' / 'bank:2' -> (box, bank)"""
    k, _, i = (v or "").partition(":")
    return (to_int(i) or None, None) if k == "box" else ((None, to_int(i) or None) if k == "bank" else (None, None))


def quick_receive(inv, F):
    """receipt registered from the invoice page"""
    m = F.get("method") if F.get("method") in KIND_METHODS["recv"] else "cash"
    line = {"method": m, "amount": to_int(F.get("amount")), "box": to_int(F.get("box")) or default_box(),
            "bank": to_int(F.get("bank")) or default_bank(), "ref": (F.get("ref") or "").strip(),
            "cheque": {"serial": F.get("serial"), "sayad": F.get("sayad"), "bank_name": F.get("cbank"), "due": F.get("due")}}
    no, _ = save_doc("recv", norm_date(F.get("date")) or today_j(), inv["customer_id"], [line], inv_id=inv["id"], note=(F.get("note") or "").strip() or None)
    return no


# ================= routes =================
def _f_period():
    from core import period
    return period()


@app.route("/treasury")
def tr_list():
    import export
    from reports import link
    f, t = _f_period()
    kind, pid = request.args.get("kind", ""), to_int(request.args.get("person"))
    w, a = " WHERE d.date BETWEEN ? AND ?", [f, t]
    if kind in TR_KIND:
        w += " AND d.kind=?"; a.append(kind)
    if pid:
        w += " AND d.person_id=?"; a.append(pid)
    rows = q(f"""SELECT d.*, p.code pcode, p.name pname, ac.name aname, s.no ino, (SELECT SUM(amount) FROM tr_lines WHERE doc_id=d.id) amt,
                 (SELECT string_agg(DISTINCT method, ',') FROM tr_lines WHERE doc_id=d.id) ms
                 FROM tr_docs d LEFT JOIN persons p ON p.id=d.person_id LEFT JOIN accounts ac ON ac.id=d.acc_id
                 LEFT JOIN sales_invoices s ON s.id=d.inv_id {w} ORDER BY d.date DESC, d.no DESC""", a)
    R = []
    for r in rows:
        if r["kind"] == "transfer":
            party = "انتقال بین صندوق/بانک"
        else:
            party = f"{r['pcode']} - {r['pname']}" if r["pname"] else (r["aname"] or "")
        sg = 1 if r["kind"] == "recv" else (-1 if r["kind"] == "pay" else 0)
        R.append([link(r["no"], url_for("tr_view", did=r["id"])), r["date"], TR_KIND[r["kind"]], party,
                  link(r["ino"], url_for("invoice", iid=r["inv_id"])) if r["ino"] else "", "، ".join(TR_METHOD.get(m, m) for m in (r["ms"] or "").split(",") if m),
                  r["note"] or "", r["amt"] if sg >= 0 else 0, r["amt"] if sg < 0 else 0])
    tr = sum(r[7] for r in R if R and r[2] == TR_KIND["recv"])
    tp = sum(r[8] for r in R)
    spec = {"title": "اسناد خزانه (دریافت و پرداخت)", "sub": f"از {f} تا {t}", "filters": ["period"],
            "selects": [("kind", [("", "همه انواع")] + list(TR_KIND.items()), kind),
                        ("person", [("", "همه اشخاص")] + [(str(p["id"]), f"{p['code']} - {p['name']}") for p in persons_all()], str(pid or ""))],
            "actions": [(url_for("tr_new", kind="recv"), "سند دریافت جدید", "g"), (url_for("tr_new", kind="pay"), "سند پرداخت جدید", "o"),
                        (url_for("tr_new", kind="transfer"), "انتقال وجه", "s")] if can("treasury") else [],
            "kpis": [("جمع دریافت", tr, "money"), ("جمع پرداخت", tp, "money"), ("خالص", tr - tp, "money")],
            "sections": [{"cols": [("شماره", "text"), ("تاریخ", "text"), ("نوع", "text"), ("طرف حساب", "text"), ("فاکتور", "text"), ("روش", "text"),
                                   ("توضیح", "text"), ("دریافت", "money"), ("پرداخت", "money")], "rows": R,
                          "total": ["جمع", "", "", "", "", "", "", tr, tp]}]}
    return export.out(spec)


@app.route("/treasury/new/<kind>", methods=["GET", "POST"])
def tr_new(kind):
    if kind not in TR_KIND:
        abort(404)
    F = request.form
    if request.method == "POST":
        try:
            date = norm_date(F.get("date"))
            if kind == "transfer":
                fb = _pt(F.get("from"))
                tb = _pt(F.get("to"))
                amt = to_int(F.get("amount"))
                if amt <= 0:
                    raise ValueError("مبلغ انتقال را وارد کنید.")
                no, did = save_doc("transfer", date, None, [{"method": "transfer", "amount": amt, "ref": (F.get("ref") or "").strip()}],
                                   note=(F.get("note") or "").strip() or None, transfer=fb + tb)
            else:
                no, did = save_doc(kind, date, to_int(F.get("person")) or None, _lines_from_form(kind, F), acc_id=to_int(F.get("acc_id")) or None,
                                   cc_id=to_int(F.get("cc_id")) or None, inv_id=to_int(F.get("inv_id")) or None,
                                   pur_id=to_int(F.get("pur_id")) or None, note=(F.get("note") or "").strip() or None)
            db().commit()
            flash(f"سند {TR_KIND[kind]} شماره {no} ثبت شد.", "ok")
            return redirect(url_for("tr_view", did=did))
        except ValueError as e:
            db().rollback()
            flash(str(e), "bad")
    return render_template("tr_form.html", kind=kind, persons=persons_all(), boxes=boxes(), banks=banks(), accounts=leaf_accounts(),
                           ccs=q("SELECT * FROM cost_centers WHERE active=1 ORDER BY code"),
                           chqs=q("""SELECT c.*, p.name pname FROM cheques c LEFT JOIN persons p ON p.id=c.person_id
                                     WHERE c.kind='recv' AND c.status='in_box' ORDER BY c.due_date"""),
                           methods=KIND_METHODS.get(kind, []), TR_METHOD=TR_METHOD, TR_KIND=TR_KIND,
                           sel_person=to_int(F.get("person") or request.args.get("person")), sel_inv=to_int(F.get("inv_id") or request.args.get("inv")),
                           box0=default_box(), bank0=default_bank())


def tr_doc(did):
    d = q("""SELECT d.*, p.code pcode, p.name pname, ac.code acode, ac.name aname, cc.name ccname, s.no ino, pu.no puno
             FROM tr_docs d LEFT JOIN persons p ON p.id=d.person_id LEFT JOIN accounts ac ON ac.id=d.acc_id
             LEFT JOIN cost_centers cc ON cc.id=d.cc_id LEFT JOIN sales_invoices s ON s.id=d.inv_id LEFT JOIN purchases pu ON pu.id=d.pur_id
             WHERE d.id=?""", (did,), one=True) or abort(404)
    lines = q("""SELECT l.*, b.name bname, x.name xname, c.serial, c.sayad, c.due_date, c.bank_name cbank, c.status cstatus, c.id cid
                 FROM tr_lines l LEFT JOIN banks b ON b.id=l.bank_id LEFT JOIN cash_boxes x ON x.id=l.box_id LEFT JOIN cheques c ON c.id=l.cheque_id
                 WHERE l.doc_id=? ORDER BY l.id""", (did,))
    return d, lines


def _ptname(box, bank):
    if box:
        return "صندوق " + (v1("SELECT name FROM cash_boxes WHERE id=?", (box,)) or "")
    if bank:
        return "بانک " + (v1("SELECT name FROM banks WHERE id=?", (bank,)) or "")
    return ""


@app.route("/treasury/<int:did>")
def tr_view(did):
    d, lines = tr_doc(did)
    vid = v1("SELECT id FROM vouchers WHERE kind='auto' AND src_type='TRD' AND src_id=?", (did,))
    return render_template("tr_view.html", d=d, lines=lines, TR_KIND=TR_KIND, TR_METHOD=TR_METHOD, STATUS=gl.STATUS_LABEL, vid=vid,
                           ptname=_ptname, uname=uname)


@app.post("/treasury/<int:did>/delete")
def tr_delete(did):
    try:
        d = delete_doc(did)
        db().commit()
        flash(f"سند خزانه {d['no']} حذف شد.", "ok")
        return redirect(url_for("invoice", iid=d["inv_id"]) if d["inv_id"] and request.form.get("back") == "inv" else url_for("tr_list"))
    except ValueError as e:
        db().rollback()
        flash(str(e), "bad")
        return redirect(url_for("tr_view", did=did))


# ---------------- cheques
CHQ_SQL = """SELECT c.*, p.code pcode, p.name pname, h.name hname, b.name bname FROM cheques c LEFT JOIN persons p ON p.id=c.person_id
             LEFT JOIN persons h ON h.id=c.holder_id LEFT JOIN banks b ON b.id=c.bank_id"""


@app.route("/cheques")
def cheques():
    import export
    from reports import link
    kind = request.args.get("kind") if request.args.get("kind") in ("recv", "pay") else "recv"
    st = request.args.get("status", "")
    f = norm_date(request.args.get("from") or "") or ""
    t = norm_date(request.args.get("to") or "") or ""
    w, a = " WHERE c.kind=?", [kind]
    if st:
        w += " AND c.status=?"; a.append(st)
    if f:
        w += " AND c.due_date>=?"; a.append(f)
    if t:
        w += " AND c.due_date<=?"; a.append(t)
    rows = q(CHQ_SQL + w + " ORDER BY c.due_date, c.id", a)
    today = today_j()
    R = [[link(r["serial"] or f"#{r['id']}", url_for("cheque_view", cid=r["id"])), r["sayad"] or "", r["due_date"],
          r["bank_name"] or r["bname"] or "", f"{r['pcode']} - {r['pname']}" if r["pname"] else "", r["hname"] or "",
          {"v": gl.STATUS_LABEL.get(r["status"], r["status"]), "cls": "bad" if r["status"] == "bounced" or (r["status"] in ("in_box", "issued") and r["due_date"] < today) else None},
          r["amount"]] for r in rows]
    sts = sorted({s for flow in CHQ_FLOW[kind].values() for s in flow} | set(CHQ_FLOW[kind]))
    by = {}
    for r in rows:
        by[r["status"]] = by.get(r["status"], 0) + r["amount"]
    spec = {"title": "چک‌های دریافتی" if kind == "recv" else "چک‌های پرداختی", "sub": (f"سررسید از {f} " if f else "") + (f"تا {t}" if t else ""),
            "filters": [], "selects": [("kind", [("recv", "چک‌های دریافتی"), ("pay", "چک‌های پرداختی")], kind),
                                        ("status", [("", "همه وضعیت‌ها")] + [(s, gl.STATUS_LABEL[s]) for s in sts], st)],
            "inputs": [("from", "سررسید از", f), ("to", "تا", t)],
            "kpis": [(gl.STATUS_LABEL[k], v, "money") for k, v in sorted(by.items())][:4] or [("جمع", 0, "money")],
            "sections": [{"cols": [("سریال", "text"), ("صیادی", "text"), ("سررسید", "text"), ("بانک", "text"),
                                   ("از / به" if kind == "recv" else "در وجه", "text"), ("خرج شده به", "text"), ("وضعیت", "text"), ("مبلغ", "money")],
                          "rows": R, "total": ["جمع", "", "", "", "", "", "", sum(r["amount"] for r in rows)]}]}
    return export.out(spec)


@app.route("/cheques/<int:cid>", methods=["GET", "POST"])
def cheque_view(cid):
    c = q(CHQ_SQL + " WHERE c.id=?", (cid,), one=True) or abort(404)
    F = request.form
    if request.method == "POST":
        try:
            if F.get("action") == "undo":
                e = q("SELECT * FROM cheque_events WHERE cheque_id=? ORDER BY id DESC LIMIT 1", (cid,), one=True)
                if not e or e["tr_doc_id"]:
                    raise ValueError("آخرین عملیات این چک از طریق سند خزانه ثبت شده؛ آن سند را حذف کنید.")
                gl.unpost("CHQ", e["id"])
                x("DELETE FROM cheque_events WHERE id=?", (e["id"],))
                x("UPDATE cheques SET status=?, holder_id=CASE WHEN ?='spent' THEN NULL ELSE holder_id END WHERE id=?", (e["from_status"], e["to_status"], cid))
                if e["to_status"] == "in_bank":
                    x("UPDATE cheques SET bank_id=NULL WHERE id=?", (cid,))
                db().commit()
                flash("آخرین عملیات چک برگشت داده شد.", "ok")
            else:
                to = F.get("to_status")
                if to not in CHQ_FLOW[c["kind"]].get(c["status"], []):
                    raise ValueError("تغییر وضعیت مجاز نیست.")
                date = norm_date(F.get("date"))
                if not date:
                    raise ValueError("تاریخ نامعتبر است.")
                bank, box, person = to_int(F.get("bank")) or None, None, to_int(F.get("person")) or None
                if to == "cleared" and c["kind"] == "recv" and c["status"] == "in_box":
                    box, bank = _pt(F.get("dest"))
                if c["kind"] == "recv":
                    if to == "in_bank" and not bank:
                        raise ValueError("بانک واگذاری را انتخاب کنید.")
                    if to == "cleared" and c["status"] == "in_box" and not (bank or box):
                        raise ValueError("محل واریز وجه چک (بانک یا صندوق) را انتخاب کنید.")
                    if to == "spent" and not person:
                        raise ValueError("شخصی که چک به او واگذار می‌شود را انتخاب کنید.")
                if c["status"] == "spent" and to == "bounced":
                    person = c["holder_id"]
                eid = x("""INSERT INTO cheque_events(cheque_id,date,from_status,to_status,bank_id,box_id,person_id,note,created_by)
                           VALUES(?,?,?,?,?,?,?,?,?)""", (cid, date, c["status"], to, bank, box, person, (F.get("note") or "").strip() or None, uid()))
                x("UPDATE cheques SET status=? WHERE id=?", (to, cid))
                if c["kind"] == "recv" and to == "in_bank":
                    x("UPDATE cheques SET bank_id=? WHERE id=?", (bank, cid))
                if to == "spent":
                    x("UPDATE cheques SET holder_id=? WHERE id=?", (person, cid))
                gl.post_cheque_event(eid)
                db().commit()
                flash(f"وضعیت چک: {gl.STATUS_LABEL[to]}", "ok")
        except ValueError as e:
            db().rollback()
            flash(str(e), "bad")
        return redirect(url_for("cheque_view", cid=cid))
    ev = q("""SELECT e.*, b.name bname, x.name xname, p.name pname, d.no dno FROM cheque_events e LEFT JOIN banks b ON b.id=e.bank_id
              LEFT JOIN cash_boxes x ON x.id=e.box_id LEFT JOIN persons p ON p.id=e.person_id LEFT JOIN tr_docs d ON d.id=e.tr_doc_id
              WHERE e.cheque_id=? ORDER BY e.id""", (cid,))
    src = q("SELECT d.id, d.no FROM tr_lines l JOIN tr_docs d ON d.id=l.doc_id WHERE l.cheque_id=? ORDER BY d.id LIMIT 1", (cid,), one=True)
    return render_template("cheque_view.html", c=c, ev=ev, src=src, nxt=CHQ_FLOW[c["kind"]].get(c["status"], []), ACT=CHQ_ACTION,
                           STATUS=gl.STATUS_LABEL, banks=banks(), boxes=boxes(), persons=persons_all(), uname=uname)


# ---------------- cash boxes & banks
def place_balance(col, i, until=None):
    a = [i]
    w = ""
    if until:
        w = " AND v.date<=?"; a.append(until)
    return v1(f"SELECT SUM(vl.debit-vl.credit) FROM voucher_lines vl JOIN vouchers v ON v.id=vl.voucher_id WHERE vl.{col}=? AND vl.account_id IN (?, ?){w}",
              [i, gl.acc("cash"), gl.acc("bank")] + a[1:])


@app.route("/cash-banks", methods=["GET", "POST"])
def cash_banks():
    F = request.form
    if request.method == "POST":
        t, i = F.get("t"), to_int(F.get("id"))
        code, name = (F.get("code") or "").strip(), (F.get("name") or "").strip()
        tbl = "cash_boxes" if t == "box" else "banks"
        if not code or not name:
            flash("کد و نام الزامی است.", "bad")
        elif v1(f"SELECT COUNT(*) FROM {tbl} WHERE code=? AND id<>?", (code, i)):
            flash("کد تکراری است.", "bad")
        else:
            dflt, act = 1 if F.get("is_default") else 0, 1 if (F.get("active") or not i) else 0
            if dflt:
                x(f"UPDATE {tbl} SET is_default=0")
            if t == "box":
                vals = (code, name, (F.get("keeper") or "").strip() or None, dflt, act)
                if i:
                    x("UPDATE cash_boxes SET code=?, name=?, keeper=?, is_default=?, active=? WHERE id=?", vals + (i,))
                else:
                    x("INSERT INTO cash_boxes(code,name,keeper,is_default,active) VALUES(?,?,?,?,?)", vals)
            else:
                vals = (code, name, (F.get("bank_name") or "").strip() or None, (F.get("branch") or "").strip() or None,
                        (F.get("account_no") or "").strip() or None, (F.get("iban") or "").strip() or None, (F.get("card_no") or "").strip() or None,
                        1 if F.get("is_pos") else 0, dflt, act)
                if i:
                    x("""UPDATE banks SET code=?, name=?, bank_name=?, branch=?, account_no=?, iban=?, card_no=?, is_pos=?, is_default=?, active=?
                         WHERE id=?""", vals + (i,))
                else:
                    x("""INSERT INTO banks(code,name,bank_name,branch,account_no,iban,card_no,is_pos,is_default,active)
                         VALUES(?,?,?,?,?,?,?,?,?,?)""", vals)
            db().commit()
            flash("ذخیره شد.", "ok")
        return redirect(url_for("cash_banks"))
    bx = [dict(r, bal=place_balance("box_id", r["id"])) for r in boxes(False)]
    bk = [dict(r, bal=place_balance("bank_id", r["id"])) for r in banks(False)]
    cs = {r["k"]: r for r in q("""SELECT kind || ':' || status k, COUNT(*) n, SUM(amount) s FROM cheques
                                  WHERE status IN ('in_box','in_bank','issued') GROUP BY kind, status""")}
    return render_template("cash_banks.html", bx=bx, bk=bk, cs=cs)


# ---------------- api
@app.route("/api/open-invoices")
def api_open_invoices():
    pid = to_int(request.args.get("person"))
    out = []
    for r in q(INV_SQL + " WHERE s.customer_id=? AND s.kind<>'return' ORDER BY s.date, s.no", (pid,)):
        if r["rem"] > 0:
            out.append({"id": r["id"], "no": r["no"], "date": r["date"], "total": r["total"], "paid": r["paid"], "rem": r["rem"]})
    return jsonify(out)


@app.route("/api/person-balance")
def api_person_balance():
    pid = to_int(request.args.get("id"))
    return jsonify({"bal": gl.person_balance(pid) or 0})
