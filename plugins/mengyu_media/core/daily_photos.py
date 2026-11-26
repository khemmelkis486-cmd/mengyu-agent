"""Optional photos from the current life activity."""

import hashlib
import json

from data.plugins.mengyu_media.core.photo_activity import LIFE_PHOTO_ORIGIN, can_deliver


LIFE_GUIDANCE = """【自己的生活与随手分享】
在家也是自己的生活，不用等出门、工作交差、新闻或他来问才有东西可说。游戏、休闲、身边的小事、临时念头和无所事事都能自然发展，不轮流打卡，也不每做一件就汇报。前面的工作和夸奖已经聊完，就让它成为背景；想分享时看眼下真正关心的事，不反复说还得意、舍不得关预览或继续评价他。没想说的可以不发，不为有内容强行换活动。
想让他看看眼前时，自拍、手边物件、窗外、饭菜、路上偶遇的东西都可以成为随手照。在家、出门、路上都按自己的活动接着过。可以选照片、文字、表情或什么都不发。
"""

PHOTO_WISH_GUIDANCE = """
【生活里的随手拍】
如果眼下的活动里有东西想留在照片里，可以在规划JSON里附上photo_wish={"subject":"mengyu或scene","query":"想拍的具体画面","reason":"为什么想留一张或想给他看","caption":"可空的一句配文"}。想拍自己用mengyu，拍衣服、饭菜、小猫、窗外或路上看见的东西用scene。按下方当前活动和地点取景，日常画面可自然补充近处细节。保留主计划，继续原活动可以用kind=continue。不为拍照换行程或重复已有的图，不想拍就省略。照片生成并验图通过后，主动联系再决定什么时候分享。
"""


def parse_wish(value):
    if not isinstance(value, dict) or value.get("subject") not in ("mengyu", "scene"):
        return None
    query, reason, caption = (
        value.get("query"),
        value.get("reason"),
        value.get("caption", ""),
    )
    if (
        not isinstance(query, str)
        or not 10 <= len(query.strip()) <= 1800
        or not isinstance(reason, str)
        or not 1 <= len(reason.strip()) <= 200
        or not isinstance(caption, str)
        or len(caption) > 100
    ):
        return None
    return {
        "subject": value["subject"],
        "query": query.strip(),
        "reason": reason.strip(),
        "caption": caption.strip(),
    }


def wish_key(wish, binding):
    return hashlib.sha256(
        json.dumps(
            [binding["activity_id"], binding["phase_at"], wish["subject"], wish["query"]],
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


def home_context(current, records, now):
    """Keep a known home location after an activity/day ends, without old chores."""
    if current:
        if current.get("mode") != "home":
            return None
        if not current.get("completed"):
            return current
        return {
            "mode": "home",
            "completed": True,
            "destination": "虚构角色的住处",
            "note": "前一项在家活动已结束；不固定原房间、旧饭菜或旧衣着",
        }
    recent = [
        item
        for item in records
        if not item.get("cancelled_at")
        and 0 <= now - item.get("created_at", 0) <= 129600
    ]
    if not recent:
        return None
    latest = max(recent, key=lambda item: item.get("created_at", 0))
    if latest.get("mode") != "home":
        return None
    return {
        "mode": "home",
        "completed": True,
        "source": "last_known_home",
        "source_at": latest["created_at"],
        "destination": "虚构角色的住处",
        "note": "最近明确在家，没有新出门记录；只沿用家中地点，不表示仍在做昨天的事，不固定昨日房间或穿着。",
    }


def shareable_image(item, now):
    """Keep current life photos fresh while distinguishing creative artwork."""
    if item.get("shared_at") or item.get("delivery_blocked") or item.get("retired_at"):
        return False
    if item.get("kind") != "image":
        return True
    if item.get("image_mode") == "artwork":
        return True
    if item.get("sharing_origin") == LIFE_PHOTO_ORIGIN:
        return bool(item.get("photo_activity") and can_deliver(item, None, archived_at=now))
    environment = item.get("photo_environment")
    if environment:
        return 0 <= now - environment.get("captured_at", 0) <= 1800
    return item.get("subject") not in ("mengyu", "user", "scene")
