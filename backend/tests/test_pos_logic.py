"""POS hisob-kitob mantiqi uchun unit-testlar (baza va server talab qilinmaydi).

Texnik topshiriqning 12-bo'limidagi qabul qilish mezonlarini tekshiradi:
  #7  90 tabletka, kuniga 3 mahal, 1 tadan → 30 kun
  #8  shu misolda 3-, 15- va 25-kunlarga vazifalar
  #9  bir nechta dorining tugash sanalari alohida
"""
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pos_logic import (  # noqa: E402
    DEFAULT_FOLLOW_UP_RULES,
    compute_daily_usage,
    compute_estimated_days,
    compute_follow_up_schedule,
    compute_regimen,
    format_sale_code,
    line_totals,
    normalize_phone,
    phone_tail,
    sanitize_rules,
    units_from_product,
)

SALE_DAY = date(2026, 9, 21)


class TestRegimen:
    def test_daily_usage(self):
        assert compute_daily_usage(3, 1) == 3
        assert compute_daily_usage(2, 2) == 4
        assert compute_daily_usage(0, 5) == 0

    def test_spec_example_90_tablets_30_days(self):
        reg = compute_regimen(quantity=1, units_per_package=90, times_per_day=3,
                              units_per_intake=1, course_start=SALE_DAY)
        assert reg["total_units"] == 90
        assert reg["daily_usage"] == 3
        assert reg["estimated_days"] == 30
        assert reg["estimated_end_date"] == "2026-10-21"

    def test_rounds_down_to_full_days(self):
        # 20 dona, kuniga 3 dona = 6 to'liq kun (6.67 emas)
        assert compute_estimated_days(20, 3) == 6

    def test_zero_usage_gives_zero_days(self):
        assert compute_estimated_days(90, 0) == 0
        reg = compute_regimen(quantity=1, units_per_package=90, times_per_day=0,
                              units_per_intake=0, course_start=SALE_DAY)
        assert reg["estimated_days"] == 0 and reg["estimated_end_date"] is None

    def test_quantity_multiplies_packages(self):
        reg = compute_regimen(quantity=2, units_per_package=60, times_per_day=2,
                              units_per_intake=1, course_start=SALE_DAY)
        assert reg["total_units"] == 120 and reg["estimated_days"] == 60

    def test_explicit_total_units_overrides(self):
        reg = compute_regimen(quantity=1, units_per_package=90, times_per_day=1,
                              units_per_intake=1, course_start=SALE_DAY, total_units=45)
        assert reg["estimated_days"] == 45


class TestFollowUpSchedule:
    def test_spec_example_days_3_15_25(self):
        sched = compute_follow_up_schedule(30, SALE_DAY)
        assert [s["day_offset"] for s in sched] == [3, 15, 25]
        assert [s["scheduled_date"] for s in sched] == ["2026-09-24", "2026-10-06", "2026-10-16"]
        assert [s["follow_up_number"] for s in sched] == [1, 2, 3]
        assert all(not s["manual_review"] for s in sched)

    def test_last_contact_is_before_end_not_on_end(self):
        sched = compute_follow_up_schedule(30, SALE_DAY)
        assert sched[-1]["day_offset"] == 25 < 30

    def test_short_course_no_duplicates_no_past(self):
        # 6 kunlik kurs: 3 (start), 3 (50%), 1 (6-5) → [1, 3], takror yo'q
        sched = compute_follow_up_schedule(6, SALE_DAY)
        days = [s["day_offset"] for s in sched]
        assert days == sorted(set(days))
        assert all(1 <= d < 6 for d in days)
        assert all(s["manual_review"] for s in sched)

    def test_two_day_course_single_contact(self):
        sched = compute_follow_up_schedule(2, SALE_DAY)
        assert [s["day_offset"] for s in sched] == [1]

    def test_one_day_or_zero_course_creates_nothing(self):
        assert compute_follow_up_schedule(1, SALE_DAY) == []
        assert compute_follow_up_schedule(0, SALE_DAY) == []

    def test_each_product_has_own_end_date(self):
        a = compute_regimen(quantity=1, units_per_package=90, times_per_day=3, units_per_intake=1, course_start=SALE_DAY)
        b = compute_regimen(quantity=1, units_per_package=60, times_per_day=1, units_per_intake=1, course_start=SALE_DAY)
        assert a["estimated_end_date"] == "2026-10-21"
        assert b["estimated_end_date"] == "2026-11-20"
        assert compute_follow_up_schedule(a["estimated_days"], SALE_DAY) != compute_follow_up_schedule(b["estimated_days"], SALE_DAY)

    def test_custom_rules_from_admin(self):
        rules = {"stages": [
            {"key": "a", "label": "A", "type": "offset", "value": 2},
            {"key": "b", "label": "B", "type": "before_end", "value": 3},
        ], "min_course_days": 5}
        sched = compute_follow_up_schedule(20, SALE_DAY, rules)
        assert [s["day_offset"] for s in sched] == [2, 17]

    def test_long_course_percent_rounding(self):
        sched = compute_follow_up_schedule(45, SALE_DAY)
        assert [s["day_offset"] for s in sched] == [3, 22, 40]


class TestRulesSanitize:
    def test_default_rules_are_valid(self):
        out = sanitize_rules(DEFAULT_FOLLOW_UP_RULES)
        assert len(out["stages"]) == 3 and out["min_course_days"] == 10

    @pytest.mark.parametrize("bad", [
        {"stages": [], "min_course_days": 10},
        {"stages": [{"type": "weird", "value": 1}], "min_course_days": 10},
        {"stages": [{"type": "percent", "value": 150}], "min_course_days": 10},
        {"stages": [{"type": "offset", "value": "x"}], "min_course_days": 10},
        {"stages": [{"type": "offset", "value": 3}], "min_course_days": 1},
    ])
    def test_invalid_rules_raise(self, bad):
        with pytest.raises(ValueError):
            sanitize_rules(bad)


class TestMisc:
    def test_sale_code_format(self):
        assert format_sale_code(SALE_DAY, 1) == "SALE-20260921-001"
        assert format_sale_code(SALE_DAY, 20) == "SALE-20260921-020"

    def test_phone_normalization_and_tail(self):
        assert normalize_phone("+998 (90) 123-45-67") == "998901234567"
        assert normalize_phone("90 123 45 67") == "998901234567"
        assert phone_tail("+998901234567") == "4567"
        assert phone_tail("") == ""

    def test_units_from_product_name_and_description(self):
        assert units_from_product({"name": "5-HTP 100МГ КАПС №120 ( NOW )", "description": ""}) == 120
        assert units_from_product({"name": "X", "description": "O'rashda: 60 dona."}) == 60
        assert units_from_product({"name": "X", "description": "", "units_per_package": 30}) == 30
        assert units_from_product({"name": "Krem", "description": ""}) == 1

    def test_line_totals_with_discounts(self):
        t = line_totals(100000, 2, 10, 5)
        assert t["discount_percent"] == 15
        assert t["unit_price"] == 85000
        assert t["line_total"] == 170000
        assert t["discount_total"] == 30000

    def test_line_totals_caps_discount_at_100(self):
        t = line_totals(1000, 1, 80, 50)
        assert t["unit_price"] == 0 and t["discount_percent"] == 100


# ---------- Dona / ml ----------
from pos_logic import package_from_product  # noqa: E402


@pytest.mark.parametrize("name, expected", [
    ("АЛЛЕРТ-Г КИДС 150МЛ СИРОП №1", ("ml", 150.0)),
    ("L-КАРНИТИН 3000МГ 500МЛ", ("ml", 500.0)),
    ("СИРОП 2,5ML", ("ml", 2.5)),
    ("5-HTP 100МГ КАПС №120 ( NOW )", ("dona", 120)),
    ("АМПУЛА 2МЛ №10", ("dona", 10)),          # ampulalar — dona
    ("ВИТАМИН Д3 1МЛН ТАБ №60", ("dona", 60)),  # "МЛН" ml emas
])
def test_package_unit_detection(name, expected):
    assert package_from_product({"name": name}) == expected


def test_regimen_in_ml_with_fractional_dose():
    # 150 ml sirop, kuniga 3 mahal 5 ml -> 15 ml/kun -> 10 kun
    r = compute_regimen(quantity=1, units_per_package=150, times_per_day=3,
                        units_per_intake=5, course_start=date(2026, 10, 1))
    assert (r["total_units"], r["daily_usage"], r["estimated_days"]) == (150, 15, 10)
    assert r["estimated_end_date"] == "2026-10-11"
    # 2 shisha × 100 ml, kuniga 2 mahal 2.5 ml -> 5 ml/kun -> 40 kun
    r = compute_regimen(quantity=2, units_per_package=100, times_per_day=2, units_per_intake=2.5)
    assert (r["total_units"], r["daily_usage"], r["estimated_days"]) == (200, 5, 40)
    # 0.1 qadamli dozada suzuvchi nuqta xatosi kunni kamaytirmasin
    r = compute_regimen(quantity=1, units_per_package=3, times_per_day=1, units_per_intake=0.3)
    assert r["estimated_days"] == 10
