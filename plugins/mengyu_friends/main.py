"""AstrBot entry point for Mengyu's fictional friends."""

import asyncio
import json
import sqlite3

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Star, StarTools, register

from .service import Friends

NAME = "mengyu_friends"


@register(NAME, "local", "梦雨的朋友圈、邀约和见面记录", "0.2.0")
class MengyuFriends(Star):
    """Own the Friends database and offer read-only chat queries."""

    ready = False
    service = None

    async def initialize(self):
        """Load the independent friends service."""
        try:
            root = StarTools.get_data_dir(NAME)
            service = Friends(root / "friends.sqlite3")
            await asyncio.to_thread(service.initialize)
        except (OSError, sqlite3.Error, ValueError):
            logger.exception("Mengyu friends store initialization failed")
            return
        self.service = service
        self.ready = True

    async def terminate(self):
        """Stop exposing the service during unload."""
        self.ready = False
        self.service = None

    @filter.llm_tool(name="mengyu_friends")
    async def read_friends(
        self, event: AstrMessageEvent, action: str = "status", query: str = ""
    ) -> str:
        """Read Mengyu's recorded friends and factual meetup status.

        Args:
            action(string): status, list, detail, meetups, or recent.
            query(string): Name or alias for detail, or empty for all.

        Returns:
            Read-only JSON based on persisted friend records.
        """
        service = self.service
        if not self.ready or service is None:
            return '{"ok":false,"error":"friends_unavailable"}'
        from data.plugins.mengyu_contact.read_tools import ORIGIN, OWNER

        if (
            str(event.unified_msg_origin) != ORIGIN
            or str(event.get_sender_id()) != OWNER
        ):
            return '{"ok":false,"error":"forbidden"}'
        if action not in ("status", "list", "detail", "meetups", "recent"):
            return '{"ok":false,"error":"unknown_action"}'
        try:
            summary = await asyncio.to_thread(
                service.context_for_chat, query or "朋友", (), None, 3
            )
        except (OSError, sqlite3.Error, ValueError):
            logger.exception("Mengyu friends query failed")
            return '{"ok":false,"error":"friends_unavailable"}'
        keys = {
            "list": ("朋友",),
            "detail": ("朋友", "约见", "最近见面"),
            "meetups": ("约见",),
            "recent": ("最近见面",),
            "status": ("朋友", "约见", "最近见面"),
        }[action]
        return json.dumps(
            {"ok": True, **{key: summary[key] for key in keys}},
            ensure_ascii=False,
            separators=(",", ":"),
        )
