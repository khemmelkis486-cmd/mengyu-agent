"""Persist real reading, small creations and evidence-backed follow-ups."""

from data.plugins.mengyu_contact import config

import asyncio
import hashlib
import json
import logging
import math
import random
import re
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import httpx

from data.plugins.mengyu_contact.state_write import write_json

from data.plugins.mengyu_contact.local_clock import TZ

from data.plugins.mengyu_media.core import photo_activity
from data.plugins.mengyu_contact.current_state import photo_activity as current_photo_activity
from data.plugins.mengyu_life.core.life_schedule import normalise_daylife_plan
from data.plugins.mengyu_life.core.meal_preferences import infer_meal_from_activity


class PhotoIntentMismatch(ValueError):
    """A proposed photo does not match the current conversational intent."""


class PhotoSceneMismatch(ValueError):
    """The image exists but must be withheld after scene verification."""


class PhotoReviewPending(ValueError):
    """A saved image is waiting for a retry of its visual review."""

    def __init__(self, record):
        super().__init__("Visual review deferred for saved image")
        self.record = record


def safe_minutes(value, default):
    """Convert a model-provided duration into a whole number of minutes.

    Args:
        value: Raw value from the plan (int, str, None, bool, ...).
        default: Fallback when the value cannot be read safely.

    Returns:
        An integer, falling back to default for empty or malformed input.
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or "").strip()
    if not text:
        return default
    if text.isdigit():
        return int(text)
    return default



KINDS = (
    "reading",
    "games",
    "music",
    "writing",
    "diary",
    "drawing",
    "memes",
    "collect",
    "search",
    "web",
    "study",
    "project",
    "bilibili",
    "image",
    "daylife",
)


class ActivityStore:
    def __init__(self, root):
        self.searcher = None
        self.image_generator = None
        self.image_reviewer = None
        self.image_intent_reviewer = None
        self.activity_scheduler = None
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "activities.json"
        self.data = {
            "history": [],
            "followups": [],
            "book_offset": 0,
            "favorites": [],
            "learning": {},
            "projects": [],
            "virtual_life": [],
        }
        if self.path.exists():
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("Invalid activities store")
            self.data.update(loaded)

    def cancel(self, activity_id, now=None, reason="cancelled"):
        """Cancel a daylife activity through the owning lifecycle store."""
        now = time.time() if now is None else float(now)
        target = next((item for item in self.data.get("virtual_life", [])
                       if isinstance(item, dict) and str(item.get("id") or "") == str(activity_id)), None)
        if not target or target.get("cancelled_at") is not None:
            return False
        self.sync_lifecycle(now)
        target["cancelled_at"] = now
        target["activity_status"] = "cancelled"
        target["status_updated_at"] = now
        target["cancel_reason"] = str(reason or "cancelled")[:100]
        self.save()
        return True

    def pause(self, activity_id, now=None, reason="scheduler_preemption"):
        """The sole public writer for pausing an existing daylife activity."""
        now = time.time() if now is None else float(now)
        target = next((item for item in self.data.get("virtual_life", [])
                       if isinstance(item, dict) and str(item.get("id") or "") == str(activity_id)), None)
        if not target or target.get("cancelled_at") is not None:
            return False
        self.sync_lifecycle(now)
        target["cancelled_at"] = now
        target["paused_by_scheduler"] = True
        target["activity_status"] = "paused"
        target["paused_at"] = now
        target["status_updated_at"] = now
        target["pause_reason"] = str(reason or "paused")[:100]
        self.save()
        return True

    def set_night_wake(self, patch, now=None):
        """Persist night-wake job state through the activity-independent sleep service."""
        # Kept here only as a compatibility shim; sleep state is owned by sleep_state.py.
        raise RuntimeError("Night-wake state belongs to SleepService")

    def adjust_phase_end(self, activity_id, end_at):
        """Adjust a known planned phase end without exposing raw list mutation."""
        target = next((item for item in self.data.get("virtual_life", [])
                       if isinstance(item, dict) and str(item.get("id") or "") == str(activity_id)), None)
        if not target:
            return False
        phases = target.get("phases") or []
        if not phases or not isinstance(end_at, (int, float)):
            return False
        phases[-1]["at"] = float(end_at)
        self.save()
        return True

    def replace_virtual_life(self, segments):
        """Replace the daylife collection through its owning store."""
        if not isinstance(segments, list):
            raise TypeError("segments must be a list")
        self.data["virtual_life"] = segments[-20:]
        self.save()
        return True

    def update_segment(self, activity_id, updates, now=None):
        """Apply a bounded update to a known activity through ActivityStore."""
        if not isinstance(updates, dict):
            return False
        allowed = {"cancelled_at", "activity_status", "paused_at", "status_updated_at",
                   "completed_at", "started_at", "photo_state", "photo_roll",
                   "return_rolled", "return_note"}
        target = next((item for item in self.data.get("virtual_life", [])
                       if isinstance(item, dict) and str(item.get("id") or "") == str(activity_id)), None)
        if target is None:
            return False
        for key, value in updates.items():
            if key in allowed:
                target[key] = value
        self.save()
        return True

    def save(self):
        write_json(self.path, self.data, "ActivityStore")

    def last_kind_at(self, kind):
        """Return when an activity of this kind was last recorded.

        Args:
            kind: Activity kind such as diary, daylife or web.

        Returns:
            Unix timestamp of the latest matching record, or 0 when none exists.
        """
        times = [
            item.get("at", 0)
            for item in self.data["history"]
            if item.get("kind") == kind
        ]
        return max(times) if times else 0

    def last_life_end(self):
        """Return when the most recent fictional daily-life phase was reached.

        Returns:
            Unix timestamp of the last recorded phase, or 0 when none exists.
        """
        segments = self.data["virtual_life"]
        if not segments:
            return 0
        phases = segments[-1].get("phases") or []
        return phases[-1].get("at", 0) if phases else 0

    @staticmethod
    def _phase_progress_id(segment_id, phase):
        """Return a stable identity for one reached timeline phase."""
        at = phase.get("at", 0)
        try:
            at_key = str(int(float(at) * 1000))
        except (TypeError, ValueError, OverflowError):
            at_key = "unknown"
        stage = str(phase.get("stage") or "").strip()[:60]
        stop_index = phase.get("stop_index", "")
        return f"phase:{segment_id}:{at_key}:{stage}:{stop_index}"

    @staticmethod
    def _lifecycle_snapshot(segment, now):
        """Derive lifecycle state from reached phases without mutating storage.

        Scheduler task state describes queue ownership. This state describes
        whether the fictional activity itself is merely planned, underway, or
        finished. The two intentionally remain separate.
        """
        phases = [
            phase for phase in (segment.get("phases") or [])
            if isinstance(phase, dict) and isinstance(phase.get("at"), (int, float))
        ]
        cancelled_at = segment.get("cancelled_at")
        is_cancelled = isinstance(cancelled_at, (int, float)) and cancelled_at <= now
        cancelled_status = "paused" if segment.get("paused_by_scheduler") else "cancelled"
        if not phases:
            return (cancelled_status if is_cancelled else "planned"), None, None, []

        elapsed = [phase for phase in phases if phase["at"] <= now]
        planning_stages = {"准备出门", "打算做", "临时改主意"}
        active_elapsed = [
            phase for phase in elapsed
            if str(phase.get("stage") or "").strip() not in planning_stages
        ]
        completed = phases[-1]["at"] <= now and not is_cancelled
        if is_cancelled:
            status = cancelled_status
        elif completed:
            status = "completed"
        elif active_elapsed:
            status = "in_progress"
        else:
            status = "planned"

        started_at = active_elapsed[0]["at"] if active_elapsed else None
        completed_at = phases[-1]["at"] if completed else None
        progress = []
        segment_id = str(segment.get("id") or "")
        for index, phase in enumerate(elapsed):
            stage = str(phase.get("stage") or "").strip()
            if not stage or stage in planning_stages:
                continue
            progress.append({
                "progress_id": ActivityStore._phase_progress_id(segment_id, phase),
                "kind": "phase",
                "phase_index": next(
                    (i for i, original in enumerate(phases) if original is phase), index
                ),
                "stage": stage[:60],
                "place": str(phase.get("place") or "")[:100],
                "at": float(phase["at"]),
                "source": "timeline_phase",
            })
        return status, started_at, completed_at, progress

    def sync_lifecycle(self, now=None):
        """Persist reached daylife phases and their planned/active/completed state.

        This is deterministic and makes no model call. It is safe to run on
        every activity tick; phase IDs prevent duplicate progress entries.
        """
        now = time.time() if now is None else float(now)
        changed = False
        updates = []
        for segment in self.data.get("virtual_life", []):
            if not isinstance(segment, dict):
                continue
            segment_id = str(segment.get("id") or "")
            if not segment_id:
                continue
            status, started_at, completed_at, reached = self._lifecycle_snapshot(
                segment, min(now, float(segment.get("cancelled_at", now)))
                if isinstance(segment.get("cancelled_at"), (int, float))
                else now,
            )
            old_status = segment.get("activity_status")
            if segment.get("planned_at") is None:
                segment["planned_at"] = segment.get("created_at", now)
                changed = True
            if old_status != status:
                segment["activity_status"] = status
                segment["status_updated_at"] = now
                changed = True
            elif "activity_status" not in segment:
                segment["activity_status"] = status
                segment["status_updated_at"] = now
                changed = True
            if segment.get("activity_chain_id") is None:
                segment["activity_chain_id"] = str(
                    segment.get("scheduler_task_id") or segment_id
                )
                changed = True
            if started_at is not None and segment.get("started_at") is None:
                segment["started_at"] = started_at
                changed = True
            if status == "completed":
                if segment.get("completed_at") != completed_at:
                    segment["completed_at"] = completed_at
                    changed = True
            elif segment.get("completed_at") is not None:
                segment["completed_at"] = None
                changed = True

            existing = segment.get("progress")
            if not isinstance(existing, list):
                existing = []
            known = {
                str(item.get("progress_id"))
                for item in existing if isinstance(item, dict) and item.get("progress_id")
            }
            added = []
            for entry in reached:
                if entry["progress_id"] not in known:
                    added.append(entry)
                    known.add(entry["progress_id"])
            if added:
                segment["progress"] = (existing + added)[-80:]
                segment["last_progress_at"] = max(item["at"] for item in added)
                changed = True
            elif "progress" not in segment:
                segment["progress"] = existing
                changed = True

            for history_record in reversed(self.data.get("history", [])):
                if (
                    not isinstance(history_record, dict)
                    or str(history_record.get("id") or "") != segment_id
                    or history_record.get("kind") != "daylife"
                ):
                    continue
                if history_record.get("activity_status") != status:
                    history_record["activity_status"] = status
                    history_record["status_updated_at"] = now
                    changed = True
                if started_at is not None and history_record.get("started_at") != started_at:
                    history_record["started_at"] = started_at
                    changed = True
                if history_record.get("completed_at") != completed_at:
                    history_record["completed_at"] = completed_at
                    changed = True
                break

            if old_status != status or added:
                updates.append({
                    "activity_id": segment_id,
                    "activity_status": status,
                    "started_at": segment.get("started_at"),
                    "completed_at": segment.get("completed_at"),
                    "new_progress": added,
                })
        if changed:
            self.save()
        return updates

    def record_progress(self, activity_id, value, now=None):
        """Record a concrete new step for the currently running daylife activity.

        A reflection or mood alone is not progress. The activity must already
        be in progress, and the entry is idempotent for the same activity,
        stage and detail.
        """
        now = time.time() if now is None else float(now)
        if not isinstance(value, dict):
            return None
        stage = str(value.get("stage") or "").strip()
        detail = str(value.get("detail") or "").strip()
        next_step = str(value.get("next_step") or "").strip()
        if not (1 <= len(stage) <= 60 and 4 <= len(detail) <= 300 and len(next_step) <= 160):
            return None
        self.sync_lifecycle(now)
        segment = next(
            (
                item for item in self.data.get("virtual_life", [])
                if isinstance(item, dict) and str(item.get("id") or "") == str(activity_id)
            ),
            None,
        )
        if not segment or segment.get("activity_status") != "in_progress":
            return None
        identity_text = "|".join((
            str(segment.get("activity_chain_id") or segment.get("id") or ""),
            re.sub(r"\s+", "", stage).lower(),
            re.sub(r"\s+", "", detail).lower(),
        ))
        progress_id = "detail:" + hashlib.sha1(identity_text.encode("utf-8")).hexdigest()[:20]
        progress = segment.setdefault("progress", [])
        if any(
            isinstance(item, dict) and item.get("progress_id") == progress_id
            for item in progress
        ):
            return None
        entry = {
            "progress_id": progress_id,
            "kind": "detail",
            "stage": stage,
            "detail": detail,
            "next_step": next_step,
            "at": now,
            "source": "planner_continue",
        }
        segment["progress"] = (progress + [entry])[-80:]
        segment["last_progress_at"] = now
        self.save()
        return entry

    def virtual_context(self, now=None):
        """Return the current phase of a consented fictional outing.

        Args:
            now: UTC timestamp; defaults to the current time.

        Returns:
            Explicitly fictional context or an empty dict when no current story exists.
        """
        now = time.time() if now is None else now
        today = (
            datetime.fromtimestamp(now, TZ).date().isoformat()
        )
        eligible = [
            item
            for item in self.data["virtual_life"]
            if item["created_at"] <= now
            and not (
                item.get("cancelled_at") is not None and item["cancelled_at"] <= now
            )
            and (item["day"] == today or item["phases"][-1]["at"] > now)
        ]
        if not eligible:
            return {}
        latest = eligible[-1]
        elapsed = [phase for phase in latest["phases"] if phase["at"] <= now]
        upcoming = [phase for phase in latest["phases"] if phase["at"] > now]
        lifecycle_status, derived_started_at, derived_completed_at, reached_progress = self._lifecycle_snapshot(
            latest, now
        )
        visible_progress = [
            item for item in (latest.get("progress") or []) if isinstance(item, dict)
        ]
        visible_ids = {
            str(item.get("progress_id"))
            for item in visible_progress if item.get("progress_id")
        }
        visible_progress.extend(
            item for item in reached_progress
            if item.get("progress_id") not in visible_ids
        )
        return {
            "fictional": True,
            "id": latest["id"],
            "day": latest["day"],
            "destination": latest["destination"],
            "reason": latest["reason"],
            "clothes": latest["clothes"],
            "detail": latest.get("detail", "") if not latest.get("daily_anchor_id") or lifecycle_status == "completed" else "",
            "game_title": latest.get("game_title", ""),
            "game_genre": latest.get("game_genre", ""),
            "mode": latest.get("mode", "out"),
            "activity_status": lifecycle_status,
            "activity_chain_id": latest.get("activity_chain_id") or latest.get("scheduler_task_id") or latest.get("id"),
            "daily_anchor_id": latest.get("daily_anchor_id"),
            "started_at": latest.get("started_at") or derived_started_at,
            "completed_at": latest.get("completed_at") or derived_completed_at,
            "progress": visible_progress[-12:],
            "itinerary": latest.get("itinerary", []),
            "current": elapsed[-1],
            "next": upcoming[0] if upcoming else None,
            "completed": lifecycle_status == "completed",
            "as_of": now,
            "elapsed_seconds": max(0, int(now - latest["created_at"])),
            "phase_elapsed_seconds": max(0, int(now - elapsed[-1]["at"])),
            "seconds_until_next_phase": max(0, int(upcoming[0]["at"] - now))
            if upcoming
            else 0,
            "earliest_complete_at": latest["phases"][-1]["at"],
            "seconds_until_complete": max(0, int(latest["phases"][-1]["at"] - now)),
            "origin": "用户同意的虚构日常，不是现实身体出行，也不是用户经历",
        }

    def live_photo_binding(self, now=None):
        now = time.time() if now is None else now
        scheduler = self.activity_scheduler.data if self.activity_scheduler else {}
        return current_photo_activity(self.data.get("virtual_life", []), scheduler, now)

    def live_photo_ok(self, record, now=None, *, allow_archived=False):
        """Check the photo against the current activity or its verified capture."""
        now = time.time() if now is None else now
        return photo_activity.can_deliver(
            record, self.live_photo_binding(now), archived_at=now if allow_archived else None,
        )

    async def bind_photo(self, plan, prompt):
        place, dish, meal_state = photo_activity.requested_scene(plan, prompt)
        binding = self.live_photo_binding()
        expected_binding = plan.get("_expected_photo_binding")
        if expected_binding is not None and binding != expected_binding:
            raise PhotoIntentMismatch("照片原先对应的活动阶段已经变了")
        expected = plan.get("_expected_activity_id")
        if expected and (not binding or binding["activity_id"] != expected):
            raise PhotoIntentMismatch("照片原先对应的活动已经变了")
        if not binding:
            now = time.time()
            for item in self.data.get("virtual_life", []):
                if self._lifecycle_snapshot(item, now)[0] in ("planned", "in_progress"):
                    raise PhotoIntentMismatch("已有待开始或无法唯一确认的活动")
            previous = self.virtual_context(now) or {}
            last_place = (previous.get("current") or {}).get("place")
            if not place and previous.get("mode") == "home" and not dish:
                last_place = "住处"
            await self.perform(photo_activity.seed_plan(place, dish, meal_state, last_place, now))
            if self.activity_scheduler:
                self.activity_scheduler.reconcile_life(self.data["virtual_life"], time.time())
            binding = self.live_photo_binding()
        if not binding or photo_activity.conflicts(binding, place, dish, meal_state):
            raise PhotoIntentMismatch("照片地点、正在做的事或菜品与当前活动不一致")
        return binding

    async def review_saved_photo(self, record):
        """Review the existing PNG using its original capture evidence.

        Args:
            record: Persisted image record with pending review inputs.

        Returns:
            The recorded review status.
        """
        job = record["photo_review_job"]
        now = time.time()
        path = Path(record["artifact"]).resolve()
        binding = record.get("photo_activity")
        archived = record.get("sharing_origin") == photo_activity.LIFE_PHOTO_ORIGIN
        reason = ""
        if now >= job["expires_at"]:
            reason = "expired"
        elif not path.is_relative_to((self.root / "creations").resolve()) or path.suffix.lower() != ".png" or not path.is_file():
            reason = "missing_png"
        elif binding and not archived and binding != self.live_photo_binding(now):
            reason = "activity_changed"
        if not reason:
            job["attempts"] += 1
            job["retry_at"] = now + min(60 * (2 ** min(job["attempts"] - 1, 4)), 900)
            self.save()
            try:
                review = await self.image_reviewer(str(path), job["prompt"], job.get("scene"))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep unverifiable photos blocked
                cause = exc
                for _ in range(5):
                    status = getattr(cause, "status_code", None) or getattr(getattr(cause, "response", None), "status_code", None)
                    if isinstance(cause, (TimeoutError, httpx.TransportError)) or (
                        isinstance(status, int) and 500 <= status < 600
                    ):
                        job["reason"] = f"http_{status}" if status else type(cause).__name__
                        self.save()
                        return "pending"
                    cause = cause.__cause__ or cause.__context__
                    if cause is None:
                        break
                reason = type(exc).__name__
            else:
                record["image_review"] = review
                if binding and not archived and binding != self.live_photo_binding():
                    reason = "activity_changed"
                elif binding and not photo_activity.verified_review(record.get("photo_review_binding") or binding, review):
                    reason = "scene_mismatch"
                elif not binding and (not isinstance(review, dict) or review.get("matches_scene") is False):
                    reason = "scene_mismatch"
                else:
                    job["status"] = "approved"
                    record.pop("delivery_blocked", None)
                    if isinstance(review, dict) and review.get("description"):
                        record["material"] = "实际成图核对：" + str(review["description"]) + "\n绘图要求（不等同成图证据）：" + job["prompt"]
        if reason:
            job["status"] = "rejected"
            job["reason"] = reason
            record["material"] = "图片未通过当前活动与画面核对，已拦截。"
        job.pop("prompt", None)
        job.pop("scene", None)
        self.save()
        return job["status"]

    async def perform(self, plan, client=None, catalog=None):
        """Execute one bounded activity or save a planned daylife timeline.

        Args:
            plan: Model-selected kind, optional query/title/text/theme.
            client: Async HTTP client for the fixed public sources.
            catalog: Current installed meme catalog, without modifying its files.

        Returns:
            Completed record with material, sources and any saved artifact.

        Raises:
            ValueError: Kind or material is invalid or unavailable.
            httpx.HTTPError: A source could not be fetched successfully.
        """
        kind = plan.get("kind")
        if kind == "writing":
            kind = "diary"
        if kind not in KINDS:
            raise ValueError("Unsupported activity")
        now = time.time()
        record = {"id": uuid4().hex, "kind": kind, "at": now, "sources": []}
        artifacts = self.root / "creations"
        artifacts.mkdir(exist_ok=True)
        if kind == "daylife":
            plan = normalise_daylife_plan(plan, now)
            current = self.virtual_context(now)
            changing = bool(
                current and not current["completed"] and plan.get("change_plan") is True
            )
            if current and not current["completed"] and not changing:
                raise ValueError("A fictional outing is already in progress")
            mode = str(plan.get("mode") or "out")
            destination = str(
                plan.get("destination")
                or plan.get("query")
                or ("住处" if mode == "home" else "")
            ).strip()
            reason = str(plan.get("reason") or "想做点自己喜欢的事").strip()
            travel = 0 if mode == "home" else safe_minutes(plan.get("travel_minutes"), 15)
            stay = safe_minutes(plan.get("stay_minutes"), 45)
            preparation = safe_minutes(plan.get("prep_minutes"), 5)
            if not 1 <= len(destination) <= 80:
                destination = "住处"
            if mode not in ("home", "out"):
                mode = "home"
            preparation = max(0, min(60, preparation))
            departure = now + preparation * 60
            itinerary = plan.get("itinerary") if mode == "out" and isinstance(plan.get("itinerary"), list) else []
            if mode == "out" and not itinerary:
                itinerary = [{
                    "destination": destination,
                    "reason": reason,
                    "category": plan.get("_activity_profile", "outing"),
                    "stay_minutes": stay,
                    "travel_minutes": travel,
                }]
            phases = [{"at": now, "stage": "准备出门", "place": "住处"}]
            if mode == "out" and plan.get("_photo_on_site"):
                stop = itinerary[0]
                phases = [
                    {"at": now, "stage": "到了", "place": destination,
                     "category": stop.get("category", ""), "reason": reason, "stop_index": 0},
                    {"at": now + stay * 60, "stage": "回来的路上", "place": "回住处的路上"},
                    {"at": now + (stay + 10) * 60, "stage": "回来了", "place": "住处"},
                ]
                travel = 10
            elif mode == "out":
                cursor = departure
                first_destination = str((itinerary[0] if itinerary else {}).get("destination") or destination)[:80]
                phases.append({"at": departure, "stage": "去的路上", "place": "去" + first_destination + "的路上"})
                total_travel = 0
                total_stay = 0
                for index, stop in enumerate(itinerary[:4]):
                    stop_destination = str(stop.get("destination") or destination)[:80]
                    stop_reason = str(stop.get("reason") or reason)[:200]
                    stop_travel = max(5, min(60, safe_minutes(stop.get("travel_minutes"), 10)))
                    stop_stay = max(10, min(180, safe_minutes(stop.get("stay_minutes"), 60)))
                    arrival = cursor + stop_travel * 60
                    phases.append({
                        "at": arrival,
                        "stage": "到了",
                        "place": stop_destination,
                        "category": str(stop.get("category") or "explore"),
                        "reason": stop_reason,
                        "stop_index": index,
                    })
                    cursor = arrival + stop_stay * 60
                    total_travel += stop_travel
                    total_stay += stop_stay
                    if index < min(len(itinerary), 4) - 1:
                        next_destination = str(itinerary[index + 1].get("destination") or "下一站")[:80]
                        phases.append({
                            "at": cursor,
                            "stage": "前往下一站",
                            "place": "去" + next_destination + "的路上",
                            "next_place": next_destination,
                        })
                return_travel = max(5, min(60, safe_minutes(itinerary[min(len(itinerary), 4) - 1].get("travel_minutes"), 10)))
                phases.append({"at": cursor, "stage": "回来的路上", "place": "回住处的路上"})
                phases.append({"at": cursor + return_travel * 60, "stage": "回来了", "place": "住处"})
                travel = total_travel + return_travel
                stay = total_stay
                destination = str(itinerary[0].get("destination") or destination)[:80]
            else:
                stay = min(2880, max(stay, 30 if re.search(r"做饭|煮|收拾|打扫|洗", reason) else 20))
                phases = [
                    {"at": now, "stage": "打算做", "place": destination},
                    {"at": departure, "stage": "正在做", "place": destination},
                    {"at": departure + stay * 60, "stage": "做完了", "place": destination},
                ]
            meal = plan.get("meal") if isinstance(plan.get("meal"), dict) else infer_meal_from_activity(reason, plan.get("detail", ""))
            meal_name = (meal.get("dish") or meal.get("name")) if isinstance(meal, dict) else ""
            if meal_name:
                meal = {
                    "dish": str(meal_name)[:40],
                    "category": str(meal.get("category") or "其他")[:30],
                    "status": "planned",
                }
            else:
                meal = None
            session = {
                "id": record["id"],
                "fictional": True,
                # The scheduler's task status only reserves/owns a time slot.
                # This lifecycle describes the activity itself.
                "activity_status": "planned",
                "planned_at": now,
                "started_at": None,
                "completed_at": None,
                "status_updated_at": now,
                "activity_chain_id": str(
                    plan.get("_activity_chain_id")
                    or plan.get("_scheduler_task_id")
                    or record["id"]
                )[:120],
                "resumed_from_activity_id": str(
                    plan.get("_resume_from_activity_id") or ""
                )[:120] or None,
                "progress": [
                    item for item in (plan.get("_resume_progress") or [])
                    if isinstance(item, dict)
                ][-80:],
                "activity_profile": plan.get("_activity_profile", "general"),
                "planned_duration_minutes": stay,
                "itinerary": itinerary if mode == "out" else [],
                "prep_minutes": preparation,
                "travel_minutes": travel,
                "meal": meal,
                "created_at": now,
                "day": datetime.fromtimestamp(now, TZ)
                .date()
                .isoformat(),
                "destination": destination,
                "mode": mode,
                "reason": reason[:200],
                "detail": str(plan.get("detail") or "")[:300],
                "game_title": str(plan.get("game_title") or "")[:80],
                "game_genre": str(plan.get("game_genre") or "")[:50],
                "clothes": str(plan.get("clothes") or "日常便装")[:160],
                "scheduler_task_id": str(plan.get("_scheduler_task_id") or "")[:100],
                **({"daily_anchor_id": str(plan["_daily_anchor_id"])[:100]} if plan.get("_daily_anchor_id") else {}),
                "work": plan.get("work") if isinstance(plan.get("work"), dict) else None,
                "phases": phases,
            }
            if changing:
                # Capture every phase that has actually elapsed before the
                # old segment is closed or paused.
                self.sync_lifecycle(now)
                session["phases"][0].update(
                    stage="临时改主意", place=current["current"]["place"]
                )
                session["replaces"] = current["id"]
            target = artifacts / f"{record['id']}-daylife.json"
            with target.open("x", encoding="utf-8") as handle:
                json.dump(session, handle, ensure_ascii=False, indent=2)
            if changing:
                for previous in self.data["virtual_life"]:
                    if previous["id"] == current["id"]:
                        previous["cancelled_at"] = now
                        if str(plan.get("_preempted_activity_id") or "") == str(previous.get("id")):
                            previous["paused_by_scheduler"] = True
                            previous["activity_status"] = "paused"
                            previous["paused_at"] = now
                        else:
                            previous["activity_status"] = "cancelled"
                        previous["status_updated_at"] = now
            self.data["virtual_life"] = (self.data["virtual_life"] + [session])[-20:]
            self.sync_lifecycle(now)
            artifact_temp = target.with_suffix(".tmp")
            artifact_temp.write_text(
                json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            artifact_temp.replace(target)
            record.update(
                fictional=True,
                plan_saved_only=True,
                activity_status=session.get("activity_status", "planned"),
                activity_id=record["id"],
                title="虚构日常：" + destination,
                material="用户已同意的虚构生活时间线，只能按当前阶段自然分享，不能声称现实出行；不把未来阶段说成已经发生。\n"
                + json.dumps(session, ensure_ascii=False),
                artifact=str(target),
            )
        elif kind == "image":
            if self.image_generator is None:
                raise RuntimeError("Mengyu media is unavailable")
            prompt = str(
                plan.get("visual_prompt") or plan.get("query") or plan.get("text") or ""
            ).strip()
            from data.plugins.mengyu_contact.image_intent import image_mode
            mode = image_mode(plan)
            live_photo = mode == "live"
            live_constraints = live_photo and plan.get("subject") != "user"
            record["image_mode"] = mode
            subject = str(plan.get("subject") or ("other" if not live_photo else ""))
            if live_constraints and subject == "other":
                subject = "scene"
            if not subject and re.search(
                r"梦雨|自拍|selfie|mengyu|人像|写真|成年女性|woman",
                prompt + " " + str(plan.get("title") or ""),
                re.IGNORECASE,
            ):
                subject = "mengyu"
            if subject == "mengyu" and not re.search(
                r"梦雨|自拍|照片|写实|摄影|镜头|人物|人像|写真|女性|穿|脸|portrait|selfie|woman|photo",
                prompt,
                re.IGNORECASE,
            ):
                raise ValueError(
                    "A selfie needs a visual description, not a chat caption"
                )
            record["requested_visual_prompt"] = prompt
            binding = await self.bind_photo(plan, prompt) if live_constraints else None
            if binding:
                subject = subject or "scene"
                record["photo_activity"] = binding
                record["activity_id"] = binding["activity_id"]
                if plan.get("_expected_photo_binding") is not None:
                    record["sharing_origin"] = photo_activity.LIFE_PHOTO_ORIGIN
                    record["share_reason"] = str(plan.get("_share_reason") or "")[:200]
            life_photo = record.get("sharing_origin") == photo_activity.LIFE_PHOTO_ORIGIN
            if (
                self.image_intent_reviewer
                and live_photo
                and subject in ("mengyu", "scene")
                and not life_photo
            ):
                decision = await self.image_intent_reviewer(
                    prompt, subject, mode, str(plan.get("owner_request") or "")
                )
                if decision is not None:
                    if (
                        not isinstance(decision, dict)
                        or decision.get("allow") is not True
                    ):
                        reason = (
                            decision.get("reason", "")
                            if isinstance(decision, dict)
                            else ""
                        )
                        raise PhotoIntentMismatch(
                            "Photo not submitted: " + str(reason)[:400]
                        )
                    record["shopping_photo"] = decision
            record["caption"] = str(plan.get("caption") or "")[:300]
            scene = None
            review_binding = binding
            if binding:
                if binding != self.live_photo_binding():
                    raise PhotoIntentMismatch("开始生成前，当前活动已经变了")
                # An incidental subject can be framed away from the meal itself.
                if life_photo and subject == "scene" and binding["dish"] and binding["dish"] not in prompt:
                    review_binding = {**binding, "dish": "", "meal_state": ""}
                    record["photo_review_binding"] = review_binding
                scene = {
                    "mode": binding["mode"], "current": {"place": binding["place"]},
                    "photo_activity": review_binding,
                    "incidental_subject": life_photo and subject == "scene",
                }
                record["photo_scene"] = scene
                prompt += (
                    "\n【同一条已经开始的虚构活动】"
                    + json.dumps(review_binding, ensure_ascii=False)
                    + "。以这条活动为画面的硬约束。地点、阶段、行为和菜名以活动记录为准。"
                    "菜名非空时，必须清楚可见指定菜品及其关键特征，不能用另一道菜或只拍泛泛的餐馆。"
                    "home 表示室内。正在路上就按当前路途取景，不能提前到目的地。"
                    "根据当前活动安排自然构图，不添加无关同伴或地标。"
                )
                if scene["incidental_subject"]:
                    prompt += "物件或风景是照片主体，可以只取局部，不要求梦雨出镜。"

            record["visual_prompt"] = prompt
            environment = None
            if live_constraints:
                from data.plugins.mengyu_media.core.live_photo import photo_context
                environment = await photo_context()
                record["photo_environment"] = environment
            if binding is not None and binding != self.live_photo_binding():
                raise PhotoIntentMismatch("开始生成前，当前活动已经变了")
            record["subject"] = subject
            if subject == "user":
                raise ValueError("The portfolio edition only generates fictional characters and scenes")
            if binding and binding != self.live_photo_binding():
                raise PhotoIntentMismatch("提交生图前，当前活动已经变了")
            generated = await self.image_generator(
                client,
                prompt,
                artifacts / f"{record['id']}-image.png",
                environment=environment,
            )
            record.update(generated)
            review_kind = "artwork"
            if live_photo:
                review_kind = "selfie" if subject == "mengyu" else "scene"
            record["review_kind"] = review_kind
            record.update(
                title=str(plan.get("title") or "新画")[:100],
                media_origin="ai_generated",
                material="实际调用gpt-image-2生成并保存的图片。画面是AI创作，不是现实拍摄或现实经历的证明。\n绘图描述："
                + prompt,
            )
            if binding or (self.image_reviewer and review_kind == "selfie"):
                record["delivery_blocked"] = True
                record["photo_review_job"] = {
                    "status": "pending",
                    "attempts": 0,
                    "retry_at": time.time(),
                    "expires_at": time.time() + (photo_activity.LIFE_PHOTO_MAX_AGE if life_photo else 900),
                    "prompt": prompt,
                    "scene": {**(scene or {}), "shopping_photo": record["shopping_photo"]}
                    if record.get("shopping_photo") else scene,
                    "promise_id": plan.get("_review_promise_id"),
                    "request_user_ts": plan.get("_review_request_user_ts"),
                    "permit_event_id": plan.get("_review_permit_event_id"),
                    "own_photo_key": plan.get("_review_own_photo_key"),
                }
                self.data["history"] = (self.data["history"] + [record])[-60:]
                self.save()
                if not self.image_reviewer:
                    raise PhotoReviewPending(record)
                status = await self.review_saved_photo(record)
                if status == "pending":
                    raise PhotoReviewPending(record)
                if status == "rejected":
                    raise PhotoSceneMismatch("Photo activity or visual scene did not match")
                return record
        elif kind == "bilibili":
            from data.plugins.mengyu_contact.bilibili import browse_bilibili
            record.update(await browse_bilibili(client, str(plan.get("query") or "")))
            target = artifacts / f"{record['id']}-bilibili.md"
            with target.open("x", encoding="utf-8") as handle:
                handle.write(
                    record["title"]
                    + "\n\n"
                    + record["material"]
                    + "\n\n"
                    + json.dumps(record["sources"], ensure_ascii=False)
                )
            record["artifact"] = str(target)
        elif kind in ("search", "web", "study"):
            from data.plugins.mengyu_life.core.exploration import read_public, search_public
            query = str(plan.get("query") or "").strip()
            if kind == "web":
                page = await read_public(client, query)
                record.update(
                    title=page["title"],
                    material=page["text"] + "\n参考链接：\n" + "\n".join(page["links"]),
                )
                record["sources"] = [{"title": page["title"], "url": page["url"]}]
            else:
                source_url = str(plan.get("url") or "").strip()
                results = (
                    [{"title": str(plan.get("title") or query), "url": source_url}]
                    if kind == "study" and source_url
                    else await (self.searcher or search_public)(client, query)
                )
                if kind == "search":
                    record.update(
                        title="搜索：" + query,
                        material="仅搜索结果摘要，不等于读完原文。\n"
                        + json.dumps(results, ensure_ascii=False),
                        sources=results,
                    )
                else:
                    pages = []
                    for item in results[:3]:
                        try:
                            page = await read_public(client, item["url"])
                            if len(page["text"]) >= 80:
                                pages.append(page)
                        except Exception as exc:  # noqa: BLE001 - try another source
                            logging.getLogger(__name__).warning(
                                "Study source unavailable: %s", type(exc).__name__
                            )
                            continue
                        if len(pages) >= 2:
                            break
                    if not pages:
                        raise ValueError("No study source could actually be read")
                    record.update(
                        title=str(plan.get("title") or query)[:100],
                        query=query,
                        material="\n\n".join(
                            page["title"] + "\n" + page["text"][:5000] for page in pages
                        ),
                        sources=[
                            {"title": page["title"], "url": page["url"]}
                            for page in pages
                        ],
                    )
            target = artifacts / f"{record['id']}-{kind}.md"
            with target.open("x", encoding="utf-8") as handle:
                handle.write(
                    record["title"]
                    + "\n\n"
                    + record["material"]
                    + "\n\n"
                    + json.dumps(record["sources"], ensure_ascii=False, indent=2)
                )
            record["artifact"] = str(target)
        elif kind == "project":
            text = str(plan.get("text") or "").strip()
            if not 1 <= len(text) <= 30000:
                raise ValueError("Project needs actual content")
            extension = str(plan.get("format") or "md").lower()
            if extension not in ("md", "txt", "html", "svg", "py", "js", "css", "json"):
                raise ValueError("Unsupported project document format")
            title = str(plan.get("title") or "小项目")[:100]
            target = artifacts / f"{record['id']}-project.{extension}"
            with target.open("x", encoding="utf-8") as handle:
                handle.write(text)
            record.update(
                title=title, material=text, artifact=str(target), format=extension
            )
            self.data["projects"].append(
                {
                    "id": record["id"],
                    "title": title,
                    "at": now,
                    "artifact": str(target),
                    "format": extension,
                }
            )
        elif kind == "reading":
            url = "https://www.gutenberg.org/cache/epub/24225/pg24225.txt"
            response = await client.get(url)
            response.raise_for_status()
            text = response.text[:2_000_000]
            match = re.search(
                r"\*\*\* START OF THE PROJECT GUTENBERG EBOOK (.*?) \*\*\*", text
            )
            if not match:
                raise ValueError("Missing book body")
            title = match.group(1)
            body = (
                text[match.end() :]
                .split("*** END OF THE PROJECT GUTENBERG EBOOK", 1)[0]
                .strip()
            )
            start = int(self.data.get("book_offset", 0))
            if start >= len(body):
                start = 0
            excerpt = body[start : start + 2400]
            if not excerpt:
                raise ValueError("Empty book excerpt")
            record.update(title=title, start=start, material=excerpt)
            record["sources"] = [{"title": title, "url": url}]
            self.data["book_offset"] = start + len(excerpt)
        elif kind == "games":
            from data.plugins.mengyu_life.core.game_life import discover_games

            query = str(
                plan.get("query")
                or plan.get("title")
                or (
                    str(datetime.fromtimestamp(now, TZ).year)
                    + "年近期值得关注的PC独立游戏新作、玩法特色与玩家评价"
                )
            ).strip()[:300]
            discovery = await discover_games(client, self.searcher, query)
            record.update(discovery)
        elif kind == "music":
            response = await client.get(
                "https://itunes.apple.com/search",
                params={
                    "term": str(plan.get("query") or "陈奕迅")[:80],
                    "entity": "song",
                    "limit": 5,
                    "country": "HK",
                },
            )
            response.raise_for_status()
            songs = [
                {
                    "歌名": str(x.get("trackName", ""))[:150],
                    "作者": str(x.get("artistName", ""))[:150],
                    "专辑": str(x.get("collectionName", ""))[:150],
                    "链接": x.get("trackViewUrl", ""),
                }
                for x in response.json().get("results", [])
                if x.get("trackName")
            ]
            if not songs:
                raise ValueError("No song metadata")
            record.update(
                title="曲目资料",
                material="仅查询曲名、作者、专辑元数据，未播放或听过音频，不能评价旋律、唱腔或编造歌词。\n"
                + json.dumps(songs, ensure_ascii=False),
            )
            record["sources"] = [{"title": x["歌名"], "url": x["链接"]} for x in songs]
        elif kind == "diary":
            text = str(plan.get("text") or "").strip()
            if not 1 <= len(text) <= 3000:
                raise ValueError("Missing or oversized diary entry")
            date = datetime.fromtimestamp(now, TZ).strftime(
                "%Y-%m-%d %H:%M"
            )
            title = str(plan.get("title") or date + "的日记")[:80]
            target = artifacts / f"{record['id']}-diary.md"
            with target.open("x", encoding="utf-8") as handle:
                handle.write(title + "\n" + date + "\n梦雨的私人日记\n\n" + text + "\n")
            record.update(
                title=title,
                material=text,
                artifact=str(target),
                visibility="private",
                diary_date=date,
                wish=str(plan.get("wish") or "")[:240],
                sharing="self_decides",
            )
        elif kind == "drawing":
            from PIL import Image, ImageDraw

            image = Image.new("RGB", (640, 640), "#f9f4e9")
            draw = ImageDraw.Draw(image)
            theme = plan.get("theme", "stars")
            theme = theme if theme in ("stars", "clouds", "flower") else "stars"
            rng = random.Random(record["id"])
            if theme == "stars":
                draw.ellipse((240, 150, 450, 360), fill="#f2c867")
                draw.ellipse((290, 125, 470, 310), fill="#f9f4e9")
                for _ in range(14):
                    x, y = rng.randint(70, 570), rng.randint(65, 530)
                    radius = rng.randint(8, 20)
                    points = [
                        (
                            x
                            + math.cos(i * math.pi / 5 - math.pi / 2)
                            * radius
                            * (1 if i % 2 == 0 else 0.43),
                            y
                            + math.sin(i * math.pi / 5 - math.pi / 2)
                            * radius
                            * (1 if i % 2 == 0 else 0.43),
                        )
                        for i in range(10)
                    ]
                    draw.polygon(points, fill="#a4c9bc")
            elif theme == "clouds":
                for x, y in ((130, 220), (370, 370), (340, 150)):
                    draw.rounded_rectangle(
                        (x - 70, y, x + 115, y + 70), radius=35, fill="#bdd8d3"
                    )
                    draw.ellipse((x - 25, y - 45, x + 65, y + 45), fill="#bdd8d3")
            else:
                draw.line((320, 320, 320, 530), fill="#739f86", width=9)
                draw.ellipse((320, 400, 425, 450), fill="#a4c9bc")
                for i in range(6):
                    x = 320 + math.cos(i * math.pi / 3) * 65
                    y = 280 + math.sin(i * math.pi / 3) * 65
                    draw.ellipse((x - 45, y - 45, x + 45, y + 45), fill="#e8b6ba")
                draw.ellipse((285, 245, 355, 315), fill="#f2cf7a")
            target = artifacts / f"{record['id']}-{theme}.png"
            image.save(target)
            record.update(
                title={"stars": "月亮与星星", "clouds": "几朵云", "flower": "一朵小花"}[
                    theme
                ],
                material="实际完成并保存的程序简笔画；可以说画了张小涂鸦，不说使用了完整AI绘画模型。",
                artifact=str(target),
            )
        elif kind == "collect":
            source = next(
                (
                    item
                    for item in self.data["history"]
                    if item["id"] == plan.get("source_id")
                    and item["kind"] not in ("collect", "memes")
                ),
                None,
            )
            if source is None:
                raise ValueError("Collection source must be real previous work")
            if any(
                item["source_id"] == source["id"] for item in self.data["favorites"]
            ):
                raise ValueError("Already collected this source")
            note = str(plan.get("note") or "想留着以后再看")[:300]
            favorite = {
                "source_id": source["id"],
                "title": source["title"],
                "note": note,
                "at": now,
                "sources": source["sources"],
            }
            self.data["favorites"] = (self.data["favorites"] + [favorite])[-100:]
            target = self.root / "favorites.json"
            target.write_text(
                json.dumps(self.data["favorites"], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            record.update(
                title="收藏：" + source["title"],
                material=json.dumps(
                    {"内容": source["material"][:2000], "自己的理由": note},
                    ensure_ascii=False,
                ),
                sources=source["sources"],
                artifact=str(target),
            )
            if source.get("visibility") == "private":
                record["visibility"] = "private"
        else:
            if not catalog:
                raise ValueError("No installed memes")
            target = self.root / "meme_index.json"
            target.write_text(
                json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            record.update(
                title="整理已安装表情包",
                material=json.dumps(
                    {
                        key: {"含义": value["description"], "张数": len(value["files"])}
                        for key, value in catalog.items()
                    },
                    ensure_ascii=False,
                ),
                artifact=str(target),
            )
        self.data["history"] = (self.data["history"] + [record])[-60:]
        self.save()
        return record

    def record_learning(self, record, note, next_question):
        """Persist learning notes against an actually completed study session.

        Args:
            record: Completed study record from this store.
            note: Generated reflection grounded in that record.
            next_question: Optional next question for a later session.

        Raises:
            ValueError: The activity is not a completed, sourced study session.
        """
        if (
            record.get("kind") != "study"
            or not record.get("sources")
            or not any(item["id"] == record.get("id") for item in self.data["history"])
        ):
            raise ValueError("Learning progress requires actual study sources")
        title = record["title"]
        progress = self.data["learning"].setdefault(
            title, {"title": title, "notes": [], "sessions": 0}
        )
        progress.update(
            last_query=record.get("query", ""),
            updated_at=time.time(),
            next_question=str(next_question or "")[:200],
            thought=str(note.get("thought") or "")[:1000],
            sources=record["sources"],
        )
        folder = self.root / "learning"
        folder.mkdir(exist_ok=True)
        target = folder / f"{record['id']}-notes.md"
        with target.open("x", encoding="utf-8") as handle:
            handle.write(
                title
                + "\n\n"
                + progress["thought"]
                + "\n\n下一问题："
                + progress["next_question"]
                + "\n\n来源：\n"
                + json.dumps(record["sources"], ensure_ascii=False, indent=2)
            )
        progress["notes"].append(str(target))
        progress["sessions"] += 1
        learned = note.get("learning_complete", True) is True
        progress["status"] = "learned" if learned else "needs_source"
        progress["successful_sessions"] = progress.get("successful_sessions", 0) + int(
            learned
        )
        self.save()
        target = self.root / "learning.json"
        temp = target.with_suffix(".tmp")
        temp.write_text(
            json.dumps(self.data["learning"], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp.replace(target)

    def record_computer(self, result):
        """Save a completed visual inspection only with existing, hashed frame evidence.

        Args:
            result: Browser-generated source, summary and frame metadata.

        Returns:
            Persisted computer activity record.

        Raises:
            ValueError: Actual visual evidence or source is missing.
        """
        frames = result.get("frames") or []
        if (
            len(frames) < 2
            or not result.get("sources")
            or not result.get("material")
            or result.get("scope") != "video_frames"
        ):
            raise ValueError("Computer activity requires actual frame evidence")
        for frame in frames:
            path = Path(frame["path"]).resolve()
            if (
                not path.is_relative_to(self.root.resolve())
                or not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != frame.get("sha256")
            ):
                raise ValueError("Computer frame evidence is invalid")
        record = {
            **result,
            "id": uuid4().hex,
            "kind": "computer",
            "at": time.time(),
        }
        target = self.root / "creations" / f"{record['id']}-computer.json"
        target.parent.mkdir(exist_ok=True)
        with target.open("x", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, indent=2)
        record["artifact"] = str(target)
        self.data["history"] = (self.data["history"] + [record])[-60:]
        self.save()
        return record

    def add_followup(self, candidate, turns, now):
        """Accept a future concern only with a real, recent, short user quote.

        Args:
            candidate: Proposed quote, topic and Unix due_at.
            turns: Actual timestamped chat turns.
            now: Current Unix timestamp.

        Returns:
            Whether a new evidence-backed concern was saved.
        """
        if not isinstance(candidate, dict):
            return False
        quote = candidate.get("quote", "")
        topic = candidate.get("topic", "")
        due = candidate.get("due_at")
        if (
            not isinstance(quote, str)
            or not 5 <= len(quote) <= 100
            or not isinstance(topic, str)
            or not 1 <= len(topic) <= 60
        ):
            return False
        if not isinstance(due, (int, float)) or not now + 600 <= due <= now + 7 * 86400:
            return False
        evidence = next(
            (
                t
                for t in reversed(turns)
                if t.get("role") == "user"
                and len(t.get("text", "")) <= 200
                and quote in t.get("text", "")
                and 0 <= now - t.get("at", 0) <= 86400
                and not re.search(
                    r"如果|假如|比如|小说|正文|他说|她说|取消|不用|别提醒", t["text"]
                )
            ),
            None,
        )
        if not evidence:
            return False
        identity = hashlib.sha256((str(evidence["at"]) + quote).encode()).hexdigest()[
            :16
        ]
        if any(x["id"] == identity for x in self.data["followups"]):
            return False
        self.data["followups"] = (
            self.data["followups"]
            + [
                {
                    "id": identity,
                    "quote": quote,
                    "topic": topic,
                    "due_at": due,
                    "source_at": evidence["at"],
                    "created_at": now,
                }
            ]
        )[-30:]
        self.save()
        return True

    def due_followups(self, now):
        return [
            x
            for x in self.data["followups"]
            if not x.get("shared_at") and x["due_at"] <= now <= x["due_at"] + 86400
        ]

    def mark_followup_shared(self, identity, now):
        for item in self.data["followups"]:
            if item["id"] == identity:
                item["shared_at"] = now
                self.save()
                return


class ActivityService(ActivityStore):
    """Domain write service for activity lifecycle and daylife records.

    ActivityStore remains as a compatibility name for existing integrations;
    all lifecycle writes are implemented here and exposed through this service.
    """
