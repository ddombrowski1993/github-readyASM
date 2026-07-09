from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

from src.database import log_action


REPORT_DIR = Path("reports")
REPORT_DIR.mkdir(exist_ok=True)


def _cell_text(value, max_chars=220):
    text = escape(str(value if value is not None else "")).replace("\n", "<br/>")
    return text[:max_chars] + "..." if len(text) > max_chars else text


def fit_pdf_table(rows, available_width, font_size=7, header_color="#1f2937"):
    if not rows:
        rows = [["No records"]]
    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "PdfTableBody",
        parent=styles["BodyText"],
        fontSize=font_size,
        leading=font_size + 1,
        wordWrap="CJK",
        splitLongWords=True,
    )
    header = ParagraphStyle(
        "PdfTableHeader",
        parent=body,
        fontName="Helvetica-Bold",
        textColor=colors.white,
    )
    col_count = max(len(rows[0]), 1)
    col_widths = [available_width / col_count] * col_count
    wrapped_rows = []
    for row_index, row in enumerate(rows):
        padded = list(row) + [""] * (col_count - len(row))
        style = header if row_index == 0 else body
        wrapped_rows.append([Paragraph(_cell_text(value), style) for value in padded[:col_count]])
    table = Table(wrapped_rows, repeatRows=1, colWidths=col_widths, hAlign="LEFT", splitByRow=True)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(header_color)),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d1d5db")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), font_size),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
            ]
        )
    )
    return table


def fit_pdf_dataframe(df, available_width, max_columns=9, max_rows=80, font_size=7, header_color="#1f2937"):
    table_df = df.copy().fillna("")
    if len(table_df.columns) > max_columns:
        table_df = table_df.iloc[:, :max_columns]
    rows = [list(table_df.columns)] + table_df.astype(str).head(max_rows).values.tolist()
    return fit_pdf_table(rows, available_width, font_size=font_size, header_color=header_color)


def build_pdf_report(title, df, filename, notes=""):
    path = REPORT_DIR / filename
    doc = SimpleDocTemplate(str(path), pagesize=landscape(letter), rightMargin=32, leftMargin=32, topMargin=28, bottomMargin=28)
    styles = getSampleStyleSheet()
    story = [
        Paragraph(title, styles["Title"]),
        Paragraph(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}", styles["Normal"]),
    ]
    if notes:
        story.append(Paragraph(notes, styles["Normal"]))
    story.append(Spacer(1, 12))
    if df.empty:
        story.append(Paragraph("No records matched the selected filters.", styles["Normal"]))
    else:
        story.append(fit_pdf_dataframe(df, doc.width, max_columns=9, max_rows=80, font_size=7))
    doc.build(story)
    log_action("PDF exported", "reports", description=title)
    return path


def pdf_bytes(path):
    return Path(path).read_bytes()
