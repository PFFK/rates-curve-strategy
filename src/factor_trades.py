"""Multi-leg curve trades sized to PCA factor exposures, not just DV01.

The attribution (attribution.py) showed DV01-neutral legs aren't
factor-neutral: a 5s30s DV01-neutral trade carries more curvature
exposure than slope exposure. This module sizes legs directly in PCA
factor space instead: pick the legs, pick which factor to be exposed to,
and solve for the $ DV01 per leg that gives the target exposure to that
factor and zero exposure to the others.

With three legs and three constraints (level, slope, curvature) the
solution is unique; the fourth PC (residual, ~0.6% of variance) is left
unhedged and reported.

No lookahead: unlike attribution, these loadings drive positions, so they
come from a trailing PCA (ROLLING_PCA_WINDOW days of changes up to and
including the sizing date), never the full-sample fit.

Shape caveat: with a 3-year window, the slope factor isn't monotonic
across maturities in 2012-15 (2Y pinned near zero by policy, so it barely
loads), which pca_factors._validate_factor_shapes would reject. It's still
a slope factor economically (long end vs. belly), so the rolling fit keeps
the sign conventions from _orient_signs but skips the strict shape check.

Sizing convention, applied identically to every construction compared
here (including the DV01-neutral benchmark run through this same
simulator) so construction is the only thing that differs: size at entry,
re-size every REBALANCE_DAYS while in a trade, 0.5bp cost on notional
traded, 1-day lag between a decision at day t's close and its PnL.
"""

import os

import numpy as np
import pandas as pd

from src.pca_factors import FACTOR_NAMES, MATURITIES, _orient_signs, compute_daily_changes
from src.positions import DEFAULT_TARGET_DV01, DEFAULT_TRANSACTION_COST_BPS, MATURITY_YEARS, modified_duration

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")

ROLLING_PCA_WINDOW = 756  # 3 years of daily changes
REBALANCE_DAYS = 21
HEDGED_FACTORS = ["level", "slope", "curvature"]
SERIES_COLORS = ["#2a78d6", "#eb6834"]  # categorical slots 1-2, CVD-validated pair


def rolling_loadings(yields_df: pd.DataFrame, window: int = ROLLING_PCA_WINDOW) -> dict:
    """{date: (maturity x factor) loadings} from a trailing PCA, sign-oriented."""
    changes = compute_daily_changes(yields_df)
    x = changes[MATURITIES].to_numpy()
    empty_scores = pd.DataFrame(columns=FACTOR_NAMES)
    out = {}
    for i in range(window - 1, len(x)):
        w = x[i - window + 1:i + 1]
        _, vecs = np.linalg.eigh(np.cov(w.T))
        loadings = pd.DataFrame(vecs[:, ::-1], index=MATURITIES, columns=FACTOR_NAMES)
        out[changes.index[i]], _ = _orient_signs(loadings, empty_scores)
    return out


def solve_leg_exposures(loadings: pd.DataFrame, legs: list[str], target_factor: str, target_beta: float) -> pd.Series:
    """$ DV01 per leg with exposure target_beta to target_factor and 0 to the
    other hedged factors. Returns a Series over all MATURITIES (0 off-leg).
    """
    a = loadings.loc[legs, HEDGED_FACTORS].to_numpy().T  # factors x legs
    b = np.array([target_beta if f == target_factor else 0.0 for f in HEDGED_FACTORS])
    exposures = pd.Series(0.0, index=MATURITIES)
    exposures[legs] = np.linalg.solve(a, b)
    return exposures


def simulate_exposure_trade(
    yields_df: pd.DataFrame,
    position: pd.Series,
    exposure_fn,
    rebalance_days: int = REBALANCE_DAYS,
    transaction_cost_bps: float = DEFAULT_TRANSACTION_COST_BPS,
) -> pd.DataFrame:
    """Day-by-day walk for an arbitrary multi-leg trade.

    position: +1/-1/0 per day, decided at that day's close.
    exposure_fn(date): $ DV01 per maturity (Series over MATURITIES) for a
    +1 position sized as of that date's close; scaled by the position sign.
    """
    idx = position.index
    y = yields_df[MATURITIES].reindex(idx)
    dy_bps = (y.diff() * 100).to_numpy()
    dv01_pu = np.column_stack([
        modified_duration(y[m], MATURITY_YEARS[m]).to_numpy() * 0.0001 for m in MATURITIES
    ])
    pos = position.to_numpy()
    n, k = len(idx), len(MATURITIES)
    cost_rate = transaction_cost_bps / 10_000

    notional = np.zeros((n, k))
    exposure = np.zeros((n, k))
    cost = np.zeros(n)
    gross = np.zeros(n)

    cur = np.zeros(k)
    days_since_sizing = 0
    for i in range(n):
        if i > 0:
            gross[i] = np.nansum(notional[i - 1] * dv01_pu[i - 1] * dy_bps[i])

        prev_pos = pos[i - 1] if i > 0 else 0
        days_since_sizing += 1
        if pos[i] == 0:
            new = np.zeros(k)
        elif pos[i] != prev_pos or days_since_sizing >= rebalance_days:
            new = pos[i] * exposure_fn(idx[i]).to_numpy() / dv01_pu[i]
            days_since_sizing = 0
        else:
            new = cur
        cost[i] = cost_rate * np.abs(new - cur).sum()
        cur = new
        notional[i] = cur
        exposure[i] = cur * dv01_pu[i]

    result = pd.DataFrame(exposure, index=idx, columns=[f"dv01_{m}" for m in MATURITIES])
    result["position"] = pos
    result["gross_pnl"] = gross
    result["transaction_cost"] = np.concatenate([[0.0], cost[:-1]])
    result["net_pnl"] = result["gross_pnl"] - result["transaction_cost"]
    result["cumulative_pnl"] = result["net_pnl"].cumsum()
    return result


def held_exposures(trade_df: pd.DataFrame) -> pd.DataFrame:
    """Signed $ DV01 per maturity held into each day (lagged one day), the
    input attribution.attribute_exposures expects.
    """
    exposures = trade_df[[f"dv01_{m}" for m in MATURITIES]].shift(1).fillna(0.0)
    exposures.columns = MATURITIES
    return exposures


# Third leg per pair, chosen on construction quality alone (no PnL): the
# candidate with the smaller unhedged residual-factor exposure and gross
# DV01 per unit of slope exposure. 2s10s+5Y needed ~2x the gross DV01 and
# ~4x the residual exposure of 2s10s+30Y; 5s30s+10Y was worse still vs +2Y.
SLOPE_NEUTRAL_LEGS = {
    "2s10s": ["2Y", "10Y", "30Y"],
    "5s30s": ["2Y", "5Y", "30Y"],
}


def run_slope_neutral_test(save: bool = True) -> dict:
    """Same signal, two constructions: DV01-neutral (2 legs) vs. slope-only
    (3 legs, zero level and curvature exposure). Both sized to the same
    slope exposure at every sizing date and run through the same simulator,
    so the only difference is the unintended level/curvature exposure.
    """
    from src.attribution import attribute_exposures
    from src.backtest import compute_series_stats
    from src.signal import SPREAD_PAIRS, build_signal, load_yields

    yields_df = load_yields()
    loadings_by_date = rolling_loadings(yields_df)
    start = min(loadings_by_date)

    results = {}
    for pair, (short, long_) in SPREAD_PAIRS.items():
        position = build_signal(yields_df, pair=pair)["position"]
        position[position.index < start] = 0

        def dv01_neutral(date, short=short, long_=long_):
            e = pd.Series(0.0, index=MATURITIES)
            e[short], e[long_] = -DEFAULT_TARGET_DV01, DEFAULT_TARGET_DV01
            return e

        def slope_only(date, short=short, long_=long_, pair=pair):
            loadings = loadings_by_date[date]
            slope_beta = dv01_neutral(date) @ loadings["slope"]  # match the DV01 trade's slope exposure
            return solve_leg_exposures(loadings, SLOPE_NEUTRAL_LEGS[pair], "slope", slope_beta)

        print(f"\n[{pair}] same signal, {start.date()} onward (first date with a trailing 3y PCA)")
        results[pair] = {}
        for name, fn in [("dv01_neutral", dv01_neutral), ("slope_only", slope_only)]:
            trade = simulate_exposure_trade(yields_df, position, fn)
            attribution = attribute_exposures(yields_df, held_exposures(trade), trade, f"{pair} {name}")
            stats = compute_series_stats(trade["net_pnl"])
            totals = attribution.sum()
            print(f"  {name}: net PnL=${stats['total_net_pnl']:,.0f}, Sharpe={stats['sharpe']:.2f}, "
                  f"max DD=${stats['max_drawdown']:,.0f} | "
                  + ", ".join(f"{k}=${v:,.0f}" for k, v in totals.items()))
            results[pair][name] = {"trade": trade, "attribution": attribution, "stats": stats}
            if save:
                attribution.to_csv(os.path.join(DATA_DIR, f"attribution_{pair}_{name}.csv"))

    if save:
        plot_slope_test(results)
        print("\n  saved to data/attribution_{pair}_{dv01_neutral,slope_only}.csv, outputs/slope_only_vs_dv01_neutral.png")
    return results


def plot_slope_test(results: dict, outputs_dir: str = OUTPUT_DIR) -> None:
    """Factor PnL of DV01-neutral vs. slope-only, one panel per factor."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    os.makedirs(outputs_dir, exist_ok=True)
    pairs = list(results)
    constructions = [("dv01_neutral", "DV01-neutral (2 legs)"), ("slope_only", "slope-only (3 legs)")]
    fig, axes = plt.subplots(1, 3, figsize=(11, 4), sharey=True)
    width, gap = 0.38, 0.02
    x = np.arange(len(pairs))
    for ax, factor in zip(axes, HEDGED_FACTORS):
        for j, ((key, label), color) in enumerate(zip(constructions, SERIES_COLORS)):
            vals = [results[p][key]["attribution"][factor].sum() / 1e6 for p in pairs]
            pos = x + (j - 0.5) * (width + gap)
            ax.bar(pos, vals, width=width, color=color, label=label)
            for xi, v in zip(pos, vals):
                ax.annotate(f"{v:+.1f}", (xi, v), xytext=(0, 3 if v >= 0 else -3), textcoords="offset points",
                            ha="center", va="bottom" if v >= 0 else "top", fontsize=8, color="#333333")
        ax.axhline(0, color="#888888", linewidth=0.8)
        ax.set_xticks(x, pairs)
        ax.set_title(factor, fontsize=10)
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="y", color="#e5e5e5", linewidth=0.6)
        ax.set_axisbelow(True)
    lo, hi = axes[0].get_ylim()
    axes[0].set_ylim(lo - 0.08 * (hi - lo), hi)
    axes[0].set_ylabel("Cumulative PnL, 2004-2026 ($M)")
    axes[0].legend(fontsize=8, frameon=False, loc="lower left")
    fig.suptitle("Same signal, two constructions: removing curvature exposure leaves a losing slope bet", fontsize=11)
    fig.tight_layout()
    fig.savefig(os.path.join(outputs_dir, "slope_only_vs_dv01_neutral.png"), dpi=150)
    plt.close(fig)


# Curvature test: rules fixed in docs/curvature_preregistration.md before
# any of this was run. Don't tune anything here against its output.
CURVATURE_LEGS = ["2Y", "5Y", "30Y"]
CURVATURE_TARGET_BETA = 10_000  # $ per unit of curvature factor score
HOLDOUT_START = "2023-01-01"


def fly_spread(yields_df: pd.DataFrame) -> pd.Series:
    """2 x 5Y - 2Y - 30Y, in bps: rises when the belly cheapens vs. the wings."""
    return ((2 * yields_df["5Y"] - yields_df["2Y"] - yields_df["30Y"]) * 100).rename("fly_bps")


def plot_curvature_test(trades: dict, outputs_dir: str = OUTPUT_DIR) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    os.makedirs(outputs_dir, exist_ok=True)
    fig, ax = plt.subplots(figsize=(10, 4.5))
    for (label, trade), color in zip(trades.items(), [SERIES_COLORS[0], SERIES_COLORS[1]]):
        ax.plot(trade.index, trade["cumulative_pnl"], label=label, color=color)
    ax.axvspan(pd.Timestamp(HOLDOUT_START), max(t.index[-1] for t in trades.values()),
               color="gray", alpha=0.15, label="holdout (verdict)")
    ax.axhline(0, color="gray", linewidth=0.8)
    ax.set_ylabel("Cumulative net PnL ($)")
    ax.set_title("Pre-registered curvature fly (2Y/5Y/30Y, level- and slope-neutral)")
    ax.legend()
    fig.tight_layout()
    fig.savefig(os.path.join(outputs_dir, "curvature_test.png"), dpi=150)
    plt.close(fig)


def run_curvature_test(save: bool = True) -> dict:
    from src.attribution import attribute_exposures
    from src.backtest import compute_series_stats
    from src.signal import build_signal, compute_zscore, generate_positions_mean_reversion, load_yields

    yields_df = load_yields()
    loadings_by_date = rolling_loadings(yields_df)
    start = min(loadings_by_date)

    def curvature_fly(date):
        return solve_leg_exposures(loadings_by_date[date], CURVATURE_LEGS, "curvature", CURVATURE_TARGET_BETA)

    signals = {
        "H1: fly mean-reversion": generate_positions_mean_reversion(compute_zscore(fly_spread(yields_df))),
        "H2: -1 x 5s30s signal": -build_signal(yields_df, pair="5s30s")["position"],
    }

    results, trades = {}, {}
    for label, position in signals.items():
        position = position.copy()
        position[position.index < start] = 0
        trade = simulate_exposure_trade(yields_df, position, curvature_fly)
        attribution = attribute_exposures(yields_df, held_exposures(trade), trade, label)

        prev = trade["position"].shift(1).fillna(0)
        entries = trade.index[(trade["position"] != 0) & (trade["position"] != prev)]
        periods = {
            "holdout": trade.loc[HOLDOUT_START:, "net_pnl"],
            "pre-holdout": trade.loc[:pd.Timestamp(HOLDOUT_START) - pd.Timedelta(days=1), "net_pnl"],
            "full sample": trade["net_pnl"],
        }
        print(f"\n[{label}]  ({len(entries)} entries total, {int((entries >= HOLDOUT_START).sum())} in holdout)")
        stats = {}
        for name, net in periods.items():
            st = compute_series_stats(net)
            stats[name] = st
            print(f"  {name}: net PnL=${st['total_net_pnl']:,.0f}, Sharpe={st['sharpe']:.2f}, max DD=${st['max_drawdown']:,.0f}")
        totals = attribution.sum()
        print("  full-sample attribution: " + ", ".join(f"{k}=${v:,.0f}" for k, v in totals.items()))
        verdict = stats["holdout"]["total_net_pnl"] > 0 and stats["holdout"]["sharpe"] > 0
        print(f"  pre-registered verdict: {'PASS' if verdict else 'FAIL'}")

        results[label] = {"trade": trade, "attribution": attribution, "stats": stats, "pass": verdict}
        trades[label] = trade

    if save:
        os.makedirs(DATA_DIR, exist_ok=True)
        for label, res in results.items():
            tag = label.split(":")[0].lower()
            res["trade"].to_csv(os.path.join(DATA_DIR, f"curvature_test_{tag}.csv"))
        plot_curvature_test(trades)
        print("\n  saved to data/curvature_test_{h1,h2}.csv, outputs/curvature_test.png")
    return results


if __name__ == "__main__":
    run_slope_neutral_test()
    run_curvature_test()
