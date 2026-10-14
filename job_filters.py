"""Configurable, deterministic checks before an automatic BOSS contact."""

import re
import unicodedata


_NUMBER = r"(\d+(?:\.\d+)?)"


def minimum_monthly_salary_k(salary):
    """Return the displayed monthly salary lower bound in thousands of yuan.

    Unknown, negotiable, daily and hourly rates return None. Extra monthly
    payments (13薪 etc.) do not raise the monthly lower bound.
    """
    if not salary:
        return None
    text = unicodedata.normalize("NFKC", str(salary)).upper()
    text = re.sub(r"\s+", "", text).replace("—", "-").replace("–", "-")
    text = text.replace("～", "-").replace("~", "-").replace("至", "-")
    if ("面议" in text or "年薪" in text or
            re.search(r"/(?:年|周|天|日|小时)", text)):
        return None

    patterns = (
        (rf"{_NUMBER}-(?:\d+(?:\.\d+)?)K", 1),
        (rf"{_NUMBER}K", 1),
        (rf"{_NUMBER}万-(?:\d+(?:\.\d+)?)万", 10),
        (rf"{_NUMBER}-(?:\d+(?:\.\d+)?)万", 10),
        (rf"{_NUMBER}万", 10),
        (rf"{_NUMBER}-(?:\d+(?:\.\d+)?)千", 1),
        (rf"{_NUMBER}千", 1),
        (rf"{_NUMBER}-(?:\d+(?:\.\d+)?)元/月", 0.001),
        (rf"{_NUMBER}元/月", 0.001),
    )
    for pattern, multiplier in patterns:
        match = re.search(pattern, text)
        if match:
            return float(match.group(1)) * multiplier
    return None


def salary_floor_for_city(floors, actual_city, search_city=""):
    """Find a configured floor for a job's city without built-in city values."""
    if not isinstance(floors, dict):
        return None
    city = str(actual_city or "").strip()
    if not city:
        city = str(search_city or "").strip()
    if city == "不限":
        return None
    for name in sorted(floors, key=len, reverse=True):
        key = str(name).strip().removesuffix("市")
        if key and (city == key or city.startswith(key + "市") or
                    city.startswith(key + "·") or city.startswith(key)):
            return name, float(floors[name])
    return None


def salary_range_for_city(ranges, actual_city, search_city=""):
    """Return the configured monthly K range for the exact searched city."""
    city = str(actual_city or search_city or "").strip().removesuffix("市")
    if not city or city == "不限" or not isinstance(ranges, list):
        return None
    for row in ranges:
        if not isinstance(row, dict):
            continue
        name = str(row.get("city") or "").strip().removesuffix("市")
        if name and city.startswith(name):
            return name, float(row["min_k"]), (float(row["max_k"])
                         if row.get("max_k") not in (None, "") else None)
    return None
