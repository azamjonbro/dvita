import csv
import io
from datetime import datetime
from typing import Any, Iterable, List, Sequence

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def _parse_dt(value: Any):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return None
        try:
            if candidate.endswith("Z"):
                candidate = candidate[:-1] + "+00:00"
            return datetime.fromisoformat(candidate)
        except ValueError:
            try:
                return datetime.strptime(candidate, "%Y-%m-%d")
            except ValueError:
                return None
    return None


def _filter_in_month(items: Iterable[dict], year: int, month: int):
    out = []
    for item in items or []:
        created = item.get("created_at") or item.get("sale_date_time") or item.get("sale_date")
        dt = _parse_dt(created)
        if dt and dt.year == year and dt.month == month:
            out.append(item)
    return out


def _row_from_sale(item: dict, idx: int):
    created = item.get("created_at") or item.get("sale_date_time") or item.get("sale_date") or "-"
    dt = _parse_dt(created)
    date_str = dt.strftime("%d.%m.%Y") if dt else str(created)
    customer = " ".join(filter(None, [item.get("customer_name"), item.get("customer_surname")])).strip() or item.get("customer_phone") or "-"
    product = item.get("product_name") or item.get("product_id") or "-"
    qty = item.get("quantity") or 0
    total = item.get("total") or item.get("line_total") or item.get("profit") or 0
    return [idx, date_str, customer, product, str(qty), f"{float(total):,.2f}"]


def _row_from_order(item: dict, idx: int):
    created = item.get("created_at") or item.get("created_at") or item.get("date") or "-"
    dt = _parse_dt(created)
    date_str = dt.strftime("%d.%m.%Y") if dt else str(created)
    customer = " ".join(filter(None, [item.get("customer_name"), item.get("customer_surname")])).strip() or item.get("customer_phone") or "-"
    product = item.get("product_name") or item.get("product_id") or "-"
    qty = item.get("quantity") or item.get("qty") or 0
    total = item.get("total") or item.get("amount") or 0
    return [idx, date_str, customer, product, str(qty), f"{float(total):,.2f}"]


def build_csv(year: int, month: int, sales: Sequence[dict], orders: Sequence[dict]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")

    writer.writerow(["Maison Glow"])
    writer.writerow(["OYLIK HISOBOT"])
    writer.writerow([f"Yil: {year}", f"Oy: {month:02d}"])
    writer.writerow([])
    writer.writerow(["DO'KON SOTUVLARI"])
    writer.writerow(["#", "Sana", "Mijoz", "Mahsulot", "Soni", "Summasi"])

    sales_rows = [_row_from_sale(item, i + 1) for i, item in enumerate(sales or [])]
    if sales_rows:
        writer.writerows(sales_rows)
    else:
        writer.writerow(["-", "-", "-", "-", "-", "-"])

    writer.writerow([])
    writer.writerow(["ONLAYN BUYURTMALAR"])
    writer.writerow(["#", "Sana", "Mijoz", "Mahsulot", "Soni", "Summasi"])

    order_rows = [_row_from_order(item, i + 1) for i, item in enumerate(orders or [])]
    if order_rows:
        writer.writerows(order_rows)
    else:
        writer.writerow(["-", "-", "-", "-", "-", "-"])

    return buffer.getvalue().encode("utf-8-sig")


def build_pdf(year: int, month: int, sales: Sequence[dict], orders: Sequence[dict]) -> bytes:
    buffer = io.BytesIO()
    styles = getSampleStyleSheet()
    title_style = styles["Title"]
    heading_style = styles["Heading2"]
    normal_style = styles["BodyText"]

    story = [
        Paragraph("Maison Glow", title_style),
        Paragraph(f"Oylik hisobot: {year}-{month:02d}", normal_style),
        Spacer(1, 10 * mm),
    ]

    def make_section(title: str, rows: Sequence[Sequence[str]]):
        story.append(Paragraph(title, heading_style))
        if not rows:
            story.append(Paragraph("Ma'lumotlar mavjud emas.", normal_style))
            story.append(Spacer(1, 6 * mm))
            return
        table_data = [["#", "Sana", "Mijoz", "Mahsulot", "Soni", "Summasi"], *rows]
        table = Table(table_data, repeatRows=1)
        table.setStyle(
            TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F2937")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.whitesmoke, colors.white]),
            ])
        )
        story.append(table)
        story.append(Spacer(1, 10 * mm))

    make_section("DO'KON SOTUVLARI", [_row_from_sale(item, i + 1) for i, item in enumerate(sales or [])])
    make_section("ONLAYN BUYURTMALAR", [_row_from_order(item, i + 1) for i, item in enumerate(orders or [])])

    doc = SimpleDocTemplate(buffer, pagesize=A4, title=f"Maison Glow {year}-{month:02d}")
    doc.build(story)
    return buffer.getvalue()
