"""Separate tiredness, a sleep intention, actual sleep and conversational waking."""

import json
import random
import re
import time
from datetime import datetime

if __package__:
    from .jobs import night_record
    from .local_clock import TZ
else:
    from jobs import night_record
    from local_clock import TZ

GUIDANCE = (
    "\n【睡意与聊天选择】躺在床上、拍床上自拍、过零点或说一句晚安，不自动等于已经睡着。"
    "他挽留、叫你醒醒、问还在不在或叫你时，先按当前已被叫醒接住，可以有困意；"
    "由你决定再陪一会儿或清楚说实在想睡，不固定只回一句，不急着自动补晚安。"
    "已经答应再聊就顺着聊，不一边说陪他一边马上晚安。"
    '只有这轮真的改变睡眠选择时，在末尾另加私有标记，例如[[MENGYU_SLEEP:{"phase":"drowsy"}]]，phase只从awake、drowsy、preparing、sleeping中选一个。'
    "awake=醒着，drowsy=困但仍醒着，preparing=打算睡但还在，sleeping=这轮确实结束聊天去睡。"
    "标记只是内部选择，不向他解释；不把标记写在正文里，不因以前说过晚安就强制sleeping。"
)

GUIDANCE += (
    "\n【睡前要说一声】真要睡或小憩时，先在回复里明确告诉他你去睡/眯一会儿、大概多久，不要静默消失；到作息就寝时间也先说一句晚安再睡。他只是随口关心或问你在干嘛，不等于让你去睡。"
)

def user_paused(text):
    """Recognize the owner's own break, excluding questions about her sleep.

    Args:
        text: Current user message.

    Returns:
        Whether the owner explicitly closes or pauses the conversation.
    """
    text = str(text or "")
    if re.search(
        r"(?:你|怎么|又).{0,8}(?:睡|晚安)|别.{0,3}晚安|不要.{0,3}晚安", text
    ) and not re.search(
        r"我(?:先|要|去)?睡(?:了|啦|觉)|我(?:要|去|先)睡|我先忙|我去上课|先不聊|不用回",
        text,
    ):
        return False
    pauses = list(
        re.finditer(
            r"先不聊|先别聊|别发|不要发|不许发|别打扰|不要打扰|不想聊|我(?:先|要|去)?睡(?:了|啦|觉)|我(?:要|去|先)睡(?!不着)|晚安|"
            r"(?:^|[，,。\n])(?:我)?(?:先)?(?:睡|眯|歇|休息)(?:一小|一|小)?会(?:儿)?|"
            r"(?:^|[，。\n])睡了(?:[，。！!\s]|$)|我先忙|先去忙|我有点忙|我现在有点忙|待会再聊|不想说话|不用回|"
            r"去吃饭|吃饭去了|先吃饭|我要吃饭|我去.{0,4}(吃饭|上课|洗澡|开会|考试)|去上课|上课去了|去考试|去洗澡|洗澡去了|去开会",
            text,
        )
    )
    resumes = list(
        re.finditer(
            r"不睡了|先不睡|继续聊|接着聊|我(?:睡醒了|醒了|回来了|忙完了)|(?:^|[，,。\n])(?:醒了|睡醒了|回来了|忙完了)(?!吗|没)",
            text,
        )
    )
    return bool(pauses and (not resumes or pauses[-1].start() > resumes[-1].start()))


def owner_pause_kind(text):
    """Separate a rest leave-message window from a request for silence.

    Args:
        text: Owner's complete break notice.

    Returns:
        night for bedtime, rest for a short rest, or quiet for explicit silence.
    """
    if re.search(
        r"别发|不要发|不许发|别打扰|不要打扰|不用回|先别聊|先不聊|不想聊", text
    ):
        return "quiet"
    if re.search(
        r"晚安|我(?:先|要|去)?睡(?:了|啦|觉)|(?:^|[，,。\n])睡了", text
    ) and not re.search(r"睡.{0,3}会|眯|午睡|午觉|小睡", text):
        return "night"
    return "rest" if re.search(r"睡|眯|歇|休息|晕车", text) else "quiet"


def release_night_pause(state, now):
    """Release a bedtime pause when its recorded sleep window has elapsed.

    Args:
        state: Mutable persisted companion state.
        now: Current timestamp.

    Returns:
        Whether an elapsed bedtime pause was released. Explicit silence stays.
    """
    if not state.get("conversation_paused"):
        return False
    until = (state.get("sleep") or {}).get("until", 0)
    if not until or now < until or until <= state.get("last_user_ts", 0):
        return False
    kind = state.get("owner_pause_kind")
    if kind != "night":
        # Recover legacy goodnights only with an exact, fully handled input.
        answered = state.get("answered_tail") or {}
        if not (
            kind == "quiet"
            and answered.get("delivery") == "complete"
            and answered.get("user_ts") == state.get("last_user_ts")
            and owner_pause_kind(str(answered.get("user") or "")) == "night"
        ):
            return False
    # SleepService owns the sleep transition only. OwnerPresenceService owns
    # the contact pause and its compatibility mirrors.
    state["night_pause_released_at"] = now
    wake(state, now)
    return True


def wake_requested(text):
    """Recognize natural waking or asking her to stay, rather than exact commands.

    Args:
        text: Current user message.

    Returns:
        Whether the message addresses her to wake or keep chatting.
    """
    text = str(text or "")
    if re.search(r"不吵你|别吵|不要吵|不用陪|别陪|你好好睡|快睡|去睡吧|让你睡", text):
        return False
    return bool(
        re.search(
            r"醒醒|醒一醒|起来|别睡|别.{0,3}晚安|不要.{0,3}晚安|怎么又晚安|陪我|陪陪我|再等|等等嘛|就一会|再聊|再唠|还醒着|"
            r"你.{0,5}睡|真睡了|睡了[吗么没]|还在吗|在不在|急事|出事了|(?:^|[\s，。])(?:歪|喂)(?:[\s，。？！!?]|$)",
            text,
        )
    )


def extract_choice(text):
    """Remove private sleep metadata and accept a bounded known phase.

    Args:
        text: Raw assistant reply.

    Returns:
        Clean public text and optional phase decision.
    """
    phase = None
    for match in re.finditer(r"\[\[MENGYU_SLEEP:(.*?)\]\]", text, re.DOTALL):
        try:
            value = json.loads(match[1])
            candidate = value.get("phase") if isinstance(value, dict) else None
            if candidate in ("awake", "drowsy", "preparing", "sleeping"):
                phase = candidate
        except (ValueError, TypeError):
            pass
    return re.sub(
        r"\[\[MENGYU_SLEEP:.*?(?:\]\]|$)", "", text, flags=re.DOTALL
    ).strip(), phase


def wake(state, now):
    """Wake the existing episode without inventing a new sleep start.

    Args:
        state: Mutable persisted companion state.
        now: Wake time.
    """
    sleep = state.setdefault("sleep", {})
    sleep.update(active=False, phase="awake", woken_at=now)
    job = sleep.get("night_wake") or {}
    if job.get("status") in ("pending", "ready", "generating"):
        job["status"] = "skipped"
        sleep["night_wake_done"] = True


def owner_bedtime(user_text):
    """Whether the owner's own words explicitly close the night.

    The owner says "我要睡了/晚安" when he is actually going to bed, so this
    decision must not wait for the model to declare sleep in her reply.
    """
    text = str(user_text or "")
    if not text or wake_requested(text):
        return False
    return user_paused(text) and owner_pause_kind(text) == "night"


def commit_choice(state, text, user_text, now, phase=None):
    """Apply a sleep choice only after the public reply is actually delivered.

    Args:
        state: Mutable persisted companion state.
        text: Confirmed delivered public response.
        user_text: Source user message.
        now: Delivery time.
        phase: Optional validated model choice.
    """
    old = state.get("sleep") or {}
    forced = owner_bedtime(user_text)
    chatting = bool(
        re.search(r"再.{0,6}(?:聊|唠)|陪.{0,5}(?:聊|唠)|先不睡|还不睡", text)
    ) and not re.search(r"不想.{0,5}(?:聊|唠)|不陪.{0,5}(?:聊|唠)", text)
    if forced:
        # He said goodnight: sleep now, even if she offered to stay up.
        phase = "sleeping"
    elif chatting:
        phase = "drowsy" if "困" in text else "awake"
    if phase is None:
        if re.search(
            r"我(?:先|去|要)?睡了|我去睡|先睡了|睡觉了|睡啦", text
        ) and not re.search(r"不睡|不想睡|没睡|还没|等.{0,12}再睡|图.{0,8}睡", text):
            phase = "sleeping"
        elif "晚安" in text:
            phase = (
                "sleeping"
                if user_paused(user_text) and not wake_requested(user_text)
                else "preparing"
            )
        else:
            return
    if phase == "sleeping":
        hour = datetime.fromtimestamp(now, TZ).hour
        nap = re.search(r"小憩|眯一会儿|眯一下|睡一会儿|躺一会儿|补个觉|打个盹", text)
        declared = re.search(
            r"我(?:先|去|要|该|得)?睡了|我睡了|去睡了|睡觉去|准备睡|要睡了|晚安", text
        )
        # She must tell him before sleeping; an explicit owner goodnight is
        # enough on its own, and marker-only or silent sleep is still ignored.
        if not (nap or declared or forced):
            return
        if nap and not declared and not forced:
            minutes = random.randint(40, 90)
            state["sleep"] = {
                "active": True,
                "phase": "sleeping",
                "since": now,
                "until": now + minutes * 60,
                "nap": True,
                "secret_used": False,
                "night_wake_done": True,
            }
            state["followup_due_ts"] = 0
            state["absence_due_ts"] = 0
            return
        if not forced and 7 <= hour < 21:
            # Night sleep only in the evening; a daytime "sleep" needs 小憩.
            return
        if old.get("active") and now < old.get("until", 0):
            old["phase"] = "sleeping"
            return
        state["sleep"] = {
            "active": True,
            "phase": "sleeping",
            "since": now,
            "until": now + 7.5 * 3600,
            "secret_used": False,
        }
        state["followup_due_ts"] = 0
        state["absence_due_ts"] = 0
    elif phase in ("awake", "drowsy"):
        wake(state, now)
        state["sleep"]["phase"] = phase
    elif not old.get("active"):
        state.setdefault("sleep", {}).update(active=False, phase="preparing")


class SleepService:
    """Single public write boundary for sleep state and night-wake metadata."""

    def __init__(self, state):
        self._state = state

    def commit_choice(self, text, user_text, now, phase=None):
        return commit_choice(self._state, text, user_text, now, phase)

    def wake(self, now):
        return wake(self._state, now)

    def release_night_pause(self, now):
        return release_night_pause(self._state, now)

    def patch_night_wake(self, patch, now=None):
        now = time.time() if now is None else float(now)
        sleep = self._state.setdefault("sleep", {})
        job = sleep.setdefault("night_wake", {})
        if not isinstance(patch, dict):
            return False
        allowed = {"status", "finished_at", "id", "at", "text", "reason", "message"}
        for key, value in patch.items():
            if key in allowed:
                job[key] = value
        if patch.get("status") == "sent":
            sleep["night_wake_done"] = True
            sleep["night_wake_uncertain"] = False
        elif patch.get("status") == "uncertain":
            sleep["night_wake_uncertain"] = True
        elif patch.get("status") == "skipped":
            sleep["night_wake_done"] = True
        return True

    def update_night_wake(self, job_id, status, now, **fields):
        sleep = self._state.get("sleep") or {}
        job = sleep.get("night_wake") or {}
        if not job_id or job.get("id") != job_id:
            return False
        terminal = ("sent", "uncertain", "skipped", "cancelled")
        if job.get("status") in terminal:
            return False
        return self.patch_night_wake({**fields, "status": status}, now)

    def ensure_night_wake(self, now=None):
        now = time.time() if now is None else float(now)
        sleep = self._state.setdefault("sleep", {})
        job = sleep.get("night_wake")
        if not isinstance(job, dict) or not job.get("id"):
            job = night_record(sleep)
            sleep["night_wake"] = job
        return dict(job)

    def get_night_wake(self):
        sleep = self._state.get("sleep") or {}
        job = sleep.get("night_wake") or {}
        return dict(job) if isinstance(job, dict) else {}

    def mark_night_wake_done(self, done=True, uncertain=False):
        sleep = self._state.setdefault("sleep", {})
        sleep["night_wake_done"] = bool(done)
        sleep["night_wake_uncertain"] = bool(uncertain)

    def set_inactive_after_morning(self, now):
        sleep = self._state.setdefault("sleep", {})
        if sleep.get("active"):
            wake(self._state, float(now))
        else:
            sleep["phase"] = "awake"
        return sleep
