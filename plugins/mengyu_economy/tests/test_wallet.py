"""Regression tests for wallet posting, restarts, and calendar behavior."""

import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from mengyu_economy.service import Wallet
from mengyu_economy.settings import Settings, WalletError

TZ = ZoneInfo("UTC")


def at(value):
    """Convert a local test date into a midday wallet timestamp.

    Args:
        value: ISO local date.

    Returns:
        The local midday Unix timestamp.
    """
    return int(datetime.fromisoformat(value + "T12:00:00").replace(tzinfo=TZ).timestamp())


class WalletTest(unittest.TestCase):
    """Exercise the wallet with an isolated SQLite file per case."""

    def setUp(self):
        """Prepare a private directory for this wallet."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "wallet.sqlite3"

    def open(self, start="2026-01-30", **overrides):
        """Open a wallet with deterministic sample settings.

        Args:
            start: Local opening or restart date.
            **overrides: Values to override in the sample config.

        Returns:
            A ready wallet instance.
        """
        config = {
            "enabled": True,
            "opening_cash_cents": 10000,
            "emergency_reserve_cents": 0,
            "monthly_budgets": dict.fromkeys(("food", "transport", "shopping", "entertainment", "gifts"), 5000),
            **overrides,
        }
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        wallet = Wallet(self.path, Settings.load(config_path))
        wallet.initialize(at(start))
        return wallet

    def quote(self, wallet, identity, amount, *, start="2026-01-30", quantity=1):
        """Save one quoted retail item for a fixed trip.

        Args:
            wallet: Wallet under test.
            identity: Unique quote ID.
            amount: Unit price in cents.
            start: Local time at which the quote is seen.
            quantity: Quoted quantity.
        """
        wallet.create_quote(
            {
                "quote_id": identity, "source_ref": "trip:test", "label": identity,
                "amount_cents": amount, "quantity": quantity,
                "expires_at": at(start) + 86400,
            },
            at(start),
        )

    def test_fixed_bills_resume_and_close_month(self):
        """Process short-month bills exactly once even when the bot is offline."""
        rent = [{"id": "rent", "label": "Rent", "day": 31, "amount_cents": 3500}]
        wallet = self.open(bill_templates=rent)
        self.assertEqual(wallet.snapshot(at("2026-01-30"))["protected_cents"], 3500)
        wallet = self.open("2026-03-01", bill_templates=rent, opening_cash_cents=45000)
        wallet.settle_due(at("2026-03-01"))
        self.assertEqual(wallet.snapshot(at("2026-03-01"))["cash_cents"], 3000)
        paid = [
            row["payload"]["date"] for row in wallet.events_since()["events"] if row["kind"] == "bill_paid"
        ]
        self.assertEqual(paid, ["2026-01-31", "2026-02-28"])
        self.assertEqual(wallet.read("monthly", at("2026-03-01"))["spent_cents"], 3500)

    def test_late_bill_paid_with_new_income(self):
        """Keep an unpaid bill protected and settle it after an actual income."""
        wallet = self.open(
            bill_templates=[{"id": "rent", "label": "Rent", "day": 31, "amount_cents": 15000}],
            work_income_rules={"default": {"amount_cents": 16000, "delay_days": 2, "policy": "simulated_auto"}},
        )
        today = at("2026-01-30")
        evidence = {"artifact_id": "work:asset:1", "delivered_at": today}
        invoice = wallet.register_receivable("example", evidence, now=today)
        self.assertEqual(invoice["settled_cents"], 0)
        self.assertEqual(wallet.snapshot(today)["cash_cents"], 10000)
        wallet.settle_due(at("2026-01-31"))
        self.assertEqual(wallet.snapshot(at("2026-01-31"))["pressure"], "critical")
        wallet.settle_due(at("2026-02-01"))
        self.assertEqual(wallet.snapshot(at("2026-02-01"))["cash_cents"], 11000)
        self.assertEqual(wallet.evidence(invoice["id"])["settled_cents"], 16000)
        self.assertEqual(wallet.evidence("bill:rent:2026-01")["settled_cents"], 15000)
        self.assertEqual(wallet.read("monthly", at("2026-02-01"))["cash_cents"], 10000)

    def test_same_checkout_retries_after_expiration(self):
        """Check atomic checkout, quote expiry and the immutable receipt."""
        wallet = self.open()
        self.quote(wallet, "scarf", 1600, quantity=2)
        action = {
            "action_id": "cart-1", "kind": "purchase", "source_ref": "trip:test", "quote_ids": ["scarf"]
        }
        today = at("2026-01-30")
        self.assertTrue(wallet.preview(action, today)["ok"])
        self.assertEqual(wallet.snapshot(today)["cash_cents"], 10000)
        result = wallet.commit(action, today)
        self.assertEqual(result, wallet.commit(action, at("2026-02-02")))
        self.assertEqual(result["amount_cents"], 3200)
        item_id = result["item_ids"][0]
        wallet.update_item(item_id, location="Wardrobe", status="in_use", now=today)
        self.assertEqual(wallet.evidence(item_id)["last_used_at"], today)
        with self.assertRaisesRegex(WalletError, "conflicting_action"):
            wallet.commit({**action, "category": "gifts"}, today)
        refund = wallet.commit({"action_id": "refund-1", "kind": "refund", "item_id": item_id}, today)
        self.assertEqual(refund["cash_delta_cents"], 3200)
        self.assertEqual(wallet.evidence(item_id)["status"], "returned")
        self.assertEqual(wallet.snapshot(today)["budgets"]["shopping"]["spent_cents"], 0)

    def test_competing_checkouts_cannot_overspend(self):
        """Only one concurrently submitted cart can use the remaining category budget."""
        wallet = self.open()
        self.quote(wallet, "first", 3200)
        self.quote(wallet, "second", 3200)

        def attempt(identity):
            try:
                return wallet.commit(
                    {
                        "action_id": identity, "kind": "purchase", "source_ref": "trip:test",
                        "quote_ids": [identity],
                    },
                    at("2026-01-30"),
                )["ok"]
            except WalletError as exc:
                return str(exc)

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(attempt, ("first", "second")))
        self.assertCountEqual(outcomes, [True, "category_limit"])
        self.assertEqual(wallet.snapshot(at("2026-01-30"))["cash_cents"], 6800)
        with wallet.store.transaction(write=False) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM items").fetchone()[0], 1)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM (SELECT tx_id FROM ledger_entries GROUP BY tx_id HAVING SUM(amount_cents)!=0)"
            ).fetchone()[0], 0)

    def test_quote_rules_and_owner_budget(self):
        """Keep quotes separate from transactions and respect gifted spending limits."""
        wallet = self.open()
        self.quote(wallet, "present", 4500)
        cart = {
            "action_id": "gift-1", "source_ref": "trip:test",
            "kind": "purchase", "category": "gifts", "quote_ids": ["present"],
        }
        today = at("2026-01-30")
        self.assertEqual(wallet.preview({**cart, "source_ref": "wrong"}, today)["error"], "quote_mismatch")
        self.assertEqual(wallet.preview(cart, at("2026-01-31"))["error"], "quote_expired")
        wallet.commit(cart, today)
        budgets = wallet.snapshot(today)["budgets"]
        self.assertEqual(budgets["gifts"]["remaining_cents"], 500)
        self.assertEqual(budgets["shopping"]["remaining_cents"], 5000)

    def test_savings_retry_and_loan_partial_repayment(self):
        """Keep savings and unpaid loan principal outside spendable cash."""
        wallet = self.open(
            "2026-02-01", opening_cash_cents=3000,
            savings_goals=[{"id": "camera", "label": "Camera", "target_cents": 9000, "monthly_cents": 5000}],
        )
        today = at("2026-02-02")
        advance = wallet.commit(
            {
                "action_id": "loan", "kind": "loan", "person_id": "fictional-friend",
                "amount_cents": 1000, "label": "Loan", "due_date": "2026-02-03",
            },
            today,
        )
        wallet.settle_due(at("2026-02-04"))
        self.assertEqual(wallet.snapshot(at("2026-02-04"))["cash_cents"], 2000)
        wallet.commit(
            {
                "action_id": "repayment", "kind": "loan_repayment", "obligation_id": advance["obligation_id"],
                "amount_cents": 400,
            },
            at("2026-02-04"),
        )
        self.assertEqual(wallet.evidence(advance["obligation_id"])["settled_cents"], 400)
        wallet.commit(
            {"action_id": "income", "kind": "income", "label": "Income", "amount_cents": 7000},
            at("2026-02-04"),
        )
        wallet.settle_due(at("2026-02-05"))
        result = wallet.snapshot(at("2026-02-05"))
        self.assertEqual((result["cash_cents"], result["savings_cents"]), (4400, 5000))

    def test_refund_in_a_new_month_restores_cash_without_expanding_budget(self):
        """Retain the original purchase period and keep the new limit intact."""
        wallet = self.open()
        self.quote(wallet, "january", 2400)
        bought = wallet.commit(
            {
                "action_id": "buy-january",
                "source_ref": "trip:test",
                "kind": "purchase",
                "quote_ids": ["january"],
            },
            at("2026-01-30"),
        )
        when = at("2026-02-02")
        wallet.commit(
            {
                "action_id": "refund-february",
                "kind": "refund",
                "item_id": bought["item_ids"][0],
            },
            when,
        )
        status = wallet.snapshot(when)
        self.assertEqual(status["cash_cents"], 10000)
        self.assertEqual(status["budgets"]["shopping"]["remaining_cents"], 5000)
        self.assertEqual(wallet.read("monthly", when)["category_spent_cents"]["shopping"], 2400)

    def test_old_work_and_missing_terms(self):
        """Require a configured payout and positive evidence within the ledger period."""
        wallet = self.open()
        with self.assertRaisesRegex(WalletError, "work_before_wallet"):
            wallet.register_receivable(
                "old", {"artifact_id": "previous", "delivered_at": at("2026-01-29")}, now=at("2026-01-30")
            )
        with self.assertRaisesRegex(WalletError, "needs_terms"):
            wallet.register_receivable(
                "new", {"artifact_id": "new", "delivered_at": at("2026-01-30")}, now=at("2026-01-30")
            )
        self.assertEqual(len(wallet.events_since()["events"]), 1)

    def test_disabled_and_corrupt_data(self):
        """Default to disabled and preserve a corrupt database on failure."""
        self.assertIsNone(Settings.load(self.root / "config.json"))
        self.path.write_bytes(b"not a wallet database")
        with self.assertRaises(sqlite3.DatabaseError):
            self.open()
        self.assertEqual(self.path.read_bytes(), b"not a wallet database")

