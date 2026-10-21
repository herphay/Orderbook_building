# Analysis C across the quarter

64 cached sessions, 20260701–20260930, 14 ISO-week clusters. All dates are included in summary.json.

## Presentation table

| ETF | t50 (ms) | 95% CI | t90 (ms) | 95% CI | Response ratio at 1 second |
|---|---:|---|---:|---|---:|
| TSLR | 0.604 | [0.531, 0.741] | 69.472 | [34.398, 128.182] | 0.856 |
| TSLL | 4.072 | [2.689, 5.410] | 400.780 | [335.066, 455.690] | 0.809 |
| TSLG | 14.519 | [11.617, 20.149] | 585.492 | [560.026, 609.562] | 0.687 |

t50 and t90 measure time to 50% and 90% of each ETF's own observed response at one second. They do not measure time to complete price discovery or a guaranteed trading opportunity.

## Method

Original forward Analysis C: sum(sign(TSLA trigger)*ETF log-price response) / sum(2*sign(TSLA trigger)*TSLA log-price response)

Equal-session mean daily beta within ISO week, then equal-week mean. Not an event-pooled ratio.

Paired ISO-week bootstrap, percentile 95% pointwise CIs; recompute normalized curves and t50/t90 for every draw.

Exclude TSLA triggers without a full [-1ms,+1s] window within 09:30-16:00 New York.

The original event-level standard errors are not used. Bootstrap resampling uses the same weeks for all ETFs, preserving their dependence. See pairwise_timing.csv for uncertainty in timing differences; its faster fractions are not p-values.

The daily median timing in timing_summary.csv describes the distribution of session-level crossing times. It is distinct from the first-crossing time of the aggregate curve used in the presentation table.

## Limitations

- Descriptive conditional response, not a causal identification or a latency-to-fill estimate.
- t50/t90 are relative to the observed 1-second response, not 50%/90% of theoretical full adjustment and not median event waiting times.
- Original estimator's denominator evolves with the TSLA price response; the curve can be nonmonotone. Timing uses the first crossing.
- Nasdaq displayed midquotes and exchange event timestamps only; no consolidated NBBO or execution costs.
- Retains the original valid-quote series preprocessing and as-of conventions; simultaneous updates are included at tau=0.
- Week bootstrap handles dependence within weeks, but 14 weeks is a limited sample; confidence bands are pointwise, not simultaneous.
- Excludes opening 1ms and final 1s, so even matching dates can differ slightly from the legacy single-day entry point.

## Reproduce

```bash
python analyze_impulse_batch.py --workers 1 --resume
```

PNG/SVG charts: 01_impulse_response and 02_response_timing. CSVs: daily_curves, daily_timing, aggregate_curves, timing_summary, pairwise_timing. sessions/ contains resumable daily curves.
