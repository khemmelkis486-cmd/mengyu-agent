"""Lightweight variety guidance for spontaneous sharing.

The helper records expression *shapes*, not topics or emotional states.  It
uses recent delivered proactive messages to gently steer away from repeated
structures without imposing a fixed round-robin script.
"""

import random
import re

SHARE_KINDS = {
    "spontaneous_share",
    "self_share",
    "rest_share",
    "casual_contact",
    "life_share",
}

SHAPES = {
    "daily_report": "动作、细节、心情结论连在一起的生活日报",
    "fragment": "半句话、短感叹或一个突然冒出的念头",
    "complaint": "一句具体吐槽或小抱怨",
    "question": "一个有来由、真想知道答案的小问题",
    "feeling": "只说此刻的一种感受或偏好",
    "observation": "只分享一个刚注意到的细节或发现",
    "affection": "直接亲近、调情、打趣或撒娇",
    "anecdote": "一件小事的自然片段，不附加总结",
}

PREFERRED_SHAPES = (
    "fragment",
    "complaint",
    "question",
    "feeling",
    "observation",
    "anecdote",
)

_ACTION = re.compile(
    r"(收拾|整理|归拢|煮|做饭|洗|晾|买|下楼|出门|回家|写|画|读|看完|擦|摆|换了|修了|整理了|收好了|弄了)"
)
_DETAIL = re.compile(
    r"(床头|桌面|桌上|桌子|柜子|窗台|充电线|锅里|碗里|衣服|书页|杯子|顺手|还把|也把|里面|旁边|角落|那一小块|那一处|钥匙|耳机|纸巾|盘子|调料|葱|鸡蛋|青菜|面条|被子|枕头|手机)"
)
_FEELING = re.compile(
    r"(心情|舒服|清爽|轻松|满足|开心|踏实|自在|心里|觉得|感觉|有点烦|有点累|期待|失落|安心)"
)
_COMPLAINT = re.compile(r"(离谱|无语|服了|烦死|气笑|真受不了|怎么又|怎么还|凭什么|懒得跟|有点恼火|小崩溃)")
_AFFECTION = re.compile(r"(想你|想亲|亲亲|抱抱|抱一下|撩你|逗你|撒娇|喜欢你|黏你|想靠近)")
_OBSERVATION = re.compile(r"(看到|看见|发现|听到|听见|闻到|窗外|路边|刚刚|刚才|那只|那片|这个颜色|这个声音)")


def _share_rows(history, limit=10):
    rows = []
    for row in history or []:
        if not isinstance(row, dict):
            continue
        kind = row.get("contact_kind")
        if kind is None or kind in SHARE_KINDS:
            text = row.get("text")
            if isinstance(text, str) and text.strip():
                rows.append(row)
    return rows[-limit:]


def normalize_expression_shape(value):
    """Return a supported shape label, or an empty string."""
    value = str(value or "").strip().lower()
    return value if value in SHAPES else ""


def classify_expression_shape(text, hint=None):
    """Classify a delivered message; never let a hint hide a daily-report pattern."""
    value = re.sub(r"\s+", "", str(text or ""))
    if not value:
        return "fragment"

    has_action = bool(_ACTION.search(value))
    has_detail = bool(_DETAIL.search(value))
    has_feeling = bool(_FEELING.search(value))
    if has_action and has_detail and has_feeling and len(value) >= 24:
        return "daily_report"

    hinted = normalize_expression_shape(hint)
    if hinted:
        return hinted
    if re.search(r"[？?]", value):
        return "question"
    if _COMPLAINT.search(value):
        return "complaint"
    if _AFFECTION.search(value):
        return "affection"
    if has_feeling and not has_action:
        return "feeling"
    if _OBSERVATION.search(value):
        return "observation"
    if len(value) <= 14:
        return "fragment"
    return "anecdote"


def shape_history_context(history, limit=10):
    """Return compact recent shape evidence for the proactive decision prompt."""
    result = []
    for row in _share_rows(history, limit=limit):
        text = str(row.get("text") or "").strip()
        shape = classify_expression_shape(text, row.get("expression_shape"))
        result.append({"shape": shape, "text": text[:100]})
    return result


def choose_expression_shape(history, rng=None, allowed_shapes=None):
    """Suggest a soft alternative only when recent delivered structures repeat.

    This is intentionally not a fixed rotation.  With varied recent messages it
    returns None and lets the model choose freely.  When daily-report structure
    or another shape is overrepresented, it chooses randomly among the least
    used alternatives.
    """
    rows = shape_history_context(history, limit=10)
    if len(rows) < 2:
        return None

    shapes = [row["shape"] for row in rows]
    counts = {shape: shapes.count(shape) for shape in SHAPES}
    last_three = shapes[-3:]
    daily_reports = counts["daily_report"]
    repeated_last_shape = len(last_three) >= 2 and last_three[-1] == last_three[-2]
    dominant_shape = max(counts, key=counts.get)
    dominant_count = counts[dominant_shape]

    daily_report_overused = daily_reports >= max(2, len(rows) // 3)
    another_shape_overused = dominant_shape != "daily_report" and dominant_count >= 3
    if not (daily_report_overused or another_shape_overused or repeated_last_shape):
        return None

    allowed = set(allowed_shapes or PREFERRED_SHAPES)
    candidates = [
        shape for shape in PREFERRED_SHAPES
        if shape in allowed and shape != shapes[-1]
    ]
    if not candidates:
        candidates = [shape for shape in PREFERRED_SHAPES if shape in allowed]
    if not candidates:
        return None
    minimum = min(counts[shape] for shape in candidates)
    candidates = [shape for shape in candidates if counts[shape] == minimum]
    chooser = rng or random
    return chooser.choice(candidates)


def shape_guidance(shape=None):
    """Return concise, non-binding style guidance for this share."""
    common = (
        "reason、detail、feeling只是可选背景，不是写作清单；可以只取其中一项，自然需要时也可组合，但不为完整而凑齐。活动记录和当前心情不是必须分享的素材。"
        "不要求同时出现‘做了什么+具体细节+心情结论’，也不要求每次交代事情经过。"
        "避免习惯性用‘心情也跟着好了/舒服了/轻松了’总结收尾；没有真实情绪变化就不补结论。"
        "表达形状只是轻微倾向，若不贴合此刻内容就自然选择别的，不为轮换而生造内容。"
    )
    specific = {
        "fragment": "这次优先试试半句话、短感叹或一个突然想到的念头；说到点上就停，不必补活动经过，也不必补心情总结。",
        "complaint": "这次可以只吐槽一个具体的小状况或自己的小失误；不追加‘不过最后还是很开心’之类的收尾。",
        "question": "这次可以只问一个你确实好奇、对方容易接的话；问题本身就是内容，不先写一段活动经过。",
        "feeling": "这次可以只说此刻真实的一种感受、喜恶或偏好；不必交代它由哪件日常小事引起。",
        "observation": "这次只抓一个刚注意到的画面、声音、气味或物件细节；不扩写成完整活动记录，也不补心情结论。",
        "affection": "语气按此刻的自主心情和实际互动来，不生造亲近的意思。",
        "anecdote": "这次可以讲一个小插曲，但只保留最有意思的部分；自然停在细节或反应上，不强行总结感受。",
        "daily_report": "近期这种结构已经偏多，本轮尽量不要再写成‘做了一件事—补充细节—总结心情’。",
    }
    selected = specific.get(normalize_expression_shape(shape), "")
    return common + selected
