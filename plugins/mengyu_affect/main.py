import time

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Star, register
from data.plugins.mengyu_affect.core import affect_engine
from data.plugins.mengyu_contact.plugin_services import PeriodicWorker
from data.plugins.mengyu_contact.read_tools import read_tool

from . import operations


@register("mengyu_affect", "local", "互动情绪、关系变化和情绪记忆", "1.0.2")
class MengyuAffect(Star):
    ready = False
    record_event = staticmethod(affect_engine.record_event)
    record_mood = staticmethod(affect_engine.record_mood)

    interaction_context = staticmethod(operations.interaction_context)
    assess_interaction = staticmethod(operations.assess_interaction)
    save_interaction_signal = staticmethod(operations.save_interaction_signal)
    assess_interaction_silence = staticmethod(operations.assess_interaction_silence)

    def __init__(self, context):
        super().__init__(context)
        self.worker = PeriodicWorker(context, 60, self.life_tick)

    async def life_tick(self, core):
        """Let her own day move her mood; this never sends anything."""
        async with core.lock:
            if operations.observe_life(core, time.time()):
                core.save()

    async def initialize(self):
        try:
            from data.plugins.mengyu_contact import life_gate
            life_gate.guard("affect")
        except Exception:
            pass
        self.ready = True
        self.worker.start()

    async def terminate(self):
        self.ready = False
        await self.worker.stop()

    @filter.llm_tool(name="mengyu_affect")
    async def read_status(self, event: AstrMessageEvent, action: str = "status") -> str:
        """Check Mengyu's recorded feelings or recent shareable memories when asked how she feels or why she seems upset. Use the result to speak naturally. A missing mood is unknown.

        Args:
            action(string): status (default) or memories.

        Returns:
            Compact read-only JSON or an error code.
        """
        return await read_tool(self, event, "affect", action)
