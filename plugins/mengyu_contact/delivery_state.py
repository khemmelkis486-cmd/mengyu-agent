"""Keep segment confirmations separate from transient send results."""

from collections.abc import Mapping


def accepted(result):
    if isinstance(result, Mapping):
        status = str(result.get("status") or "").lower()
        code = result.get("retcode")
        if status in ("failed", "error", "rejected") or code not in (None, 0, "0"):
            return False
        return bool(status in ("ok", "success", "sent") or result.get("success") is True or platform_id(result))
    return bool(result)


def explicitly_failed(result):
    """Do not interpret a missing or ambiguous result as a safe retry."""
    if isinstance(result, Mapping):
        status = str(result.get("status") or "").lower()
        return (status in ("failed", "error", "rejected") or result.get("success") is False
                or result.get("retcode") not in (None, 0, "0"))
    return result is False


def platform_id(result):
    if isinstance(result, Mapping):
        nested = result.get("data")
        value = result.get("message_id") or (nested.get("message_id") if isinstance(nested, Mapping) else None)
    else:
        value = getattr(result, "message_id", None)
    return str(value)[:100] if value is not None else None


def confirm(entry, index, text, stamp, media=(), receipt=None):
    segments = entry.setdefault("segments", {})
    key = str(index)
    old = segments.get(key) or {}
    if old.get("status") == "confirmed":
        return False
    segments[key] = {
        "status": "confirmed", "at": stamp, "text": str(text or "")[:1200],
        "media": list(media), "platform_message_id": receipt,
    }
    current = [
        segments[key]["text"] for key in sorted(segments, key=int)
        if segments[key].get("status") == "confirmed" and segments[key].get("text")
    ]
    entry["text"] = "\n".join(current)[:6000]
    return True


def mark(entry, index, status, stamp):
    segments = entry.setdefault("segments", {})
    key = str(index)
    existing = (segments.get(key) or {}).get("status")
    if existing == "confirmed" or existing == status:
        return False
    segments[key] = {"status": status, "at": stamp}
    return True


def recover(entry, now):
    changed = False
    for segment in (entry.get("segments") or {}).values():
        if isinstance(segment, dict) and segment.get("status") == "reserved":
            segment.update(status="uncertain", recovered_at=now)
            changed = True
    if entry.get("status") == "reserved":
        entry.update(status="uncertain", recovered_at=now)
        changed = True
    return changed
