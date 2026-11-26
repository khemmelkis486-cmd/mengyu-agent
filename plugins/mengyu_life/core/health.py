"""Low-frequency fictional illness state with an explicit recovery cooldown."""

from data.plugins.mengyu_contact import config

import json
import random
import re
import time
from datetime import datetime
from pathlib import Path

from data.plugins.mengyu_contact.state_write import write_json

PATH = config.path("health.json", plugin="mengyu_contact")
from data.plugins.mengyu_contact.local_clock import TZ
DEFAULT_MONTHLY_CHANCE = 0.08
RECOVERY_COOLDOWN_DAYS = 45

SYMPTOMS = {
    "cold": "着凉了，嗓子疼、鼻子不通，人有点发蔫",
    "fever": "低烧，头昏、身上发酸，只想窝着",
    "stomach": "胃不舒服，吃不下东西，犯恶心",
}


def _month_key(now):
    return datetime.fromtimestamp(float(now), TZ).strftime("%Y-%m")


def load() -> dict:
    if PATH.exists():
        try:
            value = json.loads(PATH.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {"sick": None}
        except (ValueError, OSError):
            pass
    return {"sick": None, "last_roll_month": "", "cooldown_until": 0}


def save(data: dict) -> None:
    write_json(PATH, data, "HealthStore")

def status(now=None) -> dict:
    """Return current fictional health and persist recovery exactly once."""
    now = time.time() if now is None else float(now)
    data = load()
    sick = data.get("sick")
    if sick and now < sick.get("until", 0):
        return {
            "sick": True,
            "note": sick.get("note", "有点不舒服"),
            "since": sick.get("since"),
            "until": sick.get("until"),
        }
    if sick:
        recovered_at = float(sick.get("until", now))
        data["last_recovery"] = {
            "kind": sick.get("kind"),
            "since": sick.get("since"),
            "until": recovered_at,
            "recovered_at": recovered_at,
        }
        data["last_illness_at"] = float(sick.get("since", recovered_at))
        data["cooldown_until"] = max(
            float(data.get("cooldown_until", 0)),
            recovered_at + RECOVERY_COOLDOWN_DAYS * 86400,
        )
        data["sick"] = None
        save(data)
    return {
        "sick": False,
        "note": "",
        "last_recovery": data.get("last_recovery"),
        "cooldown_until": data.get("cooldown_until", 0),
    }


def maybe_fall_ill(now=None, chance=DEFAULT_MONTHLY_CHANCE) -> dict:
    """Roll at most once per calendar month, with 45 days after recovery blocked.

    ``chance`` is a monthly probability, not a daily probability. The default
    is intentionally low; the background loop may call this every 30 seconds
    without multiplying the chance.
    """
    now = time.time() if now is None else float(now)
    current_status = status(now)
    if current_status.get("sick"):
        return current_status
    data = load()
    if float(data.get("cooldown_until", 0) or 0) > now:
        return current_status
    month = _month_key(now)
    if data.get("last_roll_month") == month:
        return current_status

    # Migrate the old daily-roll marker conservatively: never grant an extra
    # roll during the month in which the previous version last checked.
    legacy_day = str(data.get("last_roll_day") or "")
    if legacy_day[:7] == month:
        data["last_roll_month"] = month
        data.pop("last_roll_day", None)
        save(data)
        return current_status

    data["last_roll_month"] = month
    data.pop("last_roll_day", None)
    probability = max(0.0, min(0.20, float(chance)))
    if random.random() < probability:
        kind = random.choice(list(SYMPTOMS))
        days = random.randint(1, 2)
        data["sick"] = {
            "kind": kind,
            "note": SYMPTOMS[kind],
            "since": now,
            "until": now + days * 86400,
        }
        data["last_illness_at"] = now
        data["cooldown_until"] = now + days * 86400 + RECOVERY_COOLDOWN_DAYS * 86400
        save(data)
        return status(now)
    save(data)
    return current_status


def make_sick(kind="cold", days=2, now=None) -> dict:
    """Force a fictional illness for explicit tests/story beats only."""
    now = time.time() if now is None else float(now)
    days = max(1, min(2, int(days)))
    data = load()
    data["sick"] = {
        "kind": kind,
        "note": SYMPTOMS.get(kind, SYMPTOMS["cold"]),
        "since": now,
        "until": now + days * 86400,
    }
    data["last_illness_at"] = now
    data["cooldown_until"] = now + days * 86400 + RECOVERY_COOLDOWN_DAYS * 86400
    save(data)
    return status(now)


def current_context(value, health):
    """Remove expired illness descriptions from current background projections."""
    if health.get("sick"):
        return value
    if isinstance(value, dict):
        return {
            key: current_context(item, health)
            if key in (
                "reason", "detail", "feeling", "carryover", "activity", "thought",
                "mood", "current", "recent", "notes", "recorded_mood",
                "newer_experiences", "wish",
            ) else item
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [current_context(item, health) for item in value]
    if isinstance(value, str):
        return "".join(
            part for part in re.split(r"(?<=[，,。；;\n])", value)
            if not re.search(
                r"感冒|着凉|生病|病号|发烧|低烧|鼻塞|鼻子.{0,6}(?:堵|不通)|嗓子.{0,8}(?:疼|痛|不舒服|没.{0,3}好)|身体.{0,6}发蔫",
                part,
            )
        ).strip("，,。；; ")
    return value


def guidance(health):
    if health.get("sick"):
        return "\n【当前健康状态，优先于旧日常】仍在这次病程内：" + health.get(
            "note", "有点不舒服"
        )
    return (
        "\n【当前健康状态，优先于旧日常、旧心情与旧聊天】当前没有正在持续的疾病。"
        "过去的身体状态按活动记录的开始与结束时间判断。"
        "不因旧聊天仍说不舒服就自行延长病程，也不靠安排喝水、休息或撒娇把旧病延续下去。"
        "对方问近况时按当前状态回答。"
        "经期、普通疲倦和情绪分别按当前记录判断；健康正常是默认状态，不要为了增加剧情主动制造疾病。"
    )


def interaction_guidance(sick, phase, irritable=False):
    if sick and phase == "period":
        preference = "当前生病又在经期，按实际精神和感受说话。累了可以少说，想休息就休息。"
    elif sick:
        preference = "当前生病，按身体和心情选择平常聊天、自己做点事或安静休息。"
    elif phase == "period" and irritable:
        preference = "当前经期，今天确实烦躁、没耐心。可以少说一点或自己待着，不迁怒。"
    elif phase == "period":
        preference = "当前经期，按她实际的身体感受和心情聊天。"
    elif phase == "pre" and irritable:
        preference = "当前经前，今天确实烦躁、没耐心。可以直接表现出来，不必找一个原因。"
    elif phase == "pre":
        preference = "当前经前，按她眼下的实际感受聊天。"
    elif phase == "ovulation":
        preference = "当前排卵期，精力和兴致通常好一点，轻快些就好。"
    elif phase == "follicular":
        preference = "当前卵泡期，精力在回来，可以主动些。"
    else:
        return ""
    return (
        "\n【身体状态下的相处倾向】" + preference
        + "身体状态只是背景，不是每轮联系的理由。已经向他要过安慰、抱抱、亲亲或陪伴，尤其已经得到回应后，让这份亲近落下来，安心做自己的事；不要只是换个动作接着索取，也不把隔了几条消息当作需要重新要哄。平常的想法、兴趣和正在聊的事仍是相处内容。没有想说的可以不发，不硬换成生活汇报。他主动亲近时自然接住，不为降低主动撒娇频率而冷淡或拒绝。不因没及时回复而催促、加重症状或编他冷落你。自己的主见仍保留，明确忙、睡觉和别打扰仍尊重。"
    )
