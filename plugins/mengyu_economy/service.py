"""Independent service for the fictional economy."""

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import commerce, settlement
from .settings import CATEGORIES, Settings, WalletError, day, integer, text
from .store import CASH, Store

ASSET_LOANS = "asset:receivable:loan"
ASSET_WORK = "asset:receivable:work"
LIABILITY = "liability:payable"
SPENDING_CATEGORIES = (*CATEGORIES, "repair", "medical", "other")


class Wallet:
    """Persist and query one isolated virtual wallet."""

    def __init__(self, path: Path, settings: Settings):
        self.store = Store(path)
        self.settings = settings

    def _date(self, now):
        """Resolve the wallet clock.

        Args:
            now: Optional POSIX timestamp.

        Returns:
            UTC booking seconds and the local business date.

        Raises:
            WalletError: The timestamp is invalid.
        """
        now = time.time() if now is None else now
        if type(now) not in (int, float) or not 0 <= now < 253398758400:
            raise WalletError("invalid_timestamp")
        stamp = int(now)
        return stamp, datetime.fromtimestamp(stamp, self.settings.timezone).date()

    def initialize(self, now=None):
        """Open the reused ledger and seed the original balance once.

        Args:
            now: Optional clock override for replay or tests.

        Raises:
            WalletError: The wallet configuration conflicts with its ledger.
        """
        stamp, local_date = self._date(now)
        enabled = self.settings.enabled_date or local_date
        if enabled > local_date:
            raise WalletError("wallet_not_due")
        self.store.initialize()
        with self.store.transaction() as db:
            existing = settlement.meta(db, "enabled_date")
            if existing is None:
                settlement.set_meta(db, "enabled_date", enabled.isoformat())
                settlement.set_meta(db, "timezone", self.settings.timezone.key)
                amount = self.settings.opening_cash_cents
                self.store.post(
                    db,
                    "opening",
                    {"amount_cents": amount},
                    {CASH: amount, "equity:opening": -amount},
                    kind="opening",
                    label="初始余额",
                    category=None,
                    effective_date=enabled.isoformat(),
                    stamp=stamp,
                    extra={"amount_cents": amount},
                )
            elif self.settings.timezone.key != settlement.meta(db, "timezone"):
                raise WalletError("wallet_timezone_changed")
            elif self.settings.enabled_date and existing != enabled.isoformat():
                raise WalletError("wallet_opening_date_changed")
            first_goal_date = existing or enabled.isoformat()
            for goal in self.settings.savings_goals:
                db.execute(
                    """INSERT INTO goals VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                       label=excluded.label, target_cents=excluded.target_cents,
                       monthly_cents=excluded.monthly_cents, due_day=excluded.due_day""",
                    (
                        goal["id"],
                        goal["label"],
                        goal["target_cents"],
                        goal["monthly_cents"],
                        goal["day"],
                        local_date.isoformat() if existing else first_goal_date,
                    ),
                )
            settlement.advance(db, self.store, self.settings, local_date, stamp)

    def settle_due(self, now=None):
        """Apply due ledger operations and close prior months.

        Args:
            now: Optional clock override.
        """
        stamp, today = self._date(now)
        with self.store.transaction() as db:
            settlement.advance(db, self.store, self.settings, today, stamp)

    def snapshot(self, now=None):
        """Return the wallet's current balance, pressure and obligations.

        Args:
            now: Optional clock override.

        Returns:
            A private read-only balance projection.
        """
        _, today = self._date(now)
        with self.store.transaction(write=False) as db:
            return self._snapshot(db, today)

    def _snapshot(self, db, today):
        """Build a projection from one SQLite snapshot.

        Args:
            db: Open wallet database connection.
            today: Current business date.

        Returns:
            Current wallet summary.
        """
        cash, protected, flexible = settlement.cash_limits(db, self.settings, today)
        budgets = settlement.budget_rows(db, today.isoformat()[:7])
        savings = db.execute(
            """SELECT COALESCE(SUM(e.amount_cents),0) FROM ledger_entries e
               JOIN transactions t USING(tx_id)
               WHERE e.account LIKE 'asset:savings:%' AND t.effective_date<=?""",
            (today.isoformat(),),
        ).fetchone()[0]
        if cash < protected:
            pressure = "critical"
        elif flexible == 0:
            pressure = "tight"
        elif flexible < max(1, self.settings.emergency_reserve_cents) or any(
            row["remaining_cents"] <= 0 < row["limit_cents"] for row in budgets.values()
        ):
            pressure = "watch"
        else:
            pressure = "comfortable"

        def pending(kind):
            row = db.execute(
                """SELECT label,due_date,amount_cents-settled_cents AS remaining_cents
                   FROM obligations WHERE kind=? AND settled_cents<amount_cents
                   ORDER BY due_date,id LIMIT 1""",
                (kind,),
            ).fetchone()
            return dict(row) if row else None

        return {
            "ok": True,
            "currency": "CNY",
            "month": today.isoformat()[:7],
            "cash_cents": cash,
            "protected_cents": protected,
            "flexible_cents": flexible,
            "savings_cents": savings,
            "pressure": pressure,
            "budgets": budgets,
            "next_bill": pending("bill"),
            "next_income": pending("work"),
            "as_of": settlement.meta(db, "last_settlement_date"),
        }

    def create_quote(self, quote, now=None):
        """Record a fictional price for a shopping trip or wish.

        Args:
            quote: Structured quote with ID, binding and price.
            now: Optional clock override.

        Returns:
            The saved quote.
        """
        stamp, _ = self._date(now)
        with self.store.transaction() as db:
            return commerce.store_quote(db, quote, stamp)

    def preview(self, action, now=None):
        """Check the current funds and budget for a proposed purchase or expense.

        Args:
            action: Purchase or spending request.
            now: Optional clock override.

        Returns:
            Decision and amount without storing a transaction.
        """
        stamp, today = self._date(now)
        with self.store.transaction(write=False) as db:
            try:
                if action.get("kind") == "purchase":
                    category, _, amount = commerce.purchase_terms(db, action, stamp)
                    essential = False
                elif action.get("kind") == "expense":
                    amount, category, essential, _ = self._expense_terms(action)
                else:
                    raise WalletError("unsupported_action")
                error = settlement.spending_error(
                    db, self.settings, today, amount, category, essential=essential
                )
            except WalletError as exc:
                return {"ok": False, "error": str(exc)}
            return {
                "ok": not error,
                "amount_cents": amount,
                **({"error": error} if error else {}),
            }

    @staticmethod
    def _expense_terms(action):
        """Validate a proposed cash expense.

        Args:
            action: Expense payload.

        Returns:
            Amount, category, essential flag, and description.

        Raises:
            WalletError: Invalid category, amount or flag.
        """
        category = action.get("category")
        essential = action.get("essential", False)
        if category not in SPENDING_CATEGORIES or type(essential) is not bool:
            raise WalletError("invalid_expense")
        return (
            integer(action.get("amount_cents"), minimum=1),
            category,
            essential,
            text(action.get("label")),
        )

    def register_receivable(self, work_ref, evidence, terms=None, now=None):
        """Register simulated work income based on an already-delivered artifact.

        Args:
            work_ref: The originating work order ID.
            evidence: Trusted artifact ID and delivery timestamp.
            terms: Configured rule name, defaulting to "default".
            now: Optional clock override.

        Returns:
            An obligation record with current settlement totals.

        Raises:
            WalletError: Evidence, dates or configured terms are invalid.
        """
        stamp, today = self._date(now)
        work_ref = text(work_ref)
        if not isinstance(evidence, dict):
            raise WalletError("missing_work_evidence")
        artifact_id = text(evidence.get("artifact_id"))
        delivered_at = integer(evidence.get("delivered_at"), minimum=1, maximum=stamp)
        delivered = self._date(delivered_at)[1]
        identity = f"invoice:{work_ref}"
        with self.store.transaction() as db:
            existing = db.execute(
                "SELECT * FROM obligations WHERE id=?", (identity,)
            ).fetchone()
            if existing:
                if existing["evidence_id"] != artifact_id:
                    raise WalletError("conflicting_work_evidence")
                return dict(existing)
            if delivered < day(settlement.meta(db, "enabled_date")):
                raise WalletError("work_before_wallet")
            rule = self.settings.work_income_rules.get(text(terms or "default"))
            if rule is None:
                raise WalletError("needs_terms")
            # Keep recovered invoices in their original due period. An overdue
            # invoice discovered after catch-up settles on the current day.
            due = (delivered + timedelta(days=rule["delay_days"])).isoformat()
            amount = rule["amount_cents"]
            label = text(evidence.get("label", "稿费"))
            db.execute(
                """INSERT INTO obligations
                   (id,kind,source_ref,evidence_id,label,due_date,amount_cents,policy)
                   VALUES(?,'work',?,?,?,?,?,?)""",
                (identity, work_ref, artifact_id, label, due, amount, rule["policy"]),
            )
            self.store.post(
                db,
                identity,
                {"work_ref": work_ref, "artifact_id": artifact_id},
                {ASSET_WORK: amount, "income:work": -amount},
                kind="work_receivable",
                label=label,
                category=None,
                effective_date=today.isoformat(),
                stamp=stamp,
                extra={
                    "amount_cents": amount,
                    "due_date": due,
                    "obligation_id": identity,
                },
            )
            settlement.advance(db, self.store, self.settings, today, stamp)
            return dict(
                db.execute(
                    "SELECT * FROM obligations WHERE id=?", (identity,)
                ).fetchone()
            )

    def commit(self, action, now=None):
        """Settle a trusted, explicitly identified fictional action.

        Args:
            action: Internal request with a stable action ID.
            now: Optional clock override.

        Returns:
            The transaction receipt, identical on retry.

        Raises:
            WalletError: The action conflicts with an existing request or cannot post.
        """
        if not isinstance(action, dict):
            raise WalletError("invalid_action")
        stamp, today = self._date(now)
        identity = "action:" + text(action.get("action_id"), limit=160)
        kind = action.get("kind")
        with self.store.transaction() as db:
            prior = self.store.prior_result(db, identity, action)
            if prior:
                return prior
            settlement.advance(db, self.store, self.settings, today, stamp)
            if kind == "purchase":
                return commerce.purchase(
                    db, self.store, self.settings, identity, action, today, stamp
                )
            if kind in ("refund", "sale"):
                receipt = commerce.dispose_item(
                    db, self.store, identity, action, today, stamp
                )
            elif kind in ("work_payment", "loan_repayment", "pay_bill"):
                obligation_id = text(action.get("obligation_id"), limit=180)
                row = db.execute(
                    "SELECT * FROM obligations WHERE id=?", (obligation_id,)
                ).fetchone()
                allowed = {
                    "work_payment": ("work",),
                    "loan_repayment": ("loan",),
                    "pay_bill": ("bill", "payable"),
                }
                if not row or row["kind"] not in allowed[kind]:
                    raise WalletError("obligation_missing")
                receipt = settlement.settle_obligation(
                    db,
                    self.store,
                    row,
                    integer(action.get("amount_cents"), minimum=1),
                    identity,
                    action,
                    today,
                    stamp,
                )
            else:
                receipt = self._commit_posting(db, identity, action, today, stamp)
            if receipt["cash_delta_cents"] > 0:
                settlement.advance(db, self.store, self.settings, today, stamp)
            return receipt

    def _commit_posting(self, db, identity, action, today, stamp):
        """Post a non-purchase action and any associated obligation.

        Args:
            db: Open write transaction.
            identity: Stable transaction identity.
            action: Validated internal action envelope.
            today: Business date.
            stamp: Booking timestamp.

        Returns:
            A durable transaction receipt.

        Raises:
            WalletError: The operation cannot post.
        """
        kind = action.get("kind")
        amount = integer(action.get("amount_cents"), minimum=1)
        label = text(action.get("label"))
        category = None
        extra = {"amount_cents": amount}
        if kind == "expense":
            _, category, essential, _ = self._expense_terms(action)
            error = settlement.spending_error(
                db, self.settings, today, amount, category, essential=essential
            )
            if error:
                raise WalletError(error)
            entries = {CASH: -amount, f"expense:{category}": amount}
        elif kind == "income":
            raw_source = str(action.get("source") or "other").strip()
            if raw_source not in ("other", "gift", "refund", "sale"):
                raise WalletError("invalid_income_source")
            entries = {CASH: amount, f"income:{raw_source}": -amount}
            extra = {"amount_cents": amount, "source": raw_source}
        elif kind in ("loan", "payable"):
            obligation_id = identity
            due = day(action.get("due_date")).isoformat()
            source = text(
                action.get("person_id") if kind == "loan" else action.get("source_ref")
            )
            if kind == "loan":
                if amount > settlement.cash_limits(db, self.settings, today)[2]:
                    raise WalletError("low_available")
                entries = {CASH: -amount, ASSET_LOANS: amount}
            else:
                entries = {"expense:unpaid": amount, LIABILITY: -amount}
            db.execute(
                """INSERT INTO obligations (id,kind,source_ref,label,due_date,amount_cents,policy)
                   VALUES(?,?,?,?,?,?,'manual')""",
                (obligation_id, kind, source, label, due, amount),
            )
            extra.update({"obligation_id": obligation_id, "due_date": due})
        elif kind in ("savings_deposit", "savings_withdrawal"):
            goal_id = text(action.get("goal_id"))
            goal = db.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
            if not goal:
                raise WalletError("goal_missing")
            account = "asset:savings:" + goal_id
            if kind == "savings_deposit":
                if amount > settlement.cash_limits(db, self.settings, today)[2]:
                    raise WalletError("low_available")
                entries = {CASH: -amount, account: amount}
            else:
                if self.store.balance(db, account) < amount:
                    raise WalletError("insufficient_savings")
                entries = {CASH: amount, account: -amount}
            extra["goal_id"] = goal_id
        else:
            raise WalletError("unsupported_action")
        return self.store.post(
            db,
            identity,
            action,
            entries,
            kind=kind,
            label=label,
            category=category,
            effective_date=today.isoformat(),
            stamp=stamp,
            extra=extra,
        )

    def update_item(self, item_id, *, location, status, now=None):
        """Record how an owned item is stored, used, gifted, consumed or lost.

        Args:
            item_id: Stable wallet inventory identity.
            location: New location description.
            status: Inventory state.
            now: Optional clock override.

        Returns:
            The updated item record.

        Raises:
            WalletError: The item or state transition is invalid.
        """
        stamp, _ = self._date(now)
        valid_states = ("owned", "stored", "in_use", "gifted", "consumed", "lost")
        if (
            status not in valid_states
            or not isinstance(location, str)
            or len(location) > 150
        ):
            raise WalletError("invalid_item_update")
        with self.store.transaction() as db:
            item_id = text(item_id, limit=150)
            row = commerce.find_item(db, item_id)
            if (
                row
                and row["status"] == status
                and row["location"] == location.strip()
                and status != "in_use"
            ):
                return dict(row)
            if not row or row["status"] not in valid_states[:3]:
                raise WalletError("item_unavailable")
            db.execute(
                """UPDATE items SET location=?, status=?, updated_at=?,
                   last_used_at=CASE WHEN ?='in_use' THEN ? ELSE last_used_at END WHERE id=?""",
                (location.strip(), status, stamp, status, stamp, item_id),
            )
            return dict(commerce.find_item(db, item_id))

    def evidence(self, ref):
        """Read the authoritative record for one transaction, item or obligation.

        Args:
            ref: Stable wallet identifier.

        Returns:
            A structured evidence record or None.
        """
        ref = text(ref, limit=200)
        with self.store.transaction(write=False) as db:
            record = commerce.find_item(db, ref)
            if record:
                return {"kind": "item", **dict(record)}
            record = db.execute(
                "SELECT * FROM obligations WHERE id=?", (ref,)
            ).fetchone()
            if record:
                return dict(record)
            record = db.execute(
                "SELECT result FROM transactions WHERE tx_id=?", (ref,)
            ).fetchone()
            return json.loads(record["result"]) if record else None

    def events_since(self, cursor=0, limit=50):
        """Read durable wallet events without acknowledging them.

        Args:
            cursor: Last processed sequence number.
            limit: Maximum number of events.

        Returns:
            Ordered events and the last sequence returned.
        """
        cursor = integer(cursor, maximum=2**63 - 1)
        limit = integer(limit, minimum=1, maximum=200)
        with self.store.transaction(write=False) as db:
            events = [
                {**dict(row), "payload": json.loads(row["payload"])}
                for row in db.execute(
                    "SELECT * FROM wallet_events WHERE seq>? ORDER BY seq LIMIT ?",
                    (cursor, limit),
                )
            ]
        return {"events": events, "cursor": events[-1]["seq"] if events else cursor}

    def read(self, action, now=None):
        """Return a compact read-only view for the model tool.

        Args:
            action: A supported query name.
            now: Optional clock override.

        Returns:
            Structured result for the requested view.
        """
        stamp, today = self._date(now)
        with self.store.transaction(write=False) as db:
            if action == "status":
                result = self._snapshot(db, today)
                result.pop("budgets")
                return result
            if action == "budget":
                budgets = settlement.budget_rows(db, today.isoformat()[:7])
                for row in budgets.values():
                    row.pop("spent_cents")
                return {"ok": True, "month": today.isoformat()[:7], "budgets": budgets}
            if action == "recent":
                records = db.execute(
                    """SELECT t.kind,t.label,t.effective_date AS date,e.amount_cents AS cash_delta_cents
                       FROM transactions t JOIN ledger_entries e USING(tx_id)
                       WHERE e.account=? AND t.kind!='opening' AND t.effective_date<=?
                       ORDER BY t.effective_date DESC,t.rowid DESC LIMIT 3""",
                    (CASH, today.isoformat()),
                )
                return {"ok": True, "recent": [dict(row) for row in records]}
            if action == "goals":
                goals = [
                    {
                        "label": row["label"],
                        "target_cents": row["target_cents"],
                        "saved_cents": self.store.balance(
                            db, "asset:savings:" + row["id"]
                        ),
                    }
                    for row in db.execute("SELECT * FROM goals ORDER BY id LIMIT 3")
                ]
                return {"ok": True, "goals": goals}
            if action == "wishlist":
                records = db.execute(
                    """SELECT label,amount_cents,quantity FROM quotes
                       WHERE scope='wish' AND tx_id IS NULL AND expires_at>?
                       ORDER BY rowid DESC LIMIT 3""",
                    (stamp,),
                )
                return {"ok": True, "wishlist": [dict(row) for row in records]}
            if action == "monthly":
                row = db.execute(
                    "SELECT * FROM monthly_reports ORDER BY month DESC LIMIT 1"
                ).fetchone()
                if not row:
                    return {"ok": False, "error": "no_closed_month"}
                result = json.loads(row["payload"])
                result["fixed_bills_cents"] = -result.pop("cash_flows").get(
                    "bill_paid", 0
                )
                result["category_spent_cents"] = {
                    category: budget["spent_cents"]
                    for category, budget in result.pop("budgets").items()
                }
                return {"ok": True, "version": row["version"], **result}
            return {"ok": False, "error": "unknown_action"}
