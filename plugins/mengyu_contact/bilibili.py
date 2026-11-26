"""Browse public Bilibili metadata and any openly available subtitles."""

import json
import logging
import random
import re

if __package__:
    from data.plugins.mengyu_life.core.exploration import fetch_public
else:
    from data.plugins.mengyu_life.core.exploration import fetch_public


async def browse_bilibili(client, query, max_duration=0):
    """Find a video and read actual public metadata or available subtitles.

    Args:
        client: HTTP client used for the fixed Bilibili API endpoints.
        query: Search keywords, a BV identifier, or a regular video URL.
        max_duration: Optional maximum seconds for autonomous short-video browsing.

    Returns:
        Title, material, source links and whether subtitles were actually read.

    Raises:
        ValueError: The public API is unavailable or no video can be selected.
        httpx.HTTPError: A required request fails.
    """
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}
    match = re.search(r"BV[0-9A-Za-z]{10}", query)
    bvid = match.group(0) if match else None
    candidates = []
    if not bvid:
        url = "https://api.bilibili.com/x/web-interface/" + (
            "search/type" if query.strip() else "popular"
        )
        params = (
            {"search_type": "video", "keyword": query[:120], "page": 1}
            if query.strip()
            else {"pn": 1, "ps": 12}
        )
        response = await client.get(url, params=params, headers=headers)
        response.raise_for_status()
        obj = response.json()
        if obj.get("code") != 0:
            raise ValueError("Bilibili public browsing unavailable")
        data = obj.get("data") or {}
        candidates = [
            item
            for item in (data.get("result") or data.get("list") or [])
            if item.get("bvid")
        ]
        if max_duration:
            short = []
            preferred = []
            for item in candidates:
                raw = item.get("duration")
                try:
                    if isinstance(raw, str) and ":" in raw:
                        seconds = 0
                        for part in raw.split(":"):
                            seconds = seconds * 60 + int(part)
                    else:
                        seconds = float(raw or 0)
                except (ValueError, TypeError):
                    continue
                if 0 < seconds <= max_duration:
                    short.append(item)
                    if seconds <= 90:
                        preferred.append(item)
            candidates = preferred or short
            if not candidates:
                raise ValueError(
                    "No verified short video results; not selecting a long fallback"
                )
        if not candidates:
            raise ValueError("No public video results")
        selected = (
            random.choice(candidates[:8])
            if max_duration
            else candidates[0]
            if query.strip()
            else random.choice(candidates)
        )
        bvid = selected["bvid"]
    response = await client.get(
        "https://api.bilibili.com/x/web-interface/view",
        params={"bvid": bvid},
        headers=headers,
    )
    response.raise_for_status()
    obj = response.json()
    if obj.get("code") != 0 or not obj.get("data"):
        raise ValueError("Video details unavailable")
    video = obj["data"]
    duration = float(video.get("duration") or 0)
    if max_duration and not 0 < duration <= max_duration:
        raise ValueError("Video duration is unknown or exceeds the short-video limit")
    title = str(video.get("title") or bvid)[:200]
    sources = [{"title": title, "url": "https://www.bilibili.com/video/" + bvid}]
    transcript = ""
    if video.get("cid"):
        try:
            response = await client.get(
                "https://api.bilibili.com/x/player/v2",
                params={"bvid": bvid, "cid": video["cid"]},
                headers=headers,
            )
            response.raise_for_status()
            subtitle_data = response.json()
            subtitles = ((subtitle_data.get("data") or {}).get("subtitle") or {}).get(
                "subtitles"
            ) or []
            if subtitle_data.get("code") == 0 and subtitles:
                chosen = next(
                    (
                        item
                        for item in subtitles
                        if str(item.get("lan", "")).startswith("zh")
                    ),
                    subtitles[0],
                )
                url = chosen.get("subtitle_url", "")
                if url.startswith("//"):
                    url = "https:" + url
                if url:
                    _, raw, _ = await fetch_public(client, url, headers=headers)
                    transcript = "\n".join(
                        str(item.get("content", ""))
                        for item in json.loads(raw).get("body", [])
                    )[:10000]
        except Exception as exc:  # noqa: BLE001 - metadata browsing still succeeded
            logging.getLogger(__name__).warning(
                "Public subtitles unavailable: %s", type(exc).__name__
            )
    material = {
        "标题": title,
        "UP主": (video.get("owner") or {}).get("name"),
        "分区": video.get("tname"),
        "时长秒": video.get("duration"),
        "简介": str(video.get("desc") or "")[:4000],
        "本次读取范围": "已实际读取公开字幕，没有播放或看完视频画面"
        if transcript
        else "仅浏览视频标题和简介，没有播放或看完，也没有读到字幕；不能编造镜头、台词或视频结论",
        "公开字幕": transcript,
        "同次看到的其他视频": [
            {
                "bvid": item["bvid"],
                "title": re.sub("<[^>]+>", "", str(item.get("title", ""))),
            }
            for item in candidates[:5]
        ],
    }
    return {
        "title": title,
        "material": json.dumps(material, ensure_ascii=False),
        "sources": sources,
        "transcript_available": bool(transcript),
        "bvid": bvid,
        "duration_seconds": duration,
    }
