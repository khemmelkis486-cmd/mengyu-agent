"""Conflict diagnostics for the state projection (append-only JSONL)."""

import json
import time
from pathlib import Path


def log_conflicts(projection, stage="", path=None):
    """Append observed conflicts; never changes runtime decisions or raises."""
    consistency = (projection or {}).get("consistency") or {}
    conflicts = consistency.get("conflicts") or []
    if not conflicts:
        return 0
    target = Path(path) if path is not None else Path(__file__).resolve().parent / "state_diagnostics.jsonl"
    rows = []
    for conflict in conflicts[:10]:
        if not isinstance(conflict, dict):
            continue
        rows.append({
            "at": (projection or {}).get("as_of") or time.time(),
            "snapshot_id": (projection or {}).get("snapshot_id"),
            "stage": str(stage or ""),
            "type": str(conflict.get("type") or "unknown"),
            "detail": dict(conflict),
            "resolution": str(conflict.get("resolution") or ""),
        })
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        return 0
    return len(rows)
