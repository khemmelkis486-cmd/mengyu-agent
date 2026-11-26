"""Quotes, purchase settlement, and inventory for the virtual wallet."""

from .settings import WalletError, integer, text
from .settlement import spending_error
from .store import CASH


def store_quote(db, quote, stamp):
    """Save an immutable quote linked to a shopping trip or wish.

    Args:
        db: Open write transaction.
        quote: Quote ID, scope, source, price and item metadata.
        stamp: Current UTC timestamp.

    Returns:
        The saved quote.

    Raises:
        WalletError: The quote is invalid or conflicts with an existing quote.
    """
    identity = text(quote.get("quote_id"))
    scope = quote.get("scope", "trip")
    source = text(quote.get("source_ref"))
    price_source = quote.get("price_source", "fictional_estimate")
    variant = quote.get("variant", "")
    if scope not in ("trip", "wish") or price_source not in (
        "fictional_estimate",
        "user_supplied",
        "external_reference",
    ):
        raise WalletError("invalid_quote")
    if not isinstance(variant, str) or len(variant) > 120:
        raise WalletError("invalid_quote")
    payload = {
        "id": identity,
        "scope": scope,
        "source_ref": source,
        "label": text(quote.get("label")),
        "variant": variant.strip(),
        "amount_cents": integer(quote.get("amount_cents"), minimum=1),
        "quantity": integer(quote.get("quantity", 1), minimum=1, maximum=100),
        "price_source": price_source,
        "expires_at": integer(quote.get("expires_at"), maximum=253402300799),
    }
    existing = db.execute("SELECT * FROM quotes WHERE id=?", (identity,)).fetchone()
    if existing:
        if any(existing[key] != value for key, value in payload.items()):
            raise WalletError("conflicting_quote")
        return dict(existing)
    if payload["expires_at"] <= stamp:
        raise WalletError("quote_expired")
    db.execute(
        """INSERT INTO quotes
           (id,scope,source_ref,label,variant,amount_cents,quantity,price_source,expires_at)
           VALUES(:id,:scope,:source_ref,:label,:variant,:amount_cents,:quantity,:price_source,:expires_at)""",
        payload,
    )
    return {**payload, "tx_id": None}


def purchase_terms(db, action, stamp):
    """Resolve eligible quotes for a trusted settlement request.

    Args:
        db: Open database connection.
        action: A checkout request with its matching trip or wish.
        stamp: Current UTC timestamp.

    Returns:
        The budget category, quote rows, and total cents.

    Raises:
        WalletError: A quote or checkout is invalid or cannot be purchased.
    """
    group = action.get("quote_ids")
    if not isinstance(group, list) or not 1 <= len(group) <= 20:
        raise WalletError("invalid_quote_ids")
    identities = [text(value) for value in group]
    if len(set(identities)) != len(identities):
        raise WalletError("invalid_quote_ids")
    source = text(action.get("source_ref"))
    scope = action.get("scope", "trip")
    category = action.get("category", "shopping")
    if scope not in ("trip", "wish") or category not in ("shopping", "gifts"):
        raise WalletError("invalid_checkout")
    quotes = []
    for identity in identities:
        row = db.execute("SELECT * FROM quotes WHERE id=?", (identity,)).fetchone()
        if not row:
            raise WalletError("quote_missing")
        if row["source_ref"] != source or row["scope"] != scope:
            raise WalletError("quote_mismatch")
        if row["tx_id"]:
            raise WalletError("quote_already_used")
        if row["expires_at"] <= stamp:
            raise WalletError("quote_expired")
        quotes.append(row)
    total = integer(
        sum(row["amount_cents"] * row["quantity"] for row in quotes), minimum=1
    )
    return category, quotes, total


def purchase(db, store, settings, identity, action, today, stamp):
    """Commit quotes, cash debits and item records as one purchase.

    Args:
        db: Open write transaction.
        store: Ledger store.
        settings: Wallet configuration.
        identity: Stable transaction identity.
        action: Trusted checkout payload.
        today: Business date.
        stamp: Booking timestamp.

    Returns:
        The purchase receipt.

    Raises:
        WalletError: Quotes or funds are unavailable for checkout.
    """
    category, quotes, total = purchase_terms(db, action, stamp)
    reason = spending_error(db, settings, today, total, category)
    if reason:
        raise WalletError(reason)
    items = [f"item:{row['id']}" for row in quotes]
    receipt = store.post(
        db,
        identity,
        action,
        {CASH: -total, f"expense:{category}": total},
        kind="purchase",
        label=", ".join(row["label"] for row in quotes)[:160],
        category=category,
        effective_date=today.isoformat(),
        stamp=stamp,
        extra={
            "amount_cents": total,
            "item_ids": items,
            "quote_ids": [row["id"] for row in quotes],
            "source_ref": action["source_ref"],
        },
    )
    db.executemany(
        "INSERT INTO items(id,quote_id,tx_id) VALUES(?,?,?)",
        ((item, row["id"], identity) for item, row in zip(items, quotes, strict=True)),
    )
    db.executemany(
        "UPDATE quotes SET tx_id=? WHERE id=?",
        ((identity, row["id"]) for row in quotes),
    )
    return receipt


def find_item(db, item_id):
    """Return inventory details and the originating purchase.

    Args:
        db: Open database connection.
        item_id: Wallet item ID.

    Returns:
        Inventory row including the item's quote metadata, or None.
    """
    return db.execute(
        """SELECT i.*, q.label, q.variant, q.amount_cents, q.quantity, q.source_ref, t.category
           FROM items i JOIN quotes q ON q.id=i.quote_id JOIN transactions t ON t.tx_id=i.tx_id
           WHERE i.id=?""",
        (item_id,),
    ).fetchone()


def dispose_item(db, store, identity, action, today, stamp):
    """Refund a purchase line or sell an owned item in the fictional economy.

    Args:
        db: Open write transaction.
        store: Ledger store.
        identity: Stable transaction identity.
        action: Trusted refund or sale payload.
        today: Business date.
        stamp: Booking timestamp.

    Returns:
        Receipt with the original purchase and item identifier.

    Raises:
        WalletError: The item has already left inventory or cannot be found.
    """
    item = find_item(db, text(action.get("item_id"), limit=150))
    if not item or item["status"] not in ("owned", "stored", "in_use"):
        raise WalletError("item_unavailable")
    if action["kind"] == "refund":
        amount = item["amount_cents"] * item["quantity"]
        account = f"expense:{item['category']}"
        category, status = item["category"], "returned"
        kind = "refund"
    else:
        amount = integer(action.get("amount_cents"), minimum=1)
        account = "income:sale"
        category, status = None, "sold"
        kind = "item_sold"
    db.execute(
        "UPDATE items SET status=?, updated_at=? WHERE id=?",
        (status, stamp, item["id"]),
    )
    return store.post(
        db,
        identity,
        action,
        {CASH: amount, account: -amount},
        kind=kind,
        label=item["label"],
        category=category,
        effective_date=today.isoformat(),
        stamp=stamp,
        extra={
            "amount_cents": amount,
            "item_id": item["id"],
            "purchase_tx_id": item["tx_id"],
        },
    )
