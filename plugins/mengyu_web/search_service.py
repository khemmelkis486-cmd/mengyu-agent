"""One web-search entry point shared by every Mengyu plugin.

Wraps the existing authenticated Luna search so callers only deal with
``query -> [{title, url, snippet}]``. Results are cached briefly because the
same activity detail is often used by several messages.
"""

from __future__ import annotations

import asyncio
import time

import httpx
from astrbot.api import logger

from .native_search import search_native

_CACHE = {}
_CACHE_TTL = 15 * 60
_FAIL_TTL = 60
_ATTEMPTS = 3


async def search_web(query, limit=3):
    """Search the web with retries; return an empty list instead of raising."""
    query = str(query or "").strip()[:280]
    if not query:
        return []
    cached = _CACHE.get(query)
    if cached and time.time() - cached[0] < _CACHE_TTL:
        return cached[1][:limit]
    for attempt in range(_ATTEMPTS):
        try:
            async with httpx.AsyncClient(timeout=100, trust_env=False) as client:
                rows = await search_native(client, query)
            rows = list(rows or [])
            _CACHE[query] = (time.time(), rows)
            return rows[:limit]
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            last = type(exc).__name__
            if attempt + 1 < _ATTEMPTS:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            logger.info("[Mengyu search] unavailable after retries: %s", last)
        except Exception as exc:  # noqa: BLE001 - search is optional context
            logger.info("[Mengyu search] unavailable: %s", type(exc).__name__)
            break
    _CACHE[query] = (time.time() - _CACHE_TTL + _FAIL_TTL, [])
    return []
