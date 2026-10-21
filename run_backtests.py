#!/usr/bin/env python3
"""Run selected strategy backtests over a date range with session workers."""
from __future__ import annotations

import argparse
from collections import defaultdict
import subprocess
import sys
from pathlib import Path

import pandas as pd

RUNNERS = {
    "residual": "backtest_batch.py",
    "signals": "backtest_signal_batch.py",
    "imbalance": "backtest_imbalance_batch.py",
    "queue-maker": "backtest_queue_maker.py",
    "execution-sensitivity": "backtest_execution_sensitivity.py",
}


def write_period_summary(root: Path) -> None:
    """Aggregate generated trade logs by calendar month and full sample."""
    accum = defaultdict(lambda: {"trades": 0, "wins": 0, "net_pnl": 0.0,
                                 "mid_pnl": 0.0, "spread_cost": 0.0, "fees": 0.0})
    candidates = []
    for path in root.rglob("*.csv"):
        if path.name == "all_trades.csv" or path.name == "trades.csv" or path.name.endswith("_trades.csv"):
            candidates.append(path)
    for path in candidates:
        rel = path.relative_to(root)
        if path.name == "all_trades.csv":
            config = "/".join(rel.parts[:-1])
        elif path.name == "trades.csv":
            config = "/".join(rel.parts[:-1])
        else:
            config = "/".join((*rel.parts[:-1], path.stem.removesuffix("_trades")))
        try:
            chunks = pd.read_csv(path, chunksize=150_000)
            for chunk in chunks:
                if chunk.empty or "day" not in chunk or "net_pnl" not in chunk or "symbol" not in chunk:
                    continue
                dates = pd.to_datetime(chunk["day"].astype(str), errors="coerce")
                chunk = chunk.loc[dates.notna()].copy()
                chunk["month"] = dates[dates.notna()].dt.strftime("%Y-%m").to_numpy()
                chunk["_mid"] = (chunk["mid_pnl_before_spread"] if "mid_pnl_before_spread" in chunk
                                 else chunk["pair_pnl_marked_to_mid"] if "pair_pnl_marked_to_mid" in chunk
                                 else 0.0)
                chunk["_spread"] = (chunk["entry_spread_cost"].fillna(0) + chunk["exit_spread_cost"].fillna(0)
                                    if "entry_spread_cost" in chunk and "exit_spread_cost" in chunk else 0.0)
                chunk["_fees"] = chunk["fees"].fillna(0) if "fees" in chunk else 0.0
                group_cols = ["month", "symbol"]
                if "requested_quantity" in chunk:
                    group_cols.append("requested_quantity")
                for group_key, group in chunk.groupby(group_cols, sort=False):
                    month, symbol = group_key[:2]
                    group_config = config
                    if len(group_key) > 2:
                        group_config += f"/q{int(group_key[2])}"
                    key = (group_config, str(month), str(symbol))
                    agg = accum[key]
                    pnl = pd.to_numeric(group["net_pnl"], errors="coerce").fillna(0)
                    agg["trades"] += len(group)
                    agg["wins"] += int((pnl > 0).sum())
                    agg["net_pnl"] += float(pnl.sum())
                    agg["mid_pnl"] += float(pd.to_numeric(group["_mid"], errors="coerce").fillna(0).sum())
                    agg["spread_cost"] += float(pd.to_numeric(group["_spread"], errors="coerce").fillna(0).sum())
                    agg["fees"] += float(pd.to_numeric(group["_fees"], errors="coerce").fillna(0).sum())
        except (pd.errors.EmptyDataError, OSError):
            continue

    monthly_rows = []
    for (config, month, symbol), values in accum.items():
        monthly_rows.append({"configuration": config, "period": month, "symbol": symbol,
                             **values, "win_rate": values["wins"] / values["trades"] if values["trades"] else 0.0})
    total_accum = defaultdict(lambda: {"trades": 0, "wins": 0, "net_pnl": 0.0,
                                       "mid_pnl": 0.0, "spread_cost": 0.0, "fees": 0.0})
    for row in monthly_rows:
        key = (row["configuration"], row["symbol"])
        target = total_accum[key]
        for field in target:
            target[field] += row[field]
    for (config, symbol), values in total_accum.items():
        monthly_rows.append({"configuration": config, "period": "ALL", "symbol": symbol,
                             **values, "win_rate": values["wins"] / values["trades"] if values["trades"] else 0.0})
    pd.DataFrame(monthly_rows).sort_values(["configuration", "period", "symbol"]).to_csv(
        root / "period_summary.csv", index=False)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start-day", required=True, help="First session YYYYMMDD, inclusive")
    ap.add_argument("--end-day", required=True, help="Last session YYYYMMDD, inclusive")
    ap.add_argument("--max-sessions", type=int, default=None,
                    help="Pilot on the first N cached sessions in the requested date range")
    ap.add_argument("--workers", type=int, default=2,
                    help="Concurrent daily child processes per runner")
    ap.add_argument("--strategies", nargs="+", choices=RUNNERS,
                    default=list(RUNNERS), help="Backtest runners to invoke")
    ap.add_argument("--data-dir", type=Path, default=Path("XNAS_MBO"))
    ap.add_argument("--out-dir", type=Path, default=Path("backtests"))
    ap.add_argument("--skip-date", action="append", default=[],
                    help="Holiday/non-session YYYYMMDD to exclude (repeatable)")
    a = ap.parse_args()
    if a.workers < 1:
        ap.error("--workers must be at least 1")
    if a.end_day < a.start_day:
        ap.error("--end-day must be on or after --start-day")
    if a.max_sessions is not None and a.max_sessions < 1:
        ap.error("--max-sessions must be at least 1")

    args_by_strategy = {
        "residual": ["--entry-bps", "15", "30", "--exit-bps", "5", "--max-hold-s", "30",
                     "--latency-ms", "1", "--quantity", "100"],
        "signals": ["--lookback-ms", "100", "1000", "--threshold-bps", "5", "15",
                    "--hold-ms", "1000", "10000", "--mode", "momentum", "reversal",
                    "--latency-ms", "1", "--quantity", "100"],
        "imbalance": ["--imbalance", "0.5", "0.8", "--hold-ms", "1000", "10000",
                      "--latency-ms", "1", "--quantity", "100"],
        "queue-maker": ["--quantity", "100", "--imbalance", "0.7", "--quote-wait-ms", "1000",
                        "--hold-ms", "1000", "--latency-ms", "1", "--edge-buffer-bps", "2"],
        "execution-sensitivity": ["--entry-bps", "30", "--exit-bps", "5", "--max-hold-s", "30",
                                  "--latency-ms", "1", "--quantities", "100", "500", "1000", "5000",
                                  "--levels", "10"],
    }
    skip_dates = {"20260703", "20260907", *a.skip_date}
    run_start, run_end = a.start_day, a.end_day
    if a.max_sessions is not None:
        available = []
        for date in pd.date_range(pd.to_datetime(a.start_day), pd.to_datetime(a.end_day), freq="B"):
            day = date.strftime("%Y%m%d")
            if day in skip_dates:
                continue
            cache = a.data_dir / f"xnas-itch-{day}.mbo.dbn.zst.TSLA-TSLL-TSLR-TSLG.npz"
            if cache.exists():
                available.append(day)
        if len(available) < a.max_sessions:
            ap.error(f"requested {a.max_sessions} pilot sessions but found only {len(available)} cached sessions")
        selected = available[:a.max_sessions]
        run_end = selected[-1]
        # Date-range runners cannot accept a discontiguous session list. Exclude
        # missing cache dates inside this pilot window explicitly instead.
        selected_set = set(selected)
        for date in pd.date_range(pd.to_datetime(run_start), pd.to_datetime(run_end), freq="B"):
            day = date.strftime("%Y%m%d")
            cache = a.data_dir / f"xnas-itch-{day}.mbo.dbn.zst.TSLA-TSLL-TSLR-TSLG.npz"
            if day not in skip_dates and day not in selected_set and not cache.exists():
                skip_dates.add(day)
        print(f"Pilot sessions ({len(selected)}): {selected[0]} through {selected[-1]}", flush=True)
    skip_dates = sorted(skip_dates)
    for strategy in a.strategies:
        out = a.out_dir / f"{run_start}-{run_end}" / strategy
        command = [sys.executable, RUNNERS[strategy], "--start-day", run_start,
                   "--end-day", run_end, "--workers", str(a.workers),
                   "--data-dir", str(a.data_dir), "--out", str(out)]
        if strategy != "execution-sensitivity":
            for day in skip_dates:
                command.extend(["--skip-date", day])
        command.extend(args_by_strategy[strategy])
        print(f"Running {strategy} with {a.workers} session workers", flush=True)
        subprocess.run(command, check=True)
    write_period_summary(a.out_dir / f"{run_start}-{run_end}")
    print(f"All selected backtests saved under {a.out_dir / f'{run_start}-{run_end}'}")


if __name__ == "__main__":
    main()
