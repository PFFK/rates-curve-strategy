"""Credit-stress hedge overlay for the curve RV trades.

The curve backtest (backtest.py) shows most of its lifetime losses come
from three regime shifts: 2004-06, 2007-08 (GFC), and 2021-23. Checking
Baa-Treasury credit spread behavior during those exact windows showed a
mixed picture -- it blew out during the 2008 credit crisis (+442bp) but
actually *tightened* during the 2004-06 hiking cycle (-35bp), since that
was a routine tightening in a healthy economy, not a credit event. An
always-on short-credit overlay would therefore help in one regime and hurt
in another.

So this hedge only activates when the credit spread's own z-score signals
stress (reusing signal.py's rolling z-score machinery on a different
series), not just whenever a curve trade happens to be open. It's modeled
as a spread-isolated exposure (duration to the OAS/credit-spread component
only) -- the curve legs already carry the Treasury rate risk, so folding a
full corporate-bond total-return (rates + credit) exposure in here would
double up on rate risk instead of cleanly diversifying it.
"""

import os

import numpy as np
import pandas as pd

from src.data_loader import load_credit_spread
from src.positions import modified_duration
from src.signal import compute_zscore

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
CREDIT_SPREAD_PATH = os.path.join(DATA_DIR, "credit_spread.csv")

# Moody's "seasoned" Baa series isn't a fixed-maturity index like the CMT
# Treasury yields; there's no exact tenor to plug into the duration formula.
# 20 years approximates the long, seasoned corporate issuance the series
# tracks -- a labeled assumption, not a precise figure.
CREDIT_ASSUMED_MATURITY_YEARS = 20

DEFAULT_WINDOW = 252
DEFAULT_ENTRY_Z = 1.5
DEFAULT_EXIT_Z = 0.0
DEFAULT_TARGET_DV01 = 10_000
DURATION_MISMATCH_THRESHOLD_YEARS = 0.1
DEFAULT_TRANSACTION_COST_BPS = 0.5


def load_credit_data(path: str = CREDIT_SPREAD_PATH) -> pd.DataFrame:
    return pd.read_csv(path, index_col="date", parse_dates=True)


def generate_hedge_signal(zscore: pd.Series, entry_z: float = DEFAULT_ENTRY_Z, exit_z: float = DEFAULT_EXIT_Z) -> pd.Series:
    """1 = short credit (stress detected), 0 = no hedge.

    One-sided: activates when the credit spread's z-score rises past
    entry_z (spread elevated/widening vs its trailing norm), deactivates
    once it reverts back through exit_z. No long-credit side -- this is a
    defensive overlay, not a bidirectional credit RV trade.
    """
    active = np.zeros(len(zscore), dtype=int)
    state = 0
    for i, z in enumerate(zscore.to_numpy()):
        if np.isnan(z):
            state = 0
        elif state == 0 and z >= entry_z:
            state = 1
        elif state == 1 and z <= exit_z:
            state = 0
        active[i] = state
    return pd.Series(active, index=zscore.index, name="hedge_active")


def build_hedge_positions(
    credit_df: pd.DataFrame,
    hedge_active: pd.Series,
    target_dv01: float = DEFAULT_TARGET_DV01,
    mismatch_threshold_years: float = DURATION_MISMATCH_THRESHOLD_YEARS,
    transaction_cost_bps: float = DEFAULT_TRANSACTION_COST_BPS,
) -> pd.DataFrame:
    """Single-leg banded-rebalance sizing, same convention as positions.py:
    notional fixed at activation, resized only once duration has drifted
    past the threshold, with a transaction cost on every notional change.
    """
    dbaa = credit_df["DBAA"].reindex(hedge_active.index).to_numpy()
    active = hedge_active.to_numpy()
    n = len(hedge_active)

    notional = np.zeros(n)
    dv01_per_unit = np.zeros(n)
    rebalanced = np.zeros(n, dtype=bool)
    transaction_cost = np.zeros(n)

    cur_notional = 0.0
    ref_dur = None
    cost_rate = transaction_cost_bps / 10_000

    for i in range(n):
        state = active[i]
        prev_state = active[i - 1] if i > 0 else 0
        mod_dur = modified_duration(dbaa[i], CREDIT_ASSUMED_MATURITY_YEARS)
        dv01_per_unit[i] = mod_dur * 0.0001

        if state == 0:
            if prev_state != 0:
                transaction_cost[i] = cost_rate * cur_notional
                rebalanced[i] = True
            cur_notional = 0.0
            ref_dur = None
        else:
            needs_sizing = prev_state == 0 or abs(mod_dur - ref_dur) > mismatch_threshold_years
            if needs_sizing:
                new_notional = target_dv01 / dv01_per_unit[i]
                transaction_cost[i] = cost_rate * abs(new_notional - cur_notional)
                cur_notional = new_notional
                ref_dur = mod_dur
                rebalanced[i] = True

        notional[i] = cur_notional

    return pd.DataFrame({
        "dv01_per_unit": dv01_per_unit,
        "notional": notional,
        "current_dv01": notional * dv01_per_unit,
        "rebalanced": rebalanced,
        "transaction_cost": transaction_cost,
        "hedge_active": active,
    }, index=hedge_active.index)


def compute_hedge_pnl(credit_df: pd.DataFrame, hedge_positions_df: pd.DataFrame) -> pd.DataFrame:
    """Daily PnL, lagged one day like backtest.py: a signal from day t's
    close can't be acted on until day t+1.
    """
    d_spread_bps = credit_df["credit_spread_bps"].reindex(hedge_positions_df.index).diff()

    active_lag = hedge_positions_df["hedge_active"].shift(1).fillna(0)
    dv01_lag = hedge_positions_df["current_dv01"].shift(1).fillna(0)
    cost_lag = hedge_positions_df["transaction_cost"].shift(1).fillna(0)

    gross_pnl = (active_lag * dv01_lag * d_spread_bps).fillna(0)
    net_pnl = gross_pnl - cost_lag

    result = pd.DataFrame({
        "gross_pnl": gross_pnl,
        "transaction_cost": cost_lag,
        "net_pnl": net_pnl,
        "hedge_active": hedge_positions_df["hedge_active"],
    })
    result["cumulative_pnl"] = result["net_pnl"].cumsum()
    return result


def build_credit_hedge(
    target_dv01: float = DEFAULT_TARGET_DV01,
    window: int = DEFAULT_WINDOW,
    entry_z: float = DEFAULT_ENTRY_Z,
    exit_z: float = DEFAULT_EXIT_Z,
) -> pd.DataFrame:
    credit_df = load_credit_data()
    zscore = compute_zscore(credit_df["credit_spread_bps"], window)
    hedge_active = generate_hedge_signal(zscore, entry_z, exit_z)
    hedge_positions = build_hedge_positions(credit_df, hedge_active, target_dv01)
    pnl_df = compute_hedge_pnl(credit_df, hedge_positions)
    pnl_df["credit_spread_bps"] = credit_df["credit_spread_bps"]
    pnl_df["zscore"] = zscore
    return pnl_df


if __name__ == "__main__":
    pnl_df = build_credit_hedge()
    n_activations = int(((pnl_df["hedge_active"].shift(1).fillna(0) == 0) & (pnl_df["hedge_active"] == 1)).sum())
    pct_active = pnl_df["hedge_active"].mean()
    print(f"credit hedge: {n_activations} activations, {pct_active:.1%} of days active, "
          f"net PnL=${pnl_df['net_pnl'].sum():,.0f}, "
          f"total transaction cost=${pnl_df['transaction_cost'].sum():,.0f}")

    os.makedirs(DATA_DIR, exist_ok=True)
    pnl_df.to_csv(os.path.join(DATA_DIR, "credit_hedge.csv"))
    print(f"  saved to data/credit_hedge.csv")
