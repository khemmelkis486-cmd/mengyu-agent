"""QQ face IDs verified against the installed NapCat face table."""

import re

FACES = {
    "微笑": 14,
    "撇嘴": 1,
    "得意": 4,
    "害羞": 6,
    "大哭": 9,
    "调皮": 12,
    "呲牙": 13,
    "偷笑": 20,
    "可爱": 21,
    "憨笑": 28,
    "委屈": 106,
    "笑哭": 182,
    "捂脸": 264,
    "比心": 319,
    "拥抱": 49,
    "爱心": 66,
    "亲亲": 360,
}
PATTERN = re.compile(r"\[\[QQ:([^\]\n]{1,12})\]\]")
FALLBACK = {
    14: "笑了",
    1: "哼",
    4: "那当然",
    6: "有点不好意思了",
    9: "难过了",
    12: "逗你的",
    13: "哈哈",
    20: "被你逗笑了",
    21: "嘿嘿",
    28: "嘿嘿",
    106: "有点委屈",
    182: "笑死",
    264: "哎呀",
    319: "喜欢你",
    49: "抱抱你",
    66: "喜欢",
    360: "亲亲你",
}


def limit_face_parts(parts, plain_since_face, explicit=False):
    faces = [value for kind, value in parts if kind in ("face", "meme")]
    if not faces:
        return parts, plain_since_face + 1
    if explicit or plain_since_face >= 3 or any(kind == "meme" for kind, _ in parts):
        used = False
        result = []
        preferred = "meme" if any(kind == "meme" for kind, value in parts) else "face"
        for kind, value in parts:
            if kind in ("face", "meme"):
                if kind != preferred:
                    continue
                if used:
                    continue
                used = True
            result.append((kind, value))
        return result, 0
    result = [(kind, value) for kind, value in parts if kind not in ("face", "meme")]
    if not any(kind != "text" or str(value).strip() for kind, value in result):
        face_id = next((value for kind, value in parts if kind == "face"), None)
        result = [("text", FALLBACK.get(face_id, "嗯"))]
    return result, plain_since_face + 1


def segments(text):
    parts = []
    last = 0
    used = False
    for match in PATTERN.finditer(text):
        if match.start() > last:
            parts.append(("text", text[last : match.start()]))
        name = match.group(1)
        if name in FACES:
            if not used:
                parts.append(("face", FACES[name]))
                used = True
        else:
            parts.append(("text", match.group(0)))
        last = match.end()
    if last < len(text):
        parts.append(("text", text[last:]))
    if not parts and text:
        parts = [("text", text)]
    return parts
