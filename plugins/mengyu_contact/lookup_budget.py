"""Bound foreground lookups without changing autonomous background research."""

import re
import time
from difflib import SequenceMatcher

MAX_CALLS = 2
MAX_SECONDS = 75


def reserve(event, action, query, now=None):
    """Reserve before I/O; failed attempts count, fresh user events start fresh."""
    if action not in ("search", "web", "study"):
        return None
    getter, setter = (
        getattr(event, "get_extra", None),
        getattr(event, "set_extra", None),
    )
    if not callable(getter) or not callable(setter):
        return None
    now = time.monotonic() if now is None else now
    budget = getter("mengyu_lookup_budget")
    if budget is None:
        budget = {"started": now, "queries": []}
        setter("mengyu_lookup_budget", budget)
    normalized = re.sub(r"[^\w]", "", str(query).lower())
    if len(budget["queries"]) >= MAX_CALLS or now - budget["started"] >= MAX_SECONDS:
        return "本轮资料查询到这里收住，不再调用search/web/study。用已查到的可靠内容简短作答；不确定的就说明未查准，不编答案。先接他当前这句话，别让旧攻略堵住新的聊天。"
    if any(
        action == previous[0]
        and normalized
        and SequenceMatcher(None, normalized, previous[1]).ratio() >= 0.88
        for previous in budget["queries"]
    ):
        return "这个问题本轮已经查过相近内容，不再重复查询。沿用现有来源，先接当前消息；不足以确认的部分就说未查准。"
    budget["queries"].append((action, normalized))
    return None
