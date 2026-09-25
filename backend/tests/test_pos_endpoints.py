"""POS (asosiy sotuv oynasi) endpointlari uchun integratsion testlar.

Ishlayotgan serverga (REACT_APP_BACKEND_URL) qarshi yuradi; `conftest.py`dagi
kabi login bo'lmasa skip qilinadi. Texnik topshiriq 12-bo'lim mezonlari:
kunlik raqam, qidiruv, ko'p mahsulot, mijoz typeahead, qabul tartibi,
30 kun / 3-15-25-kun vazifalari, ombor, idempotentlik, atomiklik.
"""
import uuid

import pytest
import requests

from .conftest import BASE_URL


def _session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def worker(director_client):
    workers = director_client.get(f"{BASE_URL}/api/users/workers").json()
    for w in workers:
        if w.get("email", "").startswith("test_pos_"):
            director_client.delete(f"{BASE_URL}/api/users/workers/{w['id']}")
    if len(director_client.get(f"{BASE_URL}/api/users/workers").json()) >= 4:
        pytest.skip("4 workers already exist, cannot create test worker")
    email, pw = f"test_pos_{uuid.uuid4().hex[:6]}@x.com", "Work1234"
    r = director_client.post(f"{BASE_URL}/api/users/workers",
                             json={"email": email, "password": pw, "name": "Pos", "surname": "Tester"})
    assert r.status_code == 200, r.text
    wid = r.json()["id"]
    s = _session()
    tok = s.post(f"{BASE_URL}/api/auth/login", json={"email": email, "password": pw}).json()["token"]
    s.headers.update({"Authorization": f"Bearer {tok}"})
    yield s
    director_client.delete(f"{BASE_URL}/api/users/workers/{wid}")


@pytest.fixture(scope="module")
def products(worker):
    """Qoldig'i yetarli ikkita mahsulot."""
    items = worker.get(f"{BASE_URL}/api/products").json()
    ok = [p for p in items if int(p.get("stock", 0)) >= 3]
    if len(ok) < 2:
        pytest.skip("Not enough stocked products")
    return ok[0], ok[1]


def _phone():
    """Har chaqiruvda noyob O'zbekiston raqami."""
    return "+998 9" + str(uuid.uuid4().int)[:8]


def _payload(p1, p2, phone):
    return {
        "client_request_id": str(uuid.uuid4()),
        "customer": {"first_name": "Bahodir", "last_name": "Jalilov", "phone": phone, "age": 34, "complaint": "test"},
        "discount_amount": 0,
        "items": [
            {"product_id": p1["id"], "quantity": 1, "is_medicine": True,
             "regimen": {"units_per_package": 90, "times_per_day": 3, "units_per_intake": 1,
                         "recommendation": "ovqatdan keyin", "course_start_date": "2026-09-21"}},
            {"product_id": p2["id"], "quantity": 2, "is_medicine": True,
             "regimen": {"units_per_package": 30, "times_per_day": 1, "units_per_intake": 1}},
        ],
    }


class TestPosBasics:
    def test_next_number_shape(self, worker):
        r = worker.get(f"{BASE_URL}/api/pos/next-number")
        assert r.status_code == 200
        d = r.json()
        assert d["daily_number"] >= 1
        assert d["sale_code"].startswith("SALE-") and d["sale_code"].endswith(f"{d['daily_number']:03d}")

    def test_product_search_by_name_and_barcode(self, worker, products):
        p = products[0]
        r = worker.get(f"{BASE_URL}/api/pos/products/search", params={"q": p["name"], "limit": 50})
        assert r.status_code == 200 and any(x["id"] == p["id"] for x in r.json())
        if len(p["name"]) > 3:
            middle = p["name"][1:3]
            r = worker.get(f"{BASE_URL}/api/pos/products/search", params={"q": middle, "limit": 50})
            assert r.status_code == 200 and any(x["id"] == p["id"] for x in r.json())
        if p.get("barcode"):
            r = worker.get(f"{BASE_URL}/api/pos/products/search", params={"q": p["barcode"]})
            assert r.json()[0]["id"] == p["id"]  # aniq kod mosligi birinchi

    def test_settings_default_and_permissions(self, worker, director_client):
        r = worker.get(f"{BASE_URL}/api/pos/settings")
        assert r.status_code == 200 and len(r.json()["stages"]) >= 1
        assert worker.put(f"{BASE_URL}/api/pos/settings", json={"stages": [{"type": "offset", "value": 3}]}).status_code == 403
        assert director_client.put(f"{BASE_URL}/api/pos/settings", json={"stages": [], "min_course_days": 10}).status_code == 400


class TestCompleteSale:
    def test_full_flow(self, worker, products):
        p1, p2 = products
        s1, s2 = int(p1["stock"]), int(p2["stock"])
        phone = _phone()
        payload = _payload(p1, p2, phone)
        r = worker.post(f"{BASE_URL}/api/pos/sales", json=payload)
        assert r.status_code == 200, r.text
        out = r.json()
        sale = out["sale"]
        assert sale["daily_number"] >= 1 and sale["sale_code"].startswith("SALE-")
        assert sale["employee_name"] == "Pos Tester"
        assert len(out["items"]) == 2
        assert out["duplicate"] is False

        # #7: 90 tabletka, 3 mahal × 1 → 30 kun
        it1 = next(i for i in out["items"] if i["product_id"] == p1["id"])
        assert it1["daily_usage"] == 3 and it1["estimated_days"] == 30 and it1["estimated_end_date"] == "2026-10-21"
        # #9: ikkinchi dori alohida — 2 × 30 dona, kuniga 1 → 60 kun
        it2 = next(i for i in out["items"] if i["product_id"] == p2["id"])
        assert it2["total_units"] == 60 and it2["estimated_days"] == 60
        assert it1["estimated_end_date"] != it2["estimated_end_date"]

        # #8: 3-, 15-, 25-kun vazifalari
        fu1 = sorted([f for f in out["follow_ups"] if f["sale_item_id"] == it1["id"]], key=lambda f: f["follow_up_number"])
        assert [f["day_offset"] for f in fu1] == [3, 15, 25]
        assert [f["scheduled_at"] for f in fu1] == ["2026-09-24", "2026-10-06", "2026-10-16"]
        assert all(f["status"] == "planned" and f["assigned_employee_id"] for f in fu1)

        # jami summa
        assert abs(sale["subtotal"] - sum(i["line_total"] for i in out["items"])) < 0.01
        assert abs(sale["total"] - (sale["subtotal"] - sale["discount"])) < 0.01

        # #11: takroriy bosish — bir xil sotuv qaytadi, yangisi yaratilmaydi
        r2 = worker.post(f"{BASE_URL}/api/pos/sales", json=payload)
        assert r2.status_code == 200 and r2.json()["duplicate"] is True
        assert r2.json()["sale"]["id"] == sale["id"]

        # #10: ombor kamaydi
        assert worker.get(f"{BASE_URL}/api/products/{p1['id']}").json()["stock"] == s1 - 1
        assert worker.get(f"{BASE_URL}/api/products/{p2['id']}").json()["stock"] == s2 - 2

        # #4/#5: mijoz bazaga tushdi va typeahead orqali topiladi
        cs = worker.get(f"{BASE_URL}/api/customers/search", params={"q": "Jali"}).json()
        me = next(c for c in cs if c["id"] == out["customer"]["id"])
        assert me["phone_tail"] == phone.replace(" ", "")[-4:] and me["purchases_count"] >= 1
        detail = worker.get(f"{BASE_URL}/api/customers/{me['id']}").json()
        assert len(detail["purchases"]) >= 1 and len(detail["active_follow_ups"]) == len(out["follow_ups"])

        # Takroriy telefon bilan yangi profil → 409 ogohlantirish
        dup = {**payload, "client_request_id": str(uuid.uuid4()),
               "customer": {"first_name": "Boshqa", "phone": phone}}
        r3 = worker.post(f"{BASE_URL}/api/pos/sales", json=dup)
        assert r3.status_code == 409 and r3.json()["detail"]["code"] == "duplicate_phone"

        # legacy `sales` ham yozildi (direktor statistikasi uchun)
        mine = worker.get(f"{BASE_URL}/api/sales/mine").json()
        assert any(m.get("pos_sale_id") == sale["id"] for m in mine)

        # Sotuv tafsiloti
        assert worker.get(f"{BASE_URL}/api/pos/sales/{sale['id']}").status_code == 200
        today = worker.get(f"{BASE_URL}/api/pos/sales").json()
        assert any(s["id"] == sale["id"] for s in today)

    def test_validation_and_atomicity(self, worker, products):
        p1, _ = products
        before = worker.get(f"{BASE_URL}/api/products/{p1['id']}").json()["stock"]
        count_before = len(worker.get(f"{BASE_URL}/api/pos/sales").json())
        base = {"client_request_id": str(uuid.uuid4()), "customer": {"first_name": "A", "phone": _phone()}}
        # dori bo'lsa qabul tartibi majburiy
        r = worker.post(f"{BASE_URL}/api/pos/sales", json={**base, "items": [
            {"product_id": p1["id"], "quantity": 1, "is_medicine": True, "regimen": {"times_per_day": 0, "units_per_intake": 0}}]})
        assert r.status_code == 400
        # qoldiqdan ko'p
        r = worker.post(f"{BASE_URL}/api/pos/sales", json={**base, "client_request_id": str(uuid.uuid4()), "items": [
            {"product_id": p1["id"], "quantity": before + 1000, "is_medicine": False}]})
        assert r.status_code == 400
        # bo'sh savat / ism yo'q
        assert worker.post(f"{BASE_URL}/api/pos/sales", json={**base, "client_request_id": str(uuid.uuid4()), "items": []}).status_code == 400
        assert worker.post(f"{BASE_URL}/api/pos/sales", json={"client_request_id": str(uuid.uuid4()), "customer": {"phone": "901111111"},
                           "items": [{"product_id": p1["id"], "quantity": 1, "is_medicine": False}]}).status_code == 400
        # #12: hech narsa qismiy saqlanmadi
        assert worker.get(f"{BASE_URL}/api/products/{p1['id']}").json()["stock"] == before
        assert len(worker.get(f"{BASE_URL}/api/pos/sales").json()) == count_before

    def test_follow_up_overrides_and_past_date(self, worker, products):
        _, p2 = products
        ok = {"client_request_id": str(uuid.uuid4()), "customer": {"first_name": "B", "phone": _phone()}, "items": [
            {"product_id": p2["id"], "quantity": 1, "is_medicine": True,
             "regimen": {"units_per_package": 30, "times_per_day": 1, "units_per_intake": 1},
             "follow_ups": [{"scheduled_date": "2099-01-05"}, {"scheduled_date": "2099-01-05"}, {"scheduled_date": "2099-01-20"}]}]}
        r = worker.post(f"{BASE_URL}/api/pos/sales", json=ok)
        assert r.status_code == 200, r.text
        assert [f["scheduled_at"] for f in r.json()["follow_ups"]] == ["2099-01-05", "2099-01-20"]  # takror yo'q
        bad = {**ok, "client_request_id": str(uuid.uuid4())}
        bad["items"][0]["follow_ups"] = [{"scheduled_date": "2020-01-01"}]
        assert worker.post(f"{BASE_URL}/api/pos/sales", json=bad).status_code == 400


class TestFollowUps:
    def test_list_summary_and_patch(self, worker, products):
        p1, p2 = products
        r = worker.post(f"{BASE_URL}/api/pos/sales", json=_payload(p1, p2, _phone()))
        assert r.status_code == 200, r.text
        fid = r.json()["follow_ups"][0]["id"]
        summ = worker.get(f"{BASE_URL}/api/followups/summary").json()
        assert set(summ) >= {"today", "overdue", "upcoming_7d"}
        active = worker.get(f"{BASE_URL}/api/followups", params={"scope": "active"}).json()
        assert any(f["id"] == fid for f in active)
        # keyinga qoldirish — sana majburiy
        assert worker.patch(f"{BASE_URL}/api/followups/{fid}", json={"status": "postponed"}).status_code == 400
        r = worker.patch(f"{BASE_URL}/api/followups/{fid}", json={"status": "postponed", "next_follow_up_at": "2099-02-01", "result_note": "band"})
        assert r.status_code == 200 and r.json()["scheduled_at"] == "2099-02-01" and r.json()["status"] == "postponed"
        r = worker.patch(f"{BASE_URL}/api/followups/{fid}", json={"status": "done", "result_note": "yaxshi"})
        assert r.status_code == 200 and r.json()["status"] == "done" and r.json()["completed_at"]
        assert worker.patch(f"{BASE_URL}/api/followups/{fid}", json={"status": "weird"}).status_code == 400
        done = worker.get(f"{BASE_URL}/api/followups", params={"scope": "done"}).json()
        assert any(f["id"] == fid for f in done)
