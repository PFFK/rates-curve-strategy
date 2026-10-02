"""PnL attribution: decompose the curve trade's realized PnL into level,
slope, and curvature contributions using the PCA from step 1.

The decomposition is exact, not a regression. PCA is fit on all four
maturities with four components, so each day's yield change vector
(in bps) is exactly

    dy = mu + L @ f

where mu is the PCA's sample-mean daily change, L is the (maturity x factor)
loadings matrix and f is that day's factor scores. The curve position's
gross PnL is e . dy, where e is its signed DV01 per maturity (held from
the previous close, same 1-day lag as the backtest). So

    gross PnL = e . mu + sum_k f_k * (e . L_k)

and each factor's contribution is f_k * (e . L_k): that day's factor move
times the position's exposure to it. The pieces sum back to gross PnL to
machine precision (checked in attribute_pnl), and costs plus the credit
hedge are added as their own buckets so the total matches net PnL.

Caveat: the PCA is fit once on the full 25-year sample, so the loadings
use future data. That's fine for after-the-fact attribution (it never
feeds a trading decision), but it would be lookahead if the loadings ever
drove positions.
"""

import os

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from src.pca_factors import MATURITIES, compute_daily_changes, fit_pca
from src.positions import MATURITY_YEARS, modified_duration
from src.signal import SPREAD_PAIRS, load_yields

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")

FACTORS = ["level", "slope", "curvature", "residual"]


def position_exposures(yields_df: pd.DataFrame, curve_pnl_df: pd.DataFrame, pair: str) -> pd.DataFrame:
    """Signed $ DV01 per maturity held into each day (lagged one day).

    Rebuilt from the simulation's notionals and the previous close's
    duration, exactly as risk_controls.simulate_curve_trade prices PnL.
    """
    short, long_ = SPREAD_PAIRS[pair]
    idx = curve_pnl_df.index
    y = yields_df.reindex(idx)
    dv01_short = curve_pnl_df["notional_short"] * modified_duration(y[short], MATURITY_YEARS[short]) * 0.0001
    dv01_long = curve_pnl_df["notional_long"] * modified_duration(y[long_], MATURITY_YEARS[long_]) * 0.0001
    pos = curve_pnl_df["position"]

    exposures = pd.DataFrame(0.0, index=idx, columns=MATURITIES)
    exposures[short] = -pos * dv01_short
    exposures[long_] = pos * dv01_long
    return exposures.shift(1).fillna(0.0)


def attribute_exposures(yields_df: pd.DataFrame, exposures: pd.DataFrame, pnl_df: pd.DataFrame, label: str) -> pd.DataFrame:
    """Factor decomposition for any trade, given its held $ DV01 per
    maturity (already lagged to the day it earns PnL) and its gross/net PnL.
    """
    changes = compute_daily_changes(yields_df)
    pca, loadings, scores = fit_pca(changes)
    mu = pd.Series(pca.mean_, index=MATURITIES)

    exposures = exposures.reindex(scores.index).fillna(0.0)
    factor_beta = exposures[MATURITIES] @ loadings[FACTORS]  # $ per 1-unit factor move, per day

    attribution = factor_beta * scores[FACTORS]
    attribution["drift"] = exposures[MATURITIES] @ mu
    attribution = attribution.reindex(pnl_df.index).fillna(0.0)

    max_err = (attribution.sum(axis=1) - pnl_df["gross_pnl"]).abs().max()
    if max_err > 1e-4:
        raise ValueError(f"[{label}] factor contributions don't sum to gross PnL (max error ${max_err:,.6f})")

    # The simulations charge each day's cost against the *next* day's PnL
    # (same 1-day lag as positions), so take it as gross minus net.
    attribution["transaction_cost"] = pnl_df["net_pnl"] - pnl_df["gross_pnl"]
    return attribution


def attribute_pnl(
    yields_df: pd.DataFrame,
    curve_pnl_df: pd.DataFrame,
    pair: str,
    hedge_net_pnl: pd.Series | None = None,
) -> pd.DataFrame:
    exposures = position_exposures(yields_df, curve_pnl_df, pair)
    attribution = attribute_exposures(yields_df, exposures, curve_pnl_df, pair)
    if hedge_net_pnl is not None:
        attribution["credit_hedge"] = hedge_net_pnl.reindex(curve_pnl_df.index).fillna(0.0)
    return attribution


def factor_exposure_summary(yields_df: pd.DataFrame, curve_pnl_df: pd.DataFrame, pair: str) -> pd.Series:
    """Average |exposure| to each factor while in a trade, normalized by
    the slope exposure. Shows how "pure slope" a DV01-neutral trade really is.
    """
    changes = compute_daily_changes(yields_df)
    _, loadings, _ = fit_pca(changes)
    exposures = position_exposures(yields_df, curve_pnl_df, pair)
    beta = (exposures[MATURITIES] @ loadings[FACTORS]).abs()
    in_trade = exposures.abs().sum(axis=1) > 0
    mean_beta = beta[in_trade].mean()
    return mean_beta / mean_beta["slope"]


def plot_attribution(attribution: pd.DataFrame, pair: str, title_suffix: str, filename: str, outputs_dir: str = OUTPUT_DIR) -> None:
    os.makedirs(outputs_dir, exist_ok=True)
    fig, axes = plt.subplots(2, 1, figsize=(10, 7.5), height_ratios=[3, 2])

    cumulative = attribution.cumsum()
    colors = {
        "level": "steelblue", "slope": "darkorange", "curvature": "seagreen", "residual": "orchid",
        "drift": "gray", "transaction_cost": "firebrick", "credit_hedge": "goldenrod",
    }
    for col in attribution.columns:
        axes[0].plot(cumulative.index, cumulative[col], label=col, color=colors.get(col))
    axes[0].plot(cumulative.index, cumulative.sum(axis=1), label="total", color="black", linewidth=1.6)
    axes[0].axhline(0, color="gray", linewidth=0.8)
    axes[0].set_ylabel("Cumulative PnL ($)")
    axes[0].set_title(f"{pair}: PnL attribution by PCA factor ({title_suffix})")
    axes[0].legend(ncol=4, fontsize=8)

    yearly = attribution.groupby(attribution.index.year).sum()
    bottom_pos = np.zeros(len(yearly))
    bottom_neg = np.zeros(len(yearly))
    for col in yearly.columns:
        vals = yearly[col].to_numpy()
        base = np.where(vals >= 0, bottom_pos, bottom_neg)
        axes[1].bar(yearly.index, vals, bottom=base, color=colors.get(col), label=col, width=0.8)
        bottom_pos += np.where(vals >= 0, vals, 0)
        bottom_neg += np.where(vals < 0, vals, 0)
    axes[1].axhline(0, color="gray", linewidth=0.8)
    axes[1].set_ylabel("PnL by year ($)")

    fig.tight_layout()
    fig.savefig(os.path.join(outputs_dir, filename), dpi=150)
    plt.close(fig)


def run_attribution_pipeline(save: bool = True) -> dict:
    from src.full_stack import run_full_stack_pipeline
    from src.risk_controls import simulate_curve_trade

    yields_df = load_yields()
    stack = run_full_stack_pipeline(save=save)

    results = {}
    print()
    for pair in SPREAD_PAIRS:
        baseline = simulate_curve_trade(yields_df, pair)
        variants = {
            "baseline": (attribute_pnl(yields_df, baseline, pair), f"attribution_{pair}_baseline.png"),
            "full stack": (
                attribute_pnl(yields_df, stack[pair]["controlled"], pair, stack[pair]["hedge_net"]),
                f"attribution_{pair}_full_stack.png",
            ),
        }
        exposure = factor_exposure_summary(yields_df, baseline, pair)
        print(f"[{pair}] avg |factor exposure| relative to slope, while in a trade: "
              + ", ".join(f"{k}={v:.2f}" for k, v in exposure.items()))

        for name, (attribution, filename) in variants.items():
            totals = attribution.sum()
            expected = (baseline if name == "baseline" else stack[pair]["controlled"])["net_pnl"].sum()
            if name != "baseline":
                expected += stack[pair]["hedge_net"].sum()
            assert abs(totals.sum() - expected) < 1e-2, f"{pair} {name}: attribution ${totals.sum():,.2f} != net ${expected:,.2f}"
            print(f"[{pair} {name}] " + ", ".join(f"{k}=${v:,.0f}" for k, v in totals.items())
                  + f" | total=${totals.sum():,.0f}")
            if save:
                attribution.to_csv(os.path.join(DATA_DIR, filename.replace(".png", ".csv")))
                plot_attribution(attribution, pair, name, filename)
        results[pair] = {name: attr for name, (attr, _) in variants.items()}

    return results


if __name__ == "__main__":
    run_attribution_pipeline()
