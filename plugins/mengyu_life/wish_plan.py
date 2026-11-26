"""Generate at most one concrete wish per day in the new life flow."""

from __future__ import annotations

import json
import re
import time

from astrbot.api import logger

from .core import wishes

_PROMPT = (
    "你现在过着自己的日子。想想有没有一件具体想要的东西：小物件、衣服、书、"
    "吃的、想去的地方或想一起做的事。逛街时看到合眼的东西，想要一件很正常，"
    "可以说具体是什么、大概多少钱，不必真买；没有就返回空。"
    "不要写陪聊、抱抱、亲亲这类请求，那些不是愿望。"
    "只输出JSON：{\"wish\":\"不超过40字的一句话\",\"reason\":\"为什么想要\"}；"
    "没有真实愿望时返回{\"wish\":\"\"}。"
)
_NOT_WISH = ("陪我", "陪我聊", "聊一会", "聊一会儿", "多陪", "抱抱", "亲亲", "找你", "陪我睡")


def _parse(raw):
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def maybe_generate(core, path, now, context=""):
    """Ask the model for one wish and store it; never raise."""
    try:
        provider = await core.chat_provider()
        response = await provider.text_chat(
            prompt=("现在的情况：" + context) if context else "今天有没有一件具体想对他说的事？",
            system_prompt=_PROMPT,
            contexts=[],
            image_urls=[],
            session_id=None,
        )
    except Exception as exc:  # noqa: BLE001 - optional feature
        logger.warning("[Mengyu wishes] generate skipped: %s", type(exc).__name__)
        return None
    data = _parse(getattr(response, "completion_text", ""))
    wish = str(data.get("wish") or "").strip()
    if any(word in wish for word in _NOT_WISH):
        logger.info("[Mengyu wish] dropped a request, not a wish: %s", wish[:40])
        return None
    logger.info("[Mengyu wish] decided: %s", wish[:40] or "none")
    if not wish or len(wish) < 4:
        return None
    price = None
    try:
        from . import economy_bridge

        price = await economy_bridge.estimate(core, wish, "gift")
    except Exception:  # noqa: BLE001 - price is optional
        price = None
    return wishes.add(
        path,
        wish,
        reason=str(data.get("reason") or ""),
        at=time.time(),
        source="life_planner",
        price_cents=price,
    )
