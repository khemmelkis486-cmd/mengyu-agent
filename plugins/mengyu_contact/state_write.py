"""Atomic domain writes, on-disk writer fingerprints and redacted diagnostics."""

import argparse
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid

if __package__:
    from . import config, state_owners
else:
    import config
    import state_owners

LOGGER = logging.getLogger(__name__)
LOCK = threading.RLock()
AUDITS = {}
TRIGGER = ContextVar("mengyu_write_trigger", default="")
FILES = {
    "state.json": "contact/presence/sleep/delivery",
    "life.json": "reflections",
    "activities.json": "activity",
    "activity_schedule.json": "scheduler",
    "events.json": "events",
    "gifts.json": "gifts/meals",
    "work.json": "work",
    "health.json": "health",
    "wishes.json": "wishes",
    "meal_preferences.json": "meal_preferences",
    "computer/state.json": "browser",
    "places.json": "places",
    "game_life.json": "games",
    "task_handoff.json": "task_inbox",
}
LIMIT = 1024 * 1024


class ExternalStateChange(RuntimeError):
    """An audited file was modified outside the configured writer."""


def stable_id(prefix, value):
    return f"{prefix}:{hashlib.sha256(str(value).encode('utf-8')).hexdigest()[:24]}"


def fingerprint(payload):
    return hashlib.sha256(payload).hexdigest() if payload is not None else None


def read_bytes(path):
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def atomic_bytes(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def json_bytes(data):
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")


def log_record(path, record):
    """Rotate bounded, text-free diagnostic records."""
    try:
        with LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size >= LIMIT:
                path.replace(path.with_suffix(path.suffix + ".1"))
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"at": time.time(), **record}, ensure_ascii=False) + "\n")
        return True
    except OSError as exc:
        LOGGER.warning("Mengyu diagnostics write failed: %s", type(exc).__name__)
        return False


@contextmanager
def write_trigger(correlation):
    token = TRIGGER.set(correlation)
    try:
        yield
    finally:
        TRIGGER.reset(token)


def _changed_fields(previous, following):
    if not isinstance(following, dict):
        return []
    try:
        old = json.loads(previous) if previous else {}
    except (TypeError, ValueError):
        old = {}
    if not isinstance(old, dict):
        return []
    missing = object()
    return sorted(key for key in old.keys() | following.keys()
                  if old.get(key, missing) != following.get(key, missing))


def acquire_writer_lease(root):
    """Keep another participating process from writing during protect mode."""
    path = Path(root) / "diagnostics" / "writer.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise ExternalStateChange("The data directory already has a protected writer") from None
    return handle


class WriterAudit:
    def __init__(self, root, mode="observe"):
        self.root = Path(root).resolve()
        self.mode = mode if mode in ("observe", "protect") else "observe"
        self.diag = self.root / "diagnostics"
        self.index_path = self.diag / "write-index.json"
        self.log_path = self.diag / "state-write.jsonl"
        self.index = {"files": {}}
        if self.index_path.exists():
            value = json.loads(self.index_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or not isinstance(value.get("files"), dict):
                raise ValueError("Invalid state write index")
            self.index = value
        self.drift = {}
        self.lease = None

    def _save_index(self):
        atomic_bytes(self.index_path, json_bytes(self.index))

    def _record(self, result, name, **fields):
        return log_record(self.log_path, {
            "result": result, "file": name, "domain": FILES[name],
            "correlation_id": TRIGGER.get() or None, **fields,
        })

    def _check(self, name, raw):
        value = fingerprint(raw)
        known = self.index["files"]
        if name not in known:
            known[name] = {"revision": value}
            self._save_index()
            self._record("baseline", name, revision=value)
        row = known[name]
        pending = row.get("pending")
        if pending and value == pending["after"]:
            known[name] = {"revision": value}
            self._save_index()
            self._record("recovered_commit", name, **pending)
        elif pending and value == row.get("revision"):
            known[name] = {"revision": value}
            self._save_index()
            self._record("not_applied", name, **pending)
        if known[name].get("revision") == value:
            self.drift.pop(name, None)
        elif name not in self.drift or self.drift[name] != value:
            self.drift[name] = value
            saved = False
            if raw is not None:
                # Keep one private copy per data file; the diagnostic contains only digests.
                snapshot = self.diag / "quarantine" / name
                try:
                    atomic_bytes(snapshot, raw)
                    saved = True
                except OSError as exc:
                    LOGGER.warning("Mengyu quarantine write failed: %s", type(exc).__name__)
            self._record("external_change", name, writer="unknown",
                         before=known[name].get("revision"), after=value,
                         snapshot_saved=saved)
            LOGGER.warning("Mengyu external file change (%s): %s", self.mode, name)
        return value

    def scan(self):
        with LOCK:
            for name in FILES:
                self._check(name, read_bytes(self.root / name))
            return set(self.drift)

    def write(self, name, data, writer, payload):
        with LOCK:
            path = self.root / name
            before = read_bytes(path)
            actual = self._check(name, before)
            if name in self.drift and self.mode == "protect":
                self._record("blocked", name, writer=writer, revision=actual)
                raise ExternalStateChange(name)
            after = fingerprint(payload)
            if actual == after:
                return
            changed = _changed_fields(before, data)
            if name == "state.json" and before is not None:
                actor = state_owners.WRITER.get() if writer == "MengyuContact" else writer
                violations = state_owners.violations((key for key in changed if key != "version"), actor)
                if violations:
                    log_record(self.diag / "state-violations.jsonl", {
                        "writer": actor, "fields": violations, "correlation_id": TRIGGER.get(),
                    })
            pending = {
                "operation_id": uuid.uuid4().hex, "writer": writer,
                "before": actual, "after": after,
                "fields": changed[:32],
            }
            known = self.index["files"]
            known[name]["pending"] = pending
            self._save_index()
            try:
                if fingerprint(read_bytes(path)) != actual:
                    self._check(name, read_bytes(path))
                    raise ExternalStateChange(name)
                atomic_bytes(path, payload)
            except OSError as exc:
                self._record("write_error", name, **pending, error=type(exc).__name__)
                raise
            known[name] = {"revision": after}
            self.drift.pop(name, None)
            # If final bookkeeping fails, the persisted intent lets startup recover it.
            try:
                self._save_index()
            except OSError as exc:
                self._record("commit_index_pending", name, **pending, error=type(exc).__name__)
            else:
                self._record("committed", name, **pending)

    def adopt(self, name, expected):
        """Accept an operator-reviewed revision while the owning service is stopped."""
        if name not in FILES:
            raise ValueError("Unknown domain file")
        with LOCK:
            current = self._check(name, read_bytes(self.root / name))
            if current != expected:
                raise ExternalStateChange("The observed revision no longer matches")
            old = self.index["files"][name].get("revision")
            self.index["files"][name] = {"revision": current}
            self._save_index()
            self._record("operator_adopted", name, before=old, after=current,
                         writer="maintenance", operation_id=uuid.uuid4().hex)
            self.drift.pop(name, None)


def register(root):
    path = Path(root).resolve()
    with LOCK:
        audit = AUDITS.get(path)
        if audit is None:
            audit = WriterAudit(path, config.get("write_mode", root=path))
            if audit.mode == "protect":
                audit.lease = acquire_writer_lease(path)
            try:
                audit.scan()
            except Exception:
                if audit.lease:
                    audit.lease.close()
                raise
            AUDITS[path] = audit
        return audit


def write_json(path, data, writer):
    path = Path(path).resolve()
    with LOCK:
        if path.name == "state.json" and (writer == "MengyuContact" or path.parent in AUDITS):
            if __package__:
                from .state_schema import prepare_state
            else:
                from state_schema import prepare_state

            data, issues = prepare_state(data)
            if issues:
                log_record(path.parent / "diagnostics" / "state-schema.jsonl", {
                    "stage": "save", "issues": issues, "correlation_id": TRIGGER.get(),
                })
        payload = json_bytes(data)
        for root in (path.parent, path.parent.parent):
            audit = AUDITS.get(root)
            if audit:
                name = path.relative_to(root).as_posix()
                if name in FILES:
                    return audit.write(name, data, writer, payload)
        atomic_bytes(path, payload)


def inspect(args):
    with acquire_writer_lease(args.data):
        audit = WriterAudit(args.data)
        drift = audit.scan()
        if args.adopt:
            name, revision = args.adopt
            audit.adopt(name, None if revision == "missing" else revision)
            drift.discard(name)
        for name in sorted(drift):
            print(f"{name}\t{audit.drift[name] or 'missing'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Inspect Mengyu domain revisions with AstrBot stopped.")
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--adopt", nargs=2, metavar=("FILE", "REVISION"))
    inspect(parser.parse_args())
