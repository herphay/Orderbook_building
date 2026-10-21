# MBO behavioral activity groups — 2026-10-09

Source cache: `XNAS_MBO/xnas-itch-20261009.mbo.dbn.zst.TSLA-TSLL-TSLR-TSLG.npz`

These labels summarize order behavior in the feed. They do not identify a person, firm, account, or stable actor. The cache omits MPID and channel_id; order IDs identify displayed orders, not participants.

Exclusive order labels use a 1 ms cutoff for unfilled orders canceled quickly. Replenishment is a separate marker: a new order at the same symbol, side, and price within 1 ms after a recorded fill at that price.

## Order behavior by symbol

| symbol | behavior_group                         | orders  | initial_shares | filled_shares | cancel_event_shares_raw | median_lifetime_us | orders_with_modifications | replenishment_marked_orders |
| ------ | -------------------------------------- | ------- | -------------- | ------------- | ---------------- | ------------------ | ------------------------- | --------------------------- |
| TSLA   | persistent or unfilled displayed quote | 1026455 | 56407612       | 0             | 55778953         | 167272.198         | 0                         | 1891                        |
| TSLA   | rapid cancel / fleeting quote          | 337336  | 19909346       | 0             | 19909346         | 198.077            | 0                         | 2694                        |
| TSLA   | execution_touched displayed liquidity  | 75425   | 4424559        | 4051788       | 829643           | 743295.414         | 0                         | 6129                        |
| TSLL   | rapid cancel / fleeting quote          | 47986   | 158122550      | 0             | 158122550        | 134.7385           | 0                         | 812                         |
| TSLL   | persistent or unfilled displayed quote | 300082  | 747374472      | 0             | 746808379        | 1225284.267        | 0                         | 759                         |
| TSLL   | execution_touched displayed liquidity  | 5703    | 9627547        | 6369012       | 4092247          | 5990460.632        | 0                         | 277                         |
| TSLR   | persistent or unfilled displayed quote | 251326  | 87643264       | 0             | 87630104         | 407982.98600000003 | 0                         | 160                         |
| TSLR   | rapid cancel / fleeting quote          | 73606   | 11320000       | 0             | 11320000         | 199.9785           | 0                         | 136                         |
| TSLR   | execution_touched displayed liquidity  | 2215    | 238970         | 212936        | 40858            | 46016.899          | 0                         | 189                         |
| TSLG   | persistent or unfilled displayed quote | 82035   | 452347585      | 0             | 452216039        | 1380412.571        | 0                         | 66                          |
| TSLG   | rapid cancel / fleeting quote          | 12616   | 69690862       | 0             | 69690862         | 127.5725           | 0                         | 72                          |
| TSLG   | execution_touched displayed liquidity  | 841     | 4309108        | 2470636       | 2313464          | 11721151.032       | 0                         | 29                          |

## Trade-print flow

Trade (`T`) messages are summarized separately because they do not provide a stable aggressor identity; side may also be absent.

| symbol | trade_prints | shares  | side_known_prints | side_unknown_prints |
| ------ | ------------ | ------- | ----------------- | ------------------- |
| TSLA   | 137223       | 8098863 | 90728             | 46495               |
| TSLG   | 1255         | 2699201 | 1225              | 30                  |
| TSLL   | 7690         | 7494661 | 7164              | 526                 |
| TSLR   | 2612         | 251373  | 2364              | 248                 |

## Feed event counts

| symbol | action | events  |
| ------ | ------ | ------- |
| TSLA   | R      | 1       |
| TSLA   | A      | 1439216 |
| TSLA   | T      | 137223  |
| TSLA   | C      | 1440940 |
| TSLA   | F      | 90728   |
| TSLL   | R      | 1       |
| TSLL   | A      | 353771  |
| TSLL   | C      | 412728  |
| TSLL   | T      | 7690    |
| TSLL   | F      | 7165    |
| TSLR   | R      | 1       |
| TSLR   | A      | 327147  |
| TSLR   | C      | 340780  |
| TSLR   | T      | 2612    |
| TSLR   | F      | 2364    |
| TSLG   | R      | 1       |
| TSLG   | A      | 95492   |
| TSLG   | C      | 112902  |
| TSLG   | T      | 1255    |
| TSLG   | F      | 1225    |

## Interpretation limits

- Execution-touched displayed liquidity means the tracked resting order received at least one fill; it does not reveal who submitted it.
- Rapid cancel / fleeting quote describes unfilled orders canceled within the selected lifetime cutoff. It is not evidence of spoofing or intent.
- Persistent or unfilled displayed quote means the order remained longer or was present at the end of the captured stream; this does not prove market making.
- Replenishment markers are price-level patterns, not proof the same participant replenished. Another participant can post at the same price.
- `cancel_event_shares_raw` sums raw size on Cancel records. Nasdaq execution messages normalize to Trade, Fill, then Cancel, so this value can overlap with filled shares and is not standalone cancellation volume.
- Orders with lifecycle events missing from the filtered day, replace semantics, or cache boundaries can be misclassified. Results are exploratory proxies.

Files: `group_summary.csv`, `orders.csv`, `trade_prints.csv`, and `event_counts.csv`.
