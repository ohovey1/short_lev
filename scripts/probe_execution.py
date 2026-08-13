"""Probe the three open questions in docs/execution-notes.md, on the paper account.

Run on the VPS with the venv python, one subcommand per question:

    python scripts/probe_execution.py open-orders [--client-id N]
    python scripts/probe_execution.py fills

Read-only by construction: every connection goes through broker.connect(),
which is readonly, and there is no placeOrder here -- this script observes and
must never submit. Spec 009 section 1; the answers get recorded under a dated
"Verified on paper" heading in docs/execution-notes.md, and the submit path is
not written until they are.

What each run answers:

  open-orders   Question 1: does reqAllOpenOrders() answer at all on a
                readonly connection with Gateway's Read-Only API enabled?
                The call is timed and bounded (30s), so "hangs" is an
                observable answer rather than a stuck terminal. The cached
                ib.openTrades() count is printed alongside to confirm the
                reconnaissance finding that it stays empty under readonly.

                Question 2: is orderRef echoed on rows queried from a
                clientId other than the submitter's? Run this twice with two
                different --client-id values while a working order carrying
                an Order Ref exists. Prerequisite, stated plainly: nothing in
                this repo may place that order, so it is hand-entered (a TWS
                ticket with the "Order ref" field filled arrives as clientId
                0), or the question waits for the first armed submission.
                orderRef prints as a repr so empty-vs-missing is visible.

  fills         Question 3: the execution-replay window across the nightly
                reset. Run once in the evening after any paper fill, note
                which executions appear, then run again after the ~23:45
                reset and compare. Both ib.fills() (the connect-time sync)
                and an explicit reqExecutions() are printed, timed, with the
                server clock, so the before/after outputs line up.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# A request that will not answer must become a printed answer, not a hung
# terminal: 0 (ib_async's default) means wait forever.
REQUEST_TIMEOUT_SECONDS = 30.0


def _timed(label, fn):
    """Run one API request, print how it ended and how long it took."""
    start = time.monotonic()
    try:
        result = fn()
    except Exception as exc:
        elapsed = time.monotonic() - start
        print(f"{label}: FAILED after {elapsed:.1f}s -- "
              f"{type(exc).__name__}: {exc}")
        return None
    elapsed = time.monotonic() - start
    print(f"{label}: answered in {elapsed:.1f}s")
    return result


def _connect(client_id):
    # Env override BEFORE the connect, so there is exactly one connect path in
    # the repo and this script cannot drift from how the monitor connects.
    if client_id is not None:
        os.environ["IB_CLIENT_ID"] = str(client_id)

    import broker
    ib = broker.connect()
    ib.RequestTimeout = REQUEST_TIMEOUT_SECONDS

    server_time = _timed("reqCurrentTime", ib.reqCurrentTime)
    print(f"server time: {server_time}")
    print()
    return ib


def probe_open_orders(client_id):
    ib = _connect(client_id)

    trades = _timed("reqAllOpenOrders", ib.reqAllOpenOrders)
    if trades is None:
        print("=> question 1 answer: the query did NOT answer on this "
              "connection. The orphan check is blind; stop and rethink.")
        return

    print(f"rows: {len(trades)}")
    for t in trades:
        o, s = t.order, t.orderStatus
        print(f"  {t.contract.symbol} {o.action} {o.totalQuantity:g} "
              f"{o.orderType} lmt={getattr(o, 'lmtPrice', None)} "
              f"status={s.status} permId={o.permId} orderId={o.orderId} "
              f"clientId={o.clientId} orderRef={o.orderRef!r}")

    # The reconnaissance says the cached accessor is empty under readonly
    # because connectAsync skips the open-orders sync. Confirm in the same
    # breath: if these two numbers ever agree while orders exist, that
    # finding is stale.
    print(f"cached ib.openTrades(): {len(ib.openTrades())} row(s)")
    print()
    print("=> question 1: answered above (answered/failed, and in how long).")
    print("=> question 2: read orderRef off each row. Run again with a "
          "different --client-id and compare.")


def probe_fills(client_id):
    ib = _connect(client_id)

    def show(label, fills):
        if fills is None:
            return
        print(f"{label}: {len(fills)} row(s)")
        for f in fills:
            e = f.execution
            print(f"  {e.time} {f.contract.symbol} {e.side} {e.shares:g} "
                  f"@ {e.price} execId={e.execId} permId={e.permId} "
                  f"orderRef={e.orderRef!r}")

    show("ib.fills() [connect-time sync]", _timed("ib.fills", ib.fills))
    print()
    show("reqExecutions() [explicit]", _timed("reqExecutions", ib.reqExecutions))
    print()
    print("=> question 3: run this before and after the nightly reset; the "
          "executions still listed afterwards ARE the replay window.")


def main():
    parser = argparse.ArgumentParser(
        description="Read-only probes for spec 009's three open questions.")
    parser.add_argument("probe", choices=["open-orders", "fills"])
    parser.add_argument("--client-id", type=int, default=None,
                        help="override IB_CLIENT_ID for this run "
                             "(question 2 wants two runs, two ids)")
    args = parser.parse_args()

    import logging
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)-7s %(name)s: %(message)s")

    if args.probe == "open-orders":
        probe_open_orders(args.client_id)
    else:
        probe_fills(args.client_id)


if __name__ == "__main__":
    main()
