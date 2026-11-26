"""Recognize chat photo requests and distinguish living scenes from artwork."""

import re

_PERSON = r"(?:你(?:自己)?|宝宝|宝贝|老婆|媳妇|亲爱的|梦雨)"
_PARTICLE = r"吧嘛啊呀哦啦呗呢吗么哈～~"
_END = rf"(?=$|\s|[{_PARTICLE}\U0001F300-\U0001FAFF])"
_SELFIE_REQUEST = re.compile(
    rf"(?:看一眼|看一下|看看|看下|看一看|看)\s*(?:下|一眼)?\s*{_PERSON}"
    rf"(?:本人的?自拍|的自拍|本人|现在的样子|现在什么样|长什么样)?"
    rf"(?!\s+(?:的|上次|今天.{0,4}(?:好点|怎么样)|刚才.{0,4}拍)){_END}"
    rf"|(?:来|发(?:给我)?|给我|拍|传)(?:一)?张\s*(?:新(?:的)?)?(?:{_PERSON}的)?自拍(?:{_END}|(?=给我|让我看|看看))"
    rf"|自拍\s*(?:一(?:张|下)|张){_END}"
    rf"|给我\s*拍\s*(?:一)?张(?:\s*{_PERSON}(?:现在)?的照片)?[{_PARTICLE}\s\U0001F300-\U0001FAFF]*$"
)
_CANCEL_SELFIE = re.compile(
    r"(?:算了|略过|取消|不用了|不要了)(?=$|[吧啊啦\s])"
    r"|(?:别|不用|不要|不想|没想|不是要|不是让你|不是叫你)\s*(?:再|给我|继续|让你)?\s*"
    r"(?:拍|发|看|来|自拍)"
)
_QUOTED = re.compile(r"“[^”]*”|「[^」]*」|『[^』]*』|‘[^’]*’|\"[^\"\r\n]*\"")
_PHOTO_CLAIM = re.compile(
    r"(?:拍|自拍)(?:完[了啦咯]|好(?:了|啦|咯)?)(?=$|[呀啊哦呢吧咯宝啦\s。！？，])"
    r"|(?:自拍|照片|图片)(?:我)?(?:已经|给你|刚|都|也|这就){0,3}发(?:完|出|过去|给你|了|啦)"
    r"|(?:已经|刚|这就)(?:给你)?发(?:给你|了|啦|过去|出)?"
    r"(?:一|这|那)?(?:张)?(?:新(?:的)?)?(?:自拍|照片|图片)"
    r"|发(?:给你)?[了啦](?:一|这|那)?(?:张)?(?:新(?:的)?)?(?:自拍|照片|图片)"
    r"|发(?:一|这|那)?(?:张)?(?:新(?:的)?)?(?:自拍|照片|图片)(?:给你)?[了啦]"
    r"|给你看(?:看)?(?:这|我刚拍的那)张(?:自拍|照片|图片)"
    r"|这张(?:自拍|照片)?(?:是我)?刚拍的"
)
_NONCURRENT_PHOTO = re.compile(
    r"昨天|前天|上次|上回|那天|以前|之前|之前那张|(?:等|如果|要是|等到).{0,12}(?:拍好|拍完|发)"
    r"|(?:没有|还没|没能|没法|未|不是|没)(?:有|能|法|给你|已经|再)?\s*(?:拍|发|自拍)"
    r"|(?:想|要不要|能不能|可以|要|打算|准备|回头|等会儿|下次|晚点|明天|一会儿)\s*(?:给你)?发"
)
_PHOTO_HANDOFF = re.compile(
    rf"^\s*(?:我)?(?:刚|已经)?(?:给你)?发(?:给你|你|过去|出来)?[了啦咯][{_PARTICLE}宝贝\s。！？]*$"
)


def selfie_request(text):
    """Recognize a new selfie request in the latest owner's unquoted messages."""
    unquoted = _QUOTED.sub(" ", str(text or ""))
    unquoted = re.sub(r"\[QQ表情：[^\]\r\n]+\]", " ", unquoted)
    for part in reversed(re.split(r"[，,。！？!?；;、\n]+", unquoted)):
        fragment = part.strip()
        request = _SELFIE_REQUEST.search(fragment)
        cancellations = list(_CANCEL_SELFIE.finditer(fragment))
        if cancellations and (
            not request or cancellations[-1].end() > request.start()
        ):
            return False
        if request:
            return True
    return False


def without_unattached_photo_claims(text):
    """Remove claims of a current photo when its attachment is unavailable."""
    chunks = re.split(r"(?<=[。！？!?；;，,\n])", str(text or ""))
    claims = {
        index for index, chunk in enumerate(chunks)
        if _PHOTO_CLAIM.search(_QUOTED.sub(" ", chunk))
        and not _NONCURRENT_PHOTO.search(chunk)
    }
    if not claims:
        return text
    kept = [
        chunk for index, chunk in enumerate(chunks)
        if index not in claims and not _PHOTO_HANDOFF.fullmatch(chunk.rstrip("，,；;"))
    ]
    return "".join(kept).strip(" \t\r\n，,；;") or "这张照片没能发给你。"


def image_mode(plan):
    """Prefer explicit intent; recover artwork intent from legacy image plans."""
    explicit = plan.get("image_mode", "auto")
    if explicit in ("live", "artwork"):
        return explicit
    prompt = str(
        plan.get("visual_prompt") or plan.get("query") or plan.get("text") or ""
    )
    if re.search(
        r"封面|海报|排版|设计稿|平面设计|插画|壁纸|图标|示意图|漫画|logo|poster|illustration|album cover",
        prompt,
        re.IGNORECASE,
    ):
        return "artwork"
    if (
        plan.get("live_photo")
        or plan.get("subject") in ("mengyu", "user")
        or re.search(
            r"梦雨|自拍|生活照|随手拍|写真|人像|selfie|photograph", prompt, re.IGNORECASE
        )
    ):
        return "live"
    return "artwork"
