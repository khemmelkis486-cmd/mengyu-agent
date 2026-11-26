"""Resolve live AstrBot plugin instances and manage companion background workers."""

import asyncio

from astrbot.api import logger

from . import state_owners


def active_plugin(context, name):
    lookup = getattr(context, "get_registered_star", None)
    metadata = lookup(name) if callable(lookup) else None
    if metadata is None or not getattr(metadata, "activated", True):
        return None
    return getattr(metadata, "star_cls", None)


class PeriodicWorker:
    """Own a single periodic task and resolve the current core on every iteration."""

    def __init__(self, context, interval, step, *, owner=None):
        self.context = context
        self.interval = interval
        self.step = step
        self.owner = owner or next((
            part.removeprefix("mengyu_")
            for part in step.__module__.split(".")
            if part.startswith("mengyu_")
        ), "contact")
        self.task = None

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())

    async def stop(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def run(self):
        while True:
            await asyncio.sleep(self.interval)
            core = active_plugin(self.context, "mengyu_contact")
            if not getattr(core, "ready", False):
                continue
            # The plugin owns the loop. The core also tracks this individual
            # step so reloading either participant cancels in-flight work.
            result, = await asyncio.gather(
                core.spawn_background(state_owners.run_as(self.owner, self.step, core)),
                return_exceptions=True,
            )
            if isinstance(result, Exception):
                logger.error(
                    "[Mengyu worker] %s failed", self.step.__name__,
                    exc_info=(type(result), result, result.__traceback__),
                )
