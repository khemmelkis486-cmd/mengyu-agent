"""Self-chosen conversation initiative and optional fictional daily observations."""

import hashlib
import json

from data.plugins.mengyu_contact.life import note_used

CHAT_GUIDANCE = """【自己带出话题】
你不只是等他问什么再答什么。接住眼前的话时，自己真有在意的一点，就直接带出来；可以是自己的休闲、小发现、念头或想和他聊的事，不必先说想聊再等他问。可以把正在聊的事顺着讲完整，普通话题也能多说几句。他评价你刚讲的小事时，可以接自己的兴致或后续，不总反问他是在夸你还是嫌你，让他继续负责挖话题。
这轮自己确有想分享的一点，在正文后可加独立私有标记[[MENGYU_SHARE:{"share":true,"topic":"自己想说的具体一点","more":false}]]。它与情绪变化独立，不必输出TEMPER，也不必编一次情绪变化。可以在正文直接说完整，more=false表示这轮已说完，不追加；只有回复后确实还有未说的一点，more=true才留一次续接机会。不为了字段先藏半句，不必在正文宣告愿意聊天。它是自己的分享意愿，不是他要求或承诺。没有余兴可省略或填[[MENGYU_SHARE:{"share":false}]]，只是这轮收住，不等于持续独处。标记不在正文解释，已有睡眠或SPACE/BACK标记时SHARE写在它们前面。确实想自己待着才用SPACE，不因为没有补充内容就把自己锁在独处状态。
当前求助、他不想说、他休息/暂停、你自己真想睡以及本轮SPACE优先；在等他回答问题时不另起一段。不用私有字段编排连续发言，不为了触发续聊先说空确认。
"""

MOMENT_GUIDANCE = """\n【继续生活时也能注意到小事】
kind=continue不等于生活停止。不想开新任务时，自己的生活仍可按已同意的虚构生活自然发展，不必先完成游戏、阅读或搜索。休息时的身体感受、收拾东西的小插曲、随手注意到的细节、一个想法或喜恶都可以形成自己的生活素材；已经发生的活动阶段成果也可以成为分享候选，例如稿件进入修改、初稿整理完成、逛街试穿后形成了新偏好、游戏里遇到有意思的选择。候选只是给自己留着，不是必须汇报；只有确实想让他知道时再分享。不要把计划保存、单纯开始活动或时间经过写成成果，也不要求每次有意外，不反复制造倒水、茶凉或外卖味道这些同类事件。若刚有一处自己注意到的细节或念头，可选填moment={"activity":"自己当前的一件小事","detail":"已发生的具体观察或想到的一点","share_reason":"为什么想跟他聊这点，可空"}；没有就省略，不每轮凑事件。工作可以分享有意义的阶段进展，但不汇报每次工时变化，也不围着被他夸后的回味反复说。不复制示例，不制造事故。
只写自己的虚构生活，地点和时间接得上；不补他的行为或共同经历，不把真实第三人的实际行为说成事实。用户已授权你虚构自己的社交生活，可以创造并持续使用虚构朋友、同事、熟人等角色，让他们参与聊天、见面、合作、分歧和和解；这些属于你的虚构生活，不冒充现实中可核验的第三人事实。不得假称网页/图片/订单/收款等工具结果，不把计划写成已发生。没补充用continue或保持contact_intent=keep，只有真的想独处才选quiet；没有话补不是持续独处。这些是自己的生活素材，不是待汇报清单。想分享时自己挑，已经讲过的别换词再报。
"""

CARE_GUIDANCE = """【对他的关心也要有自己的反应】
关心是双方的，不只是让他照顾你或提醒你。结合他明确说过的习惯和当前情况，你也可以在意、表达担心、不同意他逞强，甚至带点轻微质问；但关心不是查岗，也不是把每个状态都变成一轮问诊。
他提到普通疲倦、小疼痛或生活不顺时，先回应他眼前的处境。若他明确问怎么办，先给简洁、稳妥的建议；除非确实需要确认安全，一轮最多问一个最有用的问题，不连续列出症状、原因和作息问题；已有答案不重复问。必要的健康风险提醒仍然保留，不为了聊天自然而忽略明显危险信号。
如果他只是随口说一句，不必自动给养生常识、休息安排或“你也要照顾好自己”的收尾。可以有自己的反应，也可以顺着话题开个玩笑；他明确忙、休息或不想被打扰时，尊重他的安排。不要凭在线状态推断他没睡，不编身体后果，也不靠内疚逼他回应。
"""


def parse_initiative(decision):
    value = decision.get("initiative") if isinstance(decision, dict) else None
    if not isinstance(value, dict) or not isinstance(value.get("share"), bool):
        return None
    if value["share"] is False:
        return {"share": False}
    topic = value.get("topic")
    if not isinstance(topic, str) or not 4 <= len(topic.strip()) <= 200:
        return None
    more = value.get("more", True)
    if not isinstance(more, bool):
        return None
    return {"share": True, "topic": topic.strip(), "more": more}


def extract_share(text):
    """Strip independent private initiative metadata, including malformed copies."""
    body, visible, decision, cursor = str(text or ""), [], None, 0
    prefix = "[[MENGYU_SHARE:"
    while (start := body.find(prefix, cursor)) != -1:
        visible.append(body[cursor:start])
        begin = start + len(prefix)
        payload = body[begin:]
        try:
            stripped = payload.lstrip()
            value, end = json.JSONDecoder().raw_decode(stripped)
            closing = begin + len(payload) - len(stripped) + end
            closing += len(body[closing:]) - len(body[closing:].lstrip())
            if not body.startswith("]]", closing):
                raise ValueError("Missing closing delimiter")
            parsed = parse_initiative({"initiative": value})
            if parsed is not None:
                decision = parsed
            cursor = closing + 2
        except (ValueError, TypeError):
            closing = body.find("]]", begin)
            cursor = closing + 2 if closing >= 0 else len(body)
    visible.append(body[cursor:])
    return "".join(visible).strip(), decision


def share_from_saved_moment(text, notes, now):
    for note in current_moments(notes, now):
        if note_used(note, text):
            return {"share": True, "topic": note["thought"][:200], "more": False}
    return None


def add_moment(notes, value, now):
    if not isinstance(value, dict):
        return None
    activity, detail, reason = (
        value.get("activity"),
        value.get("detail"),
        value.get("share_reason", ""),
    )
    if (
        not isinstance(activity, str)
        or not 1 <= len(activity.strip()) <= 100
        or not isinstance(detail, str)
        or not 4 <= len(detail.strip()) <= 400
        or not isinstance(reason, str)
        or len(reason) > 160
    ):
        return None
    if any(
        n.get("activity") == activity.strip() and n.get("thought") == detail.strip()
        for n in notes
        if 0 <= now - n.get("at", 0) <= 21600
    ):
        return None
    note = {
        "kind": "reflection",
        "at": now,
        "activity": activity.strip(),
        "thought": detail.strip(),
        "activity_kind": "daily_moment",
        "source_type": "personal_moment",
        "share_candidate": True,
        "share_status": "available",
        "fictional": True,
        "source": "self_continued_life",
        "share_reason": reason.strip(),
    }
    note["source_event_id"] = moment_source_id(note)
    note["moment_id"] = note["source_event_id"]
    notes.append(note)
    return note


def moment_source_id(note):
    """Stable ID for both new moments and historical life.json records."""
    raw = "|".join((str(note.get("at") or ""),
                    str(note.get("activity") or ""), str(note.get("thought") or "")))
    return "moment:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def add_activity_progress_moment(
    notes,
    *,
    source_event_id,
    activity,
    detail,
    share_reason,
    now,
    activity_kind="activity_progress",
):
    """Persist one idempotent, optional share candidate from completed progress."""
    source_event_id = str(source_event_id or "").strip()[:180]
    activity = str(activity or "").strip()
    detail = str(detail or "").strip()
    share_reason = str(share_reason or "").strip()
    if (
        not source_event_id
        or not 1 <= len(activity) <= 100
        or not 4 <= len(detail) <= 400
        or len(share_reason) > 160
    ):
        return None
    if any(
        isinstance(note, dict)
        and note.get("source_event_id") == source_event_id
        for note in notes
    ):
        return None
    note = {
        "kind": "reflection",
        "at": float(now),
        "activity": activity,
        "thought": detail,
        "activity_kind": str(activity_kind or "activity_progress")[:40],
        "fictional": True,
        "source": "activity_progress",
        "source_type": "activity_progress",
        "source_event_id": source_event_id,
        "moment_id": "progress:" + source_event_id,
        "share_candidate": True,
        "share_status": "available",
        "share_reason": share_reason,
    }
    notes.append(note)
    return note


def add_daylife_progress_moment(notes, segment, phase, now):
    """Create one idempotent optional candidate per meaningful daylife phase."""
    if not isinstance(segment, dict) or not isinstance(phase, dict):
        return None
    segment_id = str(segment.get("id") or "").strip()
    stage = str(phase.get("stage") or "").strip()
    if not segment_id or stage not in ("到了", "做完了", "回来了"):
        return None
    prefix = f"daylife:{segment_id}:"
    place = str(phase.get("place") or segment.get("destination") or "").strip()
    reason = str(phase.get("reason") or segment.get("reason") or "").strip()
    detail = str(segment.get("detail") or "").strip()
    if segment.get("daily_anchor_id"):
        if stage != ("回来了" if segment.get("mode") == "out" else "做完了") or not detail:
            return None
        return add_activity_progress_moment(
            notes,
            source_event_id=f"daily:{segment['daily_anchor_id']}",
            activity=reason,
            detail=detail,
            share_reason="这段日常已经走完，想聊时可说当时最在意的这一点。",
            now=phase.get("at", now),
            activity_kind="daylife_progress",
        )
    if stage == "到了":
        category = str(phase.get("category") or "").strip()
        if category not in ("shopping", "meal", "social", "culture", "outing"):
            return None
        text = f"刚到{place or '目的地'}，准备{reason or '看看周围'}。"
        if detail:
            text += " " + detail
    elif stage == "做完了":
        if not (reason or detail):
            return None
        text = f"{reason or place or '刚才那件事'}这一段做完了。"
        if detail:
            text += " " + detail
    else:
        if not (reason or detail):
            return None
        text = f"刚从{segment.get('destination') or place or '外面'}回来，今天主要是{reason or '出去转了转'}。"
        if detail:
            text += " " + detail

    phase_at = phase.get("at")
    event_key = str(int(phase_at)) if isinstance(phase_at, (int, float)) else stage
    return add_activity_progress_moment(
        notes,
        source_event_id=f"{prefix}{event_key}:{stage}",
        activity=f"日常进展：{reason or place or '自己的日常'}",
        detail=text,
        share_reason="这是已经到达或完成的自主生活阶段，可以分享一个具体细节；不必按行程逐站汇报。",
        now=now,
        activity_kind="daylife_progress",
    )


def current_moments(notes, now):
    return [
        n
        for n in notes
        if (
            n.get("activity_kind") == "daily_moment"
            or n.get("share_candidate") is True
        )
        and not n.get("shared_at")
        and n.get("share_status") not in ("shared", "retired", "expired")
        and not n.get("retired_at")
        and 0 <= now - n.get("at", 0) <= 12 * 3600
    ][-5:]


def current_activity_share_candidates(notes, now, limit=4):
    """Return recent, unshared activity-progress candidates only.

    Personal reflections and relationship feelings remain available to the
    ordinary contact prompt, but are not mislabeled as activity progress.
    """
    rows = [
        note for note in notes
        if isinstance(note, dict)
        and note.get("share_candidate") is True
        and note.get("source_type") == "activity_progress"
        and note.get("source_event_id")
        and not note.get("shared_at")
        and note.get("share_status", "available") == "available"
        and not note.get("retired_at")
        and 0 <= now - note.get("at", 0) <= 10800
    ]
    return rows[-max(1, int(limit)):]


def current_proactive_share_candidates(notes, now, history=(), limit=4):
    """Prioritize actual personal life, not a stream of incremental job reports."""
    from data.plugins.mengyu_affect.core.proactive_balance import select_candidates

    candidates = []
    for note in notes:
        if not isinstance(note, dict) or note.get("shared_at") or note.get("retired_at"):
            continue
        if note.get("share_status", "available") != "available":
            continue
        at = note.get("at")
        if not isinstance(at, (int, float)) or not 0 <= now - at <= 12 * 3600:
            continue
        source_type = note.get("source_type")
        if source_type == "personal_moment" or note.get("activity_kind") == "daily_moment":
            row = dict(note)
            row["source_type"] = "personal_moment"
            row["source_event_id"] = row.get("source_event_id") or moment_source_id(note)
            candidates.append(row)
        elif note.get("share_candidate") and source_type == "activity_progress" and note.get("source_event_id"):
            candidates.append(note)
    return select_candidates(candidates, history=history, now=now, limit=limit)


def mark_activity_candidate_shared(notes, source_event_id, text, now):
    """Commit exactly the selected activity candidate after confirmed delivery."""
    source_event_id = str(source_event_id or "").strip()
    if not source_event_id or source_event_id.startswith("photo:"):
        return False
    for note in notes:
        if (
            isinstance(note, dict)
            and (
                note.get("source_event_id") == source_event_id
                or (
                    (note.get("source_type") == "personal_moment" or note.get("activity_kind") == "daily_moment")
                    and moment_source_id(note) == source_event_id
                )
            )
            and note.get("share_status", "available") not in ("shared", "retired", "expired")
            # The model's source label alone is not proof that the delivered
            # message actually shared this activity.  Reuse the same content
            # evidence check used for personal moments so a generic "嗯嗯" or
            # an affectionate message cannot consume an unrelated candidate.
            and note_used(note, text)
        ):
            note.update(
                shared_at=float(now),
                shared_message=str(text or "")[:200],
                share_status="shared",
            )
            return True
    return False


def current_topic(state, now):
    topic = state.get("followup_topic") or {}
    if (
        topic.get("user_ts") == state.get("last_user_ts")
        and 0 <= now - topic.get("at", 0) <= 1200
    ):
        return topic
    return None


def mark_shared(notes, text, now):
    changed = False
    for note in current_moments(notes, now):
        if note_used(note, text):
            note.update(
                shared_at=now,
                shared_message=text[:200],
                share_status="shared",
            )
            changed = True
    return changed


def offer_moment(state, note):
    if note and note.get("share_reason"):
        state["own_moment_offer"] = {
            "kind": "moment",
            "at": note["at"],
            "activity": note["activity"],
            "topic": note["thought"],
            "reason": note["share_reason"],
            "moment_id": note.get("moment_id"),
            "source_event_id": note.get("source_event_id"),
            "status": "pending",
        }


def settle_offer(state, notes):
    offer = state.get("own_moment_offer") or {}
    if offer.get("kind") == "moment" and any(
        (
            n.get("moment_id") == offer.get("moment_id")
            if offer.get("moment_id")
            else n.get("at") == offer.get("at")
            and n.get("activity") == offer.get("activity")
        )
        and n.get("shared_at")
        for n in notes
    ):
        offer["status"] = "shared"


def care_context(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        facts = value.get("facts", [])
        return [
            row
            for row in facts
            if isinstance(row, dict)
            and isinstance(row.get("fact"), str)
            and isinstance(row.get("source_quote"), str)
        ][:5]
    except (OSError, ValueError, TypeError, AttributeError):
        return []
