# TSLA and leveraged ETF order book research

This guide describes the current research pipeline: obtaining Databento MBO data, building quote caches, running the lead/lag analysis and strategy backtests, and reading their outputs. The code is exploratory research, not a production execution simulator.

## Project map

- `download_mbo.py` downloads daily XNAS.ITCH files and creates the filtered `.npz` cache.
- `utils/leadlag.py` is the shared library and lead/lag analysis entry point. It resolves instrument IDs, loads/caches MBO records, rebuilds displayed order books, and runs the A–D analyses.
- `utils/legacy_leadlag.py` contains the earlier sampled-grid lead/lag analyzer. `leadlag.py` and `leadlag2.py` remain root-level compatibility entry points.
- `backtest.py` runs a single-session strategy for one ETF. It supports a trailing TSLA momentum strategy and an ETF-versus-TSLA estimated mispricing strategy.
- `backtest_batch.py` runs the mispricing strategy across cached sessions and all three ETFs. It supports `--long-only`.
- `backtest_signal_batch.py` sweeps long-only TSLA-triggered momentum and reversal strategies across cached sessions.
- `backtest_imbalance_batch.py` sweeps a long-only signal based on the ETF's displayed best bid/ask size imbalance.
- `backtest_queue_maker.py` evaluates queue-aware maker quotes; `analyze_queue_maker_markouts.py` summarizes maker markouts.
- `backtest_stale_quote.py` replays TSLA-triggered IOC orders against displayed ETF MBO quotes, checking that the exact resting quote orders survive until modeled arrival. It defaults to a 50 ms rolling TSLA-return signal; `--signal-mode session-anchor` selects the original cumulative-from-open signal. Signal lookback, ETF quote-age limits, maximum trade-signed top-five book pressure, maximum ETF spread, signal time window, and maximum arrival-size filters are configurable; the replay also records ETF depth and imbalance over the top 1, 5, and 10 levels. Exit modes include first-profitable-quote, a conditional adverse exit, a tick-based profit target with an adverse midprice stop, and fixed markouts. Non-fixed exits are evaluated per position and can overlap later entries, subject to the configured maximum hold time.
- `run_backtests.py` runs selected baseline backtests over a date range; the runners process independent sessions in configurable child processes.
- `utils/parallel.py` provides the shared process-pool helper used by the date-based runners.
- `strategies.md` summarizes strategy experiments and points to their retained result files.
- `XNAS_MBO/` holds raw Databento files and generated caches. Keep large market data out of source control.
- `backtests/` holds per-trade logs and summaries. Each run should use a distinct output directory so that results are not overwritten.

The downloader and backtest scripts import the same `utils.leadlag` functions, so cache decoding, symbol configuration, and displayed-book reconstruction stay consistent. Use the root-level `leadlag2.py` command for compatibility, or call the library directly:

```sh
python3 -c "from utils import leadlag; leadlag.main(dbn='XNAS_MBO/xnas-itch-20261012.mbo.dbn.zst', out='leadlag_out/20261012')"
```

The current backtest parameters use 2x leverage for TSLL, TSLR, and TSLG (`utils.leadlag.FUNDS`). Check the fund's actual objective and corporate actions before extending the dates or changing this assumption.

## Setup and API key

From the project root, install the pinned packages:

```sh
python3 -m pip install -r requirements.txt
```

Set the Databento key in the shell that will download data. The downloader also reads `DATABENTO_API_KEY` from a local `.env` file, which is ignored by Git. Never put a live key in a script, notebook, command history, or a tracked file:

```sh
export DATABENTO_API_KEY='YOUR_KEY'
```

## Download and cache MBO data

`download_mbo.py` downloads TSLA, TSLL, TSLR, and TSLG from `XNAS.ITCH`, saves one compressed DBN file per day under `XNAS_MBO/`, and builds the filtered NPZ cache used by the backtests. It estimates the request cost before downloading. MBO charges depend on the account's plan and the returned data volume, so review the printed estimate before a large request.

The date window is half-open: `--start-day` is inclusive and `--end-day` is exclusive. The defaults cover August 2026. For example:

```sh
python3 download_mbo.py \
  --start-day 2026-08-01 --end-day 2026-09-01 \
  --workers 2
```

Preview the estimate without requesting data:

```sh
python3 download_mbo.py \
  --start-day 2026-08-01 --end-day 2026-09-01 \
  --estimate-only
```

The script uses a `ThreadPoolExecutor` to download independent daily files concurrently. It defaults to two workers, matching the conservative parallel pattern used in the original example. `--workers` accepts 1–8; start at 2 and increase only if Databento account/API limits and local network performance permit. Each worker uses a separate Databento client, retries transient failures, and writes a temporary file before publishing the completed daily file. Completed raw files and caches are skipped on reruns, so a failed range can be resumed by running the same command again.

The downloader skips weekends, but it does not embed an exchange-holiday calendar. Pass each holiday explicitly with repeatable `--skip-date` arguments, for example:

```sh
python3 download_mbo.py \
  --start-day 2026-09-01 --end-day 2026-10-01 \
  --workers 2 --skip-date 2026-09-07
```

Other arguments are `--data-dir` to change the output folder and `--estimate-only` to run only the cost estimate. If a day fails, the program reports it after other daily jobs finish; rerun the same command to resume. Avoid starting a second downloader for the same dates while one is running.

### Build the per-day cache and lead/lag outputs

Run `utils.leadlag.main` once per downloaded day. It creates the filtered-record NPZ beside the raw file by default; that filename is the one the backtest runners expect.

```sh
python3 -c "from utils import leadlag; leadlag.main(dbn='XNAS_MBO/xnas-itch-20261012.mbo.dbn.zst', out='leadlag_out/20261012')"
```

Repeat for each downloaded day. The cache avoids decoding the entire compressed MBO file again. To rebuild it from raw data, pass `no_cache=True`. If instrument-ID mapping fails, make sure the key is set so the script can query Databento symbology, or provide a valid `symbology.json` / `ids` override as described by the error message.

`utils/leadlag.py` tests several descriptive relationships (A–D): signed move co-occurrence by lag, ETF reaction timing compared with a shifted-time null, impulse response, and quote movement after an estimated fair-value breach. A single day's summary is descriptive and is not by itself proof of causality or statistical significance. To test whether same-direction ETF midquote changes tend to follow TSLA over the cached quarter, run `python3 -m utils.leadlag_significance`; it reports week-blocked inference against an intraday shifted-time placebo and writes the session detail and summary under `leadlag_out/leadlag_significance_20260701_20260930/` by default. This remains a timing test, not proof of a tradable edge.

## Run the backtests

### Single day

The momentum strategy's signal is a TSLA mid-price return over a trailing lookback; it buys the ETF after an upward threshold crossing and shorts it after a downward crossing. Example:

```sh
python3 backtest.py --day 20261009 --symbol TSLL \
  --strategy momentum --lookback-ms 100 --threshold-bps 5 \
  --hold-ms 1000 --latency-ms 1 --quantity 100
```

The mispricing strategy compares the ETF mid with an opening-anchored estimate based on TSLA's return multiplied by the ETF's leverage target. It fades a sufficiently large estimated discount by buying, or premium by shorting. It exits when the gap reaches the configured exit condition, when the maximum hold expires, or at the session end.

```sh
python3 backtest.py --day 20261009 --symbol TSLL \
  --strategy mispricing --mispricing-entry-bps 30 \
  --mispricing-exit-bps 10 --max-hold-s 10 \
  --latency-ms 1 --quantity 100
```

### Multiple sessions and ETFs

`backtest_batch.py` expects one NPZ cache per session in `XNAS_MBO/`. The standard cache name is `xnas-itch-YYYYMMDD.mbo.dbn.zst.TSLA-TSLL-TSLR-TSLG.npz`. Example, matching the recent 28-session experiments:

```sh
python3 backtest_batch.py \
  --start-day 20260901 --end-day 20261009 \
  --entry-bps 30 --exit-bps 10 --max-hold-s 10 \
  --latency-ms 1 --quantity 100 \
  --out backtests/20260901-20261009/gap30_exit10_hold10s
```

To take discount signals only and never open ETF shorts, add `--long-only`:

```sh
python3 backtest_batch.py \
  --start-day 20260901 --end-day 20261009 \
  --entry-bps 30 --exit-bps 10 --max-hold-s 10 \
  --latency-ms 1 --quantity 100 --long-only \
  --out backtests/20260901-20261009/gap30_exit10_hold10s_longonly
```

The runner skips weekends and, by default, September 7, 2026 (Labor Day). For other holidays pass `--skip-date YYYYMMDD`; this option can be repeated. Missing daily cache files stop the run with the expected path in the error message. Use a new `--out` directory when changing any parameter set.

### Run a shared date range with parallel session workers

Use the wrapper to run the baseline suite over an inclusive date range. It
invokes each selected runner in turn; each runner splits independent sessions
across `--workers` child processes.

```sh
python3 run_backtests.py \
  --start-day 20260701 --end-day 20260930 --workers 2 \
  --strategies residual signals imbalance queue-maker execution-sensitivity \
  --out-dir backtests
```

July 3 and September 7 are excluded by default. Add `--skip-date YYYYMMDD` for
additional non-sessions. Output is organized under
`backtests/20260701-20260930/<strategy>/`. The wrapper runs strategies
sequentially and limits `--workers` to each strategy's daily jobs; this avoids
multiplying the memory use by running all strategies at once. Each process loads
the day's MBO cache, so begin with two workers and increase only when memory
allows. Set `--workers 1` to run inline while debugging. After the selected
runners finish, the wrapper writes `period_summary.csv` at the date-range root,
with monthly and full-sample counts, win rates, net P&L, mid P&L, spread cost,
and fees for each configuration and symbol.

For a first-pass pilot, limit a selected strategy to the first ten cached
sessions in a broader date range. This selects sessions chronologically and
stores output under the actual pilot range:

```sh
python3 run_backtests.py \
  --start-day 20260701 --end-day 20260930 --max-sessions 10 \
  --strategies queue-maker --workers 4 --out-dir backtests/pilots
```

Use the pilot to catch implementation issues and screen for a promising
mechanism. Freeze the strategy settings before expanding to the full sample;
ten sessions are not enough to establish durable performance.

### Evaluate queue-maker execution

The queue-maker runner records the order funnel as well as completed trades.
It allows one live order at a time per ETF pair and blocks overlapping quotes
until cancel/timeout. Queue position is replayed from order-level MBO add,
modify, fill, and cancel events: orders resting at the limit price when our
order arrives are ahead, while later orders are behind. The default exchange
arrival delay is 50 microseconds; hedge and exit delays remain separately
configurable in milliseconds:

```sh
python3 backtest_queue_maker.py \
  --start-day 20260701 --end-day 20260930 --workers 4 \
  --exchange-entry-delay-us 50 --cancel-latency-ms 0 \
  --hedge-latency-ms 1 --exit-latency-ms 1 \
  --out backtests/queue_maker_execution_eval
```

`candidates.csv.gz` contains compressed submitted order attempts and their outcomes;
`signal_screening_by_day.csv` counts signals that passed direction/residual
checks and how many were filtered by the expected-cost gate. `candidate_rates.csv`
reports complete fills and partially filled orders per submitted order and
per eligible signal. Partially filled shares are paired at the last partial
fill time for this exploratory model. These denominators matter because fills
alone hide the number of missed or filtered opportunities.
`cancel_diagnostics.csv` groups cancellations by ETF quote change, opposite
touch crossing, TSLA signal fade, or quote timeout, with order lifetime,
MBO executions, and queue progress. `pre_arrival_diagnostics.csv` separately
counts quotes that became stale or signals that faded before the order arrival
latency elapsed; those are not orders that reached the simulated book.
`trades.csv` records complete and partial pair fills. Then run the markout/TCA analysis:

To evaluate the TSLA price lead/lag trigger with the same order-level queue
replay, select `--signal-mode leadlag`. The default trigger uses a 100 ms TSLA
mid-return threshold of 5 bp. It only posts when the ETF residual indicates
that the ETF is lagging TSLA in the same direction:

```sh
python3 backtest_queue_maker.py \
  --start-day 20260701 --end-day 20260715 --skip-date 20260703 \
  --signal-mode leadlag --signal-lookback-ms 100 --signal-threshold-bps 5 \
  --exchange-entry-delay-us 0 --hedge-latency-ms 0 --exit-latency-ms 0 \
  --workers 4 --out backtests/leadlag_queue_replay
```

This is a thresholded TSLA return trigger combined with the residual cost gate;
it is distinct from the descriptive response-time analysis in `utils/leadlag.py`
and from the default queue-maker imbalance trigger. To use the TSLA return only
to choose ETF direction, without a residual gate or TSLA hedge, add
`--trade-mode etf-only`:

```sh
python3 backtest_queue_maker.py \
  --start-day 20260701 --end-day 20260715 --skip-date 20260703 \
  --signal-mode leadlag --signal-lookback-ms 100 --signal-threshold-bps 5 \
  --trade-mode etf-only --exchange-entry-delay-us 0 \
  --hedge-latency-ms 0 --exit-latency-ms 0 --workers 4 \
  --out backtests/leadlag_etf_only_queue_replay
```

By default, the queue-maker cancels if the rolling TSLA signal falls below its
threshold. For a one-shot threshold-crossing strategy, add
`--keep-order-on-signal-fade`; the ETF order then remains live until an ETF
quote invalidates it or the quote-wait timeout expires. A return dropping below
the threshold can happen because the 100 ms lookback window rolls forward,
without TSLA reversing.

```sh
python3 analyze_queue_maker_markouts.py \
  --trades backtests/queue_maker_execution_eval/trades.csv \
  --out backtests/queue_maker_execution_eval/markouts
```

The markout output separates ETF and TSLA hedge markouts at 100 ms, 500 ms,
1 s, and 5 s, and summarizes entry and exit implementation shortfall versus
midquotes. The queue fraction is a sensitivity assumption on displayed size,
not exact FIFO priority. Unfilled-order counterfactual markouts assume a fill
at the limit and are not realized P&L. Costs such as borrow, market impact,
and exchange fees remain dependent on the supplied assumptions.

### Compare TSLA momentum and reversal signals

The signal sweep compares buying the ETF in the direction of a trailing TSLA return (`momentum`) with buying against it (`reversal`). By default both are long-only; `--allow-shorts` enables short trades. The following runs a small grid over all available sessions:

```sh
python3 backtest_signal_batch.py \
  --start-day 20260901 --end-day 20261009 \
  --lookback-ms 100 1000 --threshold-bps 5 15 \
  --hold-ms 1000 10000 --mode momentum reversal \
  --out backtests/20260901-20261009/signal_sweep
```

It writes a `config_summary.csv` ranking each tested setting, with a detailed directory for each configuration. This ranking is an in-sample exploration: do not treat the best row as a validated strategy. Select parameters on earlier dates, then evaluate them once on later untouched sessions; after inspecting the holdout, it is no longer untouched.

### Sweep top-of-book imbalance

This separate strategy buys when `(best bid size - best ask size) / (best bid size + best ask size)` crosses above a threshold. It exits after a fixed holding time. Example:

```sh
python3 backtest_imbalance_batch.py \
  --start-day 20260901 --end-day 20261009 \
  --imbalance 0.5 0.8 --hold-ms 1000 10000 \
  --latency-ms 1 --quantity 100 \
  --out backtests/20260901-20261009/imbalance_sweep
```

The signal uses only the ETF's current displayed best-level sizes. Those sizes can change or cancel before an order arrives; the test does not model queue position, adverse selection, or impact.

## Parameters to explore

- `--entry-bps`: minimum absolute ETF/fair-value gap that opens a trade. A higher threshold usually means fewer signals.
- `--exit-bps`: convergence condition that exits a trade. It must be below the entry threshold. The long and short conditions are direction-aware in the strategy implementation.
- `--max-hold-s`: time limit after entry if the exit condition has not triggered.
- `--long-only`: permit only long ETF entries when the ETF is estimated to be at a discount.
- `--latency-ms`: delay between signal/exit condition and the first eligible ETF quote.
- `--quantity`: fixed shares. Entry and exit are accepted only when the displayed best-level size covers the full quantity; otherwise the trade is skipped.
- `--fee-per-share`: commission assumption applied on both entry and exit.
- Momentum-only settings: `--lookback-ms`, `--threshold-bps`, and `--hold-ms`.

Change one parameter at a time when comparing runs. Keep a record of each parameter set and data range. Parameter sweeps on the same dates can overfit; reserve later sessions as an out-of-sample evaluation period.

## Read the outputs and cost calculation

Each batch threshold directory contains:

- `all_trades.csv`: one row per completed simulated trade, including direction, signal gap, entry/exit timestamps and prices, displayed sizes, exit reason, and P&L components.
- `trades/YYYYMMDD_SYMBOL.csv`: the same trade rows split by date and ETF.
- `daily_summary.csv`: per-day, per-ETF metrics.
- `symbol_summary.csv`: aggregate metrics by ETF.
- `summary.json`: parameters, aggregate results, daily results, and stated limitations.

For a long, simulated entry crosses the ask and exit crosses the bid. For a short, entry crosses the bid and exit crosses the ask. The code also computes mid-price P&L and the cost of crossing the spread:

```text
mid P&L = direction × (exit mid − entry mid) × shares
spread cost = entry spread cost + exit spread cost
P&L after spread = mid P&L − spread cost
net P&L = P&L after spread − commissions
```

The current estimates are based on one venue's displayed best bid/ask. They assume a full fill at that price when displayed size covers the order, after the configured latency. They are not actual historical fills. Market impact beyond top-of-book, queue position, hidden liquidity, partial fills, consolidated NBBO, short borrow, fund fees, and financing are not modeled. `net_return_bps_on_entry_notional` divides aggregate net P&L by total entry notional; it is not an annualized or capital-weighted portfolio return.

For example, the recent 30 bps entry / 10 bps exit / 10-second long-only run produced 2,104 trades and estimated aggregate P&L of −$1,544 after spread and before any commissions. That is a result under these assumptions and this date sample; it does not establish that the strategy will continue to lose or make money in other periods.

## Research cautions

- The opening-anchored leverage estimate is a tracking proxy, not the ETF's official intraday NAV or a directly observed premium/discount.
- Leveraged ETF daily targets, splits, distributions, fees, and tracking behavior can change. Validate fund details and historical adjustments before using more dates.
- XNAS.ITCH is a venue-specific order book, not all US trading venues.
- One-second and millisecond strategies are sensitive to timestamp alignment, quote staleness, feed latency, and execution assumptions.
- Report sample size, dates, skipped sessions, parameters, gross/mid P&L, spread costs, commissions, and net P&L together. Aggregate results can hide session-level concentration.
- Treat repeated parameter selection on one historical sample as exploratory. Use holdout sessions and uncertainty estimates before drawing conclusions.

## Current exploratory strategy comparison

On the cached September 1–October 9, 2026 sample (28 sessions), the tested long-only TSLA momentum and reversal configurations did not show a reliable positive result after estimated spread. A high-threshold momentum configuration (100 ms lookback, 30 bps signal, 30-second hold) showed +$57 across 12 trades, but $56 came from September 9 alone. On the later September 21–October 9 segment it took only two trades and lost $6. Because the configuration was chosen after reviewing the full sample, that later segment is not a clean independent test. Treat the apparent in-sample gain as an example of why sparse results and parameter selection need strict holdouts, not as a demonstrated edge.

The initial ETF-size-imbalance sweep also lost after estimated spread for all four tested combinations (0.5/0.8 imbalance thresholds and 1/10-second holds). It produced roughly 70,000–246,000 trades per setting, illustrating how turnover can overwhelm small mid-price gains. Dollar totals reflect repeated 100-share simulated tickets and zero commission; they are not portfolio returns.

