import uuid
import requests
import pytest

from .conftest import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    BASE_URL,
    DIRECTOR_EMAIL,
    DIRECTOR_PASSWORD,
)


def _login(email, password):
    session = requests.Session()
    session.headers.update({"Content-Type": "application/json"})
    r = session.post(f"{BASE_URL}/api/auth/login", json={"email": email, "password": password}, timeout=20)
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    session.headers.update({"Authorization": f"Bearer {token}"})
    return session


def test_branch_scoping_and_filters():
    director = _login(DIRECTOR_EMAIL, DIRECTOR_PASSWORD)

    branch1 = director.post(f"{BASE_URL}/api/branches", json={
        "name": f"Filial A {uuid.uuid4().hex[:4]}",
        "code": f"A-{uuid.uuid4().hex[:4]}",
        "address": "Tashkent"
    }, timeout=20)
    assert branch1.status_code == 200, branch1.text
    branch1_id = branch1.json()["id"]

    branch2 = director.post(f"{BASE_URL}/api/branches", json={
        "name": f"Filial B {uuid.uuid4().hex[:4]}",
        "code": f"B-{uuid.uuid4().hex[:4]}",
        "address": "Samarkand"
    }, timeout=20)
    assert branch2.status_code == 200, branch2.text
    branch2_id = branch2.json()["id"]

    worker1_email = f"test_branch_worker_{uuid.uuid4().hex[:8]}@vita.com"
    worker1 = director.post(f"{BASE_URL}/api/users/workers", json={
        "email": worker1_email,
        "password": "Branch1234",
        "name": "Filial",
        "surname": "Ishchisi",
        "phone": "+998900000001",
        "branch_id": branch1_id,
    }, timeout=20)
    assert worker1.status_code == 200, worker1.text
    worker1_id = worker1.json()["id"]

    worker2_email = f"test_branch_worker_{uuid.uuid4().hex[:8]}@vita.com"
    worker2 = director.post(f"{BASE_URL}/api/users/workers", json={
        "email": worker2_email,
        "password": "Branch1234",
        "name": "Filial",
        "surname": "Ishchisi 2",
        "phone": "+998900000002",
        "branch_id": branch2_id,
    }, timeout=20)
    assert worker2.status_code == 200, worker2.text
    worker2_id = worker2.json()["id"]

    product1 = director.post(f"{BASE_URL}/api/products", json={
        "name": f"Branch product {uuid.uuid4().hex[:4]}",
        "description": "for branch 1",
        "price": 150000,
        "cost_price": 100000,
        "image_url": "https://example.com/1.png",
        "category": "skincare",
        "stock": 10,
        "branch_id": branch1_id,
    }, timeout=20)
    assert product1.status_code == 200, product1.text
    product1_id = product1.json()["id"]

    product2 = director.post(f"{BASE_URL}/api/products", json={
        "name": f"Branch product {uuid.uuid4().hex[:4]}",
        "description": "for branch 2",
        "price": 170000,
        "cost_price": 110000,
        "image_url": "https://example.com/2.png",
        "category": "skincare",
        "stock": 7,
        "branch_id": branch2_id,
    }, timeout=20)
    assert product2.status_code == 200, product2.text
    product2_id = product2.json()["id"]

    worker1_session = _login(worker1_email, "Branch1234")
    branch1_products = worker1_session.get(f"{BASE_URL}/api/products", timeout=20)
    assert branch1_products.status_code == 200, branch1_products.text
    branch1_items = branch1_products.json()
    ids = {item["id"] for item in branch1_items if isinstance(item, dict)}
    assert product1_id in ids
    assert product2_id not in ids

    worker2_session = _login(worker2_email, "Branch1234")
    branch2_products = worker2_session.get(f"{BASE_URL}/api/products", timeout=20)
    assert branch2_products.status_code == 200, branch2_products.text
    branch2_items = branch2_products.json()
    ids2 = {item["id"] for item in branch2_items if isinstance(item, dict)}
    assert product2_id in ids2
    assert product1_id not in ids2

    director_all = director.get(f"{BASE_URL}/api/products?branch_id={branch1_id}", timeout=20)
    assert director_all.status_code == 200, director_all.text
    director_items = director_all.json()
    if isinstance(director_items, dict):
        director_items = director_items.get("items", [])
    assert any(item["id"] == product1_id for item in director_items)

    sales = director.get(f"{BASE_URL}/api/sales/all?branch_id={branch1_id}&worker_id={worker1_id}", timeout=20)
    assert sales.status_code == 200, sales.text

    director.delete(f"{BASE_URL}/api/users/workers/{worker1_id}", timeout=20)
    director.delete(f"{BASE_URL}/api/users/workers/{worker2_id}", timeout=20)
    director.delete(f"{BASE_URL}/api/branches/{branch1_id}", timeout=20)
    director.delete(f"{BASE_URL}/api/branches/{branch2_id}", timeout=20)


def test_worker_product_operations_are_restricted_to_assigned_branch():
    director = _login(DIRECTOR_EMAIL, DIRECTOR_PASSWORD)
    branch1 = director.post(f"{BASE_URL}/api/branches", json={
        "name": f"Worker Product Branch {uuid.uuid4().hex[:4]}",
        "code": f"WP-{uuid.uuid4().hex[:4]}",
        "address": "Tashkent",
    }, timeout=20)
    assert branch1.status_code == 200, branch1.text
    branch1_id = branch1.json()["id"]

    branch2 = director.post(f"{BASE_URL}/api/branches", json={
        "name": f"Other Product Branch {uuid.uuid4().hex[:4]}",
        "code": f"OP-{uuid.uuid4().hex[:4]}",
        "address": "Samarkand",
    }, timeout=20)
    assert branch2.status_code == 200, branch2.text
    branch2_id = branch2.json()["id"]

    worker_email = f"test_product_worker_{uuid.uuid4().hex[:8]}@vita.com"
    worker = director.post(f"{BASE_URL}/api/users/workers", json={
        "email": worker_email,
        "password": "Branch1234",
        "name": "Product",
        "surname": "Worker",
        "phone": "+998900000001",
        "branch_id": branch1_id,
    }, timeout=20)
    assert worker.status_code == 200, worker.text
    worker_session = _login(worker_email, "Branch1234")

    product1 = director.post(f"{BASE_URL}/api/products", json={
        "name": f"Worker product one {uuid.uuid4().hex[:4]}",
        "description": "branch one",
        "price": 150000,
        "cost_price": 100000,
        "image_url": "https://example.com/1.png",
        "category": "test",
        "stock": 10,
        "branch_id": branch1_id,
    }, timeout=20)
    assert product1.status_code == 200, product1.text
    product1_id = product1.json()["id"]

    product2 = director.post(f"{BASE_URL}/api/products", json={
        "name": f"Worker product two {uuid.uuid4().hex[:4]}",
        "description": "branch two",
        "price": 170000,
        "cost_price": 110000,
        "image_url": "https://example.com/2.png",
        "category": "test",
        "stock": 7,
        "branch_id": branch2_id,
    }, timeout=20)
    assert product2.status_code == 200, product2.text
    product2_id = product2.json()["id"]

    try:
        listed = worker_session.get(f"{BASE_URL}/api/products", timeout=20)
        assert listed.status_code == 200, listed.text
        listed_ids = {item["id"] for item in listed.json()}
        assert product1_id in listed_ids
        assert product2_id not in listed_ids

        assert worker_session.get(f"{BASE_URL}/api/products/{product1_id}", timeout=20).status_code == 200
        assert worker_session.get(f"{BASE_URL}/api/products/{product2_id}", timeout=20).status_code == 403

        # Xodim filial yubormasa ham mahsulot uning filialiga biriktiriladi
        own_branch = worker_session.post(f"{BASE_URL}/api/products", json={
            "name": f"Own branch {uuid.uuid4().hex[:4]}",
            "description": "auto branch",
            "price": 100,
            "image_url": "https://example.com/1.png",
            "category": "test",
            "stock": 1,
        }, timeout=20)
        assert own_branch.status_code == 200, own_branch.text
        assert own_branch.json()["branch_id"] == branch1_id
        assert own_branch.json()["all_branches"] is False
        director.delete(f"{BASE_URL}/api/products/{own_branch.json()['id']}", timeout=20)

        # Director filialsiz mahsulot yarata olmaydi
        missing_branch = director.post(f"{BASE_URL}/api/products", json={
            "name": f"Missing branch {uuid.uuid4().hex[:4]}",
            "description": "requires branch",
            "price": 100,
            "image_url": "https://example.com/1.png",
            "category": "test",
            "stock": 1,
        }, timeout=20)
        assert missing_branch.status_code == 400
        assert "Filialni tanlang" in missing_branch.text

        # Bir xil shtrix-kodli tovar har filialga alohida kiritiladi, bitta filialda esa takrorlanmaydi
        code = f"BR{uuid.uuid4().hex[:8]}"
        same_a = director.post(f"{BASE_URL}/api/products", json={
            "name": "Paratsetamol", "description": "a", "price": 100, "image_url": "https://e.com/a.png",
            "stock": 1, "barcode": code, "branch_id": branch1_id}, timeout=20)
        same_b = director.post(f"{BASE_URL}/api/products", json={
            "name": "Paratsetamol", "description": "b", "price": 100, "image_url": "https://e.com/a.png",
            "stock": 1, "barcode": code, "branch_id": branch2_id}, timeout=20)
        dup_a = director.post(f"{BASE_URL}/api/products", json={
            "name": "Paratsetamol", "description": "c", "price": 100, "image_url": "https://e.com/a.png",
            "stock": 1, "barcode": code, "branch_id": branch1_id}, timeout=20)
        assert same_a.status_code == 200, same_a.text
        assert same_b.status_code == 200, same_b.text
        assert dup_a.status_code == 400
        director.delete(f"{BASE_URL}/api/products/{same_a.json()['id']}", timeout=20)
        director.delete(f"{BASE_URL}/api/products/{same_b.json()['id']}", timeout=20)

        unknown_branch = director.post(f"{BASE_URL}/api/products", json={
            "name": f"Unknown branch {uuid.uuid4().hex[:4]}",
            "description": "requires existing branch",
            "price": 100,
            "image_url": "https://example.com/1.png",
            "category": "test",
            "stock": 1,
            "branch_id": "no-such-branch",
        }, timeout=20)
        assert unknown_branch.status_code == 400

        other_branch = worker_session.post(f"{BASE_URL}/api/products", json={
            "name": f"Other branch product {uuid.uuid4().hex[:4]}",
            "description": "blocked",
            "price": 100,
            "image_url": "https://example.com/1.png",
            "category": "test",
            "stock": 1,
            "branch_id": branch2_id,
        }, timeout=20)
        assert other_branch.status_code == 403

        assert worker_session.put(f"{BASE_URL}/api/products/{product2_id}", json={
            "name": product2.json()["name"],
            "description": product2.json()["description"],
            "price": 170000,
            "cost_price": 110000,
            "image_url": product2.json()["image_url"],
            "category": product2.json()["category"],
            "stock": 7,
            "branch_id": branch2_id,
        }, timeout=20).status_code == 403
        assert worker_session.delete(f"{BASE_URL}/api/products/{product2_id}", timeout=20).status_code == 403

        # Ishchini boshqa filialga o'tkazish — mahsulotlar ro'yxati darhol yangi filial bo'yicha
        moved = director.put(f"{BASE_URL}/api/users/workers/{worker.json()['id']}", json={
            "name": "Product", "surname": "Worker", "phone": "+998900000001", "branch_id": branch2_id,
        }, timeout=20)
        assert moved.status_code == 200, moved.text
        assert moved.json()["branch_id"] == branch2_id
        moved_ids = {item["id"] for item in worker_session.get(f"{BASE_URL}/api/products", timeout=20).json()}
        assert product2_id in moved_ids and product1_id not in moved_ids
        director.put(f"{BASE_URL}/api/users/workers/{worker.json()['id']}", json={
            "name": "Product", "surname": "Worker", "phone": "+998900000001", "branch_id": branch1_id,
        }, timeout=20)
        assert director.put(f"{BASE_URL}/api/users/workers/{worker.json()['id']}", json={
            "name": "Product", "branch_id": "",
        }, timeout=20).status_code == 400

        # Boshqa filial mahsulotini POS orqali sotib bo'lmaydi (ombor o'zgarmaydi)
        pos_sale = worker_session.post(f"{BASE_URL}/api/pos/sales", json={
            "client_request_id": str(uuid.uuid4()),
            "customer": {"first_name": "Filial", "phone": "+998 90 " + str(uuid.uuid4().int)[:7]},
            "items": [{"product_id": product2_id, "quantity": 1, "is_medicine": False}],
        }, timeout=20)
        assert pos_sale.status_code == 403, pos_sale.text
        assert director.get(f"{BASE_URL}/api/products/{product2_id}", timeout=20).json()["stock"] == 7
    finally:
        director.delete(f"{BASE_URL}/api/products/{product1_id}", timeout=20)
        director.delete(f"{BASE_URL}/api/products/{product2_id}", timeout=20)
        director.delete(f"{BASE_URL}/api/users/workers/{worker.json()['id']}", timeout=20)
        director.delete(f"{BASE_URL}/api/branches/{branch1_id}", timeout=20)
        director.delete(f"{BASE_URL}/api/branches/{branch2_id}", timeout=20)


def test_branchless_admin_can_list_global_products():
    director = _login(DIRECTOR_EMAIL, DIRECTOR_PASSWORD)
    admin = _login(ADMIN_EMAIL, ADMIN_PASSWORD)
    branch = director.post(f"{BASE_URL}/api/branches", json={
        "name": f"Global Branch {uuid.uuid4().hex[:4]}",
        "code": f"GB-{uuid.uuid4().hex[:4]}",
        "address": "Tashkent",
    }, timeout=20)
    assert branch.status_code == 200, branch.text
    branch_id = branch.json()["id"]
    product = director.post(f"{BASE_URL}/api/products", json={
        "name": f"Global admin product {uuid.uuid4().hex[:6]}",
        "description": "visible to global admin",
        "price": 150000,
        "image_url": "https://example.com/global.png",
        "category": "test",
        "stock": 5,
        "all_branches": True,
        "branch_id": branch_id,
    }, timeout=20)
    assert product.status_code == 200, product.text
    product_id = product.json()["id"]

    try:
        response = admin.get(f"{BASE_URL}/api/products", timeout=20)
        assert response.status_code == 200, response.text
        assert any(item["id"] == product_id for item in response.json())
    finally:
        director.delete(f"{BASE_URL}/api/products/{product_id}", timeout=20)
        director.delete(f"{BASE_URL}/api/branches/{branch_id}", timeout=20)


def test_worker_can_pick_seller_from_own_branch_only():
    director = _login(DIRECTOR_EMAIL, DIRECTOR_PASSWORD)
    b1 = director.post(f"{BASE_URL}/api/branches", json={"name": f"Seller A {uuid.uuid4().hex[:4]}",
                                                          "code": f"SA-{uuid.uuid4().hex[:4]}"}, timeout=20).json()["id"]
    b2 = director.post(f"{BASE_URL}/api/branches", json={"name": f"Seller B {uuid.uuid4().hex[:4]}",
                                                          "code": f"SB-{uuid.uuid4().hex[:4]}"}, timeout=20).json()["id"]
    ids, emails = [], []
    for branch_id in (b1, b1, b2):
        email = f"test_seller_{uuid.uuid4().hex[:8]}@vita.com"
        r = director.post(f"{BASE_URL}/api/users/workers", json={
            "email": email, "password": "Branch1234", "name": "Seller", "branch_id": branch_id}, timeout=20)
        assert r.status_code == 200, r.text
        ids.append(r.json()["id"])
        emails.append(email)
    product = director.post(f"{BASE_URL}/api/products", json={
        "name": f"Seller product {uuid.uuid4().hex[:4]}", "description": "d", "price": 1000,
        "image_url": "https://example.com/1.png", "stock": 5, "branch_id": b1}, timeout=20).json()
    cashier = _login(emails[0], "Branch1234")

    def sell(employee_id):
        return cashier.post(f"{BASE_URL}/api/pos/sales", json={
            "client_request_id": str(uuid.uuid4()),
            "employee_id": employee_id,
            "customer": {"first_name": "Mijoz", "phone": "+998 90 " + str(uuid.uuid4().int)[:7]},
            "items": [{"product_id": product["id"], "quantity": 1, "is_medicine": False}],
        }, timeout=20)

    try:
        colleagues = {w["id"] for w in cashier.get(f"{BASE_URL}/api/users/workers", timeout=20).json()}
        assert colleagues == {ids[0], ids[1]}  # faqat o'z filiali sotuvchilari
        ok = sell(ids[1])
        assert ok.status_code == 200, ok.text
        assert ok.json()["sale"]["employee_id"] == ids[1]
        assert sell(ids[2]).status_code == 403  # boshqa filial sotuvchisi
    finally:
        director.delete(f"{BASE_URL}/api/products/{product['id']}", timeout=20)
        for wid in ids:
            director.delete(f"{BASE_URL}/api/users/workers/{wid}", timeout=20)
        director.delete(f"{BASE_URL}/api/branches/{b1}", timeout=20)
        director.delete(f"{BASE_URL}/api/branches/{b2}", timeout=20)
