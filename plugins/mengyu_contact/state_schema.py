"""Separate contracts for persisted state and immutable projections."""

from collections.abc import Mapping

if __package__:
    from . import topic_state
    from .state_owners import OWNERS
else:
    import topic_state
    from state_owners import OWNERS

SCHEMA_VERSION = 2
STATE_VERSION = 2
STATE_FIELDS = OWNERS.keys() | set(
    """
absence_due_ts absence_user_ts activity_notice_promises activity_notices answered_tail
casual_considered_at contact_rhythm_version conversation_paused correction_note count day
delivery_status enabled event_candidates exploration_version face_expected followup_due_ts
followup_reply_delivery followup_reply_ts followup_thread followup_topic followup_user_ts
gift_mentions hobby_update interaction_mood_mode interest_threads last_absence_user_ts
last_ack_handled_user_ts last_activity_attempt last_contact_life_phase last_meme_delivery
last_meme_file last_message last_message_mode last_message_time last_own_photo_at
last_reply_choice last_silent_ts last_user_ts life_rhythm_version meal_endings
meal_location_preference meal_progress morning_greeted_day next_check night_pause_released_at
nudge_topic out_check_at out_check_claimed_at outing_photo_day own_choice own_moment_offer
own_photo_budget owner_pause_expired_at owner_pause_kind owner_rest_reason pending_gift_ack_ids
pending_gift_candidate pending_question personality_mode photo_focus photo_moment photo_promise
plain_since_face recent_inbound_ids recent_meme_categories recent_meme_files recovered_message_ids
retired_interest_context retired_topics search_backend self_space share_event shopping
shopping_preferences version work_artifact_pending_delivery world_notes_day
""".split()
)
STATE_TYPES = {
    "enabled": bool,
    "conversation_paused": bool,
    "turns": list,
    "conversation": dict,
    "life_now": dict,
    "life_clock": dict,
    "owner_presence": dict,
    "sleep": dict,
    "delivery_ledger": dict,
    "emotional_memories": list,
}


def prepare_state(raw):
    """Migrate and validate a copy, returning it with field-level diagnostics.

    Args:
        raw: Persisted state or an in-memory state about to be saved.

    Returns:
        A writable dictionary and redacted field diagnostics.

    Raises:
        ValueError: The document is invalid or written by an unsupported version.
    """
    if not isinstance(raw, dict):
        raise ValueError("Contact state must be a JSON object")
    version = raw.get("version", 1)
    if type(version) is not int or version not in (1, STATE_VERSION):
        raise ValueError("Unsupported Contact state version")
    issues = []
    state = {}
    for key, value in raw.items():
        if key not in STATE_FIELDS:
            issues.append({"field": key, "reason": "unknown_field"})
        elif key in STATE_TYPES and not isinstance(value, STATE_TYPES[key]):
            issues.append({"field": key, "reason": "invalid_type"})
        elif key == "conversation" and (
            value.get("schema_version") != topic_state.SCHEMA_VERSION
            or not isinstance(value.get("topics"), list)
        ):
            issues.append({"field": key, "reason": "invalid_topic_schema"})
        else:
            state[key] = value
    if version == 1 or "conversation" not in state:
        topic_state.initialize(state)
    state.setdefault("life_now", {})
    state["version"] = STATE_VERSION
    return state, issues


REQUIRED_TOP = (
    "schema_version",
    "snapshot_id",
    "as_of",
    "mengyu",
    "owner",
    "scheduler",
    "contact",
    "affect",
    "memories",
    "consistency",
)
REQUIRED_MENGYU = (
    "sleep",
    "life_clock",
    "activity",
    "recent_completed",
    "recently_paused",
)
PHASES = ("awake", "drowsy", "preparing", "sleeping")


def valid(projection):
    """Return contract violations without mutating the supplied projection."""
    problems = []
    if not isinstance(projection, Mapping):
        return ["not_a_mapping"]
    for key in REQUIRED_TOP:
        if key not in projection:
            problems.append("missing:" + key)
    mengyu = projection.get("mengyu") or {}
    if not isinstance(mengyu, Mapping):
        return problems + ["bad:mengyu"]
    for key in REQUIRED_MENGYU:
        if key not in mengyu:
            problems.append("missing:mengyu." + key)
    sleep = mengyu.get("sleep") or {}
    if not isinstance(sleep, Mapping):
        problems.append("bad:mengyu.sleep")
    elif sleep.get("effective_phase") not in PHASES:
        problems.append("bad_effective_phase")
    if not isinstance(projection.get("as_of"), (int, float)):
        problems.append("bad:as_of")
    if not str(projection.get("snapshot_id") or ""):
        problems.append("bad:snapshot_id")
    for key in ("owner", "scheduler", "contact", "affect", "memories", "consistency"):
        if key in projection and not isinstance(projection.get(key), Mapping):
            problems.append("bad:" + key)
    return problems
