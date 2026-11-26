"""Persistent fictional meal history and preference hints for varied, coherent choices."""

import json
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path

from data.plugins.mengyu_contact.state_write import write_json

from data.plugins.mengyu_contact.local_clock import TZ
_LOCK = threading.RLock()
GUIDANCE = """【饮食偏好与近期吃过的东西】饮食选择结合自己的口味、当下想吃什么、最近几天实际记录过的餐食和当前时间。不要总选同一道菜，也不要为了多样化强迫自己每顿换花样；喜欢的食物可以重复，但如果最近两顿同一道菜，或近三顿都集中在同一类别，优先考虑另一种自己也愿意吃的选择。可以吃米饭、面食、饺子锅贴、家常菜、汤粥、轻食、小吃或自己做的东西，不把这些当固定轮换表。订单买到不等于已经吃了，只有实际开始/完成的明确记录才进入已吃历史。已经买回来的晚饭不要无缘无故拖两三个小时才突然说刚开始吃；若有延误，要么说明已有的合理安排，要么决定留到下一顿。不要虚构现实下单或付款。"""


def _blank():
    return {"schema_version": 1, "meals": [], "preferences": {"likes": [], "avoids": []}}


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
        result = _blank()
        result.update(data)
        if not isinstance(result.get("meals"), list):
            result["meals"] = []
        if not isinstance(result.get("preferences"), dict):
            result["preferences"] = _blank()["preferences"]
        return result


def _save(path, data):
    write_json(path, data, "MealPreferences")

def category_for(name):
    text = str(name or "")
    for category, pattern in (
        ("面食", r"面|粉|馄饨|面条"),
        ("米饭", r"饭|盖饭|炒饭|焖饭|煲仔"),
        ("饺子小吃", r"锅贴|饺子|包子|馒头|烧卖|小笼"),
        ("家常菜", r"炒菜|家常|番茄|牛腩|鸡|鱼|肉|蛋|豆腐|排骨|虾"),
        ("汤粥", r"汤|粥|羹"),
        ("轻食", r"沙拉|三明治|酸奶|水果"),
    ):
        if re.search(pattern, text):
            return category
    return "其他"


def _normalise(name):
    value = re.sub(r"\s+", "", str(name or ""))
    value = re.sub(r"^(?:一份|一碗|一盒|一个|一盘)", "", value)
    value = re.sub(r"(?:一份|一碗|一盒|一个|一盘|的|了|啦|咯|呀|啊)$", "", value)
    return value.strip("，,。！？!?；; ")[:40]


def record_eaten(path, name, at, source="delivered_text", source_id=None):
    dish = _normalise(name)
    if len(dish) < 2:
        return False
    data = load(path)
    meals = data["meals"]
    if any(
        item.get("dish") == dish
        and abs(float(item.get("at", 0)) - float(at)) < 3 * 3600
        for item in meals[-10:]
    ):
        return False
    meals.append({
        "id": uuid.uuid4().hex,
        "dish": dish,
        "category": category_for(dish),
        "at": float(at),
        "status": "eaten",
        "source": source,
        "source_id": str(source_id or ""),
    })
    data["meals"] = meals[-120:]
    _save(path, data)
    return True


def infer_meal_from_activity(reason, detail=""):
    text = str(reason or "") + " " + str(detail or "")
    if re.search(r"买|点外卖|下单|取餐|送到", text) and not re.search(r"吃|做饭|煮|炒|蒸|烤", text):
        return None
    if not re.search(r"吃|做饭|煮|炒|蒸|烤|炖", text):
        return None
    patterns = (
        r"(?:吃|煮|做|炒|蒸|烤|炖)(?:了|上|一个|一份|一碗|一点|点)?\s*([\u4e00-\u9fffA-Za-z0-9]{2,16})",
        r"([\u4e00-\u9fffA-Za-z0-9]{2,16}(?:饭|面|锅贴|饺子|馄饨|包子|粥|汤|菜|蛋|豆腐|排骨|鸡翅|虾))",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            dish = _normalise(match.group(1))
            if len(dish) >= 2:
                return {"dish": dish, "category": category_for(dish)}
    if re.search(r"晚饭|午饭|早饭|早餐|午餐|晚餐", text):
        return {"dish": "家常餐", "category": "家常菜"}
    return None


def sync_completed_segments(path, segments, now):
    changed = False
    for segment in segments or []:
        if not isinstance(segment, dict) or segment.get("cancelled_at"):
            continue
        meal = segment.get("meal")
        phases = segment.get("phases") or []
        if not isinstance(meal, dict) or not phases:
            continue
        if meal.get("status") == "eaten" or phases[-1].get("at", now + 1) > now:
            continue
        changed = record_eaten(
            path,
            meal.get("dish", ""),
            phases[-1].get("at", now),
            source="completed-daylife",
            source_id=segment.get("id"),
        ) or changed
        meal["status"] = "eaten"
        meal["eaten_at"] = phases[-1].get("at", now)
        changed = True
    return changed


def record_from_text(path, text, at, source_id=None):
    """Record clearly stated first-person meal choices from delivered text only."""
    content = str(text or "")
    if re.search(r"假如|如果|比如|例如|打算|准备|想吃|下次|明天|你吃|他吃|她吃", content):
        return False
    patterns = (
        r"(?:我|今晚|今天|刚刚)?(?:刚|已经|今天|今晚)?(?:吃完了|吃好了|吃了|吃上了|吃上|做了|煮了|炒了|蒸了|烤了)(?:一份|一碗|一个|一点|点)?\s*([\u4e00-\u9fffA-Za-z0-9]{2,16}(?:饭|面|锅贴|饺子|馄饨|包子|粥|汤|菜|肉|鱼|蛋|豆腐|排骨|鸡翅|虾))",
        r"([\u4e00-\u9fffA-Za-z0-9]{2,16}?)(?:已经|刚刚|刚)?(?:吃完了|吃完啦|吃好了|吃了)",
        r"(?:晚饭|午饭|早饭|早餐|午餐|晚餐)(?:是|吃的)?\s*([\u4e00-\u9fffA-Za-z0-9]{2,16}(?:饭|面|锅贴|饺子|馄饨|包子|粥|汤|菜|蛋|豆腐|排骨|鸡翅|虾))",
    )
    changed = False
    data = load(path)
    preference_changed = False
    for clause in re.split(r"(?<=[，,。！？!?；;\n])", content):
        clause = clause.strip()
        if not clause:
            continue
        sentiment = "likes" if re.search(r"喜欢|爱吃|最爱|挺好吃|特别好吃|真香", clause) else None
        if re.search(r"不喜欢|不爱吃|吃腻|不吃|太难吃", clause):
            sentiment = "avoids"
        if sentiment:
            match = re.search(r"(?:喜欢|爱吃|最爱|好吃|不喜欢|不爱吃|吃腻|不吃|难吃)(?:吃)?\s*([\u4e00-\u9fffA-Za-z0-9]{2,16})", clause)
            if not match:
                match = re.search(r"([\u4e00-\u9fffA-Za-z0-9]{2,16}(?:饭|面|锅贴|饺子|馄饨|包子|粥|汤|菜|蛋|豆腐|排骨|鸡翅|虾))", clause)
            if match:
                dish = _normalise(match.group(1))
                if len(dish) >= 2:
                    bucket = data["preferences"].setdefault(sentiment, [])
                    if dish not in bucket:
                        bucket.append(dish)
                        data["preferences"][sentiment] = bucket[-30:]
                        preference_changed = True
    if preference_changed:
        _save(path, data)
        changed = True
    for pattern in patterns:
        for match in re.finditer(pattern, content):
            changed = record_eaten(path, match.group(1), at, "delivered_text", source_id) or changed
    return changed


def context(path, now, days=7):
    data = load(path)
    cutoff = now - days * 86400
    recent = [item for item in data["meals"] if item.get("status") == "eaten" and cutoff <= item.get("at", 0) <= now]
    recent.sort(key=lambda item: item.get("at", 0))
    counts = {}
    categories = {}
    for item in recent:
        counts[item["dish"]] = counts.get(item["dish"], 0) + 1
        categories[item["category"]] = categories.get(item["category"], 0) + 1
    return {
        "最近三餐": [
            {"dish": item["dish"], "category": item["category"], "at": datetime.fromtimestamp(item["at"], TZ).strftime("%m-%d %H:%M")}
            for item in recent[-3:]
        ],
        "最近吃过的餐食": [
            {"dish": item["dish"], "category": item["category"], "at": datetime.fromtimestamp(item["at"], TZ).strftime("%m-%d %H:%M")}
            for item in recent[-12:]
        ],
        "近期重复较多": [dish for dish, count in counts.items() if count >= 2],
        "近期类别分布": categories,
        "近期重复类别": [category for category, count in categories.items() if count >= 3],
        "口味偏好": data["preferences"],
    }
