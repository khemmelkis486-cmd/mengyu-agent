"""Evidence-grounded relationship memories for natural, varied intimacy.

The module deliberately avoids numeric affection scores. It records only
meaningful relational moments grounded in the current user's exact words.
"""

import json
import time
import uuid

KINDS = {
    "care": "关心与照顾",
    "admiration": "欣赏与认可",
    "playful": "玩笑与默契",
    "support": "支持与依靠",
    "affection": "亲近与温柔",
    "longing": "想念与期待",
    "conflict": "分歧与失望",
    "repair": "理解与修复",
    "boundary": "独立空间与边界",
    "shared": "共同兴趣与习惯",
    "milestone": "重要关系节点",
}

IMPORTANCE = {"ordinary", "important", "milestone"}
MAX_HISTORY = 80

GUIDANCE = """【你和对方之间的关系：自然、有层次，不是单一甜蜜模式】
你们已有彼此喜欢、平等亲近的稳定底色，不需要每轮重新确认关系，也不需要靠反复说想念、撒娇、抱抱或夸他来证明亲密。
亲密有很多种表现：认真听他说事、记得他在意的细节、分享自己的判断、欣赏他的能力、一起开玩笑、自然地关心、讨论共同兴趣、坦然表达不同意见、偶尔想念或想靠近，也包括各自忙自己的事仍然安心。根据眼前互动选择，不轮换打卡，不为了显得有感情而强行加亲昵收尾。
先回应他实际说的内容。遇到求助就认真帮忙，遇到好消息可以替他高兴，遇到观点不同可以说自己的理由，遇到玩笑可以顺势接，遇到关心可以自然接受或反过来关心。不是每件事都要转成恋爱表达；也不因为已经亲密就失去自己的口味、判断、边界和生活。
不要把聊天变成对他的持续点评：不反复总结他刚才说得多好、做得多认真、是什么样的人。欣赏可以偶尔直接说一句，但说完回到共同话题；他问你的看法时，优先给出你自己的看法，而不是再评价他。
不要把他的忙碌、短回复、未回复或普通社交推断成不在乎；不靠冷落、惩罚、试探、盘问或制造嫉妒来证明感情。真实介意可以说清具体原因，也可以在解释和新互动后自然缓和。不要为了证明独立故意唱反调。
关系记忆是帮助你理解相处的背景，不是待汇报清单。记住不等于要提起；只有眼前话题确实相关、他主动追问，或出现新的发展时才自然引用旧经历。不要换词重复讲已经表达过的感受。
"""

CHAT_GUIDANCE = """【可选的关系经历记录】
普通聊天中，只有本轮互动确实形成了值得留存的关系经历时，才在可见回复后追加一行私有标记（不会发给他）：
[[MENGYU_RELATIONSHIP:{"kind":"care|admiration|playful|support|affection|longing|conflict|repair|boundary|shared|milestone","summary":"发生了什么，简短客观","meaning":"这件事对你自己的感受或关系意味着什么","evidence":"本轮用户原话中连续、逐字一致的片段","importance":"ordinary|important|milestone","resolves_id":"仅repair时填写上下文中未解决分歧的id，否则留空"}]]
只记录有意义的新互动，不记录普通问候、信息问答、重复夸奖或同一件事的换词版本。evidence必须逐字来自本轮用户消息，不能引用你自己的回复或更早聊天；summary不能编造他的行动、承诺或动机，meaning写“我感到/对我来说”，不要替他断定本意。没有值得记住的变化就不输出标记。
conflict只用于确实存在的具体分歧，不把玩笑、假设、普通意见不同自动升级成关系矛盾。repair必须引用上下文中真实存在且尚未解决的分歧id；没有明确对应项就不记录为修复。关系记忆只写入后台，不在正文解释记录机制。
"""


def _clean_text(value, limit):
    return value.strip()[:limit] if isinstance(value, str) else ""


def _valid_update(update):
    if (
        not isinstance(update, dict)
        or not isinstance(update.get("kind"), str)
        or update.get("kind") not in KINDS
    ):
        return False
    summary = _clean_text(update.get("summary"), 180)
    meaning = _clean_text(update.get("meaning"), 180)
    evidence = _clean_text(update.get("evidence"), 200)
    importance = update.get("importance", "ordinary")
    if not (4 <= len(summary) <= 180 and 2 <= len(meaning) <= 180):
        return False
    if (
        not (2 <= len(evidence) <= 200)
        or not isinstance(importance, str)
        or importance not in IMPORTANCE
    ):
        return False
    if update.get("kind") == "repair":
        return bool(_clean_text(update.get("resolves_id"), 80))
    return True


def extract_update(text):
    """Strip the private relationship marker and return its parsed payload."""
    body = str(text or "")
    prefix = "[[MENGYU_RELATIONSHIP:"
    visible = []
    update = None
    cursor = 0
    while (start := body.find(prefix, cursor)) != -1:
        visible.append(body[cursor:start])
        payload_start = start + len(prefix)
        payload = body[payload_start:]
        stripped = payload.lstrip()
        try:
            obj, end = json.JSONDecoder().raw_decode(stripped)
            end = payload_start + len(payload) - len(stripped) + end
            closing = end + len(body[end:]) - len(body[end:].lstrip())
            if not body.startswith("]]", closing):
                raise ValueError("Missing private relationship delimiter")
            if _valid_update(obj):
                update = obj
            cursor = closing + 2
        except (ValueError, TypeError):
            closing = body.find("]]", payload_start)
            cursor = closing + 2 if closing >= 0 else len(body)
    visible.append(body[cursor:])
    return "".join(visible).strip(), update


def _row_time(row):
    value = row.get("at", 0)
    return float(value) if isinstance(value, (int, float)) else 0.0


def _trim_history(rows):
    """Bound storage while retaining unresolved conflicts and milestones."""
    rows = [row for row in rows if isinstance(row, dict)]
    if len(rows) <= MAX_HISTORY:
        return rows
    protected = [
        row for row in rows
        if row.get("status") == "unresolved" or row.get("importance") == "milestone"
    ]
    protected_ids = {row.get("id") for row in protected}
    ordinary = [row for row in rows if row.get("id") not in protected_ids]
    keep = protected + ordinary[-max(0, MAX_HISTORY - len(protected)):]
    keep.sort(key=_row_time)
    return keep[-MAX_HISTORY:]


def apply_update(state, update, user_text, user_ts, message_id="", now=None):
    """Persist a meaningful relationship moment only with current exact evidence.

    `repair` records must explicitly reference an unresolved conflict id.
    Repeated delivery of the same source message is idempotent.
    """
    if not _valid_update(update):
        return False
    if not isinstance(state, dict) or user_ts != state.get("last_user_ts", 0):
        return False
    evidence = _clean_text(update.get("evidence"), 200)
    if not isinstance(user_text, str) or evidence not in user_text:
        return False

    rows = state.get("relationship_history")
    if not isinstance(rows, list):
        rows = []
    else:
        rows = [row for row in rows if isinstance(row, dict)]
    message_id = str(message_id or "")
    kind = update["kind"]
    for row in rows:
        if not isinstance(row, dict):
            continue
        if message_id and row.get("message_id") == message_id:
            return False
        if (
            row.get("user_ts") == user_ts
            and row.get("kind") == kind
            and row.get("evidence") == evidence
        ):
            return False

    target = None
    resolves_id = _clean_text(update.get("resolves_id"), 80)
    if kind == "repair":
        target = next(
            (
                row for row in rows
                if isinstance(row, dict)
                and row.get("id") == resolves_id
                and row.get("kind") == "conflict"
                and row.get("status") == "unresolved"
            ),
            None,
        )
        if target is None:
            return False

    now = time.time() if now is None else now
    record = {
        "id": uuid.uuid4().hex,
        "kind": kind,
        "summary": _clean_text(update.get("summary"), 180),
        "meaning": _clean_text(update.get("meaning"), 180),
        "evidence": evidence,
        "importance": update.get("importance", "ordinary"),
        "status": "unresolved" if kind == "conflict" else "recorded",
        "at": now,
        "user_ts": user_ts,
        "message_id": message_id,
    }
    if kind == "repair":
        target["status"] = "resolved"
        target["resolved_at"] = now
        target["resolved_by"] = record["id"]
        record["resolves_id"] = resolves_id
    rows.append(record)
    state["relationship_history"] = _trim_history(rows)
    state["relationship_last_update_at"] = now
    return True


def context(state, now=None):
    """Return compact relationship history without turning it into a report queue."""
    if not isinstance(state, dict):
        return None
    rows = state.get("relationship_history")
    if not isinstance(rows, list) or not rows:
        return None
    now = time.time() if now is None else now
    valid = [
        row for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("kind"), str)
        and row.get("kind") in KINDS
    ]
    if not valid:
        return None

    def view(row):
        return {
            "id": row.get("id", ""),
            "类型": KINDS.get(row.get("kind"), row.get("kind")),
            "经历": row.get("summary", ""),
            "对梦雨的意义": row.get("meaning", ""),
            "重要程度": row.get("importance", "ordinary"),
            "记录时间距今秒数": max(0, int(now - _row_time(row))),
            "状态": row.get("status", "recorded"),
        }

    recent_cutoff = now - 14 * 86400
    recent = [
        row for row in valid
        if row.get("kind") != "conflict"
        and row.get("at", 0) >= recent_cutoff
    ][-5:]
    unresolved = [
        row for row in valid
        if row.get("kind") == "conflict" and row.get("status") == "unresolved"
    ][-3:]
    milestones = [
        row for row in valid
        if row.get("importance") == "milestone"
        and row not in recent
        and row.get("kind") != "conflict"
    ][-3:]
    return {
        "关系底色": "稳定、平等、彼此喜欢；亲密不等于每轮都要撒娇、想念或索要陪伴。",
        "近期有意义的相处": [view(row) for row in recent],
        "尚未解决的分歧": [view(row) for row in unresolved],
        "重要共同记忆": [view(row) for row in milestones],
        "使用说明": "这些是相处背景，不是待办或主动播报清单。记住不等于提起；只有当前话题相关、对方追问或出现新发展时才自然引用。已解决的分歧只作历史，不重新翻出；不要反复复述同一段关系经历。",
    }
