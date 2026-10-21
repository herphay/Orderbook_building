#!/usr/bin/env python3
"""Event-driven, quote-based backtest for TSLA and its leveraged ETFs.

The included example strategy trades an ETF when TSLA's trailing return crosses
a threshold. It is a research scaffold, not a recommendation. Signals use only
TSLA quotes observed by the signal timestamp; fills use an ETF quote strictly
after the signal plus configured latency. Fills cross the displayed top of book.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from utils import leadlag


@dataclass
class Quotes:
    t: np.ndarray
    bid: np.ndarray
    ask: np.ndarray
    bid_sz: np.ndarray
    ask_sz: np.ndarray


def load_quotes(cache: Path, symbol: str) -> tuple[Quotes, pd.Timestamp]:
    z = np.load(cache, allow_pickle=False)
    day = pd.Timestamp(str(z["day"]))
    bbo = leadlag.build_bbo(z[symbol]).sort_values("ts_event", kind="stable")
    bbo = bbo.drop_duplicates("ts_event", keep="last")
    t = bbo.ts_event.to_numpy(np.int64)
    bid, ask = bbo.bid.to_numpy(float), bbo.ask.to_numpy(float)
    ok = np.isfinite(bid) & np.isfinite(ask) & (bid > 0) & (ask > bid)
    return Quotes(t[ok], bid[ok], ask[ok], bbo.bid_sz.to_numpy(np.int64)[ok],
                  bbo.ask_sz.to_numpy(np.int64)[ok]), day


def rth_quotes(q: Quotes, day: pd.Timestamp) -> Quotes:
    date = day.date()
    lo = pd.Timestamp(f"{date} 09:30:00", tz="America/New_York").tz_convert("UTC").value
    hi = pd.Timestamp(f"{date} 16:00:00", tz="America/New_York").tz_convert("UTC").value
    keep = (q.t >= lo) & (q.t < hi)
    return Quotes(q.t[keep], q.bid[keep], q.ask[keep], q.bid_sz[keep], q.ask_sz[keep])


def first_future(t: np.ndarray, when: int) -> int:
    """First quote strictly after when; avoids same-time cross-symbol lookahead."""
    return int(np.searchsorted(t, when, side="right"))


def run_backtest(tsla: Quotes, etf: Quotes, *, symbol: str, lookback_ns: int,
                 threshold: float, hold_ns: int, latency_ns: int, quantity: int,
                 fee_per_share: float, signal_mode: str = "momentum",
                 long_only: bool = False) -> pd.DataFrame:
    if not len(tsla.t) or not len(etf.t):
        raise ValueError("No valid regular-session quotes for TSLA or ETF")
    if signal_mode not in ("momentum", "reversal"):
        raise ValueError("signal_mode must be 'momentum' or 'reversal'")
    smid = (tsla.bid + tsla.ask) / 2
    emid = (etf.bid + etf.ask) / 2
    ref_idx = np.searchsorted(tsla.t, tsla.t - lookback_ns, side="right") - 1
    returns = np.zeros(len(tsla.t), dtype=float)
    eligible = (ref_idx >= 0)
    returns[eligible] = smid[eligible] / smid[ref_idx[eligible]] - 1.0
    zones = np.where(returns >= threshold, 1, np.where(returns <= -threshold, -1, 0))
    previous_zones = np.r_[0, zones[:-1]]
    signal_indices = np.flatnonzero((zones != 0) & (zones != previous_zones))
    blocked_until = -1
    trades: list[dict] = []

    for k in signal_indices:
        signal_time = tsla.t[k]
        zone = int(zones[k])
        if signal_time <= blocked_until:
            continue

        direction = int(zone) if signal_mode == "momentum" else -int(zone)
        if long_only and direction < 0:
            continue
        entry_i = first_future(etf.t, int(signal_time) + latency_ns)
        if entry_i >= len(etf.t):
            continue
        entry_time = int(etf.t[entry_i])
        entry_px = etf.ask[entry_i] if direction > 0 else etf.bid[entry_i]
        entry_depth = etf.ask_sz[entry_i] if direction > 0 else etf.bid_sz[entry_i]
        if entry_depth < quantity:
            continue  # no partial or hidden-liquidity fills in this version

        exit_target = entry_time + hold_ns
        exit_i = first_future(etf.t, exit_target + latency_ns)
        forced_exit = exit_i >= len(etf.t)
        if forced_exit:
            exit_i = len(etf.t) - 1
            if exit_i <= entry_i:
                continue
        exit_time = int(etf.t[exit_i])
        exit_px = etf.bid[exit_i] if direction > 0 else etf.ask[exit_i]
        exit_depth = etf.bid_sz[exit_i] if direction > 0 else etf.ask_sz[exit_i]
        if exit_depth < quantity:
            continue

        entry_mid, exit_mid = emid[entry_i], emid[exit_i]
        mid_pnl = direction * (exit_mid - entry_mid) * quantity
        executable_pnl = direction * (exit_px - entry_px) * quantity
        entry_slippage = (entry_px - entry_mid) * direction * quantity
        exit_slippage = (exit_mid - exit_px) * direction * quantity
        fees = 2 * quantity * fee_per_share
        entry_notional = entry_px * quantity
        trades.append({
            "symbol": symbol, "signal_time_ns": int(signal_time), "signal_return": returns[k],
            "direction": "long" if direction > 0 else "short", "quantity": quantity,
            "entry_time_ns": entry_time, "entry_price": entry_px, "entry_mid": entry_mid,
            "entry_notional": entry_notional,
            "entry_depth": int(entry_depth), "exit_target_ns": exit_target,
            "exit_time_ns": exit_time, "exit_price": exit_px, "exit_mid": exit_mid,
            "exit_depth": int(exit_depth), "forced_eod_exit": forced_exit,
            "mid_pnl_before_spread": mid_pnl, "pnl_after_spread_before_fees": executable_pnl,
            "entry_spread_cost": entry_slippage,
            "exit_spread_cost": exit_slippage, "fees": fees,
            "net_pnl": executable_pnl - fees,
        })
        blocked_until = exit_time
    return pd.DataFrame(trades)


def run_imbalance_backtest(etf: Quotes, *, symbol: str, threshold: float,
                           hold_ns: int, latency_ns: int, quantity: int,
                           fee_per_share: float) -> pd.DataFrame:
    """Buy after top-of-book bid-size imbalance crosses a positive threshold."""
    if not len(etf.t):
        raise ValueError("No valid regular-session ETF quotes")
    total = etf.bid_sz + etf.ask_sz
    imbalance = np.divide(etf.bid_sz - etf.ask_sz, total,
                          out=np.zeros(len(total), dtype=float), where=total > 0)
    above = imbalance >= threshold
    signal_indices = np.flatnonzero(above & ~np.r_[False, above[:-1]])
    mid = (etf.bid + etf.ask) / 2
    trades: list[dict] = []
    blocked_until = -1
    for k in signal_indices:
        signal_time = int(etf.t[k])
        if signal_time <= blocked_until:
            continue
        entry_i = first_future(etf.t, signal_time + latency_ns)
        if entry_i >= len(etf.t):
            continue
        entry_time = int(etf.t[entry_i])
        if etf.ask_sz[entry_i] < quantity:
            continue
        target = entry_time + hold_ns
        exit_i = first_future(etf.t, target + latency_ns)
        forced_eod = exit_i >= len(etf.t)
        if forced_eod:
            exit_i = len(etf.t) - 1
            if exit_i <= entry_i:
                continue
        exit_time = int(etf.t[exit_i])
        if etf.bid_sz[exit_i] < quantity:
            continue
        entry_px, exit_px = etf.ask[entry_i], etf.bid[exit_i]
        entry_mid, exit_mid = mid[entry_i], mid[exit_i]
        mid_pnl = (exit_mid - entry_mid) * quantity
        execution_pnl = (exit_px - entry_px) * quantity
        entry_cost = (entry_px - entry_mid) * quantity
        exit_cost = (exit_mid - exit_px) * quantity
        fees = 2 * quantity * fee_per_share
        trades.append({
            "symbol": symbol, "signal_time_ns": signal_time,
            "signal_imbalance": float(imbalance[k]), "direction": "long",
            "quantity": quantity, "entry_time_ns": entry_time,
            "entry_price": entry_px, "entry_mid": entry_mid,
            "entry_depth": int(etf.ask_sz[entry_i]), "exit_target_ns": target,
            "exit_time_ns": exit_time, "exit_price": exit_px, "exit_mid": exit_mid,
            "exit_depth": int(etf.bid_sz[exit_i]), "forced_eod_exit": forced_eod,
            "mid_pnl_before_spread": mid_pnl,
            "pnl_after_spread_before_fees": execution_pnl,
            "entry_spread_cost": entry_cost, "exit_spread_cost": exit_cost,
            "fees": fees, "net_pnl": execution_pnl - fees,
            "entry_notional": entry_px * quantity,
        })
        blocked_until = exit_time
    return pd.DataFrame(trades)


def run_mispricing_backtest(tsla: Quotes, etf: Quotes, *, symbol: str, leverage: float,
                           entry_bps: float, exit_bps: float, max_hold_ns: int,
                           latency_ns: int, quantity: int, fee_per_share: float,
                           long_only: bool = False) -> pd.DataFrame:
    """Trade ETF deviations from an opening-anchored leveraged TSLA fair price."""
    if not len(tsla.t) or not len(etf.t):
        raise ValueError("No valid regular-session quotes for TSLA or ETF")
    smid = (tsla.bid + tsla.ask) / 2
    emid = (etf.bid + etf.ask) / 2
    anchor_time = max(int(tsla.t[0]), int(etf.t[0]))
    ai0 = int(np.searchsorted(tsla.t, anchor_time, side="right")) - 1
    ei0 = int(np.searchsorted(etf.t, anchor_time, side="right")) - 1
    if ai0 < 0 or ei0 < 0:
        raise ValueError("Could not form causal opening anchor")
    anchor_tsla, anchor_etf = smid[ai0], emid[ei0]

    events = np.union1d(tsla.t, etf.t)
    ai = np.searchsorted(tsla.t, events, side="right") - 1
    ei = np.searchsorted(etf.t, events, side="right") - 1
    valid = (ai >= ai0) & (ei >= ei0) & (events >= anchor_time)
    fair = anchor_etf * (1.0 + leverage * (smid[np.maximum(ai, 0)] / anchor_tsla - 1.0))
    gaps = np.full(len(events), np.nan)
    good = valid & (fair > 0)
    gaps[good] = (emid[np.maximum(ei[good], 0)] / fair[good] - 1.0) * 1e4

    trades: list[dict] = []
    previous_zone = 0
    i = int(np.searchsorted(events, anchor_time, side="left"))
    while i < len(events):
        gap = gaps[i]
        if not np.isfinite(gap):
            i += 1
            continue
        zone = -1 if gap <= -entry_bps else (1 if gap >= entry_bps else 0)
        if zone == 0 or zone == previous_zone:
            previous_zone = zone
            i += 1
            continue

        signal_time = int(events[i])
        direction = 1 if zone < 0 else -1  # ETF discount -> long; premium -> short
        if long_only and direction < 0:
            previous_zone = zone
            i += 1
            continue
        entry_i = first_future(etf.t, signal_time + latency_ns)
        previous_zone = zone
        if entry_i >= len(etf.t):
            break
        entry_time = int(etf.t[entry_i])
        entry_px = etf.ask[entry_i] if direction > 0 else etf.bid[entry_i]
        entry_depth = etf.ask_sz[entry_i] if direction > 0 else etf.bid_sz[entry_i]
        if entry_depth < quantity:
            i = int(np.searchsorted(events, entry_time, side="right"))
            continue

        # Wait for convergence, or close when the maximum holding time expires.
        j = int(np.searchsorted(events, entry_time, side="right"))
        exit_reason = "max_hold"
        condition_time = None
        hold_deadline = entry_time + max_hold_ns
        while j < len(events):
            g = gaps[j]
            t = int(events[j])
            converged = np.isfinite(g) and (g >= -exit_bps if direction > 0 else g <= exit_bps)
            if converged:
                exit_reason, condition_time = "converged", t
                break
            if t >= hold_deadline:
                condition_time = t
                break
            j += 1

        forced_eod = condition_time is None
        if forced_eod:
            exit_i = len(etf.t) - 1
        else:
            exit_i = first_future(etf.t, int(condition_time) + latency_ns)
            if exit_i >= len(etf.t):
                exit_i = len(etf.t) - 1
                forced_eod = True
        if exit_i <= entry_i:
            break
        exit_time = int(etf.t[exit_i])
        exit_px = etf.bid[exit_i] if direction > 0 else etf.ask[exit_i]
        exit_depth = etf.bid_sz[exit_i] if direction > 0 else etf.ask_sz[exit_i]
        if exit_depth < quantity:
            # This version requires the full displayed size at both ends.
            i = int(np.searchsorted(events, exit_time, side="right"))
            continue

        entry_mid, exit_mid = emid[entry_i], emid[exit_i]
        mid_pnl = direction * (exit_mid - entry_mid) * quantity
        execution_pnl = direction * (exit_px - entry_px) * quantity
        entry_cost = (entry_px - entry_mid) * direction * quantity
        exit_cost = (exit_mid - exit_px) * direction * quantity
        fees = 2 * quantity * fee_per_share
        gap_i = min(i, len(gaps) - 1)
        trades.append({
            "symbol": symbol, "signal_time_ns": signal_time, "mispricing_bps": float(gap),
            "direction": "long" if direction > 0 else "short", "quantity": quantity,
            "anchor_time_ns": anchor_time, "anchor_tsla_mid": anchor_tsla,
            "anchor_etf_mid": anchor_etf, "entry_time_ns": entry_time,
            "entry_price": entry_px, "entry_mid": entry_mid, "entry_depth": int(entry_depth),
            "exit_reason": exit_reason, "exit_condition_time_ns": condition_time,
            "exit_time_ns": exit_time, "exit_price": exit_px, "exit_mid": exit_mid,
            "exit_depth": int(exit_depth), "forced_eod_exit": forced_eod,
            "mid_pnl_before_spread": mid_pnl,
            "pnl_after_spread_before_fees": execution_pnl,
            "entry_spread_cost": entry_cost, "exit_spread_cost": exit_cost,
            "fees": fees, "net_pnl": execution_pnl - fees,
            "entry_notional": entry_px * quantity,
        })
        # Resume only after the exit fill; this prevents overlapping positions.
        i = int(np.searchsorted(events, exit_time, side="right"))
        if i < len(events) and np.isfinite(gaps[i - 1]):
            g_after = gaps[i - 1]
            previous_zone = -1 if g_after <= -entry_bps else (1 if g_after >= entry_bps else 0)
        else:
            previous_zone = 0
    return pd.DataFrame(trades)


def summarize(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {"trades": 0, "message": "No trades met the signal and displayed-depth rules."}
    net = trades.net_pnl.to_numpy(float)
    wins = net[net > 0]
    losses = net[net < 0]
    return {
        "trades": int(len(trades)), "long_trades": int((trades.direction == "long").sum()),
        "short_trades": int((trades.direction == "short").sum()),
        "win_rate_net": float((net > 0).mean()),
        "mid_pnl_before_spread_dollars": float(trades.mid_pnl_before_spread.sum()),
        "spread_cost_dollars": float((trades.entry_spread_cost + trades.exit_spread_cost).sum()),
        "pnl_after_spread_before_fees_dollars": float(trades.pnl_after_spread_before_fees.sum()),
        "commissions_dollars": float(trades.fees.sum()), "net_pnl_dollars": float(net.sum()),
        "net_return_bps_on_entry_notional": float(10_000 * net.sum() / trades.entry_notional.sum()),
        "mean_net_pnl_per_trade": float(net.mean()),
        "profit_factor_net": float(wins.sum() / abs(losses.sum())) if len(losses) and wins.sum() else None,
        "max_drawdown_trade_sequence": float((np.maximum.accumulate(np.r_[0.0, np.cumsum(net)]) - np.r_[0.0, np.cumsum(net)]).max()),
        "forced_eod_exits": int(trades.forced_eod_exit.sum()),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--day", default="20261009", help="Trading date YYYYMMDD (default: available latest day)")
    ap.add_argument("--symbol", choices=leadlag.FUNDS, default="TSLL")
    ap.add_argument("--strategy", choices=("momentum", "mispricing"), default="momentum")
    ap.add_argument("--lookback-ms", type=float, default=100.0)
    ap.add_argument("--threshold-bps", type=float, default=5.0)
    ap.add_argument("--hold-ms", type=float, default=1000.0)
    ap.add_argument("--latency-ms", type=float, default=1.0)
    ap.add_argument("--quantity", type=int, default=100)
    ap.add_argument("--fee-per-share", type=float, default=0.0)
    ap.add_argument("--mispricing-entry-bps", type=float, default=30.0)
    ap.add_argument("--mispricing-exit-bps", type=float, default=5.0)
    ap.add_argument("--max-hold-s", type=float, default=30.0)
    ap.add_argument("--data-dir", type=Path, default=Path("XNAS_MBO"))
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    cache = a.data_dir / f"xnas-itch-{a.day}.mbo.dbn.zst.TSLA-TSLL-TSLR-TSLG.npz"
    if not cache.exists():
        ap.error(f"Filtered MBO cache not found: {cache}")

    tsla, day = load_quotes(cache, "TSLA")
    etf, _ = load_quotes(cache, a.symbol)
    tsla, etf = rth_quotes(tsla, day), rth_quotes(etf, day)
    params = {"day": str(day.date()), "symbol": a.symbol, "strategy": a.strategy,
              "lookback_ms": a.lookback_ms, "threshold_bps": a.threshold_bps, "hold_ms": a.hold_ms,
              "latency_ms": a.latency_ms, "quantity": a.quantity,
              "mispricing_entry_bps": a.mispricing_entry_bps,
              "mispricing_exit_bps": a.mispricing_exit_bps, "max_hold_s": a.max_hold_s,
              "fee_per_share": a.fee_per_share,
              "signal": ("cross ETF-vs-TSLA leveraged fair-value gap; fade the gap" if a.strategy == "mispricing"
                         else "cross trailing TSLA mid return threshold; same direction ETF"),
              "fill_rule": "first ETF BBO strictly after signal/exit target plus latency; cross ask/bid",
              "depth_rule": "all-or-none if displayed best-level size covers quantity"}
    if a.strategy == "mispricing":
        if a.mispricing_exit_bps >= a.mispricing_entry_bps:
            ap.error("--mispricing-exit-bps must be below --mispricing-entry-bps")
        trades = run_mispricing_backtest(tsla, etf, symbol=a.symbol,
            leverage=leadlag.FUNDS[a.symbol]["L"], entry_bps=a.mispricing_entry_bps,
            exit_bps=a.mispricing_exit_bps, max_hold_ns=int(a.max_hold_s * 1e9),
            latency_ns=int(a.latency_ms * 1e6), quantity=a.quantity,
            fee_per_share=a.fee_per_share)
    else:
        trades = run_backtest(tsla, etf, symbol=a.symbol,
            lookback_ns=int(a.lookback_ms * 1e6), threshold=a.threshold_bps / 1e4,
            hold_ns=int(a.hold_ms * 1e6), latency_ns=int(a.latency_ms * 1e6),
            quantity=a.quantity, fee_per_share=a.fee_per_share)
    summary = {"parameters": params, "results": summarize(trades),
               "limitations": ["One Nasdaq venue's displayed book, not consolidated NBBO.",
                   "Top level only; orders larger than displayed best depth are skipped.",
                   "No market impact, queue priority, hidden liquidity, borrow cost, or ETF fees.",
                   "Fair value is anchored at the first jointly observable session quotes, not official NAV.",
                   "One day and one parameter setting are not statistical evidence."]}
    out = a.out or Path("backtests") / a.day / a.symbol / a.strategy
    out.mkdir(parents=True, exist_ok=True)
    trades.to_csv(out / "trades.csv", index=False)
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    print(f"Trade log: {out / 'trades.csv'}")


if __name__ == "__main__":
    main()
