"""One trace id per turn, from inbound message to decision.

Diagnostics only: every helper swallows IO errors so a log problem can never
block a reply.
"""

from __future__ import annotations

from . import config

import json
import time
import uuid

DATA = config.path()
LOG = DATA / "diagnostics" / "chat-trace.jsonl"


def start(event=None):
    """Return this event's trace id, creating one on first use."""
    trace_id = ""
    if event is not None and hasattr(event, "get_extra"):
        trace_id = str(event.get_extra("mengyu_trace_id") or "")
    if trace_id:
        return trace_id
    trace_id = "TR-" + uuid.uuid4().hex[:12]
    if event is not None and hasattr(event, "set_extra"):
        event.set_extra("mengyu_trace_id", trace_id)
    return trace_id


def new():
    """A trace id for work without an inbound event (proactive decisions)."""
    return "TR-" + uuid.uuid4().hex[:12]


def note(trace_id, stage, reason="", **fields):
    """Append one stage row; never raise."""
    row = {
        "at": time.time(),
        "trace_id": str(trace_id or ""),
        "stage": str(stage or ""),
        "reason": str(reason or ""),
    }
    row.update(fields)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return row
