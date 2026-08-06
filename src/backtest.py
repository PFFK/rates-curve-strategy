"""Backtest engine: daily PnL, cumulative PnL, drawdowns, and summary stats.

PnL uses each leg's actual currently-held DV01 (notional fixed since the
last sizing event, multiplied by that day's duration-implied per-unit DV01),
not the idealized target_dv01 -- so the drift positions.py introduces
between rebalances flows through into realized PnL, not just an idealized
constant-DV01 approximation.

The position/DV01 used to compute day t's PnL is lagged by one day: a
signal computed from day t's close can only be acted on starting day t+1,
so trading on the same close used to generate it would be lookahead.
"""

import os

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from src.positions import build_positions
from src.signal import SPREAD_PAIRS, build_signal, load_yields

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")

TRADING_DAYS_PER_YEAR = 252


def compute_daily_pnl(yields_df: pd.DataFrame, positions_df: pd.DataFrame, pair: str) -> pd.DataFrame:
    short, long_ = SPREAD_PAIRS[pair]
    d_short_bps = yields_df[short].reindex(positions_df.index).diff() * 100
    d_long_bps = yields_df[long_].reindex(positions_df.index).diff() * 100

    position_lag = positions_df["position"].shift(1).fillna(0)
    dv01_short_lag = positions_df["current_dv01_short"].shift(1).fillna(0)
    dv01_long_lag = positions_df["current_dv01_long"].shift(1).fillna(0)
    cost_lag = positions_df["transaction_cost"].shift(1).fillna(0)

    gross_pnl = position_lag * (-dv01_short_lag * d_short_bps + dv01_long_lag * d_long_bps)
    gross_pnl = gross_pnl.fillna(0)
    net_pnl = gross_pnl - cost_lag

    result = pd.DataFrame({
        "gross_pnl": gross_pnl,
        "transaction_cost": cost_lag,
        "net_pnl": net_pnl,
        "position": positions_df["position"],
    })
    result["cumulative_pnl"] = result["net_pnl"].cumsum()
    result["running_max"] = result["cumulative_pnl"].cummax()
    result["drawdown"] = result["cumulative_pnl"] - result["running_max"]
    return result


def summarize_backtest(pnl_df: pd.DataFrame) -> dict:
    daily = pnl_df["net_pnl"]
    sharpe = daily.mean() / daily.std() * np.sqrt(TRADING_DAYS_PER_YEAR) if daily.std() > 0 else np.nan
    max_drawdown = pnl_df["drawdown"].min()

    position = pnl_df["position"]
    prev = position.shift(1).fillna(0)
    entries = pnl_df.index[(prev == 0) & (position != 0)]
    exits = list(pnl_df.index[(prev != 0) & (position == 0)])

    trade_pnls = []
    for e in entries:
        later_exits = [x for x in exits if x > e]
        x = later_exits[0] if later_exits else pnl_df.index[-1]
        trade_pnls.append(pnl_df.loc[e:x, "net_pnl"].sum())
    trade_pnls = pd.Series(trade_pnls)
    win_rate = (trade_pnls > 0).mean() if len(trade_pnls) else np.nan

    return {
        "total_net_pnl": daily.sum(),
        "total_gross_pnl": pnl_df["gross_pnl"].sum(),
        "total_transaction_cost": pnl_df["transaction_cost"].sum(),
        "sharpe": sharpe,
        "max_drawdown": max_drawdown,
        "n_trades": len(trade_pnls),
        "trade_win_rate": win_rate,
    }


def plot_backtest(pnl_df: pd.DataFrame, pair: str, outputs_dir: str = OUTPUT_DIR) -> None:
    os.makedirs(outputs_dir, exist_ok=True)
    fig, axes = plt.subplots(2, 1, figsize=(10, 6), sharex=True, height_ratios=[2, 1])

    axes[0].plot(pnl_df.index, pnl_df["cumulative_pnl"], color="steelblue")
    axes[0].set_ylabel("Cumulative net PnL ($)")
    axes[0].set_title(f"{pair}: Cumulative PnL")
    axes[0].axhline(0, color="gray", linewidth=0.8)

    axes[1].fill_between(pnl_df.index, pnl_df["drawdown"], 0, color="firebrick", alpha=0.6)
    axes[1].set_ylabel("Drawdown ($)")
    axes[1].set_xlabel("Date")

    fig.tight_layout()
    fig.savefig(os.path.join(outputs_dir, f"backtest_{pair}.png"), dpi=150)
    plt.close(fig)


def compute_series_stats(net_pnl: pd.Series) -> dict:
    cumulative = net_pnl.cumsum()
    running_max = cumulative.cummax()
    drawdown = cumulative - running_max
    sharpe = net_pnl.mean() / net_pnl.std() * np.sqrt(TRADING_DAYS_PER_YEAR) if net_pnl.std() > 0 else np.nan
    return {
        "total_net_pnl": net_pnl.sum(),
        "sharpe": sharpe,
        "max_drawdown": drawdown.min(),
        "cumulative": cumulative,
        "drawdown": drawdown,
    }


def plot_combined(curve_cumulative: pd.Series, combined_cumulative: pd.Series, pair: str, outputs_dir: str = OUTPUT_DIR) -> None:
    os.makedirs(outputs_dir, exist_ok=True)
    fig, ax = plt.subplots(figsize=(10, 4.5))
    ax.plot(curve_cumulative.index, curve_cumulative, label="curve only", color="steelblue")
    ax.plot(combined_cumulative.index, combined_cumulative, label="curve + credit hedge", color="darkorange")
    ax.axhline(0, color="gray", linewidth=0.8)
    ax.set_ylabel("Cumulative net PnL ($)")
    ax.set_title(f"{pair}: curve-only vs. curve + credit hedge")
    ax.legend()
    fig.tight_layout()
    fig.savefig(os.path.join(outputs_dir, f"backtest_{pair}_with_credit_hedge.png"), dpi=150)
    plt.close(fig)


def run_combined_backtest_pipeline(save: bool = True) -> dict:
    from src.credit_hedge import build_credit_hedge

    curve_results = run_backtest_pipeline(save=save)
    credit_pnl_df = build_credit_hedge()
    credit_net = credit_pnl_df["net_pnl"]

    print()
    for pair, res in curve_results.items():
        curve_net = res["pnl"]["net_pnl"]
        curve_stats = res["stats"]
        combined_net = curve_net.add(credit_net, fill_value=0)
        combined_stats = compute_series_stats(combined_net)
        res["combined_stats"] = combined_stats

        print(f"[{pair} + credit hedge] net PnL=${combined_stats['total_net_pnl']:,.0f} "
              f"(curve-only was ${curve_stats['total_net_pnl']:,.0f}), "
              f"Sharpe={combined_stats['sharpe']:.2f} (curve-only was {curve_stats['sharpe']:.2f}), "
              f"max drawdown=${combined_stats['max_drawdown']:,.0f} (curve-only was ${curve_stats['max_drawdown']:,.0f})")

        if save:
            plot_combined(res["pnl"]["cumulative_pnl"], combined_stats["cumulative"], pair)
            print(f"  saved to outputs/backtest_{pair}_with_credit_hedge.png")

    return curve_results


def run_backtest_pipeline(save: bool = True) -> dict:
    yields_df = load_yields()

    results = {}
    for pair in SPREAD_PAIRS:
        signal_df = build_signal(yields_df, pair=pair)
        positions_df = build_positions(yields_df, signal_df, pair)
        pnl_df = compute_daily_pnl(yields_df, positions_df, pair)
        stats = summarize_backtest(pnl_df)

        print(f"[{pair}] net PnL=${stats['total_net_pnl']:,.0f} "
              f"(gross=${stats['total_gross_pnl']:,.0f}, costs=${stats['total_transaction_cost']:,.0f}), "
              f"Sharpe={stats['sharpe']:.2f}, max drawdown=${stats['max_drawdown']:,.0f}, "
              f"{stats['n_trades']} trades, win rate={stats['trade_win_rate']:.1%}")

        results[pair] = {"pnl": pnl_df, "stats": stats}

        if save:
            os.makedirs(DATA_DIR, exist_ok=True)
            pnl_df.to_csv(os.path.join(DATA_DIR, f"backtest_{pair}.csv"))
            plot_backtest(pnl_df, pair)
            print(f"  saved to data/backtest_{pair}.csv, outputs/backtest_{pair}.png")

    return results


if __name__ == "__main__":
    run_combined_backtest_pipeline()
