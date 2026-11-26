"""Daily plan: meals as single events, work, outings, light chores, filler."""

from __future__ import annotations

from data.plugins.mengyu_contact import config

import json
import random
import time
from pathlib import Path

DATA = config.path(plugin="mengyu_life")
PLAN = DATA / "daily_plan.json"

WORK = [
    ("起稿", 50, 90),
    ("改稿", 40, 80),
    ("画客户要的图", 50, 90),
    ("排版/修图", 30, 60),
    ("整理参考图", 15, 30),
    ("回客户消息", 10, 20),
]
OUT = [
    ("去商场逛逛", 60, 120),
    ("逛便利店", 15, 30),
    ("去书店翻书", 40, 80),
    ("出门走一圈", 25, 50),
    ("去咖啡馆坐会儿", 50, 90),
]
CHORE = [
    ("拖个地", 15, 25),
    ("洗衣服", 10, 15),
    ("整理衣柜", 25, 45),
    ("收拾桌子", 10, 20),
]
FILL = [
    ("看两集剧", 40, 70),
    ("打会儿游戏", 30, 60),
    ("听会儿歌", 20, 40),
    ("刷手机", 10, 25),
    ("读几页书", 20, 40),
    ("泡杯茶发呆", 15, 30),
    ("躺着刷视频", 20, 50),
    ("追一集剧", 35, 55),
    ("开一把游戏", 20, 45),
    ("在窗边发会儿呆", 10, 25),
    ("翻购物软件", 10, 25),
    ("切点水果", 10, 20),
]


def _mk(name, start, minutes, commitment=False, kind="small"):
    return {
        "id": f"slot-{int(start)}-{name[:4]}",
        "name": name,
        "kind": kind,
        "start": start,
        "duration_min": int(minutes),
        "end": start + int(minutes) * 60,
        "status": "planned",
        "replaceable": kind != "meal" and not commitment,
        "commitment": commitment,
    }


def _free(slots, start, end):
    return all(end <= s["start"] or start >= s["end"] for s in slots)


def _place(rng, slots, name, minutes, h0, h1, day, **kw):
    for _ in range(60):
        start = day + rng.uniform(h0, max(h0, h1 - minutes / 60.0)) * 3600
        if _free(slots, start, start + minutes * 60):
            slot = _mk(name, start, minutes, **kw)
            slots.append(slot)
            return slot
    return None


def new_day(commitments=(), now=None, seed=None):
    """One day: 3 meals (sometimes skipped), 2-3 work blocks, outing, light chores."""
    now = float(time.time() if now is None else now)
    day = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    rng = random.Random(seed if seed is not None else int(day))
    wake_at = day + rng.uniform(7.5, 10.0) * 3600
    slots = [_mk("起床洗漱", wake_at, rng.randint(15, 25), kind="routine")]
    wake_hour = (wake_at - day) / 3600.0
    lunch_end = 0.0
    for name, h, skip in (("早饭", 8.5, 0.65), ("午饭", 12.0, 0.12), ("晚饭", 18.0, 0.0)):
        if name == "晚饭" and lunch_end and lunch_end > day + 14.5 * 3600:
            # Lunch ran late: dinner is pushed later or skipped.
            if rng.random() < 0.5:
                continue
            h += 1.0
        if rng.random() < skip:
            continue
        minutes = rng.randint(25, 45)
        start = day + (h + rng.uniform(-0.7, 0.7)) * 3600
        start = max(start, slots[0]["end"] + rng.uniform(5, 40) * 60)
        while any(not (start + minutes * 60 <= s["start"] or start >= s["end"]) for s in slots):
            start += 20 * 60
        slots.append(_mk(name + "（做饭到收拾）", start, minutes, commitment=True, kind="meal"))
        if name == "午饭":
            lunch_end = start + minutes * 60
    work_count = rng.randint(2, 3)
    for _ in range(work_count):
        name, lo, hi = rng.choice(WORK)
        _place(rng, slots, name, rng.randint(lo, hi), 9, 19, day, kind="work", commitment=True)
    out_count = 2 if rng.random() < 0.55 else 1
    for _ in range(out_count):
        name, lo, hi = rng.choice(OUT)
        _place(rng, slots, name, rng.randint(lo, hi), 12, 21, day, kind="outing")
    for _ in range(rng.randint(0, 2)):
        name, lo, hi = rng.choice(CHORE)
        _place(rng, slots, name, rng.randint(lo, hi), 10, 21, day, kind="chore")
    for idx, text in enumerate(commitments or []):
        _place(rng, slots, str(text)[:60], 40, 9, 19, day, commitment=True, kind="commitment")
    target = rng.randint(28, 36)
    attempts = 0
    while len(slots) < target and attempts < 40:
        attempts += 1
        name, lo, hi = rng.choice(FILL)
        if not _place(rng, slots, name, rng.randint(lo, hi), wake_hour + 0.5, 23.5, day, kind="fill"):
            continue
    bedtime = day + 24 * 3600 + rng.uniform(0.3, 1.6) * 3600
    slots.append(_mk("洗漱躺下准备睡", bedtime - rng.randint(15, 30) * 60, rng.randint(15, 30), kind="routine"))
    slots.sort(key=lambda s: s["start"])
    plan = {"day": int(day), "created_at": now, "wake_at": wake_at, "bedtime_at": bedtime, "slots": slots, "revisions": 0}
    save(plan)
    return plan


def save(plan):
    DATA.mkdir(parents=True, exist_ok=True)
    PLAN.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")


def load():
    try:
        return json.loads(PLAN.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def substitute(plan, slot_id, reason="busy"):
    for slot in plan["slots"]:
        if slot["id"] != slot_id:
            continue
        if not slot.get("replaceable"):
            return None
        used = {s["name"] for s in plan["slots"]}
        pool = [x for x in FILL + CHORE + OUT if x[0] not in used] or FILL
        name, lo, hi = random.choice(pool)
        slot.update(name=name, duration_min=random.randint(lo, hi), status="planned", replaced_reason=reason)
        slot["end"] = slot["start"] + slot["duration_min"] * 60
        plan["revisions"] = plan.get("revisions", 0) + 1
        save(plan)
        return slot
    return None






# --- constrained mode: time-of-day weights, dedupe, weekly outing quota ---
WEEK = DATA / "weekly_plan.json"
OUT_COUNT = {"去商场逛逛", "去书店翻书", "去咖啡馆坐会儿", "出门走一圈"}


def _week_key(now):
    t = time.localtime(now)
    return time.strftime("%Y-W%W", t)


def week_state(now):
    try:
        data = json.loads(WEEK.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return data


def note_outing(now):
    """Deprecated: generation must not consume the weekly quota."""
    data = week_state(now)
    key = _week_key(now)
    entry = data.setdefault(key, {"outings": []})
    entry["outings"].append(time.strftime("%Y-%m-%d", time.localtime(now)))
    WEEK.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def confirm_outing(slot_id, at=None):
    """Record one actually completed big outing, idempotently."""
    at = float(time.time() if at is None else at)
    sid = str(slot_id or "")
    if not sid:
        return False
    data = week_state(at)
    entry = data.setdefault(_week_key(at), {"outings": []})
    confirmed = entry.setdefault("confirmed", {})
    if sid in confirmed:
        return False
    day = time.strftime("%Y-%m-%d", time.localtime(at))
    confirmed[sid] = day
    entry.setdefault("outings", []).append(day)
    WEEK.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def outings_left(now):
    data = week_state(now)
    entry = data.get(_week_key(now)) or {}
    confirmed = entry.get("confirmed") or {}
    days = (
        set(confirmed.values())
        if confirmed
        else set(entry.get("outings") or [])
    )
    done = len(days)
    days_left = 6 - time.localtime(now).tm_wday
    return max(0, 2 - done), max(0, days_left)


def constrained_day(commitments=(), now=None, seed=None):
    """Pool day with time-of-day weighting, dedupe and a weekly outing quota."""
    now = float(time.time() if now is None else now)
    day = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    rng = random.Random(seed if seed is not None else int(day))
    wake_at = day + rng.uniform(7.5, 10.0) * 3600
    wake_hour = (wake_at - day) / 3600.0
    slots = [_mk("起床洗漱", wake_at, rng.randint(15, 25), kind="routine")]
    lunch_end = 0.0
    # Wake time decides the first meal: early -> breakfast + lunch; late -> lunch only.
    early = wake_hour <= 9.6
    for name, h, skip in (("早饭", 8.5, 0.1 if early else 1.0), ("午饭", 12.0, 0.05), ("晚饭", 18.0, 0.0)):
        if name == "晚饭" and lunch_end and lunch_end > day + 14.5 * 3600:
            if rng.random() < 0.5:
                continue
            h += 1.0
        if name == "午饭" and not early:
            h = max(12.0, wake_hour + 1.5)
        if rng.random() < skip:
            continue
        minutes = rng.randint(25, 45)
        start = max(day + (h + rng.uniform(-0.7, 0.7)) * 3600, slots[0]["end"] + rng.uniform(5, 40) * 60)
        while any(not (start + minutes * 60 <= s["start"] or start >= s["end"]) for s in slots):
            start += 20 * 60
        slots.append(_mk(name + "（做饭到收拾）", start, minutes, commitment=True, kind="meal"))
        if name == "午饭":
            lunch_end = start + minutes * 60
    need, days_left = outings_left(now)
    force_outing = need > 0 and days_left - 1 <= need
    used = {}

    def pick(pool, h0, h1):
        for _ in range(40):
            name, lo, hi = rng.choice(pool)
            if used.get(name, 0) >= (2 if name in ("刷手机", "躺着刷视频", "泡杯茶发呆") else 1):
                continue
            minutes = rng.randint(lo, hi)
            start = day + rng.uniform(max(h0, wake_hour + 0.5), max(h0, h1 - minutes / 60.0)) * 3600
            if not _free(slots, start, start + minutes * 60):
                continue
            used[name] = used.get(name, 0) + 1
            kind = (
                "work"
                if pool is WORK
                else "outing"
                if pool is OUT
                else "chore"
                if pool is CHORE
                else "fill"
            )
            slot = _mk(name, start, minutes, kind=kind)
            slots.append(slot)
            return slot
        return None

    if force_outing:
        pick(OUT, 13, 19)
    for _ in range(rng.randint(2, 3)):
        pick(WORK, 9, 19)
    for _ in range(1 if need else rng.randint(0, 1)):
        pick(OUT, 12, 21)
    for _ in range(rng.randint(0, 2)):
        pick(CHORE, 10, 20)
    for text in commitments or []:
        _place(rng, slots, str(text)[:60], 40, 9, 19, day, commitment=True, kind="commitment")
    while len(slots) < rng.randint(18, 24):
        if not pick(FILL, 9, 23):
            break
    bedtime = day + 24 * 3600 + rng.uniform(0.3, 1.6) * 3600
    # Evening wash, then fill the day so she is rarely doing "nothing".
    _place(rng, slots, "洗澡", rng.randint(20, 35), 21, 23.5, day, kind="routine")
    for _ in range(60):
        slots.sort(key=lambda s: s["start"])
        gap = None
        for a, b in zip(slots, slots[1:]):
            if (b["start"] - a["end"]) / 60 >= 25 and b["start"] < bedtime - 20 * 60:
                gap = (a["end"], b["start"])
                break
        if not gap:
            break
        length = int((gap[1] - gap[0]) / 60) - 5
        name, lo, hi = rng.choice(FILL + CHORE)
        minutes = max(10, min(length, rng.randint(lo, hi)))
        start = gap[0] + rng.uniform(0, max(0, length - minutes)) * 60
        if not _free(slots, start, start + minutes * 60):
            break
        slots.append(_mk(name, start, minutes, kind="fill"))
    chat_start = bedtime - rng.uniform(45, 80) * 60
    _place(rng, slots, "躺床上和你聊会儿", rng.randint(20, 50), (chat_start - day) / 3600.0, (chat_start - day) / 3600.0 + 1.0, day, kind="chat")
    slots.append(_mk("洗漱躺下准备睡", bedtime - rng.randint(15, 30) * 60, rng.randint(15, 30), kind="routine"))
    slots.sort(key=lambda s: s["start"])
    plan = {"day": int(day), "created_at": now, "wake_at": wake_at, "bedtime_at": bedtime, "slots": slots, "revisions": 0}
    save(plan)
    return plan
