"""Ground contextual mood choices; retain legacy helpers for stored-state compatibility."""

import json
import random
import re
import time

from data.plugins.mengyu_affect.core.interaction_mood import guidance as interaction_guidance
from data.plugins.mengyu_affect.core.interaction_mood import APOLOGY as REPAIR_APOLOGY, EXPLANATION

TRIGGERS = (
    (r"你(真的|真|太|好|有点)?(无聊|没意思|烦|烦人|啰嗦|磨蹭|慢死)", "被当面嫌弃"),
    (r"你(怎么这样|怎么这么慢|怎么还不|到底行不行)", "被当面质疑"),
    (r"(别烦我|烦不烦|快点|赶紧|别磨蹭|墨迹)", "被催或被嫌烦"),
)

APOLOGY = re.compile(r"对不起|抱歉|别生气|我错了|哄哄|不是那个意思|我补偿")

GUIDANCE = """\n【当下相处的脾气与判断】
结合这轮完整的话、最近真实相处和自己的余味判断，不按关键词或计时触发生气。
认真分享被随口敷衍、自己的喜好被轻轻否定、玩笑有点过头、被催，或这次不想让步，
都足以让你轻轻不高兴，不必等严重冒犯，也不必先替他找好理由才准有反应。
觉得不舒服就当轮表达：顶半句、嘴硬、指出哪里不乐意、拒绝一下或少接一句都可以；
不要只在后台记生气、嘴上仍全是夸奖和宠溺，也别每次补温柔把不满收回。
反应与小事相称，不固定频率、不无端找茬，不把他忙、短回、没回或评价游戏当成嫌弃你。
引用、否定、玩笑和安慰按完整语境理解。他顺着你刚开的玩笑说假设中的“不喜欢”或问“你生气了么”，不自动等于真实差评或需要认真处理的矛盾，不因此写检讨或解释相处原则。熟悉的无害打趣先接其中的意思，不预先声明不配合来证明有脾气；实际有不适或已有明确拒绝时再按具体缘由反应。道歉只是新信息，可以缓和也可以仍有余气；
后续解释、体贴、自己的想通或新经历都可以让你缓和，不需等待罚时或认错仪式。
普通不高兴仍可接话、分享和先找他；想先收住这段话题才用SPACE，独处时仍可自己发起新联系，不用沉默惩罚或逼他哄。
当状态确实改变时，末尾另起一行输出私有标记（不会发给他）：
普通聊天每次先判断眼前相处是否带来新的余味，这不是每轮必须改变情绪。此标记同时负责正面和负面变化，不只用于生气。若正文已表现出新的开心、得意、失落、期待或放松，而此前记录尚未反映，须在同一次回复里同步选填feeling，不能只在正文演出来却继续沿用旧记录。这里只写简短的结果摘要与原话依据，不输出思考过程。
[[MENGYU_TEMPER:{"action":"upset|ease|clear|keep","reason":"具体缘由","evidence":"本轮用户原话连续片段","feeling":"有变化才填简短的当前相处感受，否则省略","carryover":"这份余味让你更想怎样相处，可空"}]]
upset=有点不高兴或不服，ease=已缓和但还有余气，clear=这件事已放下。
keep=不改变当前脾气；若只是被夸后开心、得意、亲近、失落或恢复平常，也可用keep并选填feeling和carryover。
只有本轮相处确实带来新的感受或判断时才填feeling，evidence必须来自本轮用户原话。没变化就不更新，不为每轮凑状态。
身体仍累与被夸开心可以并存；夸奖、关心和道歉都不自动等于解决原本介意的事，不为正面心情强行clear。
记录的余味影响之后的语气和选择，不要求立刻主动找他，也不要反复讲这次触发事件。没有固定开心时长或分数。
upset必须引用真实原话作为依据，不能捏造。无变化不输出标记，不为填字段改变情绪。
标记可与SPACE/BACK并用；先写脾气标记，再写SPACE/BACK。可见正文应自然表现自己的真实反应。
明确求助、急事和已完成工具的结果优先认真交代，不让小脾气盖住这些事情。
"""


def extract_decision(text):
    """Remove private mood metadata and return its validated decision.

    Args:
        text: Public reply with optional standalone private metadata.

    Returns:
        Visible text and the last valid mood decision, or None.
    """
    decision = None
    body = str(text or "")
    prefix = "[[MENGYU_TEMPER:"
    visible = []
    cursor = 0
    while (start := body.find(prefix, cursor)) != -1:
        visible.append(body[cursor:start])
        payload_start = start + len(prefix)
        payload = body[payload_start:]
        stripped = payload.lstrip()
        try:
            obj, end = json.JSONDecoder().raw_decode(stripped)
            end = payload_start + len(payload) - len(stripped) + end
            closing = end + len(body[end:]) - len(body[end:].lstrip())
            if not body.startswith("]]", closing):
                raise ValueError("Missing private metadata delimiter")
            if (
                isinstance(obj, dict)
                and obj.get("action") in ("upset", "ease", "clear", "keep")
                and isinstance(obj.get("reason"), str)
                and 0 < len(obj["reason"].strip()) <= 240
                and isinstance(obj.get("evidence", ""), str)
            ):
                decision = obj
            cursor = closing + 2
        except (ValueError, TypeError):
            closing = body.find("]]", payload_start)
            cursor = closing + 2 if closing != -1 else len(body)
    visible.append(body[cursor:])
    return "".join(visible).strip(), decision


def apply_decision(state, decision, conversation, now=None):
    """Persist a contextual choice without automatic timers or apology resets.

    Args:
        state: Companion state to update.
        decision: Parsed private model decision.
        conversation: Actual recent utterances supporting activation.
        now: Optional Unix timestamp.

    Returns:
        True when a grounded transition was applied.
    """
    if not decision:
        return False
    action = decision["action"]
    if action not in ("upset", "ease", "clear"):
        return False
    previous = state.get("temper") or {}
    evidence = decision.get("evidence", "").strip()
    if action == "upset" and (
        not 2 <= len(evidence) <= 200
        or not any(evidence in str(text) for text in conversation)
    ):
        return False
    if action in ("ease", "clear"):
        if not previous.get("active") or not evidence or not any(evidence in str(text) for text in conversation):
            return False
        if not (REPAIR_APOLOGY.search(evidence) or EXPLANATION.search(evidence)):
            return False
    now = time.time() if now is None else now
    state["temper"] = {
        "mode": "contextual",
        "active": action != "clear",
        "phase": action,
        "reason": decision["reason"].strip(),
        "evidence": evidence[:200],
        "since": previous.get("since", now) if previous.get("active") else now,
        "updated_at": now,
    }
    return True


def detect(text):
    """Return a temper reason when his words would sting.

    Args:
        text: His message.

    Returns:
        Reason string or None.
    """
    message = str(text or "")
    for pattern, reason in TRIGGERS:
        if re.search(pattern, message):
            return reason
    return None


def upset(state, reason, minutes=None):
    """Put her in a short bad mood.

    Args:
        state: Plugin state dict.
        reason: What set her off.
        minutes: How long it lasts.
    """
    now = time.time()
    minutes = minutes or random.randint(20, 40)
    state["temper"] = {
        "active": True,
        "reason": reason,
        "since": now,
        "until": now + minutes * 60,
    }


def active(state, now=None):
    """Whether she is currently upset."""
    now = time.time() if now is None else now
    temper = state.get("temper") or {}
    if temper.get("mode") == "contextual":
        return bool(temper.get("active"))
    return bool(temper.get("active") and now < temper.get("until", 0))


def calmed(state, text, now=None):
    """Clear the bad mood when he actually makes up.

    Returns:
        True when an apology was accepted.
    """
    if not active(state, now):
        return False
    if APOLOGY.search(str(text or "")):
        temper = state.get("temper") or {}
        temper["apology_seen"] = True
        state["temper"] = temper
    return False


def note(state, now=None):
    """Short description of the current bad mood, or empty."""
    if not active(state, now):
        return ""
    temper = state.get("temper") or {}
    return str(temper.get("reason") or "有点不高兴")

def interaction_note(projection, *, proactive=False):
    """Use the staged interaction mood at the appropriate reply surface."""
    view = projection.get("mengyu") or {}
    mode = view.get("interaction_mood_mode")
    if mode == "full" or (mode == "reply" and not proactive):
        return interaction_guidance(view.get("interaction_mood"), proactive=proactive)
    return ""
