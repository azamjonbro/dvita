"""Tests for endpoints introduced in current iteration (Bearer-only auth, search,
soft-delete + restore, stock-add, multi-sale, best-sellers, low-stock notifications)."""
import uuid
import time
import requests
import pytest
from .conftest import BASE_URL, DIRECTOR_EMAIL, DIRECTOR_PASSWORD


def _make_session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def worker_ctx(director_client):
    """Create (or recycle) a dedicated worker for this module's sale tests."""
    # cleanup any TEST_ workers first
    workers = director_client.get(f"{BASE_URL}/api/users/workers").json()
    for w in workers:
        if w.get("email", "").startswith("TEST_"):
            director_client.delete(f"{BASE_URL}/api/users/workers/{w['id']}")
    # if still at cap, drop one non-test worker just to make space? No — skip instead
    workers = director_client.get(f"{BASE_URL}/api/users/workers").json()
    if len(workers) >= 4:
        pytest.skip("4 workers already exist, cannot create test worker")

    wmail = f"TEST_newep_{uuid.uuid4().hex[:6]}@x.com"
    wpw = "Work1234"
    r = director_client.post(f"{BASE_URL}/api/users/workers", json={
        "email": wmail, "password": wpw, "name": "Newep", "surname": "Wkr", "phone": "+9"
    })
    assert r.status_code == 200, r.text
    wid = r.json()["id"]
    s = _make_session()
    lr = s.post(f"{BASE_URL}/api/auth/login", json={"email": wmail, "password": wpw})
    assert lr.status_code == 200, lr.text
    token = lr.json()["token"]
    s.headers.update({"Authorization": f"Bearer {token}"})
    yield {"session": s, "worker_id": wid, "name": "Newep Wkr"}
    director_client.delete(f"{BASE_URL}/api/users/workers/{wid}")


# ---------- Bearer-only auth: cookie should NOT be set ----------
class TestBearerOnlyAuth:
    def test_login_returns_token_no_cookie(self):
        s = _make_session()
        r = s.post(f"{BASE_URL}/api/auth/login",
                   json={"email": DIRECTOR_EMAIL, "password": DIRECTOR_PASSWORD})
        assert r.status_code == 200, r.text
        data = r.json()
        assert isinstance(data.get("token"), str) and len(data["token"]) > 10
        # No access_token cookie set
        assert all(c.name != "access_token" for c in r.cookies), \
            "access_token cookie still being set; tab isolation expected"
        set_cookie = r.headers.get("set-cookie", "") or ""
        assert "access_token=" not in set_cookie

    def test_bearer_token_authenticates(self):
        s = _make_session()
        r = s.post(f"{BASE_URL}/api/auth/login",
                   json={"email": DIRECTOR_EMAIL, "password": DIRECTOR_PASSWORD})
        token = r.json()["token"]
        # Fresh session w/o cookies, only Bearer
        s2 = _make_session()
        s2.headers.update({"Authorization": f"Bearer {token}"})
        me = s2.get(f"{BASE_URL}/api/auth/me")
        assert me.status_code == 200
        assert me.json()["role"] == "director"


# ---------- Product search ----------
class TestProductSearch:
    def test_search_param_filters(self, api_client):
        # pull arbitrary product and use its name fragment as search
        all_items = api_client.get(f"{BASE_URL}/api/products").json()
        assert len(all_items) > 0
        sample = all_items[0]
        frag = sample["name"][:3]
        r = api_client.get(f"{BASE_URL}/api/products", params={"search": frag})
        assert r.status_code == 200
        items = r.json()
        assert isinstance(items, list)
        # every result must include the fragment (case-insensitive) in one of the search fields
        for p in items:
            blob = " ".join([
                str(p.get("name", "")), str(p.get("barcode", "")),
                str(p.get("category", "")), str(p.get("description", "")),
            ]).lower()
            assert frag.lower() in blob

    def test_search_no_match_empty(self, api_client):
        r = api_client.get(f"{BASE_URL}/api/products",
                           params={"search": "ZZZ_NO_MATCH_" + uuid.uuid4().hex})
        assert r.status_code == 200
        assert r.json() == []


# ---------- Soft delete + restore + deleted list ----------
class TestSoftDeleteRestore:
    def test_soft_delete_then_restore(self, admin_client):
        # create
        payload = {"name": f"TEST_softdel_{uuid.uuid4().hex[:6]}", "description": "d",
                   "price": 1000, "image_url": "https://x", "category": "test",
                   "stock": 5, "barcode": "TESTSOFT_" + uuid.uuid4().hex[:8]}
        cr = admin_client.post(f"{BASE_URL}/api/products", json=payload)
        assert cr.status_code == 200, cr.text
        pid = cr.json()["id"]

        # soft-delete
        dr = admin_client.delete(f"{BASE_URL}/api/products/{pid}")
        assert dr.status_code == 200

        # should NOT appear in default list
        listing = admin_client.get(f"{BASE_URL}/api/products").json()
        assert all(p["id"] != pid for p in listing), "soft-deleted product leaked into default list"

        # should appear in /products/deleted (admin only)
        deleted = admin_client.get(f"{BASE_URL}/api/products/deleted")
        assert deleted.status_code == 200
        ids = [p["id"] for p in deleted.json()]
        assert pid in ids

        # restore
        rr = admin_client.post(f"{BASE_URL}/api/products/{pid}/restore")
        assert rr.status_code == 200
        # back in default list
        listing2 = admin_client.get(f"{BASE_URL}/api/products").json()
        assert any(p["id"] == pid for p in listing2)
        # cleanup (hard via second soft-delete is fine)
        admin_client.delete(f"{BASE_URL}/api/products/{pid}")

    def test_deleted_list_admin_only(self, director_client):
        r = director_client.get(f"{BASE_URL}/api/products/deleted")
        # director NOT in allowed list per code (require_roles("admin"))
        assert r.status_code == 403


# ---------- Stock-add ----------
class TestStockAdd:
    def test_stock_add_increments(self, admin_client):
        payload = {"name": f"TEST_stockadd_{uuid.uuid4().hex[:6]}", "description": "d",
                   "price": 100, "image_url": "https://x", "category": "test",
                   "stock": 10, "barcode": "TESTSTK_" + uuid.uuid4().hex[:8]}
        cr = admin_client.post(f"{BASE_URL}/api/products", json=payload)
        assert cr.status_code == 200
        pid = cr.json()["id"]
        r = admin_client.post(f"{BASE_URL}/api/products/{pid}/stock-add", json={"add": 25})
        assert r.status_code == 200, r.text
        assert r.json()["stock"] == 35
        # verify via GET
        g = admin_client.get(f"{BASE_URL}/api/products/{pid}").json()
        assert g["stock"] == 35
        # invalid: non-positive
        bad = admin_client.post(f"{BASE_URL}/api/products/{pid}/stock-add", json={"add": 0})
        assert bad.status_code == 400
        admin_client.delete(f"{BASE_URL}/api/products/{pid}")


# ---------- Single sale: stock decrement + low-stock notification ----------
class TestSaleStockAndLowStock:
    def test_single_sale_decrements_and_notifies(self, admin_client, director_client, worker_ctx):
        # create a product with stock=6 so 2x sale of 1 drops <=5 -> notif
        payload = {"name": f"TEST_lowstock_{uuid.uuid4().hex[:6]}", "description": "d",
                   "price": 1000, "cost_price": 500, "image_url": "https://x",
                   "category": "test", "stock": 6,
                   "barcode": "TESTLOW_" + uuid.uuid4().hex[:8]}
        cr = admin_client.post(f"{BASE_URL}/api/products", json=payload)
        assert cr.status_code == 200, cr.text
        pid = cr.json()["id"]
        # clear admin notif queue
        admin_client.post(f"{BASE_URL}/api/notifications/seen")
        director_client.post(f"{BASE_URL}/api/notifications/seen")

        w = worker_ctx["session"]
        r = w.post(f"{BASE_URL}/api/sales", json={
            "customer_name": "Low", "customer_surname": "Stock",
            "customer_phone": "+998900000099",
            "product_id": pid, "quantity": 2, "reason": "test"
        })
        assert r.status_code == 200, r.text
        # stock should be 4 now
        fresh = admin_client.get(f"{BASE_URL}/api/products/{pid}").json()
        assert fresh["stock"] == 4

        # low_stock notif for admin should exist
        time.sleep(0.4)
        poll_admin = admin_client.get(f"{BASE_URL}/api/notifications/poll").json()
        types_a = {(i.get("type"), i.get("ref_id")) for i in poll_admin["items"]}
        assert ("low_stock", pid) in types_a, f"expected low_stock notif for admin; got {types_a}"

        # and director should also have low_stock (silent)
        poll_dir = director_client.get(f"{BASE_URL}/api/notifications/poll").json()
        types_d = {(i.get("type"), i.get("ref_id")) for i in poll_dir["items"]}
        assert ("low_stock", pid) in types_d
        # cleanup
        admin_client.delete(f"{BASE_URL}/api/products/{pid}")


# ---------- Multi-product cart sale ----------
class TestMultiSale:
    def test_multi_sale_receipt_and_decrement(self, admin_client, worker_ctx):
        # two products
        ids = []
        for i in range(2):
            cr = admin_client.post(f"{BASE_URL}/api/products", json={
                "name": f"TEST_multi_{i}_{uuid.uuid4().hex[:6]}", "description": "d",
                "price": 10000, "cost_price": 5000, "image_url": "https://x",
                "category": "test", "stock": 20,
                "barcode": f"TESTMUL{i}_" + uuid.uuid4().hex[:6]})
            assert cr.status_code == 200, cr.text
            ids.append(cr.json()["id"])

        w = worker_ctx["session"]
        payload = {
            "customer_name": "Multi", "customer_surname": "Cart",
            "customer_phone": "+998901112233", "reason": "cart test",
            "items": [
                {"product_id": ids[0], "quantity": 2, "discount_override": 10},
                {"product_id": ids[1], "quantity": 3, "discount_override": 0},
            ],
        }
        r = w.post(f"{BASE_URL}/api/sales/multi", json=payload)
        assert r.status_code == 200, r.text
        receipt = r.json()
        assert "receipt_id" in receipt
        assert receipt["worker_name"]  # non-empty
        assert receipt["customer_name"] == "Multi"
        assert isinstance(receipt["items"], list) and len(receipt["items"]) == 2
        # grand_total math: item0: 10000*0.9*2=18000; item1: 10000*3=30000; sum=48000
        assert receipt["grand_total"] == 48000, receipt
        # each item has worker_name & receipt_id
        for it in receipt["items"]:
            assert it["receipt_id"] == receipt["receipt_id"]
            assert it["worker_name"] == receipt["worker_name"]

        # stock decremented
        p0 = admin_client.get(f"{BASE_URL}/api/products/{ids[0]}").json()
        p1 = admin_client.get(f"{BASE_URL}/api/products/{ids[1]}").json()
        assert p0["stock"] == 18 and p1["stock"] == 17

        # cleanup
        for pid in ids:
            admin_client.delete(f"{BASE_URL}/api/products/{pid}")

    def test_multi_sale_empty_items_400(self, worker_ctx):
        w = worker_ctx["session"]
        r = w.post(f"{BASE_URL}/api/sales/multi", json={
            "customer_name": "x", "items": []
        })
        assert r.status_code == 400

    def test_multi_sale_worker_only(self, admin_client):
        # admin should be 403
        r = admin_client.post(f"{BASE_URL}/api/sales/multi", json={
            "customer_name": "x",
            "items": [{"product_id": "nope", "quantity": 1}]
        })
        assert r.status_code == 403


# ---------- Best sellers ----------
class TestBestSellers:
    def test_best_sellers_public_list(self, api_client, admin_client, worker_ctx):
        # ensure at least one sale exists so endpoint isn't trivially empty
        pcr = admin_client.post(f"{BASE_URL}/api/products", json={
            "name": f"TEST_best_{uuid.uuid4().hex[:6]}", "description": "d",
            "price": 100, "cost_price": 50, "image_url": "https://x",
            "category": "test", "stock": 100,
            "barcode": "TESTBEST_" + uuid.uuid4().hex[:8]})
        assert pcr.status_code == 200
        pid = pcr.json()["id"]
        w = worker_ctx["session"]
        sr = w.post(f"{BASE_URL}/api/sales", json={
            "customer_name": "B", "customer_surname": "S", "customer_phone": "+9",
            "product_id": pid, "quantity": 1, "reason": "best"
        })
        assert sr.status_code == 200, sr.text

        r = api_client.get(f"{BASE_URL}/api/products/best-sellers")
        assert r.status_code == 200
        items = r.json()
        assert isinstance(items, list)
        assert len(items) <= 6
        for it in items:
            assert "id" in it and "name" in it and "sold_qty" in it
            assert isinstance(it["sold_qty"], (int, float)) and it["sold_qty"] > 0
            assert "_id" not in it
        # cleanup
        admin_client.delete(f"{BASE_URL}/api/products/{pid}")

    def test_best_sellers_excludes_deleted(self, api_client, admin_client, worker_ctx):
        # product, sell it once, soft-delete, ensure it is NOT in best-sellers
        pcr = admin_client.post(f"{BASE_URL}/api/products", json={
            "name": f"TEST_bestdel_{uuid.uuid4().hex[:6]}", "description": "d",
            "price": 100, "cost_price": 50, "image_url": "https://x",
            "category": "test", "stock": 100,
            "barcode": "TESTBDEL_" + uuid.uuid4().hex[:8]})
        pid = pcr.json()["id"]
        w = worker_ctx["session"]
        w.post(f"{BASE_URL}/api/sales", json={
            "customer_name": "B", "customer_surname": "D", "customer_phone": "+9",
            "product_id": pid, "quantity": 1, "reason": "x"
        })
        admin_client.delete(f"{BASE_URL}/api/products/{pid}")
        items = api_client.get(f"{BASE_URL}/api/products/best-sellers").json()
        assert all(it["id"] != pid for it in items), "deleted product appeared in best-sellers"
