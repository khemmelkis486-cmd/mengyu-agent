"""Bridge between her life events and the economy wallet."""

from __future__ import annotations

from data.plugins.mengyu_contact import config

import asyncio
import json
import re
import sqlite3
import time
from pathlib import Path

from astrbot.api import logger

WALLET_DB = config.path("wallet.sqlite3", plugin="mengyu_economy")
TASK_DB = config.path("task_center.sqlite3", plugin="mengyu_task_center")


def recent_items(limit=3):
    """Recently bought items with their prices."""
    if not WALLET_DB.exists():
        return []
    conn = sqlite3.connect(WALLET_DB, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT q.label, q.amount_cents FROM items i "
            "JOIN quotes q ON q.id=i.quote_id WHERE q.tx_id IS NOT NULL "
            "ORDER BY i.updated_at DESC LIMIT ?",
            (int(limit),),
        )
        return [{"name": row["label"], "price_cents": row["amount_cents"]} for row in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def wallet(core):
    try:
        plugin = core.plugin_service("mengyu_economy")
    except Exception:  # noqa: BLE001 - economy is optional
        return None
    if plugin is None or not getattr(plugin, "ready", False):
        return None
    return getattr(plugin, "wallet", None)


def affordability(core):
    """Snapshot cash and whether the plan should tighten today."""
    book = wallet(core)
    if book is None:
        return {"tight": False, "cash": None, "flexible": None}
    try:
        snap = book.snapshot()
    except Exception:  # noqa: BLE001
        return {"tight": False, "cash": None, "flexible": None}
    cash = int(snap.get("cash_cents") or 0)
    flexible = int(snap.get("flexible_cents") or 0)
    pressure = str(snap.get("pressure") or "")
    tight = pressure in ("tight", "watch") or flexible < 30000
    return {"tight": tight, "cash": cash, "flexible": flexible}


async def estimate(core, label, category):
    """Ask the chat model for a plausible price in yuan."""
    try:
        provider = await core.chat_provider()
        response = await provider.text_chat(
            prompt="要给这件事估一个现实的人民币价格：" + str(label)[:120]
            + "（类别：" + category + "）。只输出JSON：{\"yuan\":整数}。",
            system_prompt="你在给虚构日常消费估价，只给一个合理的整数元，不要解释。",
            contexts=[],
            image_urls=[],
            session_id=None,
        )
        text = str(getattr(response, "completion_text", "") or "")
        match = re.search(r"\{.*?\}", text, re.S)
        data = json.loads(match.group(0)) if match else {}
        yuan = int(data.get("yuan") or 0)
        return max(1, min(yuan, 5000)) * 100
    except Exception:  # noqa: BLE001 - estimation is best effort
        return 5000


def _charge(core, amount, category, label, action_id):
    book = wallet(core)
    if book is None:
        return None
    try:
        return book.commit(
            {
                "kind": "expense",
                "action_id": action_id,
                "amount_cents": int(amount),
                "category": category,
                "label": str(label)[:120],
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("[Mengyu economy] spend skipped: %s", type(exc).__name__)
        return None


def _purchase(core, name, amount, action_id):
    book = wallet(core)
    if book is None:
        return None
    try:
        quote_id = "q:" + action_id
        book.create_quote(
            {
                "quote_id": quote_id,
                "scope": "trip",
                "source_ref": action_id,
                "label": str(name)[:120],
                "amount_cents": int(amount),
                "expires_at": int(time.time()) + 3600,
            }
        )
        receipt = book.commit(
            {
                "kind": "purchase",
                "action_id": action_id,
                "quote_ids": [quote_id],
                "source_ref": action_id,
                "scope": "trip",
                "category": "shopping",
                "checkout_id": "checkout:" + action_id,
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("[Mengyu economy] purchase skipped: %s", type(exc).__name__)
        return None
    try:
        from .core import home_items

        home_items.add(name, 1, "家里")
    except Exception:  # noqa: BLE001 - home inventory is optional
        pass
    return receipt

async def process_pending(core):
    """Settle queued meal and purchase spending."""
    pending = list(core.state.get("pending_spend") or [])
    if not pending:
        return 0
    core.state["pending_spend"] = []
    core.save()
    done = 0
    for row in pending:
        if not isinstance(row, dict):
            continue
        label = str(row.get("label") or "")
        category = str(row.get("category") or "food")
        amount = int(row.get("amount_cents") or 0)
        if not amount:
            amount = await estimate(core, label, category)
        action_id = str(row.get("id") or ("spend:" + str(int(time.time()))))
        if row.get("kind") == "purchase":
            receipt = _purchase(core, label, amount, action_id)
            if receipt:
                _mark_bought(core, label, receipt)
        else:
            receipt = _charge(core, amount, category, label, action_id)
        if receipt:
            done += 1
    return done


def _mark_bought(core, label, receipt):
    """Attach the wallet receipt to the shopping item she just bought."""
    state = getattr(core, "state", None)
    if not isinstance(state, dict):
        return
    shopping = state.get("shopping") or {}
    items = shopping.get("items") or []
    item_ids = [str(value) for value in receipt.get("item_ids") or []]
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if name and (name in label or label in name):
            item["bought"] = True
            item["item_ids"] = item_ids
            item["price_cents"] = int(receipt.get("amount_cents") or 0)
            state["shopping"] = shopping
            core.save()
            return


def _work_rows():
    if not TASK_DB.exists():
        return []
    conn = sqlite3.connect(TASK_DB, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT id, external_ref, status, updated_at FROM tasks "
                "WHERE type='work' AND status IN ('review','completed','done') "
                "ORDER BY updated_at ASC LIMIT 20"
            )
        ]
    finally:
        conn.close()


async def sync_work_income(core):
    """Register delivered work as receivable income."""
    book = wallet(core)
    if book is None:
        return 0
    count = 0
    for task in _work_rows():
        work_ref = str(task.get("external_ref") or task.get("id") or "")
        if not work_ref:
            continue
        conn = sqlite3.connect(TASK_DB, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            artifact = conn.execute(
                "SELECT id FROM artifacts WHERE task_id=? AND status!='blocked' "
                "ORDER BY created_at ASC LIMIT 1",
                (task["id"],),
            ).fetchone()
        finally:
            conn.close()
        if artifact is None:
            continue
        try:
            book.register_receivable(
                work_ref,
                {
                    "artifact_id": artifact["id"],
                    "delivered_at": int(task.get("updated_at") or 0),
                    "label": "稿费",
                },
                now=int(task.get("updated_at") or time.time()),
            )
            count += 1
        except Exception:  # noqa: BLE001 - old work or already invoiced
            continue
    return count


async def tick(core, now=None):
    """Run both directions once per life tick."""
    try:
        await process_pending(core)
        await sync_work_income(core)
    except Exception as exc:  # noqa: BLE001 - economy must never break life
        logger.warning("[Mengyu economy] tick failed: %s", type(exc).__name__)
