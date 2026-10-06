"""Filial qoldig'i (astatka) Excel faylini o'qish.

Fayl har doim bir xil ko'rinishda (1C/apteka dasturidan "Export" varag'i):
sarlavha qatori — Номер | Код товара | Наименование | Производитель | Кол-во | Штучно |
Срок.год | Цена Пок | Цена Прод | ... | Штрих код | Штуки | ИКПУ.
Sarlavha qatori nomi bo'yicha qidiriladi, shuning uchun yuqoridagi bo'sh qatorlar
yoki ustunlar tartibi o'zgarsa ham ishlaydi.
"""
from __future__ import annotations

import io
from datetime import datetime
from typing import Dict, List, Optional

import openpyxl

COLUMNS = {
    "Код товара": "code",
    "Наименование": "name",
    "Производитель": "manufacturer",
    "Кол-во": "qty",
    "Срок.год": "expiry",
    "Цена Пок": "cost_price",
    "Цена Прод": "price",
    "Штрих код": "barcode",
    "Штуки": "units_per_package",
}
REQUIRED = ("code", "name", "qty", "price", "barcode")


class StockImportError(ValueError):
    pass


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).replace("\t", " ").strip()


def _number(value) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(_text(value).replace(" ", "").replace(",", "."))
    except ValueError:
        return 0.0


def _expiry(value) -> str:
    """'01.07.2029' yoki Excel sanasi -> '2029-07-01' (tizimdagi format)."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    raw = _text(value)
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def parse_stock_workbook(data: bytes) -> List[Dict]:
    """Excel'dan qoldiq qatorlarini o'qiydi. Bir xil tovar (kod/shtrix-kod) bir necha
    partiyada kelsa — miqdorlar qo'shiladi, eng yaqin muddat, eng katta sotuv narxi va
    o'rtacha tortilgan tannarx (Σ soni × narx ÷ Σ soni) olinadi."""
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001 — har qanday buzuq fayl
        raise StockImportError("Excel faylni o'qib bo'lmadi (.xlsx bo'lishi kerak)") from exc

    ws = wb.worksheets[0]
    header: Optional[Dict[str, int]] = None
    merged: Dict[str, Dict] = {}
    for row in ws.iter_rows(values_only=True):
        if header is None:
            names = {_text(c): i for i, c in enumerate(row) if c is not None}
            if "Код товара" in names and "Кол-во" in names:
                header = {key: names[title] for title, key in COLUMNS.items() if title in names}
                missing = [k for k in REQUIRED if k not in header]
                if missing:
                    raise StockImportError("Excel ustunlari topilmadi: " + ", ".join(missing))
            continue

        def get(key):
            idx = header.get(key)
            return row[idx] if idx is not None and idx < len(row) else None

        name = _text(get("name"))
        code = _text(get("code"))
        barcode = _text(get("barcode"))
        if not name or not (code or barcode):
            continue  # bo'sh yoki "jami" qatori
        item = {
            "sku": code,
            "name": name,
            "manufacturer": _text(get("manufacturer")),
            "stock": max(0, int(_number(get("qty")))),
            "expiry_date": _expiry(get("expiry")),
            "cost_price": _number(get("cost_price")),
            "price": _number(get("price")),
            "barcode": barcode,
            "units_per_package": max(0, int(_number(get("units_per_package")))),
        }
        key = code or barcode
        prev = merged.get(key)
        if prev is None:
            item["_cost_sum"] = item["stock"] * item["cost_price"]
            merged[key] = item
            continue
        prev["_cost_sum"] += item["stock"] * item["cost_price"]
        prev["stock"] += item["stock"]
        if item["expiry_date"] and (not prev["expiry_date"] or item["expiry_date"] < prev["expiry_date"]):
            prev["expiry_date"] = item["expiry_date"]
        prev["price"] = max(prev["price"], item["price"])
        prev["cost_price"] = (round(prev["_cost_sum"] / prev["stock"], 2) if prev["stock"]
                              else max(prev["cost_price"], item["cost_price"]))

    if header is None:
        raise StockImportError("Sarlavha qatori topilmadi ('Код товара', 'Кол-во' ustunlari kerak)")
    if not merged:
        raise StockImportError("Faylda tovar qatorlari topilmadi")
    for item in merged.values():
        item.pop("_cost_sum", None)
    return list(merged.values())
