"""Independent fictional-life clock, separate from the owner's app/PC uptime.

The clock stores a stable daily wake/bedtime schedule. When AstrBot starts after
her wake time, it opens one bounded offline catch-up window; the planner may
record a few fictional personal activities that fit entirely inside that elapsed
window. Empty time is valid and is never filled automatically.
"""

import random
from datetime import datetime, time as clock_time, timedelta

if __package__:
    from .local_clock import TZ
else:
    from local_clock import TZ
SCHEMA_VERSION = 1
MAX_CATCHUP_HOURS = 10
MAX_CATCHUP_ACTIVITIES = 3

# The daily plan is the only schedule source. These defaults are used only
# before the first plan exists.
DEFAULT_WAKE = (8, 30)
DEFAULT_BED = (0, 40)


def _local(ts):
    return datetime.fromtimestamp(float(ts), TZ)


def _format(ts):
    return _local(ts).strftime("%Y-%m-%d %H:%M") if ts else None


def _default_schedule(day):
    wake = datetime.combine(day, clock_time(*DEFAULT_WAKE), TZ)
    bedtime = datetime.combine(
        day + timedelta(days=1), clock_time(*DEFAULT_BED), TZ
    )
    return wake.timestamp(), bedtime.timestamp()


def _plan_schedule(state):
    """Wake/bedtime decided by the autonomous day plan, if available."""
    plan = state.get("life_plan") or {}
    wake = plan.get("wake_at")
    bedtime = plan.get("bedtime_at")
    if isinstance(wake, (int, float)) and isinstance(bedtime, (int, float)):
        if bedtime > wake:
            return float(wake), float(bedtime)
    return None


def ensure_clock(state, now=None, sleep_until=None):
    """Ensure today's stable schedule and update its offline catch-up window."""
    now = float(datetime.now(TZ).timestamp() if now is None else now)
    local_now = _local(now)
    day = local_now.date()
    # Before 06:00 she is still in the previous wake-day's night, so the
    # schedule must not roll over to a new morning until she wakes.
    if local_now.hour < 6:
        day = day - timedelta(days=1)
    day_key = day.isoformat()
    clock = state.get("life_clock")
    if not isinstance(clock, dict) or clock.get("day") != day_key:
        history = list((clock or {}).get("history") or [])[-6:]
        if isinstance(clock, dict) and clock.get("catchup_status") == "pending":
            history.append({
                "day": clock.get("day"),
                "status": "expired_unreconstructed",
                "note": "插件未运行期间没有足够证据补写具体经历，保留为空档",
            })
        planned = _plan_schedule(state)
        wake_at, bedtime_at = planned if planned else _default_schedule(day)
        clock = {
            "schema_version": SCHEMA_VERSION,
            "day": day_key,
            "wake_at": wake_at,
            "bedtime_at": bedtime_at,
            "catchup_status": "not_due",
            "catchup_from": wake_at,
            "catchup_until": None,
            "catchup": None,
            "history": history[-7:],
        }
        state["life_clock"] = clock

    wake_at = float(clock.get("wake_at") or 0)
    bedtime_at = float(clock.get("bedtime_at") or 0)
    if isinstance(sleep_until, (int, float)) and sleep_until > wake_at and sleep_until < bedtime_at:
        wake_at = float(sleep_until)
        clock["wake_at"] = wake_at
        clock["catchup_from"] = wake_at
    if not wake_at or not bedtime_at or bedtime_at <= wake_at:
        planned = _plan_schedule(state)
        wake_at, bedtime_at = planned if planned else _default_schedule(day)
        clock.update(wake_at=wake_at, bedtime_at=bedtime_at)

    status = clock.get("catchup_status", "not_due")
    if status == "pending" and now < wake_at:
        clock["catchup_status"] = "not_due"
        clock["catchup_until"] = None
        status = "not_due"
    if status in ("not_due", "pending") and now >= wake_at:
        elapsed_hours = (min(now, bedtime_at) - wake_at) / 3600
        if now >= bedtime_at or elapsed_hours > MAX_CATCHUP_HOURS:
            clock["catchup_status"] = "expired"
            clock["catchup_until"] = min(now, bedtime_at)
        else:
            clock["catchup_status"] = "pending"
            clock["catchup_until"] = min(now, bedtime_at)
    elif status == "not_due" and now < wake_at:
        clock["catchup_until"] = None

    clock["phase"] = (
        "sleeping" if now < wake_at or now >= bedtime_at else "awake"
    )
    return clock


def is_life_asleep(state, now=None):
    """Return whether the independent fictional-life schedule is in sleep hours."""
    now = float(datetime.now(TZ).timestamp() if now is None else now)
    clock = ensure_clock(state, now)
    return now < float(clock["wake_at"]) or now >= float(clock["bedtime_at"])


def catchup_context(state, now=None):
    """Return the one pending offline interval or the latest completed summary."""
    now = float(datetime.now(TZ).timestamp() if now is None else now)
    clock = ensure_clock(state, now)
    if clock.get("catchup_status") == "pending":
        start = float(clock["catchup_from"])
        end = min(float(clock.get("catchup_until") or now), now)
        return {
            "status": "pending",
            "day": clock["day"],
            "wake_at": _format(start),
            "until": _format(end),
            "elapsed_minutes": max(0, int((end - start) / 60)),
            "rule": "这是AstrBot未运行期间的虚构生活补衔接窗口；最多补记少量个人活动，空档可以保留，不得编用户行为、真实第三人事实或已执行的外部操作。",
        }
    if clock.get("catchup_status") == "done":
        catchup = clock.get("catchup") or {}
        return {
            "status": "done",
            "day": clock["day"],
            "summary": catchup.get("summary", ""),
            "activities": catchup.get("activities", []),
        }
    return None


def _parse_local_time(value, day):
    if not isinstance(value, str):
        return None
    value = value.strip()
    try:
        hour_text, minute_text = value.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except (ValueError, TypeError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return datetime.combine(day, clock_time(hour, minute), TZ).timestamp()


def apply_offline_catchup(state, candidate, now=None):
    """Validate and persist a concise, elapsed-only fictional morning summary."""
    now = float(datetime.now(TZ).timestamp() if now is None else now)
    clock = ensure_clock(state, now)
    if clock.get("catchup_status") != "pending" or not isinstance(candidate, dict):
        return False
    summary = str(candidate.get("summary") or "").strip()
    activities = candidate.get("activities", [])
    if not summary or len(summary) > 240 or not isinstance(activities, list):
        return False
    if len(activities) > MAX_CATCHUP_ACTIVITIES:
        return False
    day = datetime.fromisoformat(clock["day"]).date()
    lower = float(clock["catchup_from"])
    upper = min(float(clock.get("catchup_until") or now), now)
    validated = []
    for item in activities:
        if not isinstance(item, dict):
            return False
        start = _parse_local_time(item.get("start"), day)
        end = _parse_local_time(item.get("end"), day)
        activity = str(item.get("activity") or "").strip()
        detail = str(item.get("detail") or "").strip()
        if (
            start is None or end is None or not activity
            or len(activity) > 60 or len(detail) > 120
            or start < lower or end > upper or end <= start
            or end - start < 5 * 60 or end - start > 240 * 60
        ):
            return False
        validated.append({
            "start_at": start,
            "end_at": end,
            "start": _format(start)[11:],
            "end": _format(end)[11:],
            "activity": activity,
            "detail": detail,
            "fictional": True,
        })
    validated.sort(key=lambda row: row["start_at"])
    for previous, current in zip(validated, validated[1:]):
        if current["start_at"] < previous["end_at"]:
            return False
    record = {
        "day": clock["day"],
        "summary": summary,
        "activities": validated,
        "from": lower,
        "until": upper,
        "recorded_at": now,
        "fictional": True,
    }
    clock["catchup"] = record
    clock["catchup_status"] = "done"
    clock["history"] = (list(clock.get("history") or []) + [record])[-7:]
    return True


def clock_context(state, now=None):
    """Compact context for chat/planning, without implying every gap was busy."""
    now = float(datetime.now(TZ).timestamp() if now is None else now)
    clock = ensure_clock(state, now)
    return {
        "day": clock["day"],
        "wake_at": _format(clock["wake_at"]),
        "bedtime_at": _format(clock["bedtime_at"]),
        "phase": clock.get("phase"),
        "catchup_status": clock.get("catchup_status"),
        "catchup": clock.get("catchup"),
        "recent_history": list(clock.get("history") or [])[-3:],
        "rule": "起床时间由自己的作息决定，不跟随用户开机时间。若catchup_status=pending，表示你已在wake_at醒来，但离线时段尚未补衔接；不要说自己刚随用户开机才醒，也不要提前编具体活动，先按一般状态回应，规划器会补记少量已过去的虚构日常。未记录的空档不自动编成经历。",
    }
