"""One topic record shared by replies, follow-ups and proactive contact.

The first version keeps the legacy ``followup_*`` fields in sync so existing
callers keep working; the topic record is the source for what was said, what is
still open and who should speak next.
"""

from __future__ import annotations

import re
import time
import uuid

SCHEMA_VERSION = 1

_NOISE = re.compile(r"^[\s哈呵嘿嘻嗯哦啊呀额呃哎唉诶~～!！?？。.,，、…]+$")
_EXPLICIT_SWITCH = re.compile(r"^(?:先不说这个|换个话题|说点别的|这个先放一放)")


def _is_noise(text):
    """Laughter and filler are tone, not a topic point."""
    value = str(text or "").strip()
    return bool(value) and bool(_NOISE.fullmatch(value))


MAX_POINTS = 8
MAX_TOPICS = 6
TOPIC_TTL = 6 * 3600


def _now(now=None):
    return float(time.time() if now is None else now)


def _store(state):
    data = state.get("conversation")
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        data = {"schema_version": SCHEMA_VERSION, "revision": 0, "topics": []}
        state["conversation"] = data
        _migrate_legacy(state, data)
    data.setdefault("topics", [])
    return data


def _migrate_legacy(state, data):
    """Carry a still-pending legacy follow-up into the topic record."""
    legacy = state.get("followup_topic") or {}
    due = state.get("followup_due_ts") or 0
    if not legacy.get("topic") or not due:
        return
    now = _now()
    topic = {
        "topic_id": "T-" + uuid.uuid4().hex[:10],
        "label": str(legacy.get("topic"))[:40],
        "phase": "active",
        "next_actor": "mengyu",
        "open_points": [str(legacy.get("topic"))[:90]],
        "covered_points": [],
        "pending_followup": {
            "topic_id": "",
            "due_at": float(due),
            "source_delivery_id": str(state.get("followup_reply_delivery") or ""),
            "revision": 0,
            "reason": "legacy_followup",
            "created_at": now,
        },
        "source_turn_ids": [],
        "created_at": now,
        "updated_at": now,
        "expires_at": now + TOPIC_TTL,
    }
    topic["pending_followup"]["topic_id"] = topic["topic_id"]
    data["topics"].append(topic)


def revision(state):
    return int(_store(state).get("revision") or 0)


def initialize(state):
    """Initialize the topic namespace during an explicit state migration."""
    _store(state)


def _bump(state):
    data = _store(state)
    data["revision"] = int(data.get("revision") or 0) + 1
    return data["revision"]


def current(state, now=None):
    now = _now(now)
    for topic in reversed(_store(state)["topics"]):
        if topic.get("phase") not in ("active", "waiting", "parked"):
            continue
        if now - float(topic.get("updated_at") or 0) > TOPIC_TTL:
            topic["phase"] = "closed"
            topic["closed_reason"] = "expired"
            continue
        return topic
    return None


def _add_point(topic, key, text):
    text = str(text or "").strip()
    if not text:
        return
    points = list(topic.get(key) or [])
    if text[:90] in points:
        return
    topic[key] = (points + [text[:90]])[-MAX_POINTS:]


def begin_user_turn(state, text, now=None):
    """A real user message cancels pending sends and moves the topic forward."""
    now = _now(now)
    cancel(state, "new_user_message")
    topic = current(state, now)
    if topic is not None and _EXPLICIT_SWITCH.search(str(text or "").strip()):
        close(state, "user_changed_topic", now)
        topic = None
    if topic is None:
        topic = {
            "topic_id": "T-" + uuid.uuid4().hex[:10],
            "label": str(text or "")[:40],
            "phase": "active",
            "next_actor": "mengyu",
            "open_points": [],
            "covered_points": [],
            "pending_followup": None,
            "source_turn_ids": [now],
            "created_at": now,
            "updated_at": now,
            "expires_at": now + TOPIC_TTL,
        }
        _store(state)["topics"].append(topic)
    else:
        topic["phase"] = "active"
        topic["next_actor"] = "mengyu"
        topic["updated_at"] = now
        topic["expires_at"] = now + TOPIC_TTL
        topic["label"] = topic.get("label") or str(text or "")[:40]
        topic["source_turn_ids"] = (
            list(topic.get("source_turn_ids") or []) + [now]
        )[-12:]
        if not _is_noise(text):
            _add_point(topic, "open_points", text)
    _bump(state)
    data = _store(state)
    data["topics"] = data["topics"][-MAX_TOPICS:]
    return topic


def note_reply(state, user_text, reply_text, delivered, now=None):
    """Record a delivered reply against the current topic."""
    now = _now(now)
    reply = str(reply_text or "").strip()
    topic = current(state, now)
    if topic is None:
        topic = begin_user_turn(state, user_text, now)
    if delivered and reply:
        _add_point(topic, "covered_points", reply)
        topic["last_reply_at"] = now
        topic["next_actor"] = "user" if ("?" in reply or "？" in reply) else "mengyu"
        topic["phase"] = "waiting" if topic["next_actor"] == "user" else "active"
        topic["updated_at"] = now
        _bump(state)
    return topic


def arm_followup(state, topic, due_at, source_delivery_id, now=None):
    """Store the one pending follow-up this topic may have."""
    now = _now(now)
    if topic is None:
        return None
    follow = {
        "topic_id": str(topic.get("topic_id") or ""),
        "due_at": float(due_at),
        "source_delivery_id": str(source_delivery_id or ""),
        "revision": revision(state),
        "reason": "reply_invites_more",
        "created_at": now,
    }
    topic["pending_followup"] = follow
    _bump(state)
    return follow


def pending(state, now=None):
    """The follow-up that is due right now, if any."""
    now = _now(now)
    topic = current(state, now)
    follow = (topic or {}).get("pending_followup") or {}
    if not follow:
        return {}
    due = float(follow.get("due_at") or 0)
    return follow if due <= now < due + 180 else {}


def cancel(state, reason="cancelled"):
    """Drop every pending follow-up; also clears the legacy mirror fields."""
    changed = False
    data = _store(state)
    for topic in data["topics"]:
        if topic.get("pending_followup"):
            topic["pending_followup"] = None
            topic["cancelled_reason"] = reason
            changed = True
    if changed:
        data["revision"] = int(data.get("revision") or 0) + 1
    state["followup_due_ts"] = 0
    state.pop("followup_topic", None)
    return changed


def close(state, reason="done", now=None):
    now = _now(now)
    topic = current(state, now)
    if topic is None:
        return None
    topic["phase"] = "closed"
    topic["closed_reason"] = reason
    topic["updated_at"] = now
    _bump(state)
    return topic


def projection(state, now=None):
    """Read-only view for prompts and diagnostics."""
    data = state.get("conversation") or {}
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        return {"revision": 0, "phase": "none", "next_actor": "none"}
    snapshot_revision = data.get("revision", 0)
    stamp = _now(now)
    topic = next((
        item for item in reversed(data.get("topics") or [])
        if isinstance(item, dict)
        and item.get("phase") in ("active", "waiting", "parked")
        and isinstance(item.get("updated_at"), (int, float))
        and stamp - item["updated_at"] <= TOPIC_TTL
    ), None)
    if topic is None:
        return {"revision": snapshot_revision, "phase": "none", "next_actor": "none"}
    return {
        "revision": snapshot_revision,
        "topic_id": topic.get("topic_id"),
        "label": topic.get("label"),
        "phase": topic.get("phase"),
        "next_actor": topic.get("next_actor"),
        "open_points": list(topic.get("open_points") or []),
        "covered_points": list(topic.get("covered_points") or []),
        "pending_followup": dict(topic.get("pending_followup") or {}),
    }
