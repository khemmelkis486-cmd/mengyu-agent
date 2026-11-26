"""Separate saved historical attempts from active interests and future work."""

import re
import time


def active_records(records):
    """Return current records without modifying or deleting saved history.

    Args:
        records: Saved activity, project, learning or reflection records.

    Returns:
        Records that have not been explicitly retired.
    """
    return [item for item in records if not item.get("retired_at")]


def interest_context(state):
    """Read saved personal views without turning them into claimed play history.

    Args:
        state: Companion state containing grounded interest threads.

    Returns:
        Up to eight current topic views with their original activity provenance.
    """
    return [
        {
            key: item.get(key)
            for key in (
                "topic",
                "stance",
                "curiosity",
                "activity_id",
                "activity_kind",
                "at",
            )
        }
        for item in state.get("interest_threads", [])
        if isinstance(item, dict)
        and not item.get("retired_at")
        and isinstance(item.get("topic"), str)
        and item["topic"]
        and isinstance(item.get("stance"), str)
        and item["stance"]
    ][-8:]


def retired_paper_topic(text):
    """Recognize the specific abandoned craft, without blocking game mechanisms.

    Args:
        text: Generated topic text or serialized generated activity metadata.

    Returns:
        Whether it refers to the retired paper-craft topic.
    """
    return bool(
        re.search(r"折纸|纸艺|贺卡|花朵卡|纸舌|茶包纸套|纸扣|纸机关|纸套", str(text))
        or ("草图" in str(text) and "转轴" in str(text))
    )


SUBJECT_ALIASES = {
    "画中世界": "gorogoa",
    "星露谷": "星露谷物语",
}

SUBJECT_STOPWORDS = {
    "https",
    "http",
    "html",
    "about",
    "other",
    "worlds",
    "review",
    "steam",
    "google",
    "apple",
}


def subject_labels(text):
    """Name the works or topics a title mentions.

    Args:
        text: Activity title, plan title, or short query.

    Returns:
        (key, label) pairs; keys are lowercased names used for counting.
    """
    text = str(text)
    found = []
    seen = set()
    for name in re.findall(r"《([^》]{1,40})》", text):
        label = name.strip()
        key = SUBJECT_ALIASES.get(label.lower(), label.lower())
        if key and key not in seen:
            seen.add(key)
            found.append((key, label))
    for name in re.findall(r"[A-Za-z][A-Za-z0-9'\-]{4,}", text):
        key = SUBJECT_ALIASES.get(name.lower(), name.lower())
        if len(key) < 5 or key in SUBJECT_STOPWORDS or key in seen:
            continue
        seen.add(key)
        found.append((key, name))
    return found


def subject_mentions(records, since, now=None):
    """Count how often each subject appears in recent activity records.

    Args:
        records: Reflection records carrying `activity` and `at`.
        since: Lower Unix bound; older records are ignored.
        now: Optional upper bound, defaulting to the current time.

    Returns:
        Dicts with key, name, count and last timestamp, most frequent first.
    """
    now = time.time() if now is None else now
    found = {}
    for item in records:
        at = item.get("at") or 0
        if at < since or at > now:
            continue
        for key, label in subject_labels(item.get("activity", "")):
            entry = found.setdefault(
                key, {"key": key, "name": label, "count": 0, "last": 0}
            )
            entry["count"] += 1
            entry["last"] = max(entry["last"], at)
    return sorted(found.values(), key=lambda item: (-item["count"], -item["last"]))
