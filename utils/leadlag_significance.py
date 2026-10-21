#!/usr/bin/env python3
"""Week-blocked test for short-horizon TSLA -> LETF quote-move timing.

For every valid LETF midquote move, measure whether the most recent TSLA
midquote move in the same direction occurred within a given horizon. Compare
with same-session placebo times made by independently shifting LETF move times
by 1--5 seconds. The primary horizon is 1 ms; other horizons are descriptive.
ISO trading weeks, not quote events or individual days, are the resampling units.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from utils.leadlag import FUNDS, build_bbo, make_series

HORIZONS_NS = np.array([100_000, 1_000_000, 10_000_000, 100_000_000, 1_000_000_000], dtype=np.int64)
N_NULL = 20
N_BOOT = 20_000
N_SIGNFLIP = 50_000


def _matching_lags(tsla_t, tsla_s, etf_t, etf_s):
    """Elapsed ns since the latest same-sign TSLA move, or -1 if none."""
    out = np.full(len(etf_t), -1, dtype=np.int64)
    for sign in (-1, 1):
        st = tsla_t[tsla_s == sign]
        mask = etf_s == sign
        # Strictly earlier. Equal exchange timestamps are simultaneous updates,
        # not evidence that TSLA moved first and the ETF reacted afterward.
        ix = np.searchsorted(st, etf_t[mask], side="left") - 1
        good = ix >= 0
        vals = np.full(mask.sum(), -1, dtype=np.int64)
        vals[good] = etf_t[mask][good] - st[ix[good]]
        out[mask] = vals
    return out


def _response_fractions(lags, n_events):
    return np.array([np.count_nonzero((lags >= 0) & (lags <= h)) / n_events
                     for h in HORIZONS_NS], dtype=float)


def _holm(pvals):
    p = np.asarray(pvals, dtype=float)
    order = np.argsort(p)
    adj = np.empty(len(p), dtype=float)
    running = 0.0
    m = len(p)
    for rank, idx in enumerate(order):
        running = max(running, (m - rank) * p[idx])
        adj[idx] = min(1.0, running)
    return adj


def _cluster_inference(cluster_deltas, rng):
    """Equal-week mean, percentile block bootstrap CI, paired sign-flip p."""
    x = np.asarray(cluster_deltas, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    obs = float(x.mean())
    boot_ix = rng.integers(0, n, size=(N_BOOT, n))
    boot = x[boot_ix].mean(axis=1)
    # One-sided H1: observed response exceeds the shifted-time placebo.
    signs = rng.choice(np.array([-1.0, 1.0]), size=(N_SIGNFLIP, n))
    null = (signs * x).mean(axis=1)
    p = (1 + np.count_nonzero(null >= obs)) / (N_SIGNFLIP + 1)
    return {
        "week_clusters": int(n),
        "mean_excess_fraction": obs,
        "ci95_low": float(np.quantile(boot, 0.025)),
        "ci95_high": float(np.quantile(boot, 0.975)),
        "signflip_p_one_sided": float(p),
        "positive_week_fraction": float(np.mean(x > 0)),
        "median_week_excess": float(np.median(x)),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", default="XNAS_MBO")
    ap.add_argument("--start-day", default="20260701")
    ap.add_argument("--end-day", default="20260930")
    ap.add_argument("--out", default="leadlag_out/leadlag_significance_20260701_20260930")
    ap.add_argument("--seed", type=int, default=6908)
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)
    data_dir, out = Path(args.data_dir), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    lo_day, hi_day = int(args.start_day), int(args.end_day)
    caches = sorted(data_dir.glob("*.mbo.dbn.zst.TSLA-TSLL-TSLR-TSLG.npz"))
    caches = [p for p in caches if lo_day <= int(p.name.split("xnas-itch-")[1][:8]) <= hi_day]
    if not caches:
        raise SystemExit("No matching filtered-record NPZ caches found")

    day_rows = []
    quote_gap_totals = {sym: [0, 0] for sym in ["TSLA", *FUNDS]}
    total_days = len(caches)
    for k, path in enumerate(caches, 1):
        day = path.name.split("xnas-itch-")[1][:8]
        print(f"[{k}/{total_days}] {day}", flush=True)
        z = np.load(path, allow_pickle=False)
        syms = ["TSLA", *FUNDS]
        day_stamp = pd.Timestamp(day)
        rth_start = pd.Timestamp(f"{day_stamp.date()} 09:30:00", tz="America/New_York").tz_convert("UTC").value
        rth_end = pd.Timestamp(f"{day_stamp.date()} 16:00:00", tz="America/New_York").tz_convert("UTC").value
        lo, hi = rth_start - 300 * 1_000_000_000, rth_end + 60 * 1_000_000_000
        series = {}
        for sym in syms:
            bbo = build_bbo(z[sym])
            series[sym] = make_series(bbo, sym, "event", "mid", lo, hi)
            qt = series[sym].t
            qt = qt[(qt >= rth_start) & (qt < rth_end)]
            gaps = np.diff(qt)
            quote_gap_totals[sym][0] += int(gaps.sum())
            quote_gap_totals[sym][1] += int(len(gaps))
        s = series["TSLA"]
        sm = (s.tm >= rth_start) & (s.tm < rth_end)
        tsla_t = s.tm[sm]
        tsla_s = np.sign(s.dxm[sm]).astype(np.int8)
        for sym in FUNDS:
            q = series[sym]
            qm = (q.tm >= rth_start) & (q.tm < rth_end)
            etf_t = q.tm[qm]
            etf_s = np.sign(q.dxm[qm]).astype(np.int8)
            if not len(etf_t) or not len(tsla_t):
                continue
            obs_lags = _matching_lags(tsla_t, tsla_s, etf_t, etf_s)
            obs = _response_fractions(obs_lags, len(etf_t))
            same_i = np.searchsorted(tsla_t, etf_t, side="left")
            same_time = (same_i < len(tsla_t))
            same_time &= np.where(same_time, tsla_t[np.minimum(same_i, len(tsla_t) - 1)] == etf_t, False)
            same_dir_same_time = same_time & np.where(same_time, tsla_s[np.minimum(same_i, len(tsla_s) - 1)] == etf_s, False)
            null = np.zeros((N_NULL, len(HORIZONS_NS)), dtype=float)
            session_len = rth_end - rth_start
            for j in range(N_NULL):
                offsets = rng.integers(1_000_000_000, 5_000_000_001, len(etf_t))
                offsets *= rng.choice(np.array([-1, 1], dtype=np.int64), len(etf_t))
                shifted = rth_start + np.mod(etf_t - rth_start + offsets, session_len)
                shifted.sort()
                # Preserve each event's original direction while sorting times.
                order = np.argsort(rth_start + np.mod(etf_t - rth_start + offsets, session_len), kind="stable")
                shifted_sign = etf_s[order]
                null_lags = _matching_lags(tsla_t, tsla_s, shifted, shifted_sign)
                null[j] = _response_fractions(null_lags, len(etf_t))
            row = {"day": day, "symbol": sym, "tsla_moves": len(tsla_t), "etf_moves": len(etf_t)}
            row["same_time_same_direction_fraction"] = float(same_dir_same_time.mean())
            for h_idx, h in enumerate(HORIZONS_NS):
                suffix = f"{h / 1e6:g}ms" if h >= 1_000_000 else f"{h / 1e3:g}us"
                row[f"obs_{suffix}"] = obs[h_idx]
                row[f"null_{suffix}"] = float(null[:, h_idx].mean())
                row[f"excess_{suffix}"] = obs[h_idx] - row[f"null_{suffix}"]
            day_rows.append(row)
        z.close()

    days = pd.DataFrame(day_rows)
    days.to_csv(out / "session_results.csv", index=False)
    days["week"] = pd.to_datetime(days.day, format="%Y%m%d").dt.strftime("%G-W%V")
    summary = {"period": [args.start_day, args.end_day], "cache_sessions": total_days,
               "statistical_unit": "ISO trading week (day-blocked)", "primary_horizon": "1ms",
               "alternative_horizons": ["100us", "10ms", "100ms", "1000ms"],
               "null": "independently shift each LETF quote-move time by +/-1-5 seconds within the same session; preserve move direction",
               "inference": "equal-weight ISO-week mean; 95% week-block bootstrap CI; paired week sign-flip one-sided test",
               "quote_update_interarrival": {sym: {"adjacent_bbo_intervals": n,
                   "mean_microseconds": (total / n / 1e3 if n else None)}
                   for sym, (total, n) in quote_gap_totals.items()},
               "pairs": {}}
    raw_primary_p = []
    primary_syms = []
    for sym in FUNDS:
        part = days[days.symbol == sym]
        sym_summary = {"sessions": int(part.day.nunique()), "same_timestamp_same_direction_fraction": float(part.same_time_same_direction_fraction.mean()), "mean_moves_per_session": {
            "TSLA": float(part.tsla_moves.mean()), "LETF": float(part.etf_moves.mean())}, "horizons": {}}
        for h in HORIZONS_NS:
            suffix = f"{h / 1e6:g}ms" if h >= 1_000_000 else f"{h / 1e3:g}us"
            weekly = part.groupby("week")[f"excess_{suffix}"].mean()
            inf = _cluster_inference(weekly.to_numpy(), rng)
            inf["mean_observed_fraction"] = float(part[f"obs_{suffix}"].mean())
            inf["mean_placebo_fraction"] = float(part[f"null_{suffix}"].mean())
            sym_summary["horizons"][suffix] = inf
            if h == 1_000_000:
                raw_primary_p.append(inf["signflip_p_one_sided"])
                primary_syms.append(sym)
        summary["pairs"][sym] = sym_summary
    adjusted = _holm(raw_primary_p)
    for sym, p_adj in zip(primary_syms, adjusted):
        summary["pairs"][sym]["horizons"]["1ms"]["holm_p_across_3_etfs"] = float(p_adj)
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    print(f"Saved results under {out}")


if __name__ == "__main__":
    main()
