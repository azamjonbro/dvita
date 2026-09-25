from dotenv import load_dotenv
from pathlib import Path

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

import os
import re
import uuid
import asyncio
import logging
import math
import bcrypt
import jwt as pyjwt
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Literal

from fastapi import FastAPI, APIRouter, HTTPException, Depends, Request, Response, status
from fastapi.responses import StreamingResponse
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, Field, EmailStr

from notifications import notify_director, notify_admin
from reports import build_csv, build_pdf, _filter_in_month
from pos import build_pos_router, ensure_pos_indexes, backfill_units_per_package, normalize_phone


# ---------- Setup ----------
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

JWT_SECRET = os.environ['JWT_SECRET']
JWT_ALGO = "HS256"

logger = logging.getLogger("server")
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

@asynccontextmanager
async def lifespan(app: FastAPI):
    await seed_initial_data()
    logger.info("Glow Cosmetics API ready")
    yield
    client.close()


app = FastAPI(title="Glow Cosmetics API", lifespan=lifespan)
api = APIRouter(prefix="/api")


# ---------- Models ----------
Role = Literal["customer", "worker", "director", "admin"]


class LocationModel(BaseModel):
    lat: float
    lng: float


class UserOut(BaseModel):
    id: str
    email: str
    name: str
    surname: Optional[str] = ""
    phone: Optional[str] = ""
    role: Role
    branch_id: Optional[str] = None
    branch_name: Optional[str] = None
    created_at: str


class BranchIn(BaseModel):
    name: str
    code: str = ""
    address: str = ""
    active: bool = True


class RegisterCustomer(BaseModel):
    email: EmailStr
    password: str
    name: str
    surname: str
    phone: str


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class CreateWorker(BaseModel):
    email: EmailStr
    password: str
    name: str
    surname: str = ""
    phone: str = ""
    branch_id: Optional[str] = None


class ProductIn(BaseModel):
    name: str
    description: str
    price: float
    cost_price: float = 0
    discount_percent: float = 0
    image_url: str
    category: str = "skincare"
    stock: int = 100
    barcode: str = ""
    expiry_date: str = ""
    branch_id: Optional[str] = None
    all_branches: bool = True
    parent_barcode: str = ""  # Eski barkod — variantlar bog'lanishi uchun
    sku: str = ""  # Ichki artikul — POS qidiruvida nom/shtrix-kod bilan birga ishlatiladi
    units_per_package: int = 0  # Qadoqdagi dona/tabletka soni (0 = nomdan avtomatik aniqlanadi)


class ProductOut(ProductIn):
    id: str
    created_at: str


class OrderIn(BaseModel):
    product_id: str
    quantity: int = 1
    location: LocationModel
    note: Optional[str] = ""


class SaleIn(BaseModel):
    customer_name: str
    customer_surname: str
    customer_phone: str
    product_id: str
    quantity: int = 1
    reason: str
    discount_override: Optional[float] = None


class AttendanceIn(BaseModel):
    type: Literal["checkin", "checkout"]
    location: LocationModel


class QuestionIn(BaseModel):
    name: str
    email: EmailStr
    message: str


class NewsIn(BaseModel):
    title: str
    content: str
    image_url: str = ""


# ---------- Helpers ----------
def hash_password(p: str) -> str:
    return bcrypt.hashpw(p.encode(), bcrypt.gensalt()).decode()


def verify_password(p: str, h: str) -> bool:
    try:
        return bcrypt.checkpw(p.encode(), h.encode())
    except Exception:
        return False


def make_token(user_id: str, email: str, role: str) -> str:
    payload = {
        "sub": user_id,
        "email": email,
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(days=7),
    }
    return pyjwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


def serialize_user(u: dict) -> dict:
    out = {
        "id": u["id"],
        "email": u["email"],
        "name": u.get("name", ""),
        "surname": u.get("surname", ""),
        "phone": u.get("phone", ""),
        "role": u["role"],
        "branch_id": u.get("branch_id"),
        "branch_name": u.get("branch_name"),
        "created_at": u.get("created_at", datetime.now(timezone.utc).isoformat()),
    }
    return out


async def get_branch_name(branch_id: Optional[str]) -> Optional[str]:
    if not branch_id:
        return None
    branch = await db.branches.find_one({"id": branch_id}, {"_id": 0})
    return branch.get("name") if branch else None


async def ensure_branch_access(user: dict, branch_id: Optional[str] = None, allow_missing: bool = False):
    if user.get("role") == "director":
        return
    my_branch = user.get("branch_id")
    target = branch_id or my_branch
    if not target:
        if allow_missing:
            return
        raise HTTPException(status_code=403, detail="Filial tanlanmagan")
    if my_branch and target != my_branch:
        raise HTTPException(status_code=403, detail="Siz ushbu filial mahsulotlarini ko'ra olmaysiz")
    if not my_branch and user.get("role") == "admin":
        return
    if not my_branch and user.get("role") == "worker":
        raise HTTPException(status_code=403, detail="Ishchi filialsiz ishlay olmaydi")


def apply_branch_filter_to_query(query: dict, user: dict, requested_branch_id: Optional[str] = None):
    if user.get("role") == "director":
        if requested_branch_id:
            query["branch_id"] = requested_branch_id
        return query
    target_branch = requested_branch_id or user.get("branch_id")
    if not target_branch:
        query["branch_id"] = {"$exists": False}
        return query
    query["branch_id"] = target_branch
    return query


def normalize_image_url(url: Optional[str]) -> str:
    fallback = "https://images.unsplash.com/photo-1522335789203-aabd1fc54bc9?auto=format&fit=crop&w=900&q=80"
    if not url or not isinstance(url, str):
        return fallback
    cleaned = url.strip()
    if not cleaned:
        return fallback
    if cleaned.startswith(("http://", "https://", "data:", "/")):
        return cleaned
    return fallback


def effective_price(product: dict) -> float:
    price = float(product.get("price", 0) or 0)
    disc = float(product.get("discount_percent", 0) or 0)
    if disc <= 0:
        return price
    if disc > 100:
        disc = 100
    return round(price * (100 - disc) / 100, 2)


def public_product(p: dict, hide_cost: bool = True) -> dict:
    out = dict(p)
    out.pop("_id", None)
    if hide_cost:
        out.pop("cost_price", None)
    out["image_url"] = normalize_image_url(out.get("image_url"))
    out["final_price"] = effective_price(p)
    out["discount_amount"] = round(float(p.get("price", 0) or 0) - out["final_price"], 2)
    return out


async def get_current_user(request: Request) -> dict:
    token = None
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
    if not token:
        token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=401, detail="Avtorizatsiya talab qilinadi")
    try:
        payload = pyjwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token muddati tugagan")
    except pyjwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Token noto'g'ri")
    user = await db.users.find_one({"id": payload["sub"]}, {"_id": 0})
    if not user:
        raise HTTPException(status_code=401, detail="Foydalanuvchi topilmadi")
    return user


async def try_current_user(request: Request) -> Optional[dict]:
    try:
        return await get_current_user(request)
    except HTTPException:
        return None


def require_roles(*allowed: str):
    async def dep(user=Depends(get_current_user)):
        if user["role"] not in allowed:
            raise HTTPException(status_code=403, detail="Ruxsat yo'q")
        return user
    return dep


def set_auth_cookie(response: Response, token: str):
    # Cookie qo'yilmaydi — bu har bir browser tab uchun alohida sessionStorage
    # tokeni ishlatilishiga imkon beradi (multi-account, multi-tab).
    return


# ---------- Auth ----------
@api.post("/auth/register")
async def register(data: RegisterCustomer, response: Response):
    email = data.email.lower()
    if await db.users.find_one({"email": email}):
        raise HTTPException(status_code=400, detail="Bu email allaqachon ro'yxatdan o'tgan")
    user = {
        "id": str(uuid.uuid4()),
        "email": email,
        "password_hash": hash_password(data.password),
        "name": data.name,
        "surname": data.surname,
        "phone": data.phone,
        "role": "customer",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.users.insert_one(user)
    token = make_token(user["id"], user["email"], user["role"])
    set_auth_cookie(response, token)
    return {"user": serialize_user(user), "token": token}


@api.post("/auth/login")
async def login(data: LoginIn, response: Response):
    email = data.email.lower()
    user = await db.users.find_one({"email": email}, {"_id": 0})
    if not user or not verify_password(data.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Email yoki parol noto'g'ri")
    if user.get("branch_id"):
        branch = await db.branches.find_one({"id": user["branch_id"]}, {"_id": 0})
        if branch and branch.get("active", True) is False:
            raise HTTPException(status_code=403, detail="Sizning filialingiz vaqtincha ishlamayapti")
        if branch and not user.get("branch_name"):
            user["branch_name"] = branch.get("name")
    token = make_token(user["id"], user["email"], user["role"])
    set_auth_cookie(response, token)
    return {"user": serialize_user(user), "token": token}


@api.post("/auth/logout")
async def logout(response: Response):
    response.delete_cookie("access_token", path="/")
    return {"ok": True}


@api.get("/auth/me")
async def me(user=Depends(get_current_user)):
    return serialize_user(user)


# ---------- Products ----------
# ⚠️ MUHIM: static route lar ({pid} kabi wildcard) dan OLDIN kelishi SHART
# Aks holda FastAPI "audit-logs" ni pid deb o'qiydi → 404

@api.get("/products/audit-logs")
async def get_audit_logs(user=Depends(require_roles("director"))):
    """Director uchun: admin mahsulotlarda qilgan barcha tahrirlash loglari."""
    logs = await db.product_audit_logs.find({}, {"_id": 0}).sort("edited_at", -1).to_list(1000)
    return logs


@api.get("/products/by-barcode/{code}")
async def get_product_by_barcode(code: str, request: Request):
    """Skaner orqali QR yoki shtrix-kod boyicha mahsulot qidirish.
    Bir kodga boglik barcha mahsulotlar (asl + variantlar) arrayini qaytaradi."""
    if not code or not code.strip():
        raise HTTPException(400, "Kod bosh bolmasligi kerak")
    trimmed = code.strip()
    user = await try_current_user(request)
    hide = not (user and user["role"] in ("admin", "director", "worker"))
    query = {"barcode": trimmed, "deleted": {"$ne": True}}
    if user and user["role"] in ("admin", "worker"):
        if user.get("branch_id"):
            query["$or"] = [{"all_branches": True}, {"branch_id": user["branch_id"]}, {"branch_id": {"$exists": False}}]
        else:
            return []
    direct = await db.products.find(query, {"_id": 0}).to_list(50)
    variants_query = {"parent_barcode": trimmed, "deleted": {"$ne": True}}
    if user and user["role"] in ("admin", "worker"):
        if user.get("branch_id"):
            variants_query["$or"] = [{"all_branches": True}, {"branch_id": user["branch_id"]}, {"branch_id": {"$exists": False}}]
        else:
            variants_query["branch_id"] = {"$exists": False}
    variants = await db.products.find(variants_query, {"_id": 0}).to_list(50)
    all_products = direct + variants
    if not all_products:
        raise HTTPException(404, "Bu kod boyicha mahsulot topilmadi")
    return [public_product(p, hide_cost=hide) for p in all_products]


@api.get("/products")
async def list_products(
    request: Request,
    search: str = "",
    include_deleted: bool = False,
    limit: int = 40,
    page: int = 1,
    stock_status: str = "all",
    price_min: Optional[float] = None,
    price_max: Optional[float] = None,
    sort: str = "newest",
    branch_id: Optional[str] = None,
):
    limit = max(1, min(int(limit), 50))
    page = max(1, int(page))
    query = {} if include_deleted else {"deleted": {"$ne": True}}
    user = await try_current_user(request)
    if user and user["role"] in ("admin", "worker"):
        if branch_id and user.get("branch_id") and branch_id != user["branch_id"]:
            raise HTTPException(status_code=403, detail="Bu filial mahsulotlarini ko'ra olmaysiz")
        target_branch = branch_id or user.get("branch_id")
        if not target_branch and user["role"] == "worker":
            return []
        if target_branch:
            query["$and"] = [{"$or": [{"all_branches": True}, {"branch_id": target_branch}, {"branch_id": {"$exists": False}}]}]
    elif user and user["role"] == "director" and branch_id:
        query["branch_id"] = branch_id

    if stock_status == "in_stock":
        query["stock"] = {"$gt": 0}
    elif stock_status == "out_of_stock":
        query["stock"] = 0
    elif stock_status == "low_stock":
        query["stock"] = {"$gt": 0, "$lte": 5}

    if price_min is not None:
        min_value = float(price_min)
        existing = query.get("price")
        if isinstance(existing, dict):
            existing["$gte"] = min_value
            query["price"] = existing
        else:
            query["price"] = {"$gte": min_value}

    if price_max is not None:
        max_value = float(price_max)
        existing = query.get("price")
        if isinstance(existing, dict):
            existing["$lte"] = max_value
            query["price"] = existing
        else:
            query["price"] = {"$lte": max_value}

    if search and search.strip():
        term = search.strip()[:80]
        if len(term) < 2 and not term.isdigit():
            return []

        prefix = {"$regex": f"^{re.escape(term)}", "$options": "i"}
        query["$or"] = [
            {"name": prefix},
            {"sku": prefix},
            {"barcode": prefix},
            {"category": prefix},
            {"description": prefix},
        ]

    sort_field = "created_at"
    sort_direction = -1
    if sort == "price_asc":
        sort_field = "price"
        sort_direction = 1
    elif sort == "price_desc":
        sort_field = "price"
        sort_direction = -1
    elif sort == "stock_asc":
        sort_field = "stock"
        sort_direction = 1
    elif sort == "stock_desc":
        sort_field = "stock"
        sort_direction = -1

    total = await db.products.count_documents(query)
    skip = (page - 1) * limit
    items = await db.products.find(query, {"_id": 0}).sort(sort_field, sort_direction).skip(skip).limit(limit).to_list(length=None)

    hide = not (user and user["role"] in ("admin", "director"))
    result = [public_product(p, hide_cost=hide) for p in items]

    is_paginated = any(
        key in request.query_params
        for key in ["page", "limit", "stock_status", "price_min", "price_max", "sort"]
    )
    if is_paginated:
        total_pages = max(1, math.ceil(total / limit)) if total else 1
        return {
            "items": result,
            "page": page,
            "limit": limit,
            "total": total,
            "total_pages": total_pages,
        }
    return result


@api.get("/products/deleted")
async def list_deleted_products(user=Depends(require_roles("admin"))):
    items = await db.products.find({"deleted": True}, {"_id": 0}).sort("deleted_at", -1).to_list(1000)
    return [public_product(p, hide_cost=False) for p in items]


@api.get("/products/best-sellers")
async def best_sellers():
    """Eng ko'p sotilgan mahsulotlar (banner uchun)."""
    top = await db.sales.aggregate([
        {"$group": {
            "_id": "$product_id",
            "qty": {"$sum": "$quantity"},
        }},
        {"$sort": {"qty": -1}},
        {"$limit": 6},
    ]).to_list(6)
    out = []
    for t in top:
        p = await db.products.find_one({"id": t["_id"], "deleted": {"$ne": True}}, {"_id": 0})
        if p:
            out.append({**public_product(p, hide_cost=True), "sold_qty": t["qty"]})
    return out


@api.get("/products/{pid}")
async def get_product(pid: str, request: Request):
    p = await db.products.find_one({"id": pid}, {"_id": 0})
    if not p:
        raise HTTPException(404, "Mahsulot topilmadi")
    user = await try_current_user(request)
    if user and user["role"] in ("admin", "worker"):
        target_branch = user.get("branch_id")
        if target_branch and not p.get("all_branches") and p.get("branch_id") and p["branch_id"] != target_branch:
            raise HTTPException(status_code=403, detail="Bu filial mahsulitlarini ko'ra olmaysiz")
        if target_branch and not p.get("all_branches") and p.get("branch_id") is None:
            return public_product(p, hide_cost=hide)
    hide = not (user and user["role"] in ("admin", "director"))
    return public_product(p, hide_cost=hide)


@api.post("/products")
async def create_product(data: ProductIn, user=Depends(require_roles("admin", "director"))):
    p = data.model_dump()
    if p["price"] < 0 or p["cost_price"] < 0:
        raise HTTPException(400, "Narx manfiy bo'lmasligi kerak")
    if p["discount_percent"] < 0 or p["discount_percent"] > 100:
        raise HTTPException(400, "Chegirma 0 dan 100 gacha bo'lishi kerak")
    if p.get("barcode"):
        exists = await db.products.find_one({"barcode": p["barcode"]})
        if exists:
            raise HTTPException(400, "Bu shtrix-kod allaqachon mavjud")
    if user.get("role") == "admin":
        branch_id = p.get("branch_id") or user.get("branch_id")
        if not branch_id:
            raise HTTPException(400, "Mahsulot filialga biriktirilishi kerak")
        p["branch_id"] = branch_id
        p["all_branches"] = False
    else:
        p["branch_id"] = p.get("branch_id")
    p["id"] = str(uuid.uuid4())
    p["created_at"] = datetime.now(timezone.utc).isoformat()
    await db.products.insert_one(p)
    return public_product(p, hide_cost=False)


@api.put("/products/{pid}")
async def update_product(pid: str, data: ProductIn, user=Depends(require_roles("admin", "director"))):
    payload = data.model_dump()
    if payload["price"] < 0 or payload["cost_price"] < 0:
        raise HTTPException(400, "Narx manfiy bo'lmasligi kerak")
    if payload["discount_percent"] < 0 or payload["discount_percent"] > 100:
        raise HTTPException(400, "Chegirma 0 dan 100 gacha bo'lishi kerak")
    if payload.get("barcode"):
        exists = await db.products.find_one({"barcode": payload["barcode"], "id": {"$ne": pid}})
        if exists:
            raise HTTPException(400, "Bu shtrix-kod boshqa mahsulotda mavjud")
    if user["role"] == "admin" and user.get("branch_id"):
        payload["branch_id"] = user["branch_id"]
        payload["all_branches"] = False

    # Audit log
    old = await db.products.find_one({"id": pid}, {"_id": 0})
    if old:
        changes = []
        field_labels = {
            "name": "Nomi", "description": "Tavsif", "price": "Narx",
            "cost_price": "Xarid narxi", "discount_percent": "Chegirma",
            "image_url": "Rasm", "category": "Kategoriya",
            "stock": "Miqdor", "barcode": "Shtrix-kod", "expiry_date": "Yaroqlilik muddati"
        }
        for field, label in field_labels.items():
            old_val = old.get(field)
            new_val = payload.get(field)
            if str(old_val) != str(new_val):
                changes.append({"field": field, "label": label, "old": str(old_val), "new": str(new_val)})
        if changes:
            audit_entry = {
                "id": str(uuid.uuid4()),
                "product_id": pid,
                "product_name": old.get("name", ""),
                "admin_id": user["id"],
                "admin_name": user.get("name", "Admin"),
                "changes": changes,
                "edited_at": datetime.now(timezone.utc).isoformat(),
            }
            await db.product_audit_logs.insert_one(audit_entry)

    await db.products.update_one({"id": pid}, {"$set": payload})
    p = await db.products.find_one({"id": pid}, {"_id": 0})
    if not p:
        raise HTTPException(404, "Mahsulot topilmadi")
    return public_product(p, hide_cost=False)


@api.delete("/products/{pid}")
async def delete_product(pid: str, user=Depends(require_roles("admin", "director"))):
    """Soft delete: mahsulot 'deleted' deb belgilanadi, qaytarib olish mumkin."""
    await db.products.update_one(
        {"id": pid},
        {"$set": {"deleted": True, "deleted_at": datetime.now(timezone.utc).isoformat()}},
    )
    return {"ok": True}


@api.post("/products/{pid}/restore")
async def restore_product(pid: str, user=Depends(require_roles("admin", "director"))):
    await db.products.update_one(
        {"id": pid},
        {"$set": {"deleted": False}, "$unset": {"deleted_at": ""}},
    )
    p = await db.products.find_one({"id": pid}, {"_id": 0})
    if not p:
        raise HTTPException(404, "Mahsulot topilmadi")
    return public_product(p, hide_cost=False)


class StockAddIn(BaseModel):
    add: int


@api.post("/products/{pid}/stock-add")
async def add_stock(pid: str, data: StockAddIn, user=Depends(require_roles("admin"))):
    """Mavjud mahsulotga ombor sonini qo'shish (skanerda hech narsa o'zgartirilmagan holat)."""
    if data.add <= 0:
        raise HTTPException(400, "Qo'shiladigan miqdor 0 dan katta bo'lishi kerak")
    res = await db.products.update_one({"id": pid}, {"$inc": {"stock": data.add}})
    if res.matched_count == 0:
        raise HTTPException(404, "Mahsulot topilmadi")
    p = await db.products.find_one({"id": pid}, {"_id": 0})
    return public_product(p, hide_cost=False)


# ---------- News ----------
@api.get("/news")
async def list_news():
    return await db.news.find({}, {"_id": 0}).sort("created_at", -1).to_list(200)


@api.post("/news")
async def add_news(data: NewsIn, user=Depends(require_roles("admin"))):
    n = data.model_dump()
    n["id"] = str(uuid.uuid4())
    n["created_at"] = datetime.now(timezone.utc).isoformat()
    await db.news.insert_one(n)
    n.pop("_id", None)
    return n


@api.delete("/news/{nid}")
async def delete_news(nid: str, user=Depends(require_roles("admin"))):
    await db.news.delete_one({"id": nid})
    return {"ok": True}


# ---------- Orders ----------
@api.post("/orders")
async def create_order(data: OrderIn, user=Depends(require_roles("customer"))):
    product = await db.products.find_one({"id": data.product_id}, {"_id": 0})
    if not product:
        raise HTTPException(404, "Mahsulot topilmadi")
    unit_price = effective_price(product)
    original_price = float(product.get("price", 0) or 0)
    cost_price = float(product.get("cost_price", 0) or 0)
    discount_amount_per_unit = round(original_price - unit_price, 2)
    order = {
        "id": str(uuid.uuid4()),
        "customer_id": user["id"],
        "customer_name": user.get("name", ""),
        "customer_surname": user.get("surname", ""),
        "customer_phone": user.get("phone", ""),
        "customer_email": user["email"],
        "product_id": product["id"],
        "product_name": product["name"],
        "product_price": unit_price,
        "original_price": original_price,
        "discount_percent": float(product.get("discount_percent", 0) or 0),
        "discount_total": round(discount_amount_per_unit * data.quantity, 2),
        "cost_price": cost_price,
        "cost_total": round(cost_price * data.quantity, 2),
        "profit": round((unit_price - cost_price) * data.quantity, 2),
        "quantity": data.quantity,
        "total": round(unit_price * data.quantity, 2),
        "location": data.location.model_dump(),
        "note": data.note,
        "status": "new",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.orders.insert_one(order)
    notif_msg = (
        f"{order['customer_name']} {order['customer_surname']} - {order['product_name']}\n"
        f"Telefon: {order['customer_phone']}\n"
        f"Jami: {order['total']:,} so'm\n"
        f"Lokatsiya: {order['location']['lat']:.5f}, {order['location']['lng']:.5f}"
    )
    await db.notifications.insert_one({
        "id": str(uuid.uuid4()),
        "type": "new_order",
        "for_role": "director",
        "sound": True,
        "title": "Yangi buyurtma",
        "message": f"{order['customer_name']} {order['customer_surname']} - {order['product_name']}",
        "ref_id": order["id"],
        "created_at": order["created_at"],
        "seen": False,
    })
    asyncio.create_task(notify_director("Yangi buyurtma", notif_msg))
    order.pop("_id", None)
    return order


@api.get("/orders")
async def list_orders(user=Depends(require_roles("director", "admin"))):
    return await db.orders.find({}, {"_id": 0}).sort("created_at", -1).to_list(2000)


# ---------- Sales ----------
@api.post("/sales")
async def create_sale(data: SaleIn, user=Depends(require_roles("worker"))):
    product = await db.products.find_one({"id": data.product_id}, {"_id": 0})
    if not product:
        raise HTTPException(404, "Mahsulot topilmadi")
    current_stock = int(product.get("stock", 0) or 0)
    if current_stock <= 0:
        raise HTTPException(400, "Mahsulot tugagan, sotib bolmaydi")
    if int(data.quantity) > current_stock:
        raise HTTPException(400, f"Yetarli mahsulot yoq. Omborda: {current_stock} ta")
    now = datetime.now(timezone.utc)
    base_disc = float(product.get("discount_percent", 0) or 0)
    extra_disc = float(data.discount_override or 0)
    if extra_disc < 0:
        extra_disc = 0
    total_disc = min(base_disc + extra_disc, 100)
    original_price = float(product.get("price", 0) or 0)
    unit_price = round(original_price * (100 - total_disc) / 100, 2) if total_disc > 0 else original_price
    cost_price = float(product.get("cost_price", 0) or 0)
    discount_amount_per_unit = round(original_price - unit_price, 2)
    sale = {
        "id": str(uuid.uuid4()),
        "worker_id": user["id"],
        "worker_name": f"{user.get('name','')} {user.get('surname','')}".strip(),
        "branch_id": user.get("branch_id"),
        "branch_name": user.get("branch_name") or (await get_branch_name(user.get("branch_id"))),
        "customer_name": data.customer_name,
        "customer_surname": data.customer_surname,
        "customer_phone": data.customer_phone,
        "product_id": product["id"],
        "product_name": product["name"],
        "product_price": unit_price,
        "original_price": original_price,
        "discount_percent": total_disc,
        "discount_override": extra_disc if extra_disc > 0 else 0,
        "discount_total": round(discount_amount_per_unit * data.quantity, 2),
        "cost_price": cost_price,
        "cost_total": round(cost_price * data.quantity, 2),
        "profit": round((unit_price - cost_price) * data.quantity, 2),
        "quantity": data.quantity,
        "total": round(unit_price * data.quantity, 2),
        "reason": data.reason,
        "created_at": now.isoformat(),
        "follow_up_due_at": (now + timedelta(days=30)).isoformat(),
        "follow_up_done": False,
    }
    await db.sales.insert_one(sale)

    # ---- Stock decrement + kam qoldi ogohlantirish ----
    await db.products.update_one(
        {"id": product["id"]},
        {"$inc": {"stock": -int(data.quantity)}},
    )
    fresh = await db.products.find_one({"id": product["id"]}, {"_id": 0})
    new_stock = int(fresh.get("stock", 0)) if fresh else 0
    if new_stock <= 5:
        await db.notifications.insert_one({
            "id": str(uuid.uuid4()),
            "type": "low_stock",
            "for_role": "admin",
            "sound": True,
            "title": "Mahsulot oz qoldi",
            "message": f"{product['name']} — atigi {new_stock} ta qoldi",
            "ref_id": product["id"],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "seen": False,
        })
        await db.notifications.insert_one({
            "id": str(uuid.uuid4()),
            "type": "low_stock",
            "for_role": "director",
            "sound": False,
            "title": "Mahsulot oz qoldi",
            "message": f"{product['name']} — atigi {new_stock} ta qoldi",
            "ref_id": product["id"],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "seen": False,
        })
    sale.pop("_id", None)
    return sale


class CartItem(BaseModel):
    product_id: str
    quantity: int = 1
    discount_override: Optional[float] = None  # foiz, qo'shimcha


class MultiSaleIn(BaseModel):
    customer_name: str
    customer_surname: str = ""
    customer_phone: str = ""
    reason: str = ""
    items: List[CartItem]


@api.post("/sales/multi")
async def create_multi_sale(data: MultiSaleIn, user=Depends(require_roles("worker"))):
    """Bitta haridor uchun ko'p mahsulot sotuvi — har bir item alohida sale yozuvi bo'lib saqlanadi."""
    if not data.items:
        raise HTTPException(400, "Mahsulotlar tanlanmagan")
    now = datetime.now(timezone.utc)
    receipt_id = str(uuid.uuid4())
    saved = []
    for item in data.items:
        product = await db.products.find_one({"id": item.product_id}, {"_id": 0})
        if not product:
            continue
        item_stock = int(product.get("stock", 0) or 0)
        if item_stock <= 0:
            raise HTTPException(400, f"{product.get('name','Mahsulot')} tugagan, sotib bolmaydi")
        if int(item.quantity) > item_stock:
            raise HTTPException(400, f"{product.get('name','Mahsulot')} — yetarli yoq. Omborda: {item_stock} ta")
        base_disc = float(product.get("discount_percent", 0) or 0)
        extra_disc = max(float(item.discount_override or 0), 0)
        total_disc = min(base_disc + extra_disc, 100)
        original_price = float(product.get("price", 0) or 0)
        unit_price = round(original_price * (100 - total_disc) / 100, 2) if total_disc > 0 else original_price
        cost_price = float(product.get("cost_price", 0) or 0)
        discount_amount_per_unit = round(original_price - unit_price, 2)
        sale = {
            "id": str(uuid.uuid4()),
            "receipt_id": receipt_id,
            "worker_id": user["id"],
            "worker_name": f"{user.get('name','')} {user.get('surname','')}".strip(),
            "branch_id": user.get("branch_id"),
            "branch_name": user.get("branch_name") or (await get_branch_name(user.get("branch_id"))),
            "customer_name": data.customer_name,
            "customer_surname": data.customer_surname,
            "customer_phone": data.customer_phone,
            "product_id": product["id"],
            "product_name": product["name"],
            "product_price": unit_price,
            "original_price": original_price,
            "discount_percent": total_disc,
            "discount_override": extra_disc if extra_disc > 0 else 0,
            "discount_total": round(discount_amount_per_unit * item.quantity, 2),
            "cost_price": cost_price,
            "cost_total": round(cost_price * item.quantity, 2),
            "profit": round((unit_price - cost_price) * item.quantity, 2),
            "quantity": item.quantity,
            "total": round(unit_price * item.quantity, 2),
            "reason": data.reason,
            "created_at": now.isoformat(),
            "follow_up_due_at": (now + timedelta(days=30)).isoformat(),
            "follow_up_done": False,
        }
        await db.sales.insert_one(sale)
        await db.products.update_one({"id": product["id"]}, {"$inc": {"stock": -int(item.quantity)}})
        fresh = await db.products.find_one({"id": product["id"]}, {"_id": 0})
        new_stock = int(fresh.get("stock", 0)) if fresh else 0
        if new_stock <= 5:
            await db.notifications.insert_one({
                "id": str(uuid.uuid4()), "type": "low_stock", "for_role": "admin",
                "sound": True, "title": "Mahsulot oz qoldi",
                "message": f"{product['name']} — atigi {new_stock} ta qoldi",
                "ref_id": product["id"], "created_at": now.isoformat(), "seen": False,
            })
        sale.pop("_id", None)
        saved.append(sale)
    if not saved:
        raise HTTPException(404, "Hech qaysi mahsulot topilmadi")
    grand_total = sum(s["total"] for s in saved)
    grand_discount = sum(s["discount_total"] for s in saved)
    return {
        "receipt_id": receipt_id,
        "worker_name": f"{user.get('name','')} {user.get('surname','')}".strip(),
        "customer_name": data.customer_name,
        "customer_surname": data.customer_surname,
        "customer_phone": data.customer_phone,
        "items": saved,
        "grand_total": grand_total,
        "grand_discount": grand_discount,
        "created_at": now.isoformat(),
    }


@api.get("/sales/follow-ups")
async def my_follow_ups(user=Depends(require_roles("worker"))):
    now = datetime.now(timezone.utc).isoformat()
    items = await db.sales.find({
        "follow_up_done": False,
        "follow_up_due_at": {"$lte": now},
    }, {"_id": 0}).sort("follow_up_due_at", 1).to_list(500)
    return items


@api.get("/sales/mine")
async def my_sales(user=Depends(require_roles("worker"))):
    items = await db.sales.find({"worker_id": user["id"]}, {"_id": 0}).sort("created_at", -1).to_list(2000)
    for s in items:
        s.pop("cost_price", None)
        s.pop("cost_total", None)
        s.pop("profit", None)
    return items


@api.get("/sales/all")
async def all_sales(
    user=Depends(require_roles("director", "admin")),
    branch_id: Optional[str] = None,
    worker_id: Optional[str] = None,
):
    query = {}
    if user["role"] == "admin":
        target_branch = branch_id or user.get("branch_id")
        if target_branch and target_branch != user.get("branch_id"):
            raise HTTPException(status_code=403, detail="Bu filial sotuvlarini ko'ra olmaysiz")
        if user.get("branch_id"):
            query["branch_id"] = user["branch_id"]
        elif target_branch:
            query["branch_id"] = target_branch
    elif branch_id:
        query["branch_id"] = branch_id
    if worker_id:
        query["worker_id"] = worker_id
    return await db.sales.find(query, {"_id": 0}).sort("created_at", -1).to_list(5000)


@api.get("/sales/product-summary")
async def sales_product_summary(
    user=Depends(require_roles("director", "admin")),
    branch_id: Optional[str] = None,
    worker_id: Optional[str] = None,
):
    query = {}
    if user["role"] == "admin":
        target_branch = branch_id or user.get("branch_id")
        if target_branch and target_branch != user.get("branch_id"):
            raise HTTPException(status_code=403, detail="Bu filial sotuvlarini ko'ra olmaysiz")
        if target_branch:
            query["branch_id"] = target_branch
    elif branch_id:
        query["branch_id"] = branch_id
    if worker_id:
        query["worker_id"] = worker_id

    sold = await db.sales.aggregate([
        {"$match": query},
        {"$group": {
            "_id": "$product_id",
            "name": {"$first": "$product_name"},
            "sold_qty": {"$sum": {"$ifNull": ["$quantity", 0]}},
            "revenue": {"$sum": {"$ifNull": ["$total", 0]}},
        }},
    ]).to_list(5000)
    sold_map = {item["_id"]: item for item in sold}
    products = await db.products.find({"deleted": {"$ne": True}}, {"_id": 0}).to_list(5000)
    result = []
    for product in products:
        if user["role"] == "admin" and user.get("branch_id"):
            if not product.get("all_branches") and product.get("branch_id") not in (None, user["branch_id"]):
                continue
        item = sold_map.get(product["id"], {})
        result.append({
            "product_id": product["id"],
            "name": product.get("name", ""),
            "sold_qty": int(item.get("sold_qty", 0) or 0),
            "revenue": item.get("revenue", 0),
            "stock": int(product.get("stock", 0) or 0),
        })
    return sorted(result, key=lambda item: (-item["sold_qty"], item["name"]))


@api.post("/sales/{sid}/follow-up-done")
async def mark_followup_done(sid: str, user=Depends(require_roles("worker"))):
    res = await db.sales.update_one(
        {"id": sid, "worker_id": user["id"]},
        {"$set": {"follow_up_done": True, "follow_up_done_at": datetime.now(timezone.utc).isoformat()}},
    )
    if res.matched_count == 0:
        raise HTTPException(404, "Sotuv topilmadi")
    return {"ok": True}


# ---------- Attendance ----------
@api.post("/attendance")
async def attendance_punch(data: AttendanceIn, user=Depends(require_roles("worker"))):
    now = datetime.now(timezone.utc)
    local = now + timedelta(hours=5)
    is_late = False
    is_early = False
    if data.type == "checkin":
        if local.hour > 8 or (local.hour == 8 and local.minute > 0):
            is_late = True
        elif local.hour < 8:
            is_early = True
    rec = {
        "id": str(uuid.uuid4()),
        "worker_id": user["id"],
        "worker_name": f"{user.get('name','')} {user.get('surname','')}".strip(),
        "type": data.type,
        "location": data.location.model_dump(),
        "timestamp": now.isoformat(),
        "local_time": local.strftime("%H:%M"),
        "is_late": is_late,
        "is_early": is_early,
    }
    await db.attendance.insert_one(rec)
    if data.type == "checkin" and is_late:
        await db.notifications.insert_one({
            "id": str(uuid.uuid4()), "type": "late_checkin", "for_role": "director",
            "sound": True, "title": "Ishchi kech keldi!",
            "message": f"{rec['worker_name']} {rec['local_time']} da ishga keldi",
            "ref_id": rec["id"], "created_at": rec["timestamp"], "seen": False,
        })
        asyncio.create_task(notify_director(
            "Ishchi kech keldi",
            f"{rec['worker_name']} bugun {rec['local_time']} da ishga keldi (kechikdi)."
        ))
    elif data.type == "checkin" and is_early:
        await db.notifications.insert_one({
            "id": str(uuid.uuid4()), "type": "early_checkin", "for_role": "director",
            "sound": False, "title": "Ishchi erta keldi",
            "message": f"{rec['worker_name']} {rec['local_time']} da ishga keldi",
            "ref_id": rec["id"], "created_at": rec["timestamp"], "seen": False,
        })
    else:
        await db.notifications.insert_one({
            "id": str(uuid.uuid4()), "type": "attendance", "for_role": "director",
            "sound": False, "title": "Davomat" if data.type == "checkin" else "Ish tugatildi",
            "message": f"{rec['worker_name']} - {rec['local_time']}",
            "ref_id": rec["id"], "created_at": rec["timestamp"], "seen": False,
        })
    rec.pop("_id", None)
    return rec


@api.get("/attendance")
async def get_attendance(user=Depends(require_roles("director"))):
    return await db.attendance.find({}, {"_id": 0}).sort("timestamp", -1).to_list(5000)


@api.get("/attendance/mine")
async def my_attendance(user=Depends(require_roles("worker"))):
    return await db.attendance.find({"worker_id": user["id"]}, {"_id": 0}).sort("timestamp", -1).to_list(1000)


# ---------- Branches ----------
class BranchOut(BaseModel):
    id: str
    name: str
    code: str = ""
    address: str = ""
    created_at: str
    active: bool = True


@api.get("/branches")
async def list_branches(request: Request, user=Depends(get_current_user)):
    if user["role"] == "director":
        branches = await db.branches.find({}, {"_id": 0}).sort("created_at", -1).to_list(500)
        return branches
    if user["role"] == "admin":
        if user.get("branch_id"):
            return await db.branches.find({"id": user["branch_id"]}, {"_id": 0}).to_list(50)
        branches = await db.branches.find({}, {"_id": 0}).sort("created_at", -1).to_list(500)
        return branches
    branch_id = user.get("branch_id")
    if not branch_id:
        return []
    return await db.branches.find({"id": branch_id}, {"_id": 0}).to_list(50)


@api.post("/branches")
async def create_branch(data: BranchIn, user=Depends(require_roles("director", "admin"))):
    if user["role"] == "admin" and user.get("branch_id"):
        raise HTTPException(status_code=403, detail="Admin filial yaratolmaydi")
    code = (data.code or data.name).strip()
    if not code:
        raise HTTPException(status_code=400, detail="Filial kodi yoki nomi bo'sh bo'lmasligi kerak")
    if await db.branches.find_one({"code": code}):
        raise HTTPException(status_code=400, detail="Bunday filial kodi allaqachon mavjud")
    branch = {
        "id": str(uuid.uuid4()),
        "name": data.name.strip(),
        "code": code,
        "address": data.address.strip(),
        "active": data.active,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.branches.insert_one(branch)
    return {key: value for key, value in branch.items() if key != "_id"}


@api.put("/branches/{bid}")
async def update_branch(bid: str, data: BranchIn, user=Depends(require_roles("director", "admin"))):
    if user["role"] == "admin" and user.get("branch_id") and bid != user["branch_id"]:
        raise HTTPException(status_code=403, detail="Siz boshqa filialni o'zgartira olmaysiz")
    existing = await db.branches.find_one({"id": bid}, {"_id": 0})
    if not existing:
        raise HTTPException(status_code=404, detail="Filial topilmadi")
    new_code = (data.code or data.name).strip()
    if new_code and await db.branches.find_one({"code": new_code, "id": {"$ne": bid}}):
        raise HTTPException(status_code=400, detail="Bunday filial kodi allaqachon mavjud")
    update = {
        "name": data.name.strip(),
        "code": new_code,
        "address": data.address.strip(),
        "active": data.active,
    }
    await db.branches.update_one({"id": bid}, {"$set": update})
    return {**existing, **update}


@api.delete("/branches/{bid}")
async def delete_branch(bid: str, user=Depends(require_roles("director", "admin"))):
    if user["role"] == "admin" and user.get("branch_id") and bid != user["branch_id"]:
        raise HTTPException(status_code=403, detail="Siz boshqa filialni o'chira olmaysiz")
    branch = await db.branches.find_one({"id": bid}, {"_id": 0})
    if not branch:
        raise HTTPException(status_code=404, detail="Filial topilmadi")
    await db.users.update_many({"branch_id": bid}, {"$set": {"branch_id": None, "branch_name": None}})
    await db.products.update_many({"branch_id": bid}, {"$set": {"branch_id": None}})
    await db.sales.update_many({"branch_id": bid}, {"$set": {"branch_id": None}})
    await db.branches.delete_one({"id": bid})
    return {"ok": True}


# ---------- Users ----------
@api.get("/users")
async def list_users(user=Depends(require_roles("director", "admin"))):
    query = {}
    if user["role"] == "admin" and user.get("branch_id"):
        query["branch_id"] = user["branch_id"]
    users = await db.users.find(query, {"_id": 0, "password_hash": 0}).sort("created_at", -1).to_list(5000)
    return users


@api.get("/users/workers")
async def list_workers(user=Depends(require_roles("director", "worker", "admin"))):
    query = {"role": "worker"}
    if user["role"] == "worker" and user.get("branch_id"):
        query["branch_id"] = user["branch_id"]
    elif user["role"] == "admin" and user.get("branch_id"):
        query["branch_id"] = user["branch_id"]
    return await db.users.find(query, {"_id": 0, "password_hash": 0}).to_list(100)


@api.post("/users/workers")
async def create_worker(data: CreateWorker, user=Depends(require_roles("director", "admin"))):
    count = await db.users.count_documents({"role": "worker"})
    if count >= 4:
        raise HTTPException(400, "Maksimal 4 ta ishchi bo'lishi mumkin")
    email = data.email.lower()
    if await db.users.find_one({"email": email}):
        raise HTTPException(400, "Bu email allaqachon mavjud")
    branch_id = data.branch_id or user.get("branch_id")
    if user["role"] == "admin" and branch_id and branch_id != user.get("branch_id"):
        raise HTTPException(status_code=403, detail="Admin boshqa filialga ishchi qo'sha olmaydi")
    if user["role"] == "admin" and not branch_id:
        raise HTTPException(status_code=400, detail="Ishchi filialga biriktirilishi kerak")
    if not branch_id:
        branch = await db.branches.find_one({}, {"_id": 0})
        if branch:
            branch_id = branch["id"]
    branch_name = None
    if branch_id:
        branch = await db.branches.find_one({"id": branch_id}, {"_id": 0})
        branch_name = branch.get("name") if branch else branch_name
    w = {
        "id": str(uuid.uuid4()),
        "email": email,
        "password_hash": hash_password(data.password),
        "name": data.name,
        "surname": data.surname,
        "phone": data.phone,
        "role": "worker",
        "branch_id": branch_id,
        "branch_name": branch_name,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.users.insert_one(w)
    return serialize_user(w)


@api.delete("/users/workers/{wid}")
async def delete_worker(wid: str, user=Depends(require_roles("director", "admin"))):
    target = await db.users.find_one({"id": wid, "role": "worker"}, {"_id": 0})
    if not target:
        raise HTTPException(404, "Ishchi topilmadi")
    if user["role"] == "admin" and user.get("branch_id") and target.get("branch_id") and target["branch_id"] != user["branch_id"]:
        raise HTTPException(status_code=403, detail="Bu filial ishchisini o'chira olmaysiz")
    res = await db.users.delete_one({"id": wid, "role": "worker"})
    if res.deleted_count == 0:
        raise HTTPException(404, "Ishchi topilmadi")
    return {"ok": True}


# ---------- Questions ----------
@api.post("/questions")
async def submit_question(data: QuestionIn):
    q = data.model_dump()
    q["id"] = str(uuid.uuid4())
    q["answered"] = False
    q["created_at"] = datetime.now(timezone.utc).isoformat()
    await db.questions.insert_one(q)
    await db.notifications.insert_one({
        "id": str(uuid.uuid4()), "type": "new_question", "for_role": "admin",
        "sound": True, "title": "Yangi savol",
        "message": f"{q['name']} - {q['message'][:60]}",
        "ref_id": q["id"], "created_at": q["created_at"], "seen": False,
    })
    asyncio.create_task(notify_admin(
        "Yangi savol",
        f"{q['name']} ({q['email']}) yangi savol yubordi:\n\n{q['message']}"
    ))
    q.pop("_id", None)
    return q


@api.get("/questions")
async def list_questions(user=Depends(require_roles("admin"))):
    return await db.questions.find({}, {"_id": 0}).sort("created_at", -1).to_list(2000)


@api.post("/questions/{qid}/answered")
async def mark_question_answered(qid: str, user=Depends(require_roles("admin"))):
    await db.questions.update_one({"id": qid}, {"$set": {"answered": True}})
    return {"ok": True}


# ---------- Notifications ----------
@api.get("/notifications/poll")
async def poll_notifications(request: Request, user=Depends(get_current_user)):
    role = user["role"]
    if role not in ("director", "admin"):
        return {"items": [], "play_sound": False}
    items = await db.notifications.find(
        {"for_role": role, "seen": False}, {"_id": 0}
    ).sort("created_at", -1).to_list(50)
    play_sound = any(i.get("sound") for i in items)
    return {"items": items, "play_sound": play_sound}


@api.post("/notifications/seen")
async def mark_notifications_seen(user=Depends(get_current_user)):
    await db.notifications.update_many(
        {"for_role": user["role"], "seen": False},
        {"$set": {"seen": True}},
    )
    return {"ok": True}


# ---------- Stats ----------
@api.get("/stats/overview")
async def stats_overview(branch_id: Optional[str] = None, user=Depends(require_roles("director"))):
    if branch_id and not await db.branches.find_one({"id": branch_id}):
        raise HTTPException(status_code=404, detail="Filial topilmadi")
    branch_match = {"branch_id": branch_id} if branch_id else {}
    total_orders = await db.orders.count_documents(branch_match)
    total_sales = await db.sales.count_documents(branch_match)
    total_customers = await db.users.count_documents({"role": "customer"})
    total_workers = await db.users.count_documents({"role": "worker"})

    # Sales aggregation — "total" maydoni bo'yicha yig'amiz
    sales_agg = await db.sales.aggregate(([{"$match": branch_match}] if branch_match else []) + [
        {"$group": {
            "_id": None,
            "revenue": {"$sum": {"$ifNull": ["$total", 0]}},
            "cost": {"$sum": {"$ifNull": ["$cost_total", 0]}},
            "profit": {"$sum": {"$ifNull": ["$profit", 0]}},
            "discount": {"$sum": {"$ifNull": ["$discount_total", 0]}},
        }}
    ]).to_list(1)
    sales_stats = sales_agg[0] if sales_agg else {"revenue": 0, "cost": 0, "profit": 0, "discount": 0}

    # Orders aggregation
    orders_agg = await db.orders.aggregate(([{"$match": branch_match}] if branch_match else []) + [
        {"$group": {
            "_id": None,
            "revenue": {"$sum": {"$ifNull": ["$total", 0]}},
            "cost": {"$sum": {"$ifNull": ["$cost_total", 0]}},
            "profit": {"$sum": {"$ifNull": ["$profit", 0]}},
            "discount": {"$sum": {"$ifNull": ["$discount_total", 0]}},
        }}
    ]).to_list(1)
    orders_stats = orders_agg[0] if orders_agg else {"revenue": 0, "cost": 0, "profit": 0, "discount": 0}

    # Per-worker
    per_worker = await db.sales.aggregate(([{"$match": branch_match}] if branch_match else []) + [
        {"$group": {
            "_id": "$worker_id",
            "worker_name": {"$first": "$worker_name"},
            "count": {"$sum": "$quantity"},
            "revenue": {"$sum": {"$ifNull": ["$total", 0]}},
            "profit": {"$sum": {"$ifNull": ["$profit", 0]}},
        }},
        {"$sort": {"revenue": -1}},
    ]).to_list(10)
    for w in per_worker:
        w["worker_id"] = w.pop("_id")

    # Daily last 14 days
    cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    daily_match = {**branch_match, "created_at": {"$gte": cutoff}}
    daily = await db.sales.aggregate([
        {"$match": daily_match},
        {"$group": {
            "_id": {"$substr": ["$created_at", 0, 10]},
            "revenue": {"$sum": {"$ifNull": ["$total", 0]}},
            "profit": {"$sum": {"$ifNull": ["$profit", 0]}},
            "discount": {"$sum": {"$ifNull": ["$discount_total", 0]}},
            "count": {"$sum": "$quantity"},
        }},
        {"$sort": {"_id": 1}},
    ]).to_list(50)
    daily = [{"date": d["_id"], "revenue": d["revenue"], "profit": d.get("profit", 0),
              "discount": d.get("discount", 0), "count": d["count"]} for d in daily]

    # Monthly last 12
    monthly = await db.sales.aggregate(([{"$match": branch_match}] if branch_match else []) + [
        {"$group": {
            "_id": {"$substr": ["$created_at", 0, 7]},
            "revenue": {"$sum": {"$ifNull": ["$total", 0]}},
            "profit": {"$sum": {"$ifNull": ["$profit", 0]}},
            "discount": {"$sum": {"$ifNull": ["$discount_total", 0]}},
            "count": {"$sum": "$quantity"},
        }},
        {"$sort": {"_id": -1}},
        {"$limit": 12},
    ]).to_list(12)
    monthly = list(reversed([{
        "month": m["_id"], "revenue": m["revenue"], "profit": m.get("profit", 0),
        "discount": m.get("discount", 0), "count": m["count"]
    } for m in monthly]))

    # Top products
    top_products = await db.sales.aggregate(([{"$match": branch_match}] if branch_match else []) + [
        {"$group": {
            "_id": "$product_id",
            "name": {"$first": "$product_name"},
            "qty": {"$sum": "$quantity"},
            "revenue": {"$sum": {"$ifNull": ["$total", 0]}},
            "profit": {"$sum": {"$ifNull": ["$profit", 0]}},
        }},
        {"$sort": {"qty": -1}},
        {"$limit": 5},
    ]).to_list(5)
    for p in top_products:
        p["product_id"] = p.pop("_id")

    total_revenue = sales_stats["revenue"] + orders_stats["revenue"]
    total_cost = sales_stats["cost"] + orders_stats["cost"]
    total_profit = sales_stats["profit"] + orders_stats["profit"]
    total_discount = sales_stats["discount"] + orders_stats["discount"]

    return {
        "total_orders": total_orders,
        "total_sales": total_sales,
        "total_customers": total_customers,
        "total_workers": total_workers,
        "sales_revenue": sales_stats["revenue"],
        "orders_revenue": orders_stats["revenue"],
        "total_revenue": total_revenue,
        "total_cost": total_cost,
        "total_profit": total_profit,
        "total_discount": total_discount,
        "per_worker": per_worker,
        "daily": daily,
        "monthly": monthly,
        "top_products": top_products,
    }


@api.get("/stats/customer-last-purchase")
async def customer_last_purchase(user=Depends(require_roles("director"))):
    last_sales = await db.sales.aggregate([
        {"$sort": {"created_at": -1}},
        {"$group": {
            "_id": "$customer_phone",
            "customer_name": {"$first": "$customer_name"},
            "customer_surname": {"$first": "$customer_surname"},
            "last_product": {"$first": "$product_name"},
            "last_worker": {"$first": "$worker_name"},
            "last_at": {"$first": "$created_at"},
        }},
    ]).to_list(2000)
    count_map = {
        c.get("phone_normalized"): int(c.get("purchases_count", 0) or 0)
        for c in await db.customers.find({}, {"_id": 0, "phone_normalized": 1, "purchases_count": 1}).to_list(5000)
        if c.get("phone_normalized")
    }
    out = []
    for x in last_sales:
        phone = x.pop("_id")
        out.append({
            **x,
            "customer_phone": phone,
            "purchase_count": count_map.get(normalize_phone(phone), 0),
        })
    return out


# ---------- Reports ----------
@api.get("/reports/monthly")
async def monthly_report(year: int, month: int, format: str = "pdf", user=Depends(require_roles("director"))):
    if month < 1 or month > 12:
        raise HTTPException(400, "Oy 1-12 bo'lishi kerak")
    sales_all = await db.sales.find({}, {"_id": 0}).to_list(20000)
    orders_all = await db.orders.find({}, {"_id": 0}).to_list(20000)
    sales = _filter_in_month(sales_all, year, month)
    orders = _filter_in_month(orders_all, year, month)
    if format.lower() == "csv":
        data = build_csv(year, month, sales, orders)
        return StreamingResponse(
            iter([data]),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename=maison-glow-{year}-{month:02d}.csv"},
        )
    else:
        data = build_pdf(year, month, sales, orders)
        return StreamingResponse(
            iter([data]),
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename=maison-glow-{year}-{month:02d}.pdf"},
        )


@api.get("/")
async def health():
    return {"ok": True, "service": "Glow Cosmetics API"}


# ---------- CORS & Wire up ----------
_cors_env = os.environ.get(
    "CORS_ORIGINS",
    "http://localhost:3000,http://127.0.0.1:3000,http://localhost:5173,http://127.0.0.1:5173,https://doctorvita.netlify.app",
)
_cors_origins = [o.strip() for o in _cors_env.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=600,
)

api.include_router(build_pos_router(db=db, client=client, require_roles=require_roles, public_product=public_product))
app.include_router(api)


# ---------- Startup ----------
async def seed_initial_data():
    await db.users.create_index("email", unique=True)
    await db.products.create_index("id", unique=True)
    await db.products.create_index("barcode")
    await db.products.create_index([("deleted", 1), ("barcode", 1)])
    await db.products.create_index([("deleted", 1), ("sku", 1)])
    await db.products.create_index([("deleted", 1), ("name", 1)])
    await db.products.create_index([("deleted", 1), ("category", 1)])
    await db.products.create_index([("deleted", 1), ("parent_barcode", 1)])
    await db.orders.create_index("created_at")
    await db.sales.create_index("worker_id")
    await db.sales.create_index("follow_up_due_at")
    await db.attendance.create_index("worker_id")
    await db.notifications.create_index("for_role")
    await ensure_pos_indexes(db)

    director_email = os.environ["DIRECTOR_EMAIL"].lower()
    director_pw = os.environ["DIRECTOR_PASSWORD"]
    existing = await db.users.find_one({"email": director_email})
    if not existing:
        await db.users.insert_one({
            "id": str(uuid.uuid4()),
            "email": director_email,
            "password_hash": hash_password(director_pw),
            "name": "Director",
            "surname": "",
            "phone": "",
            "role": "director",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    elif not verify_password(director_pw, existing["password_hash"]):
        await db.users.update_one({"email": director_email}, {"$set": {"password_hash": hash_password(director_pw)}})

    admin_email = os.environ["ADMIN_EMAIL"].lower()
    admin_pw = os.environ["ADMIN_PASSWORD"]
    existing = await db.users.find_one({"email": admin_email})
    if not existing:
        await db.users.insert_one({
            "id": str(uuid.uuid4()),
            "email": admin_email,
            "password_hash": hash_password(admin_pw),
            "name": "Admin",
            "surname": "",
            "phone": "",
            "role": "admin",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    elif not verify_password(admin_pw, existing["password_hash"]):
        await db.users.update_one({"email": admin_email}, {"$set": {"password_hash": hash_password(admin_pw)}})

    # Real products from products_seed.json (CSV-derived inventory, 856 items)
    # IDEMPOTENT: inserts only products whose barcode is not already in DB.
    # Safe to re-run on every startup; existing admin-edited products are never touched.
    seed_path = ROOT_DIR / "products_seed.json"
    if seed_path.exists():
        import json as _json
        try:
            with open(seed_path, "r", encoding="utf-8") as _f:
                seed_products = _json.load(_f)
        except Exception as e:
            logger.error("Failed to read products_seed.json: %s", e)
            seed_products = []

        if seed_products:
            existing_barcodes = set()
            async for p in db.products.find(
                {"barcode": {"$exists": True, "$ne": ""}}, {"_id": 0, "barcode": 1}
            ):
                bc = (p.get("barcode") or "").strip()
                if bc:
                    existing_barcodes.add(bc)
            existing_names_no_bc = set()
            async for p in db.products.find(
                {"$or": [{"barcode": ""}, {"barcode": {"$exists": False}}]},
                {"_id": 0, "name": 1},
            ):
                existing_names_no_bc.add((p.get("name") or "").strip().lower())

            now_iso = datetime.now(timezone.utc).isoformat()
            seen_seed_barcodes = set()
            to_insert = []
            for d in seed_products:
                bc = (d.get("barcode") or "").strip()
                if bc:
                    if bc in existing_barcodes or bc in seen_seed_barcodes:
                        continue
                    seen_seed_barcodes.add(bc)
                else:
                    nm = (d.get("name") or "").strip().lower()
                    if nm in existing_names_no_bc:
                        continue
                doc = dict(d)
                doc["id"] = str(uuid.uuid4())
                doc["created_at"] = now_iso
                to_insert.append(doc)

            if to_insert:
                CHUNK = 500
                for i in range(0, len(to_insert), CHUNK):
                    await db.products.insert_many(to_insert[i:i + CHUNK])
                logger.info(
                    "Seeded %d new products from products_seed.json (skipped %d already-present)",
                    len(to_insert), len(seed_products) - len(to_insert),
                )
            else:
                logger.info(
                    "products_seed.json: all %d items already in DB, nothing to seed",
                    len(seed_products),
                )

    if await db.products.count_documents({}) == 0:
        demo = [
            {
                "name": "Hyaluron Glow Serum",
                "description": "Yuzga namlik beruvchi va terini yorug'lashtiruvchi yengil zardob. 30ml.",
                "price": 285000, "cost_price": 180000, "discount_percent": 10,
                "image_url": "https://static.prod-images.emergentagent.com/jobs/42a44add-5feb-4f26-bbbe-91660dd15858/images/6c81797aa02470f660c1607df9805d01242fe23e50b576d24dd4b74e26a17c7e.png",
                "category": "serum", "stock": 50, "barcode": "8901234567001", "expiry_date": "2027-06-30",
            },
            {
                "name": "Velvet Night Cream",
                "description": "Tunda ishlaydigan, terini tiklaydigan boy kechki krem. 50ml.",
                "price": 320000, "cost_price": 210000, "discount_percent": 0,
                "image_url": "https://static.prod-images.emergentagent.com/jobs/42a44add-5feb-4f26-bbbe-91660dd15858/images/368a89711f89c8185079803e32c9df624381192274ce0052518533f3aef2aa80.png",
                "category": "cream", "stock": 30, "barcode": "8901234567002", "expiry_date": "2027-03-15",
            },
            {
                "name": "Silk Lotion",
                "description": "Tana uchun yengil va shimibgina ketadigan lotion. Yasmin va vanil hidi.",
                "price": 195000, "cost_price": 120000, "discount_percent": 5,
                "image_url": "https://images.unsplash.com/photo-1688380337044-18c03ba32199?crop=entropy&cs=srgb&fm=jpg&ixlib=rb-4.1.0&q=85",
                "category": "body", "stock": 40, "barcode": "8901234567003", "expiry_date": "2026-12-01",
            },
            {
                "name": "Atelier Trio Set",
                "description": "Tozalovchi, tonizator va krem - to'liq parvarish to'plami.",
                "price": 540000, "cost_price": 350000, "discount_percent": 15,
                "image_url": "https://images.pexels.com/photos/7256060/pexels-photo-7256060.jpeg?auto=compress&cs=tinysrgb&dpr=2&h=650&w=940",
                "category": "set", "stock": 20, "barcode": "8901234567004", "expiry_date": "2027-09-20",
            },
        ]
        for d in demo:
            d["id"] = str(uuid.uuid4())
            d["created_at"] = datetime.now(timezone.utc).isoformat()
            await db.products.insert_one(d)

    await db.products.update_many({"cost_price": {"$exists": False}}, {"$set": {"cost_price": 0}})
    await db.products.update_many({"discount_percent": {"$exists": False}}, {"$set": {"discount_percent": 0}})
    await db.products.update_many({"barcode": {"$exists": False}}, {"$set": {"barcode": ""}})
    await db.products.update_many({"parent_barcode": {"$exists": False}}, {"$set": {"parent_barcode": ""}})
    await db.products.update_many({"expiry_date": {"$exists": False}}, {"$set": {"expiry_date": ""}})
    await db.products.update_many({"sku": {"$exists": False}}, {"$set": {"sku": ""}})
    await backfill_units_per_package(db)

    if await db.news.count_documents({}) == 0:
        demo_news = [
            {
                "id": str(uuid.uuid4()),
                "title": "Yangi kollektsiya - Velvet Night",
                "content": "Yangi tungi parvarish liniyasi do'konimizda.",
                "image_url": "https://static.prod-images.emergentagent.com/jobs/42a44add-5feb-4f26-bbbe-91660dd15858/images/368a89711f89c8185079803e32c9df624381192274ce0052518533f3aef2aa80.png",
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        ]
        for n in demo_news:
            await db.news.insert_one(n)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        reload=False,
    )

