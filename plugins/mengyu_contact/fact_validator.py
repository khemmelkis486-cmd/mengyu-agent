"""Extract verifiable Chinese claims and compare them with scoped evidence."""

import re
import time
from datetime import datetime, timedelta

from .local_clock import TZ

SUPPORTED = "SUPPORTED"
CONTRADICTED = "CONTRADICTED"
UNSUPPORTED = "UNSUPPORTED"
NOT_CHECKABLE = "NOT_CHECKABLE"
RULE_VERSION = "1.56.0"
GUIDANCE = (
    "\n【事实】可以自然表达情绪、打算和虚构日常。"
    "说用户做了什么要有他亲口确认，动作和时间都要吻合。"
    "你正在做的事以当前活动为准。计划、暂停、完成、已发出、收到各是不同状态。"
    "复诊按事件记录推进；生成成果和图片不代表已经发给对方。语境不确定时接着聊，不猜结果。"
)

TIME = r"今天早些时候|刚刚|刚才|前天|今天|昨天|昨晚|早些时候|之前|刚"
TIME_MOD = r"(?:(?:只|就|也|还|又|确实)?(?:已经|并没有|还没|没有|并没|没|未曾|未)?(?:刚刚|刚才|刚)?)"
OWNER_ACTIONS = {
    "点了": "order", "点过": "order", "下单了": "order",
    "吃了": "eat", "吃过": "eat", "喝了": "drink", "喝过": "drink",
    "买了": "buy", "买过": "buy", "买的": "buy", "买好了": "buy",
    "买到": "buy", "购买了": "buy",
    "去了": "visit", "去过": "visit", "到过": "visit",
    "收到了": "receive", "收到": "receive", "拿到了": "receive",
    "订了": "book", "订过": "book",
    "发给我了": "send", "送出去了": "send", "寄出了": "send",
    "发了": "send", "送了": "send", "寄了": "send",
    "做完了": "finish", "做完": "finish",
    "完成了": "finish", "完成": "finish", "交了": "submit",
}
OWNER_VERBS = "|".join(sorted(OWNER_ACTIONS, key=len, reverse=True))
BARE_VERBS = "下单|购买|吃|喝|点|买|去|收到|拿到|订|寄出|发|寄|送|做完|完成|交"
NEGATE = re.compile(r"没有|还没|并没|没|未曾|未")
OWNER_RE = re.compile(
    rf"^(?P<time_before>{TIME})?(?P<who>我|你)(?P<when>{TIME})?"
    rf"(?P<mod>{TIME_MOD})(?P<verb>{OWNER_VERBS}|{BARE_VERBS})(?P<obj>.+)$"
)
BARE_ACTIONS = {
    "下单": "order", "点": "order", "吃": "eat", "喝": "drink", "买": "buy",
    "购买": "buy", "去": "visit", "拿到": "receive", "订": "book",
    "发": "send", "寄": "send", "寄出": "send", "送": "send", "交": "submit",
}
OWNER_ACTIONS.update(BARE_ACTIONS)
SELF_RE = re.compile(
    rf"^(?:我|梦雨)(?P<when>{TIME}|现在|这会儿|此刻)?"
    r"(?P<mod>已经|还在|正在|正|在|没有|还没|没)?(?P<body>.+)$"
)
ELLIPSIS_RE = re.compile(r"^(?:我|梦雨)?(?:已经|刚才|刚刚|刚)?(?:从[^，。]{1,35}回来|到家了|回家了)$")
ACTION_GROUPS = {
    "cook": "做饭|炒菜|煮饭|做菜", "eat": "吃饭|吃东西|吃",
    "wash": "洗澡|洗漱", "sleep": "睡觉|睡着|入睡",
    "shop": "逛街|购物", "outing": "外出|出门|在路上",
    "commute": "开车|坐车", "school": "上课", "meeting": "开会",
    "writing": "写稿|起稿|改稿|写文案|写文章",
    "coding": "写代码|编程", "drawing": "画画|画稿|画图|画插画",
    "gaming": "打游戏|玩游戏", "reading": "看书|读书",
    "music": "听歌|听音乐", "exercise": "健身|跑步",
    "work": "工作|做项目|赶稿|画海报|设计海报|排版",
    "housework": "做家务|收拾房间",
}
ACTIONS = {action: re.compile(words) for action, words in ACTION_GROUPS.items()}
LOCATION_GROUPS = {
    "kitchen": ("厨房", "灶台"),
    "home": ("家里", "在家", "住处", "住宅", "卧室", "客厅", "家中", "回家", "到家"),
    "restaurant": ("餐馆", "饭店", "餐厅", "肯德基", "麦当劳", "店里吃饭"),
    "office": ("公司", "办公室", "工位"),
    "school": ("学校", "教室", "图书馆"),
    "cafe": ("咖啡馆", "咖啡店", "咖啡厅"),
    "store": ("便利店", "书店"),
    "outside": ("外面", "街上", "商场", "超市", "路上"),
}
QUOTE = re.compile(r"“[^”]*”|「[^」]*」|『[^』]*』|\"[^\"\n]*\"|《[^》]*》")
QUOTE_CONTEXT = re.compile(r"(?:说|问|引用|转发|原话|听说|假设)[:：]?\s*$")
NONFACT = re.compile(
    r"^(?:要是|如果|假如|假设|假装|例如|比如|听说|有人说|转发|他说|她说|刚想|"
    r"我(?:刚才|刚刚|刚|现在|这会儿)?(?:还)?(?:想|觉得|感觉|希望|准备|打算|计划|本来想|差点|好想|有点想|可能|也许|应该)|"
    r"你(?:是不是|有没有|会不会|想不想|要不要|觉得|可能|也许|应该|该不会|别|先|明天|下次|打算|想))"
)
QUESTION = re.compile(r"^(?:是不是|有没有|会不会|难道)|(?:吗|么|没|没有|对吧|是不是|对不对|怎么了)[啊呀呢]?$")
CONNECTOR = re.compile(r"^(?:其实|准确说|说错了|等等|不过|但是|可我|然后|而且|另外|幸好|倒是|对了|刚才说错了)[：: ]*")
CORRECTION = re.compile(r"^(?:其实)?(?:是|不是我[，,是]*)?(?:我)?(?:朋友|同事|家人|室友|爸妈|别人|他|她)(?:刚)?(?P<verb>买|点|订|吃|喝|收|发|送)的(?P<obj>.*)$")
TRAIL = re.compile(r"(?:了|啦|呢|呀|啊|嘛|哦|哈|吧)+$")
MEDIA_NAMES = re.compile(r"自拍|照片|图片|图|表情包|语音|文件|链接|视频|海报|封面|稿子")
MEDIA_LEAD = re.compile(r"^(?:这[份张个]|那[份张个])?(?:自拍|照片|图片|图|表情包|语音|文件|链接|视频|海报|封面|稿子)(?:我|梦雨|已发|已经发)")
TRANSMIT = re.compile(r"(?:已经|刚(?:刚|才)?)?(?:发(?:送)?(?:给你|你了|出去|过来|上去)|传给你|发了|发送了|寄给你)(?:了|啦|哈)?")
FRIEND_DONE = re.compile(
    r"见了(?:面)?|见过(?:面)?|见到了|见完|聚过|聚了|聊过|聊完|聊了|"
    r"(?:吃|喝|看|逛|走)(?:过|了|完).{0,8}|(?:吃饭|喝咖啡|看展|逛街|散步|看电影|逛书店)(?:结束了|了|回来)"
)
FRIEND_BOOKED = re.compile(r"约好了?|约定了|定好了?|约上了|已经约了|答应了")
FRIEND_ACTIVITY = {
    "meal": re.compile(r"吃|饭|聚餐"), "cafe": re.compile(r"咖啡"),
    "exhibition": re.compile(r"展"), "shopping": re.compile(r"逛街|商场|买衣"),
    "walk": re.compile(r"散步|公园"), "cinema": re.compile(r"电影"), "bookstore": re.compile(r"书店"),
}
DOMAIN_CLAIMS = (
    ("work_finished", re.compile(r"(?:(?:稿子|画稿|工作|工单|海报|插画|折页|封面|排版).{0,10}(?:做完|写完|画完|完成|交付|交稿|收尾完|结单)|(?:做完|写完|画完|完成|交付|交了|交稿).{0,12}(?:稿子|画稿|工作|工单|海报|插画|折页|封面|排版))(?:了|啦|好|$)")),
    ("visit_finished", re.compile(r"(?:复诊|看诊|检查|看完医生|问诊).{0,6}(?:结束|做完|看完|完成|完事)|(?:刚|已经|今天)看完了?(?:医生|诊)|(?:刚|已经|今天)(?:做完|完成)(?:了)?(?:复诊|检查|看诊)")),
    ("visit_started", re.compile(r"(?:到了|正在|来了|来|在)(?:医院|诊所|复诊|看诊|候诊|问诊|做检查)|(?:复诊|看诊)(?:开始了|去了|到了)")),
    ("meal_finished", re.compile(r"(?:吃完|用完餐)(?:了|啦|饭|早餐|午餐|晚餐|$)|(?:饭|早餐|午餐|晚餐)(?:吃完|吃好了)")),
    ("meal_arrived", re.compile(r"(?:外卖|餐|晚饭|午饭)(?:已经|刚刚|刚才|刚)?(?:到了|送到|收到了)")),
    ("gift_received", re.compile(r"(?:收到|拿到)(?:了)?(?:你.{0,9})?(?:送|寄|买|给).{0,12}|(?:收到|拿到)(?:了)?(?:.{0,6}(?:礼物|耳机|耳钉|戒指|手链|项链|玩偶|口红|花))")),
)


def _text(value):
    return str(value or "").strip()


def _time(value):
    return float(value) if isinstance(value, (int, float)) else None


def _window(label, at):
    local = datetime.fromtimestamp(at, TZ)
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if label in ("刚", "刚才", "刚刚"):
        return at - 1800, at + 1
    if label == "昨晚":
        return (day - timedelta(hours=6)).timestamp(), day.timestamp()
    if label in ("昨天", "前天"):
        offset = 1 if label == "昨天" else 2
        return (day - timedelta(days=offset)).timestamp(), (day - timedelta(days=offset-1)).timestamp()
    if label in ("之前", "早些时候"):
        return at - 36 * 3600, at + 1
    return day.timestamp(), min(at + 1, (day + timedelta(days=1)).timestamp())


def _event_overlaps(claim, source, at, now, strict):
    if at is None:
        return not strict and (claim == source or not claim or not source)
    if at > now + 5:
        return False
    a, b = _window(claim, now)
    start, end = _window(source, at) if source else (at, at + 1)
    if not claim:
        return max(a, start) < min(b, end)
    if claim in ("刚", "刚才", "刚刚"):
        if source not in ("刚", "刚才", "刚刚", ""):
            return False
        # The user saying “刚” a few seconds earlier is the same conversational window.
        if source and 0 <= now - at <= 120:
            return True
    # An imprecise timestamp in one day cannot prove a statement in a sub-window of that day.
    return a <= start and end <= b


def _object(value):
    value = TRAIL.sub("", re.sub(r"\s+", "", _text(value)))
    value = re.sub(r"^(?:把|个|了|过|了个|一(?:次|件|本|瓶|杯|盒|份|顿|个|张|只|套)|那个|这个|一些|一点|同一份)+", "", value)
    return TRAIL.sub("", value).strip("的")


def _owner_parts(value, who):
    value = CONNECTOR.sub("", value, count=1)
    for prefix in ("我把", "你把", "我给你", "你给我"):
        if value.startswith(prefix):
            value = prefix[:prefix.index("把")] + value[len(prefix):] if "把" in prefix else prefix[0] + value[len(prefix):]
            break
    match = OWNER_RE.match(value)
    if not match or match["who"] != who:
        return None
    mod = match["mod"]
    neg = bool(NEGATE.search(mod))
    verb = match["verb"]
    if verb in BARE_VERBS.split("|") and verb not in ("收到", "完成", "做完") and not neg:
        return None
    obj = _object(match["obj"])
    if not obj or re.search(r"(?:会|要|想|打算|可能|完就|了再|看看)$", obj):
        return None
    label = match["time_before"] or match["when"] or ("刚" if "刚" in mod else "")
    return OWNER_ACTIONS[verb], obj, not neg, label


def _masked(value):
    return QUOTE.sub(lambda m: " " * len(m[0]), value)


def _clauses(value):
    """Keep source offsets and sentence-final question scope across short clauses."""
    masked = _masked(value)
    for sentence in re.finditer(r"[^。！？!?\n]+[。！？!?]?", masked):
        question = sentence[0].endswith(("?", "？"))
        parts = list(re.finditer(r"[^，,；;。！？!?]+", sentence[0]))
        for index, piece in enumerate(parts):
            raw = piece[0]
            clause = raw.strip()
            if not clause:
                continue
            start = sentence.start() + piece.start() + len(raw) - len(raw.lstrip())
            end = start + len(clause)
            scoped_question = bool(QUESTION.search(clause)) or (
                question and (index == len(parts) - 1 or len(parts) == 1)
            )
            if (
                NONFACT.search(clause) or scoped_question
                or QUOTE_CONTEXT.search(clause)
                or re.match(r"^(?:明天|后天|下周|等会|晚点|回头|之后再|下次)", clause)
            ):
                yield clause, start, end, False
            elif question and len(parts) == 2 and re.search(r"对不对|对吧|是吗|是不是", parts[-1][0]):
                yield clause, start, end, False
            else:
                yield clause, start, end, True


def _owner_evidence(parts, owner_rows, now, strict):
    action, obj, positive, label = parts
    mismatch = "no_user_message_evidence"
    prior = None
    result = None
    for row in list(owner_rows)[-24:]:
        if isinstance(row, str):
            row = {"text": row}
        if not isinstance(row, dict) or row.get("role", "user") != "user" or row.get("quoted"):
            continue
        at = _time(row.get("at"))
        if strict and (at is None or now - at > 4 * 86400 or at > now + 5):
            continue
        ref = str(row.get("message_id") or (f"turn:{int(at * 1000)}" if at is not None else ""))
        for clause, _, _, factual in _clauses(_text(row.get("text"))):
            if not factual:
                continue
            clean = CONNECTOR.sub("", clause, count=1)
            source = _owner_parts(clean, "我")
            correction = CORRECTION.match(clean)
            if correction and prior and prior[0] == action and prior[1] == obj:
                corrected_action = OWNER_ACTIONS.get(correction["verb"] + "了", BARE_ACTIONS.get(correction["verb"]))
                corrected_object = _object(correction["obj"])
                if corrected_action == action and (not corrected_object or corrected_object == obj):
                    if not strict or prior[4] is not None and at is not None and 0 <= at - prior[4] <= 900:
                        result = (CONTRADICTED, "user_corrected_subject", ref)
                    continue
            if not source and prior and re.match(r"^(?:不是|我(?:还)?没|我没有)", clean):
                alternate = _owner_parts("我没" + clean.removeprefix("不是").removeprefix("我没").removeprefix("我没有") + obj, "我")
                if clean in ("不是", "我没有", "我没") and 0 <= (at or now) - (prior[4] or now) <= 900:
                    source = (prior[0], prior[1], False, prior[3])
                elif alternate and alternate[1] == obj:
                    source = alternate
            if not source:
                continue
            source_action, source_obj, source_positive, source_label = source
            if source_obj != obj:
                continue
            if source_action != action:
                mismatch = "predicate_mismatch"
                continue
            if not _event_overlaps(label, source_label, at, now, strict):
                mismatch = "time_mismatch"
                continue
            prior = (*source, at)
            result = (
                SUPPORTED if source_positive == positive else CONTRADICTED,
                "matched_recent_user_message" if source_positive == positive else "user_explicit_correction",
                ref,
            )
    return result or (UNSUPPORTED, mismatch, "")


def _place(value):
    value = _text(value)
    return next((group for group, terms in LOCATION_GROUPS.items()
                 if any(term in value for term in terms)), "")


def _compatible(a, b):
    return a == b or not a or not b or {a, b} == {"home", "kitchen"}


def _activity_match(value, row):
    actions = {key for key, pattern in ACTIONS.items() if pattern.search(value)}
    travelling = bool(re.search(r"路上|返程|回程|前往", _text(row.get("stage")) + _text(row.get("place"))))
    fields = ("place", "stage") if travelling else ("name", "action", "stage")
    recorded = " ".join(_text(row.get(key)) for key in fields)
    if actions:
        return all(ACTIONS[key].search(recorded) for key in actions)
    place = _place(value)
    if place:
        observed = _place(row.get("place"))
        return place == observed or place == "home" and observed == "kitchen"
    return bool(row.get("place") and TRAIL.sub("", re.sub(r"^(?:就)?在", "", value)) == row["place"])


def _activity_claim(body, label, current, past, projection, now, strict):
    rows = [current] if current else []
    if label:
        rows += past
        rows = [row for row in rows if _event_overlaps(
            label, "", _time(row.get("completed_at") or row.get("status_updated_at") or row.get("started_at")), now, strict,
        )]
    claim_place = _place(body)
    for row in rows:
        observed = _place(row.get("place"))
        named = _place(row.get("name"))
        if claim_place and not _compatible(named, observed) and not row.get("phase_place"):
            return UNSUPPORTED, "conflicting_current_activity_location_sources", "", None
        consistency = (projection.get("consistency") or {})
        activity_conflict = any(
            any(term in str(item.get("type", "")) for term in ("activit", "scheduler"))
            for item in (consistency.get("conflicts") or []) if isinstance(item, dict)
        )
        if not label and (activity_conflict or consistency.get("status") == "invalid"):
            return UNSUPPORTED, "source_conflict", "", None
        precise = re.match(r"^(?:就)?在([^，]{1,40}(?:咖啡店|咖啡馆|书店|便利店|商场|公园))(?:了|呢|呀|啊)?$", body)
        if precise and row.get("place") and not observed == "outside" and precise[1] != row["place"]:
            if not label and claim_place != observed:
                return (CONTRADICTED, "explicit_current_location_conflicts_with_projection",
                        str(row.get("activity_id") or ""), "我现在在" + row["place"])
            continue
        if claim_place and observed and not _compatible(claim_place, observed):
            if not label:
                return (CONTRADICTED, "explicit_current_location_conflicts_with_projection",
                        str(row.get("activity_id") or ""), "我现在在" + row["place"])
            continue
        if _activity_match(body, row):
            return (SUPPORTED, "matched_recent_activity_ledger" if label else "matched_current_activity",
                    str(row.get("activity_id") or ""), None)
    return (UNSUPPORTED, "no_matching_recent_activity_evidence" if label else
            "no_matching_current_activity_evidence", "", None)


def _domain_claim(kind, value, label, evidence, now, strict):
    for row in evidence.get(kind, ()):
        if not isinstance(row, dict):
            continue
        obj = _text(row.get("object"))
        words = MEDIA_NAMES.findall(value) if kind == "output_sent" else []
        if words and not any(word in obj or obj and obj in word for word in words):
            continue
        if kind == "activity_visited" and _object(obj) != _object(value):
            continue
        if kind == "activity_returned":
            origin = re.search(r"从(.+?)回来", value)
            if origin and origin[1] != obj:
                continue
        if kind == "gift_received" and obj and obj not in value and "礼物" not in value:
            continue
        if kind in ("visit_finished", "visit_started") and "复诊" in value and obj != "复诊":
            continue
        if kind == "work_finished":
            named = re.findall(r"海报|封面|插画|折页", value)
            if named and not any(word in obj for word in named):
                continue
        if _event_overlaps(label, "", _time(row.get("at")), now, strict):
            return SUPPORTED, "matched_domain_record", str(row.get("evidence_id") or "")
    return UNSUPPORTED, "unsupported_completion", ""


def _friend_claim(part, evidence, now, strict):
    """Check a concrete meetup assertion about a known fictional friend.

    Args:
        part: The visible reply clause.
        evidence: Current friend profiles and confirmed meetup facts.
        now: Evaluation timestamp.
        strict: Whether approximate dates require narrow matching.

    Returns:
        A fact finding tuple or None if the clause is not a meetup assertion.
    """
    profiles = evidence.get("friend_profiles", ())
    if not profiles or not re.match(r"^(?:我|梦雨|刚|今天|昨天|前天|上次|已经|昨晚|跟|和)", part):
        return None
    people = {
        person["friend_id"] for person in profiles
        if any(name and name in part for name in person["names"])
    }
    if (
        not people and not re.search(r"(?:和|跟|同)(?:一个|几个)?朋友", part)
        or re.search(r"没|别|要不要|下次|准备|打算|想约|可能|应该", part)
        or NONFACT.search(part)
    ):
        return None
    if FRIEND_BOOKED.search(part):
        kind = "friend_booked"
    elif FRIEND_DONE.search(part) or re.search(r"(?:一起|和|跟).{0,16}(?:吃饭|看展|逛街|散步|看电影|逛书店).{0,8}刚回来", part):
        kind = "friend_met"
    else:
        return None
    activities = {key for key, pattern in FRIEND_ACTIVITY.items() if pattern.search(part)}
    label = (re.search(TIME, part) or [""])[0]
    for row in evidence.get(kind, ()):
        if people and not people.issubset(row["friend_ids"]):
            continue
        if activities and row["activity"] not in activities:
            continue
        if kind == "friend_met" and not _event_overlaps(label, "", _time(row.get("at")), now, strict):
            continue
        return kind, SUPPORTED, "matched_friend_record", row.get("evidence_id", "")
    return kind, UNSUPPORTED, "missing_confirmed_friend_record", ""


def inspect_claims(text, projection, owner_texts=(), now=None, *, evidence=None, strict=False):
    now = time.time() if now is None else float(now)
    evidence = evidence or {}
    source = str(text or "")
    if not source or not isinstance(projection, dict):
        return []
    mem = projection.get("mengyu") or {}
    current = mem.get("activity")
    if current and current.get("status", "in_progress") != "in_progress":
        current = None
    past = [row for key in ("recent_completed", "recently_paused")
            for row in (mem.get(key) or ()) if isinstance(row, dict)]
    results = []
    subject = ""
    last_subject_end = 0
    for clause, start, end, factual in _clauses(source):
        part = CONNECTOR.sub("", clause, count=1)
        if not factual:
            subject = ""
            continue
        if re.search(r"[。！？!?\n]", source[last_subject_end:start]):
            subject = ""
        last_subject_end = end
        owner = _owner_parts(part, "你")
        if not owner:
            observed = re.match(r"^我(?:刚|刚刚|已经)?(?:看到|看见|发现)(你.+)$", part)
            owner = _owner_parts(observed[1], "你") if observed else None
        match = SELF_RE.match(part)
        explicit_self = match is not None or bool(ELLIPSIS_RE.match(part) or MEDIA_LEAD.match(part))
        self_part = explicit_self or subject == "self"
        correction = None
        kind = None
        friend_claim = _friend_claim(part, evidence, now, strict) if not owner else None
        if friend_claim:
            subject = "self"
            kind, grade, reason, ref = friend_claim
        elif owner:
            subject = "owner"
            kind = "owner_action"
            grade, reason, ref = _owner_evidence(owner, owner_texts, now, strict)
            if grade == UNSUPPORTED and source[:start].strip() == "" and source[end:].strip() in ("", "。", "！", "!"):
                correction = re.sub(r"^(你)", r"\1是不是", part, count=1) + "？"
        elif self_part or re.match(r"^(?:已经|刚刚|刚才|刚|今天|昨天|上次)?(?:复诊|外卖|收到|发给你|已发|吃完|完工)", part):
            subject = "self"
            body = match["body"] if match else part
            label = (match["when"] or "") if match else ((re.match(TIME, part) or [""])[0])
            past_label = "" if label in ("现在", "这会儿", "此刻") else label
            negative = bool(NEGATE.search((match["mod"] or "") if match else "")) or bool(re.search(r"还没|没到|没发|没完成|没吃完|没收到", part))
            domain = next((code for code, pattern in DOMAIN_CLAIMS if pattern.search(part)), "")
            if TRANSMIT.search(part) and (MEDIA_NAMES.search(part) or re.search(r"发给你了|发你了|发过去了|发出去了|给你发了", part)):
                domain = "output_sent"
            tentative = bool(re.search(r"(?:可能|应该|想|这就|马上|待会|等会|晚点|过会|下次|打算|准备|希望|计划|就要).{0,6}(?:发|送|收|完|做|写|画|去|到|查|买|吃)", part))
            if domain and not negative and not tentative and not NONFACT.search(part):
                kind = domain
                grade, reason, ref = _domain_claim(kind, part, past_label, evidence, now, strict)
            elif explicit_self and not negative and not tentative and ELLIPSIS_RE.match(part):
                kind = "activity_returned"
                grade, reason, ref = _domain_claim(kind, part, past_label or "刚才", evidence, now, strict)
            elif match and not negative and not tentative:
                if match["mod"] == "在":
                    body = "在" + body
                location_only = bool(re.match(r"^(?:就)?在.{1,45}$", body)) and bool(
                    re.search(r"店|馆|家|学校|公司|路上|公园|办公室|商场|菜市场|厨房|车上|房间|窗边|阳台|宿舍", body)
                )
                visited = re.match(r"^去(?:了|过)(.{1,35})$", body)
                if visited:
                    kind = "activity_visited"
                    grade, reason, ref = _domain_claim(kind, visited[1], past_label, evidence, now, strict)
                elif (match["when"] or match["mod"] in ("正在", "还在", "在", "正")) and (
                    any(pattern.search(body) for pattern in ACTIONS.values()) or _place(body) or location_only
                ):
                    kind = "mengyu_past_activity" if past_label else "mengyu_current_activity"
                    sleep = mem.get("sleep") or {}
                    if not past_label and ACTIONS["sleep"].search(body) and sleep.get("actual_phase") == "sleeping":
                        grade, reason, ref = SUPPORTED, "matched_actual_sleep", str(sleep.get("since") or "")
                    else:
                        grade, reason, ref, correction = _activity_claim(
                            body, past_label, current, past, projection, now, strict,
                        )
        else:
            subject = "owner" if part.startswith("你") else ""
        if kind:
            results.append({
                "claim_id": f"{start}:{end}", "grade": grade, "kind": kind,
                "claim": source[start:end], "reason": reason, "correction": correction,
                "start": start, "end": end, "evidence_id": ref or None,
            })
    return results


def _rewrite(source, issues, enforce):
    for item in reversed(issues):
        grade, kind = item["grade"], item["kind"]
        correction = item.get("correction")
        start, end = item["start"], item["end"]
        allowed_legacy = correction and (
            grade == CONTRADICTED or grade == UNSUPPORTED and kind == "owner_action"
        )
        if enforce and grade in (UNSUPPORTED, CONTRADICTED):
            # Removing a concrete assertion leaves emotional/context clauses intact.
            correction = correction if grade == CONTRADICTED and kind == "mengyu_current_activity" else None
            if correction is None:
                actual_end = end
                if source[end:end + 1] in ("，", ",", "；", ";", "。", "！", "!", "\n"):
                    actual_end += 1
                source = source[:start] + source[actual_end:]
                item["corrected"] = True
                continue
        if correction and (enforce or allowed_legacy):
            tail = source[end:]
            if correction.endswith("？") and tail.strip() in ("", "。", "！", "!"):
                tail = ""
            source = source[:start] + correction + tail
            item["corrected"] = True
        else:
            item["corrected"] = False
    if enforce and any(item.get("corrected") for item in issues):
        source = re.sub(r"(^|[。！？!?\n])[，,；;。]+", r"\1", source)
        source = re.sub(r"[，,；;]+(?=[。！？!?]|$)", "", source)
        source = re.sub(r"^[，,；;。！？!?\s]+", "", source)
    return source.strip()


def validate(text, projection, owner_texts=(), now=None):
    """Compatibility interface for integrations which used the advisory validator."""
    issues = inspect_claims(text, projection, owner_texts, now)
    return _rewrite(str(text or ""), issues, False), issues


def review(text, projection, owner_rows=(), *, now=None, evidence=None, enforce=True):
    """Evaluate the final public text. Shadow mode returns an unchanged draft."""
    issues = inspect_claims(text, projection, owner_rows, now, evidence=evidence, strict=True)
    if not enforce:
        return str(text or ""), issues
    return _rewrite(str(text or ""), issues, True), issues
