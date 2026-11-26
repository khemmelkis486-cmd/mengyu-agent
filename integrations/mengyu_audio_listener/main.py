"""Owner-only audio intake before AstrBot's per-conversation model lock."""

from data.plugins.mengyu_contact import config

import asyncio
import copy
import json
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Star, register

from .backend import (
    MAX_BYTES,
    AudioBackend,
    candidate,
    configure_decoder,
    result_context,
    validate_range,
)

UMO = config.message_origin()
DATA = config.path(plugin="mengyu_audio_listener")


@register("mengyu_audio_listener", "local", "本地音频转写、片段与副歌候选分析", "0.2.1")
class MengyuAudioListener(Star):
    def __init__(self, context):
        super().__init__(context)
        configure_decoder()
        self.backend = AudioBackend(DATA)

    def current(self, event):
        companion = self.context.get_registered_star("mengyu_contact")
        contact = getattr(companion, "star_cls", None)
        accepts = getattr(contact, "accepts_message_sequence", None)
        return not callable(accepts) or accepts(event.get_extra("mengyu_burst_seq"))

    @filter.event_message_type(filter.EventMessageType.ALL, priority=9000)
    async def receive(self, event: AstrMessageEvent):
        if str(event.unified_msg_origin) != UMO:
            return
        parts = [(p, candidate(p)) for p in event.get_messages() if candidate(p)]
        if not parts:
            return
        name = "语音"
        try:
            if len(parts) != 1:
                raise ValueError("一次请只发一份音频，以免混淆内容")
            part, kind = parts[0]
            name = str(getattr(part, "name", "") or "QQ语音")[:120]
            source = await asyncio.wait_for(
                part.convert_to_file_path() if kind == "voice" else part.get_file(), 30
            )
            path = Path(source)
            if not path.is_file() or not 0 < path.stat().st_size <= MAX_BYTES:
                raise ValueError("音频文件不存在、为空或超过25MB")
            source_info = await self.backend.retain(path, kind, name)
            value = await self.backend.analyze(path, kind, lambda: self.current(event))
            value = {
                **value,
                "name": name,
                "source_id": source_info["id"],
                "received_at": time.time(),
            }
        except asyncio.CancelledError:
            event.stop_event()
            return
        except Exception as exc:  # noqa: BLE001 - isolate third-party/media failures
            logger.warning("[Mengyu audio] Analysis failed: %s", type(exc).__name__)
            value = {"status": "error", "name": name, "detail": str(exc)[:240]}
        if not self.current(event):
            event.stop_event()
            return
        value.setdefault("received_at", time.time())
        event.set_extra("mengyu_audio_result", value)
        DATA.mkdir(parents=True, exist_ok=True)
        (DATA / "latest.json").write_text(
            json.dumps(value, ensure_ascii=False), encoding="utf-8"
        )
        event.message_str = (
            str(event.message_str or "") + "\n[用户发送音频：" + name + "]"
        ).strip()

    @filter.on_llm_request(priority=20000)
    async def inject(self, event, request):
        if str(event.unified_msg_origin) != UMO:
            return
        value = event.get_extra("mengyu_audio_result")
        tools = getattr(request, "func_tool", None)
        if tools:
            request.func_tool = copy.copy(tools)
            request.func_tool.tools = list(tools.tools)
            for index, tool in enumerate(tools.tools):
                if tool.name == "mengyu_listen_audio":
                    local = copy.copy(tool)
                    local.parameters = copy.deepcopy(tool.parameters)
                    fields = local.parameters.get("properties", {})
                    if "target" in fields:
                        fields["target"]["enum"] = ["music", "voice", "recent"] + [
                            r["id"] for r in self.backend.catalog()
                        ]
                    if "action" in fields:
                        fields["action"]["enum"] = ["segment", "chorus", "list"]
                    request.func_tool.tools[index] = local
        request.system_prompt += (
            "\n你现在可通过本地插件接收QQ语音和常见音频文件。只有本轮提供status=ok的分析数据才有实际听取依据；"
            "新歌曲默认先分析开头60秒，普通语音最多180秒。未收到结果不能猜已经听过，也不说不确定接口能不能听。"
            "有结果就结合眼下的话聊自己的反应，失败简短说清楚，不编听到了。"
            "他问先前发来的音频时，可用mengyu_hear_recent_audio读取最近一次真实分析；没有记录不能补编。"
            "他要求听副歌时，调用mengyu_listen_audio(action=chorus,target=music)，先定位候选再分析；"
            "他指定时间时用action=segment，start/end可写1:10等，不能只用旧开头结果猜指定片段。"
            "默认target=music找最近歌曲，后来的QQ语音不会顶掉它；需听刚才的语音用target=voice，"
            "不清楚指哪一份时先询问，不把别的文件结果套上。"
        )
        sources = [
            {k: r[k] for k in ("id", "name", "kind")} for r in self.backend.catalog()
        ]
        if sources:
            request.system_prompt += (
                "\n可重听的真实原音频（不是新事件）："
                + json.dumps(sources, ensure_ascii=False)
            )
        if value:
            request.audio_urls = []
            request.extra_user_content_parts.append(
                {"type": "text", "text": result_context(value)}
            )

    @filter.llm_tool(name="mengyu_hear_recent_audio")
    async def recent_audio(self, event: AstrMessageEvent) -> str:
        """Read the latest actual owner audio analysis, including its source and limited scope.

        Returns:
            Actual recent analysis only; no guessing or new file downloads.
        """
        if str(event.unified_msg_origin) != UMO:
            return "没有可供此会话读取的音频。"
        try:
            value = json.loads((DATA / "latest.json").read_text(encoding="utf-8"))
            if not 0 <= time.time() - value.get("received_at", 0) <= 86400:
                return "没有最近24小时的可用音频结果，请重新发送。"
            return result_context(value)
        except (OSError, ValueError, TypeError):
            return "没有已完成的音频分析；请发一份支持的音频文件。"

    async def terminate(self):
        await self.backend.close()

    @filter.llm_tool(name="mengyu_listen_audio")
    async def listen_segment(
        self,
        event: AstrMessageEvent,
        action: str = "segment",
        target: str = "music",
        start: str = "",
        end: str = "",
        occurrence: int = 1,
    ) -> str:
        """Listen to a chosen range or a candidate chorus in an actual retained owner audio.

        Args:
            action(string): segment for explicit time range, chorus for whole-track repetition scan then candidate excerpt, list for available source names.
            target(string): music for latest uploaded song, voice for latest QQ voice, recent for latest audio, or an exact id from list. Never a path or URL.
            start(string): Start time as seconds or mm:ss, e.g. 70 or 1:10. Used only for segment.
            end(string): End time as seconds or mm:ss. Omit for 30 seconds from start; maximum excerpt is 60 seconds.
            occurrence(int): Which repeated candidate passage to analyze, starting at 1; only for chorus. Role and boundaries remain uncertain.

        Returns:
            Actual excerpt evidence with absolute timestamps, or uncertainty/error. A whole-track structure scan does not mean all lyrics were heard.
        """
        if str(event.unified_msg_origin) != UMO:
            return "没有可供此会话读取的音频。"
        try:
            if action == "list":
                return json.dumps(
                    [
                        {k: v for k, v in r.items() if k != "file"}
                        for r in self.backend.catalog()
                    ],
                    ensure_ascii=False,
                )
            if action not in ("segment", "chorus") or not 1 <= occurrence <= 5:
                raise ValueError("不支持的音频操作或副歌次数")
            a, b = validate_range(start, end) if action == "segment" else (0, None)
            source = self.backend.source(target)
            value = await self.backend.analyze(
                source["path"],
                source["kind"],
                lambda: self.current(event),
                start=a,
                end=b,
                chorus=action == "chorus",
                occurrence=occurrence,
            )
            if not self.current(event):
                raise asyncio.CancelledError
            value = {
                **value,
                "name": source["name"],
                "source_id": source["id"],
                "received_at": source["received_at"],
                "analyzed_at": time.time(),
            }
            (DATA / "latest.json").write_text(
                json.dumps(value, ensure_ascii=False), encoding="utf-8"
            )
            return result_context(value)
        except asyncio.CancelledError:
            return json.dumps(
                {
                    "status": "cancelled",
                    "detail": "已有更新消息，旧音频处理已让出；没有新的完成结果",
                },
                ensure_ascii=False,
            )
        except Exception as exc:  # noqa: BLE001 - preserve chat on media failures
            return result_context({"status": "error", "detail": str(exc)[:240]})
