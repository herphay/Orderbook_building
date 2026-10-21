"""Execution helpers for depth-aware taker fills and queue sensitivity."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .leadlag import UNDEF_PRICE, njit


@dataclass
class DepthSnapshots:
    """Book state at the first completed MBO event after each requested time."""

    requested_ns: np.ndarray
    event_ns: np.ndarray
    bid_px: np.ndarray
    bid_sz: np.ndarray
    ask_px: np.ndarray
    ask_sz: np.ndarray


@dataclass
class Fill:
    requested_qty: int
    filled_qty: int
    vwap: float
    last_px: float
    levels_used: int


@njit(cache=True)
def _depth_snapshots_engine(action, side, pidx, size, oidx, flags, ts,
                            n_prices, n_orders, target, n_levels, use_last):
    n = action.shape[0]
    m = target.shape[0]
    bid_lvl = np.zeros(n_prices, np.int64)
    ask_lvl = np.zeros(n_prices, np.int64)
    o_p = np.full(n_orders, -1, np.int64)
    o_s = np.zeros(n_orders, np.int64)
    o_side = np.zeros(n_orders, np.uint8)
    bp = np.full((m, n_levels), -1, np.int64)
    ap = np.full((m, n_levels), -1, np.int64)
    bs = np.zeros((m, n_levels), np.int64)
    ass = np.zeros((m, n_levels), np.int64)
    event_ns = np.full(m, -1, np.int64)
    q = 0
    best_b, best_a = -1, n_prices
    for i in range(n):
        a = action[i]
        if a == 65 or a == 77:  # add / modify
            o = oidx[i]
            p = o_p[o]
            if p >= 0:
                s = o_s[o]
                if o_side[o] == 66:
                    bid_lvl[p] -= s
                    if p == best_b and bid_lvl[p] <= 0:
                        while best_b >= 0 and bid_lvl[best_b] <= 0:
                            best_b -= 1
                else:
                    ask_lvl[p] -= s
                    if p == best_a and ask_lvl[p] <= 0:
                        while best_a < n_prices and ask_lvl[best_a] <= 0:
                            best_a += 1
                o_p[o], o_s[o] = -1, 0
            p, s, sd = pidx[i], size[i], side[i]
            if p >= 0 and s > 0 and (sd == 66 or sd == 65):
                o_p[o], o_s[o], o_side[o] = p, s, sd
                if sd == 66:
                    bid_lvl[p] += s
                    if p > best_b:
                        best_b = p
                else:
                    ask_lvl[p] += s
                    if p < best_a:
                        best_a = p
        elif a == 67:  # partial/full cancel
            o = oidx[i]
            p = o_p[o]
            if p >= 0:
                c = size[i]
                if c <= 0 or c > o_s[o]:
                    c = o_s[o]
                o_s[o] -= c
                if o_side[o] == 66:
                    bid_lvl[p] -= c
                    if p == best_b and bid_lvl[p] <= 0:
                        while best_b >= 0 and bid_lvl[best_b] <= 0:
                            best_b -= 1
                else:
                    ask_lvl[p] -= c
                    if p == best_a and ask_lvl[p] <= 0:
                        while best_a < n_prices and ask_lvl[best_a] <= 0:
                            best_a += 1
                if o_s[o] <= 0:
                    o_p[o], o_s[o] = -1, 0
        elif a == 82:  # clear
            bid_lvl[:] = 0
            ask_lvl[:] = 0
            o_p[:] = -1
            o_s[:] = 0
            best_b, best_a = -1, n_prices

        event_end = (not use_last) or ((flags[i] & 128) != 0)
        if event_end:
            while q < m and target[q] < ts[i]:  # strict-after arrival
                event_ns[q] = ts[i]
                p = best_b
                k = 0
                while p >= 0 and k < n_levels:
                    if bid_lvl[p] > 0:
                        bp[q, k], bs[q, k] = p, bid_lvl[p]
                        k += 1
                    p -= 1
                p = best_a
                k = 0
                while p < n_prices and k < n_levels:
                    if ask_lvl[p] > 0:
                        ap[q, k], ass[q, k] = p, ask_lvl[p]
                        k += 1
                    p += 1
                q += 1
    return event_ns, bp, bs, ap, ass


def depth_at_times(records: np.ndarray, target_ns, levels: int = 10) -> DepthSnapshots:
    """Replay MBO once and capture top depth after each target arrival time.

    ``target_ns`` values are order-arrival times; the returned state is the
    first completed MBO event strictly after each time. Missing end-of-day
    states have event_ns=-1 and zero depth.
    """
    targets = np.asarray(target_ns, dtype=np.int64)
    if targets.ndim != 1:
        raise ValueError("target_ns must be one-dimensional")
    if levels < 1:
        raise ValueError("levels must be positive")
    if not len(targets):
        shape = (0, levels)
        empty = np.empty(shape, dtype=float)
        return DepthSnapshots(targets, np.empty(0, np.int64), empty, empty.copy(),
                              empty.copy(), empty.copy())

    order = np.argsort(targets, kind="stable")
    sorted_targets = targets[order]
    action = records["action"].astype(np.uint8, copy=False)
    price = records["price"].astype(np.int64, copy=False)
    valid = ((action == 65) | (action == 77)) & (price != UNDEF_PRICE)
    unique_prices = np.unique(price[valid])
    pidx = np.full(len(records), -1, np.int64)
    pidx[valid] = np.searchsorted(unique_prices, price[valid])
    _, oidx = np.unique(records["order_id"], return_inverse=True)
    ev, bp_idx, bs, ap_idx, ass = _depth_snapshots_engine(
        action, records["side"].astype(np.uint8, copy=False), pidx,
        records["size"].astype(np.int64, copy=False), oidx.astype(np.int64),
        records["flags"].astype(np.uint8, copy=False),
        records["ts_event"].astype(np.int64, copy=False), len(unique_prices),
        int(oidx.max()) + 1 if len(records) else 0, sorted_targets, levels,
        bool((records["flags"] & 128).any()))
    bp = np.full(bp_idx.shape, np.nan, float)
    ap = np.full(ap_idx.shape, np.nan, float)
    good_b, good_a = bp_idx >= 0, ap_idx >= 0
    if len(unique_prices):
        bp[good_b] = unique_prices[bp_idx[good_b]] / 1e9
        ap[good_a] = unique_prices[ap_idx[good_a]] / 1e9
    unsort = np.empty_like(order)
    unsort[order] = np.arange(len(order))
    return DepthSnapshots(targets, ev[unsort], bp[unsort], bs[unsort],
                          ap[unsort], ass[unsort])


def walk_market_order(depth: DepthSnapshots, row: int, direction: int,
                      quantity: int, max_levels: int = 10) -> Fill:
    """Sweep displayed levels; direction is +1 buy or -1 sell.

    The unfilled remainder is returned instead of being assumed filled.
    ``max_levels`` defines the depth horizon included in this scenario.
    """
    if direction not in (-1, 1):
        raise ValueError("direction must be +1 (buy) or -1 (sell)")
    if quantity < 1 or max_levels < 1:
        raise ValueError("quantity and max_levels must be positive")
    pxs = depth.ask_px[row] if direction > 0 else depth.bid_px[row]
    sizes = depth.ask_sz[row] if direction > 0 else depth.bid_sz[row]
    filled, notional, last_px, used = 0, 0.0, np.nan, 0
    for px, size in zip(pxs[:max_levels], sizes[:max_levels]):
        if not np.isfinite(px) or size <= 0 or filled >= quantity:
            break
        take = min(quantity - filled, int(size))
        filled += take
        notional += float(px) * take
        last_px, used = float(px), used + 1
    return Fill(quantity, filled, notional / filled if filled else np.nan,
                last_px, used)
