"""POS (asosiy sotuv oynasi) uchun sof hisob-kitob mantiqi.

Bu modul bazaga tegmaydi — faqat sonlar va sanalar bilan ishlaydi, shuning
uchun uni alohida unit-test qilish oson. Frontend'dagi `posLogic.js` shu
qoidalarning aynan ko'zgusi; ikkalasi bir xil natija berishi shart.

Asosiy formulalar (texnik topshiriq, 6-7 bo'lim):
    kunlik sarf        = kuniga necha mahal × har mahaldagi dona
    foydalanish muddati = sotilgan jami dona // kunlik sarf   (pastga yaxlitlanadi)
    tugash sanasi      = kurs boshlanish sanasi + muddat

Qayta aloqa bosqichlari (standart, admin sozlamasida o'zgartiriladi):
    1) sotuvdan 3 kun keyin              ("offset")
    2) kurs muddatining 50% qismida      ("percent")
    3) tugashidan 5 kun oldin            ("before_end")
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, List, Optional

# O'zbekiston vaqti — kunlik raqamlash va "bugun" tushunchasi shu zona bo'yicha.
LOCAL_TZ = timezone(timedelta(hours=5))

DEFAULT_FOLLOW_UP_RULES = {
    "stages": [
        {"key": "start", "label": "Boshlanish holati", "type": "offset", "value": 3,
         "purpose": "Qabul qilishni boshlagani va holatini bilish"},
        {"key": "mid", "label": "Oraliq natija", "type": "percent", "value": 50,
         "purpose": "Oraliq natija va foydalanishni tekshirish"},
        {"key": "end", "label": "Qayta buyurtma", "type": "before_end", "value": 5,
         "purpose": "Tugashidan oldin qayta buyurtma yoki qo'shimcha mahsulot taklifi"},
    ],
    # Kurs shundan qisqa bo'lsa sanalar siqiladi va sotuvchiga qo'lda tekshirish taklif etiladi.
    "min_course_days": 10,
}

FOLLOW_UP_STATUSES = ("planned", "done", "no_answer", "postponed", "cancelled")
FOLLOW_UP_STATUS_LABELS = {
    "planned": "Rejalashtirilgan",
    "done": "Bajarildi",
    "no_answer": "Javob bermadi",
    "postponed": "Keyinga qoldirildi",
    "cancelled": "Bekor qilindi",
}


# ---------- Sana yordamchilari ----------
def local_now() -> datetime:
    return datetime.now(LOCAL_TZ)


def local_today() -> date:
    return local_now().date()


def format_sale_code(day: date, seq: int) -> str:
    """Ichki noyob ID: SALE-20260921-001."""
    return f"SALE-{day.strftime('%Y%m%d')}-{seq:03d}"


def parse_date(value: Optional[str], fallback: Optional[date] = None) -> Optional[date]:
    """'YYYY-MM-DD' yoki ISO datetime satrini `date`ga aylantiradi."""
    if not value:
        return fallback
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return fallback


def normalize_phone(phone: str) -> str:
    """Telefon raqamini faqat raqamlardan iborat kanonik ko'rinishga keltiradi.

    +998 90 123-45-67 → 998901234567;  90 123 45 67 → 998901234567
    Bu maydon mijozlar bazasida noyob identifikator vazifasini o'taydi.
    """
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 9:  # mamlakat kodisiz kiritilgan O'zbekiston raqami
        digits = "998" + digits
    return digits


def phone_tail(phone: str, n: int = 4) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return digits[-n:] if digits else ""


# ---------- Qabul tartibi ----------
def units_from_product(product: dict) -> int:
    """Mahsulot nomi/tavsifidan qadoqdagi dona sonini topishga urinadi.

    Seed ma'lumotlarda nom '... КАПС №120 ( NOW )' va tavsif "O'rashda: 120 dona"
    ko'rinishida keladi. Topilmasa 1 qaytariladi — sotuvchi qo'lda kiritadi.
    """
    explicit = product.get("units_per_package")
    try:
        if explicit and int(explicit) > 0:
            return int(explicit)
    except (TypeError, ValueError):
        pass
    for text in (product.get("description") or "", product.get("name") or ""):
        m = re.search(r"O['ʼ’]rashda:\s*(\d+)\s*dona", text, re.IGNORECASE)
        if m:
            return int(m.group(1))
        m = re.search(r"№\s*(\d+)", text)
        if m:
            return int(m.group(1))
    return 1


def compute_daily_usage(times_per_day: int, units_per_intake: int) -> int:
    t = max(int(times_per_day or 0), 0)
    u = max(int(units_per_intake or 0), 0)
    return t * u


def compute_estimated_days(total_units: int, daily_usage: int) -> int:
    """Dori yetadigan TO'LIQ kunlar soni (pastga yaxlitlash). 20 dona / 3 = 6 kun."""
    if daily_usage <= 0 or total_units <= 0:
        return 0
    return int(total_units) // int(daily_usage)


def compute_regimen(*, quantity: int, units_per_package: int, times_per_day: int,
                    units_per_intake: int, course_start: Optional[date] = None,
                    total_units: Optional[int] = None) -> dict:
    """Bitta savat qatori uchun qabul tartibini to'liq hisoblaydi."""
    start = course_start or local_today()
    if total_units is None:
        total_units = max(int(quantity or 0), 0) * max(int(units_per_package or 0), 0)
    daily = compute_daily_usage(times_per_day, units_per_intake)
    days = compute_estimated_days(total_units, daily)
    end = start + timedelta(days=days) if days > 0 else None
    return {
        "total_units": int(total_units),
        "daily_usage": daily,
        "estimated_days": days,
        "course_start_date": start.isoformat(),
        "estimated_end_date": end.isoformat() if end else None,
    }


# ---------- Qayta aloqa jadvali ----------
def _stage_day(stage: dict, days: int) -> Optional[int]:
    """Bosqich qoidasidan kurs ichidagi kun raqamini (1-asosli) chiqaradi."""
    kind = stage.get("type")
    try:
        value = float(stage.get("value", 0))
    except (TypeError, ValueError):
        return None
    if kind == "offset":
        return int(round(value))
    if kind == "percent":
        return int(round(days * value / 100.0))
    if kind == "before_end":
        return days - int(round(value))
    return None


def compute_follow_up_schedule(estimated_days: int, course_start: date,
                               rules: Optional[dict] = None) -> List[dict]:
    """Kurs muddatidan kelib chiqib qayta aloqa sanalarini yaratadi.

    Qoidalar:
      * sanalar kurs ichida bo'lishi shart: 1 ≤ kun ≤ muddat − 1
        (oxirgi aloqa dori tugaydigan kuni emas, undan oldin);
      * bir xil sanaga tushgan bosqichlar birlashtiriladi — takror yaratilmaydi;
      * kurs `min_course_days` dan qisqa bo'lsa sanalar siqiladi va har bir
        vazifa `manual_review=True` bilan qaytadi — sotuvchi tekshirib chiqsin.
    """
    rules = rules or DEFAULT_FOLLOW_UP_RULES
    days = int(estimated_days or 0)
    if days <= 1:
        return []
    min_days = int(rules.get("min_course_days", DEFAULT_FOLLOW_UP_RULES["min_course_days"]))
    short_course = days < min_days

    seen_days: set = set()
    out: List[dict] = []
    for idx, stage in enumerate(rules.get("stages", []), start=1):
        raw = _stage_day(stage, days)
        if raw is None:
            continue
        # Kurs ichiga siqish: eng erta 1-kun, eng kech tugashdan bir kun oldin.
        day = max(1, min(raw, days - 1))
        if day in seen_days:
            continue
        seen_days.add(day)
        out.append({
            "follow_up_number": len(out) + 1,
            "stage_key": stage.get("key", f"stage{idx}"),
            "stage_label": stage.get("label", f"{idx}-aloqa"),
            "purpose": stage.get("purpose", ""),
            "day_offset": day,
            "scheduled_date": (course_start + timedelta(days=day)).isoformat(),
            "manual_review": short_course or raw != day,
        })
    out.sort(key=lambda x: x["day_offset"])
    for i, item in enumerate(out, start=1):
        item["follow_up_number"] = i
    return out


def sanitize_rules(payload: dict) -> dict:
    """Admin yuborgan qoidalarni tekshirib, saqlash uchun toza ko'rinishga keltiradi."""
    stages_in = payload.get("stages") or []
    if not isinstance(stages_in, list) or not stages_in:
        raise ValueError("Kamida bitta qayta aloqa bosqichi bo'lishi kerak")
    if len(stages_in) > 10:
        raise ValueError("Bosqichlar soni 10 tadan oshmasin")
    stages = []
    for i, s in enumerate(stages_in, start=1):
        kind = (s or {}).get("type")
        if kind not in ("offset", "percent", "before_end"):
            raise ValueError(f"{i}-bosqich turi noto'g'ri (offset / percent / before_end)")
        try:
            value = float(s.get("value"))
        except (TypeError, ValueError):
            raise ValueError(f"{i}-bosqich qiymati son bo'lishi kerak")
        if value < 0 or (kind == "percent" and value > 100):
            raise ValueError(f"{i}-bosqich qiymati noto'g'ri")
        stages.append({
            "key": str(s.get("key") or f"stage{i}")[:32],
            "label": str(s.get("label") or f"{i}-aloqa")[:80],
            "type": kind,
            "value": int(value) if value.is_integer() else value,
            "purpose": str(s.get("purpose") or "")[:200],
        })
    try:
        min_days = int(payload.get("min_course_days", DEFAULT_FOLLOW_UP_RULES["min_course_days"]))
    except (TypeError, ValueError):
        raise ValueError("min_course_days butun son bo'lishi kerak")
    if min_days < 2 or min_days > 365:
        raise ValueError("min_course_days 2 dan 365 gacha bo'lishi kerak")
    return {"stages": stages, "min_course_days": min_days}


def money(x) -> float:
    return round(float(x or 0), 2)


def line_totals(unit_price: float, quantity: int, base_discount: float, extra_discount: float) -> dict:
    """Qator summasi: mahsulot chegirmasi + sotuvchi qo'shimcha chegirmasi (foiz)."""
    total_disc = min(max(float(base_discount or 0), 0) + max(float(extra_discount or 0), 0), 100)
    original = money(unit_price)
    final_unit = money(original * (100 - total_disc) / 100) if total_disc > 0 else original
    qty = int(quantity)
    return {
        "discount_percent": total_disc,
        "unit_price": final_unit,
        "original_price": original,
        "line_total": money(final_unit * qty),
        "discount_total": money((original - final_unit) * qty),
    }
