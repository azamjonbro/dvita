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


def test_branchless_admin_can_list_global_products():
    director = _login(DIRECTOR_EMAIL, DIRECTOR_PASSWORD)
    admin = _login(ADMIN_EMAIL, ADMIN_PASSWORD)
    product = director.post(f"{BASE_URL}/api/products", json={
        "name": f"Global admin product {uuid.uuid4().hex[:6]}",
        "description": "visible to global admin",
        "price": 150000,
        "image_url": "https://example.com/global.png",
        "category": "test",
        "stock": 5,
        "all_branches": True,
    }, timeout=20)
    assert product.status_code == 200, product.text
    product_id = product.json()["id"]

    try:
        response = admin.get(f"{BASE_URL}/api/products", timeout=20)
        assert response.status_code == 200, response.text
        assert any(item["id"] == product_id for item in response.json())
    finally:
        director.delete(f"{BASE_URL}/api/products/{product_id}", timeout=20)
