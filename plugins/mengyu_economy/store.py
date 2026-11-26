"""SQLite storage for the wallet ledger and its related state."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .settings import WalletError

CASH = "asset:cash"
SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE transactions (
    tx_id TEXT PRIMARY KEY,
    request TEXT NOT NULL,
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    category TEXT,
    effective_date TEXT NOT NULL,
    booked_at INTEGER NOT NULL,
    result TEXT NOT NULL
);
CREATE TABLE ledger_entries (
    tx_id TEXT NOT NULL REFERENCES transactions(tx_id),
    account TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents != 0),
    PRIMARY KEY (tx_id, account)
);
CREATE INDEX ledger_by_account ON ledger_entries(account);
CREATE INDEX transactions_by_day ON transactions(effective_date, category);
CREATE TABLE monthly_budgets (
    month TEXT NOT NULL,
    category TEXT NOT NULL,
    limit_cents INTEGER NOT NULL CHECK (limit_cents >= 0),
    PRIMARY KEY (month, category)
);
CREATE TABLE obligations (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    evidence_id TEXT,
    label TEXT NOT NULL,
    due_date TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    settled_cents INTEGER NOT NULL DEFAULT 0
        CHECK (settled_cents BETWEEN 0 AND amount_cents),
    policy TEXT NOT NULL
);
CREATE INDEX obligations_by_day ON obligations(due_date);
CREATE TABLE quotes (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    label TEXT NOT NULL,
    variant TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    price_source TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    tx_id TEXT REFERENCES transactions(tx_id)
);
CREATE TABLE items (
    id TEXT PRIMARY KEY,
    quote_id TEXT NOT NULL UNIQUE REFERENCES quotes(id),
    tx_id TEXT NOT NULL REFERENCES transactions(tx_id),
    location TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'owned',
    updated_at INTEGER,
    last_used_at INTEGER
);
CREATE TABLE goals (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    target_cents INTEGER NOT NULL CHECK (target_cents > 0),
    monthly_cents INTEGER NOT NULL CHECK (monthly_cents >= 0),
    due_day INTEGER NOT NULL CHECK (due_day BETWEEN 1 AND 31),
    created_date TEXT NOT NULL
);
CREATE TABLE savings_attempts (
    goal_id TEXT NOT NULL REFERENCES goals(id),
    month TEXT NOT NULL,
    income_count INTEGER NOT NULL,
    attempts INTEGER NOT NULL CHECK (attempts BETWEEN 1 AND 2),
    tx_id TEXT REFERENCES transactions(tx_id),
    PRIMARY KEY (goal_id, month)
);
CREATE TABLE wallet_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    occurred_at INTEGER NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE monthly_reports (
    month TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    payload TEXT NOT NULL
);
"""


def dump(value):
    """Encode a JSON value for storage or a model response.

    Args:
        value: The JSON value to encode.

    Returns:
        Compact canonical JSON text.
    """
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


class Store:
    """Open short-lived database connections and own transaction boundaries."""

    def __init__(self, path: Path):
        self.path = path

    def initialize(self):
        """Create the schema once and reject corrupt or unsupported databases.

        Raises:
            WalletError: The database does not match the supported schema.
            sqlite3.Error: The database cannot be opened or checked.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise WalletError("wallet_corrupt")
            db.execute("PRAGMA journal_mode=WAL")
        with self.transaction() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == SCHEMA_VERSION:
                return
            if (
                version
                or db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1"
                ).fetchone()
            ):
                raise WalletError("unsupported_wallet_schema")
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @contextmanager
    def connection(self):
        """Open one SQLite connection for the calling thread.

        Yields:
            A configured SQLite connection.
        """
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self, *, write=True):
        """Run a unit of work with a stable snapshot or reserved write lock.

        Args:
            write: Acquire the write lock before reading mutable balances.

        Yields:
            A SQLite connection in a transaction.
        """
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            with db:
                yield db

    @staticmethod
    def balance(db, account=CASH, *, through=None):
        """Sum debit-positive entries for an asset account.

        Args:
            db: Active wallet database connection.
            account: Ledger account name.
            through: Optional inclusive business date.

        Returns:
            Signed account balance in cents.
        """
        return db.execute(
            """SELECT COALESCE(SUM(e.amount_cents), 0)
               FROM ledger_entries e JOIN transactions t USING (tx_id)
               WHERE e.account=? AND (? IS NULL OR t.effective_date<=?)""",
            (account, through, through),
        ).fetchone()[0]

    @staticmethod
    def prior_result(db, identity, request):
        """Return a recorded receipt for an identical retry.

        Args:
            db: Active wallet database connection.
            identity: Stable transaction identifier.
            request: Canonical operation payload.

        Returns:
            The original receipt, or None for a new operation.

        Raises:
            WalletError: The identifier is already bound to another request.
        """
        row = db.execute(
            "SELECT request, result FROM transactions WHERE tx_id=?", (identity,)
        ).fetchone()
        if row is None:
            return None
        if row["request"] != dump(request):
            raise WalletError("conflicting_action")
        return json.loads(row["result"])

    @staticmethod
    def event(db, identity, kind, stamp, payload):
        """Store a durable event in the current transaction.

        Args:
            db: Active wallet database connection.
            identity: Stable event identifier.
            kind: Event type.
            stamp: UTC seconds when the event was recorded.
            payload: Structured event facts.
        """
        db.execute(
            "INSERT INTO wallet_events(event_id, kind, occurred_at, payload) VALUES(?,?,?,?)",
            (identity, kind, stamp, dump(payload)),
        )

    def post(
        self,
        db,
        identity,
        request,
        entries,
        *,
        kind,
        label,
        category,
        effective_date,
        stamp,
        extra=None,
    ):
        """Post one balanced transaction with its receipt and event atomically.

        Args:
            db: Active write transaction.
            identity: Stable transaction identifier.
            request: Canonical operation payload.
            entries: Account names mapped to signed cents.
            kind: Transaction type.
            label: Human-facing description.
            category: Optional budget category.
            effective_date: Local business date in ISO form.
            stamp: UTC booking timestamp.
            extra: Optional receipt facts.

        Returns:
            The stored receipt.

        Raises:
            WalletError: The posting is unbalanced or would overdraw cash.
        """
        if sum(entries.values()):
            raise WalletError("unbalanced_transaction")
        cash_after = self.balance(db) + entries.get(CASH, 0)
        if cash_after < 0:
            raise WalletError("insufficient_cash")
        receipt = {
            "ok": True,
            "tx_id": identity,
            "event_id": identity,
            "kind": kind,
            "label": label,
            "category": category,
            "date": effective_date,
            "cash_delta_cents": entries.get(CASH, 0),
            "cash_after_cents": cash_after,
            **(extra or {}),
        }
        db.execute(
            "INSERT INTO transactions VALUES(?,?,?,?,?,?,?,?)",
            (
                identity,
                dump(request),
                kind,
                label,
                category,
                effective_date,
                stamp,
                dump(receipt),
            ),
        )
        db.executemany(
            "INSERT INTO ledger_entries VALUES(?,?,?)",
            (
                (identity, account, amount)
                for account, amount in entries.items()
                if amount
            ),
        )
        self.event(db, identity, kind, stamp, receipt)
        return receipt
