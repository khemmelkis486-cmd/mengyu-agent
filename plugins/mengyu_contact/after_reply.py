"""Commit confirmed chat delivery and prepare safe conversational follow-ups."""

import asyncio
import json
import re
import time
import uuid

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain, Record
from data.plugins.mengyu_affect.core.emotional_memory import (
    mark_shared as mark_emotional_memory_shared,
)
from data.plugins.mengyu_life import meal_service
from data.plugins.mengyu_life.core import activity_reports
from data.plugins.mengyu_life.core.gift_lifecycle import (
    acknowledge_notices as acknowledge_gift_notices,
)
from data.plugins.mengyu_life.core.gift_lifecycle import mark_gift_candidate_prompted
from data.plugins.mengyu_life.core.self_sharing import mark_shared, settle_offer
from data.plugins.mengyu_life.core.shopping import record as record_shopping
from data.plugins.mengyu_life.core.wishes import (
    record_delivered_text as record_delivered_wishes,
)

from . import topic_state
from .conversation_flow import (
    expire_self_space,
    explicit_self_space,
    needs_user_evaluation_review,
    strip_unverified_user_quote_sentences,
    unverified_user_quotes,
)
from .delivery_state import accepted as transport_accepted
from .delivery_state import explicitly_failed, platform_id
from .fact_sources import medium as delivery_medium
from .policy import absence_followup_allowed, queue_followup, repeats_recent, should_followup
from .sleep_state import SleepService
from .state_write import stable_id, write_trigger
from .variety import extract_json, is_ack, meme_category


def _has_activity_promise(state, delivery_id):
    """Find an unfulfilled activity notification recorded from this reply."""
    return bool(delivery_id) and any(
        not promise.get("fulfilled")
        and str(promise.get("id") or "").startswith(f"{delivery_id}:")
        for promise in state.get("activity_notice_promises", [])
    )


async def after_reply_sent(core, event: AstrMessageEvent):
    if str(event.unified_msg_origin) != core.message_origin:
        return
    result = event.get_result()
    if result is None or not result.is_llm_result():
        return
    sequence = event.get_extra("mengyu_burst_seq")
    if event.get_extra("mengyu_silent"):
        return
    rest = event.get_extra("mengyu_bubble_rest") or []
    event.set_extra("mengyu_bubble_rest", None)
    tracked = event.get_extra("mengyu_tracked_reply") or {}
    activity_notice = event.get_extra("mengyu_activity_notice")
    delivered_text = "".join(
        part.text for part in result.chain if isinstance(part, Plain)
    )
    voice_sent = bool(
        event.get_extra("mengyu_voice_identity")
        and any(isinstance(part, Record) for part in result.chain)
    )
    spoken_text = delivered_text or (
        str(event.get_extra("mengyu_validated_voice_text") or tracked.get("text", ""))
        if voice_sent
        else ""
    )
    message_id = str(getattr(getattr(event, "message_obj", None), "message_id", ""))
    delivery_id = event.get_extra("mengyu_delivery_id") or (
        stable_id("reply", message_id) if message_id else "reply:" + uuid.uuid4().hex
    )
    event.set_extra("mengyu_delivery_id", delivery_id)
    answered = {
        "user": str(getattr(event, "message_str", "") or "")[:800],
        "reply": spoken_text[:1200],
        "at": time.time(),
        "user_ts": event.get_extra("mengyu_emotion_user_ts")
        or core.state.get("last_user_ts", 0),
        "message_id": message_id,
        "delivery_id": delivery_id,
        "delivery": "partial" if rest else "complete",
        "initiative": event.get_extra("mengyu_share_intent"),
        "activity_notice": bool(activity_notice),
    }
    if activity_id := event.get_extra("mengyu_activity_id"):
        answered["activity_id"] = activity_id
    meme_delivery = None
    requested = event.get_extra("mengyu_meme_requested")
    if requested:
        meme_delivery = {
            "requested": requested,
            "status": "delivered"
            if any(core.is_meme_image(part) for part in result.chain)
            else "not_confirmed",
            "at": time.time(),
            "user": str(getattr(event, "message_str", "") or "")[:200],
        }
        answered["meme_delivery"] = meme_delivery
    turn_record = None
    async with core.lock:
        ledger_entry = (core.state.get("delivery_ledger") or {}).get(delivery_id) or {}
        if (ledger_entry.get("segments") or {}).get("0", {}).get(
            "status"
        ) == "confirmed":
            return
        first_segment_is_current = core.inbox.is_current(sequence) and answered[
            "user_ts"
        ] == core.state.get("last_user_ts", 0)
        entry = core.record_delivery_segment(
            delivery_id,
            "ordinary_reply",
            spoken_text,
            "partial" if rest else "complete",
            index=0,
            media=delivery_medium(result.chain),
            receipt=platform_id(event.get_extra("send_result")),
        )
        if answered.get("activity_id"):
            entry["activity_id"] = answered["activity_id"]
        else:
            entry.pop("activity_id", None)
        with write_trigger(delivery_id):
            core.save()
        if meme_delivery:
            core.state["last_meme_delivery"] = meme_delivery
        if spoken_text:
            # Commit the reply only after AstrBot confirms the first message
            # was sent. Later bubbles amend this same delivered turn.
            turn_record = core.append("assistant", spoken_text)
            turn_record["delivery"] = "partial" if rest else "complete"
            turn_record["delivery_id"] = delivery_id
            if answered.get("activity_id"):
                turn_record["activity_id"] = answered["activity_id"]
            mood = core.state.get("interaction_mood") or {}
            mood_source = event.get_extra("mengyu_interaction_reply_source")
            if (
                mood_source
                and mood_source == mood.get("source_id")
                and not mood.get("reply_offered_at")
            ):
                mood["reply_offered_at"] = time.time()
            core.note_gift_mentions(spoken_text)
            if mark_gift_candidate_prompted(core.state, spoken_text, time.time()):
                core.save()
            if core.affect_enabled() and mark_emotional_memory_shared(
                core.state, spoken_text, time.time()
            ):
                core.save()
        if not rest:
            core.sync_delivery_events(delivery_id)
            try:
                acknowledge_gift_notices(
                    core.gift_path,
                    core.state,
                    time.time(),
                    source_id=delivery_id,
                    reply_text=spoken_text,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[Mengyu gifts] Acknowledgement sync skipped: %s",
                    type(exc).__name__,
                )
        core.record_delivered_meal(spoken_text, time.time(), delivery_id)
        if spoken_text:
            core.capture_activity_commitment(spoken_text, delivery_id)
            core.queue_task_event(
                "assistant", delivery_id + ":0", spoken_text, answered["at"]
            )
            try:
                record_delivered_wishes(
                    core.wish_path, spoken_text, time.time(), delivery_id
                )
            except Exception as exc:  # noqa: BLE001 - keep message delivery independent
                logger.warning(
                    "[Mengyu wishes] Delivered wish record skipped: %s",
                    type(exc).__name__,
                )
        if first_segment_is_current:
            core.state["answered_tail"] = answered
        if spoken_text and mark_shared(core.life, spoken_text, time.time()):
            settle_offer(core.state, core.life)
            core.save_life()
        if first_segment_is_current and not rest and spoken_text:
            record_shopping(
                core.state,
                core.current_life(),
                event.get_extra("mengyu_shopping_update"),
                time.time(),
            )
            SleepService(core.state).commit_choice(
                spoken_text,
                answered["user"],
                time.time(),
                event.get_extra("mengyu_sleep_phase"),
            )
            core.arm_conversation_followup(
                answered, event.get_extra("mengyu_space_decision")
            )
        core.save()

    if rest:

        async def send_rest():
            nonlocal turn_record
            all_sent = True
            try:
                for index, group in enumerate(rest, 1):
                    await asyncio.sleep(1.0)
                    async with core.lock:
                        if not core.inbox.is_current(sequence) or answered[
                            "user_ts"
                        ] != core.state.get("last_user_ts", 0):
                            all_sent = False
                            core.note_transport(
                                delivery_id, "ordinary_reply", index, "cancelled"
                            )
                            break
                        carries_notice = activity_notice and activity_notice[1] == index
                        if carries_notice:
                            notice, _, original, extra = activity_notice
                            now = time.time()
                            projection = core.current_state_projection(now)
                            if (
                                not activity_reports.allowed(
                                    core.state, projection, now
                                )
                                or not activity_reports.current_departure(
                                    notice, core.current_activity_segment(now), now
                                )
                                or core.review_public_text(
                                    notice["text"],
                                    "activity_notice",
                                    projection=projection,
                                )
                                != notice["text"]
                            ):
                                # The conversation reply still goes out without the stale notice.
                                if group[-1].text == original + extra:
                                    group = [*group[:-1], Plain(original)]
                                activity_reports.note_result(
                                    core.state, delivery_id, index, "cancelled", now
                                )
                        checked = core.review_chain_text(
                            group, "reply_bubble", trace=delivery_id
                        )
                        if (
                            carries_notice
                            and group[-1].text == original + extra
                            and (
                                not checked
                                or getattr(checked[-1], "text", "") != group[-1].text
                            )
                        ):
                            group = [*group[:-1], Plain(original)]
                            activity_reports.note_result(
                                core.state, delivery_id, index, "cancelled", time.time()
                            )
                            checked = core.review_chain_text(
                                group, "reply_bubble", trace=delivery_id
                            )
                        if (
                            not checked
                            or len(checked) != len(group)
                            or any(
                                isinstance(old, Plain) and old.text != new.text
                                for old, new in zip(group, checked)
                            )
                        ):
                            all_sent = False
                            core.note_transport(
                                delivery_id, "ordinary_reply", index, "cancelled"
                            )
                            break
                        core.note_transport(
                            delivery_id, "ordinary_reply", index, "reserved"
                        )
                        core.save()
                    try:
                        sent = await asyncio.wait_for(
                            core.context.send_message(
                                core.message_origin, MessageChain(chain=checked)
                            ),
                            30,
                        )
                    except (Exception, asyncio.CancelledError) as exc:
                        all_sent = False
                        core.note_transport(
                            delivery_id, "ordinary_reply", index, "uncertain"
                        )
                        if isinstance(exc, asyncio.CancelledError):
                            raise
                        break
                    if not transport_accepted(sent):
                        all_sent = False
                        failure = "failed"
                        if (
                            activity_notice
                            and activity_notice[1] == index
                            and not explicitly_failed(sent)
                        ):
                            failure = "uncertain"
                        core.note_transport(
                            delivery_id, "ordinary_reply", index, failure
                        )
                        break
                    segment_text = "".join(
                        part.text for part in group if isinstance(part, Plain)
                    ).strip()
                    async with core.lock:
                        core.record_delivery_segment(
                            delivery_id,
                            "ordinary_reply",
                            segment_text,
                            "partial",
                            index=index,
                            media=delivery_medium(checked),
                            receipt=platform_id(sent),
                        )
                        core.save()
                        creation = event.get_extra("mengyu_sent_creation_path")
                        if creation and core.confirm_creation_in_chain(
                            creation, checked
                        ):
                            event.set_extra("mengyu_sent_creation_path", None)
                        if segment_text:
                            core.queue_task_event(
                                "assistant",
                                f"{delivery_id}:{index}",
                                segment_text,
                                time.time(),
                            )
                        if (
                            meme_delivery
                            and core.state.get("last_meme_delivery") is meme_delivery
                            and any(core.is_meme_image(part) for part in group)
                        ):
                            meme_delivery["status"] = "delivered"
                        if segment_text:
                            try:
                                record_delivered_wishes(
                                    core.wish_path,
                                    segment_text,
                                    time.time(),
                                    delivery_id,
                                )
                            except Exception as exc:  # noqa: BLE001
                                logger.warning(
                                    "[Mengyu wishes] Later bubble wish record skipped: %s",
                                    type(exc).__name__,
                                )
                            if turn_record is None:
                                turn_record = core.append("assistant", segment_text)
                                turn_record["delivery_id"] = delivery_id
                            else:
                                turn_record["text"] = (
                                    str(turn_record.get("text", ""))
                                    + "\n"
                                    + segment_text
                                )[:1500]
                            turn_record["delivery"] = "partial"
                            core.note_gift_mentions(segment_text)
                            if core.state.get("answered_tail") is answered:
                                answered["reply"] = (
                                    (answered["reply"] + "\n")
                                    if answered["reply"]
                                    else ""
                                ) + segment_text
                                answered["reply"] = answered["reply"][:1200]
                                core.record_delivered_meal(
                                    answered["reply"], time.time(), delivery_id
                                )
                                if mark_shared(
                                    core.life, answered["reply"], time.time()
                                ):
                                    settle_offer(core.state, core.life)
                                    core.save_life()
                        if core.state.get("answered_tail") is answered:
                            answered["delivery"] = "partial"
                        core.save()
                async with core.lock:
                    if core.state.get("answered_tail") is answered:
                        answered["delivery"] = "complete" if all_sent else "partial"
                    if turn_record is not None:
                        turn_record["delivery"] = "complete" if all_sent else "partial"
                    ledger_entry = (core.state.get("delivery_ledger") or {}).get(
                        delivery_id
                    )
                    if isinstance(ledger_entry, dict):
                        ledger_entry["status"] = "complete" if all_sent else "partial"
                        ledger_entry["updated_at"] = time.time()
                    if (
                        all_sent
                        and core.state.get("answered_tail") is answered
                        and answered["user_ts"] == core.state.get("last_user_ts", 0)
                    ):
                        if answered["reply"]:
                            core.capture_activity_commitment(
                                answered["reply"], delivery_id + ":complete"
                            )
                            try:
                                acknowledge_gift_notices(
                                    core.gift_path,
                                    core.state,
                                    time.time(),
                                    source_id=delivery_id + ":complete",
                                    reply_text=answered["reply"],
                                )
                            except Exception as exc:  # noqa: BLE001
                                logger.warning(
                                    "[Mengyu gifts] Delayed acknowledgement skipped: %s",
                                    type(exc).__name__,
                                )
                            record_shopping(
                                core.state,
                                core.current_life(),
                                event.get_extra("mengyu_shopping_update"),
                                time.time(),
                            )
                            SleepService(core.state).commit_choice(
                                answered["reply"],
                                answered["user"],
                                time.time(),
                                event.get_extra("mengyu_sleep_phase"),
                            )
                            core.arm_conversation_followup(
                                answered, event.get_extra("mengyu_space_decision")
                            )
                    core.save()
            finally:
                # Also sync a partial first bubble if a later bubble fails,
                # is cancelled by a new user turn, or the sender raises.
                async with core.lock:
                    ledger_entry = (core.state.get("delivery_ledger") or {}).get(
                        delivery_id
                    )
                    if isinstance(ledger_entry, dict):
                        if ledger_entry.get("status") == "reserved":
                            ledger_entry["status"] = "partial"
                        core.sync_delivery_events(delivery_id)
                        core.save()

        core.spawn_background(send_rest())
    await core.inbox.commit(sequence)
    async with core.lock:
        creation = event.get_extra("mengyu_sent_creation_path")
        if creation and core.confirm_creation_in_chain(creation, result.chain):
            event.set_extra("mengyu_sent_creation_path", None)
        used = [
            meme_category(getattr(part, "file", "") or getattr(part, "path", ""))
            for part in result.chain
        ]
        used = [item for item in used if item]
        if used:
            core.state["recent_meme_categories"] = (
                list(core.state.get("recent_meme_categories", [])) + used
            )[-6:]
        core.state["last_reply_choice"] = "reply"
        if answered["activity_notice"] or _has_activity_promise(core.state, delivery_id) or not absence_followup_allowed(
            answered.get("user", ""),
            conversation_paused=bool(core.state.get("conversation_paused")),
            self_space_active=bool(core.state.get("self_space", {}).get("active")),
        ):
            core.state["absence_due_ts"] = 0
        else:
            core.state["absence_due_ts"] = time.time() + 420
            core.state["absence_user_ts"] = core.state.get("last_user_ts", 0)
        core.save()


def arm_conversation_followup(core, answered, space_choice=None):
    """Arm a contextual continuation only after the complete reply was delivered.

    Args:
        answered: Confirmed delivered exchange with its source user timestamp.
        space_choice: Explicit SPACE/BACK decision from this response, if any.
    """
    now = time.time()
    if answered.get("delivery") != "complete" or answered.get(
        "user_ts"
    ) != core.state.get("last_user_ts", 0):
        return
    topic_state.cancel(core.state, "new_reply")
    reply = str(answered.get("reply") or "")
    topic_record = topic_state.note_reply(
        core.state,
        str(answered.get("user") or ""),
        reply,
        answered.get("delivery") == "complete",
        now,
    )
    if space_choice == "SPACE" and explicit_self_space(reply):
        core.state["self_space"] = {
            "active": True,
            "since": now,
            "reason": "已说清想暂停聊一会",
        }
        core.state["absence_due_ts"] = 0
        core.state.pop("followup_thread", None)
    elif space_choice == "BACK":
        core.state["self_space"] = {
            "active": False,
            "since": now,
            "reason": "已重新接话",
        }
    else:
        expire_self_space(core.state, now)
    # Presence is authoritative and was processed from original messages;
    # do not infer a second pause from the merged assistant-turn snapshot.
    if (
        core.state.get("conversation_paused")
        or answered.get("activity_notice")
        or core.is_effectively_asleep(now)
        or (space_choice == "SPACE" and explicit_self_space(reply))
    ):
        return
    if _has_activity_promise(core.state, answered.get("delivery_id")):
        core.state["absence_due_ts"] = 0
        return
    initiative = answered.get("initiative")
    if isinstance(initiative, dict) and initiative.get("share") is False:
        core.state.pop("followup_topic", None)
        return
    own_share = bool(
        isinstance(initiative, dict)
        and initiative.get("share") is True
        and initiative.get("topic")
    )
    space = core.state.get("self_space") or {}
    willingness = r"再.{0,5}(?:聊|唠)|陪.{0,5}(?:聊|唠)|想.{0,5}(?:跟你|和你).{0,3}聊|回来找你|接着聊"
    wish = core.state.get("own_choice") or {}
    wants_chat = bool(
        re.search(willingness, reply)
        and answered.get("at", now) >= space.get("since", 0)
        and not re.search(
            r"(?:不想|不再|先不).{0,3}(?:聊|唠)|不陪.{0,4}(?:聊|唠)|别聊", reply
        )
    ) or bool(
        wish.get("at", 0) <= now
        and wish.get("at", 0) > space.get("since", 0)
        and re.search(willingness, str(wish.get("wish") or ""))
        and not re.search(
            r"(?:不想|不再|先不).{0,3}(?:聊|唠)|不陪.{0,4}(?:聊|唠)|别聊",
            str(wish.get("wish") or ""),
        )
    )
    if space.get("active") and (
        wants_chat or (own_share and answered.get("at", now) >= space.get("since", 0))
    ):
        core.state["self_space"] = {
            "active": False,
            "since": now,
            "reason": "已送达答复或当前愿望明确愿意继续聊",
        }
    user = str(answered.get("user") or "")
    if (own_share and initiative.get("more") is False) or not core.allow_followup:
        return
    if should_followup(
        reply,
        user,
        bool(core.state.get("self_space", {}).get("active")),
    ):
        topic = initiative["topic"] if own_share else user
        acknowledgement = is_ack(user)
        if acknowledgement and not own_share:
            thread = core.state.get("followup_thread") or {}
            topic = (
                thread.get("topic") if 0 <= now - thread.get("at", 0) <= 300 else None
            )
            topic = topic or next(
                (
                    turn["text"]
                    for turn in reversed(core.state.get("turns", []))
                    if turn.get("role") == "user"
                    and turn.get("text")
                    and not is_ack(turn["text"])
                    and 0 <= now - turn.get("at", 0) <= 300
                ),
                reply,
            )
        kind = (
            "ack"
            if acknowledgement
            else "question"
            if re.search(r"[?？]", reply)
            else "statement"
        )
        recent_replies = [
            str(turn.get("text") or "")
            for turn in core.state.get("turns", [])[-8:]
            if str(turn.get("role", "")).startswith("assistant")
        ][-3:]
        if repeats_recent(reply, recent_replies):
            core.state["followup_due_ts"] = 0
            return
        queue_followup(
            core.state, topic or reply, reply, answered["user_ts"], now, kind=kind
        )
        if due := core.state.get("followup_due_ts", 0):
            topic_state.arm_followup(
                core.state, topic_record, due, answered.get("delivery_id"), now,
            )


async def repair_chat_echo(core, text, user_text):
    """Review suspicious reuse of an already answered personal quotation.

    Args:
        text: Current public response candidate.
        user_text: Latest merged user message.

    Returns:
        Original reply or a bounded rewrite that addresses the current message.
    """
    review_user = needs_user_evaluation_review(text, user_text)
    recent_turns = core.state.get("turns", [])[-24:]
    invalid_quotes = unverified_user_quotes(
        text,
        recent_turns,
        current_user=user_text,
        now=time.time(),
        window=3600,
    )
    if (
        not invalid_quotes
        and not review_user
        and not re.search(
            r"你说|你这句|刚才你|刚才那句|刚刚那句|听着|听了|得意|害羞|想起你",
            text,
        )
    ):
        return text
    recent = [
        item
        for item in core.state.get("turns", [])[-18:]
        if str(item.get("role", "")).startswith("assistant")
        and time.time() - item.get("at", 0) <= 3600
    ]
    anchors = []
    for quote in re.findall(r"[“\"]([^”\"\n]{3,60})[”\"]", text):
        for item in recent:
            previous = item.get("text", "")
            if quote in previous:
                anchors.append(quote)
            anchors.extend(
                old
                for old in re.findall(r"[“\"]([^”\"\n]{3,60})[”\"]", previous)
                if old in quote
            )
    anchors = list(dict.fromkeys(anchors))
    if not anchors and not review_user and not invalid_quotes:
        return text
    try:
        provider = await core.chat_provider()
        response = await asyncio.wait_for(
            provider.text_chat(
                system_prompt=core.persona_prompt()
                + '\n检查这条普通聊天回复是否有以下问题：一是把已经回应过的旧夸奖、称呼或亲昵换词重讲；二是把正常聊天变成对用户的点评、人物画像或回顾总结；三是把没有出现在最近真实用户消息里的内容写成“你刚才说/你那句”或加引号归给用户。用户原话必须逐字能在role=user消息中找到，不能从关系记忆、旧对话、助手自己的回复或推测中补出赞美、承诺和态度。若引用没有依据，删除整句，不得猜测替换。尤其用户问“你呢/那你呢/你怎么看”时，回复应贡献梦雨自己的观点、偏好、做法或联想，不要转而夸他、评价他说话或总结他的表现。只有用户明确要求评价、点评、分析或夸奖时，才保留针对用户的评价；自然接住直接调情也可以保留一句，不要机械删掉亲近。若存在问题，改成直接回应当前话题、把内容主体放回梦雨自己的想法；讨论创作就说自己会怎样处理画面、动作或人物选择。保留有用的新内容，不编经历、工具结果或新承诺，不暴露检查过程，不硬加问题。若无问题只输出JSON：{"rewrite":false}；若需改写只输出JSON：{"rewrite":true,"reply":"修正后的完整正文"}。',
                prompt=json.dumps(
                    {
                        "当前用户消息": user_text,
                        "候选回复": text,
                        "重复的旧原话": anchors,
                        "是否存在未经请求的用户点评": review_user,
                        "最近完整聊天": recent_turns[-18:],
                        "未经证实的用户引用": invalid_quotes,
                    },
                    ensure_ascii=False,
                ),
                contexts=[],
                image_urls=[],
                session_id=None,
            ),
            30,
        )
        result = extract_json(response.completion_text or "") or {}
        if not invalid_quotes and (
            result.get("rewrite") is False
            or (result.get("rewrite") is None and result.get("repeat") is False)
        ):
            return text
        candidate = result.get("reply")
        if (
            (result.get("rewrite") is True or result.get("repeat") is True)
            and isinstance(candidate, str)
            and candidate.strip()
            and len(candidate) <= max(400, len(text) + 150)
            and "[[MENGYU_" not in candidate
            and not any(anchor in candidate for anchor in anchors)
            and not unverified_user_quotes(
                candidate,
                recent_turns,
                current_user=user_text,
                now=time.time(),
                window=3600,
            )
        ):
            return candidate.strip()
    except Exception as exc:  # noqa: BLE001 - retain useful current-topic clauses
        logger.warning("[Mengyu echo] Reply review unavailable: %s", type(exc).__name__)
    kept = "".join(
        part
        for part in re.split(r"(?<=[。！？!?\n])", text)
        if not any(anchor in part for anchor in anchors)
    ).strip()
    if invalid_quotes:
        kept = strip_unverified_user_quote_sentences(kept, invalid_quotes)
        if not kept:
            return "我刚才可能接错了，不乱替你补话。"
    return kept or text


async def repair_meal_reply(core, text, user_text=""):
    service = core.plugin_service("mengyu_life") or meal_service
    return await service.repair_meal_reply(core, text, user_text)
