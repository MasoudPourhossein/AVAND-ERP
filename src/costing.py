"""Moving weighted-average cost (میانگین موزون متحرک), company-wide per item.

Walks an item's stock moves in date order and gives every outgoing move the average cost at that moment:
  in : RES receipt (its unit cost; stock-count adjustments use the average), PUR purchase (net unit price after line discount),
       SRT sales return (cost of the original sale), MRJ consignment return (cost it left with)
  out: FCT direct sale, AMN consignment, PUR purchase return (at its net price, so the ledger stays equal), RES negative adjustment
  TRF transfers move stock between warehouses only and do not change the company average.
Results are written to stock_moves.cost, sales_lines.cost, cons_lines.cost, receipt_lines.unit_cost (adjustments), items.avg_cost,
and the affected vouchers are re-posted. Runs before every commit for the items whose moves changed (core.add_moves/del_moves)."""
from flask import g
from core import q, x, v1, buy_at

EPS = 1e-6


def touch(item_ids):
    s = g.setdefault("_recost", set())
    s.update(i for i in item_ids if i)


def _f(v):
    return float(v or 0)


def recost_item(i, repost):
    moves = q("""SELECT id, date, qty, cost, doc_type, doc_id FROM stock_moves WHERE item_id=? AND doc_type<>'TRF'
                 ORDER BY date, (qty<0), id""", (i,))
    pur = {r["pur_id"]: (_f(r["v"]) / r["n"] if r["n"] else 0) for r in q(
        "SELECT pur_id, SUM(qty) n, SUM(qty*price-disc) v FROM purchase_lines WHERE item_id=? GROUP BY pur_id", (i,))}
    rec = {r["receipt_id"]: r for r in q("""SELECT l.receipt_id, r.kind, SUM(l.qty*l.unit_cost)/NULLIF(SUM(l.qty),0) c FROM receipt_lines l
                                           JOIN receipts r ON r.id=l.receipt_id WHERE l.item_id=? GROUP BY l.receipt_id, r.kind""", (i,))}
    inv = {r["id"]: r for r in q("""SELECT s.id, s.kind, s.ret_of, s.cons_id, z.kind src_kind, z.cons_id src_cons FROM sales_invoices s
                                   JOIN sales_lines l ON l.inv_id=s.id LEFT JOIN sales_invoices z ON z.id=s.ret_of
                                   WHERE l.item_id=? GROUP BY s.id, s.kind, s.ret_of, s.cons_id, z.kind, z.cons_id""", (i,))}
    last = _f(buy_at(i, moves[0]["date"])) if moves else _f(buy_at(i, "9999/99/99"))
    Q, V = 0, 0.0
    sale_c, cons_c, adj_c = {}, {}, {}
    move_upd = []
    for m in moves:
        avg = V / Q if Q > 0 else last
        t, qty, d = m["doc_type"], m["qty"], m["doc_id"]
        if t == "RES":
            r = rec.get(d)
            if r and r["kind"] == "adjust":
                c = avg
                adj_c[d] = c
            else:
                c = _f(r["c"]) if r else _f(m["cost"])
        elif t == "PUR":
            c = pur.get(d, _f(m["cost"]))
        elif t == "FCT":
            c = avg
            sale_c[d] = c
        elif t == "SRT":
            s = inv.get(d) or {}
            if s.get("ret_of") and s["ret_of"] in sale_c:
                c = sale_c[s["ret_of"]]
            elif s.get("ret_of") and s.get("src_kind") == "consign" and s.get("src_cons") in cons_c:
                c = cons_c[s["src_cons"]]
            else:
                c = avg
            sale_c[d] = c
        elif t == "AMN":
            c = avg
            cons_c[d] = c
        elif t == "MRJ":
            c = cons_c.get(d, avg)
        else:
            c = avg
        if qty > 0:
            if Q < 0:                       # receiving into a shortage: the new cost becomes the average
                Q += qty
                V = Q * c if Q > 0 else 0.0
            else:
                Q += qty
                V += qty * c
            if c > 0:
                last = V / Q if Q > 0 else c
        else:
            Q += qty
            V += qty * c
            if Q <= 0:
                last, V = (avg if avg > 0 else last), 0.0
        if abs(_f(m["cost"]) - c) > EPS:
            move_upd.append((c, m["id"]))
    for c, mid in move_upd:
        x("UPDATE stock_moves SET cost=? WHERE id=?", (c, mid))
    # documents that carry the cost
    for d, c in sale_c.items():
        if v1("SELECT COUNT(*) FROM sales_lines WHERE inv_id=? AND item_id=? AND ABS(cost-?)>0.000001", (d, i, c)):
            x("UPDATE sales_lines SET cost=? WHERE inv_id=? AND item_id=?", (c, d, i))
            repost["FCT"].add(d)
    for d, c in cons_c.items():
        if v1("SELECT COUNT(*) FROM cons_lines WHERE cons_id=? AND item_id=? AND (cost IS NULL OR ABS(cost-?)>0.000001)", (d, i, c)):
            x("UPDATE cons_lines SET cost=? WHERE cons_id=? AND item_id=?", (c, d, i))
            repost["AMN"].add(d)
        for s in q("""SELECT DISTINCT l.inv_id FROM sales_lines l JOIN sales_invoices s ON s.id=l.inv_id
                      WHERE s.cons_id=? AND l.item_id=? AND ABS(l.cost-?)>0.000001""", (d, i, c)):
            x("UPDATE sales_lines SET cost=? WHERE inv_id=? AND item_id=?", (c, s["inv_id"], i))
            repost["FCT"].add(s["inv_id"])
    for d, c in adj_c.items():
        rc = round(c)
        if v1("SELECT COUNT(*) FROM receipt_lines WHERE receipt_id=? AND item_id=? AND unit_cost<>?", (d, i, rc)):
            x("UPDATE receipt_lines SET unit_cost=? WHERE receipt_id=? AND item_id=?", (rc, d, i))
            repost["RES"].add(d)
    x("UPDATE items SET avg_cost=? WHERE id=?", (V / Q if Q > 0 else last, i))


def recost(items):
    import gl
    repost = {"FCT": set(), "AMN": set(), "RES": set()}
    for i in sorted(set(items)):
        recost_item(i, repost)
    for d in repost["FCT"]:
        gl.post_sale(d)
    for d in repost["AMN"]:
        gl.post_cons(d)
    for d in repost["RES"]:
        gl.post_receipt(d)
    return sum(len(v) for v in repost.values())


def recost_all():
    return recost([r["id"] for r in q("SELECT id FROM items")])


def flush():
    """called by dbl.commit(): recompute the items touched in this transaction"""
    s = g.pop("_recost", None)
    if s:
        recost(s)
