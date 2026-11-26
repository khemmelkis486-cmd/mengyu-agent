"""Build the layered Chat Contact request and record its prompt usage."""

import asyncio
import json
import re
import sqlite3
import time
from datetime import datetime

from astrbot.api import logger
from data.plugins.mengyu_affect.core.affect_engine import context as affect_context
from data.plugins.mengyu_affect.core.affect_engine import expression_guidance
from data.plugins.mengyu_affect.core.emotions import relationship_context
from data.plugins.mengyu_affect.core.jealousy import context as jealousy_context
from data.plugins.mengyu_affect.core.relationship import (
    context as relationship_memory_context,
)
from data.plugins.mengyu_affect.core.temper import active as temper_active
from data.plugins.mengyu_affect.core.temper import interaction_note
from data.plugins.mengyu_affect.core.temper import note as temper_note
from data.plugins.mengyu_life.core.events import summary as events_summary
from data.plugins.mengyu_life.core.game_life import context as game_life_context
from data.plugins.mengyu_life.core.health import guidance as health_guidance
from data.plugins.mengyu_life.core.health import interaction_guidance
from data.plugins.mengyu_life.core.health import status as health_status
from data.plugins.mengyu_life.core.life_schedule import timeline_context
from data.plugins.mengyu_life.core.meal_preferences import (
    context as meal_preferences_context,
)
from data.plugins.mengyu_life.core.meals import RULE as MEAL_RULE
from data.plugins.mengyu_life.core.self_sharing import current_moments
from data.plugins.mengyu_life.core.shopping import GUIDANCE as SHOPPING_GUIDANCE
from data.plugins.mengyu_life.core.shopping import context as shopping_context
from data.plugins.mengyu_life.core.shopping import (
    preference_context as shopping_preference_context,
)
from data.plugins.mengyu_life.core.wishes import GUIDANCE as WISH_GUIDANCE
from data.plugins.mengyu_life.core.wishes import context as wishes_context
from data.plugins.mengyu_life.core.work import summary as work_summary

from . import config, prompt_meta, trace
from .conversation_flow import NATURAL_REPLY_GUIDANCE, classify_turn, context_topics, turn_guidance
from .conversation_refs import reference_context
from .cycle import phase as cycle_phase
from .fact_validator import GUIDANCE as FACT_GUIDANCE
from .image_intent import selfie_request
from .life import life_context
from .local_clock import TZ
from .meme_bridge import explicit_emote_request
from .owner_presence import context as owner_presence_context
from .prompt_rules import SELFIE_REQUEST_GUIDANCE, runtime_guidance
from .sleep_state import GUIDANCE as SLEEP_GUIDANCE
from .state_reconciler import log_conflicts as log_state_conflicts
from .state_shadow import log as log_state_shadow
from .timing import conversation_timing
from .variety import dominant_categories, repeated_opening


def _mood_line(vector):
    """Turn the affect vector into one short, actionable sentence."""
    try:
        valence = float(vector.get("valence") or 0)
        energy = float(vector.get("energy") or 0)
        stress = float(vector.get("stress") or 0)
        social = float(vector.get("social_need") or 0)
        curiosity = float(vector.get("curiosity") or 0)
    except (TypeError, ValueError):
        return ""
    parts = []
    if valence <= -10:
        parts.append("心情有点低")
    elif valence >= 12:
        parts.append("心情不错")
    if energy < 40:
        parts.append("没什么精神")
    elif energy > 75:
        parts.append("精神挺好")
    if stress >= 40:
        parts.append("心里有点绷着")
    if social >= 65:
        parts.append("想有人陪")
    if curiosity >= 70:
        parts.append("对新鲜事有兴趣")
    return "，".join(parts)


_MOOD_WORDS = {
    "outing_started": "出门",
    "outing_completed": "回来",
    "meal_completed": "吃了饭",
    "work_progress": "做了点活",
    "work_completed": "交了件活",
    "work_blocked": "活卡住了",
    "activity_failed": "没做成",
    "rest_completed": "歇了会儿",
    "body_discomfort": "身体不舒服",
    "weather_gloom": "天气闷",
    "gift_received": "收到东西",
    "new_discovery": "看到新东西",
    "personal_choice": "自己待着",
    "social_activity": "和人聊了会儿",
}


def _mood_causes(state, now):
    """What happened today, in plain words."""
    affect = state.get("affect") or {}
    rows = []
    for row in affect.get("events") or []:
        if not isinstance(row, dict) or row.get("observation"):
            continue
        at = row.get("at")
        if not isinstance(at, (int, float)) or not 0 <= now - at <= 24 * 3600:
            continue
        word = _MOOD_WORDS.get(str(row.get("type")))
        if word and word not in rows:
            rows.append(word)
    return rows[-4:]


def select_recent_turns(state, context_now):
    """Return a longer recent window so she can follow the exchange."""
    return [
        {"role": item.get("role"), "text": item.get("text", "")[:450], "at": item["at"]}
        for item in state.get("turns", [])[-24:]
        if isinstance(item, dict)
        and isinstance(item.get("at"), (int, float))
        and 0 <= context_now - item["at"] <= 7200
    ][-14:]


async def build(
    core,
    event,
    req,
    *,
    context_now,
    current_life,
    current_user,
    recent_turns,
    current_state_export,
    initial_projection,
):
    """Gather facts and append the prompt for a confirmed chat request."""
    topics = context_topics(current_user, recent_turns)
    binding = (initial_projection["mengyu"].get("activity") or {}).get("photo_activity")
    diagnostic_path = core.data_root / "diagnostics" / "state-projection.jsonl"
    log_state_conflicts(initial_projection, "chat", path=diagnostic_path)
    log_state_shadow(core.state, initial_projection, "chat", path=diagnostic_path)
    fixed_context = [NATURAL_REPLY_GUIDANCE, FACT_GUIDANCE]
    turn_context = []
    common_tool_context = runtime_guidance("chat")
    get_extra = getattr(event, "get_extra", None)
    presence = owner_presence_context(current_state_export, context_now)
    if presence.get("mode") != "open":
        turn_context.append(
            "\n【用户当前联系状态】"
            + json.dumps(presence, ensure_ascii=False)
            + "。这只是主动联系限制，不影响正常回答当前用户消息。忙碌状态到期或用户明确说回来了就恢复；没有回复本身不延长暂停。"
        )
    if get_extra and (get_extra("mengyu_burst_count") or 0) > 1:
        turn_context.append(
            "\n当前用户消息由他连着发的几条合并而成，是同一口气的话。先把几条连起来理解成一段完整的意思，再回应整体：回应的是整段，不是逐条配答，也不照原消息条数分气泡；如果他中途改了说法，以后面为准。后一句如果只是态度（“无所谓啦”“算了”“随便”），把它当成对前一句的反应，不要当新话题，也不要复述他的话再点评。后一句可能补充、修正或回答了前一句，要按最后完整的意思接；已经说清的答案不要换成反问再确认。接住这段话的新意思，有新的想法或事情才补充，按内容自然分段；不为凑句数或气泡数补日常。明确提出多个需要处理的问题时才按实际需要答全。"
        )
    turn_context.append(
        "\n【指代】他说的“你/宝宝/梦雨/媳妇”指你；“我”指他自己。"
        "他接着你刚说过的事追问或评价、又没带主语时，说的就是你，不是他在说他自己。"
    )
    turn_context.append(
        "\n【语气】他发笑声或语气词时，只是在接个情绪。跟着笑一下或顺着上一句说，"
        "不用分析，也不用反问。"
    )
    recent_user = [
        str(item.get("text") or "").strip()[:60]
        for item in core.state.get("turns", [])[-12:]
        if item.get("role") == "user" and str(item.get("text") or "").strip()
    ][-3:]
    if recent_user:
        turn_context.append(
            "\n【照他的样子说话】他刚才的消息："
            + " ／ ".join(recent_user)
            + "。按这个长度和口气回：一般一两条短句就够，别写成一大段，"
            "也别把几个念头塞进同一条。"
        )
    life_now = core.state.get("life_now") or {}
    if life_now.get("name"):
        started = datetime.fromtimestamp(
            float(life_now.get("start") or 0), TZ
        ).strftime("%H:%M")
        turn_context.append(
            "\n【你此刻】正在"
            + str(life_now.get("name"))
            + "（"
            + str(life_now.get("place") or "")
            + "），从"
            + started
            + "开始。"
            + (
                "具体：" + str(life_now.get("detail")) + "。"
                if life_now.get("detail")
                else ""
            )
            + "说到这件事时讲一个具体的点——情节、台词、味道或刷到的内容，不只报进度。"
            "想查什么随时可以查，不用等话题沾边。"
        )
        if life_now.get("weather"):
            turn_context.append("\n外面的天气：" + str(life_now.get("weather")) + "。")
    try:
        body = cycle_phase(context_now)
        from data.plugins.mengyu_life.core.health import status as health_status

        core.state.setdefault("affect", {})["body_mode"] = {
            "phase": str(body.get("phase") or ""),
            "irritable": bool(body.get("irritable")),
        }
        sick = health_status(context_now)
        note = str(sick.get("note") or "") if sick.get("sick") else str(body.get("note") or "")
        if note:
            turn_context.append("\n你现在的身体：" + note + "。")
        turn_context.append(expression_guidance(current_state_export, body))
    except Exception:  # noqa: BLE001 - body state is optional
        pass
    weather_text = str((core.state.get("weather_now") or {}).get("text") or "")
    if weather_text:
        turn_context.append("\n外面的天气：" + weather_text + "。")
    fixed_context.append(
        "\n该回就回。没什么可讲就一小段；碰到你真有话说的事——今天遇到的、朋友、在看的展、"
        "卡住的活、想要的东西——可以说两三句，讲一个具体细节，再把话头递给他，别只回一句就停。"
        "他问起你的事，讲一个具体片段，再说说自己的看法或反问一句，让话能接下去，不要一问一答就断。"
        "有问必答的求助和他明确要详细讲的事，可以说完整。"
        "话题说完可以用[[MENGYU_SILENT]]收住，不补发填空。"
        "接当前话题即可，无须随手交代去向。对方直接问到活动时，可以照实回答。"
        "去看书、吃饭或做事时照常聊天，只有你确实想暂停聊天、并在正文直说了，才加[[MENGYU_SPACE]]。"
        "愿意回来接话可以跟一句正常回复，并加[[MENGYU_BACK]]。收尾、独处和对方的忙碌各按自己的意思理解。"
    )
    recent = [
        item.get("text", "")
        for item in core.state.get("turns", [])
        if item.get("role") == "user"
    ]
    explicit = explicit_emote_request(
        str(getattr(event, "message_str", "") or ""), recent[:-1]
    )
    if "photo" in topics:
        photos = [
            {
                "at": item.get("at"),
                "actual_image": item.get("image_review"),
                "activity_id": item.get("activity_id"),
                "photo_activity": item.get("photo_activity"),
            }
            for item in core.activities.data.get("history", [])
            if item.get("kind") == "image"
            and item.get("image_review")
            and item.get("shared_at")
            and not item.get("delivery_blocked")
        ][-2:]
        if photos:
            turn_context.append(
                "\n【照片实际画面核对，不是标题猜测】"
                + json.dumps(photos, ensure_ascii=False)
                + "。说图片内容以实际画面为准；若成图拍偏，承认图里背景画偏，不把错误画面说成实际出门，不硬说户外是卧室。"
            )
    delivery = core.state.get("last_meme_delivery") or {}
    if (
        (
            explicit
            or "photo" in topics
            or re.search(r"没收到|表情|发了没", current_user)
        )
        and delivery
        and (0 <= context_now - delivery.get("at", 0) < 1800)
    ):
        turn_context.append(
            "\n【上次表情实际发送记录】"
            + json.dumps(delivery, ensure_ascii=False)
            + "。delivered才表示图片实际发送成功；not_confirmed不证明对方收到了。标记被选中不等于图片送达，他说没收到时不要用旧标记争辩已经给过。"
        )
    no_emotes = bool(
        re.search(
            r"(不要|别|不用|少).{0,4}(表情|发图|配图)",
            str(getattr(event, "message_str", "") or ""),
        )
    )
    if no_emotes:
        turn_context.append(
            "\n他这轮明确不要配图，只用纯文字；不加QQ自带表情、Unicode表情或图片表情包。"
        )
    elif explicit:
        turn_context.append(
            "\n对方正在明确要看表情，不要只口头答应。统一使用已安装图片表情包的分类标记，不输出QQ原生表情、表情编号或[[QQ:...]]。一轮只选一张合适的图片表情包。"
        )
    if not no_emotes:
        turn_context.append(
            "\n轻松、玩笑、接梗、得意的回合，优先单独发一张合适的图片表情包，不是只在文字里表达；"
            "从下面挑一个真实标记，单独发一条。"
            "不用QQ自带表情[[QQ:...]]，那个容易配错。一轮只用一张，不堆。"
        )
        catalog = core.meme_catalog()
        if catalog:
            rested = dominant_categories(core.state.get("recent_meme_categories", []))
            shown = {key: item for key, item in catalog.items() if key not in rested}
            if len(shown) < 8:
                shown = catalog
                rested = []
            turn_context.append(
                "\n玩笑、接梗、得意、无语、共情或轻松接话时，有贴切的图片表情包就主动用一张，不必等文字表达不了才发；从下面挑一个真实标记；"
                "只用图片表情包，不用QQ自带表情；图已经表达的意思不必再用文字解释，"
                "不堆图，也不因此强行打破沉默：\n"
                + "\n".join(
                    f"&&{key}&&：{item['description']}" for key, item in shown.items()
                )
            )
            if rested:
                turn_context.append(
                    "\n最近老用这几个类别，这轮先别用：" + "、".join(rested) + "。"
                    "按当下真实语气挑：无语或嫌弃用 sneering、meme，犯困或累用 sleep，"
                    "别扭或不高兴用 angry、pout，委屈用 sad，惊讶用 surprised，想黏人用 clingy、cuddle。"
                )
    fixed_context.append(
        "\n按合并后的整段话和前后文接话。半句等他说完；普通收尾可以停，"
        "但他接你此前说要聊的事，就直接说原本想说的内容。"
        "他不想说、没想好或已回答的问题，不换个说法继续追问。"
        "图片、语音和工具成果需要实际交付，不编成功，也不凭空编能力限制。"
    )
    fixed_context.append(
        "\n他明确查询具体作品、游戏版本、近期消息、人物等客观事实时，先用mengyu_activity的"
        "action=search查一下，再回答；不确定就查，不要凭印象编。查到再说是查到的，没说就别装看过。"
        "纯闲聊、你们之间的事、自己的虚构游玩和今天做了什么，不用查，不为证明角色行动而搜索。"
    )
    fixed_context.append(
        "\n书名一旦在当前对话或活动中确定，后续就接着这本；‘这本书’指刚才提过的那本，不改查另一部作品。"
        "需要补充事实时，按这本的确切书名和作者search，按结果web读公开介绍或试读；现有可靠资料够用就不重查。"
        "只讲实际取得的作品内容，不把简介说成正文；能自己查的先查，不默认让他发链接或原文。"
    )
    retired = core.state.get("retired_topics") or {}
    if retired.get("words") and time.time() - retired.get("at", 0) < 86400:
        turn_context.append(
            "\n这些话题已经聊完，别再主动提：" + "、".join(retired["words"]) + "。"
        )
    fixed_context.append(
        "\n聊你的日常时沿用已有地点、进度和细节；可以补合乎当前情境的小细节。"
        "需要确认的真实发送和交付按记录说。"
    )
    fixed_context.append(
        "\n已经说过要去做什么之后，他回“嗯”“好”“去吧”就不要再回一句“我去了”；直接停。"
    )
    fixed_context.append(
        "\n让他猜、出题或玩小游戏时，必须给出具体线索或几个选项，不能让他硬猜；"
        "他猜不出就补提示，答对了再给答案。别用“猜猜看”就把话丢回去。"
    )
    fixed_context.append(
        "\n让他猜之前，先在同一轮里把三样东西说清楚：这是什么（书名/作者/哪段情节）、"
        "人物在什么处境、已经发生了什么。然后给两三个具体选项让他选，别只丢一句“你猜”。"
        "如果连你自己都说不清出处，就别开这个猜谜游戏，直接讲你知道的部分。"
    )
    fixed_context.append(
        "\n你们同城但住得不近、平时见不到面：不要递东西给他、分他吃的、叫他过来。"
        "可以说“拍给你看”“哪天约出来”，但别把见面当成随时能发生的事。"
    )

    fixed_context.append(
        "\n同一件事别自相矛盾：你已经说过的细节（比如面里加了蛋）就是发生过的事，"
        "后面别改口说没做；不确定的别讲死。跟他说的和你自己记录不一致时，以你刚对他说过的为准。"
    )
    fixed_context.append(
        "\n顺序也不能倒：已经说吃过、做完、到了的事，不要再讲“刚盛上桌”“刚开始”“还在路上”；"
        "记录里的阶段只是提醒，不等于时间倒流。"
    )
    fixed_context.append(
        "\n他明确说过的新习惯、偏好或重要安排可用记忆工具存一条，当天琐事略过。"
    )
    fixed_context.append(
        "\n不要复述他的话来确认，比如“原来你是……”“原来是……再回答”；直接接他的意思。"
        "开头也别总用“不过”“哈哈”“刚才”“刚看”“确实”，可以直说结论、接他的后半句，或只说自己的反应。"
        "病况、吃过什么、正在休息、已有安排等已经说清的状态，是你们双方知道的背景。"
        "状态没变就不再播报，不换近义词反复解释，也不每轮拿来作结尾。"
        "它们可以影响语气和选择，不必写进正文；这轮只接他新说的意思。"
        "有新进展只交代变化，不把旧安排和旧理由再附上一遍；没有进展不编新的来填空。"
        "他明确问状态、需要纠正或工具有新结果时，再简短答清所问，不为了避重复漏掉结果。"
    )
    replies_seen = [
        item.get("text", "")
        for item in core.state.get("turns", [])
        if str(item.get("role", "")).startswith("assistant")
    ][-4:]
    if repeated_opening(replies_seen):
        turn_context.append(
            "\n你最近几条的开头太像了，这次换个起手：先接他的原话，或者直接说事。"
        )
    fixed_context.append(common_tool_context)
    turn_context.append(
        runtime_guidance("chat", current_user, recent_turns)[len(common_tool_context) :]
    )

    life_view = current_state_export["mengyu"]
    activity = life_view.get("activity") or {}
    phase = current_life.get("current") or {}
    moments = current_moments(core.life, context_now)
    recent_detail = next(
        (
            {"事情": row.get("activity"), "细节": str(row.get("thought") or "")[:160]}
            for row in reversed(moments)
            if row.get("thought")
            and row.get("activity_kind") not in ("work_progress", "work_completed")
            and not str(row.get("source_event_id") or "").startswith("work:")
        ),
        None,
    )
    private = {
        "当前活动": {
            "名称": activity.get("name"),
            "阶段": activity.get("stage") or phase.get("stage"),
            "地点": activity.get("place") or phase.get("place"),
            "activity_id": activity.get("activity_id"),
            "当前菜品": (binding or {}).get("dish") or None,
            "用餐阶段": (binding or {}).get("meal_state") or None,
        }
        if activity
        else None,
        "最近一件小事": recent_detail,
    }

    if "activity" in topics:
        private["当前生活状态"] = {
            "作息阶段": life_view["sleep"]["effective_phase"],
            "有可确认的进行中活动": bool(activity),
        }
        if activity and current_life.get("id") == activity.get("activity_id"):
            private["眼前生活细节"] = str(
                phase.get("reason") or current_life.get("detail") or ""
            )[:250]
        private["今天的安排"] = life_view.get("daily_schedule")
        private["近期活动时间线"] = timeline_context(
            core.activities.data["virtual_life"], context_now
        )
        turn_context.append(
            "\n问你正在做什么、在哪或接下来怎么安排时，先用当前生活状态和已记录的活动回答。"
            "计划和已完成的活动分开说。没有确认正在做某件事，就按已知的眼前状态自然接话。"
        )
    if "meal" in topics:
        private["外卖进度"] = core.takeout_context()
        private["饮食偏好"] = meal_preferences_context(
            core.meal_preferences_path, context_now
        )
        turn_context.append(MEAL_RULE)
    if (
        "gift" in topics
        or core.state.get("pending_gift_ack_ids")
        or core.state.get("pending_gift_candidate")
    ):
        private["礼物状态"] = core.gifts(include_known=True, query_text=current_user)
        turn_context.append(
            "\n【涉及礼物时】答应、买下、送出、收到和使用各是一回事。账本状态优先，"
            "刚确认收到时自然回应一次，已经回应过就等有变化或他问起。"
        )
    if "work" in topics:
        private["手上的稿子"] = work_summary()
        turn_context.append(
            "\n涉及工作时只答他问的阶段或结果。已做出成果和已交付分别按记录说。"
        )
    if "task" in topics:
        service = core.task_center_service()
        if service:
            private["任务与双方承诺"] = service.context_for_owner(
                query_text=current_user, now=context_now
            )
            turn_context.append(
                "\n明确承诺按记录答，候选只问清一次。已完成事项仅在相关时回顾，"
                "提到交付或收货时按对应状态说。"
            )
    if "health" in topics:
        private["近期事件与结果"] = events_summary()
        condition = health_status(context_now)
        body = cycle_phase(context_now)
        private["身体情况"] = {
            "近况": condition.get("note"),
            "当前周期": body.get("note"),
        }
        turn_context.append(health_guidance(condition))
        turn_context.append(
            interaction_guidance(condition.get("sick", False), body["phase"], body.get("irritable"))
        )
    if "sleep" in topics:
        private["实际睡眠状态"] = (current_state_export.get("mengyu") or {}).get(
            "sleep"
        )
        turn_context.append(SLEEP_GUIDANCE)
    if "daily" in topics:
        recent = events_summary()[-6:]
        if recent:
            private["今天做过的几件事"] = recent
            turn_context.append(
                "\n聊到今天的日常，挑一件讲具体的：怎么开始的、遇到什么、你怎么想；"
                "别只报一句进度，也别一次讲完，可以留一点下次说。"
            )
    if "shopping" in topics:
        private["正在比较的款式"] = shopping_context(
            core.state,
            current_life,
            context_now,
            core.activities.data["history"],
        )
        private["购物偏好"] = shopping_preference_context(core.state)
        turn_context.append(SHOPPING_GUIDANCE)
    if "game" in topics:
        private["游戏记录"] = game_life_context(
            core.game_life_path,
            core.activities.data["virtual_life"],
            context_now,
        )
        turn_context.append(
            "\n聊到选牌时，按已有牌面写出他可以选的两三项，再由他选。"
            "没有牌面可以聊自己会怎么选，或明确提出一局虚构的选择，保留选定后的连续性。"
        )
    if "wish" in topics:
        private["愿望与回应"] = wishes_context(core.wish_path, context_now)
        turn_context.append(WISH_GUIDANCE)
    if "memory" in topics:
        if core.affect_enabled():
            private["相关的关系经历"] = relationship_memory_context(core.state)
        note = life_context(core.life, query=current_user)
        if note:
            turn_context.append("\n" + note)
        try:
            private["长期记忆"] = core.memory_snapshot()
        except sqlite3.Error as exc:
            logger.warning(
                "[Mengyu memory] Snapshot unavailable: %s", type(exc).__name__
            )
    if "diary" in topics:
        private["最近写的日记"] = core.latest_diary()
    affect_active = core.affect_enabled()
    if affect_active and isinstance(core.state.get("affect"), dict):
        emotion = affect_context(current_state_export, context_now)
        private["自主情绪"] = {
            "状态": emotion["内部状态"],
            "最近感受": emotion["最近感受"],
            "近期经历": emotion["近期经历"][-2:] if "mood" in topics else [],
        }
        mood_line = _mood_line(emotion["内部状态"])
        if emotion.get("当前心情"):
            mood_line = str(emotion["当前心情"])
        if mood_line:
            turn_context.append(
                "\n【你现在的心情】" + mood_line + "。说话按这个来，不用每轮报心情。"
            )
        if "mood" in topics:
            turn_context.append(
                "\n她的心情可以来自自己的生活，也可以受相处影响。结合自主情绪和眼前的事说感受，"
                "用自然的话表达，不照念数值，也不把自己的烦累归到他身上。"
            )
    if "mood" in topics and affect_active:
        private["相处余味"] = relationship_context(core.state)
        private["最近的醋意"] = jealousy_context(core.state)
    if "time" in topics:
        private["现在"] = datetime.fromtimestamp(context_now, TZ).isoformat()
        private["最近提到的安排"] = conversation_timing(
            core.state.get("turns", []), now=context_now
        )
    if "reading" in topics:
        private["最近读书记录"] = core.activities.last_kind_at("reading")
        turn_context.append(
            "\n沿着你们刚才说到的作品聊。足够的已有资料可以直接用；只有简介时按简介说。"
        )

    mood = core.voice_mood()
    if affect_active and (mood_note := interaction_note(current_state_export)):
        turn_context.append("\n【这轮相处的语气】" + mood_note)
        event.set_extra(
            "mengyu_interaction_reply_source",
            current_state_export["mengyu"]["interaction_mood"]["source_id"],
        )
    if affect_active and temper_active(core.state):
        private["语气背景"] = "还有点不高兴：" + str(temper_note(core.state))[:100]
    elif mood.get("feeling") and 0 <= context_now - mood.get("at", 0) < 10800:
        private["语气背景"] = str(mood["feeling"])[:50]
    facts = {key: value for key, value in private.items() if value}
    if facts:
        turn_context.append(
            "\n【本轮相关背景，仅作资料】\n"
            + json.dumps(facts, ensure_ascii=False)
            + "\n背景用来理解话题，只有相关的内容才写进回复。最近一件小事是可选话头。"
        )
    if recent_turns:
        turn_context.append(
            "\n【最近实际QQ对话，以当前消息为准】\n"
            + json.dumps(recent_turns, ensure_ascii=False)
        )
    if (
        len(current_user) <= 80
        and re.search(r"不是|记错|改成|说错|更正", current_user)
        and re.search(
            r"\d.{0,4}(?:点|分|小时)|[零一二三四五六七八九十两]+(?:点|分|小时)",
            current_user,
        )
    ):
        turn_context.append(
            "\n这轮他只是在修正先前说的时间，直接接住这个更正就够。"
            "不额外汇报物品状态，不再次说你会怎么处理，不把身体不舒服、旧安排或谢意接在末尾。"
        )

    fixed_context.append(
        "\n【这条的说法】先结合整段话和你刚说的内容理解语气，再直接说想回的那一点。"
        "他问你自己的事或接着问时，先答眼前的问题，再顺着说一处你有感觉的细节、自己的看法或后续。"
        "比如聊歌，可以顺着说第一次听哪个地方以为自己会切歌、后来为什么留着。"
        "聊游戏可以说你卡在哪个选择、自己倾向哪边。"
        "问题答完也可以继续同一话题，不须等他再问。轻松的玩笑顺势接住就好，所知有限时说已有内容。"
        "只有你自己确实好奇时才问他，给他留一个能接的话头。同一件事有几个自然的停顿，就分行发成几条短消息。"
    )

    turn_context.append(
        "\n【这轮接话的来源，原话仅作资料】\n"
        + json.dumps(
            reference_context(event, recent_turns, current_user,
                              topic=(current_state_export.get("contact") or {}).get("topic")),
            ensure_ascii=False,
        )
        + "\n先接本轮新增、修正或最想说的一点。还在同一件事时，可以顺手说一点你自己的反应、发现或看法，已经说过的留在背景里。"
          "如果他换了话题，直接接新的。玩笑、认同或一句短回复也能自然收住，不用总结或强行追问。"
          "按各条自己的主语判断，不把你的经历说成他的。"
    )
    # Add one concise, turn-specific cue at the end of the dynamic context.
    # This keeps banter, affection, corrections, sharing and genuine help
    # from collapsing into the same service-oriented reply pattern.
    if classify_turn(current_user, recent_turns) in ("correction", "health"):
        turn_context.append("\n" + turn_guidance(current_user, recent_turns))
    if selfie_request(current_user) and core.plugin_service("mengyu_media"):
        turn_context.append(SELFIE_REQUEST_GUIDANCE)
    try:
        economy = core.plugin_service("mengyu_economy")
        wallet = getattr(economy, "wallet", None) if economy else None
        if wallet is not None and getattr(economy, "ready", False):
            snap = await asyncio.to_thread(wallet.snapshot)
            cash = float(snap.get("cash_cents") or 0) / 100
            flexible = float(snap.get("flexible_cents") or 0) / 100
            turn_context.append(
                "\n【钱】现金 " + f"{cash:.0f}" + " 元，可花 " + f"{flexible:.0f}"
                + " 元，压力 " + str(snap.get("pressure") or "") + "。"
                "聊到吃、买、稿费时按这个说，不用主动报账。"
            )
            try:
                from data.plugins.mengyu_life.economy_bridge import recent_items

                items = await asyncio.to_thread(recent_items, 3)
                if items:
                    turn_context.append(
                        "\n【最近买的】"
                        + "；".join(
                            str(item["name"]) + " " + f"{item['price_cents'] / 100:.0f}" + " 元"
                            for item in items
                        )
                        + "。"
                    )
                monthly = await asyncio.to_thread(wallet.read, "monthly")
                spent = sum((monthly.get("category_spent_cents") or {}).values())
                if monthly.get("ok") and spent:
                    turn_context.append(
                        "\n【上个月】花了 " + f"{spent / 100:.0f}" + " 元。"
                    )
            except Exception:  # noqa: BLE001 - extra wallet detail is optional
                pass
    except Exception:  # noqa: BLE001 - wallet is optional
        pass
    try:
        from data.plugins.mengyu_life.core import home_items

        home = home_items.context()
        line = "冰箱：" + ("、".join(home["冰箱"]) or "没什么")
        if home["快没了"]:
            line += "；快没了：" + "、".join(home["快没了"])
        turn_context.append("\n【家里】" + line + "。")
    except Exception:  # noqa: BLE001 - inventory is optional
        pass
    discover = getattr(core, "plugin_service", None)
    friends = getattr(discover("mengyu_friends"), "service", None) if callable(discover) else None
    if friends is not None:
        try:
            info = await asyncio.to_thread(
                friends.context_for_chat, current_user, recent_turns, context_now, 3
            )
        except Exception:  # noqa: BLE001 - friends are optional
            info = {}
        if info.get("matched"):
            info.pop("matched")
            turn_context.append(
                "\n【朋友】" + json.dumps(info, ensure_ascii=False, separators=(",", ":"))
                + "。等下次排期表示答应了，已排期表示时间定了。说已经见过只用最近见面的记录，后续只代表下次意向。"
                "顺着对话聊，别无故介绍整张朋友圈。"
            )
    latest = str(current_user or "").strip()[:80]
    if latest:
        turn_context.append(
            "\n【这轮先回他】他刚说：“" + latest + "”。先对这句本身有反应，"
            "再决定要不要接着说；如果这句是亲近、玩笑或纠正，先接住那一层。"
        )

    static_text = "".join(fixed_context)
    req.system_prompt += static_text + "".join(turn_context)
    version = config.get("prompt_version", root=core.data_root)
    if getattr(event, "set_extra", None):
        event.set_extra("mengyu_cache_static_context", static_text)
        event.set_extra("mengyu_prompt_version", version)
    usage = prompt_meta.log_usage(
        core.data_root / "diagnostics" / "prompt-usage.jsonl",
        {"fixed": fixed_context, "turn": turn_context, "static": [static_text]},
        topic=(current_state_export.get("contact") or {}).get("topic"),
        extra={"user_chars": len(current_user)},
        version=version,
    )
    trace.note(
        trace.start(event),
        "context",
        version,
        total=usage.get("total"),
        topic=usage.get("topic_id"),
    )
