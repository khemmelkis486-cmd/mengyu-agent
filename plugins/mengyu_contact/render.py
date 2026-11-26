"""Prepare final QQ components, verify bound media and split natural chat bubbles."""

import asyncio
import re
import uuid
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image, Plain
from data.plugins.mengyu_media.core import photo_activity

from . import outbound_guard
from .conversation_flow import chat_bubbles
from .image_intent import selfie_request, without_unattached_photo_claims
from .meme_bridge import explicit_emote_request, extract_meme
from .state_write import stable_id, write_trigger


async def render_qq_faces(core, event: AstrMessageEvent):
    if str(event.unified_msg_origin) != core.message_origin:
        return
    get_extra = getattr(event, "get_extra", None)
    if get_extra and (
        get_extra("mengyu_superseded")
        or not core.inbox.is_current(get_extra("mengyu_burst_seq"))
    ):
        result = event.get_result()
        if result is not None:
            result.chain = []
        tracked = get_extra("mengyu_tracked_reply")
        if tracked:
            async with core.lock:
                core.state["turns"] = [
                    item for item in core.state["turns"] if item is not tracked
                ]
                core.save()
        event.set_extra("meme_manager_pending_images", None)
        event.stop_event()
        return
    if get_extra and get_extra("mengyu_silent"):
        result = event.get_result()
        if result is not None:
            result.chain = []
        event.set_extra("meme_manager_pending_images", None)
        event.stop_event()
        return
    result = event.get_result()
    if result is None or not result.is_llm_result():
        return
    creation = event.get_extra("mengyu_creation_path")
    photo = core.saved_photo(creation)
    selfie = bool(event.get_extra("mengyu_selfie_requested")) or selfie_request(
        getattr(event, "message_str", "")
    )
    if selfie:
        event.set_extra("mengyu_voice_processed", True)
        event.set_extra("meme_manager_pending_images", None)
    binding = photo.get("photo_activity") if photo else None
    if (photo and not core.activities.live_photo_ok(photo)) or (
        selfie
        and (
            not photo
            or photo.get("subject") != "mengyu"
            or not Path(creation).is_file()
        )
    ):
        event.set_extra("mengyu_voice_processed", True)
        event.set_extra("mengyu_creation_path", None)
        event.set_extra("mengyu_bound_photo", False)
        event.set_extra("meme_manager_pending_images", None)
        result.chain = [Plain("这张没发成。")]
        creation, binding = None, None
    elif binding:
        event.set_extra("mengyu_bound_photo", True)
        event.set_extra("mengyu_activity_id", binding["activity_id"])
        event.set_extra("mengyu_voice_processed", True)
        event.set_extra("meme_manager_pending_images", None)
        result.chain = [Plain(photo_activity.caption(binding))]
    else:
        current_binding = core.activities.live_photo_binding()
        draft = "".join(part.text for part in result.chain if isinstance(part, Plain))
        correction = photo_activity.answer(
            current_binding,
            str(getattr(event, "message_str", "") or ""),
            draft,
            event.get_extra("mengyu_photo_reply_context"),
        )
        if correction:
            if current_binding:
                event.set_extra("mengyu_activity_id", current_binding["activity_id"])
            result.chain = [
                Plain(correction),
                *(part for part in result.chain if not isinstance(part, Plain)),
            ]
    converted = []
    approved = core.review_chain_text(result.chain, "reply", event=event)
    if not approved and result.chain:
        fallback = core.factual_reply_fallback(event)
        if fallback:
            approved = [Plain(fallback)]
    for component in approved:
        if isinstance(component, Plain) and "[[QQ:" in component.text:
            converted.extend(core.build_chain(component.text).chain)
        else:
            converted.append(component)
    converted = [
        part
        for part in converted
        if not (isinstance(part, Plain) and not str(part.text).strip())
    ]
    get_extra = getattr(event, "get_extra", None)
    pending = list(get_extra("meme_manager_pending_images") or []) if get_extra else []
    reply_text = "".join(
        str(part.text) for part in converted if isinstance(part, Plain)
    )
    if pending and re.search(r"晚安|睡了|闭眼|先睡|去睡|困得|不聊了", reply_text):
        pending = []
    if pending:
        converted.extend(pending)
        event.set_extra("meme_manager_pending_images", None)
    creation = get_extra("mengyu_creation_path") if get_extra else None
    if creation:
        path = Path(creation).resolve()
        if (
            path.is_relative_to((core.data_root / "creations").resolve())
            and path.is_file()
            and path.suffix == ".png"
        ):
            converted.append(Image.fromFileSystem(str(path)))
            event.set_extra("mengyu_sent_creation_path", str(path))
        event.set_extra("mengyu_creation_path", None)
    user = str(getattr(event, "message_str", "") or "")
    recent = [
        item.get("text", "")
        for item in core.state.get("turns", [])
        if item.get("role") == "user"
    ]
    explicit = explicit_emote_request(user, recent[:-1])
    tracked = event.get_extra("mengyu_tracked_reply") if get_extra else None
    selected_markers = re.findall(
        r"&&([a-zA-Z0-9_-]{1,50})&&", str((tracked or {}).get("text", ""))
    )
    if (
        (explicit or selected_markers)
        and not pending
        and not any(core.is_meme_image(part) for part in converted)
        and not (get_extra and get_extra("mengyu_voice_requested"))
        and not selfie
        and not (get_extra and get_extra("mengyu_sent_creation_path"))
    ):
        choice = str((tracked or {}).get("text", "")) or "".join(
            part.text for part in converted if isinstance(part, Plain)
        )
        choice = re.sub(r"[😗😙😚😘💋]", "&&kiss&&", choice)
        _, selected = extract_meme(
            choice,
            core.meme_catalog(),
            core.state.get("last_meme_file", ""),
            core.state.get("recent_meme_files", []),
        )
        if selected:
            converted = [
                Plain(re.sub(r"&&[a-zA-Z0-9_-]{1,50}&&|[😗😙😚😘💋]", "", part.text))
                if isinstance(part, Plain)
                else part
                for part in converted
            ]
            converted.append(Image.fromFileSystem(selected))
            core.state["last_meme_file"] = selected
            core.state["recent_meme_files"] = (
                list(core.state.get("recent_meme_files", [])) + [selected]
            )[-8:]
    if re.search(r"(不要|别|不用|少).{0,4}(表情|发图|配图)", user):
        converted = [
            Plain(re.sub(r"&&[a-zA-Z0-9_-]{1,50}&&", "", part.text))
            if isinstance(part, Plain)
            else part
            for part in converted
            if not core.is_meme_image(part)
        ]
    if not event.get_extra("mengyu_sent_creation_path") or not photo:
        converted = [
            Plain(without_unattached_photo_claims(part.text))
            if isinstance(part, Plain)
            else part
            for part in converted
        ]
    ok, guard_reason = outbound_guard.check(converted, core.data_root / "creations")
    if not ok:
        converted = [Plain("这张没发成。")]
        logger.info("[Mengyu images] Blocked photo claim (%s)", guard_reason)
    try:
        from data.plugins.mengyu_life.economy_bridge import recent_items

        items = recent_items(3)
        converted = [
            Plain(outbound_guard.strip_unbacked_purchase(part.text, items))
            if isinstance(part, Plain) else part
            for part in converted
        ]
    except Exception:  # noqa: BLE001 - wallet is optional
        pass
    async with core.lock:
        result.chain = core.limit_faces(converted, explicit)
        core.save()
    requested = selected_markers
    if explicit or requested or any(core.is_meme_image(part) for part in result.chain):
        event.set_extra("mengyu_meme_requested", requested or ["explicit"])
    result._mengyu_event = event
    await core.split_into_bubbles(result)
    if not result.chain:
        return
    mid = getattr(getattr(event, "message_obj", None), "message_id", None)
    delivery_id = stable_id("reply", mid) if mid else "reply:" + uuid.uuid4().hex
    event.set_extra("mengyu_delivery_id", delivery_id)
    async with core.lock:
        reply_user_ts = event.get_extra("mengyu_emotion_user_ts")
        if (
            not core.inbox.is_current(event.get_extra("mengyu_burst_seq"))
            or (
                reply_user_ts is not None
                and reply_user_ts != core.state.get("last_user_ts", 0)
            )
            or (core.state.get("delivery_ledger") or {}).get(delivery_id)
        ):
            result.chain = []
            event.set_extra("mengyu_silent", True)
            event.stop_event()
            return
        if binding and (
            not core.activities.live_photo_ok(photo)
            or not any(
                isinstance(part, Plain) and part.text == photo_activity.caption(binding)
                for part in result.chain
            )
            or not any(isinstance(part, Image) for part in result.chain)
        ):
            result.chain = [Plain("这张没发成。")]
            event.set_extra("mengyu_activity_id", None)
            event.set_extra("mengyu_sent_creation_path", None)
        core.attach_activity_notice(event, result, delivery_id)
        entry = core.record_delivery_segment(
            delivery_id, "ordinary_reply", "", "reserved"
        )
        if activity_id := event.get_extra("mengyu_activity_id"):
            entry["activity_id"] = activity_id
        core.note_transport(delivery_id, "ordinary_reply", 0, "reserved")
        with write_trigger(delivery_id):
            core.save()


async def split_into_bubbles(core, result):
    """Send each natural clause as its own short QQ message.

    Args:
        result: The LLM result whose chain is about to be sent.
    """
    event = getattr(result, "_mengyu_event", None)
    get_extra = getattr(event, "get_extra", None) if event is not None else None
    if get_extra and (
        get_extra("mengyu_voice_requested") or get_extra("mengyu_bound_photo")
    ):
        return

    groups = []
    for part in result.chain:
        if isinstance(part, Plain):
            groups.extend([Plain(piece)] for piece in chat_bubbles(part.text))
        elif groups:
            groups[-1].append(part)
        else:
            groups.append([part])
    if len(groups) < 2:
        return
    if (
        len(groups) > 2
        and sum(len(str(part.text)) for part in result.chain if isinstance(part, Plain))
        > 200
    ):
        remainder = []
        for group in groups[1:]:
            for item in group:
                if (
                    remainder
                    and isinstance(remainder[-1], Plain)
                    and isinstance(item, Plain)
                ):
                    remainder[-1] = Plain(remainder[-1].text + "\n" + item.text)
                else:
                    remainder.append(item)
        groups = [groups[0], remainder]
    result.chain = groups[0]
    event = getattr(result, "_mengyu_event", None)
    if event is not None:
        event.set_extra("mengyu_bubble_rest", groups[1:])
    else:
        rest = groups[1:]

        async def send_rest():
            for group in rest:
                await asyncio.sleep(1.0)
                group = core.review_chain_text(group, "reply_bubble")
                if not group:
                    return
                try:
                    await core.context.send_message(
                        core.message_origin, MessageChain(chain=group)
                    )
                except Exception:  # noqa: BLE001
                    return

        core.spawn_background(send_rest())
