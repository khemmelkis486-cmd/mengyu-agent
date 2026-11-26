"""AstrBot plugin: persistent tasks and mutual commitments for Mengyu."""

from data.plugins.mengyu_contact import config

import json
import re
import time
from datetime import datetime
from pathlib import Path
from data.plugins.mengyu_contact.local_clock import TZ

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .parser import commitment_progress, confirmation_intent, extract_due_at, parse_commitment
from .store import TaskStore

OWNER = config.owner_id()
UMO = config.message_origin()
DATA = config.path(plugin="mengyu_task_center")
DB_PATH = DATA / "task_center.sqlite3"


@register("mengyu_task_center", "local", "梦雨长期任务、工作成果与双方承诺管理", "1.1.2")
class MengyuTaskCenter(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.context = context
        self.store = TaskStore(DB_PATH)

    async def initialize(self):
        try:
            from data.plugins.mengyu_contact import life_gate
            life_gate.guard("task")
        except Exception:
            pass
        self.store.initialize()
        logger.info("[Mengyu Task Center] SQLite task ledger ready")

    async def terminate(self):
        return None

    def capture_user_message(self, text, *, message_id="", at=None):
        """Capture explicit promises and progress from each original user message."""
        at = time.time() if at is None else float(at)
        source_id = str(message_id or f"user:{int(at * 1000)}")
        result = {"created": False, "candidate": None, "updated": [], "event": None}
        value = str(text or "").strip()
        if not value:
            return result

        confirmation_ref = "confirm:" + source_id
        if self.store.confirmation_seen(confirmation_ref):
            return result

        # Resolve ambiguity only when the bot actually asked about that
        # candidate and the user's short answer arrives soon afterward.
        pending = self.store.pending_candidates(at, 1)
        if pending:
            candidate = pending[0]
            prompted_at = candidate.get("prompted_at")
            if isinstance(prompted_at, (int, float)) and 0 <= at - prompted_at <= 15 * 60:
                intent = confirmation_intent(value)
                if intent is not None:
                    resolved = self.store.resolve_candidate(candidate["id"], intent, now=at, source_id=confirmation_ref)
                    result["event"] = "candidate_confirmed" if intent else "candidate_rejected"
                    result["created"] = bool(intent and resolved and resolved.get("task_id"))
                    if intent and resolved:
                        result["updated"].append(resolved.get("task_id"))
                    return result

        fragments = [part.strip() for part in re.split(r"[\n。！？；;，,]+", value) if part.strip()]
        for index, fragment in enumerate(fragments or [value]):
            fragment_source = source_id if len(fragments) == 1 else f"{source_id}:{index}"
            progress = commitment_progress(fragment)
            if progress:
                for commitment in self._active_commitments():
                    if self._matches_commitment(fragment, commitment):
                        updated = self.store.update_commitment(
                            commitment["id"], progress, evidence=fragment,
                            source_id=fragment_source, now=at,
                        )
                        if updated:
                            result["updated"].append(commitment["id"])
                            result["event"] = "commitment_" + progress
                # A past/progress statement must never be reinterpreted as a
                # fresh promise if no matching active record was found.
                continue

            parsed = parse_commitment(fragment, speaker="user")
            if not parsed:
                continue
            # Gifts have a richer item/receipt lifecycle in gifts.json. Keep
            # that as the single source of truth instead of duplicating them
            # in the generic commitment list.
            if parsed["kind"] == "gift":
                continue
            if parsed["classification"] == "explicit":
                commitment, created = self.store.create_commitment(
                    title=parsed["title"], original_quote=parsed["text"],
                    promisor=parsed["owner"], promisee=parsed["beneficiary"],
                    confidence=parsed["confidence"], due_at=extract_due_at(fragment, at),
                    source_id=fragment_source, source_message_id=message_id,
                    kind=parsed["kind"], now=at,
                )
                result["created"] = result["created"] or created
                if created:
                    result["event"] = "commitment_created"
                if commitment:
                    result["updated"].append(commitment.get("id"))
            else:
                candidate, created = self.store.add_candidate(
                    text=parsed["text"], title=parsed["title"], kind=parsed["kind"],
                    owner=parsed["owner"], beneficiary=parsed["beneficiary"],
                    confidence=parsed["confidence"], reason=parsed["reason"],
                    source_id=fragment_source, now=at,
                )
                result["candidate"] = candidate
                if created:
                    result["event"] = "commitment_needs_confirmation"
        return result

    def capture_delivered_message(self, text, *, delivery_id="", at=None):
        """Capture delivered promises and mark delivered clarification questions."""
        at = time.time() if at is None else float(at)
        delivered_text = str(text or "")
        if re.search(r"你是认真的吗|是认真的吗|这句话算数吗|要不要.{0,10}记下来|你是认真答应的吗|是认真答应的吗|你刚才.{0,8}认真吗|这件事.{0,8}算数吗", delivered_text):
            pending = self.store.pending_candidates(at, 1)
            if pending:
                self.store.mark_candidate_prompted(
                    pending[0]["id"], now=at,
                    source_id="prompt:" + str(delivery_id) if delivery_id else None,
                )
        parsed = parse_commitment(delivered_text, speaker="mengyu")
        if not parsed:
            return None
        commitment, created = self.store.create_commitment(
            title=parsed["title"], original_quote=parsed["text"],
            promisor="mengyu", promisee="user", confidence=parsed["confidence"],
            due_at=extract_due_at(delivered_text, at),
            source_id=str(delivery_id or f"assistant:{int(at*1000)}"),
            source_message_id=str(delivery_id), kind=parsed["kind"], now=at,
        )
        return {"commitment": commitment, "created": created}

    def _active_commitments(self):
        with self.store.connect() as db:
            rows = db.execute("""SELECT c.*,t.title,t.description FROM commitments c
                JOIN tasks t ON t.id=c.task_id
                WHERE c.status NOT IN ('fulfilled','cancelled')
                ORDER BY c.updated_at DESC LIMIT 50""").fetchall()
            return [dict(row) for row in rows]

    @staticmethod
    def _matches_commitment(text, commitment):
        source = str(commitment.get("title") or "") + " " + str(commitment.get("original_quote") or "")
        tokens = re.findall(r"耳机|耳钉|银圈|戒指|项链|手链|玩偶|衣服|外套|裙子|鞋|包|围巾|手机壳|书|蛋糕|花|照片|电影|游戏|旅行|礼物|海报|文件", source)
        return any(token in text for token in tokens) if tokens else any(word in text for word in source.split() if len(word) > 1)

    def context_for_owner(self, query_text="", now=None):
        return self.store.context(now=now, query_text=query_text)

    def sync_work_order(self, order, now=None):
        return self.store.sync_work_order(order, now=now)

    def add_artifact(self, task_id, *, kind, status, uri="", description="", evidence="", now=None):
        return self.store.add_artifact(
            task_id, kind=kind, status=status, uri=uri,
            description=description, evidence=evidence, now=now,
        )

    @filter.command("任务")
    async def task_command(self, event: AstrMessageEvent, action: str = "总览", task_id: str = ""):
        if str(event.unified_msg_origin) != UMO or str(event.get_sender_id()) != OWNER:
            return
        now = time.time()
        if action in ("总览", "列表", ""):
            data = self.store.context(now=now)
            tasks = data["active_tasks"]
            commitments = data["active_commitments"]
            lines = [f"任务：{len(tasks)} 项", f"待履行承诺：{len(commitments)} 项"]
            for row in tasks[:8]:
                lines.append(f"[{row['status']}] {row['title']}（{row['id']}）")
            for row in commitments[:8]:
                lines.append(f"[承诺/{row['status']}] {row['title']}（{row['id']}）")
            if data["pending_confirmation"]:
                lines.append("待确认表达：" + "；".join(x["text"] for x in data["pending_confirmation"]))
            yield event.plain_result("\n".join(lines))
            return
        if action in ("承诺", "约定"):
            data = self.store.context(now=now)
            lines = [f"{row['title']}｜{row['promisor']}→{row['promisee']}｜{row['status']}｜{row['original_quote']}"
                     for row in data["active_commitments"]]
            yield event.plain_result("\n".join(lines) if lines else "目前没有待履行承诺。")
            return
        if action in ("工作", "进行中"):
            rows = self.store.list_tasks(statuses=("pending", "in_progress", "blocked", "review"), task_type="work")
            yield event.plain_result("\n".join(f"[{row['status']}] {row['title']}（{row['id']}）" for row in rows) or "目前没有进行中的工作任务。")
            return
        if action in ("历史", "已完成"):
            rows = self.store.list_tasks(statuses=("completed",), limit=20)
            yield event.plain_result("\n".join(f"[完成] {row['title']}（{row['id']}）" for row in rows) or "暂时没有已完成任务记录。")
            return
        if action in ("详情", "查看") and task_id:
            row = self.store.get_task_with_artifacts(task_id)
            yield event.plain_result(json.dumps(row, ensure_ascii=False, indent=2) if row else "没有找到这项任务。")
            return
        yield event.plain_result("用法：/任务 总览｜承诺｜工作｜历史｜详情 TASK-ID")

    @staticmethod
    def _parse_tool_due_at(value):
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            pass
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=TZ)
            return parsed.timestamp()
        except ValueError:
            return None

    @filter.llm_tool(name="mengyu_task")
    async def task_tool(self, event: AstrMessageEvent, action: str,
                        task_id: str = "", title: str = "", description: str = "",
                        due_at: str = "", status: str = "", evidence: str = "",
                        candidate_id: str = "", artifact_kind: str = "file",
                        artifact_status: str = "generated", artifact_uri: str = "") -> str:
        """Manage durable tasks and commitments. Does not purchase or send anything.

        Args:
            action(string): list / get / create / update / confirm_candidate / reject_candidate / add_artifact / fulfill.
            task_id(string): Existing task or commitment task ID.
            title(string): Short task title when creating.
            description(string): Task details or original commitment wording.
            due_at(string): Optional ISO date-time; leave empty if unspecified.
            status(string): pending / in_progress / blocked / review / completed, or commitment progress status.
            evidence(string): Actual evidence or explanation. Completion requires evidence.
            candidate_id(string): Pending ambiguous commitment candidate ID.
            artifact_kind(string): image / file / document / other.
            artifact_status(string): not_generated / generating / generated / blocked / failed / delivered / verified.
            artifact_uri(string): Actual saved artifact path or identifier, never an invented path.
        """
        if str(event.unified_msg_origin) != UMO or str(event.get_sender_id()) != OWNER:
            return "仅限已配置的账号在私聊使用。"
        now = time.time()
        try:
            if action == "list":
                return json.dumps(self.store.context(now=now), ensure_ascii=False)
            if action == "get":
                row = self.store.get_task_with_artifacts(task_id)
                return json.dumps(row, ensure_ascii=False) if row else "没有找到这项任务。"
            if action == "create":
                row, created = self.store.create_task(
                    task_type="personal", title=title, description=description,
                    owner="mengyu", beneficiary="mengyu", status="pending",
                    due_at=self._parse_tool_due_at(due_at), source="llm_tool", now=now,
                )
                return json.dumps({"created": created, "task": row}, ensure_ascii=False)
            if action == "update":
                row = self.store.update_task(task_id, status=status or None,
                                             description=description if description else None,
                                             due_at=self._parse_tool_due_at(due_at),
                                             evidence=evidence, now=now)
                return json.dumps(row, ensure_ascii=False) if row else "没有找到这项任务。"
            if action == "confirm_candidate":
                row = self.store.resolve_candidate(candidate_id, True, now=now)
                return json.dumps(row, ensure_ascii=False) if row else "待确认记录已不存在或已处理。"
            if action == "reject_candidate":
                row = self.store.resolve_candidate(candidate_id, False, now=now)
                return json.dumps(row, ensure_ascii=False) if row else "待确认记录已不存在或已处理。"
            if action == "add_artifact":
                row = self.store.add_artifact(task_id, kind=artifact_kind,
                                              status=artifact_status, uri=artifact_uri,
                                              description=description, evidence=evidence, now=now)
                return json.dumps(row, ensure_ascii=False) if row else "任务不存在，未保存成果。"
            if action == "fulfill":
                if not evidence.strip():
                    return "不能只凭口头选择完成：请提供实际完成依据。"
                task = self.store.get_task(task_id)
                if not task:
                    return "任务不存在。"
                with self.store.connect() as db:
                    row = db.execute("SELECT id FROM commitments WHERE task_id=?", (task_id,)).fetchone()
                    if task.get("type") == "work":
                        artifacts = db.execute("SELECT id,uri FROM artifacts WHERE task_id=? AND status='delivered' AND uri<>''", (task_id,)).fetchall()
                        if not any(Path(item["uri"]).is_file() for item in artifacts):
                            return "工作任务还没有实际发送成功的成果文件，不能标记完成。"
                if row:
                    result = self.store.update_commitment(row["id"], "fulfilled", evidence=evidence, now=now)
                    return json.dumps(result, ensure_ascii=False)
                result = self.store.update_task(task_id, status="completed", evidence=evidence, now=now)
                return json.dumps(result, ensure_ascii=False) if result else "任务不存在。"
            return "不支持的任务操作。"
        except Exception as exc:  # noqa: BLE001 - task tools must not break chat
            logger.exception("[Mengyu Task Center] Tool failed: %s", type(exc).__name__)
            return "任务记录没有成功更新，请稍后重试；原有记录未主动删除。"
