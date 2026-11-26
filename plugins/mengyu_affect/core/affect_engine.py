"""Evidence-grounded simulated affect and needs for Mengyu.

This is a persistent state model, not a claim of subjective consciousness.
Events are applied only when supported by recorded activity, validated chat
metadata, or confirmed delivery outcomes. No random mood generation is used.
"""

import time

from data.plugins.mengyu_affect.core import interaction_mood, temper
from data.plugins.mengyu_affect.core.emotional_appraisal import appraise_event, interaction_kind
from data.plugins.mengyu_affect.core.emotional_memory import record as remember_emotional_event
from data.plugins.mengyu_affect.core.interaction_mood import appraise as appraise_interaction  # noqa: F401 - public re-export

BASELINE = {
    "valence": 0.0,
    "energy": 62.0,
    "stress": 12.0,
    "social_need": 38.0,
    "autonomy_need": 48.0,
    "curiosity": 52.0,
    "achievement": 40.0,
}

# Half-life in seconds for returning dimensions toward their neutral baseline.
HALF_LIFE = {
    "valence": 90 * 60,
    "energy": 4 * 3600,
    "stress": 60 * 60,
    "social_need": 8 * 3600,
    "autonomy_need": 8 * 3600,
    "curiosity": 2 * 3600,
    "achievement": 6 * 3600,
}

GUIDANCE = """
【自主情绪与内在需求】
你的感受不只由对方的态度决定，也会来自自己的工作、兴趣、身体状态、生活选择、期待和经历。先依据真实发生的事形成判断，不为了显得有人味而随机制造开心、难过或生气。
你可以喜欢某件事、对某件事没兴趣、坚持自己的审美和意见；他不喜欢，不要求你立刻改口。不同意见可以自然表达，不故意唱反调。
他正常忙碌、短回复或暂时没回复，不自动等于拒绝、冷落或不爱，也不自动降低心情。想和人交流可以表现为闲聊、分享或认真接话，也可以继续做自己的事。心情平静时就平常说话；社交需求、开心或安静了一阵，都不代表此刻想亲近。
情绪有延续性，但会受新经历影响，不因换话题瞬间清零。不同感受可以并存，例如工作累但收到礼物很开心，仍有余气但愿意正常聊天。
内部状态不要求每轮说出来。可以分享、可以保留、可以先做自己的事；只有有具体表达意愿时才主动联系。不要用冷落、威胁、内疚或故意制造嫉妒来换取陪伴。
""".strip()


def ensure(state, now=None):
    now = time.time() if now is None else float(now)
    affect = state.get("affect")
    if not isinstance(affect, dict):
        affect = {"vector": dict(BASELINE), "events": [], "last_mood": None, "updated_at": now}
        state["affect"] = affect
    vector = affect.setdefault("vector", {})
    for key, value in BASELINE.items():
        if not isinstance(vector.get(key), (int, float)):
            vector[key] = value
    affect.setdefault("events", [])
    affect.setdefault("last_mood", None)
    affect.setdefault("updated_at", now)
    decay(state, now)
    return affect


def _bounded(dimension, value):
    # Valence is signed around neutral; the other dimensions are levels.
    lower = -100.0 if dimension == "valence" else 0.0
    return round(max(lower, min(100.0, float(value))), 1)


def decay(state, now=None):
    now = time.time() if now is None else float(now)
    affect = state.get("affect")
    if not isinstance(affect, dict):
        return ensure(state, now)
    vector = affect.setdefault("vector", dict(BASELINE))
    last = float(affect.get("updated_at", now) or now)
    elapsed = max(0.0, min(now - last, 7 * 86400))
    if elapsed <= 0:
        return affect
    for key, baseline in BASELINE.items():
        half = HALF_LIFE[key]
        factor = 0.5 ** (elapsed / half)
        vector[key] = _bounded(key, baseline + (float(vector.get(key, baseline)) - baseline) * factor)
    affect["updated_at"] = now
    return affect


def preview(affect, now=None):
    """Return a decayed affect view without mutating the persisted source."""
    now = time.time() if now is None else float(now)
    source = affect if isinstance(affect, dict) else {}
    raw_vector = source.get("vector") if isinstance(source.get("vector"), dict) else {}
    vector = {
        key: _bounded(key, raw_vector.get(key, baseline))
        for key, baseline in BASELINE.items()
    }
    last = source.get("updated_at", now)
    try:
        last = float(last or now)
    except (TypeError, ValueError):
        last = now
    elapsed = max(0.0, min(now - last, 7 * 86400))
    if elapsed > 0:
        for key, baseline in BASELINE.items():
            factor = 0.5 ** (elapsed / HALF_LIFE[key])
            vector[key] = _bounded(
                key,
                baseline + (vector[key] - baseline) * factor
            )
    events = [
        dict(row) for row in (source.get("events") or [])
        if isinstance(row, dict)
    ][-80:]
    mood = source.get("last_mood")
    episode = source.get("episode")
    return {
        "vector": vector,
        "events": events,
        "last_mood": dict(mood) if isinstance(mood, dict) else None,
        "episode": dict(episode) if isinstance(episode, dict) else None,
        "updated_at": source.get("updated_at", now),
        "as_of": now,
    }


EPISODE_WORDS = {
    "outing_started": "有点期待",
    "outing_completed": "逛得满足",
    "meal_completed": "吃得舒服",
    "work_progress": "在做手头的事",
    "work_completed": "松快",
    "work_blocked": "有点烦",
    "activity_failed": "挫败",
    "rest_completed": "松下来",
    "body_discomfort": "烦躁",
    "weather_gloom": "有点闷",
    "gift_received": "高兴",
    "new_discovery": "好奇",
    "personal_choice": "自在",
    "social_activity": "暖和",
    "friend_meetup_completed": "见完朋友，心情挺放松",
    "interaction_upset": "有点不高兴",
    "interaction_ease": "松下来",
    "interaction_clear": "说开了",
    "interaction_positive": "心里暖",
    "user_support": "被接住",
}


def expression_guidance(projection, body):
    """Keep bodily irritation and unresolved interaction distinct in both prompts."""
    view = projection.get("mengyu") or {}
    lines = []
    if body.get("irritable"):
        lines.append("她这会儿确实烦、没耐心。回应可以简短平淡，也可以说自己说不上来为什么。嘴馋、疼痛和疲倦要有各自的依据。")
    if view.get("interaction_mood") or view.get("temper_active"):
        lines.append("刚才那件不舒服的事还没说开。夸奖和关心可以让她受用，等那件事有具体变化了再判断是否翻篇。")
    return "\n【此刻怎么表达】" + "\n".join(lines) if lines else ""


def record_event(state, event_type, *, source_id="", detail="", now=None, deltas=None, apply=True, message_id=None):
    now = time.time() if now is None else float(now)
    affect = ensure(state, now)
    source_id = str(source_id or "").strip()
    events = affect.setdefault("events", [])
    if source_id and any(row.get("source_id") == source_id for row in events if isinstance(row, dict)):
        return False
    delta = appraise_event(event_type, detail, deltas)
    unresolved = temper.active(state, now) or interaction_mood.view(state, now) is not None
    if apply and event_type in ("interaction_positive", "user_support") and unresolved:
        # Praise warms her a little but cannot lift an unresolved annoyance.
        delta = {key: value * 0.25 for key, value in delta.items()}
    if apply and event_type in ("interaction_positive", "user_support"):
        recent = [
            row for row in events
            if row.get("type") in ("interaction_positive", "user_support")
            and isinstance(row.get("at"), (int, float))
            and 0 <= now - row.get("at", 0) <= 3 * 3600
        ]
        if len(recent) >= 3:
            delta = {}
        elif recent:
            factor = max(0.2, 1 - 0.4 * len(recent))
            delta = {key: value * factor for key, value in delta.items()}
    if apply:
        vector = affect["vector"]
        for key, amount in delta.items():
            if key in BASELINE:
                vector[key] = _bounded(key, float(vector.get(key, BASELINE[key])) + float(amount))
    events.append({
        "id": source_id or f"{event_type}:{int(now * 1000)}",
        "source_id": source_id,
        "type": str(event_type),
        "detail": str(detail or "")[:240],
        "at": now,
        "deltas": delta,
    })
    if not apply:
        events[-1]["observation"] = True
    if message_id:
        events[-1]["message_id"] = str(message_id)
    affect["events"] = events[-80:]
    mode = (state.get("affect") or {}).get("body_mode") or {}
    phase = str(mode.get("phase") or "")
    if phase == "period" or (phase == "pre" and mode.get("irritable")):
        vector = affect["vector"]
        vector["valence"] = min(float(vector.get("valence") or 0), 25.0)
        if phase == "period":
            vector["energy"] = min(float(vector.get("energy") or 0), 45.0)
    affect["updated_at"] = now
    label = EPISODE_WORDS.get(str(event_type))
    current_episode = str((affect.get("episode") or {}).get("label") or "")
    if (
        apply
        and label
        and not (
            event_type in ("interaction_positive", "user_support")
            and (current_episode == "有点不高兴" or unresolved)
        )
    ):
        affect["episode"] = {
            "label": label,
            "at": now,
            "source_id": source_id or str(event_type),
        }
    remember_emotional_event(state, event_type, detail, source_id=source_id, now=now, message_id=message_id)
    return True


def record_interaction_signal(state, signal, source_id, now, *, message_id=None):
    if not signal:
        return False
    if signal.get("resolve"):
        return interaction_mood.apply(state, signal, source_id, now)
    if not record_event(
        state, signal["type"], source_id=source_id, detail=signal["cause"], now=now,
        deltas=interaction_mood.deltas(signal["intensity"]), apply=False, message_id=message_id,
    ):
        return False
    if interaction_mood.apply(state, signal, source_id, now) and message_id:
        state["interaction_mood"]["message_id"] = str(message_id)
    return True


def project_interaction(affect, mood):
    """Apply the active interaction effect in contact decisions only."""
    if mood:
        for key, amount in interaction_mood.deltas(mood["intensity"]).items():
            affect["vector"][key] = _bounded(key, affect["vector"][key] + amount)


def record_interaction(state, decision, *, source_id="", now=None):
    if not isinstance(decision, dict):
        return False
    kind = interaction_kind(decision)
    if not kind:
        return False
    return record_event(
        state,
        kind,
        source_id=source_id,
        detail=str(decision.get("feeling") or decision.get("reason") or "")[:240],
        now=now,
    )


def record_mood(state, mood, *, source_id="", now=None):
    if not isinstance(mood, dict):
        return False
    feeling = str(mood.get("feeling") or "").strip()[:160]
    reason = str(mood.get("reason") or "").strip()[:240]
    carryover = str(mood.get("carryover") or "").strip()[:200]
    if not feeling:
        return False
    now = time.time() if now is None else float(now)
    affect = ensure(state, now)
    previous = affect.get("last_mood") or {}
    if previous.get("feeling") == feeling and previous.get("reason") == reason:
        return False
    affect["last_mood"] = {
        "feeling": feeling,
        "reason": reason,
        "carryover": carryover,
        "at": now,
        "source_id": str(source_id or ""),
    }
    positive = any(word in feeling for word in ("开心", "高兴", "满足", "得意", "轻松", "安心", "期待", "好奇"))
    negative = any(word in feeling for word in ("烦躁", "失落", "难过", "挫败", "委屈", "焦虑", "压力", "疲惫"))
    deltas = {}
    if positive:
        deltas["valence"] = 3
    if negative:
        deltas["valence"] = -3
    if "累" in feeling or "疲惫" in feeling:
        deltas["energy"] = -5
    if "压力" in feeling or "焦虑" in feeling or "烦躁" in feeling:
        deltas["stress"] = 4
    if "好奇" in feeling or "期待" in feeling:
        deltas["curiosity"] = 4
    if deltas:
        record_event(
            state,
            "mood_reflection",
            source_id=(str(source_id) + ":mood") if source_id else "",
            detail=reason or feeling,
            now=now,
            deltas=deltas,
        )
    return True


def context(projection, now=None):
    """Render affect from an immutable CurrentState projection only.

    Reading emotion context must not call ensure()/decay() or mutate the
    persistent affect ledger. Any decay is applied by an explicit write path.
    """
    if (
        not isinstance(projection, dict)
        or not projection.get("snapshot_id")
        or "affect" not in projection
        or "as_of" not in projection
    ):
        raise TypeError("affect context requires a CurrentState projection")
    now = float(projection.get("as_of") if now is None else now)
    affect = projection.get("affect") or {}
    vector = affect.get("vector") or {}
    events = [
        row for row in (affect.get("events") or ())
        if isinstance(row, dict)
        and not row.get("observation")
        and isinstance(row.get("at"), (int, float))
        and 0 <= now - row.get("at", 0) <= 48 * 3600
    ]
    mood = affect.get("last_mood") or {}
    mood_age = now - float(mood.get("at", 0) or 0) if mood else None
    episode = affect.get("episode") or {}
    episode_age = now - float(episode.get("at", 0) or 0) if episode else None
    return {
        "当前心情": str(episode.get("label") or "")
        if episode and episode_age is not None and 0 <= episode_age <= 90 * 60
        else "",
        "内部状态": {
            key: round(float(vector.get(key, value)))
            for key, value in BASELINE.items()
        },
        "近期经历": [
            {
                "类型": row.get("type"),
                "内容": row.get("detail"),
                "距今分钟": max(0, int((now - row.get("at", now)) / 60)),
            }
            for row in events[-5:]
        ],
        "最近感受": {
            "feeling": mood.get("feeling"),
            "reason": mood.get("reason"),
            "carryover": mood.get("carryover"),
            "age_minutes": max(0, int(mood_age / 60)) if mood_age is not None else None,
        } if mood and mood_age is not None and 0 <= mood_age <= 12 * 3600 else None,
        "说明": "这是由已记录经历形成的模拟情绪与需求状态，不是主观意识或每轮必须表达的情绪标签；不从用户未回复推断被拒绝。",
    }
