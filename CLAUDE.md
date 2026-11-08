# AVAND ERP — dev notes
- Flask app in `src/` (flat modules): core.py (config, permissions ACCESS map, helpers, stock/invoice SQL), dbl.py (SQLAlchemy+psycopg, SQL uses `?` placeholders, `%` auto-escaped, numeric->int/float loader), auth.py, masters.py (items/groups/prices/excel/persons/warehouses/accounts/cost centers), inventory.py (receipts, purchases, transfers), sales.py (consignment, settle → per-customer invoices, direct sales, payments), reports.py, export.py (xlsx/pdf), migrate_sqlite.py (one-time import of old anbar.db).
- Stock = SUM(stock_moves.qty) per item/warehouse. Every document writes moves (`add_moves`) and deletes them on removal (`del_moves`). Doc types: RES PUR TRF AMN MRJ FCT SRT (sales return).
- Totals come from views `v_si` (sales) / `v_pu` (purchases): gross, ldisc, vat_total, net_total (excl. VAT), total (payable), sg (+1, or -1 for returns). Always aggregate `x*sg`.
- Line VAT = round((qty*price - disc) * vat_rate/100); item.vat_rate NULL = company default (settings vat_on / vat_rate). Standard invoice print: printing.py + templates/std_invoice.html.
- Schema is idempotent `src/schema.sql`, applied at start by `init_db()` under an advisory lock. New columns: add `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` lines at the end of schema.sql.
- Every new endpoint needs an entry in `core.ACCESS` (or `register_report`), otherwise only users with `users` permission can open it.
- All POSTs take `pg_advisory_xact_lock` (serialized writes). Commit with `db().commit()`.
- Test: `tests/smoke.py` (drops the public schema of DATABASE_URL!). UI language: Persian, RTL, Jalali dates `YYYY/MM/DD` as TEXT.
- Deploy: push to main; on server `avand-update` (git pull + docker compose up -d --build).
