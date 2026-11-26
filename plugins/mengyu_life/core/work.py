"""Fictional freelance ledger with explicit creation and bounded work sessions.

Reading a work summary is side-effect free.  New orders are created only by
ensure_order(), called from the activity-planning path.
"""

from data.plugins.mengyu_contact import config

import json
import math
import os
import random
import re
import shutil
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from data.plugins.mengyu_contact.state_write import write_json

DATA = config.path(plugin="mengyu_contact")
PATH = DATA / "work.json"
from data.plugins.mengyu_contact.local_clock import TZ
_LOCK = threading.RLock()
BRIEFS = (
    "给一家小咖啡店画新品海报，竖版暖色",
    "给独立杂志做一页黑白人物插画",
    "排版一份两折活动折页",
    "画三张小猫贴纸",
    "给播客做方形夜间氛围封面",
    "修改旧海报的低饱和绿色配色",
)
STAGES = ("接到需求", "起稿", "修改", "成稿待确认", "交付", "完成")
NEEDED = {"接到需求": 1800, "起稿": 7200, "修改": 5400}
GUIDANCE = """【手头工作】下面工单是已约定的虚构自由职业，截止/下一步/等待期限以同一份记录为准。自己的工单往前走，不靠重复口头说赶稿；在家工作用daylife mode=home，reason写具体工序，并补work={order_id:当前工单id,stage:起稿/修改/交付}。实际经历与工时随已接受的活动时间推进，不把计划直接说成已交。成稿待确认只能等到给出的期限；到时先交当前版本/预览确认，标题如需微调再处理，不永远卡在等正式标题，不编真实客户消息或收款。交付是虚构工作里的提交动作，不替真实第三人发消息、不假称工具发送。结单后有空档，不立刻又有一单压上来。工作只是生活的一部分，不每轮汇报，临近下一步期限再合理安排。"""


def _empty():
    return {"schema_version": 2, "orders": []}


def _load_unlocked():
    if not PATH.exists():
        return _empty()
    try:
        value = json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        backup = PATH.with_name(f"{PATH.name}.corrupt-{int(time.time())}")
        try:
            os.replace(PATH, backup)
        except OSError as move_exc:
            raise RuntimeError(f"work.json unreadable; could not quarantine: {move_exc}") from exc
        return _empty()
    if not isinstance(value, dict) or not isinstance(value.get("orders"), list):
        raise ValueError("work.json has an unsupported structure")
    value.setdefault("schema_version", 1)
    value["orders"] = [row for row in value["orders"] if isinstance(row, dict)]
    return value


def load():
    with _LOCK:
        return _load_unlocked()


def _save_unlocked(data):
    data["schema_version"] = 2
    write_json(PATH, data, "WorkStore")

def save(data):
    with _LOCK:
        _save_unlocked(data)


def reconcile_legacy():
    """One-time cleanup of legacy completed sessions; preserve a backup."""
    with _LOCK:
        data = _load_unlocked()
        if int(data.get("schema_version", 1)) >= 2:
            return False
        changed = False
        for order in data["orders"]:
            if order.get("stage") == "完成" and order.pop("session", None) is not None:
                changed = True
            session = order.get("session")
            if isinstance(session, dict):
                end = session.get("end")
                credited = session.get("credited_until")
                if isinstance(end, (int, float)) and isinstance(credited, (int, float)) and credited > end:
                    session["credited_until"] = end
                    changed = True
            created = order.get("created_at")
            started = order.get("stage_started")
            if isinstance(created, (int, float)) and isinstance(started, (int, float)) and started < created:
                order["stage_started"] = created
                changed = True
        data["schema_version"] = 2
        if PATH.exists():
            backup = PATH.with_name(PATH.name + ".v1.bak")
            if not backup.exists():
                shutil.copy2(PATH, backup)
        _save_unlocked(data)
        return True


def _current_order(data):
    active = [row for row in data["orders"] if row.get("stage") != "完成"]
    return max(active, key=lambda row: row.get("created_at", 0)) if active else None


def current(now=None):
    """Return the active order without creating or advancing anything."""
    with _LOCK:
        return _current_order(_load_unlocked())


def ensure_order(now=None):
    """Create the next order only when the explicit planning path asks for it."""
    now = time.time() if now is None else float(now)
    with _LOCK:
        data = _load_unlocked()
        active = _current_order(data)
        if active:
            return active
        if data["orders"]:
            latest = max(data["orders"], key=lambda row: row.get("created_at", 0))
            available_at = latest.get(
                "next_order_after", latest.get("completed_at", now) + 18 * 3600
            )
            if now < available_at:
                return None
        order = {
            "id": str(int(now * 1000)) + "-" + uuid_suffix(),
            "brief": random.choice(BRIEFS),
            "stage": "接到需求",
            "stage_started": now,
            "created_at": now,
            "deadline": now + random.choice((1, 2, 3)) * 86400,
            "revisions": 0,
            "worked_seconds": 0,
            "fictional": True,
        }
        data["orders"].append(order)
        data["orders"] = data["orders"][-40:]
        _save_unlocked(data)
        return order


def uuid_suffix():
    # A small collision guard while keeping order IDs readable in logs.
    import uuid
    return uuid.uuid4().hex[:8]


def confirmation_limit(now):
    local = datetime.fromtimestamp(now, TZ)
    if local.hour >= 21:
        return (
            (local + timedelta(days=1))
            .replace(hour=10, minute=0, second=0, microsecond=0)
            .timestamp()
        )
    return now + 2 * 3600


def _advance_unlocked(data, now):
    order = _current_order(data)
    if not order:
        return None, False
    before = json.dumps(order, ensure_ascii=False, sort_keys=True)
    session = order.get("session") or {}
    end = min(now, session.get("end", now))
    credited = session.get("credited_until", end)
    credit = max(0, end - credited)
    order["worked_seconds"] = order.get("worked_seconds", 0) + credit
    if session:
        session["credited_until"] = max(credited, end)
        order["session"] = session
    if order["stage"] == "成稿待确认" and now >= order.get(
        "confirmation_until", confirmation_limit(now)
    ):
        # The waiting window can move the task to a delivery-ready state,
        # but elapsed time is never proof that a deliverable exists.
        order.update(
            stage="交付",
            stage_started=now,
            worked_seconds=0,
            next_action="核对实际成品并提交；没有可打开的成果时保持未完成",
            delivery_ready=True,
        )
        order.pop("session", None)
    while order["stage"] in NEEDED and order["worked_seconds"] >= NEEDED[order["stage"]]:
        stage = order["stage"]
        order["worked_seconds"] -= NEEDED[stage]
        order["stage"] = STAGES[STAGES.index(stage) + 1]
        order["stage_started"] = now
        if order["stage"] == "修改":
            order["revisions"] = order.get("revisions", 0) + 1
        if order["stage"] == "成稿待确认":
            limit = confirmation_limit(now)
            order.update(
                confirmation_until=limit,
                delivery_target=limit + 3600,
                worked_seconds=0,
                wait_reason="成稿预览待确认",
            )
            order.pop("session", None)
        # No time-based transition may mark a work order complete. Completion
        # requires a verified artifact and a confirmed delivery record.
    after = json.dumps(order, ensure_ascii=False, sort_keys=True)
    return order, before != after


def advance(now=None):
    """Persist time already spent in the current accepted work session."""
    now = time.time() if now is None else float(now)
    with _LOCK:
        data = _load_unlocked()
        order, changed = _advance_unlocked(data, now)
        if changed:
            _save_unlocked(data)
        return order


def mark_draft_ready(order_id, evidence, reason, now=None):
    now = time.time() if now is None else float(now)
    with _LOCK:
        data = _load_unlocked()
        for order in data["orders"]:
            if order.get("id") == order_id and order.get("stage") != "完成":
                order.update(
                    stage="成稿待确认",
                    stage_started=now,
                    confirmation_until=confirmation_limit(now),
                    delivery_target=confirmation_limit(now) + 3600,
                    wait_reason=reason,
                    draft_evidence=evidence,
                    worked_seconds=0,
                    fictional=True,
                )
                order.pop("session", None)
                _save_unlocked(data)
                return order
    return None


def summary(now=None):
    """Pure projection: no order creation, stage advancement or file writes."""
    now = time.time() if now is None else float(now)
    with _LOCK:
        data = _load_unlocked()
        order = _current_order(data)
        if not order:
            return {
                "稿件": None,
                "进度": "工作空档",
                "下一步": "这一轮已交付，先正常生活，不立刻自动接单",
            }
        stage = order.get("stage", "接到需求")
        worked = float(order.get("worked_seconds", 0))
        session = order.get("session") or {}
        if session:
            end = min(now, float(session.get("end", now)))
            credited = float(session.get("credited_until", end))
            worked += max(0, end - credited)
        if stage == "成稿待确认" and now >= order.get("confirmation_until", float("inf")):
            stage = "交付"
            worked = 0
        deadline = order.get("deadline", now)
        next_action = order.get("next_action") if stage == "交付" else None
        if not next_action:
            next_action = {
                "接到需求": "理清需求并开始草图",
                "起稿": "继续这单具体草图",
                "修改": "修改并整理当前版本",
                "成稿待确认": "等待到确认期限；届时先交当前版本，不无限等标题",
                "交付": "核对实际成果并提交；没有可打开的成品时保持未完成",
                "完成": "本轮交付已结束，接下来留空档",
            }.get(stage, "核对当前工作")
        needed = NEEDED.get(stage, 0)
        return {
            "工单id": order["id"],
            "稿件": order["brief"],
            "进度": stage,
            "需安排工作时段": stage in NEEDED
            and now >= min(deadline - 6 * 3600, order.get("delivery_target", deadline))
            and not (session.get("end", 0) > now),
            "本阶段剩余分钟": max(1, math.ceil((needed - worked) / 60)) if stage in NEEDED else 0,
            "截止": datetime.fromtimestamp(deadline, TZ).strftime("%Y-%m-%d %H:%M"),
            "本轮收尾时间": datetime.fromtimestamp(
                order.get("delivery_target", deadline), TZ
            ).strftime("%Y-%m-%d %H:%M"),
            "已逾期": now > deadline and stage != "完成",
            "确认期限": datetime.fromtimestamp(order["confirmation_until"], TZ).strftime("%Y-%m-%d %H:%M")
            if stage == "成稿待确认" and order.get("confirmation_until")
            else None,
            "下一步": next_action,
            "等待原因": order.get("wait_reason") if stage == "成稿待确认" else None,
            "改过次数": order.get("revisions", 0),
            "成果": [
                {"id": item.get("id"), "kind": item.get("kind"), "status": item.get("status"),
                 "uri": item.get("uri"), "description": item.get("description", "")}
                for item in order.get("artifacts", [])[-6:]
            ],
            "虚构工作": True,
        }


def attach_artifact(order_id, *, artifact_id, kind, status, uri, description="", now=None):
    """Attach an actual saved artifact to the current work order.

    A generated-but-blocked image is retained as evidence of a failed attempt,
    but it can never satisfy the completion gate.
    """
    now = time.time() if now is None else float(now)
    if status not in ("generated", "verified", "blocked", "failed", "delivered"):
        raise ValueError("unsupported artifact status")
    artifact_id = str(artifact_id or "").strip()
    uri = str(uri or "").strip()
    if not artifact_id or not uri:
        raise ValueError("artifact id and saved URI are required")
    with _LOCK:
        data = _load_unlocked()
        order = next((row for row in data["orders"] if row.get("id") == order_id), None)
        if not order:
            return None
        artifacts = order.setdefault("artifacts", [])
        existing = next((row for row in artifacts if row.get("id") == artifact_id), None)
        value = {
            "id": artifact_id, "kind": str(kind), "status": status,
            "uri": uri, "description": str(description or "")[:500],
            "at": now, "updated_at": now,
        }
        if existing:
            existing.update(value)
        else:
            artifacts.append(value)
        order["artifacts"] = artifacts[-20:]
        order["updated_at"] = now
        _save_unlocked(data)
        return value


def mark_artifact_delivered(order_id, artifact_id, now=None):
    now = time.time() if now is None else float(now)
    with _LOCK:
        data = _load_unlocked()
        order = next((row for row in data["orders"] if row.get("id") == order_id), None)
        if not order:
            return None
        artifact = next((row for row in order.get("artifacts", []) if row.get("id") == artifact_id), None)
        if not artifact or artifact.get("status") not in ("verified", "delivered"):
            return None
        if artifact.get("status") == "delivered":
            return artifact
        from pathlib import Path
        if not Path(str(artifact.get("uri") or "")).is_file():
            return None
        artifact.update(status="delivered", delivered_at=now, updated_at=now)
        _save_unlocked(data)
        return artifact


def mark_completed(order_id, artifact_id, now=None):
    """Complete only after a verified artifact was actually delivered."""
    now = time.time() if now is None else float(now)
    with _LOCK:
        data = _load_unlocked()
        order = next((row for row in data["orders"] if row.get("id") == order_id), None)
        if not order or order.get("stage") == "完成":
            return order
        artifact = next((row for row in order.get("artifacts", []) if row.get("id") == artifact_id), None)
        if not artifact or artifact.get("status") != "delivered":
            return None
        from pathlib import Path
        if not Path(str(artifact.get("uri") or "")).is_file():
            return None
        order.update(
            stage="完成", stage_started=now, completed_at=now,
            next_order_after=now + random.randint(12, 30) * 3600,
            delivery_note="实际成果已生成、核验并成功交付；这只证明本插件内的成品交付，不代表虚构客户付款或外部验收。",
            completion_evidence={"artifact_id": artifact_id, "uri": artifact.get("uri"), "at": now},
        )
        order.pop("session", None)
        _save_unlocked(data)
        return order


def recover_delivered_artifacts():
    """Replay only domain records written after an actual transport confirmation."""
    recovered = []
    for order in load()["orders"]:
        if order.get("stage") == "完成":
            continue
        for artifact in order.get("artifacts", []):
            if artifact.get("status") != "delivered" or not artifact.get("id") or not artifact.get("delivered_at"):
                continue
            result = mark_completed(order["id"], artifact["id"], artifact["delivered_at"])
            if result:
                recovered.append(result)
                break
    return recovered


def note_activity(text, now=None, duration_minutes=0, work=None, start_at=None, activity_id=None):
    now = time.time() if now is None else float(now)
    with _LOCK:
        data = _load_unlocked()
        order, advanced = _advance_unlocked(data, now)
        if not order:
            if advanced:
                _save_unlocked(data)
            return
        # _advance_unlocked() has credited the previous work session up to
        # `now`.  Any newly accepted non-work activity must then close that
        # session, including an outing whose duration is represented as 0.
        # Returning before clearing it lets the old session keep accruing time
        # while the companion is eating, travelling, or doing something else.
        previous_session = order.pop("session", None)
        if not isinstance(duration_minutes, (int, float)) or duration_minutes <= 0:
            if advanced or previous_session is not None:
                _save_unlocked(data)
            return
        structured = (
            isinstance(work, dict)
            and work.get("order_id") == order["id"]
            and work.get("stage") == order.get("stage")
        )
        relevant = bool(
            re.search(r"赶稿|改稿|起稿|交稿|交付|排版|插画|封面|海报|工单|稿件", str(text))
        ) and not re.search(
            r"收好.{0,8}(?:游戏|玩)|放下.{0,8}(?:游戏|玩)|玩.{0,8}游戏|收整.*放松",
            str(text),
        )
        # Structured work metadata is authoritative when supplied.  A stale
        # order_id/stage must not fall back to a generic keyword in the reason.
        qualifies = structured if isinstance(work, dict) else relevant
        if qualifies and order["stage"] != "成稿待确认":
            begin = max(now, float(start_at or now))
            order["session"] = {
                "credited_until": begin,
                "end": begin + min(float(duration_minutes), 480) * 60,
                "activity_id": activity_id,
            }
        _save_unlocked(data)
