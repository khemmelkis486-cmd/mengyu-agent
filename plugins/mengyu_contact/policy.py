"""Pure contact policy over the immutable CurrentState projection."""

import re
from collections.abc import Mapping
from datetime import datetime
from difflib import SequenceMatcher
from hashlib import sha256
from random import randint

from data.plugins.mengyu_media.core.photo_activity import LIFE_PHOTO_MAX_AGE

if __package__:
    from .jobs import night_due
    from .sleep_state import user_paused
    from .variety import is_ack
else:
    from jobs import night_due
    from sleep_state import user_paused
    from variety import is_ack

if __package__:
    from .local_clock import TZ
else:
    from local_clock import TZ


def _require_projection(snapshot):
    if not isinstance(snapshot, Mapping) or not snapshot.get("snapshot_id"):
        raise TypeError("policy functions require a CurrentState projection")
    return snapshot


def _section(snapshot, key):
    if not isinstance(snapshot, Mapping):
        return {}
    value = snapshot.get(key) or {}
    return value if isinstance(value, Mapping) else {}


def _contact(snapshot):
    return _section(snapshot, "contact")


def _owner(snapshot):
    return _section(snapshot, "owner")


def _mengyu(snapshot):
    return _section(snapshot, "mengyu")


def _sleep_view(snapshot):
    return _section(_mengyu(snapshot), "sleep")


def _life_clock(snapshot):
    return _section(_mengyu(snapshot), "life_clock")


def local_day(now):
    return datetime.fromtimestamp(now, TZ).date().isoformat()


def night_window(now):
    """Whether a timestamp falls inside the local late-night wake window."""
    hour = datetime.fromtimestamp(float(now), TZ).hour
    return hour >= 22 or hour < 6


def followup_ready(snapshot, now):
    _require_projection(snapshot)
    contact = _contact(snapshot)
    topic = contact.get("topic") or {}
    follow = topic.get("pending_followup") or {}
    if follow:
        due = float(follow.get("due_at") or 0)
        return bool(due and due <= now <= due + 180)
    due = contact.get("followup_due_ts", 0)
    replied = contact.get("followup_reply_ts", 0)
    return bool(
        due
        and replied
        and due <= now
        and now - replied <= 90
        and contact.get("followup_user_ts") == contact.get("last_user_ts")
        and now - contact.get("last_user_ts", 0) >= 15
    )


def own_offer_ready(snapshot, now):
    """Offer a fresh personal share without requiring a preceding chat reply."""
    _require_projection(snapshot)
    contact = _contact(snapshot)
    offer = contact.get("own_moment_offer") or {}
    photo = offer.get("kind") == "photo"
    answered = contact.get("answered_tail") or {}
    user = contact.get("last_user_ts", 0)
    space = _section(_mengyu(snapshot), "self_space")
    answered_at = answered.get("at", 0)
    recent_reply = (
        answered.get("delivery") == "complete"
        and 0 <= now - answered_at < 300
    )
    waiting_for_user = bool(
        user
        and now - user < 300
        and answered.get("user_ts") != user
        and contact.get("last_ack_handled_user_ts") != user
    )
    return bool(
        not waiting_for_user
        and offer.get("status") == "pending"
        and 0 <= now - offer.get("at", 0) <= (LIFE_PHOTO_MAX_AGE if photo else 1800)
        and (not photo or now >= contact.get("next_check", 0))
        and (not recent_reply or (
            now - answered_at >= 30
            and not re.search(r"[?？]", str(answered.get("reply", "")))
        ))
        and (not user or now - user >= 15)
        and now - contact.get("last_proactive_ts", 0) >= 60
        and (not space.get("active") or offer.get("at", 0) > space.get("since", 0))
    )


def absence_ready(snapshot, now):
    _require_projection(snapshot)
    contact = _contact(snapshot)
    due = contact.get("absence_due_ts", 0)
    user = contact.get("last_user_ts", 0)
    answered = contact.get("answered_tail") or {}
    if answered.get("user_ts") == user and is_ack(answered.get("user", "")):
        return False
    return bool(
        due
        and user
        and due <= now <= due + 300
        and contact.get("absence_user_ts") == user
        and contact.get("last_absence_user_ts") != user
        and now - contact.get("last_proactive_ts", 0) >= 30
    )


def absence_followup_allowed(user_text, *, conversation_paused=False, self_space_active=False):
    """Whether a delivered ordinary reply should arm a later absence check."""
    return bool(not conversation_paused and not self_space_active and not is_ack(user_text))


def sleeping(snapshot, now=None):
    """Read the effective sleep phase from the projection; never reconcile/write."""
    _require_projection(snapshot)
    sleep = _sleep_view(snapshot)
    return bool(sleep.get("effective_phase") == "sleeping")


def casual_ready(snapshot, now):
    """Whether to offer an optional casual conversation decision."""
    _require_projection(snapshot)
    contact = _contact(snapshot)
    owner = _owner(snapshot)
    user = contact.get("last_user_ts", 0)
    answered = contact.get("answered_tail") or {}
    answered_at = answered.get("at", 0)
    proactive_at = contact.get("last_proactive_ts", 0)
    last_assistant_at = max(
        float(answered_at) if isinstance(answered_at, (int, float)) else 0.0,
        float(proactive_at) if isinstance(proactive_at, (int, float)) else 0.0,
    )
    return bool(
        (user or contact.get("has_turns") or proactive_at)
        and (not user or now - user >= 300)
        and owner.get("mode") == "open"
        and not sleeping(snapshot, now)
        and now - contact.get("casual_considered_at", 0) >= 900
        and now - last_assistant_at >= 900
    )


def rest_sharing_allowed(snapshot):
    """Only a classified rest pause permits low-demand personal sharing."""
    _require_projection(snapshot)
    return _owner(snapshot).get("mode") == "rest"


def rest_message_allowed(text):
    """Reject clear waking questions or demands while allowing personal sharing."""
    return not bool(
        re.search(
            r"醒醒|(?:睡醒|醒).{0,3}[吗么没]|晕车.{0,10}(?:缓|好点|怎么样)|怎么.{0,4}(?:没声|不回)|在吗|在不在|你呢|(?:陪|抱|亲|哄)我|你.{0,8}[？?]",
            str(text or ""),
        )
    )


def eligible(snapshot, now):
    """Determine whether proactive contact is permitted from one projection."""
    _require_projection(snapshot)
    contact = _contact(snapshot)
    owner = _owner(snapshot)
    sleep = _sleep_view(snapshot)
    life_clock = _life_clock(snapshot)
    if not contact.get("enabled", True):
        return False, "已关闭"

    owner_mode = owner.get("mode", "open")
    if owner_mode != "open":
        if owner_mode != "rest":
            return False, "你明确要安静或正在忙，等你回来"
        if sleeping(snapshot, now) or life_clock.get("phase") == "sleeping":
            return False, "她也在睡，不打扰休息中的主人"
        if now - contact.get("last_user_ts", 0) < 300 or now < contact.get("next_check", 0):
            return False, "休息留言等待正常分享时机"
        return True, "可以留下自己的分享，不询问主人醒没醒"

    # Night-wake messages are retired: sleep is never interrupted by a ping.
    if life_clock.get("phase") == "sleeping":
        return False, "按自己的作息正在休息，不主动发消息"
    if sleeping(snapshot, now):
        return False, "已经和他说过晚安，先睡"

    last_user = contact.get("last_user_ts", 0)
    if own_offer_ready(snapshot, now):
        return True, "自己先想到了一件要分享的事，可以自然开口"
    if absence_ready(snapshot, now):
        return True, "聊天突然停下，可以顺口问一句"
    if followup_ready(snapshot, now):
        return True, "聊天中可以自然续说一句"
    if last_user and now - last_user < 300:
        return False, "刚聊过，先不打扰"
    if now < contact.get("next_check", 0):
        return False, "等待下次检查"
    return True, "可以自由发起闲聊或分享"


CLOSING = re.compile(
    r"先不聊|别发|不想聊|我要睡|睡了|晚安|先去忙|待会再聊|不想说话|不用回"
)


def repeats_recent(text, recent, threshold=0.72):
    """Whether this line is essentially one of her recent messages."""
    current = re.sub(r"[\W_]+", "", str(text or "").casefold())
    if not current:
        return False
    for item in list(recent or [])[-4:]:
        other = re.sub(r"[\W_]+", "", str(item or "").casefold())
        if other and SequenceMatcher(None, current, other).ratio() >= threshold:
            return True
    return False


def should_followup(reply, user_text, closed=False):
    """Whether a recent reply can be followed by one same-topic thought."""
    if closed or user_paused(user_text):
        return False
    text = str(reply or "").strip()
    if re.search(
        r"你接着说|你说吧|等你回答|我去睡|回头再聊|下次再聊", text
    ) or CLOSING.search(text):
        return False
    return 0 < len(text) <= 260 and not is_ack(text)


def queue_followup(state, topic, reply, user_ts, now, *, kind="statement"):
    """Schedule a single follow-up and share the same-topic delivery budget."""
    topic = str(topic or "").strip()[:240]
    normalized = re.sub(r"[\W_]+", "", topic.casefold())
    if not normalized:
        return
    fingerprint = sha256(normalized.encode()).hexdigest()
    previous = state.get("followup_thread") or {}
    same_topic = 0 <= now - previous.get("at", 0) <= 1200 and (
        previous.get("fingerprint") == fingerprint
        or SequenceMatcher(None, normalized, previous.get("normalized", "")).ratio() >= 0.72
    )
    thread = previous if same_topic else {
        "fingerprint": fingerprint, "normalized": normalized, "topic": topic, "sent": 0,
    }
    thread["at"] = now
    state["followup_thread"] = thread
    if thread.get("sent", 0) >= 2:
        return
    state.update(
        followup_topic={
            "topic": topic, "reply": str(reply)[:260], "kind": kind,
            "at": now, "user_ts": user_ts, "fingerprint": thread["fingerprint"],
        },
        followup_due_ts=now + randint(20, 45),
        followup_reply_ts=now,
        followup_user_ts=user_ts,
    )


def evening_diary_due(last_diary_at, now):
    """Whether tonight's diary is overdue."""
    return datetime.fromtimestamp(now, TZ).hour >= 21 and now - last_diary_at >= 6 * 3600


def morning_ready(snapshot, now):
    """Whether a first morning message is due, using only projected state."""
    _require_projection(snapshot)
    sleep = _sleep_view(snapshot)
    life_clock = _life_clock(snapshot)
    contact = _contact(snapshot)
    if not sleep or sleeping(snapshot, now):
        return False
    if life_clock.get("phase") == "sleeping" or life_clock.get("catchup_status") == "pending":
        return False
    if not 5 <= datetime.fromtimestamp(now, TZ).hour < 12:
        return False
    if contact.get("morning_greeted_day") == local_day(now):
        return False
    return now - contact.get("last_user_ts", 0) > 900
