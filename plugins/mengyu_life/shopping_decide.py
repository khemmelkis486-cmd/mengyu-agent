"""Close a shopping trip with a real decision: buy, ask, or pass."""

from __future__ import annotations

import random
import time

from astrbot.api import logger

from .core import wishes

_LIKE = ("喜欢", "合适", "好看", "不错", "心动", "顺眼")


def after_trip(core, now=None):
    """Decide one item at the end of a browsing trip."""
    now = time.time() if now is None else float(now)
    state = core.state
    shop = state.get("shopping") or {}
    items = [row for row in shop.get("items") or [] if isinstance(row, dict)]
    if not items:
        return None
    open_items = [
        row for row in items if str(row.get("status") or "") not in ("买了", "没买")
    ]
    suitable = [
        row for row in open_items
        if any(word in str(row.get("opinion") or "") for word in _LIKE)
    ]
    place = str((state.get("life_now") or {}).get("place") or "商场")
    if not suitable:
        for row in open_items:
            row["status"] = "没买"
        state["shopping"] = shop
        core.save()
        return {"decision": "pass", "place": place}
    target = suitable[-1]
    for row in open_items:
        if row is not target:
            row["status"] = "没买"
    if random.random() < 0.6:
        target["status"] = "买了"
        state["shopping"] = shop
        pending = list(state.get("pending_spend") or [])
        pending.append({
            "kind": "purchase",
            "category": "shopping",
            "label": str(target.get("name") or ""),
            "id": "buy:" + str(target.get("name") or "")[:40] + ":" + str(int(now)),
        })
        state["pending_spend"] = pending[-20:]
        try:
            core.life.append({
                "kind": "purchase",
                "at": now,
                "activity": place,
                "thought": "在" + place + "买了" + str(target.get("name") or ""),
            })
            core.save_life()
        except Exception:  # noqa: BLE001 - memory is best effort
            pass
        core.save()
        logger.info("[Mengyu shopping] bought %s", target.get("name"))
        return {"decision": "bought", "item": target.get("name")}
    target["status"] = "想要"
    state["shopping"] = shop
    try:
        wishes.add(
            core.wish_path,
            "想要" + str(target.get("name") or ""),
            reason=str(target.get("opinion") or "")[:180],
            at=now,
            source="shopping",
        )
    except Exception as exc:  # noqa: BLE001 - wish is optional
        logger.warning("[Mengyu shopping] wish failed: %s", type(exc).__name__)
    core.save()
    logger.info("[Mengyu shopping] wants %s", target.get("name"))
    return {"decision": "ask", "item": target.get("name")}
