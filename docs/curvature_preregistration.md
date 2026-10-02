# Curvature trade: pre-registration

Written 2026-10-02, **before any curvature-trade backtest was run**. Nothing
below may be changed after seeing results; if a rule turns out to be wrong,
the fix is a new, separately labeled test, not an edit here.

## Why this test exists
The PnL attribution found that the intended slope bet loses money and that
the only consistently profitable piece of the strategy was the *unintended*
curvature exposure in the 5s30s DV01-neutral trade (+$3.53M, positive in
every regime). That is a hypothesis found by looking at the backtest, so
it can't be confirmed on the same data. This file fixes the rules up front
and assigns the verdict to a holdout.

## Hypotheses
**H1 (primary): Treasury curvature mean-reverts.**
- Signal series: fly spread = 2 x 5Y - 2Y - 30Y, in bps.
- Signal: 252-day rolling z-score of the fly; long curvature (+1) at
  z <= -1.5, short curvature (-1) at z >= +1.5, exit when z reverts through
  0. These are the spec's existing defaults (`signal.py`), reused unchanged
  -- no new parameters.
- Direction convention: +1 = earns when the PCA curvature factor rises
  (belly yields rise vs. the wings), which moves the fly spread up.

**H2 (secondary): the 5s30s signal's curvature side, traded on its own.**
- Position = minus the 5s30s mean-reversion signal (a 5s30s steepener is
  short curvature, per the attribution), traded as a pure-curvature fly.
- This is the closest replica of what actually made money, so it is also
  the most contaminated (see below). Reported, but H1 carries the verdict.

## Construction (both hypotheses)
- Legs: 2Y / 5Y / 30Y, sized with `factor_trades.solve_leg_exposures` to
  $10,000 per unit of curvature factor score and zero exposure to level and
  slope, using the trailing 3-year PCA (`ROLLING_PCA_WINDOW`) as of each
  sizing date. 2Y/5Y/30Y because they are the maturities the curvature PC
  loads on most heavily (wings 2Y/30Y, belly 5Y), and it's the same leg set
  already chosen on construction grounds for the 5s30s slope-only trade.
- Same simulator and conventions as the slope-only test: re-size every 21
  trading days, 0.5bp cost on notional traded, 1-day signal lag.
- No stop-loss, max-hold, or credit hedge. Those were developed on
  in-sample data and would add more choices to an already data-mined test.

## Evaluation
- **Holdout: 2023-01-01 through the end of the data (2026-08-04)**, ~3.6
  years, ~937 trading days. Positions carried into 2023 from earlier
  signals count; holdout PnL is simply PnL booked on holdout dates.
- **Pass (H1):** holdout net PnL > 0 **and** holdout annualized Sharpe > 0.
- Pre-2023 and full-sample results are reported for context only and do
  not affect the verdict.

## Known limitations, stated in advance
- **The holdout isn't fully clean.** The 2020-26 attribution bucket already
  showed the 5s30s trade's curvature exposure earning +$1.26M, which
  overlaps this holdout. H2 is therefore partially contaminated. H1's fly
  z-score signal has never been run on any period, but the 2023-26 rate
  path is known to us, which is unavoidable with historical data.
- **Low power.** With ~3.6 years, the standard error on an annualized
  Sharpe is ~0.5, and the strategy trades only a handful of times. A pass
  is weak evidence of an edge; only a clear failure is very informative.
  The only fully clean test is forward (paper) trading from today.
