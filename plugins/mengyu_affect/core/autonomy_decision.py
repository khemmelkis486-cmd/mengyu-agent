"""Evidence-based autonomous contact motives over CurrentState only.

This module is a pure decision function: it does not read or mutate persisted
state and it does not force a message to be sent.
"""

from collections.abc import Mapping

from data.plugins.mengyu_affect.core.proactive_balance import is_work, pick_memory


def _require_projection(snapshot):
    if not isinstance(snapshot, Mapping) or not snapshot.get("snapshot_id"):
        raise TypeError("autonomy decisions require a CurrentState projection")
    return snapshot


def _section(snapshot, key):
    if not isinstance(snapshot, Mapping):
        return {}
    value = snapshot.get(key) or {}
    return value if isinstance(value, Mapping) else {}


def evaluate(snapshot, now=None, *, contact_kind="spontaneous_share", personal_candidate=None):
    _require_projection(snapshot)
    now = float(snapshot.get("as_of") if now is None else now)
    owner = _section(snapshot, "owner")
    contact = _section(snapshot, "contact")
    mengyu = _section(snapshot, "mengyu")
    self_space = _section(mengyu, "self_space")
    affect = _section(snapshot, "affect")
    vector = _section(affect, "vector")

    mode = owner.get("mode", "open")
    if mode in ("quiet", "busy", "night"):
        return {"can_contact": False, "motive": None, "reason": "用户明确静默或暂时忙碌"}
    if mode == "rest" and contact_kind != "rest_share":
        return {"can_contact": False, "motive": None, "reason": "用户正在休息，只允许低打扰分享"}

    space_age = (
        max(0.0, now - float(self_space.get("since", now) or now))
        if self_space.get("active")
        else None
    )
    if (
        personal_candidate
        and not is_work(personal_candidate)
        and not (space_age is not None and space_age < 20 * 60)
        and contact_kind in ("spontaneous_share", "self_share", "casual_contact")
    ):
        return {
            "can_contact": True,
            "motive": "share_current_observation",
            "reason": "自己的生活里有一处还没聊过的细节",
            "event": {
                "detail": personal_candidate.get("thought"),
                "source_id": personal_candidate.get("source_event_id"),
            },
        }
    social = float(vector.get("social_need", 38) or 38)
    curiosity = float(vector.get("curiosity", 52) or 52)
    achievement = float(vector.get("achievement", 40) or 40)
    autonomy = float(vector.get("autonomy_need", 48) or 48)
    memories = _section(snapshot, "memories").get("emotional") or ()
    history = contact.get("proactive_history") or ()
    memory = pick_memory(memories, history, now)
    if memory:
        return {
            "can_contact": True,
            "motive": "share_work_milestone" if is_work(memory) else "share_life_event",
            "reason": "有新完成的工作成果" if is_work(memory) else "有一件尚未分享的真实生活经历",
            "event": {
                "type": memory.get("type"),
                "detail": memory.get("detail"),
                "source_id": memory.get("source_id"),
            },
            "memory_source_id": memory.get("source_id"),
            "social_need": round(social),
            "curiosity": round(curiosity),
            "achievement": round(achievement),
            "autonomy_need": round(autonomy),
        }

    if self_space.get("active") and (space_age or 0) < 20 * 60:
        motive, reason = None, "刚决定独处，暂时继续自己的事情"
    elif achievement >= 62:
        motive, reason = "share_achievement", "近期有明显的成就感"
    elif curiosity >= 68:
        motive, reason = "share_discovery", "近期好奇心和探索欲较强"
    elif social >= 62:
        motive, reason = "connection", "自然产生了想交流的需求"
    elif autonomy >= 72:
        motive, reason = "continue_own_life", "当前更想按自己的节奏安排生活"
    else:
        motive, reason = None, "当前没有足够明确的主动表达动机"
    return {
        "can_contact": bool(motive and motive != "continue_own_life"),
        "motive": motive,
        "reason": reason,
        "social_need": round(social),
        "curiosity": round(curiosity),
        "achievement": round(achievement),
        "autonomy_need": round(autonomy),
    }


def guidance(decision):
    if not isinstance(decision, dict):
        return "当前没有需要强行表达的自主联系动机。"
    motive = decision.get("motive")
    labels = {
        "share_life_event": "分享一件尚未分享的真实生活经历",
        "share_current_observation": "可以用自己留意到的一个细节开场，接着讲自己的看法",
        "share_work_milestone": "可选分享真正完成的工作成果，但不要求汇报进度",
        "share_achievement": "分享自己的成就或完成进展",
        "share_discovery": "分享最近发现或正在感兴趣的东西",
        "connection": "自然地想和他聊几句",
        "continue_own_life": "继续自己的生活，不必联系用户",
    }
    return {
        "是否已有明确动机": bool(decision.get("can_contact")),
        "自主动机": labels.get(motive, "暂无明确动机"),
        "原因": decision.get("reason"),
        "可引用经历": decision.get("event"),
        "原则": "这里评估已有动机，暂无明确动机也可以由自己的兴趣、当前感受或自然联想带出话题。没有想说的话可以安静，不把没回复解释成拒绝。",
    }
