"""Reasonable time bounds and readable continuity for Mengyu's fictional daylife."""

import re
from datetime import datetime

from data.plugins.mengyu_contact.local_clock import TZ

_QUICK_FOOD_PICKUP_RE = re.compile(r"买|取餐|外卖|打包|买饭|带回家|带回住处|取外卖")

GUIDANCE = """【自主时间与活动衔接】生活时间线按同一时钟连续推进，但不是打卡表。空闲、发呆、临时改主意都允许，不为填满时间编活动。每段活动必须有合理的开始、预计结束和地点；已经结束的活动不能继续占用后续时段，也不能把计划说成已经发生。吃饭通常安排15—75分钟，洗澡10—45分钟，简单收拾10—60分钟，运动20—120分钟，阅读/游戏/看剧按实际兴趣安排20—240分钟，工作或学习按当前阶段安排20—180分钟；不要把所有活动都设成相同长度。外出需要分别计算往返交通和停留时间，短途买东西不自动占两三个小时。逛街、试穿、看饰品、美妆和商场闲逛是完整的休闲活动，不要降格成取快递或买日用品；通常安排45—180分钟，按实际兴趣决定，不要求购物或付款。一次外出可以包含多个连续地点，例如商场逛店后去附近餐馆吃饭，再回家；按顺序记录每一站，不能跳过已经承诺的外食，也不能把餐馆改成回家开火。用户明确说在外面吃时，下一顿必须保留外食地点偏好，除非用户随后明确改口。外卖送达后若长时间没开始吃，要明确决定现在吃、留到下一顿或不吃，不把一顿饭悬挂数小时后突然说刚买/刚到；如果确实拖延，只有已有合理的中间安排才补充原因。每次规划先核对最近活动的实际时间、当前阶段、已说出口的经历和订单时间，再安排下一件事。"""


def _minutes(value, default):
    if isinstance(value, bool):
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def activity_profile(reason="", destination="", mode="home"):
    text = str(reason or "") + " " + str(destination or "")
    checks = (
        ("shopping", r"逛街|逛商场|商场|购物中心|商业街|女装店|服装店|鞋店|饰品店|配饰店|包店|美妆店|化妆品店|试衣|试穿|买衣服|挑衣服|看衣服|挑包|首饰|逛店"),
        ("meal", r"吃饭|晚饭|午饭|早饭|早餐|午餐|晚餐|外面吃|出去吃|餐馆|餐厅|锅贴|饺子|馄饨|面条|米饭|盖饭|炒饭|做饭|煮面|烤肉|火锅|点心|甜品"),
        ("shower", r"洗澡|洗头|吹头发|洗漱"),
        ("cleaning", r"收拾|打扫|整理房间|洗衣|晾衣|换床品"),
        ("walk", r"散步|遛弯"),
        ("exercise", r"运动|健身|跑步|瑜伽|骑车|游泳"),
        ("rest", r"休息|午睡|睡一会|发呆|躺着"),
        ("work", r"赶稿|改稿|排版|插画|交稿|工作|处理稿件"),
        ("study", r"学习|复习|看资料|做题|读论文"),
        ("hobby", r"看书|阅读|看剧|追剧|电影|游戏|听歌|画画|练琴"),
        ("errand", r"买东西|超市|取快递|寄快递|办事|买生活用品|买日用品"),
        ("social", r"见朋友|聚会|约饭|聊天|拜访|一起玩"),
    )
    for kind, pattern in checks:
        if re.search(pattern, text):
            return kind
    return "outing" if mode == "out" else "general"


def _normalise_stop(stop, fallback_travel=10):
    stop = stop if isinstance(stop, dict) else {}
    destination = str(stop.get("destination") or "").strip()[:80]
    reason = str(stop.get("reason") or destination or "逛逛").strip()[:200]
    if not destination or re.search(r"住处|家里|客厅|卧室|厨房|房间|宿舍|阳台|窗边", destination):
        return None
    category = activity_profile(reason, destination, "out")
    stay = _minutes(stop.get("stay_minutes"), 90 if category == "shopping" else 60 if category == "meal" else 40)
    ranges = {
        "shopping": (45, 180, 90),
        "meal": (45, 120, 60),
        "walk": (30, 60, 40),
        "exercise": (20, 120, 45),
        "social": (45, 240, 90),
        "errand": (10, 75, 35),
        "culture": (30, 180, 75),
        "outing": (30, 180, 60),
    }
    low, high, default = ranges.get(category, ranges["outing"])
    quick_food_pickup = category == "meal" and bool(_QUICK_FOOD_PICKUP_RE.search(reason))
    if quick_food_pickup:
        low, high, default = 10, 45, 25
    if stay <= 0:
        stay = default
    travel = _minutes(stop.get("travel_minutes"), fallback_travel)
    travel = max(5, min(30 if quick_food_pickup else 60, travel))
    return {
        "destination": destination,
        "reason": reason,
        "category": category,
        "stay_minutes": max(low, min(high, stay)),
        "travel_minutes": travel,
    }


def normalise_daylife_plan(plan, now=None):
    """Clamp model durations and preserve a bounded multi-stop outing."""
    result = dict(plan or {})
    mode = str(result.get("mode") or "out")
    if mode not in ("home", "out"):
        mode = "home"
    reason = str(result.get("reason") or "")
    destination = str(result.get("destination") or "")
    if mode == "out" and re.search(r"住处|家里|客厅|卧室|厨房|房间|宿舍|阳台|窗边", destination):
        mode = "home"
    result["mode"] = mode
    kind = activity_profile(reason, destination, mode)
    stay = _minutes(result.get("stay_minutes"), 90 if kind == "shopping" else 60 if kind == "meal" else 45)
    ranges = {
        "meal": (15, 75, 35),
        "shopping": (45, 180, 90),
        "shower": (10, 45, 25),
        "cleaning": (10, 60, 30),
        "walk": (30, 60, 40),
        "exercise": (20, 120, 45),
        "rest": (10, 180, 45),
        "work": (20, 180, 60),
        "study": (20, 180, 60),
        "hobby": (20, 240, 60),
        "errand": (15, 100, 40),
        "social": (45, 300, 120),
        "outing": (30, 240, 90),
        "general": (15, 180, 45),
    }
    low, high, default = ranges[kind]
    if stay <= 0:
        stay = default
    result["stay_minutes"] = max(low, min(high, stay))
    prep = _minutes(result.get("prep_minutes"), 0 if mode == "home" else 5)
    result["prep_minutes"] = max(0, min(45, prep))
    if mode == "home":
        result["travel_minutes"] = 0
        result.pop("itinerary", None)
    else:
        travel = _minutes(result.get("travel_minutes"), 15)
        result["travel_minutes"] = max(5, min(60, travel))
        raw_stops = result.get("itinerary")
        stops = []
        if isinstance(raw_stops, list):
            for index, raw_stop in enumerate(raw_stops[:4]):
                stop = _normalise_stop(raw_stop, result["travel_minutes"] if index == 0 else 10)
                if stop and (not stops or stop["destination"] != stops[-1]["destination"]):
                    stops.append(stop)
        if not stops:
            stops = [_normalise_stop({
                "destination": destination or "附近街区",
                "reason": reason or "出去走走",
                "stay_minutes": result["stay_minutes"],
                "travel_minutes": result["travel_minutes"],
            }, result["travel_minutes"])]
        result["itinerary"] = stops[:4]
        result["destination"] = result["itinerary"][0]["destination"]
        result["stay_minutes"] = sum(stop["stay_minutes"] for stop in result["itinerary"])
        result["travel_minutes"] = sum(stop["travel_minutes"] for stop in result["itinerary"])
    result["_activity_profile"] = kind if mode == "home" else (result.get("itinerary") or [{}])[0].get("category", kind)
    return result


def timeline_context(segments, now, limit=10):
    """Summarise recent fictional segments and explicit idle gaps; gaps are allowed."""
    start = now - 36 * 3600
    rows = []
    for segment in (segments or []):
        if not isinstance(segment, dict) or segment.get("cancelled_at"):
            continue
        created = segment.get("created_at", 0)
        phases = segment.get("phases") or []
        if not isinstance(created, (int, float)) or not phases:
            continue
        phase_times = [p.get("at") for p in phases if isinstance(p, dict) and isinstance(p.get("at"), (int, float))]
        if not phase_times:
            continue
        end = phase_times[-1]
        if end < start or created > now:
            continue
        elapsed = [p for p in phases if isinstance(p, dict) and isinstance(p.get("at"), (int, float)) and p.get("at") <= now]
        upcoming = [p for p in phases if isinstance(p, dict) and isinstance(p.get("at"), (int, float)) and p.get("at") > now]
        current = elapsed[-1] if elapsed else phases[0]
        activity_status = segment.get("activity_status")
        if activity_status not in ("planned", "in_progress", "completed", "paused", "cancelled"):
            planning_stages = {"准备出门", "打算做", "临时改主意"}
            active_elapsed = [
                phase for phase in elapsed
                if str(phase.get("stage") or "").strip() not in planning_stages
            ]
            activity_status = (
                "completed" if not upcoming
                else "in_progress" if active_elapsed
                else "planned"
            )
        progress = segment.get("progress")
        if not isinstance(progress, list):
            progress = []
        rows.append({
            "id": segment.get("id"),
            "activity_chain_id": segment.get("activity_chain_id") or segment.get("scheduler_task_id") or segment.get("id"),
            "activity_status": activity_status,
            "start": datetime.fromtimestamp(created, TZ).strftime("%m-%d %H:%M"),
            "end": datetime.fromtimestamp(end, TZ).strftime("%m-%d %H:%M"),
            "duration_minutes": max(0, int((end - created) / 60)),
            "place": current.get("place", segment.get("destination", "")),
            "stage": current.get("stage", ""),
            "next_stage": upcoming[0].get("stage") if upcoming else None,
            "reason": str(segment.get("reason", ""))[:100],
            "game_title": str(segment.get("game_title", ""))[:80],
            "meal": segment.get("meal"),
            "completed": activity_status == "completed",
            "progress": [
                {
                    "stage": str(item.get("stage") or "")[:60],
                    "detail": str(item.get("detail") or "")[:180],
                    "place": str(item.get("place") or "")[:80],
                    "at": item.get("at"),
                }
                for item in progress[-5:]
                if isinstance(item, dict)
            ],
            "_sort_start": created,
            "_sort_end": end,
        })
    rows.sort(key=lambda item: item["_sort_start"])
    idle_gaps = []
    for previous, current in zip(rows, rows[1:]):
        gap = current["_sort_start"] - previous["_sort_end"]
        if gap >= 30 * 60:
            idle_gaps.append({
                "from": datetime.fromtimestamp(previous["_sort_end"], TZ).strftime("%m-%d %H:%M"),
                "to": datetime.fromtimestamp(current["_sort_start"], TZ).strftime("%m-%d %H:%M"),
                "minutes": int(gap / 60),
                "meaning": "这段时间没有已记录活动，不需要补造经历",
            })
    last_end = rows[-1]["_sort_end"] if rows else None
    since_last = max(0, int((now - last_end) / 60)) if last_end is not None else None
    visible = rows[-limit:]
    for row in visible:
        row.pop("_sort_start", None)
        row.pop("_sort_end", None)
    return {
        "as_of": datetime.fromtimestamp(now, TZ).strftime("%Y-%m-%d %H:%M"),
        "recent_segments": visible,
        "idle_gaps": idle_gaps[-6:],
        "minutes_since_last_recorded_activity": since_last,
        "rule": "空闲间隔允许存在，不要补造经历；新活动不得与未结束活动重叠。",
    }
