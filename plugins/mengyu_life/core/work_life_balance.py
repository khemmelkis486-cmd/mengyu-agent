"""Protect independently chosen life activities from automatic work overrides."""


def should_replace_idle_plan_with_work(plan, work_due, *, at_home, busy_life, asleep):
    """Only fill idle time with due work, never erase a concrete life plan.

    This does not prohibit the planner from explicitly choosing work. It keeps
    overdue virtual freelance orders from converting every proposed outing,
    meal, social visit or hobby into another drafting session.
    """
    if not isinstance(work_due, dict) or not work_due.get("需安排工作时段"):
        return False
    if not at_home or busy_life or asleep:
        return False
    if not isinstance(plan, dict):
        return False
    # A concrete daylife action (including a work action chosen by the planner)
    # should not be silently replaced by a different work order.
    return plan.get("kind") in ("continue", "idle")
