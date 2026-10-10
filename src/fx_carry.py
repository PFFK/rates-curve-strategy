"""G10 FX carry, implemented exactly as docs/fx_carry_preregistration.md.

Long the 3 highest-yielding / short the 3 lowest-yielding of 10 currencies
(USD included; a USD slot is cash), ranked at each month-end by the
previous month's OECD 3-month interbank rate. Each foreign position earns
its spot return vs. USD plus the rate differential to USD (covered
interest parity standing in for forward points), less 2bp of notional
traded per rebalance and 1bp/month of gross notional for forward rolls.

Holdout lock: fetch_fx_data downloads the full history, but load_fx_data
only returns dates through IN_SAMPLE_END unless include_holdout=True.
That flag is used once, for the final pre-registered run.

Simplification: weights are held at their rebalance values all month
(constant notional), rather than drifting with spot moves in between.
"""

import os

import numpy as np
import pandas as pd

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FX_DIR = os.path.join(BASE_DIR, "data", "fx")
SPOT_PATH = os.path.join(FX_DIR, "spot.csv")
RATES_PATH = os.path.join(FX_DIR, "rates.csv")
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")

FETCH_START = "2001-01-01"
IN_SAMPLE_END = "2020-12-31"

# FRED H.10 spot series; invert=True where FRED quotes foreign units per USD.
SPOT_SERIES = {
    "EUR": ("DEXUSEU", False),
    "JPY": ("DEXJPUS", True),
    "GBP": ("DEXUSUK", False),
    "CHF": ("DEXSZUS", True),
    "CAD": ("DEXCAUS", True),
    "AUD": ("DEXUSAL", False),
    "NZD": ("DEXUSNZ", False),
    "NOK": ("DEXNOUS", True),
    "SEK": ("DEXSDUS", True),
}
RATE_COUNTRY = {"USD": "US", "EUR": "EZ", "JPY": "JP", "GBP": "GB", "CHF": "CH",
                "CAD": "CA", "AUD": "AU", "NZD": "NZ", "NOK": "NO", "SEK": "SE"}
CURRENCIES = list(RATE_COUNTRY)
FOREIGN = list(SPOT_SERIES)

N_LONG = N_SHORT = 3
RATE_LAG_MONTHS = 1
MAX_RATE_CARRY_FORWARD_MONTHS = 3
TRADE_COST_BPS = 2.0
ROLL_COST_BPS_PER_MONTH = 1.0
DAY_COUNT = 360
TRADING_DAYS_PER_YEAR = 252


def fetch_fx_frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Full spot (USD per unit of foreign currency) and monthly rate history from FRED."""
    from src.data_loader import get_fred_client

    fred = get_fred_client()
    spot = {}
    for ccy, (sid, invert) in SPOT_SERIES.items():
        s = fred.get_series(sid, observation_start=FETCH_START)
        spot[ccy] = 1 / s if invert else s
    spot_df = pd.DataFrame(spot).ffill(limit=5).dropna(how="all")
    spot_df.index.name = "date"

    rates = {ccy: fred.get_series(f"IR3TIB01{c}M156N", observation_start=FETCH_START) for ccy, c in RATE_COUNTRY.items()}
    rates_df = pd.DataFrame(rates)
    rates_df.index.name = "month"
    return spot_df, rates_df


def fetch_fx_data() -> None:
    """Download full spot and rate history to data/fx/. Prints coverage
    only; no returns or statistics, so the holdout stays unseen.
    """
    spot_df, rates_df = fetch_fx_frames()
    os.makedirs(FX_DIR, exist_ok=True)
    spot_df.to_csv(SPOT_PATH)
    rates_df.to_csv(RATES_PATH)
    print(f"spot: {len(spot_df)} rows, {spot_df.index.min().date()}+; rates: {len(rates_df)} months, "
          + ", ".join(f"{c} to {rates_df[c].last_valid_index().date()}" for c in CURRENCIES))


def load_fx_data(include_holdout: bool = False) -> tuple[pd.DataFrame, pd.DataFrame]:
    spot = pd.read_csv(SPOT_PATH, index_col="date", parse_dates=True)
    rates = pd.read_csv(RATES_PATH, index_col="month", parse_dates=True)
    if not include_holdout:
        spot = spot.loc[:IN_SAMPLE_END]
        rates = rates.loc[:IN_SAMPLE_END]
    return spot, rates


def signal_rates(rates: pd.DataFrame) -> pd.DataFrame:
    """Rate usable at the end of each calendar month: month M-1's value,
    carried forward at most MAX_RATE_CARRY_FORWARD_MONTHS if missing.
    Indexed by month-start of the month whose end it applies to.
    """
    monthly = rates.resample("MS").last()
    # extend the index so the latest published month still lands on the
    # month it applies to after the lag shift
    extended = pd.date_range(monthly.index[0], periods=len(monthly) + RATE_LAG_MONTHS, freq="MS")
    monthly = monthly.reindex(extended)
    return monthly.ffill(limit=MAX_RATE_CARRY_FORWARD_MONTHS).shift(RATE_LAG_MONTHS)


def book_row(sig: pd.DataFrame, month_end: pd.Timestamp) -> pd.Series | None:
    """The book set at month_end from signal_rates output, or None if the
    ranking can't be formed (no lagged USD rate, or fewer than 6 currencies).
    """
    key = month_end.to_period("M").to_timestamp()
    if key not in sig.index:
        return None
    r = sig.loc[key].dropna()
    if len(r) < N_LONG + N_SHORT or "USD" not in r:
        return None
    # Ties at a cutoff split that slot's weight equally among the tied
    # currencies (amendment, docs/fx_carry_preregistration.md): an ordinary
    # sort breaks ties arbitrarily, and differently on macOS vs. Linux.
    ranked = r.sort_values(ascending=False)
    long_cut, short_cut = ranked.iloc[N_LONG - 1], ranked.iloc[-N_SHORT]
    if long_cut <= short_cut:
        return None  # ties span both cutoffs; no unambiguous ranking
    w = pd.Series(0.0, index=CURRENCIES)
    for cut, n, sign, beyond in [(long_cut, N_LONG, 1, r > long_cut), (short_cut, N_SHORT, -1, r < short_cut)]:
        tied = r.index[r == cut]
        w[r.index[beyond]] = sign / n
        w[tied] = sign * (n - beyond.sum()) / n / len(tied)
    return pd.concat([w.rename(lambda c: f"w_{c}"), sig.loc[key].rename(lambda c: f"rate_{c}")]).rename(month_end)


def build_portfolio(spot: pd.DataFrame, rates: pd.DataFrame) -> pd.DataFrame:
    """Daily weights (set at month-end closes, effective the next day) and
    the lagged rates they were ranked on.
    """
    sig = signal_rates(rates)
    month_ends = spot.groupby(spot.index.to_period("M")).tail(1).index

    # start at the first month-end with all 10 lagged rates (pre-registered);
    # after that, a currency missing too long just drops out of the ranking
    complete = sig.dropna().index
    if complete.empty:
        raise ValueError("no month has all 10 lagged rates")
    month_ends = month_ends[month_ends.to_period("M").to_timestamp() >= complete[0]]

    rows = [row for d in month_ends if (row := book_row(sig, d)) is not None]
    if not rows:
        raise ValueError("no month-end has a full set of lagged rates")
    return pd.DataFrame(rows)


def simulate(spot: pd.DataFrame, rates: pd.DataFrame) -> pd.DataFrame:
    return pnl_from_book(spot, build_portfolio(spot, rates))


def pnl_from_book(spot: pd.DataFrame, book: pd.DataFrame) -> pd.DataFrame:
    """Daily PnL for a sequence of month-end books (indexed by the date each
    was set). Each day earns on the latest book dated strictly before it,
    so a book dated on a weekend or holiday works too (fx_paper_trade.py
    records books by calendar month-end).
    """
    days = spot.loc[book.index[0]:].index
    # position of the book in force on each day: latest book date < day
    slot = book.index.searchsorted(days, side="left") - 1
    held = book.iloc[np.clip(slot, 0, None)].set_axis(days)
    held[slot < 0] = np.nan

    spot_ret = spot[FOREIGN].reindex(days).pct_change()
    dt = days.to_series().diff().dt.days.to_numpy() / DAY_COUNT

    w = held[[f"w_{c}" for c in FOREIGN]].to_numpy()
    diff = held[[f"rate_{c}" for c in FOREIGN]].to_numpy() - held[["rate_USD"]].to_numpy()

    spot_pnl = np.nansum(w * spot_ret.to_numpy(), axis=1)
    carry_pnl = np.nansum(w * diff / 100, axis=1) * np.nan_to_num(dt)

    # costs land on the first day the new book is held
    weights = book[[f"w_{c}" for c in CURRENCIES]]
    turnover = weights.diff().abs().sum(axis=1)
    turnover.iloc[0] = weights.iloc[0].abs().sum()
    gross = weights[[f"w_{c}" for c in FOREIGN]].abs().sum(axis=1)
    cost_at_rebalance = (TRADE_COST_BPS * turnover + ROLL_COST_BPS_PER_MONTH * gross) / 10_000
    first_day = days.searchsorted(book.index, side="right")
    cost = np.zeros(len(days))
    keep = first_day < len(days)
    np.add.at(cost, first_day[keep], cost_at_rebalance.to_numpy()[keep])

    out = pd.DataFrame({"spot": spot_pnl, "carry": carry_pnl, "cost": -cost}, index=days)
    out.iloc[0] = 0.0
    out["net"] = out.sum(axis=1)
    return out.join(held)


def summarize(daily: pd.DataFrame) -> dict:
    net = daily["net"]
    cum = net.cumsum()
    monthly = net.groupby(net.index.to_period("M")).sum()
    return {
        "ann_return": net.mean() * TRADING_DAYS_PER_YEAR,
        "ann_vol": net.std() * np.sqrt(TRADING_DAYS_PER_YEAR),
        "sharpe": net.mean() / net.std() * np.sqrt(TRADING_DAYS_PER_YEAR),
        "max_drawdown": (cum - cum.cummax()).min(),
        "worst_month": monthly.min(),
        "worst_month_date": str(monthly.idxmin()),
        "monthly_skew": monthly.skew(),
        "spot_total": daily["spot"].sum(),
        "carry_total": daily["carry"].sum(),
        "cost_total": daily["cost"].sum(),
    }


def print_summary(label: str, s: dict) -> None:
    print(f"[{label}] ann. return {s['ann_return']:.2%}, vol {s['ann_vol']:.2%}, Sharpe {s['sharpe']:.2f}, "
          f"max DD {s['max_drawdown']:.2%}, worst month {s['worst_month']:.2%} ({s['worst_month_date']}), "
          f"monthly skew {s['monthly_skew']:.2f} | spot {s['spot_total']:+.2%}, carry {s['carry_total']:+.2%}, "
          f"costs {s['cost_total']:+.2%}")


def plot_fx_carry(daily: pd.DataFrame, outputs_dir: str = OUTPUT_DIR) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from src.factor_trades import SERIES_COLORS

    os.makedirs(outputs_dir, exist_ok=True)
    fig, ax = plt.subplots(figsize=(10, 4.5))
    ax.plot(daily.index, daily["net"].cumsum() * 100, color="#222222", linewidth=1.6, label="net")
    ax.plot(daily.index, daily["carry"].cumsum() * 100, color=SERIES_COLORS[0], label="carry (rate differential)")
    ax.plot(daily.index, daily["spot"].cumsum() * 100, color=SERIES_COLORS[1], label="spot")
    if daily.index[-1] > pd.Timestamp(IN_SAMPLE_END):
        ax.axvspan(pd.Timestamp(IN_SAMPLE_END), daily.index[-1], color="gray", alpha=0.15, label="holdout (verdict)")
    ax.axhline(0, color="#888888", linewidth=0.8)
    ax.set_ylabel("Cumulative return (% of \\$1 long / \\$1 short)")
    ax.set_title("G10 FX carry: long 3 / short 3 by lagged 3M rate")
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", color="#e5e5e5", linewidth=0.6)
    ax.legend(frameon=False, loc="upper left")
    fig.tight_layout()
    fig.savefig(os.path.join(outputs_dir, "fx_carry.png"), dpi=150)
    plt.close(fig)


def run_fx_carry(final: bool = False) -> dict:
    """In-sample only by default. final=True unlocks the holdout: the one
    pre-registered run that decides the verdict.
    """
    spot, rates = load_fx_data(include_holdout=final)
    daily = simulate(spot, rates)
    periods = {"in-sample": daily.loc[:IN_SAMPLE_END]}
    if final:
        periods["holdout"] = daily.loc[pd.Timestamp(IN_SAMPLE_END) + pd.Timedelta(days=1):]
    stats = {}
    for label, d in periods.items():
        stats[label] = summarize(d)
        print_summary(label, stats[label])
    if final:
        h = stats["holdout"]
        passed = h["ann_return"] > 0 and h["sharpe"] > 0
        print(f"pre-registered verdict: {'PASS' if passed else 'FAIL'}")
        stats["pass"] = passed
        daily.to_csv(os.path.join(FX_DIR, "fx_carry_daily.csv"))
        plot_fx_carry(daily)
    return stats


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--final", action="store_true", help="unlock the holdout (the one pre-registered run)")
    run_fx_carry(final=parser.parse_args().final)
