"""Read-only model tools for the four Mengyu companion plugins."""

from . import config

import asyncio
import json
import math
import time
from datetime import datetime, timedelta
from pathlib import Path

from astrbot.api import logger

from data.plugins.mengyu_media.core import photo_activity
from .current_state import project
from data.plugins.mengyu_media.core.daily_photos import shareable_image
from .local_clock import TZ
from .plugin_services import active_plugin
from .policy import eligible

DATA = config.path()
OWNER = config.owner_id()
ORIGIN = config.message_origin()
MAX_RESULT_CHARS = 500


class ToolDataError(ValueError):
    """A required source or field is missing or invalid."""


def _dump(payload):
    return json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def _result(payload):
    """Keep JSON valid and retain total row counts when the output is shortened."""
    result = _dump(payload)
    for key in ("schedule", "pending", "recent", "memories"):
        rows = payload.get(key)
        if len(result) <= MAX_RESULT_CHARS or not rows:
            continue
        payload.setdefault(key + "_count", len(rows))
        while rows and len(result) > MAX_RESULT_CHARS:
            rows.pop()
            result = _dump(payload)
    if len(result) <= MAX_RESULT_CHARS:
        return result
    return _dump({"ok": False, "error": "result_too_large"})


def _field(source, key, expected_type):
    value = source.get(key)
    if not isinstance(value, expected_type):
        raise ToolDataError(f"missing_or_invalid:{key}")
    return value


def _timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if math.isfinite(value) and 0 < value <= 253402214399:
            return value
    return None


def _time(value):
    stamp = _timestamp(value)
    return (
        datetime.fromtimestamp(stamp, TZ).isoformat(timespec="minutes")
        if stamp
        else None
    )


def _text(value, limit):
    return value.strip()[:limit] if isinstance(value, str) else ""


def _load(root, filename, live):
    if isinstance(live, dict):
        return live
    try:
        data = json.loads((root / filename).read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ToolDataError(f"data_missing:{filename}") from exc
    except (OSError, ValueError) as exc:
        raise ToolDataError(f"data_unreadable:{filename}") from exc
    if not isinstance(data, dict):
        raise ToolDataError(f"data_invalid:{filename}")
    return data


def _life(state, activities, scheduler, now):
    clock = _field(state, "life_clock", dict)
    if not any(_timestamp(clock.get(key)) for key in ("wake_at", "bedtime_at")):
        raise ToolDataError("missing_or_invalid:life_clock")
    timeline = _field(activities, "virtual_life", list)
    tasks = _field(scheduler, "tasks", list)
    projection = project(state, activities, scheduler, now)
    if any(
        conflict["resolution"] == "unresolved_no_current_activity"
        for conflict in projection["consistency"]["conflicts"]
    ):
        raise ToolDataError("conflicting_current_activities")
    view = projection["mengyu"]
    current = view["activity"]
    activity = None
    if current:
        end = _timestamp(current.get("expected_end_at"))
        if end is None:
            record = next(
                (
                    row
                    for row in timeline
                    if isinstance(row, dict) and row.get("id") == current["activity_id"]
                ),
                {},
            )
            end = max(
                (
                    _timestamp(phase.get("at")) or 0
                    for phase in record.get("phases") or ()
                    if isinstance(phase, dict)
                ),
                default=0,
            )
        activity = {
            "name": _text(current.get("name"), 44) or None,
            "place": _text(current.get("place"), 24) or None,
            "started_at": _time(current.get("started_at")),
            "expected_end_at": _time(end) if end and end > now else None,
            "status": "in_progress",
        }

    today = datetime.fromtimestamp(now, TZ).date()
    midnight = datetime.combine(today, datetime.min.time(), TZ).timestamp()
    tomorrow = datetime.combine(
        today + timedelta(days=1), datetime.min.time(), TZ
    ).timestamp()
    agenda = state.get("daily_rhythm") or {}
    anchored = {
        row.get("daily_anchor_id")
        for row in timeline
        if isinstance(row, dict) and row.get("daily_anchor_id")
    }
    schedule = []

    def add(label, start, status):
        short_label = _text(label, 33)
        stamp = _timestamp(start)
        if short_label and stamp and midnight <= stamp < tomorrow:
            schedule.append(
                (stamp, {"name": short_label, "time": _time(stamp), "status": status})
            )

    if agenda.get("day") == today.isoformat():
        for slot in agenda.get("schedule") or ():
            if (
                slot.get("id") not in anchored
                and slot.get("id") not in (agenda.get("started") or {})
                and (_timestamp(slot.get("latest_start")) or 0) > now
            ):
                add(
                    (slot.get("plan") or {}).get("reason"),
                    slot.get("start_at"),
                    "planned",
                )
    for row in timeline:
        if not isinstance(row, dict) or row.get("cancelled_at"):
            continue
        next_phase = min(
            (
                phase["at"]
                for phase in row.get("phases") or ()
                if isinstance(phase, dict) and (_timestamp(phase.get("at")) or 0) > now
            ),
            default=None,
        )
        if next_phase and (not current or row.get("id") != current["activity_id"]):
            add(
                row.get("title") or row.get("reason") or row.get("destination"),
                next_phase,
                "planned",
            )
    timeline_ids = {
        row.get("id") for row in timeline if isinstance(row, dict) and row.get("id")
    }
    for task in tasks:
        if not isinstance(task, dict) or task.get("activity_id") in timeline_ids:
            continue
        status = task.get("status")
        if status in ("queued", "committed", "paused"):
            add(task.get("label"), task.get("due_at"), status)
    for key, label in (("wake_at", "起床"), ("bedtime_at", "就寝")):
        stamp = _timestamp(clock.get(key))
        if stamp and stamp > now:
            add(label, stamp, "planned")
    schedule.sort(key=lambda item: item[0])
    rows = list({(row["name"], row["time"]): row for _, row in schedule}.values())
    return {
        "activity": activity,
        "phase": view["sleep"]["effective_phase"],
        "schedule": rows,
    }


def _proactive(action, state, now, core):
    if not any(key in state for key in ("next_check", "last_proactive_ts")):
        raise ToolDataError("missing_or_invalid:contact_schedule")
    projection = project(state, {}, {}, now)
    contact = projection["contact"]
    allowed, reason = eligible(projection, now)
    if allowed and getattr(getattr(core, "inbox", None), "pending", None):
        allowed, reason = False, "有待回复的消息"
    payload = {
        "last_proactive_at": _time(contact["last_proactive_ts"]),
        "next_check_at": _time(contact["next_check"]),
        "allowed": allowed,
        "reason": _text(reason, 64),
        "owner_mode": projection["owner"]["mode"],
    }
    if action == "next":
        next_check = _timestamp(contact["next_check"])
        payload["check_due"] = next_check <= now if next_check is not None else None
    return payload


def _affect(action, state, now):
    raw = _field(state, "affect", dict)
    source = _field(raw, "vector", dict)
    if not source:
        raise ToolDataError("missing_or_invalid:vector")
    rows = (
        _field(state, "emotional_memories", list)
        if "emotional_memories" in state
        else []
    )
    # The normal prompt projection keeps private interaction records internal.
    memories = sorted(
        (
            row
            for row in rows
            if isinstance(row, dict)
            and not row.get("private_interaction")
            and 0 < (_timestamp(row.get("at")) or 0) <= now
        ),
        key=lambda row: row["at"],
        reverse=True,
    )
    result = [
        {
            "type": _text(row.get("type"), 24),
            "detail": _text(row.get("detail"), 19),
            "at": _time(row["at"]),
        }
        for row in memories[:3]
    ]
    if action == "memories":
        return {"memories": result}
    affect = project(state, {}, {}, now)["affect"]
    moods = (affect.get("last_mood"), state.get("daily_mood"))
    recorded = [
        row
        for row in moods
        if isinstance(row, dict)
        and 0 <= now - (_timestamp(row.get("at")) or 0) <= 43200
    ]
    mood = max(recorded, key=lambda row: row["at"], default={})
    feeling = _text(mood.get("feeling"), 23)
    vector = {key: round(value, 1) for key, value in affect["vector"].items()}
    return {"vector": vector, "mood": feeling or None, "memories": result}


def _photo_view(row, creations, binding, now):
    artifact = row.get("artifact")
    present = False
    if isinstance(artifact, str) and artifact:
        path = Path(artifact)
        path = (path if path.is_absolute() else creations / path).resolve()
        present = (
            path.is_relative_to(creations)
            and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
            and path.is_file()
        )

    review = row.get("image_review") or {}
    matched = review.get("matches_scene", review.get("matches_request"))
    bound = row.get("photo_activity")
    if bound and review:
        matched = photo_activity.verified_review(row.get("photo_review_binding") or bound, review)
    review_status = (
        "passed" if matched is True else "failed" if matched is False else "unknown"
    )
    if row.get("delivery_blocked") and review_status == "unknown":
        review_status = "blocked"

    send = row.get("delivery_status")
    if row.get("shared_at") or send == "sent":
        send = "sent"
    elif row.get("delivery_blocked"):
        send = "blocked"
    elif row.get("retired_at"):
        send = "retired"
    elif send not in (
        "reserved",
        "sending",
        "failed",
        "uncertain",
        "invalid",
        "blocked",
    ):
        if not present:
            send = "file_missing"
        elif review_status in ("failed", "blocked"):
            send = "blocked"
        elif row.get("sharing_origin") == photo_activity.LIFE_PHOTO_ORIGIN:
            send = "pending" if shareable_image(row, now) else "expired"
        elif bound and bound != binding:
            send = "expired"
        elif row.get("delivery_pending") or shareable_image(row, now):
            send = "pending" if photo_activity.can_deliver(row, binding) else "blocked"
        else:
            send = "unsent"
    return {
        "id": _text(row.get("id"), 36),
        "title": _text(row.get("title"), 10) or None,
        "at": _time(row.get("at")),
        "generation": "generated" if present else "file_missing",
        "review": review_status,
        "send": send,
    }


def _media(action, state, activities, scheduler, root, now):
    creations = (root / "creations").resolve()
    if not creations.is_dir() or not creations.is_relative_to(root.resolve()):
        raise ToolDataError("data_missing:creations")
    history = _field(activities, "history", list)
    timeline = _field(activities, "virtual_life", list)
    current = project(state, {"virtual_life": timeline}, scheduler, now)["mengyu"][
        "activity"
    ]
    current_photo = (
        current["photo_activity"] if current and current["fictional"] else None
    )
    photos = sorted(
        (
            row
            for row in history
            if isinstance(row, dict)
            and row.get("kind") == "image"
            and row.get("image_mode") != "artwork"
            and row.get("review_kind") != "artwork"
            and row.get("visibility") != "private"
        ),
        key=lambda row: _timestamp(row.get("at")) or 0,
        reverse=True,
    )
    views = [_photo_view(row, creations, current_photo, now) for row in photos]
    pending = [row for row in views if row["send"] == "pending"]
    if action == "pending":
        return {"pending": pending[:3], "pending_count": len(pending)}
    if action == "recent":
        return {"recent": views[:3]}
    return {
        "jobs": {
            "own": _text((state.get("own_photo_job") or {}).get("status"), 20) or None,
            "promise": _text((state.get("photo_promise") or {}).get("status"), 20)
            or None,
        },
        "pending_count": len(pending),
        "recent": views[:3],
    }


ACTIONS = {
    "life": ("status", "schedule"),
    "proactive": ("status", "next"),
    "affect": ("status", "memories"),
    "media": ("status", "pending", "recent"),
}


def _query(tool, action, core):
    root = Path(getattr(core, "data_root", DATA))
    now = time.time()
    state = _load(root, "state.json", getattr(core, "state", None))
    if tool == "proactive":
        return _proactive(action, state, now, core)
    if tool == "affect":
        return _affect(action, state, now)
    activities = _load(
        root,
        "activities.json",
        getattr(getattr(core, "activities", None), "data", None),
    )
    scheduler = _load(
        root,
        "activity_schedule.json",
        getattr(getattr(core, "activity_scheduler", None), "data", None),
    )
    if tool == "media":
        return _media(action, state, activities, scheduler, root, now)
    return _life(state, activities, scheduler, now)


async def read_tool(plugin, event, tool, action):
    """Query the registered live core or the persisted data without state writes.

    Args:
        plugin: Plugin instance that owns the registered tool.
        event: AstrBot event used to check the private owner conversation.
        tool: Name of the read-only view.
        action: Requested operation within the view.

    Returns:
        Compact JSON containing a read-only result or a stable error code.
    """
    try:
        if not plugin.ready:
            return _dump({"ok": False, "error": "plugin_unavailable"})
        if (
            str(event.unified_msg_origin) != ORIGIN
            or str(event.get_sender_id()) != OWNER
        ):
            return _dump({"ok": False, "error": "owner_private_chat_required"})
        action = action.strip().lower() if isinstance(action, str) else ""
        if action not in ACTIONS[tool]:
            return _dump(
                {"ok": False, "error": "unsupported_action", "actions": ACTIONS[tool]}
            )
        context = plugin.context
        core = active_plugin(context, "mengyu_contact")
        if getattr(core, "ready", False):
            payload = _query(tool, action, core)
        else:
            payload = await asyncio.to_thread(_query, tool, action, None)
        return _result({"ok": True, **payload})
    except ToolDataError as exc:
        return _dump({"ok": False, "error": str(exc)})
    except Exception as exc:  # noqa: BLE001 - tool errors must not interrupt the chat
        logger.warning(
            "[Mengyu read tools] %s lookup failed: %s", tool, type(exc).__name__
        )
        return _dump({"ok": False, "error": "data_unavailable"})
