"""Read-only evidence from domain stores and acknowledged transport segments."""

from collections import defaultdict
import json
from pathlib import Path
import re
import sqlite3

from data.plugins.mengyu_life.core.meals import meal_context
from .state_write import stable_id

MEDIA_LABELS = {
    "image": ("图", "图片", "照片", "自拍"),
    "meme": ("图", "图片", "表情包"),
    "voice": ("语音",),
    "file": ("文件", "海报", "封面", "稿子"),
    "video": ("视频",),
    "link": ("链接",),
}


def read_rows(root, file, key):
    try:
        data = json.loads((root / file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = data.get(key) if isinstance(data, dict) else None
    return [row for row in rows[-160:] if isinstance(row, dict)] if isinstance(rows, list) else []


def collect(root, state, projection, now, *, blocked=(), friends=None):
    root = Path(root)
    evidence = defaultdict(list)

    def add(kind, obj, at, source, record):
        if isinstance(at, (int, float)) and at <= now + 5 and record:
            evidence[kind].append({
                "object": obj, "at": at, "source": source,
                "evidence_id": stable_id(source, record),
            })

    if "activities.json" not in blocked:
        life = projection.get("mengyu") or {}
        for row in [life.get("activity"), *life.get("recent_completed", ()), *life.get("recently_paused", ())]:
            if not row:
                continue
            identity = row.get("activity_id")
            if row.get("returned_home"):
                add("activity_returned", row.get("return_origin") or row.get("destination"), row.get("completed_at"),
                    "activity", identity)
            if row.get("status") == "completed" and re.search(r"吃饭|用餐|吃(?:早|午|晚)饭|吃(?:早|午|晚)餐", str(row.get("name") or "")):
                add("meal_finished", row.get("name"), row.get("completed_at"), "activity", identity)
            for arrival in row.get("arrivals", ()):
                add("activity_visited", arrival.get("place"), arrival.get("at"), "activity", identity)
    if "events.json" not in blocked:
        for row in read_rows(root, "events.json", "events"):
            if row.get("kind") not in ("复诊", "看诊") or row.get("status") in ("voided", "cancelled"):
                continue
            if row.get("closed") and row.get("status") == "completed":
                add("visit_finished", row.get("kind"), row.get("completed_at") or row.get("closed_at"),
                    "event", row.get("id"))
            elif row.get("status") == "in_progress":
                add("visit_started", row.get("kind"), row.get("started_at") or row.get("updated_at"),
                    "event", row.get("id"))
    if "work.json" not in blocked:
        for row in read_rows(root, "work.json", "orders"):
            completion = row.get("completion_evidence")
            if row.get("stage") == "完成" and isinstance(completion, dict) and completion.get("artifact_id"):
                add("work_finished", row.get("brief"), row.get("completed_at"), "work", row.get("id"))
    if "gifts.json" not in blocked:
        gifts = read_rows(root, "gifts.json", "items")
        for row in gifts:
            if row.get("kind") not in ("food", "takeout", "delivery") and (
                row.get("lifecycle_status") or row.get("status")
            ) in ("received", "acknowledged", "experienced", "delivered", "given"):
                add("gift_received", row.get("item"), row.get("received_at"),
                    "gift", row.get("gift_id") or row.get("id"))
        meals = [row for row in gifts if row.get("id") and isinstance(row.get("at"), (int, float))
                 and isinstance(row.get("eta_minutes", 15), (int, float))]
        for row in meal_context(state, meals, now):
            record = row.get("order_id")
            if row.get("delivery_phase") == "delivered":
                add("meal_arrived", row.get("item"), row.get("arrives_at"), "meal", record)
            if row.get("meal_ended"):
                add("meal_finished", row.get("item"), (row.get("meal_end") or {}).get("at"),
                    "meal", record)
    if "state.json" not in blocked:
        for delivery_id, row in list((state.get("delivery_ledger") or {}).items())[-60:]:
            if not isinstance(row, dict):
                continue
            for segment in (row.get("segments") or {}).values():
                if not isinstance(segment, dict) or segment.get("status") != "confirmed":
                    continue
                for kind in segment.get("media", ()):
                    for label in MEDIA_LABELS.get(kind, ()):
                        add("output_sent", label, segment.get("at"), "transport", delivery_id)
    if friends is not None:
        try:
            social = friends.snapshot(now)
        except (OSError, sqlite3.Error, ValueError):
            social = {}
        for person in social.get("friends", ()):
            evidence["friend_profiles"].append({
                "friend_id": person["friend_id"],
                "names": [person["name"], *person["aliases"]],
            })
        for invite in social.get("invitations", ()):
            if invite["status"] in ("accepted", "scheduled") and invite["title"] and invite["updated_at"] <= now + 5:
                add("friend_booked", invite["title"], invite["updated_at"], "friends", invite["invite_id"])
                evidence["friend_booked"][-1].update(
                    friend_ids=[invite["friend_id"]], activity=invite["activity_type"],
                    scheduled_at=invite["scheduled_start_at"],
                )
        for meetup in social.get("meetups", ()):
            if meetup["status"] == "completed" and meetup["completed_at"] <= now:
                add("friend_met", meetup["title"], meetup["completed_at"], "friends", meetup["meetup_id"])
                evidence["friend_met"][-1].update(
                    friend_ids=meetup["friend_ids"], activity=meetup["activity_type"],
                )
    return evidence


def needs_domain_sources(text):
    return bool(re.search(
        r"复诊|看诊|就诊|医生|医院|诊所|检查|外卖|送达|餐|饭|吃完|收到|买|送|礼物|发|传|寄|图|照片|作品|工作|稿|海报|封面|交付|回来|到家|回家|去了|去过|约|见|聚餐|看展|电影|咖啡|散步|逛书店|逛街|聊",
        str(text or ""),
    ))


def medium(components):
    types = {"Image": "image", "Record": "voice", "File": "file", "Video": "video"}
    return tuple(sorted({
        types[type(part).__name__] for part in components if type(part).__name__ in types
    }))
