"""Helpers that keep sticker choice and opening words from repeating."""

import json
import re

ECHO_OPENINGS = ("原来", "不过", "刚才", "刚看", "哈哈", "确实")


def meme_category(path):
    """Return the pack category encoded in a sticker path.

    Args:
        path: Stored image path, possibly empty.

    Returns:
        Category folder name, or an empty string.
    """
    match = re.search(r"memes[\\/]([^\\/]+)[\\/]", str(path))
    return match.group(1) if match else ""


def dominant_categories(used, window=4, limit=2):
    """Pick at most two categories that were used repeatedly just now.

    Args:
        used: Categories of recently sent stickers, oldest first.
        window: How many recent sends to judge.
        limit: Maximum number of categories to rest.

    Returns:
        Categories to leave out this turn, most overused first.
    """
    recent = [str(item) for item in list(used)[-window:] if str(item)]
    counts = {}
    for item in recent:
        counts[item] = counts.get(item, 0) + 1
    ranked = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    return [name for name, count in ranked if count >= 2][:limit]


def repeated_opening(replies):
    """Whether recent replies keep opening with the same habitual words.

    Args:
        replies: Recent reply texts, oldest first.

    Returns:
        True when the last replies share one stock opening.
    """
    openings = [
        str(text).strip()[:2] for text in list(replies)[-3:] if str(text).strip()
    ]
    if len(openings) >= 2 and len(set(openings)) == 1:
        return True
    return sum(1 for item in openings if item in ECHO_OPENINGS) >= 2


NEGATIVE = re.compile(r"(不|别|没|不想|才不|懒得|先不).{0,6}(给|发你|让你|分享)")
PICTURE = re.compile(r"照片|自拍|相片|拍一张|拍张|拍照|图片")
FUTURE = re.compile(
    r"(等|下次|回头|待会|一会|晚点|明早|明天|拍好|拍完|弄好|做好).{0,12}(拍|照片|自拍|图)"
    r"|(拍|照).{0,10}(给你|发你|发给你)"
)
NOT_PHOTO = re.compile(r"日记|代码|文档|剧本|结局|设定|攻略")


def photo_promise(text):
    """Whether a reply promises to send a new photo later.

    Args:
        text: Her reply text.

    Returns:
        True only for an affirmative promise about a picture.
    """
    sentence = str(text or "")
    if NEGATIVE.search(sentence):
        return False
    if NOT_PHOTO.search(sentence) and not re.search(r"照片|自拍|相片", sentence):
        return False
    if not PICTURE.search(sentence):
        return False
    return bool(FUTURE.search(sentence))


def extract_json(text):
    """Return the first JSON object in a model reply, or None.

    Args:
        text: Raw model output, possibly wrapped in prose or code fences.

    Returns:
        Parsed dict, or None when no valid object is present.
    """
    body = str(text or "").strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    start, end = body.find("{"), body.rfind("}")
    candidate = body[start : end + 1] if 0 <= start < end else body
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


ACK = re.compile(
    r"^[\s，,。.!！~～]*(?:好|好的|行|嗯|嗯嗯|哦|ok|okay|哈哈|去吧|去吧去吧|那行|可以|收到)[\s，,。.!！~～]*$"
)


def is_ack(text):
    """Whether the message is only a closing acknowledgement.

    Args:
        text: User message text.

    Returns:
        True for short confirmations with no new information.
    """
    return bool(ACK.fullmatch(str(text or "").strip()))


def _normalize(text):
    return re.sub(r"[^\w\u4e00-\u9fff]", "", str(text))


def drop_repeated_clauses(text, recent_texts):
    """Remove only identical complete sentences from unsolicited candidates.

    Args:
        text: Candidate outgoing message.
        recent_texts: Recent outgoing messages, newest first.

    Returns:
        Tuple of cleaned text and whether any content remains.
    """
    recent = {
        _normalize(clause)
        for item in recent_texts
        for clause in re.split(r"(?<=[。！？!?\n])", str(item or ""))
        if clause.strip()
    }
    clauses = [
        item for item in re.split(r"(?<=[。！？!?\n])", str(text)) if item.strip()
    ]
    kept = []
    for clause in clauses:
        normalized = _normalize(clause)
        if len(normalized) >= 4 and normalized in recent:
            continue
        kept.append(clause.strip())
    return "".join(kept).strip(), bool(kept)
