"""Configuration and input validation for the simulated wallet."""

import json
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

CATEGORIES = ("food", "transport", "shopping", "entertainment", "gifts")
MAX_AMOUNT = 10**12


class WalletError(ValueError):
    """Stable error code for a rejected wallet operation."""


def integer(value, *, minimum=0, maximum=MAX_AMOUNT):
    """Validate an integer without coercing booleans, floats, or strings.

    Args:
        value: Value supplied by configuration or a trusted caller.
        minimum: Inclusive minimum.
        maximum: Inclusive maximum.

    Returns:
        The validated integer.

    Raises:
        WalletError: The value is outside the permitted range.
    """
    if type(value) is not int or not minimum <= value <= maximum:
        raise WalletError("invalid_amount")
    return value


def text(value, *, limit=120):
    """Validate and normalize a nonempty identifier or label.

    Args:
        value: Value to validate.
        limit: Maximum length.

    Returns:
        The stripped string.

    Raises:
        WalletError: The value is missing or invalid.
    """
    if not isinstance(value, str) or not 0 < len(value.strip()) <= limit:
        raise WalletError("invalid_text")
    return value.strip()


def day(value):
    """Parse a canonical ISO business date.

    Args:
        value: An ISO date string.

    Returns:
        A date object.

    Raises:
        WalletError: The input is missing or not a canonical date.
    """
    try:
        result = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise WalletError("invalid_date") from exc
    if result.isoformat() != value:
        raise WalletError("invalid_date")
    return result


@dataclass(frozen=True)
class Settings:
    """Validated configuration for one enabled wallet instance."""

    timezone: ZoneInfo
    owner_origin: str
    owner_id: str
    enabled_date: date | None
    opening_cash_cents: int
    emergency_reserve_cents: int
    reserve_days: int
    interval_seconds: int
    monthly_budgets: dict
    bill_templates: tuple
    work_income_rules: dict
    savings_goals: tuple

    @classmethod
    def load(cls, path: Path):
        """Load settings for an opted-in wallet.

        Args:
            path: Path of the wallet config file.

        Returns:
            Validated settings, or None when disabled or unconfigured.

        Raises:
            WalletError: The configuration is invalid.
        """
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return None
        if not isinstance(raw, dict) or type(raw.get("enabled")) is not bool:
            raise WalletError("invalid_config")
        if not raw["enabled"]:
            return None
        budgets = raw.get("monthly_budgets")
        bills = raw.get("bill_templates", [])
        goals = raw.get("savings_goals", [])
        rules = raw.get("work_income_rules", {})
        if (
            not isinstance(budgets, dict)
            or set(budgets) != set(CATEGORIES)
            or not isinstance(bills, list)
            or not isinstance(goals, list)
            or not isinstance(rules, dict)
        ):
            raise WalletError("invalid_config")

        billing = []
        for bill in bills:
            billing.append(
                {
                    "id": text(bill["id"]),
                    "label": text(bill["label"]),
                    "day": integer(bill["day"], minimum=1, maximum=31),
                    "amount_cents": integer(bill["amount_cents"], minimum=1),
                }
            )
        savings = []
        for goal in goals:
            savings.append(
                {
                    "id": text(goal["id"]),
                    "label": text(goal["label"]),
                    "target_cents": integer(goal["target_cents"], minimum=1),
                    "monthly_cents": integer(goal["monthly_cents"]),
                    "day": integer(goal.get("day", 1), minimum=1, maximum=31),
                }
            )
        if len({row["id"] for row in billing}) != len(billing) or len(
            {row["id"] for row in savings}
        ) != len(savings):
            raise WalletError("duplicate_config_id")
        income = {}
        for kind, rule in rules.items():
            policy = rule.get("policy", "manual")
            if policy not in ("manual", "simulated_auto"):
                raise WalletError("invalid_policy")
            income[text(kind)] = {
                "amount_cents": integer(rule["amount_cents"], minimum=1),
                "delay_days": integer(rule["delay_days"], maximum=365),
                "policy": policy,
            }
        owner_id = str(raw.get("owner_id") or "").strip()
        owner_origin = str(raw.get("owner_origin") or "").strip()
        if owner_id or owner_origin:
            if not owner_id or not owner_origin.endswith(f":FriendMessage:{owner_id}"):
                raise WalletError("invalid_owner_origin")
        return cls(
            timezone=ZoneInfo(
                raw.get("timezone")
                or os.environ.get("MENGYU_TIMEZONE")
                or "UTC"
            ),
            owner_origin=owner_origin,
            owner_id=owner_id,
            enabled_date=day(raw["enabled_date"]) if raw.get("enabled_date") else None,
            opening_cash_cents=integer(raw.get("opening_cash_cents")),
            emergency_reserve_cents=integer(raw.get("emergency_reserve_cents")),
            reserve_days=integer(raw.get("reserve_days", 7), maximum=31),
            interval_seconds=integer(
                raw.get("interval_seconds", 60), minimum=10, maximum=3600
            ),
            monthly_budgets={key: integer(value) for key, value in budgets.items()},
            bill_templates=tuple(billing),
            work_income_rules=income,
            savings_goals=tuple(savings),
        )
