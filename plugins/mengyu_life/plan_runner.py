"""Advance the daily chain plan without fighting the legacy activity loop.

The legacy ``operations.activity_loop`` still owns ``activities.json``.  In
shadow mode (the default) this module only advances ``daily_plan.json``,
records life events and writes a preview of the activity rows it *would*
publish.  Flipping :data:`MODE` to ``"live"`` after that loop is retired makes
``tick`` insert the rows through the live MengyuContact store (one writer,
inside ``core.lock``) instead of rewriting the JSON file.
"""

from __future__ import annotations

from data.plugins.mengyu_contact import config

import json
import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path

try:
    from . import daily_chain
    from . import life_events
    from . import world_notes
except ImportError:  # loaded as a top-level module in tests
    import daily_chain
    import life_events
    import world_notes

DATA = config.path(plugin="mengyu_life")
EVENTS = DATA / "life_events.jsonl"
PREVIEW = DATA / "plan_sync_preview.json"
PENDING = DATA / "pending_plan.json"

# "shadow" never touches the shared store; "live" inserts rows via the core.
MODE = "live"

SYNCED_STATUSES = ("in_progress", "completed")
# Moments worth a spontaneous photo: outings, meals, heading home.
PHOTO_MOMENT_KINDS = ("outing", "big_out", "meal", "return", "social_meetup")
logger = logging.getLogger(__name__)

# Trip destinations used when a slot is outside.  Matched against the slot name.
OUT_PLACES = (
    (("商场", "逛街", "买衣服", "逛店", "试衣"), "附近商场"),
    (("书店",), "社区书店"),
    (("咖啡馆",), "街角咖啡馆"),
    (("超市",), "社区超市"),
    (("菜市场",), "菜市场"),
    (("奶茶",), "奶茶店"),
    (("公园",), "小区旁的公园"),
    (("文具",), "文具店"),
    (("面包",), "面包店"),
    (("便利店",), "社区便利店"),
    (("小摊",), "路边小摊"),
    (("餐馆", "吃饭"), "附近的餐馆"),
)

PROFILE_BY_KIND = {
    "meal": "meal",
    "work": "work",
    "outing": "outing",
    "big_out": "shopping",
    "chore": "cleaning",
    "routine": "shower",
    "chat": "social",
    "social_meetup": "social",
    "sleep": "rest",
    "return": "walk",
}

SLEEP_STAGES = ("睡着了", "醒了")


def _log(row):
    """Append one JSON line to the life event log."""
    DATA.mkdir(parents=True, exist_ok=True)
    with EVENTS.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _date(ts):
    return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d")


def _day_start(ts):
    return time.mktime(time.localtime(float(ts))[:3] + (0, 0, 0, 0, 0, -1))


def _place(slot):
    """Return ``(mode, destination)`` for one plan slot."""
    if slot.get("place"):
        return str(slot.get("mode") or "out"), str(slot["place"])
    kind = str(slot.get("kind") or "")
    name = str(slot.get("name") or "")
    if "回家" in name or "到家" in name:
        return "home", "住处"
    if "外面" in name or "出去吃" in name:
        return "out", "附近的餐馆"
    if kind in ("outing", "big_out"):
        for keys, place in OUT_PLACES:
            if any(key in name for key in keys):
                return "out", place
        return "out", "附近街区"
    if kind == "meal":
        return "home", "厨房"
    return "home", "住处"


def _profile(slot):
    """Pick a coarse activity profile so other plugins do not see "general"."""
    kind = str(slot.get("kind") or "")
    if kind in PROFILE_BY_KIND:
        return PROFILE_BY_KIND[kind]
    name = str(slot.get("name") or "")
    if "洗" in name:
        return "shower"
    if "收拾" in name or "整理" in name or "洗衣" in name:
        return "cleaning"
    if "剧" in name or "游戏" in name or "书" in name or "歌" in name:
        return "hobby"
    if "发呆" in name or "躺" in name:
        return "rest"
    return "general"


def _had_outing(plan):
    """Whether the current plan already has a trip today."""
    for slot in (plan or {}).get("slots", []):
        if str(slot.get("kind")) in ("outing", "big_out", "social_meetup") and str(
            slot.get("status")
        ) in ("in_progress", "completed"):
            return True
    return False


def _rainy(day):
    """Whether that day's plan should stay home (rain likely)."""
    weather = (world_notes.load(day) or {}).get("weather") or {}
    try:
        chance = float(weather.get("rain") or 0)
    except (TypeError, ValueError):
        chance = 0.0
    return chance >= 60 or "雨" in str(weather.get("text") or "")


def meal_dish(detail):
    """The dish named in a meal detail line, if any."""
    match = re.search(
        r"(?:做了|点了|煮了|炒了|吃了|一笼|一碗)([^，。；、]{2,12})",
        str(detail or ""),
    )
    return match.group(1).strip() if match else ""


def _apply_world_notes(plan):
    """Give today's slots concrete places, shows, games and books."""
    day = time.strftime(
        "%Y-%m-%d", time.localtime(float(plan.get("day") or time.time()))
    )
    notes = world_notes.load(day)
    if not notes:
        return plan
    places = list(notes.get("places") or [])
    foods = list(notes.get("foods") or places)
    recent_foods = set(notes.get("recent_foods") or [])
    fresh_foods = [item for item in foods if item not in recent_foods] or foods
    shows = list(notes.get("shows") or [])
    games = list(notes.get("games") or [])
    books = list(notes.get("books") or [])
    malls = [p for p in places if any(w in p for w in ("商场", "广场", "中心", "天地", "汇", "城"))]
    streets = [p for p in places if p not in malls] or places
    seed = sum(ord(char) for char in day) if day else 0
    pi = seed % len(streets) if streets else 0
    mi = seed % len(malls) if malls else 0
    si = seed % len(shows) if shows else 0
    gi = seed % len(games) if games else 0
    bi = seed % len(books) if books else 0
    fi = seed % len(fresh_foods) if fresh_foods else 0
    used_foods = []
    for slot in plan.get("slots", []):
        name = str(slot.get("name") or "")
        kind = str(slot.get("kind") or "")
        if "剧" in name and shows:
            show = shows[si % len(shows)]
            si += 1
            title = str(show.get("name") or "")
            slot["name"] = title if title.startswith("《") else "《" + title + "》"
            slot["name"] = "看" + slot["name"]
            slot["detail"] = str(show.get("detail") or "")
        elif "游戏" in name and games:
            game = games[gi % len(games)]
            gi += 1
            slot["name"] = "玩" + game
            slot["detail"] = "玩" + game
        elif "书" in name and books:
            book = books[bi % len(books)]
            bi += 1
            slot["name"] = "读" + book
            slot["detail"] = "读" + book
        elif kind in ("outing", "big_out") and ("外面" in name or "商场" in name or "逛" in name):
            if "商场" in name and malls:
                slot["place"] = malls[mi % len(malls)]
                mi += 1
            else:
                slot["place"] = streets[pi % len(streets)]
                pi += 1
            slot["mode"] = "out"
        elif kind == "meal" and "外面" in name and fresh_foods:
            slot["place"] = fresh_foods[fi % len(fresh_foods)]
            slot["mode"] = "out"
            slot["detail"] = meal_detail(slot["place"], slot.get("id") or "")
            used_foods.append(slot["place"])
            fi += 1
    for slot in plan.get("slots", ()):
        if str(slot.get("kind")) != "meal":
            continue
        detail = str(slot.get("detail") or "")
        if not detail:
            detail = meal_detail(slot.get("place") or "", slot.get("id") or "")
            if detail:
                slot["detail"] = detail
        dish = meal_dish(detail)
        if dish:
            slot["meal"] = {**(slot.get("meal") or {}), "dish": dish}
    slots_all = plan.get("slots") or []
    if not any(str(item.get("mode")) == "out" for item in slots_all):
        kept = [item for item in slots_all if "化妆" not in str(item.get("name") or "")]
        if len(kept) != len(slots_all):
            plan["slots"] = kept
    if used_foods:
        world_notes.remember_foods(used_foods)
    return plan


def build_row(slot, now):
    """Build one activity row that satisfies the ActivityStore invariants.

    Both phases are written up front: the store derives planned / in_progress /
    completed from reached phases, so nothing has to append a second phase
    later.  Repeated ticks therefore cannot create duplicate phase records.
    """
    start = float(slot["start"])
    end = float(slot["end"])
    mode, place = _place(slot)
    name = str(slot.get("name") or "活动")[:120]
    plan_id = "plan:" + str(slot.get("id") or f"slot-{int(start)}")
    if str(slot.get("kind")) == "sleep":
        stages = SLEEP_STAGES
    else:
        stages = ("正在做", "做完了")
    return {
        "id": plan_id,
        "fictional": True,
        "activity_status": "planned",
        "planned_at": start,
        "started_at": None,
        "completed_at": None,
        "status_updated_at": float(now),
        "activity_chain_id": plan_id,
        "resumed_from_activity_id": None,
        "progress": [],
        "activity_profile": _profile(slot),
        "planned_duration_minutes": int(slot.get("duration_min") or 0),
        "itinerary": [],
        "prep_minutes": 0,
        "travel_minutes": 0,
        "meal": None,
        "created_at": start,
        "day": _date(start),
        "destination": place,
        "mode": mode,
        "reason": name,
        "detail": "",
        "game_title": "",
        "game_genre": "",
        "clothes": "日常便装",
        "scheduler_task_id": None,
        "work": None,
        "phases": [
            {"at": start, "stage": stages[0], "place": place},
            {"at": end, "stage": stages[1], "place": place},
        ],
        "source": "daily_plan",
        "title": name,
        "daily_anchor_id": str(slot.get("id") or ""),
        "quota": bool(slot.get("quota")),
        **({
            "friend_meetup_id": slot["friend_meetup_id"],
            "friend_ids": slot.get("friend_ids", []),
        } if slot.get("friend_meetup_id") else {}),
    }


REQUIRED_FIELDS = (
    "id", "created_at", "day", "destination", "reason", "clothes",
    "phases", "activity_status", "mode", "fictional",
)


def validate_row(row):
    """Return a list of problems that would break the contact projection."""
    problems = [f"missing:{key}" for key in REQUIRED_FIELDS if key not in row]
    phases = row.get("phases")
    if not isinstance(phases, list) or len(phases) != 2:
        problems.append("phases")
    else:
        first, last = phases
        if not (isinstance(first.get("at"), (int, float)) and isinstance(last.get("at"), (int, float))):
            problems.append("phase_time")
        elif not first["at"] < last["at"]:
            problems.append("phase_order")
        if not all(phase.get("place") for phase in phases):
            problems.append("phase_place")
    if row.get("day") != _date(row.get("created_at", 0)):
        problems.append("day")
    if row.get("mode") not in ("home", "out"):
        problems.append("mode")
    return problems


def _asleep(core, now):
    """Whether she is in her night sleep; a nap does not close the day."""
    state = getattr(core, "state", None)
    if not isinstance(state, dict):
        return False
    sleep = state.get("sleep") or {}
    return bool(
        sleep.get("active")
        and not sleep.get("nap")
        and now >= float(sleep.get("since") or 0)
    )


def _day_over(plan, core, now):
    """The day ends when she falls asleep; six hours past bedtime is a guard."""
    if not plan or not plan.get("slots"):
        return True
    if _asleep(core, now):
        return True
    bedtime = float(plan.get("bedtime_at") or 0)
    grace = 6 * 3600 if core is not None else 3600
    return now >= bedtime + grace


def _fresh(candidate, now):
    """A pending plan is only usable on the day it was generated for."""
    return bool(
        candidate
        and candidate.get("slots")
        and abs(_day_start(now) - float(candidate.get("day") or 0)) < 1
    )


def _resting(now):
    """Whether her body asks for a quiet day (period or illness)."""
    try:
        from data.plugins.mengyu_contact.cycle import phase as cycle_phase
        from data.plugins.mengyu_life.core.health import status as health_status

        if health_status(now).get("sick"):
            return True
        return str(cycle_phase(now).get("phase")) == "period"
    except Exception:  # noqa: BLE001 - body state is optional
        return False


def _build_day(core, target, now, tight, shortage, low=False):
    """Build a fresh Life day with any confirmed friend commitment.

    Args:
        core: Optional active Contact core for service discovery.
        target: Timestamp on the local target day.
        now: Start time of this planning operation.
        tight: Whether the budget is tight.
        shortage: Existing home inventory shortage hint.

    Returns:
        A fresh plan with the current Life options and reserved commitments.
    """
    date = _date(target)
    rain, rest = _rainy(date), _resting(target)
    service = _friend_service(core)
    commitments = ()
    if service is not None:
        try:
            if date > _date(now) or datetime.fromtimestamp(now).hour < 6:
                service.nightly(now, target_day=date, rest=rest, rain=rain, tight=tight)
            # An accepted commitment stays eligible even if the latest soft preferences changed.
            commitments = [
                slot for slot in service.planning_candidates(date) if slot["start"] >= now
            ]
        except (OSError, sqlite3.Error, ValueError):
            logger.exception("Mengyu friends planning failed")
    return daily_chain.chained_day(
        commitments=commitments, now=target, save=False,
        rain=rain, rest=rest, tight=tight, shortage=shortage, low=low,
    )


def _friend_service(core):
    """Find the currently ready Friends service.

    Args:
        core: Optional Contact core.

    Returns:
        Friends service or None.
    """
    find = getattr(core, "plugin_service", None)
    return getattr(find("mengyu_friends"), "service", None) if callable(find) else None


def _bind_friend_slots(core, plan, now):
    """Write back friend slot IDs after Life has saved a plan.

    Args:
        core: Optional Contact core.
        plan: Persisted Life plan.
        now: Planning time.
    """
    service = _friend_service(core)
    if service is None:
        return
    try:
        for slot in plan.get("slots", ()):
            if slot.get("kind") == "social_meetup":
                service.mark_scheduled(slot["friend_meetup_id"], slot["id"], slot["start"], slot["end"], now)
    except (OSError, sqlite3.Error, ValueError):
        logger.exception("Mengyu friends schedule writeback failed")


def _record_friend_transition(core, slot, stage, now):
    """Commit the meetup fact before publishing its Life event.

    Args:
        core: Contact core with access to the Friends service.
        slot: Current persisted Life slot.
        stage: Activity transition being applied.
        now: Transition timestamp.

    Returns:
        Whether the meetup store accepted the transition.
    """
    service = _friend_service(core)
    if service is None:
        return False
    identity = slot["friend_meetup_id"]
    try:
        if not service.mark_scheduled(identity, slot["id"], slot["start"], slot["end"], now):
            return False
        if not service.record_transition(identity, stage, now):
            return False
        if stage == "finished":
            meeting = service.meetup(identity)
            slot["friend_result"] = {
                key: meeting[key] for key in ("outcome", "detail", "follow_up", "mood")
            }
        return True
    except (OSError, sqlite3.Error, ValueError):
        logger.exception("Mengyu friends meetup transition failed")
        return False


def _rollover(plan, now, tight=False, shortage="", core=None, low=False):
    """Generate tomorrow's plan at midnight without touching tonight's file."""
    day = float(plan.get("day") or 0)
    if now < day + 86400 or PENDING.exists():
        return False
    nxt = _build_day(core, now, now, tight, shortage, low)
    _apply_world_notes(nxt)
    PENDING.write_text(
        json.dumps(nxt, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _bind_friend_slots(core, nxt, now)
    _log({"at": now, "type": "next_day_planned", "events": len(nxt["slots"])})
    return True


def _advance(plan, now, core=None):
    """Move slot statuses forward exactly once per transition."""
    changed = False
    slots = plan.get("slots", [])
    meetings = {slot["friend_meetup_id"]: slot for slot in slots if slot.get("kind") == "social_meetup"}
    for slot in slots:
        status = str(slot.get("status") or "planned")
        start = float(slot.get("start") or 0)
        end = float(slot.get("end") or 0)
        if status == "planned" and slot.get("kind") == "return":
            linked = meetings.get(slot.get("friend_meetup_id"))
            if linked and now >= start and linked["status"] != "completed":
                if linked["status"] in ("missed", "skipped") or now >= end:
                    slot["status"] = "skipped"
                    changed = True
                continue
        if slot.get("kind") == "social_meetup":
            stage = None
            if status == "planned" and now >= start:
                stage = "started" if now < end else "missed"
            elif status == "in_progress" and now >= end:
                stage = "finished"
            if stage and not _record_friend_transition(core, slot, stage, now):
                continue
        if (
            str(slot.get("kind")) == "sleep"
            and status == "in_progress"
            and core is not None
            and now >= start
            and not _asleep(core, now)
        ):
            # She woke up early: the plan must stop saying she is asleep.
            slot["status"] = "completed"
            slot["completed_at"] = now
            slot["end"] = now
            _truncate_rows(core, [str(slot.get("id") or "")], now)
            _publish_event(slot, "finished", now, reason="woke")
            changed = True
            continue
        if status == "planned":
            if start <= now < end:
                slot["status"] = "in_progress"
                _publish_event(slot, "started", now)
                _note_photo_moment(core, slot, "started", now)
                changed = True
            elif now >= end:
                slot["status"] = "missed"
                _log({"at": now, "type": "activity_missed",
                      "name": slot.get("name"), "id": slot.get("id")})
                changed = True
        elif status == "in_progress" and now >= end:
            slot["status"] = "completed"
            _publish_event(slot, "finished", now)
            _note_photo_moment(core, slot, "finished", now)
            if (
                core is not None
                and str(slot.get("kind")) in ("outing", "big_out")
                and any(word in str(slot.get("name")) for word in ("逛", "买"))
            ):
                try:
                    from . import shopping_decide

                    shopping_decide.after_trip(core, now)
                except Exception:  # noqa: BLE001 - shopping decision is best effort
                    pass
            if (
                core is not None
                and str(slot.get("kind")) == "meal"
                and "外面" in str(slot.get("name"))
            ):
                _, meal_place = _place(slot)
                pending = list(core.state.get("pending_spend") or [])
                pending.append({
                    "kind": "expense",
                    "category": "food",
                    "label": str(slot.get("name")) + "（" + str(meal_place) + "）",
                    "id": "meal:" + str(slot.get("id") or int(now)),
                })
                core.state["pending_spend"] = pending[-20:]
            if core is not None:
                try:
                    import random as _random

                    from .core import home_items

                    slot_name = str(slot.get("name") or "")
                    if "洗澡" in slot_name and _random.random() < 0.25:
                        home_items.consume("洗发水")
                    if "做饭" in slot_name and _random.random() < 0.5:
                        home_items.consume("米")
                        if _random.random() < 0.5:
                            home_items.consume("鸡蛋")
                    if "纸巾" in slot_name:
                        home_items.consume("纸巾")
                except Exception:  # noqa: BLE001 - consumables are optional
                    pass
            if slot.get("quota"):
                daily_chain.dp.confirm_outing(str(slot.get("id") or ""), end)
            changed = True
    return changed


DISHES = {
    "沈大成": "点了双酿团和一碗小馄饨",
    "老盛昌": "一笼汤包加一碗小馄饨",
    "王家沙": "蟹粉小笼和两面黄",
    "小杨生煎": "四只生煎加牛肉粉丝汤",
    "大壶春": "生煎和咖喱牛肉汤",
}
HOME_DISHES = ("番茄鸡蛋面", "青椒肉丝盖饭", "煮了锅小馄饨", "咖喱饭", "炒了个蛋炒饭")


def meal_detail(place, seed=""):
    """A concrete dish for a meal slot."""
    for name, dish in DISHES.items():
        if name in str(place or ""):
            return dish
    if "厨房" in str(place or "") or not place:
        index = sum(ord(char) for char in str(seed)) % len(HOME_DISHES)
        return "做了" + HOME_DISHES[index]
    return ""


def _publish_event(slot, stage, now, reason=""):
    """Emit the life event other plugins must act on."""
    _, place = _place(slot)
    identity = slot.get("friend_meetup_id")
    extra = {}
    if identity:
        extra = {
            "event_id": f"LE-{slot['id']}:{stage}",
            "metadata": {
                "friend_meetup_id": identity,
                "friend_ids": slot.get("friend_ids", []),
                **slot.get("friend_result", {}),
            },
        }
    event = life_events.publish(
        activity_id=slot.get("id"),
        kind=slot.get("kind"),
        stage=stage,
        name=slot.get("name"),
        place=place,
        now=now,
        **extra,
    )
    if reason:
        life_events.attach(event["event_id"], "reason", reason)
    return event


def _note_photo_moment(core, slot, stage, now):
    """Leave a photo opportunity for life_tick to take, maybe."""
    state = getattr(core, "state", None)
    if not isinstance(state, dict) or slot.get("kind") not in PHOTO_MOMENT_KINDS:
        return
    _, place = _place(slot)
    people = []
    if slot.get("kind") == "social_meetup":
        service = _friend_service(core)
        wanted = set(slot.get("friend_ids") or [])
        if service is not None and wanted:
            try:
                people = [
                    {
                        "name": str(row.get("name") or ""),
                        "appearance": str(row.get("appearance") or ""),
                    }
                    for row in (service.snapshot().get("friends") or [])
                    if row.get("friend_id") in wanted
                ]
            except Exception:  # noqa: BLE001 - photo moment is best effort
                people = []
    state["photo_moment"] = {
        "kind": slot.get("kind"),
        "stage": stage,
        "name": slot.get("name"),
        "place": place,
        "friends": people,
        "at": now,
    }


def _publish(core, plan, now):
    """Publish started rows: preview in shadow mode, real rows in live mode."""
    rows = [
        build_row(slot, now)
        for slot in plan.get("slots", [])
        if slot.get("status") in SYNCED_STATUSES
    ]
    problems = [
        {"id": row["id"], "problems": validate_row(row)}
        for row in rows
        if validate_row(row)
    ]
    if MODE == "live" and core is not None:
        added = _insert(core, rows)
        if added:
            _log({"at": now, "type": "activities_synced", "count": added})
        return added
    PREVIEW.parent.mkdir(parents=True, exist_ok=True)
    PREVIEW.write_text(
        json.dumps(
            {"mode": MODE, "generated_at": now, "rows": rows, "problems": problems},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return 0


def _insert(core, rows):
    """Insert rows through the live store; the caller holds ``core.lock``."""
    store = getattr(core, "activities", None)
    if store is None:
        return 0
    data = store.data.setdefault("virtual_life", [])
    known = {str(item.get("id")) for item in data if isinstance(item, dict)}
    added = [row for row in rows if str(row["id"]) not in known]
    if not added:
        return 0
    data.extend(added)
    # Match ActivityStore's own retention so the file cannot grow without bound.
    store.data["virtual_life"] = data[-20:]
    store.save()
    return len(added)


def _close_out(plan, now, core=None):
    """Close the old day before it is replaced.

    Anything still running is finished at the switch moment (reason
    ``rollover``); slots that never started are marked skipped, not completed.
    """
    closed = skipped = 0
    for slot in plan.get("slots", []):
        status = str(slot.get("status") or "planned")
        if slot.get("kind") == "social_meetup" and status in ("planned", "in_progress"):
            end = float(slot["end"])
            if status == "in_progress" and now >= end and _record_friend_transition(core, slot, "finished", now):
                slot["status"] = "completed"
                slot["completed_at"] = now
                _publish_event(slot, "finished", now, reason="rollover")
                if slot.get("quota"):
                    daily_chain.dp.confirm_outing(slot["id"], end)
                closed += 1
            else:
                _record_friend_transition(core, slot, "missed" if status == "planned" and now >= end else "cancelled", now)
                slot["status"] = "skipped"
                skipped += 1
            continue
        if status == "in_progress":
            slot["status"] = "completed"
            slot["completed_at"] = now
            _publish_event(slot, "finished", now, reason="rollover")
            closed += 1
        elif status == "planned" and float(slot.get("end") or 0) <= now:
            slot["status"] = "skipped"
            _log({"at": now, "type": "activity_skipped", "reason": "rollover",
                  "name": slot.get("name"), "id": slot.get("id")})
            skipped += 1
    return closed, skipped


def _truncate_rows(core, slot_ids, now):
    """Cut open plan rows at the switch moment so nothing stays running."""
    store = getattr(core, "activities", None)
    if store is None:
        return 0
    ids = {"plan:" + str(slot_id) for slot_id in slot_ids}
    changed = 0
    for row in store.data.get("virtual_life", []):
        if not isinstance(row, dict) or str(row.get("id")) not in ids:
            continue
        if row.get("cancelled_at"):
            continue
        phases = [
            phase for phase in row.get("phases") or []
            if isinstance(phase, dict) and isinstance(phase.get("at"), (int, float))
        ]
        if len(phases) < 2 or phases[-1]["at"] <= now:
            continue
        phases[-1]["at"] = now
        changed += 1
    if changed:
        store.save()
    return changed


def _close_rows(core, plan, now):
    return _truncate_rows(
        core, [str(s.get("id") or "") for s in plan.get("slots", [])], now
    )


def _sleep_until(core, now):
    """When her current sleep episode ends, if she is asleep right now."""
    state = getattr(core, "state", None)
    if not isinstance(state, dict):
        return 0.0
    sleep = state.get("sleep") or {}
    if sleep.get("active") and not sleep.get("nap"):
        value = sleep.get("until")
        if isinstance(value, (int, float)) and float(value) > now:
            return float(value)
    return 0.0


def _switch(core, now, tight=False, shortage="", low=False):
    """Install the pending plan, or generate the day she wakes into."""
    pending = None
    if PENDING.exists():
        try:
            pending = json.loads(PENDING.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pending = None
        _log({
            "at": now,
            "type": "plan_switched" if _fresh(pending, now) else "pending_dropped",
        })
    if _fresh(pending, now):
        plan = pending
    else:
        if isinstance(pending, dict):
            for slot in pending.get("slots", ()):
                if slot.get("kind") == "social_meetup" and float(slot["end"]) <= now:
                    _record_friend_transition(core, slot, "missed", now)
        # She sleeps tonight, so the fresh plan belongs to the day she wakes into.
        plan = _build_day(core, now + 8 * 3600, now, tight, shortage, low)
        _apply_world_notes(plan)
        plan["created_at"] = now
        _log({"at": now, "type": "day_planned", "events": len(plan["slots"])})
    sleep_until = _sleep_until(core, now)
    wake_at = float(plan.get("wake_at") or 0)
    if sleep_until and sleep_until > wake_at:
        # Her actual sleep decides when the day starts, not the random draw.
        delta = sleep_until - wake_at
        meetup = next((slot for slot in plan.get("slots", ()) if slot["kind"] == "social_meetup"), None)
        if meetup is None:
            for slot in plan.get("slots", []):
                slot["start"] = float(slot["start"]) + delta
                slot["end"] = float(slot["end"]) + delta
            plan["bedtime_at"] = float(plan.get("bedtime_at") or 0) + delta
        else:
            # Sleep can compress the morning, but it cannot move the booked meeting.
            identity = meetup["friend_meetup_id"]
            start = float(meetup["start"])
            slots = plan["slots"]
            prep = min(float(slot["start"]) for slot in slots if slot.get("friend_meetup_id") == identity)
            if sleep_until >= start:
                _record_friend_transition(core, meetup, "cancelled", now)
                slots = [slot for slot in slots if slot.get("friend_meetup_id") != identity]
            shifted = []
            for slot in slots:
                if float(slot["start"]) < prep:
                    if float(slot["end"]) + delta > prep:
                        continue
                    slot["start"] += delta
                    slot["end"] += delta
                elif float(slot["start"]) < sleep_until:
                    continue
                shifted.append(slot)
            plan["slots"] = shifted
        plan["wake_at"] = sleep_until
    if _asleep(core, now) and now < float(plan.get("wake_at") or 0) - 60:
        # The new day starts with sleep, otherwise switching to it looks awake.
        plan["slots"].insert(0, {
            "id": f"sleep-{int(now)}",
            "name": "睡觉",
            "kind": "sleep",
            "start": now,
            "duration_min": max(1, int((float(plan["wake_at"]) - now) / 60)),
            "end": float(plan["wake_at"]),
            "status": "in_progress",
            "replaceable": False,
            "commitment": False,
        })
        plan["sleep_at"] = now
        _publish_event(plan["slots"][0], "started", now)
    daily_chain.dp.save(plan)
    PENDING.unlink(missing_ok=True)
    _bind_friend_slots(core, plan, now)
    _publish(core, plan, now)
    return plan


def sync_agenda(core, plan, now):
    """Mirror the plan into ``state["daily_rhythm"]`` so she can see it.

    The contact tools and prompts read that agenda; before the cutover it was
    generated from the old random pools.  Now it describes the real plan.
    """
    state = getattr(core, "state", None)
    if not isinstance(state, dict):
        return False
    day = _date(plan.get("day") or now)
    schedule = []
    for slot in plan.get("slots", []):
        mode, place = _place(slot)
        schedule.append({
            "id": str(slot.get("id") or ""),
            "start_at": float(slot.get("start") or 0),
            "latest_start": float(slot.get("end") or 0),
            "plan": {
                "kind": str(slot.get("kind") or ""),
                "mode": mode,
                "destination": place,
                "reason": str(slot.get("name") or ""),
            },
        })
    agenda = {
        "day": day,
        "fictional": True,
        "status": "按计划推进，不代表已做",
        "source": "daily_plan",
        "anchors": [],
        "schedule": schedule,
        "started": {},
    }
    old = state.get("daily_rhythm") or {}
    wake_at = float(plan.get("wake_at") or 0)
    bedtime_at = float(plan.get("bedtime_at") or 0)
    state["life_plan"] = {
        "day": day,
        "wake_at": wake_at,
        "bedtime_at": bedtime_at,
    }
    clock = state.get("life_clock")
    if isinstance(clock, dict):
        clock["wake_at"] = wake_at
        clock["bedtime_at"] = bedtime_at
    if (
        old.get("day") == day
        and old.get("source") == "daily_plan"
        and old.get("schedule") == schedule
    ):
        return False
    state["daily_rhythm"] = agenda
    return True


def ensure_cutover(core, now):
    """Close rows the old activity loop left open, exactly once."""
    state = getattr(core, "state", None)
    if not isinstance(state, dict) or state.get("life_plan_cutover_at"):
        return False
    store = getattr(core, "activities", None)
    closed = 0
    if store is not None:
        for row in store.data.get("virtual_life", []):
            if not isinstance(row, dict) or row.get("source") == "daily_plan":
                continue
            if row.get("cancelled_at") or row.get("activity_status") == "completed":
                continue
            phases = [
                phase for phase in row.get("phases") or []
                if isinstance(phase, dict) and isinstance(phase.get("at"), (int, float))
            ]
            if phases and max(phase["at"] for phase in phases) <= now:
                continue
            row["cancelled_at"] = now
            row["activity_status"] = "cancelled"
            row["status_updated_at"] = now
            closed += 1
        if closed:
            store.save()
    state["life_plan_cutover_at"] = now
    state["life_plan_cutover_closed"] = closed
    _log({"at": now, "type": "cutover", "closed": closed})
    return True


def tick(core=None, now=None):
    """Advance one step.  ``core`` is only used in live mode."""
    now = float(time.time() if now is None else now)
    if core is not None and MODE == "live":
        ensure_cutover(core, now)
    plan = daily_chain.dp.load()
    tight = bool(core.state.get("budget_tight")) if core is not None else False
    shortage = str(core.state.get("home_shortage") or "") if core is not None else ""
    low = bool(core.state.get("mood_low")) if core is not None else False
    if plan and not _day_over(plan, core, now):
        changed = _rollover(plan, now, tight, shortage, core=core, low=low)
        changed = _advance(plan, now, core) or changed
        if changed:
            daily_chain.dp.save(plan)
            _publish(core, plan, now)
    else:
        if plan:
            closed, skipped = _close_out(plan, now, core)
            rows = _close_rows(core, plan, now)
            if closed or skipped or rows:
                _log({"at": now, "type": "day_closed", "completed": closed,
                      "skipped": skipped, "rows": rows})
        plan = _switch(core, now, tight, shortage, low)
    if core is not None and MODE == "live" and sync_agenda(core, plan, now):
        core.save()
    return plan
