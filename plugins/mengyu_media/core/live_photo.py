"""Visual constraints for fictional scenes based on time and optional weather."""

import time
from datetime import datetime

from data.plugins.mengyu_contact.local_clock import TZ
from data.plugins.mengyu_web.weather import current


async def photo_context(now=None):
    """Return timestamped weather metadata and guidance for scene lighting."""
    now = time.time() if now is None else now
    moment = datetime.fromtimestamp(now, TZ)
    weather = await current()
    if 6 <= moment.hour < 18:
        light = "白天，按天气安排自然光"
    else:
        light = "夜间，室外光线暗，室内以灯光照明"
    if weather:
        conditions = f"区域天气为{weather['text']}，气温约{weather['temp']}℃"
    else:
        conditions = "天气未知，避免明显的窗外晴雨细节"
    return {
        "captured_at": now,
        "local_time": moment.isoformat(),
        "weather": weather,
        "weather_known": bool(weather),
        "guidance": f"\n画面依据模拟场景的当地时间和天气。{light}。{conditions}。",
    }
