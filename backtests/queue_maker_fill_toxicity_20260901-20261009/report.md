# Queue-maker fill toxicity by pre-arrival conditions

Source fills: `backtests/queue_maker_20260901-20261009/trades.csv` (4,657 fills). Markouts: `backtests/queue_maker_markouts_20260901-20261009/markouts_by_trade.csv`.

Conditions use ETF BBO immediately before simulated order arrival, assuming 50 µs signal-to-exchange delay; TSLA pressure and quote state are sampled at signal time. Queue ahead is the replay's logged order-arrival estimate.

## Main sample and interpretation

The join retained 4,657 fills across 28 sessions. This is a filled-order toxicity study: it cannot estimate how a filter changes fill probability because the saved baseline run does not include the corresponding full candidate funnel in this directory. Markouts are model-generated pair mark-to-mid values after fills, not realized live returns.

Spread, touch depth, queue ahead, and quote age are split into within-ETF quartiles so differently priced ETFs and share scales are not directly mixed. Pressure buckets are signed to the ETF position: `opposed`, `weak aligned`, `moderate aligned`, and `strong aligned`. Pooled summaries are shown for exploration; use the per-symbol file to check whether a pooled pattern is driven by one ETF.

## Pooled condition summaries (100 ms and 500 ms)

| feature                   | bucket           | fills | horizon_ms | sessions | mean_markout_dollars | median_markout_dollars | win_rate | mean_net_exit_pnl_dollars |
| ------------------------- | ---------------- | ----- | ---------- | -------- | -------------------- | ---------------------- | -------- | ------------------------- |
| etf_pressure_aligned_bin  | moderate aligned | 106   | 100        | 25       | -0.789               | -0.8                   | 0.151    | -1.931                    |
| etf_pressure_aligned_bin  | opposed          | 4120  | 100        | 28       | -0.765               | -0.7                   | 0.088    | -1.704                    |
| etf_pressure_aligned_bin  | strong aligned   | 31    | 100        | 18       | -1.011               | -1.04                  | 0.161    | -2.508                    |
| etf_pressure_aligned_bin  | weak aligned     | 400   | 100        | 28       | -1.001               | -0.9                   | 0.112    | -2.259                    |
| etf_pressure_aligned_bin  | moderate aligned | 106   | 500        | 25       | -0.614               | -0.587                 | 0.198    | -1.931                    |
| etf_pressure_aligned_bin  | opposed          | 4120  | 500        | 28       | -0.715               | -0.67                  | 0.097    | -1.704                    |
| etf_pressure_aligned_bin  | strong aligned   | 31    | 500        | 18       | -1.065               | -1.04                  | 0.161    | -2.508                    |
| etf_pressure_aligned_bin  | weak aligned     | 400   | 500        | 28       | -0.923               | -0.8                   | 0.12     | -2.259                    |
| etf_quote_age_q           | Q1 low           | 1163  | 100        | 28       | -0.81                | -0.725                 | 0.101    | -1.839                    |
| etf_quote_age_q           | Q2               | 1165  | 100        | 28       | -0.792               | -0.715                 | 0.097    | -1.785                    |
| etf_quote_age_q           | Q3               | 1164  | 100        | 28       | -0.791               | -0.71                  | 0.084    | -1.75                     |
| etf_quote_age_q           | Q4 high          | 1165  | 100        | 28       | -0.756               | -0.7                   | 0.088    | -1.675                    |
| etf_quote_age_q           | Q1 low           | 1163  | 500        | 28       | -0.741               | -0.675                 | 0.105    | -1.839                    |
| etf_quote_age_q           | Q2               | 1165  | 500        | 28       | -0.744               | -0.66                  | 0.101    | -1.785                    |
| etf_quote_age_q           | Q3               | 1164  | 500        | 28       | -0.731               | -0.675                 | 0.107    | -1.75                     |
| etf_quote_age_q           | Q4 high          | 1165  | 500        | 28       | -0.716               | -0.675                 | 0.094    | -1.675                    |
| etf_spread_bps_q          | Q1 low           | 1158  | 100        | 12       | -0.767               | -0.7                   | 0.084    | -1.673                    |
| etf_spread_bps_q          | Q2               | 1167  | 100        | 17       | -0.825               | -0.735                 | 0.08     | -1.803                    |
| etf_spread_bps_q          | Q3               | 1157  | 100        | 20       | -0.742               | -0.725                 | 0.099    | -1.71                     |
| etf_spread_bps_q          | Q4 high          | 1175  | 100        | 28       | -0.814               | -0.7                   | 0.106    | -1.86                     |
| etf_spread_bps_q          | Q1 low           | 1158  | 500        | 12       | -0.697               | -0.65                  | 0.096    | -1.673                    |
| etf_spread_bps_q          | Q2               | 1167  | 500        | 17       | -0.799               | -0.705                 | 0.088    | -1.803                    |
| etf_spread_bps_q          | Q3               | 1157  | 500        | 20       | -0.69                | -0.665                 | 0.115    | -1.71                     |
| etf_spread_bps_q          | Q4 high          | 1175  | 500        | 28       | -0.744               | -0.65                  | 0.107    | -1.86                     |
| etf_touch_depth_q         | Q1 low           | 1433  | 100        | 28       | -0.882               | -0.77                  | 0.068    | -1.919                    |
| etf_touch_depth_q         | Q2               | 875   | 100        | 28       | -0.798               | -0.725                 | 0.07     | -1.777                    |
| etf_touch_depth_q         | Q3               | 1175  | 100        | 28       | -0.845               | -0.725                 | 0.085    | -1.847                    |
| etf_touch_depth_q         | Q4 high          | 1174  | 100        | 28       | -0.606               | -0.625                 | 0.146    | -1.474                    |
| etf_touch_depth_q         | Q1 low           | 1433  | 500        | 28       | -0.825               | -0.725                 | 0.077    | -1.919                    |
| etf_touch_depth_q         | Q2               | 875   | 500        | 28       | -0.732               | -0.675                 | 0.087    | -1.777                    |
| etf_touch_depth_q         | Q3               | 1175  | 500        | 28       | -0.794               | -0.7                   | 0.09     | -1.847                    |
| etf_touch_depth_q         | Q4 high          | 1174  | 500        | 28       | -0.559               | -0.59                  | 0.153    | -1.474                    |
| queue_ahead_q             | Q1 low           | 917   | 100        | 28       | -0.752               | -0.68                  | 0.077    | -1.664                    |
| queue_ahead_q             | Q2               | 1419  | 100        | 28       | -0.917               | -0.8                   | 0.068    | -2.003                    |
| queue_ahead_q             | Q3               | 1155  | 100        | 28       | -0.846               | -0.725                 | 0.077    | -1.847                    |
| queue_ahead_q             | Q4 high          | 1166  | 100        | 28       | -0.598               | -0.625                 | 0.148    | -1.462                    |
| queue_ahead_q             | Q1 low           | 917   | 500        | 28       | -0.693               | -0.65                  | 0.08     | -1.664                    |
| queue_ahead_q             | Q2               | 1419  | 500        | 28       | -0.854               | -0.75                  | 0.079    | -2.003                    |
| queue_ahead_q             | Q3               | 1155  | 500        | 28       | -0.795               | -0.7                   | 0.086    | -1.847                    |
| queue_ahead_q             | Q4 high          | 1166  | 500        | 28       | -0.553               | -0.59                  | 0.162    | -1.462                    |
| time_bucket               | 09:30-10:00      | 2593  | 100        | 28       | -0.932               | -0.815                 | 0.08     | -2.075                    |
| time_bucket               | 10:00-12:00      | 1068  | 100        | 28       | -0.634               | -0.68                  | 0.093    | -1.416                    |
| time_bucket               | 12:00-14:00      | 377   | 100        | 28       | -0.569               | -0.635                 | 0.106    | -1.342                    |
| time_bucket               | 14:00-16:00      | 619   | 100        | 28       | -0.578               | -0.62                  | 0.136    | -1.305                    |
| time_bucket               | 09:30-10:00      | 2593  | 500        | 28       | -0.856               | -0.75                  | 0.094    | -2.075                    |
| time_bucket               | 10:00-12:00      | 1068  | 500        | 28       | -0.597               | -0.65                  | 0.106    | -1.416                    |
| time_bucket               | 12:00-14:00      | 377   | 500        | 28       | -0.567               | -0.6                   | 0.09     | -1.342                    |
| time_bucket               | 14:00-16:00      | 619   | 500        | 28       | -0.551               | -0.6                   | 0.132    | -1.305                    |
| tsla_pressure_aligned_bin | moderate aligned | 1685  | 100        | 28       | -0.799               | -0.725                 | 0.088    | -1.767                    |
| tsla_pressure_aligned_bin | strong aligned   | 2972  | 100        | 28       | -0.781               | -0.71                  | 0.095    | -1.759                    |
| tsla_pressure_aligned_bin | moderate aligned | 1685  | 500        | 28       | -0.748               | -0.675                 | 0.092    | -1.767                    |
| tsla_pressure_aligned_bin | strong aligned   | 2972  | 500        | 28       | -0.724               | -0.675                 | 0.107    | -1.759                    |

## High-versus-low bucket contrasts

A positive difference means bucket B had a better mean markout than bucket A. Confidence intervals resample trading days as clusters (not individual fills); these are exploratory intervals across several tested conditions, not multiple-testing-adjusted confirmation. For TSLA pressure, the entry rule already selects aligned pressure, so the contrast is moderate versus strong alignment. For time, it is the opening half-hour versus the final two hours.

| feature                   | bucket_a         | bucket_b       | horizon_ms | bucket_a_fills | bucket_b_fills | bucket_b_minus_a_mean_markout_dollars | day_bootstrap_95ci_low | day_bootstrap_95ci_high |
| ------------------------- | ---------------- | -------------- | ---------- | -------------- | -------------- | ------------------------------------- | ---------------------- | ----------------------- |
| etf_spread_bps_q          | Q1 low           | Q4 high        | 100        | 1158           | 1175           | -0.047                                | -0.171                 | 0.087                   |
| etf_spread_bps_q          | Q1 low           | Q4 high        | 500        | 1158           | 1175           | -0.048                                | -0.142                 | 0.042                   |
| etf_touch_depth_q         | Q1 low           | Q4 high        | 100        | 1433           | 1174           | 0.276                                 | 0.226                  | 0.33                    |
| etf_touch_depth_q         | Q1 low           | Q4 high        | 500        | 1433           | 1174           | 0.266                                 | 0.207                  | 0.325                   |
| queue_ahead_q             | Q1 low           | Q4 high        | 100        | 917            | 1166           | 0.154                                 | 0.097                  | 0.215                   |
| queue_ahead_q             | Q1 low           | Q4 high        | 500        | 917            | 1166           | 0.14                                  | 0.088                  | 0.197                   |
| etf_quote_age_q           | Q1 low           | Q4 high        | 100        | 1163           | 1165           | 0.055                                 | -0.006                 | 0.111                   |
| etf_quote_age_q           | Q1 low           | Q4 high        | 500        | 1163           | 1165           | 0.025                                 | -0.039                 | 0.079                   |
| tsla_pressure_aligned_bin | moderate aligned | strong aligned | 100        | 1685           | 2972           | 0.018                                 | -0.024                 | 0.063                   |
| tsla_pressure_aligned_bin | moderate aligned | strong aligned | 500        | 1685           | 2972           | 0.023                                 | -0.017                 | 0.069                   |
| etf_pressure_aligned_bin  | opposed          | strong aligned | 100        | 4120           | 31             | -0.246                                | -0.572                 | 0.07                    |
| etf_pressure_aligned_bin  | opposed          | strong aligned | 500        | 4120           | 31             | -0.35                                 | -0.64                  | -0.007                  |
| time_bucket               | 14:00-16:00      | 09:30-10:00    | 100        | 619            | 2593           | -0.355                                | -0.466                 | -0.25                   |
| time_bucket               | 14:00-16:00      | 09:30-10:00    | 500        | 619            | 2593           | -0.305                                | -0.406                 | -0.2                    |

## Files

- `fills_with_prearrival_conditions.csv`: per-fill conditions and 100 ms–5 s markouts.
- `condition_markouts_pooled.csv`: pooled bucket summaries.
- `condition_markouts_by_symbol.csv`: ETF-specific bucket summaries.
- `day_cluster_bootstrap_contrasts.csv`: high/low contrasts with day-cluster intervals.
- `within_symbol_quartile_cutpoints.csv`: numeric cutoffs for the within-ETF quartiles.
- `daily_summary.csv`: per-session outcomes.

## Limits

- The sample comes from the older September 1–October 9 queue-maker replay (4,657 fills), not the later stricter run with 23 fills. Treat conditions as hypotheses for a cleaner follow-up.
- The saved trade file does not retain the full pre-arrival order book; spread, top-level depth, quote age, and TSLA pressure are reconstructed from the cached BBO. Queue ahead is the replay's logged estimate.
- The filtered cache omits channel_id and sequence, the fill model omits hidden liquidity and market impact, and no participant identities are available.
- A useful filter must be evaluated on all candidate orders, including unfilled orders, and on dates not used to choose the filter.
