"""Backups: list / create / download / delete, and restore (from a server backup or an uploaded .dump)."""
import os, re, datetime, subprocess
from flask import render_template, request, redirect, url_for, flash, send_file, abort, session
from werkzeug.security import check_password_hash
from core import app, q, BACKUP_DIR, init_db
import dbl

AUTO_DIR = "/auto-backups"          # daily cron dumps of the db container (mounted read-only)
NAME_RE = re.compile(r"^[\w.-]+\.dump$")


def _pg():
    u = dbl.engine.url
    return (["-h", u.host or "127.0.0.1", "-p", str(u.port or 5432), "-U", u.username], dict(os.environ, PGPASSWORD=u.password or ""), u.database)


def make_backup(prefix="avand"):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dst = os.path.join(BACKUP_DIR, f"{prefix}-{datetime.datetime.now():%Y%m%d-%H%M%S}.dump")
    a, env, dbn = _pg()
    r = subprocess.run(["pg_dump", "-Fc", *a, "-d", dbn, "-f", dst], env=env, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("پشتیبان‌گیری ناموفق بود: " + (r.stderr or "")[:300])
    return dst


def _path(src, name):
    if not NAME_RE.match(name or ""):
        abort(404)
    d = AUTO_DIR if src == "auto" else BACKUP_DIR
    p = os.path.join(d, name)
    if not os.path.isfile(p):
        abort(404)
    return p


def _files():
    out = []
    for src, d in (("manual", BACKUP_DIR), ("auto", AUTO_DIR)):
        if not os.path.isdir(d):
            continue
        for n in os.listdir(d):
            p = os.path.join(d, n)
            if NAME_RE.match(n) and os.path.isfile(p) and os.access(p, os.R_OK):
                st = os.stat(p)
                out.append({"src": src, "name": n, "size": st.st_size, "mtime": datetime.datetime.fromtimestamp(st.st_mtime)})
    return sorted(out, key=lambda f: f["mtime"], reverse=True)


def toc_without_schema(path):
    """restore list without the public schema itself (we recreate it; its comment/ACL need superuser)"""
    r = subprocess.run(["pg_restore", "-l", path], capture_output=True, text=True)
    keep = [l for l in r.stdout.splitlines() if not re.search(r"(SCHEMA - public|COMMENT - SCHEMA public|ACL - SCHEMA public) ", l)]
    lst = path + ".toc"
    with open(lst, "w") as f:
        f.write("\n".join(keep) + "\n")
    return lst


def restore_file(path):
    """drop the public schema and load the dump; on failure reload the safety backup. Returns safety backup path."""
    a, env, dbn = _pg()
    chk = subprocess.run(["pg_restore", "-l", path], capture_output=True, text=True)
    if chk.returncode != 0 or "TABLE DATA" not in chk.stdout:
        raise ValueError("فایل پشتیبان معتبر نیست (باید خروجی pg_dump با فرمت custom باشد).")
    safety = make_backup("pre-restore")

    def load(p):
        dbl.close()
        dbl.engine.dispose()
        c = dbl.engine.connect()
        c.exec_driver_sql("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()")
        c.exec_driver_sql("DROP SCHEMA IF EXISTS public CASCADE")
        c.exec_driver_sql("CREATE SCHEMA public")
        c.commit()
        c.close()
        dbl.engine.dispose()
        lst = toc_without_schema(p)
        try:
            return subprocess.run(["pg_restore", "--no-owner", "--no-privileges", "--exit-on-error", "-L", lst, *a, "-d", dbn, p],
                                  env=env, capture_output=True, text=True)
        finally:
            os.remove(lst)
    r = load(path)
    if r.returncode != 0:
        load(safety)
        init_db()
        raise ValueError("بازیابی ناموفق بود و اطلاعات قبلی برگردانده شد: " + (r.stderr or "")[-300:])
    init_db()      # upgrade an older backup to the current schema (and rebuild ledger if needed)
    return safety


@app.route("/backups", methods=["GET", "POST"])
def backups():
    if request.method == "POST":
        try:
            p = make_backup()
            flash(f"پشتیبان {os.path.basename(p)} روی سرور ساخته شد.", "ok")
        except RuntimeError as e:
            flash(str(e), "bad")
        return redirect(url_for("backups"))
    return render_template("backups.html", files=_files())


@app.route("/backup")
def backup():
    """create a backup and download it"""
    try:
        return send_file(make_backup(), as_attachment=True)
    except RuntimeError as e:
        flash(str(e), "bad")
        return redirect(url_for("backups"))


@app.route("/backups/<src>/<name>")
def backup_file(src, name):
    return send_file(_path(src, name), as_attachment=True)


@app.post("/backups/<name>/delete")
def backup_delete(name):
    os.remove(_path("manual", name))
    flash(f"فایل {name} حذف شد.", "ok")
    return redirect(url_for("backups"))


@app.post("/backups/restore")
def backup_restore():
    F = request.form
    u = q("SELECT * FROM users WHERE id=?", (session.get("uid"),), one=True)
    if F.get("confirm", "").strip() != "بازیابی" or F.get("ok") != "1":
        flash("عبارت «بازیابی» را تایپ و تیک تایید را بزنید.", "bad")
        return redirect(url_for("backups"))
    if not u or not check_password_hash(u["pw"], F.get("password", "")):
        flash("رمز عبور درست نیست.", "bad")
        return redirect(url_for("backups"))
    up = request.files.get("file")
    tmp = None
    try:
        if up and up.filename:
            os.makedirs(BACKUP_DIR, exist_ok=True)
            tmp = os.path.join(BACKUP_DIR, f"uploaded-{datetime.datetime.now():%Y%m%d-%H%M%S}.dump")
            up.save(tmp)
            path = tmp
        else:
            path = _path(F.get("src"), F.get("name"))
        safety = restore_file(path)
    except ValueError as e:
        if tmp and os.path.exists(tmp) and "معتبر نیست" in str(e):
            os.remove(tmp)
        flash(str(e), "bad")
        return redirect(url_for("backups"))
    session.clear()
    flash(f"اطلاعات از پشتیبان بازیابی شد. نسخه قبل از بازیابی: {os.path.basename(safety)}. دوباره وارد شوید.", "ok")
    return redirect(url_for("login"))
