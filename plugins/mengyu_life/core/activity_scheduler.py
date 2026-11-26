"""Persistent priority scheduler for Mengyu's autonomous activities.

The scheduler is deliberately small and JSON-backed. It coordinates the main
activity selected by the life planner, explicit first-person commitments in
confirmed outgoing messages, and the existing fictional daylife timeline.
It does not execute external actions; it only orders and records fictional or
local activities.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from data.plugins.mengyu_contact.state_write import write_json

from data.plugins.mengyu_contact.local_clock import TZ

SCHEMA_VERSION = 1

ACTIVITY_SCHEDULER_GUIDANCE = """【自主活动统一优先级】所有自主活动必须经过统一队列，不再由每个模块各自抢当前时间。优先级从高到低：P0紧急/不可延误事项；P1吃饭、睡眠、洗漱和必要休息；P2已经开始的工作/主要任务；P3有明确计划的事务、家务和外出；P4游戏、阅读、音乐、绘画等兴趣；P5临时想到的小事。数字越小越优先。当前主要任务默认保持，不因突然想到另一件低优先级的事就切换；低优先级候选进入等待队列。更高优先级任务可以打断当前任务，但必须保存原任务进度并标记暂停，之后恢复，不得静默丢弃。吃饭/休息可以合理打断创作；普通兴趣不能打断已开始的工作或正在做的饭。只有一个主要任务，简短回复消息不抢占自主任务；未明确标记为兼容的其他活动都进入同一队列。用户聊天本身不抢占自主任务。已明确说出口的近期行动承诺会进入队列：说“我去煮啦”意味着优先推进煮饭，不得下一轮又无故回到画稿；说“等稿子收尾再煮”则把做饭排在当前稿件之后。计划、承诺、开始和完成是不同状态；没有真正开始不能说正在做，没有完成证据不能标记完成。"""

PRIORITIES = {
    0: "紧急/不可延误事项",
    1: "基本生活需求",
    2: "已经开始的工作或主要任务",
    3: "有计划的事务与家务",
    4: "个人兴趣活动",
    5: "临时兴趣与随手想法",
}

_ACTIONS = (
    ("meal", 1, r"去外面吃|出去吃|去附近.{0,12}吃|找家.{0,12}吃|去餐馆吃|去餐厅吃|煮(?:面|饭)?|做饭|下厨|热饭|做面|吃早饭|吃早餐|吃午饭|吃午餐|吃晚饭|吃晚餐|去吃饭|开始吃饭", "meal"),
    ("rest", 1, r"睡觉|午睡|休息一会|躺一会|洗澡|洗漱", "rest"),
    ("work", 2, r"收个尾|收尾|赶稿|画稿|改稿|写稿|交稿|画[^，。；]{0,8}贴纸|继续画|继续写|处理稿件", "work"),
    ("chore", 3, r"收拾房间|整理房间|打扫|洗衣|晾衣|整理桌面", "chore"),
    ("outing", 3, r"出门散步|去散步|去公园|去书店|去咖啡馆|去超市|出去逛逛", "outing"),
    ("game", 4, r"玩游戏|开一局|玩《[^》]{1,40}》|玩一会游戏", "game"),
    ("hobby", 4, r"看书|阅读|看剧|追剧|听歌|画画|练琴", "hobby"),
)


def _normalise(text: str) -> str:
    return re.sub(r"[\s，。！？、；：,.!?;:'\"“”‘’（）()【】\[\]_-]+", "", str(text or "")).lower()


def _local_day(now: float) -> str:
    return datetime.fromtimestamp(float(now), TZ).date().isoformat()


def _safe_plan(plan: dict) -> dict:
    allowed = {
        "kind", "mode", "destination", "reason", "detail", "title", "query",
        "url", "text", "format", "theme", "game_title", "game_genre", "work",
        "meal", "stay_minutes", "travel_minutes", "prep_minutes", "clothes", "itinerary",
        "change_plan", "due_at", "subject", "caption", "source_id", "note", "_commitment_category", "_meal_location",
        "_daily_anchor_id", "_daily_latest_start",
    }
    result = {}
    for key in allowed:
        value = plan.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            result[key] = value
        elif isinstance(value, dict) and key in ("work", "meal"):
            result[key] = value
        elif key == "itinerary" and isinstance(value, list):
            result[key] = [
                {k: v for k, v in row.items() if k in ("destination", "reason", "stay_minutes", "travel_minutes", "category") and isinstance(v, (str, int, float))}
                for row in value[:4] if isinstance(row, dict)
            ]
    return result


def classify_plan(plan: dict) -> int:
    """Return a lower-is-more-urgent priority for a proposed activity."""
    plan = plan if isinstance(plan, dict) else {}
    text = " ".join(str(plan.get(k) or "") for k in ("reason", "detail", "wish", "title", "query", "destination"))
    category = plan.get("_commitment_category")
    if category in ("meal", "rest"):
        return 1
    if category == "work":
        return 2
    if category in ("chore", "outing"):
        return 3
    if category in ("game", "hobby"):
        return 4
    if re.search(r"急诊|紧急就医|立即就医|马上复诊|必须按时复诊|预约即将截止", text):
        return 0
    if isinstance(plan.get("work"), dict) or re.search(r"稿件|赶稿|改稿|交稿|插画订单|客户稿|工作任务|项目交付|画三张.*贴纸", text):
        return 2
    if re.search(r"吃饭|午饭|晚饭|早餐|煮(?:面|饭)?|做饭|下厨|洗澡|洗漱|睡觉|午睡|休息", text):
        return 1
    if plan.get("due_at") or re.search(r"预约|约定|截止|取快递|寄快递|买生活用品|打扫|收拾房间|洗衣|办事", text):
        return 3
    if plan.get("kind") in ("games", "music", "drawing", "reading", "study", "project", "diary", "search", "web"):
        return 4 if plan.get("kind") != "diary" else 5
    if re.search(r"游戏|看剧|电影|听歌|画画|阅读|咖啡馆|散步|逛街|探索", text):
        return 4
    return 5


def _task_key(plan: dict, now: float) -> str:
    work = plan.get("work") if isinstance(plan.get("work"), dict) else {}
    if plan.get("_daily_anchor_id"):
        raw = "daily:" + str(plan["_daily_anchor_id"])
    elif plan.get("_commitment_category") == "meal" or (
        plan.get("kind") == "daylife"
        and re.search(r"吃饭|午饭|晚饭|早餐|煮(?:面|饭)?|做饭|下厨|热饭", " ".join(str(plan.get(k) or "") for k in ("reason", "detail")))
    ):
        raw = f"daylife:meal:{_local_day(now)}"
    elif plan.get("_commitment_category"):
        category = str(plan.get("_commitment_category"))
        detail = _normalise(str(plan.get("reason") or category))
        raw = f"commitment:{category}:{detail}:{_local_day(now)}"
    elif work.get("order_id"):
        raw = f"work:{work.get('order_id')}:{work.get('stage', '')}"
    else:
        kind = str(plan.get("kind") or "daylife")
        title = plan.get("reason") or plan.get("title") or plan.get("query") or plan.get("wish") or kind
        raw = f"{kind}:{_normalise(str(title))}:{_local_day(now)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def _commitment(text: str):
    """Recognise only clear first-person near-term commitments, not wishes."""
    value = str(text or "").strip()
    if not value or len(value) > 1200 or re.search(r"[？?]", value):
        return None
    # A deferred action waits for the current task to finish.
    deferred = re.search(
        r"等(?:我)?(?P<dependency>[^，。；;]{1,35}?)(?:收个尾|收尾|做完|画完|写完|忙完|弄完|结束)"
        r"[^，。；;]{0,12}(?:就|再)(?:去|开始|来)?(?P<action>煮面|煮饭|做饭|下厨|热饭|做面|吃饭|吃午饭|吃晚饭)",
        value,
    )
    if deferred:
        action = deferred.group("action")
        return {"action": action, "category": "meal", "priority": 1, "deferred": True,
                "dependency_label": deferred.group("dependency").strip(), "dependency_kind": "work", "outside": False, "text": value[:240]}

    # A completed walk or shopping stop can lead directly into an outdoor meal.
    outing_then_meal = re.search(
        r"(?P<dependency>走完|散步结束|逛完(?:街|商场)?|逛一圈|看完电影|活动结束)"
        r"[^，。；;]{0,24}?(?:就|再)(?P<action>(?:去|到|找家|找个|在)?[^，。；;]{0,24}(?:找家热乎的吃|吃点热乎的|吃饭|吃晚饭|吃午饭|吃))",
        value,
    )
    if outing_then_meal:
        dependency = outing_then_meal.group("dependency")
        outside = bool(re.search(r"外面|外头|附近|餐馆|餐厅|找家|找个|不想回去开火|出去", value))
        return {"action": outing_then_meal.group("action").strip(), "category": "meal", "priority": 1,
                "deferred": True, "dependency_label": dependency, "dependency_kind": "shopping" if "逛" in dependency else "walk",
                "outside": outside, "text": value[:240]}

    # Immediate first-person action. Do not turn "想去/打算去/如果去" into a commitment.
    if re.search(r"(?:想|打算|准备|可能|如果|要不要|哪天).{0,8}(?:去|开始)", value):
        return None
    for category, priority, pattern, plan_kind in _ACTIONS:
        match = re.search(pattern, value)
        if not match:
            continue
        around = value[max(0, match.start() - 30):match.end() + 8]
        if not re.search(r"我.{0,12}(?:去|开始|先|这就|现在)|(?:那|好|行).{0,5}我.{0,12}(?:去|开始|先)", around):
            continue
        if re.search(r"(?:没去|不去|还没|尚未|没有去|没开始|不是要去)", around):
            continue
        if re.search(r"(?:你说|他说|她说|比如|例如|原话|如果|假如|要是|“|‘)", around):
            continue
        outside = category == "meal" and bool(re.search(r"外面|外头|附近|餐馆|餐厅|找家|出去", value))
        return {"action": match.group(0), "category": category, "priority": priority,
                "deferred": False, "dependency_label": "", "dependency_kind": "", "outside": outside, "text": value[:240]}
    return None


def _dependency_matches_current(parsed, current):
    if not parsed or not current:
        return False
    plan = current.get("plan") if isinstance(current.get("plan"), dict) else {}
    text = " ".join(str(value or "") for value in (current.get("label"), plan.get("reason"), plan.get("detail"), plan.get("destination")))
    kind = parsed.get("dependency_kind")
    if kind == "walk":
        return bool(re.search(r"散步|遛弯|走走|步行|河边|江边|公园|步道|湖边", text))
    if kind == "shopping":
        return bool(re.search(r"逛街|逛商场|商场|购物|服装店|饰品店|商业街", text))
    if kind == "work":
        return bool(re.search(r"稿|工作|项目|交付|贴纸|插画|写作|收尾", text))
    return False


class ActivityScheduler:
    """JSON-backed single-main-task scheduler with bounded preemption."""

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "activity_schedule.json"
        self._lock = threading.RLock()
        self.data = self._load()

    def _empty(self):
        return {"schema_version": SCHEMA_VERSION, "tasks": [], "active_task_id": None,
                "processed_deliveries": [], "updated_at": time.time()}

    def _load(self):
        if not self.path.exists():
            return self._empty()
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            backup = self.path.with_name(f"{self.path.name}.corrupt-{int(time.time())}")
            try:
                os.replace(self.path, backup)
            except OSError as move_exc:
                raise RuntimeError(f"activity_schedule.json unreadable; quarantine failed: {type(move_exc).__name__}") from exc
            return self._empty()
        if not isinstance(value, dict) or not isinstance(value.get("tasks"), list):
            backup = self.path.with_name(f"{self.path.name}.invalid-{int(time.time())}")
            try:
                os.replace(self.path, backup)
            except OSError as exc:
                raise RuntimeError(f"activity_schedule.json structure invalid; quarantine failed: {type(exc).__name__}") from exc
            return self._empty()
        value.setdefault("schema_version", SCHEMA_VERSION)
        value.setdefault("active_task_id", None)
        value.setdefault("processed_deliveries", [])
        value["tasks"] = [x for x in value["tasks"] if isinstance(x, dict)]
        return value

    def _save(self):
        self.data["schema_version"] = SCHEMA_VERSION
        self.data["updated_at"] = time.time()
        write_json(self.path, self.data, "ActivityScheduler")

    def _task(self, task_id):
        return next((x for x in self.data["tasks"] if x.get("id") == task_id), None)

    def active(self):
        task = self._task(self.data.get("active_task_id"))
        return task if task and task.get("status") in ("active", "committed") else None

    def _open_by_key(self, key):
        return next((x for x in reversed(self.data["tasks"])
                     if x.get("task_key") == key and x.get("status") in ("queued", "active", "committed", "paused")), None)

    def _new_task(self, plan, now, *, source="planner", status="queued", dependency=None, dependency_label=""):
        safe = _safe_plan(plan)
        key = _task_key(safe, now)
        existing = self._open_by_key(key)
        if existing:
            # Keep a committed plan stable; enrich only missing descriptive fields.
            if existing.get("status") == "queued":
                existing["plan"] = {**safe, **{k: v for k, v in existing.get("plan", {}).items() if k not in safe}}
                existing["updated_at"] = now
            return existing, False
        task = {
            "id": f"task-{int(now * 1000)}-{hashlib.sha1((key + str(len(self.data['tasks']))).encode()).hexdigest()[:8]}",
            "task_key": key,
            "label": str(safe.get("reason") or safe.get("title") or safe.get("query") or safe.get("kind") or "自主活动")[:100],
            "kind": str(safe.get("kind") or "daylife"),
            "category": str(safe.get("_commitment_category") or ""),
            "priority": classify_plan(safe),
            "status": status,
            "source": source,
            "plan": safe,
            "created_at": now,
            "updated_at": now,
            "due_at": safe.get("due_at") if isinstance(safe.get("due_at"), (int, float)) else now,
            "depends_on": dependency,
            "dependency_label": str(dependency_label or "")[:80],
            "activity_id": None,
            "end_at": None,
            "remaining_minutes": None,
            "commitment": source == "delivered_commitment",
            "attempts": 0,
        }
        self.data["tasks"].append(task)
        self.data["tasks"] = self.data["tasks"][-120:]
        return task, True

    def _dependency_ready(self, task):
        dependency = task.get("depends_on")
        if dependency:
            parent = self._task(dependency)
            if parent and parent.get("status") not in ("completed", "cancelled", "failed"):
                return False
        label = _normalise(task.get("dependency_label", ""))
        if label:
            parents = [x for x in self.data["tasks"] if label and label in _normalise(x.get("label", ""))]
            if parents and any(x.get("status") not in ("completed", "cancelled", "failed") for x in parents):
                return False
            if not parents:
                return False
        return True

    def _ready_queue(self):
        return [x for x in self.data["tasks"] if x.get("status") in ("queued", "paused") and self._dependency_ready(x)]

    def _best_ready(self):
        rows = self._ready_queue()
        return min(rows, key=lambda x: (int(x.get("priority", 5)), float(x.get("due_at") or float("inf")), x.get("created_at", 0))) if rows else None

    def _pause_task_record(self, task, now, reason="higher_priority"):
        if not task:
            return None
        task["status"] = "paused"
        task["pause_reason"] = reason
        task["updated_at"] = now
        if task.get("end_at"):
            task["remaining_minutes"] = max(1, int((task["end_at"] - now + 59) // 60))
        activity_id = task.get("activity_id")
        if activity_id:
            # Keep the old segment identity after clearing activity_id. The
            # segment is closed by main.py immediately after scheduler admission.
            task["paused_activity_id"] = activity_id
            task["paused_at"] = now
        task["activity_id"] = None
        task["end_at"] = None
        return activity_id

    def _pause_active(self, now, reason="higher_priority"):
        current = self.active()
        if not current:
            return None
        activity_id = self._pause_task_record(current, now, reason)
        self.data["active_task_id"] = None
        return activity_id

    def _activate(self, task, now):
        if not task:
            self.data["active_task_id"] = None
            return
        task["status"] = "committed" if task.get("commitment") and not task.get("activity_id") else "active"
        task["updated_at"] = now
        self.data["active_task_id"] = task["id"]

    def reconcile_life(self, segments, now=None):
        """Import an existing in-progress daylife and settle elapsed segments."""
        now = time.time() if now is None else float(now)
        changed = False
        with self._lock:
            active_segments = []
            latest_by_task = {}
            for segment in segments or ():
                if isinstance(segment, dict) and segment.get("scheduler_task_id"):
                    task_id = segment["scheduler_task_id"]
                    previous = latest_by_task.get(task_id) or {}
                    if (segment.get("created_at") or 0) >= (previous.get("created_at") or 0):
                        latest_by_task[task_id] = segment

            def linked(task, segment):
                task_id = task.get("id")
                return task.get("activity_id") == segment.get("id") or (
                    not task.get("activity_id") and task_id is not None
                    and latest_by_task.get(task_id) is segment
                )

            for segment in segments or []:
                if not isinstance(segment, dict):
                    continue
                if segment.get("cancelled_at") is not None:
                    for task in self.data["tasks"]:
                        matches_segment = (
                            linked(task, segment) or task.get("paused_activity_id") == segment.get("id")
                        )
                        if not matches_segment or task.get("status") in ("completed", "cancelled"):
                            continue
                        was_scheduler_pause = bool(segment.get("paused_by_scheduler"))
                        if was_scheduler_pause or task.get("status") in ("active", "committed"):
                            if task.get("status") != "paused":
                                task["status"] = "paused"
                                task["pause_reason"] = "timeline_cancelled"
                                task["updated_at"] = now
                                changed = True
                            if task.get("end_at"):
                                task["remaining_minutes"] = max(
                                    1,
                                    int((task["end_at"] - segment.get("cancelled_at", now) + 59) // 60),
                                )
                                changed = True
                            previous_progress = task.get("activity_progress")
                            if not isinstance(previous_progress, list):
                                previous_progress = []
                            known_progress = {
                                str(item.get("progress_id"))
                                for item in previous_progress
                                if isinstance(item, dict) and item.get("progress_id")
                            }
                            additions = [
                                item for item in (segment.get("progress") or [])
                                if isinstance(item, dict)
                                and item.get("progress_id")
                                and str(item.get("progress_id")) not in known_progress
                            ]
                            if additions:
                                task["activity_progress"] = (previous_progress + additions)[-80:]
                                changed = True
                            task["paused_activity_id"] = segment.get("id")
                            task["activity_id"] = None
                            task["end_at"] = None
                            if self.data.get("active_task_id") == task.get("id"):
                                self.data["active_task_id"] = None
                                changed = True
                    continue
                phases = segment.get("phases") or []
                if not phases:
                    continue
                if segment.get("created_at", phases[0].get("at", now)) <= now < phases[-1].get("at", now):
                    active_segments.append(segment)
                elif phases[-1].get("at", now) <= now:
                    for task in self.data["tasks"]:
                        if linked(task, segment) and task.get("status") in ("active", "committed", "queued"):
                            previous_progress = task.get("activity_progress")
                            if not isinstance(previous_progress, list):
                                previous_progress = []
                            known_progress = {
                                str(item.get("progress_id"))
                                for item in previous_progress
                                if isinstance(item, dict) and item.get("progress_id")
                            }
                            additions = [
                                item for item in (segment.get("progress") or [])
                                if isinstance(item, dict)
                                and item.get("progress_id")
                                and str(item.get("progress_id")) not in known_progress
                            ]
                            if additions:
                                task["activity_progress"] = (previous_progress + additions)[-80:]
                            task.update(activity_id=segment.get("id"), status="completed",
                                        completed_at=phases[-1].get("at", now), updated_at=now)
                            if self.data.get("active_task_id") == task.get("id"):
                                self.data["active_task_id"] = None
                            changed = True
            for task in self.data["tasks"]:
                plan = task.get("plan") or {}
                if not plan.get("_daily_anchor_id") or task.get("activity_id") or task.get("id") in latest_by_task:
                    continue
                if task.get("status") not in ("queued", "active"):
                    continue
                window_end = plan.get("_daily_latest_start")
                stalled = task.get("status") == "active" and now - task.get("updated_at", now) > 120
                if stalled or isinstance(window_end, (int, float)) and now >= window_end:
                    task.update(status="cancelled", updated_at=now, cancel_reason="unstarted_daily_slot")
                    if self.data.get("active_task_id") == task.get("id"):
                        self.data["active_task_id"] = None
                    changed = True
            active_segment = max(active_segments, key=lambda x: x.get("created_at", 0)) if active_segments else None
            if active_segment:
                task_id = active_segment.get("scheduler_task_id")
                task = self._task(task_id) if task_id else None
                if not task:
                    existing = next((x for x in self.data["tasks"] if x.get("activity_id") == active_segment.get("id")), None)
                    task = existing
                if not task:
                    reason = str(active_segment.get("reason") or active_segment.get("detail") or "继续当前日常")
                    plan = {
                        "kind": "daylife", "mode": active_segment.get("mode", "home"),
                        "destination": active_segment.get("destination", "住处"),
                        "reason": reason, "detail": active_segment.get("detail", ""),
                        "stay_minutes": max(1, int((active_segment["phases"][-1]["at"] - now) / 60)),
                        "prep_minutes": int(active_segment.get("prep_minutes", 0) or 0),
                        "travel_minutes": int(active_segment.get("travel_minutes", 0) or 0),
                        "game_title": active_segment.get("game_title", ""),
                        "game_genre": active_segment.get("game_genre", ""),
                        "clothes": active_segment.get("clothes", "日常便装"),
                        "meal": active_segment.get("meal"),
                        "work": active_segment.get("work") if isinstance(active_segment.get("work"), dict) else None,
                    }
                    task, _ = self._new_task(plan, active_segment.get("created_at", now), source="legacy_timeline", status="active")
                    task["activity_id"] = active_segment.get("id")
                    task["end_at"] = active_segment["phases"][-1].get("at")
                    task["priority"] = classify_plan({**plan, "work": active_segment.get("work")})
                    changed = True
                if task:
                    previous_progress = task.get("activity_progress")
                    if not isinstance(previous_progress, list):
                        previous_progress = []
                    known_progress = {
                        str(item.get("progress_id"))
                        for item in previous_progress
                        if isinstance(item, dict) and item.get("progress_id")
                    }
                    additions = [
                        item for item in (active_segment.get("progress") or [])
                        if isinstance(item, dict)
                        and item.get("progress_id")
                        and str(item.get("progress_id")) not in known_progress
                    ]
                    if additions:
                        task["activity_progress"] = (previous_progress + additions)[-80:]
                        changed = True
                current = self.active()
                if current and current.get("id") != task.get("id"):
                    # A delivered commitment is authoritative until it starts or is explicitly withdrawn.
                    if current.get("status") == "committed" and current.get("priority", 5) <= task.get("priority", 5):
                        self._pause_task_record(task, now, "higher_priority_commitment")
                        changed = True
                    elif task.get("priority", 5) < current.get("priority", 5):
                        self._pause_active(now, "timeline_reconciliation")
                        self._activate(task, now)
                        changed = True
                    else:
                        self._pause_task_record(task, now, "higher_priority_active_task")
                        changed = True
                elif not current:
                    self._activate(task, now)
                    changed = True
                else:
                    if task.get("status") != "active":
                        task["status"] = "active"
                        changed = True
                    self.data["active_task_id"] = task["id"]
                if task.get("status") in ("active", "committed"):
                    if task.get("activity_id") != active_segment.get("id"):
                        task["activity_id"] = active_segment.get("id")
                        task["end_at"] = active_segment["phases"][-1].get("at")
                        changed = True
                    if task.get("status") != "active":
                        task["status"] = "active"
                        changed = True
                    self.data["active_task_id"] = task["id"]
            # A paused segment remains resumable; cancelled segments are not treated as completed.
            if changed:
                self._save()
        return changed

    def context(self, now=None):
        now = time.time() if now is None else float(now)
        with self._lock:
            active = self.active()
            queue = sorted(
                [x for x in self.data["tasks"] if x.get("status") in ("queued", "paused")],
                key=lambda x: (x.get("priority", 5), x.get("due_at") or float("inf"), x.get("created_at", 0)),
            )[:8]
            def view(row):
                if not row:
                    return None
                progress = row.get("activity_progress")
                if not isinstance(progress, list):
                    progress = []
                return {"id": row.get("id"), "label": row.get("label"), "priority": row.get("priority"),
                        "priority_name": PRIORITIES.get(row.get("priority"), "其他"), "status": row.get("status"),
                        "scheduler_status": row.get("status"),
                        "status_meaning": "这是队列调度状态；active表示当前占用主要任务位，不代表虚构日常已经完成。活动实际状态见生活时间线activity_status。",
                        "recent_progress": [
                            {
                                "stage": str(item.get("stage") or "")[:60],
                                "detail": str(item.get("detail") or "")[:160],
                                "next_step": str(item.get("next_step") or "")[:100],
                                "place": str(item.get("place") or "")[:80],
                            }
                            for item in progress[-4:] if isinstance(item, dict)
                        ],
                        "source": row.get("source"), "remaining_minutes": row.get("remaining_minutes"),
                        "due_at": row.get("due_at"), "depends_on": row.get("depends_on"),
                        "dependency_label": row.get("dependency_label"),
                        "ready": self._dependency_ready(row)}
            return {"当前主要任务": view(active), "等待队列": [view(x) for x in queue],
                    "规则": "同一时刻只有一个主要任务；高优先级可打断并保存进度；低优先级进入队列；兼容的小事可并行；聊天本身不抢占任务。scheduler_status只表示排队/调度状态，不等于活动本身的planned/in_progress/completed。"}

    def _plan_for(self, task):
        plan = dict(task.get("plan") or {})
        if task.get("remaining_minutes") and plan.get("kind") == "daylife":
            plan["stay_minutes"] = max(1, min(240, int(task["remaining_minutes"])))
        plan["_scheduler_task_id"] = task.get("id")
        progress = task.get("activity_progress")
        if task.get("paused_activity_id") or isinstance(progress, list):
            if isinstance(progress, list):
                plan["_resume_progress"] = [
                    item for item in progress if isinstance(item, dict)
                ][-80:]
            plan["_resume_from_activity_id"] = str(task.get("paused_activity_id") or "")
            plan["_activity_chain_id"] = str(task.get("id") or "")
        if task.get("preempted_activity_id"):
            plan["_preempted_activity_id"] = str(task["preempted_activity_id"])
        if plan.get("kind") == "daylife":
            plan["change_plan"] = True
        return plan

    def admit_plan(self, plan, segments=None, now=None):
        """Queue or admit one planner proposal; return the authoritative plan."""
        now = time.time() if now is None else float(now)
        plan = dict(plan or {})
        with self._lock:
            self.reconcile_life(segments or [], now)
            forced = self.active()
            if plan.get("_daily_anchor_id") and (forced or self._best_ready()):
                return {"allowed": False, "plan": None, "task_id": None,
                        "reason": "先处理已有安排"}
            if forced and forced.get("status") == "committed" and not forced.get("activity_id"):
                return {"allowed": True, "plan": self._plan_for(forced), "task_id": forced["id"],
                        "preempted_activity_id": None, "forced": True}
            if plan.get("kind") in ("continue", "image"):
                if plan.get("kind") == "continue" and not self.active():
                    queued = self._best_ready()
                    if queued:
                        self._activate(queued, now)
                        self._save()
                        return {"allowed": True, "plan": self._plan_for(queued), "task_id": queued["id"],
                                "preempted_activity_id": None, "forced": True}
                return {"allowed": True, "plan": plan, "task_id": None, "preempted_activity_id": None}
            candidate, _ = self._new_task(plan, now)
            if not self._dependency_ready(candidate):
                self._save()
                return {"allowed": False, "plan": None, "task_id": candidate["id"],
                        "preempted_activity_id": None, "reason": "候选活动依赖的当前任务尚未完成"}
            current = self.active()
            if current and current.get("id") == candidate.get("id"):
                self._save()
                return {"allowed": True, "plan": plan, "task_id": candidate["id"], "preempted_activity_id": None}
            if current:
                candidate_priority = int(candidate.get("priority", 5))
                current_priority = int(current.get("priority", 5))
                voluntary_switch = bool(plan.get("change_plan") is True and current_priority >= 3 and candidate_priority == current_priority)
                if candidate_priority < current_priority or voluntary_switch:
                    preempted = self._pause_active(now, "higher_priority_task" if candidate_priority < current_priority else "voluntary_switch")
                    self._activate(candidate, now)
                    selected = candidate
                    if selected.get("plan", {}).get("kind") == "daylife":
                        selected["plan"]["change_plan"] = True
                else:
                    self._save()
                    return {"allowed": False, "plan": None, "task_id": candidate["id"], "preempted_activity_id": None,
                            "reason": "当前任务优先级更高，候选活动已排队"}
            else:
                preempted = None
                selected = self._best_ready() or candidate
                self._activate(selected, now)
            self._save()
            needs_resume = bool(
                selected.get("paused_activity_id")
                or selected.get("activity_progress")
                or selected.get("remaining_minutes")
            )
            return {"allowed": True, "plan": self._plan_for(selected) if selected.get("id") != candidate.get("id") or needs_resume else plan,
                    "task_id": selected["id"], "preempted_activity_id": preempted,
                    "forced": selected.get("id") != candidate.get("id")}

    def _append_outdoor_meal_to_current(self, current, segments, now):
        """Append a promised restaurant stop before the current outing returns home.

        Returns a result dict when the current trip already contains a meal or
        was successfully extended; returns None when a safe in-place extension
        is not possible and a separate queued task should be used instead.
        """
        if not current or not isinstance(current.get("plan"), dict):
            return None
        current_plan = current["plan"]
        if current_plan.get("mode") != "out":
            return None
        activity_id = str(current.get("activity_id") or "")
        segment = next(
            (row for row in (segments or []) if isinstance(row, dict) and str(row.get("id") or "") == activity_id),
            None,
        )
        if not segment or segment.get("mode") != "out" or segment.get("cancelled_at") is not None:
            return None

        phases = segment.get("phases")
        if not isinstance(phases, list) or len(phases) < 3:
            return None
        itinerary = segment.get("itinerary") if isinstance(segment.get("itinerary"), list) else []
        if not itinerary:
            itinerary = current_plan.get("itinerary") if isinstance(current_plan.get("itinerary"), list) else []
        if any(
            isinstance(stop, dict)
            and (stop.get("category") == "meal" or re.search(r"餐馆|餐厅|外面吃|吃(?:午饭|晚饭|饭)", str(stop.get("reason") or "")))
            for stop in itinerary
        ):
            return {"merged": True, "already_planned": True, "timeline_updated": False, "segment_id": activity_id}

        return_indices = [
            index for index, phase in enumerate(phases)
            if isinstance(phase, dict) and phase.get("stage") == "回来的路上"
            and isinstance(phase.get("at"), (int, float)) and phase["at"] > now
        ]
        if not return_indices:
            return None
        return_index = return_indices[-1]
        home_index = next(
            (index for index in range(return_index + 1, len(phases))
             if isinstance(phases[index], dict) and phases[index].get("stage") == "回来了"
             and isinstance(phases[index].get("at"), (int, float))),
            None,
        )
        if home_index is None:
            return None
        if len(itinerary) >= 4:
            return None

        return_at = float(phases[return_index]["at"])
        old_home_at = float(phases[home_index]["at"])
        return_minutes = max(5, min(60, int(round((old_home_at - return_at) / 60))))
        restaurant_travel = 10
        restaurant_stay = 60
        restaurant_arrival = return_at + restaurant_travel * 60
        restaurant_departure = restaurant_arrival + restaurant_stay * 60
        new_home_at = restaurant_departure + return_minutes * 60

        # Preserve the planned end of the current walk/shop stop, but redirect
        # the return leg to a nearby restaurant instead of the user's home.
        phases[return_index] = {
            "at": return_at, "stage": "前往下一站",
            "place": "去附近餐馆的路上", "next_place": "附近餐馆",
            "reason": "当前外出结束后按已承诺安排在外面吃饭",
        }
        phases[home_index] = {
            "at": restaurant_arrival, "stage": "到了", "place": "附近餐馆",
            "category": "meal", "reason": "在外面吃一顿热乎的饭",
            "stop_index": len(itinerary),
        }
        phases.insert(home_index + 1, {
            "at": restaurant_departure, "stage": "回来的路上",
            "place": "从附近餐馆回住处的路上",
        })
        phases.insert(home_index + 2, {
            "at": new_home_at, "stage": "回来了", "place": "住处",
        })

        if not itinerary:
            first_destination = str(segment.get("destination") or current_plan.get("destination") or "当前外出地点")
            itinerary = [{
                "destination": first_destination[:80],
                "reason": str(segment.get("reason") or current_plan.get("reason") or "当前外出活动")[:200],
                "category": str(segment.get("activity_profile") or "outing"),
                "travel_minutes": max(5, min(60, int(segment.get("travel_minutes", 10) or 10))),
                "stay_minutes": max(10, min(180, int(segment.get("planned_duration_minutes", 40) or 40))),
            }]
        meal_stop = {
            "destination": "附近餐馆",
            "reason": "散步或逛街结束后在外面吃饭，不先回住处开火",
            "category": "meal", "travel_minutes": restaurant_travel,
            "stay_minutes": restaurant_stay,
        }
        itinerary = [dict(stop) for stop in itinerary if isinstance(stop, dict)] + [meal_stop]
        segment["itinerary"] = itinerary[:4]
        segment["planned_duration_minutes"] = int(segment.get("planned_duration_minutes", 0) or 0) + restaurant_stay
        old_total_travel = int(segment.get("travel_minutes", 0) or 0)
        segment["travel_minutes"] = max(0, old_total_travel - return_minutes) + restaurant_travel + return_minutes
        segment["reason"] = (str(segment.get("reason") or "外出") + "；结束后去附近餐馆吃饭")[:200]
        segment["detail"] = (str(segment.get("detail") or "") + "；按已经说好的安排，外出结束后直接去附近餐馆，不先回家")[:300]
        segment["meal_commitment"] = {"status": "scheduled", "destination": "附近餐馆", "source": "delivered_commitment", "at": now}

        current_plan["itinerary"] = [dict(stop) for stop in segment["itinerary"]]
        current_plan["_meal_location"] = "out"
        current_plan["stay_minutes"] = int(current_plan.get("stay_minutes", 0) or 0) + restaurant_stay
        current_plan["travel_minutes"] = max(0, int(current_plan.get("travel_minutes", 0) or 0) - return_minutes) + restaurant_travel + return_minutes
        current_plan["detail"] = (str(current_plan.get("detail") or "") + "；外出结束后直接去附近餐馆吃饭")[:300]
        current["end_at"] = new_home_at
        current["updated_at"] = now
        current["outside_meal_appended"] = True
        current["label"] = (str(current.get("label") or "外出") + "，结束后外食")[:100]
        self._save()
        return {"merged": True, "already_planned": False, "timeline_updated": True, "segment_id": activity_id}

    def capture_user_meal_preference(self, location, segments=None, source_id="", now=None):
        """Schedule an explicit user instruction about where the next meal is eaten."""
        now = time.time() if now is None else float(now)
        location = "out" if location == "out" else "home"
        source_id = str(source_id or f"meal-location:{int(now * 1000)}")
        with self._lock:
            if source_id in self.data.setdefault("processed_deliveries", []):
                return None
            self.reconcile_life(segments or [], now)
            current = self.active()
            if location == "out" and current and (current.get("plan") or {}).get("mode") == "out":
                merged = self._append_outdoor_meal_to_current(current, segments or [], now)
                if merged:
                    self.data["processed_deliveries"].append(source_id)
                    self.data["processed_deliveries"] = self.data["processed_deliveries"][-300:]
                    self._save()
                    return {"task_id": current.get("id"), "created": False, "deferred": False,
                            "merged_into_current": True, "timeline_updated": merged["timeline_updated"],
                            "already_planned": merged["already_planned"], "label": current.get("label")}
            defer = bool(
                location == "out" and current
                and (current.get("plan") or {}).get("mode") == "out"
                and current.get("category") != "meal"
            )
            if location == "out":
                plan = {"kind": "daylife", "mode": "out", "destination": "附近餐馆",
                        "reason": "按用户明确要求去附近餐馆吃饭",
                        "detail": "用户明确选择外食；保留外食地点，不改成回家做饭。",
                        "prep_minutes": 5, "travel_minutes": 10, "stay_minutes": 60,
                        "_commitment_category": "meal", "_meal_location": "out"}
            else:
                plan = {"kind": "daylife", "mode": "home", "destination": "住处厨房",
                        "reason": "按用户明确要求在家吃饭",
                        "detail": "用户明确选择在家用餐。",
                        "prep_minutes": 0, "stay_minutes": 45,
                        "_commitment_category": "meal", "_meal_location": "home"}
            dependency = current.get("id") if defer else None
            task, created = self._new_task(
                plan, now, source="user_meal_preference", status="queued",
                dependency=dependency,
            )
            task["commitment"] = True
            task["commitment_text"] = "用户明确指定下一顿用餐地点：" + location
            if task.get("status") in ("queued", "committed") and not task.get("activity_id"):
                task["plan"] = {**(task.get("plan") or {}), **_safe_plan(plan)}
                task["label"] = str(plan.get("reason") or task.get("label") or "用餐")[:100]
            task["depends_on"] = dependency
            task["dependency_label"] = ""
            task["updated_at"] = now
            preempted = None
            if not defer:
                current = self.active()
                if not current:
                    self._activate(task, now)
                elif task.get("id") != current.get("id") and task.get("priority", 5) < current.get("priority", 5):
                    preempted = self._pause_active(now, "user_meal_location_preference")
                    self._activate(task, now)
            if preempted:
                task["preempted_activity_id"] = str(preempted)
            self.data["processed_deliveries"].append(source_id)
            self.data["processed_deliveries"] = self.data["processed_deliveries"][-300:]
            self._save()
            return {"task_id": task["id"], "created": created, "deferred": defer,
                    "preempted_activity_id": preempted, "label": task.get("label")}

    def mark_started(self, task_id, activity_id=None, end_at=None, now=None):
        now = time.time() if now is None else float(now)
        with self._lock:
            task = self._task(task_id)
            if not task:
                return False
            task["status"] = "active"
            task["activity_id"] = activity_id
            task["end_at"] = end_at
            task["started_at"] = task.get("started_at", now)
            task.pop("paused_activity_id", None)
            task.pop("preempted_activity_id", None)
            task["updated_at"] = now
            self.data["active_task_id"] = task_id
            self._save()
            return True

    def mark_completed(self, task_id, now=None):
        now = time.time() if now is None else float(now)
        with self._lock:
            task = self._task(task_id)
            if not task or task.get("status") == "completed":
                return False
            task.update(status="completed", completed_at=now, updated_at=now, activity_id=None, end_at=None)
            if self.data.get("active_task_id") == task_id:
                self.data["active_task_id"] = None
            self._save()
            return True

    def mark_failed(self, task_id, error="", now=None):
        now = time.time() if now is None else float(now)
        with self._lock:
            task = self._task(task_id)
            if not task:
                return False
            status = "cancelled" if (task.get("plan") or {}).get("_daily_anchor_id") else "queued"
            task.update(status=status, last_error=str(error)[:120], attempts=int(task.get("attempts", 0)) + 1, updated_at=now)
            if self.data.get("active_task_id") == task_id:
                self.data["active_task_id"] = None
            self._save()
            return True

    def capture_commitment(self, text, delivery_id, segments=None, now=None):
        """Persist an explicit first-person promise only after confirmed delivery."""
        now = time.time() if now is None else float(now)
        delivery_id = str(delivery_id or "")
        parsed = _commitment(text)
        if not delivery_id or not parsed:
            return None
        with self._lock:
            processed = self.data.setdefault("processed_deliveries", [])
            if delivery_id in processed:
                return None
            self.reconcile_life(segments or [], now)
            current = self.active()
            category = parsed["category"]
            action = parsed["action"]
            if (category == "meal" and parsed.get("outside") and parsed.get("deferred")
                    and current and _dependency_matches_current(parsed, current)):
                merged = self._append_outdoor_meal_to_current(current, segments or [], now)
                if merged:
                    processed.append(delivery_id)
                    self.data["processed_deliveries"] = processed[-300:]
                    self._save()
                    return {"task_id": current.get("id"), "created": False, "preempted_activity_id": None,
                            "deferred": True, "merged_into_current": True,
                            "timeline_updated": merged["timeline_updated"],
                            "already_planned": merged["already_planned"], "label": current.get("label")}
            if category == "meal":
                if parsed.get("outside"):
                    plan = {"kind": "daylife", "mode": "out", "destination": "附近餐馆",
                            "reason": "去附近餐馆吃一顿热乎的饭",
                            "detail": "沿用已经说好的外食安排；不要改成回家开火。到店前不算已经吃到。",
                            "prep_minutes": 0, "travel_minutes": 10, "stay_minutes": 60,
                            "_meal_location": "out"}
                else:
                    plan = {"kind": "daylife", "mode": "home", "destination": "厨房",
                            "reason": "煮面" if "面" in action else "做饭",
                            "detail": "按当前生活安排准备并完成用餐", "prep_minutes": 0, "stay_minutes": 35,
                            "_meal_location": "home"}
            elif category == "rest":
                plan = {"kind": "daylife", "mode": "home", "destination": "住处",
                        "reason": action, "detail": "按自己的节奏完成这段生活安排", "prep_minutes": 0, "stay_minutes": 30}
            elif category == "work":
                plan = {"kind": "daylife", "mode": "home", "destination": "住处",
                        "reason": action, "detail": "继续推进手头创作或稿件", "prep_minutes": 0, "stay_minutes": 30}
            elif category == "chore":
                plan = {"kind": "daylife", "mode": "home", "destination": "住处",
                        "reason": action, "detail": "完成这项家务后再安排下一件事", "prep_minutes": 0, "stay_minutes": 25}
            elif category == "outing":
                destination = "附近公园" if "公园" in action else "附近街区"
                plan = {"kind": "daylife", "mode": "out", "destination": destination,
                        "reason": action, "detail": "按计划出门活动", "prep_minutes": 5, "travel_minutes": 10, "stay_minutes": 40}
            else:
                plan = {"kind": "daylife", "mode": "home", "destination": "住处",
                        "reason": action, "detail": "按自己的兴趣玩一会儿", "prep_minutes": 0, "stay_minutes": 60}
            plan["_commitment_category"] = category
            deferred_matches = bool(parsed.get("deferred") and current and _dependency_matches_current(parsed, current))
            dependency = current.get("id") if deferred_matches else None
            task, created = self._new_task(plan, now, source="delivered_commitment", status="queued", dependency=dependency,
                                           dependency_label="" if deferred_matches else "")
            task["commitment"] = True
            task["commitment_text"] = parsed["text"]
            if task.get("status") in ("queued", "committed") and not task.get("activity_id"):
                task["plan"] = {**(task.get("plan") or {}), **_safe_plan(plan)}
                task["label"] = str(plan.get("reason") or task.get("label") or "外出用餐")[:100]
            task["depends_on"] = current.get("id") if deferred_matches else None
            task["dependency_label"] = ""
            task["updated_at"] = now
            preempted = None
            if not deferred_matches:
                current = self.active()
                if not current:
                    self._activate(task, now)
                elif task.get("id") != current.get("id") and task.get("priority", 5) < current.get("priority", 5):
                    preempted = self._pause_active(now, "delivered_commitment")
                    self._activate(task, now)
            if preempted:
                task["preempted_activity_id"] = str(preempted)
            processed.append(delivery_id)
            self.data["processed_deliveries"] = processed[-300:]
            self._save()
            return {"task_id": task["id"], "created": created, "preempted_activity_id": preempted,
                    "deferred": bool(parsed.get("deferred")), "label": task.get("label")}
