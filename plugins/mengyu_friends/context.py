"""Compact, grounded friend context for chat and the model tool."""

import re
from datetime import datetime

TOPIC = re.compile(r"朋友|同学|前同事|画手|邻居|聚餐|约见|看展|聚会")
FOLLOWUP = re.compile(
    r"^(?:嗯[嗯]?|那)?[，, ]*(?:后来呢|然后呢|她呢|他呢|那位呢|那次呢|怎么样了|见到了吗)[？?。！!]*$"
)


def match_mentions(people, text, recent_turns=()):
    """Match names, aliases or a short follow-up to a recent friend topic.

    Args:
        people: Stored public friend profiles.
        text: The current message text.
        recent_turns: Prior visible chat turns.

    Returns:
        The match result and matching friend IDs.
    """
    text = str(text or "").strip()
    candidates = [text]
    if FOLLOWUP.fullmatch(text):
        candidates.extend(
            str(row.get("text") or "").strip()
            for row in reversed(list(recent_turns)[-6:])
            if isinstance(row, dict) and str(row.get("text") or "").strip() != text
        )
    for message in candidates:
        identities = [
            row["friend_id"]
            for row in people
            if any(
                str(value) in message
                for value in (row["name"], *row["aliases"])
                if value
            )
        ]
        if identities or TOPIC.search(message):
            return {"matched": True, "friend_ids": identities}
        if message != text and len(message) > 8 and not FOLLOWUP.fullmatch(message):
            break
    return {"matched": False, "friend_ids": []}


def context_for_chat(snapshot, text, recent_turns=(), limit=3):
    """Build a small view of real fictional records relevant to the conversation.

    Args:
        snapshot: Friend service snapshot.
        text: Current message or explicit tool query.
        recent_turns: Prior visible turns.
        limit: Maximum number of records in each group.

    Returns:
        Matched profiles, bookings, and completed meetups.
    """
    people = snapshot["friends"]
    match = match_mentions(people, text, recent_turns)
    if not match["matched"]:
        return {"matched": False, "朋友": [], "约见": [], "最近见面": []}
    selected = set(match["friend_ids"]) or {row["friend_id"] for row in people}
    profiles = [
        {
            "name": row["name"],
            "relation": row["relation"],
            "note": row["current_note"],
            "interests": row["interests"],
        }
        for row in people
        if row["friend_id"] in selected
    ][:limit]
    bookings = []
    for row in snapshot["invitations"]:
        if row["friend_id"] not in selected or row["status"] not in (
            "accepted",
            "scheduled",
        ):
            continue
        scheduled = row["scheduled_start_at"]
        if scheduled:
            when = datetime.fromtimestamp(scheduled).strftime("%m-%d %H:%M")
        else:
            when = "等下次排期"
        bookings.append({"title": row["title"], "status": row["status"], "when": when})
    meetings = [
        {key: row[key] for key in ("title", "place", "outcome", "detail", "follow_up")}
        | {"when": datetime.fromtimestamp(row["completed_at"]).strftime("%m-%d %H:%M")}
        for row in snapshot["meetups"]
        if row["status"] == "completed" and selected.intersection(row["friend_ids"])
    ][:limit]
    return {
        "matched": True,
        "朋友": profiles,
        "约见": bookings[:limit],
        "最近见面": meetings,
    }
