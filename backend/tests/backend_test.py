"""Comprehensive backend tests for Glow Cosmetics API."""
import os
import time
import uuid
import requests
import pytest
from .conftest import BASE_URL, DIRECTOR_EMAIL, DIRECTOR_PASSWORD, ADMIN_EMAIL, ADMIN_PASSWORD


# ---------- Health ----------
def test_health(api_client):
    r = api_client.get(f"{BASE_URL}/api/")
    assert r.status_code == 200
    assert r.json().get("ok") is True


# ---------- Auth ----------
class TestAuth:
    def test_login_director(self, api_client):
        r = api_client.post(f"{BASE_URL}/api/auth/login",
                            json={"email": DIRECTOR_EMAIL, "password": DIRECTOR_PASSWORD})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["user"]["role"] == "director"
        assert data["user"]["email"] == DIRECTOR_EMAIL
        assert isinstance(data.get("token"), str) and len(data["token"]) > 10
        # Cookie no longer set — Bearer-only auth for per-tab isolation
        assert all(c.name != "access_token" for c in r.cookies)

    def test_login_admin(self, api_client):
        r = api_client.post(f"{BASE_URL}/api/auth/login",
                            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
        assert r.status_code == 200, r.text
        assert r.json()["user"]["role"] == "admin"

    def test_login_invalid(self, api_client):
        r = api_client.post(f"{BASE_URL}/api/auth/login",
                            json={"email": "nope@x.com", "password": "wrong"})
        assert r.status_code == 401

    def test_me_with_bearer(self, director_client):
        r = director_client.get(f"{BASE_URL}/api/auth/me")
        assert r.status_code == 200
        assert r.json()["role"] == "director"

    def test_me_unauth(self, api_client):
        s = requests.Session()
        r = s.get(f"{BASE_URL}/api/auth/me")
        assert r.status_code == 401


# ---------- Customer registration & role guards ----------
class TestCustomerAndGuards:
    @pytest.fixture(scope="class")
    def customer_session(self):
        email = f"TEST_cust_{uuid.uuid4().hex[:8]}@example.com"
        s = requests.Session()
        s.headers.update({"Content-Type": "application/json"})
        r = s.post(f"{BASE_URL}/api/auth/register", json={
            "email": email, "password": "Cust12345",
            "name": "Test", "surname": "Cust", "phone": "+998900000000"
        })
        assert r.status_code == 200, r.text
        token = r.json()["token"]
        s.headers.update({"Authorization": f"Bearer {token}"})
        return s, r.json()["user"]

    def test_register_customer(self, customer_session):
        s, user = customer_session
        assert user["role"] == "customer"
        # me endpoint reflects role
        me = s.get(f"{BASE_URL}/api/auth/me").json()
        assert me["role"] == "customer"

    def test_register_duplicate(self, customer_session):
        s, user = customer_session
        r = s.post(f"{BASE_URL}/api/auth/register", json={
            "email": user["email"], "password": "anything",
            "name": "x", "surname": "y", "phone": "z"
        })
        assert r.status_code == 400

    def test_customer_cannot_list_users(self, customer_session):
        s, _ = customer_session
        r = s.get(f"{BASE_URL}/api/users")
        assert r.status_code == 403

    def test_customer_cannot_create_product(self, customer_session):
        s, _ = customer_session
        r = s.post(f"{BASE_URL}/api/products", json={
            "name": "x", "description": "y", "price": 1, "image_url": "u"
        })
        assert r.status_code == 403


# ---------- Products ----------
class TestProducts:
    def test_list_products(self, api_client):
        r = api_client.get(f"{BASE_URL}/api/products")
        assert r.status_code == 200
        items = r.json()
        assert isinstance(items, list)
        assert len(items) >= 4, f"expected >=4 seeded products, got {len(items)}"
        for p in items:
            assert "id" in p and "name" in p and "price" in p
            assert "_id" not in p

    def test_get_product_by_id(self, api_client):
        items = api_client.get(f"{BASE_URL}/api/products").json()
        pid = items[0]["id"]
        r = api_client.get(f"{BASE_URL}/api/products/{pid}")
        assert r.status_code == 200
        assert r.json()["id"] == pid

    def test_admin_crud_product(self, admin_client):
        payload = {"name": "TEST_Prod", "description": "d", "price": 1000,
                   "image_url": "https://x", "category": "test", "stock": 5}
        r = admin_client.post(f"{BASE_URL}/api/products", json=payload)
        assert r.status_code == 200, r.text
        pid = r.json()["id"]
        # update
        payload["name"] = "TEST_Prod_Upd"
        r2 = admin_client.put(f"{BASE_URL}/api/products/{pid}", json=payload)
        assert r2.status_code == 200
        assert r2.json()["name"] == "TEST_Prod_Upd"
        # delete
        r3 = admin_client.delete(f"{BASE_URL}/api/products/{pid}")
        assert r3.status_code == 200

    def test_worker_cannot_create_product(self, director_client, admin_client):
        # create a worker
        wmail = f"TEST_w_{uuid.uuid4().hex[:6]}@x.com"
        # cleanup - delete any old workers >=4 risk
        wr = director_client.post(f"{BASE_URL}/api/users/workers", json={
            "email": wmail, "password": "Work1234", "name": "TW", "surname": "x", "phone": "1"
        })
        if wr.status_code == 400:
            pytest.skip("Max workers reached, cannot test worker create-product guard")
        assert wr.status_code == 200, wr.text
        wid = wr.json()["id"]
        # login worker
        s = requests.Session()
        s.headers.update({"Content-Type": "application/json"})
        lr = s.post(f"{BASE_URL}/api/auth/login", json={"email": wmail, "password": "Work1234"})
        assert lr.status_code == 200
        s.headers.update({"Authorization": f"Bearer {lr.json()['token']}"})
        r = s.post(f"{BASE_URL}/api/products", json={"name": "x", "description": "y", "price": 1, "image_url": "u"})
        assert r.status_code == 403
        # cleanup
        director_client.delete(f"{BASE_URL}/api/users/workers/{wid}")


# ---------- News ----------
class TestNews:
    def test_list_news_public(self, api_client):
        r = api_client.get(f"{BASE_URL}/api/news")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_admin_create_delete_news(self, admin_client):
        r = admin_client.post(f"{BASE_URL}/api/news", json={"title": "TEST_News", "content": "c", "image_url": ""})
        assert r.status_code == 200
        nid = r.json()["id"]
        r2 = admin_client.delete(f"{BASE_URL}/api/news/{nid}")
        assert r2.status_code == 200


# ---------- Orders / customer flow + notifications ----------
class TestOrdersAndNotifications:
    def test_customer_last_purchase_includes_purchase_count(self, director_client):
        r = director_client.get(f"{BASE_URL}/api/stats/customer-last-purchase")
        assert r.status_code == 200, r.text
        data = r.json()
        assert isinstance(data, list)
        assert data
        assert "purchase_count" in data[0]
        assert isinstance(data[0]["purchase_count"], int)

    def test_customer_order_creates_director_notification(self, api_client, director_client):
        # mark seen first to avoid noise
        director_client.post(f"{BASE_URL}/api/notifications/seen")
        # register customer
        email = f"TEST_ord_{uuid.uuid4().hex[:8]}@x.com"
        s = requests.Session()
        s.headers.update({"Content-Type": "application/json"})
        rr = s.post(f"{BASE_URL}/api/auth/register", json={
            "email": email, "password": "Pass1234",
            "name": "Ord", "surname": "X", "phone": "+998900000000"
        })
        assert rr.status_code == 200
        s.headers.update({"Authorization": f"Bearer {rr.json()['token']}"})
        # find product
        prods = api_client.get(f"{BASE_URL}/api/products").json()
        pid = prods[0]["id"]
        # create order
        r = s.post(f"{BASE_URL}/api/orders", json={
            "product_id": pid, "quantity": 2,
            "location": {"lat": 41.31, "lng": 69.28}, "note": "test"
        })
        assert r.status_code == 200, r.text
        order = r.json()
        assert order["product_id"] == pid
        assert order["quantity"] == 2
        # poll director
        time.sleep(1)
        poll = director_client.get(f"{BASE_URL}/api/notifications/poll").json()
        types = {i.get("type") for i in poll["items"]}
        assert "new_order" in types
        assert poll["play_sound"] is True


# ---------- Workers + Sales + Attendance ----------
class TestWorkerFlow:
    @pytest.fixture(scope="class")
    def worker_session(self):
        # Login director directly here
        ds = requests.Session()
        ds.headers.update({"Content-Type": "application/json"})
        dr = ds.post(f"{BASE_URL}/api/auth/login", json={"email": DIRECTOR_EMAIL, "password": DIRECTOR_PASSWORD})
        assert dr.status_code == 200
        ds.headers.update({"Authorization": f"Bearer {dr.json()['token']}"})

        wmail = f"TEST_wflow_{uuid.uuid4().hex[:6]}@x.com"
        wr = ds.post(f"{BASE_URL}/api/users/workers", json={
            "email": wmail, "password": "Work1234", "name": "Flow", "surname": "Wkr", "phone": "+9"
        })
        if wr.status_code == 400:
            # Cleanup any test workers and retry
            workers = ds.get(f"{BASE_URL}/api/users/workers").json()
            for w in workers:
                if w["email"].startswith("TEST_"):
                    ds.delete(f"{BASE_URL}/api/users/workers/{w['id']}")
            wr = ds.post(f"{BASE_URL}/api/users/workers", json={
                "email": wmail, "password": "Work1234", "name": "Flow", "surname": "Wkr", "phone": "+9"
            })
        assert wr.status_code == 200, wr.text
        wid = wr.json()["id"]

        s = requests.Session()
        s.headers.update({"Content-Type": "application/json"})
        lr = s.post(f"{BASE_URL}/api/auth/login", json={"email": wmail, "password": "Work1234"})
        assert lr.status_code == 200
        s.headers.update({"Authorization": f"Bearer {lr.json()['token']}"})
        yield s, wid, ds
        ds.delete(f"{BASE_URL}/api/users/workers/{wid}")

    def test_worker_login_role(self, worker_session):
        s, _, _ = worker_session
        me = s.get(f"{BASE_URL}/api/auth/me").json()
        assert me["role"] == "worker"

    def test_max_4_workers(self, worker_session):
        _, _, ds = worker_session
        # try to spam create up to >4
        created = []
        for i in range(5):
            email = f"TEST_max_{uuid.uuid4().hex[:6]}@x.com"
            r = ds.post(f"{BASE_URL}/api/users/workers", json={
                "email": email, "password": "P12345678", "name": "M", "surname": "x", "phone": "1"
            })
            if r.status_code == 200:
                created.append(r.json()["id"])
            else:
                assert r.status_code == 400
                break
        # after loop, count should be capped at 4
        workers = ds.get(f"{BASE_URL}/api/users/workers").json()
        assert len(workers) <= 4
        # cleanup
        for wid in created:
            ds.delete(f"{BASE_URL}/api/users/workers/{wid}")

    def test_sale_create_mine_all(self, worker_session, director_client, api_client):
        s, _, _ = worker_session
        prods = api_client.get(f"{BASE_URL}/api/products").json()
        pid = prods[0]["id"]
        r = s.post(f"{BASE_URL}/api/sales", json={
            "customer_name": "Ali", "customer_surname": "Vali",
            "customer_phone": "+998900000001",
            "product_id": pid, "quantity": 3, "reason": "Yangi mijoz"
        })
        assert r.status_code == 200, r.text
        sale = r.json()
        assert sale["quantity"] == 3
        assert "follow_up_due_at" in sale
        # mine
        mine = s.get(f"{BASE_URL}/api/sales/mine").json()
        assert any(x["id"] == sale["id"] for x in mine)
        # all (director)
        allsales = director_client.get(f"{BASE_URL}/api/sales/all").json()
        assert any(x["id"] == sale["id"] for x in allsales)

    def test_attendance_checkin_late_or_early(self, worker_session, director_client):
        s, _, _ = worker_session
        # mark seen first
        director_client.post(f"{BASE_URL}/api/notifications/seen")
        r = s.post(f"{BASE_URL}/api/attendance", json={
            "type": "checkin", "location": {"lat": 41.3, "lng": 69.2}
        })
        assert r.status_code == 200, r.text
        rec = r.json()
        # one of these must be true OR exactly 8:00
        assert rec["type"] == "checkin"
        # poll director
        time.sleep(1)
        poll = director_client.get(f"{BASE_URL}/api/notifications/poll").json()
        types = {i.get("type") for i in poll["items"]}
        assert types & {"late_checkin", "early_checkin", "attendance"}

    def test_attendance_checkout_silent(self, worker_session, director_client):
        s, _, _ = worker_session
        director_client.post(f"{BASE_URL}/api/notifications/seen")
        r = s.post(f"{BASE_URL}/api/attendance", json={
            "type": "checkout", "location": {"lat": 41.3, "lng": 69.2}
        })
        assert r.status_code == 200
        time.sleep(1)
        poll = director_client.get(f"{BASE_URL}/api/notifications/poll").json()
        # the latest attendance (checkout) notification must be silent
        items = [i for i in poll["items"] if i.get("type") == "attendance"]
        assert items, "expected an attendance notification"
        assert items[0]["sound"] is False

    def test_followups_endpoint(self, worker_session):
        s, _, _ = worker_session
        r = s.get(f"{BASE_URL}/api/sales/follow-ups")
        assert r.status_code == 200
        assert isinstance(r.json(), list)


# ---------- Stats ----------
class TestStats:
    def test_overview(self, director_client):
        r = director_client.get(f"{BASE_URL}/api/stats/overview")
        assert r.status_code == 200
        d = r.json()
        for k in ("total_orders", "total_sales", "total_customers", "total_workers",
                  "sales_revenue", "orders_revenue", "per_worker", "daily", "top_products"):
            assert k in d

    def test_customer_last_purchase(self, director_client):
        r = director_client.get(f"{BASE_URL}/api/stats/customer-last-purchase")
        assert r.status_code == 200
        assert isinstance(r.json(), list)


# ---------- Questions + admin notifications ----------
class TestQuestions:
    def test_submit_and_admin_notif(self, api_client, admin_client):
        admin_client.post(f"{BASE_URL}/api/notifications/seen")
        r = api_client.post(f"{BASE_URL}/api/questions", json={
            "name": "Test User", "email": "TEST_q@x.com", "message": "Salom, savol bormi?"
        })
        assert r.status_code == 200, r.text
        qid = r.json()["id"]
        time.sleep(1)
        poll = admin_client.get(f"{BASE_URL}/api/notifications/poll").json()
        types = {i.get("type") for i in poll["items"]}
        assert "new_question" in types
        assert poll["play_sound"] is True
        # list & answer
        qs = admin_client.get(f"{BASE_URL}/api/questions").json()
        assert any(q["id"] == qid for q in qs)
        ar = admin_client.post(f"{BASE_URL}/api/questions/{qid}/answered")
        assert ar.status_code == 200


# ---------- Reports (monthly PDF + CSV) ----------
class TestReports:
    def test_pdf_director_ok(self, director_client):
        r = director_client.get(f"{BASE_URL}/api/reports/monthly?year=2026&month=4&format=pdf")
        assert r.status_code == 200, r.text
        assert "application/pdf" in r.headers.get("content-type", "")
        cd = r.headers.get("content-disposition", "")
        assert "attachment" in cd.lower()
        assert "maison-glow-2026-04.pdf" in cd
        # PDF magic bytes
        assert r.content[:4] == b"%PDF"

    def test_csv_director_ok(self, director_client):
        r = director_client.get(f"{BASE_URL}/api/reports/monthly?year=2026&month=4&format=csv")
        assert r.status_code == 200, r.text
        assert "text/csv" in r.headers.get("content-type", "")
        cd = r.headers.get("content-disposition", "")
        assert "attachment" in cd.lower()
        assert "maison-glow-2026-04.csv" in cd
        body = r.content.decode("utf-8-sig", errors="replace")
        assert "Maison Glow" in body
        assert "DO'KON SOTUVLARI" in body
        assert "ONLAYN BUYURTMALAR" in body

    def test_no_auth_returns_401(self, api_client):
        s = requests.Session()
        r = s.get(f"{BASE_URL}/api/reports/monthly?year=2026&month=4&format=pdf")
        assert r.status_code == 401

    def test_admin_forbidden(self, admin_client):
        r = admin_client.get(f"{BASE_URL}/api/reports/monthly?year=2026&month=4&format=pdf")
        assert r.status_code == 403

    def test_customer_forbidden(self):
        email = f"TEST_rep_{uuid.uuid4().hex[:8]}@x.com"
        s = requests.Session()
        s.headers.update({"Content-Type": "application/json"})
        r = s.post(f"{BASE_URL}/api/auth/register", json={
            "email": email, "password": "Pass1234",
            "name": "R", "surname": "X", "phone": "+9"
        })
        assert r.status_code == 200
        s.headers.update({"Authorization": f"Bearer {r.json()['token']}"})
        rr = s.get(f"{BASE_URL}/api/reports/monthly?year=2026&month=4&format=pdf")
        assert rr.status_code == 403

    def test_worker_forbidden(self, director_client):
        # ensure under cap
        workers = director_client.get(f"{BASE_URL}/api/users/workers").json()
        for w in workers:
            if w["email"].startswith("TEST_"):
                director_client.delete(f"{BASE_URL}/api/users/workers/{w['id']}")
        wmail = f"TEST_repw_{uuid.uuid4().hex[:6]}@x.com"
        wr = director_client.post(f"{BASE_URL}/api/users/workers", json={
            "email": wmail, "password": "Work1234", "name": "RW", "surname": "x", "phone": "1"
        })
        if wr.status_code != 200:
            pytest.skip("worker create failed")
        wid = wr.json()["id"]
        s = requests.Session()
        s.headers.update({"Content-Type": "application/json"})
        lr = s.post(f"{BASE_URL}/api/auth/login", json={"email": wmail, "password": "Work1234"})
        s.headers.update({"Authorization": f"Bearer {lr.json()['token']}"})
        rr = s.get(f"{BASE_URL}/api/reports/monthly?year=2026&month=4&format=pdf")
        assert rr.status_code == 403
        director_client.delete(f"{BASE_URL}/api/users/workers/{wid}")

    def test_invalid_month_400(self, director_client):
        r = director_client.get(f"{BASE_URL}/api/reports/monthly?year=2026&month=13&format=pdf")
        assert r.status_code == 400

    def test_no_crash_when_notification_keys_empty_order(self, api_client, director_client):
        """POST /api/orders should still succeed with empty Resend/Twilio keys."""
        director_client.post(f"{BASE_URL}/api/notifications/seen")
        email = f"TEST_nocrash_{uuid.uuid4().hex[:6]}@x.com"
        s = requests.Session()
        s.headers.update({"Content-Type": "application/json"})
        r = s.post(f"{BASE_URL}/api/auth/register", json={
            "email": email, "password": "Pass1234",
            "name": "NC", "surname": "X", "phone": "+9"
        })
        assert r.status_code == 200
        s.headers.update({"Authorization": f"Bearer {r.json()['token']}"})
        prods = api_client.get(f"{BASE_URL}/api/products").json()
        rr = s.post(f"{BASE_URL}/api/orders", json={
            "product_id": prods[0]["id"], "quantity": 1,
            "location": {"lat": 41.3, "lng": 69.2}, "note": "no-key-test"
        })
        assert rr.status_code == 200, rr.text
        time.sleep(0.5)
        # in-app director notif still created
        poll = director_client.get(f"{BASE_URL}/api/notifications/poll").json()
        assert any(i.get("type") == "new_order" for i in poll["items"])
        assert poll["play_sound"] is True

    def test_no_crash_when_notification_keys_empty_question(self, api_client, admin_client):
        admin_client.post(f"{BASE_URL}/api/notifications/seen")
        r = api_client.post(f"{BASE_URL}/api/questions", json={
            "name": "TEST_nc", "email": "TEST_nc@x.com", "message": "without keys"
        })
        assert r.status_code == 200
        time.sleep(0.5)
        poll = admin_client.get(f"{BASE_URL}/api/notifications/poll").json()
        assert any(i.get("type") == "new_question" for i in poll["items"])


# ---------- Notifications seen ----------
class TestNotificationsSeen:
    def test_director_mark_seen(self, director_client):
        r = director_client.post(f"{BASE_URL}/api/notifications/seen")
        assert r.status_code == 200
        poll = director_client.get(f"{BASE_URL}/api/notifications/poll").json()
        assert poll["items"] == [] or all(False for _ in poll["items"])
