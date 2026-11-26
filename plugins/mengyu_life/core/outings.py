"""Outing-decision scheduling helpers."""

from datetime import datetime, timedelta

from data.plugins.mengyu_contact.policy import TZ

SLOTS = (10, 15, 20)


def next_out_slot(now):
    """Return the next 10:00, 15:00 or 20:00 in Mengyu's timezone."""
    moment = datetime.fromtimestamp(now, TZ)
    for hour in SLOTS:
        slot = moment.replace(hour=hour, minute=0, second=0, microsecond=0)
        if slot > moment:
            return slot.timestamp()
    tomorrow = (moment + timedelta(days=1)).replace(
        hour=SLOTS[0], minute=0, second=0, microsecond=0
    )
    return tomorrow.timestamp()


def claim_outing_decision(state, now):
    """Claim a due decision slot before asking the planner, preventing repeats.

    The slot is advanced even if the planner later chooses to continue an
    existing activity or stay home.
    """
    if now < state.get("out_check_at", 0):
        return False
    state["out_check_at"] = next_out_slot(now)
    state["out_check_claimed_at"] = now
    return True
