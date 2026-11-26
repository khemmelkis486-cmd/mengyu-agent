"""Persistent game interests, fictional play history, and evidence-backed discoveries."""

import json
import re
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from data.plugins.mengyu_contact.state_write import write_json

from data.plugins.mengyu_contact.local_clock import TZ
GENERIC_TITLES = {
    "游戏", "打游戏", "电脑游戏", "steam", "pc游戏", "独立游戏",
    "卡牌游戏", "肉鸽游戏", "动作游戏", "模拟经营",
}


def _load(path):
    path = Path(path)
    if not path.exists():
        return {"schema": 1, "sessions": [], "discoveries": [], "preferences": {}, "last_attempt": None}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Invalid game-life store")
    data.setdefault("schema", 1)
    data.setdefault("sessions", [])
    data.setdefault("discoveries", [])
    data.setdefault("preferences", {})
    data.setdefault("last_attempt", None)
    return data


def _save(path, data):
    write_json(path, data, "GameStore")

def clean_title(value):
    """Return a short, concrete game title or an empty string."""
    title = re.sub(r"\s+", " ", str(value or "")).strip().strip("《》【】[]()（） ")
    if not title or len(title) > 80 or title.casefold() in GENERIC_TITLES:
        return ""
    if re.fullmatch(r"[\W_]+", title):
        return ""
    return title


def infer_game_title(*parts):
    """Infer only an explicitly named title; never turn a generic 'game' into a title."""
    text = " ".join(str(part or "") for part in parts)
    match = re.search(r"《([^》]{1,80})》", text)
    if match:
        return clean_title(match.group(1))
    aliases = (
        ("杀戮尖塔", "杀戮尖塔"),
        ("Slay the Spire", "Slay the Spire"),
        ("星露谷物语", "星露谷物语"),
        ("Stardew Valley", "Stardew Valley"),
        ("哈迪斯", "哈迪斯"),
        ("Hades", "Hades"),
        ("空洞骑士", "空洞骑士"),
        ("Hollow Knight", "Hollow Knight"),
        ("死亡细胞", "死亡细胞"),
        ("Dead Cells", "Dead Cells"),
        ("小丑牌", "小丑牌"),
        ("Balatro", "Balatro"),
        ("潜水员戴夫", "潜水员戴夫"),
        ("Dave the Diver", "Dave the Diver"),
        ("蔚蓝", "蔚蓝"),
        ("Celeste", "Celeste"),
        ("饥荒", "饥荒"),
        ("Don't Starve", "Don't Starve"),
    )
    folded = text.casefold()
    for needle, label in aliases:
        if needle.casefold() in folded:
            return label
    return ""


def record_play(path, title, activity_id, at=None, genre="", status="planned"):
    """Record a fictional game session once, linked to a real saved daylife segment."""
    title = clean_title(title)
    activity_id = str(activity_id or "").strip()
    if not title or not activity_id:
        return False
    at = time.time() if at is None else float(at)
    data = _load(path)
    existing = next(
        (item for item in data["sessions"] if item.get("activity_id") == activity_id),
        None,
    )
    if existing:
        return False
    data["sessions"].append(
        {
            "id": activity_id,
            "activity_id": activity_id,
            "title": title,
            "genre": str(genre or "")[:50],
            "at": at,
            "status": status if status in ("planned", "active", "completed", "cancelled") else "planned",
            "source": "fictional_daylife",
        }
    )
    data["sessions"] = data["sessions"][-100:]
    _save(path, data)
    return True


def sync_sessions(path, segments, now=None):
    """Reconcile planned game sessions against their saved fictional daylife phases."""
    now = time.time() if now is None else float(now)
    data = _load(path)
    by_id = {
        item.get("id"): item
        for item in (segments or [])
        if isinstance(item, dict) and item.get("id")
    }
    changed = False
    known_ids = {item.get("activity_id") for item in data["sessions"]}
    # Backfill explicitly named games from v1.51.3 daylife records so the first
    # upgraded run already knows which titles were being repeated.
    for segment in by_id.values():
        identity = str(segment.get("id") or "")
        if not identity or identity in known_ids:
            continue
        title = clean_title(segment.get("game_title")) or infer_game_title(
            segment.get("reason"), segment.get("detail")
        )
        if not title:
            continue
        phases = [
            phase.get("at")
            for phase in segment.get("phases", [])
            if isinstance(phase, dict) and isinstance(phase.get("at"), (int, float))
        ]
        if not phases:
            continue
        if segment.get("cancelled_at"):
            status = "cancelled"
        else:
            status = "completed" if max(phases) <= now else (
                "active" if min(phases) <= now else "planned"
            )
        data["sessions"].append(
            {
                "id": identity,
                "activity_id": identity,
                "title": title,
                "genre": str(segment.get("game_genre") or "")[:50],
                "at": float(segment.get("created_at") or min(phases)),
                "status": status,
                "source": "backfilled_v1513_daylife",
            }
        )
        known_ids.add(identity)
        changed = True
    for session in data["sessions"]:
        segment = by_id.get(session.get("activity_id"))
        if not segment:
            continue
        if segment.get("cancelled_at"):
            status = "cancelled"
        else:
            phases = [
                phase.get("at")
                for phase in segment.get("phases", [])
                if isinstance(phase, dict) and isinstance(phase.get("at"), (int, float))
            ]
            if not phases:
                continue
            status = "completed" if max(phases) <= now else (
                "active" if min(phases) <= now else "planned"
            )
        if session.get("status") != status:
            session["status"] = status
            session["updated_at"] = now
            changed = True
    if changed:
        _save(path, data)
    return changed


def record_discovery(path, record, query="", now=None):
    """Save a game discovery only when it contains actual search citations."""
    now = time.time() if now is None else float(now)
    if not isinstance(record, dict) or record.get("kind") != "games":
        return False
    sources = [
        {
            "title": str(item.get("title") or "")[:180],
            "url": str(item.get("url") or "")[:500],
        }
        for item in (record.get("sources") or [])
        if isinstance(item, dict)
        and str(item.get("url") or "").startswith(("https://", "http://"))
    ][:6]
    if not sources:
        return False
    data = _load(path)
    query = str(query or record.get("query") or "")[:300]
    identity = (query.casefold(), tuple(item["url"] for item in sources))
    if any(
        (
            str(item.get("query", "")).casefold(),
            tuple(source.get("url", "") for source in item.get("sources", [])),
        ) == identity
        for item in data["discoveries"][-20:]
    ):
        data["last_attempt"] = {
            "query": query,
            "at": now,
            "status": "success",
            "error": "",
        }
        _save(path, data)
        return False
    data["last_attempt"] = {
        "query": query,
        "at": now,
        "status": "success",
        "error": "",
    }
    data["discoveries"].append(
        {
            "id": str(record.get("id") or int(now * 1000)),
            "title": str(record.get("title") or "游戏探索")[:160],
            "query": query,
            "at": now,
            "sources": sources,
            "summary": str(record.get("material") or "")[:2400],
        }
    )
    data["discoveries"] = data["discoveries"][-60:]
    _save(path, data)
    return True


def note_discovery_attempt(path, query, status="failed", error="", at=None):
    """Persist a bounded discovery attempt so unavailable search is not retried every tick."""
    at = time.time() if at is None else float(at)
    data = _load(path)
    data["last_attempt"] = {
        "query": str(query or "")[:300],
        "at": at,
        "status": status if status in ("success", "failed") else "failed",
        "error": str(error or "")[:80],
    }
    _save(path, data)
    return True


def context(path, segments=None, now=None):
    """Return recent play/discovery context and a grounded anti-repetition hint."""
    now = time.time() if now is None else float(now)
    data = _load(path)
    sessions = [
        item for item in data["sessions"]
        if isinstance(item.get("at"), (int, float)) and item["at"] <= now
    ]
    recent_7d = [item for item in sessions if now - item["at"] <= 7 * 86400]
    recent_14d = [item for item in sessions if now - item["at"] <= 14 * 86400]
    counts = Counter(item["title"].casefold() for item in recent_7d)
    display_counts = {}
    for item in recent_7d:
        display_counts[item["title"]] = counts[item["title"].casefold()]
    repeated = [
        title for title, count in display_counts.items() if count >= 3
    ]
    active = [
        {"title": item["title"], "status": item["status"], "genre": item.get("genre", "")}
        for item in sessions
        if item.get("status") == "active"
    ][-3:]
    discoveries = [
        item for item in data["discoveries"]
        if 0 <= now - item.get("at", 0) <= 30 * 86400
    ][-5:]
    last_attempt = data.get("last_attempt") or {}
    attempt_at = last_attempt.get("at", 0)
    retry_after = 7 * 86400 if last_attempt.get("status") == "success" else 3 * 86400
    discovery_due = not attempt_at or now - attempt_at >= retry_after
    recent_rows = [
        {
            "title": item["title"],
            "genre": item.get("genre", ""),
            "status": item.get("status", ""),
            "at": datetime.fromtimestamp(item["at"], TZ).strftime("%m-%d %H:%M"),
        }
        for item in recent_14d[-8:]
    ]
    return {
        "recent_play_sessions": recent_rows,
        "active_games": active,
        "games_played_counts_last_7d": display_counts,
        "titles_to_avoid_as_default": repeated,
        "recent_discoveries": [
            {
                "title": item["title"],
                "query": item.get("query", ""),
                "at": datetime.fromtimestamp(item["at"], TZ).strftime("%m-%d"),
                "sources": item.get("sources", [])[:3],
            }
            for item in discoveries
        ],
        "discovery_due": discovery_due,
        "last_discovery_attempt": {
            "at": datetime.fromtimestamp(attempt_at, TZ).strftime("%m-%d %H:%M") if attempt_at else None,
            "status": last_attempt.get("status"),
            "query": last_attempt.get("query", ""),
            "error": last_attempt.get("error", ""),
        },
        "rule": (
            "游戏游玩是角色内的虚构日常；游戏作品的客观信息、更新和发行日期必须来自真实联网来源。"
            "最近一周同一作品达到三次时，不要默认再选它，除非已有进行中的同一局/存档或确实明确想继续。"
            "不要为了轮换而随机换游戏；按自己的兴趣、最近玩过的类型和真实好奇选择。"
            "探索记录只证明检索过，不证明下载、购买或实际游玩。"
        ),
    }


GUIDANCE = """【游戏选择、角色归属与联网探索】
游戏日常分为“自己玩”和“联网了解”两件不同的事。自己玩时使用daylife，必须在game_title写出具体作品名，reason/detail写当前要玩哪一段、哪种玩法或目标；游戏过程是用户授权的虚构角色日常，不声称真的启动、购买、下载或有工具操作凭证。联网了解新作品、版本更新、发售时间、攻略或玩家评价时使用games，query写具体检索问题；games会调用实际公开网页搜索并保存来源，不是从静态知识猜测。搜索失败就如实保留失败，不编造资讯。不要每次玩游戏都搜索，也不要每次搜索后强制分享。
选择游戏要看“近期游戏记录与发现”：不要把《杀戮尖塔》设成默认游戏。若最近一周同一游戏已玩三次以上，除非当前有明确的进行中牌局/存档或自己确实想继续，否则这次选择其他作品或先探索新作品；换游戏要有自己的兴趣理由，不是随机轮换。可从熟悉类型向相邻类型探索，也可因疲惫选择轻松作品。若“游戏探索到期”为true且没有正在进行的游戏局，下一次空闲的自主规划优先做一次具体游戏探索：按自己的兴趣查一款新作品、喜欢类型的新作或常玩作品的近期更新，不要只抽随机商店推荐。一次成功探索后七天内不再安排例行搜索；搜索失败后三天内不重复轰炸，除非用户明确问到新的具体问题。若仍在一局游戏中，先正常玩完再探索。避开最近发现记录里已经查过的同一问题，除非有新的版本或具体新疑问。新游戏的客观玩法、平台、发布日期、更新内容须由联网资料支持；不确定时先查，不让用户替你搜索。
【游戏画面与选项归属】先判断谁是当前玩家，再判断谁拥有游戏画面、牌面、棋盘、背包和选项。梦雨在玩：这些内容在梦雨这边；对方说“我帮你选/我给你建议”，应由梦雨把自己的可见选项用文字列给他，让他建议，绝不能反过来要求对方发送他看不到的梦雨游戏画面。对方在玩：画面和选项在对方那边；梦雨想帮忙时，可以请他截图或描述选项。谁在玩不明确时先问清楚，不猜测、不交换主客体。 “我帮你选”表示说话者是建议者，“你帮我选”表示当前玩家请求建议；不能只根据“发给我看看”字面把屏幕所有权倒置。"""


async def discover_games(client, searcher, query, search_fallback=None, reader=None):
    """Search the public web for a concrete game question and read available sources.

    Search snippets are retained as snippets when source pages cannot be opened.
    The caller must not describe a snippet-only result as full-page reading.
    """
    query = str(query or "").strip()[:300]
    if not query:
        raise ValueError("Game discovery requires a concrete query")
    if search_fallback is None or reader is None:
        from data.plugins.mengyu_life.core.exploration import read_public, search_public
        search_fallback = search_fallback or search_public
        reader = reader or read_public

    results = []
    native_error = None
    if searcher:
        try:
            results = await searcher(client, query)
        except Exception as exc:  # noqa: BLE001 - fall back to public RSS search
            native_error = type(exc).__name__
    if not results:
        results = await search_fallback(client, query)
    if not results:
        raise ValueError("No sourced game search results")

    pages = []
    for item in results[:3]:
        url = str(item.get("url") or "")
        if not url.startswith(("https://", "http://")):
            continue
        try:
            page = await reader(client, url)
            if len(str(page.get("text") or "")) >= 100:
                pages.append(page)
        except Exception:  # noqa: BLE001 - one inaccessible source should not erase others
            continue
        if len(pages) >= 2:
            break

    if pages:
        material = (
            "真实联网检索问题：" + query
            + "\n以下是实际打开阅读的公开页面，不代表已经购买或游玩：\n"
            + "\n\n".join(
                str(page.get("title") or page.get("url") or "公开页面")
                + "\n" + str(page.get("text") or "")[:4200]
                for page in pages
            )
        )
        sources = [
            {"title": str(page.get("title") or "")[:180], "url": str(page.get("url") or "")[:500]}
            for page in pages
        ]
    else:
        material = (
            "真实联网搜索结果摘要，未能打开原文；不能声称读过全文、验证了玩法或确认发售状态：\n"
            + json.dumps(results[:6], ensure_ascii=False)
        )
        sources = [
            {
                "title": str(item.get("title") or "")[:180],
                "url": str(item.get("url") or "")[:500],
            }
            for item in results[:6]
            if str(item.get("url") or "").startswith(("https://", "http://"))
        ]
    if not sources:
        raise ValueError("Game search results contain no valid public sources")
    return {
        "query": query,
        "title": "游戏探索：" + query[:120],
        "material": material[:10000],
        "sources": sources,
        "retrieval": "native_web_search_or_public_search",
        "native_search_fallback": native_error,
        "pages_read": len(pages),
    }


def game_title_from_plan(plan):
    """Use an explicit game_title first, then only infer a clearly named title."""
    plan = plan if isinstance(plan, dict) else {}
    return clean_title(plan.get("game_title")) or infer_game_title(
        plan.get("reason"), plan.get("detail"), plan.get("wish"), plan.get("title")
    )
