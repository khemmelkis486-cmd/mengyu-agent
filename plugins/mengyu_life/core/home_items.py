"""What she has at home: consumables, places and shortages."""

from __future__ import annotations

from data.plugins.mengyu_contact import config

import json
import time
from pathlib import Path

PATH = config.path("home.json", plugin="mengyu_life")

DEFAULTS = {
    "纸巾": ("柜子", 3),
    "洗衣液": ("阳台", 2),
    "洗发水": ("浴室", 2),
    "米": ("厨房", 2),
    "鸡蛋": ("冰箱", 10),
    "牛奶": ("冰箱", 2),
    "垃圾袋": ("厨房", 2),
    "酱油": ("厨房", 1),
}
LOW = 0


def _blank():
    return {
        "items": [
            {"name": name, "place": place, "qty": qty, "updated_at": time.time()}
            for name, (place, qty) in DEFAULTS.items()
        ]
    }


def load(path=PATH):
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = _blank()
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        data = _blank()
    return data


def save(data, path=PATH):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def add(name, qty=1, place=None, path=PATH):
    """Put something in the house after a purchase."""
    name = str(name or "").strip()
    if not name:
        return None
    data = load(path)
    for item in data["items"]:
        if item.get("name") == name:
            item["qty"] = int(item.get("qty") or 0) + int(qty)
            item["place"] = place or item.get("place") or "家里"
            item["updated_at"] = time.time()
            save(data, path)
            return item
    item = {"name": name, "place": place or "家里", "qty": int(qty),
            "updated_at": time.time()}
    data["items"].append(item)
    save(data, path)
    return item


def consume(name, qty=1, path=PATH):
    """Use something up; returns the updated item."""
    data = load(path)
    for item in data["items"]:
        if item.get("name") == name:
            item["qty"] = max(0, int(item.get("qty") or 0) - int(qty))
            item["updated_at"] = time.time()
            save(data, path)
            return item
    return None


def shortages(path=PATH):
    """Items that are out or nearly out."""
    return [
        item for item in load(path)["items"]
        if int(item.get("qty") or 0) <= LOW
    ]


def context(path=PATH):
    """Short view for chat and planning."""
    data = load(path)
    fridge = [item["name"] for item in data["items"] if item.get("place") == "冰箱"]
    low = [item["name"] for item in shortages(path)]
    return {"冰箱": fridge, "快没了": low}
