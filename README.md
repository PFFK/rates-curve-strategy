# Treasury Curve Relative-Value Strategy

## Overview
A systematic Treasury yield curve relative-value strategy that trades curve
spreads (2s10s, 5s30s) using a mean-reversion signal, constructed to be
DV01-neutral to parallel yield curve shifts. Strategy performance is
backtested and PnL is attributed to curve risk factors (level, slope,
curvature) via PCA decomposition.

## Goals
- Demonstrate systematic signal construction, relative-value trade design,
  and PnL attribution methodology relevant to rates trading.
- Support configurable spread selection (2s10s vs 5s30s) using the same
  pipeline.

## Data
- Source: FRED (Federal Reserve Economic Data)
- Series: 2Y, 5Y, 10Y, 30Y Treasury constant maturity yields
- History: ~10-15 years of daily data

## Methodology

### 1. Factor Decomposition (PCA)
- Run PCA on historical daily changes in the 4 yield curve points
- Extract level, slope, and curvature factors
- Validate against expected variance explained (level ~80-90%, slope
  ~5-10%, curvature ~2-5%)

### 2. Signal Construction (Mean-Reversion)
- Compute the target spread (2s10s or 5s30s) daily
- Compute a rolling z-score of the spread (default window: 252 trading days)
- Entry/exit rules based on z-score thresholds (e.g., enter steepener when
  z < -1.5, enter flattener when z > 1.5, exit when z reverts toward 0)
- Signal logic should be modular so alternative signals (e.g., momentum)
  can be swapped in later

### 3. Position Construction (DV01-Neutral)
- Two-leg position: long duration exposure at one curve point, short
  duration exposure at the other
- Size legs so net DV01 exposure to a parallel shift is ~0
- Approximate DV01 per leg using standard bond duration/DV01 formulas

### 4. Backtest
- Simulate strategy over historical data using the signal + position
  construction above
- Track daily PnL, cumulative PnL, drawdowns
- Report: Sharpe ratio, max drawdown, win rate, cumulative PnL chart

### 5. PnL Attribution
- Decompose realized strategy PnL into contributions from level, slope,
  and curvature factors (using PCA loadings from step 1)
- Visualize as a stacked/breakdown chart over time

## Project Structure (planned)
```
curve-strategy/
├── README.md
├── data/              # raw and processed yield curve data
├── src/
│   ├── data_loader.py     # pulls/cleans FRED data
│   ├── pca_factors.py     # PCA decomposition of curve moves
│   ├── signal.py          # z-score mean-reversion signal (spread-agnostic)
│   ├── positions.py       # DV01-neutral position sizing
│   ├── backtest.py        # backtest engine
│   └── attribution.py     # PnL attribution by factor
├── notebooks/         # exploratory analysis (optional)
├── outputs/           # charts, results
└── requirements.txt
```

## Status
- [ ] Data pipeline (FRED pull + cleaning)
- [ ] PCA factor decomposition
- [ ] Signal construction
- [ ] DV01-neutral position sizing
- [ ] Backtest engine
- [ ] PnL attribution
- [ ] Run for both 2s10s and 5s30s, compare results
- [ ] Charts + writeup
