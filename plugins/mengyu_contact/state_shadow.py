"""Shadow comparison: legacy fields vs projection, without behaviour changes."""

import json
import time
from pathlib import Path


def compare(state, projection, stage="shadow"):
    """Return only valid, comparable differences between legacy and projected reads."""
    state = state if isinstance(state, dict) else {}
    projection = projection if isinstance(projection, dict) else {}
    now = float(projection.get("as_of") or time.time())
    diffs = []
    sleep = state.get("sleep") or {}
    until = sleep.get("until")
    legacy_asleep = bool(
        sleep.get("active")
        and (not isinstance(until, (int, float)) or now < until)
    )
    projected = ((projection.get("mengyu") or {}).get("sleep") or {}).get("effective_phase")
    projected_asleep = projected == "sleeping"
    if legacy_asleep != projected_asleep:
        diffs.append({
            "type": "sleep_interpretation",
            "legacy": "sleeping" if legacy_asleep else "awake",
            "projection": projected,
        })

    presence = state.get("owner_presence") or {}
    legacy_mode = (
        str(presence.get("mode") or "open")
        if isinstance(presence, dict) and presence
        else str(state.get("owner_pause_kind") or ("quiet" if state.get("conversation_paused") else "open"))
    )
    projected_mode = ((projection.get("owner") or {}).get("mode") or "open")
    if legacy_mode != projected_mode:
        diffs.append({
            "type": "owner_presence_interpretation",
            "legacy_mode": legacy_mode,
            "projection_mode": projected_mode,
        })
    return diffs


def log(state, projection, stage="shadow", path=None):
    """Append shadow diffs; never raises or alters the source state."""
    diffs = compare(state, projection, stage)
    if not diffs:
        return 0
    target = Path(path) if path is not None else Path(__file__).resolve().parent / "state_diagnostics.jsonl"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            for row in diffs[:10]:
                payload = {
                    **row,
                    "at": (projection or {}).get("as_of") or time.time(),
                    "snapshot_id": (projection or {}).get("snapshot_id"),
                    "stage": stage,
                    "shadow": True,
                }
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        return 0
    return len(diffs)
