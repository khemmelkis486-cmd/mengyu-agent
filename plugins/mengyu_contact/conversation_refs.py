"""Keep source speakers and original burst boundaries visible to the model."""

GUIDANCE = """【接话对象与主语】
一轮连续消息可以先接你刚分享的话，再讲他自己的另一件事，不要求整轮只有一个话题或同一个主语。评价你提到的活动、说它难或好玩，不等于他本人正在做、做过或失败过；明确说自己也在做时，再按他自己的经历接。先根据说话人和前后文理解每条在接谁，不按相邻两个名词就把事情串在一起。
你说过的计划、感受与经历属于你；他明确讲的自己经历才属于他。主语省略时回看接的那句话，不默认所有活动都归对方。不为了俏皮、对仗或把所有话都用上，编成他两件事都遇到了；可以自然接两件，也可以只接最想回的一点，不必硬找共同点。这个区分只用于理解，正文不逐条分析、不复述主语、不解释你是怎么接话的。
"""


def reference_context(event, recent, user_text="", *, topic=None):
    """Expose raw speaker anchors; never infer a user's experiences from topics."""
    getter = getattr(event, "get_extra", None)
    parts = getter("mengyu_burst_parts") if callable(getter) else None
    if not isinstance(parts, list) or not all(isinstance(part, str) for part in parts):
        parts = [user_text or str(getattr(event, "message_str", "") or "")]
    # Normal replies already have roles in framework history. Do not re-inject
    # an ordinary draft or a finished topic as if it were a fresh sharing anchor.
    shares = [row for row in recent if row.get("role") == "assistant_proactive"]
    context = {
        "梦雨最近主动说起的话，属于梦雨": shares[-1].get("text", "")
        if shares
        else None,
        "对方本轮的原始分条，可以分别接不同话题": [part for part in parts if part],
    }
    if topic and topic.get("phase") in ("active", "waiting"):
        context["如果还在同一话题，这些已经说过"] = (topic.get("covered_points") or [])[-2:]
    return context
