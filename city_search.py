"""Turn dashboard city names into BOSS city codes without silent broad searches."""

import json
import re
from functools import lru_cache
from urllib.request import Request, urlopen


CITY_DATA_URL = "https://www.zhipin.com/wapi/zpgeek/common/data/city/site.json"

# Verified against BOSS's city data. These keep the user's three cities usable
# if the optional live city lookup is temporarily unavailable.
KNOWN_CITIES = {
    "北京": "101010100",
    "石家庄": "101090100",
    "天津": "101030100",
}


def _canonical(name):
    name = str(name or "").strip()
    return name[:-1] if name.endswith("市") else name


@lru_cache(maxsize=1)
def _site_cities():
    request = Request(CITY_DATA_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=8) as response:
        data = json.load(response)
    found = {}

    def visit(value):
        if isinstance(value, dict):
            name, code, url = value.get("name"), value.get("code"), value.get("url")
            if name and code and url:
                found[_canonical(name)] = str(code)
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(data.get("zpData", {}))
    return found


def resolve_city_targets(raw):
    """Return ordered (city name, BOSS code) pairs; empty means nationwide."""
    names = [_canonical(part) for part in re.split(r"[,，、;；\s]+", str(raw or ""))]
    names = list(dict.fromkeys(name for name in names if name))
    if not names or names == ["不限"] or names == ["全国"]:
        return [("不限", "")]
    if any(name in ("不限", "全国") for name in names):
        raise ValueError("全国与具体城市不能同时填写")

    codes = dict(KNOWN_CITIES)
    if any(name not in codes for name in names):
        try:
            codes.update(_site_cities())
        except (OSError, ValueError):
            pass
    targets = []
    for name in names:
        if name not in codes:
            raise ValueError(f"无法识别城市「{name}」，请填写 BOSS 城市名称并重新保存")
        targets.append((name, codes[name]))
    return targets


def page_allocations(total_pages, city_count):
    """Treat the dashboard page count as a total budget across all cities."""
    total = int(total_pages)
    if total < city_count:
        raise ValueError("搜索总页数不能少于城市数量")
    base, extra = divmod(total, city_count)
    return [base + (index < extra) for index in range(city_count)]
