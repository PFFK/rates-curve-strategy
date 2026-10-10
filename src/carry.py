"""Carry and rolldown for the multi-leg factor trades.

The backtests price every trade off daily changes in constant-maturity
yields, which captures mark-to-market PnL but not the PnL of simply
holding the bonds:

- Carry: a long bond position earns its yield and pays the financing
  (repo) rate on the cash borrowed to hold it; a short earns the repo
  rate and pays the yield. Per leg, per day held:
      carry = B * (y - r) / 100 * dt
- Rolldown: a bond held for dt years becomes a dt-shorter bond, so on an
  unchanged curve its yield slides along the curve. Per leg:
      rolldown = B * DV01_per_unit * slope_bp_per_year * dt
  where slope is the curve's local slope just below that maturity.

B is the long face amount (negative for shorts). The trades store signed
$ DV01 per leg as PnL per +1bp yield rise, so B = -dv01 / dv01_per_unit.
Both pieces use the previous close's holdings and curve, like the price
PnL, and dt is actual calendar days / 365 so weekends accrue.

Approximations, stated rather than hidden:
- Financing at effective fed funds (DFF). Treasury GC repo has tracked
  fed funds within a few bp for most of the sample; SOFR only starts in
  2018. Specialness (on-the-run shorts paying a premium to borrow) is
  ignored, which flatters short legs slightly.
- Local slope is linear between the leg's CMT point and the next shorter
  one (1Y below 2Y, 3Y below 5Y, 7Y below 10Y, 20Y below 30Y). The true
  curve is concave at the front end, so this overstates rolldown there.
- CMT yields are par yields of a hypothetical constantly-rolled bond; a real
  implementation holds a specific issue that ages, re-hedged every 21 days.
"""

import os

import numpy as np
import pandas as pd

from src.pca_factors import MATURITIES
from src.positions import MATURITY_YEARS, modified_duration

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")
CARRY_INPUTS_PATH = os.path.join(DATA_DIR, "carry_inputs.csv")

CARRY_HISTORY_START = "2001-08-06"  # same fixed start as the backtest data

# The next-shorter CMT point used for each leg's local curve slope.
ROLL_NEIGHBOR = {"2Y": ("1Y", "DGS1", 1), "5Y": ("3Y", "DGS3", 3), "10Y": ("7Y", "DGS7", 7), "30Y": ("20Y", "DGS20", 20)}
FUNDING_SERIES = "DFF"


def fetch_carry_inputs(save: bool = True) -> pd.DataFrame:
    from src.data_loader import clean_yields, get_fred_client

    fred = get_fred_client()
    series = {label: fred.get_series(sid, observation_start=CARRY_HISTORY_START) for label, sid, _ in ROLL_NEIGHBOR.values()}
    series["funding"] = fred.get_series(FUNDING_SERIES, observation_start=CARRY_HISTORY_START)
    df = clean_yields(pd.DataFrame(series))
    df.index.name = "date"
    if save:
        os.makedirs(DATA_DIR, exist_ok=True)
        df.to_csv(CARRY_INPUTS_PATH)
    return df


def load_carry_inputs() -> pd.DataFrame:
    return pd.read_csv(CARRY_INPUTS_PATH, index_col="date", parse_dates=True)


def carry_rolldown(yields_df: pd.DataFrame, inputs_df: pd.DataFrame, trade_df: pd.DataFrame) -> pd.DataFrame:
    """Daily carry and rolldown $ for a factor_trades.simulate_exposure_trade result."""
    idx = trade_df.index
    y = yields_df[MATURITIES].reindex(idx)
    # DFF prints on weekends too; align to the trading calendar by last value
    inputs = inputs_df.reindex(inputs_df.index.union(idx)).ffill().reindex(idx)
    dt = idx.to_series().diff().dt.days.fillna(0).to_numpy() / 365

    carry = np.zeros(len(idx))
    roll = np.zeros(len(idx))
    for m in MATURITIES:
        dv01_pu = modified_duration(y[m], MATURITY_YEARS[m]) * 0.0001
        face = -trade_df[f"dv01_{m}"] / dv01_pu  # long face amount; shorts negative
        neighbor, _, neighbor_years = ROLL_NEIGHBOR[m]
        slope_bp_per_year = (y[m] - inputs[neighbor]) * 100 / (MATURITY_YEARS[m] - neighbor_years)

        leg_carry = face * (y[m] - inputs["funding"]) / 100
        leg_roll = face * dv01_pu * slope_bp_per_year
        # held from the previous close, so lag one day like the price PnL
        carry += (leg_carry.shift(1).fillna(0.0) * dt).to_numpy()
        roll += (leg_roll.shift(1).fillna(0.0) * dt).to_numpy()

    return pd.DataFrame({"carry": carry, "rolldown": roll}, index=idx)


def with_carry(yields_df: pd.DataFrame, inputs_df: pd.DataFrame, trade_df: pd.DataFrame) -> pd.DataFrame:
    out = trade_df[["position", "net_pnl"]].rename(columns={"net_pnl": "price_pnl"})
    out = out.join(carry_rolldown(yields_df, inputs_df, trade_df))
    out["total_pnl"] = out["price_pnl"] + out["carry"] + out["rolldown"]
    return out


def plot_h1_carry(pnl: pd.DataFrame, holdout_start: str, outputs_dir: str = OUTPUT_DIR) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from src.factor_trades import SERIES_COLORS

    os.makedirs(outputs_dir, exist_ok=True)
    fig, ax = plt.subplots(figsize=(10, 4.5))
    ax.plot(pnl.index, pnl["price_pnl"].cumsum() / 1e6, color=SERIES_COLORS[0], label="price PnL only (pre-registered basis)")
    ax.plot(pnl.index, pnl["total_pnl"].cumsum() / 1e6, color=SERIES_COLORS[1], label="+ carry + rolldown")
    ax.axvspan(pd.Timestamp(holdout_start), pnl.index[-1], color="gray", alpha=0.15, label="holdout")
    ax.axhline(0, color="#888888", linewidth=0.8)
    ax.set_ylabel("Cumulative net PnL ($M)")
    ax.set_title("H1 curvature fly: the cost of holding it")
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", color="#e5e5e5", linewidth=0.6)
    ax.legend(frameon=False, loc="upper left")
    fig.tight_layout()
    fig.savefig(os.path.join(outputs_dir, "h1_carry.png"), dpi=150)
    plt.close(fig)


def run_carry_analysis(save: bool = True) -> dict:
    """Price-only vs. price + carry + rolldown for every factor trade. The
    curvature trades also report the pre-registered holdout separately.
    Positions are unchanged; this only adds the PnL of holding them.
    """
    from src.backtest import compute_series_stats
    from src.factor_trades import (
        H1_LABEL, H2_LABEL, HOLDOUT_START, build_curvature_trade, curvature_signals, rolling_loadings,
        run_slope_neutral_test,
    )
    from src.signal import load_yields

    yields_df = load_yields()
    inputs_df = load_carry_inputs()
    loadings_by_date = rolling_loadings(yields_df)

    trades = {}
    slope = run_slope_neutral_test(save=False)
    for pair, res in slope.items():
        for name, r in res.items():
            trades[f"{pair} {name}"] = r["trade"]
    for label, position in curvature_signals(yields_df).items():
        trades[label] = build_curvature_trade(yields_df, position, loadings_by_date)

    print()
    results = {}
    for label, trade in trades.items():
        pnl = with_carry(yields_df, inputs_df, trade)
        periods = {"full": pnl}
        if label in (H1_LABEL, H2_LABEL):
            periods["holdout"] = pnl.loc[HOLDOUT_START:]
        for period, p in periods.items():
            price, total = compute_series_stats(p["price_pnl"]), compute_series_stats(p["total_pnl"])
            print(f"[{label}, {period}] price ${price['total_net_pnl']:,.0f} (Sharpe {price['sharpe']:.2f}) "
                  f"+ carry ${p['carry'].sum():,.0f} + rolldown ${p['rolldown'].sum():,.0f} "
                  f"= ${total['total_net_pnl']:,.0f} (Sharpe {total['sharpe']:.2f})")
        results[label] = pnl
        if save:
            tag = label.split(":")[0].lower().replace(" ", "_")
            pnl.to_csv(os.path.join(DATA_DIR, f"carry_{tag}.csv"))
    if save:
        plot_h1_carry(results[H1_LABEL], HOLDOUT_START)
    return results


if __name__ == "__main__":
    run_carry_analysis()
