"""PCA decomposition of Treasury curve moves into level, slope, and curvature factors."""

import os

import matplotlib
import numpy as np
import pandas as pd
from sklearn.decomposition import PCA

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")
YIELDS_PATH = os.path.join(DATA_DIR, "treasury_yields.csv")

MATURITIES = ["2Y", "5Y", "10Y", "30Y"]
FACTOR_NAMES = ["level", "slope", "curvature", "residual"]

# Expected variance-explained ranges from the strategy spec, used as a sanity
# check that the PCA output looks like a normal Treasury curve decomposition.
EXPECTED_VARIANCE_RANGES = {
    "level": (0.80, 0.90),
    "slope": (0.05, 0.10),
    "curvature": (0.02, 0.05),
}


def load_yields(path: str = YIELDS_PATH) -> pd.DataFrame:
    return pd.read_csv(path, index_col="date", parse_dates=True)


def compute_daily_changes(yields_df: pd.DataFrame) -> pd.DataFrame:
    """Daily changes in bps, the standard input scale for curve PCA."""
    changes = yields_df[MATURITIES].diff().dropna() * 100
    return changes


def _orient_signs(loadings: pd.DataFrame, scores: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Flip component signs to a consistent, interpretable orientation.

    PCA eigenvectors have arbitrary sign. Fix it so that:
    - level: rising factor score means yields broadly rose (avg loading > 0)
    - slope: rising factor score means the curve steepened (30Y loading > 2Y loading)
    - curvature: rising factor score means belly yields rose vs. the wings (belly cheapened)
      (5Y+10Y loadings > 0 on average)
    """
    loadings = loadings.copy()
    scores = scores.copy()

    sign_rules = {
        "level": lambda l: np.sign(l.mean()),
        "slope": lambda l: np.sign(l["30Y"] - l["2Y"]),
        "curvature": lambda l: np.sign(l[["5Y", "10Y"]].mean()),
    }

    for factor in loadings.columns:
        rule = sign_rules.get(factor)
        if rule is None:
            continue
        sign = rule(loadings[factor])
        if sign == 0:
            sign = 1
        loadings[factor] *= sign
        scores[factor] *= sign

    return loadings, scores


def _validate_factor_shapes(loadings: pd.DataFrame) -> None:
    """Fail loudly if the PC-rank-based labels don't match their expected shape.

    PCA only guarantees components are ordered by descending variance; it does
    not guarantee PC1 looks like a level shift, PC2 like a slope, etc. That
    correspondence is an empirical property of Treasury curves, not a
    mathematical one, so if it ever breaks (different sample window, added
    maturities) the level/slope/curvature labels would silently point at the
    wrong loadings for every downstream consumer (attribution included).
    """
    level = loadings["level"]
    if not ((level > 0).all() or (level < 0).all()):
        raise ValueError(f"PC1 doesn't look like a level factor (loadings should share one sign): {level.to_dict()}")

    slope = loadings["slope"]
    diffs = slope.diff().dropna()
    if not ((diffs >= 0).all() or (diffs <= 0).all()):
        raise ValueError(f"PC2 doesn't look like a slope factor (loadings should be monotonic across maturities): {slope.to_dict()}")

    curvature = loadings["curvature"]
    wings = np.sign(curvature[["2Y", "30Y"]])
    belly = np.sign(curvature[["5Y", "10Y"]])
    if not ((wings == wings.iloc[0]).all() and (belly == belly.iloc[0]).all() and wings.iloc[0] != belly.iloc[0]):
        raise ValueError(f"PC3 doesn't look like a curvature factor (wings should share one sign, belly the other): {curvature.to_dict()}")


def fit_pca(changes: pd.DataFrame) -> tuple[PCA, pd.DataFrame, pd.DataFrame]:
    """Fit PCA on the covariance of daily yield changes (unstandardized, in bps).

    Returns the fitted PCA object, a (maturity x factor) loadings frame, and a
    (date x factor) factor score frame.
    """
    pca = PCA(n_components=len(MATURITIES))
    scores = pca.fit_transform(changes.values)

    loadings = pd.DataFrame(pca.components_.T, index=MATURITIES, columns=FACTOR_NAMES)
    scores_df = pd.DataFrame(scores, index=changes.index, columns=FACTOR_NAMES)

    loadings, scores_df = _orient_signs(loadings, scores_df)
    _validate_factor_shapes(loadings)

    return pca, loadings, scores_df


def validate_variance(pca: PCA) -> pd.DataFrame:
    ratios = pd.Series(pca.explained_variance_ratio_, index=FACTOR_NAMES)
    rows = []
    for factor, (lo, hi) in EXPECTED_VARIANCE_RANGES.items():
        pct = ratios[factor]
        status = "PASS" if lo <= pct <= hi else "WARN"
        rows.append({"factor": factor, "variance_explained": pct, "expected_range": f"{lo:.0%}-{hi:.0%}", "status": status})
    summary = pd.DataFrame(rows).set_index("factor")

    print("PCA variance explained:")
    for factor in FACTOR_NAMES:
        print(f"  {factor:<10} {ratios[factor]:6.2%}")
    print("\nValidation vs. expected ranges:")
    print(summary.to_string())

    return summary


def plot_results(pca: PCA, loadings: pd.DataFrame, outputs_dir: str = OUTPUT_DIR) -> None:
    os.makedirs(outputs_dir, exist_ok=True)

    fig, axes = plt.subplots(1, 2, figsize=(11, 4.5))

    axes[0].bar(FACTOR_NAMES, pca.explained_variance_ratio_, color="steelblue")
    axes[0].set_ylabel("Variance explained")
    axes[0].set_title("PCA Variance Explained")
    axes[0].yaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{x:.0%}"))

    for factor in ["level", "slope", "curvature"]:
        axes[1].plot(MATURITIES, loadings[factor], marker="o", label=factor)
    axes[1].axhline(0, color="gray", linewidth=0.8)
    axes[1].set_xlabel("Maturity")
    axes[1].set_ylabel("Loading")
    axes[1].set_title("Factor Loadings")
    axes[1].legend()

    fig.tight_layout()
    fig.savefig(os.path.join(outputs_dir, "pca_factors.png"), dpi=150)
    plt.close(fig)


def run_pca_pipeline(save: bool = True) -> dict:
    yields_df = load_yields()
    changes = compute_daily_changes(yields_df)
    pca, loadings, scores = fit_pca(changes)
    variance_summary = validate_variance(pca)

    if save:
        os.makedirs(DATA_DIR, exist_ok=True)
        loadings.to_csv(os.path.join(DATA_DIR, "pca_loadings.csv"))
        scores.to_csv(os.path.join(DATA_DIR, "pca_factor_scores.csv"))
        plot_results(pca, loadings)
        print(f"\nSaved loadings, factor scores, and chart to {DATA_DIR} / {OUTPUT_DIR}")

    return {"pca": pca, "loadings": loadings, "scores": scores, "variance_summary": variance_summary}


if __name__ == "__main__":
    run_pca_pipeline()
