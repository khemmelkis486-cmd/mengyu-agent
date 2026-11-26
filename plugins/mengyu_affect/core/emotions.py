"""Keep current experience visible without inventing or randomizing emotions."""

import time


def mood_context(mood, notes, now=None):
    """Attach age and newer grounded experiences to the last recorded mood.

    Args:
        mood: Last persisted mood, which may be absent.
        notes: Saved reflections on actual or consented fictional activities.
        now: Current Unix timestamp, or the wall clock when omitted.

    Returns:
        Recorded feeling and subsequent evidence for a fresh contextual judgment.
    """
    now = time.time() if now is None else now
    at = (mood or {}).get("at", 0)
    newer = [
        {
            key: note.get(key)
            for key in ("at", "activity", "thought", "activity_id", "fictional")
        }
        for note in notes
        if note.get("kind") == "reflection"
        and not note.get("retired_at")
        and max(at, now - 10800) < note.get("at", 0) <= now
    ]
    return {
        "recorded_mood": mood,
        "record_age_seconds": max(0, int(now - at)) if at else None,
        "newer_experiences": newer[-3:],
    }


def feeling_allowed(state, feeling, now=None):
    """Decide whether a newly proposed shared-mood line should be stored.

    Args:
        state: Persisted contact state holding the previous line and timestamps.
        feeling: Newly proposed shared-mood line from an activity plan.
        now: Current Unix timestamp, or the wall clock when omitted.

    Returns:
        True only for non-empty, changed text backed by a newer conversation.
    """
    if (state.get("relationship_feeling_source") or {}).get("kind") == "chat":
        return False
    now = time.time() if now is None else now
    text = feeling.strip() if isinstance(feeling, str) else ""
    if not text or text == (state.get("relationship_feeling") or "").strip():
        return False
    written = state.get("relationship_feeling_at", 0)
    return state.get("last_user_ts", 0) > written or now - written > 21600


def apply_interaction_feeling(
    state, decision, user_text, user_ts, message_id="", now=None
):
    """Persist only a new, current and quoted interaction effect.

    Args:
        state: Mutable companion state with existing relationship feeling.
        decision: Parsed private reply metadata; no score or implicit trigger.
        user_text: Current merged user message, never assistant-generated evidence.
        user_ts: User timestamp captured when this request started.
        message_id: Current source message id for duplicate protection.
        now: Optional write timestamp.

    Returns:
        True if grounded metadata changed the relationship feeling.
    """
    if not isinstance(decision, dict):
        return False
    feeling = decision.get("feeling")
    reason = decision.get("reason")
    carryover = decision.get("carryover", "")
    evidence = decision.get("evidence")
    if (
        decision.get("action") not in ("keep", "upset", "ease", "clear")
        or not isinstance(feeling, str)
        or not 1 <= len(feeling.strip()) <= 240
        or not isinstance(reason, str)
        or not 1 <= len(reason.strip()) <= 240
        or not isinstance(carryover, str)
        or len(carryover) > 240
        or not isinstance(evidence, str)
        or not 2 <= len(evidence.strip()) <= 200
        or evidence.strip() not in user_text
        or user_ts != state.get("last_user_ts", 0)
    ):
        return False
    previous = state.get("relationship_feeling_source") or {}
    if previous.get("kind") == "chat" and (
        user_ts < previous.get("user_ts", 0)
        or (
            user_ts == previous.get("user_ts")
            and message_id == previous.get("message_id", "")
        )
    ):
        return False
    if (
        feeling.strip() == (state.get("relationship_feeling") or "").strip()
        and reason.strip() == previous.get("reason")
        and carryover.strip() == previous.get("carryover")
    ):
        return False
    now = time.time() if now is None else now
    state["relationship_feeling"] = feeling.strip()
    state["relationship_feeling_at"] = now
    state["relationship_feeling_source"] = {
        "kind": "chat",
        "reason": reason.strip(),
        "evidence": evidence.strip(),
        "carryover": carryover.strip(),
        "user_ts": user_ts,
        "message_id": str(message_id),
    }
    return True


def relationship_context(state, now=None):
    """Expose one age-aware feeling to chat, planning and proactive generation.

    Args:
        state: Companion state; legacy string feelings remain supported.
        now: Optional timestamp used only to describe recency.

    Returns:
        Private contextual data; this does not reset or extend any emotion.
    """
    feeling = state.get("relationship_feeling")
    if not feeling:
        return None
    now = time.time() if now is None else now
    age = max(0, int(now - state.get("relationship_feeling_at", 0)))
    if age >= 21600:
        return {"historical_only": True, "record_age_seconds": age}
    return {
        "feeling": feeling,
        "source": state.get("relationship_feeling_source") or {"kind": "legacy"},
        "record_age_seconds": age,
        "context_note": "这是近期相处余味，可与疲倦和未解的不满并存。时间越久参考越轻，眼前的新相处优先；不是待执行的任务或承诺，不强制发消息，也不复述触发事件。"
        if age < 10800
        else "这条记录较早，只作历史余味，不代表此刻仍有同样情绪；眼前相处优先，不重提旧事件。",
    }
