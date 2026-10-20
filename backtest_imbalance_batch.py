#!/usr/bin/env python3
"""Sweep long-only ETF top-of-book imbalance signals over cached sessions."""
from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path

import numpy as np
import pandas as pd

from utils import leadlag
from backtest import run_imbalance_backtest, summarize
from backtest_batch import load_day
from utils.parallel import run_sessions


def _process_day(task: tuple) -> tuple[str, dict[tuple, dict[str, object]]]:
    (day_str, cache_str, configs, latency_ms, quantity, fee_per_share) = task
    quotes, _ = load_day(Path(cache_str))
    day_result = {}
    for cfg in configs:
        threshold, hold_ms = cfg
        trades_by_symbol, daily = [], []
        for symbol in leadlag.FUNDS:
            trades = run_imbalance_backtest(
                quotes[symbol], symbol=symbol, threshold=threshold,
                hold_ns=int(hold_ms * 1e6), latency_ns=int(latency_ms * 1e6),
                quantity=quantity, fee_per_share=fee_per_share)
            if trades.empty:
                trades = pd.DataFrame(columns=["symbol", "net_pnl", "mid_pnl_before_spread",
                    "entry_spread_cost", "exit_spread_cost", "fees", "entry_notional",
                    "direction", "forced_eod_exit"])
            trades.insert(0, "day", day_str)
            trades_by_symbol.append(trades)
            daily.append({"day": day_str, "symbol": symbol, **summarize(trades)})
        day_result[cfg] = {"trades": trades_by_symbol, "daily": daily}
    return day_str, day_result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start-day", default="20260901")
    ap.add_argument("--end-day", default="20261009")
    ap.add_argument("--skip-date", action="append", default=["20260907"])
    ap.add_argument("--data-dir", type=Path, default=Path("XNAS_MBO"))
    ap.add_argument("--out", type=Path, default=Path("backtests/imbalance_sweep"))
    ap.add_argument("--imbalance", type=float, nargs="+", default=[0.5, 0.8])
    ap.add_argument("--hold-ms", type=float, nargs="+", default=[1000.0, 10000.0])
    ap.add_argument("--latency-ms", type=float, default=1.0)
    ap.add_argument("--quantity", type=int, default=100)
    ap.add_argument("--fee-per-share", type=float, default=0.0)
    ap.add_argument("--workers", type=int, default=1,
                    help="Number of independent daily sessions to process concurrently")
    a = ap.parse_args()

    days = [d for d in pd.date_range(pd.to_datetime(a.start_day), pd.to_datetime(a.end_day), freq="B")
            if d.strftime("%Y%m%d") not in set(a.skip_date)]
    configs = list(itertools.product(a.imbalance, a.hold_ms))
    collected = {cfg: [] for cfg in configs}
    daily_rows = {cfg: [] for cfg in configs}

    tasks = []
    for requested in days:
        day_str = requested.strftime("%Y%m%d")
        cache = a.data_dir / f"xnas-itch-{day_str}.mbo.dbn.zst.TSLA-TSLL-TSLR-TSLG.npz"
        if not cache.exists():
            raise SystemExit(f"Missing cached trading day: {cache}")
        tasks.append((day_str, str(cache), configs, a.latency_ms,
                      a.quantity, a.fee_per_share))
    for day_number, (day_str, day_result) in enumerate(
            run_sessions(_process_day, tasks, a.workers), start=1):
        for cfg in configs:
            collected[cfg].extend(day_result[cfg]["trades"])
            daily_rows[cfg].extend(day_result[cfg]["daily"])
        print(f"Processed {day_str} ({day_number}/{len(days)})", flush=True)

    a.out.mkdir(parents=True, exist_ok=True)
    rows = []
    for cfg in configs:
        threshold, hold_ms = cfg
        tag = f"imb{threshold:g}_hold{hold_ms:g}ms"
        out_dir = a.out / tag
        out_dir.mkdir(parents=True, exist_ok=True)
        combined = pd.concat(collected[cfg], ignore_index=True)
        daily = daily_rows[cfg]
        combined.to_csv(out_dir / "all_trades.csv", index=False)
        pd.DataFrame(daily).to_csv(out_dir / "daily_summary.csv", index=False)
        by_symbol = [{"symbol": symbol, **summarize(combined[combined.symbol == symbol])}
                     for symbol in leadlag.FUNDS]
        pd.DataFrame(by_symbol).to_csv(out_dir / "symbol_summary.csv", index=False)
        aggregate = summarize(combined)
        report = {
            "parameters": {"imbalance_threshold": threshold, "hold_ms": hold_ms,
                "start_day": a.start_day, "end_day": a.end_day,
                "excluded_dates": a.skip_date, "latency_ms": a.latency_ms,
                "quantity": a.quantity, "fee_per_share": a.fee_per_share,
                "workers": a.workers,
                "signal": "(best bid size - best ask size) / total best-level size crosses upward through threshold",
                "fill_rule": "first ETF BBO strictly after signal/exit target plus latency; buy at ask, sell at bid",
                "depth_rule": "all-or-none when displayed top-level size covers quantity"},
            "aggregate": aggregate, "by_symbol": by_symbol, "daily": daily,
            "limitations": [f"Uses {len(days)} sessions; still a limited sample.",
                "Uses the Nasdaq displayed book, not consolidated NBBO.",
                "Top-level imbalance can change rapidly and is not proof of executable predictive edge.",
                "No deeper-book impact, queue priority, hidden liquidity, commissions unless configured, or fund expenses.",
                "Threshold sweep is exploratory and subject to selection bias."]}
        (out_dir / "summary.json").write_text(json.dumps(report, indent=2))
        rows.append({"imbalance_threshold": threshold, "hold_ms": hold_ms, **aggregate})
    pd.DataFrame(rows).sort_values("net_pnl_dollars", ascending=False).to_csv(
        a.out / "config_summary.csv", index=False)
    print(f"Saved imbalance sweep to {a.out.resolve()}")


if __name__ == "__main__":
    main()
