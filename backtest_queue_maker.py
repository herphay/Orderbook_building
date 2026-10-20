#!/usr/bin/env python3
"""Exploratory queue-aware maker backtest for TSLA / 2x TSLA ETFs.

Uses an opening-anchored ETF/TSLA residual to choose the pair side and TSLA
top-of-book imbalance as a timing filter. It inserts a shadow order into a
time-ordered replay of the ETF's MBO book, tracks visible orders ahead by ID,
and counts fills only after executions reach orders behind the shadow order.
It remains an approximate research simulator: arrival latency is assumed,
partial fills within one order are aggregated at their last fill time, and
hidden liquidity and the shadow order's market impact are not modeled.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from utils import leadlag
from backtest import Quotes, rth_quotes, first_future
from utils.parallel import run_sessions


def get_quotes(rec: np.ndarray, day: pd.Timestamp) -> Quotes:
    bbo = leadlag.build_bbo(rec).sort_values("ts_event", kind="stable")
    bbo = bbo.drop_duplicates("ts_event", keep="last")
    q = Quotes(bbo.ts_event.to_numpy(np.int64), bbo.bid.to_numpy(float),
               bbo.ask.to_numpy(float), bbo.bid_sz.to_numpy(np.int64),
               bbo.ask_sz.to_numpy(np.int64))
    good = np.isfinite(q.bid) & np.isfinite(q.ask) & (q.bid > 0) & (q.ask > q.bid)
    return rth_quotes(Quotes(q.t[good], q.bid[good], q.ask[good],
                             q.bid_sz[good], q.ask_sz[good]), day)


class MBOQueueReplay:
    """Replay one ETF's order-by-order book and insert a shadow maker order."""

    def __init__(self, records: np.ndarray):
        # Preserve DBN message/sequence order, including events sharing a
        # timestamp. Repair only a timestamp reversal in a cached record array.
        times = records["ts_event"].astype(np.int64, copy=False)
        if len(times) > 1 and np.any(times[1:] < times[:-1]):
            records = records[np.argsort(times, kind="stable")]
        self.records = records
        self.times = self.records["ts_event"].astype(np.int64, copy=False)
        self.cursor = 0
        self.orders: dict[int, dict] = {}
        self.levels: dict[tuple[int, int], dict[int, int]] = {}

    def _remove(self, oid: int, active: dict | None, *, replacement: bool = False) -> None:
        state = self.orders.pop(oid, None)
        if state is None:
            return
        key = (state["side"], state["price"])
        level = self.levels.get(key)
        if level is not None:
            level.pop(oid, None)
            if not level:
                self.levels.pop(key, None)
        if active is not None and oid in active["ahead"]:
            # F executions are accounted at F; the following C or replace only
            # removes the still-resting remainder from the displayed queue.
            resting = max(0, state["size"] - state["pending_exec"])
            active["ahead_remaining"] = max(0, active["ahead_remaining"] - resting)
            active["ahead"].pop(oid, None)

    def _event(self, i: int, active: dict | None) -> bool:
        row = self.records[i]
        action, side = int(row["action"]), int(row["side"])
        oid, px, size = int(row["order_id"]), int(row["price"]), int(row["size"])
        if action == 82:  # clear book
            self.orders.clear()
            self.levels.clear()
            if active is not None:
                active["ahead"].clear()
                active["ahead_remaining"] = 0
            return False
        if action == 65 or action == 77:  # add / replace
            self._remove(oid, active, replacement=True)
            if size > 0 and side in (65, 66) and px != np.iinfo(np.int64).max:
                state = {"side": side, "price": px, "size": size, "pending_exec": 0}
                self.orders[oid] = state
                self.levels.setdefault((side, px), {})[oid] = size
            return False
        if action == 67:  # cancel; execution quantity is reported first as F
            state = self.orders.get(oid)
            if state is None:
                return False
            cancelled = size if 0 < size <= state["size"] else state["size"]
            executed_already = min(state["pending_exec"], cancelled)
            cancel_only = cancelled - executed_already
            state["size"] -= cancelled
            state["pending_exec"] -= executed_already
            key = (state["side"], state["price"])
            level = self.levels.get(key)
            if level is not None:
                if state["size"] > 0:
                    level[oid] = state["size"]
                else:
                    level.pop(oid, None)
                    if not level:
                        self.levels.pop(key, None)
            if active is not None and oid in active["ahead"]:
                active["ahead_remaining"] = max(0, active["ahead_remaining"] - cancel_only)
                if state["size"] <= 0:
                    active["ahead"].pop(oid, None)
            if state["size"] <= 0:
                self.orders.pop(oid, None)
            return False
        if action == 70:  # Fill, keyed to the resting order; T is its duplicate print
            state = self.orders.get(oid)
            if state is None:
                return False
            executed = min(size, max(0, state["size"] - state["pending_exec"]))
            if executed <= 0:
                return False
            if active is not None and side == active["side"] and px == active["price"]:
                active["contra_volume"] += executed
                if oid in active["ahead"]:
                    active["ahead_remaining"] = max(0, active["ahead_remaining"] - executed)
                    active["ahead"][oid] = max(0, active["ahead"][oid] - executed)
                    if active["ahead"][oid] == 0:
                        active["ahead"].pop(oid, None)
                else:
                    # A print against a post-arrival order at this price proves
                    # that the displayed queue behind our shadow order traded.
                    lot_qty = min(executed, active["requested_qty"] - active["filled_qty"])
                    active["filled_qty"] += lot_qty
                    if lot_qty > 0:
                        active["fill_events"].append((int(row["ts_event"]), lot_qty))
                        active["last_fill_time_ns"] = int(row["ts_event"])
                    if active["filled_qty"] >= active["requested_qty"]:
                        active["fill_time_ns"] = int(row["ts_event"])
                        state["pending_exec"] += executed
                        return True
            state["pending_exec"] += executed
        return False

    def advance(self, until_ns: int, active: dict | None = None) -> int | None:
        while self.cursor < len(self.records) and self.times[self.cursor] <= until_ns:
            i = self.cursor
            self.cursor += 1
            if self._event(i, active):
                return int(self.times[i])
        return None

    def insert(self, side: int, price: int, quantity: int) -> dict:
        level = self.levels.get((side, price), {})
        ahead = {oid: max(0, self.orders[oid]["size"] - self.orders[oid]["pending_exec"])
                 for oid in level if oid in self.orders}
        ahead = {oid: size for oid, size in ahead.items() if size > 0}
        return {"side": side, "price": price, "requested_qty": quantity,
                "filled_qty": 0, "fill_time_ns": None, "ahead": ahead,
                "fill_events": [], "last_fill_time_ns": None,
                "ahead_initial": sum(ahead.values()),
                "ahead_remaining": sum(ahead.values()), "contra_volume": 0}


def run_pair(tsla: Quotes, etf: Quotes, raw_etf: np.ndarray, *, symbol: str,
             leverage: float, qty: int, imbalance_cut: float,
             signal_mode: str = "imbalance", signal_lookback_ns: int = 100_000_000,
             signal_threshold: float = 5e-4,
             trade_mode: str = "pair",
             cancel_on_signal_fade: bool = True,
             max_quote_wait_ns: int, hold_ns: int, latency_ns: int,
             exchange_entry_delay_ns: int = 50_000,
             fee_per_share: float, edge_buffer_bps: float,
             min_residual_bps: float, quote_offset_ticks: int,
             hedge_latency_ns: int | None = None,
             cancel_latency_ns: int = 0,
             exit_latency_ns: int | None = None,
             return_candidates: bool = False):
    hedge_latency_ns = latency_ns if hedge_latency_ns is None else hedge_latency_ns
    exit_latency_ns = latency_ns if exit_latency_ns is None else exit_latency_ns
    smid, emid = (tsla.bid + tsla.ask) / 2, (etf.bid + etf.ask) / 2
    denom = tsla.bid_sz + tsla.ask_sz
    imb = np.divide(tsla.bid_sz - tsla.ask_sz, denom,
                    out=np.zeros(len(denom), float), where=denom > 0)
    if signal_mode == "imbalance":
        signal_values = imb
        signal_cut = imbalance_cut
        active = np.where(signal_values >= signal_cut, 1,
                          np.where(signal_values <= -signal_cut, -1, 0))
    elif signal_mode == "leadlag":
        ref_idx = np.searchsorted(tsla.t, tsla.t - signal_lookback_ns, side="right") - 1
        signal_values = np.zeros(len(tsla.t), dtype=float)
        valid_ref = ref_idx >= 0
        mid = (tsla.bid + tsla.ask) / 2
        signal_values[valid_ref] = mid[valid_ref] / mid[ref_idx[valid_ref]] - 1.0
        signal_cut = signal_threshold
        active = np.where(signal_values >= signal_cut, 1,
                          np.where(signal_values <= -signal_cut, -1, 0))
    else:
        raise ValueError("signal_mode must be 'imbalance' or 'leadlag'")
    if trade_mode not in ("pair", "etf-only"):
        raise ValueError("trade_mode must be 'pair' or 'etf-only'")
    signals = np.flatnonzero((active != 0) & (active != np.r_[0, active[:-1]]))
    anchor_time = max(int(tsla.t[0]), int(etf.t[0]))
    s0 = int(np.searchsorted(tsla.t, anchor_time, side="right") - 1)
    e0 = int(np.searchsorted(etf.t, anchor_time, side="right") - 1)
    if s0 < 0 or e0 < 0:
        empty = pd.DataFrame()
        return (empty, empty, {"eligible_signals": 0, "cost_filtered": 0}) if return_candidates else empty
    anchor_smid, anchor_emid = smid[s0], emid[e0]

    replay = MBOQueueReplay(raw_etf)
    rows, candidate_rows, blocked_until = [], [], -1
    signal_counts = {"eligible_signals": 0, "cost_filtered": 0}
    for k in signals:
        st = int(tsla.t[k])
        if st <= blocked_until:
            continue
        qi = int(np.searchsorted(etf.t, st, side="right") - 1)
        if qi < 0:
            continue
        # Residual selects the pair side; the selected TSLA signal confirms timing.
        fair = anchor_emid * (1.0 + leverage * (smid[k] / anchor_smid - 1.0))
        if fair <= 0:
            continue
        gap_bps = (emid[qi] / fair - 1.0) * 1e4
        if trade_mode == "etf-only":
            # The TSLA return predicts ETF direction; do not require a residual
            # and do not open a TSLA hedge.
            direction = int(active[k])
        else:
            direction = 1 if gap_bps < 0 else -1  # discount -> long ETF; premium -> short
            if int(active[k]) != direction:
                continue
        if trade_mode == "pair" and abs(gap_bps) < min_residual_bps:
            continue
        tick = leadlag.TICK * quote_offset_ticks
        limit_px = (etf.bid[qi] + tick if direction > 0
                    else etf.ask[qi] - tick)
        if (direction > 0 and limit_px >= etf.ask[qi]) or (direction < 0 and limit_px <= etf.bid[qi]):
            continue  # would cross or lock the opposing touch
        signal_counts["eligible_signals"] += 1
        est_hqty = (max(1, int(round(leverage * limit_px * qty / smid[k])))
                    if trade_mode == "pair" else 0)
        etf_exit_half_spread_bps = (etf.ask[qi] - etf.bid[qi]) / emid[qi] * 5e3
        tsla_hedge_roundtrip_bps = (0.0 if trade_mode == "etf-only" else
                                    leverage * (tsla.ask[k] - tsla.bid[k]) / smid[k] * 1e4)
        fee_bps = 2 * fee_per_share * (qty + est_hqty) / (limit_px * qty) * 1e4
        estimated_cost_bps = etf_exit_half_spread_bps + tsla_hedge_roundtrip_bps + fee_bps
        edge_bps = (abs(float(signal_values[k]) * 1e4) if trade_mode == "etf-only"
                    else abs(gap_bps))
        candidate = {
            "day": pd.Timestamp(tsla.t[k], unit="ns", tz="UTC").tz_convert("America/New_York").strftime("%Y%m%d"),
            "symbol": symbol, "signal_time_ns": st, "signal_imbalance": float(imb[k]),
            "signal_mode": signal_mode, "signal_value": float(signal_values[k]),
            "residual_bps_at_signal": float(gap_bps), "direction": "buy" if direction > 0 else "sell",
            "limit_price": float(limit_px), "requested_qty": int(qty),
            "initial_displayed_queue": np.nan,
            "queue_ahead_shares": np.nan,
            "estimated_roundtrip_cost_bps": float(estimated_cost_bps),
            "order_latency_ms": exchange_entry_delay_ns / 1e6,
            "cancel_latency_ms": cancel_latency_ns / 1e6,
            "hedge_latency_ms": hedge_latency_ns / 1e6,
            "exit_latency_ms": exit_latency_ns / 1e6,
            "order_sent": False, "candidate_status": "cost_filtered",
            "partial_fill_qty": 0,
        }
        if edge_bps <= estimated_cost_bps + edge_buffer_bps:
            signal_counts["cost_filtered"] += 1
            continue
        # Advance the exchange book through the modeled arrival time before
        # inserting our shadow order behind orders already resting at the price.
        arrive = st + exchange_entry_delay_ns
        deadline = arrive + max_quote_wait_ns
        replay.advance(arrive)
        arrival_quote_i = int(np.searchsorted(etf.t, arrive, side="right") - 1)
        candidate["order_arrival_ns"] = arrive
        if arrival_quote_i < 0:
            candidate["candidate_status"] = "no_arrival_quote"
            candidate_rows.append(candidate)
            blocked_until = deadline
            continue
        # If the planned passive price became stale before the order arrived,
        # inspect only BBO updates timestamped at or before arrival. A strictly
        # later quote is a post-arrival event, even when delay is zero.
        pre_start = qi + 1
        pre_end = arrival_quote_i + 1
        if direction > 0:
            stale_before_arrival = ((etf.bid[pre_start:pre_end] > limit_px)
                                    | (etf.ask[pre_start:pre_end] <= limit_px))
        else:
            stale_before_arrival = ((etf.ask[pre_start:pre_end] < limit_px)
                                    | (etf.bid[pre_start:pre_end] >= limit_px))
        if np.any(stale_before_arrival):
            candidate["candidate_status"] = "stale_before_arrival"
            candidate["cancel_reason"] = "stale_before_arrival"
            candidate["stale_quote_ns"] = int(etf.t[pre_start:pre_end][stale_before_arrival][0])
            candidate_rows.append(candidate)
            blocked_until = arrive
            continue
        # A signal that fades while the order is in transit should not create
        # a posted quote that is cancelled retroactively before its arrival.
        pre_sleft = int(np.searchsorted(tsla.t, st, side="right"))
        pre_sright = int(np.searchsorted(tsla.t, arrive, side="right"))
        pre_arrival_fade = (active[pre_sleft:pre_sright] != direction
                            if cancel_on_signal_fade else
                            np.zeros(pre_sright - pre_sleft, dtype=bool))
        if np.any(pre_arrival_fade):
            fade_time = int(tsla.t[pre_sleft:pre_sright][pre_arrival_fade][0])
            candidate["candidate_status"] = "signal_faded_before_arrival"
            candidate["cancel_reason"] = "tsla_imbalance_faded_before_arrival"
            candidate["signal_fade_ns"] = fade_time
            candidate["time_to_signal_fade_ms"] = (fade_time - st) / 1e6
            candidate_rows.append(candidate)
            blocked_until = arrive
            continue
        # Count as sent only once a fresh, executable quote is available at
        # the simulated arrival time. Earlier rows remain candidate attempts.
        candidate["order_sent"] = True
        book_side = 66 if direction > 0 else 65
        limit_px_nano = int(round(limit_px * 1e9))
        shadow = replay.insert(book_side, limit_px_nano, qty)
        queue_ahead = int(shadow["ahead_initial"])
        candidate["initial_displayed_queue"] = queue_ahead
        candidate["queue_ahead_shares"] = queue_ahead
        candidate["arrival_mid"] = float(emid[arrival_quote_i])
        candidate["entry_implementation_shortfall"] = float(
            direction * (limit_px - emid[arrival_quote_i]) * qty)
        # Find when a quote or confirming TSLA signal first invalidates the
        # passive order. The shadow order can fill during cancel transit.
        eleft = int(np.searchsorted(etf.t, arrive, side="right"))
        eright = int(np.searchsorted(etf.t, deadline, side="right"))
        if direction > 0:
            same_side_changed = etf.bid[eleft:eright] > limit_px
            opposite_crossed = etf.ask[eleft:eright] <= limit_px
        else:
            same_side_changed = etf.ask[eleft:eright] < limit_px
            opposite_crossed = etf.bid[eleft:eright] >= limit_px
        sleft = int(np.searchsorted(tsla.t, arrive, side="right"))
        sright = int(np.searchsorted(tsla.t, deadline, side="right"))
        faded = (active[sleft:sright] != direction if cancel_on_signal_fade else
                 np.zeros(sright - sleft, dtype=bool))
        trigger_times = {}
        for reason, mask, times in (
            ("etf_same_side_reprice", same_side_changed, etf.t[eleft:eright]),
            ("etf_opposite_touch_cross", opposite_crossed, etf.t[eleft:eright]),
            (f"tsla_{signal_mode}_signal_fade", faded, tsla.t[sleft:sright]),
        ):
            hits = times[mask]
            if len(hits):
                trigger_times[reason] = int(hits[0])
        cancel_at = min(trigger_times.values()) if trigger_times else None
        cancel_reasons = ([reason for reason, when in trigger_times.items() if when == cancel_at]
                          if cancel_at is not None else [])
        cancel_reason = "+".join(cancel_reasons) if cancel_reasons else None
        cancel_effective = (int(cancel_at) + cancel_latency_ns
                            if cancel_at is not None else deadline)
        fill_time = replay.advance(cancel_effective, shadow)
        cum = int(shadow["contra_volume"])
        queue_ahead_remaining = int(shadow["ahead_remaining"])
        candidate["queue_ahead_at_cancel"] = queue_ahead_remaining
        candidate["queue_volume_needed"] = queue_ahead + qty
        partial_qty = int(shadow["filled_qty"]) if fill_time is None else 0
        candidate["partial_fill_qty"] = partial_qty
        is_partial = fill_time is None and partial_qty > 0
        if fill_time is None:
            candidate["candidate_status"] = "cancelled" if cancel_at is not None else "quote_timeout"
            candidate["cancel_reason"] = cancel_reason or "quote_timeout"
            candidate["cancel_trigger_ns"] = int(cancel_at) if cancel_at is not None else None
            candidate["cancel_effective_ns"] = cancel_effective if cancel_at is not None else None
            candidate["contra_volume"] = cum
            candidate["queue_progress_ratio"] = cum / max(1, queue_ahead + qty)
            candidate["time_to_cancel_trigger_ms"] = (
                (int(cancel_at) - arrive) / 1e6 if cancel_at is not None else np.nan)
            candidate["order_live_duration_ms"] = (
                (cancel_effective if cancel_at is not None else deadline) - arrive) / 1e6
            if not is_partial:
                # Ex-post counterfactual, not realized P&L: if the passive order
                # had filled at arrival, mark the pair after the hold horizon.
                hi_cf = first_future(tsla.t, arrive + hedge_latency_ns)
                mi_cf = first_future(etf.t, arrive + hold_ns)
                si_cf = first_future(tsla.t, arrive + hold_ns)
                if hi_cf < len(tsla.t) and mi_cf < len(etf.t) and si_cf < len(tsla.t):
                    hedge_dir = -direction
                    hqty_cf = max(1, int(round(leverage * limit_px * qty / smid[hi_cf])))
                    hedge_px_cf = tsla.ask[hi_cf] if hedge_dir > 0 else tsla.bid[hi_cf]
                    candidate["unfilled_counterfactual_markout"] = float(
                        direction * (emid[mi_cf] - limit_px) * qty
                        if trade_mode == "etf-only" else
                        direction * (emid[mi_cf] - limit_px) * qty
                        + hedge_dir * (smid[si_cf] - hedge_px_cf) * hqty_cf)
                candidate_rows.append(candidate)
                # Keep the pair blocked while its unfilled quote is canceling.
                blocked_until = cancel_effective if cancel_at is not None else deadline
                continue
            fill_time = int(shadow["last_fill_time_ns"])
            trade_qty = partial_qty
            candidate["candidate_status"] = "partially_filled"
        else:
            fill_time = int(fill_time)
            trade_qty = qty
        # Hedge only after ETF fill is observable and hedge latency has elapsed.
        if trade_mode == "pair":
            hi = first_future(tsla.t, fill_time + hedge_latency_ns)
            if hi >= len(tsla.t):
                candidate["candidate_status"] = "filled_unhedgeable"
                candidate["fill_time_ns"] = fill_time
                candidate["contra_volume"] = cum
                candidate_rows.append(candidate)
                continue
            hqty = max(1, int(round(leverage * limit_px * trade_qty / smid[hi])))
            hedge_dir = -direction
            hedge_entry_px = tsla.ask[hi] if hedge_dir > 0 else tsla.bid[hi]
            hedge_entry_mid = float(smid[hi])
        else:
            hi = -1
            hqty = 0
            hedge_dir = 0
            hedge_entry_px = np.nan
            hedge_entry_mid = np.nan
        hold_target = fill_time + hold_ns
        ex_e = first_future(etf.t, hold_target + exit_latency_ns)
        ex_s = first_future(tsla.t, hold_target + exit_latency_ns) if trade_mode == "pair" else -1
        if ex_e >= len(etf.t) or (trade_mode == "pair" and ex_s >= len(tsla.t)):
            candidate["candidate_status"] = "filled_no_exit_quote"
            candidate["fill_time_ns"] = fill_time
            candidate["contra_volume"] = cum
            candidate_rows.append(candidate)
            continue
        # Exit both legs aggressively at the opposing top of book.
        etf_exit_px = etf.bid[ex_e] if direction > 0 else etf.ask[ex_e]
        hedge_exit_px = ((tsla.bid[ex_s] if hedge_dir > 0 else tsla.ask[ex_s])
                         if trade_mode == "pair" else np.nan)
        etf_pnl = direction * (etf_exit_px - limit_px) * trade_qty
        hedge_pnl = (hedge_dir * (hedge_exit_px - hedge_entry_px) * hqty
                     if trade_mode == "pair" else 0.0)
        fees = 2 * fee_per_share * (trade_qty + hqty)
        mid_exit_etf = emid[ex_e]
        mid_exit_tsla = smid[ex_s] if trade_mode == "pair" else np.nan
        mid_pair_pnl = (direction * (mid_exit_etf - limit_px) * trade_qty
                        + (hedge_dir * (mid_exit_tsla - smid[hi]) * hqty
                           if trade_mode == "pair" else 0.0))
        rows.append({
            "symbol": symbol, "trade_mode": trade_mode,
            "signal_time_ns": st, "signal_imbalance": imb[k],
            "signal_mode": signal_mode, "signal_value": signal_values[k],
            "residual_bps_at_signal": gap_bps,
            "estimated_roundtrip_cost_bps": estimated_cost_bps,
            "direction": (("long_etf_short_tsla" if direction > 0 else "short_etf_long_tsla")
                          if trade_mode == "pair" else
                          ("long_etf_only" if direction > 0 else "short_etf_only")),
            "etf_limit_price": limit_px, "queue_ahead_shares": queue_ahead,
            "quote_offset_ticks": quote_offset_ticks,
            "order_latency_ms": exchange_entry_delay_ns / 1e6,
            "cancel_latency_ms": cancel_latency_ns / 1e6,
            "hedge_latency_ms": hedge_latency_ns / 1e6,
            "exit_latency_ms": exit_latency_ns / 1e6,
            "order_arrival_ns": arrive,
            "order_arrival_mid": float(emid[arrival_quote_i]),
            "entry_implementation_shortfall": float(direction * (limit_px-emid[arrival_quote_i])*trade_qty),
            "etf_fill_mid": float(emid[first_future(etf.t, fill_time)]),
            "trade_volume_to_fill": cum, "etf_fill_time_ns": fill_time,
            "requested_qty": qty, "etf_qty": trade_qty, "partial_fill": is_partial,
            "tsla_hedge_qty": hqty,
            "tsla_hedge_entry_mid": hedge_entry_mid,
            "tsla_hedge_entry_price": hedge_entry_px,
            "etf_exit_time_ns": int(etf.t[ex_e]),
            "tsla_exit_time_ns": int(tsla.t[ex_s]) if trade_mode == "pair" else np.nan,
            "etf_exit_mid": float(mid_exit_etf), "tsla_exit_mid": float(mid_exit_tsla),
            "etf_exit_price": etf_exit_px, "tsla_hedge_exit_price": hedge_exit_px,
            "pair_pnl_marked_to_mid": mid_pair_pnl,
            "pair_pnl_at_simulated_fills": etf_pnl + hedge_pnl,
            "pnl_marked_to_mid": mid_pair_pnl,
            "pnl_at_simulated_fills": etf_pnl + hedge_pnl,
            "fees": fees, "net_pnl": etf_pnl + hedge_pnl - fees,
        })
        if not is_partial:
            candidate["candidate_status"] = "filled"
            candidate["cancel_reason"] = "filled_before_cancel"
        candidate["fill_time_ns"] = fill_time
        candidate["contra_volume"] = cum
        candidate["queue_volume_needed"] = queue_ahead + qty
        candidate["queue_progress_ratio"] = cum / max(1, queue_ahead + qty)
        candidate["time_to_fill_ms"] = (fill_time - arrive) / 1e6
        candidate["cancel_trigger_ns"] = int(cancel_at) if cancel_at is not None else None
        candidate["cancel_effective_ns"] = cancel_effective if cancel_at is not None else None
        candidate_rows.append(candidate)
        blocked_until = max(int(etf.t[ex_e]), int(tsla.t[ex_s]))
    if return_candidates:
        return pd.DataFrame(rows), pd.DataFrame(candidate_rows), signal_counts
    return pd.DataFrame(rows)


def _process_day(task: tuple) -> tuple[str, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    (day, path_str, quantity, imbalance, quote_wait_ms, hold_ms,
     signal_mode, signal_lookback_ms, signal_threshold_bps, trade_mode,
     cancel_on_signal_fade,
     exchange_entry_delay_us, latency_ms,
     fee_per_share, edge_buffer_bps, min_residual_bps, quote_offset_ticks,
     cancel_latency_ms, hedge_latency_ms,
     exit_latency_ms) = task
    z = np.load(path_str, allow_pickle=False)
    date = pd.Timestamp(str(z["day"]))
    tsla = get_quotes(z["TSLA"], date)
    results, candidates, screenings = [], [], []
    for sym, fund in leadlag.FUNDS.items():
        etf = get_quotes(z[sym], date)
        trades, candidate_log, screen_counts = run_pair(tsla, etf, z[sym], symbol=sym, leverage=fund["L"],
                          qty=quantity, imbalance_cut=imbalance,
                          signal_mode=signal_mode,
                          signal_lookback_ns=int(signal_lookback_ms * 1e6),
                          signal_threshold=signal_threshold_bps / 1e4,
                          trade_mode=trade_mode,
                          cancel_on_signal_fade=cancel_on_signal_fade,
                          max_quote_wait_ns=int(quote_wait_ms * 1e6),
                          hold_ns=int(hold_ms * 1e6), latency_ns=int(latency_ms * 1e6),
                          exchange_entry_delay_ns=int(exchange_entry_delay_us * 1e3),
                          fee_per_share=fee_per_share, edge_buffer_bps=edge_buffer_bps,
                          min_residual_bps=min_residual_bps,
                          quote_offset_ticks=quote_offset_ticks,
                          cancel_latency_ns=int(cancel_latency_ms * 1e6),
                          hedge_latency_ns=(None if hedge_latency_ms is None else int(hedge_latency_ms * 1e6)),
                          exit_latency_ns=(None if exit_latency_ms is None else int(exit_latency_ms * 1e6)),
                          return_candidates=True)
        screenings.append({"day": day, "symbol": sym, **screen_counts})
        if not trades.empty:
            trades.insert(0, "day", day)
            results.append(trades)
        if not candidate_log.empty:
            candidates.append(candidate_log)
    combined = pd.concat(results, ignore_index=True) if results else pd.DataFrame()
    combined_candidates = pd.concat(candidates, ignore_index=True) if candidates else pd.DataFrame()
    return day, combined, combined_candidates, pd.DataFrame(screenings)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start-day", default="20261005")
    ap.add_argument("--end-day", default="20261009")
    ap.add_argument("--data-dir", type=Path, default=Path("XNAS_MBO"))
    ap.add_argument("--out", type=Path, default=Path("backtests/queue_maker"))
    ap.add_argument("--quantity", type=int, default=100)
    ap.add_argument("--signal-mode", choices=("imbalance", "leadlag"), default="imbalance",
                    help="TSLA top-book imbalance (default) or short-horizon TSLA return trigger")
    ap.add_argument("--imbalance", type=float, default=0.7)
    ap.add_argument("--signal-lookback-ms", type=float, default=100.0,
                    help="TSLA return lookback when --signal-mode leadlag")
    ap.add_argument("--signal-threshold-bps", type=float, default=5.0,
                    help="Absolute TSLA return trigger when --signal-mode leadlag")
    ap.add_argument("--trade-mode", choices=("pair", "etf-only"), default="pair",
                    help="Hedge ETF fills with TSLA (default) or trade the ETF alone")
    ap.add_argument("--keep-order-on-signal-fade", action="store_true",
                    help="Treat the TSLA threshold crossing as a one-shot trigger; keep the order live until an ETF quote invalidates it or it times out")
    ap.add_argument("--quote-wait-ms", type=float, default=1000.0)
    ap.add_argument("--hold-ms", type=float, default=1000.0)
    ap.add_argument("--exchange-entry-delay-us", type=float, default=50.0,
                    help="Signal-to-exchange order arrival delay in microseconds")
    ap.add_argument("--latency-ms", type=float, default=1.0,
                    help="Fallback delay for hedge and exit; entry uses --exchange-entry-delay-us")
    ap.add_argument("--cancel-latency-ms", type=float, default=0.0,
                    help="Delay from cancel trigger to effective cancellation")
    ap.add_argument("--hedge-latency-ms", type=float, default=None,
                    help="ETF-fill to TSLA-hedge delay; defaults to --latency-ms")
    ap.add_argument("--exit-latency-ms", type=float, default=None,
                    help="Scheduled exit to executable quote delay; defaults to --latency-ms")
    ap.add_argument("--queue-ahead-fraction", type=float, default=1.0,
                    help="Deprecated compatibility option; MBO order replay determines queue position")
    ap.add_argument("--fee-per-share", type=float, default=0.0)
    ap.add_argument("--edge-buffer-bps", type=float, default=2.0)
    ap.add_argument("--min-residual-bps", type=float, default=0.0,
                    help="Optional extra residual floor; costs plus edge buffer already gate entries")
    ap.add_argument("--quote-offset-ticks", type=int, default=0,
                    help="0 joins the touch; 1 improves one tick toward the opposing quote")
    ap.add_argument("--skip-date", action="append", default=[],
                    help="Session date YYYYMMDD to skip (repeatable)")
    ap.add_argument("--workers", type=int, default=1,
                    help="Number of independent daily sessions to process concurrently")
    a = ap.parse_args()
    if a.quantity <= 0 or a.workers < 1:
        ap.error("quantity and workers must be positive")
    for name in ("exchange_entry_delay_us", "latency_ms", "cancel_latency_ms", "quote_wait_ms", "hold_ms",
                 "signal_lookback_ms", "signal_threshold_bps"):
        if getattr(a, name) < 0:
            ap.error(f"--{name.replace('_', '-')} must be nonnegative")
    if a.signal_mode == "leadlag" and (a.signal_lookback_ms <= 0 or a.signal_threshold_bps <= 0):
        ap.error("leadlag signal lookback and threshold must be positive")
    for name in ("hedge_latency_ms", "exit_latency_ms"):
        if getattr(a, name) is not None and getattr(a, name) < 0:
            ap.error(f"--{name.replace('_', '-')} must be nonnegative")
    out = []
    candidate_out = []
    screening_out = []
    days = pd.date_range(pd.to_datetime(a.start_day), pd.to_datetime(a.end_day), freq="B")
    tasks = []
    for d in days:
        day = d.strftime("%Y%m%d")
        if day in set(a.skip_date):
            continue
        path = a.data_dir / f"xnas-itch-{day}.mbo.dbn.zst.TSLA-TSLL-TSLR-TSLG.npz"
        if not path.exists():
            print(f"Skipping missing cache {path}")
            continue
        tasks.append((day, str(path), a.quantity, a.imbalance, a.quote_wait_ms,
                      a.hold_ms, a.signal_mode, a.signal_lookback_ms,
                      a.signal_threshold_bps, a.trade_mode,
                      not a.keep_order_on_signal_fade,
                      a.exchange_entry_delay_us, a.latency_ms,
                      a.fee_per_share, a.edge_buffer_bps,
                      a.min_residual_bps, a.quote_offset_ticks,
                      a.cancel_latency_ms,
                      a.hedge_latency_ms, a.exit_latency_ms))
    for day, trades, candidates, screenings in run_sessions(_process_day, tasks, a.workers):
        if not trades.empty:
            out.append(trades)
        if not candidates.empty:
            candidate_out.append(candidates)
        if not screenings.empty:
            screening_out.append(screenings)
        print(f"Processed {day}", flush=True)
    result = pd.concat(out, ignore_index=True) if out else pd.DataFrame()
    candidate_result = pd.concat(candidate_out, ignore_index=True) if candidate_out else pd.DataFrame()
    screening_result = pd.concat(screening_out, ignore_index=True) if screening_out else pd.DataFrame()
    a.out.mkdir(parents=True, exist_ok=True)
    result.to_csv(a.out / "trades.csv", index=False)
    candidate_result.to_csv(a.out / "candidates.csv.gz", index=False, compression="gzip")
    screening_result.to_csv(a.out / "signal_screening_by_day.csv", index=False)
    screening_result["month"] = screening_result.day.astype(str).str[:6]
    monthly_screen = screening_result.groupby(["month", "symbol"], as_index=False).agg(
        eligible_signals=("eligible_signals", "sum"),
        cost_filtered=("cost_filtered", "sum"),
    )
    if not candidate_result.empty:
        candidate_result["month"] = candidate_result.day.astype(str).str[:6]
        monthly_orders = candidate_result.groupby(["month", "symbol"], as_index=False).agg(
            order_attempts=("candidate_status", "size"),
            orders_sent=("order_sent", "sum"),
            filled=("candidate_status", lambda x: (x == "filled").sum()),
        )
        monthly_funnel = monthly_screen.merge(monthly_orders, on=["month", "symbol"], how="left").fillna(0)
    else:
        monthly_funnel = monthly_screen.copy()
        monthly_funnel["order_attempts"] = 0
        monthly_funnel["orders_sent"] = 0
        monthly_funnel["filled"] = 0
    monthly_funnel["fill_rate_of_orders_sent"] = np.divide(
        monthly_funnel.filled, monthly_funnel.orders_sent,
        out=np.full(len(monthly_funnel), np.nan), where=monthly_funnel.orders_sent.to_numpy() > 0)
    monthly_funnel.to_csv(a.out / "monthly_candidate_rates.csv", index=False)
    if not result.empty:
        result["month"] = result.day.astype(str).str[:6]
        monthly_trades = result.groupby(["month", "symbol"], as_index=False).agg(
            trades=("net_pnl", "size"), net_pnl=("net_pnl", "sum"),
            mean_net_pnl=("net_pnl", "mean"),
            win_rate=("net_pnl", lambda x: (x > 0).mean()),
            pnl_marked_to_mid=("pnl_marked_to_mid", "sum"),
        )
    else:
        monthly_trades = pd.DataFrame(columns=["month", "symbol", "trades", "net_pnl", "mean_net_pnl", "win_rate"])
    monthly_trades.to_csv(a.out / "monthly_trade_summary.csv", index=False)
    if not result.empty:
        summary = result.groupby("symbol").agg(
            trades=("net_pnl", "size"), net_pnl=("net_pnl", "sum"),
            mean_net_pnl=("net_pnl", "mean"), win_rate=("net_pnl", lambda x: (x > 0).mean()),
            pnl_marked_to_mid=("pnl_marked_to_mid", "sum"),
            pnl_at_simulated_fills=("pnl_at_simulated_fills", "sum"),
        ).reset_index()
    else:
        summary = pd.DataFrame(columns=["symbol", "trades", "net_pnl", "mean_net_pnl", "win_rate"])
    summary.to_csv(a.out / "summary.csv", index=False)
    if not candidate_result.empty:
        candidate_summary = candidate_result.groupby(["symbol", "candidate_status"], as_index=False).agg(
            candidates=("candidate_status", "size"),
            mean_counterfactual_markout=("unfilled_counterfactual_markout", "mean"),
            median_counterfactual_markout=("unfilled_counterfactual_markout", "median"),
        )
        cancelled = candidate_result[candidate_result.candidate_status.isin(["cancelled", "quote_timeout"])]
        if not cancelled.empty:
            cancel_diagnostics = cancelled.groupby(["symbol", "cancel_reason"], as_index=False).agg(
                orders=("candidate_status", "size"),
                mean_live_ms=("order_live_duration_ms", "mean"),
                median_live_ms=("order_live_duration_ms", "median"),
                p90_live_ms=("order_live_duration_ms", lambda x: x.quantile(.90)),
                mean_time_to_trigger_ms=("time_to_cancel_trigger_ms", "mean"),
                mean_contra_volume=("contra_volume", "mean"),
                median_queue_progress=("queue_progress_ratio", "median"),
                mean_queue_progress=("queue_progress_ratio", "mean"),
            )
            unfilled_by_symbol = cancelled.groupby("symbol").size()
            cancel_diagnostics["share_of_symbol_unfilled_orders"] = cancel_diagnostics.apply(
                lambda row: row.orders / unfilled_by_symbol[row.symbol], axis=1)
            cancel_diagnostics.to_csv(a.out / "cancel_diagnostics.csv", index=False)
            by_day = cancelled.groupby(["day", "symbol", "cancel_reason"], as_index=False).agg(
                orders=("candidate_status", "size"),
                median_live_ms=("order_live_duration_ms", "median"),
                mean_contra_volume=("contra_volume", "mean"),
                mean_queue_progress=("queue_progress_ratio", "mean"),
            )
            by_day.to_csv(a.out / "cancel_diagnostics_by_day.csv", index=False)
        else:
            pd.DataFrame(columns=["symbol", "cancel_reason", "orders"]).to_csv(
                a.out / "cancel_diagnostics.csv", index=False)
            pd.DataFrame(columns=["day", "symbol", "cancel_reason", "orders"]).to_csv(
                a.out / "cancel_diagnostics_by_day.csv", index=False)
        pre_arrival = candidate_result[candidate_result.candidate_status.isin(
            ["stale_before_arrival", "signal_faded_before_arrival", "no_arrival_quote"])]
        if not pre_arrival.empty:
            aggregations = {"candidates": ("candidate_status", "size")}
            if "time_to_signal_fade_ms" in pre_arrival.columns:
                aggregations["mean_signal_fade_ms"] = ("time_to_signal_fade_ms", "mean")
            pre_summary = pre_arrival.groupby(["symbol", "candidate_status", "cancel_reason"],
                                               as_index=False, dropna=False).agg(**aggregations)
        else:
            pre_summary = pd.DataFrame(columns=["symbol", "candidate_status", "cancel_reason", "candidates"])
        pre_summary.to_csv(a.out / "pre_arrival_diagnostics.csv", index=False)
        candidate_counts = candidate_result.groupby("symbol").agg(
            order_attempts=("candidate_status", "size"),
            orders_sent=("order_sent", "sum"),
            filled=("candidate_status", lambda x: (x == "filled").sum()),
        ).reset_index()
        screen_totals = screening_result.groupby("symbol").agg(
            eligible_signals=("eligible_signals", "sum"),
            cost_filtered=("cost_filtered", "sum"),
        ).reset_index()
        candidate_counts = screen_totals.merge(candidate_counts, on="symbol", how="outer").fillna(0)
        partial_counts = candidate_result.assign(
            has_partial=(candidate_result.partial_fill_qty > 0).astype(int)
        ).groupby("symbol").agg(
            orders_with_partial_fills=("has_partial", "sum"),
            partial_filled_shares=("partial_fill_qty", "sum"),
        ).reindex(candidate_counts.symbol)
        candidate_counts["orders_with_partial_fills"] = partial_counts.orders_with_partial_fills.fillna(0).to_numpy()
        candidate_counts["partial_filled_shares"] = partial_counts.partial_filled_shares.fillna(0).to_numpy()
        candidate_counts["orders_with_any_fill"] = (
            candidate_counts.filled + candidate_counts.orders_with_partial_fills)
        candidate_counts["fill_rate_of_orders_sent_any"] = np.divide(
            candidate_counts.orders_with_any_fill, candidate_counts.orders_sent,
            out=np.full(len(candidate_counts), np.nan),
            where=candidate_counts.orders_sent.to_numpy() > 0)
        candidate_counts["fill_rate_of_orders_sent"] = np.divide(
            candidate_counts.filled, candidate_counts.orders_sent,
            out=np.full(len(candidate_counts), np.nan), where=candidate_counts.orders_sent.to_numpy() > 0)
        candidate_counts["fill_rate_of_eligible_signals"] = (
            candidate_counts.filled / candidate_counts.eligible_signals)
        candidate_summary.to_csv(a.out / "candidate_summary.csv", index=False)
        candidate_counts.to_csv(a.out / "candidate_rates.csv", index=False)
    else:
        candidate_summary = pd.DataFrame(columns=["symbol", "candidate_status", "candidates"])
        candidate_counts = screening_result.groupby("symbol", as_index=False).agg(
            eligible_signals=("eligible_signals", "sum"), cost_filtered=("cost_filtered", "sum"))
        candidate_counts["order_attempts"] = 0
        candidate_counts["orders_sent"] = 0
        candidate_counts["filled"] = 0
        candidate_summary.to_csv(a.out / "candidate_summary.csv", index=False)
        candidate_counts.to_csv(a.out / "candidate_rates.csv", index=False)
    (a.out / "summary.json").write_text(json.dumps({
        "parameters": {"start_day": a.start_day, "end_day": a.end_day,
            "skipped_dates": a.skip_date, "quantity": a.quantity,
            "signal_mode": a.signal_mode,
            "signal_lookback_ms": a.signal_lookback_ms,
            "signal_threshold_bps": a.signal_threshold_bps,
            "trade_mode": a.trade_mode,
            "cancel_on_signal_fade": not a.keep_order_on_signal_fade,
            "imbalance": a.imbalance, "quote_wait_ms": a.quote_wait_ms,
            "hold_ms": a.hold_ms, "exchange_entry_delay_us": a.exchange_entry_delay_us,
            "latency_ms": a.latency_ms,
            "cancel_latency_ms": a.cancel_latency_ms,
            "hedge_latency_ms": a.hedge_latency_ms if a.hedge_latency_ms is not None else a.latency_ms,
            "exit_latency_ms": a.exit_latency_ms if a.exit_latency_ms is not None else a.latency_ms,
            "fee_per_share": a.fee_per_share,
            "edge_buffer_bps": a.edge_buffer_bps,
            "min_residual_bps": a.min_residual_bps,
            "quote_offset_ticks": a.quote_offset_ticks, "workers": a.workers},
        "execution_counts": {
            "order_attempts": int(len(candidate_result)),
            "orders_reached_simulated_book": int(candidate_result.order_sent.sum()) if not candidate_result.empty else 0,
            "fills": int((candidate_result.candidate_status == "filled").sum()) if not candidate_result.empty else 0,
            "orders_with_partial_fills": int((candidate_result.partial_fill_qty > 0).sum()) if not candidate_result.empty else 0,
            "partial_filled_shares": int(candidate_result.partial_fill_qty.sum()) if not candidate_result.empty else 0,
        },
        "limitations": ["Daily signals are processed independently and use fixed entry, hedge, and exit delays.",
            "Only one live ETF maker order is allowed per pair. Partial shares in an order are aggregated at the last partial-fill time for P&L.",
            "Queue position is replayed from visible MBO order IDs. Hidden liquidity, own-order interaction, and exact exchange-arrival sequencing remain unobserved.",
        "Candidates use the selected TSLA signal and trade mode; cost-filtered candidates are not submitted orders. Fill rates are also reported among submitted orders.",
            "Unfilled counterfactual markouts assume a fill at the order limit and are not realized P&L.",
            "Aggressive exits, and TSLA hedge fills in pair mode, use displayed BBO without market impact or borrow costs.",
            "Low fill counts limit inference; simulated results do not establish live profitability."]
    }, indent=2))
    print(summary.to_string(index=False))
    print(f"Saved results to {a.out}")


if __name__ == "__main__":
    main()
