"""Synchronize contact work with its optional Task Center service."""

import json
from hashlib import sha256

from astrbot.api import logger
from data.plugins.mengyu_life.core.work import current as work_current
from data.plugins.mengyu_life.core.work import load as work_load


def recover_task_handoffs(core):
    service = core.task_center_service()
    if not service:
        core._work_sync_target = None
        return
    try:
        core.task_handoff.flush(service)
        if (
            "work.json" in core.writer_audit.drift
            and core.writer_audit.mode == "protect"
        ):
            return
        if core._work_sync_target is not service:
            core.state["work_task_sync"] = {}
            core._work_sync_target = service
        for order in work_load().get("orders", []):
            core.sync_task_work(order, service=service, remember=True)
    except (OSError, ValueError, RuntimeError) as exc:
        logger.warning("[Mengyu Task Center] Recovery paused: %s", type(exc).__name__)


def sync_task_work(core, order=None, now=None, *, service=None, remember=False):
    service = service or core.task_center_service()
    if not service:
        return None
    order = order or work_current()
    if not order or not order.get("id"):
        return None
    synced = core.state.setdefault("work_task_sync", {})
    revision = sha256(
        json.dumps(order, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    identity = str(order["id"])
    if remember and synced.get(identity) == revision:
        return None
    try:
        synced_task = service.sync_work_order(order, now=now)
        if synced_task:
            synced[identity] = revision
        return synced_task
    except Exception as exc:  # noqa: BLE001 - cross-plugin sync must not block life
        logger.warning("[Mengyu Task Center] Work sync skipped: %s", type(exc).__name__)
        return None
