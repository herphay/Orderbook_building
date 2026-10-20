#!/usr/bin/env python3
"""IOC stale-quote replay: use rolling TSLA moves to take stale displayed LETF quotes.

In rolling mode, the signal is a leverage-adjusted TSLA return over a fixed
lookback window, applied to the ETF midquote at the start of that same window.
When fair value clears the ETF ask/bid by estimated exit half-spread, fees, and
an edge buffer, an IOC limit order is sent after the configured delay.
It fills only if that price or a better price and enough displayed MBO size
remain at arrival. Fills are marked out by crossing the ETF spread at fixed
horizons. This is an exchange-book replay approximation, not a live fill model.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from backtest import Quotes, first_future, rth_quotes
from backtest_queue_maker import MBOQueueReplay, get_quotes
from utils import leadlag
from utils.parallel import run_sessions

MARKOUT_HORIZONS_MS = (1, 10, 50, 100, 1000)


def _best_level(book: MBOQueueReplay, side: int):
    prices = [p for sd, p in book.levels if sd == side and book.levels[(sd, p)]]
    if not prices:
        return None, 0
    px = min(prices) if side == 65 else max(prices)
    qty = sum(max(0, state["size"] - state["pending_exec"])
              for oid in book.levels[(side, px)]
              if (state := book.orders.get(oid)) is not None)
    return px, qty


def _depth_qty(book: MBOQueueReplay, side: int, n_levels: int) -> int:
    """Displayed shares across the n best active price levels on one side."""
    prices = sorted({price for sd, price in book.levels
                     if sd == side and book.levels[(sd, price)]},
                    reverse=(side == 66))
    total = 0
    for price in prices[:n_levels]:
        for oid in book.levels[(side, price)]:
            state = book.orders.get(oid)
            if state is not None:
                total += max(0, state["size"] - state["pending_exec"])
    return total


def _book_pressure_features(book: MBOQueueReplay, direction: int) -> dict:
    """Return multi-level ETF depth and imbalance, signed to the trade side."""
    out = {}
    for n_levels in (1, 5, 10):
        bid_depth = _depth_qty(book, side=66, n_levels=n_levels)
        ask_depth = _depth_qty(book, side=65, n_levels=n_levels)
        denom = bid_depth + ask_depth
        imbalance = (bid_depth - ask_depth) / denom if denom else np.nan
        out[f"etf_bid_depth_{n_levels}"] = bid_depth
        out[f"etf_ask_depth_{n_levels}"] = ask_depth
        out[f"etf_book_imbalance_{n_levels}"] = imbalance
        out[f"directional_book_pressure_{n_levels}"] = direction * imbalance
    return out


def _level_snapshot(book: MBOQueueReplay, side: int, price: int):
    """Capture exact resting MBO orders and their live shares at a price."""
    snapshot = {}
    for oid in book.levels.get((side, price), {}):
        state = book.orders.get(oid)
        if state is not None:
            qty = max(0, state["size"] - state["pending_exec"])
            if qty:
                snapshot[oid] = qty
    return snapshot


def _surviving_snapshot_qty(book: MBOQueueReplay, side: int, price: int,
                            snapshot: dict[int, int]) -> int:
    total = 0
    for oid in snapshot:
        state = book.orders.get(oid)
        if state is not None and state["side"] == side and state["price"] == price:
            total += max(0, state["size"] - state["pending_exec"])
    return total


def _fair_value(underlier_mid: float, etf_anchor_mid: float,
                tsla_anchor_mid: float, leverage: float) -> float:
    return etf_anchor_mid * (1.0 + leverage * (underlier_mid / tsla_anchor_mid - 1.0))


def _first_profitable_exit(etf: Quotes, *, direction: int, fill_price: float,
                           fill_time_ns: int, quantity: int, fee_per_share: float,
                           exit_delay_ns: int, max_hold_ns: int):
    """Return first profitable displayed exit after modeled order delay.

    A quote update is the trigger to send an exit. At modeled arrival, evaluate
    only the latest BBO strictly before arrival. If no profitable exit appears,
    force an aggressive exit at the latest BBO at the hard hold limit.
    """
    start = int(np.searchsorted(etf.t, fill_time_ns, side="right"))
    limit_arrival = fill_time_ns + max_hold_ns + exit_delay_ns
    last_quote_idx = -1
    for trigger_i in range(start, len(etf.t)):
        trigger_time = int(etf.t[trigger_i])
        order_arrival = trigger_time + exit_delay_ns
        if order_arrival > limit_arrival:
            break
        quote_i = int(np.searchsorted(etf.t, order_arrival, side="left") - 1)
        if quote_i < 0 or quote_i == last_quote_idx:
            continue
        last_quote_idx = quote_i
        exit_px = float(etf.bid[quote_i] if direction > 0 else etf.ask[quote_i])
        exit_qty = int(etf.bid_sz[quote_i] if direction > 0 else etf.ask_sz[quote_i])
        pnl = direction * (exit_px - fill_price) * quantity - 2.0 * fee_per_share * quantity
        if exit_qty >= quantity and pnl > 0:
            return {"exit_reason": "first_profitable_quote", "exit_order_arrival_ns": order_arrival,
                    "exit_quote_time_ns": int(etf.t[quote_i]), "exit_price": exit_px,
                    "exit_qty": quantity, "realized_pnl": pnl,
                    "hold_ms": (order_arrival - fill_time_ns) / 1e6}

    # Hard cap: use the last displayed quote that was known by exit-order arrival.
    quote_i = int(np.searchsorted(etf.t, limit_arrival, side="left") - 1)
    if quote_i < 0:
        return {"exit_reason": "no_exit_quote", "exit_order_arrival_ns": limit_arrival,
                "exit_quote_time_ns": np.nan, "exit_price": np.nan,
                "exit_qty": 0, "realized_pnl": np.nan, "hold_ms": max_hold_ns / 1e6}
    exit_px = float(etf.bid[quote_i] if direction > 0 else etf.ask[quote_i])
    exit_qty = int(etf.bid_sz[quote_i] if direction > 0 else etf.ask_sz[quote_i])
    if exit_qty < quantity:
        return {"exit_reason": "max_hold_insufficient_size", "exit_order_arrival_ns": limit_arrival,
                "exit_quote_time_ns": int(etf.t[quote_i]), "exit_price": exit_px,
                "exit_qty": exit_qty, "realized_pnl": np.nan, "hold_ms": max_hold_ns / 1e6}
    pnl = direction * (exit_px - fill_price) * quantity - 2.0 * fee_per_share * quantity
    return {"exit_reason": "max_hold_forced", "exit_order_arrival_ns": limit_arrival,
            "exit_quote_time_ns": int(etf.t[quote_i]), "exit_price": exit_px,
            "exit_qty": quantity, "realized_pnl": pnl,
            "hold_ms": max_hold_ns / 1e6}


def _conditional_adverse_exit(tsla: Quotes, etf: Quotes, *, direction: int,
                              fill_price: float, fill_time_ns: int,
                              signal_tsla_mid: float, quantity: int,
                              fee_per_share: float, exit_delay_ns: int,
                              max_hold_ns: int, tsla_reversal_bps: float,
                              adverse_book_imbalance: float):
    """Exit on TSLA retracement or opposing ETF top-of-book size imbalance."""
    deadline_ns = fill_time_ns + max_hold_ns
    ti = int(np.searchsorted(tsla.t, fill_time_ns, side="right"))
    ei = int(np.searchsorted(etf.t, fill_time_ns, side="right"))
    events = [(int(tsla.t[i]), 0, i) for i in range(ti, len(tsla.t))
              if tsla.t[i] <= deadline_ns]
    events.extend((int(etf.t[i]), 1, i) for i in range(ei, len(etf.t))
                  if etf.t[i] <= deadline_ns)
    events.sort()
    tsla_mid = np.nan
    bid_sz = ask_sz = 0

    def execute(trigger_ns: int, reason: str):
        arrival_ns = trigger_ns + exit_delay_ns
        qi = int(np.searchsorted(etf.t, arrival_ns, side="left") - 1)
        if qi < 0:
            return None
        px = float(etf.bid[qi] if direction > 0 else etf.ask[qi])
        qty = int(etf.bid_sz[qi] if direction > 0 else etf.ask_sz[qi])
        if qty < quantity:
            return None
        pnl = direction * (px - fill_price) * quantity - 2.0 * fee_per_share * quantity
        return {"exit_reason": reason, "exit_order_arrival_ns": arrival_ns,
                "exit_quote_time_ns": int(etf.t[qi]), "exit_price": px,
                "exit_qty": quantity, "realized_pnl": pnl,
                "hold_ms": (arrival_ns - fill_time_ns) / 1e6}

    for event_ns, source, idx in events:
        if source == 0:
            tsla_mid = (float(tsla.bid[idx]) + float(tsla.ask[idx])) / 2.0
        else:
            bid_sz, ask_sz = int(etf.bid_sz[idx]), int(etf.ask_sz[idx])
        if not np.isfinite(tsla_mid) or bid_sz + ask_sz <= 0:
            continue
        tsla_move_bps = direction * (tsla_mid / signal_tsla_mid - 1.0) * 1e4
        directional_imbalance = direction * (bid_sz - ask_sz) / (bid_sz + ask_sz)
        if tsla_move_bps <= -tsla_reversal_bps:
            result = execute(event_ns, "tsla_reversal")
        elif directional_imbalance <= -adverse_book_imbalance:
            result = execute(event_ns, "adverse_etf_top_imbalance")
        else:
            result = None
        if result is not None:
            return result

    limit_arrival = deadline_ns + exit_delay_ns
    quote_i = int(np.searchsorted(etf.t, limit_arrival, side="left") - 1)
    if quote_i < 0:
        return {"exit_reason": "no_exit_quote", "exit_order_arrival_ns": limit_arrival,
                "exit_quote_time_ns": np.nan, "exit_price": np.nan,
                "exit_qty": 0, "realized_pnl": np.nan,
                "hold_ms": max_hold_ns / 1e6}
    exit_px = float(etf.bid[quote_i] if direction > 0 else etf.ask[quote_i])
    exit_qty = int(etf.bid_sz[quote_i] if direction > 0 else etf.ask_sz[quote_i])
    if exit_qty < quantity:
        return {"exit_reason": "max_hold_insufficient_size", "exit_order_arrival_ns": limit_arrival,
                "exit_quote_time_ns": int(etf.t[quote_i]), "exit_price": exit_px,
                "exit_qty": exit_qty, "realized_pnl": np.nan,
                "hold_ms": max_hold_ns / 1e6}
    pnl = direction * (exit_px - fill_price) * quantity - 2.0 * fee_per_share * quantity
    return {"exit_reason": "max_hold_forced", "exit_order_arrival_ns": limit_arrival,
            "exit_quote_time_ns": int(etf.t[quote_i]), "exit_price": exit_px,
            "exit_qty": quantity, "realized_pnl": pnl,
            "hold_ms": max_hold_ns / 1e6}


def _target_stop_exit(etf: Quotes, *, direction: int, fill_price: float,
                      fill_time_ns: int, quantity: int, fee_per_share: float,
                      exit_delay_ns: int, max_hold_ns: int,
                      profit_target_ticks: int, adverse_mid_stop_ticks: int,
                      tick_size: float = 0.01):
    """Take profit at a multi-tick executable gain or stop on adverse mid move."""
    entry_quote_i = int(np.searchsorted(etf.t, fill_time_ns, side="left") - 1)
    if entry_quote_i < 0:
        return {"exit_reason": "no_exit_quote", "exit_order_arrival_ns": fill_time_ns,
                "exit_quote_time_ns": np.nan, "exit_price": np.nan,
                "exit_qty": 0, "realized_pnl": np.nan, "hold_ms": 0.0}
    entry_mid = (float(etf.bid[entry_quote_i]) + float(etf.ask[entry_quote_i])) / 2.0
    deadline_ns = fill_time_ns + max_hold_ns
    target_pnl = profit_target_ticks * tick_size * quantity
    start = int(np.searchsorted(etf.t, fill_time_ns, side="right"))

    def execute(trigger_ns: int, reason: str):
        arrival_ns = trigger_ns + exit_delay_ns
        qi = int(np.searchsorted(etf.t, arrival_ns, side="left") - 1)
        if qi < 0:
            return None
        px = float(etf.bid[qi] if direction > 0 else etf.ask[qi])
        qty = int(etf.bid_sz[qi] if direction > 0 else etf.ask_sz[qi])
        if qty < quantity:
            return None
        pnl = direction * (px - fill_price) * quantity - 2.0 * fee_per_share * quantity
        if reason == "profit_target" and pnl < target_pnl:
            return None
        return {"exit_reason": reason, "exit_order_arrival_ns": arrival_ns,
                "exit_quote_time_ns": int(etf.t[qi]), "exit_price": px,
                "exit_qty": quantity, "realized_pnl": pnl,
                "hold_ms": (arrival_ns - fill_time_ns) / 1e6}

    for i in range(start, len(etf.t)):
        event_ns = int(etf.t[i])
        if event_ns > deadline_ns:
            break
        bid, ask = float(etf.bid[i]), float(etf.ask[i])
        mid = (bid + ask) / 2.0
        adverse_move = direction * (mid - entry_mid)
        if adverse_move <= -adverse_mid_stop_ticks * tick_size:
            result = execute(event_ns, "adverse_mid_stop")
            if result is not None:
                return result
        touch_px = bid if direction > 0 else ask
        touch_pnl = direction * (touch_px - fill_price) * quantity - 2.0 * fee_per_share * quantity
        if touch_pnl >= target_pnl:
            result = execute(event_ns, "profit_target")
            if result is not None:
                return result

    limit_arrival = deadline_ns + exit_delay_ns
    quote_i = int(np.searchsorted(etf.t, limit_arrival, side="left") - 1)
    if quote_i < 0:
        return {"exit_reason": "no_exit_quote", "exit_order_arrival_ns": limit_arrival,
                "exit_quote_time_ns": np.nan, "exit_price": np.nan,
                "exit_qty": 0, "realized_pnl": np.nan,
                "hold_ms": max_hold_ns / 1e6}
    exit_px = float(etf.bid[quote_i] if direction > 0 else etf.ask[quote_i])
    exit_qty = int(etf.bid_sz[quote_i] if direction > 0 else etf.ask_sz[quote_i])
    if exit_qty < quantity:
        return {"exit_reason": "max_hold_insufficient_size", "exit_order_arrival_ns": limit_arrival,
                "exit_quote_time_ns": int(etf.t[quote_i]), "exit_price": exit_px,
                "exit_qty": exit_qty, "realized_pnl": np.nan,
                "hold_ms": max_hold_ns / 1e6}
    pnl = direction * (exit_px - fill_price) * quantity - 2.0 * fee_per_share * quantity
    return {"exit_reason": "max_hold_forced", "exit_order_arrival_ns": limit_arrival,
            "exit_quote_time_ns": int(etf.t[quote_i]), "exit_price": exit_px,
            "exit_qty": quantity, "realized_pnl": pnl,
            "hold_ms": max_hold_ns / 1e6}


def run_symbol(tsla: Quotes, etf: Quotes, raw_etf: np.ndarray, *, day: str,
               symbol: str, leverage: float, quantity: int,
               delay_ns: int, exit_delay_ns: int, edge_buffer_bps: float,
               min_tsla_move_bps: float, signal_mode: str,
               signal_lookback_ns: int,
               max_etf_spread_bps: float | None,
               min_etf_quote_age_us: float | None,
               max_etf_quote_age_us: float | None,
               max_directional_book_pressure_5: float | None,
               start_time_et: str | None, end_time_et: str | None,
               max_arrival_displayed_size: int | None,
               fee_per_share: float, markouts_ms: tuple[int, ...],
               exit_condition: str, max_hold_ns: int,
               tsla_reversal_bps: float, adverse_book_imbalance: float,
               profit_target_ticks: int, adverse_mid_stop_ticks: int):
    smid = (tsla.bid + tsla.ask) / 2.0
    emid = (etf.bid + etf.ask) / 2.0
    joint_time = max(int(tsla.t[0]), int(etf.t[0]))
    si = int(np.searchsorted(tsla.t, joint_time, side="right") - 1)
    ei = int(np.searchsorted(etf.t, joint_time, side="right") - 1)
    if si < 0 or ei < 0:
        return [], [], {"day": day, "symbol": symbol, "tsla_mid_moves": 0,
                        "edge_triggers": 0, "order_attempts": 0, "filled": 0}
    s_anchor, e_anchor = float(smid[si]), float(emid[ei])
    session_date = pd.to_datetime(day, format="%Y%m%d").strftime("%Y-%m-%d")
    window_start_ns = (pd.Timestamp(f"{session_date} {start_time_et}",
                                    tz="America/New_York").value
                       if start_time_et else None)
    window_end_ns = (pd.Timestamp(f"{session_date} {end_time_et}",
                                  tz="America/New_York").value
                     if end_time_et else None)

    # Signals are TSLA mid-price changes only, not displayed-size updates.
    mid_move = np.flatnonzero(np.r_[False, smid[1:] != smid[:-1]])
    book = MBOQueueReplay(raw_etf)
    opportunities, trades = [], []
    zone_previous = 0
    blocked_until = -1
    triggers = attempts = fills = spread_filter_rejects = 0
    size_filter_rejects = quote_age_filter_rejects = 0
    old_quote_filter_rejects = 0
    book_pressure_filter_rejects = 0
    for k in mid_move:
        signal_ns = int(tsla.t[k])
        if ((window_start_ns is not None and signal_ns < window_start_ns)
                or (window_end_ns is not None and signal_ns + delay_ns >= window_end_ns)):
            zone_previous = 0
            continue
        # A wait-profitable position is tracked independently: new signals may
        # open more positions while earlier ones are still waiting to exit.
        if exit_condition == "fixed-markouts" and signal_ns <= blocked_until:
            continue
        # Reconstruct the exact resting ETF book before the TSLA event. This
        # lets the replay distinguish an old quote that survived from a new
        # order later posted at the same price.
        book.advance(signal_ns - 1)
        qidx = int(np.searchsorted(etf.t, signal_ns, side="left") - 1)
        if qidx < 0:
            zone_previous = 0
            continue
        quote_age_us_at_signal = (signal_ns - int(etf.t[qidx])) / 1e3
        if signal_mode == "rolling":
            ref_ns = signal_ns - signal_lookback_ns
            si_ref = int(np.searchsorted(tsla.t, ref_ns, side="right") - 1)
            ei_ref = int(np.searchsorted(etf.t, ref_ns, side="right") - 1)
            if si_ref < 0 or ei_ref < 0:
                zone_previous = 0
                continue
            tsla_ref_mid = float(smid[si_ref])
            etf_ref_mid = float(emid[ei_ref])
        else:
            tsla_ref_mid, etf_ref_mid = s_anchor, e_anchor
        tsla_return_bps = (float(smid[k]) / tsla_ref_mid - 1.0) * 1e4
        fair = _fair_value(float(smid[k]), etf_ref_mid, tsla_ref_mid, leverage)
        if abs(tsla_return_bps) < min_tsla_move_bps:
            zone_previous = 0
            continue
        bid, ask = float(etf.bid[qidx]), float(etf.ask[qidx])
        mid = (bid + ask) / 2.0
        half_spread_bps = ((ask - bid) / 2.0) / mid * 1e4
        etf_spread_bps = 2.0 * half_spread_bps
        if (max_etf_spread_bps is not None
                and etf_spread_bps > max_etf_spread_bps):
            spread_filter_rejects += 1
            zone_previous = 0
            continue
        fee_bps = 2.0 * fee_per_share / mid * 1e4
        buy_edge_bps = (fair / ask - 1.0) * 1e4
        sell_edge_bps = (bid / fair - 1.0) * 1e4
        gate = half_spread_bps + fee_bps + edge_buffer_bps
        zone = 1 if buy_edge_bps >= gate else (-1 if sell_edge_bps >= gate else 0)
        if zone == 0 or zone == zone_previous:
            zone_previous = zone
            continue
        zone_previous = zone
        # Apply quote-age filters after advancing the edge-zone state. This
        # ensures rejecting one crossing does not re-arm repeated entries while
        # the same edge remains active.
        if (min_etf_quote_age_us is not None
                and quote_age_us_at_signal < min_etf_quote_age_us):
            quote_age_filter_rejects += 1
            continue
        if (max_etf_quote_age_us is not None
                and quote_age_us_at_signal > max_etf_quote_age_us):
            old_quote_filter_rejects += 1
            continue
        direction = zone
        pressure = _book_pressure_features(book, direction)
        directional_pressure_5 = pressure["directional_book_pressure_5"]
        if (max_directional_book_pressure_5 is not None
                and (not np.isfinite(directional_pressure_5)
                     or directional_pressure_5 > max_directional_book_pressure_5)):
            book_pressure_filter_rejects += 1
            continue
        triggers += 1
        contra_side = 65 if direction > 0 else 66  # ask side for buy, bid for sell
        trigger_px_i, _ = _best_level(book, contra_side)
        expected_px_i = int(round((ask if direction > 0 else bid) * 1e9))
        if trigger_px_i is None or trigger_px_i != expected_px_i:
            # The independent BBO reconstruction and incremental MBO state
            # must agree before calling a displayed quote stale.
            zone_previous = 0
            continue
        trigger_px = trigger_px_i / 1e9
        trigger_orders = _level_snapshot(book, contra_side, trigger_px_i)
        if not trigger_orders:
            zone_previous = 0
            continue
        gross_edge_bps = buy_edge_bps if direction > 0 else sell_edge_bps
        arrival_ns = signal_ns + delay_ns
        attempts += 1

        row = {"day": day, "symbol": symbol, "signal_time_ns": signal_ns,
               "signal_tsla_mid": float(smid[k]), "signal_tsla_return_bps": tsla_return_bps,
               "etf_quote_age_us_at_signal": quote_age_us_at_signal,
               "signal_mode": signal_mode,
               "signal_lookback_ms": signal_lookback_ns / 1e6,
               "fair_value_at_signal": fair, "direction": "buy" if direction > 0 else "sell",
               "trigger_bid": bid, "trigger_ask": ask, "trigger_limit_price": trigger_px,
               "gross_edge_bps": gross_edge_bps, "etf_spread_bps": etf_spread_bps,
               "estimated_exit_half_spread_bps": half_spread_bps,
               **pressure,
               "fee_allowance_bps": fee_bps, "edge_buffer_bps": edge_buffer_bps,
               "arrival_ns": arrival_ns, "entry_delay_us": delay_ns / 1e3,
               "status": "pending"}

        # Exclude ETF updates at the exact cross-symbol timestamp: ordering is
        # not identified at equal ts_event, even for a zero-delay sensitivity.
        book.advance(arrival_ns - 1)
        surviving_old_qty = _surviving_snapshot_qty(book, contra_side, trigger_px_i,
                                                     trigger_orders)
        best_px_i, displayed_qty_i = _best_level(book, contra_side)
        best_px = (best_px_i / 1e9) if best_px_i is not None else np.nan
        row["arrival_best_price"] = best_px
        row["arrival_displayed_qty"] = displayed_qty_i
        row["trigger_quote_qty"] = int(sum(trigger_orders.values()))
        row["surviving_trigger_quote_qty"] = int(surviving_old_qty)
        row["arrival_quote_time_ns"] = int(etf.t[max(0, np.searchsorted(etf.t, arrival_ns, side="left") - 1)])
        row["etf_quote_age_us_at_arrival"] = (
            arrival_ns - row["arrival_quote_time_ns"]) / 1e3
        if best_px_i is None:
            row["status"] = "no_arrival_quote"
        elif surviving_old_qty < quantity:
            row["status"] = "stale_quote_gone"
        elif direction > 0 and best_px > trigger_px + 1e-9:
            row["status"] = "stale_ask_gone"
        elif direction < 0 and best_px < trigger_px - 1e-9:
            row["status"] = "stale_bid_gone"
        elif displayed_qty_i < quantity:
            row["status"] = "insufficient_displayed_size"
        elif (max_arrival_displayed_size is not None
              and displayed_qty_i > max_arrival_displayed_size):
            row["status"] = "arrival_size_above_cap"
            size_filter_rejects += 1
        else:
            fill_px = best_px
            row["status"] = "filled"
            row["fill_price"] = fill_px
            row["fill_qty"] = quantity
            row["quote_survived_delay"] = True
            fills += 1
            trade = {**row, "fill_time_ns": arrival_ns, "fill_price": fill_px,
                     "fill_qty": quantity, "exit_condition": exit_condition,
                     "max_hold_ms": max_hold_ns / 1e6,
                     "profit_target_ticks": profit_target_ticks,
                     "adverse_mid_stop_ticks": adverse_mid_stop_ticks}
            all_exits = []
            for horizon_ms in markouts_ms:
                exit_target = arrival_ns + int(horizon_ms * 1e6) + exit_delay_ns
                xi = first_future(etf.t, exit_target)
                if xi >= len(etf.t):
                    trade[f"exit_{horizon_ms}ms_price"] = np.nan
                    trade[f"pnl_{horizon_ms}ms"] = np.nan
                    continue
                exit_px = float(etf.bid[xi] if direction > 0 else etf.ask[xi])
                exit_qty = int(etf.bid_sz[xi] if direction > 0 else etf.ask_sz[xi])
                if exit_qty < quantity:
                    trade[f"exit_{horizon_ms}ms_price"] = np.nan
                    trade[f"pnl_{horizon_ms}ms"] = np.nan
                    continue
                pnl = direction * (exit_px - fill_px) * quantity - 2.0 * fee_per_share * quantity
                trade[f"exit_{horizon_ms}ms_time_ns"] = int(etf.t[xi])
                trade[f"exit_{horizon_ms}ms_price"] = exit_px
                trade[f"pnl_{horizon_ms}ms"] = pnl
                all_exits.append(int(etf.t[xi]))
            if exit_condition == "wait-profitable":
                realized = _first_profitable_exit(
                    etf, direction=direction, fill_price=fill_px,
                    fill_time_ns=arrival_ns, quantity=quantity,
                    fee_per_share=fee_per_share, exit_delay_ns=exit_delay_ns,
                    max_hold_ns=max_hold_ns)
                trade.update(realized)
            elif exit_condition == "conditional-adverse":
                realized = _conditional_adverse_exit(
                    tsla, etf, direction=direction, fill_price=fill_px,
                    fill_time_ns=arrival_ns, signal_tsla_mid=float(smid[k]),
                    quantity=quantity, fee_per_share=fee_per_share,
                    exit_delay_ns=exit_delay_ns, max_hold_ns=max_hold_ns,
                    tsla_reversal_bps=tsla_reversal_bps,
                    adverse_book_imbalance=adverse_book_imbalance)
                trade.update(realized)
            elif exit_condition == "target-stop":
                realized = _target_stop_exit(
                    etf, direction=direction, fill_price=fill_px,
                    fill_time_ns=arrival_ns, quantity=quantity,
                    fee_per_share=fee_per_share, exit_delay_ns=exit_delay_ns,
                    max_hold_ns=max_hold_ns,
                    profit_target_ticks=profit_target_ticks,
                    adverse_mid_stop_ticks=adverse_mid_stop_ticks)
                trade.update(realized)
            elif all_exits:
                blocked_until = max(all_exits)
            trades.append(trade)
        opportunities.append(row)

    screening = {"day": day, "symbol": symbol, "tsla_mid_moves": int(len(mid_move)),
                 "edge_triggers": triggers, "order_attempts": attempts, "filled": fills,
                 "entry_delay_us": delay_ns / 1e3,
                 "min_tsla_move_bps": min_tsla_move_bps,
                 "signal_mode": signal_mode,
                 "signal_lookback_ms": signal_lookback_ns / 1e6,
                 "max_etf_spread_bps": max_etf_spread_bps,
                 "min_etf_quote_age_us": min_etf_quote_age_us,
                 "max_etf_quote_age_us": max_etf_quote_age_us,
                 "max_directional_book_pressure_5": max_directional_book_pressure_5,
                 "etf_spread_filter_rejects": spread_filter_rejects,
                 "start_time_et": start_time_et,
                 "end_time_et": end_time_et,
                 "max_arrival_displayed_size": max_arrival_displayed_size,
                 "arrival_size_filter_rejects": size_filter_rejects,
                 "quote_age_filter_rejects": quote_age_filter_rejects,
                 "old_quote_filter_rejects": old_quote_filter_rejects,
                 "book_pressure_filter_rejects": book_pressure_filter_rejects}
    return opportunities, trades, screening


def _process_day(task):
    (day, cache_path, quantity, delay_us, exit_delay_us, edge_buffer_bps,
     min_tsla_move_bps, signal_mode, signal_lookback_ms, max_etf_spread_bps,
     min_etf_quote_age_us, max_etf_quote_age_us,
     max_directional_book_pressure_5,
     start_time_et, end_time_et, max_arrival_displayed_size,
     fee_per_share, markouts_ms, exit_condition, max_hold_ms,
     tsla_reversal_bps, adverse_book_imbalance,
     profit_target_ticks, adverse_mid_stop_ticks) = task
    z = np.load(cache_path, allow_pickle=False)
    date = pd.Timestamp(str(z["day"]))
    tsla = get_quotes(z["TSLA"], date)
    all_opps, all_trades, screens = [], [], []
    for symbol, fund in leadlag.FUNDS.items():
        etf = get_quotes(z[symbol], date)
        opps, trades, screening = run_symbol(
            tsla, etf, z[symbol], day=day, symbol=symbol,
            leverage=fund["L"], quantity=quantity,
            delay_ns=int(delay_us * 1e3), exit_delay_ns=int(exit_delay_us * 1e3),
            edge_buffer_bps=edge_buffer_bps,
            min_tsla_move_bps=min_tsla_move_bps,
            signal_mode=signal_mode,
            signal_lookback_ns=int(signal_lookback_ms * 1e6),
            max_etf_spread_bps=max_etf_spread_bps,
            min_etf_quote_age_us=min_etf_quote_age_us,
            max_etf_quote_age_us=max_etf_quote_age_us,
            max_directional_book_pressure_5=max_directional_book_pressure_5,
            start_time_et=start_time_et, end_time_et=end_time_et,
            max_arrival_displayed_size=max_arrival_displayed_size,
            fee_per_share=fee_per_share,
            markouts_ms=markouts_ms, exit_condition=exit_condition,
            max_hold_ns=int(max_hold_ms * 1e6),
            tsla_reversal_bps=tsla_reversal_bps,
            adverse_book_imbalance=adverse_book_imbalance,
            profit_target_ticks=profit_target_ticks,
            adverse_mid_stop_ticks=adverse_mid_stop_ticks)
        all_opps.extend(opps)
        all_trades.extend(trades)
        screens.append(screening)
    z.close()
    return day, pd.DataFrame(all_opps), pd.DataFrame(all_trades), pd.DataFrame(screens)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start-day", default="20260701")
    ap.add_argument("--end-day", default="20260715")
    ap.add_argument("--skip-date", action="append", default=["20260703"])
    ap.add_argument("--data-dir", type=Path, default=Path("XNAS_MBO"))
    ap.add_argument("--out", type=Path, default=Path("backtests/stale_quote_ioc"))
    ap.add_argument("--quantity", type=int, default=1)
    ap.add_argument("--entry-delay-us", type=float, default=50.0)
    ap.add_argument("--exit-delay-us", type=float, default=50.0)
    ap.add_argument("--edge-buffer-bps", type=float, default=1.0)
    ap.add_argument("--min-tsla-move-bps", type=float, default=10.0,
                    help="Minimum absolute TSLA return for the selected signal mode")
    ap.add_argument("--signal-mode", choices=("session-anchor", "rolling"),
                    default="rolling",
                    help="Measure TSLA return from the session anchor or over a rolling lookback")
    ap.add_argument("--signal-lookback-ms", type=float, default=50.0,
                    help="Lookback window for --signal-mode rolling")
    ap.add_argument("--max-etf-spread-bps", type=float, default=None,
                    help="Skip signals when the ETF BBO spread exceeds this many bps")
    ap.add_argument("--min-etf-quote-age-us", type=float, default=None,
                    help="Skip signals unless the last ETF BBO update is at least this old")
    ap.add_argument("--max-etf-quote-age-us", type=float, default=None,
                    help="Skip signals if the last ETF BBO update is older than this")
    ap.add_argument("--max-directional-book-pressure-5", type=float, default=None,
                    help="Keep signals only when trade-signed ETF top-5 depth imbalance is at/below this value")
    ap.add_argument("--start-time-et", default=None,
                    help="Optional inclusive signal window start in America/New_York time, HH:MM")
    ap.add_argument("--end-time-et", default=None,
                    help="Optional exclusive signal window end in America/New_York time, HH:MM")
    ap.add_argument("--max-arrival-displayed-size", type=int, default=None,
                    help="Skip fills when displayed ETF best-level size at arrival exceeds this cap")
    ap.add_argument("--fee-per-share", type=float, default=0.0)
    ap.add_argument("--exit-condition", choices=("fixed-markouts", "wait-profitable", "conditional-adverse", "target-stop"),
                    default="fixed-markouts")
    ap.add_argument("--max-hold-ms", type=float, default=5000.0,
                    help="Hard position limit for non-fixed exit conditions")
    ap.add_argument("--tsla-reversal-bps", type=float, default=5.0,
                    help="Conditional exit after this adverse TSLA move from the entry signal")
    ap.add_argument("--adverse-book-imbalance", type=float, default=0.2,
                    help="Conditional exit when signed ETF BBO size imbalance is at/below -threshold")
    ap.add_argument("--profit-target-ticks", type=int, default=2,
                    help="For target-stop exits, executable profit target in ETF ticks (1 tick = $0.01)")
    ap.add_argument("--adverse-mid-stop-ticks", type=int, default=1,
                    help="For target-stop exits, stop after this many adverse ETF midprice ticks")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    if a.quantity <= 0 or a.workers < 1:
        ap.error("quantity and workers must be positive")
    from datetime import time as _time
    for name in ("start_time_et", "end_time_et"):
        value = getattr(a, name)
        if value is not None:
            try:
                _time.fromisoformat(value)
            except ValueError:
                ap.error(f"--{name.replace('_', '-')} must be HH:MM")
    if a.start_time_et and a.end_time_et and a.start_time_et >= a.end_time_et:
        ap.error("--start-time-et must be earlier than --end-time-et")
    if a.max_arrival_displayed_size is not None and a.max_arrival_displayed_size < a.quantity:
        ap.error("--max-arrival-displayed-size must be at least --quantity")
    if a.signal_mode == "rolling" and a.signal_lookback_ms <= 0:
        ap.error("--signal-lookback-ms must be positive for rolling signals")
    if (a.min_etf_quote_age_us is not None and a.max_etf_quote_age_us is not None
            and a.min_etf_quote_age_us > a.max_etf_quote_age_us):
        ap.error("minimum ETF quote age cannot exceed maximum ETF quote age")
    nonnegative = [a.entry_delay_us, a.exit_delay_us, a.edge_buffer_bps,
                   a.min_tsla_move_bps, a.signal_lookback_ms,
                   a.fee_per_share, a.max_hold_ms,
                   a.tsla_reversal_bps, a.adverse_book_imbalance]
    if a.max_etf_spread_bps is not None:
        nonnegative.append(a.max_etf_spread_bps)
    if a.min_etf_quote_age_us is not None:
        nonnegative.append(a.min_etf_quote_age_us)
    if a.max_etf_quote_age_us is not None:
        nonnegative.append(a.max_etf_quote_age_us)
    if min(nonnegative) < 0:
        ap.error("delays, thresholds, fee, and max hold must be nonnegative")
    if (a.max_directional_book_pressure_5 is not None
            and not -1.0 <= a.max_directional_book_pressure_5 <= 1.0):
        ap.error("--max-directional-book-pressure-5 must be between -1 and 1")
    if a.profit_target_ticks <= 0 or a.adverse_mid_stop_ticks <= 0:
        ap.error("profit target and adverse mid stop ticks must be positive")
    markouts_ms = MARKOUT_HORIZONS_MS
    tasks = []
    for date in pd.date_range(pd.to_datetime(a.start_day), pd.to_datetime(a.end_day), freq="B"):
        day = date.strftime("%Y%m%d")
        if day in a.skip_date:
            continue
        cache = a.data_dir / f"xnas-itch-{day}.mbo.dbn.zst.TSLA-TSLL-TSLR-TSLG.npz"
        if cache.exists():
            tasks.append((day, str(cache), a.quantity, a.entry_delay_us,
                          a.exit_delay_us, a.edge_buffer_bps,
                          a.min_tsla_move_bps, a.signal_mode,
                          a.signal_lookback_ms, a.max_etf_spread_bps,
                          a.min_etf_quote_age_us, a.max_etf_quote_age_us,
                          a.max_directional_book_pressure_5,
                          a.start_time_et, a.end_time_et,
                          a.max_arrival_displayed_size,
                          a.fee_per_share,
                          markouts_ms, a.exit_condition,
                          a.max_hold_ms, a.tsla_reversal_bps,
                          a.adverse_book_imbalance, a.profit_target_ticks,
                          a.adverse_mid_stop_ticks))
        else:
            print(f"Skipping missing cache {cache}")

    opps_all, trades_all, screens_all = [], [], []
    for day, opps, trades, screens in run_sessions(_process_day, tasks, a.workers):
        if not opps.empty:
            opps_all.append(opps)
        if not trades.empty:
            trades_all.append(trades)
        if not screens.empty:
            screens_all.append(screens)
        print(f"Processed {day}", flush=True)
    a.out.mkdir(parents=True, exist_ok=True)
    opps = pd.concat(opps_all, ignore_index=True) if opps_all else pd.DataFrame()
    trades = pd.concat(trades_all, ignore_index=True) if trades_all else pd.DataFrame()
    screens = pd.concat(screens_all, ignore_index=True) if screens_all else pd.DataFrame()
    opps.to_csv(a.out / "opportunities.csv", index=False)
    trades.to_csv(a.out / "trades.csv", index=False)
    screens.to_csv(a.out / "signal_screening_by_day.csv", index=False)
    if not trades.empty:
        long_rows = []
        for horizon in markouts_ms:
            pnl_col = f"pnl_{horizon}ms"
            px_col = f"exit_{horizon}ms_price"
            sample = trades[trades[pnl_col].notna()]
            for symbol, group in sample.groupby("symbol"):
                p = group[pnl_col]
                long_rows.append({"symbol": symbol, "horizon_ms": horizon,
                                  "trades": int(len(group)), "wins": int((p > 0).sum()),
                                  "win_rate": float((p > 0).mean()), "net_pnl": float(p.sum()),
                                  "mean_pnl": float(p.mean()), "median_pnl": float(p.median()),
                                  "mean_exit_price": float(group[px_col].mean())})
        summary = pd.DataFrame(long_rows)
    else:
        summary = pd.DataFrame(columns=["symbol", "horizon_ms", "trades", "wins", "win_rate", "net_pnl"])
    summary.to_csv(a.out / "summary_by_horizon.csv", index=False)
    if not trades.empty and "realized_pnl" in trades.columns:
        realized = trades[trades.realized_pnl.notna()]
        realized_summary = realized.groupby(["symbol", "exit_reason"], as_index=False).agg(
            trades=("realized_pnl", "size"), wins=("realized_pnl", lambda x: int((x > 0).sum())),
            net_pnl=("realized_pnl", "sum"), mean_pnl=("realized_pnl", "mean"),
            mean_hold_ms=("hold_ms", "mean"), median_hold_ms=("hold_ms", "median"))
        realized_summary.to_csv(a.out / "realized_exit_summary.csv", index=False)
    else:
        realized_summary = pd.DataFrame()
    if not opps.empty:
        funnel = opps.groupby(["symbol", "status"], as_index=False).size().rename(columns={"size": "orders"})
    else:
        funnel = pd.DataFrame(columns=["symbol", "status", "orders"])
    funnel.to_csv(a.out / "opportunity_funnel.csv", index=False)
    print("\nFill funnel:")
    print(funnel.to_string(index=False))
    print("\nMarkout summary:")
    print(summary.to_string(index=False))
    if not realized_summary.empty:
        print("\nRealized exit summary:")
        print(realized_summary.to_string(index=False))
    print(f"\nSaved results under {a.out}")


if __name__ == "__main__":
    main()
