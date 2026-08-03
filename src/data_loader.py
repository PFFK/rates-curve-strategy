"""Pull, clean, and save daily Treasury constant maturity yields from FRED."""

import os
from datetime import date

import certifi
import pandas as pd
from dotenv import load_dotenv
from fredapi import Fred

# macOS python.org builds don't ship a CA bundle by default, which makes
# urllib (used internally by fredapi) fail SSL verification. Point it at
# certifi's bundle unless the environment already specifies one.
os.environ.setdefault("SSL_CERT_FILE", certifi.where())

# FRED series IDs for daily Treasury constant maturity yields
SERIES = {
    "2Y": "DGS2",
    "5Y": "DGS5",
    "10Y": "DGS10",
    "30Y": "DGS30",
}

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
OUTPUT_PATH = os.path.join(DATA_DIR, "treasury_yields.csv")
YEARS_OF_HISTORY = 25


def get_fred_client() -> Fred:
    load_dotenv()
    api_key = os.environ.get("FRED_API_KEY")
    if not api_key:
        raise RuntimeError(
            "FRED_API_KEY not set. Get a free key at "
            "https://fred.stlouisfed.org/docs/api/api_key.html and set it as an "
            "environment variable or in a .env file (FRED_API_KEY=...)."
        )
    return Fred(api_key=api_key)


def fetch_yields(fred: Fred, start_date: str) -> pd.DataFrame:
    """Fetch each maturity's daily series from FRED and combine into one frame."""
    series_data = {}
    for label, series_id in SERIES.items():
        series_data[label] = fred.get_series(series_id, observation_start=start_date)
    df = pd.DataFrame(series_data)
    df.index.name = "date"
    return df


def clean_yields(df: pd.DataFrame) -> pd.DataFrame:
    """Align dates and handle gaps.

    FRED marks holidays/missing observations as NaN within an otherwise
    business-day index. Forward-fill short gaps (e.g. single-day holidays)
    and then drop any remaining rows where a maturity never has data (e.g.
    leading period before a series starts).
    """
    df = df.sort_index()
    df = df.ffill(limit=5)
    df = df.dropna(how="any")
    return df


def load_treasury_yields(save: bool = True) -> pd.DataFrame:
    fred = get_fred_client()
    start_date = date(date.today().year - YEARS_OF_HISTORY, date.today().month, date.today().day).isoformat()

    raw = fetch_yields(fred, start_date)
    clean = clean_yields(raw)

    if save:
        os.makedirs(DATA_DIR, exist_ok=True)
        clean.to_csv(OUTPUT_PATH)
        print(f"Saved {len(clean)} rows ({clean.index.min().date()} to {clean.index.max().date()}) to {OUTPUT_PATH}")

    return clean


if __name__ == "__main__":
    load_treasury_yields()
