"""Resolve Contact settings, deployment overrides and plugin data paths."""

import json
import logging
import os
from pathlib import Path

LOGGER = logging.getLogger(__name__)
DEFAULTS = {
    "interaction_mood_mode": "reply",
    "fact_mode": "enforce",
    "write_mode": "observe",
    "life_gate_mode": "shadow",
    "prompt_version": "chat-2026-11-24",
}
CHOICES = {
    "interaction_mood_mode": ("off", "observe", "reply"),
    "fact_mode": ("observe", "enforce"),
    "write_mode": ("observe", "protect"),
    "life_gate_mode": ("shadow", "soft", "hard"),
}
ENV = {
    "interaction_mood_mode": "MENGYU_INTERACTION_MOOD_MODE",
    "fact_mode": "MENGYU_FACT_MODE",
    "write_mode": "MENGYU_WRITE_MODE",
    "prompt_version": "MENGYU_PROMPT_VERSION",
}


def path(*parts, plugin="mengyu_contact"):
    """Return a path relative to the framework's plugin data directory."""
    from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

    return Path(get_astrbot_plugin_data_path()).joinpath(plugin, *parts)


def _read(pathname):
    try:
        data = json.loads(pathname.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict):
            return data
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        pass
    LOGGER.warning("[Mengyu config] Invalid settings file: %s", pathname.name)
    return {}


def get(name, default=None, *, root=None, gate_override=None):
    """Read a setting with existing environment and gate overrides."""
    root = path() if root is None else Path(root)
    fallback = DEFAULTS.get(name, default)
    value = _read(root / "config.json").get(name, fallback)
    if name == "life_gate_mode":
        override = (
            gate_override if gate_override is not None else root / "life_gate.json"
        )
        value = _read(override).get("mode", value)
    if name in ENV:
        value = os.environ.get(ENV[name], value)
    if name in CHOICES:
        value = value.strip().lower() if isinstance(value, str) else value
        if value not in CHOICES[name]:
            LOGGER.warning("[Mengyu config] Invalid option: %s", name)
            return "observe" if name == "interaction_mood_mode" else fallback
    if name == "prompt_version":
        if not isinstance(value, str) or not value.strip() or len(value) > 80:
            LOGGER.warning("[Mengyu config] Invalid option: %s", name)
            return fallback
        return value.strip()
    return value


def feature(name, default=False, *, root=None):
    """Read an explicitly configured boolean feature flag."""
    value = get(name, default, root=root)
    return value if type(value) is bool else default


def env_text(name, default=""):
    """Read an optional deployment override."""
    return os.environ.get(name, default).strip()


def portable_path(name, default):
    """Resolve an optional service path without storing a machine-specific default."""
    return Path(env_text(name) or default).expanduser()


def owner_id():
    return env_text("MENGYU_OWNER_ID")


def message_origin():
    owner = owner_id()
    origin = env_text("MENGYU_OWNER_ORIGIN", f"napcat:FriendMessage:{owner}")
    fields = origin.split(":", 2)
    if owner and len(fields) == 3 and fields[0] and fields[1] == "FriendMessage" and fields[2] == owner:
        return origin
    return ""
