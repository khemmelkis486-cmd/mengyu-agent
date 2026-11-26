"""Meal delivery and reply reconciliation used by the life service."""

import asyncio
import json
import re
import time
from datetime import datetime

from astrbot.api import logger
from data.plugins.mengyu_contact.local_clock import TZ

from .core.meal_preferences import record_eaten as record_preferred_meal
from .core.meal_preferences import record_from_text as record_meal_from_text
from .core.meals import RULE as MEAL_RULE
from .core.meals import (
    meal_claims,
    meal_context,
    meal_regressions,
    meal_reopening,
    record_meal_delivery,
)


def record_delivered_meal(core, text, now, delivery_id):
    """Synchronize confirmed meal speech and the finite current activity.

    Args:
        text: Text or voice transcript already delivered successfully.
        now: Original delivery timestamp, never the replay time.
        delivery_id: Confirmed delivery identity.

    Returns:
        Whether meal component progress changed.
    """
    orders = core.meal_orders()
    before_context = meal_context(core.state, orders, now)
    claims = meal_claims(before_context, text)
    changed = record_meal_delivery(core.state, orders, text, now, delivery_id)
    meal_history_changed = record_meal_from_text(
        core.meal_preferences_path, text, now, delivery_id
    )
    before_by_id = {item["order_id"]: item for item in before_context}
    for claim in claims:
        if claim.get("phase") == "finished":
            view = before_by_id.get(claim.get("order_id"))
            if view:
                meal_history_changed = (
                    record_preferred_meal(
                        core.meal_preferences_path,
                        view.get("item") or claim.get("name"),
                        now,
                        source="confirmed-takeout-meal",
                        source_id=delivery_id,
                    )
                    or meal_history_changed
                )
    if not changed:
        return meal_history_changed
    segments = core.activities.data["virtual_life"]
    for view in meal_context(core.state, orders, now):
        progress = core.state.get("meal_progress", {}).get(view["order_id"], {})
        starts = [
            p.get("started_at", p.get("at", now))
            for p in progress.values()
            if p.get("phase") in ("eating", "finished")
        ]
        if not starts:
            continue
        started = min(starts)
        identity = "meal:" + view["order_id"]
        existing = next((seg for seg in segments if seg["id"] == identity), None)
        ending = (view.get("meal_end") or {}).get("at")
        if existing:
            if ending and ending < existing["phases"][-1]["at"]:
                core.activities.adjust_phase_end(identity, max(started, ending))
                core.state["next_life_check"] = min(
                    core.state.get("next_life_check", now), now
                )
            continue
        # A historical replay must not replace a more recent activity.
        if any(seg["created_at"] > started for seg in segments):
            continue
        previous = core.activities.virtual_context(started)
        if previous and not previous["completed"]:
            core.activities.cancel(previous["id"], started, "confirmed_meal_started")
        place = (previous.get("current") or {}).get("place", "住处")
        segments.append(
            {
                "id": identity,
                "created_at": started,
                "day": datetime.fromtimestamp(started, TZ).date().isoformat(),
                "mode": previous.get("mode", "home"),
                "destination": previous.get("destination", place),
                "clothes": previous.get("clothes", "日常便装"),
                "reason": "开始吃" + str(view["item"]),
                "detail": "本次用餐约半小时后结束，剩余食物不是待办",
                "source": "confirmed-meal-delivery",
                "delivery_id": delivery_id,
                "phases": [
                    {"at": started, "stage": "正在做", "place": place},
                    {
                        "at": min(started + 1800, ending) if ending else started + 1800,
                        "stage": "本次用餐结束",
                        "place": place,
                    },
                ],
            }
        )
        core.activities.replace_virtual_life(segments)
        mood = core.state.get("daily_mood") or {}
        if mood.get("at", 0) <= started:
            mood = dict(
                mood,
                at=started,
                reason="先放下手头的事，开始用餐",
                carryover="",
                source="confirmed-meal-delivery",
            )
            core.state["daily_mood"] = mood
        if (core.state.get("own_choice") or {}).get("at", 0) <= started:
            core.state["own_choice"] = {
                "wish": "先吃" + str(view["item"]) + "，之后再决定做什么",
                "kind": "continue",
                "at": started,
                "source": "confirmed-meal-delivery",
            }
        core.state["next_life_check"] = min(
            core.state.get("next_life_check", started + 1800), started + 1800
        )
    return changed or meal_history_changed


async def repair_meal_reply(core, text, user_text=""):
    """Repair one conflicting meal tail without inventing new consumption.

    Args:
        text: Proposed public assistant message.
        user_text: Current user's message, treated only as context.

    Returns:
        A repaired reply or unaffected original clauses as a fallback.
    """
    now = time.time()
    orders = core.meal_orders()
    conflicts = meal_regressions(core.state, orders, text, now)
    if not conflicts:
        return text
    context = core.takeout_context(now)
    allowed = {
        (c["order_id"], c["name"], c["phase"])
        for c in meal_claims(context, text)
        if c not in conflicts
    }
    try:
        provider = await core.chat_provider()
        if provider:
            response = await asyncio.wait_for(
                provider.text_chat(
                    system_prompt="你只修正回复中的外卖时间线冲突，不重新扮演人物或新增事件。"
                    + MEAL_RULE
                    + "保留对用户新消息的正常回应和原语气；删改矛盾分句即可。未确认吃的食物不能补成吃完，"
                    "不编纠正声明，不暴露后台规则，不额外播报饭菜。只输出修正后的短正文，不输出JSON或私有标记。",
                    prompt=json.dumps(
                        {
                            "用户新消息": user_text,
                            "待修正正文": text,
                            "外卖当前进度": context,
                            "冲突": conflicts,
                        },
                        ensure_ascii=False,
                    ),
                    contexts=[],
                    image_urls=[],
                    session_id=None,
                ),
                30,
            )
            candidate = (response.completion_text or "").strip()
            new_claims = meal_claims(context, candidate)
            invented = any(
                c["correction"]
                or (
                    c["phase"] in ("eating", "finished")
                    and c["phase"] != c["previous"]
                    and (c["order_id"], c["name"], c["phase"]) not in allowed
                )
                for c in new_claims
            )
            if (
                candidate
                and len(candidate) <= max(400, len(text) + 80)
                and "[[MENGYU_" not in candidate
                and not invented
                and not meal_regressions(core.state, orders, candidate, now)
            ):
                return candidate
    except Exception as exc:  # noqa: BLE001 - keep unaffected public content
        logger.warning(
            "[Mengyu meal] Conflict rewrite unavailable: %s", type(exc).__name__
        )
    return (
        "".join(
            part
            for part in re.split(r"(?<=[，,。！？!?；;\n])", text)
            if not meal_regressions(core.state, orders, part, now)
            and not (
                meal_reopening(context, text)
                and re.search(r"饭|吃|热|剩|外卖|放凉", part)
            )
        ).strip("，, ")
        or "嗯，我在呢。"
    )
