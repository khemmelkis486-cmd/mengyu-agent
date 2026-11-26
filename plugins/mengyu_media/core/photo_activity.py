"""Facts shared by the current activity, its live photo and the reply."""

import re

LIFE_PHOTO_ORIGIN = "self_chosen_life_photo"
LIFE_PHOTO_MAX_AGE = 86400

PLACE = re.compile(r"餐馆|餐厅|面馆|饭馆|饭店|咖啡馆|咖啡店|甜品店|食堂|小吃店")
_HOME = re.compile(r"住处|家里|客厅|卧室|厨房|宿舍|阳台|书房|房间")
_PLACE_HINT = re.compile(
    r"(?:在|坐在|到了?|去)(?:一家|附近的)?(餐馆|餐厅|面馆|饭馆|饭店|咖啡馆|咖啡店|食堂|"
    r"小吃店|超市|商场|公园|书店|图书馆|厨房|客厅|卧室|阳台|书房|家里|住处)"
)
_DISH = re.compile(
    r"(?:正在|正|在)?(?:吃|做|煮|炒|喝)(?:着|了|一碗|一份|一盘|一杯|几口)*"
    r"([\u4e00-\u9fff]{0,14}?(?:拌面|拉面|汤面|炒面|面条|面|炒饭|盖饭|米饭|饭|"
    r"锅贴|饺子|馄饨|包子|粥|汤|排骨|豆腐|鸡翅|牛排|披萨|汉堡|三明治|沙拉|咖啡|奶茶|蛋糕|寿司|鱼|虾|粉))"
    r"(?=$|[\s，。！？、；,.!?的和配跟与，了着呢旁])"
)
_MEAL_QUESTION = re.compile(r"吃(?:的|了|着)?(?:什么|啥|哪道菜)|(?:什么|啥)(?:菜|饭)|(?:晚|午|早)饭吃啥")
_MEAL_PHOTO = re.compile(
    r"正在吃|在吃|你吃的|你喝的|(?:拍|发|看看).{0,12}(?:吃的|这顿|饭菜|午饭|晚饭|餐食|菜品)|"
    r"(?:餐食|饭菜|晚饭|午饭|菜品).{0,8}(?:照片|特写)"
)
_PLACE_QUESTION = re.compile(r"在哪(?:儿|里)?|哪家(?:店|餐厅|餐馆|面馆)?|什么地方")
_CURRENT_MEAL = re.compile(r"(?:我)?(?:这会儿|现在|正在|正要|正|刚开始|刚吃上|已经在|我在).{0,12}(?:吃|喝|做|煮|炒)")
_GENERIC_DISHES = {"家常餐", "午饭", "晚饭", "早饭", "早餐", "午餐", "晚餐", "饭", "面", "菜", "东西", "什么饭"}


def _label(value, size):
    value = str(value or "").strip()
    if not re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9·（）()\s-]+", value) or not 1 < len(value) <= size:
        return ""
    return value


def described_dish(text):
    match = _DISH.search(str(text or ""))
    return dish_label(match[1]) if match else ""


def dish_label(value):
    value = _label(value, 32)
    return "" if value in _GENERIC_DISHES else value


def binding_for(segment, phase):
    """Resolve meal and place only for the phase that has actually started."""
    place = str(phase.get("place") or segment.get("destination") or "")
    stage = str(phase.get("stage") or "")
    mode = segment.get("mode")
    action = str(phase.get("reason") or segment.get("reason") or "")
    if stage in ("去的路上", "回来的路上", "前往下一站"):
        action = stage
    meal = segment.get("meal") or {}
    outdoor_meal = stage in ("到了", "正在用餐") and (phase.get("category") == "meal" or PLACE.search(place))
    waiting_for_food = re.search(r"等餐|还没吃|没开吃|点餐|看菜单|排队", action)
    home_meal = mode == "home" and stage == "正在做" and (
        (meal and re.search(r"吃|做|下厨|煮|炒|烤|炖|用餐", action))
        or re.search(r"吃|用餐", action)
    )
    dish, meal_state = "", ""
    if home_meal or outdoor_meal and not waiting_for_food:
        phase_dish = (phase.get("meal") or {}).get("dish") if isinstance(phase.get("meal"), dict) else phase.get("dish")
        meal_stops = sum(
            item.get("stage") == "到了" and (
                item.get("category") == "meal" or bool(PLACE.search(str(item.get("place") or "")))
            )
            for item in segment.get("phases", []) if isinstance(item, dict)
        )
        use_segment_meal = home_meal or meal_stops <= 1
        dish = (
            dish_label(phase_dish)
            or (dish_label(meal.get("dish")) if use_segment_meal and isinstance(meal, dict) else "")
            or described_dish(phase.get("reason"))
            or (described_dish(action) if use_segment_meal else "")
        )
        if dish or re.search(r"吃|用餐|点了饭", action) or phase.get("category") == "meal":
            meal_state = "cooking" if home_meal and re.search(r"做|下厨|煮|炒|烤|炖", action) and not re.search(r"正在吃|开始吃|在吃", action) else "eating"
    if dish:
        action = ("正在做" if meal_state == "cooking" else "正在吃") + dish
    return {
        "activity_id": str(segment.get("id") or ""),
        "place": place, "mode": mode, "stage": stage, "phase_at": phase.get("at"),
        "action": action[:120], "dish": dish, "meal_state": meal_state,
    }


def requested_scene(plan, prompt):
    # Explicit fields come from the same foreground photo request. Older callers
    # are supported through conservative extraction of concrete places and dishes.
    request = str(plan.get("owner_request") or "")
    place = _label(plan.get("photo_place"), 48)
    for text in (request, prompt):
        if not place and (match := _PLACE_HINT.search(text)):
            place = match[1]
    dish = (described_dish(request) or dish_label(plan.get("photo_dish"))
            or described_dish(prompt))
    meal_state = ""
    if dish:
        meal_state = "cooking" if re.search(r"(?:做|煮|炒)(?:一碗|一份|一盘)?" + re.escape(dish), request + " " + prompt) else "eating"
    elif _MEAL_PHOTO.search(request + " " + prompt):
        meal_state = "eating"
    return place, dish, meal_state


def same_place(wanted, actual):
    if not wanted or wanted in actual or actual and actual in wanted:
        return True
    if _HOME.search(wanted) and _HOME.search(actual):
        return wanted in ("家里", "住处") or actual in ("家里", "住处")
    # Generic dining-room language is compatible with a specific restaurant.
    return wanted in ("餐馆", "餐厅", "饭馆", "饭店") and bool(PLACE.search(actual))


def conflicts(binding, place, dish, meal_state):
    return (
        (place and not same_place(place, binding["place"]))
        or (dish and dish != binding["dish"])
        or (meal_state and (meal_state != binding["meal_state"] or not binding["dish"]))
    )


def seed_plan(place, dish, meal_state, last_place, now):
    place = place or last_place or "住处"
    mode = "home" if _HOME.search(place) else "out"
    if meal_state and not dish:
        options = ("葱油拌面", "番茄鸡蛋面", "牛肉盖饭", "青菜鸡蛋汤面")
        dish = options[int(now // 86400) % len(options)]
    if dish:
        reason = ("在" + place + ("做" if meal_state == "cooking" else "吃") + dish)
    else:
        reason = "在" + place + "拍一张随手照"
    plan = {
        "kind": "daylife", "mode": mode, "destination": place, "reason": reason,
        "prep_minutes": 0, "stay_minutes": 35 if dish else 15,
        "_photo_on_site": True,
    }
    if dish:
        plan["meal"] = {"dish": dish}
    return plan


def verified_review(binding, review):
    if not isinstance(review, dict):
        return False
    description = str(review.get("description") or "").strip()
    dish = binding["dish"]
    return (bool(description)
            and review.get("matches_scene") is True
            and review.get("matches_place") is True
            and review.get("matches_activity") is True
            and (not dish or (
                review.get("matches_dish") is True and dish in description
                and not re.search(r"(?:没有|没看到|不是|看不出).{0,5}" + re.escape(dish), description)
            )))


def can_deliver(record, current, *, archived_at=None):
    if not record or record.get("delivery_blocked"):
        return False
    binding = record.get("photo_activity")
    if not binding:
        return not (
            record.get("photo_scene") or record.get("review_kind") == "selfie"
            or record.get("image_mode") == "live" and record.get("subject") in ("mengyu", "scene", "", None)
        )
    archived = archived_at is not None and record.get("sharing_origin") == LIFE_PHOTO_ORIGIN
    if archived:
        if (
            record.get("kind") != "image"
            or record.get("image_mode") != "live"
            or record.get("shared_at")
            or record.get("delivery_status") in ("reserved", "sent", "uncertain")
            or record.get("retired_at")
            or not 0 <= archived_at - record.get("at", 0) <= LIFE_PHOTO_MAX_AGE
        ):
            return False
    return (
        record.get("activity_id") == binding.get("activity_id")
        and (archived or binding == current)
        and verified_review(record.get("photo_review_binding") or binding, record.get("image_review"))
    )


def caption(binding, *, archived=False):
    if archived:
        if binding["stage"] in ("去的路上", "回来的路上", "前往下一站"):
            return "这张是我在路上拍的，给你看看。"
        return f"这张是我在{binding['place']}拍的，给你看看。"
    if binding["dish"]:
        verb = "做" if binding["meal_state"] == "cooking" else "吃"
        return f"我在{binding['place']}{verb}{binding['dish']}。"
    return "给你看看这张。"


def answer(binding, user, draft, previous=None):
    """Return a deterministic correction for a current meal/place assertion."""
    if re.search(r"昨天|前天|上次|以前|刚才那张|上一张|吃了什么|吃了啥", user):
        return None
    is_meal_question = bool(_MEAL_QUESTION.search(user))
    if is_meal_question and re.search(r"我(?:现在|在|正)?吃(?:的|了)?(?:什么|啥)", user) and "你" not in user:
        is_meal_question = False
    if not binding:
        if previous and previous.get("dish") and is_meal_question:
            return "我这会儿没在吃东西。"
        return None
    if binding["dish"] and (is_meal_question or (
        _CURRENT_MEAL.search(draft or "") and (claimed := described_dish(draft))
        and claimed != binding["dish"]
    )):
        return caption(binding)
    asks_place = re.search(r"你.{0,5}(?:在哪|在哪家|在什么地方)", user) or (
        len(user.strip(" ？?。")) <= 7 and _PLACE_QUESTION.search(user)
    )
    claimed_place = _PLACE_HINT.search(draft or "") if re.search(r"我(?:现在|正)?在", draft or "") else None
    if (asks_place and not re.search(r"我在哪|怎么去|地图|哪家好|你知道|你觉得", user)) or (
        claimed_place and not same_place(claimed_place[1], binding["place"])
    ):
        return f"我现在在{binding['place']}。"
    return None
