"""Bridge the installed meme_manager selection into owner-only proactive chat."""

import random
import re
from pathlib import Path
from types import SimpleNamespace

MARKER = re.compile(r"&&([a-zA-Z0-9_-]{1,50})&&")
EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif"}


def load_catalog(context, umo, persona):
    getter = getattr(context, "get_registered_star", None)
    if not getter:
        return {}
    metadata = getter("meme_manager")
    if not metadata or not getattr(metadata, "activated", False):
        return {}
    plugin = getattr(metadata, "star_cls", None)
    resolve = getattr(plugin, "_resolve_runtime_pack_context", None)
    if not resolve:
        return {}
    req = SimpleNamespace(
        session_id=umo, conversation=SimpleNamespace(persona_id=persona)
    )
    selected = resolve(event=None, req=req)
    mapping = selected.get("category_mapping", {})
    catalog = {}
    for category, description in mapping.items():
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,50}", str(category)):
            continue
        files = []
        for directory in selected.get("pack_dirs", {}).values():
            root = Path(directory).resolve()
            folder = (root / "memes" / category).resolve()
            if not folder.is_relative_to(root) or not folder.is_dir():
                continue
            files.extend(
                str(p.resolve())
                for p in folder.iterdir()
                if p.is_file()
                and p.suffix.lower() in EXTENSIONS
                and p.resolve().is_relative_to(root)
            )
        if files:
            catalog[category] = {
                "description": str(description),
                "files": sorted(set(files)),
            }
    return catalog


NEIGHBOURS = {
    "kiss": ("shy", "like", "happy", "clingy"),
    "meow": ("happy", "shy", "like"),
    "meme": ("happy", "sneering", "confused", "surprised"),
    "clingy": ("like", "shy", "happy"),
    "cuddle": ("like", "shy", "happy"),
}


def extract_meme(text, catalog, last_file="", recent_files=()):
    chosen = None
    for marker in MARKER.finditer(text):
        category = marker.group(1)
        options = list(catalog.get(category, {}).get("files", []))
        if not options:
            continue
        pool = options
        # Tiny categories (kiss has 3 files, meow 7) make her look repetitive;
        # widen the pool with visually compatible neighbours.
        if len(options) < 8:
            for name in NEIGHBOURS.get(category, ()):
                pool = pool + list(catalog.get(name, {}).get("files", []))
        avoid = [last_file] + [path for path in recent_files if path]
        fresh = [path for path in pool if path not in avoid]
        if chosen is None:
            chosen = random.choice(fresh or pool)
    return MARKER.sub("", text).strip(), chosen


def explicit_emote_request(text, recent_user_texts=()):
    """Recognize direct picture or affectionate emote requests and short repeats.

    Args:
        text: Current user message, including merged QQ face descriptions.
        recent_user_texts: Earlier user messages used only for short references.

    Returns:
        Whether an optional image may bypass the ordinary emote cooldown.
    """
    text = str(text or "")
    if re.search(r"不要|别发|不用发|少发|不想亲|不想抱", text):
        return False
    affection = r"(?:求|要|给我|来)(?:个|一个)?(?:亲亲|亲一个|抱抱|抱一个)"
    if re.search(affection, text):
        return True
    if re.search(r"(发|来|给我).{0,10}(表情|偷笑|调皮|拥抱|爱心|比心|得意|捂脸)", text):
        return True
    previous = list(recent_user_texts)[-8:]
    clean = re.sub(r"\[QQ表情[：:][^\]]*\]", "", text).strip()
    if re.fullmatch(
        r"(?:刚才)?(?:哪里有|哪有|没收到|没看到|没发出来)(?:啦|呀|啊|呢|嘛)?[。.!！?？]*",
        clean,
    ):
        return any(re.search(affection, message) for message in previous)
    if re.fullmatch(
        r"(?:(?:我不认|不算|不够|没收到)[，,\s]*)?(?:再(?:来|给我|要)|还(?:想|要))(?:一)?(?:个|次)(?:嘛|呀|吧|啦|呗|啊)?[。.!！~～]*",
        clean,
    ):
        return any(re.search(affection, message) for message in previous)
    return bool(
        re.search(r"(发|来).{0,4}(一个|一张|个|张).{0,5}(看看|瞧瞧|看)", text)
        and any("表情" in message for message in previous[-3:])
    )
