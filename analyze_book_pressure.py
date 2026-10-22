#!/usr/bin/env python3
"""Standalone L1 pressure comparison, chronological prediction test and spread hurdle.

Example: python analyze_book_pressure.py --workers 1
Uses existing daily NPZ caches; no downloads or API key required.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

from utils.leadlag import build_bbo
from utils.parallel import run_sessions

SYMBOLS = ("TSLA", "TSLL", "TSLR", "TSLG")
VERSION = 1
BINS = np.linspace(-1, 1, 11)
MOMENTS = ("n", "sx", "sy", "sxx", "sxy", "syy")


def asof_quotes(bbo, targets):
    """Last complete state STRICTLY BEFORE target; retain invalid-state boundaries.

    Do not drop invalid quotes before as-of alignment: doing so could resurrect
    an old valid quote after a book clear/cross. Equal timestamp events are
    excluded, avoiding assumptions about within-timestamp observability.
    """
    q = bbo.sort_values("ts_event", kind="stable").drop_duplicates("ts_event", keep="last")
    times = q.ts_event.to_numpy(np.int64)
    values = q[["bid", "ask", "bid_sz", "ask_sz"]].to_numpy(float)
    ix = np.searchsorted(times, targets, side="left") - 1
    state = np.full((len(targets), 4), np.nan)
    age = np.full(len(targets), np.inf)
    found = ix >= 0
    state[found] = values[ix[found]]
    age[found] = (targets[found] - times[ix[found]]) / 1e6
    bid, ask, bs, ass = state.T
    valid = np.isfinite(state).all(axis=1) & (bid > 0) & (ask > bid) & (bs > 0) & (ass > 0)
    return state, valid, age


def sufficient(x, y):
    return dict(n=len(x), sx=float(x.sum()), sy=float(y.sum()),
                sxx=float(x @ x), sxy=float(x @ y), syy=float(y @ y))


def markout_bps(entry, future, pressure, fee_bps=0):
    """Mid-return and exact displayed spread hurdle, normalized by entry mid."""
    bid, ask = entry[:, 0], entry[:, 1]
    fb, fa = future[:, 0], future[:, 1]
    mid = (bid + ask) / 2
    y = ((fb + fa) / 2 - mid) / mid * 1e4
    cost = ((ask - bid) + (fa - fb)) / (2 * mid) * 1e4
    signed = np.sign(pressure) * y
    return y, cost, signed, signed - cost - fee_bps


def linear_fit(s):
    n = s["n"]
    if n < 3:
        return np.nan, np.nan
    var = s["sxx"] - s["sx"] ** 2 / n
    if var <= 1e-12:
        return np.nan, np.nan
    beta = (s["sxy"] - s["sx"] * s["sy"] / n) / var
    return (s["sy"] - beta * s["sx"]) / n, beta


def targets_for_day(day, grid_ms, horizons):
    date = pd.Timestamp(day).date()
    start = pd.Timestamp(f"{date} 09:30", tz="America/New_York").tz_convert("UTC").value
    end = pd.Timestamp(f"{date} 16:00", tz="America/New_York").tz_convert("UTC").value
    # Same starting observations for every horizon; no overnight targets.
    return np.arange(start + int(grid_ms * 1e6), end - int(max(horizons) * 1e6),
                     int(grid_ms * 1e6), dtype=np.int64)


def analyze_day(task):
    path_str, cfg, out_str, resume = task
    path, out = Path(path_str), Path(out_str)
    day = path.name.split("xnas-itch-")[1][:8]
    fingerprint = hashlib.sha256(json.dumps(
        {"version": VERSION, "config": cfg, "source": str(path.resolve()),
         "size": path.stat().st_size, "mtime_ns": path.stat().st_mtime_ns}, sort_keys=True
    ).encode()).hexdigest()
    checkpoint = out / "sessions" / f"{day}.json"
    if resume and checkpoint.exists():
        cached = json.loads(checkpoint.read_text())
        if cached.get("fingerprint") == fingerprint:
            return cached
    result = dict(day=day, fingerprint=fingerprint, descriptions=[], histograms=[], metrics=[], bins=[])
    grid = targets_for_day(day, cfg["grid_ms"], cfg["horizons_ms"])
    with np.load(path, allow_pickle=False) as z:
        for symbol in SYMBOLS:
            bbo = build_bbo(z[symbol])
            # Sort once here; asof_quotes also accepts arbitrary input for testing.
            bbo = bbo.sort_values("ts_event", kind="stable").drop_duplicates("ts_event", keep="last")
            state, good, age = asof_quotes(bbo, grid)
            if cfg["max_quote_age_ms"] > 0:
                good &= age <= cfg["max_quote_age_ms"]
            bid, ask, bs, ass = state.T
            mid = (bid + ask) / 2
            pressure = (bs - ass) / (bs + ass)
            x = pressure[good]
            if not len(x):
                raise ValueError(f"{day} {symbol}: no valid grid observations")
            spread = (ask - bid) / mid * 1e4
            result["descriptions"].append(dict(day=day, symbol=symbol, grid_n=len(grid), n=len(x),
                valid_fraction=float(good.mean()), mean_pressure=float(x.mean()),
                mean_abs_pressure=float(np.abs(x).mean()), std_pressure=float(x.std()),
                strong_fraction=float((np.abs(x) >= cfg["threshold"]).mean()),
                mean_spread_bps=float(spread[good].mean()), mean_bid_size=float(bs[good].mean()),
                mean_ask_size=float(ass[good].mean()), mean_quote_age_ms=float(age[good].mean())))
            hist = np.histogram(x, BINS)[0]
            for k, count in enumerate(hist):
                result["histograms"].append(dict(day=day, symbol=symbol, bin=k,
                    pressure_center=float((BINS[k] + BINS[k+1]) / 2), n=int(count), fraction=float(count / len(x))))
            for horizon in cfg["horizons_ms"]:
                future, valid_future, future_age = asof_quotes(bbo, grid + int(horizon * 1e6))
                if cfg["max_quote_age_ms"] > 0:
                    valid_future &= future_age <= cfg["max_quote_age_ms"]
                valid = good & valid_future
                if valid.sum() < 3:
                    raise ValueError(f"{day} {symbol} {horizon}ms: fewer than 3 valid targets")
                x = pressure[valid]
                # Arithmetic bps, all costs normalized by entry mid: exact decomposition.
                y, cost, signed, net = markout_bps(state[valid], future[valid], x, cfg["fee_bps"])
                strong = np.abs(x) >= cfg["threshold"]
                base = dict(day=day, symbol=symbol, horizon_ms=horizon)
                stats = sufficient(x, y)
                _, beta = linear_fit(stats)
                result["metrics"].append(dict(**base, **stats, beta_bps=float(beta) if np.isfinite(beta) else None,
                    strong_n=int(strong.sum()),
                    strong_gross_bps=float(signed[strong].mean()) if strong.any() else None,
                    strong_spread_bps=float(cost[strong].mean()) if strong.any() else None,
                    strong_net_bps=float(net[strong].mean()) if strong.any() else None,
                    strong_positive_net_fraction=float((net[strong] > 0).mean()) if strong.any() else None,
                    strong_correct_fraction=float((signed[strong] > 0).mean()) if strong.any() else None,
                    strong_zero_move_fraction=float((y[strong] == 0).mean()) if strong.any() else None))
                labels = np.clip(np.searchsorted(BINS, x, side="right") - 1, 0, 9)
                for k in range(10):
                    m = labels == k
                    if not m.any():
                        continue
                    result["bins"].append(dict(**base, bin=k, n=int(m.sum()),
                        pressure_center=float((BINS[k] + BINS[k+1]) / 2),
                        return_bps=float(y[m].mean()), signed_move_bps=float(signed[m].mean()),
                        spread_cost_bps=float(cost[m].mean()), net_bps=float(net[m].mean()),
                        up_fraction=float((y[m] > 0).mean()), down_fraction=float((y[m] < 0).mean()),
                        zero_fraction=float((y[m] == 0).mean())))
            del bbo
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    tmp = checkpoint.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, allow_nan=False))
    tmp.replace(checkpoint)
    return result


def week_interval(part, col, rng, repetitions=5000):
    """Equal-day within week, then equal-week mean and week bootstrap CI."""
    weeks = part.groupby("week")[col].mean().dropna().to_numpy(float)
    if not len(weeks):
        return dict(mean=np.nan, ci_low=np.nan, ci_high=np.nan, weeks=0)
    mean = float(weeks.mean())
    if len(weeks) < 2:
        return dict(mean=mean, ci_low=np.nan, ci_high=np.nan, weeks=len(weeks))
    boot = weeks[rng.integers(0, len(weeks), (repetitions, len(weeks)))].mean(axis=1)
    return dict(mean=mean, ci_low=float(np.quantile(boot, .025)),
                ci_high=float(np.quantile(boot, .975)), weeks=len(weeks))


def aggregate(results, args, cfg, out):
    frames = {key: pd.DataFrame([row for r in results for row in r[key]])
              for key in ("descriptions", "histograms", "metrics", "bins")}
    for key, frame in frames.items():
        if key == "metrics":
            for column in frame.columns:
                if column not in ("day", "symbol"):
                    frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame["day"] = frame.day.astype(str)
        frame["split"] = np.where(frame.day < args.test_start, "train", "test")
        frame["week"] = pd.to_datetime(frame.day, format="%Y%m%d").dt.strftime("%G-W%V")
        frame.sort_values(["day", "symbol"]).to_csv(out / f"daily_{key}.csv", index=False)
    rng = np.random.default_rng(args.seed)
    desc = frames["descriptions"]
    numeric = [c for c in desc.select_dtypes(include="number") if c not in ("n", "grid_n")]
    desc.groupby("symbol")[numeric].mean().reindex(SYMBOLS).to_csv(out / "pressure_comparison.csv")
    metrics = frames["metrics"]
    summaries, models = [], []
    for (symbol, horizon), part in metrics.groupby(["symbol", "horizon_ms"]):
        train, test = part[part.split == "train"], part[part.split == "test"]
        tr = train[list(MOMENTS)].sum()
        te = test[list(MOMENTS)].sum()
        intercept, beta = linear_fit(tr)
        if not np.isfinite(beta):
            raise ValueError(f"Cannot fit {symbol} {horizon}ms: insufficient training variation")
        # Fit only July-August. Evaluate fixed coefficients against fixed training-mean baseline.
        sse = (te.syy - 2*intercept*te.sy - 2*beta*te.sxy + intercept**2*te.n
               + 2*intercept*beta*te.sx + beta**2*te.sxx)
        baseline = tr.sy / tr.n
        sse_base = te.syy - 2*baseline*te.sy + baseline**2*te.n
        models.append(dict(symbol=symbol, horizon_ms=horizon, train_sessions=len(train), test_sessions=len(test),
            train_n=int(tr.n), test_n=int(te.n), intercept_bps=intercept, beta_bps=beta,
            test_rmse_bps=float(np.sqrt(max(0, sse) / te.n)),
            baseline_rmse_bps=float(np.sqrt(max(0, sse_base) / te.n)),
            oos_r2_vs_train_mean=float(1-sse/sse_base) if sse_base > 0 else np.nan))
        for split, subset in (("train", train), ("test", test)):
            row = dict(symbol=symbol, horizon_ms=horizon, split=split, sessions=len(subset),
                       n=int(subset.n.sum()), strong_n=int(subset.strong_n.sum()))
            for col in ("beta_bps", "strong_gross_bps", "strong_spread_bps", "strong_net_bps",
                        "strong_positive_net_fraction", "strong_correct_fraction", "strong_zero_move_fraction"):
                row.update({f"{col}_{k}": v for k, v in week_interval(subset, col, rng).items()})
            summaries.append(row)
    summary = pd.DataFrame(summaries)
    summary.to_csv(out / "predictive_summary.csv", index=False)
    models = pd.DataFrame(models)
    models.to_csv(out / "out_of_sample_models.csv", index=False)
    bin_rows = []
    for (split, symbol, horizon, k), part in frames["bins"].groupby(["split", "symbol", "horizon_ms", "bin"]):
        row = dict(split=split, symbol=symbol, horizon_ms=horizon, bin=k,
                   pressure_center=float(part.pressure_center.iloc[0]), n=int(part.n.sum()))
        for col in ("return_bps", "signed_move_bps", "spread_cost_bps", "net_bps"):
            row.update({f"{col}_{name}": value for name, value in week_interval(part, col, rng).items()})
        bin_rows.append(row)
    bin_summary = pd.DataFrame(bin_rows)
    bin_summary.to_csv(out / "pressure_bins.csv", index=False)
    metadata = dict(config=cfg, version=VERSION, selected_sessions=[r["day"] for r in results],
        train_sessions=sorted(metrics.loc[metrics.split == "train", "day"].unique()),
        test_sessions=sorted(metrics.loc[metrics.split == "test", "day"].unique()),
        test_start=args.test_start, primary_horizon_ms=args.primary_horizon_ms, seed=args.seed,
        measure="L1 displayed size imbalance: (best bid size - best ask size) / (sum of sizes)",
        inference="Equal-day within ISO week, equal-week mean; 5000 week bootstrap resamples. Descriptive CIs, no multiple-testing claim.",
        model="OLS future arithmetic mid-return bps ~ intercept + pressure. Pooled train snapshots; fixed coefficients on test. OOS R2 versus constant training-mean forecast.",
        cost="sign(pressure)*mid_return_bps - (entry spread + exit spread)/(2*entry mid)*10000 - round-trip fee_bps",
        limitations=["Nasdaq displayed L1 only, not NBBO, full-depth pressure or order-flow imbalance.",
            "Pressure is measured strictly before each grid time; targets use the last state strictly before t+h.",
            "An unchanged standing quote remains valid by default; quote age is time since last BBO state change, not a feed heartbeat.",
            "Overlapping targets are dependent; inference resamples weeks, not ticks. Few held-out weeks limit precision.",
            "Chronological holdout within already studied historical data is not an untouched prospective test.",
            "Association is not causal or incremental to TSLA returns, momentum or other predictors.",
            "Spread-adjusted markouts are a diagnostic, not a strategy backtest: no fill guarantee, latency, depth impact, queue, inventory or borrow constraints.",
            "Threshold and horizons are fixed in advance of this run; alternate specifications remain exploratory."])
    (out / "summary.json").write_text(json.dumps(metadata, indent=2))
    plot_results(frames, summary, models, bin_summary, args, out)
    write_report(metadata, summary, models, args, out)


def plot_results(frames, summary, models, bins, args, out):
    os.environ.setdefault("MPLCONFIGDIR", str(out / ".matplotlib"))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.size": 11, "axes.spines.top": False, "axes.spines.right": False,
                         "figure.dpi": 130, "savefig.dpi": 180})
    colors = dict(zip(SYMBOLS, ["#2459a6", "#139c8e", "#df8b23", "#9656ad"]))

    def save(fig, name, note):
        fig.text(.02, .018, note, fontsize=9, color="#555555")
        fig.tight_layout(rect=(0, .065, 1, .94))
        for extension in ("png", "svg"):
            fig.savefig(out / f"{name}.{extension}", bbox_inches="tight")
        plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(12, 4.8))
    for symbol in SYMBOLS:
        p = frames["histograms"].query("symbol == @symbol").groupby("pressure_center").fraction.mean()
        axes[0].plot(p.index, p*100, marker="o", label=symbol, color=colors[symbol])
    axes[0].set(xlabel="Best-quote pressure (−1 sell-heavy, +1 buy-heavy)", ylabel="Time samples (%)")
    axes[0].legend(ncol=2)
    d = frames["descriptions"].groupby("symbol").mean(numeric_only=True).reindex(SYMBOLS)
    axes[1].bar(SYMBOLS, d.strong_fraction*100, color=[colors[s] for s in SYMBOLS])
    axes[1].set(ylabel=f"Time with |pressure| ≥ {args.threshold:g} (%)")
    fig.suptitle("How does displayed book pressure differ across TSLA and its ETFs?", fontsize=15)
    save(fig, "01_pressure_comparison", f"All {frames['descriptions'].day.nunique()} sessions; equal-day averages; fixed {args.grid_ms:g} ms sampling; Nasdaq L1.")

    fig, axes = plt.subplots(2, 2, figsize=(11, 8), sharex=True)
    for ax, symbol in zip(axes.flat, SYMBOLS):
        p = bins.query("split == 'test' and symbol == @symbol and horizon_ms == @args.primary_horizon_ms").sort_values("bin")
        ax.plot(p.pressure_center, p.return_bps_mean, "o-", color=colors[symbol])
        ax.fill_between(p.pressure_center, p.return_bps_ci_low, p.return_bps_ci_high, alpha=.18, color=colors[symbol])
        ax.axhline(0, color="grey", lw=.8)
        ax.set(title=symbol, xlabel="Current pressure", ylabel="Future midprice move (bps)")
    fig.suptitle(f"Does pressure predict the next {args.primary_horizon_ms:g} ms price move?", fontsize=15)
    save(fig, "02_pressure_vs_future_return", "Held-out sessions only; fixed pressure bins; shading: 95% week bootstrap CI. Panel y-scales differ.")

    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    for symbol in SYMBOLS:
        p = summary.query("split == 'test' and symbol == @symbol").sort_values("horizon_ms")
        axes[0].plot(p.horizon_ms, p.beta_bps_mean, "o-", color=colors[symbol], label=symbol)
        axes[0].fill_between(p.horizon_ms, p.beta_bps_ci_low, p.beta_bps_ci_high, alpha=.13, color=colors[symbol])
        m = models.query("symbol == @symbol").sort_values("horizon_ms")
        axes[1].plot(m.horizon_ms, m.oos_r2_vs_train_mean*100, "o-", color=colors[symbol], label=symbol)
    for ax in axes:
        ax.set_xscale("log")
        ax.axhline(0, color="grey", lw=.8)
        ax.set_xlabel("Forecast horizon (ms, log scale)")
        ax.legend()
    axes[0].set_ylabel("Held-out slope (bps per +1 pressure)")
    axes[1].set_ylabel("Out-of-sample R² vs training-mean forecast (%)")
    fig.suptitle("Pressure association and out-of-sample predictive power", fontsize=15)
    save(fig, "03_predictive_power", "Left: held-out daily slopes, week bootstrap CI. Right: fixed training coefficients; negative R² means worse than baseline.")

    fig, axes = plt.subplots(2, 2, figsize=(11, 8), sharex=True)
    for ax, symbol in zip(axes.flat, SYMBOLS):
        p = summary.query("split == 'test' and symbol == @symbol").sort_values("horizon_ms")
        ax.plot(p.horizon_ms, p.strong_gross_bps_mean, "o-", label="Directional mid move", color=colors[symbol])
        ax.plot(p.horizon_ms, p.strong_spread_bps_mean, "--", label="Round-trip spread hurdle", color="#bb4141")
        ax.plot(p.horizon_ms, p.strong_net_bps_mean, "s-", label="Move minus spreads/fees", color="#323a48")
        ax.fill_between(p.horizon_ms, p.strong_net_bps_ci_low, p.strong_net_bps_ci_high, color="#323a48", alpha=.12)
        ax.axhline(0, color="grey", lw=.8)
        ax.set(xscale="log", title=symbol, xlabel="Horizon (ms)", ylabel="Basis points")
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", bbox_to_anchor=(.5, .945), ncol=3, fontsize=9)
    fig.suptitle(f"Is the move large enough to cover spreads? |pressure| ≥ {args.threshold:g}", fontsize=15)
    save(fig, "04_move_vs_spread", "Held-out sessions; direction = sign(pressure); both entry and exit half-spreads. Indicative markouts, not executable P&L.")


def write_report(meta, summary, models, args, out):
    rows = summary.query("split == 'test' and horizon_ms == @args.primary_horizon_ms").set_index("symbol")
    model = models.query("horizon_ms == @args.primary_horizon_ms").set_index("symbol")
    better = [s for s in SYMBOLS if model.loc[s].oos_r2_vs_train_mean > 0]
    covered = [s for s in SYMBOLS if rows.loc[s].strong_net_bps_mean > 0]
    lines = ["# Standalone book-pressure analysis", "",
        f"Sessions: {len(meta['selected_sessions'])}; training: {len(meta['train_sessions'])}; held-out: {len(meta['test_sessions'])}.", "",
        "Pressure = (best bid displayed size − best ask displayed size) / (sum of sizes). Positive means more displayed buying depth.", "",
        f"Primary display horizon: {args.primary_horizon_ms:g} ms. Fixed directional rule: buy for pressure ≥ {args.threshold:g}, sell for pressure ≤ −{args.threshold:g}.", "",
        f"The frozen pressure-only model improves held-out squared-error prediction versus the training-mean baseline for {', '.join(better) if better else 'none of the instruments'}. "
        f"The average strong-pressure move exceeds spreads and configured fees for {', '.join(covered) if covered else 'none of the instruments'} at this horizon. "
        "Forecast improvement and economic usefulness are separate questions; the cost comparison is indicative and is not a fill simulation.", "",
        "| Instrument | Held-out slope (bps / +1 pressure) | OOS R² (%) | Directional move (bps) | Spread hurdle (bps) | After spreads/fees (bps) | 95% CI for net |",
        "|---|---:|---:|---:|---:|---:|---|" ]
    for symbol in SYMBOLS:
        r = rows.loc[symbol]
        lines.append(f"| {symbol} | {r.beta_bps_mean:.4f} | {100*model.loc[symbol].oos_r2_vs_train_mean:.4f} | "
            f"{r.strong_gross_bps_mean:.4f} | {r.strong_spread_bps_mean:.4f} | {r.strong_net_bps_mean:.4f} | "
            f"[{r.strong_net_bps_ci_low:.4f}, {r.strong_net_bps_ci_high:.4f}] |")
    lines += ["", "## How to interpret", "",
        "1. Compare 01_pressure_comparison: magnitude and frequency of pressure across instruments; positive average pressure alone is not evidence of predictability.",
        "2. In 02_pressure_vs_future_return, an increasing curve indicates positive pressure/return association. Flat or irregular curves indicate weak or unstable association.",
        "3. In 03_predictive_power, a positive held-out slope is association; positive OOS R² means the frozen linear model improves squared-error prediction against the training-mean baseline. These are distinct tests.",
        "4. In 04_move_vs_spread, the directional move must exceed both entry and exit half-spreads plus configured fees. A positive midprice forecast can still be uneconomic.", "",
        "Conditional charts and slopes use equal-week averages of daily statistics; model fitting and OOS R² pool observations. They have different weighting intentionally and are labeled accordingly.", "",
        "## Scope and limitations", "", *[f"- {s}" for s in meta["limitations"]], "",
        "## Files", "", "- daily_*.csv: session-level diagnostics, moments and conditional means.",
        "- pressure_comparison.csv: equal-day descriptive comparison.",
        "- pressure_bins.csv / predictive_summary.csv: week-based estimates and intervals, train/test separated.",
        "- out_of_sample_models.csv: training coefficients and held-out errors.",
        "- summary.json: exact configuration, dates and methodology.",
        "- 01–04 PNG/SVG files: slide-ready charts.",
        "- sessions/: resumable per-session sufficient statistics; use --resume to reuse matching inputs/settings."]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", type=Path, default=Path("XNAS_MBO"))
    ap.add_argument("--start-day", default="20260701")
    ap.add_argument("--end-day", default="20260930")
    ap.add_argument("--test-start", default="20260901", help="First held-out date (inclusive)")
    ap.add_argument("--out", type=Path, default=Path("leadlag_out/book_pressure_20260701_20260930"))
    ap.add_argument("--horizons-ms", nargs="+", type=float, default=[1, 10, 100, 1000, 10000])
    ap.add_argument("--primary-horizon-ms", type=float, default=1000)
    ap.add_argument("--grid-ms", type=float, default=100)
    ap.add_argument("--threshold", type=float, default=.5)
    ap.add_argument("--fee-bps", type=float, default=0, help="Additional round-trip fees in bps")
    ap.add_argument("--max-quote-age-ms", type=float, default=0, help="0 disables age cutoff (standing quotes remain valid)")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--seed", type=int, default=6908)
    args = ap.parse_args()
    for field in ("start_day", "end_day", "test_start"):
        try:
            pd.to_datetime(getattr(args, field), format="%Y%m%d", errors="raise")
        except ValueError:
            ap.error(f"--{field.replace('_', '-')} must be YYYYMMDD")
    numbers = [args.grid_ms, args.threshold, args.fee_bps, args.max_quote_age_ms, *args.horizons_ms]
    if not all(np.isfinite(numbers)):
        ap.error("Numeric parameters must be finite")
    if args.workers < 1 or args.grid_ms < .001 or not 0 < args.threshold <= 1 or args.fee_bps < 0 or args.max_quote_age_ms < 0:
        ap.error("Invalid workers, grid, threshold, fee or age limit")
    if any(h < .001 or h >= 23_400_000 for h in args.horizons_ms):
        ap.error("Horizons must be at least .001 ms and shorter than one session")
    args.horizons_ms = sorted(set(args.horizons_ms))
    if args.primary_horizon_ms not in args.horizons_ms:
        ap.error("Primary horizon must be included in --horizons-ms")
    caches = sorted(args.data_dir.glob("xnas-itch-*.mbo.dbn.zst.TSLA-TSLL-TSLR-TSLG.npz"))
    caches = [p for p in caches if args.start_day <= p.name.split("xnas-itch-")[1][:8] <= args.end_day]
    days = [p.name.split("xnas-itch-")[1][:8] for p in caches]
    if not any(d < args.test_start for d in days) or not any(d >= args.test_start for d in days):
        ap.error("Need cached sessions on both sides of --test-start")
    args.out.mkdir(parents=True, exist_ok=True)
    cfg = {k: getattr(args, k) for k in ("grid_ms", "horizons_ms", "threshold", "fee_bps", "max_quote_age_ms")}
    print(f"{len(days)} sessions; {sum(d < args.test_start for d in days)} train / "
          f"{sum(d >= args.test_start for d in days)} test; {args.workers} worker(s)", flush=True)
    results = []
    tasks = [(str(p), cfg, str(args.out), args.resume) for p in caches]
    for result in run_sessions(analyze_day, tasks, args.workers):
        results.append(result)
        print(f"[{len(results)}/{len(tasks)}] {result['day']}", flush=True)
    results.sort(key=lambda r: r["day"])
    aggregate(results, args, cfg, args.out)
    print(f"Saved tables, report and PNG/SVG charts to {args.out.resolve()}", flush=True)


if __name__ == "__main__":
    main()
