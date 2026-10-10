"""Forward paper trading of the pre-registered G10 FX carry strategy.

The holdout passed (Sharpe 0.42) but with weak statistical power, so the
remaining test is forward. Runs the frozen rules (fx_carry.py) as written,
including the 3-month carry-forward rule: a currency whose OECD rate is
too stale (EUR and GBP at go-live) sits out of the ranking until the OECD
publishes again. Swapping in a fresher rate source would be a new test.

What gets timestamped, and why it differs from paper_trade.py: FRED's
daily FX series come from the weekly H.10 release, so price rows always
arrive days late. But daily PnL is mechanical once the month's book is
set; the decision is the book. So each run:

1. Checks the rules are still the pre-registered ones, by recomputing on
   the committed data/fx/ files and matching the recorded in-sample and
   holdout net returns.
2. Pulls fresh spot and rates into data/paper/fx_*_live.csv.
3. For each calendar month-end since the last recorded book, ranks the
   currencies on the rates published *as of this run* and appends the book
   to data/paper/fx_books.csv. A book recorded more than LATE_AFTER after
   its month-end is labeled "late".
4. Prices every day after go-live off the recorded books (never
   recomputed ones) and appends new days to data/paper/fx_ledger.csv.

Both files are append-only. If a spot revision changes the PnL of an
already-ledgered day, that's logged to data/paper/revisions.log.

Usage:
    python -m src.fx_paper_trade           # update books and ledger
    python -m src.fx_paper_trade --report  # print forward-test stats
"""

import argparse
import os
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from src.fx_carry import (
    CURRENCIES, IN_SAMPLE_END, TRADING_DAYS_PER_YEAR, book_row, fetch_fx_frames, load_fx_data, pnl_from_book,
    signal_rates, simulate,
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAPER_DIR = os.path.join(BASE_DIR, "data", "paper")
SPOT_LIVE_PATH = os.path.join(PAPER_DIR, "fx_spot_live.csv")
RATES_LIVE_PATH = os.path.join(PAPER_DIR, "fx_rates_live.csv")
BOOKS_PATH = os.path.join(PAPER_DIR, "fx_books.csv")
LEDGER_PATH = os.path.join(PAPER_DIR, "fx_ledger.csv")
REVISIONS_LOG = os.path.join(PAPER_DIR, "revisions.log")

# Net return sums of the pre-registered run on the committed data/fx/ files.
FROZEN_IN_SAMPLE_NET = 0.395497482802
FROZEN_HOLDOUT_NET = 0.130238229481

# Weekend plus the few hours GitHub's scheduler tends to run late.
LATE_AFTER = pd.Timedelta(days=4)

BOOK_COLS = [f"w_{c}" for c in CURRENCIES] + [f"rate_{c}" for c in CURRENCIES]
PNL_COLS = ["spot", "carry", "cost", "net"]


def verify_frozen_rules() -> None:
    spot, rates = load_fx_data(include_holdout=True)
    daily = simulate(spot, rates)
    in_sample = daily.loc[:IN_SAMPLE_END, "net"].sum()
    holdout = daily.loc[pd.Timestamp(IN_SAMPLE_END) + pd.Timedelta(days=1):, "net"].sum()
    if abs(in_sample - FROZEN_IN_SAMPLE_NET) > 1e-9 or abs(holdout - FROZEN_HOLDOUT_NET) > 1e-9:
        raise RuntimeError(
            f"FX carry no longer reproduces its pre-registered result (in-sample {in_sample:.12f} vs "
            f"{FROZEN_IN_SAMPLE_NET:.12f}, holdout {holdout:.12f} vs {FROZEN_HOLDOUT_NET:.12f}). "
            "The rules or the committed data/fx/ files changed; not trading."
        )


def _read(path: str) -> pd.DataFrame | None:
    if not os.path.exists(path):
        return None
    return pd.read_csv(path, index_col=0, parse_dates=True)


def _log_revision(message: str) -> None:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(REVISIONS_LOG, "a") as f:
        f.write(f"{stamp}: FX {message}\n")
    print(f"WARNING: {message}; logged to {REVISIONS_LOG}")


def record_books(rates: pd.DataFrame, now: pd.Timestamp) -> pd.DataFrame:
    """Append a book for every calendar month-end since the last recorded one."""
    books = _read(BOOKS_PATH)
    today = now.tz_convert(None).normalize()
    latest_month_end = today.to_period("M").to_timestamp() - pd.Timedelta(days=1)
    sig = signal_rates(rates)
    recorded_at = now.isoformat(timespec="seconds")

    if books is None:
        targets, row_type = [latest_month_end], "bootstrap"
    else:
        targets = pd.date_range(books.index[-1], latest_month_end, freq="ME")[1:]
        row_type = None

    new = []
    for month_end in targets:
        row = book_row(sig, month_end)
        if row is None:
            print(f"{month_end.date()}: ranking not possible from published rates; previous book carries over")
            continue
        row["row_type"] = row_type or ("late" if today - month_end > LATE_AFTER else "live")
        row["recorded_at"] = recorded_at
        new.append(row)
        longs = [c for c in CURRENCIES if row[f"w_{c}"] > 0]
        shorts = [c for c in CURRENCIES if row[f"w_{c}"] < 0]
        dropped = [c for c in CURRENCIES if pd.isna(row[f"rate_{c}"])]
        print(f"book {month_end.date()} ({row['row_type']}): long {longs}, short {shorts}"
              + (f", sitting out (stale rates): {dropped}" if dropped else ""))

    if new:
        df = pd.DataFrame(new)
        df.index.name = "month_end"
        df.to_csv(BOOKS_PATH, mode="a", header=books is None)
    return _read(BOOKS_PATH)


def record_pnl(spot: pd.DataFrame, books: pd.DataFrame, now: pd.Timestamp) -> None:
    ledger = _read(LEDGER_PATH)
    go_live = pd.Timestamp(books["recorded_at"].iloc[0]).tz_convert(None).normalize()
    if spot.index[-1] <= books.index[0]:
        print(f"FX prices only reach {spot.index[-1].date()} (H.10 is weekly); nothing to price yet.")
        return
    daily = pnl_from_book(spot, books[BOOK_COLS].astype(float))
    # month-end of the book in force each day: latest book dated strictly before it
    held_book = pd.Series(books.index[np.clip(books.index.searchsorted(daily.index) - 1, 0, None)], index=daily.index)

    if ledger is not None:
        overlap = ledger.index.intersection(daily.index)
        drift = (daily.loc[overlap, "net"] - ledger.loc[overlap, "net"]).abs()
        if (drift > 1e-9).any():
            _log_revision(f"spot revision changed ledgered PnL on {int((drift > 1e-9).sum())} day(s), "
                          f"max {drift.max():.6%}; ledger unchanged")

    after = max(go_live, ledger.index[-1]) if ledger is not None else go_live
    new = daily.loc[daily.index > after, PNL_COLS].copy()
    if new.empty:
        print(f"No new FX prices after {after.date()}; ledger unchanged.")
        return
    start = ledger["cumulative_net"].iloc[-1] if ledger is not None else 0.0
    new["cumulative_net"] = start + new["net"].cumsum()
    new["book"] = held_book.loc[new.index].dt.date
    new["recorded_at"] = now.isoformat(timespec="seconds")
    new.index.name = "date"
    new.to_csv(LEDGER_PATH, mode="a", header=ledger is None)
    print(f"Appended {len(new)} FX day(s) through {new.index[-1].date()}, "
          f"forward cumulative net {new['cumulative_net'].iloc[-1]:+.2%}")


def update() -> None:
    verify_frozen_rules()
    spot, rates = fetch_fx_frames()
    os.makedirs(PAPER_DIR, exist_ok=True)
    spot.to_csv(SPOT_LIVE_PATH)
    rates.to_csv(RATES_LIVE_PATH)

    now = pd.Timestamp(datetime.now(timezone.utc))
    books = record_books(rates, now)
    record_pnl(spot, books, now)


def report() -> None:
    books, ledger = _read(BOOKS_PATH), _read(LEDGER_PATH)
    if books is None:
        print("No FX books yet; run `python -m src.fx_paper_trade` first.")
        return
    go_live = pd.Timestamp(books["recorded_at"].iloc[0]).date()
    current = books.iloc[-1]
    longs = [c for c in CURRENCIES if current[f"w_{c}"] > 0]
    shorts = [c for c in CURRENCIES if current[f"w_{c}"] < 0]
    n_late = int((books["row_type"] == "late").sum())
    print(f"FX carry forward test: live since {go_live}, {len(books)} book(s) recorded"
          + (f" ({n_late} late)" if n_late else ""))
    print(f"  current book ({books.index[-1].date()}): long {longs}, short {shorts}")
    if ledger is None or ledger.empty:
        print("  no forward PnL yet (FX prices arrive weekly via the H.10 release)")
        return
    net = ledger["net"]
    line = f"  {len(net)} day(s) through {ledger.index[-1].date()}: cumulative net {net.sum():+.2%}"
    if len(net) >= 60:
        line += (f", ann. return {net.mean() * TRADING_DAYS_PER_YEAR:.2%}, "
                 f"Sharpe {net.mean() / net.std() * np.sqrt(TRADING_DAYS_PER_YEAR):.2f}")
    else:
        line += ", Sharpe n/a (<60 days)"
    print(line)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--report", action="store_true", help="print forward-test stats instead of updating")
    args = parser.parse_args()
    report() if args.report else update()
