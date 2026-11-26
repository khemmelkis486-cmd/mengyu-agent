"""Flexible fictional day options, never evidence that an activity happened.

The daily plan (mengyu_life) now owns the schedule.  This module keeps the
agenda container and the read helpers the prompts use.
"""

import re
from datetime import datetime, timedelta

if __package__:
    from .local_clock import TZ
else:
    from local_clock import TZ
GUIDANCE = """【自己的日常安排】起床与睡觉时间以独立生活时钟为准，不跟随用户开机或上线时间；每日生活候选只是接下来可以做的事，不是已经发生。自己选具体细节、合理耗时和接下来的去向，用daylife记录；也可以继续或暂时休息。候选不要求打卡，不每轮换活动。已经结束的活动不要在脑子里继续一整天：若先前做完饭、收过稿，之后可以洗漱换衣、洗晒整理、下楼买东西、散步、随便画两笔、追剧、听歌、翻书、刷手机或出神，具体选什么接当前时间和心情。活动时长按具体内容估计，短暂取餐、买生活用品不要默认占两三个小时；吃饭、洗澡、收拾、工作、娱乐各有合理耗时。买到晚饭不等于已经吃了，但也不要无故把已买的饭拖数小时才开始吃；如果暂时不饿，可以明确留到下一顿。一天里可以有空档，发呆和休息也是生活，不必用活动填满。不要在分享中永远只有游戏、工作或索取陪伴。游戏可以继续喜欢，但最近接连讲游戏时，看看生活里另一处自己在意的小事；能自然编自己的细节，不编他的行动。现实天气/工具结果保持真实，自己的日常按已约定的虚构生活发展。未来计划不能说成经历；没有新经历也可分享当下念头，而不是汇报已结束的厨房/旧稿。照片可以拍自己或眼前生活，不限游戏物件。"""


def ensure_day(state, now):
    """Keep the agenda container; the daily plan owns the schedule."""
    day = datetime.fromtimestamp(now, TZ).date().isoformat()
    agenda = state.get("daily_rhythm")
    if not isinstance(agenda, dict):
        agenda = {
            "day": day,
            "fictional": True,
            "status": "按计划推进，不代表已做",
            "source": "daily_plan",
            "anchors": [],
            "schedule": [],
            "started": {},
        }
        state["daily_rhythm"] = agenda
    agenda.setdefault("schedule", [])
    agenda.setdefault("anchors", [])
    return agenda


def rhythm_context(state, records, now):
    day = datetime.fromtimestamp(now, TZ).date().isoformat()
    agenda = state.get("daily_rhythm") or {}
    hour = datetime.fromtimestamp(now, TZ).hour
    options = (
        [x for x in agenda.get("anchors", []) if x["hours"][0] <= hour < x["hours"][1]]
        if agenda.get("day") == day
        else []
    )
    history = [
        r
        for r in state.get("proactive_history", [])
        if 0 <= now - r.get("at", 0) < 86400
    ][-5:]
    game = sum(
        bool(
            re.search(
                r"游戏|杀戮尖塔|星露谷|删牌|精英|手柄|卡牌|这局|打怪",
                str(r.get("text", "")),
            )
        )
        for r in history
    )
    latest = [
        r
        for r in records
        if r.get("mode") in ("home", "out") and r.get("created_at", 0) <= now
    ][-1:]
    return {
        "性质": "计划需进入活动时间线并走到相关阶段才可当作经历",
        "当天已选安排": schedule_context(state, records, now),
        "当前时段可选": options,
        "最近游戏偏多": game >= 2,
        "最近明确日常": latest,
        "当前时刻": datetime.fromtimestamp(now, TZ).strftime("%m-%d %H:%M"),
    }


def due_life_slot(state, virtual_life, now):
    """A plan slot is eligible only inside its own window and only once."""
    agenda = state.get("daily_rhythm") or {}
    if agenda.get("day") != datetime.fromtimestamp(now, TZ).date().isoformat():
        return None
    started = {row.get("daily_anchor_id") for row in virtual_life if isinstance(row, dict)}
    started.update(agenda.get("started") or {})
    for slot in agenda.get("schedule") or ():
        if slot["id"] not in started and slot["start_at"] <= now < slot["latest_start"]:
            return slot
    return None


def schedule_context(state, virtual_life, now):
    agenda = state.get("daily_rhythm") or {}
    if agenda.get("day") != datetime.fromtimestamp(now, TZ).date().isoformat():
        return []
    by_anchor = {
        row.get("daily_anchor_id"): row for row in virtual_life
        if isinstance(row, dict) and row.get("daily_anchor_id")
    }
    output = []
    for slot in agenda.get("schedule") or ():
        row = by_anchor.get(slot.get("id"))
        if row:
            status = row.get("activity_status") or "planned"
        elif slot["id"] in (agenda.get("started") or {}):
            status = "此前已接受，具体进展以活动记录为准"
        else:
            status = "planned" if now < slot["latest_start"] else "错过"
        output.append({
            "id": slot["id"], "安排": (slot.get("plan") or {}).get("reason"),
            "状态": status,
            "最早开始": datetime.fromtimestamp(slot["start_at"], TZ).strftime("%H:%M"),
        })
    return output
