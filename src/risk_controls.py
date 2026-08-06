"""Curve trade simulation with optional max-hold and stop-loss exits.

backtest.py's pipeline is three sequential passes: signal.py decides
position from the z-score alone, positions.py sizes DV01-neutral legs from
that position, then backtest.py computes PnL from the sizing. A stop-loss
breaks that ordering -- whether to still be in the trade tomorrow now
depends on today's realized dollar PnL, which depends on sizing, which
depends on being in the trade. So this module re-does all three steps as a
single day-by-day walk instead of three passes, adding two exit triggers
on top of the original z-score reversion exit:

- max_hold_days: force-exit after N trading days, regardless of z-score.
- stop_loss_dollars: force-exit if cumulative realized PnL since entry
  drops below -stop_loss_dollars, regardless of z-score.

With both set to None this reproduces backtest.py's numbers exactly (see
validate_against_baseline) -- confirming the merge didn't change behavior
before trusting the new results with the controls turned on.

A stop-loss alone whipsaws: the z-score doesn't know a stop just fired, so
if it's still past the entry threshold the very next day, the position
re-enters immediately in the same direction (measured: 93-95% of stop-loss
exits did exactly this), which just resets the loss counter to zero and
pays an extra round-trip of transaction costs without actually reducing
exposure to whatever secular move triggered the stop. To fix that,
stop_loss_reentry_cooldown requires the z-score to retrace at least halfway
back from its level at the stop-out toward exit_z before the same direction
can be re-entered; the opposite direction is never blocked, since that's a
distinct new bet, not a continuation of the one that just got stopped out.
"""

import os

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from src.positions import MATURITY_YEARS, _target_leg_sizing, modified_duration
from src.signal import DEFAULT_ENTRY_Z, DEFAULT_EXIT_Z, DEFAULT_WINDOW, SPREAD_PAIRS, compute_spread, compute_zscore, load_yields
from src.positions import DEFAULT_TARGET_DV01, DURATION_MISMATCH_THRESHOLD_YEARS, DEFAULT_TRANSACTION_COST_BPS

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")

DEFAULT_MAX_HOLD_DAYS = 252  # matches the z-score's own lookback window
DEFAULT_STOP_LOSS_DOLLARS = 500_000  # ~13-14x the ~$37-38k daily PnL std observed while in a position


def simulate_curve_trade(
    yields_df: pd.DataFrame,
    pair: str,
    window: int = DEFAULT_WINDOW,
    entry_z: float = DEFAULT_ENTRY_Z,
    exit_z: float = DEFAULT_EXIT_Z,
    target_dv01: float = DEFAULT_TARGET_DV01,
    mismatch_threshold_years: float = DURATION_MISMATCH_THRESHOLD_YEARS,
    transaction_cost_bps: float = DEFAULT_TRANSACTION_COST_BPS,
    max_hold_days: int | None = None,
    stop_loss_dollars: float | None = None,
    stop_loss_reentry_cooldown: bool = True,
) -> pd.DataFrame:
    short, long_ = SPREAD_PAIRS[pair]
    short_years, long_years = MATURITY_YEARS[short], MATURITY_YEARS[long_]

    spread = compute_spread(yields_df, pair)
    zscore = compute_zscore(spread, window)
    z = zscore.to_numpy()

    short_y = yields_df[short].reindex(spread.index).to_numpy()
    long_y = yields_df[long_].reindex(spread.index).to_numpy()
    d_short_bps = yields_df[short].reindex(spread.index).diff().to_numpy() * 100
    d_long_bps = yields_df[long_].reindex(spread.index).diff().to_numpy() * 100
    n = len(spread)

    position = np.zeros(n, dtype=int)
    notional_short = np.zeros(n)
    notional_long = np.zeros(n)
    dv01_short_per_unit = np.zeros(n)
    dv01_long_per_unit = np.zeros(n)
    rebalanced = np.zeros(n, dtype=bool)
    transaction_cost = np.zeros(n)
    gross_pnl = np.zeros(n)
    net_pnl = np.zeros(n)
    exit_reason = np.full(n, None, dtype=object)

    pos = 0
    cur_notional_short = cur_notional_long = 0.0
    ref_dur_short = ref_dur_long = None
    prev_notional_short = prev_notional_long = 0.0
    prev_dv01_short_per_unit = prev_dv01_long_per_unit = 0.0
    prev_transaction_cost = 0.0
    days_in_trade = 0
    trade_cum_pnl = 0.0
    cost_rate = transaction_cost_bps / 10_000
    blocked_dir = None  # direction locked out after a stop-loss, until partial z reversion
    unlock_z = None

    for i in range(n):
        # Step 1: today's PnL, from YESTERDAY's held position/DV01 (1-day lag,
        # same convention as backtest.py -- a signal from day t's close can't
        # be acted on until day t+1).
        if i == 0:
            day_gross = 0.0
        else:
            day_gross = pos * (
                -prev_notional_short * prev_dv01_short_per_unit * d_short_bps[i]
                + prev_notional_long * prev_dv01_long_per_unit * d_long_bps[i]
            )
        day_cost = prev_transaction_cost
        gross_pnl[i] = day_gross
        net_pnl[i] = day_gross - day_cost

        if pos != 0:
            trade_cum_pnl += net_pnl[i]
            days_in_trade += 1

        # Step 2: today's duration/DV01-per-unit (needed for sizing below and
        # as "yesterday's" value for tomorrow's PnL calc).
        mod_dur_short = modified_duration(short_y[i], short_years)
        mod_dur_long = modified_duration(long_y[i], long_years)
        dv01_short_per_unit[i] = mod_dur_short * 0.0001
        dv01_long_per_unit[i] = mod_dur_long * 0.0001

        # Step 3: decide today's position.
        if np.isnan(z[i]):
            pos = 0
        elif pos == 0:
            if blocked_dir is not None:
                if (blocked_dir == 1 and z[i] >= unlock_z) or (blocked_dir == -1 and z[i] <= unlock_z):
                    blocked_dir = None

            if z[i] <= -entry_z and blocked_dir != 1:
                pos = 1
            elif z[i] >= entry_z and blocked_dir != -1:
                pos = -1
            if pos != 0:
                days_in_trade = 0
                trade_cum_pnl = 0.0
        else:
            exit_now, reason = False, None
            if (pos == 1 and z[i] >= exit_z) or (pos == -1 and z[i] <= exit_z):
                exit_now, reason = True, "reversion"
            elif max_hold_days is not None and days_in_trade >= max_hold_days:
                exit_now, reason = True, "max_hold"
            elif stop_loss_dollars is not None and trade_cum_pnl <= -stop_loss_dollars:
                exit_now, reason = True, "stop_loss"
            if exit_now:
                exit_reason[i] = reason
                if reason == "stop_loss" and stop_loss_reentry_cooldown:
                    blocked_dir = pos
                    unlock_z = z[i] / 2  # halfway back toward exit_z (0)
                pos = 0
                days_in_trade = 0
                trade_cum_pnl = 0.0

        position[i] = pos

        # Step 4: DV01 sizing for the (possibly just-changed) position.
        if pos == 0:
            if cur_notional_short != 0.0 or cur_notional_long != 0.0:
                transaction_cost[i] = cost_rate * (cur_notional_short + cur_notional_long)
                rebalanced[i] = True
            cur_notional_short = cur_notional_long = 0.0
            ref_dur_short = ref_dur_long = None
        else:
            needs_sizing = ref_dur_short is None or (
                abs(mod_dur_short - ref_dur_short) > mismatch_threshold_years
                or abs(mod_dur_long - ref_dur_long) > mismatch_threshold_years
            )
            if needs_sizing:
                new_short, new_long, _, _ = _target_leg_sizing(
                    short_y[i], long_y[i], short_years, long_years, target_dv01
                )
                transaction_cost[i] = cost_rate * (
                    abs(new_short - cur_notional_short) + abs(new_long - cur_notional_long)
                )
                cur_notional_short, cur_notional_long = new_short, new_long
                ref_dur_short, ref_dur_long = mod_dur_short, mod_dur_long
                rebalanced[i] = True

        notional_short[i] = cur_notional_short
        notional_long[i] = cur_notional_long

        # Step 5: carry state forward for tomorrow's PnL calc.
        prev_notional_short, prev_notional_long = cur_notional_short, cur_notional_long
        prev_dv01_short_per_unit = dv01_short_per_unit[i]
        prev_dv01_long_per_unit = dv01_long_per_unit[i]
        prev_transaction_cost = transaction_cost[i]

    pnl_df = pd.DataFrame({
        "gross_pnl": gross_pnl,
        "transaction_cost": transaction_cost,
        "net_pnl": net_pnl,
        "position": position,
        "notional_short": notional_short,
        "notional_long": notional_long,
        "exit_reason": exit_reason,
        "zscore": z,
        "spread_bps": spread.to_numpy(),
    }, index=spread.index)
    pnl_df["cumulative_pnl"] = pnl_df["net_pnl"].cumsum()
    pnl_df["running_max"] = pnl_df["cumulative_pnl"].cummax()
    pnl_df["drawdown"] = pnl_df["cumulative_pnl"] - pnl_df["running_max"]
    return pnl_df


def plot_comparison(baseline_cumulative: pd.Series, controlled_cumulative: pd.Series, pair: str, outputs_dir: str) -> None:
    os.makedirs(outputs_dir, exist_ok=True)
    fig, ax = plt.subplots(figsize=(10, 4.5))
    ax.plot(baseline_cumulative.index, baseline_cumulative, label="baseline (no risk controls)", color="steelblue")
    ax.plot(controlled_cumulative.index, controlled_cumulative, label="max-hold + stop-loss w/ cooldown", color="seagreen")
    ax.axhline(0, color="gray", linewidth=0.8)
    ax.set_ylabel("Cumulative net PnL ($)")
    ax.set_title(f"{pair}: baseline vs. risk-controlled")
    ax.legend()
    fig.tight_layout()
    fig.savefig(os.path.join(outputs_dir, f"backtest_{pair}_with_risk_controls.png"), dpi=150)
    plt.close(fig)


def run_risk_controlled_pipeline(
    max_hold_days: int = DEFAULT_MAX_HOLD_DAYS,
    stop_loss_dollars: float = DEFAULT_STOP_LOSS_DOLLARS,
    save: bool = True,
) -> dict:
    from src.backtest import summarize_backtest

    BASE_OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")
    yields_df = load_yields()

    results = {}
    for pair in SPREAD_PAIRS:
        baseline = simulate_curve_trade(yields_df, pair, max_hold_days=None, stop_loss_dollars=None)
        controlled = simulate_curve_trade(
            yields_df, pair, max_hold_days=max_hold_days, stop_loss_dollars=stop_loss_dollars,
            stop_loss_reentry_cooldown=True,
        )

        base_stats = summarize_backtest(baseline)
        ctrl_stats = summarize_backtest(controlled)
        n_stop = int((controlled["exit_reason"] == "stop_loss").sum())
        n_hold = int((controlled["exit_reason"] == "max_hold").sum())

        print(f"[{pair}] baseline: net PnL=${base_stats['total_net_pnl']:,.0f}, Sharpe={base_stats['sharpe']:.2f}, "
              f"max DD=${base_stats['max_drawdown']:,.0f}")
        print(f"[{pair}] risk-controlled: net PnL=${ctrl_stats['total_net_pnl']:,.0f}, Sharpe={ctrl_stats['sharpe']:.2f}, "
              f"max DD=${ctrl_stats['max_drawdown']:,.0f} ({n_stop} stop-loss exits, {n_hold} max-hold exits)")

        results[pair] = {"baseline": baseline, "controlled": controlled, "base_stats": base_stats, "ctrl_stats": ctrl_stats}

        if save:
            os.makedirs(DATA_DIR, exist_ok=True)
            controlled.to_csv(os.path.join(DATA_DIR, f"risk_controlled_{pair}.csv"))
            plot_comparison(baseline["cumulative_pnl"], controlled["cumulative_pnl"], pair, BASE_OUTPUT_DIR)
            print(f"  saved to data/risk_controlled_{pair}.csv, outputs/backtest_{pair}_with_risk_controls.png")

    return results


if __name__ == "__main__":
    run_risk_controlled_pipeline()
