"""SQLite persistence for Mengyu tasks, commitments, artifacts and reminders."""

import json
import hashlib
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

SCHEMA_VERSION = 3


class TaskStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path, timeout=15, isolation_level=None)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=15000")
            conn.execute("PRAGMA journal_mode=WAL")
            with conn:
                yield conn

    def initialize(self):
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                owner TEXT NOT NULL DEFAULT 'mengyu',
                beneficiary TEXT NOT NULL DEFAULT 'user',
                status TEXT NOT NULL DEFAULT 'pending',
                priority INTEGER NOT NULL DEFAULT 3,
                due_at REAL,
                source TEXT NOT NULL DEFAULT 'manual',
                source_id TEXT,
                external_ref TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                completed_at REAL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_source_id
                ON tasks(source_id) WHERE source_id IS NOT NULL AND source_id <> '';
            CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_external_ref
                ON tasks(external_ref) WHERE external_ref IS NOT NULL AND external_ref <> '';
            CREATE INDEX IF NOT EXISTS idx_tasks_status_type
                ON tasks(status, type, updated_at DESC);
            CREATE TABLE IF NOT EXISTS commitments (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id) ON DELETE CASCADE,
                promisor TEXT NOT NULL,
                promisee TEXT NOT NULL,
                original_quote TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 1.0,
                status TEXT NOT NULL DEFAULT 'promised',
                due_at REAL,
                reminder_policy TEXT NOT NULL DEFAULT 'contextual',
                accepted_at REAL,
                purchased_at REAL,
                sent_at REAL,
                received_at REAL,
                fulfilled_at REAL,
                deferred_until REAL,
                evidence TEXT NOT NULL DEFAULT '',
                source_message_id TEXT,
                last_prompted_at REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_commitments_status
                ON commitments(status, updated_at DESC);
            CREATE TABLE IF NOT EXISTS artifacts (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                uri TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                evidence TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(task_id, created_at);
            CREATE TABLE IF NOT EXISTS task_events (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                event_type TEXT NOT NULL,
                from_status TEXT,
                to_status TEXT,
                detail TEXT NOT NULL DEFAULT '',
                source_id TEXT,
                at REAL NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_task_events_source
                ON task_events(task_id, event_type, source_id)
                WHERE source_id IS NOT NULL AND source_id <> '';
            CREATE TABLE IF NOT EXISTS pending_candidates (
                id TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                title TEXT NOT NULL,
                kind TEXT NOT NULL,
                owner TEXT NOT NULL,
                beneficiary TEXT NOT NULL,
                confidence REAL NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                source_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at REAL NOT NULL,
                resolved_at REAL,
                resolved_task_id TEXT,
                prompted_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_candidates_pending
                ON pending_candidates(status, created_at DESC);
            CREATE TABLE IF NOT EXISTS reminders (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                due_at REAL NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                last_sent_at REAL,
                source_id TEXT,
                created_at REAL NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_reminder_unique
                ON reminders(task_id, due_at, kind);
            CREATE TABLE IF NOT EXISTS confirmation_receipts (
                source_id TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL,
                recorded_at REAL NOT NULL
            );
            """)
            candidate_columns = {row[1] for row in db.execute("PRAGMA table_info(pending_candidates)").fetchall()}
            if "prompted_at" not in candidate_columns:
                db.execute("ALTER TABLE pending_candidates ADD COLUMN prompted_at REAL")
            db.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version',?)",
                (str(SCHEMA_VERSION),),
            )

    @staticmethod
    def _task(row):
        if row is None:
            return None
        value = dict(row)
        try:
            value["metadata"] = json.loads(value.pop("metadata_json", "{}"))
        except (ValueError, TypeError):
            value["metadata"] = {}
        return value

    def create_task(self, *, task_type, title, description="", owner="mengyu",
                    beneficiary="user", status="pending", priority=3, due_at=None,
                    source="manual", source_id=None, external_ref=None, metadata=None,
                    now=None, completed_at=None):
        now = time.time() if now is None else float(now)
        title = str(title or "").strip()[:160]
        if not title:
            raise ValueError("task title is required")
        task_id = "TASK-" + uuid.uuid4().hex[:12].upper()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if source_id:
                existing = db.execute("SELECT * FROM tasks WHERE source_id=?", (str(source_id),)).fetchone()
                if existing:
                    db.execute("COMMIT")
                    return self._task(existing), False
            if external_ref:
                existing = db.execute("SELECT * FROM tasks WHERE external_ref=?", (str(external_ref),)).fetchone()
                if existing:
                    db.execute("COMMIT")
                    return self._task(existing), False
            db.execute("""INSERT INTO tasks
                (id,type,title,description,owner,beneficiary,status,priority,due_at,source,source_id,external_ref,created_at,updated_at,metadata_json,completed_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (task_id, str(task_type), title, str(description or "")[:2000], str(owner), str(beneficiary),
                 str(status), max(1, min(5, int(priority))), due_at, str(source),
                 str(source_id) if source_id else None, str(external_ref) if external_ref else None,
                 now, now, json.dumps(metadata or {}, ensure_ascii=False), completed_at))
            db.execute("INSERT INTO task_events(id,task_id,event_type,to_status,detail,source_id,at) VALUES(?,?,?,?,?,?,?)",
                       (uuid.uuid4().hex, task_id, "created", str(status), title, str(source_id) if source_id else None, now))
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            db.execute("COMMIT")
            return self._task(row), True

    def create_commitment(self, *, title, original_quote, promisor="user", promisee="mengyu",
                          confidence=1.0, due_at=None, reminder_policy="contextual",
                          source_id=None, source_message_id=None, kind="general", now=None):
        now = time.time() if now is None else float(now)
        with self.connect() as db:
            duplicate = db.execute("""SELECT c.* FROM commitments c
                WHERE c.promisor=? AND c.promisee=? AND c.original_quote=?
                  AND c.status NOT IN ('fulfilled','cancelled') AND c.created_at>?
                ORDER BY c.created_at DESC LIMIT 1""",
                (str(promisor), str(promisee), str(original_quote)[:500], now - 30 * 86400)).fetchone()
            if duplicate:
                return dict(duplicate), False
        task, created = self.create_task(
            task_type="commitment", title=title, description=original_quote,
            owner=promisor, beneficiary=promisee, status="promised", priority=2,
            due_at=due_at, source="conversation", source_id=source_id,
            metadata={"commitment_kind": kind}, now=now)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM commitments WHERE task_id=?", (task["id"],)).fetchone()
            if row:
                db.execute("COMMIT")
                return dict(row), False
            commitment_id = "COM-" + uuid.uuid4().hex[:12].upper()
            db.execute("""INSERT INTO commitments
                (id,task_id,promisor,promisee,original_quote,confidence,status,due_at,reminder_policy,accepted_at,source_message_id,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (commitment_id, task["id"], promisor, promisee, str(original_quote)[:500],
                 max(0.0, min(1.0, float(confidence))), "promised", due_at,
                 reminder_policy, now, source_message_id, now, now))
            db.execute("COMMIT")
            return self.get_commitment(commitment_id), True

    def get_commitment(self, commitment_id):
        with self.connect() as db:
            row = db.execute("""SELECT c.*,t.title,t.type AS task_type,t.description,t.owner,t.beneficiary,t.due_at AS task_due_at,t.status AS task_status
                FROM commitments c JOIN tasks t ON t.id=c.task_id WHERE c.id=?""", (commitment_id,)).fetchone()
            return dict(row) if row else None

    def get_task(self, task_id):
        with self.connect() as db:
            return self._task(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def update_task(self, task_id, *, status=None, title=None, description=None, due_at=None,
                    evidence=None, source_id=None, now=None):
        now = time.time() if now is None else float(now)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                db.execute("ROLLBACK")
                return None
            before = row["status"]
            next_status = status or before
            if before == "completed" and next_status != "completed":
                db.execute("COMMIT")
                return self._task(row)
            if next_status == "completed":
                if not str(evidence or "").strip():
                    db.execute("ROLLBACK")
                    raise ValueError("completion evidence is required")
                if row["type"] == "work":
                    artifacts = db.execute(
                        "SELECT uri FROM artifacts WHERE task_id=? AND status='delivered' AND uri<>''",
                        (task_id,),
                    ).fetchall()
                    if not any(Path(item["uri"]).is_file() for item in artifacts):
                        db.execute("ROLLBACK")
                        raise ValueError("work completion requires an actually delivered local artifact")
            updates = {"status": next_status, "updated_at": now}
            if title is not None:
                updates["title"] = str(title)[:160]
            if description is not None:
                updates["description"] = str(description)[:2000]
            if due_at is not None:
                updates["due_at"] = due_at
            metadata = json.loads(row["metadata_json"] or "{}")
            if evidence:
                metadata["evidence"] = str(evidence)[:1000]
            updates["metadata_json"] = json.dumps(metadata, ensure_ascii=False)
            if next_status == "completed":
                updates["completed_at"] = now
            assignments = ",".join(f"{key}=?" for key in updates)
            db.execute(f"UPDATE tasks SET {assignments} WHERE id=?", (*updates.values(), task_id))
            if next_status != before:
                db.execute("INSERT OR IGNORE INTO task_events(id,task_id,event_type,from_status,to_status,detail,source_id,at) VALUES(?,?,?,?,?,?,?,?)",
                           (uuid.uuid4().hex, task_id, "status_changed", before, next_status,
                            str(evidence or "")[:500], str(source_id) if source_id else None, now))
            updated = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            db.execute("COMMIT")
            return self._task(updated)

    def update_commitment(self, commitment_id, status, *, evidence="", due_at=None, source_id=None, now=None):
        now = time.time() if now is None else float(now)
        allowed = {"promised", "in_progress", "purchased", "sent", "received", "fulfilled", "deferred", "cancelled"}
        if status not in allowed:
            raise ValueError("unsupported commitment status")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM commitments WHERE id=?", (commitment_id,)).fetchone()
            if not row:
                db.execute("ROLLBACK")
                return None
            before = row["status"]
            if before in ("fulfilled", "cancelled") and status != before:
                db.execute("COMMIT")
                return self.get_commitment(commitment_id)
            if status == "fulfilled" and not str(evidence or "").strip():
                db.execute("ROLLBACK")
                raise ValueError("fulfilment evidence is required")
            order = {"promised": 0, "in_progress": 1, "purchased": 2, "sent": 3, "received": 4, "fulfilled": 5}
            if status in order and before in order and order[status] < order[before]:
                db.execute("COMMIT")
                return self.get_commitment(commitment_id)
            values = {"status": status, "updated_at": now}
            if evidence:
                values["evidence"] = str(evidence)[:1000]
            if due_at is not None:
                values["due_at"] = due_at
            timestamp_field = {
                "purchased": "purchased_at", "sent": "sent_at",
                "received": "received_at", "fulfilled": "fulfilled_at",
            }.get(status)
            if timestamp_field:
                values[timestamp_field] = now
            assignments = ",".join(f"{key}=?" for key in values)
            db.execute(f"UPDATE commitments SET {assignments} WHERE id=?", (*values.values(), commitment_id))
            task_status = "completed" if status in ("fulfilled", "cancelled") else ("blocked" if status == "deferred" else "in_progress")
            db.execute("UPDATE tasks SET status=?,updated_at=?,completed_at=? WHERE id=?",
                       (task_status, now, now if status in ("fulfilled", "cancelled") else None, row["task_id"]))
            if status != before:
                db.execute("INSERT OR IGNORE INTO task_events(id,task_id,event_type,from_status,to_status,detail,source_id,at) VALUES(?,?,?,?,?,?,?,?)",
                           (uuid.uuid4().hex, row["task_id"], "commitment_status", before, status,
                            str(evidence or "")[:500], str(source_id) if source_id else None, now))
            db.execute("COMMIT")
            return self.get_commitment(commitment_id)

    def add_candidate(self, *, text, title, kind, owner, beneficiary, confidence, reason="", source_id=None, now=None):
        now = time.time() if now is None else float(now)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if source_id:
                old = db.execute("SELECT * FROM pending_candidates WHERE source_id=?", (str(source_id),)).fetchone()
                if old:
                    db.execute("COMMIT")
                    return dict(old), False
            old = db.execute("SELECT * FROM pending_candidates WHERE status='pending' AND title=? AND created_at>? ORDER BY created_at DESC LIMIT 1",
                             (str(title)[:160], now - 86400)).fetchone()
            if old:
                db.execute("COMMIT")
                return dict(old), False
            candidate_id = "CAND-" + uuid.uuid4().hex[:10].upper()
            db.execute("""INSERT INTO pending_candidates
                (id,text,title,kind,owner,beneficiary,confidence,reason,source_id,status,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,'pending',?)""",
                (candidate_id, str(text)[:500], str(title)[:160], str(kind), str(owner), str(beneficiary),
                 float(confidence), str(reason)[:240], str(source_id) if source_id else None, now))
            row = db.execute("SELECT * FROM pending_candidates WHERE id=?", (candidate_id,)).fetchone()
            db.execute("COMMIT")
            return dict(row), True

    def confirmation_seen(self, source_id):
        if not source_id:
            return False
        with self.connect() as db:
            return db.execute("SELECT 1 FROM confirmation_receipts WHERE source_id=?", (source_id,)).fetchone() is not None

    def mark_candidate_prompted(self, candidate_id, now=None, *, source_id=None):
        now = time.time() if now is None else float(now)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if source_id and db.execute("SELECT 1 FROM confirmation_receipts WHERE source_id=?", (source_id,)).fetchone():
                db.execute("COMMIT")
                return False
            cursor = db.execute(
                "UPDATE pending_candidates SET prompted_at=MAX(COALESCE(prompted_at, 0), ?) WHERE id=? AND status='pending'",
                (now, candidate_id),
            )
            if source_id and cursor.rowcount:
                db.execute("INSERT INTO confirmation_receipts VALUES(?,?,?)", (source_id, candidate_id, now))
            db.execute("COMMIT")
            return cursor.rowcount > 0

    def pending_candidates(self, now=None, limit=3):
        now = time.time() if now is None else float(now)
        with self.connect() as db:
            rows = db.execute("SELECT * FROM pending_candidates WHERE status='pending' AND created_at>? AND created_at<=? ORDER BY created_at DESC LIMIT ?",
                              (now - 3 * 86400, now, max(1, min(10, int(limit))))).fetchall()
            return [dict(row) for row in rows]

    def resolve_candidate(self, candidate_id, accepted, *, now=None, source_id=None):
        now = time.time() if now is None else float(now)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if source_id and db.execute("SELECT 1 FROM confirmation_receipts WHERE source_id=?", (source_id,)).fetchone():
                db.execute("COMMIT")
                return None
            row = db.execute("SELECT * FROM pending_candidates WHERE id=? AND status='pending'", (candidate_id,)).fetchone()
            if not row:
                db.execute("ROLLBACK")
                return None
            resolved_task_id = None
            if accepted:
                task_id = "TASK-" + uuid.uuid4().hex[:12].upper()
                metadata = json.dumps({"commitment_kind": row["kind"], "confirmed_candidate": candidate_id}, ensure_ascii=False)
                db.execute("""INSERT INTO tasks
                    (id,type,title,description,owner,beneficiary,status,priority,source,source_id,created_at,updated_at,metadata_json)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (task_id, "commitment", row["title"], row["text"], row["owner"], row["beneficiary"],
                     "promised", 2, "confirmed_conversation", "confirmed:" + candidate_id, now, now, metadata))
                commitment_id = "COM-" + uuid.uuid4().hex[:12].upper()
                db.execute("""INSERT INTO commitments
                    (id,task_id,promisor,promisee,original_quote,confidence,status,reminder_policy,accepted_at,source_message_id,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (commitment_id, task_id, row["owner"], row["beneficiary"], row["text"],
                     row["confidence"], "promised", "contextual", now, row["source_id"], now, now))
                db.execute("INSERT INTO task_events(id,task_id,event_type,to_status,detail,source_id,at) VALUES(?,?,?,?,?,?,?)",
                           (uuid.uuid4().hex, task_id, "created_from_confirmation", "promised", row["title"], candidate_id, now))
                resolved_task_id = task_id
            db.execute("UPDATE pending_candidates SET status=?,resolved_at=?,resolved_task_id=? WHERE id=?",
                       ("accepted" if accepted else "rejected", now, resolved_task_id, candidate_id))
            if source_id:
                db.execute("INSERT INTO confirmation_receipts VALUES(?,?,?)", (source_id, candidate_id, now))
            db.execute("COMMIT")
            return {"candidate_id": candidate_id, "accepted": bool(accepted), "task_id": resolved_task_id}

    def add_artifact(self, task_id, *, kind, status, uri="", description="", evidence="", now=None):
        now = time.time() if now is None else float(now)
        if status not in ("not_generated", "generating", "generated", "blocked", "failed", "delivered", "verified"):
            raise ValueError("unsupported artifact status")
        artifact_id = "ART-" + uuid.uuid4().hex[:12].upper()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone():
                db.execute("ROLLBACK")
                return None
            duplicate = db.execute(
                "SELECT * FROM artifacts WHERE task_id=? AND kind=? AND status=? AND uri=? AND created_at>? ORDER BY created_at DESC LIMIT 1",
                (task_id, str(kind), status, str(uri)[:1000], now - 60),
            ).fetchone()
            if duplicate:
                db.execute("COMMIT")
                return dict(duplicate)
            db.execute("INSERT INTO artifacts(id,task_id,kind,status,uri,description,evidence,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       (artifact_id, task_id, str(kind), status, str(uri)[:1000], str(description)[:500], str(evidence)[:1000], now, now))
            db.execute("INSERT INTO task_events(id,task_id,event_type,detail,source_id,at) VALUES(?,?,?,?,?,?)",
                       (uuid.uuid4().hex, task_id, "artifact_" + status, str(description)[:500], artifact_id, now))
            db.execute("COMMIT")
            return {"id": artifact_id, "task_id": task_id, "kind": kind, "status": status, "uri": uri, "description": description, "created_at": now}

    def list_tasks(self, *, statuses=None, task_type=None, limit=20):
        clauses, params = [], []
        if statuses:
            statuses = list(statuses)
            clauses.append("status IN (" + ",".join("?" for _ in statuses) + ")")
            params.extend(statuses)
        if task_type:
            clauses.append("type=?")
            params.append(task_type)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as db:
            rows = db.execute("SELECT * FROM tasks" + where + " ORDER BY updated_at DESC LIMIT ?", (*params, max(1, min(100, int(limit))))).fetchall()
            return [self._task(row) for row in rows]

    def context(self, now=None, query_text=""):
        now = time.time() if now is None else float(now)
        tasks = [row for row in self.list_tasks(statuses=("pending", "in_progress", "blocked", "review"), limit=30)
                 if row.get("type") != "commitment"][:12]
        with self.connect() as db:
            rows = db.execute("""SELECT c.*,t.title,t.type AS task_type,t.status AS task_status,t.description
                FROM commitments c JOIN tasks t ON t.id=c.task_id
                WHERE c.status NOT IN ('fulfilled','cancelled') ORDER BY c.updated_at DESC LIMIT 20""").fetchall()
            candidates = self.pending_candidates(now, 3)
        commitments = []
        for row in rows:
            item = dict(row)
            commitments.append({
                "id": item["id"], "title": item["title"], "promisor": item["promisor"],
                "promisee": item["promisee"], "status": item["status"],
                "original_quote": item["original_quote"], "due_at": item["due_at"],
                "overdue": bool(item["due_at"] and item["due_at"] < now),
                "due_in_seconds": int(item["due_at"] - now) if item["due_at"] else None,
                "reminder_policy": item["reminder_policy"],
                "evidence": item["evidence"],
            })
        return {
            "active_tasks": [
                {"id": row["id"], "type": row["type"], "title": row["title"],
                 "status": row["status"], "description": row["description"],
                 "due_at": row["due_at"],
                 "overdue": bool(row["due_at"] and row["due_at"] < now),
                 "due_in_seconds": int(row["due_at"] - now) if row["due_at"] else None,
                 "metadata": row["metadata"]}
                for row in tasks
            ],
            "active_commitments": commitments,
            "pending_confirmation": [
                {"id": row["id"], "text": row["text"], "title": row["title"],
                 "confidence": row["confidence"], "reason": row["reason"],
                 "prompted_at": row.get("prompted_at") if isinstance(row, dict) else None}
                for row in candidates
            ],
            "rule": "已完成事项保留在历史中，不进入活跃提醒；没有明确日期的承诺不每天催促；承诺只有有证据确认后才标记履行。模糊表达必须先由梦雨明确询问；只有她的确认问题已实际送达后，用户紧接着的明确答复才可确认候选。",
        }

    def sync_work_order(self, order, now=None):
        if not isinstance(order, dict) or not order.get("id"):
            return None
        now = time.time() if now is None else float(now)
        stage = str(order.get("stage") or "接到需求")
        artifacts = order.get("artifacts", []) if isinstance(order.get("artifacts"), list) else []
        delivered_evidence = bool(order.get("completion_evidence")) or any(
            isinstance(item, dict) and item.get("status") == "delivered"
            and item.get("uri") and Path(str(item.get("uri"))).is_file()
            for item in artifacts
        )
        status = "completed" if stage == "完成" and delivered_evidence else (
            "review" if stage in ("成稿待确认", "交付", "完成") else "in_progress"
        )
        title = str(order.get("brief") or "梦雨的工作")
        external_ref = "work:" + str(order["id"])
        metadata = {
            "work_order_id": order["id"], "stage": stage,
            "source_updated_at": order.get("updated_at") or order.get("completed_at") or order.get("created_at"),
            "source_revision": hashlib.sha256(json.dumps(order, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
            "worked_seconds": order.get("worked_seconds", 0),
            "revisions": order.get("revisions", 0),
            "artifacts": order.get("artifacts", [])[-8:],
        }
        task, created = self.create_task(
            task_type="work", title=title, description=str(order.get("next_action") or "")[:1000],
            owner="mengyu", beneficiary="mengyu", status=status,
            due_at=order.get("deadline"), source="work_bridge", external_ref=external_ref,
            metadata=metadata, now=now, completed_at=order.get("completed_at") if status == "completed" else None)
        if created:
            return task
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._task(db.execute("SELECT * FROM tasks WHERE id=?", (task["id"],)).fetchone())
            if current is None:
                db.execute("ROLLBACK")
                return None
            old_metadata = current.get("metadata") or {}
            new_at = metadata.get("source_updated_at")
            old_at = old_metadata.get("source_updated_at")
            if (
                old_metadata.get("source_revision") == metadata["source_revision"]
                or current["status"] == "completed" and status != "completed"
                or isinstance(old_at, (int, float)) and isinstance(new_at, (int, float)) and old_at > new_at
            ):
                db.execute("COMMIT")
                return current
            db.execute("UPDATE tasks SET title=?,description=?,status=?,due_at=?,updated_at=?,completed_at=?,metadata_json=? WHERE id=?",
                       (title, str(order.get("next_action") or "")[:1000], status,
                        order.get("deadline"), now,
                        order.get("completed_at") if status == "completed" else None,
                        json.dumps(metadata, ensure_ascii=False), task["id"]))
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task["id"],)).fetchone()
            db.execute("COMMIT")
            return self._task(row)

    def get_task_with_artifacts(self, task_id):
        task = self.get_task(task_id)
        if not task:
            return None
        with self.connect() as db:
            task["artifacts"] = [dict(row) for row in db.execute("SELECT * FROM artifacts WHERE task_id=? ORDER BY created_at DESC", (task_id,)).fetchall()]
            task["events"] = [dict(row) for row in db.execute("SELECT * FROM task_events WHERE task_id=? ORDER BY at DESC LIMIT 20", (task_id,)).fetchall()]
        return task
