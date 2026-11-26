"""Conservative Chinese commitment extraction and fulfilment parsing."""

import re
from datetime import datetime, timedelta
import os
from zoneinfo import ZoneInfo


TZ = ZoneInfo(os.environ.get("MENGYU_TIMEZONE") or "UTC")
_CN_NUMBERS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "零": 0}


def _number(value):
    value = str(value or "")
    if value.isdigit():
        return int(value)
    if value == "十":
        return 10
    if value.startswith("十"):
        return 10 + _CN_NUMBERS.get(value[1:2], 0)
    if value.endswith("十"):
        return _CN_NUMBERS.get(value[:1], 1) * 10
    if "十" in value:
        left, right = value.split("十", 1)
        return _CN_NUMBERS.get(left[:1], 1) * 10 + _CN_NUMBERS.get(right[:1], 0)
    return _CN_NUMBERS.get(value[:1])


def extract_due_at(text, now=None):
    """Extract explicit Chinese relative dates/times; return Unix seconds or None."""
    value = str(text or "")
    now = datetime.now(TZ) if now is None else datetime.fromtimestamp(float(now), TZ)
    target_date = None
    if "后天" in value:
        target_date = (now + timedelta(days=2)).date()
    elif "明天" in value:
        target_date = (now + timedelta(days=1)).date()
    elif "今天" in value or "今晚" in value:
        target_date = now.date()
    else:
        date_match = re.search(r"(\d{1,2})月(\d{1,2})[日号]", value)
        if date_match:
            year = now.year
            month, day = int(date_match.group(1)), int(date_match.group(2))
            try:
                candidate = now.replace(year=year, month=month, day=day).date()
                if candidate < now.date():
                    candidate = now.replace(year=year + 1, month=month, day=day).date()
                target_date = candidate
            except ValueError:
                return None
        else:
            weekday_match = re.search(r"(?:下周|下星期)?周([一二三四五六日天])", value)
            if weekday_match:
                weekday = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}[weekday_match.group(1)]
                days = (weekday - now.weekday()) % 7
                if days == 0 and now.hour >= 18:
                    days = 7
                if "下周" in value or "下星期" in value:
                    days += 7 if days < 7 else 0
                target_date = (now + timedelta(days=days)).date()
            elif "周末" in value:
                days = (5 - now.weekday()) % 7
                target_date = (now + timedelta(days=days)).date()

    time_match = re.search(r"(上午|早上|中午|下午|晚上|今晚)?\s*(\d{1,2}|[一二两三四五六七八九十]{1,3})\s*(?:点|时)(?:半|零?三十|三刻)?", value)
    hour, minute = 23, 59
    if time_match:
        hour = _number(time_match.group(2))
        marker = time_match.group(1) or ""
        if marker in ("下午", "晚上", "今晚") and 1 <= hour <= 11:
            hour += 12
        elif marker == "中午" and hour < 11:
            hour += 12
        elif marker in ("上午", "早上") and hour == 12:
            hour = 0
        minute = 30 if "半" in time_match.group(0) or "三十" in time_match.group(0) else (45 if "三刻" in time_match.group(0) else 0)
        if target_date is None:
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate <= now:
                candidate += timedelta(days=1)
            target_date = candidate.date()
    elif target_date is None:
        return None

    try:
        due = datetime(target_date.year, target_date.month, target_date.day, hour, minute, tzinfo=TZ)
    except ValueError:
        return None
    return due.timestamp()


GIFT_ITEMS = (
    "耳机", "耳钉", "银圈", "戒指", "项链", "手链", "手镯", "玩偶", "衣服", "外套",
    "裙子", "鞋", "包", "围巾", "手机壳", "书", "蛋糕", "花", "杯子", "饰品",
    "口红", "香水", "发夹", "帽子", "手表", "键盘", "鼠标", "相机", "抱枕", "礼物",
)

NEGATION = re.compile(r"没答应|不答应|没有答应|不买|不送|别记|不用记|不算数|只是开玩笑|随口说说|没打算")
QUESTION = re.compile(r"[吗么？?]$")
AMBIGUOUS = re.compile(r"有空|改天|以后有机会|看情况|到时候再说|如果有机会|也许|可能|我想|我打算")
PAST_OR_PROGRESS = re.compile(
    r"(?:已经|刚刚|刚才|刚|早就)?(?:买好了|买了|下单了|付款了|付过款了|寄出|发货|送出去了|寄给你了|送过去了|"
    r"送给你了|给你了|交给你了|送到了|寄到了|收到了|到货了|到了|发给你了|发过去了|陪你看完了|一起做完了|帮你做完了|给你做好了)"
)


def _item(text):
    hits = [word for word in GIFT_ITEMS if word in text]
    return max(hits, key=len) if hits else ""


def _kind(text):
    if _item(text) or re.search(r"买|送|礼物|礼品", text):
        return "gift"
    if re.search(r"陪你|一起看|一起玩|带你去|和你去|跟你去|一起去", text):
        return "shared_experience"
    if re.search(r"发给你|给你发|拍给你|告诉你|回复你|帮你", text):
        return "delivery_or_help"
    return "general"


def _title(text, kind):
    item = _item(text)
    if item:
        return f"关于{item}的约定"
    if kind == "shared_experience":
        match = re.search(r"(?:陪你|一起|带你去|和你去|跟你去)(.{2,18})", text)
        return ("一起" + match.group(1)).strip("，。！？ ")[:80] if match else "共同体验约定"
    match = re.search(r"(?:给你|帮你|替你)(.{2,18})", text)
    return (match.group(1).strip("，。！？ ") if match else text[:50]) or "一项约定"


def parse_commitment(text, speaker="user"):
    """Return explicit/ambiguous promise data, or None. speaker=user|mengyu."""
    value = str(text or "").strip()
    if not value or len(value) > 600 or NEGATION.search(value) or QUESTION.search(value):
        return None
    # A statement about something already purchased/sent/received is progress,
    # not a new promise. The progress handler owns it.
    if re.search(r"没买|还没买|没有买|没收到|还没收到|没有收到|没到|还没到|没送到|还没送到|没寄出|未发货|没发货", value):
        return None
    if PAST_OR_PROGRESS.search(value):
        return None

    if speaker == "user":
        explicit_pattern = re.compile(
            r"(?:我(?:一定|肯定|会|要|下次|明天|后天|周末|这周|下周|之后|等.{0,8}就)?\s*"
            r"(?:给你买|买给你|送你|给你送|寄给你|给你寄|陪你|带你去|和你一起|跟你一起|帮你|替你|给你发|发给你|给你拍|拍给你|给你做|给你写|给你准备))"
            r"|(?:下次|明天|后天|周末|这周|下周|之后)我(?:会|一定|肯定)?\s*(?:给你买|买给你|送你|陪你|带你去|和你一起|跟你一起|帮你|给你发|发给你|给你拍|拍给你)"
            r"|我答应(?:过)?(?:你)?(?:会|一定|肯定)?(?:给你买|买给你|送你|陪你|带你去|和你一起|跟你一起|帮你|替你|给你发|发给你|给你拍|拍给你|给你做|给你写|给你准备)",
        )
        ambiguous_pattern = re.compile(
            r"(?:有空|改天|以后有机会|看情况|到时候|如果有机会|可能|也许|我想|我打算).{0,32}"
            r"(?:给你买|买给你|送你|陪你|带你去|一起|帮你|给你发|拍给你)"
        )
        explicit = bool(explicit_pattern.search(value))
        ambiguous = bool(ambiguous_pattern.search(value))
        if ambiguous and not re.search(r"答应|一定|肯定|我会|我下次|我明天|我周末", value):
            explicit = False
        if not explicit and not ambiguous:
            return None
        owner, beneficiary = "user", "mengyu"
    else:
        explicit = bool(re.search(
            r"我(?:答应|答应过|一定|会|明天|后天|周末|下次|等.{0,8}就)?\s*(?:给你买|买给你|送你|给你送|寄给你|给你寄|陪你|带你去|和你一起|跟你一起|帮你|替你|给你发|发给你)",
            value,
        ))
        if not explicit:
            return None
        owner, beneficiary = "mengyu", "user"

    kind = _kind(value)
    confidence = 0.96 if explicit and not AMBIGUOUS.search(value) else 0.76
    return {
        "classification": "explicit" if confidence >= 0.9 else "ambiguous",
        "title": _title(value, kind),
        "kind": kind,
        "owner": owner,
        "beneficiary": beneficiary,
        "confidence": confidence,
        "text": value[:500],
        "reason": "明确表达了未来履行意图" if confidence >= 0.9 else "包含未来意向，但时间或承诺力度不明确",
    }


def confirmation_intent(text):
    """Only short, explicit answers should resolve a just-asked confirmation."""
    value = re.sub(r"[\s，。！？、,.!?]+", "", str(text or "")).strip()
    if not value or len(value) > 24:
        return None
    if re.search(r"随口说说|开玩笑|不算数|别记|不用记|没答应|不是认真的|不是真的|不算", value):
        return False
    if re.fullmatch(r"(?:当然|是|对|嗯|嗯嗯|好|可以|算数|认真的|真的|没开玩笑|记下来|我答应了|肯定|当然算数)", value):
        return True
    if re.search(r"(?:是认真的|当然认真|当然算数|说好了|我答应|没开玩笑|是真的)", value):
        return True
    return None


def commitment_progress(text, title=""):
    """Infer explicit fulfilment progress, never completion from elapsed time."""
    value = str(text or "").strip()
    if not value or QUESTION.search(value):
        return None
    if re.search(r"(?:取消|不用做了|不用买了|不需要了)", value):
        return "cancelled"
    if NEGATION.search(value):
        return None
    if re.search(r"没收到|还没收到|没有收到|没到|还没到|没买|还没买|没有买|没发|还没发", value):
        return None
    if re.search(r"(?:已经|刚刚|刚才)?(?:送给你了|给你了|交给你了|发给你了|发过去了|陪你看完了|一起做完了|帮你做完了|给你做好了|已经完成了|做完了)", value):
        return "fulfilled"
    if re.search(r"(?:已经|刚刚|刚才)?(?:送到了|寄到了|收到了|到货了|到了)", value):
        return "received"
    if re.search(r"(?:已经|刚刚|刚才)?(?:寄出|发货|送出去了|寄给你了|送过去了)", value):
        return "sent"
    if re.search(r"(?:已经|刚刚|刚才)?(?:买好了|买了|下单了|付款了|付过款了)", value):
        return "purchased"
    if re.search(r"(?:开始做|开始准备|已经开始|正在处理|已经在做)", value):
        return "in_progress"
    if re.search(r"(?:改天|延期|推迟|下次再|暂时不)", value):
        return "deferred"
    return None
