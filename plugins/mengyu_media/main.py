from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Star, register
from data.plugins.mengyu_contact.read_tools import read_tool

from . import operations
from .imagegen import generate_image


@register("mengyu_media", "local", "自拍、出图、视觉审核与照片任务", "1.0.1")
class MengyuMedia(Star):
    ready = False
    generate_image = staticmethod(generate_image)

    review_shopping_photo = staticmethod(operations.review_shopping_photo)
    review_photo = staticmethod(operations.review_photo)
    create_own_photo = staticmethod(operations.create_own_photo)
    fulfill_photo_promise = staticmethod(operations.fulfill_photo_promise)
    retry_photo_review = staticmethod(operations.retry_photo_review)
    handle_direct_selfie = staticmethod(operations.handle_direct_selfie)
    perform_image_tool = staticmethod(operations.perform_image_tool)

    async def initialize(self):
        try:
            from data.plugins.mengyu_contact import life_gate
            life_gate.guard("photo")
        except Exception:
            pass
        self.ready = True

    async def terminate(self):
        self.ready = False

    @filter.llm_tool(name="mengyu_media")
    async def read_status(self, event: AstrMessageEvent, action: str = "status") -> str:
        """Check whether a photo is ready, pending, or actually sent, or look up recent photos. To create a new selfie, use mengyu_activity with action=image; this lookup only reads records.

        Args:
            action(string): status (default), pending, or recent.

        Returns:
            Compact read-only JSON or an error code.
        """
        return await read_tool(self, event, "media", action)
