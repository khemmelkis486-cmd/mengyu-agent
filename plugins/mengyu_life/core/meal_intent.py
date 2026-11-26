"""Short-lived explicit user preference for the next meal location."""

import re

OUT_PATTERNS = (
    r"(?:就在|今天|今晚|这顿|晚饭|午饭|下一顿).{0,12}(?:外面吃|出去吃|餐馆|餐厅)",
    r"(?:在外面吃|出去吃|去外面吃|外面找家|附近找家).{0,16}",
    r"不想(?:回家)?开火|别回家做饭|不要在家做饭|别在家煮",
)
HOME_PATTERNS = (
    r"(?:就在|今天|今晚|这顿|晚饭|午饭).{0,12}(?:在家吃|回家吃|回家做饭)",
    r"在家吃|回家吃|回家开火|自己做饭|在家做饭",
)
_META_RE = re.compile(r"好像没听见|她没听见|你没听见|我说的是|我说.{0,12}(?:在外面吃|出去吃|外食)|我之前说|刚才说|截图|这个问题|这次的问题|插件")
_HYPOTHETICAL_RE = re.compile(r"如果|假如|要是|哪天|有一天|想象|比如|例如")
_NEGATED_RE = re.compile(r"不想在外面吃|不要(?:在外面|出去|去外面)吃|别(?:在外面|出去|去外面)吃|不在外面吃|不是在外面吃|不去外面吃")
_OWN_MEAL_RE = re.compile(
    r"^(?:我|本人)(?:今天|今晚|现在|待会|等会|一会儿)?"
    r"(?:(?:在外面吃|出去吃|去外面吃|外食|在家吃|回家吃|回家做饭|自己做饭)|"
    r"(?:就|要|准备|打算|可能|会|已经|刚刚|想).{0,30}"
    r"(?:在外面吃|出去吃|去外面吃|外食|在家吃|回家吃|回家做饭|自己做饭))"
)


def capture_user_meal_preference(state, text, now):
    """Save a clear, current instruction about where Mengyu should eat next.

    Meta feedback, quotations/hypotheticals and negated alternatives are ignored.
    The preference expires after eight hours unless the user states a new one.
    """
    value = str(text or "").strip()
    if not value or len(value) > 500 or _META_RE.search(value) or _HYPOTHETICAL_RE.search(value):
        return False
    # Do not mistake a first-person statement about the user's own meal for
    # an instruction about Mengyu. Shared "我们" plans remain eligible.
    if _OWN_MEAL_RE.search(value):
        return False
    location = None
    if any(re.search(pattern, value) for pattern in OUT_PATTERNS) and not _NEGATED_RE.search(value):
        location = "out"
    elif any(re.search(pattern, value) for pattern in HOME_PATTERNS):
        location = "home"
    if not location:
        return False
    previous = state.get("meal_location_preference") or {}
    if previous.get("location") == location and previous.get("status") == "pending" and now - previous.get("at", 0) < 300:
        return False
    state["meal_location_preference"] = {
        "location": location,
        "status": "pending",
        "at": now,
        "expires_at": now + 8 * 3600,
        "source_text": value[:200],
    }
    return True


def context(state, now):
    """Return an unexpired pending preference, if one exists."""
    value = state.get("meal_location_preference") or {}
    if value.get("status") != "pending" or value.get("location") not in ("out", "home"):
        return None
    if value.get("expires_at", 0) <= now:
        value["status"] = "expired"
        value["expired_at"] = now
        return None
    return {
        "location": value["location"],
        "source_text": value.get("source_text", ""),
        "expires_at": value.get("expires_at"),
        "rule": "这是用户对下一顿饭的明确地点要求；外食不能改成回家做饭，在家吃也不能擅自改成外食。",
    }


def apply_to_plan(state, plan, now):
    """Apply the pending preference to a meal plan, never to unrelated activities."""
    preference = context(state, now)
    if not preference or not isinstance(plan, dict) or plan.get("kind") != "daylife":
        return False
    from data.plugins.mengyu_life.core.life_schedule import activity_profile

    reason = str(plan.get("reason") or "")
    detail = str(plan.get("detail") or "")
    destination = str(plan.get("destination") or "")
    profile = activity_profile(reason + " " + detail, destination, str(plan.get("mode") or "home"))
    if profile != "meal":
        return False
    location = preference["location"]
    if location == "out":
        plan["mode"] = "out"
        if not destination or re.search(r"厨房|家里|住处|客厅|卧室", destination):
            plan["destination"] = "附近餐馆"
        plan["reason"] = "按用户明确要求去附近餐馆吃饭"
        plan["detail"] = "保留用户指定的外食安排；不改成回家开火。到店前不算已经吃到。"
        plan["prep_minutes"] = max(0, min(15, int(plan.get("prep_minutes") or 5)))
        plan["travel_minutes"] = max(5, min(30, int(plan.get("travel_minutes") or 10)))
        plan["stay_minutes"] = max(45, min(120, int(plan.get("stay_minutes") or 60)))
        plan["_meal_location"] = "out"
    else:
        plan["mode"] = "home"
        plan["destination"] = "住处厨房"
        plan["reason"] = "按用户明确要求在家吃饭"
        plan["detail"] = "保留用户指定的在家用餐安排。"
        plan["prep_minutes"] = 0
        plan["travel_minutes"] = 0
        plan["stay_minutes"] = max(20, min(90, int(plan.get("stay_minutes") or 45)))
        plan["_meal_location"] = "home"
    return True


def mark_scheduled(state, plan, now):
    """Consume the preference only after a matching meal plan was accepted."""
    preference = context(state, now)
    if not preference or not isinstance(plan, dict) or plan.get("kind") != "daylife":
        return False
    from data.plugins.mengyu_life.core.life_schedule import activity_profile

    profile = activity_profile(
        str(plan.get("reason") or "") + " " + str(plan.get("detail") or ""),
        str(plan.get("destination") or ""),
        str(plan.get("mode") or "home"),
    )
    if profile != "meal" or plan.get("mode") != preference["location"]:
        return False
    value = state.get("meal_location_preference") or {}
    value["status"] = "scheduled"
    value["scheduled_at"] = now
    state["meal_location_preference"] = value
    return True
