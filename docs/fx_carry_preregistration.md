# G10 FX carry: pre-registration

Written 2026-10-09, **before any FX price or interest-rate data was
loaded**. Only series metadata (start/end dates) had been checked. Nothing
below may be changed after seeing results; a change is a new, separately
labeled test.

## Why this test exists
The Treasury curve work ended with no demonstrable edge, and its 2023-26
holdout was spent. A new asset class gives a genuinely untouched holdout.
FX carry was chosen on outside evidence (Lustig & Verdelhan 2007;
Burnside, Eichenbaum, Kleshchelski & Rebelo 2011; Koijen, Moskowitz,
Pedersen & Vrugt 2018), not on anything seen in this project's data. The
one link to earlier findings is thematic: the curvature trade failed by
paying carry; this trade collects it, with known crash risk.

## Hypothesis
**A dollar-neutral G10 carry portfolio earns a positive net return.**

- **Universe (10):** USD, EUR, JPY, GBP, CHF, CAD, AUD, NZD, NOK, SEK.
- **Signal:** each currency's OECD 3-month interbank rate
  (`IR3TIB01{country}M156N`; euro area for EUR, US for USD).
- **Portfolio:** at each month-end, rank the currencies by rate; long the
  top 3, short the bottom 3, equal-weighted (+1/3 each long, -1/3 each
  short, so $1 long and $1 short in total). This is the standard
  "long 3 / short 3 of G10" construction (e.g. the Deutsche Bank G10
  Currency Harvest index). When USD ranks in the top or bottom 3, that
  slot is held in USD, i.e. as cash with no FX exposure.
- **Return:** for each foreign position, the daily spot return versus USD
  plus the rate differential (foreign minus US rate) accrued over calendar
  days / 360, i.e. covered interest parity used to approximate the forward
  points. Spot data: FRED H.10 noon rates (`DEXUSEU`, `DEXJPUS`, `DEXUSUK`,
  `DEXSZUS`, `DEXCAUS`, `DEXUSAL`, `DEXUSNZ`, `DEXNOUS`, `DEXSDUS`),
  converted to USD per unit of foreign currency.
- **No lookahead:** the OECD rate for calendar month M is a monthly
  average published after M ends, so the ranking at the end of month M
  uses month M-1's rate. The same lagged rate drives the carry accrual.
  A rate may be carried forward at most 3 months; a currency with no rate
  for longer is left out of the ranking (still long 3 / short 3 of the rest).
- **Timing:** ranks are set at the last trading day's close of each month,
  and positions earn from the next day.
- **Costs:** 2bp of notional traded at each rebalance (G10 spot bid/ask),
  plus 1bp per month on gross notional held, for rolling the forwards.
- **No overlays:** no stop-loss, volatility targeting or crash hedge in
  the primary test.

## Sample and evaluation
- **Start:** first month-end at which all 10 rates have a lagged value
  (the yen series starts 2002-04, so about mid-2002).
- **In-sample: through 2020-12-31.** Used only to check the code works (for
  example, the 2008 carry crash should appear). No parameter above may be
  changed based on it.
- **Holdout: 2021-01-01 through the end of the data.** The loader refuses
  to return holdout dates unless explicitly unlocked, and it is unlocked
  once, for the final run.
- **Pass:** holdout annualized net return > 0 **and** holdout net Sharpe
  > 0. Reported alongside: annualized vol, max drawdown, worst month,
  monthly skewness, and the share of return from spot vs. carry.
- Anything beyond the primary test (risk overlays, different N, other
  signals) is exploratory and labeled as such.

## Known limitations, stated in advance
- **Low power again:** ~5.75 years of holdout. The standard error on an
  annualized Sharpe is ~0.4, so a pass is weak evidence; a clear failure
  is more informative.
- **Rates are approximate:** interbank 3-month averages lagged a month
  are not the actual forward points traded. LIBOR-based series also
  changed as LIBOR was retired, which mostly affects the holdout.
- **Publication lag:** at the time of writing, the euro and sterling rate
  series end in January 2026, so the 3-month carry-forward rule may drop
  them from the late-holdout rankings. That's the rule working as
  written, not a reason to change it.
- **The idea isn't new to anyone:** carry's long-run premium is
  well documented, so a pass would confirm a known effect on recent data,
  not discover one.

---

## Amendment 1 (2026-10-10): tie-breaking at the cutoffs
*Added after the holdout run; the rules above are unchanged.*

**Gap:** the rules didn't say how to rank currencies with exactly equal
rates. The implementation used an ordinary sort, which breaks ties
arbitrarily, and differently on macOS (where the holdout was run) and on
Linux (GitHub Actions). The paper trader's frozen-rules check caught this
when the two platforms produced different results from identical code and
data. Seven month-ends since 2002 have a tie at a cutoff, one of them in
the holdout (2025-02-28, USD and AUD both 4.33%).

**Rule added:** when currencies tie at the long or short cutoff, that
slot's weight is split equally among them (e.g. USD and AUD at 1/6 each
instead of one at 1/3). This picks no winner and is platform-independent.
If ties ever span both cutoffs, no ranking is formed that month and the
previous book carries over.

**Effect on the verdict:** none. Holdout net return under macOS's tie order
13.02%, Linux's 13.14%, the split rule 13.08% (2.20%/yr, Sharpe 0.42).
Pass under all three.
