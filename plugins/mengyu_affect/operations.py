"""Evaluate interactions and persist changes through the chat core."""

import sqlite3
from data.plugins.mengyu_affect.core import interaction_mood
from data.plugins.mengyu_contact.state_write import log_record
from data.plugins.mengyu_affect.core.affect_engine import (
    appraise_interaction,
    record_interaction_signal,
)


def interaction_context(core, now, *, excluded=False):
    projection = core.current_state_projection(now)
    mengyu = projection["mengyu"]
    tail = core.state.get("answered_tail") or {}
    delivered = tail.get("delivery") == "complete" and tail.get("delivery_id")
    newer = next(
        (row for row in reversed(core.state.get("turns", []))
         if row.get("role") == "assistant_proactive" and row.get("delivery") == "sent"
         and row.get("at", 0) > (tail.get("at", 0) if delivered else 0)),
        None,
    )
    reply = newer.get("text") if newer else tail.get("reply") if delivered else ""
    reply_at = newer.get("at", 0) if newer else tail.get("at", 0) if delivered else 0
    return {
        "now": now,
        "owner_mode": projection["owner"]["mode"],
        "self_space": excluded or bool((mengyu.get("self_space") or {}).get("active")),
        "sleeping": ((mengyu.get("sleep") or {}).get("effective_phase") == "sleeping"
                     or (mengyu.get("life_clock") or {}).get("phase") == "sleeping"),
        "reply": reply,
        "at": reply_at,
        "delivery_id": tail.get("delivery_id") if delivered else None,
        "active": mengyu.get("interaction_mood"),
        "unanswered": bool(delivered and not newer and tail.get("user_ts") == core.state.get("last_user_ts", 0)
                           and core.state.get("last_proactive_ts", 0) <= tail.get("at", 0)),
    }


def assess_interaction(core, event, text, now):
    """Read the final merged owner turn before the model sees it."""
    if core.interaction_mood_mode == "off" or not core.inbox.is_current(event.get_extra("mengyu_burst_seq")):
        return False
    identity = event.get_extra("mengyu_incoming_id")
    if not identity or identity == core.state.get("last_mood_assessment"):
        return False
    core.state["last_mood_assessment"] = identity
    context = interaction_context(core, now, excluded=event.get_extra("mengyu_interaction_excluded"))
    context["quoted"] = any(
        type(part).__name__ in ("Reply", "Forward", "Node", "Nodes")
        for part in event.get_messages()
    )
    signal = None
    commitment_id = (context["active"] or {}).get("commitment_id")
    if commitment_id or interaction_mood.BROKEN.search(text):
        try:
            service = core.task_center_service()
            if service:
                if commitment_id:
                    item = service.store.get_commitment(commitment_id) or {}
                    if item.get("status") == "fulfilled":
                        signal = {"resolve": "之前的约定已履行"}
                if not signal and interaction_mood.BROKEN.search(text):
                    context["commitments"] = service.context_for_owner(
                        query_text=text, now=now,
                    ).get("active_commitments", [])
        except (AttributeError, OSError, ValueError, RuntimeError, sqlite3.Error):
            pass
    signal = signal or appraise_interaction(text, context)
    message_id = getattr(getattr(event, "message_obj", None), "message_id", None)
    return save_interaction_signal(core, signal, "interaction:" + identity, now, message_id)


def save_interaction_signal(core, signal, source, now, message_id):
    if not record_interaction_signal(core.state, signal, source, now, message_id=message_id):
        return False
    log_record(core.data_root / "diagnostics" / "interaction-mood.jsonl", {
        "at": now, "mode": core.interaction_mood_mode, "source_id": source,
        "message_id": str(message_id) if message_id else None,
        **signal,
    })
    return True


def assess_interaction_silence(core, now):
    if core.interaction_mood_mode == "off":
        return False
    context = interaction_context(core, now)
    signal = interaction_mood.observed_silence(context)
    if signal:
        return save_interaction_signal(
            core,
            signal, "interaction:silence:" + context["delivery_id"], now,
            (core.state.get("answered_tail") or {}).get("message_id"),
        )
    return False


# Her own day moves her mood. Each event becomes one bounded emotion change
# with the event id as evidence; nothing here sends a message.
LIFE_MOOD = {
    ("started", "outing"): ("outing_started", "出门了，有点期待"),
    ("started", "big_out"): ("outing_started", "出门逛街，有点期待"),
    ("finished", "outing"): ("outing_completed", "在外面逛完回来"),
    ("finished", "social_meetup"): ("friend_meetup_completed", "和朋友见了面"),
    ("finished", "big_out"): ("outing_completed", "逛完回来，还挺开心"),
    ("finished", "return"): ("outing_completed", "到家了"),
    ("finished", "meal"): ("meal_completed", "自己吃了顿饭"),
    ("started", "work"): ("work_progress", "开始做事"),
    ("finished", "work"): ("work_completed", "手上的事做完了"),
    ("finished", "chore"): ("chore_completed", "收拾完了"),
    ("finished", "chat"): ("social_activity", "和你聊完"),
    ("started", "sleep"): ("sleep_started", "准备睡了"),
    ("finished", "sleep"): ("sleep_finished", "睡醒了"),
}


def _life_events():
    try:
        from data.plugins.mengyu_life import life_events
    except Exception:  # noqa: BLE001 - emotion must never break the host plugin
        return None
    return life_events


def observe_life(core, now):
    """Turn the newest life event into one evidence-grounded mood change."""
    source = _life_events()
    if source is None:
        return False
    event = source.latest(now=now, exclude=("mengyu_affect",))
    if not event:
        return False
    event_id = str(event.get("event_id") or "")
    kind = event.get("kind")
    meetup = str(event.get("friend_meetup_id") or "")
    mapped = LIFE_MOOD.get((event.get("stage"), kind))
    if (kind == "return" and meetup) or (kind == "social_meetup" and not meetup):
        mapped = None
    if not mapped:
        source.consume(event_id, "mengyu_affect")
        return False
    event_type, detail = mapped
    name = str(event.get("name") or "").strip()
    try:
        recorded = core.record_affect_event(
            event_type,
            source_id="friend:" + meetup if kind == "social_meetup" else "life:" + event_id,
            detail="，".join(part for part in (name, str(event.get("outcome") or detail)) if part),
            now=now,
        )
        if recorded and kind == "social_meetup":
            # Persist the stable social source before consuming its Life event.
            core.save()
    except Exception:  # noqa: BLE001
        if kind == "social_meetup":
            return False
        recorded = False
    source.consume(event_id, "mengyu_affect")
    return bool(recorded)
