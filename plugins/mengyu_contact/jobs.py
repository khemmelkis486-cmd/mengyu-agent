"""Persistent identity and conservative restart recovery for companion jobs."""


def night_record(sleep):
    """Return this sleep window's separate delivery record.

    Args:
        sleep: Persisted sleep window including legacy flags.

    Returns:
        A record belonging to exactly this sleep window.
    """
    record = sleep.get("night_wake")
    if record:
        return record
    status = "pending"
    if sleep.get("night_wake_uncertain"):
        status = "uncertain"
    elif sleep.get("night_wake_done"):
        status = "sent"
    return {
        "id": f"night:{sleep.get('since', 0)}:{sleep.get('night_wake_at', 0)}",
        "status": status,
    }


def night_due(sleep, now):
    """Night-wake messages are retired; keep the guard permanently closed.

    Args:
        sleep: Current sleep window.
        now: Current Unix timestamp.

    Returns:
        Always False.
    """
    return False


def recover_jobs(state):
    """Recover interrupted work without resubmitting an ambiguous paid request.

    Args:
        state: State just loaded from disk.
    """
    sleep = state.get("sleep") or {}
    if sleep:
        wake = night_record(sleep)
        if wake["status"] == "sending":
            wake["status"] = "uncertain"
        elif wake["status"] == "generating":
            wake["status"] = "pending"
        sleep["night_wake"] = wake
        sleep["night_wake_uncertain"] = wake["status"] == "uncertain"
        sleep["night_wake_done"] = wake["status"] in ("sent", "skipped")
    promise = state.get("photo_promise") or {}
    jobs = state.setdefault("photo_jobs", {})
    if promise:
        promise.setdefault("id", f"legacy-photo:{promise.get('at', 0)}")
        jobs.setdefault(promise["id"], dict(promise))
    for job in jobs.values():
        if job.get("status") == "running":
            job["status"] = "uncertain"
        elif job.get("status") == "waiting":
            job["status"] = "pending"
    if promise and promise["id"] in jobs:
        state["photo_promise"] = dict(jobs[promise["id"]])
