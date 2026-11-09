import io, re, datetime
from flask import render_template, send_file, request, flash, redirect
from core import FONT_DIR, BASE, company, logo_file, POWERED
from jalali import today_j


def cell_v(c):
    return c.get("v") if isinstance(c, dict) else c


def fmt(v, kind="text"):
    if v is None or v == "":
        return ""
    if kind in ("money", "num"):
        try:
            n = int(round(float(v)))
        except (TypeError, ValueError):
            return str(v)
        return "(" + "{:,}".format(-n) + ")" if n < 0 and kind == "money" else "{:,}".format(n)
    if kind == "pct":
        try:
            return ("%+.1f%%" % float(v)) if v is not None else "—"
        except (TypeError, ValueError):
            return str(v)
    return str(v)


def _fname(title, ext):
    safe = re.sub(r"[^\w\u0600-\u06FF-]+", "_", title).strip("_") or "report"
    return f"{safe}_{today_j().replace('/', '')}.{ext}"


def _send(data, name, mime):
    return send_file(io.BytesIO(data), as_attachment=True, download_name=name, mimetype=mime)


# ---------------- Excel
def xlsx(spec):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    F = "Vazirmatn"
    thin = Side(style="thin", color="CBD5E1")
    B = Border(left=thin, right=thin, top=thin, bottom=thin)
    H = PatternFill("solid", fgColor="1E3A8A")
    T = PatternFill("solid", fgColor="E2E8F0")
    NF = {"money": '#,##0;(#,##0)', "num": "#,##0", "pct": '0.0"%"'}
    wb = Workbook()
    wb.remove(wb.active)
    used = set()

    def sheet(name):
        n = re.sub(r"[\[\]:*?/\\]", " ", name)[:31] or "Sheet"
        base, i = n, 2
        while n in used:
            n = f"{base[:28]} {i}"; i += 1
        used.add(n)
        ws = wb.create_sheet(n)
        ws.sheet_view.rightToLeft = True
        return ws

    logo = logo_file()

    def head(ws, width):
        w = max(width, 3)
        ws.cell(1, 1, spec["title"]).font = Font(name=F, bold=True, size=14)
        ws.cell(2, 1, f"{company()} | {spec.get('sub', '')} | تاریخ تهیه: {today_j()} | {POWERED}").font = Font(name=F, size=9, color="64748B")
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=w - 1)
        ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=w - 1)
        ws.row_dimensions[1].height = 42
        if logo:
            try:
                from openpyxl.drawing.image import Image as XImg
                img = XImg(logo)
                ratio = img.width / float(img.height or 1)
                img.height = 52; img.width = int(52 * ratio)
                ws.add_image(img, f"{get_column_letter(w)}1")
            except Exception:
                pass

    if spec.get("kpis"):
        ws = sheet("خلاصه")
        head(ws, 2)
        for i, (lab, val, kind) in enumerate(spec["kpis"], 4):
            a, b = ws.cell(i, 1, lab), ws.cell(i, 2, val if kind != "text" else str(val))
            for c in (a, b):
                c.font = Font(name=F, bold=(c is a)); c.border = B
            if kind in NF and val is not None:
                b.number_format = NF[kind]
        ws.column_dimensions["A"].width = 34; ws.column_dimensions["B"].width = 22
        sheet_print(ws, spec["title"])

    for sec in spec["sections"]:
        ws = sheet(sec.get("title") or spec["title"])
        cols = sec["cols"]
        head(ws, len(cols))
        r0 = 4
        if sec.get("title"):
            ws.cell(r0, 1, sec["title"]).font = Font(name=F, bold=True, size=11)
            r0 += 1
        for j, (lab, _) in enumerate(cols, 1):
            c = ws.cell(r0, j, lab)
            c.font = Font(name=F, bold=True, color="FFFFFF"); c.fill = H; c.border = B
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.row_dimensions[r0].height = 30
        widths = [len(str(l)) + 4 for l, _ in cols]
        rows = list(sec["rows"]) + ([sec["total"]] if sec.get("total") else [])
        for i, row in enumerate(rows, r0 + 1):
            is_tot = sec.get("total") is not None and i == r0 + len(rows)
            for j, cell in enumerate(row, 1):
                v = cell_v(cell)
                kind = cols[j - 1][1]
                if kind in NF and isinstance(v, (int, float)):
                    c = ws.cell(i, j, v); c.number_format = NF[kind]
                else:
                    c = ws.cell(i, j, "" if v is None else str(v))
                c.font = Font(name=F, bold=is_tot); c.border = B
                if is_tot:
                    c.fill = T
                widths[j - 1] = max(widths[j - 1], min(len(fmt(v, kind)) + 3, 45))
        for j, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(j)].width = w
        ws.freeze_panes = ws.cell(r0 + 1, 1)
        ws.auto_filter.ref = f"A{r0}:{get_column_letter(len(cols))}{r0 + len(rows)}"
        sheet_print(ws, spec["title"], r0, len(cols) > 6)
    if not wb.worksheets:
        sheet("گزارش")
    buf = io.BytesIO()
    wb.save(buf)
    return _send(buf.getvalue(), _fname(spec["title"], "xlsx"), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ---------------- PDF
def _pdf(html):
    try:
        from weasyprint import HTML
    except Exception:
        flash("خروجی PDF روی سرور نصب نیست (weasyprint).", "bad")
        return None
    return HTML(string=html, base_url=BASE).write_pdf()


def report_pdf(spec):
    wide = max((len(s["cols"]) for s in spec["sections"]), default=0) > 6
    html = render_template("pdf_report.html", spec=spec, fmt=fmt, cell_v=cell_v, font_dir=FONT_DIR, wide=wide, now=today_j(), logo=logo_file())
    data = _pdf(html)
    if data is None:
        return redirect(request.path)
    return _send(data, _fname(spec["title"], "pdf"), "application/pdf")


def doc_pdf(doc):
    html = render_template("pdf_doc.html", doc=doc, font_dir=FONT_DIR, now=today_j(), logo=logo_file())
    data = _pdf(html)
    if data is None:
        return redirect(request.path)
    return _send(data, _fname(f"{doc['title']}_{doc['no']}", "pdf"), "application/pdf")


def list_out(title, sub, cols, rows, total=None):
    """Excel / PDF of a plain list page (?fmt=xlsx|pdf); None when the page itself is requested"""
    f = request.args.get("fmt")
    if f not in ("xlsx", "pdf"):
        return None
    spec = {"title": title, "sub": sub, "sections": [{"cols": cols, "rows": rows, "total": total}]}
    return xlsx(spec) if f == "xlsx" else report_pdf(spec)


def sheet_print(ws, title, title_row=None, landscape=False):
    """page setup, repeating header row, header/footer with page numbers for printing an Excel sheet"""
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.orientation = "landscape" if landscape else "portrait"
    ws.page_setup.fitToWidth = 1; ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_options.horizontalCentered = True
    ws.page_margins.top = ws.page_margins.bottom = 0.6
    if title_row:
        ws.print_title_rows = f"{title_row}:{title_row}"
    ws.oddHeader.right.text = company(); ws.oddHeader.right.size = 8
    ws.oddHeader.left.text = title; ws.oddHeader.left.size = 8
    ws.oddFooter.center.text = "صفحه &P از &N"; ws.oddFooter.center.size = 8
    ws.oddFooter.right.text = f"تاریخ تهیه: {today_j()}"; ws.oddFooter.right.size = 8
    ws.oddFooter.left.text = POWERED; ws.oddFooter.left.size = 7


def out(spec, template="report.html", **kw):
    f = request.args.get("fmt")
    if f == "xlsx":
        return xlsx(spec)
    if f == "pdf":
        return report_pdf(spec)
    return render_template(template, spec=spec, fmt=fmt, cell_v=cell_v, **kw)
