"""Life gate: every outward plugin action must be caused by autonomous life.

Mode lives in ``plugin_data/mengyu_contact/life_gate.json`` as
``{"mode": "shadow"}``:

* ``shadow``  record only, allow everything (default)
* ``soft``    block proactive contact and photos, allow affect/task/voice
* ``hard``    block every gated plugin

The evidence is the newest unconsumed life event published by plan_runner.
"""

from __future__ import annotations

import json
import time
import uuid
from . import config

DATA = config.path()
CONFIG = DATA / "life_gate.json"
LOG = DATA / "life_gate.jsonl"

MODES = ("shadow", "soft", "hard")
SOFT_KINDS = ("proactive", "photo")
# Deliberately fixed to four outward actions. Memory, memes, voice and the
# audio listener stay outside the gate.
GATED_KINDS = ("proactive", "photo", "affect", "task")
KIND_PLUGIN = {
    "proactive": "mengyu_proactive",
    "photo": "mengyu_media",
    "affect": "mengyu_affect",
    "task": "mengyu_task_center",
}


def mode():
    """Read the gate mode, falling back to shadow on any problem."""
    return config.get("life_gate_mode", root=DATA, gate_override=CONFIG)


def _events():
    try:
        from data.plugins.mengyu_life import life_events
    except Exception:  # noqa: BLE001 - gate must never break the host plugin
        return None
    return life_events


def _log(now, kind, current, decision, reason, permit=None, shadow=False):
    row = {
        "at": now,
        "kind": kind,
        "mode": current,
        "decision": decision,
        "reason": reason,
        "event_id": (permit or {}).get("event_id"),
        "activity_id": (permit or {}).get("activity_id"),
        "plugin": KIND_PLUGIN.get(kind, ""),
        "shadow": shadow,
    }
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def check(kind, now=None):
    """Return ``(allowed, permit_or_None)`` for one plugin action.

    Args:
        kind: Gate kind (proactive, photo, affect, ...).
        now: Evaluation time; defaults to the current time.
    """
    now = float(time.time() if now is None else now)
    current = mode()
    if kind not in GATED_KINDS:
        _log(now, kind, current, "allow", "not_gated", None,
             shadow=current == "shadow")
        return True, None
    plugin = KIND_PLUGIN.get(kind, "")
    source = _events()
    event = (
        source.latest(now=now, exclude=(plugin,)) if source is not None else None
    )
    permit = None
    if event is not None:
        permit = {
            "permit_id": "PERMIT-" + uuid.uuid4().hex[:12],
            "event_id": event.get("event_id"),
            "activity_id": event.get("activity_id"),
            "kind": kind,
            "created_at": now,
            "consumed": False,
        }
    if permit is not None:
        allowed, reason = True, "life_event"
    elif current == "shadow":
        allowed, reason = True, "shadow_no_event"
    elif current == "soft" and kind not in SOFT_KINDS:
        allowed, reason = True, "soft_pass"
    else:
        allowed, reason = False, "no_life_event"
    _log(now, kind, current, "allow" if allowed else "deny", reason, permit,
         shadow=current == "shadow")
    return allowed, permit


def consume(permit, now=None):
    """Mark the permit's event as used after a successful send."""
    if not isinstance(permit, dict) or not permit.get("event_id"):
        return False
    source = _events()
    if source is None:
        return False
    plugin = KIND_PLUGIN.get(str(permit.get("kind") or ""), "")
    consumed = source.consume(permit["event_id"], plugin)
    _log(float(time.time() if now is None else now), str(permit.get("kind") or ""),
         mode(), "consume" if consumed else "consume_skipped",
         "delivered", permit)
    return consumed


def claim(kind, now=None):
    """Backward-compatible claim: the permit when life supports the action."""
    return check(kind, now)[1]


# Public interface for every plugin except mengyu_contact itself.
permit = claim


def guard(kind, now=None):
    """Shadow-compatible boolean wrapper around :func:`check`."""
    return check(kind, now)[0]
