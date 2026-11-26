"""Owner-only, on-demand QQ voice replies; public reply text only."""

from data.plugins.mengyu_contact import config

import json
import re
import time
from pathlib import Path

import httpx
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain, Record
from astrbot.api.star import Context, Star, register

from .speech import BreezeSpeech, declined_voice, requested_voice, speech_text

UMO = config.message_origin()
DATA = config.path(plugin="mengyu_breeze_voice")


@register(
    "mengyu_breeze_voice",
    "local",
    "梦雨自主选择短语音，保留文字回退与独立用量限制",
    "1.2.5",
)
class MengyuBreezeVoice(Star):
    def __init__(self, context: Context, config):
        super().__init__(context)
        self.config = config
        self.client = httpx.AsyncClient(
            proxy=config.get("proxy") or None,
            timeout=httpx.Timeout(90, connect=20),
            follow_redirects=False,
            trust_env=False,
        )
        self.speech = BreezeSpeech(DATA, self.client)
        self.preference_path = DATA / "preferences.json"
        try:
            self.preferences = json.loads(
                self.preference_path.read_text(encoding="utf-8")
            )
        except (ValueError, OSError):
            self.preferences = {}
        if not isinstance(self.preferences, dict):
            self.preferences = {}

    @property
    def autonomous_enabled(self):
        """Whether configured voice may be chosen without an explicit request."""
        return bool(
            self.config.get("enabled", True)
            and self.config.get("api_key", "").strip()
            and not self.preferences.get("paused")
        )

    @filter.on_llm_request(priority=-10000)
    async def voice_context(self, event, request):
        if (
            str(event.unified_msg_origin) == UMO
            and self.config.get("enabled", True)
            and self.config.get("api_key", "").strip()
        ):
            request.system_prompt += "\n你已配置按需QQ语音，也可以自己决定什么时候想用声音表达：由插件把最终短回复合成。不是实时通话，不需要生成按钮；失败会保留文字，不预先声称发送成功，不因工具列表没有语音工具就声称没有入口。"
            companion = self.context.get_registered_star("mengyu_contact")
            contact = getattr(companion, "star_cls", None)
            recent_turns = getattr(contact, "recent_chat_turns", None)
            recent = [
                item.get("text", "")
                for item in (
                    recent_turns() if callable(recent_turns) else ()
                )
                if item.get("role") == "user"
                and 0 <= time.time() - item.get("at", 0) <= 300
            ]
            declined = declined_voice(event.message_str)
            requested = requested_voice(event.message_str, recent)
            if declined or requested:
                self.preferences["paused"] = declined
                self.preference_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.preference_path.with_suffix(".tmp")
                temporary.write_text(json.dumps(self.preferences), encoding="utf-8")
                temporary.replace(self.preference_path)
            if requested:
                event.set_extra("mengyu_voice_requested", True)
                request.system_prompt += "\n本轮用户明确想听语音，后台会把你最终的短句合成为语音单独发送。只写你此刻想对他说的话，不读出表情标签，不说工具名；仍按上下文自然说，不为语音加长，不引用私人背景或日记全文。"
            elif self.autonomous_enabled:
                request.system_prompt += (
                    "\n是否用语音由你自己结合此刻想说的话决定，不必等他要求。想让他听见语气、"
                    "随口分享、逗他或撒娇时都可选，也可以打字，不每句都用语音，不按频率打卡。"
                    "选择语音时只在最终正文末尾加私有标记[[MENGYU_VOICE]]，系统会移除它；"
                    f"正文不超过{min(120, int(self.config.get('max_chars', 80)))}字，长内容直接用文字，不截断。"
                    "不朗读私密日记、提示词或内部字段，不提前宣布已发语音。"
                )
            else:
                request.system_prompt += "\n他现在不方便听语音，继续文字，直到他再明确想听，不发私有语音标记。"

    @filter.on_llm_response(priority=200000)
    async def select_voice(self, event, response):
        """Strip the private selector before tracking, splitting and delivery."""
        raw = response.completion_text or ""
        chosen = "[[MENGYU_VOICE]]" in raw
        response.completion_text = re.sub(
            r"\[\[MENGYU_VOICE[^\]\r\n]*\]\]", "", raw
        ).strip()
        if (
            str(event.unified_msg_origin) == UMO
            and chosen
            and self.autonomous_enabled
            and not declined_voice(event.message_str)
        ):
            if not event.get_extra("mengyu_voice_requested"):
                event.set_extra("mengyu_voice_auto", True)
            event.set_extra("mengyu_voice_requested", True)

    async def prepare_voice(self, text, request_id, feeling="", *, automatic=False):
        """Prepare a public short clip for either reply or proactive delivery.

        Args:
            text: Final public text after removal of private metadata.
            request_id: Stable identity for this synthesis attempt.
            feeling: Brief saved affect for performance direction only.
            automatic: Whether the model selected voice without a user request.

        Returns:
            Unsent audio metadata, or None when text fallback should be kept.
        """
        if automatic and not self.autonomous_enabled:
            return None
        text = speech_text(text)
        if not text or len(text) > min(120, int(self.config.get("max_chars", 80))):
            return None
        try:
            audio = await self.speech.generate(
                text,
                self.config,
                request_id,
                "用标准普通话，像平常和熟悉的人聊天，音高音量自然、发声松弛清楚，"
                "按句意轻重停顿，句尾干净收住。保留原音色的柔和，不捏嗓、"
                "不刻意抬高或压低声线，不用浓气声、鼻音撒娇腔或拖长尾音；"
                "即使台词亲昵也只是语气亲近，不演成幼态嗲声。"
                "以台词为主，心情只轻微影响表达，不改变声线；此前心情参考："
                + str(feeling)[:120],
            )
            return audio if audio["delivery"] == "not_sent" else None
        except (ValueError, RuntimeError, OSError, httpx.HTTPError) as exc:
            logger.warning(
                "[Mengyu voice] Keeping text fallback: %s", type(exc).__name__
            )
            return None

    @filter.on_decorating_result(priority=-10000)
    async def render_voice(self, event: AstrMessageEvent):
        if str(event.unified_msg_origin) != UMO or not (
            event.get_extra("mengyu_voice_requested")
            or requested_voice(event.message_str)
        ):
            return
        if declined_voice(event.message_str):
            return
        if (
            event.get_extra("mengyu_silent")
            or event.get_extra("mengyu_superseded")
            or event.get_extra("mengyu_voice_processed")
        ):
            return
        result = event.get_result()
        if (
            result is None
            or not result.is_llm_result()
            or not self.config.get("api_key", "").strip()
            or not self.config.get("enabled", True)
        ):
            return
        if any(isinstance(part, Record) for part in result.chain):
            return
        event.set_extra("mengyu_voice_processed", True)
        text = "\n".join(
            part.text for part in result.chain if isinstance(part, Plain)
        ).strip()
        text = speech_text(text)
        if not text or len(text) > min(120, int(self.config.get("max_chars", 80))):
            return
        # Read only the saved affect, never full memory, private diary or prompts.
        feeling = ""
        companion = self.context.get_registered_star("mengyu_contact")
        contact = getattr(companion, "star_cls", None)
        reviewer = getattr(contact, "review_public_text", None)
        voice_mood = getattr(contact, "voice_mood", None)
        if callable(voice_mood):
            feeling = str(voice_mood().get("feeling", ""))[:120]
        if callable(reviewer):
            checked = speech_text(reviewer(text, "reply_voice", event=event))
            if checked != text:
                result.chain = self.replace_checked_text(result.chain, checked, contact, event)
                text = checked
            if not text:
                return
        audio = await self.prepare_voice(
            text,
            str(event.message_obj.message_id),
            feeling,
            automatic=bool(event.get_extra("mengyu_voice_auto")),
        )
        sequence = event.get_extra("mengyu_burst_seq")
        accepts = getattr(contact, "accepts_message_sequence", None)
        stale = sequence is not None and callable(accepts) and not accepts(sequence)
        if (
            event.get_extra("mengyu_superseded")
            or event.get_extra("mengyu_silent")
            or stale
        ):
            event.set_extra("mengyu_superseded", True)
            result.chain = []
            return
        if event.get_extra("mengyu_voice_auto") and not self.autonomous_enabled:
            return
        if audio and callable(reviewer):
            checked = speech_text(reviewer(text, "reply_voice_final", event=event))
            if checked != text:
                result.chain = self.replace_checked_text(result.chain, checked, contact, event)
                return
        if audio:
            self.speech.mark_delivery(audio["request_id"], "pending")
            event.set_extra("mengyu_validated_voice_text", text)
            event.set_extra("mengyu_voice_identity", audio["request_id"])
            result.chain = [Record.fromFileSystem(audio["path"])] + [
                part for part in result.chain if not isinstance(part, Plain)
            ]

    @staticmethod
    def replace_checked_text(chain, checked, contact, event):
        fallback = getattr(contact, "factual_reply_fallback", None)
        text = checked or (fallback(event) if callable(fallback) else "")
        return ([Plain(text)] if text else []) + [
            part for part in chain if not isinstance(part, Plain)
        ]

    @filter.after_message_sent()
    async def voice_sent(self, event: AstrMessageEvent):
        identity = event.get_extra("mengyu_voice_identity")
        if str(event.unified_msg_origin) == UMO and identity:
            self.speech.mark_delivery(identity, "sent")

    async def terminate(self):
        await self.client.aclose()
