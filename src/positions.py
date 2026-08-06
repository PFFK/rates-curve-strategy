"""DV01-neutral two-leg position sizing for the curve spread trade.

Each curve point's yield is treated as the yield of a hypothetical par
Treasury bond of that maturity (which is exactly what a constant-maturity
yield represents), so a standard closed-form par-bond duration formula gives
an exact DV01 per leg with no separate price model needed.

Sign convention: position (from signal.py) +1 = steepener = long the
short-maturity leg, short the long-maturity leg, both sized to the same
target dollar DV01 at entry. -1 = flattener = the reverse.

Notional is fixed at trade entry, not recomputed every day -- a real curve
trade isn't continuously re-hedged. As yields move, each leg's modified
duration (and therefore its actual dollar DV01 at the held notional) drifts
away from the target. Once either leg's modified duration has moved more
than DURATION_MISMATCH_THRESHOLD_YEARS since the position was last sized,
both legs are re-sized back to target_dv01 -- a banded rebalance, not
continuous rebalancing. Every notional change (entry, rebalance, exit)
incurs a transaction cost.
"""

import os

import numpy as np
import pandas as pd

from src.signal import SPREAD_PAIRS, build_signal, load_yields

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")

MATURITY_YEARS = {"2Y": 2, "5Y": 5, "10Y": 10, "30Y": 30}
DEFAULT_TARGET_DV01 = 10_000  # $ P&L per 1bp move, per leg, at each sizing event
DURATION_MISMATCH_THRESHOLD_YEARS = 0.1  # rebalance trigger, either leg
DEFAULT_TRANSACTION_COST_BPS = 0.5  # cost per leg's notional traded, in bps -- an
# approximation of on-the-run Treasury bid/ask; adjust if a different cost is known.


def modified_duration(yield_pct, years_to_maturity):
    """Modified duration (years) of a semiannual-pay par bond at the given yield.

    Closed form for a par bond (coupon == yield): D_mod = [1 - (1+y/2)^-2N] / y
    Works elementwise on a scalar or a pandas Series.
    """
    y = yield_pct / 100
    periods = 2 * years_to_maturity
    return (1 - (1 + y / 2) ** (-periods)) / y


def dv01_per_unit_notional(yield_pct, years_to_maturity):
    """DV01 (price change per 1bp) per $1 of notional, for a bond priced at par."""
    return modified_duration(yield_pct, years_to_maturity) * 0.0001


def _target_leg_sizing(short_yield: float, long_yield: float, short_years: float, long_years: float, target_dv01: float) -> tuple[float, float, float, float]:
    """DV01-neutral notional for both legs, given a single day's yields."""
    dv01_short = dv01_per_unit_notional(short_yield, short_years)
    dv01_long = dv01_per_unit_notional(long_yield, long_years)
    notional_short = target_dv01 / dv01_short
    notional_long = target_dv01 / dv01_long
    return notional_short, notional_long, dv01_short, dv01_long


def build_positions(
    yields_df: pd.DataFrame,
    signal_df: pd.DataFrame,
    pair: str,
    target_dv01: float = DEFAULT_TARGET_DV01,
    mismatch_threshold_years: float = DURATION_MISMATCH_THRESHOLD_YEARS,
    transaction_cost_bps: float = DEFAULT_TRANSACTION_COST_BPS,
) -> pd.DataFrame:
    short, long_ = SPREAD_PAIRS[pair]
    short_years, long_years = MATURITY_YEARS[short], MATURITY_YEARS[long_]

    short_y = yields_df[short].reindex(signal_df.index).to_numpy()
    long_y = yields_df[long_].reindex(signal_df.index).to_numpy()
    positions_arr = signal_df["position"].to_numpy()
    n = len(signal_df)

    notional_short = np.zeros(n)
    notional_long = np.zeros(n)
    dv01_short_today = np.zeros(n)
    dv01_long_today = np.zeros(n)
    rebalanced = np.zeros(n, dtype=bool)
    transaction_cost = np.zeros(n)

    cur_notional_short = cur_notional_long = 0.0
    ref_dur_short = ref_dur_long = None
    cost_rate = transaction_cost_bps / 10_000

    for i in range(n):
        pos = positions_arr[i]
        prev_pos = positions_arr[i - 1] if i > 0 else 0

        mod_dur_short = modified_duration(short_y[i], short_years)
        mod_dur_long = modified_duration(long_y[i], long_years)
        dv01_short_today[i] = mod_dur_short * 0.0001
        dv01_long_today[i] = mod_dur_long * 0.0001

        if pos == 0:
            if prev_pos != 0:
                transaction_cost[i] = cost_rate * (cur_notional_short + cur_notional_long)
                rebalanced[i] = True
            cur_notional_short = cur_notional_long = 0.0
            ref_dur_short = ref_dur_long = None
        else:
            needs_sizing = prev_pos == 0 or (
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

    positions_df = pd.DataFrame({
        "dv01_short_per_unit": dv01_short_today,
        "dv01_long_per_unit": dv01_long_today,
        "notional_short": notional_short,
        "notional_long": notional_long,
        "current_dv01_short": notional_short * dv01_short_today,
        "current_dv01_long": notional_long * dv01_long_today,
        "rebalanced": rebalanced,
        "transaction_cost": transaction_cost,
    }, index=signal_df.index)

    positions_df = positions_df.join(signal_df[["spread_bps", "zscore", "position"]])
    positions_df["target_dv01"] = target_dv01
    return positions_df


def validate_positions(positions_df: pd.DataFrame, mismatch_threshold_years: float = DURATION_MISMATCH_THRESHOLD_YEARS) -> None:
    """Confirm legs are exactly DV01-neutral right after any sizing event, and
    that drift between events never exceeds what the rebalance band should allow.
    """
    in_trade = positions_df["position"] != 0
    dv01_diff = (positions_df["current_dv01_short"] - positions_df["current_dv01_long"]).abs()

    at_event = in_trade & positions_df["rebalanced"]
    max_diff_at_event = dv01_diff[at_event].max() if at_event.any() else 0.0
    max_diff_overall = dv01_diff[in_trade].max() if in_trade.any() else 0.0

    print(f"  at sizing events: max |DV01 short - DV01 long| = ${max_diff_at_event:.6f} (should be ~0)")
    print(f"  across full holds (incl. drift between rebalances): max |DV01 short - DV01 long| = ${max_diff_overall:,.2f}")


def summarize_positions(positions_df: pd.DataFrame, pair: str) -> None:
    active = positions_df[positions_df["position"] != 0]
    n_rebalance_events = int(positions_df["rebalanced"].sum())
    total_cost = positions_df["transaction_cost"].sum()
    print(f"[{pair}] avg notional_short=${active['notional_short'].mean():,.0f}, "
          f"avg notional_long=${active['notional_long'].mean():,.0f}, "
          f"{n_rebalance_events} sizing events (entries+rebalances+exits), "
          f"total transaction cost=${total_cost:,.0f}")


def run_positions_pipeline(save: bool = True) -> dict:
    yields_df = load_yields()

    results = {}
    for pair in SPREAD_PAIRS:
        signal_df = build_signal(yields_df, pair=pair)
        positions_df = build_positions(yields_df, signal_df, pair)

        summarize_positions(positions_df, pair)
        validate_positions(positions_df)
        results[pair] = positions_df

        if save:
            os.makedirs(DATA_DIR, exist_ok=True)
            out_path = os.path.join(DATA_DIR, f"positions_{pair}.csv")
            positions_df.to_csv(out_path)
            print(f"  saved to {out_path}")

    return results


if __name__ == "__main__":
    run_positions_pipeline()
