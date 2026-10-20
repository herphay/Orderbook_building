# Mini-project plan: TSLA information flow and execution in leveraged ETFs

## Working title

**Can TSLA order-flow signals be traded in leveraged TSLA ETFs after execution costs?**

**Proposed topic summary for instructor approval:** We use intraday Nasdaq order-book and trade data to measure how quickly TSLA information appears in three 2x TSLA ETFs, then test whether that response can be captured after latency, queue, hedge, and spread costs. We compare a small set of pre-specified execution choices and explain the results through HFT, information flow, liquidity, and institutional execution concepts.

## Fit with the project brief

This is **Track A: Technical (Data-Driven Analysis)**. The brief asks projects to use data or simulation, develop signals or models, and evaluate trading or execution quantitatively. It also asks every project to identify participants, information and signals, their interaction, execution or market-quality implications, and possible mitigation or design changes. This plan addresses each item below. See the [group mini-project brief](../../FN6908%20-%20group%20mini-project%20brief.pdf).

The brief schedules the presentation for Week 6 and asks for concise slides with charts, diagrams, or models where useful; submit the slides after presenting. It asks groups to get instructor approval for the topic title and a 1–2 sentence summary. The proposed summary above is ready to adapt for that approval. Any AI-assisted analysis or drafting needs to be checked by the group, which remains responsible for the claims and conclusions.

## Main research question

> **Does short-horizon information in TSLA trades and quotes predict subsequent price changes in TSLL, TSLR, and TSLG strongly and quickly enough to support a positive net execution result?**

The project should distinguish three claims that are easy to conflate:

1. **Price discovery:** TSLA moves are followed by ETF quote changes.
2. **Prediction:** information available at a decision time predicts a future ETF or pair return.
3. **Tradability:** the predicted movement remains after order latency, fill probability, spread, hedge delay, and other modeled costs.

Evidence for the first claim alone does not establish the second or third.

## Questions to answer

### A. Information flow and signal quality

1. **How quickly does each ETF respond to TSLA price and order-flow changes?**
   - Measure ETF quote or midpoint responses at short horizons after TSLA signed trades and top-of-book imbalance changes.
   - Report response size and timing separately for TSLL, TSLR, and TSLG.
   - Compare with a time-shifted placebo so ordinary co-movement is not mistaken for event-driven response.

2. **Which TSLA information is useful: price changes, signed trade flow, displayed imbalance, or a combination?**
   - Compare a small, pre-specified feature set rather than searching a large parameter grid.
   - Test whether order-flow features add information beyond the contemporaneous TSLA return and ETF-versus-TSLA residual.

3. **Does an ETF residual forecast reversion, continuation, or neither at the tested horizons?**
   - Estimate residuals using synchronized quotes and the fund's stated leverage target.
   - Compare residual-only, TSLA-flow-only, and combined signals.
   - State clearly that a fixed 2x intraday relationship is an approximation; tracking, compounding, and quote staleness can contribute to the residual.

### B. Execution and strategy economics

4. **How much signal value survives the delay from observing TSLA to placing and hedging an ETF order?**
   - Measure the response at several order and hedge delays, including a zero-delay reference and the baseline delay.
   - Keep event timestamps and quote availability rules causal; an order cannot fill before it arrives.

5. **What is the trade-off between aggressive execution and passive queue execution?**
   - Compare a marketable ETF entry with a touch-joining maker order under the same signal and exit rule.
   - For maker orders, report eligible signals, submitted orders, fills, fill rate, cancellations, and timeouts. Do not report only completed trades.
   - Vary the assumed queue ahead as a sensitivity bound and label it as an approximation, not exact FIFO replay.

6. **Where do execution costs arise?**
   - Decompose ETF entry, TSLA hedge entry, ETF exit, and TSLA hedge exit shortfall versus contemporaneous midquotes.
   - Report per-leg and paired markouts at 100 ms, 500 ms, 1 s, and 5 s after the ETF fill.
   - Separate realized simulated P&L from unfilled-order counterfactual markouts, commissions, and any costs that are not modeled.

7. **Are the conclusions consistent across the three ETFs and across time?**
   - Show results by ETF and month, including the number of sessions, signals, orders, and fills.
   - Treat July–September 2026 as exploratory because these data and baseline strategies have already been inspected. Use a genuinely untouched period for confirmatory claims if one can be obtained; otherwise describe the work as exploratory and avoid calling a parameter choice out-of-sample.

## Draft answers to the brief's five core questions

These are evidence-based draft answers for the presentation. They distinguish
what the Nasdaq MBO records show from participant identities and mechanisms
that the records cannot establish.

### 1. Who are the key participants?

The economically relevant groups are displayed-liquidity providers in TSLA and
the ETFs; aggressive buyers and sellers that execute against displayed quotes;
HFT firms that monitor cross-instrument price and order-flow changes; and
institutional or retail investors whose orders contribute to the observed
flow. ETF issuers and authorized participants matter to the broader tracking
and creation/redemption mechanism, but this order-book sample does not identify
them or their activity as such.

For October 9, 2026, our behavioral grouping classified 84,184 order
lifecycles as execution-touched displayed liquidity, 471,544 as unfilled orders
canceled within 1 ms, and 1,659,898 as persistent or unfilled displayed quotes.
These are order-behavior categories, not counts of HFT firms, institutions, or
people. Databento's XNAS.ITCH normalization omits Nasdaq MPID attribution, and
trade records do not identify the aggressor's order. We therefore cannot say
who submitted two orders or assign a reliable trader type to an order.

### 2. What information is generated or revealed through trading?

The event stream reveals the side, price, size, and timing of displayed adds,
cancels, fills, and quote changes, plus trade activity and subsequent book
state. From this we can construct TSLA returns, signed trade flow where the
feed supplies a usable side, displayed depth imbalance, ETF quote changes,
quote age, order lifetime, and an ETF residual relative to an approximate 2x
TSLA move. The one-day behavioral grouping also measures short-lived quotes
and same-price replenishment patterns; neither reveals a participant's intent
or proves that the same trader replenished.

Across 64 July-September sessions, 46.0% of same-direction TSLL quote moves,
42.4% of TSLR moves, and 47.8% of TSLG moves had a strictly prior same-direction
TSLA quote move within 1 ms, versus 0.5% under the shifted-time placebo. This
shows a strong temporal ordering in these quote series, not causal information
flow or a directly tradable signal. The sample is Nasdaq-only, and the fixed
2x residual is an approximation affected by ETF tracking, compounding, and
quote staleness.

### 3. How do participants respond to or exploit this information?

The mechanism we investigate is that TSLA quote or trade changes become visible
first, ETF liquidity providers update their quotes, and aggressive flow can
execute against ETF quotes before they move. Passive ETF orders may be filled
when contra-side activity is strongest, which exposes the maker to adverse
selection. A paired strategy then has to hedge its ETF exposure in TSLA, adding
latency and execution risk. These are plausible interpretations of the event
patterns, not identities or motives observed in the feed.

The cross-instrument timing result is consistent with faster TSLA quote
updates preceding many ETF moves. But our tested signals have not converted
that ordering into robust net profits. For example, the queue-aware maker's
historical 4,657-fill run had negative mean pair markouts from 100 ms to 5 s;
the more restrictive July-September version produced only 23 fills and lost
$30.69. Different maker runs use different filters and are not directly
comparable. More generally, the tested crossing strategies were negative
after modeled spread costs in each month. Thus the data support studying
repricing and adverse selection, but do not show that a particular participant
successfully exploited it.

### 4. What are the implications for execution, cost, or market quality?

For our strategies, a timing signal alone is insufficient: crossing the ETF
spread, uncertain queue position, fill-conditioned adverse selection, and
delayed TSLA hedging can exceed the predicted move. In the 64-session runs,
the 15 bp residual fade lost $41,554 across 62,980 trades; the 30 bp version
lost $2,112 across 4,354 trades. The tested momentum/reversal and top-level
imbalance configurations also lost after spread. The earlier queue-maker
markout analysis was negative at each measured horizon. These results suggest
that the tested signals and execution rules did not overcome costs; they do
not prove that all TSLA-ETF signals or all execution methods fail.

Market-quality implications are two-sided. Fast quote updates can reduce stale
prices and improve price alignment, while rapid cancellations make displayed
liquidity less dependable and passive fills may be adversely selected. Our
one-day categories alone cannot measure the causal effect of this activity on
spreads, depth, or other investors' execution. We observe one venue, not
consolidated NBBO, hidden liquidity, or full routing decisions.

### 5. How can strategies or market design be improved?

For this strategy, require predicted pair edge to exceed estimated ETF spread,
hedge cost, and a pre-set uncertainty buffer; reject stale quotes and signals
that fade before simulated exchange arrival; and cap size using executable
depth. Compare aggressive and passive execution on the same signals, and
report the full funnel (candidates, orders, fills, partial fills, cancels,
timeouts), leg-level shortfall, and post-fill pair markouts. For passive orders,
test conservative and optimistic queue-ahead and latency assumptions rather
than presenting a single replay as exact FIFO execution. Use an untouched
chronological holdout before claiming improvement.

The project has already tested several spread, quote-age, pressure, edge, and
exit filters. They reduced activity or loss in some pilots but did not produce
a robust positive result. A next improvement should focus on execution
measurement and fill toxicity, then evaluate a small pre-specified set of
filters across all ETFs and dates. Any market-design recommendation (for
example, changing maker/taker incentives or adding a speed bump) would need a
separate causal analysis; this dataset and backtest do not identify the effect
of such a design change.

Supporting results are recorded in [strategies.md](../strategies.md),
[the October 9 behavioral-group report](../analysis/actor_groups_20261009/report.md),
and [the project guide](PROJECT_GUIDE.md).

## Proposed analysis design

### Data and sample

- Nasdaq ITCH MBO data already cached in `XNAS_MBO/` for TSLA, TSLL, TSLR, and TSLG.
- The existing July–September 2026 cache has 64 regular sessions (22 July, 21 August, 21 September; exchange holidays are absent).
- Reconstruct synchronized BBO and midpoint series from the event stream. Keep event time, quote time, and simulated order-arrival time explicit.
- The current repository's data are Nasdaq venue data. They do not show consolidated NBBO, hidden liquidity, trader identity, or full order-routing behavior.

### Core comparisons

Keep the presentation to a small set of comparisons:

1. **Response diagnostic:** TSLA trade/quote signal versus ETF response, including a shifted-time placebo.
2. **Signal comparison:** residual only, TSLA order-flow only, and combined signal, with fixed horizons and thresholds selected before the final comparison.
3. **Execution comparison:** marketable entry versus touch-joining maker entry under matched signals, position size, holding rule, and exit logic.
4. **Execution sensitivity:** order/hedge latency and queue-ahead assumptions; report fill rates and execution shortfall alongside P&L.

Avoid presenting the best row from a broad parameter sweep as the headline result. If tuning is necessary, separate tuning data from evaluation data by date and report the split.

### Primary outcome measures

- Signal response: signed ETF midquote change by horizon and response time after TSLA events.
- Prediction: mean and median future paired markout, sign accuracy, and sample count.
- Execution: candidate-to-order conversion, fill rate, time to fill, cancellation/timeout rate, and unhedged interval.
- Cost: per-leg implementation shortfall, pair execution cost, fees where available, and markouts after fills.
- Strategy result: gross midquote P&L, simulated fill P&L, fees, net P&L, trade count, win rate, and monthly breakdown.
- Uncertainty: dispersion across days and ETFs; do not rely on a win rate or aggregate P&L alone.

## Existing evidence and what remains open

The repository's July–September baseline runs found negative net results for the tested residual, momentum/reversal, and ETF imbalance configurations. The current selective queue-maker baseline recorded only 23 fills, all negative, so its performance is too sparse to assess reliably. Earlier queue-maker markouts were negative on average, but the older simulator did not report all candidate and leg-level execution diagnostics.

The initial 64-session execution evaluation recorded 23 fills from roughly 1.54 million order attempts, but a later review found that it did not reject TSLA signals that faded during the 1 ms order transit. Treat its order funnel as superseded pending a corrected full-period rerun. In the corrected 10-session pilot, 104,057 cost-qualified signals faded before arrival; among orders that reached the simulated book, TSLA imbalance fades triggered 90.5% of cancellations. The paired markouts from the earlier run were negative, but should be recomputed on the corrected fills before inclusion in final conclusions. Unfilled-order counterfactuals assume fills that may not have been achievable and are not realized returns.

These are useful motivation, not final project conclusions. The immediate analysis improvement is to report signal counts and order/fill denominators, separate entry/hedge/exit costs, and assess fill markouts. Strategy comparisons should use matched signals and clearly stated assumptions.

See [strategies.md](../strategies.md) for the running experiment log and [the project guide](PROJECT_GUIDE.md) for data and runner details.

## Recommended presentation storyline

1. **Problem:** ETF quotes may react to TSLA information with a delay, but an observed lead/lag is not automatically tradeable.
2. **Mechanism:** explain who observes TSLA flow, how ETF liquidity providers may react, and why passive fills can be adversely selected.
3. **Method:** show data, synchronization, signal definitions, matched execution rules, latency and queue assumptions, and evaluation split.
4. **Evidence:** present response curves, a simple passive-versus-aggressive comparison, candidate/fill funnel, leg-level shortfall, and monthly results.
5. **Implications:** explain whether information was predictable and whether the opportunity survived execution; state limits of venue data and simulated fills.
6. **Mitigation:** recommend cost/markout gates, cancellation rules, latency-aware sizing, or no-trade conditions supported by the results.

## Decisions to settle before the final run

- Which one or two TSLA signals will be the primary test: signed trades, displayed imbalance, or both?
- Which fixed holding horizons and order sizes are feasible to explain in the presentation?
- Is there an untouched date range available for a confirmation run, or should all results be labeled exploratory?
- Which fee, borrow, and ETF tracking assumptions can be sourced and included? If unavailable, report them as limitations and show cost sensitivity instead of silently treating them as zero.
- Who will prepare the response analysis, execution comparison, and microstructure interpretation for the group presentation?

## Scope boundary

This is a course mini-project, not a production HFT deployment. The goal is to produce a defensible comparison and explain the market mechanism. The simulator approximates queue priority and fills, uses Nasdaq data, and does not model every fee, routing decision, market impact effect, borrow constraint, or fund operating expense. Conclusions should be limited to the sampled instruments, dates, and assumptions.
