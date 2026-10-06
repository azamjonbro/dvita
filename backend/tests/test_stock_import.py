"""Filial qoldig'ini (astatka) Excel orqali to'g'rilash testlari."""
import io
import uuid

import openpyxl
import pytest
import requests

from .conftest import BASE_URL, DIRECTOR_EMAIL, DIRECTOR_PASSWORD

HEADER = ["Номер", "Код товара", "Наименование", "Производитель", "Кол-во", "Штучно", "Срок.год",
          "Цена Пок", "Цена Прод", "Цена Опт", "Сумма Пок", "Сумма Прод", "Сумма Опт", "Штрих код", "Штуки", "ИКПУ"]


def _xlsx(rows):
    """Apteka dasturi eksportiga o'xshash fayl: 2 ta bo'sh qator, sarlavha, ma'lumot, jami."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Export"
    ws.append([None] * 16)
    ws.append([None] * 16)
    ws.append(HEADER)
    for i, (code, name, qty, expiry, cost, price, barcode, units) in enumerate(rows, start=1):
        ws.append([i, code, name + "\t", "NOW FOODS\t", qty, 0, expiry + "\t", cost, price, 0,
                   cost * qty, price * qty, 0, barcode + "\t", units, "\t"])
    ws.append([None] * 16)
    ws.append([None] * 10 + [123456])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _login():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login", json={"email": DIRECTOR_EMAIL, "password": DIRECTOR_PASSWORD}, timeout=20)
    assert r.status_code == 200, r.text
    s.headers.update({"Authorization": f"Bearer {r.json()['token']}"})
    return s


def _upload(s, branch_id, content, apply, mode="stock"):
    return s.post(f"{BASE_URL}/api/products/import-stock",
                  data={"branch_id": branch_id, "apply": str(apply).lower(), "mode": mode},
                  files={"file": ("astatka.xlsx", content,
                                  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                  timeout=30)


@pytest.fixture()
def branch():
    s = _login()
    r = s.post(f"{BASE_URL}/api/branches", json={"name": f"Vokzal TEST {uuid.uuid4().hex[:4]}",
                                                 "code": f"VK-{uuid.uuid4().hex[:6]}"}, timeout=20)
    assert r.status_code == 200, r.text
    bid = r.json()["id"]
    yield s, bid
    for p in s.get(f"{BASE_URL}/api/products", params={"branch_id": bid, "page": 1, "limit": 50}, timeout=20).json()["items"]:
        s.delete(f"{BASE_URL}/api/products/{p['id']}", timeout=20)
    s.delete(f"{BASE_URL}/api/branches/{bid}", timeout=20)


def _branch_products(s, bid):
    items = s.get(f"{BASE_URL}/api/products", params={"branch_id": bid, "page": 1, "limit": 50}, timeout=20).json()["items"]
    return {p["sku"]: p for p in items}


def test_receive_creates_then_increments_and_stock_only_edits(branch):
    s, bid = branch
    tag = uuid.uuid4().hex[:6]
    first = _xlsx([
        ("A1", f"TEST Vitamin C {tag}", 5, "01.07.2029", 100, 150, f"BC{tag}1", 60),
        ("A2", f"TEST Omega {tag}", 2, "01.03.2028", 200, 290, f"BC{tag}2", 90),
        ("A2", f"TEST Omega {tag}", 3, "01.01.2028", 210, 300, f"BC{tag}2", 90),  # ikkinchi partiya
        ("A3", f"TEST Zinc {tag}", 4, "01.05.2030", 50, 80, f"BC{tag}3", 100),
    ])

    # Astatka faqat tahrirlaydi — bo'sh filialda hech narsa yaratilmaydi
    stock_only = _upload(s, bid, first, apply=True, mode="stock").json()
    assert (stock_only["created"], stock_only["not_found"]) == (0, 3)
    assert _branch_products(s, bid) == {}

    # Tovar qabul qilish: oldindan ko'rish bazaga yozmaydi
    preview = _upload(s, bid, first, apply=False, mode="receive").json()
    assert (preview["rows"], preview["created"], preview["applied"], preview["total_stock"]) == (3, 3, False, 14)
    assert preview["already_imported_at"] is None
    assert _branch_products(s, bid) == {}

    applied = _upload(s, bid, first, apply=True, mode="receive").json()
    assert applied["created"] == 3
    products = _branch_products(s, bid)
    assert products["A2"]["stock"] == 5 and products["A2"]["expiry_date"] == "2028-01-01"
    assert products["A2"]["cost_price"] == 206
    assert products["A1"]["branch_id"] == bid and products["A1"]["all_branches"] is False

    # Xuddi shu fayl qayta qabul qilinsa — soni oshadi, lekin ogohlantirish beriladi
    again = _upload(s, bid, first, apply=False, mode="receive").json()
    assert (again["created"], again["increased"]) == (0, 3)
    assert again["already_imported_at"]
    _upload(s, bid, first, apply=True, mode="receive")
    assert _branch_products(s, bid)["A1"]["stock"] == 10

    # Astatka: A1 soni o'zgardi, A3 faylda yo'q (0), A4 filialda yo'q (o'tkazib yuboriladi)
    second = _xlsx([
        ("A1", f"TEST Vitamin C {tag}", 1, "01.07.2029", 100, 160, f"BC{tag}1", 60),
        # A2: ikki partiyaning o'rtacha tortilgan tannarxi (2×200 + 3×210) / 5 = 206 — o'zgarmagan
        ("A2", f"TEST Omega {tag}", 10, "01.01.2028", 206, 300, f"BC{tag}2", 90),
        ("A4", f"TEST Iron {tag}", 7, "01.02.2031", 70, 99, f"BC{tag}4", 30),
    ])
    synced = _upload(s, bid, second, apply=True, mode="stock").json()
    assert (synced["updated"], synced["unchanged"], synced["created"], synced["zeroed"], synced["not_found"]) == (1, 1, 0, 1, 1)
    products = _branch_products(s, bid)
    assert products["A1"]["stock"] == 1 and products["A1"]["price"] == 160
    assert products["A2"]["stock"] == 10
    assert products["A3"]["stock"] == 0
    assert "A4" not in products


def test_stock_import_rejects_bad_file_and_unknown_branch(branch):
    s, bid = branch
    bad = _upload(s, bid, b"not an excel file", apply=False)
    assert bad.status_code == 400
    wb = openpyxl.Workbook()
    wb.active.append(["Boshqa", "ustunlar"])
    buf = io.BytesIO()
    wb.save(buf)
    assert _upload(s, bid, buf.getvalue(), apply=False).status_code == 400
    assert _upload(s, bid, _xlsx([("Z", "TEST", 1, "01.01.2030", 1, 2, "BCZ", 1)]), apply=False, mode="other").status_code == 422
    assert _upload(s, "no-such-branch", _xlsx([("Z", "TEST", 1, "01.01.2030", 1, 2, "BCZ", 1)]), apply=False).status_code == 400
