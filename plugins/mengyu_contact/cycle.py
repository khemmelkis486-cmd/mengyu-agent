"""Optional calendar for fictional schedules, disabled without a configured start."""

import os
from datetime import date, datetime

if __package__:
    from .local_clock import TZ
else:
    from local_clock import TZ

PHASE_NOTES = {
    "period": "经期，可能有点累、腰酸，也可能还好，按身体实际感受来",
    "follicular": "卵泡期，精力在回来，想做的事多一点",
    "ovulation": "排卵期，体力和兴致通常好一些",
    "luteal": "黄体期，状态一般比较平稳",
    "pre": "经前，身体感受和精力按当天状态",
}
ENERGY = {
    "period": "low",
    "follicular": "rising",
    "ovulation": "high",
    "luteal": "steady",
    "pre": "uneven",
}


def _span(index):
    """Cycle length and period length for one cycle index (stable)."""
    length = 27 + (index * 7 + 3) % 6
    period = 4 + (index * 5 + 1) % 3
    return length, period


def _locate(moment, anchor):
    total = (moment.date() - anchor).days
    index, start = 0, 0
    while True:
        length, period = _span(index)
        if total < start + length:
            return index, total - start + 1, length, period
        start += length
        index += 1


def phase(now=None):
    """Return her current cycle phase.

    Args:
        now: UTC timestamp; defaults to the current time.

    Returns:
        Dict with phase, cycle day, a short note and energy level.
    """
    raw_anchor = os.environ.get("MENGYU_FICTIONAL_CYCLE_START", "").strip()
    try:
        anchor = date.fromisoformat(raw_anchor)
    except ValueError:
        return {"phase": "unknown", "note": "", "irritable": False}
    moment = datetime.now(TZ) if now is None else datetime.fromtimestamp(now, TZ)
    if moment.date() < anchor:
        return {"phase": "unknown", "note": "", "irritable": False}
    index, day, length, period_len = _locate(moment, anchor)
    ovulation_start = max(period_len + 1, length - 16)
    ovulation_end = min(length - 5, length - 11)
    pre_start = length - 3
    if day <= period_len:
        key = "period"
    elif day > pre_start:
        key = "pre"
    elif ovulation_start <= day <= ovulation_end:
        key = "ovulation"
    elif day < ovulation_start:
        key = "follicular"
    else:
        key = "luteal"
    return {
        "phase": key,
        "day": day,
        "cycle_day": day,
        "cycle_length": length,
        "cycle_index": index,
        "note": PHASE_NOTES[key],
        "energy": ENERGY[key],
        "period_day": day if key == "period" else None,
        # Most period and premenstrual days actually feel irritable.
        "irritable": key in ("period", "pre") and (index * 3 + day * 7) % 10 < 7,
    }
