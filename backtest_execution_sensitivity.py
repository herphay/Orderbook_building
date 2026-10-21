#!/usr/bin/env python3
"""Compare touch fills with visible multi-level execution bounds.

This is a frozen-signal sensitivity analysis: it replays each baseline
mispricing trade's signal and exit condition under alternative displayed-depth
fills. A rejected/partial fill does not cause later signals to be regenerated.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from utils import leadlag
from utils.execution import depth_at_times, walk_market_order
from backtest import (Quotes, rth_quotes, run_mispricing_backtest, summarize,
                      first_future)
from utils.parallel import run_sessions


def load_quotes(records: np.ndarray, day: pd.Timestamp) -> Quotes:
    bbo = leadlag.build_bbo(records).sort_values("ts_event", kind="stable")
    bbo = bbo.drop_duplicates("ts_event", keep="last")
    q = Quotes(bbo.ts_event.to_numpy(np.int64), bbo.bid.to_numpy(float),
               bbo.ask.to_numpy(float), bbo.bid_sz.to_numpy(np.int64),
               bbo.ask_sz.to_numpy(np.int64))
    good = np.isfinite(q.bid) & np.isfinite(q.ask) & (q.bid > 0) & (q.ask > q.bid)
    return rth_quotes(Quotes(q.t[good], q.bid[good], q.ask[good],
                             q.bid_sz[good], q.ask_sz[good]), day)


def scenario_trades(baseline: pd.DataFrame, etf: Quotes, records: np.ndarray, *,
                    latency_ns: int, quantity: int, fee_per_share: float,
                    levels: int, model: str) -> tuple[pd.DataFrame, dict]:
    if baseline.empty:
        return baseline.copy(), {"signals": 0, "entry_rejected": 0, "exit_rejected": 0,
                                 "partial_entries": 0, "partial_exits": 0}
    targets, exit_targets = [], []
    for row in baseline.itertuples():
        targets.append(int(row.signal_time_ns) + latency_ns)
        exit_condition = row.exit_condition_time_ns
        if pd.isna(exit_condition):
            exit_targets.append(int(etf.t[-1]) - 1)
        else:
            exit_targets.append(int(exit_condition) + latency_ns)
    # Keep premarket records to establish book state, but exclude post-session
    # messages that could otherwise become the forced-close execution snapshot.
    session_records = records[records["ts_event"] <= int(etf.t[-1])]
    snapshots = (depth_at_times(session_records, np.asarray(targets + exit_targets, np.int64), levels)
                 if model != "touch" else None)
    out, entry_rejected, exit_rejected = [], 0, 0
    partial_entries, partial_exits = 0, 0
    for i, row in enumerate(baseline.itertuples()):
        direction = 1 if row.direction == "long" else -1
        if model == "touch":
            entry_i = first_future(etf.t, targets[i])
            if entry_i >= len(etf.t):
                entry_rejected += 1
                continue
            entry_px = etf.ask[entry_i] if direction > 0 else etf.bid[entry_i]
            entry_depth = int(etf.ask_sz[entry_i] if direction > 0 else etf.bid_sz[entry_i])
            entry_qty = min(quantity, entry_depth)
            entry_levels, entry_time = 1, int(etf.t[entry_i])
        else:
            entry = walk_market_order(snapshots, i, direction, quantity, levels)
            entry_px, entry_qty, entry_levels = entry.vwap, entry.filled_qty, entry.levels_used
            entry_time = int(snapshots.event_ns[i])
        if entry_qty == 0 or (model in ("touch", "complete") and entry_qty < quantity):
            entry_rejected += 1
            continue
        if entry_qty < quantity:
            partial_entries += 1
        exit_row = len(baseline) + i
        exit_cond = row.exit_condition_time_ns
        exit_target = exit_targets[i]
        if model == "touch":
            exit_i = first_future(etf.t, exit_target)
            if exit_i >= len(etf.t):
                exit_i = len(etf.t) - 1
            exit_px = etf.bid[exit_i] if direction > 0 else etf.ask[exit_i]
            exit_depth = int(etf.bid_sz[exit_i] if direction > 0 else etf.ask_sz[exit_i])
            exit_qty = min(entry_qty, exit_depth)
            exit_levels, exit_time = 1, int(etf.t[exit_i])
        else:
            exit = walk_market_order(snapshots, exit_row, -direction, entry_qty, levels)
            exit_px, exit_qty, exit_levels = exit.vwap, exit.filled_qty, exit.levels_used
            exit_time = int(snapshots.event_ns[exit_row])
        if exit_qty < entry_qty or (model == "touch" and exit_qty < quantity):
            exit_rejected += 1
            if exit_qty > 0:
                partial_exits += 1
            continue
        qty = entry_qty
        entry_i = first_future(etf.t, targets[i])
        if entry_i >= len(etf.t):
            entry_i = len(etf.t) - 1
        if model != "touch":
            # Mark-to-mid at the first ETF quote at or after the simulated MBO fill.
            entry_i = min(int(np.searchsorted(etf.t, entry_time, side="left")), len(etf.t)-1)
            exit_i = min(int(np.searchsorted(etf.t, exit_time, side="left")), len(etf.t)-1)
        entry_mid = (etf.bid[entry_i] + etf.ask[entry_i]) / 2
        exit_mid = (etf.bid[exit_i] + etf.ask[exit_i]) / 2
        pnl = direction * (exit_px - entry_px) * qty
        fees = 2 * qty * fee_per_share
        out.append({**row._asdict(), "quantity": qty,
                    "entry_time_ns": entry_time,
                    "entry_price": entry_px, "entry_depth": entry_qty,
                    "entry_levels_used": entry_levels,
                    "exit_time_ns": exit_time,
                    "exit_price": exit_px, "exit_depth": exit_qty,
                    "exit_levels_used": exit_levels,
                    "entry_mid": entry_mid, "exit_mid": exit_mid,
                    "mid_pnl_before_spread": direction * (exit_mid - entry_mid) * qty,
                    "pnl_after_spread_before_fees": pnl,
                    "entry_spread_cost": direction * (entry_px-entry_mid) * qty,
                    "exit_spread_cost": direction * (exit_mid-exit_px) * qty,
                    "fees": fees, "net_pnl": pnl-fees,
                    "entry_notional": entry_px*qty,
                    "fill_scenario": model if model == "touch" else f"l2_{model}",
                    "entry_levels": entry_levels, "exit_levels": exit_levels})
    stats = {"signals": len(baseline), "entry_rejected": entry_rejected,
             "exit_rejected": exit_rejected, "partial_entries": partial_entries,
             "partial_exits": partial_exits}
    return pd.DataFrame(out), stats


def _process_day(task: tuple) -> tuple[str, dict[str, list[pd.DataFrame]], list[dict]]:
    (day, cache_str, entries, exit_bps, max_hold_s, latency_ms, quantities,
     fee_per_share, levels) = task
    z = np.load(cache_str, allow_pickle=False)
    date = pd.Timestamp(str(z["day"]))
    tsla = load_quotes(z["TSLA"], date)
    day_results: dict[str, list[pd.DataFrame]] = {
        "touch": [], "l2_partial": [], "l2_complete": []}
    diagnostics = []
    latency_ns = int(latency_ms * 1e6)
    for symbol, fund in leadlag.FUNDS.items():
        raw = z[symbol]
        etf = load_quotes(raw, date)
        for entry_bps in entries:
            baseline = run_mispricing_backtest(
                tsla, etf, symbol=symbol, leverage=fund["L"],
                entry_bps=entry_bps, exit_bps=exit_bps,
                max_hold_ns=int(max_hold_s * 1e9), latency_ns=latency_ns,
                quantity=1, fee_per_share=fee_per_share)
            if baseline.empty:
                continue
            baseline["day"] = day
            for quantity in quantities:
                for model, tag in (("touch", "touch"), ("partial", "l2_partial"),
                                   ("complete", "l2_complete")):
                    scenario, stats = scenario_trades(
                        baseline, etf, raw, latency_ns=latency_ns,
                        quantity=quantity, fee_per_share=fee_per_share,
                        levels=levels, model=model)
                    if not scenario.empty:
                        scenario["day"] = day
                        scenario["requested_quantity"] = quantity
                        scenario["entry_threshold_bps"] = entry_bps
                        day_results[tag].append(scenario)
                    diagnostics.append({"day": day, "symbol": symbol,
                                        "entry_threshold_bps": entry_bps,
                                        "requested_quantity": quantity,
                                        "scenario": tag, **stats})
    return day, day_results, diagnostics


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start-day", default="20261005")
    ap.add_argument("--end-day", default="20261009")
    ap.add_argument("--data-dir", type=Path, default=Path("XNAS_MBO"))
    ap.add_argument("--out", type=Path, default=Path("backtests/execution_sensitivity"))
    ap.add_argument("--entry-bps", type=float, default=30.0)
    ap.add_argument("--exit-bps", type=float, default=5.0)
    ap.add_argument("--max-hold-s", type=float, default=30.0)
    ap.add_argument("--latency-ms", type=float, default=1.0)
    ap.add_argument("--quantities", type=int, nargs="+", default=[100, 500, 1000, 5000],
                    help="Requested order sizes; touch requires top-level depth, L2 scenarios sweep up to --levels")
    ap.add_argument("--fee-per-share", type=float, default=0.0)
    ap.add_argument("--levels", type=int, default=10)
    ap.add_argument("--workers", type=int, default=1,
                    help="Number of independent daily sessions to process concurrently")
    a = ap.parse_args()
    days = pd.date_range(pd.to_datetime(a.start_day), pd.to_datetime(a.end_day), freq="B")
    results: dict[str, list[pd.DataFrame]] = {"touch": [], "l2_partial": [], "l2_complete": []}
    diagnostics = []
    tasks = []
    for d in days:
        day = d.strftime("%Y%m%d")
        path = a.data_dir / f"xnas-itch-{day}.mbo.dbn.zst.TSLA-TSLL-TSLR-TSLG.npz"
        if not path.exists():
            print(f"Skipping missing cache {path}")
            continue
        tasks.append((day, str(path), [a.entry_bps], a.exit_bps,
                      a.max_hold_s, a.latency_ms, a.quantities,
                      a.fee_per_share, a.levels))
    for day, day_results, day_diagnostics in run_sessions(_process_day, tasks, a.workers):
        for scenario in results:
            results[scenario].extend(day_results[scenario])
        diagnostics.extend(day_diagnostics)
        print(f"Processed {day}", flush=True)
    a.out.mkdir(parents=True, exist_ok=True)
    all_rows = []
    summaries = []
    for scenario, frames in results.items():
        trades = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        if not trades.empty:
            trades.to_csv(a.out / f"{scenario}_trades.csv", index=False)
            for (symbol, quantity), part in trades.groupby(["symbol", "requested_quantity"]):
                summaries.append({"scenario": scenario, "symbol": symbol,
                                  "requested_quantity": int(quantity), **summarize(part)})
            all_rows.append(trades)
    pd.DataFrame(summaries).to_csv(a.out / "summary.csv", index=False)
    pd.DataFrame(diagnostics).to_csv(a.out / "fill_diagnostics.csv", index=False)
    report = {"parameters": vars(a) | {"data_dir": str(a.data_dir), "out": str(a.out)},
              "summary": summaries, "fill_diagnostics": diagnostics,
              "limitations": ["L2 scenarios freeze the touch-model signal and exit-condition sequence; rejected fills do not regenerate later signals.",
                  "Partial model accepts a partial entry and requires the resulting position to be fully exit-able within the displayed depth horizon; complete model also requires the requested entry quantity.",
                  "Market orders sweep displayed levels up to the configured depth horizon; hidden liquidity, impact beyond displayed depth, and queue position are not modeled.",
                  "Single Nasdaq venue and limited sample; results are execution sensitivities, not evidence of live profitability."]}
    (a.out / "summary.json").write_text(json.dumps(report, indent=2, default=str))
    print(pd.DataFrame(summaries).to_string(index=False))
    print(f"Saved execution sensitivity to {a.out}")


if __name__ == "__main__":
    main()
