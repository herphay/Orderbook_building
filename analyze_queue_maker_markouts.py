#!/usr/bin/env python3
"""Execution shortfall and leg/pair markouts for queue-maker fills."""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from backtest_queue_maker import get_quotes
from backtest import first_future


HORIZONS_MS = (100, 500, 1000, 5000)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trades", type=Path,
                    default=Path("backtests/queue_maker_20260901-20261009/trades.csv"))
    ap.add_argument("--data-dir", type=Path, default=Path("XNAS_MBO"))
    ap.add_argument("--out", type=Path,
                    default=Path("backtests/queue_maker_markouts_20260901-20261009"))
    ap.add_argument("--hedge-latency-ms", type=float, default=1.0)
    a = ap.parse_args()

    fills = pd.read_csv(a.trades)
    records: list[dict] = []
    for day, daily in fills.groupby("day", sort=True):
        cache = a.data_dir / f"xnas-itch-{int(day)}.mbo.dbn.zst.TSLA-TSLL-TSLR-TSLG.npz"
        z = np.load(cache, allow_pickle=False)
        date = pd.Timestamp(str(z["day"]))
        tsla = get_quotes(z["TSLA"], date)
        by_symbol = {s: get_quotes(z[s], date) for s in daily.symbol.unique()}
        for trade in daily.itertuples(index=False):
            etf = by_symbol[trade.symbol]
            etf_direction = 1 if trade.direction == "long_etf_short_tsla" else -1
            hedge_direction = -etf_direction
            hedge_entry_i = first_future(
                tsla.t, int(trade.etf_fill_time_ns + a.hedge_latency_ms * 1e6))
            if hedge_entry_i >= len(tsla.t):
                continue
            one = {"day": int(day), "symbol": trade.symbol,
                   "direction": trade.direction,
                   "fill_time_ns": int(trade.etf_fill_time_ns),
                   "etf_qty": int(trade.etf_qty),
                   "tsla_hedge_qty": int(trade.tsla_hedge_qty)}
            # Signed implementation shortfall: positive means the execution
            # paid through the relevant contemporaneous midpoint.
            if hasattr(trade, "order_arrival_mid"):
                one["etf_entry_shortfall_dollars"] = (
                    etf_direction * (trade.etf_limit_price - trade.order_arrival_mid)
                    * trade.etf_qty)
                one["etf_entry_shortfall_bps"] = (
                    etf_direction * (trade.etf_limit_price / trade.order_arrival_mid - 1)
                    * 1e4)
            if hasattr(trade, "tsla_hedge_entry_mid"):
                one["tsla_hedge_entry_shortfall_dollars"] = (
                    hedge_direction * (trade.tsla_hedge_entry_price - trade.tsla_hedge_entry_mid)
                    * trade.tsla_hedge_qty)
                one["tsla_hedge_entry_shortfall_bps"] = (
                    hedge_direction * (trade.tsla_hedge_entry_price / trade.tsla_hedge_entry_mid - 1)
                    * 1e4)
            if hasattr(trade, "etf_exit_mid"):
                one["etf_exit_shortfall_dollars"] = (
                    -etf_direction * (trade.etf_exit_price - trade.etf_exit_mid)
                    * trade.etf_qty)
                one["tsla_hedge_exit_shortfall_dollars"] = (
                    -hedge_direction * (trade.tsla_hedge_exit_price - trade.tsla_exit_mid)
                    * trade.tsla_hedge_qty)
                one["exit_shortfall_dollars"] = (
                    one["etf_exit_shortfall_dollars"] + one["tsla_hedge_exit_shortfall_dollars"])
            for horizon_ms in HORIZONS_MS:
                target = int(trade.etf_fill_time_ns + horizon_ms * 1e6)
                ei = int(np.searchsorted(etf.t, target, side="left"))
                si = int(np.searchsorted(tsla.t, target, side="left"))
                if ei >= len(etf.t) or si >= len(tsla.t):
                    one[f"markout_{horizon_ms}ms"] = np.nan
                    continue
                etf_mid = (etf.bid[ei] + etf.ask[ei]) / 2
                tsla_mid = (tsla.bid[si] + tsla.ask[si]) / 2
                # P&L from actual passive ETF entry and marketable TSLA hedge
                # entry, marked to both instruments' future midquotes.
                etf_pnl = etf_direction * (etf_mid - trade.etf_limit_price) * trade.etf_qty
                hedge_pnl = hedge_direction * (tsla_mid - trade.tsla_hedge_entry_price) * trade.tsla_hedge_qty
                one[f"etf_markout_{horizon_ms}ms"] = etf_pnl
                one[f"tsla_hedge_markout_{horizon_ms}ms"] = hedge_pnl
                one[f"markout_{horizon_ms}ms"] = etf_pnl + hedge_pnl
                one[f"etf_quote_time_{horizon_ms}ms"] = int(etf.t[ei])
                one[f"tsla_quote_time_{horizon_ms}ms"] = int(tsla.t[si])
            records.append(one)
        print(f"Processed {int(day)}", flush=True)

    detail = pd.DataFrame(records)
    a.out.mkdir(parents=True, exist_ok=True)
    detail.to_csv(a.out / "markouts_by_trade.csv", index=False)
    long = detail.melt(id_vars=["day", "symbol"],
                       value_vars=[f"markout_{h}ms" for h in HORIZONS_MS],
                       var_name="horizon", value_name="pair_markout_dollars")
    long["horizon_ms"] = long.horizon.str.extract(r"(\d+)").astype(int)
    summary = long.groupby(["symbol", "horizon_ms"], as_index=False).agg(
        observations=("pair_markout_dollars", "count"),
        mean_markout=("pair_markout_dollars", "mean"),
        median_markout=("pair_markout_dollars", "median"),
        win_rate=("pair_markout_dollars", lambda x: (x > 0).mean()),
        p10=("pair_markout_dollars", lambda x: x.quantile(.10)),
        p90=("pair_markout_dollars", lambda x: x.quantile(.90)),
    )
    totals = long.groupby("horizon_ms", as_index=False).agg(
        observations=("pair_markout_dollars", "count"),
        mean_markout=("pair_markout_dollars", "mean"),
        median_markout=("pair_markout_dollars", "median"),
        win_rate=("pair_markout_dollars", lambda x: (x > 0).mean()),
        p10=("pair_markout_dollars", lambda x: x.quantile(.10)),
        p90=("pair_markout_dollars", lambda x: x.quantile(.90)),
    )
    summary.to_csv(a.out / "symbol_summary.csv", index=False)
    totals.insert(0, "symbol", "ALL")
    totals.to_csv(a.out / "aggregate_summary.csv", index=False)
    # Leg-level markouts expose whether adverse selection is concentrated in
    # the passive ETF fill or the delayed TSLA hedge.
    leg_records = []
    for horizon in HORIZONS_MS:
        for leg, col in (("ETF", f"etf_markout_{horizon}ms"),
                         ("TSLA hedge", f"tsla_hedge_markout_{horizon}ms")):
            if col not in detail:
                continue
            values = detail[["symbol", col]].rename(columns={col: "markout_dollars"})
            values["leg"] = leg
            values["horizon_ms"] = horizon
            leg_records.append(values)
    leg_long = pd.concat(leg_records, ignore_index=True) if leg_records else pd.DataFrame()
    if not leg_long.empty:
        leg_summary = leg_long.groupby(["symbol", "leg", "horizon_ms"], as_index=False).agg(
            observations=("markout_dollars", "count"),
            mean_markout=("markout_dollars", "mean"),
            median_markout=("markout_dollars", "median"),
            win_rate=("markout_dollars", lambda x: (x > 0).mean()),
        )
        leg_summary.to_csv(a.out / "leg_markout_summary.csv", index=False)
    execution_cols = [c for c in detail if c.endswith("shortfall_dollars")]
    if execution_cols:
        execution_rows = []
        for col in execution_cols:
            group = detail.groupby("symbol")[col]
            part = group.agg(observations="count", mean_dollars="mean", median_dollars="median", total_dollars="sum").reset_index()
            part.insert(1, "metric", col.removesuffix("_dollars"))
            execution_rows.append(part)
        pd.concat(execution_rows, ignore_index=True).to_csv(a.out / "implementation_shortfall_summary.csv", index=False)
    print(totals.to_string(index=False))
    print(f"Saved markouts to {a.out}")


if __name__ == "__main__":
    main()
