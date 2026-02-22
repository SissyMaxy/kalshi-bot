"""
Safe Order Gateway — ALL order placements must go through place_order_safe().

Guards:
1. Exit orders: never sell more contracts than the original trade's contract count.
2. Dollar cap: no single order can exceed 10% of bankroll OR $5, whichever is smaller.
3. Logs CRITICAL and returns None if any check fails.
"""

import logging

log = logging.getLogger("kalshi-bot")


def place_order_safe(client, db, ticker, action, side, quantity, price_cents,
                     bankroll, trade_id=None, order_type="limit", dry_run=False):
    """
    Central gateway for ALL Kalshi order placements.

    Args:
        client:      KalshiClient instance
        db:          Database instance
        ticker:      market ticker
        action:      "buy" or "sell"
        side:        "yes" or "no"
        quantity:    number of contracts
        price_cents: price in cents (1-99)
        bankroll:    current cash balance
        trade_id:    DB trade ID (required for sell/exit orders)
        order_type:  "limit" or "market"
        dry_run:     if True, skip API call, return fake result

    Returns:
        Kalshi API response dict, or None if blocked.
    """

    ledger_action = "EXIT" if action == "sell" else "OPEN"
    was_capped = False
    prefix = "DRY RUN " if dry_run else ""

    # ── AUDIT: Log every call on entry ───────────────────────────────
    if action == "buy":
        if side == "yes":
            cost_est = (price_cents / 100.0) * quantity
        else:
            cost_est = ((100 - price_cents) / 100.0) * quantity
        log.info(
            f"{prefix}ORDER GATEWAY: BUY {side.upper()} {quantity}x {ticker} "
            f"@ {price_cents}c | bankroll=${bankroll:.2f} | "
            f"dollar_cap=${min(bankroll * 0.10, 5.0):.2f} | "
            f"cost=${cost_est:.2f}"
        )
    else:
        log.info(
            f"{prefix}ORDER GATEWAY: SELL {side.upper()} {quantity}x {ticker} "
            f"@ {price_cents}c | trade_id={trade_id} | "
            f"requested={quantity}"
        )

    # ── GUARD 1: Exit contract cap ────────────────────────────────────
    original_contracts = None
    if action == "sell":
        if trade_id is None:
            log.critical(f"BLOCKED: sell order on {ticker} has no trade_id — "
                         f"cannot verify contract count")
            db.log_to_ledger(ledger_action, ticker, side, quantity, price_cents,
                             None, "BLOCKED")
            return None

        trade = db.get_trade_by_id(trade_id)
        if not trade:
            log.critical(f"BLOCKED: sell order on {ticker} references "
                         f"trade #{trade_id} which does not exist")
            db.log_to_ledger(ledger_action, ticker, side, quantity, price_cents,
                             None, "BLOCKED")
            return None

        original_contracts = trade["original_contracts"] or trade["contracts"]
        if quantity > original_contracts:
            log.critical(
                f"BLOCKED: exit on {ticker} tried to sell {quantity} contracts "
                f"but original trade #{trade_id} only ordered {original_contracts}. "
                f"Capping to {original_contracts}."
            )
            quantity = original_contracts

        # Check if we already placed an exit order for this trade
        if trade["status"] == "exiting":
            log.critical(
                f"BLOCKED: trade #{trade_id} {ticker} is already exiting — "
                f"refusing duplicate exit order"
            )
            db.log_to_ledger(ledger_action, ticker, side, quantity, price_cents,
                             None, "BLOCKED")
            return None

    # ── GUARD 2: Dollar cap ───────────────────────────────────────────
    if side == "yes":
        cost_per_contract = price_cents / 100.0
    else:
        cost_per_contract = (100 - price_cents) / 100.0

    total_cost = cost_per_contract * quantity
    bankroll_limit = bankroll * 0.10
    hard_cap = min(bankroll_limit, 8.0)

    # Only enforce dollar cap on BUY orders (sells return money, not spend it)
    if action == "buy" and total_cost > hard_cap and hard_cap > 0:
        max_contracts = int(hard_cap / cost_per_contract) if cost_per_contract > 0 else 0
        if max_contracts <= 0:
            log.critical(
                f"BLOCKED: buy {quantity}x {ticker} @ {price_cents}c "
                f"costs ${total_cost:.2f} — exceeds cap ${hard_cap:.2f} "
                f"(10% of ${bankroll:.2f} or $5). Cannot afford even 1 contract."
            )
            db.log_to_ledger(ledger_action, ticker, side, quantity, price_cents,
                             None, "BLOCKED")
            return None
        log.critical(
            f"CAPPED: buy {ticker} from {quantity}x to {max_contracts}x — "
            f"${total_cost:.2f} exceeds cap ${hard_cap:.2f} "
            f"(10% of ${bankroll:.2f} or $5)"
        )
        was_capped = True
        quantity = max_contracts
        total_cost = cost_per_contract * quantity

    # ── GUARD 3: Sanity checks ────────────────────────────────────────
    if quantity <= 0:
        log.critical(f"BLOCKED: {action} {ticker} with quantity={quantity}")
        db.log_to_ledger(ledger_action, ticker, side, quantity, price_cents,
                         None, "BLOCKED")
        return None

    if price_cents < 1 or price_cents > 99:
        log.critical(f"BLOCKED: {action} {ticker} with price_cents={price_cents}")
        db.log_to_ledger(ledger_action, ticker, side, quantity, price_cents,
                         None, "BLOCKED")
        return None

    # ── AUDIT: Log PASSED with final values ──────────────────────────
    if action == "sell":
        log.info(
            f"{prefix}ORDER GATEWAY: SELL {side.upper()} {quantity}x {ticker} "
            f"@ {price_cents}c | trade_id={trade_id} | "
            f"original={original_contracts} | requested={quantity} | PASSED"
        )
    else:
        log.info(
            f"{prefix}ORDER GATEWAY: BUY {side.upper()} {quantity}x {ticker} "
            f"@ {price_cents}c | bankroll=${bankroll:.2f} | "
            f"dollar_cap=${hard_cap:.2f} | cost=${total_cost:.2f} | PASSED"
        )

    # ── EXECUTE ───────────────────────────────────────────────────────
    body = {
        "ticker": ticker,
        "action": action,
        "side": side,
        "type": order_type,
        "count": quantity,
    }
    if order_type == "limit":
        body["yes_price"] = price_cents if side == "yes" else (100 - price_cents)

    if dry_run:
        log.info(f"DRY RUN: would send {body}")
        order_id = "DRY_RUN"
        ledger_result = "CAPPED" if was_capped else "PASSED"
        db.log_to_ledger(ledger_action, ticker, side, quantity, price_cents,
                         order_id, ledger_result)
        return {"order": {"order_id": "DRY_RUN", "status": "dry_run"}}

    resp = client._auth_request("POST", "/portfolio/orders", json=body)
    order_id = resp.get("order", {}).get("order_id", "unknown") if resp else "unknown"
    ledger_result = "CAPPED" if was_capped else "PASSED"
    db.log_to_ledger(ledger_action, ticker, side, quantity, price_cents,
                     order_id, ledger_result)
    return resp
