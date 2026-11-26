from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from data.plugins.mengyu_contact.plugin_services import PeriodicWorker
from data.plugins.mengyu_contact.read_tools import read_tool

from . import operations


@register("mengyu_proactive", "local", "决定主动联系时机、内容和分享频率", "1.0.1")
class MengyuProactive(Star):
    ready = False

    decide = staticmethod(operations.decide)
    tick = staticmethod(operations.tick)

    def __init__(self, context: Context):
        super().__init__(context)
        self.worker = PeriodicWorker(context, 15, self.tick)

    async def initialize(self):
        try:
            from data.plugins.mengyu_contact import life_gate
            life_gate.guard("proactive")
        except Exception:
            pass
        self.ready = True
        self.worker.start()

    async def terminate(self):
        self.ready = False
        await self.worker.stop()

    @filter.llm_tool(name="mengyu_proactive")
    async def read_status(self, event: AstrMessageEvent, action: str = "status") -> str:
        """Check when Mengyu last reached out and whether she can reach out now. Use for casual questions about why she has not messaged or when she might. The result does not promise or send a message.

        Args:
            action(string): status (default) or next.

        Returns:
            Compact read-only JSON or an error code.
        """
        return await read_tool(self, event, "proactive", action)
