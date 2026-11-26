"""Canonical, immutable projection of Mengyu's current state.

The projection is a read-only view. It copies the source data, derives only
bounded facts, and never writes to the stores it reads.
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime

from data.plugins.mengyu_affect.core.affect_engine import preview as preview_affect, project_interaction
from data.plugins.mengyu_affect.core.interaction_mood import view as interaction_view
from data.plugins.mengyu_affect.core.temper import active as temper_active
from .daily_rhythm import schedule_context
from . import topic_state
from data.plugins.mengyu_media.core.photo_activity import binding_for
from .state_schema import valid as validate_projection

if __package__:
    from .local_clock import TZ
else:
    from local_clock import TZ
SCHEMA_VERSION = 2
RECENT_ACTIVITY_SECONDS = 36 * 3600
MAX_CURRENT_ACTIVITY_AGE = 24 * 3600
PHASE_OK = ("awake", "drowsy", "preparing", "sleeping")


class FrozenDict(dict):
    """JSON-serialisable immutable dict used at decision boundaries."""

    def __init__(self, source=None, **kwargs):
        dict.__init__(self)
        data = dict(source or {})
        data.update(kwargs)
        for key, value in data.items():
            dict.__setitem__(self, key, freeze(value))

    @staticmethod
    def _immutable(*_args, **_kwargs):
        raise TypeError("CurrentState projection is read-only")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable

    def __deepcopy__(self, memo):
        return self


def freeze(value):
    if isinstance(value, FrozenDict):
        return value
    if isinstance(value, dict):
        return FrozenDict(value)
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(freeze(item) for item in value)
    return value


def _number(value, default=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _copy_dict(value):
    return dict(value) if isinstance(value, dict) else {}


def _activity_status(item, now):
    """Derive lifecycle from elapsed phases without mutating the activity."""
    cancelled_at = _number(item.get("cancelled_at"))
    if cancelled_at is not None and cancelled_at <= now:
        return ("paused" if item.get("paused_by_scheduler") else "cancelled"), None, None

    phases = [
        phase for phase in (item.get("phases") or [])
        if isinstance(phase, dict) and _number(phase.get("at")) is not None
    ]
    if phases:
        elapsed = [phase for phase in phases if float(phase["at"]) <= now]
        planning = {"准备出门", "打算做", "临时改主意"}
        active_elapsed = [
            phase for phase in elapsed
            if str(phase.get("stage") or "").strip() not in planning
        ]
        if phases[-1]["at"] <= now:
            return "completed", (
                float(active_elapsed[0]["at"]) if active_elapsed else None
            ), float(phases[-1]["at"])
        if active_elapsed:
            return "in_progress", float(active_elapsed[0]["at"]), None
        return "planned", None, None

    status = str(item.get("activity_status") or item.get("status") or "")
    started_at = _number(item.get("started_at") or item.get("at"))
    completed_at = _number(item.get("completed_at"))
    if status == "in_progress" and started_at is not None and started_at > now:
        return "planned", None, None
    if completed_at is not None and completed_at <= now:
        return "completed", started_at, completed_at
    return status, started_at, completed_at


def _activity_view(item, status, started_at=None, completed_at=None, now=None):
    phases = [phase for phase in (item.get("phases") or ()) if isinstance(phase, dict)
              and _number(phase.get("at")) is not None]
    elapsed = [phase for phase in phases if now is not None and phase["at"] <= now]
    phase = elapsed[-1] if elapsed else {}
    return {
        "activity_id": str(item.get("id") or ""),
        "name": str(item.get("title") or item.get("reason") or item.get("destination") or "")[:120],
        "place": str(phase.get("place") or item.get("place") or item.get("destination") or "")[:80],
        "destination": str(item.get("destination") or "")[:80],
        "phase_place": bool(phase.get("place")),
        "photo_activity": binding_for(item, phase) if status == "in_progress" and phase.get("place") else None,
        "arrivals": [
            {"place": str(previous.get("place") or "")[:80], "at": previous["at"]}
            for previous in elapsed if previous.get("stage") == "到了"
        ],
        "return_origin": next((str(previous.get("place") or "")[:80] for previous in reversed(elapsed)
                               if previous.get("stage") == "到了"), ""),
        "stage": str(phase.get("stage") or ""),
        "action": str(phase.get("reason") or phase.get("detail") or "")[:120],
        "returned_home": status == "completed" and item.get("mode") == "out" and (
            str(phase.get("stage") or "") in ("到家", "回到家", "回到住处", "已回家", "回来了")
            or str(phase.get("place") or "") in ("住处", "家里")
        ),
        "status": status,
        "started_at": started_at if started_at is not None else item.get("started_at") or item.get("at"),
        "completed_at": completed_at if completed_at is not None else item.get("completed_at"),
        "status_updated_at": item.get("status_updated_at") or item.get("paused_at") or item.get("completed_at"),
        "expected_end_at": item.get("expected_end_at") or item.get("end_at"),
        "mode": item.get("mode"),
        "fictional": bool(item.get("fictional", True)),
        "scheduler_task_id": str(item.get("scheduler_task_id") or ""),
    }


def _pick_activity(records, now, scheduler_data=None):
    """Return one unambiguous current activity and recent lifecycle history."""
    paused, completed, running = [], [], []
    conflicts = []
    for item in records:
        if not isinstance(item, dict):
            continue
        status, started_at, completed_at = _activity_status(item, now)
        view = _activity_view(item, status, started_at, completed_at, now)
        stamp = _number(
            item.get("status_updated_at")
            or item.get("paused_at")
            or item.get("completed_at")
            or item.get("started_at")
            or item.get("created_at")
            or item.get("at")
        )
        if status == "in_progress":
            if started_at is None:
                conflicts.append({
                    "type": "current_activity_missing_start_time",
                    "id": view["activity_id"],
                    "resolution": "excluded_from_current",
                })
                continue
            if now - float(started_at) > MAX_CURRENT_ACTIVITY_AGE:
                conflicts.append({
                    "type": "stale_in_progress_activity",
                    "id": view["activity_id"],
                    "started_at": started_at,
                    "resolution": "excluded_from_current",
                })
                continue
            running.append(view)
        elif status == "paused":
            if stamp is None or 0 <= now - stamp <= RECENT_ACTIVITY_SECONDS:
                paused.append(view)
        elif status == "completed":
            if stamp is None or 0 <= now - stamp <= RECENT_ACTIVITY_SECONDS:
                completed.append(view)

    scheduler_data = scheduler_data if isinstance(scheduler_data, dict) else {}
    tasks = [task for task in (scheduler_data.get("tasks") or []) if isinstance(task, dict)]
    active_task_id = scheduler_data.get("active_task_id")
    active_task = next((task for task in tasks if task.get("id") == active_task_id), None)

    current = None
    if len(running) == 1:
        current = running[0]
    elif len(running) > 1:
        linked_id = str((active_task or {}).get("activity_id") or "")
        linked = [item for item in running if item.get("activity_id") == linked_id]
        if len(linked) == 1:
            current = linked[0]
            conflicts.append({
                "type": "multiple_active_activities",
                "ids": [item.get("activity_id") for item in running],
                "resolution": "scheduler_linked_activity_selected",
            })
        else:
            conflicts.append({
                "type": "multiple_active_activities",
                "ids": [item.get("activity_id") for item in running],
                "resolution": "unresolved_no_current_activity",
            })

    paused.sort(key=lambda item: _number(item.get("status_updated_at")) or 0)
    completed.sort(key=lambda item: _number(item.get("completed_at")) or 0)
    return current, paused[-3:], completed[-3:], conflicts, active_task


def photo_activity(activities, scheduler, now):
    current, _, _, _, _ = _pick_activity(activities, now, scheduler)
    if current and current["fictional"]:
        return current["photo_activity"]
    return None


def _sleep_view(state, now):
    clock = _copy_dict(state.get("life_clock"))
    sleep = _copy_dict(state.get("sleep"))
    wake_at = _number(clock.get("wake_at"), 0.0) or 0.0
    bedtime_at = _number(clock.get("bedtime_at"), 0.0) or 0.0
    scheduled = "unknown"
    if wake_at and bedtime_at and bedtime_at > wake_at:
        scheduled = "awake" if wake_at <= now < bedtime_at else "sleeping"
    else:
        phase_hint = str(clock.get("phase") or "")
        if phase_hint in ("awake", "sleeping"):
            scheduled = phase_hint

    active = bool(sleep.get("active"))
    until = _number(sleep.get("until"))
    phase = str(sleep.get("phase") or ("sleeping" if active else "awake"))
    conflicts = []

    if active and until is not None and now >= until:
        actual = "awake"
        conflicts.append({
            "type": "expired_sleep_record",
            "until": until,
            "resolution": "treated_as_awake_read_only",
        })
    elif active:
        actual = phase if phase in PHASE_OK else "sleeping"
        if actual != "sleeping":
            conflicts.append({
                "type": "sleep_active_phase_mismatch",
                "phase": phase,
                "resolution": "active_sleep_record_kept_as_sleeping",
            })
            actual = "sleeping"
    else:
        actual = phase if phase in PHASE_OK else "awake"
        if actual == "sleeping":
            actual = "awake"
            conflicts.append({
                "type": "sleep_phase_without_active_window",
                "resolution": "treated_as_awake_read_only",
            })

    effective = actual
    if actual == "sleeping" and scheduled == "awake" and not bool(sleep.get("nap")):
        effective = "drowsy"
        conflicts.append({
            "type": "sleep_conflicts_schedule",
            "scheduled": scheduled,
            "actual": actual,
            "resolution": "daytime_sleep_requires_nap_marker",
        })

    return {
        "scheduled_phase": scheduled,
        "actual_phase": actual,
        "effective_phase": effective,
        "active": active and (until is None or now < until),
        "phase": actual,
        "since": sleep.get("since"),
        "until": sleep.get("until"),
        "nap": bool(sleep.get("nap")),
        "night_wake_at": sleep.get("night_wake_at"),
        "night_wake": _copy_dict(sleep.get("night_wake")),
        "catchup_status": clock.get("catchup_status"),
        "source": "sleep_state" if active else "life_clock",
    }, conflicts


def project(state, activities_data, scheduler_data, now=None, *, life_context=None, current_life=None):
    """Build a single immutable read-only view for one decision cycle."""
    now = float(time.time() if now is None else now)
    state = state if isinstance(state, dict) else {}
    activities_data = activities_data if isinstance(activities_data, dict) else {}
    scheduler_data = scheduler_data if isinstance(scheduler_data, dict) else {}

    sleep, sleep_conflicts = _sleep_view(state, now)
    current, paused, completed, activity_conflicts, active_task = _pick_activity(
        list(activities_data.get("virtual_life") or []), now, scheduler_data
    )
    conflicts = sleep_conflicts + activity_conflicts

    presence = _copy_dict(state.get("owner_presence"))
    if not presence:
        legacy_paused = bool(state.get("conversation_paused"))
        owner_mode = str(state.get("owner_pause_kind") or ("quiet" if legacy_paused else "open"))
        owner_since = state.get("last_user_ts") if legacy_paused else None
        owner_expires = None
        owner_source = "legacy_mirror" if legacy_paused else "default"
    else:
        owner_mode = str(presence.get("mode") or "open")
        owner_since = presence.get("since")
        owner_expires = presence.get("expires_at")
        owner_source = "owner_presence"

    if owner_mode in ("busy", "rest") and _number(owner_expires) is not None and now >= float(owner_expires):
        owner_mode = "open"
        conflicts.append({
            "type": "expired_owner_pause",
            "source_mode": presence.get("mode"),
            "expires_at": owner_expires,
            "resolution": "treated_as_open_read_only",
        })
    elif owner_mode in ("busy", "rest") and owner_expires is None:
        conflicts.append({
            "type": "temporary_owner_pause_missing_expiry",
            "mode": owner_mode,
            "since": owner_since,
            "resolution": "kept_blocked_until_writer_repairs",
        })

    last_user = _number(state.get("last_user_ts"), 0.0) or 0.0
    if owner_mode in ("busy", "rest") and last_user > (_number(owner_since, 0.0) or 0.0):
        # Presence is not silently cleared just because a new message arrived.
        # The message must have passed the explicit resume classifier.
        conflicts.append({
            "type": "owner_presence_after_new_message",
            "mode": owner_mode,
            "last_user_at": last_user,
            "resolution": "keep_mode_until_explicit_resume_or_expiry",
        })

    tasks = [task for task in (scheduler_data.get("tasks") or []) if isinstance(task, dict)]
    queued = [task for task in tasks if task.get("status") in ("queued", "committed")]
    if scheduler_data.get("active_task_id") and active_task is None:
        conflicts.append({
            "type": "scheduler_active_task_missing",
            "id": str(scheduler_data.get("active_task_id")),
            "resolution": "no_active_scheduler_task",
        })
    active_activity_id = str((active_task or {}).get("activity_id") or "")
    if active_activity_id and current and active_activity_id != current.get("activity_id"):
        conflicts.append({
            "type": "scheduler_activity_mismatch",
            "scheduler_activity_id": active_activity_id,
            "current_activity_id": current.get("activity_id"),
            "resolution": "retain_activity_lifecycle_as_current",
        })

    contact = {
        "enabled": bool(state.get("enabled", True)),
        "last_user_ts": last_user,
        "last_proactive_ts": _number(state.get("last_proactive_ts"), 0.0) or 0.0,
        "answered_tail": _copy_dict(state.get("answered_tail")),
        "followup_due_ts": _number(state.get("followup_due_ts"), 0.0) or 0.0,
        "followup_reply_ts": _number(state.get("followup_reply_ts"), 0.0) or 0.0,
        "followup_user_ts": _number(state.get("followup_user_ts"), 0.0) or 0.0,
        "own_moment_offer": _copy_dict(state.get("own_moment_offer")),
        "last_ack_handled_user_ts": _number(state.get("last_ack_handled_user_ts"), 0.0) or 0.0,
        "absence_due_ts": _number(state.get("absence_due_ts"), 0.0) or 0.0,
        "absence_user_ts": _number(state.get("absence_user_ts"), 0.0) or 0.0,
        "last_absence_user_ts": _number(state.get("last_absence_user_ts"), 0.0) or 0.0,
        "next_check": _number(state.get("next_check"), 0.0) or 0.0,
        "casual_considered_at": _number(state.get("casual_considered_at"), 0.0) or 0.0,
        "self_space": _copy_dict(state.get("self_space")),
        "turns_count": len(state.get("turns") or []),
        "has_turns": bool(state.get("turns")),
        "share_event": _copy_dict(state.get("share_event")),
        "followup_topic": _copy_dict(state.get("followup_topic")),
        "topic": _copy_dict(topic_state.projection(state, now)),
        "life_now": _copy_dict(state.get("life_now")),
        "nudge_topic": _copy_dict(state.get("nudge_topic")),
        "own_photo_job": _copy_dict(state.get("own_photo_job")),
        "proactive_history": [
            _copy_dict(item) for item in (state.get("proactive_history") or [])[-80:]
            if isinstance(item, dict)
        ],
        "morning_greeted_day": state.get("morning_greeted_day"),
    }

    interaction = interaction_view(state, now)
    interaction_mode = state.get("interaction_mood_mode", "observe")
    affect = preview_affect(state.get("affect"), now)
    if interaction_mode == "full":
        project_interaction(affect, interaction)
    history = {
        "emotional_memories": [
            _copy_dict(item) for item in (state.get("emotional_memories") or [])[-80:]
            if isinstance(item, dict) and not item.get("private_interaction")
        ],
        "proactive_history": [
            _copy_dict(item) for item in (state.get("proactive_history") or [])[-80:]
            if isinstance(item, dict)
        ],
    }

    projection = {
        "schema_version": SCHEMA_VERSION,
        "as_of": now,
        "snapshot_id": hashlib.sha1(
            json.dumps(
                {
                    "as_of_ms": int(now * 1000),
                    "last_user_ts": last_user,
                    "activity_id": (current or {}).get("activity_id"),
                    "interaction_mood": (interaction["type"], interaction["source_id"]) if interaction else None,
                    "sleep": (sleep.get("actual_phase"), sleep.get("effective_phase")),
                    "owner_mode": owner_mode,
                    "owner_expires": _number(owner_expires),
                    "contact_enabled": contact.get("enabled"),
                    "contact_due": (contact.get("followup_due_ts"), contact.get("next_check")),
                    "scheduler_active": active_activity_id,
                    "affect_vector": affect.get("vector"),
                },
                sort_keys=True,
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()[:16],
        "mengyu": {
            "sleep": sleep,
            "life_clock": {
                "phase": sleep.get("scheduled_phase"),
                "scheduled_phase": sleep.get("scheduled_phase"),
                "wake_at": _number((state.get("life_clock") or {}).get("wake_at")),
                "bedtime_at": _number((state.get("life_clock") or {}).get("bedtime_at")),
                "catchup_status": sleep.get("catchup_status"),
            },
            "self_space": _copy_dict(state.get("self_space")),
            "mood_type": interaction["type"] if interaction else None,
            "interaction_mood": interaction,
            "temper_active": temper_active(state, now),
            "interaction_mood_mode": interaction_mode,
            "current_life": _copy_dict(current_life) if current_life is not None else _copy_dict(life_context),
            "daily_schedule": schedule_context(state, activities_data.get("virtual_life") or [], now),
            "activity": current,
            "life_context": _copy_dict(life_context),
            "recent_completed": completed,
            "recently_paused": paused,
        },
        "owner": {
            "last_seen_at": last_user or None,
            "last_user_ts": last_user,
            "mode": owner_mode,
            "since": owner_since,
            "expires_at": owner_expires,
            "source": owner_source,
            "explicit_quiet": owner_mode == "quiet",
        },
        "scheduler": {
            "active_task_id": str((active_task or {}).get("id") or "") or None,
            "active_activity_id": active_activity_id or None,
            "active_status": (active_task or {}).get("status"),
            "queued_count": len(queued),
        },
        "contact": contact,
        "affect": affect,
        "history": history,
        "memories": {"emotional": history["emotional_memories"]},
        "consistency": {
            "status": "conflict" if conflicts else "consistent",
            "conflicts": conflicts,
            "stale_sources": [],
            "contract_errors": [],
        },
    }
    errors = validate_projection(projection)
    projection["consistency"]["contract_errors"] = errors
    if errors:
        projection["consistency"]["status"] = "invalid"
    return freeze(projection)


def summary(projection):
    """One compact Chinese paragraph for prompts."""
    mengyu = projection.get("mengyu") or {}
    sleep = mengyu.get("sleep") or {}
    activity = mengyu.get("activity") or {}
    owner = projection.get("owner") or {}
    parts = []
    phase = sleep.get("effective_phase")
    if phase in ("sleeping", "preparing", "drowsy"):
        parts.append("她现在" + {"sleeping": "在睡", "preparing": "准备睡", "drowsy": "有点困但醒着"}[phase])
    else:
        parts.append("她醒着")
    if activity:
        parts.append(
            f"当前活动：{activity.get('name') or '未命名'}"
            + (f"（{activity.get('place')}）" if activity.get("place") else "")
        )
    else:
        parts.append("当前没有可确认的进行中活动")
    if mengyu.get("recently_paused"):
        parts.append("刚暂停：" + "、".join(x.get("name", "") for x in mengyu["recently_paused"]))
    if owner.get("mode") != "open":
        parts.append("你这边：" + str(owner.get("mode")))
    if projection.get("consistency", {}).get("conflicts"):
        parts.append("状态存在冲突，不要替系统猜")
    return "；".join(parts)
