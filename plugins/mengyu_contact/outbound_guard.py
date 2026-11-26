"""Final outbound guard: a promised photo must ship with a real photo.

The chain may contain a meme or nothing at all while the text says "拍好了".
Only an image file under this plugin's creations directory counts as a photo.
"""

from __future__ import annotations

import re
from pathlib import Path

_CLAIM = re.compile(r"拍好|拍到了|刚拍|给你看|发你|看看这张|这张照片|看这张")
_PHOTO = re.compile(r"照|图|自拍|镜头")


def _visible_text(chain):
    return "".join(str(getattr(part, "text", "")) for part in chain)


def _real_photo(part, root):
    raw = getattr(part, "file", None) or getattr(part, "path", None)
    if not raw:
        return False
    text = str(raw)
    if text.startswith("file:///"):
        text = text[8:]
    try:
        path = Path(text).resolve()
        return path.is_file() and path.is_relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return False


_PURCHASE = re.compile(r"(?:我)?(?:刚|今天|昨天)?(?:买|买下|入手)了([^\s，。！？]{1,12})")


def strip_unbacked_purchase(text, items):
    """Downgrade a bought-claim when no matching purchase exists."""
    names = [str(item.get("name") or "") for item in items or []]

    def replace(match):
        thing = match.group(1)
        if any(name and (name in thing or thing in name) for name in names):
            return match.group(0)
        return "刚看到" + thing

    return _PURCHASE.sub(replace, str(text or ""))


def check(chain, creations_root):
    """Return ``(ok, reason)`` for the final outgoing chain.

    Args:
        chain: Message components about to be sent.
        creations_root: Directory that holds real generated photos.
    """
    text = _visible_text(chain)
    if not (_CLAIM.search(text) and _PHOTO.search(text)):
        return True, ""
    if any(_real_photo(part, creations_root) for part in chain):
        return True, ""
    return False, "photo_missing"
