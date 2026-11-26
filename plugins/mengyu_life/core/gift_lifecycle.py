"""Gift lifecycle reconciliation for legacy gifts.json without deleting history."""

import hashlib
import json
import re
import threading
import time
from pathlib import Path

from data.plugins.mengyu_contact.state_write import write_json

_LOCK = threading.RLock()
FOOD_KINDS = {"food", "delivery", "takeout"}
GIFT_WORDS = (
    "耳机", "耳钉", "银圈", "戒指", "项链", "手链", "手镯", "玩偶", "衣服", "外套",
    "裙子", "鞋", "包", "围巾", "手机壳", "书", "蛋糕", "花", "杯子", "礼物", "饰品",
    "口红", "香水", "发夹", "帽子", "手表", "键盘", "鼠标", "相机", "抱枕",
)


def _load(path):
    path = Path(path)
    if not path.exists():
        return {"items": []}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        raise ValueError("gifts.json has an unsupported structure")
    return value


def _save(path, data):
    write_json(path, data, "GiftStore")

def _norm(text):
    return re.sub(r"[\s，。！？、；：,.!?（）()\[\]{}\-_/]+", "", str(text or "")).lower()


def _gift_id(entry):
    existing = str(entry.get("gift_id") or entry.get("id") or "").strip()
    if existing:
        return existing
    raw = f"{entry.get('item','')}|{entry.get('at',0)}|{entry.get('note','')}"
    return "gift-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _is_gift(entry):
    return isinstance(entry, dict) and entry.get("kind") not in FOOD_KINDS


def _known_item(text):
    matches = [word for word in GIFT_WORDS if word in text]
    if not matches:
        return ""
    return max(matches, key=len)


def _status_from_text(text):
    value = str(text or "").strip()
    if not value or re.search(r"(?:吗|么|没收到|有没有收到|是不是到了|到了吗|收到没)[？?！!。]*$", value):
        return None
    if re.search(r"(?:没买|还没买|没有买|不买|没收到|还没收到|没有收到|没到|还没到|没送到|还没送到|没寄出|未发货|没发货|没答应|别记|不用记|只是开玩笑|随口说说)", value):
        return None
    if re.search(r"(?:给你买的|买给你的|给你准备的|寄给你的|送给你的|给你的).{0,24}(?:到了|送到了|送过来了|收到|已经到)", value) or re.search(r"(?:已经|刚刚|刚才)?(?:送到|送过来|寄到|收到|到货了|到了|已经给你了|给你了|送给你了|交给你了|送完了)", value) or re.search(r"(?:把)?[\u4e00-\u9fff]{1,8}(?:送给你了|给你了|已经给你|送给你啦)", value):
        return "received"
    if re.search(r"(?:已经|刚刚|刚才)?(?:寄出|发货|发出去|送出去了|寄给你了|送过去了|已经寄给你)", value):
        return "sent"
    if re.search(r"(?:已经|刚刚|刚才)?(?:买好了|买了|下单了|付款了|付过款了)", value):
        return "purchased"
    if re.search(r"我(?:答应|答应过|一定|肯定|会|要|下次|明天|后天|周末|这周|下周|之后|改天)?\s*(?:给你买|买给你|送你|给你送|寄给你|给你寄)", value) or re.search(r"(?:下次|明天|后天|周末|这周|下周)我(?:会|一定|肯定)?\s*(?:给你买|买给你|送你|给你送)", value):
        return "promised"
    return None


def _match_entries(items, text):
    normalized = _norm(text)
    active_statuses = {"desired", "promised", "purchased", "sent", "received"}
    active = [
        row for row in items
        if _is_gift(row)
        and row.get("lifecycle_status", row.get("status", "")) in active_statuses
    ]
    short = _known_item(text)
    rows = []
    for entry in active:
        name = _norm(entry.get("item", ""))
        if name and (name in normalized or normalized in name or (short and short in name)):
            rows.append(entry)
    if rows:
        rows.sort(key=lambda row: float(row.get("updated_at", row.get("at", 0)) or 0), reverse=True)
        return rows[:1]
    return active if len(active) == 1 else []


def _new_entry(text, status, now, source_id):
    item = _known_item(text)
    if not item:
        return None
    gift_id = "gift-" + hashlib.sha1(f"{_norm(item)}|{source_id}".encode("utf-8")).hexdigest()[:16]
    return {
        "id": gift_id,
        "gift_id": gift_id,
        "kind": "gift",
        "item": item,
        "at": now,
        "status": status,
        "lifecycle_status": status,
        "owner": "user",
        "beneficiary": "mengyu",
        "source_id": source_id,
        "known_by_mengyu": status == "received",
        "notice_pending": status == "received",
        "received_at": now if status == "received" else None,
        "acknowledged_at": None,
    }


def reconcile_external(path, state=None, now=None):
    """Migrate external delivery states once; never repeatedly re-notify old gifts."""
    now = time.time() if now is None else float(now)
    state = state if isinstance(state, dict) else {}
    with _LOCK:
        data = _load(path)
        changed = False
        pending = list(state.get("pending_gift_ack_ids") or [])
        mentions = state.get("gift_mentions") or {}
        for entry in data["items"]:
            if not _is_gift(entry):
                continue
            gift_id = _gift_id(entry)
            if entry.get("gift_id") != gift_id:
                entry["gift_id"] = gift_id
                changed = True
            legacy = entry.get("status")
            if legacy in ("given", "delivered", "received") and not entry.get("known_by_mengyu"):
                age = max(0, now - float(entry.get("at", now) or now))
                if mentions.get(entry.get("id"), 0) > 0:
                    entry.update(
                        lifecycle_status="acknowledged",
                        known_by_mengyu=True,
                        notice_pending=False,
                        acknowledged_at=entry.get("acknowledged_at") or entry.get("at") or now,
                    )
                else:
                    entry.update(
                        lifecycle_status="received",
                        known_by_mengyu=True,
                        notice_pending=True,
                        received_at=entry.get("received_at") or entry.get("at") or now,
                    )
                    if gift_id not in pending:
                        pending.append(gift_id)
                changed = True
            elif entry.get("lifecycle_status") == "received" and entry.get("notice_pending"):
                if gift_id not in pending:
                    pending.append(gift_id)
        if changed:
            _save(path, data)
        state["pending_gift_ack_ids"] = pending[-20:]
        return pending



# Ambiguous gifts are proposals, not durable promises. They must be confirmed
# after Mengyu has actually asked about the specific proposal.
_AMBIGUOUS_GIFT = re.compile(r"改天|有空|以后有机会|看情况|到时候再说|如果有机会|也许|可能|我想|我打算|要不|考虑")
_FIRM_GIFT = re.compile(r"我答应|我一定|我肯定|一定给你|肯定给你|我会给你|我下次给你")
_YES_GIFT = re.compile(r"^(?:嗯+|嗯嗯|是|是的|对|对的|好|好的|当然|当然了|认真的|真的|说定了|算数|没错|会的|一定|答应你|确定)[呀啊哦啦呢吧！!。~～\s]*$")
_NO_GIFT = re.compile(r"^(?:不|不是|没有|没|算了|开玩笑|随口说说|别记|不算数|还没决定|不用)[呀啊哦啦呢吧！!。~～\s]*$")


def _gift_confirmation(state, text, now):
    pending = state.get("pending_gift_candidate")
    if not isinstance(pending, dict):
        return None
    since = float(pending.get("at", now) or now)
    if now - since > 24 * 3600 or now < since:
        state.pop("pending_gift_candidate", None)
        return None
    asked_at = pending.get("asked_at")
    if not isinstance(asked_at, (int, float)) or not 0 <= now - asked_at <= 15 * 60:
        return None
    value = str(text or "").strip()
    if _YES_GIFT.fullmatch(value):
        state.pop("pending_gift_candidate", None)
        return pending
    if _NO_GIFT.fullmatch(value):
        state.pop("pending_gift_candidate", None)
        return False
    return None


def mark_gift_candidate_prompted(state, reply_text, now=None):
    """Mark a candidate prompted only if a delivered reply asks about this gift."""
    now = time.time() if now is None else float(now)
    pending = state.get("pending_gift_candidate")
    if not isinstance(pending, dict) or now - float(pending.get("at", now) or now) > 24 * 3600:
        return False
    text = str(reply_text or "")
    item = str(pending.get("item") or "")
    if item and item in text and re.search(r"认真|算数|当真|要不要.*记|是不是.*答应|真的.*买|确定.*买", text):
        pending["asked_at"] = now
        return True
    return False

def apply_user_message(path, state, text, now=None, source_id=""):
    """Apply a clearly stated purchase/delivery update to the matching gift."""
    now = time.time() if now is None else float(now)
    confirmed = _gift_confirmation(state, text, now)
    if confirmed is False:
        return {"changed": False, "event": "gift_candidate_rejected", "gift_ids": []}
    if confirmed:
        # Confirmation resolves the original proposal, not the bare “嗯嗯”.
        text = str(confirmed["text"])
        source_id = str(confirmed.get("source_id") or source_id) + ":confirmed"
        status = "promised"
    else:
        status = _status_from_text(text)
    if not status:
        return {"changed": False, "event": None, "gift_ids": []}
    if status == "promised" and confirmed is None and _AMBIGUOUS_GIFT.search(str(text)) and not _FIRM_GIFT.search(str(text)):
        item = _known_item(text)
        if item:
            state["pending_gift_candidate"] = {
                "item": item, "text": str(text)[:240], "at": now,
                "source_id": str(source_id or f"user:{int(now*1000)}"),
                "asked_at": None,
            }
            return {"changed": False, "event": "gift_needs_confirmation", "gift_ids": []}
    with _LOCK:
        data = _load(path)
        rows = _match_entries(data["items"], text)
        item = _known_item(text)
        if not rows and status in ("promised", "purchased", "sent", "received") and item:
            created = _new_entry(text, status, now, str(source_id or f"{int(now*1000)}"))
            if created:
                data["items"].append(created)
                rows = [created]
        if not rows:
            return {"changed": False, "event": None, "gift_ids": []}
        changed = False
        event_type = None
        gift_ids = []
        gift_items = []
        for entry in rows:
            gift_id = _gift_id(entry)
            gift_ids.append(gift_id)
            gift_items.append(str(entry.get("item") or ""))
            previous = entry.get("lifecycle_status") or entry.get("status") or "desired"
            if previous in ("given", "delivered"):
                previous = "received"
            # A duplicate message must not create a second notification.
            if entry.get("last_status_source_id") == source_id and source_id:
                continue
            order = {"desired": 0, "promised": 1, "purchased": 2, "sent": 3, "received": 4, "acknowledged": 5, "experienced": 6, "archived": 7}
            target = status
            if order.get(target, 0) < order.get(previous, 0) and target != "promised":
                # Historical statements must not roll a gift backwards.
                continue
            if target == "promised" and previous not in ("desired", "promised"):
                continue
            entry["gift_id"] = gift_id
            entry["lifecycle_status"] = target
            entry["status"] = entry.get("status") if entry.get("kind") in FOOD_KINDS else target
            entry["last_status_source_id"] = str(source_id or "")
            entry["last_status_at"] = now
            entry["updated_at"] = now
            entry.setdefault("owner", "user")
            entry.setdefault("beneficiary", "mengyu")
            if target == "received":
                if previous != "received" and previous != "acknowledged":
                    entry["received_at"] = now
                    entry["notice_pending"] = True
                    entry["known_by_mengyu"] = True
                    state.setdefault("pending_gift_ack_ids", []).append(gift_id)
                    event_type = "gift_received"
                elif not entry.get("acknowledged_at") and not entry.get("notice_pending"):
                    entry["notice_pending"] = True
                    entry["known_by_mengyu"] = True
                    state.setdefault("pending_gift_ack_ids", []).append(gift_id)
                    event_type = "gift_received"
            elif target in ("purchased", "sent"):
                entry["known_by_mengyu"] = True
            changed = True
        if changed:
            # De-duplicate pending IDs without losing order.
            state["pending_gift_ack_ids"] = list(dict.fromkeys(state.get("pending_gift_ack_ids", [])))[-20:]
            data["items"] = data["items"][-200:]
            _save(path, data)
        return {"changed": changed, "event": event_type, "gift_ids": gift_ids, "gift_items": gift_items}


def _reply_acknowledges_gift(reply_text, item, pending_count):
    text = str(reply_text or "").strip()
    if not text:
        return False
    if re.search(r"没收到|还没收到|没有收到|没到|还没到|没看见|没有看见", text):
        return False
    if item and item not in text:
        # Without naming the item, only an explicit receipt statement can
        # acknowledge a single pending gift; generic “谢谢” may refer to anything.
        if pending_count != 1 or not re.search(r"收到|到啦|到了|看到了|看见了|收下了|拆开了|打开了", text):
            return False
    return bool(re.search(
        r"收到|到啦|到了|看到了|看见了|谢谢|喜欢|开心|好耶|太好了|真的呀|终于|拆开|打开看看|让我看看|收下了|戴着|用着",
        text,
    ))


def acknowledge_notices(path, state, now=None, source_id="", reply_text=""):
    """Acknowledge only gifts that the delivered reply actually responds to.

    A successful QQ send alone is not enough: if the reply talks about another
    topic, the gift notice remains pending for the next relevant turn.
    """
    now = time.time() if now is None else float(now)
    pending_ids = list(dict.fromkeys(state.get("pending_gift_ack_ids") or []))
    if not pending_ids or not str(reply_text or "").strip():
        return False
    with _LOCK:
        data = _load(path)
        changed = False
        acknowledged = set()
        for entry in data["items"]:
            if not _is_gift(entry):
                continue
            gift_id = _gift_id(entry)
            if gift_id not in pending_ids or not entry.get("notice_pending"):
                continue
            if not _reply_acknowledges_gift(reply_text, str(entry.get("item") or ""), len(pending_ids)):
                continue
            entry.update(
                notice_pending=False,
                known_by_mengyu=True,
                acknowledged_at=now,
                lifecycle_status="acknowledged",
                acknowledged_source_id=str(source_id or ""),
            )
            acknowledged.add(gift_id)
            changed = True
        state["pending_gift_ack_ids"] = [item for item in pending_ids if item not in acknowledged]
        if changed:
            _save(path, data)
        return changed


def context(path, now=None, query_text=""):
    """Return only active or newly received gifts; historical items need a query."""
    now = time.time() if now is None else float(now)
    query = _norm(query_text)
    with _LOCK:
        data = _load(path)
    output = []
    for entry in reversed(data["items"][-200:]):
        if not _is_gift(entry):
            continue
        status = entry.get("lifecycle_status") or entry.get("status") or "desired"
        name = str(entry.get("item") or "未命名礼物")
        relevant_history = bool(query and _norm(name) in query)
        if status in ("promised", "purchased", "sent") or entry.get("notice_pending") or relevant_history:
            state_text = {
                "desired": "梦雨曾表达愿望，尚未承诺",
                "promised": "用户已经答应，尚未确认购买",
                "purchased": "用户已购买，尚未确认送达",
                "sent": "已经寄出或送出，尚未确认收到",
                "received": "已经收到，正等待梦雨回应",
                "acknowledged": "已经收到并知晓，属于共同经历",
                "experienced": "已经收到并体验过，属于共同经历",
                "archived": "历史礼物",
                "given": "已经收到并知晓，属于共同经历",
                "delivered": "已经收到并知晓，属于共同经历",
            }.get(status, str(status))
            if entry.get("notice_pending"):
                state_text = "刚确认收到，当前回复应自然回应一次；回复送达后停止重复提醒"
            output.append({
                "gift_id": _gift_id(entry),
                "item": name,
                "status": status,
                "status_text": state_text,
                "note": str(entry.get("note") or "")[:160],
                "at": entry.get("at"),
                "received_at": entry.get("received_at"),
                "historical_query_match": relevant_history,
            })
        if len(output) >= 8:
            break
    return output
