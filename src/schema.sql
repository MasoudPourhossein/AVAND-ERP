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

-- payments: legacy table converted to the treasury view below (see core.init_db)

-- ---------- v2: standard invoices (line discount + VAT), sales return, seller/buyer info
ALTER TABLE sales_lines ADD COLUMN IF NOT EXISTS disc BIGINT NOT NULL DEFAULT 0;
ALTER TABLE sales_lines ADD COLUMN IF NOT EXISTS vat_rate DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE sales_lines ADD COLUMN IF NOT EXISTS vat BIGINT NOT NULL DEFAULT 0;
ALTER TABLE purchase_lines ADD COLUMN IF NOT EXISTS disc BIGINT NOT NULL DEFAULT 0;
ALTER TABLE purchase_lines ADD COLUMN IF NOT EXISTS vat_rate DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE purchase_lines ADD COLUMN IF NOT EXISTS vat BIGINT NOT NULL DEFAULT 0;
ALTER TABLE sales_invoices ADD COLUMN IF NOT EXISTS due_date TEXT;
ALTER TABLE sales_invoices ADD COLUMN IF NOT EXISTS pay_terms TEXT;
ALTER TABLE sales_invoices ADD COLUMN IF NOT EXISTS ref_no TEXT;
ALTER TABLE sales_invoices ADD COLUMN IF NOT EXISTS ret_of INT REFERENCES sales_invoices(id) ON DELETE SET NULL;
ALTER TABLE sales_invoices ADD COLUMN IF NOT EXISTS updated_by INT;
ALTER TABLE purchases ADD COLUMN IF NOT EXISTS due_date TEXT;
ALTER TABLE purchases ADD COLUMN IF NOT EXISTS pay_terms TEXT;
ALTER TABLE purchases ADD COLUMN IF NOT EXISTS updated_by INT;
ALTER TABLE items ADD COLUMN IF NOT EXISTS vat_rate DOUBLE PRECISION;
ALTER TABLE persons ADD COLUMN IF NOT EXISTS ptype TEXT NOT NULL DEFAULT 'real';
ALTER TABLE persons ADD COLUMN IF NOT EXISTS postal_code TEXT;
ALTER TABLE persons ADD COLUMN IF NOT EXISTS reg_no TEXT;

-- invoice-level totals (views are rebuilt on every start so new columns appear)
DROP VIEW IF EXISTS v_si;
CREATE VIEW v_si AS
SELECT s.*, CASE WHEN s.kind='return' THEN -1 ELSE 1 END AS sg,
  COALESCE(t.gross,0) AS gross, COALESCE(t.ldisc,0) AS ldisc, COALESCE(t.vat,0) AS vat_total, COALESCE(t.qty,0) AS qty_total,
  COALESCE(t.cogs,0) AS cogs_total, COALESCE(t.gross,0)-COALESCE(t.ldisc,0)-s.discount AS net_total,
  COALESCE(t.gross,0)-COALESCE(t.ldisc,0)+COALESCE(t.vat,0)-s.discount AS total
FROM sales_invoices s LEFT JOIN (SELECT inv_id, SUM(qty*price) gross, SUM(disc) ldisc, SUM(vat) vat, SUM(qty) qty, SUM(qty*cost) cogs
  FROM sales_lines GROUP BY inv_id) t ON t.inv_id=s.id;

DROP VIEW IF EXISTS v_pu;
CREATE VIEW v_pu AS
SELECT u.*, CASE WHEN u.kind='return' THEN -1 ELSE 1 END AS sg,
  COALESCE(t.gross,0) AS gross, COALESCE(t.ldisc,0) AS ldisc, COALESCE(t.vat,0) AS vat_total, COALESCE(t.qty,0) AS qty_total,
  COALESCE(t.gross,0)-COALESCE(t.ldisc,0)-u.discount AS net_total,
  COALESCE(t.gross,0)-COALESCE(t.ldisc,0)+COALESCE(t.vat,0)-u.discount AS total
FROM purchases u LEFT JOIN (SELECT pur_id, SUM(qty*price) gross, SUM(disc) ldisc, SUM(vat) vat, SUM(qty) qty
  FROM purchase_lines GROUP BY pur_id) t ON t.pur_id=u.id;


-- ---------- v3: treasury
CREATE TABLE IF NOT EXISTS cash_boxes(id SERIAL PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL, keeper TEXT,
  is_default INT NOT NULL DEFAULT 0, active INT NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS banks(id SERIAL PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL, bank_name TEXT, branch TEXT,
  account_no TEXT, iban TEXT, card_no TEXT, is_pos INT NOT NULL DEFAULT 0, is_default INT NOT NULL DEFAULT 0, active INT NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS cheques(
  id SERIAL PRIMARY KEY, kind TEXT NOT NULL,                 -- recv | pay
  serial TEXT, sayad TEXT, bank_name TEXT, branch TEXT, account_no TEXT, owner TEXT,
  amount BIGINT NOT NULL, issue_date TEXT, due_date TEXT NOT NULL,
  person_id INT REFERENCES persons(id),                      -- received from / paid to
  holder_id INT REFERENCES persons(id),                      -- received cheque spent to (supplier)
  bank_id INT REFERENCES banks(id),                          -- pay: our account, recv: bank deposited to
  status TEXT NOT NULL, note TEXT, created TEXT);
CREATE INDEX IF NOT EXISTS ix_chq_status ON cheques(kind, status, due_date);
CREATE TABLE IF NOT EXISTS tr_docs(
  id SERIAL PRIMARY KEY, no BIGINT NOT NULL, kind TEXT NOT NULL,   -- recv | pay | transfer
  date TEXT NOT NULL, person_id INT REFERENCES persons(id), acc_id INT REFERENCES accounts(id), cc_id INT REFERENCES cost_centers(id),
  inv_id INT REFERENCES sales_invoices(id) ON DELETE SET NULL, pur_id INT REFERENCES purchases(id) ON DELETE SET NULL,
  visitor_id INT REFERENCES persons(id), note TEXT, created_by INT,
  from_box INT REFERENCES cash_boxes(id), from_bank INT REFERENCES banks(id), to_box INT REFERENCES cash_boxes(id), to_bank INT REFERENCES banks(id));
CREATE TABLE IF NOT EXISTS tr_lines(
  id SERIAL PRIMARY KEY, doc_id INT NOT NULL REFERENCES tr_docs(id) ON DELETE CASCADE,
  method TEXT NOT NULL,                                       -- cash | pos | bank | cheque | cheque_out | transfer
  amount BIGINT NOT NULL, box_id INT REFERENCES cash_boxes(id), bank_id INT REFERENCES banks(id),
  cheque_id INT REFERENCES cheques(id), ref TEXT);
CREATE INDEX IF NOT EXISTS ix_trd_person ON tr_docs(person_id);
CREATE INDEX IF NOT EXISTS ix_trd_inv ON tr_docs(inv_id);
CREATE INDEX IF NOT EXISTS ix_trd_date ON tr_docs(date);
CREATE INDEX IF NOT EXISTS ix_trl_doc ON tr_lines(doc_id);
CREATE TABLE IF NOT EXISTS cheque_events(
  id SERIAL PRIMARY KEY, cheque_id INT NOT NULL REFERENCES cheques(id) ON DELETE CASCADE, date TEXT NOT NULL,
  from_status TEXT, to_status TEXT NOT NULL, bank_id INT REFERENCES banks(id), box_id INT REFERENCES cash_boxes(id),
  person_id INT REFERENCES persons(id), tr_doc_id INT REFERENCES tr_docs(id) ON DELETE CASCADE, note TEXT, created_by INT);

-- ---------- v3: general ledger (double entry)
CREATE TABLE IF NOT EXISTS vouchers(
  id SERIAL PRIMARY KEY, no BIGINT NOT NULL, date TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'auto',  -- auto | manual
  src_type TEXT, src_id INT, descr TEXT, created_by INT);
CREATE INDEX IF NOT EXISTS ix_vch_src ON vouchers(src_type, src_id);
CREATE INDEX IF NOT EXISTS ix_vch_date ON vouchers(date);
CREATE TABLE IF NOT EXISTS voucher_lines(
  id BIGSERIAL PRIMARY KEY, voucher_id INT NOT NULL REFERENCES vouchers(id) ON DELETE CASCADE, account_id INT NOT NULL REFERENCES accounts(id),
  person_id INT REFERENCES persons(id), bank_id INT REFERENCES banks(id), box_id INT REFERENCES cash_boxes(id),
  cc_id INT REFERENCES cost_centers(id), debit BIGINT NOT NULL DEFAULT 0, credit BIGINT NOT NULL DEFAULT 0, descr TEXT);
CREATE INDEX IF NOT EXISTS ix_vl_v ON voucher_lines(voucher_id);
CREATE INDEX IF NOT EXISTS ix_vl_acc ON voucher_lines(account_id);
CREATE INDEX IF NOT EXISTS ix_vl_person ON voucher_lines(person_id);

-- received amounts per sales invoice (compat view used by sales reports)
DROP VIEW IF EXISTS payments;
CREATE VIEW payments AS
SELECT l.id, d.no, d.inv_id, d.date, CASE WHEN l.method='cheque' THEN 'cheque' WHEN l.method IN ('bank') THEN 'bank'
       WHEN l.method='pos' THEN 'pos' ELSE 'cash' END AS method, l.amount, COALESCE(l.ref, c.serial) AS ref, d.created_by,
       d.person_id, d.visitor_id, d.id AS doc_id
FROM tr_lines l JOIN tr_docs d ON d.id=l.doc_id LEFT JOIN cheques c ON c.id=l.cheque_id WHERE d.kind='recv';

-- GL balance view
DROP VIEW IF EXISTS v_gl;
CREATE VIEW v_gl AS SELECT vl.*, v.date, v.no AS vno, v.kind AS vkind, v.src_type, v.src_id, v.descr AS vdescr, a.code AS acode, a.name AS aname
FROM voucher_lines vl JOIN vouchers v ON v.id=vl.voucher_id JOIN accounts a ON a.id=vl.account_id;
