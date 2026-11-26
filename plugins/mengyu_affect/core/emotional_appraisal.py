"""Evidence-based appraisal rules for simulated emotion updates."""

EVENT_DELTAS = {
    "work_progress": {"valence": 2, "achievement": 4, "stress": -1},
    "work_completed": {"valence": 9, "achievement": 14, "stress": -6},
    "work_blocked": {"valence": -4, "stress": 8, "energy": -3},
    "gift_received": {"valence": 11, "social_need": -5, "energy": 2},
    "gift_experienced": {"valence": 5, "achievement": 2},
    "new_discovery": {"valence": 4, "curiosity": 10},
    "activity_failed": {"valence": -3, "stress": 5},
    "rest_completed": {"energy": 9, "stress": -5},
    "social_activity": {"valence": 4, "social_need": -8},
    "friend_meetup_completed": {"valence": 5, "social_need": -8, "energy": -2},
    "personal_choice": {"autonomy_need": -5, "valence": 2},
    "user_support": {"valence": 4, "stress": -3, "social_need": -2},
    "interaction_upset": {"valence": -5, "stress": 4},
    "interaction_ease": {"valence": 2, "stress": -3},
    "interaction_clear": {"valence": 4, "stress": -5},
    "interaction_positive": {"valence": 3, "social_need": -2},
    # Life events published by plan_runner: her own day moves her mood.
    "outing_started": {"valence": 3, "curiosity": 6},
    "outing_completed": {"valence": 5, "curiosity": 4, "energy": -3},
    "meal_completed": {"valence": 3, "energy": 6, "stress": -3},
    "chore_completed": {"valence": 1, "achievement": 3, "stress": -2},
    "sleep_started": {"energy": -2, "stress": -2},
    "body_discomfort": {"valence": -5, "stress": 6, "energy": -4},
    "weather_gloom": {"valence": -2, "stress": 1},
    "sleep_finished": {"energy": 10, "stress": -4},
}


def appraise_event(event_type, detail="", overrides=None):
    """Return bounded-dimension deltas grounded in a known event category."""
    delta = dict(EVENT_DELTAS.get(str(event_type), {}))
    if isinstance(overrides, dict):
        for key, value in overrides.items():
            if key in {"valence", "energy", "stress", "social_need", "autonomy_need", "curiosity", "achievement"} and isinstance(value, (int, float)):
                delta[key] = float(value)
    return delta


def interaction_kind(decision):
    if not isinstance(decision, dict):
        return None
    action = decision.get("action")
    if action == "upset":
        return "interaction_upset"
    if action == "ease":
        return "interaction_ease"
    if action == "clear":
        return "interaction_clear"
    if action == "keep":
        feeling = str(decision.get("feeling") or "")
        if any(word in feeling for word in ("开心", "高兴", "满足", "得意", "期待", "安心", "喜欢")):
            return "interaction_positive"
    return None
