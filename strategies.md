# TSLA and leveraged ETF strategy experiments

This page records the strategies and variants explored in this repository and
the results that are still available in `backtests/` and `leadlag_out/`. P&L
numbers are simulated results under the assumptions listed below. They are not
live results or evidence of a durable trading edge.

## Results at a glance

| Experiment | Sample and implementation | Recorded result | Takeaway |
|---|---|---|---|
| Lead/lag diagnostics | October 9, 2026; TSLA vs. TSLL, TSLR, TSLG; event clock | The analysis found measurable repricing after TSLA moves, but timings varied by ETF and most response is far slower than a few microseconds. See details below. | Useful for characterizing the pair; a descriptive lead is not by itself a tradable signal. |
| TSLA lead/lag trigger with order-level MBO execution | July 1–15, 2026 (10 sessions); 100 ms / 5 bp TSLA return trigger; zero entry, hedge, and exit delay | 1,580 orders reached the simulated book; 1 partial fill (10 shares), no complete fills; **−$0.23** on the partial pair. | Signals were sparse after the residual/cost gate, and the lone fill cannot establish profitability. |
| TSLA lead/lag trigger, ETF-only with order-level MBO execution | Same 10 sessions; one-shot 100 ms / 5 bp TSLA return trigger; no residual gate or TSLA hedge; zero delays | 213 orders reached the book; 12 full and 10 partial fills; 6/22 trades won; **−$2.37** net. | Removing the rolling-signal persistence cancel increased fills, but the pilot remains small and negative. |
| TSLA lead/lag trigger, ETF-only, 50 ms lookback | Same 10 sessions and execution settings; 50 ms / 5 bp trigger | 268 orders reached the book; 14 full and 9 partial fills; 7/23 trades won; **+$4.63** net. | A small positive pilot result is inconclusive and was tested after the 100 ms setting. |
| TSLA lead/lag trigger, ETF-only, 50 ms / 1 bp | Same 10 sessions; 1 bp trigger and 1 bp cost buffer | 200 orders reached the book; 9 full and 10 partial fills; 5/19 trades won; **−$4.09** net. | The lower trigger generated many more candidates, but the cost gate rejected nearly all of them; P&L was negative. |
| TSLA lead/lag trigger, ETF-only, 50 ms / 5 bp / 1 bp buffer | Same 10 sessions and execution settings | 281 orders reached the book; 17 full and 7 partial fills; 8/24 trades won; **−$17.49** net. | Lowering the cost buffer increased orders but worsened P&L in this sample. |
| Same 50 ms / 5 bp / 1 bp ETF-only setup, 1-share size | Same 10 sessions; all other settings unchanged | Same 281 orders and 24 fills; 8/24 trades won; **−$0.06** net across 24 shares. | Smaller size reduced dollar P&L; per-share results remain sample-dependent. |
| Same setup, 1-share size, improve limit by 1 tick | Same 10 sessions; one-cent improvement where the quote remained passive; zero delays | 278 orders reached the book; 18 fills; 3/18 won; **−$0.49** net. | One-tick improvement reduced fills and worsened P&L versus joining the touch (24 fills, 8 wins, −$0.06); this small pilot does not support the added price aggression. |
| Opening-anchored residual fade, 15 bp entry | September 1–October 9, 2026 (28 sessions); cross the ETF spread; 1 ms latency; 100 shares; 5 bp exit; 30 s max hold | 25,963 trades; 6.9% net win rate; **−$18,097** after modeled spread; $27,917 spread cost. | The estimated residual did not overcome execution costs. |
| Opening-anchored residual fade, 30 bp entry | Same 28 sessions and execution assumptions | 2,348 trades; 8.9% net win rate; **−$1,280**; $2,491 spread cost. | Fewer trades reduced losses, but the result remained negative. |
| TSLA momentum / reversal, long only | September 1–October 9, 2026; 100/1,000 ms lookbacks; 5/15/30/50 bp thresholds; 1/10/30 s holds | Most tested configurations lost after spread. Some high-threshold momentum combinations made money in-sample on very few trades; e.g. 100 ms / 30 bp / 30 s: +$57 on 12 trades. On the later Sep 21–Oct 9 holdout, that configuration made **−$6 on 2 trades**. | The apparent best in-sample settings did not persist and have too few trades for confidence. |
| ETF displayed-imbalance signal, long only | September 1–October 9, 2026; thresholds 0.5/0.8; 1/10 s holds | All four settings lost after spread; losses ranged from **−$53,517 to −$230,617** with 70k–246k trades. | Frequent signals paid much more spread than the mid-price movement earned. |
| Queue-aware maker pair | September 1–October 9, 2026; queue estimated from displayed size and subsequent MBO executions; TSLA hedge after ETF fill | 4,657 trades; **0.41% win rate**; **−$8,205.78** simulated fill P&L; −$1,083.13 marked-to-mid at exit. | The simulated passive fills were strongly adversely selected; the TSLA hedge and aggressive exits added cost. |
| Queue-aware maker, join/improve and quote-life sweep | Same 28-session period; TSLL sweep across touch joins / one-tick improvements and 100–1,000 ms quote lives | The recorded cases had 2–10 fills each and **0% win rate**; total P&L ranged from −$2.90 (join, 100 ms) to −$12.95 (join, 1,000 ms). | The small fill counts do not support ranking quote variants; none was profitable in the saved sweep. |
| TSLA-triggered stale-quote IOC | July 1–15, 2026 (10 sessions); 1 share; 1 bp edge buffer; require original MBO quote orders to survive; 50 µs entry/exit delay | 62,422 quote-crossing candidates; 59,442 fills (95.2%); at 1 s, 2,300/59,420 winners (3.9%) and **−$514.86** before fees. Zero-delay sensitivity: 61,458 fills, 4.4% winners, **−$511.26** at 1 s. | The latency signal identified persistent quotes, but this 2x opening-anchor fair-value rule did not predict enough ETF repricing to pay the spread. It is not profitable in this pilot. |
| Fill-model sensitivity | October 5–9, 2026; 30 bp entry / 5 bp exit / 30 s max hold; 1 ms latency; 100–5,000 shares; up to 10 visible levels | Only TSLG generated baseline signals (7 trades). At 100–1,000 shares, all three execution scenarios matched at **−$5 / −$25 / −$50** respectively. At 5,000 shares, touch-only accepted 4 trades and lost **−$100**; the ten-level sweep accepted 7 and lost **−$305.65**, with modeled spread cost rising from $225 to $455.65. | Larger orders can be filled across deeper levels, but walking the book materially worsened cost. No partial entry occurred in this sample; these are frozen-signal diagnostics, not a strategy retest. |

## July–September 2026 expansion (64 sessions)

The full cached sample contains 22 July sessions, 21 August sessions, and 21
September sessions. Every strategy used the previously recorded baseline
settings, 100-share tickets where applicable, 1 ms latency, and zero
commissions. The wrapper ran each date independently with four workers. The
period-level trade summary is in
[`period_summary.csv`](backtests/parallel_workers4/20260701-20260930/period_summary.csv);
the complete run outputs are under
[`backtests/parallel_workers4/20260701-20260930/`](backtests/parallel_workers4/20260701-20260930/).
The suite command was `python3 run_backtests.py --start-day 20260701
--end-day 20260930 --workers 4 --out-dir backtests/parallel_workers4`.

| Strategy | July net P&L | August net P&L | September net P&L | Full-sample result |
|---|---:|---:|---:|---|
| Residual fade, 15 bp entry | −$14,090 | −$12,662 | −$14,802 | **−$41,554** on 62,980 trades; all months and all three ETFs lost. |
| Residual fade, 30 bp entry | −$659 | −$280 | −$1,173 | **−$2,112** on 4,354 trades; all months and all three ETFs lost. |
| Momentum/reversal baseline grid | All 16 settings lost | All 16 settings lost | All 16 settings lost | No setting was positive over the quarter. The least negative was 100 ms momentum / 15 bp / 1 s: −$667 on 808 trades. |
| ETF top-level imbalance | All 4 settings lost | All 4 settings lost | All 4 settings lost | Best tested setting was 0.8 threshold / 10 s hold: **−$138,047** on 175,300 trades. |
| Queue-aware maker, current default filters | −$20.28 on 15 fills | −$4.09 on 3 fills | −$6.32 on 5 fills | **−$30.69**, 0 wins in 23 fills. This is too few fills to assess an edge. |

The wider date range did not change the main conclusions: the tested crossing
signals failed to cover spread costs in every month. Higher thresholds reduced
the residual strategy's turnover and losses, but did not make it profitable.
Momentum/reversal remained negative even at lower turnover. The short-hold
imbalance settings generated especially high turnover and losses. The current
maker implementation uses stricter estimated-cost and confirmation filters
than the earlier saved September–October maker run (4,657 trades); those runs
are not directly comparable. The quarter's 23 fills are a sparse diagnostic,
not a robust estimate of maker performance.

The depth-fill comparison for the 30 bp residual signals also remained
negative. At 5,000 shares, touch-only accepted 3,274 trades across the three
ETFs and lost about $88.2k; the ten-level all-or-complete model accepted 4,366
and lost about $164.0k after walking displayed depth. Its extra completed fills
came with substantially higher spread cost. The partial-entry model accepted
4,394 and lost $173.5k; it allowed 40 entries to fill fewer than the requested
shares and rejected 23 positions that could not be fully closed within ten
levels. Partial-entry and complete-entry results are reported separately
because they accept different orders. At 1,000 shares the touch and complete
depth cases lost $19.1k and $28.2k, respectively.

## What was tried

### 1. Lead/lag and fair-value response study

`utils/leadlag.py` measures signed move co-occurrence, ETF reaction latency,
impulse response, and displayed-quote response after an estimated fair-value
breach. The saved October 9 summary is in `leadlag_out/summary.json`.

- TSLL, TSLR, and TSLG each showed more quote responses after fair-value breach
  triggers than after the shifted-time placebo. The recorded median response
  times were about 26.7 ms, 1.14 ms, and 77.0 ms, respectively.
- The estimated 50% impulse-response times were about 4.0 ms for TSLL, 0.43 ms
  for TSLR, and 16.9 ms for TSLG.
- These are single-session descriptive measurements. They do not establish
  out-of-sample predictability, achievable queue position, or profitability
  after latency and fees.

To test whether the timing pattern persists across sessions, the 64 cached
July–September 2026 sessions were analyzed with
`utils/leadlag_significance.py`. For each ETF midquote move, the test asks
whether the latest same-direction TSLA midquote move occurred strictly earlier
and within the stated horizon. A same-session placebo independently shifts
ETF move times by ±1–5 seconds while preserving move directions. Confidence
intervals and one-sided sign-flip tests use ISO trading weeks as blocks; the
1 ms primary-horizon p-values are Holm-adjusted across the three ETFs.

| Pair | Same-direction ETF moves with a strictly prior TSLA move within 1 ms | Shifted-time placebo | Excess (week-blocked 95% CI) | Holm-adjusted p |
|---|---:|---:|---:|---:|
| TSLL | 46.0% | 0.5% | 45.6 pp (43.4–47.4) | 0.00024 |
| TSLR | 42.4% | 0.5% | 42.2 pp (37.4–46.8) | 0.00024 |
| TSLG | 47.8% | 0.5% | 47.4 pp (46.1–48.9) | 0.00024 |

All 64 sessions had positive effects; the effect also appeared at 100 µs and
10 ms. No same-direction TSLA/ETF quote moves shared an identical timestamp in
this sample. This supports a repeatable short-horizon ordering in the quote
data, but not causal attribution to TSLA alone: common information, venue event
sequencing, and the shifted-time null's limits remain. It also does not show
that the timing is exploitable after queue position, price movement, and costs.
Across the same sample, the mean interval between consecutive valid BBO quote
state changes was 41.5 ms for TSLA, 44.7 ms for TSLL, 57.6 ms for TSLR, and
141.1 ms for TSLG. These are averages across quote events, not medians; bursts
of rapid updates and quieter periods both contribute.
Detailed session estimates and the summary are in
[`leadlag_out/leadlag_significance_20260701_20260930/`](leadlag_out/leadlag_significance_20260701_20260930/).

### 2. Opening-anchored ETF residual fade

`backtest.py` estimates an ETF fair value from the first jointly observable
regular-session TSLA and ETF quotes, then scales the TSLA return by the ETF's
2x leverage target. It buys ETF discounts and shorts ETF premiums, entering
after 1 ms and crossing the displayed spread. Exit is on residual convergence
or a 30-second time limit. This is an ETF directional trade triggered by the
pair residual; it does not hold a simultaneous TSLA hedge.

The 15 bp and 30 bp runs across September 1–October 9 both lost after spread.
The 30 bp threshold lowered turnover and the dollar loss, but did not turn the
result positive. The 15 bp run's 6.9% win rate and $27.9k spread bill illustrate
why a favorable mid-price move alone was insufficient.

### Execution fill sensitivity

`backtest_execution_sensitivity.py` replays the October 5–9 mispricing signals
under three fill rules: touch-only all-or-none, a visible-depth sweep that may
accept a partial entry, and a visible-depth sweep that requires the requested
entry quantity. All scenarios require the resulting position to be fully
exitable within the configured ten price levels. Requested order sizes are
100, 500, 1,000, and 5,000 shares by default. Results and rejected-fill counts
are saved under `backtests/execution_sensitivity_oct05_09/`.

Only TSLG produced signals at the 30 bp threshold on these five sessions: 7
trades from October 7–9. At 100–1,000 shares, the depth sweeps stayed at the
touch and matched the touch-only P&L. At 5,000 shares, touch-only skipped three
opportunities because the top level could not support the full entry/exit,
while ten-level execution filled all 7. The depth sweep's modeled spread cost
was $455.65 versus $225 for the four touch-only fills, and net P&L was
−$305.65 versus −$100. No entry was partial in this sample, so the partial-fill
case was not exercised. `fill_diagnostics.csv` records per-day rejection and
partial-fill counts.

This is a frozen-signal comparison: signals and exit conditions come from a
one-share baseline, so rejected fills do not alter later signal generation or
position timing. The depth replay sweeps visible Nasdaq MBO levels at the
simulated arrival time. It still omits hidden liquidity, impact beyond the
ten-level horizon, and order-book changes caused by our own hypothetical
orders. Five sessions and seven signals are too few to draw a profitability
conclusion.

The repository contains no separate saved result table for an estimated-beta
residual model. The beta-hedged residual discussed in the research notes is a
model idea (ETF return minus a rolling beta times TSLA return), not a strategy
with a result that can be claimed here. The tested residual used the fund's
stated leverage target instead of estimating a rolling beta.

### 3. Momentum, reversal, and displayed imbalance

The long-only sweeps are in:

- `backtests/20260901-20261009/signal_sweep_longonly/config_summary.csv`
- `backtests/20260901-20261009/signal_sweep_high_thresholds_longonly/config_summary.csv`
- `backtests/20260901-20261009/imbalance_sweep_longonly/config_summary.csv`
- `backtests/20260921-20261009/selected_momentum_holdout/config_summary.csv`

The less selective signals traded often and lost heavily to spread. A few
high-threshold momentum combinations were positive in the full sample, but
their subsequent holdout runs were negative or had too few trades to judge.
This is a clear case where choosing the best in-sample row would overstate the
evidence.

### 4. Queue-aware maker pair and refinements

`backtest_queue_maker.py` uses an opening-anchored residual to choose long ETF / short TSLA or short ETF / long TSLA, with TSLA displayed imbalance as a timing filter. It now replays ETF MBO events by order ID, inserts a shadow order at its modeled exchange arrival, removes queue-ahead shares on cancels and fills, and recognizes a fill when executions reach post-arrival orders behind it. The TSLA hedge is opened after the ETF fill and both legs exit aggressively after the hold interval.

The base 28-session run is in `backtests/queue_maker_20260901-20261009/`.
Across 4,657 fills, only 19 trades were positive (0.41%); simulated fill P&L
was −$8,205.78. The separate pair markout analysis in
`backtests/queue_maker_markouts_20260901-20261009/` shows negative average
markouts at each horizon: −$0.79 at 100 ms, −$0.73 at 500 ms, −$0.71 at 1 s,
and −$0.67 at 5 s. This supports adverse selection as the main issue rather
than only a poor quote-life choice.

The execution-evaluation rerun covers July–September 2026 in
`backtests/execution_eval_20260701-20260930/`. It enforces one live ETF order
per pair, logs the order outcome funnel, separates order/cancel/hedge/exit
latencies, and measures per-leg implementation shortfall and markouts. The
same baseline produced 23 fills (10 TSLL, 13 TSLR, none TSLG), all negative,
for −$30.69 net P&L. The funnel contained about 1.54 million order attempts;
1.49 million reached the simulated book, for a 0.0015% fill rate per attempt
or 0.00155% per on-book order. These are sequential attempts over the full
quarter, not simultaneously resting orders. This very low rate reflects the
strict passive queue and cancellation assumptions and makes the result highly
sensitive to how queue priority and order lifetimes are modeled. The monthly
fill counts were 15 in July, 3 in August, and 5 in September; monthly net P&L
was −$20.28, −$4.09, and −$6.32. About 6.99 million signals passed the
residual/direction checks, and 5.45 million were rejected by the expected-cost
gate before order attempts.

**Caveat:** this 64-session funnel predates the correction that rejects TSLA
signals fading during the 1 ms order transit. Its posted-order denominator and
fill rate are superseded; the 10-session corrected pilot below is the current
cancellation diagnostic. Re-run the full quarter before using corrected
aggregate execution rates in final conclusions.

A corrected chronological 10-session pilot (July 1–15) is saved in
`backtests/pilots_cancel_diagnostics_fixed/20260701-20260715/queue-maker/`.
After excluding signals that faded before the 1 ms arrival, 82,763 orders
reached the simulated book and 5 filled, all negative, for −$8.19. Among
82,758 unfilled on-book orders, 74,852 (90.5%) were canceled after the TSLA
imbalance faded; 5,123 (6.2%) reached the one-second quote timeout, while the
rest were canceled by ETF quote changes or crossing. Another 104,057 candidate
signals faded before arrival and 11,425 ETF quotes became stale before arrival.
These are diagnosis counts from ten sessions, not evidence of stable rates.

**Superseded latency sensitivity:** the earlier 0 ms versus 1 ms comparison
used aggregate trades and a fixed touch-size queue estimate. It is retained in
`backtests/pilots_latency0/` and `backtests/pilots_latency1_current/` for audit,
but its fill counts are not comparable to the order-level MBO replay below.

**Order-level queue replay pilot:** the corrected 10-session July pilot is
saved in `backtests/pilots_order_level_50us_all_delays/20260701-20260715/queue-maker/`.
Entry, hedge, and exit delays are each 50 microseconds; cancel latency is zero.
Of 154,277 orders reaching the simulated book, 19 filled completely and 50
received partial fills (990 shares total). Thus 69 orders had some fill, a
0.045% any-fill rate per submitted order. The paired simulation reports −$38.25
net P&L across those 69 orders, with 2 winners; TSLL contributed 15 filled
orders and −$16.40, while TSLR contributed 54 and −$21.85. TSLG had no fills.
Partial shares from each order are aggregated at the last partial-fill time
for hedge and P&L, so these results remain approximate. The old aggregate-queue
pilot's 5–8 fill counts should not be used alongside this run as a direct
latency comparison: both the queue model and delays differ.

**TSLA lead/lag signal on the order-level replay:** `backtest_queue_maker.py`
now accepts `--signal-mode leadlag`. This mode detects a 100 ms TSLA mid-return
crossing a configured threshold, then requires the opening-anchored ETF
residual to show the ETF lagging in that same direction. The passive ETF order
uses the same MBO queue replay and residual cost gate; TSLA is hedged after an
ETF fill. This is a concrete trading rule built from the earlier descriptive
lead/lag study, rather than a direct conversion of its response-time estimate.

The 10-session July pilot used a 5 bp trigger, 100-share requested size, a
one-second quote lifetime and holding period, and zero entry, hedge, exit, and
cancel delays. Of 13,326 residual-and-direction eligible signals, 11,746 were
rejected by the expected-cost gate. Of 1,580 remaining order attempts, all
reached the simulated book. One order received a 10-share partial fill; none
filled completely. The partial pair lost $0.23. Of 1,579 orders with no fill,
1,287 (81.5%) were cancelled after the rolling TSLA return fell below the
trigger threshold. Results are
in `backtests/pilots_leadlag_order_level_zero_delays/20260701-20260715/queue-maker/`.
Zero delays are an optimistic counterfactual, and this one partial fill is far
too little evidence to assess the strategy. The lead/lag trigger is also not
directly comparable with the imbalance-triggered queue-maker results above.

**ETF-only TSLA lead/lag:** this is the strategy of using TSLA's 100 ms return
to predict ETF direction, without requiring an ETF residual and without holding
a TSLA hedge. It was first tested with aggressive BBO fills; over July–September
the 100 ms / 5 bp / 1 s long-only setting lost $23,379 on 24,791 trades after
spread. The order-level queue replay posts passively at the ETF touch and exits
the ETF after one second. The 100 ms window defines the trigger; once the
threshold crossing occurs, the order remains live until an ETF quote change,
opposite-touch crossing, or one-second timeout. It does not require the rolling
return to remain above 5 bp. In the zero-delay July 1–15 pilot, 213 orders
reached the book: 12 filled fully and 10 partially (131 partial shares total).
The 22 filled orders produced 6 winners and **−$2.37 net P&L**; TSLL contributed
−$0.12 across 10 filled orders, TSLR −$2.25 across 10, and TSLG $0 across 2.
The results are in
`backtests/pilots_leadlag_etf_only_event_trigger_zero_delays/20260701-20260715/queue-maker/`.
An earlier variant cancelled as soon as the rolling return fell back below
threshold; it reached 1,285 orders with 18 fills and −$9.86. That cancellation
rule did not match a one-shot threshold trigger, so its result is superseded.
Both MBO tests use zero delays and one short pilot; neither establishes whether
passive execution improves the strategy.

The otherwise identical **50 ms lookback** variant generated 268 orders, of
which 23 received fills (14 full, 9 partial; 77 partial shares). It had 7
winners and **+$4.63 net P&L**: TSLL +$8.00 across 10 filled orders, TSLR −$1.37
across 10, and TSLG −$2.00 across 3. Results are in
`backtests/pilots_leadlag_etf_only_50ms_event_trigger_zero_delays/20260701-20260715/queue-maker/`.
Because this variant was run after reviewing the 100 ms result, its positive
P&L is exploratory and needs an untouched holdout before interpretation.

Lowering both the trigger and edge buffer to 1 bp while keeping the 50 ms
lookback generated 218,405 eligible signals. The ETF cost gate filtered 218,205,
leaving 200 orders. Nineteen orders received fills (9 full and 10 partial, 184
partial shares); 5 won and net P&L was **−$4.09**. TSLL contributed −$4.00
across 9 fills and TSLR −$0.09 across 10; TSLG had no fills. Results are in
`backtests/pilots_leadlag_etf_only_50ms_1bp_trigger_buffer_zero_delays/20260701-20260715/queue-maker/`.
This is another same-sample parameter change, so compare it as exploratory
diagnostics rather than evidence that 1 bp is an inferior stable setting.

Restoring the 5 bp TSLA trigger while keeping the 1 bp cost buffer produced
281 orders and 24 filled orders (17 full, 7 partial; 57 partial shares). Eight
trades won and net P&L was **−$17.49**: TSLL −$7.00, TSLR −$7.49, and TSLG
−$3.00. The output is in
`backtests/pilots_leadlag_etf_only_50ms_5bp_trigger_1bp_buffer_zero_delays/20260701-20260715/queue-maker/`.
This setting and the 5 bp / 2 bp setting produce sharply different results on
the same ten sessions, which points to substantial sample sensitivity.

Reducing requested size to one ETF share kept the same 281 order attempts and
24 filled orders, now all complete. Eight trades won; net P&L was **−$0.06**
across 24 shares: TSLL −$0.07, TSLR +$0.04, and TSLG −$0.03. Results are in
`backtests/pilots_leadlag_etf_only_50ms_5bp_1share_zero_delays/20260701-20260715/queue-maker/`.

The one-tick-improved quote variant used the same signals, cost gate, one-share
size, and zero delays, with `--quote-offset-ticks 1`. It reached the book 278
times and filled 18 one-share orders (3 winners), for **−$0.49** net. The
simulator skips a one-tick improvement when it would lock or cross the opposite
touch, so this tests passive one-cent price improvement only where available.
Its output is in
`backtests/pilots_leadlag_etf_only_50ms_5bp_1share_1tick_zero_delays/20260701-20260715/queue-maker/`.

The 23-trade paired markout averaged −$0.64 at 100 ms, −$0.55 at 500 ms,
−$0.47 at 1 s, and −$0.39 at 5 s. ETF maker entry shortfall versus the order
arrival midpoint was favorable by $8.50 total, while the TSLA hedge entry cost
was $6.33 and the two aggressive exit legs cost $19.95 versus their midquotes.
These dollar decompositions describe different reference points: the paired
markout starts from the ETF limit and TSLA midpoint, while arrival shortfall
compares the posted ETF price with the midpoint when the order became
actionable. Full per-trade and per-leg summaries are in the run's `markouts/`
folder. Unfilled-order counterfactuals are labeled separately and are not
realized P&L. Cancelled and timed-out attempts had a +$0.38 mean hypothetical
markout under an assumed fill at arrival. This is not an achievable return,
but it illustrates why the small set of actual passive fills can have very
different outcomes from all signal opportunities.

Variants with saved outputs:

- **Join vs. improve one tick, quote lifetime sweep:**
  `backtests/queue_maker_quote_sweep_20260901-20261009/comparison.csv`. All
  saved rows had zero wins and few fills. Joining for 100 ms had the least
  negative P&L (−$2.90 across three trades); longer quote lives accumulated
  larger losses.
- **Cancel-edge filters, with and without a residual floor:**
  `backtests/queue_maker_cancel_edge_20260901-20261009/` and
  `backtests/queue_maker_cancel_edge_nofloor_20260901-20261009/`. The filters
  reduced activity to 2–10 trades, all losses. This is too little activity to
  call an improvement.
- **Additional join / improve1 hold-time trials:** the `queue_maker_tune_*`
  folders save 100, 250, and 500 ms holding variants. Their observed fills
  were few and all recorded win rates were zero.

The earlier numbered conversational variants do not all have uniquely labeled
result files. This page therefore reports the named, retained output folders
above rather than assigning results to an uncertain numbered variant.

#### Fill toxicity by pre-arrival conditions

I joined the saved 4,657-fill September 1–October 9 queue-maker run to the
100 ms, 500 ms, 1 s, and 5 s pair markouts, then reconstructed ETF and TSLA
top-of-book conditions immediately before simulated ETF order arrival. ETF
spread, same-side touch depth, queue ahead, and ETF quote age were bucketed
into within-ETF quartiles. TSLA pressure was signed to the ETF position; ETF
pressure was similarly signed. Confidence intervals resample trading days as
clusters. The results are conditional on a fill: the saved directory lacks the
matching complete candidate funnel, so this cannot estimate how a filter
would affect fill rate.

At 100 ms, mean pair markout was −$0.787 across all fills. The clearest
associations were:

| Condition contrast | Fills (A / B) | Mean markout A / B at 100 ms | B − A, day-bootstrap 95% CI | At 500 ms, B − A |
|---|---:|---:|---:|---:|
| Low / high ETF touch-depth quartile | 1,433 / 1,174 | −$0.882 / −$0.606 | +$0.276 (+$0.226, +$0.330) | +$0.266 (+$0.207, +$0.325) |
| Low / high queue-ahead quartile | 917 / 1,166 | −$0.752 / −$0.598 | +$0.154 (+$0.097, +$0.215) | +$0.140 (+$0.088, +$0.196) |
| Afternoon / opening half-hour | 619 / 2,593 | −$0.578 / −$0.932 | −$0.355 (−$0.466, −$0.250) | −$0.305 (−$0.406, −$0.200) |

Spread quartiles did not show a clear relationship: high-minus-low mean
markout was −$0.047 at 100 ms (95% CI −$0.171 to +$0.087). ETF quote age was
also inconclusive (+$0.055 for oldest-minus-newest quartile; CI −$0.006 to
+$0.111). Strong versus moderate TSLA pressure showed little difference
(+$0.018; CI −$0.024 to +$0.063); the entry rule already requires aligned
pressure, so this sample contains no opposed-TSLA-pressure group. A strong
ETF-pressure-aligned bucket had only 31 fills, too few to support a filter
claim.

The more negative opening markouts appeared in all three ETFs. The better
markouts with deeper touch size and larger queue ahead also appeared by ETF,
but these two conditions are related and may reflect selection into which
orders eventually fill. These are not causal estimates: all buckets remained
negative on average, and the pattern cannot show that excluding low-depth or
low-queue orders would improve strategy P&L after its effect on fills and
opportunity cost. The existing queue-maker strategy remains negative overall.

Outputs are in
`backtests/queue_maker_fill_toxicity_20260901-20261009/`. Recreate the study
with `python3 analyze_fill_toxicity.py`; the script saves per-fill conditions,
pooled and per-symbol tables, quartile cutpoints, and day-cluster bootstrap
contrasts. This uses the older September–October simulator run; do not combine
its result directly with the later, stricter 23-fill run.

### 5. TSLA-triggered stale-quote IOC

`backtest_stale_quote.py` uses the first jointly observed regular-session
TSLA/ETF midquotes as a 2x opening-anchored fair value. At each TSLA midprice
change, it checks whether that fair value clears the ETF's displayed ask or bid
by the estimated exit half-spread plus a 1 bp buffer. It sends a one-share IOC
limit at the stale touch after the configured delay. The replay snapshots the
resting MBO order IDs at signal time and only counts a fill if those orders are
still present at arrival; it then crosses the ETF spread at 1, 10, 50, 100,
and 1,000 ms markout horizons.

The 10-session July pilot uses 50 µs entry and exit delays, zero fees, and one
share. Of 62,422 candidates, 59,442 filled; 2,980 stale quotes were gone before
arrival. Even with the 95.2% fill rate, the 1 s markout lost **$514.86** before
fees, with 2,300 winners among 59,420 positions with an available exit (3.9%).
The 100 ms markout lost $542.91. The zero-delay sensitivity filled 61,458
orders and lost $511.26 at 1 s. The delay assumption changes fill counts but
does not change the negative conclusion. The result indicates that this fair
value signal is not sufficiently predictive over the tested horizons; it does
not rule out a differently calibrated or better-conditioned stale-quote model.

The stale-quote replay now also has `--min-tsla-move-bps`, which screens on
the absolute cumulative TSLA return from the session anchor. This is distinct
from the ETF edge buffer. With the 500 ms hold, 5 bp move threshold, and
overlapping positions, the run filled 128,585 times, had a 4.42% profitable
exit rate, and lost **$1,164.32**. Raising the threshold to 10 bp reduced fills
to 126,702 and profitable exits to 4.34%, while the loss improved slightly to
**$1,148.11**. The higher threshold did not improve win rate, and the dollar
change is small relative to the pilot loss. Raising it to 20 bp produced
121,837 fills, a 4.15% profitable-exit rate, and **−$1,103.88**. At 30 bp,
there were 116,883 fills, a 3.98% profitable-exit rate, and **−$1,053.76**.
The lower aggregate losses came with fewer trades; win rate declined at each
higher threshold, and average loss per fill was nearly unchanged. The CLI
default is now 10 bp.

Since the ETF is the traded instrument, its full displayed BBO spread is the
more direct execution-cost filter. The edge gate already requires estimated
fair-value edge to clear half the ETF spread plus a 1 bp buffer. I added
optional `--max-etf-spread-bps` to skip signals when the full spread exceeds a
cap. At the 10 bp TSLA move threshold and 500 ms hold, the unfiltered
candidate-set median ETF spread was 4.76 bp (TSLG 16.09 bp, TSLL 7.99 bp, TSLR
4.67 bp):

| Maximum ETF spread | Fills | Profitable exits | Total P&L |
|---|---:|---:|---:|
| None | 126,702 | 4.34% | −$1,148.11 |
| 5 bp | 86,767 | 5.53% | −$766.05 |
| 8 bp | 106,599 | 5.11% | −$957.01 |
| 10 bp | 115,478 | 4.69% | −$1,037.83 |

The 5 bp cap performed best among these settings, but all its fills were in
TSLR; 8 bp and 10 bp included TSLL and TSLR, while TSLG's sampled spreads were
above those caps. The cap therefore changes which ETF is traded as well as
the execution cost. All runs still lost, and this 10-session sample does not
establish 5 bp as a generally optimal cutoff. The cap is on the full displayed
spread at signal time; other execution costs remain omitted or zero as
configured.

#### What distinguishes profitable exits?

For the 5 bp ETF-spread cap, I compared the 4,800 positions that found a
positive exit quote within 500 ms with the 81,967 positions forced out at the
time cap. Every position in this comparison was TSLR because the cap excluded
TSLL and TSLG on this signal sample. First-profitable-quote exits took a median
of about 193 ms and earned roughly **+$0.01** each; forced exits averaged about
**−$0.0099** each.

The strongest descriptive differences were:

| Signal cohort | Reached a profitable quote within 500 ms |
|---|---:|---:|
| Signal time, 9:30–10:00 ET | 13.0% |
| Signal time, after 10:00 ET | 2.2% |
| ETF displayed size at arrival, ≤25 shares | 13.0% |
| ETF displayed size at arrival, >200 shares | 2.1% |

Comparing features conditioned on the eventual exit:

| Signal-time feature | Profitable exits | Forced exits |
|---|---:|---:|
| ETF spread, median | 4.48 bp | 4.65 bp |
| Fair-value edge, median | 4.11 bp | 4.00 bp |
| Absolute TSLA move from open, median | 129 bp | 172 bp |

Early-session signals and thinner displayed ETF touch size were associated
with more frequent favorable quote moves. The initial edge was nearly the
same for both groups; larger opening-anchored TSLA moves did not identify the
better exits. These are associations, not causal explanations. The outcomes
are defined by first reaching a positive quote, the sample covers only 10
days, and positions overlap, so observations are not independent. A useful
next check is to set hour/depth filters on earlier sessions and evaluate them
on held-out days, then test whether gains persist after realistic fees and
constrained aggregate inventory.

#### Restrict entries to the early window and thin ETF touch

I applied both candidate filters: signal entries only from 9:30 to 10:00 ET
(with modeled entry arrival required before 10:00), and a maximum displayed
ETF best-level size of 25 shares at arrival. The other settings stayed at a
10 bp TSLA move threshold, 5 bp ETF spread cap, 500 ms maximum hold, one share,
50 µs entry/exit delays, and zero fees. The run produced 3,821 fills, of which
779 (20.4%) reached a profitable quote; realized P&L was **−$23.52**. The
positive exits averaged 144 ms to exit and about +$0.01; the 3,042 forced
exits averaged −$0.0103. There were also 13,933 otherwise-triggered orders
rejected because displayed size exceeded 25 shares and 1,269 stale quotes that
were gone by arrival.

Compared with the all-day 5 bp ETF-spread-cap run, the hit rate rose from 5.5%
to 20.4%, while fills fell from 86,767 to 3,821 and total loss narrowed from
−$766.05 to −$23.52. The remaining loss means the filters improved this pilot
but did not make it profitable. Since the time and size conditions were chosen
after examining these same sessions, this result needs held-out session
validation; overlapping positions and omitted costs still apply. The entry
window does not constrain exits, so positions can exit after 10:00 ET.

Results are in
`backtests/pilot_stale_quote_wait_profit_open30m_size25/20260701-20260715/`.

#### Rolling TSLA signal

The original signal used cumulative TSLA return and ETF fair value from the
first jointly available RTH quotes. Rolling mode instead measures TSLA return
over a fixed lookback and applies that return to the ETF midquote at the start
of the same window. At every TSLA midquote update, the reference is the last
TSLA and ETF quote at or before the lookback boundary. Rolling mode is now the
CLI default with a 50 ms lookback; the original is available with
`--signal-mode session-anchor`. The ETF edge gate and stale-touch execution
are otherwise unchanged.

I compared 50 ms and 100 ms lookbacks with 5 bp and 10 bp minimum TSLA moves.
All runs retained one share, zero fees, 50 µs entry/exit delays, a 1 bp edge
buffer, 5 bp ETF spread cap, 09:30–10:00 ET signal window, 25-share maximum
arrival size, and a 500 ms wait-profitable exit. There was no ETF quote-age
cap. The anchor-based control under these entry filters had 3,821 fills, a
20.4% hit rate, and **−$23.52**.

| Lookback | TSLA threshold | Fills | Profitable exits | Hit rate | Net P&L | Forced exits |
|---:|---:|---:|---:|---:|---:|---:|
| 50 ms | 5 bp | 288 | 121 | 42.0% | −$1.74 | 167 |
| 50 ms | 10 bp | 41 | 20 | 48.8% | −$0.48 | 21 |
| 50 ms | 15 bp | 7 | 4 | 57.1% | −$0.03 | 3 |
| 100 ms | 5 bp | 339 | 128 | 37.8% | −$2.06 | 211 |
| 100 ms | 10 bp | 48 | 25 | 52.1% | −$0.47 | 23 |

All rolling runs had higher observed hit rates and lower aggregate losses than
the anchor control, but remain negative. At 15 bp, only 7 of 20 order attempts
filled, across four sessions; four exits made one cent each, while the forced
exits lost seven cents combined. The apparent 57.1% hit rate is too sparse to
interpret. The 10 bp runs also have only 41 and 48 fills, and forced exits
still account for nearly all net loss (−$0.68 to −$0.72 at 10 bp). The results
are in-sample and overlapping trades are not independent; they warrant a
held-out test but do not establish an edge. Outputs are in
`backtests/pilot_stale_quote_open30m_size25_rolling50ms_move5bps/20260701-20260715/`,
`backtests/pilot_stale_quote_open30m_size25_rolling50ms_move10bps/20260701-20260715/`,
`backtests/pilot_stale_quote_open30m_size25_rolling50ms_move15bps/20260701-20260715/`,
`backtests/pilot_stale_quote_open30m_size25_rolling100ms_move5bps/20260701-20260715/`,
and
`backtests/pilot_stale_quote_open30m_size25_rolling100ms_move10bps/20260701-20260715/`.

#### Entry filters on the rolling signal

I tested two entry filters on the 50 ms / 5 bp rolling signal, separately and
together: maximum ETF quote age of 50 µs at the TSLA signal, and trade-signed
ETF top-five-level depth imbalance at or below zero (the book is not leaning
with the proposed trade). The 500 ms wait-profitable exit and all other entry
settings stayed fixed.

| Entry filters | Fills | Profitable exits | Hit rate | Net P&L | Mean P&L / fill |
|---|---:|---:|---:|---:|---:|
| None (control) | 288 | 121 | 42.0% | −$1.74 | −0.60¢ |
| Quote age ≤50 µs | 95 | 33 | 34.7% | −$0.68 | −0.72¢ |
| Top-five pressure ≤0 | 133 | 59 | 44.4% | −$0.88 | −0.66¢ |
| Both filters | 46 | 16 | 34.8% | −$0.38 | −0.83¢ |

Quote age and combined filters reduced total dollar loss mainly by reducing
trade count; average P&L per fill got worse. The pressure filter raised hit
rate by 2.4 percentage points, but still lost more per fill than the control.
None demonstrates improved expectancy. These remain exploratory, in-sample
filters on 10 July sessions. Outputs are in
`backtests/pilot_stale_quote_open30m_size25_rolling50ms_move5bps_age50us/20260701-20260715/`,
`backtests/pilot_stale_quote_open30m_size25_rolling50ms_move5bps_pressure5le0/20260701-20260715/`,
and
`backtests/pilot_stale_quote_open30m_size25_rolling50ms_move5bps_age50us_pressure5le0/20260701-20260715/`.

#### Multi-tick target with an adverse-mid stop

I tested a 2-tick executable profit target, a stop after a 1-tick adverse move
in ETF midprice from entry, and the existing 500 ms cap. The stop uses midprice
movement to avoid triggering solely because the exit crosses the spread; the
actual exit is modeled at the BBO after 50 µs. Entry settings matched the
50 ms / 5 bp rolling baseline: 288 fills, one share, zero fees, 1 bp edge
buffer, 5 bp ETF spread cap, 09:30–10:00 ET window, and 25-share arrival-size
cap.

The target-stop rule produced 18 target exits (+$0.50), 87 adverse-mid stops
(−$2.57), and 183 exits at the 500 ms cap (−$0.39). There were 72 profitable
trades overall (25.0%) and **−$2.46** net P&L, worse than the matched
wait-profitable baseline's 121 profitable trades (42.0%) and **−$1.74**. The
one-tick mid stop was too sensitive in this sample: stopped trades lost about
2.95¢ each on average after crossing the BBO, while the higher target did not
make up for those losses. This test does not support this exit configuration.
Results are in
`backtests/pilot_stale_quote_open30m_size25_rolling50ms_move5bps_target2_stop1tick/20260701-20260715/`.

#### Multi-level book pressure and five-minute buckets

I recorded ETF depth imbalance at the TSLA signal using the visible shares at
the top 1, 5, and 10 price levels. Imbalance is `(bid depth - ask depth) /
(bid depth + ask depth)`; the directional version is positive when the book is
heavier on the side of the proposed trade. With the 9:30–10:00 / 25-share
filters above, no depth horizon produced a monotonic increase in profitable
exit rate as pressure aligned more strongly with the trade:

| Trade-aligned pressure quartile | Top 1 level | Top 5 levels | Top 10 levels |
|---|---:|---:|---:|
| Lowest | 22.4% | 25.2% | 20.1% |
| Second | 22.7% | 18.7% | 19.4% |
| Third | 19.6% | 18.4% | 20.7% |
| Highest | 16.9% | 19.2% | 21.4% |

The top-5 result hints that pressure against the trade direction may be
associated with more favorable ETF repricing, but it is non-monotonic and all
quartiles still lost money. It is a candidate for an out-of-sample test, not a
validated filter.

The six 5-minute windows showed a generally declining hit rate through the
half-hour, though the pattern was not perfectly monotonic:

| Signal window ET | Fills | Profitable exits | Hit rate | Net P&L |
|---|---:|---:|---:|---:|
| 9:30–9:35 | 927 | 256 | 27.6% | −$7.75 |
| 9:35–9:40 | 798 | 181 | 22.7% | −$4.86 |
| 9:40–9:45 | 690 | 119 | 17.2% | −$3.78 |
| 9:45–9:50 | 559 | 92 | 16.5% | −$3.09 |
| 9:50–9:55 | 459 | 77 | 16.8% | −$1.94 |
| 9:55–10:00 | 388 | 54 | 13.9% | −$2.10 |

Every bucket remained negative after modeled exits. These are exploratory
subgroups from the same 10 July sessions used to choose the filters; the
pressure and time cutoffs need held-out validation. Depth fields are included
in the run's `trades.csv` and `opportunities.csv` for further analysis.

#### ETF quote age

Quote age is measured from the last ETF BBO update strictly before the TSLA
signal to the signal timestamp. In the 3,821-fill baseline above, successful
exits had a median signal-time quote age of **355 µs**, versus **647 µs** for
forced exits; at modeled entry arrival the medians were 79 µs and 124 µs.
Contrary to the simple stale-quote hypothesis, newer ETF quotes were more
likely to reach a positive exit within 500 ms.

I tested maximum signal-time quote ages with all other filters held fixed:

| Maximum ETF quote age | Fills | Profitable exits | Hit rate | Net P&L |
|---|---:|---:|---:|---:|
| None | 3,821 | 779 | 20.4% | −$23.52 |
| 50 µs | 914 | 221 | 24.2% | −$5.30 |
| 100 µs | 1,145 | 274 | 23.9% | −$6.87 |
| 250 µs | 1,533 | 360 | 23.5% | −$9.53 |
| 500 µs | 1,857 | 423 | 22.8% | −$11.70 |

The 50 µs cap had the highest hit rate of those tested, but remained negative
before fees and sharply reduced fills. The age-to-hit relationship is not
monotonic at every finer age bin, so this is a modest in-sample association,
not evidence that fresh quotes cause better outcomes. Quote-age fields are in
the baseline run's `trades.csv`. The capped runs are in
`backtests/pilot_stale_quote_open30m_size25_quoteage50us/20260701-20260715/`,
`backtests/pilot_stale_quote_open30m_size25_quoteage100us/20260701-20260715/`,
`backtests/pilot_stale_quote_open30m_size25_quoteage250us_v2/20260701-20260715/`,
and
`backtests/pilot_stale_quote_open30m_size25_quoteage500us/20260701-20260715/`.

#### Entry edge-buffer sensitivity

I raised the additional entry edge buffer from the 1 bp baseline to 2, 3, and
5 bp, holding the other filters and the 500 ms wait-profitable exit fixed.
All runs use one share, zero fees, 50 µs entry/exit delays, the 10 bp TSLA
trigger, 5 bp ETF spread cap, 50 µs maximum ETF quote age, 09:30–10:00 ET
signal window, and 25-share maximum arrival size.

| Edge buffer | Fills | Profitable exits | Hit rate | Net P&L |
|---:|---:|---:|---:|---:|
| 1 bp baseline | 914 | 221 | 24.2% | −$5.30 |
| 2 bp | 797 | 193 | 24.2% | −$4.58 |
| 3 bp | 624 | 152 | 24.4% | −$3.53 |
| 5 bp | 336 | 86 | 25.6% | −$1.85 |

The stricter edge filter modestly raised hit rate and reduced the dollar loss,
but none of the settings was profitable. Average P&L per fill improved only
slightly (about −0.58¢ at 1 bp to −0.55¢ at 5 bp), while the number of fills
fell by 63%. These are exploratory in-sample results with overlapping
positions; the 5 bp setting needs an untouched holdout before it should be
favored. Outputs are in
`backtests/pilot_stale_quote_open30m_size25_quoteage50us_edge2bps/20260701-20260715/`,
`backtests/pilot_stale_quote_open30m_size25_quoteage50us_edge3bps/20260701-20260715/`,
and
`backtests/pilot_stale_quote_open30m_size25_quoteage50us_edge5bps/20260701-20260715/`.

Outputs are in
`backtests/pilot_stale_quote_ioc_50us/20260701-20260715/` and
`backtests/pilot_stale_quote_ioc_0us/20260701-20260715/`. These are Nasdaq MBO
replays, one ETF share per signal, with no commissions; real queue races,
failed IOC messages, market impact, and other trading venues are not observed.

#### Conditional adverse exit

I added `--exit-condition conditional-adverse` to exit when either TSLA
retraces at least 5 bp from its midquote at the entry signal, or ETF top-of-book
size imbalance turns at least 0.2 against the position. The imbalance is
`(bid size - ask size) / (bid size + ask size)`, signed to the trade direction.
The exit uses the latest ETF BBO strictly before its 50 µs modeled arrival; a
500 ms hard cap remains in place. This exit uses top-level displayed sizes,
not full-depth pressure or queue-position simulation.

On the same 10-session pilot, with the 10 bp TSLA trigger, 5 bp ETF spread cap,
50 µs maximum ETF quote age, 09:30–10:00 ET signal window, and 25-share maximum
arrival size, all 914 entries filled. The conditional rule exited 851 positions
on opposing ETF imbalance, 3 on TSLA reversal, and 60 at the 500 ms cap. It
produced 13 winners (1.4%) and **−$5.01** total P&L. The matched
wait-profitable exit produced 221 winners (24.2%) and **−$5.30**. The
conditional exit trimmed the loss by $0.29, but its very low win rate shows
that this top-of-book trigger often exits before the ETF quote has a chance to
move favorably. It is not an improvement in overall strategy expectancy.

This is a quote-based exit approximation: it assumes the displayed BBO can be
crossed at modeled arrival and does not account for fees, market impact, or
partial fills. Results are in
`backtests/pilot_stale_quote_open30m_size25_quoteage50us_conditional_exit/20260701-20260715/`.

#### Wait for a profitable exit

As a follow-up, `--exit-condition wait-profitable` holds each filled position
until the first later ETF quote where crossing the displayed spread would give
positive P&L after the configured per-share fees. If no such quote appears, it
forces an exit at the maximum hold time. The exit uses the latest BBO known at
modeled exit-order arrival and requires enough displayed top-level size for
the order. This is an executable-quote approximation, not an MBO queue replay
for the exit.

With the same 10 sessions, one share, 50 µs entry/exit delays, and zero fees,
the 1 s cap produced 62,400 fills. Only 4,033 (6.5%) reached a profitable
quote; the rest were forced out at the cap. Total realized P&L was **−$529.97**.
At a 5 s cap, the strategy took 35,472 fills because each position blocked
further signals longer; 7,613 (21.5%) exited profitably, but the forced exits
left total P&L at **−$272.36**. Longer holding improved the loss per pilot, but
waiting for positive quotes did not make the strategy profitable, and it
substantially reduced trading capacity.

Outputs are in
`backtests/pilot_stale_quote_wait_profit_1s/20260701-20260715/` and
`backtests/pilot_stale_quote_wait_profit_5s/20260701-20260715/`. The profit
condition includes the configured fees, but not commissions or other omitted
execution costs; the zero-fee runs only require a positive displayed-quote
P&L.

The initial wait-profitable runs serialized entries until the prior position
exited. I changed the wait-profitable mode so each position now exits
independently and later signals can open additional positions. On the same
sample, both hold caps admitted 130,028 fills. With a 1 s cap, 10,358 positions
(8.0%) exited at a profitable quote and total P&L was **−$1,150.27**. With a
5 s cap, 33,402 (25.7%) exited profitably and P&L was **−$1,040.08**. In both
cases the profitable exits averaged about 1¢, while forced exits dominated
losses. These overlapping-position results replace the serialized figures
above as the relevant estimate when new signals are allowed during a hold.

I also tested a 500 ms cap with overlapping entries. It retained the same
130,028 fills, but only 5,818 (4.5%) reached a profitable quote; total P&L was
**−$1,177.45**. This was worse than the 1 s and 5 s caps, so shortening the
hold to 500 ms did not improve this pilot.

The independent-position run has no cap on concurrent positions or aggregate
inventory, so these P&L totals assume each one-share entry can be funded and
exited independently. Results are in
`backtests/pilot_stale_quote_wait_profit_independent_1s/20260701-20260715/`
and `backtests/pilot_stale_quote_wait_profit_independent_5s/20260701-20260715/`.
The 500 ms run is in
`backtests/pilot_stale_quote_wait_profit_independent_500ms/20260701-20260715/`.
The 5 bp and 10 bp threshold runs are in
`backtests/pilot_stale_quote_wait_profit_independent_500ms_move5bps/20260701-20260715/`
and
`backtests/pilot_stale_quote_wait_profit_independent_500ms_move10bps/20260701-20260715/`.
The 20 bp and 30 bp runs are in
`backtests/pilot_stale_quote_wait_profit_independent_500ms_move20bps/20260701-20260715/`
and
`backtests/pilot_stale_quote_wait_profit_independent_500ms_move30bps/20260701-20260715/`.
ETF spread-cap runs are in
`backtests/pilot_stale_quote_wait_profit_independent_500ms_move10bps_etfspread5bps/20260701-20260715/`,
`backtests/pilot_stale_quote_wait_profit_independent_500ms_move10bps_etfspread8bps/20260701-20260715/`,
and
`backtests/pilot_stale_quote_wait_profit_independent_500ms_move10bps_etfspread10bps/20260701-20260715/`.

## Connections to the lecture notes

The notes give a useful framework for interpreting these experiments. The
connections below are applications of the lecture concepts to our measured
results; they are interpretations, not claims that the lecture slides validate
these strategies.

| Lecture concept | Where it appears in the notes | Connection to our experiments |
|---|---|---|
| Statistical arbitrage and stable pair relationships | Session 3-2, pp. 22–27 (pairs trading, especially p. 26); Session 1, pp. 104, 109 | Our ETF residual fade is a simple relative-value signal. It assumes the ETF should track roughly 2x TSLA over the intraday window. The negative net P&L at both 15 bp and 30 bp entry thresholds says this residual plus the tested execution rule did not produce a profitable relationship in this sample. A rolling beta or a model of time-varying tracking error would be a distinct, still-untested model. |
| Informed trading, adverse selection, and price impact | Session 3-1, pp. 2, 13, 16–25, 28–30 (Glosten–Milgrom and Kyle); Session 3-2, p. 29 | The queue-maker's passive ETF fills are conditional on contra-side executions consuming the displayed queue. Those fills can arrive when the ETF price is moving against our quote. The maker's 100 ms pair markout was −$0.79 on average and only 9.2% positive, consistent with adverse selection being a major concern. The simulator is approximate, so this is evidence to investigate, not a precise estimate of the information component of spread. |
| FIFO queue position and queue value | Session 2, pp. 11, 20–23, 29, 35–38 | The updated queue-maker replays visible MBO order IDs: orders present at arrival are ahead, and later orders are behind. Fill counts increased relative to the former fixed-touch-queue approximation, but hidden orders, exact exchange arrival sequencing, and the impact of inserting our hypothetical order remain uncertain. |
| Latency competition and event timing | Session 3-2, pp. 15, 44, 52–53; Session 2, pp. 43–49, 61–63 | The lead/lag study uses `ts_event` and found impulse-response half-times from about 0.43 ms (TSLR) to 16.9 ms (TSLG) on October 9. The latest queue-maker pilot models 50 µs from signal to exchange arrival and 50 µs for hedge and exit delays. These fixed delays are assumptions, not measured end-to-end latency distributions; the replay is not an exchange-colocated latency model. |
| Liquidity, spread, depth, and resiliency | Session 4-2, pp. 13–14, 18–20, 22–26 | Crossing backtests show positive pre-spread mid-price P&L but negative net P&L after spreads in many configurations. The 15 bp residual run earned about $9.8k before spread and paid about $27.9k in modeled spread. This matches the lecture distinction between quoted/effective spread, size-aware round-trip costs, available depth, and how quotes replenish after liquidity is removed. Our analysis uses displayed Nasdaq BBO, not consolidated or deeper-book liquidity. |
| Book pressure, trade pressure, lead/lag, trend, and reversion features | Session 3-2, pp. 32–41 | We tested TSLA best-level imbalance as the queue-maker timing filter, and separately tested an ETF best-level imbalance long-only signal. The latter lost $53.5k–$230.6k after spread across its four settings. We also tested simple momentum/reversal signals and measured cross-instrument lead/lag. The results suggest these signals need stronger conditioning; they do not rule out multi-level imbalance, signed trade flow, smoothed pressure, volatility-normalized trend, or combinations with residual signals. |
| Transaction-cost analysis and urgency trade-offs | Session 5, pp. 2–3, 28–37, 48, 52 | Our strategy summaries separate mid-price P&L from spread costs, and the markout study measures pair P&L after fills at 100 ms through 5 s. This is an initial TCA view. It omits or simplifies commissions, market impact, hedge slippage, borrow, and uncertainty in fill probability, all of which the notes identify as relevant execution costs. |
| Fundamental versus transitory price movements; inventory and liquidity risk | Session 4-2, pp. 22–26; Session 1, pp. 26, 108–110 | A residual can reflect temporary ETF dislocation, stale quotes, or a real change in expected value. The current fixed-leverage residual does not distinguish these causes. A paired position can reduce common TSLA exposure, but asynchronous hedge fills leave temporary directional exposure; our queue-maker enters the TSLA hedge only after the ETF passive fill. |

The lecture PDFs referenced here are located one directory above the repo:
`FN6908 AY2026-27 - Session 1 - Assoc Prof (Practice) Nelson Lau.pdf`,
`FN6908 AY2026-27 - Session 2 - Assoc Prof (Practice) Nelson Lau.pdf`,
`FN6908 AY2026-27 - Session 3-1 (Prc Frm Info Mkts) - Assoc Prof (Practice) Nelson Lau.pdf`,
`FN6908 AY2026-27 - Session 3-2 (HFT) - Assoc Prof (Practice) Nelson Lau.pdf`,
`FN6908 AY2026-27 - Session 4-2 (Liquidity & Volatility) - Assoc Prof (Practice) Nelson Lau.pdf`,
and `FN6908 AY2026-27 - Session 5 - Assoc Prof (Practice) Nelson Lau (2).pdf`.
Page numbers above refer to PDF pages/slides.

The most direct lecture-to-code map is Session 3-2: its taxonomy distinguishes
latency arbitrage, statistical arbitrage, and market making (p. 15), then
separately discusses pair selection (p. 26), market-making risks (p. 29), and
microstructure signals including book pressure, trade pressure, lead/lag, and
trend/reversion (pp. 32–41). Our current work touches each category, but the
saved results point most strongly to execution costs and adverse selection as
the unresolved problems.

## Next tests suggested by the lecture concepts

These are proposed experiments, not completed results. Run them on the same
dates and instruments where possible, and reserve a later date range as a
holdout.

1. **Attribute maker markouts and costs by condition.** For each ETF fill,
   separate ETF-leg and TSLA-hedge P&L, then bucket by side, residual size,
   imbalance, spread, displayed depth, recent signed trade flow, volatility,
   queue ahead, and time of day. Report 100 ms–5 s markouts, effective spread,
   hedge slippage, fees/rebates, and net P&L. This would test whether maker
   losses arise at fill selection, hedge timing, or aggressive exit. (Session
   4-2, pp. 14, 22; Session 5, pp. 34–46.)
2. **Build a point-in-time rolling-beta residual model.** Estimate beta and
   tracking error using only prior observations; compare it against the fixed
   2x leverage residual on identical dates. Use a beta-sized TSLA hedge and
   enter only when expected residual convergence clears measured pair
   round-trip costs plus a buffer. Check coefficient stability and residual
   behavior by volatility/spread regime, then evaluate on untouched dates.
   (Session 3-2, p. 26; Session 4-2, pp. 13–20; Session 5, pp. 6–8.)
3. **Add a maker toxicity filter.** Predict short-horizon pair markout using
   only pre-order features, and skip or shade quotes when book pressure, signed
   trade flow, spread/depth, or volatility indicates poor expected markout.
   Fit/calibrate on earlier sessions and test on later ones; include no-fill
   opportunity cost and maker rebates/fees. (Session 3-2, pp. 29, 32–36;
   Session 4-2, pp. 14, 20, 22; Session 5, pp. 12, 35, 43–46.)
4. **Make queue and latency assumptions explicit.** Track order-level FIFO
   priority from MBO messages where possible; separately vary signal-to-order,
   cancel, and hedge delays, and use latency distributions or activity
   regimes instead of a single fixed 1 ms. Report fill rate, markout, P&L, and
   unfilled opportunity cost under conservative/base/optimistic queue
   assumptions. (Session 2, pp. 20–23, 35–38; Session 3-2, pp. 44, 52–53;
   Session 5, pp. 6–12, 43–46.)
5. **Use an execution-cost benchmark consistently.** Compare passive and
   crossing variants using arrival-mid implementation shortfall, effective
   spread, post-fill markout, and market-adjusted pair P&L. Normalize for
   timing risk and report partial/no-fill outcomes, not only completed trades.
   (Session 4-2, p. 22; Session 5, pp. 34–46, 48–56.)
6. **Ablate richer order-flow and trend features.** Compare the tested top-level
   imbalance with distance-weighted multi-level pressure, signed trade count
   and size, EWMA-smoothed pressure, and residual × pressure interactions.
   Separately test volatility-standardized residual Z-scores, EMA trend, and
   rate-of-change signals. First score future pair markout, then simulate
   trading; use chronological holdouts and keep the feature set small because
   unstable or multicollinear signals can look strong in-sample. (Session 3-2,
   pp. 32–41, 44.)
7. **Study leveraged-ETF rebalance flow near the close.** Session 3-2 p. 28
   discusses estimating leveraged-ETF reset flow from leverage, assets, and
   underlying return. Build an event study around large estimated rebalance
   demand, then test a close-window strategy with costs. This is a slower
   short-term/MFT hypothesis, not a millisecond lead/lag trade; available AUM
   and actual hedge-flow data may limit the estimate. (Session 3-2, p. 28.)
8. **Condition execution on measured lead/lag and latency.** Trade only where
   the predicted ETF response remains after modeled signal, order, and hedge
   arrival times; compare fixed delays such as 0.5, 1, 5, and 20 ms and separate
   predictive horizon from quote-response time. The October 9 event-time
   response is descriptive and can be too fast to monetize under a 1 ms model
   delay. (Session 3-2, pp. 37–39, 53.)

## How to interpret these results

All backtests use Nasdaq displayed top-of-book quotes and the repository's
simulated fill rules. They do not model consolidated NBBO, hidden liquidity,
full queue priority, market impact, short-borrow availability/cost, ETF fees,
or complete hedge execution risk. Queue position is approximated from MBO
prints and is not exchange-certified. Commissions were zero in the reported
crossing backtests unless otherwise noted. The datasets cover a limited number
of sessions, and parameters were explored on overlapping dates.

Treat the current results as strategy diagnostics. Any follow-up comparison
should use the same dates, instruments, latency, costs, fill assumptions, and
an untouched holdout period, and should report trade count alongside win rate
and net P&L.
