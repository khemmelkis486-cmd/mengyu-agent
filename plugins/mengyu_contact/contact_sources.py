"""Unified, non-blocking classification for proactive contact motives.

`contact_kind` remains the legacy scheduler/prompt mode (morning, photo_share,
conversation_followup, etc.). `contact_source` is the canonical motive recorded
for every proactive delivery.
"""

import re

from .life import note_used

ACTIVITY_SHARE = "activity_share"
RELATIONSHIP_CONTACT = "relationship_contact"
CONVERSATION_FOLLOWUP = "conversation_followup"
CASUAL_CONTACT = "casual_contact"
CONTACT_SOURCES = (
    ACTIVITY_SHARE,
    RELATIONSHIP_CONTACT,
    CONVERSATION_FOLLOWUP,
    CASUAL_CONTACT,
)

_AFFECTION = re.compile(
    r"想你|想亲|亲亲|抱抱|抱一下|喜欢你|爱你|撒娇|黏你|想靠近|想贴着|逗你|撩你|想见你|想陪你"
)


def available_contact_sources(
    contact_kind,
    activity_candidates=(),
    *,
    own_offer=None,
    share_event=None,
):
    """Return sources genuinely available for this scheduler slot.

    Special scheduler modes keep their existing behavior. Ordinary slots expose
    relationship and casual contact independently; activity sharing is exposed
    only when there is a real unshared activity candidate (or a photo slot).
    """
    kind = str(contact_kind or "spontaneous_share")
    if kind == "conversation_followup":
        return [CONVERSATION_FOLLOWUP]
    if kind == "night_wake":
        return [RELATIONSHIP_CONTACT]
    if kind == "absence_check":
        return [CASUAL_CONTACT]
    if kind == "photo_share":
        return [ACTIVITY_SHARE]

    candidates = list(activity_candidates or [])
    offer = own_offer if isinstance(own_offer, dict) else {}
    queued = share_event if isinstance(share_event, dict) else {}
    activity_available = bool(candidates)
    if offer.get("kind") == "photo" or queued.get("source_type") in (
        "activity_progress",
        "daylife_progress",
        "work_progress",
    ):
        activity_available = True
    options = []
    if activity_available:
        options.append(ACTIVITY_SHARE)
    options.extend((RELATIONSHIP_CONTACT, CASUAL_CONTACT))
    return options


def resolve_contact_source(
    requested,
    options,
    *,
    contact_kind="spontaneous_share",
    message="",
    source_event_id=None,
    activity_candidates=(),
    own_offer=None,
    share_event=None,
    artifact_id=None,
):
    """Validate the model's motive and bind activity shares to a real event.

    Returns `(source, source_event_id, candidate)`. Invalid/missing labels use a
    conservative semantic fallback; they never suppress an otherwise valid
    message.
    """
    allowed = [item for item in (options or []) if item in CONTACT_SOURCES]
    candidates = [
        item for item in (activity_candidates or [])
        if isinstance(item, dict) and item.get("source_event_id")
    ]
    requested = str(requested or "").strip()
    event_id = str(source_event_id or "").strip()
    candidate = None

    if requested == ACTIVITY_SHARE and ACTIVITY_SHARE in allowed:
        candidate = next(
            (item for item in candidates if item.get("source_event_id") == event_id),
            None,
        )
        if candidate is None and not event_id and len(candidates) == 1:
            candidate = candidates[0]
            event_id = str(candidate.get("source_event_id") or "")
        if candidate is not None:
            return ACTIVITY_SHARE, event_id, candidate

        offer = own_offer if isinstance(own_offer, dict) else {}
        queued = share_event if isinstance(share_event, dict) else {}
        if artifact_id and contact_kind == "photo_share":
            return ACTIVITY_SHARE, "photo:" + str(artifact_id), None
        if offer.get("kind") == "photo" and offer.get("artifact_id"):
            return ACTIVITY_SHARE, "photo:" + str(offer["artifact_id"]), None
        if queued.get("source_type") in (
            "activity_progress",
            "daylife_progress",
            "work_progress",
        ) and queued.get("source_event_id"):
            return ACTIVITY_SHARE, str(queued["source_event_id"]), None

    if requested in allowed and requested != ACTIVITY_SHARE:
        return requested, "", None

    kind = str(contact_kind or "spontaneous_share")
    if kind == "conversation_followup" and CONVERSATION_FOLLOWUP in allowed:
        return CONVERSATION_FOLLOWUP, "", None
    if kind == "night_wake" and RELATIONSHIP_CONTACT in allowed:
        return RELATIONSHIP_CONTACT, "", None
    if kind == "absence_check" and CASUAL_CONTACT in allowed:
        return CASUAL_CONTACT, "", None
    if kind == "photo_share" and ACTIVITY_SHARE in allowed:
        if artifact_id:
            return ACTIVITY_SHARE, "photo:" + str(artifact_id), None
        return ACTIVITY_SHARE, "", None

    text = str(message or "")
    if RELATIONSHIP_CONTACT in allowed and _AFFECTION.search(text):
        return RELATIONSHIP_CONTACT, "", None
    if CASUAL_CONTACT in allowed:
        return CASUAL_CONTACT, "", None
    if RELATIONSHIP_CONTACT in allowed:
        return RELATIONSHIP_CONTACT, "", None
    if ACTIVITY_SHARE in allowed and len(candidates) == 1:
        candidate = candidates[0]
        return ACTIVITY_SHARE, str(candidate.get("source_event_id") or ""), candidate
    if allowed:
        return allowed[0], "", None
    return CASUAL_CONTACT, "", None


def optional_life_share(item):
    return bool(
        item.get("source_event_id") and item.get("text")
        and item.get("source_type") in ("activity_progress", "daylife_progress")
    )


def life_share_matches(item, source_id, message):
    """Only consume an optional life entry when the sent words still carry its detail."""
    value = str(message or "").strip()
    return bool(value and source_id == str(item.get("source_event_id") or "") and (
        value == str(item.get("text") or "").strip() or note_used({"thought": item.get("text") or ""}, value)
    ))
