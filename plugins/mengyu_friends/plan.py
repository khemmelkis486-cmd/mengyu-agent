"""Deterministic invitations and fictional meetup details."""

from datetime import datetime, time
from hashlib import blake2b

# Hours are local to the Life planner. Details describe the fictional activity.
ACTIVITIES = {
    "meal": ("吃饭", "附近的餐馆", "evening", 80, "聊了最近常点的菜", "下回试试另一家"),
    "exhibition": (
        "看展",
        "美术馆",
        "afternoon",
        85,
        "一起挑了各自喜欢的作品",
        "遇到合适的展再一起去",
    ),
    "shopping": (
        "逛街",
        "附近商场",
        "afternoon",
        80,
        "试了几件衣服，聊了配色",
        "下回再看看别的店",
    ),
    "cafe": (
        "喝咖啡",
        "街角咖啡馆",
        "afternoon",
        65,
        "聊了这几天各自忙的事",
        "有空再找家安静的小店",
    ),
    "walk": (
        "散步",
        "小区旁的公园",
        "afternoon",
        60,
        "边走边聊近况，绕了一圈",
        "天气好时再走走",
    ),
    "cinema": (
        "看电影",
        "附近的电影院",
        "evening",
        115,
        "出来后聊了电影的结尾",
        "有想看的片再约",
    ),
    "bookstore": (
        "逛书店",
        "社区书店",
        "afternoon",
        60,
        "互相推荐了最近翻过的书",
        "下回交换读书感想",
    ),
}
RELATIONS = {
    "classmate": "同学",
    "former_colleague": "前同事",
    "illustrator": "画手朋友",
    "neighbor": "邻居",
    "friend": "朋友",
}
PROFILES = {
    "lan": ("classmate", "nearby", 78, ("做饭", "散步"), ("meal", "walk"), 4, 12),
    "man": (
        "former_colleague",
        "same_city",
        61,
        ("探店", "穿搭"),
        ("cafe", "shopping"),
        6,
        16,
    ),
    "zhou": (
        "illustrator",
        "same_city",
        72,
        ("插画", "展览"),
        ("exhibition", "bookstore"),
        7,
        18,
    ),
}
OPEN_INVITES = ("invited", "candidate")


def select_activity(friend_id, target, activities, *, rain=False, tight=False):
    """Choose a stable activity suited to the friend and conditions.

    Args:
        friend_id: Profile key.
        target: Target plan date.
        activities: Profile activity choices.
        rain: Avoid outdoor walks.
        tight: Prefer free activities.

    Returns:
        Activity type.
    """
    choices = [
        kind
        for kind in activities
        if kind in ACTIVITIES and not (rain and kind == "walk")
    ]
    if tight:
        return "bookstore" if rain else "walk"
    if not choices:
        return "bookstore" if rain else "cafe"
    seed = blake2b(f"{friend_id}:{target}".encode(), digest_size=2).digest()
    return choices[int.from_bytes(seed, "big") % len(choices)]


def slot_times(target, activity, window):
    """Give a meetup a fixed slot in the local plan day.

    Args:
        target: A local date object.
        activity: Activity key.
        window: Afternoon, evening, or weekend.

    Returns:
        Start and end Unix timestamps.
    """
    hour, minute = (18, 30) if window == "evening" else (14, 0)
    start = datetime.combine(target, time(hour, minute)).timestamp()
    return start, start + ACTIVITIES[activity][3] * 60
