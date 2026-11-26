"""Small, durable memory of emotionally meaningful life events.

Memories are retained after completion. They are only offered as proactive
sharing material while unshared; direct user questions can retrieve history.
"""

import re
import time

from data.plugins.mengyu_affect.core.interaction_mood import TYPES as INTERACTION_TYPES

MEMORY_TYPES = {
    "work_completed", "gift_received", "gift_experienced", "new_discovery",
    "activity_failed", "rest_completed", "social_activity", "personal_choice",
}
STOPWORDS = {
    "完成", "开始", "进行", "发生", "今天", "刚刚", "已经", "用户", "自己",
    "一件", "事情", "活动", "工作", "过程", "感觉", "记录", "一次", "一个",
    "这个", "那个", "有点", "之后", "然后", "因为", "所以", "还是",
    "已经", "收到", "送达", "完成了", "刚刚", "刚才", "喜欢", "开心",
}


def record(state, event_type, detail, *, source_id="", now=None, message_id=None):
    now = time.time() if now is None else float(now)
    event_type = str(event_type or "")
    detail = str(detail or "").strip()[:300]
    if event_type not in MEMORY_TYPES | INTERACTION_TYPES or not detail:
        return False
    memories = state.setdefault("emotional_memories", [])
    source_id = str(source_id or f"{event_type}:{int(now * 1000)}")
    if any(isinstance(row, dict) and row.get("source_id") == source_id for row in memories):
        return False
    memories.append({
        "id": source_id,
        "source_id": source_id,
        "type": event_type,
        "detail": detail,
        "at": now,
        "shared_at": None,
        "recalled_at": None,
    })
    if message_id:
        memories[-1]["message_id"] = str(message_id)
    if event_type in INTERACTION_TYPES:
        memories[-1]["private_interaction"] = True
    state["emotional_memories"] = memories[-100:]
    return True


def _keywords(detail):
    value = str(detail or "")
    runs = re.findall(r"[\u4e00-\u9fff]{2,}", value)
    tokens = set(re.findall(r"[A-Za-z0-9_-]{3,}", value))
    # Chinese does not use spaces between words. Build short substrings so a
    # detail like “银圈已经收到” can be matched by a later “银圈收到了”.
    for run in runs:
        for size in (2, 3, 4):
            for index in range(0, max(0, len(run) - size + 1)):
                tokens.add(run[index:index + size])
    return [token for token in tokens if token not in STOPWORDS and len(token) >= 2]


def mark_shared(state, reply_text, now=None):
    now = time.time() if now is None else float(now)
    text = str(reply_text or "")
    if not text:
        return 0
    changed = 0
    for row in state.get("emotional_memories", []):
        if not isinstance(row, dict) or row.get("shared_at") or row.get("private_interaction"):
            continue
        if now - float(row.get("at", now) or now) > 7 * 86400:
            continue
        detail = str(row.get("detail") or "")
        if detail and detail in text:
            row["shared_at"] = now
            changed += 1
            continue
        keywords = _keywords(detail)
        # Require a distinctive token; generic verbs alone never consume a memory.
        if any(token in text for token in keywords):
            row["shared_at"] = now
            changed += 1
    return changed


def mark_selected_shared(state, source_id, message, now=None):
    """Consume a specifically selected event only if it appeared in sent text.

    Selection, prompting, or an unsuccessful send is never a share.
    """
    source_id = str(source_id or "")
    message = str(message or "")
    if not source_id or not message:
        return False
    now = time.time() if now is None else float(now)
    for row in state.get("emotional_memories", []):
        if not isinstance(row, dict) or row.get("source_id") != source_id or row.get("shared_at"):
            continue
        detail = str(row.get("detail") or "")
        # Single incidental generic words should not consume unrelated memories.
        anchors = [word for word in _keywords(detail) if len(word) >= 2]
        if detail in message or (anchors and any(word in message for word in anchors)):
            row["shared_at"] = now
            row["shared_message"] = message[:200]
            return True
    return False


def context(state, now=None, query_text="", limit=5):
    now = time.time() if now is None else float(now)
    query = str(query_text or "")
    rows = [row for row in state.get("emotional_memories", [])
            if isinstance(row, dict) and not row.get("private_interaction")]
    output = []
    seen = set()
    for row in reversed(rows):
        age = now - float(row.get("at", now) or now)
        relevant = bool(query and any(token in query for token in _keywords(row.get("detail", ""))))
        if age < 0 or age > 90 * 86400:
            continue
        if row.get("shared_at") and not relevant:
            continue
        if not row.get("shared_at") and age > 7 * 86400 and not relevant:
            continue
        key = row.get("source_id") or row.get("id")
        if key in seen:
            continue
        seen.add(key)
        output.append({
            "type": row.get("type"),
            "detail": row.get("detail"),
            "age_hours": round(age / 3600, 1),
            "already_shared": bool(row.get("shared_at")),
            "rule": "未分享经历可以作为自然分享素材；已分享经历仅在用户问起或出现新进展时回忆。",
        })
        if len(output) >= max(1, min(10, int(limit))):
            break
    return output
