"""A small circle of friends she can meet and mention."""

from __future__ import annotations

from data.plugins.mengyu_contact import config

import json
import random
import time
from pathlib import Path

PATH = config.path("friends.json", plugin="mengyu_friends")

FRIENDS = (
    {"id": "lan", "name": "示例同学", "relation": "大学同学，住在附近，常一起吃饭"},
    {"id": "man", "name": "示例同事", "relation": "前同事，爱探店，消息很多"},
    {"id": "zhou", "name": "示例画友", "relation": "画手朋友，偶尔约看展"},
)


def load(path=PATH):
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {"friends": list(FRIENDS), "meetings": []}
    data.setdefault("friends", list(FRIENDS))
    data.setdefault("meetings", [])
    return data


def save(data, path=PATH):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def pick():
    return random.choice(load()["friends"])


def meet(name=None, path=PATH):
    data = load(path)
    data["meetings"] = (
        list(data["meetings"])
        + [
            {
                "name": name or pick()["name"],
                "at": time.time(),
            }
        ]
    )[-20:]
    save(data, path)
    return data["meetings"][-1]


def context(path=PATH):
    data = load(path)
    meetings = data.get("meetings") or []
    return {
        "朋友": [
            {"name": item["name"], "relation": item["relation"]}
            for item in data["friends"]
        ],
        "最近见面": meetings[-1] if meetings else None,
    }
