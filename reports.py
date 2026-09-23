"""Report builders for Bidvest ESG: PDF (polished/visual), DOCX and XLSX."""
import io

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image,
    PageBreak, HRFlowable, NextPageTemplate, PageTemplate, Frame, BaseDocTemplate,
)
from reportlab.pdfgen import canvas as pdfcanvas

NAVY = colors.HexColor("#141549")
BLUE = colors.HexColor("#4a99d1")
BRASS = colors.HexColor("#c9896d")
GREEN = colors.HexColor("#1f8a4c")
AMBER = colors.HexColor("#c98a12")
RED = colors.HexColor("#c0392b")
GREY = colors.HexColor("#6b7280")
LIGHT = colors.HexColor("#f4f6f9")

STATUS_COLORS_MPL = {
    "Not started": "#9aa2b1", "In progress": "#c98a12", "Collected": "#1f8a4c",
    "Verified": "#146c38", "N/A": "#cccccc",
}

STATUSES = ["Not started", "In progress", "Collected", "Verified", "N/A"]


# ------------------------------------------------------------- chart helpers

def _pillar_bar_chart(pillars):
    fig, ax = plt.subplots(figsize=(6.2, 3), dpi=170)
    names = [p["pillar"] for p in pillars]
    pcts = [p["pct_complete"] for p in pillars]
    bar_colors = ["#4a99d1", "#c9896d", "#141549"][: len(names)]
    bars = ax.bar(names, pcts, color=bar_colors, width=0.55, zorder=3)
    for b, p in zip(bars, pcts):
        ax.text(b.get_x() + b.get_width() / 2, b.get_height() + 2, f"{p:.0f}%",
                ha="center", va="bottom", fontsize=11, fontweight="bold", color="#141549")
        # subtle "3D" drop shadow behind each bar
        ax.add_patch(plt.Rectangle((b.get_x() + 0.04, 0), b.get_width(), b.get_height(),
                                    color="#00000022", zorder=1))
    ax.set_ylim(0, 110)
    ax.set_ylabel("% complete", fontsize=9, color="#6b7280")
    ax.spines[["top", "right"]].set_visible(False)
    ax.spines[["left", "bottom"]].set_color("#d7dce3")
    ax.tick_params(colors="#3a3f4b", labelsize=10)
    ax.set_axisbelow(True)
    ax.grid(axis="y", color="#e2e6ec", linewidth=0.8, zorder=0)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", transparent=True)
    plt.close(fig)
    buf.seek(0)
    return buf


def _status_donut(total):
    fig, ax = plt.subplots(figsize=(3.6, 3.6), dpi=170)
    labels = [s for s in STATUSES if total.get(s, 0) > 0]
    sizes = [total[s] for s in labels]
    clrs = [STATUS_COLORS_MPL[s] for s in labels]
    if not sizes:
        labels, sizes, clrs = ["No data"], [1], ["#e2e6ec"]
    wedges, _ = ax.pie(sizes, colors=clrs, startangle=90, counterclock=False,
                        wedgeprops=dict(width=0.42, edgecolor="white", linewidth=2))
    ax.text(0, 0.06, f"{total.get('pct_complete', 0):.0f}%", ha="center", va="center",
            fontsize=20, fontweight="bold", color="#141549")
    ax.text(0, -0.18, "complete", ha="center", va="center", fontsize=9, color="#6b7280")
    ax.legend(wedges, labels, loc="lower center", bbox_to_anchor=(0.5, -0.28), ncol=2,
              fontsize=7.5, frameon=False)
    ax.set_aspect("equal")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", transparent=True)
    plt.close(fig)
    buf.seek(0)
    return buf


def _unit_perf_chart(unit_perf):
    rows = unit_perf[:12]
    if not rows:
        return None
    fig, ax = plt.subplots(figsize=(6.4, max(2.2, 0.38 * len(rows))), dpi=170)
    names = [r["unit_name"] for r in rows]
    pcts = [r["pct_complete"] for r in rows]
    clrs = ["#c0392b" if p < 40 else "#c98a12" if p < 75 else "#1f8a4c" for p in pcts]
    y = range(len(rows))
    ax.barh(y, pcts, color=clrs, height=0.6, zorder=3)
    ax.set_yticks(list(y))
    ax.set_yticklabels(names, fontsize=9)
    ax.invert_yaxis()
    ax.set_xlim(0, 100)
    for i, p in enumerate(pcts):
        ax.text(p + 1.5, i, f"{p:.0f}%", va="center", fontsize=8.5, color="#3a3f4b")
    ax.spines[["top", "right"]].set_visible(False)
    ax.spines[["left", "bottom"]].set_color("#d7dce3")
    ax.grid(axis="x", color="#e2e6ec", linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    ax.set_xlabel("% complete", fontsize=9, color="#6b7280")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", transparent=True)
    plt.close(fig)
    buf.seek(0)
    return buf


# ------------------------------------------------------------- PDF

def _cover_and_footer(canvas_obj, doc, scope_label, unit_label, generated_at):
    canvas_obj.saveState()
    if doc.page == 1:
        canvas_obj.setFillColor(NAVY)
        canvas_obj.rect(0, 0, doc.pagesize[0], doc.pagesize[1], fill=1, stroke=0)
        canvas_obj.setFillColor(BLUE)
        canvas_obj.rect(0, doc.pagesize[1] - 8, doc.pagesize[0], 8, fill=1, stroke=0)
        canvas_obj.setFillColorRGB(1, 1, 1)
        canvas_obj.setFont("Helvetica-Bold", 30)
        canvas_obj.drawString(24 * mm, doc.pagesize[1] - 90 * mm, "Bidvest ESG")
        canvas_obj.setFont("Helvetica", 15)
        canvas_obj.drawString(24 * mm, doc.pagesize[1] - 102 * mm, f"{scope_label} ESG report")
        canvas_obj.setFont("Helvetica", 11)
        canvas_obj.setFillColor(colors.HexColor("#cdd8ea"))
        canvas_obj.drawString(24 * mm, doc.pagesize[1] - 112 * mm, unit_label)
        canvas_obj.drawString(24 * mm, doc.pagesize[1] - 120 * mm,
                               f"Generated {generated_at.strftime('%d %B %Y, %H:%M UTC')}")
        canvas_obj.setFillColor(BRASS)
        canvas_obj.rect(24 * mm, doc.pagesize[1] - 128 * mm, 38 * mm, 2.4, fill=1, stroke=0)
    else:
        canvas_obj.setFillColor(NAVY)
        canvas_obj.rect(0, doc.pagesize[1] - 14 * mm, doc.pagesize[0], 14 * mm, fill=1, stroke=0)
        canvas_obj.setFillColorRGB(1, 1, 1)
        canvas_obj.setFont("Helvetica-Bold", 10)
        canvas_obj.drawString(16 * mm, doc.pagesize[1] - 9.5 * mm, "Bidvest ESG")
        canvas_obj.setFont("Helvetica", 9)
        canvas_obj.drawRightString(doc.pagesize[0] - 16 * mm, doc.pagesize[1] - 9.5 * mm, scope_label)
        canvas_obj.setFillColor(GREY)
        canvas_obj.setFont("Helvetica", 8)
        canvas_obj.drawString(16 * mm, 10 * mm, "Bidvest Catering Services – ESG data checklist")
        canvas_obj.drawRightString(doc.pagesize[0] - 16 * mm, 10 * mm, f"Page {doc.page - 1}")
    canvas_obj.restoreState()


def _stat_card_table(cards):
    """Rounded, shadowed stat cards laid out in a row, drawn with Table borders/backgrounds
    to give a subtle raised ('3D') card look without external image assets."""
    cells = []
    for label, value, color in cards:
        inner = Table([[Paragraph(f"<font size=8 color='#6b7280'>{label.upper()}</font>")],
                        [Paragraph(f"<font size=20 color='{color}'><b>{value}</b></font>")]],
                       colWidths=[41 * mm])
        inner.setStyle(TableStyle([
            ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ("BACKGROUND", (0, 0), (-1, -1), colors.white),
            ("BOX", (0, 0), (-1, -1), 0.75, colors.HexColor("#e2e6ec")),
            ("LINEBELOW", (0, 0), (-1, 0), 2.4, colors.HexColor(color)),
        ]))
        cells.append(inner)
    row = Table([cells], colWidths=[44 * mm] * len(cells), hAlign="LEFT")
    row.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    return row


def build_pdf_report(ctx):
    styles = getSampleStyleSheet()
    h2 = ParagraphStyle("H2", parent=styles["Heading2"], textColor=NAVY, spaceBefore=14, spaceAfter=6)
    h3 = ParagraphStyle("H3", parent=styles["Heading3"], textColor=BLUE, spaceBefore=10, spaceAfter=4)
    body = ParagraphStyle("Body", parent=styles["BodyText"], fontSize=9.2, leading=13)
    small = ParagraphStyle("Small", parent=styles["BodyText"], fontSize=8, textColor=GREY)

    scope = ctx["scope"]
    scope_label = "Complete" if scope == "all" else scope
    unit_label = (f"{len(ctx['units'])} unit(s) in scope" if len(ctx["units"]) != 1
                  else f"Unit: {ctx['units'][0]['name']}")
    summary = ctx["summary"]

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=20 * mm, bottomMargin=20 * mm,
                             leftMargin=16 * mm, rightMargin=16 * mm)

    story = [Spacer(1, 210 * mm), PageBreak()]

    story.append(Paragraph("Executive summary", h2))
    story.append(_stat_card_table([
        ("Overall complete", f"{summary['total']['pct_complete']:.0f}%", "#141549"),
        ("Data points", str(summary["total_items"]), "#4a99d1"),
        ("Units in scope", str(summary["total_units"]), "#c9896d"),
        ("Verified", str(summary["total"]["Verified"]), "#1f8a4c"),
    ]))

    chart_row = Table([[
        Image(_pillar_bar_chart(summary["pillars"]), width=90 * mm, height=43.5 * mm),
        Image(_status_donut(summary["total"]), width=55 * mm, height=55 * mm),
    ]], colWidths=[95 * mm, 60 * mm])
    chart_row.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    story.append(chart_row)
    story.append(Spacer(1, 6))

    story.append(Paragraph(
        "This report summarises Bidvest Catering Services' progress collecting the ESG data points "
        "in scope, aggregated across the units listed below. Percent-complete counts a data point as "
        "done once the current reporting period has a value logged (Collected) or has been reviewed "
        "and signed off (Verified); N/A items are excluded from the denominator.", body))

    story.append(Paragraph("Pillar breakdown", h3))
    pillar_rows = [["Pillar", "Data points", "Not started", "In progress", "Collected", "Verified", "N/A", "% complete"]]
    for p in summary["pillars"]:
        pillar_rows.append([p["pillar"], p["data_points"], p["Not started"], p["In progress"],
                             p["Collected"], p["Verified"], p["N/A"], f"{p['pct_complete']:.0f}%"])
    t = Table(pillar_rows, colWidths=[28 * mm, 20 * mm, 20 * mm, 20 * mm, 20 * mm, 18 * mm, 14 * mm, 20 * mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8.3), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e6ec")),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(t)

    if summary["unit_performance"]:
        story.append(Paragraph("Unit performance (lowest first)", h3))
        img = _unit_perf_chart(summary["unit_performance"])
        if img:
            story.append(Image(img, width=150 * mm, height=max(35, 9.5 * len(summary["unit_performance"][:12])) * mm))
        risk_rows = [["Unit", "Category", "% complete", "Flag"]]
        for u in summary["unit_performance"]:
            flag = "Needs attention" if u["pct_complete"] < 40 else ("Watch" if u["pct_complete"] < 75 else "On track")
            risk_rows.append([u["unit_name"], u["category"], f"{u['pct_complete']:.0f}%", flag])
        rt = Table(risk_rows, colWidths=[55 * mm, 40 * mm, 30 * mm, 35 * mm])
        row_styles = []
        for i, u in enumerate(summary["unit_performance"], start=1):
            color = RED if u["pct_complete"] < 40 else (AMBER if u["pct_complete"] < 75 else GREEN)
            row_styles.append(("TEXTCOLOR", (3, i), (3, i), color))
        rt.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), NAVY), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 8.3), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e6ec")),
            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("FONTNAME", (3, 1), (3, -1), "Helvetica-Bold"),
        ] + row_styles))
        story.append(rt)

    story.append(PageBreak())
    story.append(Paragraph(f"Data point detail – {scope_label}", h2))
    story.append(Paragraph(
        "Values below are the current-period roll-up per data point: repeated entries logged daily, "
        "weekly or monthly are summed or averaged automatically depending on the metric.", small))
    story.append(Spacer(1, 4))

    by_pillar = {}
    for item in ctx["items"]:
        by_pillar.setdefault(item["pillar"], {}).setdefault(item["category"], []).append(item)

    db = ctx["db"]
    compute = ctx["compute_item_status"]
    unit_ids = [u["id"] for u in ctx["units"]] or [None]

    for pillar, cats in by_pillar.items():
        story.append(Paragraph(pillar, h3))
        for cat, cat_items in cats.items():
            story.append(Paragraph(f"<b>{cat}</b>", body))
            rows = [["ID", "Data point", "Unit", "Status", "Value"]]
            for item in cat_items:
                statuses = [compute(db, item, uid) for uid in unit_ids if uid]
                if statuses:
                    order = {"N/A": 0, "Not started": 1, "In progress": 2, "Collected": 3, "Verified": 4}
                    worst = min(statuses, key=lambda s: order[s["status"]])
                    values = [str(s["rollup_value"]) for s in statuses if s["rollup_value"] not in (None, "")]
                    value_display = "; ".join(values[:3]) if values else "–"
                else:
                    worst = {"status": "Not started"}
                    value_display = "–"
                rows.append([item["id"], item["data_point"][:60], item["unit"] or "", worst["status"], value_display])
            it = Table(rows, colWidths=[12 * mm, 75 * mm, 24 * mm, 24 * mm, 30 * mm])
            it.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef1f6")),
                ("FONTSIZE", (0, 0), (-1, -1), 7.6), ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#e2e6ec")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]))
            story.append(it)
            story.append(Spacer(1, 5))

    doc.build(story, onFirstPage=lambda c, d: _cover_and_footer(c, d, scope_label, unit_label, ctx["generated_at"]),
               onLaterPages=lambda c, d: _cover_and_footer(c, d, scope_label, unit_label, ctx["generated_at"]))
    buf.seek(0)
    return buf


# ------------------------------------------------------------- DOCX

def build_docx_report(ctx):
    from docx import Document
    from docx.shared import Pt, RGBColor, Inches
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    scope = ctx["scope"]
    scope_label = "Complete" if scope == "all" else scope
    summary = ctx["summary"]

    doc = Document()
    title = doc.add_heading("Bidvest ESG Report", level=0)
    title.runs[0].font.color.rgb = RGBColor(0x14, 0x15, 0x49)
    p = doc.add_paragraph(f"{scope_label} ESG report")
    p.runs[0].font.size = Pt(14)
    p.runs[0].font.color.rgb = RGBColor(0x4a, 0x99, 0xd1)
    doc.add_paragraph(f"Generated {ctx['generated_at'].strftime('%d %B %Y, %H:%M UTC')}")
    doc.add_paragraph(f"Units in scope: {len(ctx['units'])}")

    doc.add_heading("Executive summary", level=1)
    t = doc.add_table(rows=1, cols=4)
    t.style = "Light Grid Accent 1"
    hdr = t.rows[0].cells
    hdr[0].text, hdr[1].text, hdr[2].text, hdr[3].text = "Overall complete", "Data points", "Units", "Verified"
    row = t.add_row().cells
    row[0].text = f"{summary['total']['pct_complete']:.0f}%"
    row[1].text = str(summary["total_items"])
    row[2].text = str(summary["total_units"])
    row[3].text = str(summary["total"]["Verified"])

    doc.add_heading("Pillar breakdown", level=1)
    pt = doc.add_table(rows=1, cols=8)
    pt.style = "Light Grid Accent 1"
    for i, h in enumerate(["Pillar", "Points", "Not started", "In progress", "Collected", "Verified", "N/A", "% complete"]):
        pt.rows[0].cells[i].text = h
    for p_ in summary["pillars"]:
        r = pt.add_row().cells
        r[0].text = p_["pillar"]; r[1].text = str(p_["data_points"])
        r[2].text = str(p_["Not started"]); r[3].text = str(p_["In progress"])
        r[4].text = str(p_["Collected"]); r[5].text = str(p_["Verified"]); r[6].text = str(p_["N/A"])
        r[7].text = f"{p_['pct_complete']:.0f}%"

    if summary["unit_performance"]:
        doc.add_heading("Unit performance", level=1)
        ut = doc.add_table(rows=1, cols=3)
        ut.style = "Light Grid Accent 1"
        for i, h in enumerate(["Unit", "Category", "% complete"]):
            ut.rows[0].cells[i].text = h
        for u in summary["unit_performance"]:
            r = ut.add_row().cells
            r[0].text = u["unit_name"]; r[1].text = u["category"]; r[2].text = f"{u['pct_complete']:.0f}%"

    doc.add_heading(f"Data point detail – {scope_label}", level=1)
    db = ctx["db"]
    compute = ctx["compute_item_status"]
    unit_ids = [u["id"] for u in ctx["units"]]
    by_pillar = {}
    for item in ctx["items"]:
        by_pillar.setdefault(item["pillar"], []).append(item)
    for pillar, its in by_pillar.items():
        doc.add_heading(pillar, level=2)
        dt = doc.add_table(rows=1, cols=5)
        dt.style = "Light Grid Accent 1"
        for i, h in enumerate(["ID", "Data point", "Unit", "Status", "Value"]):
            dt.rows[0].cells[i].text = h
        for item in its:
            statuses = [compute(db, item, uid) for uid in unit_ids]
            order = {"N/A": 0, "Not started": 1, "In progress": 2, "Collected": 3, "Verified": 4}
            worst = min(statuses, key=lambda s: order[s["status"]]) if statuses else {"status": "Not started"}
            values = [str(s["rollup_value"]) for s in statuses if s.get("rollup_value") not in (None, "")]
            r = dt.add_row().cells
            r[0].text = item["id"]; r[1].text = item["data_point"]; r[2].text = item["unit"] or ""
            r[3].text = worst["status"]; r[4].text = "; ".join(values[:3]) if values else "–"

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf


# ------------------------------------------------------------- XLSX

def build_xlsx_report(ctx):
    import openpyxl
    from openpyxl.styles import Font, PatternFill

    summary = ctx["summary"]
    db = ctx["db"]
    compute = ctx["compute_item_status"]
    unit_ids = [u["id"] for u in ctx["units"]]

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Checklist"
    headers = ["ID", "Pillar", "Category", "Data point", "Unit", "Frequency",
               "Status (worst across units)", "Latest / rolled-up value", "Units reporting"]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="141549")
        cell.font = Font(bold=True, color="FFFFFF")

    order = {"N/A": 0, "Not started": 1, "In progress": 2, "Collected": 3, "Verified": 4}
    for item in ctx["items"]:
        statuses = [compute(db, item, uid) for uid in unit_ids]
        worst = min(statuses, key=lambda s: order[s["status"]]) if statuses else {"status": "Not started"}
        values = [str(s["rollup_value"]) for s in statuses if s.get("rollup_value") not in (None, "")]
        ws.append([item["id"], item["pillar"], item["category"], item["data_point"], item["unit"],
                   item["frequency"], worst["status"], "; ".join(values[:5]) if values else "",
                   sum(1 for s in statuses if s["status"] in ("Collected", "Verified"))])
    for col in ws.columns:
        width = max((len(str(c.value)) for c in col if c.value), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 10), 55)

    ws2 = wb.create_sheet("Summary")
    ws2.append(["Pillar", "Data points"] + STATUSES + ["% complete"])
    for p in summary["pillars"]:
        ws2.append([p["pillar"], p["data_points"]] + [p[s] for s in STATUSES] + [p["pct_complete"]])
    t = summary["total"]
    ws2.append(["Total", t["data_points"]] + [t[s] for s in STATUSES] + [t["pct_complete"]])

    ws3 = wb.create_sheet("Unit performance")
    ws3.append(["Unit", "Category"] + STATUSES + ["% complete"])
    for u in summary["unit_performance"]:
        ws3.append([u["unit_name"], u["category"]] + [u[s] for s in STATUSES] + [u["pct_complete"]])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf
