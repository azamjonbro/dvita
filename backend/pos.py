"""Asosiy sotuv oynasi (POS) uchun API: mijozlar, sotuvni yakunlash, qayta aloqa.

Router `server.py` ichida `build_pos_router(...)` orqali yig'iladi — bazaga va
auth yordamchilariga bog'lanish shu yerda, hisob-kitob esa `pos_logic.py`da.

Kolleksiyalar:
    customers      — mijozlar kartasi (telefon = noyob identifikator)
    pos_sales      — sotuv sarlavhasi (daily_number, SALE-YYYYMMDD-NNN, jami)
    pos_sale_items — sotuv qatorlari + qabul tartibi + hisoblangan muddat
    follow_ups     — har bir dori bo'yicha qayta aloqa vazifalari
    pos_counters   — kunlik tartib raqami uchun atomik hisoblagich
    settings       — admin sozlamalari (qayta aloqa qoidalari)
    sales          — ESKI kolleksiya: direktor statistikasi va hisobotlar shu
                     yerdan o'qiydi, shuning uchun har bir qator uchun mos
                     yozuv saqlab boriladi (orqaga moslik).
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError, OperationFailure

from pos_logic import (
    DEFAULT_FOLLOW_UP_RULES,
    FOLLOW_UP_STATUSES,
    FOLLOW_UP_STATUS_LABELS,
    LOCAL_TZ,
    compute_follow_up_schedule,
    compute_regimen,
    format_sale_code,
    line_totals,
    local_now,
    local_today,
    money,
    normalize_phone,
    parse_date,
    phone_tail,
    sanitize_rules,
    units_from_product,
)

logger = logging.getLogger("pos")

STAFF = ("worker", "director", "admin")
SETTINGS_KEY = "pos_follow_up_rules"


# ---------- Pydantic modellar ----------
class CustomerIn(BaseModel):
    first_name: str = ""
    last_name: str = ""
    phone: str = ""
    age: Optional[int] = None
    gender: str = ""            # "" | "male" | "female"
    source: str = ""            # "instagram" | "telegram" | "referral" | "walk_in" | "from_region" | "other"
    height_cm: Optional[float] = None
    weight_kg: Optional[float] = None
    allergies: str = ""         # allergiyalar
    conditions: str = ""        # surunkali kasalliklar / doimiy dorilar
    complaint: str = ""
    note: str = ""
    allow_duplicate_phone: bool = False


class CustomerRef(CustomerIn):
    id: Optional[str] = None


class RegimenIn(BaseModel):
    units_per_package: int = 1
    total_units: Optional[int] = None  # berilmasa quantity × units_per_package
    times_per_day: int = 0
    units_per_intake: int = 0
    recommendation: str = ""
    course_start_date: Optional[str] = None  # YYYY-MM-DD


class FollowUpOverride(BaseModel):
    stage_key: str = ""
    stage_label: str = ""
    purpose: str = ""
    scheduled_date: str  # YYYY-MM-DD


class PosItemIn(BaseModel):
    product_id: str
    quantity: int = 1
    discount_override: float = 0  # qo'shimcha chegirma, foiz
    is_medicine: bool = True
    regimen: Optional[RegimenIn] = None
    follow_ups: Optional[List[FollowUpOverride]] = None  # sotuvchi tahrirlagan sanalar


class PosSaleIn(BaseModel):
    client_request_id: str = Field(..., min_length=8, max_length=80)
    customer: CustomerRef
    items: List[PosItemIn]
    discount_amount: float = 0  # umumiy chegirma, so'm
    note: str = ""


class FollowUpPatch(BaseModel):
    status: Optional[str] = None
    result_note: Optional[str] = None
    next_follow_up_at: Optional[str] = None
    scheduled_at: Optional[str] = None


class RulesIn(BaseModel):
    stages: List[Dict[str, Any]]
    min_course_days: int = 10


# ---------- Yordamchilar ----------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(doc: Optional[dict]) -> Optional[dict]:
    if doc is None:
        return None
    doc = dict(doc)
    doc.pop("_id", None)
    return doc


def _bmi(height_cm, weight_kg) -> Optional[float]:
    """Tana massa indeksi — bo'y va vazn kiritilgan bo'lsa hisoblanadi."""
    try:
        h = float(height_cm or 0) / 100
        w = float(weight_kg or 0)
    except (TypeError, ValueError):
        return None
    if h <= 0 or w <= 0:
        return None
    return round(w / (h * h), 1)


def _customer_view(c: dict) -> dict:
    out = _clean(c) or {}
    out["full_name"] = f"{out.get('first_name', '')} {out.get('last_name', '')}".strip()
    out["phone_tail"] = phone_tail(out.get("phone", ""))
    out["bmi"] = _bmi(out.get("height_cm"), out.get("weight_kg"))
    return out


def _regimen_text(reg: Optional[dict]) -> str:
    if not reg or not reg.get("daily_usage"):
        return ""
    txt = f"Kuniga {reg['times_per_day']} mahal, har mahal {reg['units_per_intake']} tadan"
    if reg.get("recommendation"):
        txt += f" — {reg['recommendation']}"
    return txt


def _worker_name(user: dict) -> str:
    return f"{user.get('name', '')} {user.get('surname', '')}".strip()


def _is_txn_unsupported(err: Exception) -> bool:
    """Standalone MongoDB tranzaksiyani qo'llamaydi (kod 20 / IllegalOperation)."""
    if isinstance(err, OperationFailure):
        if err.code in (20, 263):
            return True
        return "Transaction numbers" in str(err) or "replica set" in str(err)
    return False


def _validate_vitals(c: CustomerIn):
    """Bo'y / vazn / yosh mantiqiy oraliqda bo'lsin."""
    if c.age is not None and not (0 <= c.age <= 120):
        raise HTTPException(400, "Yosh 0 dan 120 gacha bo'lishi kerak")
    if c.height_cm is not None and not (30 <= c.height_cm <= 260):
        raise HTTPException(400, "Bo'y 30 dan 260 sm gacha bo'lishi kerak")
    if c.weight_kg is not None and not (2 <= c.weight_kg <= 400):
        raise HTTPException(400, "Vazn 2 dan 400 kg gacha bo'lishi kerak")


def build_pos_router(*, db, client, require_roles: Callable, public_product: Callable) -> APIRouter:
    r = APIRouter()

    async def load_rules() -> dict:
        doc = await db.settings.find_one({"_id": SETTINGS_KEY})
        if not doc:
            return dict(DEFAULT_FOLLOW_UP_RULES)
        return {"stages": doc.get("stages", DEFAULT_FOLLOW_UP_RULES["stages"]),
                "min_course_days": doc.get("min_course_days", DEFAULT_FOLLOW_UP_RULES["min_course_days"])}

    # =============== Sozlamalar ===============
    @r.get("/pos/settings")
    async def get_settings(user=Depends(require_roles(*STAFF))):
        rules = await load_rules()
        return {**rules, "statuses": [{"key": k, "label": FOLLOW_UP_STATUS_LABELS[k]} for k in FOLLOW_UP_STATUSES]}

    @r.put("/pos/settings")
    async def put_settings(data: RulesIn, user=Depends(require_roles("director", "admin"))):
        try:
            rules = sanitize_rules(data.model_dump())
        except ValueError as e:
            raise HTTPException(400, str(e))
        await db.settings.update_one(
            {"_id": SETTINGS_KEY},
            {"$set": {**rules, "updated_at": _now_iso(), "updated_by": user["id"]}},
            upsert=True,
        )
        return rules

    # =============== Mahsulot qidiruvi (typeahead) ===============
    @r.get("/pos/products/search")
    async def pos_product_search(q: str = "", limit: int = Query(12, ge=1, le=50),
                                 user=Depends(require_roles(*STAFF))):
        """Nom, SKU yoki shtrix-kod bo'yicha tezkor qidiruv. Aniq kod mosligi birinchi chiqadi."""
        term = (q or "").strip()
        base = {"deleted": {"$ne": True}}
        branch_id = user.get("branch_id")
        if branch_id:
            base["$or"] = [{"all_branches": True}, {"branch_id": branch_id}]
        else:
            return []
        if not term:
            items = await db.products.find(base, {"_id": 0}).sort("name", 1).limit(limit).to_list(length=None)
        else:
            if len(term) < 2 and not term.isdigit():
                return []
            prefix = {"$regex": f"^{re.escape(term)}", "$options": "i"}
            exact = await db.products.find(
                {"$and": [base, {"$or": [{"barcode": term}, {"sku": term}]}]}, {"_id": 0}
            ).limit(limit).to_list(length=None)
            exact_ids = {p["id"] for p in exact}
            rest = await db.products.find(
                {"$and": [base, {"$or": [{"name": prefix}, {"sku": prefix}, {"barcode": prefix}, {"manufacturer": prefix}]}]},
                {"_id": 0},
            ).sort("name", 1).limit(limit).to_list(length=None)
            items = exact + [p for p in rest if p["id"] not in exact_ids]
            items = items[:limit]
        out = []
        for p in items:
            v = public_product(p, hide_cost=True)
            v["units_per_package"] = units_from_product(p)
            v.setdefault("sku", "")
            out.append(v)
        return out

    # =============== Mijozlar ===============
    @r.get("/customers/search")
    async def customers_search(q: str = "", limit: int = Query(8, ge=1, le=30),
                               user=Depends(require_roles(*STAFF))):
        term = (q or "").strip()
        if len(term) < 2:
            return []
        digits = re.sub(r"\D", "", term)
        ors: List[dict] = []
        rx = {"$regex": re.escape(term), "$options": "i"}
        ors += [{"first_name": rx}, {"last_name": rx}, {"full_name_lc": rx}]
        if digits:
            ors.append({"phone_normalized": {"$regex": re.escape(digits)}})
        found = await db.customers.find({"$or": ors}, {"_id": 0}).sort("last_purchase_at", -1).to_list(limit)
        return [_customer_view(c) for c in found]

    @r.get("/customers/{cid}")
    async def customer_detail(cid: str, user=Depends(require_roles(*STAFF))):
        c = await db.customers.find_one({"id": cid}, {"_id": 0})
        if not c:
            raise HTTPException(404, "Mijoz topilmadi")
        sales = await db.pos_sales.find({"customer_id": cid}, {"_id": 0}).sort("sale_date_time", -1).to_list(30)
        for s in sales:
            s["items"] = await db.pos_sale_items.find({"sale_id": s["id"]}, {"_id": 0}).to_list(100)
        fus = await db.follow_ups.find(
            {"customer_id": cid, "status": {"$in": ["planned", "postponed"]}}, {"_id": 0}
        ).sort("scheduled_at", 1).to_list(100)
        return {**_customer_view(c), "purchases": sales, "active_follow_ups": fus}

    async def upsert_customer(ref: CustomerRef, session=None) -> dict:
        """Mijozni topadi yoki yaratadi. Telefon takrorlansa va ruxsat bo'lmasa 409."""
        now = _now_iso()
        phone_norm = normalize_phone(ref.phone)
        patch = {
            "first_name": ref.first_name.strip(),
            "last_name": ref.last_name.strip(),
            "phone": ref.phone.strip(),
            "phone_normalized": phone_norm,
            "full_name_lc": f"{ref.first_name} {ref.last_name}".strip().lower(),
            "age": ref.age,
            "gender": ref.gender if ref.gender in ("male", "female") else "",
            "source": ref.source.strip(),
            "height_cm": ref.height_cm,
            "weight_kg": ref.weight_kg,
            "allergies": ref.allergies.strip(),
            "conditions": ref.conditions.strip(),
            "complaint": ref.complaint.strip(),
            "note": ref.note.strip(),
            "updated_at": now,
        }
        if ref.id:
            existing = await db.customers.find_one({"id": ref.id}, session=session)
            if not existing:
                raise HTTPException(404, "Tanlangan mijoz bazada topilmadi")
            # Bo'sh yuborilgan maydonlar eski qiymatni o'chirib yubormasin.
            for k in ("first_name", "last_name", "phone", "complaint", "note",
                      "gender", "source", "allergies", "conditions"):
                if not patch[k]:
                    patch[k] = existing.get(k, "")
            for k in ("age", "height_cm", "weight_kg"):
                if patch[k] is None:
                    patch[k] = existing.get(k)
            patch["phone_normalized"] = normalize_phone(patch["phone"])
            patch["full_name_lc"] = f"{patch['first_name']} {patch['last_name']}".strip().lower()
            await db.customers.update_one({"id": ref.id}, {"$set": patch}, session=session)
            return {**existing, **patch}

        same_phone = await db.customers.find_one({"phone_normalized": phone_norm}, session=session) if phone_norm else None
        if same_phone and not ref.allow_duplicate_phone:
            raise HTTPException(409, {
                "code": "duplicate_phone",
                "msg": f"Bu telefon raqami bilan mijoz mavjud: {same_phone.get('first_name','')} {same_phone.get('last_name','')}",
                "customer": _customer_view(same_phone),
            })
        doc = {"id": str(uuid.uuid4()), "created_at": now, "purchases_count": 0,
               "last_purchase_at": None, **patch}
        await db.customers.insert_one(doc, session=session)
        return doc

    @r.post("/customers")
    async def create_customer(data: CustomerIn, user=Depends(require_roles(*STAFF))):
        if not data.first_name.strip():
            raise HTTPException(400, "Mijoz ismi majburiy")
        if not normalize_phone(data.phone):
            raise HTTPException(400, "Telefon raqami majburiy")
        _validate_vitals(data)
        c = await upsert_customer(CustomerRef(**data.model_dump()))
        return _customer_view(c)

    @r.put("/customers/{cid}")
    async def update_customer(cid: str, data: CustomerIn, user=Depends(require_roles(*STAFF))):
        c = await upsert_customer(CustomerRef(id=cid, **data.model_dump()))
        return _customer_view(c)

    # =============== Kunlik raqam ===============
    def counter_key(day) -> str:
        return f"pos_daily:{day.isoformat()}"

    @r.get("/pos/next-number")
    async def next_number(user=Depends(require_roles("worker"))):
        """Faqat ko'rsatish uchun — haqiqiy raqam sotuv yakunlanganda atomik beriladi."""
        today = local_today()
        c = await db.pos_counters.find_one({"_id": counter_key(today)})
        seq = int(c.get("seq", 0)) + 1 if c else 1
        return {"daily_number": seq, "sale_code": format_sale_code(today, seq),
                "date": today.isoformat(), "server_time": local_now().isoformat()}

    # =============== Sotuvni yakunlash ===============
    @r.post("/pos/sales")
    async def complete_sale(data: PosSaleIn, user=Depends(require_roles("worker"))):
        # 1) Idempotentlik: bir xil so'rov ikki marta kelsa, avvalgi sotuv qaytariladi.
        dup = await db.pos_sales.find_one({"client_request_id": data.client_request_id}, {"_id": 0})
        if dup:
            return {**(await sale_bundle(dup)), "duplicate": True}

        # 2) Majburiy maydonlar
        if not data.items:
            raise HTTPException(400, "Kamida bitta mahsulot tanlang")
        if not data.customer.id and not data.customer.first_name.strip():
            raise HTTPException(400, "Mijoz ismi majburiy")
        if not data.customer.id and not normalize_phone(data.customer.phone):
            raise HTTPException(400, "Mijoz telefon raqami majburiy")
        if data.discount_amount < 0:
            raise HTTPException(400, "Chegirma manfiy bo'lmasligi kerak")
        _validate_vitals(data.customer)

        rules = await load_rules()
        today = local_today()
        now_utc = datetime.now(timezone.utc)

        # 3) Mahsulotlarni yuklash, qoldiqni tekshirish, qatorlarni hisoblash
        prepared = []
        for idx, it in enumerate(data.items, start=1):
            if it.quantity <= 0:
                raise HTTPException(400, f"{idx}-qator: miqdor 0 dan katta bo'lishi kerak")
            product = await db.products.find_one({"id": it.product_id, "deleted": {"$ne": True}}, {"_id": 0})
            if not product:
                raise HTTPException(404, f"{idx}-qator: mahsulot topilmadi")
            stock = int(product.get("stock", 0) or 0)
            if it.quantity > stock:
                raise HTTPException(400, f"{product['name']} — omborda atigi {stock} ta bor")
            totals = line_totals(product.get("price", 0), it.quantity,
                                 product.get("discount_percent", 0), it.discount_override)
            regimen = None
            follow_ups: List[dict] = []
            if it.is_medicine:
                reg = it.regimen
                if not reg or reg.times_per_day <= 0 or reg.units_per_intake <= 0:
                    raise HTTPException(400, f"{product['name']}: kuniga necha mahal va har mahal nechtadan — majburiy")
                start = parse_date(reg.course_start_date, today)
                upp = reg.units_per_package if reg.units_per_package > 0 else units_from_product(product)
                calc = compute_regimen(quantity=it.quantity, units_per_package=upp,
                                       times_per_day=reg.times_per_day, units_per_intake=reg.units_per_intake,
                                       course_start=start, total_units=reg.total_units)
                if calc["total_units"] <= 0:
                    raise HTTPException(400, f"{product['name']}: jami dona soni 0 dan katta bo'lishi kerak")
                regimen = {
                    "units_per_package": upp,
                    "times_per_day": reg.times_per_day,
                    "units_per_intake": reg.units_per_intake,
                    "recommendation": reg.recommendation.strip(),
                    **calc,
                }
                if it.follow_ups is not None:
                    # Sotuvchi tahrirlagan sanalar — tekshirib qabul qilinadi
                    seen = set()
                    for fo in it.follow_ups:
                        d = parse_date(fo.scheduled_date)
                        if not d:
                            raise HTTPException(400, f"{product['name']}: qayta aloqa sanasi noto'g'ri")
                        if d < today:
                            raise HTTPException(400, f"{product['name']}: o'tib ketgan qayta aloqa sanasi ({d.isoformat()})")
                        if d.isoformat() in seen:
                            continue
                        seen.add(d.isoformat())
                        follow_ups.append({
                            "follow_up_number": len(follow_ups) + 1,
                            "stage_key": fo.stage_key or f"custom{len(follow_ups) + 1}",
                            "stage_label": fo.stage_label or f"{len(follow_ups) + 1}-aloqa",
                            "purpose": fo.purpose,
                            "day_offset": (d - start).days,
                            "scheduled_date": d.isoformat(),
                            "manual_review": False,
                        })
                    follow_ups.sort(key=lambda x: x["scheduled_date"])
                    for i, f in enumerate(follow_ups, start=1):
                        f["follow_up_number"] = i
                else:
                    follow_ups = compute_follow_up_schedule(calc["estimated_days"], start, rules)
            prepared.append({"product": product, "item": it, "totals": totals,
                             "regimen": regimen, "follow_ups": follow_ups})

        subtotal = money(sum(p["totals"]["line_total"] for p in prepared))
        item_discount = money(sum(p["totals"]["discount_total"] for p in prepared))
        extra_discount = money(min(data.discount_amount, subtotal))
        total = money(subtotal - extra_discount)

        sale_id = str(uuid.uuid4())
        worker_name = _worker_name(user)

        # 4) Atomik yozish: tranzaksiya (replica set) yoki kompensatsiyali fallback
        undo_stock: List[tuple] = []  # fallback rejimida qoldiqni qaytarish uchun

        async def commit(session) -> dict:
            customer = await upsert_customer(data.customer, session=session)
            counter = await db.pos_counters.find_one_and_update(
                {"_id": counter_key(today)}, {"$inc": {"seq": 1}},
                upsert=True, return_document=ReturnDocument.AFTER, session=session,
            )
            daily_number = int(counter["seq"])
            sale_code = format_sale_code(today, daily_number)
            sale_doc = {
                "id": sale_id,
                "sale_code": sale_code,
                "client_request_id": data.client_request_id,
                "daily_number": daily_number,
                "sale_date": today.isoformat(),
                "sale_date_time": now_utc.isoformat(),
                "employee_id": user["id"],
                "employee_name": worker_name,
                "customer_id": customer["id"],
                "customer_name": f"{customer.get('first_name','')} {customer.get('last_name','')}".strip(),
                "customer_phone": customer.get("phone", ""),
                "customer_age": customer.get("age"),
                "customer_height_cm": customer.get("height_cm"),
                "customer_weight_kg": customer.get("weight_kg"),
                "subtotal": subtotal,
                "item_discount": item_discount,
                "discount": extra_discount,
                "total": total,
                "note": data.note.strip(),
                "status": "completed",
                "created_at": now_utc.isoformat(),
            }
            item_docs, fu_docs, legacy_docs = [], [], []
            for p in prepared:
                prod, it, t = p["product"], p["item"], p["totals"]
                # Qoldiq shartli kamaytiriladi — parallel sotuvda minusga tushmaydi
                res = await db.products.update_one(
                    {"id": prod["id"], "stock": {"$gte": it.quantity}},
                    {"$inc": {"stock": -int(it.quantity)}}, session=session,
                )
                if res.matched_count == 0:
                    raise HTTPException(409, f"{prod['name']} — qoldiq o'zgarib qoldi, sotuv bekor qilindi. Qayta urinib ko'ring.")
                undo_stock.append((prod["id"], int(it.quantity)))
                item_id = str(uuid.uuid4())
                reg = p["regimen"] or {}
                item_doc = {
                    "id": item_id,
                    "sale_id": sale_id,
                    "product_id": prod["id"],
                    "product_name": prod["name"],
                    "sku": prod.get("sku", ""),
                    "barcode": prod.get("barcode", ""),
                    "quantity": int(it.quantity),
                    "unit_price": t["unit_price"],
                    "original_price": t["original_price"],
                    "discount_percent": t["discount_percent"],
                    "line_total": t["line_total"],
                    "discount_total": t["discount_total"],
                    "is_medicine": bool(it.is_medicine),
                    "units_per_package": reg.get("units_per_package"),
                    "total_units": reg.get("total_units"),
                    "times_per_day": reg.get("times_per_day"),
                    "units_per_intake": reg.get("units_per_intake"),
                    "recommendation": reg.get("recommendation", ""),
                    "course_start_date": reg.get("course_start_date"),
                    "daily_usage": reg.get("daily_usage"),
                    "estimated_days": reg.get("estimated_days"),
                    "estimated_end_date": reg.get("estimated_end_date"),
                    "follow_up_count": len(p["follow_ups"]),
                }
                item_docs.append(item_doc)
                for f in p["follow_ups"]:
                    fu_docs.append({
                        "id": str(uuid.uuid4()),
                        "customer_id": customer["id"],
                        "customer_name": sale_doc["customer_name"],
                        "customer_phone": sale_doc["customer_phone"],
                        "sale_id": sale_id,
                        "sale_code": sale_code,
                        "daily_number": daily_number,
                        "sale_item_id": item_id,
                        "product_id": prod["id"],
                        "product_name": prod["name"],
                        "recommendation": _regimen_text(reg),
                        "sale_date": today.isoformat(),
                        "estimated_end_date": reg.get("estimated_end_date"),
                        "assigned_employee_id": user["id"],
                        "assigned_employee_name": worker_name,
                        "follow_up_number": f["follow_up_number"],
                        "stage_key": f["stage_key"],
                        "stage_label": f["stage_label"],
                        "purpose": f.get("purpose", ""),
                        "day_offset": f["day_offset"],
                        "scheduled_at": f["scheduled_date"],
                        "manual_review": bool(f.get("manual_review")),
                        "status": "planned",
                        "result_note": "",
                        "next_follow_up_at": None,
                        "completed_at": None,
                        "created_at": now_utc.isoformat(),
                        "updated_at": now_utc.isoformat(),
                    })
                cost_price = float(prod.get("cost_price", 0) or 0)
                # Eski `sales` kolleksiyasi — direktor statistikasi/hisobotlar uchun
                legacy_docs.append({
                    "id": str(uuid.uuid4()),
                    "receipt_id": sale_id,
                    "pos_sale_id": sale_id,
                    "sale_code": sale_code,
                    "daily_number": daily_number,
                    "worker_id": user["id"],
                    "worker_name": worker_name,
                    "customer_id": customer["id"],
                    "customer_name": customer.get("first_name", ""),
                    "customer_surname": customer.get("last_name", ""),
                    "customer_phone": customer.get("phone", ""),
                    "product_id": prod["id"],
                    "product_name": prod["name"],
                    "product_price": t["unit_price"],
                    "original_price": t["original_price"],
                    "discount_percent": t["discount_percent"],
                    "discount_override": float(it.discount_override or 0),
                    "discount_total": t["discount_total"],
                    "cost_price": cost_price,
                    "cost_total": money(cost_price * it.quantity),
                    "profit": money((t["unit_price"] - cost_price) * it.quantity),
                    "quantity": int(it.quantity),
                    "total": t["line_total"],
                    "reason": customer.get("complaint", ""),
                    "created_at": now_utc.isoformat(),
                    "follow_up_due_at": reg.get("estimated_end_date") or (now_utc + timedelta(days=30)).isoformat(),
                    "follow_up_done": True,  # yangi follow_ups kolleksiyasi ishlatiladi
                })
            await db.pos_sales.insert_one(sale_doc, session=session)
            await db.pos_sale_items.insert_many(item_docs, session=session)
            if fu_docs:
                await db.follow_ups.insert_many(fu_docs, session=session)
            await db.sales.insert_many(legacy_docs, session=session)
            await db.customers.update_one(
                {"id": customer["id"]},
                {"$inc": {"purchases_count": 1}, "$set": {"last_purchase_at": now_utc.isoformat()}},
                session=session,
            )
            return {"sale": sale_doc, "items": item_docs, "follow_ups": fu_docs,
                    "customer": customer, "legacy_ids": [d["id"] for d in legacy_docs]}

        result: Optional[dict] = None
        try:
            async with await client.start_session() as s:
                async with s.start_transaction():
                    result = await commit(s)
        except DuplicateKeyError:
            dup = await db.pos_sales.find_one({"client_request_id": data.client_request_id}, {"_id": 0})
            if dup:
                return {**(await sale_bundle(dup)), "duplicate": True}
            raise HTTPException(409, "Sotuv allaqachon saqlangan")
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            if not _is_txn_unsupported(e):
                logger.exception("POS sale failed inside transaction")
                raise HTTPException(500, "Sotuvni saqlashda xatolik — hech narsa saqlanmadi. Qayta urinib ko'ring.")
            # Fallback: tranzaksiyasiz — xatolikda qo'lda orqaga qaytariladi
            logger.warning("MongoDB tranzaksiyani qo'llamaydi — kompensatsiyali rejimda saqlanadi")
            undo_stock.clear()
            try:
                result = await commit(None)
            except Exception as inner:  # noqa: BLE001
                await _rollback_partial(sale_id, undo_stock, data.customer)
                if isinstance(inner, HTTPException):
                    raise
                if isinstance(inner, DuplicateKeyError):
                    raise HTTPException(409, "Sotuv allaqachon saqlangan")
                logger.exception("POS sale failed (fallback)")
                raise HTTPException(500, "Sotuvni saqlashda xatolik — operatsiya bekor qilindi.")

        assert result is not None
        # 5) Kam qoldiq bildirishnomasi — tranzaksiyadan tashqarida, muhim emas
        for p in prepared:
            fresh = await db.products.find_one({"id": p["product"]["id"]}, {"_id": 0, "stock": 1, "name": 1})
            if fresh and int(fresh.get("stock", 0)) <= 5:
                for role, sound in (("admin", True), ("director", False)):
                    await db.notifications.insert_one({
                        "id": str(uuid.uuid4()), "type": "low_stock", "for_role": role, "sound": sound,
                        "title": "Mahsulot oz qoldi",
                        "message": f"{fresh['name']} — atigi {int(fresh.get('stock', 0))} ta qoldi",
                        "ref_id": p["product"]["id"], "created_at": _now_iso(), "seen": False,
                    })
        return {
            "sale": _clean(result["sale"]),
            "items": [_clean(i) for i in result["items"]],
            "follow_ups": [_clean(f) for f in result["follow_ups"]],
            "customer": _customer_view(result["customer"]),
            "duplicate": False,
        }

    async def _rollback_partial(sale_id: str, undo_stock: List[tuple], cust: CustomerRef):
        """Tranzaksiyasiz rejimda yarim saqlangan yozuvlarni tozalaydi."""
        try:
            for pid, qty in undo_stock:
                await db.products.update_one({"id": pid}, {"$inc": {"stock": int(qty)}})
            sale_existed = await db.pos_sales.find_one({"id": sale_id})
            await db.pos_sale_items.delete_many({"sale_id": sale_id})
            await db.follow_ups.delete_many({"sale_id": sale_id})
            await db.sales.delete_many({"pos_sale_id": sale_id})
            await db.pos_sales.delete_one({"id": sale_id})
            if sale_existed and cust.id:
                await db.customers.update_one({"id": cust.id}, {"$inc": {"purchases_count": -1}})
        except Exception:  # noqa: BLE001
            logger.exception("Rollback failed for sale %s", sale_id)

    async def sale_bundle(sale: dict) -> dict:
        items = await db.pos_sale_items.find({"sale_id": sale["id"]}, {"_id": 0}).to_list(200)
        fus = await db.follow_ups.find({"sale_id": sale["id"]}, {"_id": 0}).sort("scheduled_at", 1).to_list(500)
        cust = await db.customers.find_one({"id": sale.get("customer_id")}, {"_id": 0})
        return {"sale": _clean(sale), "items": items, "follow_ups": fus,
                "customer": _customer_view(cust) if cust else None}

    @r.get("/pos/sales")
    async def list_pos_sales(date: str = "", user=Depends(require_roles(*STAFF))):
        day = parse_date(date, local_today())
        q: dict = {"sale_date": day.isoformat()}
        if user["role"] == "worker":
            q["employee_id"] = user["id"]
        sales = await db.pos_sales.find(q, {"_id": 0}).sort("daily_number", -1).to_list(500)
        for s in sales:
            s["items"] = await db.pos_sale_items.find({"sale_id": s["id"]}, {"_id": 0}).to_list(100)
        return sales

    @r.get("/pos/sales/{sid}")
    async def get_pos_sale(sid: str, user=Depends(require_roles(*STAFF))):
        sale = await db.pos_sales.find_one({"id": sid}, {"_id": 0})
        if not sale:
            raise HTTPException(404, "Sotuv topilmadi")
        if user["role"] == "worker" and sale.get("employee_id") != user["id"]:
            raise HTTPException(403, "Ruxsat yo'q")
        return await sale_bundle(sale)

    # =============== Qayta aloqa vazifalari ===============
    def _fu_scope_query(scope: str, today_iso: str) -> dict:
        active = {"$in": ["planned", "postponed"]}
        if scope == "today":
            return {"scheduled_at": today_iso, "status": active}
        if scope == "overdue":
            return {"scheduled_at": {"$lt": today_iso}, "status": active}
        if scope == "upcoming":
            return {"scheduled_at": {"$gt": today_iso}, "status": active}
        if scope == "active":
            return {"status": active}
        if scope == "done":
            return {"status": {"$in": ["done", "no_answer", "cancelled"]}}
        return {}

    @r.get("/followups/summary")
    async def followups_summary(user=Depends(require_roles(*STAFF))):
        today_iso = local_today().isoformat()
        base = {"assigned_employee_id": user["id"]} if user["role"] == "worker" else {}
        async def count(scope):
            return await db.follow_ups.count_documents({**base, **_fu_scope_query(scope, today_iso)})
        week = (local_today() + timedelta(days=7)).isoformat()
        upcoming = await db.follow_ups.count_documents({
            **base, "status": {"$in": ["planned", "postponed"]},
            "scheduled_at": {"$gt": today_iso, "$lte": week},
        })
        return {"today": await count("today"), "overdue": await count("overdue"),
                "upcoming_7d": upcoming, "date": today_iso}

    @r.get("/followups")
    async def list_followups(scope: str = "active", mine: bool = True, customer_id: str = "",
                             date_from: str = "", date_to: str = "", limit: int = Query(300, ge=1, le=2000),
                             user=Depends(require_roles(*STAFF))):
        today_iso = local_today().isoformat()
        q = _fu_scope_query(scope, today_iso)
        if user["role"] == "worker" or mine:
            if user["role"] == "worker":
                q["assigned_employee_id"] = user["id"]
        if customer_id:
            q["customer_id"] = customer_id
        rng = {}
        if date_from:
            rng["$gte"] = date_from[:10]
        if date_to:
            rng["$lte"] = date_to[:10]
        if rng:
            q["scheduled_at"] = {**(q.get("scheduled_at") if isinstance(q.get("scheduled_at"), dict) else {}), **rng}
        items = await db.follow_ups.find(q, {"_id": 0}).sort([("scheduled_at", 1), ("customer_id", 1)]).to_list(limit)
        for f in items:
            f["is_overdue"] = f["status"] in ("planned", "postponed") and f["scheduled_at"] < today_iso
            f["status_label"] = FOLLOW_UP_STATUS_LABELS.get(f["status"], f["status"])
        return items

    @r.patch("/followups/{fid}")
    async def patch_followup(fid: str, data: FollowUpPatch, user=Depends(require_roles(*STAFF))):
        f = await db.follow_ups.find_one({"id": fid}, {"_id": 0})
        if not f:
            raise HTTPException(404, "Vazifa topilmadi")
        if user["role"] == "worker" and f.get("assigned_employee_id") != user["id"]:
            raise HTTPException(403, "Bu vazifa boshqa xodimga biriktirilgan")
        now = _now_iso()
        patch: dict = {"updated_at": now, "updated_by": user["id"]}
        if data.status is not None:
            if data.status not in FOLLOW_UP_STATUSES:
                raise HTTPException(400, "Status noto'g'ri")
            patch["status"] = data.status
            patch["completed_at"] = now if data.status in ("done", "no_answer", "cancelled") else None
            if data.status == "postponed":
                nd = parse_date(data.next_follow_up_at)
                if not nd:
                    raise HTTPException(400, "Keyinga qoldirish uchun yangi sana kiriting")
                patch["postponed_from"] = f.get("scheduled_at")
                patch["scheduled_at"] = nd.isoformat()
                patch["next_follow_up_at"] = nd.isoformat()
        if data.result_note is not None:
            patch["result_note"] = data.result_note.strip()
        if data.next_follow_up_at is not None and "next_follow_up_at" not in patch:
            nd = parse_date(data.next_follow_up_at)
            patch["next_follow_up_at"] = nd.isoformat() if nd else None
        if data.scheduled_at is not None and "scheduled_at" not in patch:
            nd = parse_date(data.scheduled_at)
            if not nd:
                raise HTTPException(400, "Sana noto'g'ri")
            patch["scheduled_at"] = nd.isoformat()
        await db.follow_ups.update_one({"id": fid}, {"$set": patch})
        out = await db.follow_ups.find_one({"id": fid}, {"_id": 0})
        out["status_label"] = FOLLOW_UP_STATUS_LABELS.get(out["status"], out["status"])
        out["is_overdue"] = out["status"] in ("planned", "postponed") and out["scheduled_at"] < local_today().isoformat()
        return out

    return r


async def ensure_pos_indexes(db):
    await db.customers.create_index("id", unique=True)
    await db.customers.create_index("phone_normalized")
    await db.customers.create_index("full_name_lc")
    await db.pos_sales.create_index("id", unique=True)
    await db.pos_sales.create_index("client_request_id", unique=True)
    await db.pos_sales.create_index([("sale_date", 1), ("daily_number", 1)])
    await db.pos_sales.create_index("customer_id")
    await db.pos_sale_items.create_index("sale_id")
    await db.follow_ups.create_index("id", unique=True)
    await db.follow_ups.create_index([("assigned_employee_id", 1), ("status", 1), ("scheduled_at", 1)])
    await db.follow_ups.create_index("customer_id")
    await db.follow_ups.create_index("sale_id")
    await db.products.create_index("sku")
    await db.products.create_index([("deleted", 1), ("name", 1)])
    await db.products.create_index([("deleted", 1), ("barcode", 1)])
    await db.products.create_index([("deleted", 1), ("sku", 1)])
    await db.products.create_index([("deleted", 1), ("category", 1)])
    await db.products.create_index([("deleted", 1), ("manufacturer", 1)])


async def backfill_units_per_package(db):
    """Mavjud mahsulotlarga qadoqdagi dona sonini nom/tavsifdan bir marta to'ldiradi."""
    cursor = db.products.find({"units_per_package": {"$exists": False}}, {"_id": 0, "id": 1, "name": 1, "description": 1})
    n = 0
    async for p in cursor:
        await db.products.update_one({"id": p["id"]}, {"$set": {"units_per_package": units_from_product(p)}})
        n += 1
    if n:
        logger.info("units_per_package backfilled for %d products", n)
