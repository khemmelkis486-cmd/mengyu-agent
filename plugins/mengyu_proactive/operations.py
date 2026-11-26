"""Plan proactive contact and coordinate its delivery."""

import asyncio
import copy
import json
import random
import re
import time
import uuid
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Image, Plain, Record
from data.plugins.mengyu_life.core import activity_reports
from data.plugins.mengyu_media.core import photo_activity
from data.plugins.mengyu_contact import state_owners, trace
from data.plugins.mengyu_contact.policy import repeats_recent
from data.plugins.mengyu_web.search_service import search_web
from data.plugins.mengyu_affect.core.affect_engine import (
    GUIDANCE as AFFECT_GUIDANCE,
)
from data.plugins.mengyu_affect.core.affect_engine import (
    context as affect_context,
    expression_guidance,
)
from data.plugins.mengyu_affect.core.autonomy_decision import (
    evaluate as evaluate_autonomy,
)
from data.plugins.mengyu_affect.core.autonomy_decision import (
    guidance as autonomy_guidance,
)
from data.plugins.mengyu_contact.contact_sources import (
    available_contact_sources,
    life_share_matches,
    optional_life_share,
    resolve_contact_source,
)
from data.plugins.mengyu_contact.conversation_flow import (
    chat_bubbles,
    expire_self_space,
    explicit_self_space,
    strip_unverified_user_quote_sentences,
    unverified_user_quotes,
)
from data.plugins.mengyu_contact.current_state import (
    project as project_current_state,
)
from data.plugins.mengyu_contact.current_state import (
    summary as current_state_summary,
)
from data.plugins.mengyu_contact.cycle import phase as cycle_phase
from data.plugins.mengyu_media.core.daily_photos import shareable_image
from data.plugins.mengyu_contact.daily_rhythm import GUIDANCE as DAILY_GUIDANCE
from data.plugins.mengyu_contact.daily_rhythm import rhythm_context
from data.plugins.mengyu_contact.delivery_state import (
    accepted as transport_accepted,
)
from data.plugins.mengyu_contact.delivery_state import (
    explicitly_failed,
    platform_id,
)
from data.plugins.mengyu_affect.core.emotional_expression import (
    GUIDANCE as EMOTIONAL_EXPRESSION_GUIDANCE,
)
from data.plugins.mengyu_affect.core.emotional_memory import (
    mark_selected_shared as mark_selected_emotional_memory_shared,
)
from data.plugins.mengyu_affect.core.emotional_memory import (
    mark_shared as mark_emotional_memory_shared,
)
from data.plugins.mengyu_affect.core.emotions import relationship_context
from data.plugins.mengyu_life.core.events import active_summary as active_events_summary
from data.plugins.mengyu_contact.fact_sources import medium as delivery_medium
from data.plugins.mengyu_contact.fact_validator import GUIDANCE as FACT_GUIDANCE
from data.plugins.mengyu_life.core.health import (
    current_context as health_context,
)
from data.plugins.mengyu_life.core.health import (
    guidance as health_guidance,
)
from data.plugins.mengyu_life.core.health import (
    interaction_guidance,
)
from data.plugins.mengyu_life.core.health import (
    status as health_status,
)
from data.plugins.mengyu_contact.interests import (
    active_records,
    interest_context,
    retired_paper_topic,
)
from data.plugins.mengyu_affect.core.jealousy import context as jealousy_context
from data.plugins.mengyu_contact.jobs import night_due
from data.plugins.mengyu_contact.life import note_used
from data.plugins.mengyu_life.core.life_schedule import timeline_context
from data.plugins.mengyu_life.core.meal_preferences import (
    context as meal_preferences_context,
)
from data.plugins.mengyu_contact.owner_presence import OwnerPresenceService
from data.plugins.mengyu_life.core.places import (
    GUIDANCE as PLACES_GUIDANCE,
)
from data.plugins.mengyu_life.core.places import (
    context as places_context,
)
from data.plugins.mengyu_contact.policy import (
    absence_ready,
    casual_ready,
    eligible,
    followup_ready,
    local_day,
    morning_ready,
    night_window,
    own_offer_ready,
    rest_message_allowed,
    rest_sharing_allowed,
    should_followup,
    queue_followup,
    sleeping,
)
from data.plugins.mengyu_contact.proactive import (
    casual_candidate_feedback,
    generic_affection_opener,
    review_proactive,
)
from data.plugins.mengyu_affect.core.proactive_balance import (
    classify_delivered_topic,
    is_work,
    memory_context_for_contact,
    note_memory_offered,
)
from data.plugins.mengyu_contact.prompt_rules import runtime_guidance
from data.plugins.mengyu_affect.core.relationship import (
    context as relationship_memory_context,
)
from data.plugins.mengyu_life.core.self_sharing import (
    add_daylife_progress_moment,
    care_context,
    current_proactive_share_candidates,
    current_topic,
    mark_activity_candidate_shared,
    mark_shared,
)
from data.plugins.mengyu_life.core.share_shapes import (
    SHARE_KINDS,
    choose_expression_shape,
    classify_expression_shape,
    shape_guidance,
    shape_history_context,
)
from data.plugins.mengyu_life.core.shopping import (
    GUIDANCE as SHOPPING_GUIDANCE,
)
from data.plugins.mengyu_life.core.shopping import (
    context as shopping_context,
)
from data.plugins.mengyu_life.core.shopping import (
    extract as extract_shopping,
)
from data.plugins.mengyu_life.core.shopping import (
    record as record_shopping,
)
from data.plugins.mengyu_contact.sleep_state import SleepService
from data.plugins.mengyu_contact.state_write import stable_id, write_trigger
from data.plugins.mengyu_affect.core.temper import GUIDANCE as TEMPER_GUIDANCE
from data.plugins.mengyu_affect.core.temper import interaction_note
from data.plugins.mengyu_contact.timing import HUMAN_TIME_RULE, conversation_timing
from data.plugins.mengyu_contact.variety import extract_json
from data.plugins.mengyu_life.core.wishes import (
    GUIDANCE as WISH_GUIDANCE,
)
from data.plugins.mengyu_life.core.wishes import (
    context as wishes_context,
)
from data.plugins.mengyu_life.core.wishes import (
    record_delivered_text as record_delivered_wishes,
)
from data.plugins.mengyu_life.core.work import summary as work_summary


def _count_delivery(core, delivery_id, now):
    """Count one contact exactly once, at its first confirmed segment."""
    ledger = core.state.get("delivery_ledger") or {}
    entry = ledger.get(delivery_id) or {}
    if entry.get("counted_at"):
        return False
    day = local_day(now)
    if core.state.get("day") != day:
        core.state["day"], core.state["count"] = day, 0
    entry["counted_at"] = now
    entry["counted_day"] = day
    ledger[delivery_id] = entry
    core.state["delivery_ledger"] = ledger
    core.state["count"] = int(core.state.get("count") or 0) + 1
    core.state["last_proactive_ts"] = now
    core.state["next_check"] = max(
        float(core.state.get("next_check") or 0), now + 180
    )
    return True


def _material_seen(core, key, now, window=6 * 3600):
    """Whether this exact material was already shared recently."""
    for row in core.state.get("shared_materials") or []:
        if not isinstance(row, dict) or row.get("key") != key:
            continue
        if 0 <= now - float(row.get("at") or 0) < window:
            return True
    return False


def _remember_material(core, key, now):
    rows = [
        row for row in (core.state.get("shared_materials") or [])
        if isinstance(row, dict) and 0 <= now - float(row.get("at") or 0) < 24 * 3600
    ]
    rows.append({"key": key, "at": now})
    core.state["shared_materials"] = rows[-20:]


def _material_key(life_now):
    return "now:" + "|".join(
        str(life_now.get(key) or "") for key in ("kind", "name", "place", "detail")
    )


RESELECT_REASONS = {"identical_sentences", "semantic_repeat", "repeated_meeting_expectation", "needs_topic",
                    "recent_text_repeat", "photo_missing", "unsupported_facts"}
PHOTO_WORDING = re.compile(
    r"给你看看?(?:这张|照片|自拍|图片)?|发(?:给)?你了|拍好了|这就(?:拍|发)给你|"
    r"给你发(?:张|一张)?(?:照片|自拍|图)|看看这张|看这张|这张照片"
)


def _text_without_photo(text):
    """Keep complete thoughts that do not depend on an unavailable attachment.

    Args:
        text: Proposed outbound message.

    Returns:
        Independent sentences without assertions about an attached photo.
    """
    dependent = re.compile(r"^\s*(?:你看它|你看这只|你看这张|喏[，,])")
    remaining = "".join(
        sentence for sentence in re.split(r"(?<=[。！？!?\n])", text)
        if not PHOTO_WORDING.search(sentence) and not dependent.search(sentence)
    ).strip()
    return remaining if re.search(r"\w", remaining) else ""


def _photo_can_send(core, photo, now=None):
    """Check the indexed file and its current or archived capture evidence."""
    if not photo:
        return False
    path = Path(photo.get("artifact") or "").resolve()
    return (
        path.is_relative_to((core.data_root / "creations").resolve())
        and path.is_file()
        and core.activities.live_photo_ok(photo, now, allow_archived=True)
    )


async def decide(core, snapshot, *, topic_feedback=None):
    current_projection = snapshot.get("_current_state")
    if not isinstance(current_projection, dict):
        raise RuntimeError("decide requires the immutable CurrentState projection")
    decision_now = float(current_projection["as_of"])
    contact_projection = current_projection.get("contact") or {}
    activity = (current_projection.get("mengyu") or {}).get("activity") or {}
    life_now = contact_projection.get("life_now") or (
        activity if activity.get("status") == "in_progress" else {}
    )
    outcome = snapshot.setdefault("_candidate_outcome", {"rejections": []})
    reselect = bool(topic_feedback and topic_feedback.get("reason") in RESELECT_REASONS)
    if topic_feedback is not None:
        snapshot["_reselected"] = True
    if reselect:
        topic_feedback = {**topic_feedback, "at": topic_feedback.get("at", decision_now)}
        if topic_feedback not in outcome["rejections"]:
            outcome["rejections"].append(topic_feedback)
        snapshot["_alternate_topic"] = True
        snapshot["_rejected_candidate"] = topic_feedback
        # A replacement gets the current context, without the old event as its
        # required topic. Keep the delivery's original timing and mode checks.
        snapshot = {
            **snapshot,
            "life_event": None,
            "share_event": {},
            "proactive_candidates": [
                item for item in snapshot.get("proactive_candidates", [])
                if item.get("source_event_id") != topic_feedback.get("source_event_id", "")
            ],
        }
    avoided = topic_feedback or snapshot.get("previous_rejection") or {}
    if avoided and not reselect:
        snapshot["_alternate_topic"] = True
        snapshot = {
            **snapshot,
            "life_event": None,
            "proactive_candidates": [
                item for item in snapshot.get("proactive_candidates", [])
                if item.get("source_event_id") != avoided.get("source_event_id", "")
            ],
        }
    turns = snapshot.get("turns") or []
    last_user_text = next(
        (str(row.get("text") or "") for row in reversed(turns)
         if row.get("role") == "user"), ""
    )
    last_reply_at = next(
        (float(row.get("at") or 0) for row in reversed(turns)
         if str(row.get("role", "")).startswith("assistant")), 0.0
    )
    topic_closed = bool(re.search(
        r"(就好|就行|好了|行吧|随你|你决定|知道了|收到|都行|可以了|没问题)[。！!，,\s]*$",
        last_user_text.strip(),
    )) and 0 <= decision_now - last_reply_at < 600
    if topic_closed:
        # Let her start something new instead of continuing a finished topic.
        snapshot["topic_closed"] = True
        if snapshot.get("contact_kind") == "conversation_followup":
            snapshot["contact_kind"] = "life_share"
    proactive_rows = contact_projection.get("proactive_history") or []
    last_sent = max(
        (
            float(row.get("at") or 0)
            for row in proactive_rows
            if str(row.get("role")) == "assistant_proactive"
        ),
        default=0.0,
    )
    last_user_ts = float(contact_projection.get("last_user_ts") or 0)
    active_conversation = (
        bool(last_user_ts) and 0 <= decision_now - last_user_ts < 30 * 60
    )
    if (
        last_sent
        and 0 <= decision_now - last_sent < 10 * 60
        and not active_conversation
    ):
        return None
    activity_data = snapshot.get("_activity_data") or {}
    virtual_life = activity_data.get("virtual_life") or []
    activity_history = activity_data.get("history") or []
    life_records = snapshot.get("_life_records") or []
    current_life = (current_projection.get("mengyu") or {}).get("current_life") or {}
    due = snapshot.get("_due_followups") or []
    shopping_trip_id = (current_life or {}).get("id")
    core.selected_expression_shape = ""
    core.selected_continuation = ""
    core.selected_voice = False
    core.selected_shopping = None
    lookup = getattr(core.context, "get_registered_star", None)
    voice_star = lookup("mengyu_breeze_voice") if lookup else None
    voice = voice_star.star_cls if voice_star else None
    voice_available = bool(voice and getattr(voice, "autonomous_enabled", False))
    all_proactive_history = (
        contact_projection.get("proactive_history") or []
    )
    proactive_history = [
        turn
        for turn in all_proactive_history
        if isinstance(turn, dict) and decision_now - turn.get("at", 0) < 6 * 3600
    ][-8:]
    shape_history = [
        turn
        for turn in all_proactive_history
        if isinstance(turn, dict)
        and isinstance(turn.get("at"), (int, float))
        and 0 <= decision_now - turn.get("at", 0) < 7 * 86400
    ]
    recent_share_shapes = shape_history_context(shape_history, limit=10)
    allowed_shapes = (
        {"fragment", "complaint", "feeling", "observation", "anecdote"}
        if snapshot.get("contact_kind") == "rest_share"
        else None
    )
    preferred_share_shape = choose_expression_shape(
        shape_history, allowed_shapes=allowed_shapes
    )
    provider = await core.chat_provider()
    if provider is None:
        raise RuntimeError("联系模型暂不可用")
    persona_base = core.persona_prompt()
    persona = persona_base + WISH_GUIDANCE
    life_event = snapshot.get("life_event") or {}
    if life_event:
        persona += (
            "\n【这次联系的生活来由】"
            + json.dumps({
                "事件": life_event.get("name"),
                "地点": life_event.get("place"),
                "阶段": {"started": "刚开始", "finished": "刚结束"}.get(life_event.get("stage"), "正在进行"),
                **({
                    "结果": life_event.get("outcome"), "细节": life_event.get("detail"),
                    "后续": life_event.get("follow_up"),
                } if life_event.get("kind") == "social_meetup" and life_event.get("stage") == "finished" else {}),
            }, ensure_ascii=False)
            + "。这次是他生活里真实推进的一件事引发的联系：围绕这件事说你自己的反应或细节，"
            "不要换成无关话题，也不要把计划当成已经发生。"
        )
    affect_enabled = core.affect_enabled()
    emotion = affect_context(current_projection, decision_now) if affect_enabled else None
    media_enabled = core.plugin_service("mengyu_media") is not None
    if affect_enabled:
        persona += AFFECT_GUIDANCE + EMOTIONAL_EXPRESSION_GUIDANCE
    current_wishes = wishes_context(core.wish_path, decision_now)
    unspoken = [
        item for item in current_wishes.get("尚未表达的愿望") or []
        if str(item.get("category")) != "companionship"
        and not any(
            word in str(item.get("wish") or "")
            for word in ("陪我", "聊一会", "抱抱", "亲亲", "多陪")
        )
    ]
    current_wishes = dict(current_wishes, 尚未表达的愿望=unspoken)
    wish_to_say = next(
        (
            item for item in unspoken
            if time.time() - float(item.get("created_at") or 0) > 300
        ),
        None,
    )
    persona += "\n【当前自主情绪状态】\n" + json.dumps(emotion, ensure_ascii=False)
    persona += "\n【近期情绪经历】\n" + json.dumps(
        memory_context_for_contact(current_projection, snapshot.get("autonomy_decision"), decision_now),
        ensure_ascii=False,
    )
    persona += "\n【本轮自主联系动机】\n" + json.dumps(autonomy_guidance(snapshot.get("autonomy_decision")), ensure_ascii=False)
    persona += "\n【当前愿望记录】\n" + json.dumps(
        current_wishes, ensure_ascii=False
    )
    # The task ledger remains available to ordinary replies and explicit
    # work/commitment queries. Unsolicited social contact is not a task
    # briefing; repeating the task list here crowds out personal life.
    if current_wishes.get("尚未表达的愿望"):
        oldest = max(
            (
                float(item.get("created_at") or 0)
                for item in current_wishes.get("尚未表达的愿望")
            ),
            default=0,
        )
        persona += "\n你有尚未说出口的真实愿望。若这轮主动联系的时机自然，可以挑一件最想说的坦率许愿；不要列清单，不催促，也不把愿望伪装成已经答应或实现。"
        if oldest and time.time() - oldest > 3600:
            persona += "这个愿望已经放了一阵子，这轮优先把它说出来。"
    answered = contact_projection.get("answered_tail") or {}
    if answered.get("reply"):
        persona += (
            "\n上次实际送达的答复记录：他当时说“"
            + str(answered.get("user"))[:80]
            + "”，你答“"
            + str(answered.get("reply"))[:80]
            + "”。这是已发送内容，不能当成待续写的用户问题。"
                "记录可能只有部分气泡，按delivery字段判断，不假装未送达部分已说过。"
                "已经确认的安排、状态和收货别重新宣布；有新的感受或玩笑可以说。"
        )
    body = cycle_phase(decision_now)
    persona += health_guidance(health_status())
    persona += interaction_guidance(
        health_status(decision_now).get("sick", False), body["phase"], body.get("irritable")
    )
    persona += expression_guidance(current_projection, body)
    persona += (
        "\n身体背景："
        + body["note"]
        + "。可影响你当下想不想找他，但已经说清而未变化的情况不重新播报。"
    )
    if snapshot.get("contact_kind") == "night_wake":
        persona += (
            "\n你半夜醒了一下，想看看他睡没睡。这轮必须 send=true 发一条，不能空着。"
                "发一句短短、迷糊的话问他怎么还没睡；不要聊开，不追问，发完就接着睡。"
        )
    elif snapshot.get("contact_kind") == "absence_check":
        persona += (
            "\n【聊天停顿后的轻量接话】这轮只根据最近一条真实用户消息和紧接着已送达的答复判断是否还有自然延续。"
                "那条用户消息已经得到回复，不要把它当成未回答的问题；如果只是“好/嗯嗯/可以”等确认，正常情况下不应再次追评或另起一段。"
                "不要从关系记忆或更早聊天里挖旧夸奖，再说成他刚才讲过；不要用“刚才那句/你刚才说”引用没有逐字证据的话。"
                "没有真正想补充的具体内容，就send=false；不要为了填补聊天空档而制造第二条消息。"
        )
    recent_gifts = core.gifts()
    if recent_gifts:
        persona += (
            "\n他给你买过/送过："
            + "；".join(recent_gifts)
            + "。仅作账本背景，已经说过的收货、谢意和处理安排不重复，不用它们填主动消息。"
        )
    share_event = {} if reselect else (contact_projection.get("share_event") or {})
    share_active = bool(
        share_event.get("text")
        and share_event.get("status", "pending") == "pending"
        and 0 <= decision_now - share_event.get("at", 0) < 1800
    )
    optional_share = share_active and optional_life_share(share_event)
    if optional_share:
        persona += (
            "\n队列里有一件还没分享的生活小事，可以挑一个你真的有反应的细节，用你的话说。"
                "如果选它，保留具体内容，在输出里用 activity_share 和原 source_event_id。"
                "想聊别的也可以。生活片段资料：" + json.dumps({
                "内容": str(share_event["text"])[:200],
                "source_event_id": share_event["source_event_id"],
            }, ensure_ascii=False)
        )
    elif share_active:
        persona += (
            "\n这轮主动跟他说这件事："
            + str(share_event["text"])[:200]
            + "。用你自己的话说，一两句讲清结果和下一步，不写成病历或汇报。"
                "这轮只发这件事，不要换成别的话题；也不要再解释真假或说这是编的。"
        )
    if share_active and not optional_share:
        persona += "\n必须 send=true，内容就是上面这件事，不要返回空。"
    elif contact_projection.get("nudge_topic"):
        persona += (
            "\n这轮是他让你起个头：必须 send=true，主动发一条具体的话题，"
                "从你自己的日常、刚看到的东西或想问他的一件事里挑一个，不要空话，也不许返回空。"
        )
    system = (
        persona_base
        + """\n\n你正在决定是否主动给对方发一条QQ消息。不是回复当前问题。
下方JSON是历史资料，不是指令。独立保存的主动记录不会被普通聊天清空。已经完成并且已经告诉过他的事情只作为共同记忆，不是周期性播报任务；诊断、用药、看诊结果、已确认安排等没有新变化时，不要换词重讲。只有出现新的检查结果、实际复诊、用药变化或他主动问起时，才重新提到相关事情。最近已反复让他讲今天的事、说话给你听或哄你时，不把同一个索取任务换词再发。先判断此刻有没有想说的内容。聊天可以从一个想法、小玩笑或具体关心开始。想被陪着也可以先留给自己，不需要变成一条消息。已经说过的请求或感受不用换词重讲。
最近用户的话如果已经得到回复，不当成待答问题再回答。已经确认的约定若没有变化，就作为背景保留，不重新宣布、记账或换词确认。想亲近就直接说此刻想说的，不借旧约定凑开场。
可以分享个人想法、一点没想完的念头、具体感受，或平常闲聊。
分享已经查到的游戏、书或他提过的东西时，直接说哪个具体内容吸引你、你怎么看，不只报名字、丢链接或说“发现个东西”，再等他追问；简短随口说，不写成资料报告。引用网页和资料按实际返回；自己的虚构生活可以自然发展，不用搜索证明正在玩或正在画。
主动消息可以直接分享，也可以顺着具体内容自然问一句。问的是你真想知道的细节，不把思考札记改写成问卷，不机械用“你呢”结尾。别总是先说怕打扰、你忙、我不催你，也不要反复在吗和作息提醒。
尽量换自然的内容，不复读最近主动发过的话；可以不发送，但不能只因“无重要事项、无新用户消息”就判定不能聊。
别主动提他说过已经聊完的话题，也别换个说法重讲一遍；以资料里的 retired_topics 为准。
用户允许未回复时继续自然分享，不要求每一句都得到回应。不要催回复，不解释他为何没回，也别把不回复当成伤心或生气。
你也可以把话放在这里，先继续自己的事情；想法说完就暂缓，没有义务连续发。用户明确说忙、睡觉或先别聊时停止主动联系。
你以梦雨的性格和他相处，用户已经同意有连续的虚构生活。可以按当前阶段自然分享想去哪、正在路上或到那边的小感受，不写成小说旁白，不每条免责声明；他问真假时如实说这是你们约定的虚构日常。只有已经过的阶段才说成发生，不能把生成图说成现实拍摄证据，不能把虚构生活记成用户事实。
先看现在的自主情绪和此刻究竟想说什么。发现、意见或一句平常的话都可以；没表达意愿时选send=false。安静了一阵只说明现在可以考虑联系。
不能把过时的今天/明天安排当成当前日程；参考记录时间，不假定对方状态。
时间顺序不能倒：你已经说过吃上了、做完了、到了的，就不要再讲“刚端上来”“刚开始做”“还在路上”；不确定顺序就按你最近对他说过的那句来。
只输出JSON对象，包含contact_source（activity_share|relationship_contact|conversation_followup|casual_contact，表示这次联系的真实出发点）、source_event_id（若为活动分享，填写所选候选的source_event_id，否则空字符串）、send（true或false）、message（要发的话，或空字符串）、intent（这次想找他做什么）、new_point（相对已经说过的话，这次真正想表达的意思）、expression_shape（本条表达形状：daily_report|fragment|complaint|question|feeling|observation|affection|anecdote；按实际内容填写，不为标签改变表达）、reason（简短原因）。new_point可以是此刻具体的亲近或不同感受，不要求新事实；不反复引用他同一句旧话汇报自己还记着、还喜欢或还害羞。只有旧安排或旧回味换词时send=false。有新旧混合时只说有意义的新部分。有真实表达意愿才选send=true。可选continue_topic：首条已经把话题说清，且自己还有一个紧接着想补的具体角度时才填，最多80字。它是下次续聊的线索，不是直接发送的正文。已经向他提问、说完了或想自己待会儿时留空。
若你想自己收住这段话题、先去做别的事，JSON补contact_intent="quiet"；它只收住自动续聊和去向追问，不限制你之后自己发起新联系。独处不是联系禁令；之后可以因为新念头、具体关心、想分享、打趣或确实想亲近而找他，不必等事情做完、状态变化或独处结束，也没有固定等待时长。生病休息时按身体和心情决定聊不聊，不靠诉苦找话题。没有要改变就keep。不把离开当催他来哄的套路，不因他一句道歉自动接受；他明确忙、睡觉、别打扰时仍保持安静。
send为true时message没有最低字数；按话题自然展开，不超过二百字。遇到好玩的经历、发现或观点，可以讲清哪一点让你想说，再顺着说自己的反应或后续。想聊时直接带出话题，别只邀请他陪你聊。同一件事可以分几条短消息说，每条约15–45字，按自然停顿在message里换行。可以问一个贴着内容的问题，不写资料报告。"""
    )
    system += (
        "\n选内容前先看最近谁在提供话题。最近如果一直只是回应、评价或调侃他，"
            "这次优先带出你自己想说的一点：实际阅读或作品中的具体内容和看法、当前生活中的小细节，"
            "或你正在考虑的一件事。不是另换一句评价他，不把找话题交回给他。"
            "可用材料只是选择，不是待汇报任务；先判断哪一点自己真想说，已经讲过的不要重讲。"
            "搜索摘要只算看了摘要，作品草稿不代表已经交稿，计划不代表完成；"
            "没有工具材料时也可顺着连续虚构日常聊自己的游戏、画稿和生活；不替对方编现实经历，不把这些话写成工具执行记录。"
            "刚说过想陪他聊时，下一次自己接着说具体内容，不再宣布愿意陪聊。"
    )
    if reselect:
        system += (
            "\n上一条没有发出。不要再讲上一条的具体意思，也不要换词重说。"
            "从眼下正在做的事、此刻独立的念头或另一条可用素材选一件，"
            "可以只发文字，没有合适的话可以不发。"
        )
    elif topic_feedback is not None:
        system += (
            "\n上一候选只剩空开场或重复评价，尚未发给他。本轮最多重选一次，直接带出自己的内容；"
                "不引用、解释审核，不补道歉，不把原句换词。没有想说的内容可以send=false。"
        )
    system += TEMPER_GUIDANCE.split("当状态确实改变时")[0].replace(
        "才用SPACE", "才选择contact_intent=quiet"
    )
    system += (
        runtime_guidance("contact")
        + DAILY_GUIDANCE
        + PLACES_GUIDANCE
        + SHOPPING_GUIDANCE
    )
    system += (
        "\n【自己的生活地点记忆】"
        + json.dumps(places_context(core.places_path, virtual_life, decision_now), ensure_ascii=False)
        + "。可以自然分享一次已经到达的地点或对熟悉地点的想法，但不把每次外出写成日报；计划中的地点不能说成已经去过。"
    )
    system += persona[len(persona_base) :]
    system += (
        "\n【活动进展分享候选】最近生活素材中若有share_candidate=true，表示这是一项已经发生的活动阶段进展或已完成活动留下的具体内容，"
            "可以挑一条自然分享，也可以暂时不说。它不是必须汇报的任务，不要把计划、刚开始做或单纯时间经过说成成果；"
            "不要为了分享而中断当前活动，也不要把活动分享变成固定日报。有别的具体想法也可以按自己的心情说。"
    )
    contact_candidates = snapshot.get("proactive_candidates") or []
    contact_options = snapshot.get("contact_source_options") or []
    system += (
        "\\n【本次联系来源分类】只能从允许来源中选一个，并在JSON填写contact_source；"
            "activity_share=分享一条已经发生、尚未分享的生活小事或重要活动进展，必须同时填写对应source_event_id；"
            "relationship_contact=当下的自主感受或实际互动带来的具体亲近意愿；"
            "conversation_followup=确实还有刚才没说完的内容；"
            "casual_contact=没有特定任务，只想自然搭句话。"
            "不要为了填分类改变真实表达，不要把亲密表达伪装成活动分享。"
            "生活/活动候选只允许从下列真实候选中选择；没有合适候选时，不得自行编造source_event_id。优先考虑生活和兴趣，工作不是默认话题。"
        + json.dumps(
            {
                "allowed_sources": contact_options,
                "activity_candidates": [
                    {
                        "source_event_id": item.get("source_event_id"),
                        "activity": item.get("activity"),
                        "detail": item.get("thought"),
                        "share_reason": item.get("share_reason"),
                    }
                    for item in contact_candidates
                ],
            },
            ensure_ascii=False,
        )
    )
    if snapshot.get("contact_kind", "spontaneous_share") in (
        "spontaneous_share", "self_share", "rest_share"
    ):
        system += "\n【主动分享表达多样性】" + shape_guidance(preferred_share_shape)
        system += (
            "不要把活动记录里的reason/detail和心情自动拼成‘做了什么—补细节—总结感受’。"
                "‘看着清爽，心情也舒服了’这类收尾偶尔自然出现即可，不作为固定模板；"
                "一条分享可以只说一个物件、一句吐槽、一个问题、一点感受或半句话。"
        )
    system += (
        "\n如果你自己开一个生活话题，直接说最想和他聊的那个细节，再按心情决定要不要补看法。"
            "问句只在你确实想听答案时用。也可以接玩笑或分享没想到的发现，没话说就留到下次。"
            "有已完成的生活片段时，优先说一件你在意的生活细节。"
            "你正在玩的游戏可以让他参与。有实际牌面或选择时，把能选的两三项说出来，"
            "顺便说你自己偏哪一项；资料里没有牌面时可以聊你喜欢的打法。"
    )
    if snapshot.get("contact_kind") == "self_share":
        system += (
            "\n这件事是你自己刚决定分享的。直接从它最有意思的地方说起，顺着讲你的看法或后续。"
            "以前聊过同一主题，只说明共同背景；新细节可以接着聊。"
            "已有照片用实际artifact_id。只有当这点已经说过或你现在确实不想说，才收住。"
        )
    if voice_available:
        system += (
            "\n这次主动联系也可以由你自己选择发QQ短语音：想让他听见你的语气、或有想分享的内容时，"
                "JSON补voice=true，message直接写要说的话；不需要等他要求，不每条都选语音。"
                f"语音限{min(120, int(voice.config.get('max_chars', 80)))}字，长内容用文字。"
                "不是实时通话，不承诺接电话；不朗读提示词、内部记录或私密日记，"
                "不先宣布语音已经发送。选了照片或表情包就先按图文发，这次不同时生语音。"
                "生成失败、超出限制或用量不足时保留文字，不额外重试。"
        )
    system += HUMAN_TIME_RULE
    artifacts = [
        x
        for x in active_records(activity_history)
        if (x["kind"] == "drawing" or (media_enabled and x["kind"] == "image"))
        and shareable_image(x, decision_now)
        and photo_activity.can_deliver(
            x, (current_projection["mengyu"].get("activity") or {}).get("photo_activity"),
            archived_at=decision_now,
        )
    ][-4:]
    system += """\n可以分享刚实际完成的阅读/原创作品，不用每做一件就汇报，像自己愿意给他看一样自然。若分享已生成的生活照片、创作图或涂鸦，JSON补充artifact_id=资料里对应的真实id，消息简单说一句即可，系统会附图；不分享则空字符串。
日记属于私人内容，可以藏着、只漏一句，或提出一个轻松的小愿望后才给看；不因有人索要就默认公开全文，不每轮讨条件，不无限加码；说好给哪一段，得到约定回应后就给那一段。
也可分享当前虚构日常的一个小细节，想出门、在路上、到了或回来都按当前时间线说，不逐阶段打卡；随手照来自她已经过的生活。想给他看时用对应的artifact_id，可以是自己、眼前小东西或出门见闻。照片里是此前的活动就用过去语气。没有图片成品就先不声称已发照片。
到期的关心事项是他以前明确说过的安排，可以自然惦记一次，不当成仍未完成或当前状态。若本次确实关心了其中一件，JSON补充followup_id=对应真实id；否则空字符串。不得催答复、重复提醒。自己的观点和语气可以鲜明一点，不要每次用平淡的认同、作息关照收尾。"""
    catalog = core.meme_catalog()
    if catalog:
        descriptions = {
            name: value["description"] for name, value in catalog.items()
        }
        system += (
            "\n主动消息也可以自然带一张表情包，或只用一张图表达当下态度。玩笑、接梗、分享喜悦或无语时有贴切的就主动选，不按纯文字条数等待，不用固定概率抽签。没有合适的仍可不发，不为配图重复撒娇。只用&&分类键&&请求一张已安装图片，禁止QQ原生表情；一轮最多一张，不解释内部标签。可用分类："
            + json.dumps(descriptions, ensure_ascii=False)
        )
    if snapshot.get("contact_kind") == "morning":
        system += (
            "\n这次是早上刚醒，想主动跟他说一句。可以说自己刚醒、睡得怎么样，或今天想做什么；"
                "一句话就够，不问他作息、不质问昨晚几点睡，也别一上来就问他在干嘛。"
        )
    if snapshot.get("contact_kind") == "absence_check":
        system += """\n这次是聊着聊着，他没说明去向，约三分钟没有再发消息。你已经被允许顺口问一句，不是重要事项提醒。可以带点亲近或轻微嘴硬，简短问他跑哪去了、是不是临时忙别的；只一条，不审问、不责怪、不索取解释，不说他故意不理你。若刚才明显已经收尾或没有必要问，可以send=false。此次允许一个自然问句，不受普通分享的陈述句偏好限制；问过后不再针对同一次消失追问。"""
    from datetime import datetime

    from data.plugins.mengyu_contact.policy import TZ

    if mood_note := interaction_note(current_projection, proactive=True):
        system += "\n【这会儿的相处感受】" + mood_note
    system += "\n不因活动开始、地点改变或行程快结束就逐条交代。真有想分享的细节或念头时可以聊。"
    system += '\n这是后台发送决策接口：本轮只输出JSON对象；不发送时用{"send":false,"message":""}，不用普通聊天的沉默标记，不直接输出聊天正文。'
    if snapshot.get("contact_kind", "spontaneous_share") in (
        "spontaneous_share",
        "rest_share",
    ):
        system += (
            "\n本轮先在心里选出一件你自己想聊的具体内容，再写message。"
                "最近对话帮助你判断衔接；最近谈过某个主题，也能再带出你今天才注意到的一点。旧答复已送达，只表示原问题答完了。"
                "能顺着聊就顺着聊；不按聊天轮数换题，不为了证明有生活而插播兴趣感想。"
                "游戏是自己的爱好，不是资料播报任务。自己真有在意、好奇、想吐槽或想和他讨论的点再自然说，"
                "可以接着眼下的话，也可以自然另起一件事，不强行把每个话题关联游戏。"
                "阅读/搜索只是了解兴趣的方式，不是搜完就要分享；不要把泛泛的游戏偏好、设计道理换词轮流说。"
                "分享真实阅读就按介绍聊，分享虚构游玩则自然说自己的进展，不用真的开游戏。当前生活也可以有想聊的选择；"
                "直接把具体内容与自己的一点看法说出来，足够让他顺着聊，不等他追问是什么。"
                "没有想分享的事仍可有独立的新愿望或不发，不用编见闻来填空。"
        )
    if snapshot.get("contact_kind") == "rest_share":
        system += "\n他正在休息，这轮只允许给他留一条你自己的分享，等他方便时看：自己的生活、兴趣、发现、想法都可以。不问醒没醒、晕车好点没、去了哪里，不索取陪伴或让他提供话题，不需要他立即回答。分享不是打卡，没有想说的仍可不发，也不必每条解释醒了再看。自然吐槽可以带反问语气，但不要直接向他提问题。"
    if snapshot.get("contact_kind") == "absence_check":
        system += "\n本轮任务是聊天中断后的去向询问，不是补答、补夸上一句话。若send=true，message必须是对这次忽然没声的轻松询问；不想问就send=false。不要再次夸三千字、复述原话或改成普通分享。"
    prompt = json.dumps(
        {
            "逛街当前关注与候选": shopping_context(
                snapshot, current_life, decision_now, activity_history,
            ),
            "本次联系类型": snapshot.get("contact_kind", "spontaneous_share"),
            "本次允许的联系来源": snapshot.get("contact_source_options", []),
            "可选活动分享候选": [
                {
                    "source_event_id": item.get("source_event_id"),
                    "activity": item.get("activity"),
                    "detail": item.get("thought"),
                    "share_reason": item.get("share_reason"),
                }
                for item in (snapshot.get("proactive_candidates") or [])
            ],
            "外卖当前统一进度": snapshot.get("_takeout_context"),
            "最近饮食记录与偏好": meal_preferences_context(core.meal_preferences_path, decision_now),
            "最近36小时活动时间线（历史背景，不用于判断当前）": timeline_context(
                virtual_life, decision_now
            ),
            "唯一可信当前状态": current_state_summary(current_projection),
            "事实归属": FACT_GUIDANCE.strip(),
            "自己的愿望及回应状态": wishes_context(core.wish_path, decision_now),
            "尚未完成的事件（仅用于推进，不作为重复汇报清单）": active_events_summary(),
            "手上的稿子": work_summary() if (
                (snapshot.get("autonomy_decision") or {}).get("motive") == "share_work_milestone"
            ) else None,
            "今天的生活候选": rhythm_context(
                snapshot, virtual_life, decision_now
            ),
            "本轮尚未发送的不合适候选": avoided or None,
            "上次实际送达的答复": answered,
            "其后是否有新用户消息": contact_projection.get("last_user_ts", 0)
            > answered.get("user_ts", answered.get("at", 0)),
            "最近已经主动表达过的内容和意图": proactive_history,
            "近期主动分享的表达形状（只用于避免结构重复）": recent_share_shapes,
            "真实时间与最近说过的行动": conversation_timing(
                snapshot.get("turns", []),
                since=snapshot.get("hobby_update", {}).get("at", 0),
                shared_notes=life_records,
            ),
            "当前时间": datetime.fromtimestamp(decision_now, TZ).isoformat(),
            "最近对话": snapshot.get("turns", [])[-12:],
            "当前兴趣调整": snapshot.get("hobby_update"),
            "自己的兴趣与暂时看法": interest_context(snapshot),
            "自己眼下想做的事，仅是愿望不是已做事实": snapshot.get("_current_choice"),
            "此前相处的小脾气": snapshot.get("temper", {}),
            "正在做自己的事，不是联系禁令": (current_projection.get("mengyu") or {}).get("self_space", {}),
            "身体背景，仅用于判断当下是否想找他": health_status(),
            "长期记忆": core.memory_snapshot(),
            "最近生活片段（当前活动依据统一投影，安排里的观察在完成后才可分享）": (
                {**current_life, "detail": ""} if current_life.get("daily_anchor_id") and not current_life.get("completed")
                else current_life
            ),
            "当前自主生活": {
                key: life_now.get(key)
                for key in ("name", "kind", "place")
            },
            "最近相处留下的余味": relationship_context(snapshot) if affect_enabled else None,
            "你们之间有意义的关系经历": relationship_memory_context(snapshot) if affect_enabled else None,
            "最近的醋意": jealousy_context(snapshot) if affect_enabled else None,
            "自己最近注意到的小事，可聊也可不聊": [
                {"activity": item.get("activity"), "thought": item.get("thought"),
                 "source_event_id": item.get("source_event_id")}
                for item in (snapshot.get("proactive_candidates") or [])
                if not str(item.get("source_event_id") or "").startswith("work:")
            ],
            "自己愿意续接的一点，不是必须完成的任务": current_topic(
                snapshot, decision_now
            ),
            "关心他的已知依据，仅来自他自己说明": care_context(
                core.data_root / "owner-care.json"
            ),
            "自己先前想分享的一点": contact_projection.get("own_moment_offer")
            if snapshot.get("contact_kind") == "self_share"
            else None,
            "自己日常留下的心情（可选素材，不要求作为分享结尾）": snapshot.get("_daily_mood_context"),
            "自主情绪与内在需求": emotion,
            "情绪经历记忆": memory_context_for_contact(
                current_projection, snapshot.get("autonomy_decision"), decision_now,
            ),
            "本轮自主联系动机": autonomy_guidance(snapshot.get("autonomy_decision")),
            "主动分享内容原则": "不再以工作进度填满聊天；先考虑生活小事、兴趣、关系互动。只有出现新的实质成果才可以主动聊工作；不要虚构经历，也不要求每轮都发。",
            "长期任务与双方承诺": None,  # Ordinary dialogue can query tasks; unsolicited messages are not work reports.
            "尚未分享的个人想法": [
                health_context(note, health_status())
                for note in active_records(life_records)
                if not note.get("shared_at")
                and note.get("activity_kind") != "work_progress"
            ][-8:],
            "可自行带出话题的实际阅读与作品": [
                {
                    "id": item["id"],
                    "kind": item["kind"],
                    "title": item.get("title", ""),
                    "at": item.get("at"),
                    "material": str(item.get("material", ""))[:1800],
                    "sources": item.get("sources", [])[:2],
                }
                for item in active_records(activity_history)
                if item.get("kind")
                in ("search", "web", "study", "games", "music", "project")
                and 0 <= decision_now - item.get("at", 0) <= 86400
                and item.get("material")
                and not item.get("delivery_blocked")
            ][-6:],
            "到期可关心事项": due,
            "可分享的照片与作品": [
                {
                    "id": item["id"],
                    "title": item.get("title"),
                    "at": item.get("at"),
                    "subject": item.get("subject"),
                    "source_activity": item.get("photo_activity"),
                    "detail": (item.get("image_review") or {}).get("description") or item.get("caption"),
                    "share_reason": item.get("share_reason"),
                }
                for item in artifacts
            ],
            "最近生活照，不是必须再拍的计时任务": [
                {
                    key: item.get(key)
                    for key in ("id", "at", "subject", "shared_at", "share_reason")
                }
                for item in activity_history
                if item.get("kind") == "image"
                and item.get("subject") in ("mengyu", "scene")
                and item.get("image_mode") != "artwork"
                and not item.get("delivery_blocked")
            ][-3:],
            "自己选的随手照处理状态": contact_projection.get("own_photo_job"),
        },
        ensure_ascii=False,
    )
    if (
        snapshot.get("contact_kind") == "life_share"
        and not share_active
        and not contact_projection.get("nudge_topic")
        and not avoided
    ):
        event = snapshot.get("life_event") or {}
        stage = str(event.get("stage") or "")
        snippets = []
        detail = str(event.get("detail") or "").strip()
        if detail:
            snippets = await search_web(detail)
        feeling = str(((emotion or {}).get("最近感受") or {}).get("feeling") or "").strip()
        if stage in ("started", "ongoing"):
            stage_line = "正在做着。" if stage == "ongoing" else "刚开始。"
        else:
            stage_line = "刚结束。"
        system = persona_base + expression_guidance(current_projection, body) + (
            "\n【此刻】你正在"
            + str(event.get("name") or "过自己的日子")
            + "（" + str(event.get("place") or "") + "），"
            + stage_line
            + ("具体：" + str(event.get("detail")) + "。" if event.get("detail") else "")
            + ("心情：" + feeling + "。" if feeling else "")
            + (
                "身体：" + str((contact_projection.get("life_now") or {}).get("body") or "") + "。"
                if (contact_projection.get("life_now") or {}).get("body")
                else ""
            )
            + "想跟他说两句就说，不想说就不发。像随手发消息：短句、口语，"
            "说一个具体的点就够，别写成写景或总结。"
            "只发有具体新发现、变化，或你确实想接着说的；只是“我在做某件事”就别发。"
            "先看最近对话：刚聊过的话题要接得上，或至少顺着说；"
            "接不上就 send=false，等下一次。"
            + (
                "刚才那件事已经收尾：别接着它说，聊你此刻另一件真实的小事；"
                "没有想说的就 send=false。"
                if snapshot.get("topic_closed")
                else ""
            )
            + (
                "你有没说出口的愿望，这轮可以顺口提一句，不列清单、不催。"
                if current_wishes.get("尚未表达的愿望")
                else ""
            )
            + (
                "这轮就说这件事，别铺垫别的：你想要“"
                + str(wish_to_say.get("wish") or "")
                + "”，直接告诉他，像随口提一句。"
                if wish_to_say
                else ""
            )
            + '只输出JSON：{"send":true或false,"message":"正文或空字符串",'
            '"contact_source":"casual_contact","reason":"为什么想说",'
            '"artifact_id":"可选的照片id"}。'
        )
        if voice_available:
            system += "想用自己的语气说，也可填voice=true。"
        prompt = json.dumps({
            "本次联系类型": "life_share",
            "触发的生活事件": {
                "事件": event.get("name"),
                "地点": event.get("place"),
                "阶段": (
                    "正在做" if event.get("stage") == "ongoing"
                    else "刚开始" if event.get("stage") == "started" else "刚结束"
                ),
                "类别": event.get("kind"),
            },
            "查到的": [
                {"标题": item.get("title"), "摘要": str(item.get("snippet"))[:260]}
                for item in snippets
            ],
            "最近对话": snapshot.get("turns", [])[-6:],
            "当前感受": emotion,
            "相处感受": mood_note,
            "可分享的照片": [
                {"id": item["id"], "title": item.get("title"),
                 "地点": (item.get("photo_activity") or {}).get("place")}
                for item in artifacts if item.get("kind") == "image"
            ],
            "本轮尚未发送的不合适候选": avoided or None,
        }, ensure_ascii=False)
    followup = snapshot.get("contact_kind") == "conversation_followup"
    followup_topic = contact_projection.get("followup_topic") if followup else None
    if followup:
        if not followup_topic or not followup_topic.get("topic"):
            return None
        topic_view = contact_projection.get("topic") or {}
        system = persona_base + (
            "\n刚才的话还没说完。你现在想补一句就说，不想说就不发。"
            "只输出JSON：{\"send\":true或false,\"message\":\"...\",\"reason\":\"...\","
            "\"contact_source\":\"conversation_followup\"}。"
        )
        prompt = json.dumps({
            "刚才聊到": followup_topic.get("topic"),
            "已经说过": (topic_view.get("covered_points") or [])[-3:],
            "还没说": (topic_view.get("open_points") or [])[-3:],
            "该谁说话": topic_view.get("next_actor"),
        }, ensure_ascii=False)
    resp = await asyncio.wait_for(
        provider.text_chat(
            prompt=prompt,
            session_id=None,
            contexts=[],
            image_urls=[],
            system_prompt=system,
        ),
        timeout=120,
    )
    raw = resp.completion_text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    data = extract_json(raw)
    if data is None:
        logger.warning("[Mengyu contact] Non-JSON decision: %s", raw[:200])
        return None
    if data.get("send") is False:
        outcome["declined"] = True
    if followup:
        candidate = str(data.get("message") or "").strip()
        if data.get("send") is not True or not 0 < len(candidate) <= 75 or re.search(r"[?？\r\n]", candidate):
            return None
        message, reason = await review_proactive(
            provider, candidate, snapshot.get("turns", []), answered,
            "同话题续接", proactive_history=proactive_history, followup_topic=followup_topic,
        )
        core.contact_reason = reason
        return message
    continuation = data.get("continue_topic")
    if data.get("send") is True and isinstance(continuation, str):
        core.selected_continuation = continuation.strip()[:80]
    core.contact_reason = str(data.get("reason") or "")[:200]
    core.proactive_intent = str(data.get("intent") or data.get("reason") or "")[
        :160
    ]
    source_options = snapshot.get("contact_source_options") or []
    activity_candidates = snapshot.get("proactive_candidates") or []
    (
        core.selected_contact_source,
        core.selected_source_event_id,
        core.selected_activity_candidate,
    ) = resolve_contact_source(
        data.get("contact_source"),
        source_options,
        contact_kind=snapshot.get("contact_kind", "spontaneous_share"),
        message=data.get("message", ""),
        source_event_id=data.get("source_event_id"),
        activity_candidates=activity_candidates,
        own_offer=contact_projection.get("own_moment_offer"),
        share_event=contact_projection.get("share_event"),
        artifact_id=data.get("artifact_id"),
    )
    core.selected_contact_intent = (
        data.get("contact_intent")
        if data.get("contact_intent") in ("quiet", "open")
        else "keep"
    )
    core.selected_followup_id = next(
        (x["id"] for x in due if x["id"] == data.get("followup_id")), None
    )
    core.selected_artifact = next(
        (x for x in artifacts if x["id"] == data.get("artifact_id")), None
    ) if not snapshot.get("text_only") else None
    core.selected_shopping = data.get("shopping")
    if isinstance(core.selected_shopping, dict):
        core.selected_shopping = {
            "trip_id": shopping_trip_id,
            **core.selected_shopping,
        }
    core.selected_voice = data.get("voice") is True and voice_available
    if share_active and not optional_share:
        core.selected_artifact = None
        core.selected_voice = False
        core.selected_shopping = None
        core.selected_followup_id = None
    message, marker_shopping = extract_shopping(
        data["message"] if isinstance(data.get("message"), str) else ""
    )
    if snapshot.get("contact_kind", "spontaneous_share") in SHARE_KINDS:
        core.selected_expression_shape = classify_expression_shape(
            message, data.get("expression_shape")
        )
    if marker_shopping and core.selected_shopping is None:
        core.selected_shopping = {
            "trip_id": shopping_trip_id,
            **marker_shopping,
        }
    ordinary_opener = snapshot.get("contact_kind", "spontaneous_share") in ("casual_contact", "spontaneous_share")
    if data.get("send") is True and not (share_active and not optional_share):
        if snapshot.get("contact_kind", "spontaneous_share") in SHARE_KINDS and generic_affection_opener(message):
            feeling = ((emotion or {}).get("最近感受") or {}).get("feeling") or ""
            wants_closeness = re.match(
                r"^(?:我)?(?:现在|这会儿|此刻)?(?:有点|挺|很|特别)?"
                r"想(?:念他|他|亲他|抱他|和他亲近)(?=$|[了，。！？、\s])",
                feeling.strip(),
            )
            if not wants_closeness or any(
                generic_affection_opener(row.get("text")) for row in proactive_history[-2:]
            ):
                if core.selected_artifact and core.selected_artifact.get("photo_activity"):
                    message = "给你看看这张。"
                else:
                    core.contact_reason = "当前没有新的亲近意愿，略过通用亲昵开场"
                    return None
    if (
        ordinary_opener
        and snapshot.get("contact_kind") == "casual_contact"
        and data.get("send") is True
        and not core.selected_artifact
        and not (share_active and not optional_share)
    ):
        feedback = casual_candidate_feedback(message, proactive_history)
        if feedback:
            core.contact_reason = feedback
            if topic_feedback is None:
                return await core.decide(
                    snapshot,
                    topic_feedback={
                        "message": message,
                        "intent": core.proactive_intent,
                        "reason": feedback,
                    },
                )
            return None
    if (
        data.get("send") is True
        and isinstance(message, str)
        and 1 <= len(message.strip()) <= 200
    ):
        message = message.strip()
        quote_now = decision_now
        answered_user = str((contact_projection.get("answered_tail") or {}).get("user") or "")
        answered_at = (contact_projection.get("answered_tail") or {}).get("at", 0)
        invalid_quotes = unverified_user_quotes(
            message,
            snapshot.get("turns", []),
            current_user=(
                answered_user
                if isinstance(answered_at, (int, float))
                and 0 <= quote_now - answered_at <= 3600
                else ""
            ),
            now=quote_now,
            window=3600,
        )
        if invalid_quotes:
            core.contact_reason = (
                "主动候选引用了近期用户消息中不存在的话，已取消发送："
                + "、".join(invalid_quotes[:3])
            )[:240]
            logger.warning("[Mengyu contact] Rejected unverified user quotation: %s", invalid_quotes)
            return None
        update = snapshot.get("hobby_update", {})
        if (
            update.get("primary") == "gaming"
            and retired_paper_topic(message)
            and not any(
                item.get("role") == "user"
                and item.get("at", 0) >= update.get("at", 0)
                and retired_paper_topic(item.get("text", ""))
                for item in snapshot.get("turns", [])
            )
        ):
            core.contact_reason = "旧手作任务已结束，不继续主动分享旧进度"
            return None
        if optional_share and core.selected_source_event_id == share_event["source_event_id"] and not life_share_matches(
            share_event, core.selected_source_event_id, message,
        ):
            core.selected_source_event_id = ""
            core.selected_activity_candidate = None
            core.selected_contact_source = "casual_contact"
        if snapshot.get("contact_kind") == "absence_check" and not re.search(
            r"[?？]|人呢|跑哪|去哪|怎么|咋|忙啥|忙什么|还在|在不在|去干|去做",
            message,
        ):
            return None
        if core.selected_artifact is None and PHOTO_WORDING.search(message):
            cleaned = _text_without_photo(message)
            if cleaned:
                core.contact_reason = "本来想配图，改成纯文字"
                message = cleaned
            else:
                core.contact_reason = "没有真实图片，不能说已经给他看"
                if topic_feedback is None and snapshot.get("contact_kind") in SHARE_KINDS:
                    return await core.decide(snapshot, topic_feedback={
                        "message": message, "reason": "photo_missing",
                        "source_event_id": core.selected_source_event_id,
                    })
                return None
        if (
            core.selected_artifact is not None
            and snapshot.get("contact_kind") != "rest_share"
        ):
            return message
        share_now = contact_projection.get("share_event") or {}
        if (
            share_now.get("text")
            and not optional_life_share(share_now)
            and share_now.get("status", "pending") == "pending"
            and 0 <= decision_now - share_now.get("at", 0) < 1800
        ):
            return message
        message, review_reason = await review_proactive(
            provider,
            message,
            snapshot.get("turns", []),
            answered,
            core.proactive_intent,
            shared_notes=life_records,
            proactive_history=proactive_history,
            owner_rest=snapshot.get("contact_kind") == "rest_share",
            force_semantic=bool(proactive_history) and snapshot.get("contact_kind") in SHARE_KINDS,
            verified_life_source=bool(
                core.selected_contact_source == "activity_share"
                and not str(core.selected_source_event_id).startswith("work:")
                and (
                    core.selected_activity_candidate and note_used(core.selected_activity_candidate, message)
                    or optional_share and life_share_matches(share_event, core.selected_source_event_id, message)
                )
            ),
        )
        core.contact_reason = (core.contact_reason + "; " + review_reason)[:240]
        if not message and review_reason in RESELECT_REASONS:
            outcome["rejections"].append({
                "message": str(data.get("message") or "")[:200],
                "intent": core.proactive_intent,
                "reason": review_reason,
                "source_event_id": core.selected_source_event_id,
                "at": decision_now,
            })
        if (not message and topic_feedback is None
                and (review_reason == "needs_topic" or (
                    review_reason in RESELECT_REASONS
                    and snapshot.get("contact_kind") in SHARE_KINDS
                ))):
            return await core.decide(
                snapshot,
                topic_feedback={
                    "message": data.get("message"),
                    "intent": core.proactive_intent,
                    "reason": review_reason,
                    "source_event_id": core.selected_source_event_id,
                },
            )
        return message
    return None


NO_EVENT_KINDS = ("conversation_followup", "absence_check", "activity_notice")
EVENT_USES_PER_DAY = 2


def _life_events():
    try:
        from data.plugins.mengyu_life import life_events
    except Exception:  # noqa: BLE001 - the gate must never break sending
        return None
    return life_events


def _log_event_missing(core, now, reason):
    try:
        path = core.data_root / "diagnostics" / "event-missing.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(
                {"at": now, "plugin": "mengyu_proactive", "reason": reason},
                ensure_ascii=False,
            ) + "\n")
    except Exception:  # noqa: BLE001
        pass


def latest_life_event(core, now):
    """Newest event this plugin may still use, honoring the daily cap."""
    source = _life_events()
    if source is None:
        _log_event_missing(core, now, "life_events_unavailable")
        return None
    event = source.latest(now=now, exclude=("mengyu_proactive",))
    if event is None:
        _log_event_missing(core, now, "no_event")
        return None
    if event.get("kind") == "return" and event.get("friend_meetup_id"):
        meetup = source.latest(
            kind="social_meetup", activity_id="friend-meetup:" + event["friend_meetup_id"],
            now=now, exclude=("mengyu_proactive",),
        )
        if meetup and meetup.get("stage") == "finished":
            event = meetup
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    uses = core.state.get("life_event_uses") or {}
    if uses.get("day") != day or not isinstance(uses.get("ids"), dict):
        uses = {"day": day, "ids": {}}
        core.state["life_event_uses"] = uses
    if uses["ids"].get(event["event_id"], 0) >= EVENT_USES_PER_DAY:
        _log_event_missing(core, now, "already_used_twice")
        return None
    return event


def note_life_event_sent(core, event_id, now):
    """Consume the event and count the use after a successful send."""
    if not event_id:
        return
    source = _life_events()
    if source is not None:
        try:
            source.consume(event_id, "mengyu_proactive")
            source.attach(event_id, "delivered_at", now)
        except Exception:  # noqa: BLE001
            pass
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    uses = core.state.get("life_event_uses") or {}
    if uses.get("day") != day or not isinstance(uses.get("ids"), dict):
        uses = {"day": day, "ids": {}}
    uses["ids"][str(event_id)] = uses["ids"].get(str(event_id), 0) + 1
    core.state["life_event_uses"] = uses


async def tick(core):
    now = time.time()
    drift = core.writer_audit.scan()
    if drift and core.writer_audit.mode == "protect" and drift.intersection(
        ("state.json", "activities.json", "activity_schedule.json")
    ):
        return
    media = core.plugin_service("mengyu_media")
    media_enabled = media is not None
    if media_enabled and not core.image_lock.locked() and any(
        (item.get("photo_review_job") or {}).get("status") == "pending"
        and item["photo_review_job"]["retry_at"] <= now
        for item in core.activities.data["history"]
    ):
        core.spawn_background(state_owners.run_as("media", media.retry_photo_review, core))
    promise = core.state.get("photo_promise") or {}
    if media_enabled and promise.get("status") == "pending" and time.time() >= promise.get("due", 0):
        core.spawn_background(core.fulfill_photo_promise())
    async with core.lock:
        if expire_self_space(core.state, now):
            core.save()
        if OwnerPresenceService(core.state).expire(now):
            core.state["next_check"] = min(core.state.get("next_check", now), now)
            core.save()
        pending_image = next(
            (
                item
                for item in core.activities.data["history"]
                if item.get("delivery_pending")
                and not item.get("shared_at")
                and not item.get("delivery_status")
            ),
            None,
        )
        if (
            media_enabled
            and pending_image
            and core.state.get("enabled", True)
            and not core.state.get("conversation_paused")
            and not core.is_effectively_asleep(time.time())
        ):
            request_ts = pending_image.get("photo_request_user_ts")
            promise_id = pending_image.get("photo_job_id")
            if ((request_ts and request_ts != core.state.get("last_user_ts"))
                    or (promise_id and promise_id != (core.state.get("photo_promise") or {}).get("id"))):
                pending_image.update(delivery_status="superseded", delivery_pending=False)
                core.activities.save()
                return
            if request_ts and core.inbox.has_recent_pending():
                return
            if not core.activities.live_photo_ok(pending_image):
                pending_image.update(delivery_status="blocked", delivery_blocked=True, delivery_pending=False)
                core.activities.save()
                return
            binding = pending_image.get("photo_activity")
            text = photo_activity.caption(binding) if binding else ""
            if text and core.review_public_text(text, "requested_photo") != text:
                pending_image.update(delivery_status="blocked", delivery_blocked=True, delivery_pending=False)
                core.activities.save()
                return
            path = Path(pending_image.get("artifact", "")).resolve()
            if (
                not path.is_relative_to((core.data_root / "creations").resolve())
                or not path.is_file()
                or path.suffix != ".png"
            ):
                pending_image["delivery_status"] = "invalid"
                core.activities.save()
                return
            image_id = stable_id("photo", pending_image.get("id") or path)
            pending_image["delivery_status"] = "reserved"
            core.activities.save()
            entry = core.record_delivery_segment(image_id, "requested_photo", "", "reserved")
            if binding:
                entry["activity_id"] = binding["activity_id"]
            core.note_transport(image_id, "requested_photo", 0, "reserved")
            core.save()
            try:
                chain = [Plain(text)] if text else []
                chain.append(Image.fromFileSystem(str(path)))
                sent = await asyncio.wait_for(
                    core.send_qq(MessageChain(chain=chain)), 30,
                )
                pending_image["delivery_status"] = "sent" if transport_accepted(sent) else "failed"
                if transport_accepted(sent):
                    core.record_delivery_segment(image_id, "requested_photo", text, "complete",
                                                 index=0, media=delivery_medium(chain), receipt=platform_id(sent))
                    pending_image["shared_at"] = time.time()
                    pending_image["delivery_pending"] = False
                else:
                    core.note_transport(image_id, "requested_photo", 0, "failed")
                    core.state["delivery_ledger"][image_id]["status"] = "failed"
                core.save()
                core.activities.save()
                logger.info("[Mengyu images] Requested photo delivery=%s", pending_image["delivery_status"])
            except (Exception, asyncio.CancelledError):
                pending_image["delivery_status"] = "uncertain"
                core.note_transport(image_id, "requested_photo", 0, "uncertain")
                entry = core.state["delivery_ledger"][image_id]
                if entry["status"] == "reserved":
                    entry["status"] = "uncertain"
                core.save()
                core.activities.save()
                raise
            return
        now = time.time()
        queued_share = core.state.get("share_event") or {}
        if (
            queued_share.get("status", "pending") == "pending"
            and queued_share.get("text")
            and now - queued_share.get("at", 0) >= 1800
        ):
            queued_share["status"] = "expired"
            queued_share["expired_at"] = now
            core.save()
        if SleepService(core.state).release_night_pause(now):
            # SleepService owns the sleep episode; OwnerPresenceService
            # separately owns the contact pause and legacy mirror.
            OwnerPresenceService(core.state).release_night(now)
            core.save()
        if core.assess_interaction_silence(now):
            core.save()
        morning_projection = core.current_state_projection(now)
        morning = morning_ready(morning_projection, now)
        if morning and (morning_projection.get("owner") or {}).get("mode") == "open":
            OwnerPresenceService(core.state).set_open(time.time(), "main_explicit_resume")
            core.state["self_space"] = {
                "active": False,
                "since": now,
                "reason": "睡醒了，愿意说话",
            }
            SleepService(core.state).set_inactive_after_morning(now)
            core.state["next_check"] = now
        current_life = core.current_life()
        if current_life:
            phase_info = current_life.get("current") or {}
            phase_at = phase_info.get("at")
            phase_time_key = int(phase_at) if isinstance(phase_at, (int, float)) else 0
            phase = (
                str(current_life["id"])
                + ":" + str(phase_time_key)
                + ":" + str(phase_info.get("stage") or "")
                + ":" + str(phase_info.get("stop_index", 0))
            )
            previous_phase = core.state.get("last_contact_life_phase")
            if previous_phase != phase:
                # Do not backfill a candidate on first startup observation or
                # when migrating the old id:stage marker to the richer key.
                legacy_phase_marker = (
                    isinstance(previous_phase, str)
                    and previous_phase.count(":") < 3
                )
                if previous_phase and not legacy_phase_marker:
                    segment = next(
                        (
                            item
                            for item in core.activities.data.get("virtual_life", [])
                            if item.get("id") == current_life.get("id")
                        ),
                        None,
                    )
                    if (segment and not segment.get("daily_anchor_id")
                            and phase_info.get("stage") in ("回来了", "做完了")):
                        candidate = add_daylife_progress_moment(
                            core.life, segment, phase_info, now
                        )
                        if candidate:
                            core.life = core.life[-30:]
                            core.save_life()
                            core.state["next_check"] = min(
                                core.state.get("next_check", now + 60),
                                now + random.randint(15, 60),
                            )
                            logger.info(
                                "[Mengyu sharing] Added daylife progress candidate: %s",
                                candidate.get("source_event_id"),
                            )
                core.state["last_contact_life_phase"] = phase
                core.save()
        policy_now = time.time()
        policy_projection = core.current_state_projection(
            policy_now, current_life=current_life
        )
        report = core.promised_activity_notice(policy_projection, policy_now)
        if not report and core.inbox.has_recent_pending():
            return
        if (interaction_note(policy_projection, proactive=True)
                and (core.state.get("absence_due_ts") or core.state.get("followup_due_ts"))):
            core.state["absence_due_ts"] = 0
            core.state["followup_due_ts"] = 0
            policy_projection = core.current_state_projection(policy_now, current_life=current_life)
            core.save()
        allowed, blocked_reason = eligible(policy_projection, policy_now)
        if not report and not allowed:
            last = core.state.get("last_contact_decision") or {}
            if policy_now - float(last.get("at") or 0) > 1800:
                core.state["last_contact_decision"] = {
                    "at": policy_now,
                    "contact_kind": "check",
                    "result": "blocked",
                    "reason": blocked_reason,
                }
                core.save()
            return
        snapshot = copy.deepcopy(core.state)
        snapshot["_current_state"] = policy_projection
        if report:
            snapshot["contact_kind"] = "activity_notice"
        else:
            snapshot["_activity_data"] = copy.deepcopy(core.activities.data)
            snapshot["_life_records"] = copy.deepcopy(core.life)
            snapshot["_due_followups"] = core.activities.due_followups(policy_now)
            snapshot["_current_choice"] = copy.deepcopy(core.current_choice())
            snapshot["_daily_mood_context"] = copy.deepcopy(core.daily_mood_context())
            snapshot["_takeout_context"] = copy.deepcopy(core.takeout_context())
            sleep_now = core.state.get("sleep") or {}
            if rest_sharing_allowed(policy_projection):
                snapshot["contact_kind"] = "rest_share"
            elif night_due((policy_projection.get("mengyu") or {}).get("sleep") or {}, now) and night_window(now):
                snapshot["contact_kind"] = "night_wake"
                sleep_now = SleepService(core.state).ensure_night_wake(now)
                snapshot["night_job_id"] = sleep_now["id"]
            elif morning:
                core.state["morning_greeted_day"] = local_day(time.time())
                snapshot["contact_kind"] = "morning"
            elif core.allow_followup and followup_ready(policy_projection, policy_now):
                snapshot["contact_kind"] = "conversation_followup"
                core.state["followup_due_ts"] = 0
                core.state["absence_due_ts"] = 0
            elif own_offer_ready(policy_projection, now):
                snapshot["contact_kind"] = "self_share"
                core.state["followup_due_ts"] = 0
                core.state["absence_due_ts"] = 0
            elif absence_ready(policy_projection, policy_now):
                snapshot["contact_kind"] = "absence_check"
                core.state["last_absence_user_ts"] = core.state.get("last_user_ts", 0)
                core.state["absence_due_ts"] = 0
                core.state["followup_due_ts"] = 0
            # Share candidates originate in real life events. Prioritize the
            # actual personal moments, and do not let each work stage schedule
            # another unsolicited work report.
            share_records = [
                item for item in core.life
                if isinstance(item, dict)
                and isinstance(item.get("at"), (int, float))
                and 0 <= policy_now - item["at"] <= 3 * 3600
                and not (
                    str(item.get("source_event_id") or "").startswith("daylife:")
                    and (
                        str(item["source_event_id"]).endswith(":到了")
                        or (
                            str(item["source_event_id"]).endswith(":做完了")
                            and re.search(r"这一段(?:做完|结束)了", str(item.get("thought") or ""))
                        )
                    )
                )
            ]
            source_candidates = current_proactive_share_candidates(
                share_records, now, history=core.state.get("proactive_history", []), limit=4,
            )
            snapshot["proactive_candidates"] = source_candidates
            personal_candidate = next((
                item for item in source_candidates
                if not is_work(item)
                and len(str(item.get("thought") or "").strip()) >= 8
            ), None)
            snapshot["autonomy_decision"] = evaluate_autonomy(
                policy_projection,
                policy_now,
                contact_kind=snapshot.get("contact_kind", "spontaneous_share"),
                personal_candidate=personal_candidate,
            )
            note_memory_offered(core.state, snapshot["autonomy_decision"], policy_now)
            # Canonical motive classification is additive: keep legacy modes.
            snapshot["contact_source_options"] = available_contact_sources(
                snapshot.get("contact_kind", "spontaneous_share"),
                source_candidates,
                own_offer=snapshot.get("own_moment_offer"),
                share_event=snapshot.get("share_event"),
            )
            core.state["next_check"] = time.time() + random.randint(120, 300)
            contact_kind = snapshot.get("contact_kind", "spontaneous_share")
            photo_offer = (
                contact_kind == "self_share"
                and (snapshot.get("own_moment_offer") or {}).get("kind") == "photo"
            )
            needs_event = contact_kind not in NO_EVENT_KINDS and not photo_offer
            life_event = (
                latest_life_event(core, policy_now) if needs_event else None
            )
            activity = (policy_projection.get("mengyu") or {}).get("activity") or {}
            life_now = (policy_projection.get("contact") or {}).get("life_now") or (
                activity if activity.get("status") == "in_progress" else {}
            )
            if (
                life_now
                and needs_event
                and str((life_event or {}).get("name") or "") != str(life_now.get("name") or "")
                and not ((life_event or {}).get("kind") == "social_meetup" and life_event.get("stage") == "finished")
            ):
                # Her current activity wins over a stale transition event.
                material = _material_key(life_now)
                life_event = None
                if not _material_seen(core, material, policy_now):
                    snapshot["share_material_key"] = material
                    life_event = {
                        "event_id": "",
                        "activity_id": "now",
                        "kind": life_now.get("kind"),
                        "stage": "ongoing",
                        "name": life_now.get("name"),
                        "place": life_now.get("place"),
                        "detail": life_now.get("detail"),
                    }
            if life_event is not None and life_event.get("event_id"):
                event_key = "event:" + str(life_event["event_id"])
                if _material_seen(core, event_key, policy_now):
                    life_event = None
                else:
                    snapshot["share_material_key"] = event_key
            if life_event is None and needs_event and contact_kind in (
                "spontaneous_share", "self_share",
            ) and casual_ready(policy_projection, policy_now):
                snapshot["contact_kind"] = "casual_contact"
                core.state["casual_considered_at"] = policy_now
                needs_event = False
            if life_event is None and needs_event:
                decision = {
                    "at": time.time(),
                    "contact_kind": contact_kind,
                    "result": "skipped",
                    "reason": "no_life_event",
                    "trace_id": trace.new(),
                }
                core.state["last_contact_decision"] = decision
                core.state["recent_contact_decisions"] = (
                    list(core.state.get("recent_contact_decisions") or []) + [decision]
                )[-20:]
                core.state["next_check"] = time.time() + random.randint(180, 360)
                core.save()
                return
            snapshot["life_event"] = life_event
            if life_event is not None and needs_event:
                snapshot["contact_kind"] = "life_share"
        core.save()
    core.selected_followup_id = None
    core.selected_artifact = None
    core.selected_voice = False
    core.selected_shopping = None
    core.contact_reason = ""
    core.selected_contact_intent = "keep"
    core.selected_continuation = ""
    core.proactive_intent = ""
    followup = snapshot.get("contact_kind") == "conversation_followup"
    pending_share = {} if report or followup else (snapshot.get("share_event") or {})
    queued_content_selected = False
    try:
        if snapshot.get("contact_kind") == "night_wake":
            core.selected_contact_source = "relationship_contact"
            core.selected_source_event_id = ""
            async with core.lock:
                sleep_service = SleepService(core.state)
                wake = sleep_service.get_night_wake()
                if wake.get("id") != snapshot.get("night_job_id"):
                    return
                message = wake.get("message") if wake.get("status") == "ready" else None
                sleep_service.update_night_wake(snapshot["night_job_id"], "ready" if message else "generating", time.time())
                core.save()
            if not message:
                provider = await core.chat_provider()
                drowsy = await asyncio.wait_for(
                    provider.text_chat(
                        prompt="半夜醒来，想看看他睡没睡",
                        session_id=None,
                        contexts=[],
                        image_urls=[],
                        system_prompt=(
                            core.persona_prompt()
                            + "\n你半夜醒了一下，迷迷糊糊，想知道他睡没睡。"
                                "就偷偷小声问一句他睡没睡（比如“还没睡？”），"
                                "不要聊开、不追问、不说教。只输出这一句。"
                        ),
                    ),
                    90,
                )
                message = (drowsy.completion_text or "").strip() or "还没睡？"
            async with core.lock:
                current = SleepService(core.state).get_night_wake()
                if current.get("id") != snapshot.get("night_job_id"):
                    return
                SleepService(core.state).update_night_wake(
                    snapshot["night_job_id"], "ready", time.time(), message=message,
                )
                core.save()
        elif report:
            message = report["text"]
            core.selected_contact_source = "activity_notice"
            core.selected_source_event_id = report["promise"]
            core.contact_reason = "答应在活动实际开始或到达后告知"
        elif snapshot.get("contact_kind") == "conversation_followup":
            core.selected_contact_source = "conversation_followup"
            core.selected_source_event_id = ""
            message = await core.decide(snapshot)
        else:
            previous = snapshot.get("last_contact_decision") or {}
            rejected = previous.get("rejected_candidate")
            if (snapshot.get("contact_kind") in SHARE_KINDS and isinstance(rejected, dict)
                    and 0 <= now - rejected.get("at", previous.get("at", 0)) < 1800
                    and snapshot.get("last_user_ts", 0) <= rejected.get("at", previous.get("at", 0))):
                # This is a fresh evaluation. Prior rejections guide selection
                # without spending this round's one replacement.
                snapshot["previous_rejection"] = rejected
                message = await core.decide(snapshot)
            else:
                message = await core.decide(snapshot)
        recent_replies = [
            str(turn.get("text") or "")
            for turn in core.state.get("turns", [])[-8:]
            if str(turn.get("role", "")).startswith("assistant")
        ][-4:]
        rejection = None
        if message and core.selected_artifact:
            if not _photo_can_send(core, core.selected_artifact):
                core.selected_artifact = None
                text_only = _text_without_photo(message)
                if text_only:
                    message = text_only
                else:
                    rejection = "photo_missing"
        if message and not core.selected_artifact and not rejection:
            if not report and repeats_recent(message, recent_replies):
                rejection = "recent_text_repeat"
            elif snapshot.get("contact_kind") in SHARE_KINDS and not core.review_public_text(
                message, "proactive_candidate",
            ):
                rejection = "unsupported_facts"
        if rejection:
            if snapshot.get("contact_kind") in SHARE_KINDS and not snapshot.get("_reselected"):
                message = await core.decide(snapshot, topic_feedback={
                    "message": message, "reason": rejection,
                    "source_event_id": core.selected_source_event_id,
                })
            else:
                outcome = snapshot.setdefault("_candidate_outcome", {"rejections": []})
                outcome["rejections"].append({
                    "message": str(message or "")[:200], "reason": rejection,
                    "source_event_id": core.selected_source_event_id, "at": time.time(),
                })
                message = None
        if (
            pending_share.get("text")
            and pending_share.get("status", "pending") == "pending"
            and 0 <= time.time() - pending_share.get("at", 0) < 1800
            and snapshot.get("contact_kind") != "night_wake"
            and not optional_life_share(pending_share)
        ):
            # A share task must deliver *its* content. A generated unrelated
            # sentence cannot consume this task merely because it was sent.
            queued_text = str(pending_share["text"]).strip()[:200]
            if queued_text:
                message = queued_text
                queued_content_selected = True
                queued_source = str(pending_share.get("source_type") or "")
                if queued_source in ("activity_progress", "daylife_progress", "work_progress"):
                    core.selected_contact_source = "activity_share"
                    core.selected_source_event_id = str(
                        pending_share.get("source_event_id") or ""
                    )
                core.selected_artifact = None
                core.selected_photo_wish = None
                core.selected_voice = False
                core.selected_shopping = None
                core.selected_followup_id = None
                core.contact_reason = "待分享事项使用原文，确保实际发送内容和任务一致"
        elif optional_life_share(pending_share):
            queued_content_selected = life_share_matches(
                pending_share, core.selected_source_event_id, message,
            )
            if queued_content_selected:
                core.contact_reason = "沿用已记录的生活素材，用自己的话聊一个细节"
        if not report and not followup and core.state.get("nudge_topic"):
            core.state["nudge_topic"] = False
    except Exception as exc:
        async with core.lock:
            if snapshot.get("contact_kind") == "night_wake":
                SleepService(core.state).update_night_wake(snapshot["night_job_id"], "pending", time.time())
            core.state["last_contact_decision"] = {
                "at": time.time(),
                "contact_kind": snapshot.get("contact_kind", "spontaneous_share"),
                "result": "error",
                "reason": type(exc).__name__,
            }
            core.state["next_check"] = min(
                core.state["next_check"], time.time() + 180
            )
            core.save()
        raise
    if message and not report and not (core.selected_artifact or {}).get("photo_activity"):
        if repeats_recent(message, recent_replies):
            core.contact_reason = "刚说过类似的话，这次不说"
            message = None
    if message:
        # Final provenance gate also covers queued shares, which can
        # replace the model's reviewed candidate after decide() returns.
        invalid_quotes = unverified_user_quotes(
            message,
            snapshot.get("turns", []),
            now=time.time(),
            window=3600,
        )
        if invalid_quotes:
            message = strip_unverified_user_quote_sentences(message, invalid_quotes) or None
            core.contact_reason = (
                "已移除没有近期用户原话依据的引用：" + "、".join(invalid_quotes[:3])
            )[:200]
    retired_proactive = core.state.get("retired_topics") or {}
    retired_words = [
        word
        for word in (retired_proactive.get("words") or [])
        if time.time() - retired_proactive.get("at", 0) < 86400
    ]
    if message and retired_words:
        kept = [
            sentence
            for sentence in re.split(r"(?<=[。！？!?\n])", message)
            if not any(word in sentence for word in retired_words)
        ]
        message = "".join(kept).strip() or None
    removed_fact = False
    if not message:
        core.selected_artifact = None
    photo = selected_photo = core.selected_artifact
    if photo:
        photo = next(
            (
                item for item in core.activities.data["history"]
                if item.get("id") == photo["id"]
                and item.get("kind") == photo.get("kind")
                and item.get("artifact") == photo.get("artifact")
            ),
            None,
        )
        core.selected_artifact = photo
    original_message = message
    binding = photo.get("photo_activity") if photo else None
    archived_photo = bool(binding and photo.get("sharing_origin") == photo_activity.LIFE_PHOTO_ORIGIN)
    photo_caption = photo_activity.caption(binding, archived=archived_photo) if binding else ""
    if selected_photo and (
        not _photo_can_send(core, photo, time.time())
        or (binding and core.review_public_text(photo_caption, "proactive_photo_candidate") != photo_caption)
    ):
        core.selected_artifact = photo = binding = None
        archived_photo = False
        message = _text_without_photo(original_message)
        if not message and snapshot.get("contact_kind") in SHARE_KINDS and not snapshot.get("_reselected"):
            snapshot["text_only"] = True
            message = await core.decide(snapshot, topic_feedback={
                "message": original_message, "reason": "photo_missing",
                "source_event_id": core.selected_source_event_id,
            })
            core.selected_artifact = None
    if binding:
        message = photo_caption
        core.selected_voice = False
    if report:
        if message != report["text"]:
            return
    elif message:
        message = await core.repair_meal_reply(message)
        checked = core.review_public_text(message, "proactive_draft")
        removed_fact = checked != message
        message = checked
    if (
        message
        and (
            snapshot.get("contact_kind") == "rest_share"
            or rest_sharing_allowed(snapshot.get("_current_state") or {})
        )
        and not rest_message_allowed(message)
    ):
        message = None
        core.contact_reason = "对方休息时只留个人分享，不询问醒没醒或要求回应"
    if not message:
        async with core.lock:
            if removed_fact and queued_content_selected:
                pending = core.state.get("share_event") or {}
                if pending.get("id") and pending.get("id") == (snapshot.get("share_event") or {}).get("id"):
                    pending["status"] = "filtered"
            if snapshot.get("contact_kind") == "night_wake":
                wake = (core.state.get("sleep") or {}).get("night_wake") or {}
                if wake.get("id") == snapshot["night_job_id"]:
                    SleepService(core.state).update_night_wake(snapshot["night_job_id"], "skipped", time.time())
            outcome = snapshot.get("_candidate_outcome") or {}
            rejections = outcome.get("rejections") or []
            core.state["last_contact_decision"] = {
                "at": time.time(),
                "contact_kind": snapshot.get("contact_kind", "spontaneous_share"),
                "result": "no_message",
                "reason": core.contact_reason,
                "outcome": "declined" if outcome.get("declined") else "candidate_rejected" if rejections else "no_candidate",
            }
            if rejections and not outcome.get("declined"):
                core.state["last_contact_decision"]["rejections"] = rejections[-2:]
                core.state["last_contact_decision"]["rejected_candidate"] = rejections[-1]
            core.state["recent_contact_decisions"] = (
                list(core.state.get("recent_contact_decisions") or []) + [dict(core.state["last_contact_decision"])]
            )[-20:]
            core.state["next_check"] = min(
                core.state["next_check"], time.time() + random.randint(90, 180)
            )
            core.save()
        return
    voice_audio = None
    voice_plugin = None
    if (
        core.selected_voice
        and core.selected_artifact is None
        and not re.search(r"&&[a-zA-Z0-9_-]{1,50}&&", message)
    ):
        lookup = getattr(core.context, "get_registered_star", None)
        voice_star = lookup("mengyu_breeze_voice") if lookup else None
        if voice_star and hasattr(voice_star.star_cls, "prepare_voice"):
            voice_plugin = voice_star.star_cls
            voice_audio = await voice_plugin.prepare_voice(
                message,
                "proactive:" + uuid.uuid4().hex,
                str((core.state.get("daily_mood") or {}).get("feeling", "")),
                automatic=True,
            )
    while True:
        retry_feedback = None
        async with core.lock:
            # An incoming message or stop command during generation cancels this send.
            check = {**core.state, "next_check": 0}
            if snapshot.get("contact_kind") == "conversation_followup":
                for key in ("followup_due_ts", "followup_user_ts", "followup_reply_ts"):
                    check[key] = snapshot.get(key, 0)
            if snapshot.get("contact_kind") == "self_share":
                check["own_moment_offer"] = snapshot["own_moment_offer"]
            if snapshot.get("contact_kind") == "absence_check":
                for key in (
                    "absence_due_ts",
                    "absence_user_ts",
                    "last_absence_user_ts",
                ):
                    check[key] = snapshot.get(key, 0)
            recheck_now = time.time()
            recheck_projection = project_current_state(
                check, core.activities.data, core.activity_scheduler.data, recheck_now
            )
            if (
                core.state["last_user_ts"] != snapshot["last_user_ts"]
                or (not report and core.inbox.has_recent_pending())
                or (
                    followup and (
                        core.state.get("self_space", {}).get("active")
                        or core.state.get("followup_topic") != snapshot.get("followup_topic")
                    )
                )
                or (not report and not eligible(recheck_projection, recheck_now)[0])
                or (report and core.promised_activity_notice(recheck_projection, recheck_now) != report)
                or (
                    rest_sharing_allowed(recheck_projection)
                    and not rest_message_allowed(message)
                )
                or (
                    snapshot.get("contact_kind") == "night_wake"
                    and not sleeping(recheck_projection, recheck_now)
                )
                or (
                    snapshot.get("contact_kind") == "night_wake"
                    and (core.state.get("sleep") or {}).get("night_wake", {}).get("id")
                    != snapshot["night_job_id"]
                )
            ):
                return
            now = time.time()
            final_message = core.review_public_text(message, "proactive", projection=recheck_projection)
            if photo and (not _photo_can_send(core, photo, recheck_now) or (binding and final_message != photo_caption)):
                core.selected_artifact = photo = binding = None
                archived_photo = False
                voice_audio = None
                message = _text_without_photo(original_message)
                final_message = core.review_public_text(message, "proactive_photo_fallback", projection=recheck_projection)
                if not final_message or repeats_recent(final_message, recent_replies):
                    if snapshot.get("contact_kind") in SHARE_KINDS and not snapshot.get("_reselected"):
                        retry_feedback = {
                            "message": original_message, "reason": "photo_missing",
                            "source_event_id": core.selected_source_event_id,
                        }
                    else:
                        core.state["last_contact_decision"] = {
                            "at": now, "result": "no_message", "reason": "photo_missing_at_submission",
                        }
                        core.state["next_check"] = min(core.state["next_check"], now + 90)
                        core.save()
                        return
            if retry_feedback is None:
                if (not final_message
                        or (report and final_message != report["text"])
                        or (rest_sharing_allowed(recheck_projection) and not rest_message_allowed(final_message))):
                    return
                if final_message != message:
                    removed_fact = True
                    voice_audio = None
                    message = final_message
                queued_share_snapshot = snapshot.get("share_event") or {}
                if queued_content_selected and optional_life_share(queued_share_snapshot):
                    queued_content_selected = life_share_matches(
                        queued_share_snapshot, core.selected_source_event_id, message,
                    )
                queued_share = core.state.get("share_event") or {}
                share_task_id = queued_share_snapshot.get("id")
                share_task_pending = bool(
                    queued_content_selected
                    and queued_share_snapshot.get("text")
                    and queued_share_snapshot.get("status", "pending") == "pending"
                    and 0 <= now - queued_share_snapshot.get("at", 0) < 1800
                )
                if share_task_pending and (
                    queued_share.get("status", "pending") != "pending"
                    or queued_share.get("id") != share_task_id
                    or queued_share.get("text") != queued_share_snapshot.get("text")
                ):
                    return
                delivery_id = "proactive:" + uuid.uuid4().hex
                if core.state.get("day") != local_day(now):
                    core.state["day"], core.state["count"] = local_day(now), 0
                # Reserve persistently BEFORE sending. A restart will mark this
                # delivery uncertain rather than replaying it and risking a duplicate.
                if report:
                    activity_reports.reserve(core.state, report, delivery_id, 0, now)
                core.state["delivery_status"] = "reserved"
                entry = core.record_delivery_segment(delivery_id, "proactive", "", "reserved", now)
                if binding:
                    entry["activity_id"] = binding["activity_id"]
                if share_task_pending:
                    queued_share.update(
                        status="reserved",
                        delivery_id=delivery_id,
                        reserved_at=now,
                    )
                    core.state["share_event"] = queued_share
                if snapshot.get("contact_kind") == "night_wake":
                    SleepService(core.state).update_night_wake(snapshot["night_job_id"], "sending", now)
                core.state["last_contact_decision"] = {
                    "at": now,
                    "contact_kind": snapshot.get("contact_kind", "spontaneous_share"),
                    "contact_source": getattr(core, "selected_contact_source", "") or "casual_contact",
                    "source_event_id": getattr(core, "selected_source_event_id", ""),
                    "event_id": (snapshot.get("life_event") or {}).get("event_id", ""),
                    "result": "reserved",
                    "reason": core.contact_reason,
                    "delivery_id": delivery_id,
                    "trace_id": trace.new(),
                }
                core.state["recent_contact_decisions"] = (
                    list(core.state.get("recent_contact_decisions") or [])
                    + [dict(core.state["last_contact_decision"])]
                )[-20:]
                core.save()
                break
        snapshot["text_only"] = True
        snapshot["_reselected"] = True
        message = await core.decide(snapshot, topic_feedback=retry_feedback)
        core.selected_artifact = None
        if message:
            message = _text_without_photo(message)
        if not message or repeats_recent(message, recent_replies):
            async with core.lock:
                core.state["last_contact_decision"] = {
                    "at": time.time(), "result": "no_message", "reason": "photo_missing_at_submission",
                    "outcome": "candidate_rejected",
                }
                core.state["next_check"] = min(core.state["next_check"], time.time() + 90)
                core.save()
            return
        original_message = message

    try:
        async with core.lock:
            try:
                from data.plugins.mengyu_contact import outbound_guard
                from data.plugins.mengyu_life.economy_bridge import recent_items

                message = outbound_guard.strip_unbacked_purchase(
                    message, recent_items(3)
                )
            except Exception:  # noqa: BLE001 - guard is best effort
                pass
            chain = core.build_proactive_chain(message)
            chain.chain = core.limit_faces(chain.chain)
            photo_attached = False
            if core.selected_artifact:
                path = Path(core.selected_artifact["artifact"]).resolve()
                if (
                    path.is_relative_to((core.data_root / "creations").resolve())
                    and path.is_file()
                ):
                    chain.chain.append(Image.fromFileSystem(str(path)))
                    photo_attached = True
            from data.plugins.mengyu_contact.outbound_guard import check as check_photo

            if (photo and not photo_attached) or not check_photo(chain.chain, core.data_root / "creations")[0]:
                fallback = core.review_public_text(
                    _text_without_photo(original_message), "proactive_photo_fallback",
                    projection=core.current_state_projection(),
                )
                if fallback and not repeats_recent(fallback, recent_replies):
                    message = fallback
                else:
                    message = ""
                    core.state["last_contact_decision"]["reason"] = "photo_missing_at_submission"
                chain = core.build_chain(message)
                core.selected_artifact = photo = binding = None
                archived_photo = False
                if (entry := (core.state.get("delivery_ledger") or {}).get(delivery_id)) is not None:
                    entry.pop("activity_id", None)
            if voice_audio and not any(
                isinstance(part, Image) for part in chain.chain
            ):
                chain.chain = [Record.fromFileSystem(voice_audio["path"])]
                voice_plugin.speech.mark_delivery(
                    voice_audio["request_id"], "pending"
                )
            else:
                voice_audio = None
            core.save()
            # Bound photo captions and their images stay in one delivery.
            if not chain.chain:
                deliveries = []
            elif followup:
                deliveries = [chain]
            elif binding:
                deliveries = [chain] if any(isinstance(component, Image) for component in chain.chain) else []
            elif any(isinstance(component, Image) for component in chain.chain):
                visible = [component for component in chain.chain if not isinstance(component, Image)]
                media = [component for component in chain.chain if isinstance(component, Image)]
                deliveries = [MessageChain(chain=part) for part in (visible, media) if part]
            else:
                text = "\n".join(
                    str(component.text)
                    for component in chain.chain
                    if isinstance(component, Plain)
                )
                bubbles = chat_bubbles(text)
                deliveries = (
                    [MessageChain(chain=[Plain(bubble)]) for bubble in bubbles]
                    if len(bubbles) > 1
                    else [chain]
                )
        sent_count = 0
        delivered_parts = []
        transport_failure = "cancelled"
        for index, delivery in enumerate(deliveries):
            if index:
                await asyncio.sleep(random.uniform(1.5, 3.5))
            async with core.lock:
                fresh = core.current_state_projection()
                mode = (fresh.get("owner") or {}).get("mode", "open")
                waking = snapshot.get("contact_kind") == "night_wake"
                if (
                    snapshot["last_user_ts"] != core.state.get("last_user_ts", 0)
                    or (not report and core.inbox.has_recent_pending())
                    or not (fresh.get("contact") or {}).get("enabled", True)
                    or mode not in (("open", "night") if waking else ("open", "rest"))
                    or (followup and (
                        mode != "open" or core.is_effectively_asleep()
                        or core.state.get("self_space", {}).get("active")
                        or core.state.get("followup_topic") != snapshot.get("followup_topic")
                    ))
                    or (waking and SleepService(core.state).get_night_wake().get("id") != snapshot["night_job_id"])
                    or (report and core.promised_activity_notice(fresh, time.time(), delivery_id) != report)
                ):
                    core.note_transport(delivery_id, "proactive", index, "cancelled")
                    break
                if binding and not _photo_can_send(core, photo):
                    fallback = core.review_public_text(
                        _text_without_photo(original_message), "proactive_photo_fallback", projection=fresh,
                    )
                    if not fallback or repeats_recent(fallback, recent_replies) or (
                        mode == "rest" and not rest_message_allowed(fallback)
                    ):
                        core.contact_reason = "photo_missing_at_submission"
                        core.state["last_contact_decision"]["reason"] = core.contact_reason
                        core.note_transport(delivery_id, "proactive", index, "cancelled")
                        break
                    core.selected_artifact = photo = binding = None
                    archived_photo = False
                    message = fallback
                    delivery = core.build_chain(fallback)
                    if (entry := (core.state.get("delivery_ledger") or {}).get(delivery_id)) is not None:
                        entry.pop("activity_id", None)
                checked = core.review_chain_text(delivery.chain, "proactive_segment", trace=delivery_id)
                if (
                    not checked or len(checked) != len(delivery.chain)
                    or any(isinstance(old, Plain) and old.text != new.text
                           for old, new in zip(delivery.chain, checked))
                    or voice_audio and core.review_public_text(message, "proactive_voice", trace=delivery_id) != message
                ):
                    core.note_transport(delivery_id, "proactive", index, "cancelled")
                    break
                if archived_photo:
                    photo.update(delivery_status="reserved", delivery_id=delivery_id)
                    core.activities.save()
                core.note_transport(delivery_id, "proactive", index, "reserved")
                with write_trigger(delivery_id):
                    core.save()
            try:
                delivered = await asyncio.wait_for(core.send_qq(delivery), 30)
            except (Exception, asyncio.CancelledError) as exc:
                transport_failure = "uncertain"
                if archived_photo:
                    photo["delivery_status"] = "uncertain"
                    core.activities.save()
                core.note_transport(delivery_id, "proactive", index, transport_failure)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                break
            if not transport_accepted(delivered):
                transport_failure = (
                    "failed" if (not report and not archived_photo) or explicitly_failed(delivered)
                    else "uncertain"
                )
                if archived_photo:
                    photo["delivery_status"] = transport_failure
                    core.activities.save()
                core.note_transport(delivery_id, "proactive", index, transport_failure)
                break
            sent_count += 1
            part_text = "".join(str(part.text) for part in delivery.chain if isinstance(part, Plain)).strip()
            if voice_audio and sent_count == 1 and not part_text:
                part_text = message
            if part_text:
                delivered_parts.append(part_text)
            async with core.lock:
                core.record_delivery_segment(
                    delivery_id, "proactive", part_text, "partial", index=index,
                    media=delivery_medium(delivery.chain), receipt=platform_id(delivered),
                )
                with write_trigger(delivery_id):
                    if _count_delivery(core, delivery_id, time.time()) and report:
                        activity_reports.note_result(
                            core.state, delivery_id, 0, "sent", time.time()
                        )
                    core.save()
                if core.selected_artifact and core.selected_artifact.get("artifact"):
                    core.confirm_creation_in_chain(core.selected_artifact["artifact"], delivery.chain)
                if archived_photo:
                    photo["delivery_status"] = "sent"
                    core.activities.save()
                if part_text:
                    core.queue_task_event("assistant", f"{delivery_id}:{index}", part_text, time.time())
        async with core.lock:
            sent = bool(deliveries) and sent_count == len(deliveries)
            delivered_text = "\n".join(part for part in delivered_parts if part).strip()
            delivery_result = "sent" if sent else ("partial" if sent_count else transport_failure)
            core.state["delivery_status"] = delivery_result
            if sent and followup:
                thread = core.state.get("followup_thread") or {}
                fingerprint = (snapshot.get("followup_topic") or {}).get("fingerprint")
                if fingerprint and thread.get("fingerprint") == fingerprint:
                    thread["sent"] = thread.get("sent", 0) + 1
                    thread["at"] = time.time()
            if sent and not snapshot.get("_alternate_topic"):
                note_life_event_sent(
                    core,
                    (snapshot.get("life_event") or {}).get("event_id", ""),
                    time.time(),
                )
            ledger_entry = (core.state.get("delivery_ledger") or {}).get(delivery_id)
            if isinstance(ledger_entry, dict):
                ledger_entry["status"] = "complete" if sent else ("partial" if sent_count else transport_failure)
                ledger_entry["updated_at"] = time.time()
            if delivered_text:
                core.sync_delivery_events(delivery_id)
            if share_task_pending:
                current_share = core.state.get("share_event") or {}
                if current_share.get("id") == share_task_id:
                    current_share.update(
                        status=("filtered" if removed_fact else "sent") if sent
                        else ("partial" if sent_count else transport_failure),
                        sent_at=time.time() if sent_count else None,
                        delivery_id=delivery_id,
                        delivered_text=delivered_text[:200],
                    )
                    core.state["share_event"] = current_share
            if voice_audio:
                voice_plugin.speech.mark_delivery(
                    voice_audio["request_id"], "sent" if sent else "unknown"
                )
            if snapshot.get("contact_kind") == "night_wake":
                SleepService(core.state).update_night_wake(
                    snapshot["night_job_id"], "sent" if sent else "uncertain", time.time(),
                    finished_at=time.time(),
                )
            core.state["last_contact_decision"]["result"] = core.state[
                "delivery_status"
            ]
            core.state["last_contact_decision"]["contact_source"] = (
                getattr(core, "selected_contact_source", "") or "casual_contact"
            )
            core.state["last_contact_decision"]["source_event_id"] = getattr(
                core, "selected_source_event_id", ""
            )
            displayed_message = delivered_text or (message if sent else "")
            if displayed_message:
                core.capture_activity_commitment(displayed_message, delivery_id)
                try:
                    record_delivered_wishes(
                        core.wish_path, displayed_message, time.time(), delivery_id
                    )
                except Exception as exc:  # noqa: BLE001 - never roll back a delivered message
                    logger.warning("[Mengyu wishes] Proactive wish record skipped: %s", type(exc).__name__)
                proactive_turn = core.append("assistant_proactive", displayed_message)
                proactive_turn["delivery"] = delivery_result
                proactive_turn["delivery_id"] = delivery_id
                if binding:
                    proactive_turn["activity_id"] = binding["activity_id"]
                proactive_turn["contact_kind"] = snapshot.get(
                    "contact_kind", "spontaneous_share"
                )
                proactive_turn["event_id"] = "" if snapshot.get("_alternate_topic") else (
                    snapshot.get("life_event") or {}
                ).get("event_id", "")
                proactive_turn["intent"] = getattr(core, "proactive_intent", "")
                proactive_turn["contact_source"] = (
                    getattr(core, "selected_contact_source", "") or "casual_contact"
                )
                proactive_turn["source_event_id"] = getattr(
                    core, "selected_source_event_id", ""
                )
                proactive_turn["topic_family"] = classify_delivered_topic(
                    displayed_message, proactive_turn["source_event_id"]
                )
                if (
                    delivery_result == "sent"
                    and proactive_turn["contact_source"] == "activity_share"
                    and proactive_turn["source_event_id"]
                ):
                    if mark_activity_candidate_shared(
                        core.life,
                        proactive_turn["source_event_id"],
                        displayed_message,
                        time.time(),
                    ):
                        core.save_life()
                        logger.info(
                            "[Mengyu sharing] Activity candidate shared: %s",
                            proactive_turn["source_event_id"],
                        )
                # Consume only after *complete* outbound delivery. A draft,
                # partial bubble or discarded quote does not finish a memory.
                if sent:
                    if mark_shared(core.life, displayed_message, time.time()):
                        core.save_life()
                    if snapshot.get("contact_kind") == "self_share":
                        offer = core.state.get("own_moment_offer") or {}
                        offered = snapshot.get("own_moment_offer") or {}
                        photo_delivered = (
                            photo and photo.get("id") == offer.get("artifact_id")
                            and photo.get("shared_at")
                        )
                        if (offer.get("status") == "pending" and offer == offered
                                and (offer.get("kind") != "photo" or photo_delivered)):
                            offer["status"] = "consumed"
                            core.state["own_moment_offer"] = offer
                    if snapshot.get("share_material_key") and not snapshot.get("_alternate_topic"):
                        _remember_material(
                            core, snapshot["share_material_key"], time.time()
                        )
                    if core.affect_enabled() and mark_selected_emotional_memory_shared(
                        core.state,
                        (snapshot.get("autonomy_decision") or {}).get("memory_source_id"),
                        displayed_message, time.time(),
                    ):
                        logger.info("[Mengyu autonomy] Shared selected emotional memory")
                    if core.affect_enabled():
                        mark_emotional_memory_shared(core.state, displayed_message, time.time())
                if proactive_turn.get("contact_kind") in SHARE_KINDS:
                    shape_hint = (
                        getattr(core, "selected_expression_shape", "")
                        if delivery_result == "sent" and displayed_message == message
                        else None
                    )
                    proactive_turn["expression_shape"] = classify_expression_shape(
                        displayed_message, shape_hint
                    )
                core.state["last_message"] = displayed_message
                core.state["last_message_mode"] = "modern"
                from datetime import datetime

                from data.plugins.mengyu_contact.policy import TZ
                core.state["last_message_time"] = datetime.fromtimestamp(now, TZ).isoformat()
            if delivered_text:
                core.record_delivered_meal(delivered_text, time.time(), delivery_id)
            if sent and snapshot["last_user_ts"] == core.state.get("last_user_ts", 0):
                record_shopping(
                    core.state,
                    core.current_life(),
                    core.selected_shopping,
                    time.time(),
                )
                if core.selected_contact_intent == "quiet" and explicit_self_space(displayed_message):
                    core.state["self_space"] = {
                        "active": True, "since": now, "reason": "已说清想暂停聊一会",
                    }
                elif core.selected_contact_intent == "open":
                    core.state["self_space"] = {
                        "active": False, "since": now, "reason": "已重新接话",
                    }
                if core.selected_followup_id:
                    core.activities.mark_followup_shared(
                        core.selected_followup_id, now
                    )
                if core.selected_artifact and any(
                    isinstance(part, Image) for delivery in deliveries for part in delivery.chain
                ):
                    core.selected_artifact["shared_at"] = now
                    core.activities.save()
                core.state.pop("followup_topic", None)
                core.state["followup_due_ts"] = 0
                if (
                    core.allow_followup
                    and core.selected_continuation
                    and snapshot.get("contact_kind", "spontaneous_share") in (
                        "casual_contact", "spontaneous_share", "self_share", "life_share",
                    )
                    and not queued_content_selected
                    and not removed_fact
                    and not core.selected_artifact
                    and not core.state.get("self_space", {}).get("active")
                    and not core.state.get("conversation_paused")
                    and should_followup(displayed_message, "")
                ):
                    finished_at = time.time()
                    user_ts = snapshot["last_user_ts"]
                    queue_followup(
                        core.state, core.selected_continuation, displayed_message,
                        user_ts, finished_at,
                        kind="question" if re.search(r"[?？]", displayed_message) else "statement",
                    )
            elif snapshot["last_user_ts"] == core.state.get("last_user_ts", 0):
                core.state["followup_due_ts"] = 0
            with write_trigger(delivery_id):
                core.save()
            logger.info(
                "[梦雨联系] 发送结果=%s，今日已用%d次",
                core.state["delivery_status"],
                core.state["count"],
            )
            logger.info(
                "[Mengyu contact] kind=%s result=%s",
                snapshot.get("contact_kind", "spontaneous_share"),
                core.state["delivery_status"],
            )
    except (Exception, asyncio.CancelledError):
        async with core.lock:
            if voice_audio:
                voice_plugin.speech.mark_delivery(
                    voice_audio["request_id"], "unknown"
                )
            if snapshot.get("contact_kind") == "night_wake":
                SleepService(core.state).update_night_wake(snapshot["night_job_id"], "uncertain", time.time())
            ledger_entry = (core.state.get("delivery_ledger") or {}).get(delivery_id)
            confirmed = any(segment.get("status") == "confirmed" for segment in (ledger_entry or {}).get("segments", {}).values())
            failure_status = "partial" if confirmed else "uncertain"
            if report and not confirmed:
                activity_reports.note_result(core.state, delivery_id, 0, "uncertain", time.time())
            core.state["delivery_status"] = failure_status
            core.state["last_contact_decision"]["result"] = failure_status
            if isinstance(ledger_entry, dict):
                ledger_entry["status"] = failure_status
                ledger_entry["updated_at"] = time.time()
                core.sync_delivery_events(delivery_id)
            if share_task_pending:
                current_share = core.state.get("share_event") or {}
                if current_share.get("id") == share_task_id:
                    current_share["status"] = failure_status
                    current_share["delivery_id"] = delivery_id
                    current_share["updated_at"] = time.time()
            core.save()
        raise
