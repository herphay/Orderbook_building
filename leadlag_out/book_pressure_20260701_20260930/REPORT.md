# Standalone book-pressure analysis

Sessions: 64; training: 43; held-out: 21.

Pressure = (best bid displayed size − best ask displayed size) / (sum of sizes). Positive means more displayed buying depth.

Primary display horizon: 1000 ms. Fixed directional rule: buy for pressure ≥ 0.5, sell for pressure ≤ −0.5.

The frozen pressure-only model improves held-out squared-error prediction versus the training-mean baseline for TSLA, TSLL, TSLR, TSLG. The average strong-pressure move exceeds spreads and configured fees for none of the instruments at this horizon. Forecast improvement and economic usefulness are separate questions; the cost comparison is indicative and is not a fill simulation.

| Instrument | Held-out slope (bps / +1 pressure) | OOS R² (%) | Directional move (bps) | Spread hurdle (bps) | After spreads/fees (bps) | 95% CI for net |
|---|---:|---:|---:|---:|---:|---|
| TSLA | 0.0485 | 0.0279 | 0.0371 | 2.0273 | -1.9902 | [-2.1900, -1.7623] |
| TSLL | 2.1867 | 6.3536 | 1.7111 | 10.6064 | -8.8953 | [-9.1963, -8.5654] |
| TSLR | 1.3176 | 5.3574 | 0.9004 | 6.0879 | -5.1875 | [-5.3987, -4.9941] |
| TSLG | 2.7287 | 6.1208 | 2.5344 | 21.2245 | -18.6901 | [-19.5310, -17.7542] |

## How to interpret

1. Compare 01_pressure_comparison: magnitude and frequency of pressure across instruments; positive average pressure alone is not evidence of predictability.
2. In 02_pressure_vs_future_return, an increasing curve indicates positive pressure/return association. Flat or irregular curves indicate weak or unstable association.
3. In 03_predictive_power, a positive held-out slope is association; positive OOS R² means the frozen linear model improves squared-error prediction against the training-mean baseline. These are distinct tests.
4. In 04_move_vs_spread, the directional move must exceed both entry and exit half-spreads plus configured fees. A positive midprice forecast can still be uneconomic.

Conditional charts and slopes use equal-week averages of daily statistics; model fitting and OOS R² pool observations. They have different weighting intentionally and are labeled accordingly.

## Scope and limitations

- Nasdaq displayed L1 only, not NBBO, full-depth pressure or order-flow imbalance.
- Pressure is measured strictly before each grid time; targets use the last state strictly before t+h.
- An unchanged standing quote remains valid by default; quote age is time since last BBO state change, not a feed heartbeat.
- Overlapping targets are dependent; inference resamples weeks, not ticks. Few held-out weeks limit precision.
- Chronological holdout within already studied historical data is not an untouched prospective test.
- Association is not causal or incremental to TSLA returns, momentum or other predictors.
- Spread-adjusted markouts are a diagnostic, not a strategy backtest: no fill guarantee, latency, depth impact, queue, inventory or borrow constraints.
- Threshold and horizons are fixed in advance of this run; alternate specifications remain exploratory.

## Files

- daily_*.csv: session-level diagnostics, moments and conditional means.
- pressure_comparison.csv: equal-day descriptive comparison.
- pressure_bins.csv / predictive_summary.csv: week-based estimates and intervals, train/test separated.
- out_of_sample_models.csv: training coefficients and held-out errors.
- summary.json: exact configuration, dates and methodology.
- 01–04 PNG/SVG files: slide-ready charts.
- sessions/: resumable per-session sufficient statistics; use --resume to reuse matching inputs/settings.
