"""Shared web search for every Mengyu plugin."""

from astrbot.api.star import Star, register

from . import search_service


@register("mengyu_web", "local", "梦雨联网", "1.0.0")
class MengyuWeb(Star):
    search = staticmethod(search_service.search_web)

    async def initialize(self):
        pass

    async def terminate(self):
        pass
