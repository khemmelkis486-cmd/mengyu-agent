"""Retain task capture inputs until the independent task plugin consumes them."""

import json
import logging
from pathlib import Path

from .state_write import write_json

LOGGER = logging.getLogger(__name__)


class TaskHandoff:
    def __init__(self, root):
        self.path = Path(root) / "task_handoff.json"

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"pending": {}, "processed": []}
        if not isinstance(data.get("pending"), dict) or not isinstance(data.get("processed"), list):
            raise ValueError("Invalid task handoff")
        return data

    def put(self, kind, identity, text, when):
        if not identity or not str(text or "").strip():
            return
        key = f"{kind}:{identity}"
        data = self._load()
        if key in data["pending"] or key in data["processed"]:
            return
        data["pending"][key] = {
            "kind": kind, "id": identity, "text": str(text)[:1800], "at": when,
        }
        write_json(self.path, data, "TaskHandoff")

    def flush(self, service, limit=40):
        if service is None:
            return 0
        data = self._load()
        completed = 0
        for key, row in list(data["pending"].items())[:limit]:
            try:
                if row["kind"] == "user":
                    service.capture_user_message(row["text"], message_id=row["id"], at=row["at"])
                else:
                    service.capture_delivered_message(row["text"], delivery_id=row["id"], at=row["at"])
            except Exception as exc:
                LOGGER.warning("Mengyu task handoff postponed (%s): %s", row["kind"], type(exc).__name__)
                break
            data["pending"].pop(key)
            data["processed"] = (data["processed"] + [key])[-2000:]
            completed += 1
        if completed:
            write_json(self.path, data, "TaskHandoff")
        return completed
