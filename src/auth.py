import os, time, datetime, subprocess
from flask import g, render_template, request, redirect, url_for, flash, send_file, abort, session, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from core import (app, q, x, v1, db, to_int, uid, PERMS, PRESETS, perms_of, can, BACKUP_DIR, LOGO_PATH, logo_file,
                  set_setting, company)
from jalali import today_j
import dbl


# ---------------- auth
_fails = {}


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if q("SELECT 1 FROM users LIMIT 1", one=True):
        return redirect(url_for("login"))
    if request.method == "POST":
        un, pw = request.form.get("username", "").strip(), request.form.get("password", "")
        if len(un) < 3 or len(pw) < 6 or pw != request.form.get("password2"):
            flash("نام کاربری حداقل ۳ و رمز حداقل ۶ کاراکتر؛ تکرار رمز باید یکسان باشد.", "bad")
        else:
            x("INSERT INTO users(username,fullname,pw,role,perms,created) VALUES(?,?,?,?,?,?)",
              (un, request.form.get("fullname", "").strip(), generate_password_hash(pw), "admin", ",".join(PERMS), today_j()))
            db().commit()
            flash("کاربر مدیر ساخته شد. وارد شوید.", "ok")
            return redirect(url_for("login"))
    return render_template("auth.html", mode="setup")


@app.route("/login", methods=["GET", "POST"])
def login():
    if not q("SELECT 1 FROM users LIMIT 1", one=True):
        return redirect(url_for("setup"))
    if request.method == "POST":
        key = (request.remote_addr or "-", request.form.get("username", "").strip().lower())
        n, until = _fails.get(key, (0, 0))
        if until > time.time():
            flash(f"ورود موقتا قفل است. {int(until - time.time()) // 60 + 1} دقیقه دیگر تلاش کنید.", "bad")
            return render_template("auth.html", mode="login")
        u = q("SELECT * FROM users WHERE username=? AND deleted=0", (request.form.get("username", "").strip(),), one=True)
        if u and u["active"] and check_password_hash(u["pw"], request.form.get("password", "")):
            _fails.pop(key, None)
            session.clear()
            session.permanent = True
            session["uid"] = u["id"]
            nxt = request.args.get("next")
            return redirect(nxt if nxt and nxt.startswith("/") and not nxt.startswith("//") else url_for("dashboard"))
        n += 1
        _fails[key] = (0, time.time() + 300) if n >= 5 else (n, 0)
        flash("نام کاربری یا رمز عبور اشتباه است.", "bad")
    return render_template("auth.html", mode="login")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/password", methods=["GET", "POST"])
def password():
    if request.method == "POST":
        new = request.form.get("new", "")
        if not check_password_hash(g.user["pw"], request.form.get("old", "")):
            flash("رمز فعلی اشتباه است.", "bad")
        elif len(new) < 6 or new != request.form.get("new2"):
            flash("رمز جدید حداقل ۶ کاراکتر و تکرار آن یکسان باشد.", "bad")
        else:
            x("UPDATE users SET pw=? WHERE id=?", (generate_password_hash(new), uid()))
            db().commit()
            flash("رمز عبور تغییر کرد.", "ok")
            return redirect(url_for("dashboard"))
    return render_template("password.html")


# ---------------- users
def _form_perms():
    return [p for p in request.form.getlist("perm") if p in PERMS]


def _other_admins(user_id):
    return [r for r in q("SELECT * FROM users WHERE active=1 AND deleted=0 AND id<>?", (user_id,)) if "users" in perms_of(r)]


@app.route("/users", methods=["GET", "POST"])
def users():
    if request.method == "POST":
        un, pw = request.form.get("username", "").strip(), request.form.get("password", "")
        role = request.form.get("role") if request.form.get("role") in PRESETS else "viewer"
        perms = _form_perms()
        if len(un) < 3 or len(pw) < 6:
            flash("نام کاربری حداقل ۳ و رمز حداقل ۶ کاراکتر لازم است.", "bad")
        elif not perms:
            flash("حداقل یک دسترسی انتخاب کنید.", "bad")
        elif q("SELECT 1 FROM users WHERE username=?", (un,), one=True):
            flash("این نام کاربری وجود دارد.", "bad")
        else:
            x("INSERT INTO users(username,fullname,pw,role,perms,created) VALUES(?,?,?,?,?,?)",
              (un, request.form.get("fullname", "").strip(), generate_password_hash(pw), role, ",".join(perms), today_j()))
            db().commit()
            flash(f"کاربر «{un}» ساخته شد.", "ok")
        return redirect(url_for("users"))
    return render_template("users.html", rows=q("SELECT * FROM users WHERE deleted=0 ORDER BY id"), perms_of=perms_of)


@app.route("/users/<int:user_id>", methods=["GET", "POST"])
def user_edit(user_id):
    u = q("SELECT * FROM users WHERE id=? AND deleted=0", (user_id,), one=True) or abort(404)
    if request.method == "POST":
        perms = _form_perms()
        active = 1 if request.form.get("active") else 0
        role = request.form.get("role") if request.form.get("role") in PRESETS else u["role"]
        if ("users" not in perms or not active) and "users" in perms_of(u) and not _other_admins(user_id):
            flash("حداقل یک کاربر فعال با دسترسی مدیریت کاربران باید بماند.", "bad")
            return redirect(url_for("user_edit", user_id=user_id))
        if not perms:
            flash("حداقل یک دسترسی انتخاب کنید.", "bad")
            return redirect(url_for("user_edit", user_id=user_id))
        pw = request.form.get("password", "")
        if pw and len(pw) < 6:
            flash("رمز حداقل ۶ کاراکتر باشد.", "bad")
            return redirect(url_for("user_edit", user_id=user_id))
        x("UPDATE users SET fullname=?, role=?, perms=?, active=? WHERE id=?",
          (request.form.get("fullname", "").strip(), role, ",".join(perms), active, user_id))
        if pw:
            x("UPDATE users SET pw=? WHERE id=?", (generate_password_hash(pw), user_id))
        db().commit()
        flash(f"کاربر «{u['username']}» ذخیره شد.", "ok")
        return redirect(url_for("users"))
    return render_template("user_edit.html", u=u, up=perms_of(u))


@app.post("/users/<int:user_id>/delete")
def user_delete(user_id):
    u = q("SELECT * FROM users WHERE id=? AND deleted=0", (user_id,), one=True) or abort(404)
    if user_id == uid():
        flash("نمی‌توانید حساب خودتان را حذف کنید.", "bad")
        return redirect(url_for("users"))
    if "users" in perms_of(u) and not _other_admins(user_id):
        flash("حداقل یک کاربر فعال با دسترسی مدیریت کاربران باید بماند.", "bad")
        return redirect(url_for("users"))
    # soft delete: keeps the name on documents this user registered, frees the username
    x("UPDATE users SET deleted=1, active=0, username=? WHERE id=?", (f"{u['username']}#del{user_id}", user_id))
    db().commit()
    flash(f"کاربر «{u['username']}» حذف شد.", "ok")
    return redirect(url_for("users"))


# ---------------- settings, logo, search
@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        from core import SELLER_KEYS
        title = request.form.get("title", "").strip()
        if title:
            set_setting("title", title[:80])
        for k, _ in SELLER_KEYS:
            set_setting(k, request.form.get(k, "").strip()[:300])
        set_setting("vat_on", "1" if request.form.get("vat_on") else "0")
        try:
            vr = float(request.form.get("vat_rate", "10").replace("٫", ".") or 0)
        except ValueError:
            vr = -1
        if not 0 <= vr <= 100:
            db().rollback()
            flash("نرخ مالیات نامعتبر است.", "bad")
            return redirect(url_for("settings"))
        set_setting("vat_rate", str(vr))
        if request.form.get("remove_logo") and os.path.exists(LOGO_PATH):
            os.remove(LOGO_PATH)
        f = request.files.get("logo")
        if f and f.filename:
            try:
                from PIL import Image
                im = Image.open(f.stream)
                im.load()
                im = im.convert("RGBA")
                im.thumbnail((600, 600))
                tmp = LOGO_PATH + ".tmp"
                im.save(tmp, "PNG")
                os.replace(tmp, LOGO_PATH)
            except Exception:
                db().rollback()
                flash("فایل لوگو معتبر نیست (PNG، JPG یا WEBP).", "bad")
                return redirect(url_for("settings"))
        db().commit()
        flash("تنظیمات ذخیره شد.", "ok")
        return redirect(url_for("settings"))
    from core import SELLER_KEYS, seller_info, setting
    return render_template("settings.html", title=company(), keys=SELLER_KEYS, si=seller_info(),
                           vat_on=setting("vat_on", "0") == "1", vat_rate=setting("vat_rate", "10"))


@app.route("/logo.png")
def logo():
    if not logo_file():
        abort(404)
    return send_file(LOGO_PATH, mimetype="image/png", max_age=300)


@app.route("/api/search")
def api_search():
    s = (request.args.get("q") or "").strip()
    out = []
    if not s:
        return jsonify(out)
    like = f"%{s}%"
    if can("items", "rep_stock", "amani", "receipt", "sale", "purchase"):
        for r in q("SELECT id, code, name FROM items WHERE code ILIKE ? OR name ILIKE ? ORDER BY (code ILIKE ?) DESC, code LIMIT 8", (like, like, s + "%")):
            out.append({"t": "کالا", "l": f"{r['code']} - {r['name']}", "u": url_for("item_edit", iid=r["id"])})
    if can("persons", "rep_fin", "rep_sales"):
        for r in q("""SELECT id, code, name, is_customer, is_supplier, is_visitor, is_employee FROM persons
                      WHERE code ILIKE ? OR name ILIKE ? OR phone ILIKE ? OR mobile ILIKE ? ORDER BY code LIMIT 8""", (like,) * 4):
            k = "ویزیتور" if r["is_visitor"] else "مشتری" if r["is_customer"] else "تامین‌کننده" if r["is_supplier"] else "کارمند" if r["is_employee"] else "شخص"
            out.append({"t": k, "l": f"{r['code']} - {r['name']}", "u": url_for("person", pid=r["id"])})
    if s.isdigit():
        n = int(s)
        if can("amani", "settle", "rep_sales"):
            for r in q("SELECT c.id, c.no, v.name FROM consignments c JOIN persons v ON v.id=c.visitor_id WHERE c.no=?", (n,)):
                out.append({"t": "فاکتور امانی", "l": f"{r['no']} - {r['name']}", "u": url_for("amani_view", cid=r["id"])})
            for r in q("SELECT id, return_no FROM consignments WHERE return_no=?", (n,)):
                out.append({"t": "برگه مرجوعی", "l": str(r["return_no"]), "u": url_for("print_return", cid=r["id"])})
        if can("payment", "settle", "sale", "rep_sales", "rep_fin"):
            for r in q("SELECT s.id, s.no, c.name FROM sales_invoices s JOIN persons c ON c.id=s.customer_id WHERE s.no=?", (n,)):
                out.append({"t": "فاکتور فروش", "l": f"{r['no']} - {r['name']}", "u": url_for("invoice", iid=r["id"])})
            for r in q("SELECT p.no, p.inv_id, c.name FROM payments p JOIN sales_invoices s ON s.id=p.inv_id JOIN persons c ON c.id=s.customer_id WHERE p.no=?", (n,)):
                out.append({"t": "سند دریافت", "l": f"{r['no']} - {r['name']}", "u": url_for("invoice", iid=r["inv_id"])})
        if can("purchase", "rep_stock", "rep_fin"):
            for r in q("SELECT p.id, p.no, s.name FROM purchases p JOIN persons s ON s.id=p.supplier_id WHERE p.no=?", (n,)):
                out.append({"t": "فاکتور خرید", "l": f"{r['no']} - {r['name']}", "u": url_for("purchase_view", pid=r["id"])})
        if can("receipt", "transfer", "rep_stock"):
            for r in q("SELECT id, no, date FROM receipts WHERE no=?", (n,)):
                out.append({"t": "رسید انبار", "l": f"{r['no']} - {r['date']}", "u": url_for("receipt_view", rid=r["id"])})
            for r in q("SELECT id, no, date FROM transfers WHERE no=?", (n,)):
                out.append({"t": "حواله انبار", "l": f"{r['no']} - {r['date']}", "u": url_for("transfer_view", tid=r["id"])})
    return jsonify(out[:24])


@app.route("/backup")
def backup():
    """pg_dump custom format (restore: pg_restore -c -d DB file)"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dst = os.path.join(BACKUP_DIR, f"avand-{datetime.datetime.now():%Y%m%d-%H%M}.dump")
    url = dbl.engine.url
    env = dict(os.environ, PGPASSWORD=url.password or "")
    r = subprocess.run(["pg_dump", "-Fc", "-h", url.host or "127.0.0.1", "-p", str(url.port or 5432), "-U", url.username,
                        "-d", url.database, "-f", dst], env=env, capture_output=True, text=True)
    if r.returncode != 0:
        flash("پشتیبان‌گیری ناموفق بود: " + (r.stderr or "")[:200], "bad")
        return redirect(url_for("dashboard"))
    return send_file(dst, as_attachment=True)
