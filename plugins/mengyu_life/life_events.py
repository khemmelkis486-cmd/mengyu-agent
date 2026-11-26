"""Life event stream published by the plan runner.

The autonomous day is the only reason a plugin may act: plan_runner publishes
one event per activity transition, and proactive contact, photos and affect
consume those events instead of inventing their own reason.

Event shape::

    {"type": "life_event", "event_id": "LE-...", "activity_id": "slot-...",
     "kind": "meal", "stage": "started", "name": "弄晚饭", "place": "住处",
     "at": 1795..., "expires_at": 1795..., "consumed_by": []}

``started`` events stay usable for 30 minutes, ``finished`` events for 60.
"""

from __future__ import annotations

from data.plugins.mengyu_contact import config

import json
import time
import uuid
from pathlib import Path

DATA = config.path(plugin="mengyu_life")
LOG = DATA / "life_events.jsonl"

STARTED_TTL = 30 * 60
FINISHED_TTL = 60 * 60
MAX_RECENT = 20

_recent = []


def publish(activity_id, kind, stage, name, place, now=None, ttl=None, *, event_id=None, metadata=None):
    """Append a life event, reusing a persisted event on a stable-ID retry.

    Args:
        activity_id: Originating Life slot.
        kind: Kind of life activity.
        stage: Activity transition.
        name: Human-readable activity title.
        place: Activity location.
        now: Optional event timestamp.
        ttl: Event lifetime in seconds.
        event_id: Optional idempotent identity.
        metadata: Additional trusted facts to publish with the event.

    Returns:
        A persisted life event.
    """
    if event_id:
        existing = next((row for row in reversed(_rows()) if row.get("event_id") == event_id), None)
        if existing:
            return existing
    now = float(time.time() if now is None else now)
    if ttl is None:
        ttl = FINISHED_TTL if stage == "finished" else STARTED_TTL
    event = {
        "type": "life_event",
        "event_id": event_id or "LE-" + uuid.uuid4().hex[:12],
        "activity_id": str(activity_id or ""),
        "kind": str(kind or ""),
        "stage": str(stage or ""),
        "name": str(name or ""),
        "place": str(place or ""),
        "at": now,
        "expires_at": now + float(ttl),
        "consumed_by": [],
    }
    if metadata:
        event.update(metadata)
    _write(event)
    _recent.append(event)
    del _recent[:-MAX_RECENT]
    return event


def _write(row):
    DATA.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _rows():
    try:
        lines = LOG.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines[-200:]:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def latest(kind=None, activity_id=None, within=1800, now=None, exclude=()):
    """Return the newest usable event, or None.

    Args:
        kind: Optional activity kind filter (meal, outing, ...).
        activity_id: Optional exact activity filter.
        within: Only look this far back (seconds).
        now: Evaluation time; defaults to the current time.
        exclude: Plugin names whose consumption disqualifies the event.
    """
    now = float(time.time() if now is None else now)
    for row in reversed(_rows()):
        if row.get("type") != "life_event" or not row.get("event_id"):
            continue
        at = row.get("at")
        if not isinstance(at, (int, float)) or at > now or now - at > within:
            continue
        if float(row.get("expires_at") or 0) <= now:
            continue
        if kind and row.get("kind") != kind:
            continue
        if activity_id and str(row.get("activity_id")) != str(activity_id):
            continue
        consumed = row.get("consumed_by") or []
        if any(name in consumed for name in exclude):
            continue
        return row
    return None


def consume(event_id, plugin):
    """Mark one event as used by a plugin, once."""
    event_id = str(event_id or "")
    plugin = str(plugin or "")
    if not event_id or not plugin:
        return False
    rows = _rows()
    changed = False
    for row in rows:
        if row.get("event_id") != event_id:
            continue
        consumed = row.setdefault("consumed_by", [])
        if plugin in consumed:
            return False
        consumed.append(plugin)
        changed = True
        break
    else:
        return False
    if changed:
        LOG.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )
        for event in _recent:
            if event.get("event_id") == event_id:
                event.setdefault("consumed_by", []).append(plugin)
    return changed


def attach(event_id, field, value):
    """Store an artifact or delivery marker on one event."""
    event_id = str(event_id or "")
    if not event_id:
        return False
    rows = _rows()
    for row in rows:
        if row.get("event_id") == event_id:
            row[str(field)] = value
            LOG.write_text(
                "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                encoding="utf-8",
            )
            return True
    return False
