"""Persistent memory for Mengyu's consented fictional places and outdoor life."""

import hashlib
import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from data.plugins.mengyu_contact.state_write import write_json

from data.plugins.mengyu_contact.local_clock import TZ
SCHEMA_VERSION = 2

GUIDANCE = """【生活空间与外出探索】住处是生活的一部分，不是默认活动范围。结合本周已经实际到达的外出地点，判断自己是否想换个环境。参考目标是每周2—4个有意义的外出日，但只是软性节奏，不是打卡任务：生病、疲惫、天气不适、工作期限、已有安排或单纯想宅在家时都可以不出门，不补课、不为了凑数出门。本周目前少于2个外出日、又没有明确的休息或现实约束时，下一段合适空档优先考虑一项轻松户外活动；不要只因为居家选项更容易生成，就连续默认待在家。逛街是重要且自然的城市生活候选，不要把它当成少见的特殊事件：可以去商场、百货、商业街、女装店、鞋店、饰品店、包店、美妆店、文创店或家居店，看看新款、试穿、比较、挑选；可以只逛不买，不必为了证明出门有成果而付款。也可以顺路去咖啡馆、甜品店或餐馆。购物偏好应逐渐形成自己的审美，不按性别刻板规定必须喜欢什么。若最近几次外出都在公园、河边或步道散步，下一次应认真考虑商场、商业街、餐馆、咖啡馆、市集、书店或朋友社交；河边不应成为默认散步目的地，只有自己确实想亲近自然或沿水边走时才选。买外卖、取快递、短暂买生活用品不算休闲外出。一次外出可以包含多个连续地点，例如先逛商场、再去附近餐馆吃饭、最后回家；按实际顺序推进，不要跳过已经答应的外食，也不要把外食擅自改成回家开火。可在社区、城市公共空间、兴趣场所和社交场景之间变化。熟悉且喜欢的地方可以重访，新地方也可以偶尔探索；不要每天随机换地点，也不要连续几天只去同一家店。地点记忆只记录虚构日常中已经到达的地点，计划中的目的地不算去过。不要声称现实定位、真实消费或真实第三人活动已被验证。"""

_HOME_RE = re.compile(r"住处|家里|客厅|卧室|厨房|房间|宿舍|阳台|窗边")
_PICKUP_RE = re.compile(r"取餐|外卖|打包|买饭|带回家|取快递|寄快递|补生活用品|买日用品")
_SHOPPING_RE = re.compile(r"逛街|逛商场|商场|购物中心|商业街|女装店|服装店|鞋店|饰品店|配饰店|包店|美妆店|化妆品店|试衣|试穿|买衣服|挑衣服|看衣服|挑包|首饰|逛店")
_SOCIAL_RE = re.compile(r"见朋友|朋友|同事|聚会|约饭|拜访|一起玩|社交")
_EXERCISE_RE = re.compile(r"运动|健身|跑步|瑜伽|骑车|游泳|球")
_WALK_RE = re.compile(r"散步|遛弯|公园|河边|江边|步道|湖边|走走|透气")
_CULTURE_RE = re.compile(r"书店|图书馆|电影院|电影|展览|博物馆|美术馆|市集|花市|唱片")
_MEAL_RE = re.compile(r"吃饭|午饭|晚饭|早餐|餐馆|餐厅|火锅|烤肉|咖啡馆|甜品")
_ERRAND_RE = re.compile(r"超市|便利店|取快递|寄快递|办事|买生活用品|补日用品")
_WATERFRONT_RE = re.compile(r"河边|江边|湖边|滨水|沿河|河岸|湖畔|江畔")
_NEGATIVE_REQUEST_RE = re.compile(r"不是|不要|别|不想|总是|老是|怎么又|怎么总|别总|不要总")


def _load(path):
    path = Path(path)
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "places": [], "visits": [], "processed_segment_ids": [], "processed_arrival_ids": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema_version": SCHEMA_VERSION, "places": [], "visits": [], "processed_segment_ids": [], "processed_arrival_ids": []}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("schema_version", SCHEMA_VERSION)
    data.setdefault("places", [])
    data.setdefault("visits", [])
    data.setdefault("processed_segment_ids", [])
    data.setdefault("processed_arrival_ids", [])
    if not isinstance(data["places"], list):
        data["places"] = []
    if not isinstance(data["visits"], list):
        data["visits"] = []
    if not isinstance(data["processed_segment_ids"], list):
        data["processed_segment_ids"] = []
    if not isinstance(data["processed_arrival_ids"], list):
        data["processed_arrival_ids"] = []
    return data


def _save(path, data):
    write_json(path, data, "PlaceStore")

def _category(destination, reason):
    text = f"{destination} {reason}"
    if _PICKUP_RE.search(reason):
        return "errand"
    if _SOCIAL_RE.search(text):
        return "social"
    if _SHOPPING_RE.search(text):
        return "shopping"
    if _EXERCISE_RE.search(text):
        return "exercise"
    if _WALK_RE.search(text):
        return "walk"
    if _CULTURE_RE.search(text):
        return "culture"
    if _MEAL_RE.search(text):
        return "meal"
    if _ERRAND_RE.search(text):
        return "errand"
    return "explore"


def _week_start(timestamp):
    local = datetime.fromtimestamp(timestamp, TZ)
    monday = (local - timedelta(days=local.weekday())).date()
    return monday.isoformat()


def _place_id(name):
    normal = re.sub(r"[\s，。！？、；,.!?]+", "", str(name or "").lower())
    return hashlib.sha1(normal.encode("utf-8")).hexdigest()[:12]


def sync_arrivals(path, segments, now):
    """Persist each reached stop in a multi-stop outing exactly once.

    Legacy ``processed_segment_ids`` entries suppress the first arrival only;
    later arrivals added by the multi-stop schema can still be imported.
    """
    data = _load(path)
    processed_segments = set(str(item) for item in data["processed_segment_ids"])
    processed_arrivals = set(str(item) for item in data["processed_arrival_ids"])
    added = 0
    touched = False
    for segment in segments or []:
        if not isinstance(segment, dict) or segment.get("mode") != "out":
            continue
        segment_id = str(segment.get("id") or "")
        if not segment_id:
            continue
        phases = segment.get("phases") or []
        arrival_rows = [
            (index, phase) for index, phase in enumerate(phases)
            if isinstance(phase, dict)
            and phase.get("stage") in ("到了", "到达", "抵达")
            and isinstance(phase.get("at"), (int, float))
            and phase.get("at", 0) <= now
        ]
        first_arrival_index = arrival_rows[0][0] if arrival_rows else None
        for phase_index, arrival in arrival_rows:
            cancelled_at = segment.get("cancelled_at")
            # If the trip was cancelled before this stop was reached, it is not
            # a visit and should not create even an empty places.json file.
            if isinstance(cancelled_at, (int, float)) and cancelled_at <= arrival["at"]:
                continue
            visit_key = f"{segment_id}:{phase_index}"
            if visit_key in processed_arrivals:
                continue
            # Old files recorded a whole segment after its single arrival. Keep
            # that first visit idempotent while permitting newly added stops.
            if segment_id in processed_segments and phase_index == first_arrival_index:
                processed_arrivals.add(visit_key)
                touched = True
                continue
            processed_arrivals.add(visit_key)
            touched = True
            name = str(arrival.get("place") or segment.get("destination") or "").strip()[:80]
            if not name or _HOME_RE.search(name):
                continue
            reason = str(arrival.get("reason") or segment.get("reason") or "")[:200]
            category = str(arrival.get("category") or _category(name, reason))
            if category not in ("shopping", "social", "exercise", "walk", "culture", "meal", "errand", "explore"):
                category = _category(name, reason)
            place_id = _place_id(name)
            place = next((item for item in data["places"] if item.get("id") == place_id), None)
            if place is None:
                place = {
                    "id": place_id,
                    "name": name,
                    "category": category,
                    "first_visit_at": arrival["at"],
                    "last_visit_at": arrival["at"],
                    "visit_count": 0,
                }
                data["places"].append(place)
            place["last_visit_at"] = arrival["at"]
            place["visit_count"] = int(place.get("visit_count", 0)) + 1
            place["category"] = category
            visit = {
                "segment_id": segment_id,
                "stop_index": arrival.get("stop_index", phase_index),
                "place_id": place_id,
                "name": name,
                "category": category,
                "at": arrival["at"],
                "day": datetime.fromtimestamp(arrival["at"], TZ).date().isoformat(),
                "counts_toward_weekly_goal": category != "errand",
                "reason": reason,
                "detail": str(segment.get("detail") or "")[:240],
            }
            data["visits"].append(visit)
            processed_segments.add(segment_id)
            added += 1
    if touched:
        data["schema_version"] = SCHEMA_VERSION
        data["processed_segment_ids"] = sorted(processed_segments)[-500:]
        data["processed_arrival_ids"] = sorted(processed_arrivals)[-2000:]
        data["places"] = sorted(data["places"], key=lambda item: item.get("last_visit_at", 0))[-100:]
        data["visits"] = sorted(data["visits"], key=lambda item: item.get("at", 0))[-500:]
        _save(path, data)
    return added


def context(path, segments, now):
    """Summarise familiar places and this week's non-errand outdoor days."""
    data = _load(path)
    week_start = _week_start(now)
    visits = [
        item for item in data["visits"]
        if isinstance(item, dict)
        and item.get("day", "") >= week_start
        and item.get("at", 0) <= now
    ]
    meaningful = [item for item in visits if item.get("counts_toward_weekly_goal")]
    meaningful_days = sorted({item.get("day") for item in meaningful if item.get("day")})
    errands = [item for item in visits if not item.get("counts_toward_weekly_goal")]
    recent = sorted(data["places"], key=lambda item: item.get("last_visit_at", 0), reverse=True)
    recent_visits = sorted(
        [item for item in data["visits"] if isinstance(item, dict) and item.get("at", 0) <= now],
        key=lambda item: item.get("at", 0), reverse=True,
    )
    recent_waterfront = [item for item in recent_visits[:8] if _WATERFRONT_RE.search(str(item.get("name") or ""))]
    recent_shopping = [item for item in recent_visits[:8] if item.get("category") == "shopping"]
    return {
        "性质": "仅记录虚构日常中已经到达的地点；不代表真实定位或现实消费",
        "本周起始日": week_start,
        "meaningful_outing_days_this_week": len(meaningful_days),
        "本周有意义的外出日期": meaningful_days,
        "quick_errands_this_week": len(errands),
        "weekly_soft_target_days": [2, 4],
        "consider_an_outing": len(meaningful_days) < 2,
        "familiar_places": [
            {"name": item.get("name"), "category": item.get("category"), "visit_count": item.get("visit_count", 0), "last_visit_at": item.get("last_visit_at")}
            for item in recent[:12]
        ],
        "recent_visits": [
            {"name": item.get("name"), "category": item.get("category"), "day": item.get("day"), "reason": item.get("reason", "")}
            for item in sorted(visits, key=lambda item: item.get("at", 0), reverse=True)[:8]
        ],
        "recent_outing_categories": [item.get("category") for item in recent_visits[:8]],
        "recent_waterfront_visit_count": len(recent_waterfront),
        "recent_shopping_visit_count": len(recent_shopping),
        "available_activity_types": ["商场/商业街逛街", "服装/鞋包/饰品/美妆店", "书店/文创/展览", "咖啡馆/甜品店/餐馆", "朋友社交", "市集与城市探索", "散步与公园", "运动"],
        "rule": "每周2—4个有意义外出日只是软性参考；逛街和外食是正常重要的生活候选；短途办事和取餐不计入；河边不是默认目的地；可以宅家，不补打卡。",
    }


def diversify_outing_plan(plan, path, now=None, user_texts=None):
    """Prevent a generic autonomous outing from defaulting to the waterfront.

    Explicit positive requests for a river/lake destination are respected;
    complaints about repeated riverside walks are not treated as requests.
    If the model picks a generic waterfront walk, the replacement is based on
    recent fictional visit history and remains a normal multi-stop-compatible
    daylife plan.
    """
    if not isinstance(plan, dict) or plan.get("kind") != "daylife" or plan.get("mode") != "out":
        return plan
    from data.plugins.mengyu_life.core.life_schedule import activity_profile

    user_rows = [str(item or "") for item in (user_texts or [])][-20:]
    explicit_river = any(
        _WATERFRONT_RE.search(text)
        and re.search(r"想去|要去|去.{0,8}(?:河边|江边|湖边)|(?:河边|江边|湖边).{0,8}(?:走走|散步|逛逛)", text)
        and not _NEGATIVE_REQUEST_RE.search(text)
        for text in user_rows
    )
    if explicit_river:
        return plan

    stops = plan.get("itinerary") if isinstance(plan.get("itinerary"), list) else []
    candidates = []
    if stops:
        for index, stop in enumerate(stops[:4]):
            if isinstance(stop, dict) and _WATERFRONT_RE.search(
                str(stop.get("destination") or "") + " " + str(stop.get("reason") or "")
            ):
                candidates.append((index, stop))
    else:
        if _WATERFRONT_RE.search(str(plan.get("destination") or "") + " " + str(plan.get("reason") or "")):
            candidates.append((None, plan))
    if not candidates:
        return plan
    if not stops and activity_profile(
        str(plan.get("reason") or ""), str(plan.get("destination") or ""), "out"
    ) == "meal":
        return plan

    data = _load(path)
    recent_visits = sorted(
        [item for item in data["visits"] if isinstance(item, dict) and item.get("at", 0) <= (now or time.time())],
        key=lambda item: item.get("at", 0), reverse=True,
    )
    recent_shopping = sum(1 for item in recent_visits[:5] if item.get("category") == "shopping")
    user_walk = any(
        re.search(r"想散步|出去散步|想走走|出去走走|遛弯", text)
        and not _NEGATIVE_REQUEST_RE.search(text)
        for text in user_rows
    )

    # Avoid replacing one repetitive pattern with another. After several recent
    # shopping visits, prefer a cafe/bookstore/culture stop instead.
    if recent_shopping >= 2:
        replacement = {
            "destination": "附近独立书店",
            "reason": "去附近独立书店翻翻新书和杂志，顺便换个环境",
            "detail": "在书架间慢慢看看，遇到感兴趣的书再多翻几页。",
            "category": "culture",
            "stay_minutes": 60,
        }
    elif user_walk:
        replacement = {
            "destination": "附近商业街步行区",
            "reason": "沿着商业街步行区散散步，顺路看看服装店和饰品店的橱窗",
            "detail": "不赶时间，走走看看店铺陈列，想进去再进去。",
            "category": "shopping",
            "stay_minutes": 60,
        }
    else:
        replacement = {
            "destination": "附近商场",
            "reason": "去商场逛逛，看看衣服、包包和饰品，不一定买东西",
            "detail": "先看看服装区和饰品区，有喜欢的款式再决定要不要试穿。",
            "category": "shopping",
            "stay_minutes": 90,
        }

    index, target = candidates[0]
    if index is None:
        plan["destination"] = replacement["destination"]
        plan["reason"] = replacement["reason"]
        plan["detail"] = replacement["detail"]
        plan["stay_minutes"] = replacement["stay_minutes"]
    else:
        target.update({
            "destination": replacement["destination"],
            "reason": replacement["reason"],
            "category": replacement["category"],
            "stay_minutes": replacement["stay_minutes"],
        })
        plan["destination"] = str(stops[0].get("destination") or replacement["destination"])
        if index == 0:
            plan["destination"] = replacement["destination"]
        plan["itinerary"] = stops
    plan["_outing_diversity_applied"] = True
    plan["_activity_profile"] = activity_profile(replacement["reason"], replacement["destination"], "out")
    plan["reason"] = replacement["reason"] if index is None or index == 0 else str(plan.get("reason") or "外出")
    return plan
