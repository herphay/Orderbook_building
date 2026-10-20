#!/usr/bin/env python3
"""Run the opening-anchored mispricing strategy over cached trading days."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from utils import leadlag
from utils.parallel import run_sessions
from backtest import Quotes, rth_quotes, run_mispricing_backtest, summarize


def load_day(cache: Path) -> tuple[dict[str, Quotes], pd.Timestamp]:
    z = np.load(cache, allow_pickle=False)
    day = pd.Timestamp(str(z["day"]))
    out = {}
    for symbol in ["TSLA", *leadlag.FUNDS]:
        bbo = leadlag.build_bbo(z[symbol]).sort_values("ts_event", kind="stable")
        bbo = bbo.drop_duplicates("ts_event", keep="last")
        t = bbo.ts_event.to_numpy(np.int64)
        bid, ask = bbo.bid.to_numpy(float), bbo.ask.to_numpy(float)
        ok = np.isfinite(bid) & np.isfinite(ask) & (bid > 0) & (ask > bid)
        q = Quotes(t[ok], bid[ok], ask[ok], bbo.bid_sz.to_numpy(np.int64)[ok],
                   bbo.ask_sz.to_numpy(np.int64)[ok])
        out[symbol] = rth_quotes(q, day)
    return out, day


def _process_day(task: tuple) -> tuple[str, dict[float, dict[str, object]]]:
    (day_str, cache_str, entries, exit_bps, max_hold_s, latency_ms,
     quantity, fee_per_share, long_only, out_str) = task
    cache, out = Path(cache_str), Path(out_str)
    quotes, _ = load_day(cache)
    day_result: dict[float, dict[str, object]] = {}
    for entry_bps in entries:
        entry_tag = f"entry_{entry_bps:g}bp"
        trades_for_entry, daily = [], []
        for symbol, fund in leadlag.FUNDS.items():
            trades = run_mispricing_backtest(
                quotes["TSLA"], quotes[symbol], symbol=symbol, leverage=fund["L"],
                entry_bps=entry_bps, exit_bps=exit_bps,
                max_hold_ns=int(max_hold_s * 1e9), latency_ns=int(latency_ms * 1e6),
                quantity=quantity, fee_per_share=fee_per_share, long_only=long_only)
            if trades.empty:
                trades = pd.DataFrame(columns=["symbol", "net_pnl", "mid_pnl_before_spread",
                    "entry_spread_cost", "exit_spread_cost", "fees", "entry_notional",
                    "direction", "forced_eod_exit"])
            trades.insert(0, "day", day_str)
            trade_dir = out / entry_tag / "trades"
            trade_dir.mkdir(parents=True, exist_ok=True)
            trades.to_csv(trade_dir / f"{day_str}_{symbol}.csv", index=False)
            trades_for_entry.append(trades)
            daily.append({"day": day_str, "symbol": symbol, **summarize(trades)})
        day_result[entry_bps] = {"trades": trades_for_entry, "daily": daily}
    return day_str, day_result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start-day", default="20261005", help="First YYYYMMDD, inclusive")
    ap.add_argument("--end-day", default="20261009", help="Last YYYYMMDD, inclusive")
    ap.add_argument("--skip-date", action="append", default=["20260907"],
                    help="Non-session date to exclude (repeatable; default excludes Labor Day)")
    ap.add_argument("--data-dir", type=Path, default=Path("XNAS_MBO"))
    ap.add_argument("--out", type=Path, default=Path("backtests/20261005-20261009"))
    ap.add_argument("--entry-bps", type=float, nargs="+", default=[15.0, 30.0])
    ap.add_argument("--exit-bps", type=float, default=5.0)
    ap.add_argument("--max-hold-s", type=float, default=30.0)
    ap.add_argument("--long-only", action="store_true",
                    help="Trade ETF discounts only; do not open short positions")
    ap.add_argument("--latency-ms", type=float, default=1.0)
    ap.add_argument("--quantity", type=int, default=100)
    ap.add_argument("--fee-per-share", type=float, default=0.0)
    ap.add_argument("--workers", type=int, default=1,
                    help="Number of independent daily sessions to process concurrently")
    a = ap.parse_args()
    days = [d for d in pd.date_range(pd.to_datetime(a.start_day), pd.to_datetime(a.end_day), freq="B")
            if d.strftime("%Y%m%d") not in set(a.skip_date)]
    accumulated = {entry: {"trades": [], "daily": []} for entry in a.entry_bps}
    tasks = []
    for day_requested in days:
        day_str = day_requested.strftime("%Y%m%d")
        cache = a.data_dir / f"xnas-itch-{day_str}.mbo.dbn.zst.TSLA-TSLL-TSLR-TSLG.npz"
        if not cache.exists():
            raise SystemExit(f"Missing cached trading day: {cache}")
        tasks.append((day_str, str(cache), list(a.entry_bps), a.exit_bps,
                      a.max_hold_s, a.latency_ms, a.quantity, a.fee_per_share,
                      a.long_only, str(a.out)))
    for day_str, day_result in run_sessions(_process_day, tasks, a.workers):
        for entry_bps in a.entry_bps:
            accumulated[entry_bps]["trades"].extend(day_result[entry_bps]["trades"])
            accumulated[entry_bps]["daily"].extend(day_result[entry_bps]["daily"])
        print(f"Processed {day_str}", flush=True)

    for entry_bps in a.entry_bps:
        entry_tag = f"entry_{entry_bps:g}bp"
        threshold_dir = a.out / entry_tag
        daily = accumulated[entry_bps]["daily"]
        combined = pd.concat(accumulated[entry_bps]["trades"], ignore_index=True)
        combined.to_csv(threshold_dir / "all_trades.csv", index=False)
        pd.DataFrame(daily).to_csv(threshold_dir / "daily_summary.csv", index=False)
        by_symbol = []
        for symbol in leadlag.FUNDS:
            symbol_trades = combined[combined.symbol == symbol]
            by_symbol.append({"symbol": symbol, **summarize(symbol_trades)})
        pd.DataFrame(by_symbol).to_csv(threshold_dir / "symbol_summary.csv", index=False)
        report = {
            "parameters": {"start_day": a.start_day, "end_day": a.end_day,
                "excluded_dates": a.skip_date,
                "entry_bps": entry_bps, "exit_bps": a.exit_bps,
                "max_hold_s": a.max_hold_s, "latency_ms": a.latency_ms,
                "quantity": a.quantity, "fee_per_share": a.fee_per_share,
                "workers": a.workers,
                "long_only": a.long_only,
                "fair_value": "first jointly observable regular-session quotes; ETF target return = leverage x TSLA return",
                "fill_rule": "first ETF BBO strictly after signal/exit target plus latency; cross ask/bid",
                "depth_rule": "all-or-none when displayed top-level size covers quantity"},
            "aggregate_all_symbols": summarize(combined),
            "aggregate_by_symbol": by_symbol, "daily": daily,
            "limitations": [f"Uses {len(days)} sessions; still a limited sample.",
                "Uses the Nasdaq displayed book, not consolidated NBBO.",
                "Opening-anchored tracking estimate is not official NAV or a measured ETF premium/discount.",
                "No deeper-book impact, queue priority, hidden liquidity, short borrow, or fund expenses.",
                "Threshold comparisons are exploratory, not statistical confirmation."]}
        (threshold_dir / "summary.json").write_text(json.dumps(report, indent=2))
        print(f"{entry_tag}: {json.dumps(by_symbol)}")
    print(f"Saved batch results to {a.out.resolve()}")


if __name__ == "__main__":
    main()
