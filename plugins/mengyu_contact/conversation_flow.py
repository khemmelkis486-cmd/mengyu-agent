"""Lightweight, turn-aware conversational guidance for ordinary QQ chat.

This module does not generate replies or infer hidden emotions.  It only
recognises a small set of obvious conversational moves and supplies one
compact reminder so the model can respond to the current interaction instead
of falling back to a generic service pattern.
"""

import re

from .image_intent import selfie_request

_PATTERNS = {
    "correction": re.compile(
        r"我说(?:的)?(?:是)?你(?:呀|呢|那|啊)?|说的是你|不是说我|不是我(?:呀|啊)?|"
        r"你听错了|我不是说我|我说你(?:呢|呀|那|啊)",
        re.IGNORECASE,
    ),
    "banter": re.compile(
        r"这么关心我|谁关心你|这么喜欢我|喜欢我吗|你是不是.{0,5}(?:喜欢|想我|吃醋)|"
        r"逗你|撩你|调戏|得意了吧|你还挺会|嘚瑟|接不接招",
        re.IGNORECASE,
    ),
    "affection": re.compile(
        r"宝宝|宝贝|抱抱|亲亲|贴贴|想你|爱你|喜欢你|老婆|媳妇|亲爱的|么么",
        re.IGNORECASE,
    ),
    "health": re.compile(
        r"脖子|颈椎|嗓子|喉咙|头疼|头痛|胃疼|肚子疼|手麻|脚麻|发烧|咳嗽|鼻塞|"
        r"胸闷|气短|呼吸困难|恶心|胃口不好|吃不下|身体不舒服|疼起来|痛起来|更痛了|更疼了",
        re.IGNORECASE,
    ),
    "feeling": re.compile(
        r"心里.{0,4}难受|难过|委屈|郁闷|失落|焦虑|压力大|心情不好|心情很差|烦死了|"
        r"有点烦|很烦|不开心|开心|高兴|兴奋|疲惫|好累|累死了",
        re.IGNORECASE,
    ),
    "task": re.compile(
        r"帮我|给我|请你|怎么(?:办|做|写|改|弄)|如何|能不能|可不可以|分析一下|"
        r"查一下|搜索|更新|修改|生成|做一版|写一份|解释一下",
        re.IGNORECASE,
    ),
    "self_view": re.compile(
        r"(?:那)?你呢[？?。！!]*$|你(?:自己)?(?:怎么看|觉得呢|会怎么想|会怎么写|会怎么处理)|"
        r"换成你|如果是你(?:呢|的话)?|你会怎么(?:写|画|做|处理|想)",
        re.IGNORECASE,
    ),
    "sharing": re.compile(
        r"我刚|刚才我|我今天|我看到|我听了|我在听|我发现|我刷到|我看了|"
        r"我买了|我做了|我觉得|我遇到|我试了|给你看|发你一个",
        re.IGNORECASE,
    ),
}

_FOLLOWUP_HEALTH = re.compile(
    r"还是那样|没缓过来|还(?:是)?疼|还(?:是)?痛|更疼|更痛|没好|没有好|不见好|"
    r"一直疼|一直痛|好一点没有|好点没",
    re.IGNORECASE,
)
_PHYSICAL_CONTEXT = re.compile(
    r"脖子|颈椎|嗓子|喉咙|头疼|头痛|胃疼|肚子疼|手麻|脚麻|发烧|咳嗽|鼻塞|"
    r"胸闷|气短|呼吸困难|受伤|扭伤|疼起来|痛起来",
    re.IGNORECASE,
)

_GUIDANCE = {
    "correction": (
        "【本轮互动方式：纠正指代】他是在纠正你刚才听错的主语或对象。简短承认具体听错了哪里，"
        "马上按正确指代继续回答；不要把他的情况猜成自己的，也不要为了补救再编一个关于他的事实。"
        "不长篇道歉，不重复盘问已经说清的内容。"
    ),
    "banter": (
        "【本轮互动方式：熟人打趣】他在逗你或顺着你开玩笑。先接住玩笑本身，可以嘴硬、反逗、"
        "笑着认一句或自然暧昧；不要把它转成问诊或建议，不解释笑点，不写相处原则，"
        "也不必用问题把话题再丢回去。给出你自己的具体反应，顺着这一来一回聊。"
    ),
    "affection": (
        "【本轮互动方式：亲近表达】他在表达亲近。自然接住，按你此刻的心情回应，可以坦率亲近、"
        "轻轻调侃或撒娇；不机械升级成索要更多抱抱、保证或反复确认，也不故意冷淡来显得独立。"
    ),
    "health": (
        "【本轮互动方式：身体不适】先像亲近的人一样接住他当下的感受，不把普通聊天自动变成问诊。"
        "如果他明确问怎么办，先给简洁、稳妥的建议；没有明显危险信号时，最多问一个最必要的问题，不连续罗列症状。"
        "若他明确提到明显肢体无力、进行性麻木、胸痛、呼吸困难等危险信号，仍要认真提醒及时就医；"
        "聊天自然不能取代必要的安全判断。"
    ),
    "feeling": (
        "【本轮互动方式：情绪表达】他在说自己的感受。先回应这份感受本身，不急着讲道理、给解决方案或追问原因；"
        "可以自然表达在意，也可以先陪他把话说完。只有他明确想分析或求办法时，再一起处理。"
    ),
    "task": (
        "【本轮互动方式：明确请求】他在请你完成一件具体事情。先直接处理请求并给出有用结果；"
        "不为了表现亲密而绕开任务，也不在结果后自动附加关心、玩笑或新问题。"
    ),
    "sharing": (
        "【本轮互动方式：他在分享】先回应他分享的具体内容中你真正有反应的那一点，可以说自己的看法、"
        "联想到的事或轻松吐槽；不只泛泛夸好、不把他的内容复述一遍，也不强行总结或追问。"
    ),
    "self_view": (
        "【本轮互动方式：他在问你的看法】他把话题转给你，直接说你自己的观点、偏好、做法或联想。"
        "如果在讨论创作，就说你会怎样处理画面、动作或人物选择；可以赞同，也可以提出不同想法。"
        "不要把回答转成对他的点评、赞美、人物画像或复述总结；除非他明确要求你评价他。"
        "把话题留在你们正在讨论的内容上，不用另加一个问题。"
    ),
    "ordinary": (
        "【本轮互动方式：普通来回】把这当作熟人之间正在进行的一段聊天，顺着眼前意思自然接话。"
        "长度跟着这轮互动走，不套固定句数；不强制提问，不把每句话改造成关心、建议、安慰或亲昵表达。"
        "可以有自己的反应，也可以自然停住。"
    ),
}


def chat_bubbles(text):
    """Keep explicit QQ bubbles or split a long line at sentence boundaries.

    Args:
        text: Already reviewed conversational text.

    Returns:
        Short intact chunks in speaking order.
    """
    result = []
    for raw in str(text).splitlines():
        line = raw.strip()
        if not line:
            continue
        buffer = ""
        for sentence in re.split(r"(?<=[。！？!?…])", line):
            if buffer and len(buffer) + len(sentence) > 45:
                result.append(buffer.strip())
                buffer = ""
            buffer += sentence
        if buffer.strip():
            result.append(buffer.strip())
    return result


_USER_EVALUATION = re.compile(
    r"你[^。！？!?\n]{0,55}(?:的时候|这句|那句|这段|那段|刚才|刚刚|说的|写的|讲的|提的|认真讲|认真说)[^。！？!?\n]{0,35}"
    r"(?:挺|很|真|特别|蛮|相当|确实|太).{0,10}(?:迷人|有魅力|厉害|聪明|会说|有意思|妙|有劲|漂亮|出色|成熟|细腻|认真|好|棒)",
    re.IGNORECASE,
)
_USER_EVALUATION_ALT = re.compile(
    r"你[^。！？!?\n]{0,35}(?:说得|写得|讲得|画得|做得|处理得)(?:真|挺|很|特别|蛮).{0,10}(?:好|妙|有劲|漂亮|出色|厉害|有意思)",
    re.IGNORECASE,
)
_EXPLICIT_FEEDBACK_REQUEST = re.compile(
    r"夸(?:夸)?我|评价(?:一下)?我|点评(?:一下)?我|分析一下我|"
    r"你觉得我.{0,12}(?:怎么样|如何|好不好)|我(?:写|画|讲|说|做|表现|处理).{0,12}(?:怎么样|如何|好不好)|"
    r"(?:这段|这句|这篇|这个画面).{0,12}(?:写得|画得|处理得)?(?:怎么样|如何|好不好|行不行)|"
    r"我迷人吗|我帅吗|我厉害吗|想听你夸",
    re.IGNORECASE,
)

_QUOTED_TEXT = re.compile(
    r"[“\"]([^”\"\n]{2,80})[”\"]|‘([^’\n]{2,80})’|「([^」\n]{2,80})」|『([^』\n]{2,80})』"
)
_USER_QUOTE_CUE = re.compile(
    r"(?:你(?:这个|刚才|刚刚|前面)(?:那句|说的|提到的|夸我的)?|"
    r"你(?:这句|那句|说(?:的)?|提到(?:的)?|提过(?:的)?|夸(?:我|梦雨))"
    r"|刚才(?:你|那句|那句话|说的)|刚刚(?:你|那句|那句话|说的)"
    r"|那句你说的)"
)


def _quoted_value(match):
    return next((value for value in match.groups() if value is not None), "").strip()


def _quote_is_user_attributed(text, start):
    prefix = str(text or "")[max(0, start - 24) : start]
    # Do not treat an explicit first-person self-quotation as the user's words.
    if re.search(r"我(?:刚才|刚刚|前面)?(?:说的|那句|这句)$", prefix):
        return False
    return bool(_USER_QUOTE_CUE.search(prefix))


def unverified_user_quotes(text, turns=(), *, current_user="", now=None, window=3600):
    """Find quoted phrases presented as the user's words without recent evidence.

    Only attribution-shaped quotations are checked; titles, concepts and the
    assistant's own quoted wording are left alone.  A phrase is valid only when
    it occurs verbatim in a recent user turn (including the current message).
    """
    import time

    now = time.time() if now is None else float(now)
    user_texts = []
    for row in list(turns or [])[-24:]:
        if not isinstance(row, dict) or row.get("role") != "user":
            continue
        at = row.get("at")
        if isinstance(at, (int, float)) and not (0 <= now - float(at) <= window):
            continue
        value = str(row.get("text") or "").strip()
        if value:
            user_texts.append(re.sub(r"\s+", "", value))
    if current_user:
        current = re.sub(r"\s+", "", str(current_user).strip())
        if current and current not in user_texts:
            user_texts.append(current)

    invalid = []
    for match in _QUOTED_TEXT.finditer(str(text or "")):
        if not _quote_is_user_attributed(text, match.start()):
            continue
        quote = _quoted_value(match)
        normalized = re.sub(r"\s+", "", quote)
        if normalized and not any(normalized in source for source in user_texts):
            invalid.append(quote)
    return list(dict.fromkeys(invalid))


def strip_unverified_user_quote_sentences(text, invalid_quotes):
    """Drop whole sentences that contain a fabricated user-attributed quote."""
    invalid = {str(value).strip() for value in invalid_quotes or [] if str(value).strip()}
    if not invalid:
        return str(text or "").strip()
    kept = []
    for match in re.finditer(r"[^。！？!?；;\n]+(?:[。！？!?；;\n]|$)", str(text or "")):
        sentence = match.group(0)
        if any(value in sentence for value in invalid):
            continue
        kept.append(sentence)
    return "".join(kept).strip()


def needs_user_evaluation_review(reply, user_text=""):
    """Flag unsolicited commentary that turns the user into the topic.

    Explicit requests for critique, assessment, or flirtatious praise are
    intentionally exempt.  This is a narrow post-generation guard, not a ban
    on ordinary compliments or useful feedback.
    """
    reply = str(reply or "")
    user_text = str(user_text or "")
    if not reply.strip() or _EXPLICIT_FEEDBACK_REQUEST.search(user_text):
        return False
    return bool(_USER_EVALUATION.search(reply) or _USER_EVALUATION_ALT.search(reply))


def _recent_text(recent_turns):
    if not isinstance(recent_turns, (list, tuple)):
        return ""
    parts = []
    for row in recent_turns[-6:]:
        if isinstance(row, dict):
            value = row.get("text", "")
        else:
            value = row
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return "\n".join(parts)


def classify_turn(current_text, recent_turns=()):
    """Classify the user's current conversational move, prioritising specificity."""
    current = str(current_text or "").strip()
    recent = _recent_text(recent_turns)

    # Correcting a mistaken referent must win over words such as "吃了" or "痛".
    if _PATTERNS["correction"].search(current):
        return "correction"
    if _PATTERNS["health"].search(current) or (
        _FOLLOWUP_HEALTH.search(current) and _PHYSICAL_CONTEXT.search(recent)
    ):
        return "health"
    if len(current) <= 48 and _PATTERNS["self_view"].search(current):
        return "self_view"
    if _PATTERNS["banter"].search(current):
        return "banter"
    if _PATTERNS["affection"].search(current):
        return "affection"
    if _PATTERNS["feeling"].search(current):
        return "feeling"
    if _PATTERNS["task"].search(current):
        return "task"
    if _PATTERNS["sharing"].search(current):
        return "sharing"
    return "ordinary"


def turn_guidance(current_text, recent_turns=()):
    """Return one concise, turn-specific instruction for the reply generator."""
    kind = classify_turn(current_text, recent_turns)
    return _GUIDANCE[kind]


NATURAL_REPLY_GUIDANCE = (
    "\n【日常聊天】先听懂他这句在接什么，按眼前的语气直接回。"
    "你可以说自己的看法、偏好、联想或反应，可以认同，也可以有不同意见。"
    "聊到有感觉的一点再展开，不复述他刚说的话，不自动给建议或总结；"
    "有想知道的可以顺着问一句，别连珠炮，也别把话头丢回去就完。"
    "他不想说时就陪一句，话题收住时也可以停。"
    "中文句子说完整，短句随语境走，别为了留白把基本意思说一半。"
)

_CONTEXT_TOPICS = {
    "meal": r"外卖|点餐|吃[过了没吗呢饭]|吃什[么麼]|吃完|想吃|饿了|早饭|午饭|晚饭|早餐|午餐|晚餐|饮食|做饭|喝汤|鸡煲",
    "gift": r"礼物|快递|包裹|给你买|买给你|买给你的|送给你|寄给你|给你带|送你[一件个]|耳[钉机]|项链|收礼|拆[开了箱]|签收",
    "work": r"你.{0,5}(工作|忙完|稿子)|工作|下班|画稿|交稿|改稿|甲方|订单|工单|客户|稿子",
    "task": r"任务|待办|承诺|答应|说好的|提醒|帮我记|别忘|约好|算数|改天|(?:有空|有机会|可能|也许|我想).{0,14}(?:陪你|帮你|一起|带你去)|下次.{0,12}(陪|帮|带|带你)|我(?:一定|会|肯定).{0,12}(?:帮|给|陪|带)",
    "health": r"复诊|就诊|医院|诊所|看病|生病|感冒|医生|检查|药|体温|经期|头疼|头痛|身体|还疼|还痛|好点|好一点|难受|不舒服",
    "sleep": r"睡|醒|晚安|午觉|作息|起床|困了|失眠|熬夜",
    "activity": r"在[干做]嘛|在[干做]啥|在忙啥|干什么|忙什么|做什么呢|现[在下]在哪|去了哪|到哪了|这会儿在哪|出门|回来|今天.{0,6}安排|今天过得|发生了啥|(?:还打算|接下来|一会儿|等会儿|待会儿).{0,5}(?:做什么|干嘛|干啥)",
    "shopping": r"衣服|外套|裙子|款式|试穿|穿搭|逛街|买东西|试衣|购物",
    "friends": r"朋友|同学|前同事|画手|邻居|聚餐|约见|看展|聚会",
    "photo": r"照片|自拍|拍[张照图]|背景|发图|成图|那张图|图里的",
    "game": r"游戏|杀戮尖塔|杀塔|选牌|拿牌|牌面|卡组|Boss|通关|第[一二三四五12345]层|爬塔",
    "reading": r"读书|看书|读到|看到哪[一儿里章]|哪[一]?章|那本|这本|书名|剧情|小说|结局|作者|试读",
    "diary": r"日记|札记",
    "wish": r"许愿|愿望|想要|想让我|想我帮|想我陪",
    "memory": r"记不记得|还记得|记得吗|之前说|上次说|以前|去年|上回|那天",
    "mood": r"心情|怎么了|不开心|高兴吗|生我气|吃醋|不高兴|想我没|有点烦|怎么这么安静|为什么别扭|感觉怎么样|今天过得怎么样",
    "time": r"几点|什么时候|多久|今天几号|明天几号|哪天|时间|刚才不是",
}
_ELLIPTICAL_CONTEXT = re.compile(
    r"^(?:啊|嗯[嗯]?|那|对了|欸|哎)?[，, ]*(?:后来呢|然后呢|接着呢|结果呢|最后呢|现在呢|你呢|"
    r"(?:那[个件张次]|这[个件张次]|它|那事|这事).{0,10}|到了吗|收到(?:了)?吗|"
    r"买到了吗|送到了吗|拿到了吗|发了[吗没]|弄好了吗|(?:做|忙|交|完)完了[吗没]|"
    r"弄得怎么样|怎么样了|好了没|好点[了吗没]*|我刚说的.{0,8}|刚才那个.{0,8})[。？！!?\s]*$"
)
_REPAIR_FRAGMENTS = re.compile(
    r"^[，,。；;、：:!?！？]|(?:[，,；;：:、]|(?:所以|而且|话说|倒是|不过|但是|然后|要不|只是|既然|虽然|反正))$"
)


def context_topics(text, recent=()):
    """Read an explicit topic or, for a short follow-up, its nearest referent."""
    value = str(text or "").strip()
    topics = {key for key, pattern in _CONTEXT_TOPICS.items() if re.search(pattern, value, re.I)}
    if selfie_request(value):
        topics.add("photo")
    if _ELLIPTICAL_CONTEXT.fullmatch(value):
        for row in reversed(list(recent)[-6:]):
            previous = str(row.get("text") or "").strip()
            if not previous or previous in value or _ELLIPTICAL_CONTEXT.fullmatch(previous):
                continue
            match = {key for key, pattern in _CONTEXT_TOPICS.items() if re.search(pattern, previous, re.I)}
            if match:
                topics.update(match)
            if match or len(previous) > 8:
                break
    return topics


def needs_reply_repair(original, checked):
    """Keep short, complete reactions. Retry when filtering leaves no natural reply."""
    return bool(original and original != checked and (
        not checked.strip() or _REPAIR_FRAGMENTS.search(checked.strip())
    ))


def confirmation_reply(text, recent=(), *, tool_called=False):
    """A grounded uncertainty response for a direct factual check."""
    if tool_called:
        return "这个结果我暂时确认不了。"
    if not re.search(r"[？?]|吗|了没|没有|收没收|是不是|能确认|发成没", str(text or "")):
        return ""
    topics = context_topics(text, recent)
    if "gift" in topics:
        return "我这边还没法确认礼物有没有到。"
    if "photo" in topics or re.search(r"语音|文件|发了", text):
        return "我这边还没法确认有没有发成功。"
    if "work" in topics:
        return "这单做到哪一步，我现在还不能确定。"
    if "health" in topics and re.search(r"复诊|检查|结果|完成|做完|后来|怎么样", text):
        return "复诊这边的结果我还不能确定。"
    if "meal" in topics and re.search(r"到了|送到|收到|点了", text):
        return "我这边还不能确认外卖到了没有。"
    return ""


_SELF_SPACE_REQUEST = re.compile(
    r"(?:^|[。！？；\n，,])\s*(?:我|让我)?(?:现在|想|得|要|先|还是|就|暂时|稍微|想要)*"
    r"(?:自己静一会|自己待一会|自己待会|一个人待一会|一个人静一会|先不聊了|不想继续聊)"
)


def explicit_self_space(text):
    return bool(_SELF_SPACE_REQUEST.search(str(text or "")))


def expire_self_space(state, now):
    space = state.get("self_space") or {}
    if not space.get("active"):
        return False
    since = space.get("since")
    if isinstance(since, (int, float)) and now - since < 20 * 60:
        return False
    state["self_space"] = {"active": False, "since": now, "reason": "这段休息已经结束"}
    return True
