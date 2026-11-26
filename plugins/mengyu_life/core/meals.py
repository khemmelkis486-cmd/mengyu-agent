"""Shared takeout progress derived from orders and confirmed public delivery."""

import copy
import math
import re
from datetime import datetime

from data.plugins.mengyu_contact.local_clock import TZ

PHASES = {"shipping": 0, "delivered": 1, "eating": 2, "finished": 3}
LABELS = {
    "shipping": "配送中",
    "delivered": "已送达，未确认开始吃",
    "eating": "正在吃",
    "finished": "已吃完/喝完",
}
RULE = (
    "food订单表示配送的餐食，按已记录的订单推进。旧备饭计划不能盖过新订单。"
    "到达时间按arrives_at_local和minutes_until_arrival读，以记录采用的本地时间为准。"
    "外卖进度是当前统一依据，覆盖旧活动里已经过时的等餐描述。配送时间到只表示约定日常中的送达，"
    "不等于吃上或吃完；汤、饭等各自独立。已确认喝完汤不表示饭也吃完。"
    "已送达不再说等这份送到，已吃上/吃完不倒回尚未开始；第二份按不同订单分开。"
    "明确纠正刚才说错时可以修正，普通闲聊不清零。只在有新进展或被问起时自然提，不每轮播报。"
    "整顿饭结束与每样食物吃光是两回事：吃好了、不吃了或饭后去做别的，就让这顿饭结束。"
    "剩饭可以剩着，未确认吃完不是待办，不为了清空外卖再安排吃饭。已结束的饭不重开；"
    "只有当下明确又饿了、自己真想吃或下一顿饭时再考虑，不从剩余食物反推饥饿。"
    "外卖送达后如果超过90分钟仍未开始吃，要明确决定现在吃、留到下一顿或不吃；"
    "不要把一份已送达的晚饭悬挂两三个小时后，突然说刚买、刚到或刚开始吃。"
    "如果中间确实有工作、外出或休息安排，按已有时间线说明，不补造理由。"
)


def meal_context(state, orders, now):
    """Read recent orders and independently confirmed component progress.

    Args:
        state: Persisted companion state.
        orders: Existing gift ledger entries.
        now: Current Unix time.

    Returns:
        Current food orders, with delivery timing separated from consumption.
    """
    result = []
    for order in orders:
        if order.get("kind") != "food" or not 0 <= now - order.get("at", 0) <= 21600:
            continue
        arrival = order["at"] + order.get("eta_minutes", 15) * 60
        baseline = "delivered" if now >= arrival else "shipping"
        saved = (state.get("meal_progress") or {}).get(str(order["id"]), {})
        components = []
        names = order.get("ingredients")
        if (
            not isinstance(names, list)
            or not names
            or not all(isinstance(x, str) for x in names)
        ):
            names = re.split(r"[、，,+＋]", str(order.get("item", "")))
        for name in names:
            name = re.sub(r"(?:一|两|二|\d+)(?:份|碗|杯|盒)$", "", name.strip())
            if not name:
                continue
            aliases = [name]
            if name.endswith("鸡煲"):
                aliases.extend(["鸡煲", "砂锅鸡"])
            elif name.endswith("砂锅鸡"):
                aliases.append("砂锅鸡")
            ending = re.search(r"(米饭|汤|粥|羹|面)$", name)
            if ending:
                aliases.append(ending[1])
                if ending[1] == "米饭":
                    aliases.append("饭")
            progress = saved.get(name, {})
            stored_phase = progress.get("phase", "unset")
            phase = stored_phase if stored_phase in PHASES else baseline
            if phase not in PHASES or PHASES[phase] < PHASES[baseline]:
                phase = baseline
            components.append(
                {
                    "name": name,
                    "stored_phase": stored_phase,
                    "effective_phase": phase,
                    "aliases": list(set(aliases)),
                    "phase": phase,
                    "status": LABELS[phase],
                    "evidence": progress.get("evidence", ""),
                }
            )
        ended = state.get("meal_endings", {}).get(str(order["id"]))
        started = [
            p.get("started_at", p.get("at", 0))
            for p in saved.values()
            if p.get("phase") in ("eating", "finished")
            and p.get("started_at", p.get("at", 0)) > 0
        ]
        if not ended and started and now >= min(started) + 30 * 60:
            ended = {
                "at": min(started) + 30 * 60,
                "source": "meal-session-elapsed",
                "evidence": "已确认开始用餐后经过约半小时，结束本次用餐；不表示所有食物吃光，剩下的不是待办",
            }
        for component in components:
            component["currently_eating"] = not ended and component["phase"] == "eating"
            if ended and component["phase"] != "finished":
                component["status"] = "本次用餐已结束；该食物剩余量未确认，不是待办"
        result.append(
            {
                "order_id": str(order["id"]),
                "item": order.get("item"),
                "ordered_at": order["at"],
                "arrives_at": arrival,
                "ordered_at_local": datetime.fromtimestamp(
                    order["at"], TZ
                ).strftime("%Y-%m-%d %H:%M"),
                "arrives_at_local": datetime.fromtimestamp(
                    arrival, TZ
                ).strftime("%Y-%m-%d %H:%M"),
                "minutes_until_arrival": max(0, math.ceil((arrival - now) / 60)),
                "minutes_since_order": max(0, int((now - order["at"]) / 60)),
                "minutes_since_arrival": max(0, int((now - arrival) / 60)) if now >= arrival else 0,
                "meal_delay_warning": bool(
                    now >= arrival + 90 * 60
                    and not any(p.get("phase") in ("eating", "finished") for p in saved.values())
                    and not ended
                ),
                "delivery_phase": baseline,
                "components": components,
                "meal_ended": bool(ended),
                "meal_end": ended,
                "remaining_is_task": False,
            }
        )
    return result[-3:]


def settle_meal_sessions(state, orders, now):
    """Persist elapsed eating sessions without claiming every component was eaten."""
    changed = False
    for view in meal_context(state, orders, now):
        ending = view.get("meal_end") or {}
        if ending.get("source") == "meal-session-elapsed" and view[
            "order_id"
        ] not in state.get("meal_endings", {}):
            state.setdefault("meal_endings", {})[view["order_id"]] = ending
            changed = True
    return changed


def record_meal_end(state, orders, text, now, source):
    """Close a meal without claiming that all its components were consumed.

    Args:
        state: Mutable persisted companion state.
        orders: Food ledger entries.
        text: Delivered speech or accepted fictional daylife evidence.
        now: Evidence timestamp.
        source: Provenance of the evidence.

    Returns:
        Whether the unique current meal was closed.
    """
    if re.search(r"[？?]|你|昨天|明天|如果|等会|打算|还没|没吃完|吃完再|吃完就", text):
        return False
    if not re.search(
        r"(?:早饭|午饭|晚饭|这顿饭|这顿|饭)(?:已经|刚刚|刚|也)?(?:吃完了|吃好了|结束了)|(?:我)?吃好了|不吃了|(?:早|午|晚)饭后",
        text,
    ):
        return False
    current = [
        item for item in meal_context(state, orders, now) if item["arrives_at"] <= now
    ]
    if len(current) != 1:
        return False
    key = current[0]["order_id"]
    if key in state.get("meal_endings", {}):
        return False
    state.setdefault("meal_endings", {})[key] = {
        "at": now,
        "evidence": text[:300],
        "source": source,
    }
    return True


def meal_reopening(context, text):
    """Identify eating again solely to finish leftovers from an ended meal.

    Args:
        context: Current food orders with meal-level closure.
        text: Candidate activity or public clause.

    Returns:
        Whether the candidate reopens a meal without a fresh appetite or occasion.
    """
    ended = [item for item in context if item.get("meal_ended")]
    active = [item for item in context if not item.get("meal_ended")]
    named_ended = any(
        c["name"] in text for item in ended for c in item.get("components", [])
    )
    named_active = any(
        c["name"] in text for item in active for c in item.get("components", [])
    )
    if active and (named_active or not named_ended):
        return False
    return bool(
        any(item.get("meal_ended") for item in context)
        and re.search(r"热.{0,5}饭|吃几口|吃点|吃饭|吃掉|吃完|继续吃|慢慢吃", text)
        and re.search(r"剩|放凉|可惜|送到|外卖|收.{0,2}尾", text)
        and not re.search(
            r"又饿|肚子饿|有点饿|下一顿|明天|早饭|午饭|夜宵|不吃了|不再吃|不用.{0,6}吃",
            text,
        )
    )


def meal_claims(context, text):
    """Resolve conservative first-person food claims to unambiguous components.

    Args:
        context: Current component views.
        text: The assistant's public text, never a user's message.

    Returns:
        Component-scoped phase claims with their exact public evidence.
    """
    if re.search(r"```|日记|小说|故事|举例|假设|引用", text):
        return []
    correction = bool(re.search(r"刚才说错|刚才说早|更正一下|更正刚才", text))
    claims = []
    active = [
        order
        for order in context
        if not order.get("meal_ended") and order.get("delivery_phase") == "delivered"
    ]
    for chunk in re.split(r"(?<=[，,。！？!?；;\n])", text):
        if re.search(r"[?？]", chunk):
            continue
        clause = chunk.strip("，,。！!；; \n")
        if not clause.strip() or re.search(
            r"你(?:已经|刚|也)?(?:把|吃|喝)|^你(?!点的|买的|送的).{0,12}(?:吃|喝)|刚才.{0,8}(?:吃|喝)(?:着|的时候)|你说|他说|昨天|上次|以前|假如|如果|下次|下一份|再点|[“”\"`]",
            clause,
        ):
            continue
        checked = re.sub(r"不用(?:再)?等|不必(?:再)?等|不再等|不是在等", "", clause)
        phase = None
        if re.search(
            r"还(?:没|未)(?:有)?(?:送到|到)|等.{0,18}(?:送到|到了)|送到.{0,8}(?:就|再).{0,6}(?:尝|吃|喝)",
            checked,
        ):
            phase = "shipping"
        elif re.search(r"还没.{0,3}(?:吃|喝)完", clause):
            phase = "eating"
        elif re.search(
            r"等会|待会|一会儿|打算|准备|想把|想先|要把", clause
        ) or re.search(
            r"还没.{0,3}(?:吃|喝|尝)|等.{0,3}(?:吃|喝)|准备(?:吃|喝)", clause
        ):
            phase = "delivered"
        elif not re.search(r"没|未|不|别|要把|想先", clause):
            if re.search(r"(?:吃|喝)(?:完|光)", clause):
                phase = "finished"
            elif (
                re.search(
                    r"正在(?:吃|喝)|(?:吃|喝)(?:上了|着)|开始(?:吃|喝)|刚(?:吃|喝)上",
                    clause,
                )
                or re.search(
                    r"(?:刚|已经)?(?:尝|吃|喝)(?:过了|了一口|了两口|了几口)", clause
                )
                or re.search(r"第一口(?:下去|咬下去|吃下去|喝下去)", clause)
            ):
                phase = "eating"
        if phase is None:
            continue
        exact, generic = [], []
        for order in context:
            for component in order["components"]:
                pair = (order, component)
                if component["name"] in clause:
                    exact.append(pair)
                elif any(alias in clause for alias in component["aliases"]):
                    generic.append(pair)
        targets = exact or generic
        if (
            not targets
            and len(active) == 1
            and (
                "外卖" in clause
                or (
                    len(active[0]["components"]) == 1
                    and re.fullmatch(
                        r"(?:好[，, ]*)?(?:我)?(?:刚|已经)?(?:(?:开始|正在)(?:吃|喝)(?:饭)?(?:呢|啦|了)?|(?:尝|吃|喝)(?:过了|了一口|了两口|了几口)(?:呢|啦)?|送到.{0,8}(?:就|再).{0,6}(?:尝|吃|喝).{0,4}|还没(?:吃|喝|尝)(?:过)?(?:呢|啦)?)",
                        clause,
                    )
                )
                or re.fullmatch(
                    r"\s*(?:我|晚饭|这份饭)?(?:刚|已经|也|都)?吃完(?:了|啦|咯)?\s*",
                    clause,
                )
            )
        ):
            targets = [(active[0], c) for c in active[0]["components"]]
        if len({o["order_id"] for o, _ in targets}) > 1:
            continue
        for order, component in targets:
            claims.append(
                {
                    "order_id": order["order_id"],
                    "name": component["name"],
                    "phase": phase,
                    "previous": component["phase"],
                    "evidence": clause.strip(),
                    "correction": correction,
                }
            )
    return claims


def record_meal_delivery(state, orders, text, now, delivery_id):
    """Commit only progress supported by confirmed delivered assistant text.

    Args:
        state: State to update under the companion lock.
        orders: Current gift ledger entries.
        text: Actually delivered text or the transcript of delivered voice.
        now: Delivery time.
        delivery_id: Source delivery identity for provenance.

    Returns:
        Whether a component progress record changed.
    """
    changed = record_meal_end(state, orders, text, now, delivery_id)
    for claim in meal_claims(meal_context(state, orders, now), text):
        phase = claim["phase"]
        previous = (
            (state.get("meal_progress") or {})
            .get(claim["order_id"], {})
            .get(claim["name"], {})
            .get("phase", claim["previous"])
        )
        if claim["correction"] and phase != "finished":
            # A clear correction may reopen the meal-level state as well as a component.
            if state.setdefault("meal_endings", {}).pop(claim["order_id"], None) is not None:
                changed = True
        if previous == "shipping" or phase == "shipping" or phase == previous:
            continue
        if not claim["correction"] and re.search(
            r"没|未|不|别|等会|待会|一会儿|打算|准备|想把|要把", claim["evidence"]
        ):
            continue
        if PHASES[phase] < PHASES[previous] and not claim["correction"]:
            continue
        order = state.setdefault("meal_progress", {}).setdefault(claim["order_id"], {})
        if order.get(claim["name"], {}).get("at", 0) > now:
            continue
        order[claim["name"]] = {
            "phase": phase,
            "at": now,
            "evidence": claim["evidence"],
            "delivery_id": delivery_id,
            "correction": claim["correction"],
            "started_at": order.get(claim["name"], {}).get(
                "started_at",
                order.get(claim["name"], {}).get("at", now)
                if order.get(claim["name"], {}).get("phase") in ("eating", "finished")
                else now,
            ),
        }
        changed = True
    return changed


def restore_delivered_meals(state, orders):
    """Replay confirmed proactive speech only; ordinary drafts are not evidence."""
    history = [
        row
        for row in state.get("proactive_history", [])
        if isinstance(row, dict)
        and row.get("role") == "assistant_proactive"
        and isinstance(row.get("at"), (int, float))
        and row["at"] > 0
        and isinstance(row.get("text"), str)
    ]
    changed = 0
    for row in sorted(history, key=lambda item: item["at"]):
        changed += bool(
            record_meal_delivery(
                state,
                orders,
                row["text"],
                row["at"],
                "historical-proactive:" + str(row["at"]),
            )
        )
    return changed


def meal_regressions(state, orders, text, now):
    """Find backwards or premature consumption claims in proposed public text.

    Args:
        state: Persisted progress.
        orders: Current gift ledger entries.
        text: Proposed assistant public text.
        now: Current time.

    Returns:
        Claims that conflict with the current progress.
    """
    context = meal_context(state, orders, now)
    conflicts = [
        claim
        for claim in meal_claims(context, text)
        if not claim["correction"]
        and (
            PHASES[claim["phase"]] < PHASES[claim["previous"]]
            or claim["previous"] == "shipping"
            and PHASES[claim["phase"]] >= 2
        )
    ]
    if meal_reopening(context, text):
        conflicts.append(
            {
                "order_id": "ended-meal",
                "name": "整顿饭",
                "phase": "eating",
                "previous": "finished",
                "correction": False,
                "evidence": text,
            }
        )
    conflicts.extend(historical_conflicts(orders, text, now))
    return conflicts


def meal_life_context(state, orders, context, now):
    """Remove obsolete food clauses while preserving the actual activity timeline.

    Args:
        state: Current progress.
        orders: Food orders.
        context: Existing virtual activity snapshot.
        now: Current time.

    Returns:
        A copy with obsolete reason/detail clauses removed.
    """
    result = copy.deepcopy(context or {})
    if not result:
        return result
    if isinstance(result.get("recorded_mood"), dict):
        result["recorded_mood"] = meal_life_context(
            state, orders, result["recorded_mood"], now
        )
    if isinstance(result.get("newer_experiences"), list):
        result["newer_experiences"] = [
            meal_life_context(state, orders, item, now)
            if isinstance(item, dict)
            else item
            for item in result["newer_experiences"]
        ]
    food = meal_context(state, orders, now)
    for field in ("reason", "detail", "wish", "carryover"):
        if field not in result:
            continue
        text = result.get(field, "")
        ended_reference = any(
            view.get("meal_ended")
            and (
                re.sub(
                    r"(?:一|两|二|\d+)(?:份|碗|杯|盒)$", "", str(view.get("item", ""))
                )
                in text
                or any(c["name"] in text for c in view["components"])
            )
            for view in food
        )
        kept = []
        for part in re.split(r"(?<=[，,。！？!?；;\n])", text):
            if meal_reopening(food, part):
                continue
            if (
                ended_reference
                and re.search(r"打算.{0,8}吃|打开.{0,8}吃|没开始", part)
                and not re.search(r"明天|下一顿|又饿", part)
            ):
                continue
            ended_food = any(
                view.get("meal_ended")
                and (
                    re.sub(
                        r"(?:一|两|二|\d+)(?:份|碗|杯|盒)$",
                        "",
                        str(view.get("item", "")),
                    )
                    in part
                    or any(c["name"] in part for c in view["components"])
                )
                for view in food
            )
            if ended_food and re.search(
                r"送到|没开始|打算|准备|打开|慢慢吃|继续吃|吃完|吃几口|趁热|还在吃|先吃",
                part,
            ):
                continue
            unsupported = any(
                claim["phase"] == "finished" and claim["previous"] != "finished"
                for claim in meal_claims(food, part)
            )
            if re.search(r"(?:早饭|午饭|晚饭|这顿饭)吃完了", part) and any(
                item.get("meal_ended") for item in food
            ):
                unsupported = False
            if not unsupported and not meal_regressions(state, orders, part, now):
                kept.append(part)
        result[field] = "".join(kept).strip("，,。 ")
    return result

def _normalise_food_name(value):
    """Normalise order labels so quantity suffixes do not defeat history checks."""
    name = re.sub(r"\s+", "", str(value or ""))
    name = re.sub(r"(?:一|两|二|\d+)(?:份|碗|杯|盒|个)$", "", name)
    return name.strip("，,。；;：:")


def historical_conflicts(orders, text, now):
    """Catch claims that treat a long-finished order as current food.

    Order labels may contain a quantity suffix (for example ``一份``), while
    natural conversation normally omits it. Match the normalised dish name and
    component names rather than requiring the raw ledger label verbatim.
    """
    message = str(text or "")
    if not re.search(r"送到|到了|送来|吃|喝|热一下|还热", message):
        return []
    conflicts = []
    for order in orders:
        if order.get("kind") != "food":
            continue
        if now - order.get("at", 0) <= 21600:
            continue
        candidates = [_normalise_food_name(order.get("item", ""))]
        ingredients = order.get("ingredients")
        if isinstance(ingredients, list):
            candidates.extend(_normalise_food_name(item) for item in ingredients)
        candidates = list(dict.fromkeys(name for name in candidates if len(name) >= 2))
        matched = next((name for name in candidates if name in message), "")
        if matched:
            conflicts.append(
                {
                    "order_id": str(order.get("id")),
                    "name": matched,
                    "phase": "historical",
                    "previous": "finished",
                    "evidence": matched,
                    "correction": False,
                }
            )
    return conflicts
