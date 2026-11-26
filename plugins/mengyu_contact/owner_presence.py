"""Owner availability state with ordered resume handling and bounded pauses.

This module deliberately treats a missing reply as silence, not as a request to
stop contacting. Only explicit pause language changes presence state.
"""

import re
import time

from .sleep_state import owner_pause_kind, user_paused

RESUME_RE = re.compile(
    r"我(?:已经|刚刚|刚才|刚)?(?:回来了|回来了呀|回来了啦|忙完了|下课了|到家了|回到家了|醒了|睡醒了)|"
    r"(?:刚回来|回来了|刚到家|到家了|回到家了|忙完了|下课了|睡醒了|醒了|不睡了|先不睡|继续聊|接着聊|我现在有空|现在有空了|吃完了|吃完饭了|吃好了|吃完啦)|"
    r"(?:我)?(?:刚刚|刚才|刚|已经)?(?:洗完了|洗完啦|洗完澡|洗完澡了|洗完澡啦|刚醒|起来了)",
    re.I,
)

EXPLICIT_RESUME = re.compile(r"^(?:好[了啦]?[，, ]*)?(?:我们|现在|可以|你可以|不用|不必)?(?:继续聊|接着聊|可以发消息了|发消息吧|来找我吧|恢复主动联系|解除静默|取消静默|不用静默了)")

_FUTURE_BEFORE = re.compile(r"(?:等我|等会|等一会|待会|一会儿|稍后|之后|再|先去|去).{0,12}$")
_FUTURE_AFTER = re.compile(r"^(?:再|之后|以后|就|回来|回来再|再来|再找|再聊)")


def _fragments(text):
    value = str(text or "").strip()
    if not value:
        return []
    # Process clauses in order. This handles both BurstInbox newlines and a
    # single message such as “我去洗澡，刚回来” without letting the first
    # clause overwrite the second.
    return [part.strip() for part in re.split(r"[\n，,。；;]+|\s+/\s+", value) if part.strip()]


def _is_resume(fragment):
    if EXPLICIT_RESUME.search(fragment):
        return True
    matches = list(RESUME_RE.finditer(fragment))
    if not matches:
        return False
    match = matches[-1]
    prefix = fragment[:match.start()].strip()
    suffix = fragment[match.end():].strip()
    # “我先去洗澡，洗完澡再找你” is a future plan, not a completed return.
    if _FUTURE_BEFORE.search(prefix) or _FUTURE_AFTER.search(suffix):
        return False
    if re.search(r"(?:不|没|还没)$", prefix):
        return False
    if re.search(r"(?:去|准备|打算|等会儿?|待会儿?).{0,8}$", prefix) and re.search(r"洗完|忙完|回来|到家", match.group(0)):
        return False
    return True


def _pause_kind(fragment):
    if re.search(r"别发|不要发|不许发|别打扰|不要打扰|不用回|先别聊|先不聊|不想聊", fragment):
        return "quiet"
    if re.search(r"晚安|我(?:先|要|去)?睡(?:了|啦|觉)|(?:^|[，,。\n])睡了", fragment) and not re.search(r"睡.{0,3}会|眯|午睡|午觉|小睡", fragment):
        return "night"
    if re.search(r"睡|眯|歇|休息|晕车", fragment):
        return "rest"
    if re.search(r"洗澡|洗漱|吃饭|吃东西|上课|考试|开会|出门|开车|坐车|工作|忙一会|先忙", fragment):
        return "busy"
    return "quiet"


def _duration(kind, fragment):
    if kind in ("night", "quiet"):
        return None
    if re.search(r"洗澡|洗漱", fragment):
        return 90 * 60
    if re.search(r"吃饭|吃东西", fragment):
        return 100 * 60
    if re.search(r"上课|考试|开会", fragment):
        return 4 * 3600
    if kind == "rest":
        return 2 * 3600
    return 2 * 3600


def _sync_legacy(state, presence):
    mode = presence.get("mode", "open")
    state["conversation_paused"] = mode != "open"
    if mode == "open":
        state.pop("owner_pause_kind", None)
    else:
        state["owner_pause_kind"] = mode


def apply_user_message(state, text, now):
    """Apply every clause in order; a later actual return wins over a pause."""
    now = float(now)
    presence = state.get("owner_presence")
    if not isinstance(presence, dict):
        legacy_paused = bool(state.get("conversation_paused"))
        legacy_kind = state.get("owner_pause_kind", "quiet")
        recent_users = [
            row for row in state.get("turns", [])
            if isinstance(row, dict) and row.get("role") == "user"
        ][-12:]
        mode = "open"
        since = now
        legacy_text = ""
        if legacy_paused:
            for row in recent_users:
                fragment = str(row.get("text") or "")
                for part in _fragments(fragment):
                    if _is_resume(part):
                        mode = "open"
                        since = float(row.get("at") or now)
                        legacy_text = part
                    elif user_paused(part):
                        mode = _pause_kind(part)
                        since = float(row.get("at") or now)
                        legacy_text = part
            if mode == "open" and not any(_is_resume(str(row.get("text") or "")) for row in recent_users):
                mode = legacy_kind
                since = float(state.get("last_user_ts", 0) or now)
        presence = {
            "mode": mode,
            "since": since,
            "expires_at": None,
            "source_text": legacy_text[:200] if legacy_text else ("legacy-state" if legacy_paused else ""),
        }
        if legacy_paused and mode in ("busy", "rest"):
            presence["expires_at"] = since + (_duration(mode, legacy_text) or 2 * 3600)

    before = presence.get("mode", "open")
    changed = False
    transitions = []
    for fragment in _fragments(text):
        if _is_resume(fragment):
            if presence.get("mode") == "quiet" and not EXPLICIT_RESUME.search(fragment):
                continue
            if presence.get("mode") != "open":
                presence.update(
                    mode="open", since=now, expires_at=None,
                    source_text=fragment[:200], resumed_at=now,
                )
                changed = True
                transitions.append("resumed")
            continue
        if user_paused(fragment):
            kind = _pause_kind(fragment)
            if presence.get("mode") == "quiet" and kind != "quiet":
                continue
            ttl = _duration(kind, fragment)
            expires_at = now + ttl if ttl else None
            if presence.get("mode") != kind or presence.get("source_text") != fragment[:200]:
                presence.update(
                    mode=kind, since=now, expires_at=expires_at,
                    source_text=fragment[:200],
                )
                changed = True
                transitions.append("paused:" + kind)

    state["owner_presence"] = presence
    _sync_legacy(state, presence)
    return {
        "changed": changed or before != presence.get("mode"),
        "before": before,
        "mode": presence.get("mode", "open"),
        "transitions": transitions,
        "expires_at": presence.get("expires_at"),
    }


def expire_temporary_pause(state, now):
    """Release bounded busy/rest pauses; explicit quiet and bedtime stay intact."""
    presence = state.get("owner_presence")
    if not isinstance(presence, dict) or presence.get("mode") not in ("busy", "rest"):
        return False
    expires_at = presence.get("expires_at")
    if not isinstance(expires_at, (int, float)):
        # Older deployments could persist a temporary pause with no expiry.
        # Reconstruct a bounded lifetime from the original event timestamp.
        origin = presence.get("since") or state.get("last_user_ts")
        if not isinstance(origin, (int, float)) or origin <= 0:
            presence["since"] = float(now)
            origin = float(now)
        expires_at = float(origin) + (_duration(presence["mode"], str(presence.get("source_text") or "")) or 2 * 3600)
        presence["expires_at"] = expires_at
    if float(now) < expires_at:
        return False
    presence.update(
        mode="open", since=float(now), expires_at=None,
        resumed_at=float(now), source_text="temporary-pause-expired",
    )
    _sync_legacy(state, presence)
    state["owner_pause_expired_at"] = float(now)
    return True


def release_night_presence(state, now):
    """Release only the contact-presence domain after SleepService did its work.

    SleepService must first set ``night_pause_released_at`` after validating
    and closing the sleep episode. This function never writes sleep fields.
    """
    released_at = state.get("night_pause_released_at")
    if not isinstance(released_at, (int, float)) or abs(float(now) - float(released_at)) > 120:
        return False
    presence = state.get("owner_presence")
    if not isinstance(presence, dict):
        presence = {}
    mode = presence.get("mode") or state.get("owner_pause_kind") or "open"
    if mode != "night":
        # A very old goodnight could live only in the legacy mirror.
        if mode != "quiet" or presence.get("source_text") not in ("legacy-state", ""):
            return False
    presence.update(
        mode="open", since=float(now), expires_at=None,
        resumed_at=float(now), source_text="night-window-ended",
    )
    state["owner_presence"] = presence
    _sync_legacy(state, presence)
    state["next_check"] = float(now)
    return True


def context(state, now=None):
    """Read owner presence from the projection when supplied; legacy reads remain diagnostic-only."""
    if isinstance(state, dict) and "owner" in state and "contact" in state:
        presence = state.get("owner") or {}
        now = float(state.get("as_of") if now is None else now)
    else:
        presence = state.get("owner_presence") or {}
        now = float(time.time() if now is None else now)
    mode = presence.get("mode", "open")
    expires_at = presence.get("expires_at")
    remaining = max(0, int(expires_at - now)) if isinstance(expires_at, (int, float)) else None
    return {
        "mode": mode,
        "since": presence.get("since"),
        "remaining_seconds": remaining,
        "rule": {
            "open": "用户可正常联系；没有回复不代表要求静默。",
            "busy": "用户暂时有事；到期自动恢复，明确回来时立即恢复。",
            "rest": "用户休息；允许低打扰个人分享，但不追问、不索取回应。",
            "night": "用户已明确晚安；遵循睡眠窗口，醒来或明确回来后恢复。",
            "quiet": "用户明确要求不要打扰；保持静默直到明确恢复。",
        }.get(mode, "按当前状态处理。"),
    }


class OwnerPresenceService:
    """Single public write boundary for owner contact availability."""

    def __init__(self, state):
        self._state = state

    def apply_message(self, text, now):
        return apply_user_message(self._state, text, now)

    def expire(self, now):
        return expire_temporary_pause(self._state, now)

    def release_night(self, now):
        return release_night_presence(self._state, now)

    def set_open(self, now, reason="explicit_resume"):
        presence = self._state.setdefault("owner_presence", {})
        before = presence.get("mode", "open")
        presence.update(mode="open", since=float(now), expires_at=None,
                        resumed_at=float(now), source_text=str(reason)[:200])
        _sync_legacy(self._state, presence)
        return before != "open"

    def set_quiet(self, now, reason="explicit_quiet"):
        presence = self._state.setdefault("owner_presence", {})
        presence.update(mode="quiet", since=float(now), expires_at=None,
                        source_text=str(reason)[:200])
        _sync_legacy(self._state, presence)
        return True
