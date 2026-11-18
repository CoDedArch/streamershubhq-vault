"""Unit tests for the business rules in section 7, and the Holder ID format."""

import re
from types import SimpleNamespace

import pytest

from app.services import AuthService
from app.services.ClipService import fixed_pay
from app.services.InvestorService import pct_of
from app.services.money import ghs, next_month, prev_month
from app.services.PayrollService import pick_tier
from app.services.StoreService import commission_for, unit_economics


def tier(label, lo, hi, pay):
    return SimpleNamespace(label=label, min_platform_income_minor=lo, max_platform_income_minor=hi, salary_minor=pay)


TIERS = [tier("Base", 0, 99999, 300000), tier("Tier 2", 100000, 499999, 350000), tier("Tier 3", 500000, 999999, 450000),
         tier("Tier 4", 1000000, None, None)]


@pytest.mark.parametrize("income,label,salary", [
    (0, "Base", 300000), (99999, "Base", 300000), (100000, "Tier 2", 350000), (499999, "Tier 2", 350000),
    (500000, "Tier 3", 450000), (999999, "Tier 3", 450000), (1000000, "Tier 4", None), (50_000_000, "Tier 4", None)])
def test_salary_tier_boundaries(income, label, salary):
    t = pick_tier(TIERS, income)
    assert t.label == label and t.salary_minor == salary


def test_commission_is_price_times_qty_times_rate():
    assert commission_for(14000, 1, 1500) == 2100        # GHS 140 cap, 15% -> GHS 21
    assert commission_for(14000, 3, 1500) == 6300
    assert commission_for(19000, 1, 1500) == 2850        # GHS 28.50, exact to the pesewa
    assert commission_for(19000, 1, 0) == 0              # in-house: no commission
    assert commission_for(333, 1, 1500) == 50            # rounds half up: 49.95 -> 50


def test_clipper_fixed_rate_and_cap():
    assert fixed_pay(212_400, 300, None) == 63_720       # 212.4K views at GHS 3 per 1,000 -> GHS 637.20
    assert fixed_pay(999, 300, None) == 300              # 2.997 -> 3.00, half up in pesewas
    assert fixed_pay(1_000_000, 300, 50_000) == 50_000   # capped at GHS 500
    assert fixed_pay(0, 300, None) == 0


def test_stake_value_and_rounding():
    assert pct_of(36_000_000, 1.5) == 540_000            # 1.50% of GHS 360,000 = GHS 5,400
    assert pct_of(33_300_000, 1.5) == 499_500


def test_unit_economics_adds_up():
    split = unit_economics(100_000, 30_000, 15_000, 1_950)
    assert sum(p["minor"] for p in split["parts"]) == 100_000
    assert split["parts"][-1] == {"label": "Net to Streamers Hub", "minor": 53_050, "percent": 53}


def test_holder_id_format_and_alphabet():
    seen = set()
    for _ in range(300):
        hid = AuthService.new_holder_id()
        assert re.fullmatch(r"[23456789A-HJ-NP-Z]{4}-[23456789A-HJ-NP-Z]{4}-[23456789A-HJ-NP-Z]{4}", hid)
        assert not set(hid) & set("01OI")
        seen.add(hid)
    assert len(seen) == 300
    assert len(AuthService.HOLDER_ALPHABET) == 32  # 12 characters x 5 bits = 60 bits


def test_holder_id_hash_verifies_and_is_not_reversible():
    hid = AuthService.new_holder_id()
    hashed = AuthService.hash_holder_id(hid)
    assert hashed.startswith("$argon2id$") and hid not in hashed and hid.replace("-", "") not in hashed
    assert AuthService.holder_id_matches(hid, hashed)
    assert AuthService.holder_id_matches(hid.lower().replace("-", " "), hashed)  # typed loosely still works
    assert not AuthService.holder_id_matches(hid[:-1] + ("2" if hid[-1] != "2" else "3"), hashed)
    assert not AuthService.holder_id_matches(hid, None)


def test_money_formatting_and_months():
    assert ghs(641910) == "GHS 6,419.10" and ghs(642000) == "GHS 6,420" and ghs(-109000) == "-GHS 1,090"
    assert prev_month("2027-01") == "2026-12" and next_month("2026-12") == "2027-01"
