"""Full stack: risk-controlled curve trade + credit hedge overlay together.

Steps 6 (credit hedge) and 7 (risk controls) were each validated on their
own against the curve-only baseline; this module runs them together.

It also answers whether the credit hedge needs its own stop-loss the way
the curve legs did. It doesn't, at least not at the same $500k level: the
hedge's worst single activation lost ~$342k, so a stop never fires.
The hedge's ex-GFC losses are a slow bleed across many small activations,
not a few large ones a stop could cut short (see run_hedge_stop_check).
"""

import os

import matplotlib
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from src.backtest import compute_series_stats
from src.credit_hedge import DEFAULT_WINDOW as CREDIT_WINDOW
from src.credit_hedge import compute_zscore, load_credit_data, simulate_hedge
from src.risk_controls import DEFAULT_MAX_HOLD_DAYS, DEFAULT_STOP_LOSS_DOLLARS, simulate_curve_trade
from src.signal import SPREAD_PAIRS, load_yields

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")

# The single hedge activation that carries the overlay (see README, step 6).
GFC_HEDGE_WINDOW = ("2007-07-26", "2009-05-22")


def run_hedge_stop_check(credit_df: pd.DataFrame, zscore: pd.Series) -> None:
    print("credit hedge stop-loss check:")
    for stop in (None, 250_000, DEFAULT_STOP_LOSS_DOLLARS):
        for cooldown in ((True,) if stop is None else (False, True)):
            res = simulate_hedge(credit_df, zscore, stop_loss_dollars=stop, stop_loss_reentry_cooldown=cooldown)
            n_stop = int((res["exit_reason"] == "stop_loss").sum())
            label = "no stop" if stop is None else f"stop ${stop:,.0f}, cooldown={cooldown}"
            print(f"  {label}: net PnL=${res['net_pnl'].sum():,.0f}, {n_stop} stop-loss exits")


def plot_stack(layers: dict[str, pd.Series], pair: str, outputs_dir: str = OUTPUT_DIR) -> None:
    os.makedirs(outputs_dir, exist_ok=True)
    fig, ax = plt.subplots(figsize=(10, 4.5))
    colors = ["steelblue", "seagreen", "darkorange", "gray"]
    for (label, cumulative), color in zip(layers.items(), colors):
        style = "--" if "ex-GFC" in label else "-"
        ax.plot(cumulative.index, cumulative, label=label, color=color, linestyle=style)
    ax.axhline(0, color="gray", linewidth=0.8)
    ax.set_ylabel("Cumulative net PnL ($)")
    ax.set_title(f"{pair}: full stack (curve + risk controls + credit hedge)")
    ax.legend()
    fig.tight_layout()
    fig.savefig(os.path.join(outputs_dir, f"backtest_{pair}_full_stack.png"), dpi=150)
    plt.close(fig)


def run_full_stack_pipeline(save: bool = True) -> dict:
    yields_df = load_yields()
    credit_df = load_credit_data()
    credit_z = compute_zscore(credit_df["credit_spread_bps"], CREDIT_WINDOW)

    run_hedge_stop_check(credit_df, credit_z)
    hedge = simulate_hedge(credit_df, credit_z)
    hedge_ex_gfc = hedge["net_pnl"].copy()
    hedge_ex_gfc[GFC_HEDGE_WINDOW[0]:GFC_HEDGE_WINDOW[1]] = 0.0

    results = {}
    for pair in SPREAD_PAIRS:
        baseline = simulate_curve_trade(yields_df, pair)
        controlled = simulate_curve_trade(
            yields_df, pair, max_hold_days=DEFAULT_MAX_HOLD_DAYS, stop_loss_dollars=DEFAULT_STOP_LOSS_DOLLARS,
        )
        hedge_net = hedge["net_pnl"].reindex(controlled.index).fillna(0)
        variants = {
            "baseline": baseline["net_pnl"],
            "+ risk controls": controlled["net_pnl"],
            "+ risk controls + credit hedge": controlled["net_pnl"] + hedge_net,
            "full stack, ex-GFC hedge activation": controlled["net_pnl"] + hedge_ex_gfc.reindex(controlled.index).fillna(0),
        }
        stats = {name: compute_series_stats(net) for name, net in variants.items()}

        print(f"\n[{pair}]")
        for name, s in stats.items():
            print(f"  {name}: net PnL=${s['total_net_pnl']:,.0f}, Sharpe={s['sharpe']:.2f}, max DD=${s['max_drawdown']:,.0f}")

        results[pair] = {"controlled": controlled, "hedge_net": hedge_net, "stats": stats}

        if save:
            os.makedirs(DATA_DIR, exist_ok=True)
            out = controlled[["net_pnl", "position"]].rename(columns={"net_pnl": "curve_net_pnl"})
            out["hedge_net_pnl"] = hedge_net
            out["net_pnl"] = out["curve_net_pnl"] + out["hedge_net_pnl"]
            out["cumulative_pnl"] = out["net_pnl"].cumsum()
            out.to_csv(os.path.join(DATA_DIR, f"full_stack_{pair}.csv"))
            plot_stack({name: s["cumulative"] for name, s in stats.items()}, pair)
            print(f"  saved to data/full_stack_{pair}.csv, outputs/backtest_{pair}_full_stack.png")

    return results


if __name__ == "__main__":
    run_full_stack_pipeline()
