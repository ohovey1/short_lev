# Strategy Spec: Banded Rebalancing

**This file is the source of truth.** Where any other document, comment, or line
of code disagrees with this file, this file wins and the other is a bug.

Scope: this describes the *live-traded strategy*. The tranche ladder in
`backtest.py` is a reference/validation implementation and is deliberately not
covered here.

Regenerate the stakeholder-facing document from this file rather than editing
the two separately.

---

## 1. Strategy mechanics

### Position opening

Each pair holds two legs: short a fixed `target` notional of a leveraged ETF,
and long `leverage x target` in the underlying. Sized to be roughly
delta-neutral at entry.

```
target = (base_capital x capital_utilization) / margin_multiplier
long_leg = leverage x target
```

`margin_multiplier` is per-pair and lives in `config.PAIRS`. It is a
regulatory-formula estimate, not a confirmed IBKR house number -- confirm via a
TWS what-if order before sizing any live position.

### Position monitoring and rebalancing

The position is polled on a fixed interval (target: every 15 minutes during
market hours). A rebalance fires only when one of two bands is breached.
Foil Decay is checked first, because its reset also re-neutralizes delta.

| Band | Condition | Action |
|---|---|---|
| **Foil Decay Band** | `abs(short_notional - target) > foil_decay_band x target` | Reset **both legs** to target |
| **Long-Short Band** | `abs(net_delta) > long_short_band x target` | Resize the **long leg only**; short carries at its drifted size |

Where:
- `short_notional` = current mark-to-market value of the short leg
- `net_delta` = `long_notional - leverage x short_notional`

Between trades both legs' dollar sizes are untouched. The frozen hedge is the
entire source of P&L: variance drag accrues precisely because the hedge ratio
is not continuously reset. Daily re-neutralization would eliminate the edge.

### Position closing

Three paths out. All three are modeled in the backtest as real actions, not
alerts only -- reported returns and drawdowns must reflect a strategy that
actually de-risks.

| Trigger | Condition | Action |
|---|---|---|
| **Drawdown stop** | Equity falls 10% below its running peak | Close both legs; stay flat for the remainder of the run. Alert. |
| **Margin de-risk** | `margin_cushion < 0` | Scale both legs down proportionally to restore cushion. Alert. |
| **Kill switch** | Manual/operational override | Close both legs fully. Flat. |

**Margin de-risk rule.** On breach, recompute target from current equity rather
than original capital:

```
target_new = (equity_now x capital_utilization) / margin_multiplier
```

then reset both legs to `target_new` and `leverage x target_new`. This is a
partial reset, not a close. Target ratchets **down only** -- it never scales
back up as equity recovers. That keeps the "target is fixed, not equity-scaled"
principle intact in the normal case while still letting the position shrink
under stress.

Kill-switch reasons include stock splits, earnings, index reconstitution,
borrow becoming unavailable or repriced, and market-wide dislocation. It is
deliberately a human decision with no automated trigger.

---

## 2. Key parameters

| Parameter | Value | Notes |
|---|---|---|
| `long_short_band` | 0.10 | To be optimized by grid search |
| `foil_decay_band` | 0.10 | To be optimized by grid search |
| `capital_utilization` | 0.75 | Eliminates margin breach days across all 13 pairs on the current window; 1.00 breaches on 38-203 days per pair |
| `drawdown_stop` | 0.10 | Peak-to-trough on equity |
| `poll_interval` | 15 min | Live only; see modeling assumption below |

**Modeling assumption -- check cadence.** The backtest evaluates bands once per
day on the close. Live polling every 15 minutes will trip bands strictly more
often. Backtest `n_trades` and turnover are therefore a **lower bound**, and
realized transaction costs will exceed modeled costs by an unquantified margin
until intraday checking is modeled.

---

## 3. Mechanics of shorting on IBKR

- Leveraged ETFs carry margin requirements above ordinary equities on both
  legs, scaling with the fund's leverage multiple. A 3x fund requires more
  collateral per dollar of notional than a 2x fund.
- Long and short legs are margined independently. There is no hedge netting
  below Portfolio Margin thresholds ($110,000 NLV plus options approval).
- Each pair carries a `margin_multiplier` reflecting the combined collateral
  requirement of both legs:
  - broad/sector-ETF underlying, 3x short: `0.25 x 3 + 0.90 = 1.65`
  - broad/sector-ETF underlying, 2x short: `0.25 x 2 + 0.60 = 1.10`
  - single-stock underlying, 2x short: `0.50 x 2 + 0.60 = 1.60`
- Capital utilization is deliberately held below 100% as a margin cushion
  against forced liquidation.

---

## 4. Worked example

**Setup.** TSLA / TSLL (2x). `base_capital` = $10,000, `margin_multiplier` =
1.60, `capital_utilization` = 0.75.

```
target   = (10,000 x 0.75) / 1.60 = $4,687.50
long_leg = 2 x 4,687.50           = $9,375.00
```

**Entry:** short $4,687.50 TSLL, long $9,375.00 TSLA, net delta = $0.

Band thresholds at this target:

| Band | Threshold | Trips when |
|---|---|---|
| Foil Decay (10%) | $468.75 | `short_notional` outside $4,218.75 - $5,156.25 |
| Long-Short (10%) | $468.75 | `abs(net_delta)` exceeds $468.75 |

### Scenario 1 -- Long-Short Band triggered

| Field | Value |
|---|---|
| Short TSLL notional | $4,850 (within Foil Decay band) |
| Net delta | +$520 (band breached) |
| Trigger | Long-Short Band |
| Action | Resize long TSLA leg only, to $9,700 |
| Short leg | Unchanged at $4,850 |
| Resulting state | Net delta = 0; short notional unchanged |

### Scenario 2 -- Foil Decay Band triggered

| Field | Value |
|---|---|
| Short TSLL notional | $5,300 (drifted >10% from $4,687.50) |
| Trigger | Foil Decay Band |
| Action | Reset **both legs** to target |
| Short leg | Back to $4,687.50 |
| Long leg | Back to $9,375.00 |
| Resulting state | Net delta = 0; both legs at fresh target |

### Scenario 3 -- Margin de-risk

| Field | Value |
|---|---|
| Equity | $8,400 (down from $10,000) |
| Margin cushion | Negative |
| Trigger | Margin de-risk |
| Action | Recompute target from current equity: `(8,400 x 0.75) / 1.60 = $3,937.50` |
| Resulting state | Short $3,937.50, long $7,875.00, net delta = 0, cushion restored |

### Scenario 4 -- Kill switch / drawdown stop

| Field | Value |
|---|---|
| Trigger examples | Equity 10% below peak, manual override, borrow unavailable |
| Band state | Irrelevant -- overrides both band checks |
| Action | Close both legs fully |
| Resulting state | Flat, no position remains |

---

## 5. Explicit scope cuts

Named here so they don't get silently implemented:

- **Positive-leverage funds only.** Inverse funds are out of scope pending the
  signed-leverage rework.
- **No re-entry after a drawdown stop.** Once flat, the run ends. Any re-entry
  rule is a new strategy parameter requiring its own justification.
- **No target ratchet-up.** Target shrinks under margin stress and never grows
  back. No reinvestment of P&L.
- **No auxiliary collateral sleeve** (BRK, GLD, etc.) to support the margin
  cushion. Logged as an open decision; it changes the margin model materially.
- **Costs still omitted:** expense ratio (already embedded in LETF price
  history -- adding it double-counts), spread, dividends, commissions.