"""Sequential day chain: each event starts when the previous one ends."""

from __future__ import annotations

import random
import time

try:
    from . import daily_plan as dp
except ImportError:  # loaded as a top-level module in tests
    import daily_plan as dp

BIG_OUT = ["去商场逛街", "逛街买衣服", "去商场随便看看"]
CASUAL = [
    ("逛便利店", 15, 30),
    ("去书店翻书", 40, 80),
    ("出门走一圈", 25, 50),
    ("去咖啡馆坐会儿", 50, 90),
    ("逛超市", 30, 70),
    ("逛菜市场", 25, 50),
    ("买杯奶茶", 15, 30),
    ("去公园坐会儿", 40, 80),
    ("逛文具店", 20, 40),
    ("买点面包", 15, 25),
    ("看路边小摊", 15, 30),
]
DAY_ONLY = {"去书店翻书", "去咖啡馆坐会儿", "逛超市", "逛菜市场", "去公园坐会儿", "逛文具店"}


def _casual(hour):
    if 9 <= hour < 18:
        return CASUAL
    return [x for x in CASUAL if x[0] not in DAY_ONLY] or CASUAL
WORK = dp.WORK
CHORE = dp.CHORE
FILL = dp.FILL


def _slot(name, start, minutes, kind):
    return {
        "id": f"slot-{int(start)}-{name[:4]}",
        "name": name,
        "kind": kind,
        "start": start,
        "duration_min": int(minutes),
        "end": start + int(minutes) * 60,
        "status": "planned",
        "replaceable": kind != "meal",
        "commitment": kind in ("meal", "work", "big_out", "commitment"),
    }


def chained_day(commitments=(), now=None, seed=None, save=True, rain=False, rest=False,
                tight=False, shortage="", low=False):
    now = float(time.time() if now is None else now)
    day = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    rng = random.Random(seed if seed is not None else int(day))
    wake = day + rng.uniform(7.5, 10.0) * 3600
    early = (wake - day) / 3600.0 <= 9.6
    meetup = next((
        dict(slot) for slot in commitments
        if slot.get("kind") == "social_meetup" and day + 14 * 3600 <= slot["start"] < slot["end"] <= day + 22 * 3600
    ), None)
    need, days_left = dp.outings_left(now)
    big_out_today = meetup is None and need > 0 and not rain and not rest and not tight and not low and (
        days_left <= need * 2 or rng.random() < 0.85
    )

    target = rng.randint(20, 26)
    slots = [_slot("起床洗漱", wake, rng.randint(15, 25), "routine")]
    cursor = slots[0]["end"]
    used = {}

    def add(name, minutes, kind, gap=None, until=None):
        nonlocal cursor
        if "游戏" in str(name):
            # Big single-player games need a proper sitting.
            minutes = rng.randint(45, 90)
        if gap is None:
            # Going out needs travel time; staying home starts almost immediately.
            gap = rng.uniform(10, 25) if kind in ("outing", "big_out") else rng.uniform(5, 20)
        if until is not None:
            room = int((until - cursor - gap * 60) // 60)
            if room < 10:
                cursor = until
                return None
            minutes = min(minutes, room)
        cursor += gap * 60
        slot = _slot(name, cursor, minutes, kind)
        slots.append(slot)
        cursor = slot["end"]
        used[name] = used.get(name, 0) + 1
        return slot

    def pick(pool):
        options = [x for x in pool if used.get(x[0], 0) == 0] or pool
        if slots and "游戏" in str(slots[-1].get("name")):
            options = [x for x in options if "游戏" not in x[0]] or options
        return rng.choice(options)

    def work_block():
        # Sometimes she dawdles before starting work.
        if rng.random() < 0.3:
            name, lo, hi = pick(FILL)
            add(name, rng.randint(15, 40), "fill")
        name, lo, hi = pick(WORK)
        add(name, rng.randint(lo, hi), "work")

    def advance(hour):
        """Fill with small things until the clock reaches the anchor hour."""
        nonlocal cursor
        target = day + hour * 3600
        guard = 0
        while cursor < target and guard < 12:
            guard += 1
            remain = (target - cursor) / 3600.0
            pool = FILL if remain > 0.4 else _casual((cursor - day) / 3600.0)
            if rain or tight:
                pool = FILL
            if pool is not FILL and remain * 60 < 35:
                # Too little room for going out; stay in instead of a 10-minute trip.
                pool = FILL
            name, lo, hi = pick(pool)
            if "游戏" in name and remain * 60 < 50:
                name, lo, hi = pick([x for x in pool if "游戏" not in x[0]] or pool)
            minutes = min(rng.randint(lo, hi), max(10, int(remain * 60) - 2))
            if pool is not FILL:
                minutes = max(minutes, 25)
            add(name, minutes, "outing" if pool is CASUAL else "fill",
                until=target if meetup is not None else None)
        cursor = max(cursor, target)

    if early:
        add("早饭（做饭到收拾）", rng.randint(25, 40), "meal")
    if meetup is not None:
        name, lo, hi = pick(WORK)
        add(name, min(rng.randint(lo, hi), 40), "work", gap=10)
    else:
        for _ in range(1 if big_out_today else rng.randint(1, 2)):
            if rng.random() < 0.65:
                work_block()
            else:
                name, lo, hi = pick(CHORE)
                add(name, rng.randint(lo, hi), "chore")
    makeup_done = False

    def makeup(gap=None):
        """Get ready before leaving home, once a day."""
        nonlocal makeup_done
        if not makeup_done:
            makeup_done = True
            return add("化妆收拾", rng.randint(20, 35), "routine", gap=gap)
        return None

    if meetup is not None:
        start = meetup["start"]
        evening = start >= day + 17 * 3600
        outside = meetup.get("mode") == "out"
        advance(12.0)
        add("午饭（做饭到收拾）", rng.randint(25, 40), "meal", gap=0)
        if evening and meetup.get("social_activity_type") != "meal":
            advance(16.8)
            add("晚饭（做饭到收拾）", 35, "meal", gap=0)
        prep_at = start - (50 * 60 if outside else 0)
        advance((prep_at - day) / 3600)
        if outside:
            makeup(gap=0)["friend_meetup_id"] = meetup["friend_meetup_id"]
        slots.append(meetup)
        cursor = meetup["end"]
        if outside:
            returned = add("回家", rng.randint(20, 30), "return", gap=0)
            returned["friend_meetup_id"] = meetup["friend_meetup_id"]
        if not evening:
            advance(17.7)
            add("晚饭（做饭到收拾）", rng.randint(25, 45), "meal")
    elif big_out_today:
        # A whole day out: lunch and dinner both happen outside.
        advance(rng.uniform(10.0, 10.8))
        makeup()
        big_out = add(rng.choice(BIG_OUT), rng.randint(45, 75), "big_out")
        big_out["quota"] = True
        add("在外面吃午饭", rng.randint(40, 60), "meal")
        add("接着逛", rng.randint(90, 150), "big_out")
        # Stay out until dinner; do not slip home-only fillers into the trip.
        dinner_at = day + rng.uniform(18.0, 18.7) * 3600
        if dinner_at - cursor >= 25 * 60:
            name, lo, hi = rng.choice(_casual((cursor - day) / 3600.0))
            room = int((dinner_at - cursor) / 60) - 2
            add(name, max(20, min(rng.randint(lo, hi), room)), "outing")
        cursor = max(cursor, dinner_at)
        add("在外面吃晚饭", rng.randint(40, 70), "meal")
        add("回家", rng.randint(20, 35), "return")
    else:
        advance(11.6)
        add("午饭（做饭到收拾）", rng.randint(25, 45), "meal")
        if not rain and not rest and not tight and rng.random() < 0.6:
            name, lo, hi = rng.choice(_casual((cursor - day) / 3600.0))
            makeup()
            add(name, max(25, rng.randint(lo, hi)), "outing")
        for _ in range(rng.randint(1, 2)):
            work_block()
            name, lo, hi = pick(FILL)
            add(name, rng.randint(lo, hi), "fill")
        if shortage and not rain and not rest and not tight and rng.random() < 0.6:
            add("下楼买" + str(shortage), rng.randint(15, 30), "outing")
        advance(17.6)
        if rng.random() < 0.25:
            add("点外卖当晚饭", rng.randint(35, 55), "meal")
        else:
            add("晚饭（做饭到收拾）", rng.randint(25, 45), "meal")
        if rng.random() < 0.35:
            work_block()
    advance(21.0)
    add("洗澡洗漱", rng.randint(25, 40), "routine")

    guard = 0
    while len(slots) < target and guard < 40:
        guard += 1
        if cursor > day + 24 * 3600 + 0.2 * 3600:
            break
        name, lo, hi = pick(FILL)
        add(name, rng.randint(lo, hi), "fill")
    add("躺床上和你聊会儿", rng.randint(20, 45), "chat", gap=2)
    # Bedtime follows the last event so the plan never says "asleep" before
    # the chat it just scheduled.
    bedtime = max(
        cursor + rng.uniform(5, 15) * 60,
        day + 24 * 3600 + rng.uniform(0.3, 1.2) * 3600,
    )
    slots.sort(key=lambda s: s["start"])
    plan = {"day": int(day), "created_at": now, "wake_at": wake, "bedtime_at": bedtime, "slots": slots, "revisions": 0}
    if save:
        dp.save(plan)
    return plan






