"""Select truthful, infrequent activity notices from the existing timeline."""

import re

from data.plugins.mengyu_life.core.activities import ActivityStore
from data.plugins.mengyu_contact.policy import CLOSING, sleeping
from data.plugins.mengyu_contact.variety import is_ack

FRESH_SECONDS = 300
COOLDOWN = 1800
PROMISE_MAX_AGE = 86400
PROMISE_FRESH_SECONDS = 3600
NOTICE_HISTORY = 7 * 86400

# Common explicit promises use the same activity vocabulary as their evidence.
_ACTIONS = {
    "cook": r"做饭|煮饭|煮面|下厨|弄(?:早|午|晚)饭|烧菜|炒菜|准备(?:早|午|晚)餐",
    "wash": r"洗漱|洗澡|冲澡",
    "draw": r"画稿|画画|画(?:插画|贴纸|水彩)",
    "work": r"工作|写稿|改稿|赶稿|收(?:个)?尾|写材料|整理材料|处理稿件",
    "chores": r"打扫|收拾(?:房间|屋子)|洗衣服|整理房间",
}
_SHORT = re.compile(r"喝水|上厕所|拿(?:个)?快递|取(?:个)?快递|扔(?:个)?垃圾|下楼拿")
_ENDING = re.compile(
    r"晚点(?:再)?(?:找|聊|说|回)|回头(?:再)?聊|一会[儿]?(?:再)?聊|"
    r"等我(?:回来|一下)|先去|先忙|回来了.{0,4}跟你说|(?:不用|别)等我|"
    r"(?:我)?去(?:洗漱|洗澡|做饭|煮饭|煮面|下厨)(?:了|啦)(?:[。！!，,]|$)"
)
_PROMISE = re.compile(
    r"(?:^|[，,])\s*(?:等我|我)?"
    r"(?:(?:到|抵达)(?P<place>[^，,。！？?!“”]{0,24}?)|"
    r"(?:开始|开工)(?P<action>[^，,。！？?!“”]{0,18}?))"
    r"(?:了|的时候|以后|之后|后|时)?[，,]?\s*(?:我)?(?:就|再|会|一定)?"
    r"(?:告诉你|跟你说(?:一声)?|和你说(?:一声)?|叫你|给你发(?:个)?消息)"
    r"(?:一声|一条)?[的啦哦呀啊哈]*$"
)
_COMPLETION_PROMISE = re.compile(
    r"(?:^|[，,])\s*(?:等我|我)?(?P<verb>洗|冲|做|煮|画|写|改|打扫|收拾|整理|忙)"
    r"(?:完|好)(?:了)?(?P<object>[^，,。！？?!“”]{0,10}?)[，,]?\s*(?:我)?(?:就|再|会|一定)?"
    r"(?:告诉你|跟你说(?:一声)?|和你说(?:一声)?|叫你|给你发(?:个)?消息)"
    r"(?:一声|一条)?[的啦哦呀啊哈]*$"
)
_PLACE_HINT = re.compile(
    r"(?:^|[，,])\s*我(?:先|要|准备|打算|想|这就)?(?:出门)?去(?:一?趟)?"
    r"(?P<place>[^，,。！？?!“”]{2,20}?)(?:买[^，,]*|拿[^，,]*|逛逛|走走|看看|一趟)?$"
)


def _chain(segment):
    return str(segment.get("activity_chain_id") or segment.get("scheduler_task_id") or segment["id"])


def _action(text):
    if text in ("画", "开画"):
        return "draw"
    return next((name for name, pattern in _ACTIONS.items() if re.search(pattern, text)), "")


def _segments(segments):
    return [item for item in segments if isinstance(item, dict) and item.get("id")]


def _arrivals(segment):
    phases = segment.get("phases") or []
    arrivals = [phase for phase in phases if isinstance(phase, dict)
                and phase.get("stage") == "到了" and isinstance(phase.get("at"), (int, float))
                and phase.get("place")]
    # A visit slot begins at the destination even without a legacy arrival
    # phase. Travel and preparation slots do not qualify as visits.
    if (segment.get("source") == "daily_plan" and segment.get("mode") == "out"
            and not arrivals and phases):
        first = phases[0]
        place = segment.get("destination")
        if (isinstance(first, dict) and first.get("stage") == "正在做"
                and place and first.get("place") == place
                and isinstance(first.get("at"), (int, float))
                and not re.search(r"路上|通勤|回程|去程|往返|出门准备|出门化妆",
                                  str(segment.get("reason") or "") + " " + place)):
            arrivals.append(first)
    return arrivals


def _return_phase(segment):
    phases = segment.get("phases") or []
    if not phases or not isinstance(last := phases[-1], dict):
        return None
    if not isinstance(last.get("at"), (int, float)) or not last.get("place"):
        return None
    if last.get("stage") == "回来了":
        return last
    if (segment.get("source") == "daily_plan" and segment.get("mode") == "home"
            and last.get("stage") == "做完了" and last["place"] == "住处"
            and str(segment.get("reason") or "").strip() in ("回家", "到家", "回住处")):
        return last
    return None


def _status(segment, now):
    status, started, _, _ = ActivityStore._lifecycle_snapshot(segment, now)
    return status, started


def remember_promise(state, segments, text, identity, now):
    """Remember a clear first-person promise from a confirmed message."""
    sentences = re.split(r"[。！？!?\n]", text)
    for index, sentence in enumerate(sentences):
        sentence = sentence.strip()
        if re.search(r"[“”‘’「」]|(?:比如|例如|如果|要是|不一定|可能|不用|不会)", sentence):
            continue
        completion = _COMPLETION_PROMISE.search(sentence)
        arrival_or_start = _PROMISE.search(sentence)
        if not completion and not arrival_or_start:
            continue
        chain = None
        milestone = "started"
        if completion:
            verb, subject = completion.group("verb", "object")
            prior = sentence[:completion.start()].strip() or (sentences[index - 1].strip() if index else "")
            action = _action(verb + subject) or _action(prior)
            if not action and verb in ("洗", "冲") and not subject:
                action = "wash"
            if not action:
                continue
            active = [segment for segment in _segments(segments)
                      if _status(segment, now)[0] == "in_progress"
                      and _action(segment.get("reason") or "") == action]
            if len(active) == 1:
                chain = _chain(active[0])
            place, milestone = None, "finished"
        else:
            place, action = arrival_or_start.group("place", "action")
            if place in ("点", "整点", "时候", "时", "时间"):
                continue
            if place is not None:
                milestone = "arrived"
        if place in ("家", "家里", "住处"):
            place = "住处"
        if place in ("", "地方", "目的地", "那边", "那里"):
            prior = sentence[:arrival_or_start.start()].strip() or (sentences[index - 1].strip() if index else "")
            hint = _PLACE_HINT.search(prior)
            if hint:
                place = hint["place"]
            else:
                known = [(segment, phase) for segment in _segments(segments)
                         if _status(segment, now)[0] in ("planned", "in_progress")
                         for phase in _arrivals(segment) if phase["at"] > now]
                if len(known) != 1:
                    continue
                segment, phase = known[0]
                chain, place = _chain(segment), phase["place"]
        if action is not None and milestone == "started":
            action = _action(action)
            if not action:
                continue
        book = state.setdefault("activity_notice_promises", [])
        if any(row["id"] == identity or (
            row.get("place") == place and row.get("action") == action and row.get("chain") == chain
            and row.get("milestone", "arrived" if row.get("place") is not None else "started") == milestone
            and row["at"] <= now < row["at"] + PROMISE_MAX_AGE and not row.get("fulfilled")
        ) for row in book):
            continue
        book.append({
            "id": identity, "at": now, "place": place, "action": action, "chain": chain,
            "milestone": milestone,
        })
        del book[:-20]
        return


def allowed(state, projection, now):
    mengyu = projection.get("mengyu") or {}
    return bool(
        state.get("enabled", True)
        and (projection.get("owner") or {}).get("mode") == "open"
        and not state.get("conversation_paused")
        and not (mengyu.get("self_space") or {}).get("active")
        and not sleeping(projection, now)
        and (mengyu.get("life_clock") or {}).get("phase") != "sleeping"
    )


def _available(state, chain, now, reservation):
    records = state.get("activity_notices") or {}
    for row in records.values():
        if reservation and row.get("delivery_id") == reservation:
            continue
        if row["status"] == "confirmed" and now - row["at"] < COOLDOWN:
            return False
        if row["status"] == "reserved" and now - row["at"] < 60:
            return False
    row = records.get(chain)
    if not row:
        return True
    if reservation is not None and row.get("delivery_id") == reservation:
        return row["status"] == "reserved"
    return (row["status"] == "cancelled" or
            (row["status"] == "failed" and row["attempts"] < 2 and now - row["at"] >= 15))


def _notice(segment, text, at, promise=None):
    return {"chain": _chain(segment), "segment": str(segment["id"]), "text": text,
            "event_at": at, "promise": promise}


def _promise_notice(promise, segment, now):
    if promise.get("chain") and promise["chain"] != _chain(segment):
        return None
    status, started, completed, _ = ActivityStore._lifecycle_snapshot(segment, now)
    if promise.get("action") and promise["action"] == _action(segment.get("reason") or ""):
        if promise.get("milestone") == "finished":
            if status != "completed" or started is None or completed is None or completed < promise["at"]:
                return None
            lines = {
                "cook": "我做完饭了。",
                "wash": "我洗完澡了。" if "洗澡" in segment.get("reason", "") or "冲澡" in segment.get("reason", "") else "我洗漱完了。",
                "draw": "我画完了。", "work": "我忙完手头的活儿了。", "chores": "我忙完家务了。",
            }
            return _notice(segment, lines[promise["action"]], completed, promise["id"]) | {"finished": True}
        if started is not None and started >= promise["at"]:
            lines = {
                "cook": "我开始做饭了。",
                "wash": "我去洗漱啦。" if "洗漱" in segment.get("reason", "") else "我去洗澡啦。",
                "draw": "我开始画了。", "work": "我开始忙手头的活儿了。",
                "chores": "我开始忙家务了。",
            }
            return _notice(segment, lines[promise["action"]], started, promise["id"])
    if promise.get("place"):
        phases = segment.get("phases") or []
        stops = _arrivals(segment)
        return_phase = _return_phase(segment) if promise["place"] == "住处" else None
        if return_phase:
            stops.append(return_phase)
        for phase in stops:
            if phase["at"] < promise["at"] or promise["place"] not in phase["place"]:
                continue
            # An arrival applies only while she is still at the same stop.
            elapsed = [item for item in phases if isinstance(item, dict)
                       and isinstance(item.get("at"), (int, float)) and item["at"] <= now]
            returned = phase is return_phase and status == "completed"
            text = "我到家了。" if returned else f"我到{phase['place']}了。"
            return _notice(segment, text, phase["at"], promise["id"]) | {
                "left_stop": not elapsed or elapsed[-1] is not phase,
                "returned": returned,
            }

    return None


def promised(state, segments, now, reservation=None):
    segments = _segments(segments)
    for promise in state.get("activity_notice_promises", []):
        if promise.get("fulfilled") or not 0 <= now - promise["at"] < PROMISE_MAX_AGE:
            continue
        # Match the first occurrence after the promise. A later similar outing
        # cannot take over an expired or abandoned notification.
        matches = [(notice, segment) for segment in segments
                   if (notice := _promise_notice(promise, segment, now))]
        if not matches:
            continue
        notice, segment = min(matches, key=lambda item: item[0]["event_at"])
        still_home = notice.get("returned") and not any(
            item.get("created_at", 0) > notice["event_at"] and _status(item, now)[0] == "in_progress"
            for item in segments if item is not segment
        )
        if ((_status(segment, now)[0] == "in_progress" or still_home or notice.get("finished"))
                and not notice.get("left_stop")
                and 0 <= now - notice["event_at"] <= PROMISE_FRESH_SECONDS
                and _available(state, notice["chain"], now, reservation)):
            return notice
    return None


def _departure_text(segment, now):
    reason = str(segment.get("reason") or "")
    if _SHORT.search(reason):
        return ""
    remaining = (segment["phases"][-1]["at"] - now) / 60
    ending = "晚点聊。"
    for limit, estimate in ((17.5, "一刻钟"), (22.5, "二十分钟"), (27.5, "二十五分钟"),
                            (35, "半小时"), (45, "四十分钟"), (55, "五十分钟"),
                            (65, "一小时"), (75, "七十分钟"), (100, "一个半小时")):
        if remaining <= limit:
            ending = f"大概{estimate}。"
            break
    if segment.get("mode") == "out":
        place = str(segment.get("destination") or "")
        if place and len(place) <= 22 and not re.search(r"[\n。！？?，、；]", place):
            return f"我出门去{place}了，{ending}"
        return "我出门一趟，" + ending
    action = _action(reason)
    if action == "wash":
        verb = "洗漱" if "洗漱" in reason else "洗个澡"
        return f"我先去{verb}，{ending}"
    if action == "cook":
        return "我先去弄点吃的，" + ending
    if action in ("draw", "work") and re.search(r"画稿|赶稿|改稿|收尾", reason):
        return "我先忙会儿手上的稿子，晚点聊。"
    if action == "work" or segment.get("work"):
        return "我先忙会儿手头的活儿，晚点聊。"
    if action == "chores":
        return "我先忙会儿家务，" + ending
    return ""


def departure(state, segment, now, user_text, reply, *, since):
    if not segment or not segment.get("id") or segment.get("resumed_from_activity_id"):
        return None
    if (is_ack(user_text) or CLOSING.search(user_text) or _ENDING.search(user_text)
            or CLOSING.search(reply) or _ENDING.search(reply)
            or not 0 < len(reply.strip()) <= 240 or re.search(r"[?？][”’」』]*$", reply.strip())):
        return None
    status, started = _status(segment, now)
    if (status != "in_progress" or started is None
            or not max(since, now - FRESH_SECONDS) <= started <= now
            or segment["phases"][-1]["at"] - started < 900):
        return None
    previous = state.get("answered_tail") or {}
    # A substantive reply already delivered before she started, and a new
    # substantive user turn in the same short exchange, establish a live chat.
    user_at = state.get("last_user_ts", 0)
    chain = _chain(segment)
    prior_notice = (state.get("activity_notices") or {}).get(chain) or {}
    retrying_in_chat = (
        prior_notice.get("status") == "failed"
        and prior_notice.get("delivery_id") == previous.get("delivery_id")
        and previous.get("delivery") == "partial"
    )
    if ((previous.get("delivery") != "complete" and not retrying_in_chat) or not previous.get("reply")
            or is_ack(previous.get("user", "")) or CLOSING.search(previous.get("user", ""))
            or CLOSING.search(previous["reply"]) or _ENDING.search(previous["reply"])
            or not 0 < user_at - previous.get("user_ts", 0) <= FRESH_SECONDS
            or not user_at - FRESH_SECONDS <= previous.get("at", 0) <= user_at
            or (previous["at"] > started and not retrying_in_chat)
            or not 0 <= now - user_at <= FRESH_SECONDS):
        return None
    if not _available(state, chain, now, None):
        return None
    for promise in state.get("activity_notice_promises", []):
        if promise.get("fulfilled") or not 0 <= now - promise["at"] < PROMISE_MAX_AGE:
            continue
        if promise.get("chain") == chain or _promise_notice(promise, segment, now):
            return None
    sentence = _departure_text(segment, now)
    return _notice(segment, sentence, started) if sentence else None


def reserve(state, notice, delivery_id, index, now):
    rows = state.setdefault("activity_notices", {})
    old = rows.get(notice["chain"]) or {}
    rows[notice["chain"]] = {
        **notice, "delivery_id": delivery_id, "index": index,
        "status": "reserved", "at": now, "attempts": old.get("attempts", 0) + 1,
    }
    for key in list(rows):
        if rows[key]["at"] >= now - NOTICE_HISTORY:
            continue
        del rows[key]


def note_result(state, delivery_id, index, status, now, text=""):
    for row in (state.get("activity_notices") or {}).values():
        if (row["delivery_id"] != delivery_id or row["index"] != index
                or row["status"] != "reserved"):
            continue
        if status == "confirmed":
            # A first bubble, voice conversion or filtered tail is not evidence.
            status = "confirmed" if text.rstrip().endswith(row["text"]) else "cancelled"
        row.update(status=status, at=now)
        if status == "cancelled":
            row["attempts"] = max(0, row["attempts"] - 1)
        if status == "confirmed" and row.get("promise"):
            for promise in state.get("activity_notice_promises", []):
                if promise["id"] == row["promise"]:
                    promise["fulfilled"] = now
        return


def current_departure(notice, segment, now):
    """Check again before sending a delayed reply bubble."""
    return bool(
        segment and segment["id"] == notice["segment"]
        and _status(segment, now) == ("in_progress", notice["event_at"])
        and 0 <= now - notice["event_at"] <= FRESH_SECONDS
    )


def recover(state, now):
    recovered = False
    ledger = state.get("delivery_ledger") or {}
    for row in (state.get("activity_notices") or {}).values():
        if row["status"] != "reserved":
            continue
        delivery = ledger.get(row["delivery_id"]) or {}
        actual = (delivery.get("segments") or {}).get(str(row["index"])) or {}
        status = actual.get("status")
        note_result(
            state, row["delivery_id"], row["index"],
            status if status in ("confirmed", "failed", "cancelled") else "uncertain",
            actual.get("at", now), actual.get("text", ""),
        )
        recovered = True
    return recovered
