"""Optional weather for the configured fictional setting."""

from __future__ import annotations

import time
from urllib.parse import quote

import httpx
from data.plugins.mengyu_contact import config

_CACHE = {}
_TTL = 30 * 60


_ZH = {
    "clear": "晴", "sunny": "晴", "overcast": "阴", "cloudy": "多云",
    "partly cloudy": "多云", "mist": "雾", "fog": "雾", "haze": "霾",
    "light rain": "小雨", "moderate rain": "中雨", "heavy rain": "大雨",
    "light drizzle": "毛毛雨", "patchy rain nearby": "局部有雨",
    "rain": "雨", "snow": "雪", "thunder": "雷雨", "thundery outbreaks": "雷雨",
}


def _text(value):
    text = str(value or "").strip()
    low = text.lower()
    for key, zh in _ZH.items():
        if key in low:
            return zh
    return text or "多云"


async def _payload():
    city = config.env_text("MENGYU_WEATHER_CITY")
    if not city:
        return {}
    cached = _CACHE.get(city)
    if cached and time.time() - cached[0] < _TTL:
        return cached[1]
    async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
        response = await client.get(f"https://wttr.in/{quote(city, safe='')}?format=j1&lang=zh")
        response.raise_for_status()
        data = response.json()
    _CACHE[city] = (time.time(), data)
    return data


async def current():
    """Now: {"text","temp","rain"} or {} when unavailable."""
    try:
        data = await _payload()
    except Exception:  # noqa: BLE001 - weather is optional
        return {}
    if not data.get("current_condition"):
        return {}
    now = (data.get("current_condition") or [{}])[0]
    desc = (now.get("weatherDesc") or [{}])[0].get("value")
    try:
        precip = float(now.get("precipMM") or 0)
    except (TypeError, ValueError):
        precip = 0.0
    text = _text(desc)
    return {
        "text": text,
        "temp": now.get("temp_C"),
        "rain": precip > 0 or any(word in text for word in ("雨", "雪", "雷")),
    }


async def forecast(days=2):
    """Per-day forecast: [{"date","text","rain","tmin","tmax"}]."""
    try:
        data = await _payload()
    except Exception:  # noqa: BLE001 - weather is optional
        return []
    result = []
    for day in (data.get("weather") or [])[:days]:
        chance = 0
        desc = ""
        for hour in day.get("hourly") or []:
            try:
                chance = max(chance, int(hour.get("chanceofrain") or 0))
            except (TypeError, ValueError):
                pass
            value = (hour.get("weatherDesc") or [{}])[0].get("value")
            if value and not desc:
                desc = value
        result.append({
            "date": day.get("date"),
            "text": _text(desc),
            "rain": chance,
            "tmin": day.get("mintempC"),
            "tmax": day.get("maxtempC"),
        })
    return result
