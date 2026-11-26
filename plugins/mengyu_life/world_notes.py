"""Concrete places, shows and games for today's plan, from shared web search.

The plan generator stays deterministic; this module turns one search pass into
a short, concrete list so outings, meals, shows and games are specific instead
of "附近的餐馆" / "看两集剧".
"""

from __future__ import annotations

from data.plugins.mengyu_contact import config

import json
import re
import time
from pathlib import Path

PATH = config.path("world_notes.json", plugin="mengyu_life")

QUERIES = (
    "最近 热播电视剧 推荐",
    "最近 热门 单机游戏 推荐",
    "最近 值得读的书 推荐",
)

_FALLBACK_PLACES = (
    "社区书店", "河边步道", "附近的公园", "社区美术馆", "街角咖啡馆",
)
_FALLBACK_FOODS = ("家常面馆", "蒸饺", "砂锅饭", "时蔬汤")
_FALLBACK_SHOWS = ("《开端》", "《漫长的季节》", "《繁花》")
_FALLBACK_GAMES = ("《星露谷物语》", "《塞尔达传说》", "《动物森友会》")
_FALLBACK_BOOKS = ("《夜晚的潜水艇》", "《长安的荔枝》", "《人间草木》")

_SCHEMA_HINT = (
    "只输出JSON，不要解释。格式："
    '{"places":["具体店名或街区"],"foods":["具体店名或菜"],'
    '"shows":[{"name":"剧名","detail":"这集大概讲什么"}],'
    '"games":["具体游戏名"],"books":["具体书名"]}。'
    "只保留搜索结果里明确出现过的名字；不够就少给，不要编。"
)


def _today():
    return time.strftime("%Y-%m-%d", time.localtime())


def _raw_load():
    """Last saved payload regardless of day, for reuse when search fails."""
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load(day=None):
    """Notes for the given day (default today), or an empty dict."""
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if data.get("day") == (day or _today()) else {}


def titles():
    data = load()
    return [str(item) for item in (data.get("places") or []) if item]


def _extract_json(raw):
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except ValueError:
        return None


def _names(values, limit=5):
    out = []
    for item in values or []:
        if isinstance(item, dict):
            item = item.get("name")
        name = str(item or "").strip()
        if 1 <= len(name) <= 24 and name not in out:
            out.append(name)
    return out[:limit]


def _shows(values, limit=3):
    out = []
    for item in values or []:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            detail = str(item.get("detail") or "").strip()
        else:
            name, detail = str(item or "").strip(), ""
        if 1 <= len(name) <= 24 and all(item["name"] != name for item in out):
            out.append({"name": name, "detail": detail[:120]})
    return out[:limit]


async def _ask_model(core, rows):
    provider = await core.chat_provider()
    material = "\n".join(
        f"- {row.get('title')}：{str(row.get('snippet'))[:400]}" for row in rows
    )[:5000]
    response = await provider.text_chat(
        prompt="下面是联网检索结果，请从中挑出今天可以用的具体地点、吃的、剧、游戏和书。\n"
        + material,
        system_prompt=_SCHEMA_HINT,
        contexts=[],
        image_urls=[],
        session_id=None,
    )
    data = _extract_json(getattr(response, "completion_text", ""))
    if not isinstance(data, dict):
        return {}
    return {
        "places": _names(data.get("places")),
        "foods": _names(data.get("foods")),
        "shows": _shows(data.get("shows")),
        "games": _names(data.get("games"), 3),
        "books": _names(data.get("books"), 3),
    }


def remember_foods(names):
    """Remember recently used restaurants so tomorrow avoids them."""
    data = _raw_load()
    recent = list(data.get("recent_foods") or [])
    for name in names:
        if name and name not in recent:
            recent.append(name)
    data["recent_foods"] = recent[-6:]
    try:
        PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


async def refresh(core=None, day=None):
    """Fetch and condense today's concrete material; safe to call once a day."""
    from data.plugins.mengyu_web.search_service import search_web

    city = config.env_text("MENGYU_WEATHER_CITY")
    local_queries = (f"{city} 适合散步的公共场所", f"{city} 家常餐馆推荐") if city else ()
    rows = []
    for query in (*local_queries, *QUERIES):
        try:
            rows.extend(await search_web(query, 2))
        except Exception:  # noqa: BLE001 - one query may fail
            continue

    data = {}
    if core is not None and rows:
        try:
            data = await _ask_model(core, rows)
        except Exception:  # noqa: BLE001 - fall back to fixed pools
            data = {}

    previous = _raw_load()
    places = data.get("places") or previous.get("places") or list(_FALLBACK_PLACES)
    payload = {
        "day": day or _today(),
        "places": places[:5],
        "foods": (data.get("foods") or previous.get("foods") or list(_FALLBACK_FOODS))[:5],
        "shows": data.get("shows") or previous.get("shows")
        or [{"name": name, "detail": ""} for name in _FALLBACK_SHOWS],
        "games": data.get("games") or previous.get("games") or list(_FALLBACK_GAMES),
        "books": data.get("books") or previous.get("books") or list(_FALLBACK_BOOKS),
        "at": time.time(),
        "sources": sum(1 for row in rows if row.get("url")),
        "recent_foods": list(previous.get("recent_foods") or []),
    }
    try:
        from datetime import datetime as _dt

        age = (
            _dt.strptime(payload["day"], "%Y-%m-%d")
            - _dt.strptime(str(previous.get("day") or ""), "%Y-%m-%d")
        ).days
    except (ValueError, TypeError):
        age = 99
    if 0 <= age <= 7:
        # Keep the same show, game and book for about a week.
        for key in ("shows", "games", "books"):
            if previous.get(key):
                payload[key] = previous[key]
    try:
        from data.plugins.mengyu_web.weather import forecast as weather_forecast

        target = payload["day"]
        for item in await weather_forecast(3):
            if item.get("date") == target:
                payload["weather"] = item
                break
    except Exception:  # noqa: BLE001 - weather is optional
        pass
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload
