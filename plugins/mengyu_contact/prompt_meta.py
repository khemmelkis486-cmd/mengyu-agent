"""Prompt version and per-layer measurements for the chat path.

The five layers the update plan asks for are: action boundaries, persona,
current conversational move, this-turn facts, and on-demand capabilities.
This module only labels and measures; text still comes from the existing
builders until the offline comparison is done.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import config

PROMPT_VERSION = config.DEFAULTS["prompt_version"]
LAYER_NAMES = ("fixed", "turn", "static")


def measure(parts):
    """Character count for each layer from ``{name: [strings]}``."""
    return {name: sum(len(str(item)) for item in parts.get(name, [])) for name in LAYER_NAMES}


def log_usage(path, parts, *, topic=None, extra=None, version=None):
    """Append one prompt measurement row; never raise on IO problems."""
    sizes = measure(parts)
    row = {
        "at": time.time(),
        "version": version or config.get("prompt_version", root=Path(path).parent.parent),
        "chars": sizes,
        "total": sum(sizes.values()),
        "topic_id": (topic or {}).get("topic_id"),
        "topic_phase": (topic or {}).get("phase"),
    }
    if extra:
        row.update(extra)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return row
