"""IBKR reads. Connect, read one pair's position, hand back a PositionState.

No decisions, no loop, no file I/O -- that is monitor.py's job. This module is
the seam between the broker and the pure decision layer. It shares knowledge
of ib_async with exactly one other module: execution.py, the submit-side seam
(spec 009), where every wire-touching line lives.

Everything HERE is READ-only. Order submission exists only in
execution._submit, reached solely through dispatch's four default-off rails.
Until the arming steps of spec 009 section 8 are taken, IB Gateway runs with
Read-Only API enabled and connect() passes readonly=True, so a submission is
rejected at the wire -- a property of the broker, not a promise in a
docstring.

The governing bias is fail loudly over guess. Every path that could hand back a
plausible-looking but wrong number logs and returns None instead. The specific
failure this is written against: IBKR reports a short as a negative position, so
an unchecked abs() on a leg that was accidentally opened LONG produces a state
that passes every downstream sanity check and is completely wrong. Assert the
sign, then take the magnitude -- never the other way round.
"""

import logging
import os

from dotenv import load_dotenv
from ib_async import IB

import config
import decision
import execution

load_dotenv()

log = logging.getLogger(__name__)

# Every ib_async example uses client id 0 or 1. Reconnecting with an id still
# held by a half-dead session fails, so default to something we own and let the
# env override it.
DEFAULT_CLIENT_ID = 11

# Account values arrive as currency-tagged strings; filter on this rather than
# taking the first match, or a multi-currency account silently reports the wrong
# number in the wrong denomination.
BASE_CURRENCY = "USD"

# ib.portfolio() frequently returns an empty list immediately after connect()
# even on a perfectly healthy account. Wait for it rather than reporting a false
# "no position found" on the first check.
PORTFOLIO_WAIT_SECONDS = 10.0
PORTFOLIO_POLL_SECONDS = 0.25


def connect():
    """Connect to IB Gateway and return the IB handle.

    Logs the port and account on connect. IB_PORT is the only thing separating
    paper (4002) from live (4001), so a misconfigured deploy has to announce
    itself on line one -- not after a week of reading paper numbers in the
    belief they were live.
    """
    host = os.environ.get("IB_HOST", "127.0.0.1")
    port = int(os.environ.get("IB_PORT", 4002))
    client_id = int(os.environ.get("IB_CLIENT_ID", DEFAULT_CLIENT_ID))

    # Spec 009 section 2: readonly is DERIVED from the same env the
    # submission gate reads -- never a bare literal False. With
    # EXECUTION_ENABLED anything but "1" the socket itself is read-only, so
    # through the dry-run day an accidental submit is rejected at the wire
    # before any rail is even consulted. A free rail; kept as long as
    # possible (section 8 step 3 is where it comes off, deliberately).
    readonly = not execution.execution_enabled()

    ib = IB()
    ib.connect(host, port, clientId=client_id, readonly=readonly)

    accounts = ib.managedAccounts()
    log.info(
        "connected: host=%s port=%s clientId=%s readonly=%s accounts=%s (%s)",
        host, port, client_id, readonly, ",".join(accounts) or "none",
        "PAPER" if port == 4002 else "LIVE -- verify this is intended",
    )

    _wait_for_portfolio(ib)
    return ib


def disconnect(ib):
    """Disconnect if still connected. Safe to call on an already-dead handle."""
    if ib is not None and ib.isConnected():
        ib.disconnect()
        log.info("disconnected")


def _wait_for_portfolio(ib):
    """Block until account data populates, or the timeout expires.

    An empty portfolio right after connect is a not-ready signal, not the
    no-position case. Distinguishing them is the whole point: treating
    not-ready as no-position makes a healthy account look empty on the first
    check of every session.

    ib.sleep, never time.sleep -- see the module note in monitor.py.
    """
    waited = 0.0
    while waited < PORTFOLIO_WAIT_SECONDS:
        if ib.portfolio() and ib.accountValues():
            return True
        ib.sleep(PORTFOLIO_POLL_SECONDS)
        waited += PORTFOLIO_POLL_SECONDS

    log.warning(
        "portfolio still empty after %.0fs. Either the account genuinely holds "
        "no positions or account data has not arrived -- these look identical "
        "here, so treat the next read as unconfirmed.",
        PORTFOLIO_WAIT_SECONDS,
    )
    return False


def _account_value(ib, tag, account=None):
    """Return one account value as a float, filtered to the base currency.

    Returns None if the tag is absent rather than defaulting to zero: a missing
    NetLiquidation silently read as 0.0 would trip the drawdown stop and the
    margin de-risk at once, on an account that is perfectly fine.
    """
    for av in ib.accountValues():
        if av.tag != tag or av.currency != BASE_CURRENCY:
            continue
        if account and av.account != account:
            continue
        try:
            return float(av.value)
        except (TypeError, ValueError):
            log.error("account value %s is not numeric: %r", tag, av.value)
            return None

    log.error("account value %s not found in %s", tag, BASE_CURRENCY)
    return None


def net_liquidation(ib):
    """Return NetLiquidation as a float, or None if unreadable.

    Exposed separately so the caller can resolve peak_equity before building a
    PositionState -- the state is frozen, so its peak has to be right the first
    time.
    """
    return _account_value(ib, "NetLiquidation", os.environ.get("IB_ACCOUNT") or None)


def _leg(ib, ticker, account=None):
    """Return the portfolio item for ticker, or None if not held."""
    matches = [
        item for item in ib.portfolio()
        if item.contract.symbol == ticker
        and (not account or item.account == account)
    ]
    if not matches:
        return None
    if len(matches) > 1:
        # Same symbol on two exchanges or in two currencies. Summing them would
        # be a guess about which is the intended leg.
        log.error(
            "%s matches %d portfolio entries; refusing to guess which is the leg",
            ticker, len(matches),
        )
        return None
    return matches[0]


def read_position(ib, pair_key, target, peak_equity):
    """Build a decision.PositionState for one pair, or return None.

    None means "do not decide on this" -- a missing leg, an inverted leg, or an
    unreadable account value. The caller logs and keeps polling; it must never
    substitute a default and evaluate anyway.

    target and peak_equity are passed in. This module derives neither: target is
    the caller's job (from base_capital, every cycle) and peak_equity is the
    caller's persisted state.
    """
    pair = config.PAIRS[pair_key]
    lev_ticker = pair["leveraged_ticker"]
    und_ticker = pair["underlying_ticker"]
    account = os.environ.get("IB_ACCOUNT") or None

    lev_item = _leg(ib, lev_ticker, account)
    und_item = _leg(ib, und_ticker, account)

    if lev_item is None or und_item is None:
        log.error(
            "leg check failed: %s=%s %s=%s. Need both legs held; not deciding.",
            lev_ticker, "held" if lev_item else "MISSING",
            und_ticker, "held" if und_item else "MISSING",
        )
        return None

    # Sign checks before any abs(). The share count is the unambiguous signal --
    # marketValue can be negative for reasons other than a short. >= 0 rather
    # than > 0 so a flat leg fails here too instead of passing through as a $0
    # notional and quietly halving the position we think we hold.
    if lev_item.position >= 0:
        log.error(
            "leg check failed: %s position is %+.0f shares, expected SHORT "
            "(negative). A long leveraged leg would produce a plausible and "
            "completely wrong decision. Not deciding.",
            lev_ticker, lev_item.position,
        )
        return None

    if und_item.position <= 0:
        log.error(
            "leg check failed: %s position is %+.0f shares, expected LONG "
            "(positive). Not deciding.",
            und_ticker, und_item.position,
        )
        return None

    # Sign asserted above, so the magnitude is now meaningful. evaluate()
    # expects short_notional as a positive number.
    short_notional = abs(lev_item.marketValue)
    long_notional = abs(und_item.marketValue)

    account_equity = _account_value(ib, "NetLiquidation", account)
    margin_required = _account_value(ib, "MaintMarginReq", account)
    if account_equity is None or margin_required is None:
        log.error("account values unreadable; not deciding this cycle")
        return None

    # How far off is the modeled margin from what IBKR actually charges. Logged
    # every check, tripped or not -- this comparison is what caught both the
    # rate and the shape errors that spec 005 corrected, so it stays on.
    # Two-term now: the old short-only form could not move when only the long
    # leg changed, which is precisely how the shape error stayed invisible.
    modeled_margin = (pair["long_rate"] * long_notional
                      + pair["short_rate"] * short_notional)
    ratio = margin_required / modeled_margin if modeled_margin else float("nan")
    log.info(
        "margin: ibkr_maint=%.2f modeled=%.2f "
        "(long %.2f x %.2f + short %.2f x %.2f) ratio=%.3f",
        margin_required, modeled_margin,
        pair["long_rate"], long_notional,
        pair["short_rate"], short_notional, ratio,
    )

    return decision.PositionState(
        short_notional=short_notional,
        long_notional=long_notional,
        leverage=pair["leverage"],
        target=target,
        account_equity=account_equity,
        peak_equity=peak_equity,
        margin_required=margin_required,
        margin_multiplier=config.margin_multiplier(pair),
    )


def open_orders(ib):
    """Every working order the API can see, summarised for the orphan check.

    A read, not an action -- safe in Read-Only API mode. reqAllOpenOrders
    covers orders from every API client id AND tickets entered by hand in the
    TWS/Gateway UI, which is exactly the population that matters: this repo
    submits nothing, so ANY working order on the account is one the monitor
    cannot account for.

    Returns a list of plain-dict summaries, or None when the query itself
    failed -- the caller must treat None as "unknown", not as "none found".
    """
    try:
        trades = ib.reqAllOpenOrders()
    except Exception as exc:
        log.error("open-order query failed: %s: %s -- orphan state UNKNOWN",
                  type(exc).__name__, exc)
        return None

    return [{
        "symbol": t.contract.symbol,
        "action": t.order.action,
        "quantity": t.order.totalQuantity,
        "order_type": t.order.orderType,
        "limit_price": getattr(t.order, "lmtPrice", None),
        "status": t.orderStatus.status,
        "perm_id": t.order.permId,
        "client_id": t.order.clientId,
        "order_ref": t.order.orderRef or None,
    } for t in trades]


def fills(ib):
    """Every execution the API reports for this session, as plain dicts.

    A read, for the reconcile spine. ib.fills() is populated by the
    connect-time executions sync, which ib_async issues on EVERY connect
    regardless of the readonly flag -- so fills that happened while
    disconnected arrive here as data even though their events never fired
    (execution-notes section 3). exec_id is the dedup key: replays and
    event/reconcile overlap both deliver the same execution twice.

    Returns None when the read itself failed -- the caller must treat None
    as "unknown", never as "no fills".
    """
    try:
        raw = ib.fills()
    except Exception as exc:
        log.error("fills read failed: %s: %s -- fill state UNKNOWN",
                  type(exc).__name__, exc)
        return None

    return [{
        "exec_id": f.execution.execId,
        "time": str(f.execution.time),
        "symbol": f.contract.symbol,
        "side": f.execution.side,
        "shares": f.execution.shares,
        "price": f.execution.price,
        "perm_id": f.execution.permId,
        "client_id": f.execution.clientId,
        "order_ref": f.execution.orderRef or None,
    } for f in raw]


def leg_details(ib, pair_key):
    """Return {ticker: {price, shares, avg_cost}} for both legs.

    Separate from read_position because it is presentation, not decision input:
    the decision is made entirely in notional dollars, and these fields only
    matter when converting a dollar delta into "buy 43 TSLA" or reporting the
    legs over Telegram. shares is the positive magnitude -- the sign was
    already asserted (or the leg rejected) by read_position.

    This is also how the poller sees share counts and marks: they ride out in
    the widened checks.jsonl record rather than the poller asking IBKR, which
    is what keeps exactly one process holding a connection.
    """
    pair = config.PAIRS[pair_key]
    account = os.environ.get("IB_ACCOUNT") or None
    details = {}
    for ticker in (pair["leveraged_ticker"], pair["underlying_ticker"]):
        item = _leg(ib, ticker, account)
        if item is None:
            continue
        details[ticker] = {
            "price": item.marketPrice or None,
            "shares": abs(item.position),
            "avg_cost": getattr(item, "averageCost", None),
        }
    return details
