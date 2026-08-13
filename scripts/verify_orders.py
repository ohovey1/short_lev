"""Verify orders.py offline. Run from the project root:

    .venv/Scripts/python.exe scripts/verify_orders.py

This is the gate that matters most in spec 008 and it needs no market: the
limit arithmetic is pure, so every boundary formula is hand-checkable here.
Decisions come from the real decision.evaluate() rather than being constructed
by hand wherever possible, so a drift between the two files cannot hide.

Checks:
  a. Foil decay, short too LARGE: the boundary limit is below the mark (a
     passive buy), and held_shares x limit lands exactly on target x (1+band).
  b. Foil decay, short too SMALL: boundary above the mark (a passive sell),
     landing exactly on target x (1-band).
  c. Long-short, net delta positive: the long-leg boundary reproduces
     |net_delta| = band x target exactly, with the short frozen at its mark.
  d. Long-short, net delta negative: same, other side.
  e. Gate 10: ticket and alert state the same share counts, and every dollar
     figure on each derives from the rounded count at its own price.
  f. Gate 11: margin de-risk prices through the touch (buy above mark, sell
     below), never at a band boundary.
  g. Drawdown stop closes both legs marketably.
  h. A long-short ticket touches only the long leg.
  i. A sub-one-share leg renders as an explicit no-trade, not a $0 order.
  j. orders.py is pure: no imports at all -- no broker, no requests, no I/O.
  k. The rendered proposal is self-consistent and says nothing was sent.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import config
import decision
import monitor
import orders


def check(label, ok, detail):
    print(f"{label}")
    print(f"  {detail}")
    print(f"  => {'PASS' if ok else 'FAIL'}\n")
    return ok


PAIR = config.PAIRS["TSLL"]
PARAMS = decision.BandParams(
    long_short_band=0.10, foil_decay_band=0.10,
    capital_utilization=0.75, drawdown_stop=0.10, margin_derisk=True,
)
TARGET = 75000 / 11  # the worked example's 6,818.18...


def make_state(short, long_, equity=10000.0, peak=10000.0, maint=7000.0):
    return decision.PositionState(
        short_notional=short, long_notional=long_, leverage=PAIR["leverage"],
        target=TARGET, account_equity=equity, peak_equity=peak,
        margin_required=maint,
        margin_multiplier=config.margin_multiplier(PAIR),
    )


def propose(state, marks, held):
    d = decision.evaluate(state, PARAMS)
    return d, orders.build_proposal(state, d, PARAMS, marks, held, PAIR)


def leg_by_ticker(proposal, ticker):
    return next(leg for leg in proposal["legs"] if leg["ticker"] == ticker)


def check_a():
    """Short grew to 7,600 against a 6,818.18 target (band edge 7,500). The
    fix is buying back shares, and the boundary must sit BELOW the mark: we
    only trade if price comes back to us."""
    held = {"TSLL": 800, "TSLA": 40}
    marks = {"TSLL": 9.50, "TSLA": 340.0}
    state = make_state(short=7600.0, long_=13600.0)
    d, proposal = propose(state, marks, held)

    leg = leg_by_ticker(proposal, "TSLL")
    edge = TARGET * (1 + PARAMS.foil_decay_band)          # 7,500 exactly
    landed = held["TSLL"] * leg["limit_exact"]
    ok = (
        d.trigger == "foil decay band"
        and leg["side"] == "BUY TO COVER"
        and leg["limit_exact"] < marks["TSLL"]
        and abs(landed - edge) < 1e-9
        and proposal["style"] == orders.PASSIVE
    )
    return check("a. foil decay (short too large): passive buy at the exact edge",
                 ok, f"trigger={d.trigger}, {leg['side']} limit_exact="
                     f"{leg['limit_exact']:.6f} vs mark {marks['TSLL']}, "
                     f"held x limit = {landed:.6f} vs edge {edge:.6f}")


def check_b():
    """Short shrank to 6,000 (band edge 6,136.36): sell more, above the mark."""
    held = {"TSLL": 800, "TSLA": 40}
    marks = {"TSLL": 7.50, "TSLA": 340.0}
    state = make_state(short=6000.0, long_=13600.0)
    d, proposal = propose(state, marks, held)

    leg = leg_by_ticker(proposal, "TSLL")
    edge = TARGET * (1 - PARAMS.foil_decay_band)
    landed = held["TSLL"] * leg["limit_exact"]
    ok = (
        d.trigger == "foil decay band"
        and leg["side"] == "SELL (short more)"
        and leg["limit_exact"] > marks["TSLL"]
        and abs(landed - edge) < 1e-9
    )
    return check("b. foil decay (short too small): passive sell at the exact edge",
                 ok, f"trigger={d.trigger}, {leg['side']} limit_exact="
                     f"{leg['limit_exact']:.6f} vs mark {marks['TSLL']}, "
                     f"held x limit = {landed:.6f} vs edge {edge:.6f}")


def check_c():
    """Long drifted to 14,400 against 2 x 6,800 = 13,600: net delta +800 with a
    681.82 band. At the derived long price the net delta must equal the band
    exactly, with the short frozen at its current mark."""
    held = {"TSLL": 800, "TSLA": 40}
    marks = {"TSLL": 8.50, "TSLA": 360.0}
    state = make_state(short=6800.0, long_=14400.0)
    d, proposal = propose(state, marks, held)

    leg = leg_by_ticker(proposal, "TSLA")
    band_dollars = PARAMS.long_short_band * TARGET
    net_at_limit = held["TSLA"] * leg["limit_exact"] - state.leverage * state.short_notional
    ok = (
        d.trigger == "long-short band"
        and leg["side"] == "SELL"
        and abs(net_at_limit - band_dollars) < 1e-9
    )
    return check("c. long-short (net delta +): boundary reproduces the band exactly",
                 ok, f"trigger={d.trigger}, {leg['side']} limit_exact="
                     f"{leg['limit_exact']:.6f}, net delta at limit "
                     f"{net_at_limit:.6f} vs band {band_dollars:.6f}")


def check_d():
    """Other side: long fell to 12,800, net delta -800."""
    held = {"TSLL": 800, "TSLA": 40}
    marks = {"TSLL": 8.50, "TSLA": 320.0}
    state = make_state(short=6800.0, long_=12800.0)
    d, proposal = propose(state, marks, held)

    leg = leg_by_ticker(proposal, "TSLA")
    band_dollars = PARAMS.long_short_band * TARGET
    net_at_limit = held["TSLA"] * leg["limit_exact"] - state.leverage * state.short_notional
    ok = (
        d.trigger == "long-short band"
        and leg["side"] == "BUY"
        and abs(net_at_limit + band_dollars) < 1e-9
    )
    return check("d. long-short (net delta -): boundary reproduces the band exactly",
                 ok, f"trigger={d.trigger}, {leg['side']} limit_exact="
                     f"{leg['limit_exact']:.6f}, net delta at limit "
                     f"{net_at_limit:.6f} vs -band {-band_dollars:.6f}")


def _shares_from_trade_line(line):
    head = line.split("\n")[0]
    return int(head.split(" shares ")[0].split()[-1].replace(",", ""))


def _amount_from_trade_line(line):
    head = line.split("\n")[0]
    return float(head.split("= $")[1].replace(",", ""))


def check_e():
    """Gate 10. The alert prices at the mark and the ticket at the limit, so
    the two dollar amounts differ by construction -- what must agree is the
    SHARE COUNT, with every dollar figure derived from that rounded count at
    its own price. If the counts disagree, one artifact is lying about what
    will be sent."""
    held = {"TSLL": 800, "TSLA": 38}
    marks = {"TSLL": 9.50, "TSLA": 340.0}
    # Long at 12,900 so BOTH legs trade whole shares on the reset: the short
    # covers back to target and the long buys up to 2 x target.
    state = make_state(short=7600.0, long_=12900.0)
    d, proposal = propose(state, marks, held)

    failures = []
    for ticker, current in (("TSLL", state.short_notional),
                            ("TSLA", state.long_notional)):
        new = (d.new_short_notional if ticker == "TSLL"
               else d.new_long_notional)
        line = monitor.format_trade_line(
            ticker, current, new, marks[ticker], short_leg=(ticker == "TSLL"))
        alert_shares = _shares_from_trade_line(line)
        alert_amount = _amount_from_trade_line(line)
        leg = leg_by_ticker(proposal, ticker)

        if leg["shares"] != alert_shares:
            failures.append(f"{ticker}: ticket {leg['shares']} sh, "
                            f"alert {alert_shares} sh")
        if abs(leg["shares"] * leg["limit"] - leg["notional"]) > 0.005:
            failures.append(f"{ticker}: ticket {leg['shares']} x "
                            f"{leg['limit']} != {leg['notional']}")
        if abs(alert_shares * marks[ticker] - alert_amount) > 0.005:
            failures.append(f"{ticker}: alert {alert_shares} x "
                            f"{marks[ticker]} != {alert_amount}")

    ok = not failures
    lev_leg = leg_by_ticker(proposal, "TSLL")
    return check("e. gate 10: ticket and alert agree on shares; dollars derive "
                 "from the rounded count",
                 ok, "; ".join(failures) if failures else
                 f"TSLL {lev_leg['shares']} sh on both; ticket "
                 f"{lev_leg['shares']} x {lev_leg['limit']} = {lev_leg['notional']}")


def check_f():
    """Gate 11. Cushion negative -> margin de-risk. The scenario a passive
    limit fails in is exactly the one a de-risk exists for, so both legs must
    be priced THROUGH the touch, not at a band boundary."""
    held = {"TSLL": 800, "TSLA": 40}
    marks = {"TSLL": 8.50, "TSLA": 340.0}
    state = make_state(short=6800.0, long_=13600.0,
                       equity=9500.0, peak=10000.0, maint=9800.0)
    d, proposal = propose(state, marks, held)

    lev_leg = leg_by_ticker(proposal, "TSLL")     # shrinking a short = buying
    und_leg = leg_by_ticker(proposal, "TSLA")     # shrinking a long = selling
    off = orders.DEFAULT_MARKETABLE_OFFSET
    ok = (
        d.trigger == "margin de-risk"
        and proposal["style"] == orders.MARKETABLE
        and lev_leg["side"] == "BUY TO COVER"
        and abs(lev_leg["limit_exact"] - marks["TSLL"] * (1 + off)) < 1e-9
        and und_leg["side"] == "SELL"
        and abs(und_leg["limit_exact"] - marks["TSLA"] * (1 - off)) < 1e-9
    )
    return check("f. gate 11: margin de-risk is a marketable limit through the touch",
                 ok, f"trigger={d.trigger}, style={proposal['style']}, "
                     f"buy limit {lev_leg['limit_exact']:.4f} vs mark "
                     f"{marks['TSLL']} (+{off * 1e4:.0f}bp), sell limit "
                     f"{und_leg['limit_exact']:.4f} vs mark {marks['TSLA']} "
                     f"(-{off * 1e4:.0f}bp)")


def check_g():
    """A drawdown stop closes both legs; nothing about a close is passive."""
    held = {"TSLL": 800, "TSLA": 40}
    marks = {"TSLL": 8.50, "TSLA": 340.0}
    state = make_state(short=6800.0, long_=13600.0,
                       equity=8900.0, peak=10000.0, maint=7000.0)
    d, proposal = propose(state, marks, held)

    lev_leg = leg_by_ticker(proposal, "TSLL")
    und_leg = leg_by_ticker(proposal, "TSLA")
    ok = (
        d.trigger == "drawdown stop"
        and proposal["style"] == orders.MARKETABLE
        and lev_leg["side"] == "BUY TO COVER" and lev_leg["shares"] == 800
        and und_leg["side"] == "SELL" and und_leg["shares"] == 40
        and lev_leg["limit_exact"] > marks["TSLL"]
        and und_leg["limit_exact"] < marks["TSLA"]
    )
    return check("g. drawdown stop closes both legs marketably",
                 ok, f"trigger={d.trigger}, TSLL {lev_leg['side']} "
                     f"{lev_leg['shares']} sh (held 800), TSLA "
                     f"{und_leg['side']} {und_leg['shares']} sh (held 40)")


def check_h():
    """The long-short rule moves the long leg only. A ticket that also touched
    the short would be re-deriving the rebalance rule, which is decision.py's
    and only decision.py's."""
    held = {"TSLL": 800, "TSLA": 40}
    marks = {"TSLL": 8.50, "TSLA": 360.0}
    state = make_state(short=6800.0, long_=14400.0)
    d, proposal = propose(state, marks, held)

    tickers = [leg["ticker"] for leg in proposal["legs"]]
    ok = d.trigger == "long-short band" and tickers == ["TSLA"]
    return check("h. a long-short ticket touches only the long leg",
                 ok, f"trigger={d.trigger}, legs={tickers}")


def check_i():
    """A sub-one-share requirement must say 'no trade', not render a 0-share
    order that reads as actionable. Constructed Decision: a genuine band trip
    cannot produce a delta this small."""
    held = {"TSLL": 800, "TSLA": 40}
    marks = {"TSLL": 8.50, "TSLA": 340.0}
    state = make_state(short=6800.0, long_=13600.0)
    d = decision.Decision(
        trigger="foil decay band", terminal=False,
        new_short_notional=state.short_notional + 3.0,
        new_long_notional=state.long_notional,
        new_target=state.target, net_delta=0.0, margin_cushion=3000.0,
    )
    proposal = orders.build_proposal(state, d, PARAMS, marks, held, PAIR)
    leg = leg_by_ticker(proposal, "TSLL")
    text = orders.format_proposal(proposal)
    ok = (not leg["tradeable"] and leg["shares"] == 0
          and "no trade" in text and "@ limit" not in text)
    return check("i. a sub-one-share leg renders as an explicit no-trade",
                 ok, f"tradeable={leg['tradeable']}, note={leg.get('note')!r}")


def check_j():
    """Purity is the testability guarantee: no imports at all means no broker,
    no requests, no I/O, and nothing to mock."""
    path = os.path.join(os.path.dirname(__file__), "..", "src", "orders.py")
    with open(path, encoding="utf-8") as f:
        source = f.read()
    offenders = [
        line.strip() for line in source.splitlines()
        if line.startswith("import ") or line.startswith("from ")
    ]
    ok = not offenders and "placeOrder" not in source
    return check("j. orders.py imports nothing and contains no order submission",
                 ok, f"import lines: {offenders or 'none'}, "
                     f"placeOrder absent={'placeOrder' not in source}")


def check_k():
    """The rendered proposal must be arithmetically self-consistent (a phone
    reader multiplies the two numbers shown) and unambiguous that nothing was
    sent."""
    held = {"TSLL": 800, "TSLA": 40}
    marks = {"TSLL": 9.50, "TSLA": 340.0}
    state = make_state(short=7600.0, long_=13600.0)
    _, proposal = propose(state, marks, held)
    text = orders.format_proposal(proposal)

    failures = []
    if not text.startswith("PROPOSED -- not submitted. Enter manually or ignore."):
        failures.append("missing the not-submitted framing")
    if "TIF: DAY" not in text:
        failures.append("missing TIF")
    if "Limit basis:" not in text or "Residual after rounding:" not in text:
        failures.append("missing basis or residual line")
    for line in text.splitlines():
        if "@ limit $" not in line:
            continue
        shares = int(line.split(" sh ")[0].split()[-1].replace(",", ""))
        limit = float(line.split("@ limit $")[1].split()[0].replace(",", ""))
        stated = float(line.split("= $")[1].replace(",", ""))
        if abs(shares * limit - stated) > 0.005:
            failures.append(f"{shares} x {limit} != {stated}")

    ok = not failures
    return check("k. rendered proposal is self-consistent and says nothing was sent",
                 ok, "; ".join(failures) if failures else
                 text.splitlines()[2].strip())


def main():
    results = [
        check_a(), check_b(), check_c(), check_d(), check_e(),
        check_f(), check_g(), check_h(), check_i(), check_j(),
        check_k(),
    ]
    print("=" * 60)
    if all(results):
        print("All orders offline checks PASSED.")
        print("Gates 9, 10, 11 of spec 008 are covered here; the approval-loop"
              " gates are live and the operator's to run.")
    else:
        print("Some checks FAILED -- see above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
