-- AVAND ERP schema (PostgreSQL). Idempotent: safe to run on every start.
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS counters(kind TEXT PRIMARY KEY, last BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS schema_info(id SERIAL PRIMARY KEY, version INT NOT NULL);

CREATE TABLE IF NOT EXISTS users(
  id SERIAL PRIMARY KEY, username TEXT UNIQUE NOT NULL, fullname TEXT, pw TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'sales', perms TEXT, active INT NOT NULL DEFAULT 1, deleted INT NOT NULL DEFAULT 0, created TEXT);

-- ---------- base data
CREATE TABLE IF NOT EXISTS item_groups(
  id SERIAL PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL, parent_id INT REFERENCES item_groups(id), active INT NOT NULL DEFAULT 1);

CREATE TABLE IF NOT EXISTS items(
  id SERIAL PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL, group_id INT REFERENCES item_groups(id),
  unit TEXT NOT NULL DEFAULT 'باکس', pack INT NOT NULL DEFAULT 1, pack_unit TEXT NOT NULL DEFAULT 'کارتن',
  barcode TEXT, note TEXT, active INT NOT NULL DEFAULT 1);

CREATE TABLE IF NOT EXISTS prices(
  id SERIAL PRIMARY KEY, item_id INT NOT NULL REFERENCES items(id) ON DELETE CASCADE, date TEXT NOT NULL,
  price BIGINT NOT NULL DEFAULT 0, buy BIGINT NOT NULL DEFAULT 0);

CREATE TABLE IF NOT EXISTS persons(
  id SERIAL PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
  is_customer INT NOT NULL DEFAULT 0, is_supplier INT NOT NULL DEFAULT 0, is_visitor INT NOT NULL DEFAULT 0,
  is_employee INT NOT NULL DEFAULT 0, is_other INT NOT NULL DEFAULT 0,
  visitor_id INT REFERENCES persons(id),           -- customer's responsible visitor
  phone TEXT, mobile TEXT, address TEXT, national_id TEXT, economic_code TEXT, note TEXT,
  credit_limit BIGINT NOT NULL DEFAULT 0, active INT NOT NULL DEFAULT 1, created TEXT);

CREATE OR REPLACE VIEW visitors AS
  SELECT id, code, name, COALESCE(NULLIF(mobile,''), phone) AS phone, note, active FROM persons WHERE is_visitor=1;

CREATE TABLE IF NOT EXISTS warehouses(
  id SERIAL PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL, is_default INT NOT NULL DEFAULT 0,
  keeper TEXT, address TEXT, active INT NOT NULL DEFAULT 1);

CREATE TABLE IF NOT EXISTS cost_centers(
  id SERIAL PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL, active INT NOT NULL DEFAULT 1);

-- chart of accounts: level 1 = گروه, 2 = کل, 3 = معین. Tafsili = persons / banks / cash boxes / cost centers.
CREATE TABLE IF NOT EXISTS accounts(
  id SERIAL PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL, level INT NOT NULL,
  parent_id INT REFERENCES accounts(id), nature TEXT NOT NULL DEFAULT '-',  -- D / C / -
  sys_key TEXT UNIQUE, active INT NOT NULL DEFAULT 1);

-- ---------- stock ledger: every in/out of every document
CREATE TABLE IF NOT EXISTS stock_moves(
  id BIGSERIAL PRIMARY KEY, date TEXT NOT NULL, item_id INT NOT NULL REFERENCES items(id), wh_id INT NOT NULL REFERENCES warehouses(id),
  qty BIGINT NOT NULL, cost DOUBLE PRECISION NOT NULL DEFAULT 0, doc_type TEXT NOT NULL, doc_id INT NOT NULL, doc_no BIGINT, note TEXT);
CREATE INDEX IF NOT EXISTS ix_sm_item ON stock_moves(item_id, wh_id);
CREATE INDEX IF NOT EXISTS ix_sm_doc ON stock_moves(doc_type, doc_id);
CREATE INDEX IF NOT EXISTS ix_sm_date ON stock_moves(date);

-- ---------- documents
CREATE TABLE IF NOT EXISTS receipts(   -- رسید انبار (موجودی اولیه / ورود متفرقه)
  id SERIAL PRIMARY KEY, no BIGINT NOT NULL, date TEXT NOT NULL, wh_id INT NOT NULL REFERENCES warehouses(id),
  kind TEXT NOT NULL DEFAULT 'in', note TEXT, created_by INT);
CREATE TABLE IF NOT EXISTS receipt_lines(
  id SERIAL PRIMARY KEY, receipt_id INT NOT NULL REFERENCES receipts(id) ON DELETE CASCADE, item_id INT NOT NULL REFERENCES items(id),
  qty BIGINT NOT NULL, unit_cost BIGINT NOT NULL DEFAULT 0);

CREATE TABLE IF NOT EXISTS purchases(  -- فاکتور خرید / برگشت از خرید
  id SERIAL PRIMARY KEY, no BIGINT NOT NULL, kind TEXT NOT NULL DEFAULT 'buy', date TEXT NOT NULL,
  supplier_id INT NOT NULL REFERENCES persons(id), wh_id INT NOT NULL REFERENCES warehouses(id),
  ref_no TEXT, discount BIGINT NOT NULL DEFAULT 0, note TEXT, created_by INT);
CREATE TABLE IF NOT EXISTS purchase_lines(
  id SERIAL PRIMARY KEY, pur_id INT NOT NULL REFERENCES purchases(id) ON DELETE CASCADE, item_id INT NOT NULL REFERENCES items(id),
  qty BIGINT NOT NULL, price BIGINT NOT NULL DEFAULT 0);

CREATE TABLE IF NOT EXISTS transfers(  -- حواله بین انبار
  id SERIAL PRIMARY KEY, no BIGINT NOT NULL, date TEXT NOT NULL, from_wh INT NOT NULL REFERENCES warehouses(id),
  to_wh INT NOT NULL REFERENCES warehouses(id), note TEXT, created_by INT);
CREATE TABLE IF NOT EXISTS transfer_lines(
  id SERIAL PRIMARY KEY, tr_id INT NOT NULL REFERENCES transfers(id) ON DELETE CASCADE, item_id INT NOT NULL REFERENCES items(id), qty BIGINT NOT NULL);

CREATE TABLE IF NOT EXISTS consignments(  -- فاکتور امانی به ویزیتور
  id SERIAL PRIMARY KEY, no BIGINT NOT NULL, date TEXT NOT NULL, visitor_id INT NOT NULL REFERENCES persons(id),
  wh_id INT NOT NULL REFERENCES warehouses(id), status TEXT NOT NULL DEFAULT 'open', note TEXT,
  settle_date TEXT, return_no BIGINT, created_by INT, settled_by INT);
CREATE TABLE IF NOT EXISTS cons_lines(
  id SERIAL PRIMARY KEY, cons_id INT NOT NULL REFERENCES consignments(id) ON DELETE CASCADE, item_id INT NOT NULL REFERENCES items(id),
  qty BIGINT NOT NULL, returned BIGINT NOT NULL DEFAULT 0, price BIGINT NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_cons_status ON consignments(status, settle_date);
CREATE INDEX IF NOT EXISTS ix_cons_visitor ON consignments(visitor_id);
CREATE INDEX IF NOT EXISTS ix_cl_cons ON cons_lines(cons_id);

CREATE TABLE IF NOT EXISTS sales_invoices(  -- فاکتور فروش قطعی (مستقیم یا حاصل تسویه امانی)
  id SERIAL PRIMARY KEY, no BIGINT NOT NULL, kind TEXT NOT NULL DEFAULT 'direct', date TEXT NOT NULL,
  customer_id INT NOT NULL REFERENCES persons(id), visitor_id INT REFERENCES persons(id), wh_id INT NOT NULL REFERENCES warehouses(id),
  cons_id INT REFERENCES consignments(id) ON DELETE CASCADE, discount BIGINT NOT NULL DEFAULT 0, note TEXT, created_by INT);
CREATE TABLE IF NOT EXISTS sales_lines(
  id SERIAL PRIMARY KEY, inv_id INT NOT NULL REFERENCES sales_invoices(id) ON DELETE CASCADE, item_id INT NOT NULL REFERENCES items(id),
  qty BIGINT NOT NULL, price BIGINT NOT NULL DEFAULT 0, cost DOUBLE PRECISION NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_si_date ON sales_invoices(date);
CREATE INDEX IF NOT EXISTS ix_si_customer ON sales_invoices(customer_id);
CREATE INDEX IF NOT EXISTS ix_si_visitor ON sales_invoices(visitor_id);
CREATE INDEX IF NOT EXISTS ix_si_cons ON sales_invoices(cons_id);
CREATE INDEX IF NOT EXISTS ix_sl_inv ON sales_lines(inv_id);

CREATE TABLE IF NOT EXISTS payments(  -- دریافت بابت فاکتور فروش (فاز ۲: خزانه کامل)
  id SERIAL PRIMARY KEY, no BIGINT NOT NULL, inv_id INT NOT NULL REFERENCES sales_invoices(id), date TEXT NOT NULL,
  method TEXT NOT NULL, amount BIGINT NOT NULL, ref TEXT, created_by INT);
CREATE INDEX IF NOT EXISTS ix_pay_inv ON payments(inv_id);
CREATE INDEX IF NOT EXISTS ix_pay_date ON payments(date);
