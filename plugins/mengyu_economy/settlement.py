"""Calendar processing and balance projections for the standalone wallet."""

import calendar
import json
from datetime import timedelta

from .settings import CATEGORIES, WalletError, day
from .store import CASH, Store, dump


def meta(db, key):
    """Read a wallet metadata value.

    Args:
        db: Open database connection.
        key: Metadata key.

    Returns:
        Stored text, or None if absent.
    """
    row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(db, key, value):
    """Upsert a wallet metadata value.

    Args:
        db: Open write transaction.
        key: Metadata key.
        value: Text value.
    """
    db.execute(
        """INSERT INTO metadata VALUES(?,?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
        (key, value),
    )


def months(start, end):
    """Iterate first-of-month dates spanning two business dates.

    Args:
        start: First included date.
        end: Last included date.

    Yields:
        The first day of each included month.
    """
    current = start.replace(day=1)
    while current <= end:
        yield current
        if current.year == 9999 and current.month == 12:
            break
        current = (current.replace(day=28) + timedelta(days=4)).replace(day=1)


def monthly_day(month, target_day):
    """Clamp a recurring due day to the end of a short month.

    Args:
        month: Any date in the target month.
        target_day: Day number from 1 to 31.

    Returns:
        The actual due date.
    """
    return month.replace(
        day=min(target_day, calendar.monthrange(month.year, month.month)[1])
    )


def budget_rows(db, month):
    """Read that month's stored limits and cash spending net of refunds.

    Args:
        db: Open wallet database connection.
        month: Business month in YYYY-MM form.

    Returns:
        A mapping from category to limit, spent, and remaining cents.
    """
    results = {}
    for row in db.execute(
        """SELECT b.category, b.limit_cents, COALESCE(-SUM(e.amount_cents),0) AS spent
           FROM monthly_budgets b
           LEFT JOIN transactions t ON t.category=b.category
                AND t.effective_date BETWEEN b.month || '-01' AND b.month || '-31'
                AND t.kind IN ('expense','purchase','refund','payable_paid')
                AND (t.kind!='refund' OR EXISTS (
                    SELECT 1 FROM transactions original
                    WHERE original.tx_id=json_extract(t.result,'$.purchase_tx_id')
                      AND substr(original.effective_date,1,7)=b.month
                ))
           LEFT JOIN ledger_entries e ON e.tx_id=t.tx_id AND e.account=?
           WHERE b.month=? GROUP BY b.category, b.limit_cents""",
        (CASH, month),
    ):
        results[row["category"]] = {
            "limit_cents": row["limit_cents"],
            "spent_cents": row["spent"],
            "remaining_cents": row["limit_cents"] - row["spent"],
        }
    return results


def cash_limits(db, settings, today):
    """Calculate cash available for optional expenses.

    Args:
        db: Open wallet database connection.
        settings: Wallet configuration.
        today: Business date being evaluated.

    Returns:
        Cash, protected liabilities, and flexible cash in cents.
    """
    cash = Store.balance(db, through=today.isoformat())
    cutoff = (today + timedelta(days=settings.reserve_days)).isoformat()
    protected = db.execute(
        """SELECT COALESCE(SUM(amount_cents-settled_cents),0) FROM obligations
           WHERE kind IN ('bill','payable') AND due_date<=?""",
        (cutoff,),
    ).fetchone()[0]
    return cash, protected, max(0, cash - protected - settings.emergency_reserve_cents)


def create_periods(db, settings, enabled, start, today):
    """Materialize monthly budgets and fixed bills through the reserve window.

    Args:
        db: Open write transaction.
        settings: Wallet configuration.
        enabled: First day covered by this wallet.
        start: Prior settlement date or opening date.
        today: Target business date.
    """
    for month in months(start, today):
        db.executemany(
            "INSERT OR IGNORE INTO monthly_budgets VALUES(?,?,?)",
            (
                (month.isoformat()[:7], key, value)
                for key, value in settings.monthly_budgets.items()
            ),
        )
    horizon = today + timedelta(days=settings.reserve_days)
    for month in months(start, horizon):
        for bill in settings.bill_templates:
            due = monthly_day(month, bill["day"])
            if due < enabled or due > horizon:
                continue
            period = month.isoformat()[:7]
            identity = f"bill:{bill['id']}:{period}"
            db.execute(
                """INSERT OR IGNORE INTO obligations
                   (id,kind,source_ref,label,due_date,amount_cents,policy)
                   VALUES(?,'bill',?,?,?,?,'simulated_auto')""",
                (
                    identity,
                    bill["id"],
                    bill["label"],
                    due.isoformat(),
                    bill["amount_cents"],
                ),
            )


def settle_obligation(db, store, row, amount, identity, request, today, stamp):
    """Record a partial or full settlement of a single obligation.

    Args:
        db: Open write transaction.
        store: Ledger store.
        row: Obligation record.
        amount: Settlement amount in cents.
        identity: Stable settlement ID.
        request: Canonical payment request.
        today: Date of simulated payment.
        stamp: Actual booking timestamp.

    Returns:
        Receipt for the new settlement.

    Raises:
        WalletError: The amount exceeds the amount still due.
    """
    if amount > row["amount_cents"] - row["settled_cents"]:
        raise WalletError("exceeds_outstanding")
    kind = row["kind"]
    if kind in ("work", "loan"):
        account = "asset:receivable:work" if kind == "work" else "asset:receivable:loan"
        entries = {CASH: amount, account: -amount}
        posting_kind = "work_received" if kind == "work" else "loan_repaid"
    else:
        account = "expense:bills" if kind == "bill" else "liability:payable"
        entries = {CASH: -amount, account: amount}
        posting_kind = "bill_paid" if kind == "bill" else "payable_paid"
    db.execute(
        "UPDATE obligations SET settled_cents=settled_cents+? WHERE id=?",
        (amount, row["id"]),
    )
    return store.post(
        db,
        identity,
        request,
        entries,
        kind=posting_kind,
        label=row["label"],
        category=None,
        effective_date=today.isoformat(),
        stamp=stamp,
        extra={
            "amount_cents": amount,
            "obligation_id": row["id"],
            "due_date": row["due_date"],
        },
    )


def save_for_goals(db, store, settings, today, stamp):
    """Attempt due savings transfers once and retry once after new income.

    Args:
        db: Open write transaction.
        store: Ledger store.
        settings: Wallet configuration.
        today: Target business date.
        stamp: Booking timestamp.
    """
    month = today.isoformat()[:7]
    income_count = db.execute(
        """SELECT COUNT(*) FROM transactions WHERE kind IN ('work_received','income','item_sold')
           AND effective_date BETWEEN ? AND ?""",
        (month + "-01", today.isoformat()),
    ).fetchone()[0]
    for goal in db.execute(
        "SELECT * FROM goals WHERE monthly_cents>0 ORDER BY id"
    ).fetchall():
        if (
            today < monthly_day(today, goal["due_day"])
            or today.isoformat() < goal["created_date"]
        ):
            continue
        previous = db.execute(
            "SELECT * FROM savings_attempts WHERE goal_id=? AND month=?",
            (goal["id"], month),
        ).fetchone()
        if previous and (
            previous["tx_id"]
            or previous["attempts"] >= 2
            or previous["income_count"] >= income_count
        ):
            continue
        account = "asset:savings:" + goal["id"]
        amount = min(
            goal["monthly_cents"], goal["target_cents"] - store.balance(db, account)
        )
        if amount <= 0:
            continue
        flexible = cash_limits(db, settings, today)[2]
        # Keep the second attempt available until new income is enough to fund it.
        if previous and amount > flexible:
            continue
        identity = None
        if amount <= flexible:
            identity = f"savings:{goal['id']}:{month}"
            store.post(
                db,
                identity,
                {"goal_id": goal["id"], "month": month},
                {CASH: -amount, account: amount},
                kind="savings_deposit",
                label=goal["label"],
                category=None,
                effective_date=today.isoformat(),
                stamp=stamp,
                extra={"amount_cents": amount},
            )
        db.execute(
            """INSERT INTO savings_attempts VALUES(?,?,?,1,?) ON CONFLICT(goal_id,month)
               DO UPDATE SET attempts=attempts+1, income_count=excluded.income_count,
                             tx_id=excluded.tx_id""",
            (goal["id"], month, income_count, identity),
        )


def month_report(db, month):
    """Calculate cash activity and period-end balances for one month.

    Args:
        db: Open wallet database connection.
        month: Month in YYYY-MM form.

    Returns:
        Structured cash activity and savings balance.
    """
    through = month + "-31"
    rows = db.execute(
        """SELECT t.kind, SUM(e.amount_cents) AS cents, COUNT(*) AS transactions
           FROM transactions t JOIN ledger_entries e USING(tx_id)
           WHERE e.account=? AND t.effective_date BETWEEN ? AND ?
           GROUP BY t.kind""",
        (CASH, month + "-01", through),
    ).fetchall()
    flows = {row["kind"]: row["cents"] for row in rows}
    saving = db.execute(
        """SELECT COALESCE(SUM(e.amount_cents),0)
           FROM ledger_entries e JOIN transactions t USING(tx_id)
           WHERE e.account LIKE 'asset:savings:%' AND t.effective_date<=?""",
        (through,),
    ).fetchone()[0]
    spend = -sum(
        flows.get(kind, 0)
        for kind in ("expense", "purchase", "refund", "bill_paid", "payable_paid")
    )
    income = sum(
        flows.get(kind, 0) for kind in ("work_received", "income", "item_sold")
    )
    purchased = db.execute(
        """SELECT COALESCE(SUM(q.quantity),0) FROM items i
           JOIN quotes q ON q.id=i.quote_id JOIN transactions t ON t.tx_id=i.tx_id
           WHERE t.effective_date BETWEEN ? AND ?""",
        (month + "-01", through),
    ).fetchone()[0]
    return {
        "month": month,
        "cash_cents": Store.balance(db, through=through),
        "savings_cents": saving,
        "income_cents": income,
        "spent_cents": spend,
        "refund_cents": flows.get("refund", 0),
        "purchased_units": purchased,
        "budgets": budget_rows(db, month),
        "cash_flows": flows,
    }


def advance(db, store, settings, today, stamp):
    """Catch up missed settlement dates in chronological order.

    Args:
        db: Open write transaction.
        store: Ledger store.
        settings: Wallet configuration.
        today: Target business date.
        stamp: Actual booking timestamp.

    Raises:
        WalletError: An operation tried to move the business clock backwards.
    """
    enabled = day(meta(db, "enabled_date"))
    start = day(meta(db, "last_settlement_date") or enabled.isoformat())
    if today < start:
        raise WalletError("wallet_date_in_past")
    create_periods(db, settings, enabled, start, today)
    schedule = {today}
    for row in db.execute(
        """SELECT DISTINCT due_date FROM obligations WHERE policy='simulated_auto'
           AND settled_cents<amount_cents AND due_date BETWEEN ? AND ?""",
        (start.isoformat(), today.isoformat()),
    ):
        schedule.add(day(row["due_date"]))
    for row in db.execute("SELECT due_day FROM goals WHERE monthly_cents>0"):
        for month in months(start, today):
            due = monthly_day(month, row["due_day"])
            if start <= due <= today:
                schedule.add(due)

    for current in sorted(schedule):
        rows = db.execute(
            """SELECT * FROM obligations WHERE policy='simulated_auto'
               AND settled_cents<amount_cents AND due_date<=?
               ORDER BY CASE kind WHEN 'work' THEN 0 WHEN 'bill' THEN 1 ELSE 2 END, due_date, id""",
            (current.isoformat(),),
        ).fetchall()
        for row in rows:
            amount = row["amount_cents"] - row["settled_cents"]
            if row["kind"] != "work" and store.balance(db) < amount:
                break
            identity = f"settlement:{row['id']}"
            settle_obligation(
                db, store, row, amount, identity, {"auto": row["id"]}, current, stamp
            )
        save_for_goals(db, store, settings, current, stamp)

    for month in months(start, today):
        key = month.isoformat()[:7]
        if key == today.isoformat()[:7]:
            break
        payload = month_report(db, key)
        serialized = dump(payload)
        previous = db.execute(
            "SELECT version,payload FROM monthly_reports WHERE month=?", (key,)
        ).fetchone()
        if previous and previous["payload"] == serialized:
            continue
        version = previous["version"] + 1 if previous else 1
        db.execute(
            """INSERT INTO monthly_reports VALUES(?,?,?) ON CONFLICT(month)
               DO UPDATE SET version=excluded.version, payload=excluded.payload""",
            (key, version, serialized),
        )
        store.event(
            db,
            f"monthly:{key}:{version}",
            "monthly_closed",
            stamp,
            {"month": key, "version": version, **json.loads(serialized)},
        )
    set_meta(db, "last_settlement_date", today.isoformat())


def spending_error(db, settings, today, amount, category, *, essential=False):
    """Return the current reason a spend cannot be committed, if any.

    Args:
        db: Open wallet database connection.
        settings: Wallet configuration.
        today: Evaluation date.
        amount: Amount in cents.
        category: Budget category.
        essential: Permit paying out of earmarked cash or beyond a category cap.

    Returns:
        A wallet error code or None.
    """
    cash, _, flexible = cash_limits(db, settings, today)
    if amount > cash:
        return "insufficient_cash"
    if essential:
        return None
    if amount > flexible:
        return "low_available"
    if category in CATEGORIES:
        row = budget_rows(db, today.isoformat()[:7]).get(category)
        remaining = (
            row["remaining_cents"] if row else settings.monthly_budgets[category]
        )
        if amount > remaining:
            return "category_limit"
    return None
