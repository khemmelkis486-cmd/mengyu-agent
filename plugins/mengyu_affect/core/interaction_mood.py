"""Evidence requirements, expiry and repair for interaction mood."""

import re

TYPES = frozenset(("hurt", "disappointed", "conflict"))
MODES = frozenset(("off", "observe", "reply", "full"))
ACK = re.compile(r"(?:嗯+|哦+|噢+|好|行|随便|都行|不知道|知道了)[。！!~～\s]*$")
LIGHT_ACK = re.compile(r"(?:嗯+|哦+|噢+|随便|都行|不知道)[。！!~～\s]*$")
PLAY = re.compile(r"哈哈|嘿嘿|嘻嘻|开玩笑|逗你|略略|狗头|滑稽|\[QQ表情")
BUSY = re.compile(r"(?:我|这会儿|现在)?(?:正忙|在忙|要睡|去睡|工作呢|开会|有事|先忙|晚点(?:再)?聊|待会(?:儿)?聊)")
DISMISS = re.compile(r"^(?:我觉得)?(?:你想多了|没必要(?:吧)?|没那个必要|你太敏感了|别矫情|你(?:这样|说得)不对|不对)[。！!\s]*$")
STRONG = re.compile(r"别烦我|你(?:真|太|真的|就是)?(?:烦人|没意思|无聊|矫情)|你怎么这么烦")
BROKEN = re.compile(r"还没(?:做|弄|买|送|交|看|寄)|忘了|没做|没弄|不做了|再说吧|改天吧|没来得及")
APOLOGY = re.compile(r"对不起|抱歉|我错了|是我不好|刚才我不该|刚刚我不该")
EXPLANATION = re.compile(r"刚(?:才|刚).{0,12}(?:在忙|在开会|没看到|没听清)|我不是那个意思|我有认真看|我是说.{4,40}")
_PERSONAL = re.compile(r"我(?:觉得|想|喜欢|不喜欢|希望|介意|有点|刚才|今天)|你觉得|你想|你愿意|你会")
_FEELING = re.compile(r"我的(?:感受|喜好)|我.{0,6}(?:不舒服|不开心|难过|失落|喜欢|介意|生气|失望|在意|希望)")
_QUESTION = re.compile(r"你.{0,16}(?:觉得|选|怎么看|要不要|想|方便|愿意|喜欢|为什么|会)|(?:哪个|哪一款|怎么样|怎么想|为什么|想听你)")
_LABELS = {"hurt": "有点失落", "disappointed": "有点失望", "conflict": "有点不高兴"}


def applicable(context):
    return (
        context.get("owner_mode") == "open"
        and not context.get("self_space")
        and not context.get("sleeping")
        and not context.get("quoted")
    )


def _signal(kind, strength, cause, **fields):
    return {"type": kind, "intensity": strength, "cause": cause, **fields}


def _overdue_item(text, previous, commitments, now):
    if not BROKEN.search(text):
        return None
    for item in commitments:
        due = item.get("due_at")
        if (item.get("promisor") != "user" or item.get("status") not in ("promised", "pending", "in_progress", "active", "confirmed", "acknowledged")
                or not isinstance(due, (int, float)) or due >= now):
            continue
        name = str(item.get("title") or "")
        key = re.sub(r"^关于|的约定$", "", name)
        if (len(key) >= 2 or key in ("书", "花")) and (key in text or key in previous):
            return item
    return None


def appraise(user_text, context):
    """Decide from one actual turn and the preceding confirmed assistant message."""
    text = str(user_text or "").strip()
    prior = str(context.get("reply") or "")
    now, at = context["now"], context.get("at", 0)
    if (not text or text.startswith("/") or context.get("quoted")
            or PLAY.search(text) or re.search(r"“|”|「|」|比如|例如|引用", text)):
        return None
    active = context.get("active") or {}
    if active and not re.search(r"才怪|行了吧|总行了吧", text):
        if APOLOGY.search(text) or EXPLANATION.search(text):
            return {"resolve": "听到了道歉或针对刚才的解释"}
    if not applicable(context) or BUSY.search(text):
        return None
    item = _overdue_item(text, prior, context.get("commitments", ()), now)
    if item:
        return _signal("disappointed", .7, "之前说定的事过了约定时间，刚才也没有明确结果",
                       commitment_id=str(item.get("id") or ""))
    if STRONG.search(text) and "？" not in text and "?" not in text and not re.search(r"不是|不觉得|他说|她说", text):
        return _signal("conflict", .82, "刚才那句直接嫌弃的话让我不舒服")
    if (0 <= now - at <= 1800 and prior and DISMISS.fullmatch(text) and not PLAY.search(prior)
            and (_FEELING.search(prior) or text.startswith(("你想多了", "别矫情", "你太敏感了")))):
        return _signal("conflict", .45, "刚说的感受或看法就被一句话否定了")
    if prior and not PLAY.search(prior) and ACK.fullmatch(text):
        if 1200 <= now - at <= 14400 and (("？" in prior or "?" in prior) and _QUESTION.search(prior)
                                            or _PERSONAL.search(prior) and len(prior) >= 16):
            return _signal("disappointed", .62, "刚才认真说的那件事只等到一句很短的回应")
        if 0 <= now - at < 1200 and LIGHT_ACK.fullmatch(text) and ("？" in prior or "?" in prior) and _QUESTION.search(prior):
            return _signal("hurt", .35, "刚才问了一个具体问题，还不知道你的想法")
    return None


def observed_silence(context):
    """A missed answer can leave a feeling, without assuming an intent."""
    reply = str(context.get("reply") or "")
    elapsed = context["now"] - context.get("at", 0)
    if (applicable(context) and context.get("delivery_id") and context.get("unanswered")
            and 5400 <= elapsed <= 14400 and ("？" in reply or "?" in reply)
            and _QUESTION.search(reply) and _PERSONAL.search(reply) and not PLAY.search(reply)):
        return _signal("hurt", .28, "刚才问的事还没有下文", silence=True)
    return None


def view(state, now):
    active = state.get("interaction_mood") or {}
    if (state.get("interaction_mood_mode", "observe") == "off" or active.get("resolved")
            or active.get("type") not in TYPES or not active.get("since", 0) <= now < active.get("expires_at", 0)):
        return None
    remaining = (active["expires_at"] - now) / (active["expires_at"] - active["since"])
    return {**active, "intensity": round(active["intensity"] * (.5 + .5 * remaining), 2)}


def apply(state, signal, source_id, now):
    if not signal:
        return False
    existing = state.get("interaction_mood") or {}
    if signal.get("resolve"):
        if not view(state, now):
            return False
        existing.update(resolved=True, resolved_at=now, resolution=signal["resolve"])
        return True
    if existing.get("source_id") == source_id:
        return False
    kind, strength = signal["type"], min(.85, signal["intensity"])
    if view(state, now) and now - existing["since"] < 900 and strength <= existing["intensity"]:
        return False
    length = 8 * 3600 if strength >= .8 else 3 * 3600 if strength >= .6 else 45 * 60
    state["interaction_mood"] = {
        **signal, "type": kind, "intensity": strength, "since": now, "expires_at": now + length,
        "source_id": source_id, "resolved": False, "reply_offered_at": None,
    }
    return True


def deltas(intensity):
    return {"valence": -18 * intensity, "stress": 14 * intensity, "social_need": -13 * intensity}


def guidance(mood, *, proactive=False):
    if not mood:
        return ""
    label = _LABELS[mood["type"]]
    if proactive:
        return f"现在{label}，想主动撒娇的劲头可以低一些。照常过自己的生活，有想分享的事可以聊。不要为这件事主动追问或催回复。"
    result = f"现在{label}。可以简短接话、坚持自己的想法，亲昵称呼少一点。确实不想聊这件事时可以直接说。认真求助照常回答。无需为了收尾先道歉或假装已经和好。"
    if mood.get("commitment_id"):
        result += "这件事可以等有实际结果再恢复期待。"
    if not mood.get("reply_offered_at") and not mood.get("silence"):
        return result + f"如果眼下还在聊同一件事，可以说一次为什么不高兴，原因是{mood['cause']}。"
    return result + "这件事留在当前话题就好，不反复提，也不用让他给交代。"
