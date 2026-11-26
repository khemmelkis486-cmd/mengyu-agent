"""Persistent, idempotent life-event ledger for Mengyu.

The event ledger owns durable event state (for example a clinic visit).  It is
not a second work-order system: freelance work remains owned by work.py.
"""

from data.plugins.mengyu_contact import config

import json
import os
import re
import shutil
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from data.plugins.mengyu_contact.state_write import write_json

DATA = config.path(plugin="mengyu_contact")
PATH = DATA / "events.json"
_LOCK = threading.RLock()
SCHEMA_VERSION = 4
from data.plugins.mengyu_contact.local_clock import TZ

STAGE_WORDS = {
    "看诊": (
        ("候诊", "挂号后等候叫号"),
        ("接诊", "医生了解症状并进行初步判断"),
        ("检查", "完成必要检查并等待结果"),
        ("离院", "本次看诊结束，保留诊断和后续安排"),
    ),
    "复诊": (("待复诊", "按新的预约时间复诊"),),
}

CLINIC_WORDS = (
    "看诊", "问诊", "医生", "门诊", "医院", "诊室", "复诊", "离院",
    "看病", "挂号", "候诊", "叫号", "检查", "开药", "拿药", "诊断", "喉咙", "嗓子",
)
RESULT_WORDS = (
    "医生说是", "医生判断", "医生诊断", "诊断", "确诊",
    "检查结果", "结果出来", "开了药", "开药", "给我开", "拿了药",
    "拿完药", "确诊为", "诊断为", "医生说没事", "问题不大",
)
VISIT_START_WORDS = (
    "去医院", "到医院", "在医院", "去门诊", "到门诊", "进诊室",
    "挂号", "候诊", "等叫号", "看了医生", "看医生", "去看诊",
    "去看病", "开始问诊", "医生叫到", "看了喉咙", "看喉咙",
    "看了嗓子", "看嗓子", "去看喉咙", "去看嗓子",
)

FUTURE_OR_NEGATED = re.compile(
    r"(?:还没|尚未|没有去|没去|并没有|根本没|其实没|不去|先不去|"
    r"(?:准备|打算|计划).{0,8}(?:去医院|去门诊|去看诊|去看病|复诊)|"
    r"等.{0,12}(?:再去|去医院|去看诊|去看病)|明天去|后天去|改天去|如果去|要是去)"
)
META_RETRACTION = re.compile(
    r"(?:前面那次|刚才那次|此前那次).{0,16}(?:并没有发生|没有发生|并未发生)|"
    r"(?:不能|没法|不该).{0,16}(?:说成真实|说成真的|当成真实)"
)
NEW_VISIT = re.compile(
    r"(?:今天|刚才|刚刚|现在|又|再次|重新).{0,10}(?:去医院|到医院|去门诊|看诊|复诊|看病)|"
    r"(?:去医院|到医院|去门诊|看诊|复诊|看病).{0,8}(?:了|过|完成)"
)
FOLLOWUP_RE = re.compile(
    r"(?:三天后|两天后|一天后|明天|后天|下周(?:一|二|三|四|五|六|日|天)?|"
    r"周[一二三四五六日天]|过几天|按时).{0,12}(?:复诊|复查|回诊)|"
    r"(?:复诊|复查|回诊)(?:安排在|时间是|约在|改到|改约到|调整到|挪到|定在)[^，。！？!?；;\n]{0,32}"
)
RESCHEDULE_RE = re.compile(r"改到|改约到|改成|调整到|挪到|重新约到|约到|预约到|安排到|安排在|约在|定在|时间是")
RESCHEDULE_UNCONFIRMED_RE = re.compile(
    r"(?:还没|没有|没|未|不).{0,8}(?:改到|改约到|改成|调整到|挪到|重新约到|约到|预约到)|"
    r"(?:准备|打算|计划|考虑|想|可能会).{0,12}(?:改到|改约到|改成|调整到|挪到|重新约到|约到|预约到|安排在|约在|定在|时间是)"
)
VISIT_RESULT_CONTEXT = re.compile(r"(?:复诊|复查|回诊).{0,10}(?:结果|结束|看完|离院|医生|开药|检查)")
CLOSE_WORDS = ("结束", "看完", "拿完药", "离院", "回住处", "回家", "到家")


def _empty() -> dict:
    return {"schema_version": SCHEMA_VERSION, "events": [], "processed_deliveries": []}


def _load_unlocked() -> dict:
    if not PATH.exists():
        return _empty()
    try:
        value = json.loads(PATH.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        # Never silently replace a corrupt ledger with an empty one.
        backup = PATH.with_name(f"{PATH.name}.corrupt-{int(time.time())}")
        try:
            os.replace(PATH, backup)
        except OSError as move_exc:
            raise RuntimeError(
                f"events.json is unreadable and could not be quarantined: {move_exc}"
            ) from exc
        return _empty()
    if not isinstance(value, dict) or not isinstance(value.get("events"), list):
        raise ValueError("events.json has an unsupported structure")
    value.setdefault("schema_version", 1)
    value.setdefault("processed_deliveries", [])
    value["events"] = [item for item in value["events"] if isinstance(item, dict)]
    return value


def load() -> dict:
    with _LOCK:
        return _load_unlocked()


def _save_unlocked(data: dict) -> None:
    data["schema_version"] = SCHEMA_VERSION
    write_json(PATH, data, "EventStore")

def save(data: dict) -> None:
    with _LOCK:
        _save_unlocked(data)


def _is_voided(item: dict) -> bool:
    return bool(item.get("voided") or item.get("superseded_by"))


def _normalise_event_title(item: dict) -> str:
    title = str(item.get("title") or item.get("stage") or item.get("kind") or "").strip()
    if item.get("kind") == "复诊" and ("复诊" in title or item.get("stage") == "待复诊"):
        title = "复诊"
    return re.sub(r"[\s，。！？、；：,.!?;:'\"“”‘’（）()【】\[\]_-]+", "", title).lower()


def _event_identity(item: dict):
    due = item.get("eta_at")
    if not isinstance(due, (int, float)):
        return None
    return (str(item.get("kind") or ""), _normalise_event_title(item), int(float(due) // 60))


def _dedupe_open_events(data: dict, now: float) -> bool:
    """Merge duplicate open events by normalized title + scheduled minute.

    Duplicate rows remain as audit history with ``superseded_by``; they are
    excluded from active planning without deleting their original evidence.
    """
    groups = {}
    for item in data.get("events", []):
        if item.get("closed") or _is_voided(item):
            continue
        identity = _event_identity(item)
        if identity is None:
            continue
        groups.setdefault(identity, []).append(item)
    changed = False
    status_rank = {"awaiting_result": 4, "in_progress": 3, "scheduled": 2, "pending": 1}
    for rows in groups.values():
        if len(rows) < 2:
            continue
        rows.sort(key=lambda row: (
            -status_rank.get(str(row.get("status") or ""), 0),
            row.get("created_at", now),
            row.get("id", ""),
        ))
        canonical = rows[0]
        parents = set(canonical.get("followup_parents") or [])
        if canonical.get("followup_of"):
            parents.add(canonical["followup_of"])
        for duplicate in rows[1:]:
            if duplicate.get("followup_of"):
                parents.add(duplicate["followup_of"])
            if duplicate.get("followup_parents"):
                parents.update(duplicate["followup_parents"])
            canonical["outcome"] = _merge_outcome(
                str(canonical.get("outcome") or ""), str(duplicate.get("outcome") or "")
            )
            history = canonical.setdefault("reschedule_history", [])
            for entry in duplicate.get("reschedule_history") or []:
                if entry not in history:
                    history.append(entry)
            duplicate["superseded_by"] = canonical.get("id")
            duplicate["status"] = "superseded_duplicate"
            duplicate["duplicate_marked_at"] = now
            duplicate["updated_at"] = max(now, duplicate.get("updated_at", now))
            changed = True
        if parents:
            canonical["followup_parents"] = sorted(parents)
        canonical["updated_at"] = max(
            [canonical.get("updated_at", now)] + [row.get("updated_at", now) for row in rows[1:]]
        )
    return changed


def _active_clinic(data: dict):
    active = [
        item for item in data["events"]
        if item.get("kind") in ("看诊", "复诊") and not item.get("closed") and not _is_voided(item)
    ]
    return max(active, key=lambda row: row.get("updated_at", row.get("created_at", 0))) if active else None


def _latest_clinic(data: dict, now: float, max_age: float = 72 * 3600):
    rows = [
        item for item in data["events"]
        if item.get("kind") in ("看诊", "复诊")
        and not _is_voided(item)
        and 0 <= now - item.get("created_at", 0) <= max_age
    ]
    return max(rows, key=lambda row: row.get("updated_at", row.get("created_at", 0))) if rows else None


def _followup_plan(message: str) -> str:
    match = FOLLOWUP_RE.search(message)
    return match.group(0).strip(" ，,。；;")[:100] if match else ""


def _chinese_number(value):
    mapping = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    if value.isdigit():
        return int(value)
    if value == "十":
        return 10
    if value.startswith("十"):
        return 10 + mapping.get(value[1:], 0)
    if value.endswith("十"):
        return mapping.get(value[0], 1) * 10
    if "十" in value:
        left, right = value.split("十", 1)
        return mapping.get(left, 1) * 10 + mapping.get(right, 0)
    return mapping.get(value)


def _parse_reschedule_at(message: str, now: float, fallback=0):
    """Parse an explicit future date/time in Chinese appointment text."""
    text = str(message or "")
    current = datetime.fromtimestamp(now, TZ)
    target_day = None

    match = re.search(r"(20\d{2})[-年/.](\d{1,2})[-月/.](\d{1,2})日?", text)
    if match:
        try:
            target_day = datetime(int(match[1]), int(match[2]), int(match[3]), tzinfo=TZ).date()
        except ValueError:
            return None
    if target_day is None:
        match = re.search(r"(?<!\d)(\d{1,2})月(\d{1,2})日", text)
        if not match:
            match = re.search(r"(?<!\d)(\d{1,2})/(\d{1,2})(?!\d)", text)
        if match:
            month, day = int(match[1]), int(match[2])
            try:
                target_day = datetime(current.year, month, day, tzinfo=TZ).date()
            except ValueError:
                return None
            if "明年" in text:
                try:
                    target_day = target_day.replace(year=current.year + 1)
                except ValueError:
                    return None
            elif target_day < current.date():
                return None

    if target_day is None:
        relative = re.search(r"大后天|后天|明天|今天", text)
        if relative:
            offset = {"今天": 0, "明天": 1, "后天": 2, "大后天": 3}[relative.group(0)]
            target_day = current.date() + timedelta(days=offset)
        else:
            days = re.search(r"([一二两三四五六七八九十\d]+)天后", text)
            if days:
                count = _chinese_number(days.group(1))
                if count is not None and 1 <= count <= 60:
                    target_day = current.date() + timedelta(days=count)

    if target_day is None:
        week = re.search(r"(下周|本周|这周)?(?:星期|周)([一二三四五六日天])", text)
        if week:
            weekday = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}[week[2]]
            delta = (weekday - current.weekday()) % 7
            if week[1] == "下周":
                days_to_next_monday = 7 - current.weekday()
                target_day = current.date() + timedelta(days=days_to_next_monday + weekday)
            else:
                if week[1] in ("本周", "这周") and delta == 0 and current.hour >= 9:
                    delta = 7
                elif not week[1] and delta == 0:
                    delta = 7
                target_day = current.date() + timedelta(days=delta)

    if target_day is None:
        return None

    hour = minute = None
    clock = re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", text)
    if clock:
        hour, minute = int(clock[1]), int(clock[2])
    else:
        clock = re.search(r"(上午|早上|中午|下午|晚上|傍晚)?\s*([一二两三四五六七八九十\d]{1,3})点(半|[一二两三四五六七八九十\d]{1,3}分?)?", text)
        if clock:
            period, raw_hour, raw_minute = clock.groups()
            hour = _chinese_number(raw_hour)
            if hour is None:
                return None
            if raw_minute == "半":
                minute = 30
            elif raw_minute:
                minute = _chinese_number(re.sub(r"分$", "", raw_minute))
                if minute is None:
                    return None
            else:
                minute = 0
            if period in ("下午", "晚上", "傍晚") and hour < 12:
                hour += 12
            elif period in ("上午", "早上") and hour == 12:
                hour = 0
    if hour is None:
        if fallback:
            previous = datetime.fromtimestamp(float(fallback), TZ)
            hour, minute = previous.hour, previous.minute
        else:
            hour, minute = 9, 30
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    result = datetime.combine(target_day, datetime.min.time(), TZ).replace(hour=hour, minute=minute)
    if result.timestamp() <= now:
        return None
    return result.timestamp()


def _schedule_followup_event(data: dict, parent: dict, message: str, now: float):
    plan = _followup_plan(message)
    # Once a clinic visit has just been completed, a natural follow-up such as
    # "那就改到周五下午三点" may omit the word 复诊.  The closed parent event
    # supplies the context; keep the explicit reschedule text as the plan.
    if not plan and RESCHEDULE_RE.search(str(message or "")):
        plan = str(message).strip(" ，,。；;")[:100]
    if not plan:
        return None
    due_at = _parse_reschedule_at(plan, now)
    if due_at is None:
        return None
    identity = ("复诊", "复诊", int(float(due_at) // 60))
    existing = next((
        item for item in data["events"]
        if item.get("followup_of") == parent.get("id") and not item.get("closed") and not _is_voided(item)
    ), None)
    if existing:
        existing["eta_at"] = due_at
        existing["status"] = "scheduled"
        existing.pop("due_triggered_at", None)
        existing.pop("last_due_eta_at", None)
        existing["stage"] = "待复诊"
        existing["title"] = existing.get("title") or "后续复诊"
        existing["next_step"] = "按预约时间复诊"
        existing["updated_at"] = max(now, existing.get("updated_at", now))
        existing.setdefault("followup_parents", [])
        if parent.get("id") and parent["id"] not in existing["followup_parents"]:
            existing["followup_parents"].append(parent["id"])
        _dedupe_open_events(data, now)
        return next((row for row in data["events"] if _event_identity(row) == identity and not _is_voided(row)), existing)
    # Two historical parent rows can describe the same actual follow-up slot.
    existing = next((
        item for item in data["events"]
        if not item.get("closed") and not _is_voided(item) and _event_identity(item) == identity
    ), None)
    if existing:
        existing.setdefault("followup_parents", [])
        if parent.get("id") and parent["id"] not in existing["followup_parents"]:
            existing["followup_parents"].append(parent["id"])
        existing["followup_plan"] = plan[:100]
        existing["updated_at"] = max(now, existing.get("updated_at", now))
        _dedupe_open_events(data, now)
        return next((row for row in data["events"] if _event_identity(row) == identity and not _is_voided(row)), existing)
    event = {
        "id": uuid.uuid4().hex,
        "kind": "复诊",
        "title": "后续复诊",
        "stage": "待复诊",
        "next_step": "按预约时间复诊",
        "eta_at": due_at,
        "outcome": "",
        "followup_plan": plan[:100],
        "followup_of": parent.get("id"),
        "followup_parents": [parent.get("id")] if parent.get("id") else [],
        "status": "scheduled",
        "fictional": True,
        "closed": False,
        "voided": False,
        "created_at": now,
        "updated_at": now,
    }
    data["events"] = (data["events"] + [event])[-80:]
    _dedupe_open_events(data, now)
    return next((row for row in data["events"] if _event_identity(row) == _event_identity(event) and not _is_voided(row)), event)


def _has_clinic_result(message: str) -> bool:
    return any(word in message for word in RESULT_WORDS)


def _has_visit_start(message: str) -> bool:
    if not any(word in message for word in CLINIC_WORDS):
        return False
    if FUTURE_OR_NEGATED.search(message) or META_RETRACTION.search(message):
        return False
    return any(word in message for word in VISIT_START_WORDS)


def _new_event(data: dict, now: float, title: str, *, stage="候诊", next_step="挂号后等候叫号") -> dict:
    event = {
        "id": uuid.uuid4().hex,
        "kind": "看诊",
        "title": title[:80] or "看诊",
        "status": "scheduled",
        "fictional": True,
        "stage": stage,
        "next_step": next_step,
        "eta_at": now + 2 * 3600,
        "outcome": "",
        "followup_plan": "",
        "closed": False,
        "voided": False,
        "created_at": now,
        "updated_at": now,
    }
    data["events"] = (data["events"] + [event])[-40:]
    return event


def open_event(kind, title, now=None, stage=None, next_step=None, eta_hours=1.0) -> dict:
    now = time.time() if now is None else float(now)
    due_at = now + max(0, float(eta_hours)) * 3600
    candidate = {"kind": kind, "title": str(title or kind)[:80], "eta_at": due_at}
    identity = _event_identity(candidate)
    with _LOCK:
        data = _load_unlocked()
        for item in data["events"]:
            if item.get("closed") or _is_voided(item):
                continue
            if _event_identity(item) == identity:
                return item
        stages = STAGE_WORDS.get(kind)
        event = {
            "id": uuid.uuid4().hex,
            "kind": kind,
            "title": str(title or kind)[:80],
            "status": "scheduled",
            "fictional": True,
            "stage": stage or (stages[0][0] if stages else "进行中"),
            "next_step": next_step if next_step is not None else (stages[0][1] if stages else ""),
            "eta_at": due_at,
            "outcome": "",
            "followup_plan": "",
            "closed": False,
            "voided": False,
            "created_at": now,
            "updated_at": now,
        }
        data["events"] = (data["events"] + [event])[-80:]
        _dedupe_open_events(data, now)
        _save_unlocked(data)
        return next((row for row in data["events"] if _event_identity(row) == identity and not _is_voided(row)), event)


def advance(event_id, stage=None, next_step=None, eta_hours=None, now=None) -> dict:
    now = time.time() if now is None else float(now)
    with _LOCK:
        data = _load_unlocked()
        for item in data["events"]:
            if item.get("id") != event_id or item.get("closed") or _is_voided(item):
                continue
            if stage:
                item["stage"] = stage
            if next_step is not None:
                item["next_step"] = next_step
            if eta_hours is not None:
                item["eta_at"] = now + max(0, float(eta_hours)) * 3600
            item["updated_at"] = max(now, item.get("created_at", now), item.get("updated_at", now))
            identity = _event_identity(item)
            _dedupe_open_events(data, now)
            _save_unlocked(data)
            return next((row for row in data["events"] if _event_identity(row) == identity and not _is_voided(row)), item)
    return {}


def close_event(event_id, outcome="", now=None, followup_plan=None) -> dict:
    now = time.time() if now is None else float(now)
    with _LOCK:
        data = _load_unlocked()
        for item in data["events"]:
            if item.get("id") != event_id:
                continue
            item["closed"] = True
            item["status"] = "completed"
            item["completed_at"] = now
            item["stage"] = "离院" if item.get("kind") in ("看诊", "复诊") else item.get("stage", "已结束")
            if outcome:
                item["outcome"] = str(outcome)[:500]
            item["next_step"] = ""
            if followup_plan is not None:
                item["followup_plan"] = str(followup_plan)[:100]
            item["updated_at"] = max(now, item.get("created_at", now), item.get("updated_at", now))
            _save_unlocked(data)
            return item
    return {}


def current(now=None) -> list:
    now = time.time() if now is None else float(now)
    active = []
    for item in load()["events"]:
        if item.get("closed") or _is_voided(item):
            continue
        view = dict(item)
        view["overdue"] = now > item.get("eta_at", now)
        view["minutes_open"] = max(0, int((now - item.get("created_at", now)) / 60))
        active.append(view)
    return active


def active_summary(now=None) -> list:
    """Return only unfinished events for planners and proactive decisions.

    A completed event is durable memory, not a recurring announcement candidate.
    Keep this projection deliberately free of outcomes from closed events so a
    periodic planner cannot repeatedly re-introduce an already shared result.
    """
    now = time.time() if now is None else float(now)
    lines = []
    for item in current(now):
        tip = "已超过预计时间，需要确认进展或调整安排" if item["overdue"] else ""
        lines.append({
            "id": item.get("id"),
            "kind": item.get("kind"),
            "title": item.get("title"),
            "stage": item.get("stage"),
            "status": item.get("status", "scheduled"),
            "due_at": item.get("eta_at"),
            "next": item.get("next_step"),
            "提示": tip,
        })
    return lines


def create_planned_event(candidate, now=None) -> dict:
    """Create a durable generic life commitment only from a concrete future date."""
    now = time.time() if now is None else float(now)
    if not isinstance(candidate, dict):
        return {}
    kind = str(candidate.get("kind") or "约定").strip()[:24]
    title = str(candidate.get("title") or "").strip()[:80]
    due_value = candidate.get("due_at")
    if isinstance(due_value, (int, float)):
        due_at = float(due_value)
    else:
        due_at = _parse_reschedule_at(str(due_value or ""), now)
    if not title or due_at is None or not now < due_at <= now + 90 * 86400:
        return {}
    with _LOCK:
        data = _load_unlocked()
        candidate_identity = (kind, _normalise_event_title({"kind": kind, "title": title}), int(float(due_at) // 60))
        existing = next((
            row for row in data["events"]
            if not row.get("closed") and not _is_voided(row)
            and _event_identity(row) == candidate_identity
        ), None)
        if existing:
            return dict(existing)
        event = {
            "id": uuid.uuid4().hex,
            "kind": kind,
            "title": title,
            "stage": "已安排",
            "next_step": str(candidate.get("next_step") or "按约定时间处理并记录结果")[:160],
            "eta_at": due_at,
            "outcome": "",
            "followup_plan": "",
            "status": "scheduled",
            "fictional": True,
            "source": "life_planner",
            "closed": False,
            "voided": False,
            "created_at": now,
            "updated_at": now,
        }
        data["events"] = (data["events"] + [event])[-40:]
        _dedupe_open_events(data, now)
        _save_unlocked(data)
        return dict(event)


def process_due(now=None) -> list:
    """Transition every newly overdue open event to a single result-needed state."""
    now = time.time() if now is None else float(now)
    changed = []
    with _LOCK:
        data = _load_unlocked()
        for item in data["events"]:
            if item.get("closed") or _is_voided(item):
                continue
            due_at = item.get("eta_at")
            if not isinstance(due_at, (int, float)) or due_at > now:
                continue
            if item.get("status") == "awaiting_result":
                continue
            if item.get("last_due_eta_at") == due_at:
                continue
            item["status"] = "awaiting_result"
            item["due_triggered_at"] = now
            item["last_due_eta_at"] = due_at
            item["next_step"] = "确认已经完成、需要改期，还是决定取消"
            item["updated_at"] = max(now, item.get("updated_at", now))
            changed.append(dict(item))
        if changed:
            _save_unlocked(data)
    return changed


def apply_planner_update(update, now=None) -> dict:
    """Apply an explicit planner outcome to one overdue event, idempotently."""
    now = time.time() if now is None else float(now)
    if not isinstance(update, dict):
        return {}
    identity = str(update.get("event_id") or update.get("id") or "")
    action = str(update.get("action") or "").lower()
    if not identity or action not in ("completed", "rescheduled", "cancelled", "not_done"):
        return {}
    with _LOCK:
        data = _load_unlocked()
        event = next((row for row in data["events"] if row.get("id") == identity), None)
        if not event or event.get("closed") or _is_voided(event):
            return {}
        if event.get("status") != "awaiting_result":
            return {}
        outcome = str(update.get("outcome") or "").strip()[:500]
        if action == "completed":
            if not outcome:
                return {}
            event["closed"] = True
            event["status"] = "completed"
            event["completed_at"] = now
            event["stage"] = "离院" if event.get("kind") in ("看诊", "复诊") else "已结束"
            event["outcome"] = _merge_outcome(event.get("outcome", ""), outcome)
            event["next_step"] = ""
            followup = _followup_plan(outcome)
            if followup:
                event["followup_plan"] = followup
                _schedule_followup_event(data, event, outcome, now)
        elif action == "rescheduled":
            target = update.get("rescheduled_at")
            if isinstance(target, (int, float)) and target > now:
                new_due = float(target)
            else:
                new_due = _parse_reschedule_at(str(update.get("rescheduled_to") or ""), now, event.get("eta_at"))
            if new_due is None:
                return {}
            old_due = event.get("eta_at")
            event.setdefault("reschedule_history", []).append({"from": old_due, "to": new_due, "at": now})
            event["eta_at"] = new_due
            event["status"] = "scheduled"
            event.pop("due_triggered_at", None)
            event.pop("last_due_eta_at", None)
            event["stage"] = "待复诊" if event.get("kind") in ("看诊", "复诊") else event.get("stage", "已安排")
            event["next_step"] = str(update.get("next_step") or "按新的时间安排推进")[:160]
            if outcome:
                event["outcome"] = _merge_outcome(event.get("outcome", ""), outcome)
        elif action == "cancelled":
            event["closed"] = True
            event["status"] = "cancelled"
            event["cancelled_at"] = now
            event["outcome"] = _merge_outcome(event.get("outcome", ""), outcome or "已取消")
            event["next_step"] = ""
        else:
            eta_hours = update.get("eta_hours", 2)
            try:
                eta_hours = max(0.25, min(168, float(eta_hours)))
            except (TypeError, ValueError):
                eta_hours = 2
            event["status"] = "in_progress"
            event.pop("due_triggered_at", None)
            event["eta_at"] = now + eta_hours * 3600
            event["next_step"] = str(update.get("next_step") or "继续处理，并在下一节点确认结果")[:160]
            if outcome:
                event["outcome"] = _merge_outcome(event.get("outcome", ""), outcome)
        event["updated_at"] = max(now, event.get("updated_at", now))
        identity = _event_identity(event) if not event.get("closed") else None
        _dedupe_open_events(data, now)
        _save_unlocked(data)
        canonical = next((row for row in data["events"] if identity and _event_identity(row) == identity and not _is_voided(row)), None)
        return dict(canonical or event)


def recent_completed_summary(now=None, max_age=86400, limit=3) -> list:
    """Return recent completed events for conversational recall only."""
    now = time.time() if now is None else float(now)
    data = load()
    finished = [
        item for item in data["events"]
        if item.get("closed") and item.get("status") != "cancelled" and not _is_voided(item)
        and 0 <= now - item.get("updated_at", 0) < max_age
    ][-limit:]
    return [
        {
            "kind": item.get("kind"),
            "title": item.get("title"),
            "结果": item.get("outcome") or "已结束",
            "后续安排": item.get("followup_plan") or None,
            "已结束": True,
        }
        for item in finished
    ]


def summary(now=None) -> list:
    """Backward-compatible full summary for direct conversation context.

    Do not pass this combined projection to periodic planning or proactive
    message generation; use active_summary() there instead.
    """
    return active_summary(now) + recent_completed_summary(now)


def _merge_outcome(old: str, new: str) -> str:
    old, new = str(old or "").strip(), str(new or "").strip()
    if not new:
        return old
    if not old:
        return new[:500]
    if new in old:
        return old[:500]
    if old in new:
        return new[:500]
    return (old + "；" + new)[:500]


def _mark_completed(data: dict, event: dict, message: str, now: float) -> None:
    event["closed"] = True
    event["status"] = "completed"
    event["completed_at"] = now
    event["stage"] = "离院" if event.get("kind") in ("看诊", "复诊") else "已结束"
    event["next_step"] = ""
    event["outcome"] = _merge_outcome(event.get("outcome", ""), message[:500])
    followup = _followup_plan(message)
    if followup:
        event["followup_plan"] = followup
        if event.get("kind") in ("看诊", "复诊"):
            _schedule_followup_event(data, event, message, now)
    event["updated_at"] = max(now, event.get("created_at", now), event.get("updated_at", now))


def ensure_from_text(text, now=None):
    """Create only for a stated visit, never for a future plan or diagnosis recap."""
    now = time.time() if now is None else float(now)
    message = str(text or "").strip()
    if not message or META_RETRACTION.search(message) or FUTURE_OR_NEGATED.search(message):
        return None
    if _has_clinic_result(message):
        return None  # result text updates/closes an existing or recent event in note_text
    if not _has_visit_start(message):
        return None
    active = [item for item in current(now) if item.get("kind") in ("看诊", "复诊")]
    if active:
        return active[0]
    return open_event("看诊", message[:60] or "看诊", now=now, eta_hours=2)


def confirm_followup(text, now, source_id, *, waiting_for_this_event=False):
    """Apply explicit user updates only to an unambiguous existing follow-up."""
    message = str(text or "").strip()
    if not message or re.search(r"[？?]|吗[。！!]?$|如果|要是|可能|准备|想改|还没|没做|没去|没看|未完|待会|明天做|明天去|后天做|后天去", message):
        return False
    mentions = "复诊" in message
    if re.match(r"^(?:我|我的).{0,8}复诊", message) and not waiting_for_this_event:
        return False
    is_reschedule = bool(re.search(r"改(?:约|到|成|期)|推到|挪到|换到", message))
    is_complete = bool(re.search(r"(?:复诊.{0,8}(?:做完|结束|完成)|(?:做完|完成|结束).{0,5}复诊|^(?:我)?(?:今天|刚刚|刚才|刚)?(?:已经)?(?:做完了|结束了|完成了))[。！!呀呢啦]*$", message))
    if not (mentions or waiting_for_this_event) or not (is_reschedule or is_complete):
        return False
    with _LOCK:
        data = _load_unlocked()
        processed = data.setdefault("processed_followup_input", [])
        if source_id and source_id in processed:
            return False
        targets = [
            item for item in data["events"]
            if item.get("kind") == "复诊" and not item.get("closed") and not _is_voided(item)
        ]
        if len(targets) != 1:
            return False
        target = targets[0]
        if is_reschedule:
            due = _parse_reschedule_at(message, now, target.get("eta_at", 0))
            if due is None:
                return False
            old_due = target.get("eta_at")
            target.setdefault("reschedule_history", []).append({"from": old_due, "to": due, "at": now})
            target.update(eta_at=due, status="scheduled", stage="待复诊", next_step="按预约时间复诊")
            target.pop("last_due_eta_at", None)
            target.pop("due_triggered_at", None)
        else:
            _mark_completed(data, target, message, now)
        target["updated_at"] = max(now, target.get("updated_at", now))
        if source_id:
            data["processed_followup_input"] = (processed + [source_id])[-150:]
        _dedupe_open_events(data, now)
        _save_unlocked(data)
        return True


def note_text(text, now=None, source_id=None) -> None:
    """Apply confirmed delivered text once; keep one visit through its result."""
    now = time.time() if now is None else float(now)
    message = str(text or "").strip()
    if not message:
        return
    if META_RETRACTION.search(message):
        # Do not let a meta-level retraction erase an already established fictional event.
        return
    with _LOCK:
        data = _load_unlocked()
        if source_id:
            processed = data.setdefault("processed_deliveries", [])
            if source_id in processed:
                return
        changed = False
        sentences = [s.strip() for s in re.split(r"[，,。！？!?；;\n]+", message) if s.strip()]
        clinic_text = "；".join(
            sentence for sentence in sentences
            if any(word in sentence for word in CLINIC_WORDS)
            or FOLLOWUP_RE.search(sentence)
        ) or message
        active = _active_clinic(data)
        latest_clinic = _latest_clinic(data, now)
        for sentence in sentences:
            has_clinic_context = any(word in sentence for word in CLINIC_WORDS)
            can_reschedule_recent_visit = bool(
                RESCHEDULE_RE.search(sentence)
                and (
                    active is not None
                    or (latest_clinic is not None and latest_clinic.get("closed"))
                )
            )
            if not has_clinic_context and not (
                active and any(word in sentence for word in CLOSE_WORDS)
            ) and not can_reschedule_recent_visit:
                continue
            if RESCHEDULE_UNCONFIRMED_RE.search(sentence):
                continue
            if FUTURE_OR_NEGATED.search(sentence) and not RESCHEDULE_RE.search(sentence):
                continue
            result = _has_clinic_result(sentence)
            if result:
                target = active or _latest_clinic(data, now)
                future_followup_recap = bool(
                    target
                    and target.get("kind") == "复诊"
                    and target.get("stage") == "待复诊"
                    and not NEW_VISIT.search(sentence)
                    and not VISIT_RESULT_CONTEXT.search(sentence)
                )
                if not future_followup_recap:
                    if target is None or (NEW_VISIT.search(sentence) and target.get("closed")):
                        target = _new_event(data, now, sentence[:60], stage="离院", next_step="")
                    _mark_completed(data, target, clinic_text, now)
                    active = None
                    changed = True
                    continue
                if not RESCHEDULE_RE.search(sentence):
                    # A repeated diagnosis recap is not evidence that the future visit happened.
                    continue
            if RESCHEDULE_RE.search(sentence) and active is None:
                parent = latest_clinic or _latest_clinic(data, now)
                if parent and parent.get("closed"):
                    active = _schedule_followup_event(data, parent, sentence, now)
                    if active:
                        changed = True
                        continue
            if RESCHEDULE_RE.search(sentence) and active:
                new_due = _parse_reschedule_at(sentence, now, active.get("eta_at"))
                if new_due is not None:
                    old_due = active.get("eta_at")
                    active.setdefault("reschedule_history", []).append({"from": old_due, "to": new_due, "at": now})
                    active["eta_at"] = new_due
                    active["status"] = "scheduled"
                    active.pop("due_triggered_at", None)
                    active.pop("last_due_eta_at", None)
                    active["stage"] = "待复诊" if active.get("kind") in ("看诊", "复诊") else active.get("stage", "已安排")
                    active["next_step"] = "按新的预约时间复诊" if active.get("kind") in ("看诊", "复诊") else "按新的时间安排推进"
                    active["updated_at"] = max(now, active.get("updated_at", now))
                    changed = True
                    continue
            if active is None and _has_visit_start(sentence):
                active = _new_event(data, now, sentence[:60])
                changed = True
            if active is None:
                continue
            if re.search(r"叫到|进诊室|医生问|开始问诊|医生给我看", sentence):
                active["stage"] = "接诊"
                active["status"] = "in_progress"
                active["next_step"] = "医生了解症状并进行初步判断"
                active["eta_at"] = now + 3600
                active["updated_at"] = max(now, active.get("created_at", now))
                changed = True
            elif re.search(r"做了检查|正在检查|检查中|要做.{0,3}检查|还要检查|安排检查|抽血|拍片|检查结果还", sentence):
                active["stage"] = "检查"
                active["status"] = "in_progress"
                active["next_step"] = "等待检查结果或医生说明"
                active["eta_at"] = now + 3600
                active["updated_at"] = max(now, active.get("created_at", now))
                changed = True
            elif any(word in sentence for word in CLOSE_WORDS):
                _mark_completed(data, active, sentence, now)
                active = None
                changed = True
        if source_id:
            processed = data.setdefault("processed_deliveries", [])
            processed.append(str(source_id))
            data["processed_deliveries"] = processed[-200:]
            changed = True
        if changed:
            _dedupe_open_events(data, now)
            _save_unlocked(data)


def reconcile_legacy(now=None) -> bool:
    """Conservative migration and idempotent repair of duplicate life events."""
    now = time.time() if now is None else float(now)
    with _LOCK:
        data = _load_unlocked()
        old_version = int(data.get("schema_version", 1))
        if old_version >= SCHEMA_VERSION:
            changed = _dedupe_open_events(data, now)
            if changed:
                backup = PATH.with_name(f"{PATH.name}.v{old_version}.dedupe.bak")
                if PATH.exists() and not backup.exists():
                    shutil.copy2(PATH, backup)
                _save_unlocked(data)
            return changed
        rows = [
            item for item in data["events"]
            if item.get("kind") == "看诊" and not _is_voided(item)
        ]
        rows.sort(key=lambda row: row.get("created_at", 0))
        changed = False
        clusters = []
        for item in rows:
            if not clusters or item.get("created_at", 0) - clusters[-1][-1].get("created_at", 0) > 4 * 3600:
                clusters.append([item])
            else:
                clusters[-1].append(item)
        for cluster in clusters:
            if len(cluster) < 2:
                continue
            titles = " ".join(str(x.get("title", "")) for x in cluster)
            outcomes = " ".join(str(x.get("outcome", "")) for x in cluster)
            if "复诊" not in titles + outcomes and not any(word in outcomes for word in RESULT_WORDS):
                continue
            canonical = next(
                (x for x in cluster if x.get("closed") and x.get("outcome") and not META_RETRACTION.search(x.get("title", ""))),
                cluster[0],
            )
            for item in cluster:
                if item is canonical:
                    continue
                if META_RETRACTION.search(str(item.get("title", ""))):
                    item["voided"] = True
                    item["status"] = "voided_correction"
                else:
                    canonical["outcome"] = _merge_outcome(canonical.get("outcome", ""), item.get("outcome", ""))
                    plan = item.get("followup_plan") or _followup_plan(str(item.get("title", "")) + "；" + str(item.get("outcome", "")))
                    if plan:
                        canonical["followup_plan"] = plan
                    item["superseded_by"] = canonical.get("id")
                    item["status"] = "superseded_duplicate"
                changed = True
            # A diagnosis/result record is conclusive evidence that the visit ended.
            if any(word in canonical.get("outcome", "") for word in RESULT_WORDS):
                canonical["closed"] = True
                canonical["stage"] = "离院"
                canonical["next_step"] = ""
                plan = canonical.get("followup_plan") or _followup_plan(canonical.get("outcome", ""))
                if plan:
                    canonical["followup_plan"] = plan
            canonical["updated_at"] = max(
                [canonical.get("updated_at", 0), canonical.get("created_at", 0)]
                + [x.get("updated_at", 0) for x in cluster]
            )
        # v2 stored follow-up appointments only as text on the completed visit.
        # Materialise one linked future event so it can become due and be resolved.
        for parent in list(data["events"]):
            if parent.get("kind") not in ("看诊", "复诊") or not parent.get("closed"):
                continue
            plan = str(parent.get("followup_plan") or "")
            if not plan or any(
                row.get("followup_of") == parent.get("id") for row in data["events"]
            ):
                continue
            anchor = float(parent.get("updated_at") or parent.get("created_at") or now)
            due_at = _parse_reschedule_at(plan, anchor)
            if due_at is None:
                continue
            child = {
                "id": uuid.uuid4().hex,
                "kind": "复诊",
                "title": "后续复诊",
                "stage": "待复诊",
                "next_step": "按预约时间复诊",
                "eta_at": due_at,
                "outcome": "",
                "followup_plan": plan[:100],
                "followup_of": parent.get("id"),
                "status": "scheduled",
                "fictional": True,
                "closed": False,
                "voided": False,
                "created_at": parent.get("updated_at", now),
                "updated_at": now,
            }
            data["events"].append(child)
            changed = True
        changed = _dedupe_open_events(data, now) or changed
        data["events"] = data["events"][-80:]
        data["schema_version"] = SCHEMA_VERSION
        if PATH.exists():
            backup = PATH.with_name(f"{PATH.name}.v{old_version}.bak")
            if not backup.exists():
                shutil.copy2(PATH, backup)
        _save_unlocked(data)
        return True
