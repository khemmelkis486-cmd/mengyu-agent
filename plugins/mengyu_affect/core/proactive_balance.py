"""Source-aware proactive selection.  Never treat work progress as a social quota.

The selector only offers genuine, persisted material.  A content share is committed
by the delivery path, not by selecting an item or generating draft text.
"""

import time
import re

WORK_TYPES = frozenset(("work_progress", "work_completed"))
GIFT_HISTORY_TYPES = frozenset(("gift_received", "gift_experienced"))


def classify_delivered_topic(message, source_event_id=""):
    """Conservative work classifier for incorrectly labeled casual messages."""
    text = str(message or "")
    if str(source_event_id or "").startswith("work:"):
        return "work"
    if re.search(r"(?:稿子|客户|甲方|工单|工作|项目|海报|排版|设计稿|文案).{0,20}(?:改|修|做|完成|推进|交|调整|定稿|画|排)", text) or re.search(r"(?:改|修|做|完成|推进|交|调整|定稿|画|排).{0,12}(?:稿子|客户|甲方|工单|工作|项目|海报|排版|设计稿|文案)", text):
        return "work"
    return "personal"


def is_work(item):
    if not isinstance(item, dict):
        return False
    if (str(item.get("source_event_id") or "").startswith("work:")
            or str(item.get("activity_kind") or item.get("type") or "") in WORK_TYPES):
        return True
    # Legacy daily_moment records sometimes describe freelance jobs without
    # the structured work_progress tag. Do not rebrand those as personal life.
    content = " ".join(str(item.get(key) or "") for key in ("activity", "thought", "detail"))
    return classify_delivered_topic(content) == "work"


def recent_work_count(history, now, hours=6):
    """Count actual delivered proactive work shares, not failed drafts."""
    count = 0
    for item in history or ():
        if not isinstance(item, dict) or not isinstance(item.get("at"), (int, float)):
            continue
        if not 0 <= now - item["at"] <= hours * 3600:
            continue
        if item.get("delivery") not in (None, "complete", "sent"):
            continue
        if is_work(item) or item.get("topic_family") == "work":
            count += 1
    return count


def select_candidates(candidates, history=(), now=None, limit=4):
    """Prefer distinct personal life moments; keep a restrained work option.

    A run of work updates must not starve daily life.  We do not force any
    message or fabricate a personal event when none exists.
    """
    now = time.time() if now is None else float(now)
    valid = [c for c in candidates or () if isinstance(c, dict) and c.get("source_event_id")]
    personal = [c for c in valid if not is_work(c)]
    work = [c for c in valid if is_work(c)]
    personal.sort(key=lambda c: float(c.get("at") or 0), reverse=True)
    work.sort(key=lambda c: float(c.get("at") or 0), reverse=True)
    recent = recent_work_count(history, now)
    chosen = personal[:max(1, int(limit))]
    # Work is optional after a prior work announcement: a new phase does not
    # automatically earn a new unsolicited message.
    if recent == 0 and len(chosen) < limit and work:
        chosen.append(work[0])
    return chosen[:max(1, int(limit))]


def pick_memory(memories, history, now):
    """Prefer personal memories and rotate away from merely offered items."""
    recent_work = recent_work_count(history, now)
    candidates = []
    for row in memories or ():
        if not isinstance(row, dict) or row.get("shared_at"):
            continue
        at = row.get("at")
        if not isinstance(at, (int, float)) or not 0 <= now - at <= 72 * 3600:
            continue
        offered_at = row.get("offered_at")
        if isinstance(offered_at, (int, float)) and 0 <= now - offered_at < 2 * 3600:
            continue
        if row.get("type") in GIFT_HISTORY_TYPES:
            # Gifts already have a dedicated acknowledgement/receipt flow;
            # retain the memory for recall without sending it again as news.
            continue
        if is_work(row) and recent_work:
            continue
        candidates.append(row)
    if not candidates:
        return None
    # Personal experiences outrank work. Within one category, recency matters.
    return max(candidates, key=lambda r: (not is_work(r), float(r.get("at") or 0)))


def note_memory_offered(state, decision, now):
    """Reserve a decision opportunity; not proof of a successful share."""
    source_id = str((decision or {}).get("memory_source_id") or "")
    if not source_id:
        return False
    for row in state.get("emotional_memories") or ():
        if isinstance(row, dict) and str(row.get("source_id") or "") == source_id and not row.get("shared_at"):
            row["offered_at"] = float(now)
            return True
    return False


def memory_context_for_contact(projection, decision, now, limit=4):
    """Read contact memories from the same immutable projection as the decision."""
    selected = str((decision or {}).get("memory_source_id") or "")
    memories = ((projection.get("memories") or {}).get("emotional") or ())
    rows = []
    for row in reversed(memories):
        if not isinstance(row, dict) or row.get("shared_at"):
            continue
        at = row.get("at")
        if not isinstance(at, (int, float)) or not 0 <= now - at <= 24 * 3600:
            continue
        if row.get("type") in GIFT_HISTORY_TYPES:
            continue
        if is_work(row) and row.get("source_id") != selected:
            continue
        rows.append({"type": row.get("type"), "detail": row.get("detail"),
                     "source_id": row.get("source_id")})
        if len(rows) >= limit:
            break
    return rows
