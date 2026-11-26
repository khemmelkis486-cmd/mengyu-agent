"""Declare state field ownership and identify the domain attempting a write."""

from contextlib import contextmanager
from contextvars import ContextVar

WRITER = ContextVar("mengyu_state_writer", default="contact")

OWNERS = {
    "turns": "contact",
    "conversation": "contact",
    "facts": "contact",
    "sleep": "contact",
    "deliveries": "contact",
    "delivery_ledger": "contact",
    "owner_presence": "contact",
    "affect": "affect",
    "emotional_memories": "affect",
    "interaction_mood": "affect",
    "last_mood_assessment": "affect",
    "jealousy": "affect",
    "temper": "affect",
    "relationship_feeling": "affect",
    "relationship_feeling_at": "affect",
    "relationship_feeling_source": "affect",
    "relationship_history": "affect",
    "relationship_last_update_at": "affect",
    "life_clock": "life",
    "life_plan": "life",
    "life_now": "life",
    "body_now": "life",
    "weather_now": "life",
    "wish_try_at": "life",
    "pending_spend": "life",
    "budget_tight": "life",
    "home_shortage": "life",
    "mood_low": "life",
    "recent_dishes": "life",
    "shared_materials": "proactive",
    "recent_new_points": "proactive",
    "photo_moment": "life",
    "daily_rhythm": "life",
    "daily_mood": "life",
    "activity": "life",
    "next_life_check": "life",
    "life_plan_cutover_at": "life",
    "life_plan_cutover_closed": "life",
    "last_contact_decision": "proactive",
    "recent_contact_decisions": "proactive",
    "proactive_history": "proactive",
    "last_proactive_ts": "proactive",
    "life_event_uses": "proactive",
    "photo_jobs": "media",
    "own_photo_job": "media",
    "work_task_sync": "task_center",
}


@contextmanager
def writing_as(writer):
    """Attribute writes within a service call, including across awaits."""
    token = WRITER.set(writer)
    try:
        yield
    finally:
        WRITER.reset(token)


async def run_as(writer, action, *args, **kwargs):
    """Run an asynchronous operation with its domain's writer identity."""
    with writing_as(writer):
        return await action(*args, **kwargs)


def violations(fields, writer):
    """Return changed fields that belong to a different domain."""
    return {
        key: owner for key in fields if (owner := OWNERS.get(key, "contact")) != writer
    }
