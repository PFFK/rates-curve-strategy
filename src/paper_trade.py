"""Forward paper trading of the pre-registered H1 curvature fly.

The 2023-26 holdout was short and partly seen in aggregate before the test
(docs/curvature_preregistration.md), so the only fully clean test of H1 is
forward: decisions recorded before the outcomes exist.

Each run:
1. Checks the H1 rules are still the pre-registered ones, by recomputing H1
   on the committed backtest data and matching its exact recorded results.
   Any code change that alters the rules fails here and nothing is traded.
2. Pulls fresh yields from FRED into data/paper/yields_live.csv, using the
   backtest's fixed history start (not data_loader's rolling "today minus
   25 years", which would shift the backtest's own data if reused).
3. Rebuilds H1 over the full live history with the same code the backtest
   uses (factor_trades.build_curvature_trade) and appends rows for dates
   after the ledger's last row to data/paper/h1_ledger.csv.

The ledger is append-only: past rows are never rewritten. If FRED revises
a past yield and the recomputed position or legs no longer match the
ledger, that's logged to data/paper/revisions.log and the ledger stands.

The first run writes a "bootstrap" row: the position H1 holds as of that
close, carried in from the backtest (same convention as the holdout).
Forward PnL starts the next trading day.

Usage:
    python -m src.paper_trade           # update the ledger
    python -m src.paper_trade --report  # print forward-test stats
"""

import argparse
import os
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from src.factor_trades import (
    CURVATURE_LEGS, H1_LABEL, HOLDOUT_START, build_curvature_trade, curvature_signals, fly_spread,
)
from src.signal import compute_zscore, load_yields

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAPER_DIR = os.path.join(BASE_DIR, "data", "paper")
LIVE_YIELDS_PATH = os.path.join(PAPER_DIR, "yields_live.csv")
LEDGER_PATH = os.path.join(PAPER_DIR, "h1_ledger.csv")
REVISIONS_LOG = os.path.join(PAPER_DIR, "revisions.log")

LIVE_HISTORY_START = "2001-08-06"  # first date of the committed backtest data

# Fingerprint of the pre-registered H1 result on the committed backtest data.
FROZEN_H1_HOLDOUT_PNL = 51_604.38
FROZEN_H1_FULL_PNL = 2_632_624.10

LEG_COLS = [f"dv01_{m}" for m in CURVATURE_LEGS]
# A row recorded more than this long after its date is labeled "late": its
# position still follows the frozen rules, but its timestamp no longer shows
# it was logged before the outcome (e.g. catch-up after failed runs). Covers
# FRED's ~1-day publication lag plus a weekend.
LATE_AFTER = pd.Timedelta(days=4)

LEDGER_COLS = ["fly_bps", "zscore", "position", *LEG_COLS, "gross_pnl", "transaction_cost", "net_pnl"]


def h1_trade(yields_df: pd.DataFrame) -> pd.DataFrame:
    trade = build_curvature_trade(yields_df, curvature_signals(yields_df)[H1_LABEL])
    trade["fly_bps"] = fly_spread(yields_df)
    trade["zscore"] = compute_zscore(trade["fly_bps"])
    return trade


def verify_frozen_rules() -> None:
    trade = h1_trade(load_yields())
    holdout = trade.loc[HOLDOUT_START:, "net_pnl"].sum()
    full = trade["net_pnl"].sum()
    if abs(holdout - FROZEN_H1_HOLDOUT_PNL) > 0.01 or abs(full - FROZEN_H1_FULL_PNL) > 0.01:
        raise RuntimeError(
            f"H1 no longer reproduces its pre-registered result (holdout ${holdout:,.2f} vs "
            f"${FROZEN_H1_HOLDOUT_PNL:,.2f}, full ${full:,.2f} vs ${FROZEN_H1_FULL_PNL:,.2f}). "
            "The rules or the committed backtest data changed; not trading."
        )


def fetch_live_yields() -> pd.DataFrame:
    from src.data_loader import clean_yields, fetch_yields, get_fred_client

    live = clean_yields(fetch_yields(get_fred_client(), LIVE_HISTORY_START))
    os.makedirs(PAPER_DIR, exist_ok=True)
    live.to_csv(LIVE_YIELDS_PATH)
    return live


def load_ledger() -> pd.DataFrame | None:
    if not os.path.exists(LEDGER_PATH):
        return None
    return pd.read_csv(LEDGER_PATH, index_col="date", parse_dates=True)


def check_revisions(ledger: pd.DataFrame, trade: pd.DataFrame) -> None:
    recomputed = trade.reindex(ledger.index)
    cols = ["position", *LEG_COLS]
    diff = (recomputed[cols] - ledger[cols]).abs()
    mismatched = diff.index[(diff > 0.01).any(axis=1) | recomputed[cols].isna().any(axis=1)]
    if len(mismatched):
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with open(REVISIONS_LOG, "a") as f:
            f.write(f"{stamp}: recomputed H1 differs from ledger on {len(mismatched)} date(s): "
                    f"{', '.join(str(d.date()) for d in mismatched[:10])}\n")
        print(f"WARNING: data revision changed {len(mismatched)} ledgered date(s); logged to {REVISIONS_LOG}, ledger unchanged")


def update_ledger() -> pd.DataFrame:
    verify_frozen_rules()
    live = fetch_live_yields()
    trade = h1_trade(live)
    ledger = load_ledger()
    recorded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if ledger is None:
        new = trade[LEDGER_COLS].iloc[[-1]].copy()
        new[["gross_pnl", "transaction_cost", "net_pnl"]] = 0.0  # PnL before go-live isn't forward PnL
        new["row_type"] = "bootstrap"
        cumulative_start = 0.0
    else:
        check_revisions(ledger, trade)
        new = trade.loc[trade.index > ledger.index[-1], LEDGER_COLS].copy()
        now = pd.Timestamp(recorded_at).tz_localize(None)
        new["row_type"] = np.where(now - new.index > LATE_AFTER, "late", "live")
        cumulative_start = ledger["cumulative_pnl"].iloc[-1]

    if new.empty:
        print(f"No new data after {ledger.index[-1].date()}; ledger unchanged.")
        return ledger

    new["cumulative_pnl"] = cumulative_start + new["net_pnl"].cumsum()
    new["recorded_at"] = recorded_at
    new.index.name = "date"
    new.to_csv(LEDGER_PATH, mode="a", header=ledger is None)

    last = new.iloc[-1]
    print(f"Appended {len(new)} row(s) through {new.index[-1].date()}: position={int(last['position']):+d}, "
          + ", ".join(f"{c}=${last[c]:,.0f}" for c in LEG_COLS)
          + f", forward cumulative PnL=${last['cumulative_pnl']:,.0f}")
    return load_ledger()


def report() -> None:
    from src.backtest import compute_series_stats

    ledger = load_ledger()
    if ledger is None:
        print("No ledger yet; run `python -m src.paper_trade` first.")
        return
    live = ledger[ledger["row_type"] != "bootstrap"]
    n_late = int((ledger["row_type"] == "late").sum())
    go_live = ledger.index[0].date()
    print(f"H1 forward test: live since {go_live} close, {len(live)} trading day(s) of forward PnL"
          + (f" ({n_late} recorded late, see row_type)" if n_late else ""))
    current = ledger.iloc[-1]
    print(f"  current position: {int(current['position']):+d} as of {ledger.index[-1].date()} "
          f"(fly {current['fly_bps']:.1f}bp, z={current['zscore']:.2f})")
    if live.empty:
        return
    stats = compute_series_stats(live["net_pnl"])
    position = ledger["position"]
    entries = int(((position != 0) & (position != position.shift(1))).iloc[1:].sum())
    sharpe = f"{stats['sharpe']:.2f}" if len(live) >= 60 else "n/a (<60 days)"
    print(f"  net PnL=${stats['total_net_pnl']:,.0f}, Sharpe={sharpe}, max DD=${stats['max_drawdown']:,.0f}, "
          f"{entries} new entries since go-live")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--report", action="store_true", help="print forward-test stats instead of updating")
    args = parser.parse_args()
    report() if args.report else update_ledger()
