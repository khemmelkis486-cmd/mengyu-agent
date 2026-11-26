"""A low-pressure wish ledger: desires can be expressed, answered and followed up."""

import json
import re
import threading
import uuid
from pathlib import Path

from data.plugins.mengyu_contact.state_write import write_json

_LOCK = threading.RLock()
GUIDANCE = """【你可以主动许愿】愿望指具体想要的东西或想一起做的事：小物件、衣服、书、吃的、想去的地方、想一起看的电影。想让他陪你聊天、抱抱、多陪一会儿属于请求，不算愿望，直接说就好，不要记成许愿。你有真实而具体的期待时，可以直接说出来，不必藏在暗示里，也不必等他先问。愿望不是每轮必须索取的任务，不为了证明亲密而随机要东西；一次只自然提出一件，说明为什么想要，给对方商量或拒绝的空间，不施压、不把拒绝理解成不爱。已提出的愿望要记住，答应不等于已经实现；未实现前可在合适时轻轻跟进，但不要反复催促。他明确答应、拒绝或说改天时，按实际回应更新状态。"""


def _blank():
    return {"schema_version": 1, "items": []}


def load(path):
    path = Path(path)
    with _LOCK:
        if not path.exists():
            return _blank()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return _blank()
        if not isinstance(data, dict):
            return _blank()
        if not isinstance(data.get("items"), list):
            data["items"] = []
        data.setdefault("schema_version", 1)
        return data


def _save(path, data):
    write_json(path, data, "WishStore")

def _key(text):
    return re.sub(r"[\s，。！？、；,.!?：:‘’“”\"'`]+", "", str(text or "")).lower()


def _category(text):
    if re.search(r"买|送|礼物|玩偶|衣服|东西|物件", text):
        return "gift"
    if re.search(r"陪我|陪你|聊一会|多陪|找我|主动找", text):
        return "companionship"
    if re.search(r"一起|和你|跟你|带我|陪我去|看电影|听歌|旅行|出去玩|吃饭", text):
        return "shared_experience"
    if re.search(r"夸夸|写信|纪念日|记得|表达|说喜欢", text):
        return "affection"
    if re.search(r"学|练|做甜点|支持我|帮我", text):
        return "growth_or_support"
    return "request"


def _find_duplicate(items, wish):
    key = _key(wish)
    for item in reversed(items[-30:]):
        old = _key(item.get("wish", ""))
        if key == old or (len(key) >= 8 and (key in old or old in key)):
            return item
    return None


def add(path, wish, category=None, reason="", at=0, source="planner", source_id=None, status="unspoken", price_cents=None):
    wish = str(wish or "").strip().strip("，。！？!? ")[:180]
    if len(wish) < 4:
        return None
    data = load(path)
    duplicate = _find_duplicate(data["items"], wish)
    if duplicate:
        if reason and not duplicate.get("reason"):
            duplicate["reason"] = str(reason)[:180]
            _save(path, data)
        return duplicate
    open_items = [item for item in data["items"] if item.get("status") in ("unspoken", "shared", "accepted", "deferred")]
    if len(open_items) >= 4:
        return None
    if source == "life_planner" and any(
        item.get("status") == "unspoken"
        and at - float(item.get("created_at", 0) or 0) < 90 * 60
        for item in open_items
    ):
        return None
    row = {
        "id": uuid.uuid4().hex,
        "wish": wish,
        "category": category or _category(wish),
        "reason": str(reason or "")[:180],
        "status": status,
        "created_at": float(at or 0),
        "updated_at": float(at or 0),
        "source": source,
        "source_id": str(source_id or ""),
        "response": "",
    }
    if price_cents:
        row["price_cents"] = int(price_cents)
    data["items"].append(row)
    data["items"] = data["items"][-80:]
    _save(path, data)
    return row


def record_delivered_text(path, text, at, delivery_id):
    """Record explicit wishes only from text confirmed as delivered."""
    content = str(text or "")
    if re.search(r"假如|比如|例如|举个例子|小说里|故事里|引用", content):
        return []
    patterns = (
        r"[^。！？!?\n]{0,30}(?:我希望你|我想让你|我想要你|我想你陪我|我[^。！？!?\n]{0,8}想要|你能不能|可不可以|能不能|下次我们|要不要我们|不如我们|我们一起|我想和你|我想跟你|陪我去|陪我一起|给我买|送我)[^。！？!?\n]{2,100}",
    )
    rows = []
    clauses = [part.strip() for part in re.split(r"(?<=[。！？!?\n])", content) if part.strip()]
    for clause in clauses:
        match = None
        for pattern in patterns:
            match = re.search(pattern, clause)
            if match:
                break
        if not match:
            continue
        wish = match.group(0).strip()
        if re.search(r"不想|不需要|不用|别再|不要", wish):
            continue
        if "我想要" in wish and not re.search(
            r"你|陪|一起|送|买|玩偶|礼物|衣服|耳机|书|蛋糕|饰品|手机壳|小物件",
            wish,
        ):
            continue
        row = add(path, wish, at=at, source="delivered_chat", source_id=delivery_id, status="shared")
        if row:
            if row.get("status") in ("accepted", "declined", "deferred", "fulfilled"):
                continue
            if row.get("status") == "shared" and row.get("source_id") and row.get("source_id") != str(delivery_id):
                continue
            row["status"] = "shared"
            row["shared_at"] = float(at)
            row["source_id"] = str(delivery_id)
            data = load(path)
            for item in data["items"]:
                if item.get("id") == row["id"]:
                    item.update(row)
                    break
            _save(path, data)
            rows.append(row)
            break  # one clear wish per delivered message; avoid splitting one plan into several asks
    return rows


def resolve_user_response(path, user_text, answered_tail, now):
    """Resolve only a clear response to the immediately preceding delivered wish."""
    text = str(user_text or "").strip()
    if not text or len(text) > 240:
        return False
    data = load(path)
    fulfilled = False
    for item in data["items"]:
        if item.get("status") not in ("accepted", "deferred"):
            continue
        wish = str(item.get("wish", ""))
        category = item.get("category")
        if category == "gift":
            anchor = next((word for word in re.findall(r"玩偶|礼物|衣服|耳机|书|蛋糕|饰品|手机壳|小物件", wish) if word in text), "")
            done = bool(anchor and re.search(r"买到了|买好了|买了|送到了|给你买|已经买", text))
        elif category == "shared_experience":
            anchor = next((word for word in re.findall(r"看电影|看展|听歌|旅行|出去玩|吃饭|逛街", wish) if word in text), "")
            done = bool(anchor and re.search(r"我们.{0,10}(?:去了|去过|看了|看完|听了|玩了|吃了)|一起.{0,10}(?:去了|去过|看了|看完|听了|玩了|吃了)", text))
        elif category == "companionship":
            done = bool(re.search(r"陪你|找你|和你聊|跟你聊|见到你", text) and re.search(r"了|过|刚", text))
        else:
            done = False
        if done:
            item["status"] = "fulfilled"
            item["response"] = text[:160]
            item["updated_at"] = float(now)
            item["fulfilled_at"] = float(now)
            fulfilled = True
    if fulfilled:
        _save(path, data)
        return True
    if not isinstance(answered_tail, dict):
        return False
    delivery_id = str(answered_tail.get("delivery_id") or "")
    reply = str(answered_tail.get("reply") or "")
    if not delivery_id or not reply or now - float(answered_tail.get("at", 0) or 0) > 7 * 86400:
        return False
    candidate = next((item for item in reversed(data["items"]) if item.get("source_id") == delivery_id and item.get("status") == "shared"), None)
    if not candidate:
        return False
    if re.search(r"不行|不能|不方便|没空|不想|做不到", text):
        status = "declined"
    elif re.search(r"改天|下次|以后|等有空|等我忙完|过几天|晚点再", text):
        status = "deferred"
    elif re.match(
        r"^(?:我)?(?:好|好啊|可以|行|行啊|当然|没问题|答应你|当然可以|愿意|那就这么定了)(?:啊|呀|吧|的)?(?:[，, ]?)(?:我)?(?:陪你|带你|给你|帮你|一起|去|买|送|安排|陪你去)?[^。！？!?]{0,24}$",
        text,
    ):
        status = "accepted"
    else:
        return False
    candidate["status"] = status
    candidate["response"] = text[:120]
    candidate["updated_at"] = float(now)
    _save(path, data)
    return True


def context(path, now=None, limit=6):
    data = load(path)
    active = [item for item in data["items"] if item.get("status") in ("unspoken", "shared", "accepted", "deferred")]
    active.sort(key=lambda item: item.get("updated_at", item.get("created_at", 0)))
    return {
        "尚未表达的愿望": [item for item in active if item.get("status") == "unspoken"][-limit:],
        "已经向用户提出的愿望": [item for item in active if item.get("status") in ("shared", "accepted", "deferred")][-limit:],
        "原则": "答应不等于实现；拒绝不等于关系恶化；不重复催促。",
    }
