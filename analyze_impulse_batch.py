#!/usr/bin/env python3
"""Extend forward Analysis C (TSLA -> ETFs) across cached sessions.

python analyze_impulse_batch.py --workers 1 --resume
Daily curves use utils.leadlag.impulse; uncertainty resamples ISO weeks,
jointly across ETFs, rather than treating overlapping quote events as iid.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

from utils.leadlag import FUNDS, IMPULSE_TAUS, build_bbo, impulse, make_series
from utils.parallel import run_sessions

VERSION = 1
SYMBOLS = tuple(FUNDS)


def crossing_times(curves, taus, fraction):
    """First crossing of fraction of each curve's terminal response.

    Same log-time interpolation as Analysis C. Nonpositive/nonfinite terminal
    responses have undefined timing; no monotonicity is imposed on the curve.
    """
    if not 0 < fraction < 1:
        raise ValueError("fraction must be between 0 and 1")
    taus = np.asarray(taus)
    pos = taus >= 0
    ts = taus[pos].astype(float)
    y = np.atleast_2d(curves)[:, pos]
    ref = y[:, -1]
    valid = np.isfinite(y).all(axis=1) & (ref > 0)
    hits = y >= fraction * ref[:, None]
    k = hits.argmax(axis=1)
    answer = np.full(len(y), np.nan)
    eligible = valid & hits.any(axis=1)
    answer[eligible] = ts[k[eligible]]
    interp = eligible & (k > 0) & (ts[np.maximum(k-1, 0)] > 0)
    rows = np.flatnonzero(interp)
    j = k[rows]
    b0, b1 = y[rows, j-1], y[rows, j]
    weight = (fraction*ref[rows] - b0) / (b1 - b0)
    answer[rows] = np.exp(np.log(ts[j-1]) + weight*(np.log(ts[j]) - np.log(ts[j-1])))
    return answer


def session_window(day):
    date = pd.Timestamp(day).date()
    return tuple(pd.Timestamp(f"{date} {h}", tz="America/New_York").tz_convert("UTC").value
                 for h in ("09:30", "16:00"))


def process_day(task):
    path_str, out_str, resume, library_hash = task
    path, out = Path(path_str), Path(out_str)
    day = path.name.split("xnas-itch-")[1][:8]
    source = dict(version=VERSION, path=str(path.resolve()), size=path.stat().st_size,
                  mtime_ns=path.stat().st_mtime_ns, library_hash=library_hash)
    fingerprint = hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()
    checkpoint = out / "sessions" / f"{day}.json"
    if resume and checkpoint.exists():
        saved = json.loads(checkpoint.read_text())
        if saved.get("fingerprint") == fingerprint:
            return saved
    start, end = session_window(day)
    series = {}
    with np.load(path, allow_pickle=False) as z:
        for symbol in ("TSLA", *SYMBOLS):
            series[symbol] = make_series(build_bbo(z[symbol]), symbol, "event", "mid",
                                         start - 300_000_000_000, end)
    own = series["TSLA"]
    # Unlike the single-day convenience entry point, keep every trigger's
    # full [-1ms,+1s] measurement window inside regular trading hours.
    left = start - int(IMPULSE_TAUS.min())
    right = end - int(IMPULSE_TAUS.max())
    # Ensure as-of lookups cannot use a negative index at either endpoint.
    left = max(left, max(int(s.t[0]) for s in series.values()) - int(IMPULSE_TAUS.min()) + 1)
    eligible = (own.tm >= left) & (own.tm < right)
    t, dx = own.tm[eligible], own.dxm[eligible]
    if not len(t):
        raise ValueError(f"{day}: no TSLA triggers with a complete within-session window")
    result = dict(day=day, fingerprint=fingerprint, source=source, n_triggers=len(t), pairs={})
    for symbol in SYMBOLS:
        curve = impulse(t, dx, series[symbol], own, IMPULSE_TAUS, FUNDS[symbol]["L"])
        if not np.isfinite(curve.beta).all():
            raise ValueError(f"{day} {symbol}: undefined impulse response; inspect denominator")
        # Do not retain the legacy iid event standard error: weekly inference
        # below accounts for clustered/overlapping event observations instead.
        result["pairs"][symbol] = dict(tau_ns=curve.tau_ns.tolist(), beta=curve.beta.tolist())
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    tmp = checkpoint.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, allow_nan=False))
    tmp.replace(checkpoint)
    return result


def interval(values):
    v = np.asarray(values)
    v = v[np.isfinite(v)]
    return [float(q) for q in np.quantile(v, [.025, .975])] if len(v) else [None, None]


def aggregate(results, args):
    out = args.out
    days = [r["day"] for r in results]
    week_ids = pd.to_datetime(days, format="%Y%m%d").strftime("%G-W%V").to_numpy()
    weeks = sorted(set(week_ids))
    if len(weeks) < 2:
        raise ValueError("At least two ISO weeks are needed for week-bootstrap inference")
    rng = np.random.default_rng(args.seed)
    # Common bootstrap weights preserve cross-ETF dependence when comparing ranks.
    weights = rng.multinomial(len(weeks), np.full(len(weeks), 1/len(weeks)), size=args.bootstrap) / len(weeks)
    daily_rows, curve_rows, summaries, boot_times = [], [], [], {}
    taus = IMPULSE_TAUS
    for symbol in SYMBOLS:
        daily = np.array([r["pairs"][symbol]["beta"] for r in results])
        weekly = np.array([daily[week_ids == week].mean(axis=0) for week in weeks])
        mean = weekly.mean(axis=0)
        boot = weights @ weekly
        raw_lo, raw_hi = np.quantile(boot, [.025, .975], axis=0)
        good_ref = np.isfinite(boot[:, -1]) & (boot[:, -1] > 0)
        normalized = boot[good_ref] / boot[good_ref, -1, None]
        if len(normalized) < .95 * args.bootstrap or mean[-1] <= 0:
            raise ValueError(f"{symbol}: terminal response is too unstable for normalized timing")
        norm_lo, norm_hi = np.quantile(normalized, [.025, .975], axis=0)
        day_t50 = crossing_times(daily, taus, .5) / 1e6
        day_t90 = crossing_times(daily, taus, .9) / 1e6
        bt50 = crossing_times(boot, taus, .5) / 1e6
        bt90 = crossing_times(boot, taus, .9) / 1e6
        boot_times[symbol] = {"t50": bt50, "t90": bt90}
        t50 = float(crossing_times(mean, taus, .5)[0] / 1e6)
        t90 = float(crossing_times(mean, taus, .9)[0] / 1e6)
        row = dict(symbol=symbol, sessions=len(days), week_clusters=len(weeks),
            beta_at_1s=float(mean[-1]), beta_at_1s_ci_low=float(raw_lo[-1]), beta_at_1s_ci_high=float(raw_hi[-1]),
            t50_ms=t50, t50_ci_low_ms=interval(bt50)[0], t50_ci_high_ms=interval(bt50)[1],
            t90_ms=t90, t90_ci_low_ms=interval(bt90)[0], t90_ci_high_ms=interval(bt90)[1],
            daily_t50_median_ms=float(np.nanmedian(day_t50)), daily_t50_q25_ms=float(np.nanquantile(day_t50, .25)),
            daily_t50_q75_ms=float(np.nanquantile(day_t50, .75)),
            daily_t90_median_ms=float(np.nanmedian(day_t90)), daily_t90_q25_ms=float(np.nanquantile(day_t90, .25)),
            daily_t90_q75_ms=float(np.nanquantile(day_t90, .75)),
            daily_timing_defined=int(np.isfinite(day_t50).sum()), bootstrap_timing_defined=int(np.isfinite(bt50).sum()),
            beta_at_zero=float(mean[taus == 0][0]), pre_move_1ms=float(mean[taus == -1_000_000][0]),
            downward_steps=int((np.diff(mean[taus >= 0]) < -1e-10).sum()))
        summaries.append(row)
        for i, tau in enumerate(taus):
            curve_rows.append(dict(symbol=symbol, tau_ns=int(tau), tau_ms=float(tau / 1e6),
                beta=float(mean[i]), beta_ci_low=float(raw_lo[i]), beta_ci_high=float(raw_hi[i]),
                fraction_of_1s=float(mean[i]/mean[-1]), fraction_ci_low=float(norm_lo[i]), fraction_ci_high=float(norm_hi[i])))
        for j, day in enumerate(days):
            for i, tau in enumerate(taus):
                daily_rows.append(dict(day=day, week=week_ids[j], symbol=symbol, n_triggers=results[j]["n_triggers"],
                    tau_ns=int(tau), beta=float(daily[j, i])))
    summary = pd.DataFrame(summaries)
    curves = pd.DataFrame(curve_rows)
    daily_frame = pd.DataFrame(daily_rows)
    daily_frame.to_csv(out / "daily_curves.csv", index=False)
    curves.to_csv(out / "aggregate_curves.csv", index=False)
    summary.to_csv(out / "timing_summary.csv", index=False)
    daily_timing = []
    for (day, symbol), group in daily_frame.groupby(["day", "symbol"]):
        g = group.sort_values("tau_ns")
        daily_timing.append(dict(day=day, symbol=symbol, n_triggers=int(g.n_triggers.iloc[0]),
            beta_at_1s=float(g.beta.iloc[-1]),
            t50_ms=float(crossing_times(g.beta.to_numpy(), g.tau_ns.to_numpy(), .5)[0]/1e6),
            t90_ms=float(crossing_times(g.beta.to_numpy(), g.tau_ns.to_numpy(), .9)[0]/1e6)))
    pd.DataFrame(daily_timing).to_csv(out / "daily_timing.csv", index=False)
    pairwise = []
    for i, a in enumerate(SYMBOLS):
        for b in SYMBOLS[i+1:]:
            for metric in ("t50", "t90"):
                aa, bb = boot_times[a][metric], boot_times[b][metric]
                valid = np.isfinite(aa) & np.isfinite(bb)
                delta = aa[valid] - bb[valid]
                pairwise.append(dict(a=a, b=b, metric=metric, difference_ci_low_ms=interval(delta)[0],
                    difference_ci_high_ms=interval(delta)[1], bootstrap_fraction_a_faster=float((delta < 0).mean()),
                    note="Paired bootstrap stability, not a null-hypothesis p-value"))
    pd.DataFrame(pairwise).to_csv(out / "pairwise_timing.csv", index=False)
    meta = dict(start_day=args.start_day, end_day=args.end_day, cache_sessions=len(days), selected_sessions=days,
        week_clusters=weeks, bootstrap=args.bootstrap, seed=args.seed, pairs=summaries,
        estimator="Original forward Analysis C: sum(sign(TSLA trigger)*ETF log-price response) / sum(2*sign(TSLA trigger)*TSLA log-price response)",
        aggregation="Equal-session mean daily beta within ISO week, then equal-week mean. Not an event-pooled ratio.",
        timing="First crossing of 50%/90% of the aggregate curve's 1-second endpoint, log-interpolated on the original horizon grid.",
        inference="Paired ISO-week bootstrap, percentile 95% pointwise CIs; recompute normalized curves and t50/t90 for every draw.",
        session_boundaries="Exclude TSLA triggers without a full [-1ms,+1s] window within 09:30-16:00 New York.",
        limitations=["Descriptive conditional response, not a causal identification or a latency-to-fill estimate.",
            "t50/t90 are relative to the observed 1-second response, not 50%/90% of theoretical full adjustment and not median event waiting times.",
            "Original estimator's denominator evolves with the TSLA price response; the curve can be nonmonotone. Timing uses the first crossing.",
            "Nasdaq displayed midquotes and exchange event timestamps only; no consolidated NBBO or execution costs.",
            "Retains the original valid-quote series preprocessing and as-of conventions; simultaneous updates are included at tau=0.",
            "Week bootstrap handles dependence within weeks, but 14 weeks is a limited sample; confidence bands are pointwise, not simultaneous.",
            "Excludes opening 1ms and final 1s, so even matching dates can differ slightly from the legacy single-day entry point."])
    (out / "summary.json").write_text(json.dumps(meta, indent=2, allow_nan=False))
    plot(curves, summary, args)
    report(meta, summary, out)
    print(summary[["symbol", "t50_ms", "t50_ci_low_ms", "t50_ci_high_ms", "t90_ms", "t90_ci_low_ms", "t90_ci_high_ms"]].to_string(index=False))


def plot(curves, summary, args):
    out = args.out
    os.environ.setdefault("MPLCONFIGDIR", str(out / ".matplotlib"))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import NullFormatter
    plt.rcParams.update({"font.size": 11, "axes.spines.top": False, "axes.spines.right": False})
    colors = dict(zip(SYMBOLS, ["#139c8e", "#df8b23", "#9656ad"]))
    n = int(summary.sessions.iloc[0])
    weeks = int(summary.week_clusters.iloc[0])

    def save(fig, name, footnote):
        fig.text(.025, .018, footnote, fontsize=9, color="#555555")
        fig.tight_layout(rect=(0, .075, 1, .93))
        for extension in ("png", "svg"):
            fig.savefig(out / f"{name}.{extension}", dpi=180, bbox_inches="tight")
        plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(13, 5.3))
    for symbol in SYMBOLS:
        p = curves[(curves.symbol == symbol) & (curves.tau_ns >= 0)]
        for ax, value, low, high in ((axes[0], "beta", "beta_ci_low", "beta_ci_high"),
                                      (axes[1], "fraction_of_1s", "fraction_ci_low", "fraction_ci_high")):
            ax.plot(p.tau_ms, 100*p[value], color=colors[symbol], label=symbol, lw=2)
            ax.fill_between(p.tau_ms, 100*p[low], 100*p[high], color=colors[symbol], alpha=.16)
            ax.set_xscale("symlog", linthresh=.001)
            ax.set_xticks([0, .001, .01, .1, 1, 10, 100, 1000], ["0", ".001", ".01", ".1", "1", "10", "100", "1000"])
            ax.set_xlabel("Time after TSLA midprice move (ms)")
            ax.grid(alpha=.15)
            ax.legend()
    axes[0].set(title="Leverage-scaled response", ylabel="ETF response / (2 × TSLA response), %")
    axes[1].set(title="Speed relative to the 1-second response", ylabel="Fraction of observed 1-second response (%)")
    for level in (50, 90):
        axes[1].axhline(level, color="#777777", lw=.8, ls="--")
    fig.suptitle(f"TSLA → ETF price response across {n} sessions", fontsize=17)
    save(fig, "01_impulse_response", f"{args.start_day}–{args.end_day} · Equal-week mean · Shading: 95% pointwise CI, {weeks} week clusters · Descriptive response, not executable latency")

    ordered = summary.sort_values("t50_ms")
    fig, axes = plt.subplots(1, 2, figsize=(11, 5))
    for ax, metric, label in ((axes[0], "t50", "50% of the 1-second response"),
                               (axes[1], "t90", "90% of the 1-second response")):
        for i, row in enumerate(ordered.itertuples()):
            value = getattr(row, f"{metric}_ms")
            low, high = getattr(row, f"{metric}_ci_low_ms"), getattr(row, f"{metric}_ci_high_ms")
            # Intervals are drawn independently of the point: bootstrap
            # percentile intervals need not bracket a nonlinear point estimate.
            ax.hlines(i, low, high, color=colors[row.symbol], linewidth=3)
            ax.plot(value, i, "o", color=colors[row.symbol], markersize=8)
            ax.annotate(f"{value:.3g} ms", (value, i), xytext=(0, 12), textcoords="offset points", ha="center")
        ax.set_yticks(range(len(ordered)), ordered.symbol)
        ax.invert_yaxis()
        ax.set_ylim(len(ordered)-.5, -.65)
        lower = float(ordered[f"{metric}_ci_low_ms"].min())
        upper = float(ordered[f"{metric}_ci_high_ms"].max())
        if lower > 0:
            ax.set_xscale("log")
            ax.set_xlim(lower*.65, upper*1.5)
            ticks = [v for v in (.001, .002, .005, .01, .02, .05, .1, .2, .5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000)
                     if lower*.65 <= v <= upper*1.5]
            ax.set_xticks(ticks, [f"{v:g}" for v in ticks])
        else:
            ax.set_xscale("symlog", linthresh=.001)
            ax.set_xlim(0, max(.001, upper*1.5))
        ax.set(title=label, xlabel="Time (ms, logarithmic scale)")
        ax.xaxis.set_minor_formatter(NullFormatter())
        ax.grid(axis="x", alpha=.2)
    fig.suptitle(f"How quickly do the ETFs adjust? {n}-session estimates", fontsize=16)
    save(fig, "02_response_timing", "Points: first-crossing times of aggregate curve · Lines: paired week-bootstrap 95% CIs · These are not median event delays")


def report(meta, summary, out):
    lines = ["# Analysis C across the quarter", "",
        f"{meta['cache_sessions']} cached sessions, {meta['selected_sessions'][0]}–{meta['selected_sessions'][-1]}, "
        f"{len(meta['week_clusters'])} ISO-week clusters. All dates are included in summary.json.", "",
        "## Presentation table", "",
        "| ETF | t50 (ms) | 95% CI | t90 (ms) | 95% CI | Response ratio at 1 second |",
        "|---|---:|---|---:|---|---:|" ]
    for r in summary.sort_values("t50_ms").itertuples():
        lines.append(f"| {r.symbol} | {r.t50_ms:.3f} | [{r.t50_ci_low_ms:.3f}, {r.t50_ci_high_ms:.3f}] | "
                     f"{r.t90_ms:.3f} | [{r.t90_ci_low_ms:.3f}, {r.t90_ci_high_ms:.3f}] | {r.beta_at_1s:.3f} |")
    lines += ["", "t50 and t90 measure time to 50% and 90% of each ETF's own observed response at one second. "
              "They do not measure time to complete price discovery or a guaranteed trading opportunity.", "",
              "## Method", "", meta["estimator"], "", meta["aggregation"], "", meta["inference"], "",
              meta["session_boundaries"], "",
              "The original event-level standard errors are not used. Bootstrap resampling uses the same weeks for all ETFs, "
              "preserving their dependence. See pairwise_timing.csv for uncertainty in timing differences; its faster fractions are not p-values.", "",
              "The daily median timing in timing_summary.csv describes the distribution of session-level crossing times. "
              "It is distinct from the first-crossing time of the aggregate curve used in the presentation table.", "",
              "## Limitations", "", *[f"- {s}" for s in meta["limitations"]], "",
              "## Reproduce", "", "```bash", "python analyze_impulse_batch.py --workers 1 --resume", "```", "",
              "PNG/SVG charts: 01_impulse_response and 02_response_timing. CSVs: daily_curves, daily_timing, "
              "aggregate_curves, timing_summary, pairwise_timing. sessions/ contains resumable daily curves."]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", type=Path, default=Path("XNAS_MBO"))
    ap.add_argument("--start-day", default="20260701")
    ap.add_argument("--end-day", default="20260930")
    ap.add_argument("--out", type=Path, default=Path("leadlag_out/impulse_20260701_20260930"))
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--bootstrap", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=6908)
    args = ap.parse_args()
    if args.workers < 1 or args.bootstrap < 100:
        ap.error("Need workers >= 1 and bootstrap >= 100")
    for day in (args.start_day, args.end_day):
        try:
            pd.to_datetime(day, format="%Y%m%d", errors="raise")
        except ValueError:
            ap.error("Dates must be YYYYMMDD")
    if args.end_day < args.start_day:
        ap.error("End date must not precede start date")
    caches = sorted(args.data_dir.glob("xnas-itch-*.mbo.dbn.zst.TSLA-TSLL-TSLR-TSLG.npz"))
    caches = [p for p in caches if args.start_day <= p.name.split("xnas-itch-")[1][:8] <= args.end_day]
    if not caches:
        ap.error("No matching cached sessions")
    args.out.mkdir(parents=True, exist_ok=True)
    library_hash = hashlib.sha256(Path(__file__).with_name("utils").joinpath("leadlag.py").read_bytes()).hexdigest()
    tasks = [(str(p), str(args.out), args.resume, library_hash) for p in caches]
    results = []
    print(f"Analysis C: {len(tasks)} sessions, {args.workers} worker(s)", flush=True)
    for result in run_sessions(process_day, tasks, args.workers):
        results.append(result)
        print(f"[{len(results)}/{len(tasks)}] {result['day']} — {result['n_triggers']:,} TSLA triggers", flush=True)
    results.sort(key=lambda r: r["day"])
    aggregate(results, args)
    print(f"Saved results to {args.out.resolve()}", flush=True)


if __name__ == "__main__":
    main()
