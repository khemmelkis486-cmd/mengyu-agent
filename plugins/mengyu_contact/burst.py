"""Merge a burst before the framework acquires its per-session model lock."""

import asyncio
import re
import time


class BurstInbox:
    def __init__(self, quiet=15.0, max_wait=60.0):
        self.quiet = quiet
        self.max_wait = max_wait
        self.sequence = 0
        self.pending = []
        self.events = {}
        self.window_started = None
        self.last_arrival = 0
        self.closed = False
        self.lock = asyncio.Lock()
        self.changed = asyncio.Event()

    def register(self, event):
        """Record arrival before the event waits for shared state."""
        if self.closed:
            event.stop_event()
            return False
        incoming_id = event.get_extra("mengyu_incoming_id")
        if incoming_id and any(
            item.get_extra("mengyu_incoming_id") == incoming_id
            for item in self.events.values()
        ):
            event.stop_event()
            return False
        self.sequence += 1
        sequence = self.sequence
        event.set_extra("mengyu_burst_seq", sequence)
        now = time.monotonic()
        if self.window_started is None:
            self.window_started = now
        self.last_arrival = now
        self.pending.append(
            (sequence, event.message_str, list(event.message_obj.message))
        )
        self.events[sequence] = event
        self.changed.set()
        self.changed = asyncio.Event()
        return True

    async def collect(self, event):
        """Let only the newest event continue with every unanswered fragment.

        Args:
            event: Owner message with mutable text and original components.

        Returns:
            True for the merged event, False for superseded or closed events.
        """
        sequence = event.get_extra("mengyu_burst_seq")
        if sequence is None:
            if not self.register(event):
                return False
            sequence = event.get_extra("mengyu_burst_seq")
        # Synchronous inbound housekeeping can consume the quiet window.
        # Let callbacks already queued on the loop register before closing it.
        await asyncio.sleep(0)
        while True:
            async with self.lock:
                if not self.is_current(sequence) or not self.pending:
                    event.stop_event()
                    return False
                now = time.monotonic()
                quiet = self.quiet
                last_text = self.pending[-1][1] if self.pending else ""
                if last_text and not re.search(
                    r"[。！？!?…~～]$", str(last_text).strip()
                ):
                    quiet += 6.0
                window_started = self.window_started if self.window_started is not None else self.last_arrival
                remaining = min(
                    quiet - (now - self.last_arrival),
                    self.max_wait - (now - window_started),
                )
                if remaining <= 0:
                    self._populate(event)
                    self.window_started = None
                    return True
                changed = self.changed
            try:
                await asyncio.wait_for(changed.wait(), remaining)
            except TimeoutError:
                pass

    def _populate(self, event):
        """Expose the exact fragments and attachments owned by this reply."""
        parts = [text for _, text, _ in self.pending if text]
        event.message_str = "\n".join(parts)
        event.message_obj.message = [component for _, _, items in self.pending for component in items]
        event.set_extra("mengyu_burst_count", len(self.pending))
        event.set_extra("mengyu_burst_parts", parts)
        event.set_extra("mengyu_burst_range", (self.pending[0][0], self.pending[-1][0]))

    def is_current(self, sequence):
        return sequence is None or (not self.closed and sequence == self.sequence)

    def has_recent_pending(self):
        """Give a fresh inbound turn priority without blocking on stale failures."""
        return bool(self.pending and time.monotonic() - self.last_arrival < 300)

    async def adopt_captured(self, event):
        """Keep a runner's reply when AstrBot already consumed its newer messages.

        Args:
            event: Original runner event with its message identifier and sequence.

        Returns:
            True only if every newer message was consumed by this exact runner.
        """
        sequence = event.get_extra("mengyu_burst_seq")
        run_id = getattr(getattr(event, "message_obj", None), "message_id", None)
        if sequence is None or run_id is None:
            return False
        async with self.lock:
            if self.closed or sequence == self.sequence:
                return False
            newer = [
                self.events.get(seq) for seq, _, _ in self.pending if seq > sequence
            ]
            if not newer or any(
                item is None
                or (item.get_extra("_follow_up_captured") or {}).get("target_run_id")
                != str(run_id)
                for item in newer
            ):
                return False
            event.set_extra("mengyu_burst_seq", self.sequence)
            self._populate(event)
            return True

    async def commit(self, sequence):
        if sequence is None:
            return
        async with self.lock:
            self._remove_through(sequence)

    def _remove_through(self, sequence):
        self.pending = [item for item in self.pending if item[0] > sequence]
        self.events = {
            seq: item for seq, item in self.events.items() if seq > sequence
        }

    async def discard(self, through):
        """Clear fragments through the command's arrival, preserving newer ones."""
        async with self.lock:
            if through == self.sequence:
                self.sequence += 1
            self._remove_through(through)
            self.window_started = self.last_arrival if self.pending else None
            self.changed.set()
            self.changed = asyncio.Event()

    async def close(self):
        async with self.lock:
            self.closed = True
            self.sequence += 1
            self.pending.clear()
            self.events.clear()
            self.changed.set()
