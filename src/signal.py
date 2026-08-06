"""Z-score mean-reversion signal construction for Treasury curve spreads.

Spread-agnostic: works on any two-point curve spread (2s10s, 5s30s, ...).
Position generation is isolated in its own function so an alternative signal
(e.g. momentum) can be swapped in later without touching spread/z-score
computation.
"""

import os

import numpy as np
import pandas as pd

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
YIELDS_PATH = os.path.join(DATA_DIR, "treasury_yields.csv")

# spread = long-maturity yield - short-maturity yield
SPREAD_PAIRS = {
    "2s10s": ("2Y", "10Y"),
    "5s30s": ("5Y", "30Y"),
}

DEFAULT_WINDOW = 252
DEFAULT_ENTRY_Z = 1.5
DEFAULT_EXIT_Z = 0.0


def load_yields(path: str = YIELDS_PATH) -> pd.DataFrame:
    return pd.read_csv(path, index_col="date", parse_dates=True)


def compute_spread(yields_df: pd.DataFrame, pair: str) -> pd.Series:
    """Long-maturity minus short-maturity yield, in bps."""
    short, long_ = SPREAD_PAIRS[pair]
    spread = (yields_df[long_] - yields_df[short]) * 100
    spread.name = "spread_bps"
    return spread


def compute_zscore(spread: pd.Series, window: int = DEFAULT_WINDOW) -> pd.Series:
    """Rolling z-score of the spread; NaN until a full window is available."""
    rolling_mean = spread.rolling(window, min_periods=window).mean()
    rolling_std = spread.rolling(window, min_periods=window).std()
    zscore = (spread - rolling_mean) / rolling_std
    zscore.name = "zscore"
    return zscore


def generate_positions_mean_reversion(
    zscore: pd.Series,
    entry_z: float = DEFAULT_ENTRY_Z,
    exit_z: float = DEFAULT_EXIT_Z,
) -> pd.Series:
    """Stateful entry/exit signal from a z-score series.

    +1 = steepener (spread unusually tight, bet it widens back out)
    -1 = flattener (spread unusually wide, bet it narrows back in)
     0 = flat

    A position, once entered, is held until the z-score reverts back through
    exit_z rather than being re-evaluated against the entry threshold every
    bar -- otherwise the signal would flicker in and out right at the
    threshold instead of riding the reversion.
    """
    positions = np.zeros(len(zscore), dtype=int)
    position = 0
    for i, z in enumerate(zscore.to_numpy()):
        if np.isnan(z):
            position = 0
        elif position == 0:
            if z <= -entry_z:
                position = 1
            elif z >= entry_z:
                position = -1
        elif position == 1 and z >= exit_z:
            position = 0
        elif position == -1 and z <= exit_z:
            position = 0
        positions[i] = position
    return pd.Series(positions, index=zscore.index, name="position")


def build_signal(
    yields_df: pd.DataFrame,
    pair: str = "2s10s",
    window: int = DEFAULT_WINDOW,
    entry_z: float = DEFAULT_ENTRY_Z,
    exit_z: float = DEFAULT_EXIT_Z,
) -> pd.DataFrame:
    spread = compute_spread(yields_df, pair)
    zscore = compute_zscore(spread, window)
    position = generate_positions_mean_reversion(zscore, entry_z, exit_z)
    return pd.concat([spread, zscore, position], axis=1)


def summarize_signal(signal_df: pd.DataFrame, pair: str) -> None:
    position = signal_df["position"]
    prev = position.shift(1).fillna(0)
    n_entries = int(((prev == 0) & (position != 0)).sum())
    pct_in_position = (position != 0).mean()
    pct_steepener = (position == 1).mean()
    pct_flattener = (position == -1).mean()

    print(f"[{pair}] {n_entries} trades, {pct_in_position:.1%} of days in a position "
          f"({pct_steepener:.1%} steepener, {pct_flattener:.1%} flattener)")


def run_signal_pipeline(save: bool = True) -> dict:
    yields_df = load_yields()

    results = {}
    for pair in SPREAD_PAIRS:
        signal_df = build_signal(yields_df, pair=pair)
        summarize_signal(signal_df, pair)
        results[pair] = signal_df

        if save:
            os.makedirs(DATA_DIR, exist_ok=True)
            out_path = os.path.join(DATA_DIR, f"signal_{pair}.csv")
            signal_df.to_csv(out_path)
            print(f"  saved to {out_path}")

    return results


if __name__ == "__main__":
    run_signal_pipeline()
