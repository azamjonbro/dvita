"""Sotuvni tahrirlash/o'chirish — faqat director uchun."""
from .conftest import BASE_URL


def test_admin_cannot_edit_or_delete_sale(admin_client):
    body = {"quantity": 1, "product_price": 1000}
    assert admin_client.put(f"{BASE_URL}/api/sales/nonexistent", json=body).status_code == 403
    assert admin_client.delete(f"{BASE_URL}/api/sales/nonexistent").status_code == 403


def test_director_missing_sale_404(director_client):
    body = {"quantity": 1, "product_price": 1000}
    assert director_client.put(f"{BASE_URL}/api/sales/nonexistent", json=body).status_code == 404
    assert director_client.delete(f"{BASE_URL}/api/sales/nonexistent").status_code == 404
