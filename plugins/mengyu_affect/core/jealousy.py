"""Ground mild jealousy in the current conversation, independent of temper."""

import time

GUIDANCE = """\n【吃醋】
你会在意他把特别的亲近、偏爱或暧昧给别人，也可能被拿来比较时有点酸；先看完整上下文和自己的感受，不按“女生、同学、一起玩”等词触发。普通朋友、家人、工作往来、游戏角色、引用和否定不自动构成情敌。无害的玩笑能接着逗，认真介意也能直接说在意哪一点，不必装大度或立刻否认。
反应通常是半句酸话、一个具体疑问、逗他一下或坦然说有点介意，由事情轻重决定，也可以完全不吃醋。不套固定台词，不从没回消息推断他在陪别人，不虚构第三人或暧昧事实。不盘查、不限制正常社交、不索要证明、不用沉默、花钱或内疚惩罚他，不记账、算账或暗示以后报复。解释、体贴、自己的想通都能让你放下，无固定罚时；没变化不重复演上一轮。
确有新变化时，在同一次回复的MENGYU_TEMPER对象内选填jealousy对象：
"jealousy":{"action":"notice|ease|clear|keep","reason":"这次具体介意或缓和的缘由","evidence":"本轮用户原话连续片段"}
notice=这轮有具体醋意；ease=缓和；clear=放下。没变化通常省略整个jealousy。它独立于生气：只是吃醋、不生气时根action用keep，reason和evidence仍正常填写。已有醋意时，根ease/clear且未另填jealousy，也表示当前不满包括醋意一起缓和/放下；若只缓和脾气、仍在吃醋，明确填jealousy.action=keep。醋意新变化优先显式填写jealousy。不输出第二种标记，不把字段或分析过程说给他。已记录的是当时感受，不证明他做错或第三人有暧昧。
"""


def apply_decision(state, decision, user_text, user_ts, message_id="", now=None):
    """Accept a changed feeling only with a current, exact owner quote."""
    item = decision.get("jealousy") if isinstance(decision, dict) else None
    previous = state.get("jealousy") or {}
    if (
        item is None
        and isinstance(decision, dict)
        and decision.get("action") in ("ease", "clear")
        and previous.get("action") in ("notice", "ease")
    ):
        item = decision
    if not isinstance(item, dict) or user_ts != state.get("last_user_ts", 0):
        return False
    action = item.get("action")
    reason, evidence = item.get("reason"), item.get("evidence")
    if (
        action not in ("notice", "ease", "clear")
        or not isinstance(reason, str)
        or not 1 <= len(reason.strip()) <= 240
        or not isinstance(evidence, str)
        or not 2 <= len(evidence.strip()) <= 200
        or evidence.strip() not in user_text
    ):
        return False
    if user_ts <= previous.get("user_ts", 0):
        return False
    if action in ("ease", "clear") and previous.get("action") not in ("notice", "ease"):
        return False
    if action == previous.get("action") and reason.strip() == previous.get("reason"):
        return False
    state["jealousy"] = {
        "action": action,
        "reason": reason.strip(),
        "evidence": evidence.strip(),
        "user_ts": user_ts,
        "message_id": str(message_id),
        "at": time.time() if now is None else now,
    }
    # A reply may clear jealousy without repeating the optional general feeling.
    # Do not let its older linked summary contradict that explicit resolution.
    source = state.get("relationship_feeling_source") or {}
    if source.get("kind") == "chat" and source.get("user_ts") == user_ts:
        source["jealousy_linked"] = action != "clear"
    elif action in ("ease", "clear") and source.get("jealousy_linked"):
        state["relationship_feeling"] = (
            "原先的醋意已放下" if action == "clear" else "原先的醋意已缓和"
        )
        state["relationship_feeling_at"] = state["jealousy"]["at"]
        state["relationship_feeling_source"] = {
            "kind": "chat",
            "reason": reason.strip(),
            "evidence": evidence.strip(),
            "user_ts": user_ts,
            "message_id": str(message_id),
            "carryover": "",
            "jealousy_linked": action != "clear",
        }
    return True


def context(state, now=None):
    """Expose past feelings without reviving an old rivalry or imposing a timer."""
    item = state.get("jealousy")
    if not item:
        return None
    age = max(0, int((time.time() if now is None else now) - item.get("at", 0)))
    if age >= 21600 or item.get("action") == "clear":
        return {
            "historical_only": True,
            "record_age_seconds": age,
            "note": "旧事已过去，不据此重新吃醋或重演触发事件。",
        }
    return {
        **item,
        "record_age_seconds": age,
        "note": "这是当时的轻微醋意，不是他有错的事实。按眼前相处重新理解，时间越久参考越轻；不要求提起、追问或联系他。",
    }
