from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from data.plugins.mengyu_contact.plugin_services import PeriodicWorker
from data.plugins.mengyu_contact.read_tools import read_tool

from . import meal_service, operations, plan_runner


@register("mengyu_life", "local", "独立推进梦雨的作息、日程和活动", "1.1.0")
class MengyuLife(Star):
    ready = False

    life_tick = staticmethod(operations.life_tick)
    refresh_life = staticmethod(operations.refresh_life)
    record_delivered_meal = staticmethod(meal_service.record_delivered_meal)
    repair_meal_reply = staticmethod(meal_service.repair_meal_reply)

    def __init__(self, context: Context):
        super().__init__(context)
        self.worker = PeriodicWorker(context, 30, self.life_tick)
        self.plan_worker = PeriodicWorker(context, 60, self.plan_tick)

    async def plan_tick(self, core):
        """Advance the daily plan; it is the only writer of activity rows."""
        async with core.lock:
            plan_runner.tick(core)

    async def initialize(self):
        try:
            from data.plugins.mengyu_contact import life_gate
            life_gate.guard("life")
        except Exception:
            pass
        self.ready = True
        self.worker.start()
        self.plan_worker.start()

    async def terminate(self):
        self.ready = False
        await self.plan_worker.stop()
        await self.worker.stop()

    @filter.llm_tool(name="mengyu_life")
    async def read_status(self, event: AstrMessageEvent, action: str = "status") -> str:
        """Check what Mengyu is doing, where she is, or what she plans to do today. Use for casual questions when current details need checking. Plans are not completed events.

        Args:
            action(string): status (default) or schedule.

        Returns:
            Compact read-only JSON or an error code.
        """
        return await read_tool(self, event, "life", action)
