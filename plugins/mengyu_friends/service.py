"""Persistent friends, their invitations, and completed meetups."""

import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from . import context
from .friends import FRIENDS
from .plan import (
    ACTIVITIES,
    OPEN_INVITES,
    PROFILES,
    RELATIONS,
    select_activity,
    slot_times,
)
from .store import Store

DAY = 86400


def _as_meetup(row):
    """Expose friend IDs as a list.

    Args:
        row: Stored meetup or None.

    Returns:
        Decoded record or None.
    """
    if row is None:
        return None
    result = dict(row)
    result["friend_ids"] = json.loads(result.pop("friend_ids_json"))
    return result


class Friends:
    """Run fictional friend decisions without a model or a separate worker."""

    def __init__(self, path):
        self.store = Store(Path(path))

    def initialize(self, now=None):
        """Seed existing character identities on the first run.

        Args:
            now: Optional injected clock for testing.
        """
        stamp = float(time.time() if now is None else now)
        self.store.initialize()
        legacy = self.store.path.with_name("friends.json")
        names = {}
        try:
            people = json.loads(legacy.read_text(encoding="utf-8")).get("friends", ())
            names = {row["id"]: row["name"] for row in people if isinstance(row, dict)}
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            pass
        with self.store.use(write=True) as db:
            for friend in FRIENDS:
                key = friend["id"]
                relation, distance, level, interests, activities, minimum, maximum = (
                    PROFILES[key]
                )
                db.execute(
                    """INSERT OR IGNORE INTO friends (
                       friend_id,name,aliases_json,relation_type,distance_band,familiarity,status,
                       interests_json,activities_json,contact_min_days,contact_max_days,next_invite_after,
                       current_note,created_at,updated_at) VALUES(?,?,?, ?,?,?,'active', ?,?,?,?, ?,?,?,?)""",
                    (
                        key,
                        str(names.get(key) or friend["name"]),
                        "[]",
                        relation,
                        distance,
                        level,
                        json.dumps(interests, ensure_ascii=False),
                        json.dumps(activities),
                        minimum,
                        maximum,
                        stamp,
                        friend["relation"],
                        stamp,
                        stamp,
                    ),
                )

    @staticmethod
    def _accept(db, invite, stamp):
        """Create the unique meetup associated with an accepted invitation.

        Args:
            db: Active write connection.
            invite: Invitation record joined with the friend's name.
            stamp: Current time.

        Returns:
            Decoded meetup or None for an invalid transition.
        """
        if invite is None:
            return None
        identity = "meetup:" + invite["invite_id"]
        existing = db.execute(
            "SELECT * FROM meetups WHERE meetup_id=?", (identity,)
        ).fetchone()
        if existing:
            return (
                _as_meetup(existing)
                if existing["status"] not in ("cancelled", "no_show")
                else None
            )
        if invite["status"] not in (*OPEN_INVITES, "deferred"):
            return None
        activity = invite["activity_type"]
        display, place = ACTIVITIES[activity][:2]
        db.execute(
            """INSERT INTO meetups (meetup_id,invite_id,friend_ids_json,title,activity_type,status,
               mode,place,created_at,updated_at) VALUES(?,?,?,?,?,'scheduled','out',?,?,?)""",
            (
                identity,
                invite["invite_id"],
                json.dumps([invite["friend_id"]]),
                "和" + invite["name"] + display,
                activity,
                place,
                stamp,
                stamp,
            ),
        )
        db.execute(
            "UPDATE invitations SET status='accepted', decision_reason=?, updated_at=? WHERE invite_id=?",
            ("时间合适，想一起出去", stamp, invite["invite_id"]),
        )
        db.execute(
            """UPDATE friends SET last_contact_at=?,next_invite_after=MAX(next_invite_after, ? + contact_min_days*?),
               updated_at=? WHERE friend_id=?""",
            (stamp, stamp, DAY, stamp, invite["friend_id"]),
        )
        return _as_meetup(
            db.execute(
                "SELECT * FROM meetups WHERE meetup_id=?", (identity,)
            ).fetchone()
        )

    def nightly(
        self, now=None, *, target_day=None, rest=False, rain=False, tight=False
    ):
        """Generate and decide at most two invitations for one plan date.

        Args:
            now: Timestamp of the planning call.
            target_day: Explicit local date the planner is generating.
            rest: The persona needs a quiet day.
            rain: Outdoor walks are unsuitable.
            tight: Prefer low-cost outings.

        Returns:
            IDs of newly invited and accepted invitations.
        """
        stamp = float(time.time() if now is None else now)
        target = target_day or (
            datetime.fromtimestamp(stamp).date() + timedelta(days=1)
        )
        if isinstance(target, str):
            target = date.fromisoformat(target)
        key = target.isoformat()
        invited = []
        accepted = []
        with self.store.use(write=True) as db:
            count = db.execute(
                "SELECT COUNT(*) FROM invitations WHERE preferred_date=?", (key,)
            ).fetchone()[0]
            if count >= 2:
                return {"invited": invited, "accepted": accepted}
            already_planned = bool(
                db.execute(
                    """SELECT 1 FROM meetups m JOIN invitations i USING(invite_id)
                   WHERE (i.status='accepted' AND i.preferred_date<=?)
                      OR (m.status IN ('scheduled','in_progress') AND
                          m.scheduled_start_at>=? AND m.scheduled_start_at<?) LIMIT 1""",
                    (
                        key,
                        datetime.combine(target, datetime.min.time()).timestamp(),
                        datetime.combine(
                            target + timedelta(days=1), datetime.min.time()
                        ).timestamp(),
                    ),
                ).fetchone()
            )
            social_tired = (
                db.execute(
                    "SELECT COUNT(*) FROM meetups WHERE status='completed' AND completed_at>=?",
                    (stamp - 2 * DAY,),
                ).fetchone()[0]
                >= 2
            )
            candidates = db.execute(
                """SELECT * FROM friends f WHERE status='active' AND next_invite_after<=?
                   AND (last_meetup_at IS NULL OR last_meetup_at+contact_min_days*?<=?)
                   AND NOT EXISTS (SELECT 1 FROM invitations i WHERE i.friend_id=f.friend_id
                       AND (i.preferred_date=? OR i.status IN ('invited','candidate','accepted')))
                   AND NOT EXISTS (
                       SELECT 1 FROM meetups m JOIN invitations i USING(invite_id)
                       WHERE i.friend_id=f.friend_id AND m.life_slot_id IS NOT NULL
                         AND m.status IN ('scheduled','in_progress'))
                   ORDER BY COALESCE(last_meetup_at,0),familiarity DESC,friend_id LIMIT ?""",
                (stamp, DAY, stamp, key, 2 - count),
            ).fetchall()
            for friend in candidates:
                friend_id = friend["friend_id"]
                activity = select_activity(
                    friend_id,
                    target,
                    json.loads(friend["activities_json"]),
                    rain=rain,
                    tight=tight,
                )
                identity = f"invite:{friend_id}:{key.replace('-', '')}:1"
                initiated = friend["last_contact_at"]
                initiator = (
                    "mengyu"
                    if initiated
                    and stamp - initiated >= friend["contact_max_days"] * DAY
                    else "friend"
                )
                reason = (
                    "好久没见，想约出来聊聊"
                    if initiator == "mengyu"
                    else "朋友问要不要一起" + ACTIVITIES[activity][0]
                )
                db.execute(
                    """INSERT INTO invitations (invite_id,friend_id,seq,initiator,activity_type,
                       status,window,preferred_date,reason,created_at,updated_at)
                       VALUES(?,?,1,?,?,'invited',?,?,?,?,?)""",
                    (
                        identity,
                        friend_id,
                        initiator,
                        activity,
                        ACTIVITIES[activity][2],
                        key,
                        reason,
                        stamp,
                        stamp,
                    ),
                )
                invited.append(identity)
                if (
                    rest
                    or social_tired
                    or already_planned
                    or friend["distance_band"] == "far"
                ):
                    why = "这两天想休息" if rest or social_tired else "这次时间不合适"
                    decision = (
                        "declined" if friend["distance_band"] == "far" else "deferred"
                    )
                    self._answer(db, identity, decision, why, stamp, stamp + 3 * DAY)
                    continue
                invite = {
                    **dict(friend),
                    "invite_id": identity,
                    "status": "invited",
                    "activity_type": activity,
                }
                self._accept(db, invite, stamp)
                accepted.append(identity)
                already_planned = True
        return {"invited": invited, "accepted": accepted}

    def accept(self, invite_id, now=None):
        """Accept an existing invitation and return its stable meetup.

        Args:
            invite_id: Invitation identity.
            now: Optional injected clock.

        Returns:
            Meetup record or None.
        """
        stamp = float(time.time() if now is None else now)
        with self.store.use(write=True) as db:
            invite = db.execute(
                "SELECT i.*,f.name FROM invitations i JOIN friends f USING(friend_id) WHERE i.invite_id=?",
                (invite_id,),
            ).fetchone()
            return self._accept(db, invite, stamp)

    @staticmethod
    def _answer(db, invite_id, status, reason, stamp, until):
        """Apply a decision to an open invitation exactly once.

        Args:
            db: Active write connection.
            invite_id: Invitation identity.
            status: Deferred or declined.
            reason: Brief reason for the decision.
            stamp: Decision time.
            until: Earliest next invitation time.

        Returns:
            Whether the invitation changed.
        """
        changed = db.execute(
            """UPDATE invitations SET status=?,decision_reason=?,updated_at=?
               WHERE invite_id=? AND status IN ('invited','candidate') RETURNING friend_id""",
            (status, reason, stamp, invite_id),
        ).fetchone()
        if changed is None:
            return False
        db.execute(
            """UPDATE friends SET last_contact_at=?,next_invite_after=MAX(next_invite_after,?),
               updated_at=? WHERE friend_id=?""",
            (stamp, until, stamp, changed["friend_id"]),
        )
        return True

    def decline(self, invite_id, reason="", now=None):
        """Decline an open invitation and give the friend a cooldown.

        Args:
            invite_id: Invitation identity.
            reason: Optional decision note.
            now: Optional injected clock.

        Returns:
            Whether the invitation changed.
        """
        stamp = float(time.time() if now is None else now)
        with self.store.use(write=True) as db:
            return self._answer(
                db, invite_id, "declined", str(reason)[:160], stamp, stamp + 3 * DAY
            )

    def defer(self, invite_id, until=None, now=None):
        """Set an open invitation aside for a later day.

        Args:
            invite_id: Invitation identity.
            until: Earliest next invitation timestamp.
            now: Optional injected clock.

        Returns:
            Whether the invitation changed.
        """
        stamp = float(time.time() if now is None else now)
        cooldown = max(
            stamp + DAY, float(until) if until is not None else stamp + 3 * DAY
        )
        with self.store.use(write=True) as db:
            return self._answer(
                db, invite_id, "deferred", "等有合适的空再约", stamp, cooldown
            )

    def planning_candidates(self, day):
        """Project one accepted meetup for a newly generated local day.

        Args:
            day: ISO date or date object.

        Returns:
            Zero or one fixed Life commitment.
        """
        day = date.fromisoformat(day) if isinstance(day, str) else day
        key = day.isoformat()
        midnight = datetime.combine(day, datetime.min.time()).timestamp()
        next_midnight = datetime.combine(
            day + timedelta(days=1), datetime.min.time()
        ).timestamp()
        with self.store.use() as db:
            rows = db.execute(
                """SELECT m.*,i.window FROM meetups m JOIN invitations i USING(invite_id)
                   WHERE m.status='scheduled' AND (
                       (i.status='accepted' AND i.preferred_date<=? AND m.life_slot_id IS NULL)
                       OR (i.status='scheduled' AND m.scheduled_start_at>=? AND m.scheduled_start_at<?))
                   ORDER BY (m.life_slot_id IS NULL),m.created_at,m.meetup_id LIMIT 1""",
                (key, midnight, next_midnight),
            ).fetchall()
        commitments = []
        for row in rows:
            start, end = slot_times(day, row["activity_type"], row["window"])
            start = (
                row["scheduled_start_at"]
                if row["scheduled_start_at"] is not None
                else start
            )
            end = (
                row["scheduled_end_at"] if row["scheduled_end_at"] is not None else end
            )
            identity = row["meetup_id"]
            commitments.append(
                {
                    "id": "friend-meetup:" + identity,
                    "friend_meetup_id": identity,
                    "friend_ids": json.loads(row["friend_ids_json"]),
                    "name": row["title"],
                    "kind": "social_meetup",
                    "social_activity_type": row["activity_type"],
                    "start": start,
                    "end": end,
                    "duration_min": int((end - start) / 60),
                    "status": "planned",
                    "replaceable": False,
                    "commitment": True,
                    "mode": row["mode"],
                    "place": row["place"],
                    "quota": row["mode"] == "out",
                }
            )
        return commitments

    def mark_scheduled(self, meetup_id, slot_id, start, end, now=None):
        """Bind the immutable Life slot after its plan has been saved.

        Args:
            meetup_id: Meetup identity.
            slot_id: Stable Life slot identifier.
            start: Start timestamp.
            end: End timestamp.
            now: Optional injected clock.

        Returns:
            Whether the binding already exists or could be made.
        """
        if slot_id != "friend-meetup:" + meetup_id or not start < end:
            return False
        stamp = float(time.time() if now is None else now)
        with self.store.use(write=True) as db:
            row = db.execute(
                "SELECT * FROM meetups WHERE meetup_id=?", (meetup_id,)
            ).fetchone()
            if row is None:
                return False
            if row["life_slot_id"]:
                return (
                    row["life_slot_id"],
                    row["scheduled_start_at"],
                    row["scheduled_end_at"],
                ) == (slot_id, start, end)
            if row["status"] != "scheduled":
                return False
            db.execute(
                """UPDATE meetups SET life_slot_id=?,scheduled_start_at=?,scheduled_end_at=?,
                   updated_at=? WHERE meetup_id=?""",
                (slot_id, start, end, stamp, meetup_id),
            )
            db.execute(
                "UPDATE invitations SET status='scheduled',updated_at=? WHERE invite_id=?",
                (stamp, row["invite_id"]),
            )
            return True

    def record_transition(self, meetup_id, stage, now, **details):
        """Complete an executed meetup or record a skipped appointment.

        Args:
            meetup_id: Meetup identity.
            stage: Started, finished, missed, or cancelled.
            now: Timestamp of the transition.
            **details: Optional factual outcome overrides from Life.

        Returns:
            Whether the transition is recorded, including identical replays.
        """
        next_state = {
            "started": "in_progress",
            "finished": "completed",
            "missed": "no_show",
            "cancelled": "cancelled",
        }.get(stage)
        if not next_state:
            return False
        with self.store.use(write=True) as db:
            row = db.execute(
                "SELECT m.*,i.friend_id FROM meetups m JOIN invitations i USING(invite_id) WHERE m.meetup_id=?",
                (meetup_id,),
            ).fetchone()
            if row is None or not row["life_slot_id"]:
                return False
            if row["status"] == next_state or (
                stage == "started" and row["status"] == "completed"
            ):
                return True
            if stage == "finished":
                if row["status"] != "in_progress" or now < row["scheduled_end_at"]:
                    return False
                _, _, _, _, note, follow = ACTIVITIES[row["activity_type"]]
                values = tuple(
                    str(details.get(key) or fallback).strip()[:160] or fallback
                    for key, fallback in (
                        ("outcome", "见到了，聊得挺自在"),
                        ("mood", "放松"),
                        ("detail", note),
                        ("follow_up", follow),
                    )
                )
                db.execute(
                    """UPDATE meetups SET status='completed',outcome=?,mood=?,detail=?,follow_up=?,
                       completed_at=?,updated_at=? WHERE meetup_id=?""",
                    (*values, now, now, meetup_id),
                )
                db.execute(
                    """UPDATE friends SET familiarity=MIN(100,familiarity+?),last_meetup_at=?,
                       last_contact_at=?,next_invite_after=?+contact_min_days*?,updated_at=? WHERE friend_id=?""",
                    (
                        3 if details.get("particularly_good") else 2,
                        now,
                        now,
                        now,
                        DAY,
                        now,
                        row["friend_id"],
                    ),
                )
                return True
            if row["status"] != "scheduled" and not (
                stage == "cancelled" and row["status"] == "in_progress"
            ):
                return False
            if (
                stage == "started"
                and not row["scheduled_start_at"] <= now < row["scheduled_end_at"]
            ):
                return False
            if stage == "missed" and now < row["scheduled_end_at"]:
                return False
            db.execute(
                "UPDATE meetups SET status=?,updated_at=? WHERE meetup_id=?",
                (next_state, now, meetup_id),
            )
            if stage != "started":
                db.execute(
                    "UPDATE invitations SET status='cancelled',updated_at=? WHERE invite_id=?",
                    (now, row["invite_id"]),
                )
            return True

    def meetup(self, meetup_id):
        """Read one meetup.

        Args:
            meetup_id: Meetup identity.

        Returns:
            Decoded meetup or None.
        """
        with self.store.use() as db:
            return _as_meetup(
                db.execute(
                    "SELECT * FROM meetups WHERE meetup_id=?", (meetup_id,)
                ).fetchone()
            )

    def snapshot(self, now=None):
        """Read a consistent profile, invitation and activity summary.

        Args:
            now: Optional clock for projecting relative availability.

        Returns:
            A local structured snapshot.
        """
        stamp = float(time.time() if now is None else now)
        with self.store.use() as db:
            people = []
            for row in db.execute("SELECT * FROM friends ORDER BY friend_id"):
                friend = dict(row)
                for key in ("aliases", "interests", "activities"):
                    friend[key] = json.loads(friend.pop(key + "_json"))
                friend["relation"] = RELATIONS.get(friend["relation_type"], "朋友")
                people.append(friend)
            invites = [
                dict(row)
                for row in db.execute(
                    """SELECT i.*,m.title,m.scheduled_start_at,m.status AS meetup_status
                   FROM invitations i LEFT JOIN meetups m USING(invite_id)
                   WHERE m.status IS NULL OR m.status IN ('scheduled','in_progress')
                   ORDER BY i.updated_at DESC,i.invite_id LIMIT 24"""
                )
            ]
            meetups = [
                _as_meetup(row)
                for row in db.execute(
                    "SELECT * FROM meetups ORDER BY updated_at DESC,meetup_id LIMIT 24"
                )
            ]
        return {
            "as_of": stamp,
            "friends": people,
            "invitations": invites,
            "meetups": meetups,
        }

    def match_mentions(self, text, recent_turns=()):
        """Resolve known names or aliases in a visible turn.

        Args:
            text: Current user text.
            recent_turns: Recent visible turns.

        Returns:
            Match status and stable friend IDs.
        """
        return context.match_mentions(self.snapshot()["friends"], text, recent_turns)

    def context_for_chat(self, query_text, recent_turns=(), now=None, limit=3):
        """Read grounded friend context for the current topic.

        Args:
            query_text: Current topic text.
            recent_turns: Recent visible turns.
            now: Optional injected clock.
            limit: Number of returned records per section.

        Returns:
            A topic-filtered context with booking and completion distinguished.
        """
        return context.context_for_chat(
            self.snapshot(now), query_text, recent_turns, limit
        )
