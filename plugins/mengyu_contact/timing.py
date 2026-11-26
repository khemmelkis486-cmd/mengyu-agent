"""Expose wall-clock intervals so dialogue turns cannot fast-forward daily life."""

import time
from datetime import datetime

if __package__:
    from .local_clock import TZ
else:
    from local_clock import TZ

from .life import note_used

HUMAN_TIME_RULE = "\n日常推进看真实经过的时间，不看聊了几轮。自己刚说要出门、回去拿衣服、吃饭、洗澡、写日记或玩一局后，只过几十秒或一两分钟，就仍是准备或进行中，不为了接话补成已经往返、吃完、洗完、写完或打完。参考最近自己说话的elapsed_seconds、当前行程阶段和seconds_until_complete；还没到下一阶段，就不能跳到下一阶段。手边拿件外套可以很快，人在外面回去拿还要算往返路程；活动之间的移动和收尾也占时间。若临时不做了或改主意，可以如实转向，不把取消说成完成。工具文件生成成功与角色生活过程结束是两回事。普通回复自然说话，不报秒表或解释内部时间字段。"


def conversation_timing(turns, now=None, since=0, shared_notes=()):
    """Describe elapsed time since actual public statements, without inventing actions.

    Args:
        turns: Actual timestamped conversation records.
        now: Current Unix time, or the real clock when omitted.
        since: Optional lower bound for the active conversation context.
        shared_notes: Reflections already shared, excluded to avoid reinjection.

    Returns:
        Current local time and elapsed seconds since recent assistant statements.
    """
    now = time.time() if now is None else now
    recent = []
    for item in turns:
        if any(
            note.get("shared_at") and note_used(note, item.get("text", ""))
            for note in shared_notes
        ):
            continue
        at = item.get("at", 0)
        if (
            str(item.get("role", "")).startswith("assistant")
            and isinstance(at, (int, float))
            and max(since, now - 1800) <= at <= now
        ):
            recent.append(
                {
                    "text": str(item.get("text", ""))[:160],
                    "sent_at": at,
                    "elapsed_seconds": int(now - at),
                }
            )
    return {
        "now": datetime.fromtimestamp(now, TZ).isoformat(),
        "recent_own_actions": recent[-4:],
    }
