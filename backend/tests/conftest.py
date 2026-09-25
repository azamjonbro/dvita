import os
import pytest
import requests
from pathlib import Path
from dotenv import load_dotenv

# Load frontend .env to get REACT_APP_BACKEND_URL
load_dotenv(Path(__file__).resolve().parents[2] / "frontend" / ".env")
load_dotenv(Path(__file__).resolve().parents[1] / ".env")


BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
DIRECTOR_EMAIL = os.environ.get("DIRECTOR_EMAIL", "akbarali@vita.com").lower()
DIRECTOR_PASSWORD = os.environ.get("DIRECTOR_PASSWORD", "akbaralivita.com")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "adminvita@vita.com").lower()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "adminvita")


def _login_client(email: str, password: str):
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    r = s.post(f"{BASE_URL}/api/auth/login", json={"email": email, "password": password}, timeout=20)
    if r.status_code != 200:
        pytest.skip(f"Login failed for {email}: {r.status_code} {r.text}")
    token = r.json().get("token")
    s.headers.update({"Authorization": f"Bearer {token}"})
    return s, token, r.json()["user"]


@pytest.fixture(scope="session")
def base_url():
    return BASE_URL


@pytest.fixture(scope="session")
def api_client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="session")
def director_client():
    s, token, user = _login_client(DIRECTOR_EMAIL, DIRECTOR_PASSWORD)
    return s


@pytest.fixture(scope="session")
def admin_client():
    s, token, user = _login_client(ADMIN_EMAIL, ADMIN_PASSWORD)
    return s
