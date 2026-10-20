#!/usr/bin/env python3
"""Condition queue-maker fill markouts on pre-arrival market conditions.

This is a descriptive analysis of modeled fills. Conditions are sampled no later
than the simulated ETF order arrival; the analysis does not identify traders.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from backtest_queue_maker import get_quotes


HORIZONS = (100, 500, 1000, 5000)
FEATURES = (
    "etf_spread_bps_q",
    "etf_touch_depth_q",
    "queue_ahead_q",
    "etf_quote_age_q",
    "tsla_pressure_aligned_bin",
    "etf_pressure_aligned_bin",
    "time_bucket",
)


def direction_sign(value: str) -> int:
    return 1 if str(value).startswith("long_etf") else -1


def latest_at_or_before(q, ts: int) -> int:
    return int(np.searchsorted(q.t, ts, side="right") - 1)


def add_symbol_quartile(frame: pd.DataFrame, source: str, dest: str) -> None:
    frame[dest] = ""
    for symbol, idx in frame.groupby("symbol").groups.items():
        values = frame.loc[idx, source]
        # Rank first so ties do not collapse bins; quartiles are relative within
        # ETF because their price and displayed-size scales differ.
        pct = values.rank(method="average", pct=True)
        frame.loc[idx, dest] = pd.cut(
            pct, [0, .25, .5, .75, 1.000001],
            labels=["Q1 low", "Q2", "Q3", "Q4 high"], include_lowest=True,
        ).astype(str).to_numpy()


def mean_day_bootstrap_difference(frame: pd.DataFrame, feature: str,
                                  low: str, high: str, value: str,
                                  reps: int, rng: np.random.Generator) -> tuple[float, float, float]:
    low_df = frame[frame[feature].astype(str) == low]
    high_df = frame[frame[feature].astype(str) == high]
    point = high_df[value].mean() - low_df[value].mean()
    days = np.array(sorted(set(low_df.day.astype(str)) | set(high_df.day.astype(str))))
    if not len(days):
        return np.nan, np.nan, np.nan
    low_by_day = {d: low_df.loc[low_df.day.astype(str) == d, value].to_numpy(float) for d in days}
    high_by_day = {d: high_df.loc[high_df.day.astype(str) == d, value].to_numpy(float) for d in days}
    draws = np.full(reps, np.nan)
    for b in range(reps):
        sampled = rng.choice(days, size=len(days), replace=True)
        lo_parts = [low_by_day[d] for d in sampled if len(low_by_day[d])]
        hi_parts = [high_by_day[d] for d in sampled if len(high_by_day[d])]
        if lo_parts and hi_parts:
            draws[b] = np.concatenate(hi_parts).mean() - np.concatenate(lo_parts).mean()
    draws = draws[np.isfinite(draws)]
    if not len(draws):
        return point, np.nan, np.nan
    lo_ci, hi_ci = np.quantile(draws, [.025, .975])
    return float(point), float(lo_ci), float(hi_ci)


def markdown_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "(no rows)"
    vals = [[str(v) for v in row] for row in df.itertuples(index=False, name=None)]
    headers = [str(c) for c in df.columns]
    widths = [max(len(headers[i]), *(len(row[i]) for row in vals)) for i in range(len(headers))]
    head = "| " + " | ".join(headers[i].ljust(widths[i]) for i in range(len(headers))) + " |"
    rule = "| " + " | ".join("-" * w for w in widths) + " |"
    body = ["| " + " | ".join(row[i].ljust(widths[i]) for i in range(len(headers))) + " |" for row in vals]
    return "\n".join([head, rule, *body])


def run(trades_path: Path, markouts_path: Path, data_dir: Path,
        out: Path, entry_delay_us: float, bootstrap_reps: int, seed: int) -> None:
    trades = pd.read_csv(trades_path)
    markouts = pd.read_csv(markouts_path)
    keys = ["day", "symbol", "direction", "etf_fill_time_ns"]
    mark_key = markouts.rename(columns={"fill_time_ns": "etf_fill_time_ns"})
    data = trades.merge(mark_key, on=keys, how="inner", validate="one_to_one")
    features: list[dict] = []
    delay_ns = int(entry_delay_us * 1_000)

    for day, daily in data.groupby("day", sort=True):
        cache = data_dir / f"xnas-itch-{int(day)}.mbo.dbn.zst.TSLA-TSLL-TSLR-TSLG.npz"
        if not cache.exists():
            raise FileNotFoundError(cache)
        z = np.load(cache, allow_pickle=False)
        date = pd.Timestamp(str(z["day"]))
        tsla = get_quotes(z["TSLA"], date)
        etfs = {sym: get_quotes(z[sym], date) for sym in daily.symbol.unique()}
        for trade in daily.itertuples(index=False):
            arrival = int(trade.signal_time_ns) + delay_ns
            ei = latest_at_or_before(etfs[trade.symbol], arrival)
            si = latest_at_or_before(tsla, int(trade.signal_time_ns))
            if ei < 0 or si < 0:
                continue
            eq, sq = etfs[trade.symbol], tsla
            sign = direction_sign(trade.direction)
            emid = (eq.bid[ei] + eq.ask[ei]) / 2
            smid = (sq.bid[si] + sq.ask[si]) / 2
            etf_imb = (eq.bid_sz[ei] - eq.ask_sz[ei]) / max(1, eq.bid_sz[ei] + eq.ask_sz[ei])
            tsla_imb = (sq.bid_sz[si] - sq.ask_sz[si]) / max(1, sq.bid_sz[si] + sq.ask_sz[si])
            local = pd.Timestamp(int(arrival), unit="ns", tz="UTC").tz_convert("America/New_York")
            minute = local.hour * 60 + local.minute
            if minute < 10 * 60:
                tod = "09:30-10:00"
            elif minute < 12 * 60:
                tod = "10:00-12:00"
            elif minute < 14 * 60:
                tod = "12:00-14:00"
            else:
                tod = "14:00-16:00"
            features.append({
                "day": str(int(day)), "symbol": trade.symbol,
                "direction": trade.direction, "direction_sign": sign,
                "signal_time_ns": int(trade.signal_time_ns),
                "order_arrival_ns_assumed": arrival,
                "fill_time_ns": int(trade.etf_fill_time_ns),
                "time_to_fill_ms": (int(trade.etf_fill_time_ns) - arrival) / 1e6,
                "signal_imbalance_logged": float(trade.signal_imbalance),
                "tsla_pressure_at_signal": float(tsla_imb),
                "tsla_pressure_aligned": float(sign * tsla_imb),
                "tsla_spread_bps_at_signal": (sq.ask[si] - sq.bid[si]) / smid * 1e4,
                "tsla_quote_age_us_at_signal": (int(trade.signal_time_ns) - int(sq.t[si])) / 1e3,
                "etf_spread_bps_at_arrival": (eq.ask[ei] - eq.bid[ei]) / emid * 1e4,
                "etf_bid_size_at_arrival": int(eq.bid_sz[ei]),
                "etf_ask_size_at_arrival": int(eq.ask_sz[ei]),
                "etf_touch_depth_at_arrival": int(eq.bid_sz[ei] if sign > 0 else eq.ask_sz[ei]),
                "etf_pressure_aligned": float(sign * etf_imb),
                "etf_quote_age_us_at_arrival": (arrival - int(eq.t[ei])) / 1e3,
                "queue_ahead_shares": int(trade.queue_ahead_shares),
                "time_bucket": tod,
            })
        print(f"Joined features for {int(day)}", flush=True)

    feat = pd.DataFrame(features)
    if feat.empty:
        raise RuntimeError("No fills had valid pre-arrival quotes")
    data["day"] = data.day.astype(str)
    data_for_join = data.rename(columns={"etf_fill_time_ns": "fill_time_ns"})
    merged = feat.merge(
        data_for_join[["day", "symbol", "direction", "fill_time_ns"]
                       + [f"markout_{h}ms" for h in HORIZONS] + ["net_pnl"]],
        on=["day", "symbol", "direction", "fill_time_ns"],
        how="inner", validate="one_to_one")
    # The signal's logged imbalance is already the signal input; use the
    # independently reconstructed TSLA top-of-book pressure as a cross-check.
    merged["tsla_pressure_aligned_bin"] = pd.cut(
        merged.tsla_pressure_aligned,
        [-1.000001, 0, .5, .8, 1.000001],
        labels=["opposed", "weak aligned", "moderate aligned", "strong aligned"],
        include_lowest=True,
    ).astype(str)
    merged["etf_pressure_aligned_bin"] = pd.cut(
        merged.etf_pressure_aligned,
        [-1.000001, 0, .5, .8, 1.000001],
        labels=["opposed", "weak aligned", "moderate aligned", "strong aligned"],
        include_lowest=True,
    ).astype(str)
    add_symbol_quartile(merged, "etf_spread_bps_at_arrival", "etf_spread_bps_q")
    add_symbol_quartile(merged, "etf_touch_depth_at_arrival", "etf_touch_depth_q")
    add_symbol_quartile(merged, "queue_ahead_shares", "queue_ahead_q")
    add_symbol_quartile(merged, "etf_quote_age_us_at_arrival", "etf_quote_age_q")

    out.mkdir(parents=True, exist_ok=True)
    merged.to_csv(out / "fills_with_prearrival_conditions.csv", index=False)
    cutpoints = []
    for symbol, g in merged.groupby("symbol"):
        for feature, source in (("etf_spread_bps_q", "etf_spread_bps_at_arrival"),
                                ("etf_touch_depth_q", "etf_touch_depth_at_arrival"),
                                ("queue_ahead_q", "queue_ahead_shares"),
                                ("etf_quote_age_q", "etf_quote_age_us_at_arrival")):
            q = g[source].quantile([.25, .5, .75])
            cutpoints.append({"symbol": symbol, "feature": feature,
                              "q25": q.loc[.25], "median": q.loc[.5], "q75": q.loc[.75]})
    pd.DataFrame(cutpoints).to_csv(out / "within_symbol_quartile_cutpoints.csv", index=False)
    summaries = []
    for feature in FEATURES:
        for (symbol, bucket), g in merged.groupby(["symbol", feature], observed=True, sort=False):
            for horizon in HORIZONS:
                x = g[f"markout_{horizon}ms"].dropna()
                summaries.append({
                    "feature": feature, "bucket": str(bucket), "symbol": symbol,
                    "horizon_ms": horizon,
                    "fills": len(x), "sessions": g.loc[x.index, "day"].nunique(),
                    "mean_markout_dollars": x.mean(), "median_markout_dollars": x.median(),
                    "win_rate": (x > 0).mean(), "mean_net_exit_pnl_dollars": g.loc[x.index, "net_pnl"].mean(),
                })
    by_symbol = pd.DataFrame(summaries)
    by_symbol.to_csv(out / "condition_markouts_by_symbol.csv", index=False)
    pooled = []
    for feature in FEATURES:
        for bucket, g in merged.groupby(feature, observed=True, sort=False):
            for horizon in HORIZONS:
                x = g[f"markout_{horizon}ms"].dropna()
                pooled.append({
                    "feature": feature, "bucket": str(bucket), "fills": len(x),
                    "horizon_ms": horizon,
                    "sessions": g.loc[x.index, "day"].nunique(),
                    "mean_markout_dollars": x.mean(), "median_markout_dollars": x.median(),
                    "win_rate": (x > 0).mean(), "mean_net_exit_pnl_dollars": g.loc[x.index, "net_pnl"].mean(),
                })
    pooled_df = pd.DataFrame(pooled)
    pooled_df.to_csv(out / "condition_markouts_pooled.csv", index=False)

    # Day-cluster bootstrap for the widest observable contrast per condition.
    rng = np.random.default_rng(seed)
    endpoint = {
        "etf_spread_bps_q": ("Q1 low", "Q4 high"),
        "etf_touch_depth_q": ("Q1 low", "Q4 high"),
        "queue_ahead_q": ("Q1 low", "Q4 high"),
        "etf_quote_age_q": ("Q1 low", "Q4 high"),
        # The entry rule itself requires a strong TSLA imbalance, so compare
        # moderate with strong aligned pressure instead of an absent opposed bin.
        "tsla_pressure_aligned_bin": ("moderate aligned", "strong aligned"),
        "etf_pressure_aligned_bin": ("opposed", "strong aligned"),
        "time_bucket": ("14:00-16:00", "09:30-10:00"),
    }
    contrasts = []
    for feature, (low, high) in endpoint.items():
        for horizon in HORIZONS:
            col = f"markout_{horizon}ms"
            sub = merged.dropna(subset=[col]).copy()
            sub[feature] = sub[feature].astype(str)
            nlow = int((sub[feature] == low).sum())
            nhigh = int((sub[feature] == high).sum())
            diff, ci_low, ci_high = mean_day_bootstrap_difference(
                sub, feature, low, high, col, bootstrap_reps, rng)
            contrasts.append({"feature": feature, "bucket_a": low,
                              "bucket_b": high, "horizon_ms": horizon,
                              "bucket_a_fills": nlow, "bucket_b_fills": nhigh,
                              "bucket_b_minus_a_mean_markout_dollars": diff,
                              "day_bootstrap_95ci_low": ci_low,
                              "day_bootstrap_95ci_high": ci_high})
    contrast_df = pd.DataFrame(contrasts)
    contrast_df.to_csv(out / "day_cluster_bootstrap_contrasts.csv", index=False)

    day_summary = merged.groupby("day", as_index=False).agg(fills=("net_pnl", "size"),
                                                             mean_100ms=("markout_100ms", "mean"),
                                                             mean_500ms=("markout_500ms", "mean"),
                                                             net_exit_pnl=("net_pnl", "sum"))
    day_summary.to_csv(out / "daily_summary.csv", index=False)
    top_view = (pooled_df[pooled_df.horizon_ms.isin([100, 500])]
                .sort_values(["feature", "horizon_ms", "bucket"]))
    contrast_view = contrast_df[contrast_df.horizon_ms.isin([100, 500])].copy()
    lines = [
        "# Queue-maker fill toxicity by pre-arrival conditions", "",
        f"Source fills: `{trades_path}` ({len(trades):,} fills). Markouts: `{markouts_path}`.", "",
        f"Conditions use ETF BBO immediately before simulated order arrival, assuming {entry_delay_us:g} µs signal-to-exchange delay; TSLA pressure and quote state are sampled at signal time. Queue ahead is the replay's logged order-arrival estimate.", "",
        "## Main sample and interpretation", "",
        f"The join retained {len(merged):,} fills across {merged.day.nunique()} sessions. This is a filled-order toxicity study: it cannot estimate how a filter changes fill probability because the saved baseline run does not include the corresponding full candidate funnel in this directory. Markouts are model-generated pair mark-to-mid values after fills, not realized live returns.", "",
        "Spread, touch depth, queue ahead, and quote age are split into within-ETF quartiles so differently priced ETFs and share scales are not directly mixed. Pressure buckets are signed to the ETF position: `opposed`, `weak aligned`, `moderate aligned`, and `strong aligned`. Pooled summaries are shown for exploration; use the per-symbol file to check whether a pooled pattern is driven by one ETF.", "",
        "## Pooled condition summaries (100 ms and 500 ms)", "",
        markdown_table(top_view.round({"mean_markout_dollars": 3, "median_markout_dollars": 3, "win_rate": 3, "mean_net_exit_pnl_dollars": 3})), "",
        "## High-versus-low bucket contrasts", "",
        "A positive difference means bucket B had a better mean markout than bucket A. Confidence intervals resample trading days as clusters (not individual fills); these are exploratory intervals across several tested conditions, not multiple-testing-adjusted confirmation. For TSLA pressure, the entry rule already selects aligned pressure, so the contrast is moderate versus strong alignment. For time, it is the opening half-hour versus the final two hours.", "",
        markdown_table(contrast_view.round({"bucket_b_minus_a_mean_markout_dollars": 3, "day_bootstrap_95ci_low": 3, "day_bootstrap_95ci_high": 3})), "",
        "## Files", "",
        "- `fills_with_prearrival_conditions.csv`: per-fill conditions and 100 ms–5 s markouts.",
        "- `condition_markouts_pooled.csv`: pooled bucket summaries.",
        "- `condition_markouts_by_symbol.csv`: ETF-specific bucket summaries.",
        "- `day_cluster_bootstrap_contrasts.csv`: high/low contrasts with day-cluster intervals.",
        "- `within_symbol_quartile_cutpoints.csv`: numeric cutoffs for the within-ETF quartiles.",
        "- `daily_summary.csv`: per-session outcomes.", "",
        "## Limits", "",
        "- The sample comes from the older September 1–October 9 queue-maker replay (4,657 fills), not the later stricter run with 23 fills. Treat conditions as hypotheses for a cleaner follow-up.",
        "- The saved trade file does not retain the full pre-arrival order book; spread, top-level depth, quote age, and TSLA pressure are reconstructed from the cached BBO. Queue ahead is the replay's logged estimate.",
        "- The filtered cache omits channel_id and sequence, the fill model omits hidden liquidity and market impact, and no participant identities are available.",
        "- A useful filter must be evaluated on all candidate orders, including unfilled orders, and on dates not used to choose the filter.",
    ]
    (out / "report.md").write_text("\n".join(lines) + "\n")
    print(f"Saved fill toxicity study to {out}")
    print(contrast_view.to_string(index=False))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trades", type=Path, default=Path("backtests/queue_maker_20260901-20261009/trades.csv"))
    ap.add_argument("--markouts", type=Path, default=Path("backtests/queue_maker_markouts_20260901-20261009/markouts_by_trade.csv"))
    ap.add_argument("--data-dir", type=Path, default=Path("XNAS_MBO"))
    ap.add_argument("--out", type=Path, default=Path("backtests/queue_maker_fill_toxicity_20260901-20261009"))
    ap.add_argument("--entry-delay-us", type=float, default=50.0)
    ap.add_argument("--bootstrap-reps", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=6908)
    a = ap.parse_args()
    run(a.trades, a.markouts, a.data_dir, a.out, a.entry_delay_us, a.bootstrap_reps, a.seed)


if __name__ == "__main__":
    main()
