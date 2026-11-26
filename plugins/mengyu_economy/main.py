"""AstrBot entry point for Mengyu's standalone wallet."""

import asyncio
import sqlite3

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

from .service import Wallet
from .settings import Settings
from .store import dump

PLUGIN_NAME = "mengyu_economy"
MAX_OUTPUT_LENGTH = 500


@register(PLUGIN_NAME, "local", "梦雨的虚构生活钱包", "0.1.0")
class MengyuEconomy(Star):
    """Own the wallet life cycle and its read-only model tool."""

    def __init__(self, context: Context):
        super().__init__(context)
        self.wallet = None
        self.ready = False
        self._worker = None
        self._stop = asyncio.Event()
        self._unavailable_reason = "wallet_inactive"

    async def initialize(self):
        """Load an opted-in wallet and start the independent settlement loop."""
        try:
            root = StarTools.get_data_dir(PLUGIN_NAME)
            settings = await asyncio.to_thread(Settings.load, root / "config.json")
            if settings is None:
                return
            wallet = Wallet(root / "wallet.sqlite3", settings)
            await asyncio.to_thread(wallet.initialize)
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
            self._unavailable_reason = "wallet_unavailable"
            logger.exception("Mengyu wallet initialization failed")
            return
        self.wallet = wallet
        self.ready = True
        self._stop.clear()
        self._worker = asyncio.create_task(self._settlement_loop(wallet))

    async def terminate(self):
        """Stop accepting queries and wait for the active settlement to finish."""
        self.ready = False
        self._stop.set()
        if self._worker:
            await self._worker
            self._worker = None
        self.wallet = None

    async def _settlement_loop(self, wallet):
        """Run due settlements without using Contact or a model provider.

        Args:
            wallet: The initialized wallet instance for this plugin lifetime.
        """
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(), wallet.settings.interval_seconds
                )
                return
            except TimeoutError:
                if self._stop.is_set():
                    return
            try:
                await asyncio.to_thread(wallet.settle_due)
            except (OSError, sqlite3.Error, ValueError):
                logger.exception(
                    "Mengyu wallet settlement failed; the next tick will retry"
                )

    @filter.llm_tool(name="mengyu_economy")
    async def read_wallet(self, event: AstrMessageEvent, action: str = "status") -> str:
        """Read Mengyu's fictional wallet, budget or past expenses in the owner's private chat.

        Args:
            action(string): status, budget, recent, goals, wishlist, or monthly.

        Returns:
            Compact read-only JSON with amounts in cents.
        """
        wallet = self.wallet
        if not self.ready or wallet is None:
            return dump({"ok": False, "error": self._unavailable_reason})
        settings = wallet.settings
        if (
            not settings.owner_origin
            or str(event.unified_msg_origin) != settings.owner_origin
            or str(event.get_sender_id()) != settings.owner_id
        ):
            return dump({"ok": False, "error": "forbidden"})
        try:
            result = await asyncio.to_thread(wallet.read, action)
        except (OSError, sqlite3.Error, ValueError):
            logger.exception("Mengyu wallet query failed")
            return dump({"ok": False, "error": "wallet_unavailable"})
        encoded = dump(result)
        entries = result.get(action) if isinstance(action, str) else None
        if isinstance(entries, list):
            while entries and len(encoded) > MAX_OUTPUT_LENGTH:
                entries.pop()
                result["truncated"] = True
                encoded = dump(result)
        if len(encoded) <= MAX_OUTPUT_LENGTH:
            return encoded
        return dump({"ok": False, "error": "result_too_large"})
