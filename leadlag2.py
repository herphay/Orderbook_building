#!/usr/bin/env python3
"""
letf_leadlag.py -- nanosecond lead/lag between TSLA and its 2x LETFs (TSLL, TSLR, TSLG)
from one day of Databento XNAS.ITCH MBO data.

Pipeline
  1. Stream the .dbn.zst in chunks and keep only the 4 instruments (cached to .npz,
     so re-runs skip the decode).
  2. Rebuild each full-depth order book (numba) and emit the top of book on every
     event (F_LAST) where it changes, tagging whether that event contained an execution.
  3. Four complementary lead/lag measurements per (TSLA, LETF) pair, on the exchange's
     nanosecond matching-engine clock (ts_event) by default:
       A. Signed cross-correlogram of mid-price changes: for every TSLA mid move, count
          same-direction minus opposite-direction LETF mid moves in lag bins from
          -1 s to +1 s (bins down to 10 ns). Mass at positive lags = TSLA leads.
       B. Reaction-latency histogram: for every LETF mid move, time since the most recent
          same-direction TSLA mid move, against a null built from the same LETF move times
          shifted by 1-5 s. The excess over the null peaks at the HFT reaction latency.
          (Hayashi-Yoshida is NOT used: it cannot resolve lags shorter than the typical
          gap between ticks, which is exactly the microsecond regime of interest.)
       C. Impulse response: fraction of a TSLA move (x leverage) already reflected in
          the LETF mid tau after the move. Gives "50% / 90% priced-in" times.
          Also the reverse (LETF move -> TSLA).
       D. Fair-value breach response: when a TSLA move pushes LETF fair value through
          the LETF's own displayed bid/ask, how long until that stale quote moves, and
          was it removed by a trade (sniped) or by a cancel (market maker requote)?
          Compared with a placebo (same test from random later times).
  4. CSVs, one PNG per LETF, summary.json, and a printed summary.

Run (Mac):
  pip install databento numba pandas numpy matplotlib
  python letf_leadlag.py
  python letf_leadlag.py --clock recv            # Databento receive clock instead
  python letf_leadlag.py --ids TSLA=123,TSLL=456,TSLR=789,TSLG=1011   # if symbology missing
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

IN_NOTEBOOK = "ipykernel" in sys.modules
_CACHE = "__file__" in globals()  # numba can only cache functions that live in a .py file

try:
    from numba import njit
    HAVE_NUMBA = True
except ImportError:  # still runs, just much slower on a full day
    HAVE_NUMBA = False

    def njit(*args, **kwargs):
        if args and callable(args[0]):
            return args[0]
        return lambda f: f

# --------------------------------------------------------------------------- config
DBN_PATH = "XNAS_MBO/xnas-itch-20261009.mbo.dbn.zst"
UNDERLYING = "TSLA"
PREV_CLOSE = {"TSLA": 375.0, "TSLL": 10.01, "TSLR": 17.39, "TSLG": 5.0}
FUNDS = {
    "TSLL": {"L": 2.0, "E": 0.0083},  # Direxion Daily TSLA Bull 2X
    "TSLR": {"L": 2.0, "E": 0.0095},  # GraniteShares 2x Long TSLA Daily
    "TSLG": {"L": 2.0, "E": 0.0075},  # Leverage Shares 2X Long TSLA Daily
}
RISK_FREE = 0.0388     # R: annual fed funds / risk-free rate
LEND_PREMIUM = 0.0050  # P: swap lending premium (~50 bp)
DIV_YIELD = 0.0        # D_S: TSLA pays no dividend
W_EQUITY = 0.0         # W_E: fraction of fund held as direct equity
W_TREASURY = 0.0       # W_T: fraction of fund held as treasuries
TICK = 0.01
RTH = ("09:30:00", "16:00:00")  # America/New_York

# lag grids (ns), 1-2.5-5 spacing
_125 = np.array([1.0, 2.5, 5.0])
XCORR_EDGES = np.unique(np.concatenate([[0], (_125[None, :] * 10.0 ** np.arange(1, 9)[:, None]).ravel(), [1e9]])).astype(np.int64)
LAT_EDGES = np.unique(np.round(np.logspace(0, 9, 91))).astype(np.int64)                 # 1 ns .. 1 s, 10/decade
_tpos = np.unique(np.round(np.logspace(1, 9, 81))).astype(np.int64)                        # 10 ns .. 1 s, 10/decade
_neg = -np.unique((_125[None, :] * 10.0 ** np.arange(2, 6)[:, None]).ravel()).astype(np.int64)[::-1]  # -500 us .. -100 ns
IMPULSE_TAUS = np.concatenate([[-1_000_000], _neg, [0], _tpos])

# --------------------------------------------------------------------------- DBN loading
REC_DTYPE = np.dtype([
    ("ts_event", "i8"), ("ts_recv", "i8"), ("order_id", "u8"), ("price", "i8"),
    ("size", "u4"), ("flags", "u1"), ("action", "u1"), ("side", "u1"),
])
MBO_RTYPE = 0xA0
UNDEF_PRICE = np.iinfo(np.int64).max
A_ADD, A_CANCEL, A_MODIFY, A_CLEAR, A_TRADE, A_FILL = 65, 67, 77, 82, 84, 70  # 'A','C','M','R','T','F'
S_ASK, S_BID = 65, 66                                                           # 'A','B'
F_LAST = 128


def _as_u1(col: np.ndarray) -> np.ndarray:
    """Databento char fields ('S1' bytes, or ints) -> uint8 codes."""
    k = col.dtype.kind
    if k in "iu":
        return col.astype(np.uint8)
    if k == "U":
        col = col.astype("S1")
    return np.frombuffer(np.ascontiguousarray(col.astype("S1")).tobytes(), dtype=np.uint8).copy()


def _iter_mapping_intervals(store):
    """Yield (raw_symbol, start_date, end_date, mapped_symbol) from DBN metadata, across databento versions."""
    maps = getattr(store.metadata, "mappings", None)
    if not maps:
        maps = getattr(store, "mappings", None) or {}
    items = maps.items() if isinstance(maps, dict) else [(m.raw_symbol, m.intervals) for m in maps]
    for raw, ivs in items:
        for iv in ivs:
            if isinstance(iv, dict):
                yield raw, iv.get("start_date", iv.get("d0")), iv.get("end_date", iv.get("d1")), iv.get("symbol", iv.get("s"))
            else:
                yield raw, iv.start_date, iv.end_date, iv.symbol


def _file_day(store, dbn_path: Path):
    """Trading date: YYYYMMDD in the file name if present, else the UTC date of the file's start."""
    import re
    m = re.search(r"(20\d{6})", dbn_path.name)
    if m:
        return pd.Timestamp(m.group(1)).date()
    return pd.Timestamp(int(store.metadata.start), unit="ns", tz="UTC").date()  # Databento days are UTC


def _describe_metadata(store):
    md = store.metadata
    lines = []
    for k in ("dataset", "schema", "stype_in", "stype_out", "start", "end"):
        lines.append(f"    {k}: {getattr(md, k, '?')}")
    syms = list(getattr(md, "symbols", []) or [])
    lines.append(f"    symbols ({len(syms)}): {syms[:10]}{' ...' if len(syms) > 10 else ''}")
    try:
        ivs = list(_iter_mapping_intervals(store))
        lines.append(f"    mapping intervals: {len(ivs)}; first 5: {ivs[:5]}")
    except Exception as e:  # noqa: BLE001
        lines.append(f"    mappings unreadable: {e!r}")
    return "\n".join(lines)


def resolve_ids(store, dbn_path: Path, symbols, day, override) -> dict[int, str]:
    if override:
        return {int(v): k for k, v in override.items()}
    found: dict[int, str] = {}
    d_lo, d_hi = pd.Timestamp(day), pd.Timestamp(day) + pd.Timedelta(days=1)

    def consider(raw, d0, d1, sid):
        raw, sid = str(raw).strip(), str(sid).strip()
        if raw.isdigit() and not sid.isdigit():  # mapping stored the other way round
            raw, sid = sid, raw
        if raw not in symbols or not sid.isdigit():
            return
        try:  # keep the interval that overlaps the trading day
            a = pd.Timestamp(str(d0)).tz_localize(None) if d0 is not None else d_lo
            b = pd.Timestamp(str(d1)).tz_localize(None) if d1 is not None else d_hi
            if not (a < d_hi and b > d_lo):
                return
        except (ValueError, TypeError):
            pass
        found[int(sid)] = raw

    def missing():
        return sorted(set(symbols) - set(found.values()))

    # 1. mappings embedded in the DBN metadata
    try:
        for raw, d0, d1, sid in _iter_mapping_intervals(store):
            consider(raw, d0, d1, sid)
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] could not read metadata mappings: {e!r}")

    # 2. symbology.json shipped with a batch download
    if missing():
        cands = []
        for d in (dbn_path.parent, dbn_path.parent.parent):
            cands += sorted(d.glob("*symbology*.json"))
        for cand in cands:
            try:
                res = json.loads(cand.read_text()).get("result", {})
                for raw, ivs in res.items():
                    for iv in ivs:
                        consider(raw, iv.get("d0"), iv.get("d1"), iv.get("s"))
                print(f"  read {cand}")
            except Exception as e:  # noqa: BLE001
                print(f"  [warn] could not parse {cand}: {e!r}")

    # 3. Databento symbology API (free; needs DATABENTO_API_KEY in the environment)
    if missing():
        try:
            import os
            import databento as db
            if os.environ.get("DATABENTO_API_KEY"):
                res = db.Historical().symbology.resolve(
                    dataset=str(getattr(store.metadata, "dataset", "XNAS.ITCH")), symbols=missing(),
                    stype_in="raw_symbol", stype_out="instrument_id",
                    start_date=str(day), end_date=str((d_hi).date()))
                for raw, ivs in res.get("result", {}).items():
                    for iv in ivs:
                        consider(raw, iv.get("d0"), iv.get("d1"), iv.get("s"))
                print("  resolved via Databento symbology API")
            else:
                print("  [info] DATABENTO_API_KEY not set; skipping symbology API lookup")
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] symbology API lookup failed: {e!r}")

    if missing():
        sys.exit(f"Could not map {missing()} to instrument_ids for {day}.\n"
                 f"What the DBN metadata contains:\n{_describe_metadata(store)}\n"
                 f"Fix: set DATABENTO_API_KEY, put symbology.json next to the file, "
                 f"or pass --ids TSLA=12345,TSLL=23456,TSLR=34567,TSLG=45678")
    return found


def load_mbo(dbn_path: Path, symbols, override=None, chunk=10_000_000):
    import databento as db

    store = db.DBNStore.from_file(dbn_path)
    day = _file_day(store, dbn_path)
    id2sym = resolve_ids(store, dbn_path, symbols, day, override)
    print(f"  date {day}; instrument ids: " + ", ".join(f"{s}={i}" for i, s in sorted(id2sym.items(), key=lambda x: x[1])))
    keep = np.fromiter(id2sym.keys(), dtype=np.int64)
    parts = {s: [] for s in symbols}
    try:
        it = store.to_ndarray(count=chunk)
    except TypeError:  # very old databento: no chunked iterator
        it = [store.to_ndarray()]
    n_seen, t0 = 0, time.time()
    for arr in it:
        n_seen += len(arr)
        m = np.isin(arr["instrument_id"].astype(np.int64), keep)
        if "rtype" in arr.dtype.names:
            m &= arr["rtype"] == MBO_RTYPE
        if m.any():
            sub = arr[m]
            rec = np.empty(len(sub), REC_DTYPE)
            for f in ("ts_event", "ts_recv", "order_id", "price", "size", "flags"):
                rec[f] = sub[f]
            rec["action"] = _as_u1(sub["action"])
            rec["side"] = _as_u1(sub["side"])
            iid = sub["instrument_id"].astype(np.int64)
            for i, s in id2sym.items():
                parts[s].append(rec[iid == i])
        print(f"\r  decoded {n_seen / 1e6:,.1f}M records ({time.time() - t0:,.0f}s)", end="", flush=True)
    print()
    recs = {s: (np.concatenate(p) if p else np.empty(0, REC_DTYPE)) for s, p in parts.items()}
    return recs, day


def get_records(args, symbols):
    dbn = Path(args.dbn)
    cache = Path(args.cache) if args.cache else dbn.with_name(dbn.name + "." + "-".join(symbols) + ".npz")
    if cache.exists() and not args.no_cache:
        print(f"Loading cached records: {cache}")
        z = np.load(cache, allow_pickle=False)
        recs = {s: z[s] for s in symbols}
        day = pd.Timestamp(str(z["day"])).date()
        return recs, day
    print(f"Decoding {dbn}")
    override = dict(kv.split("=") for kv in args.ids.split(",")) if args.ids else None
    recs, day = load_mbo(dbn, symbols, override)
    try:
        np.savez(cache, day=str(day), **recs)
        print(f"  cached to {cache}")
    except OSError as e:
        print(f"  [warn] could not write cache: {e}")
    return recs, day


# --------------------------------------------------------------------------- order book
@njit(cache=_CACHE)
def _scan_down(lvl, p):
    while p >= 0 and lvl[p] <= 0:
        p -= 1
    return p


@njit(cache=_CACHE)
def _scan_up(lvl, p):
    n = lvl.shape[0]
    while p < n and lvl[p] <= 0:
        p += 1
    return p


@njit(cache=_CACHE)
def _book_engine(action, side, pidx, size, oidx, flags, ts, n_prices, n_orders, use_last):
    n = action.shape[0]
    bid_lvl = np.zeros(n_prices, np.int64)
    ask_lvl = np.zeros(n_prices, np.int64)
    o_p = np.full(n_orders, -1, np.int64)
    o_s = np.zeros(n_orders, np.int64)
    o_side = np.zeros(n_orders, np.uint8)
    best_b = -1
    best_a = n_prices
    out_row = np.empty(n, np.int64)
    out_b = np.empty(n, np.int64)
    out_a = np.empty(n, np.int64)
    out_bs = np.empty(n, np.int64)
    out_as = np.empty(n, np.int64)
    out_tr = np.empty(n, np.uint8)
    k = 0
    lb, la, lbs, las = -2, -2, -1, -1
    last_trade_ts = -1
    for i in range(n):
        a = action[i]
        if a == 65 or a == 77:  # add / modify: remove existing then (re)insert
            o = oidx[i]
            p = o_p[o]
            if p >= 0:
                s = o_s[o]
                if o_side[o] == 66:
                    bid_lvl[p] -= s
                    if p == best_b and bid_lvl[p] <= 0:
                        best_b = _scan_down(bid_lvl, p)
                else:
                    ask_lvl[p] -= s
                    if p == best_a and ask_lvl[p] <= 0:
                        best_a = _scan_up(ask_lvl, p)
                o_p[o] = -1
                o_s[o] = 0
            p = pidx[i]
            s = size[i]
            sd = side[i]
            if p >= 0 and s > 0 and (sd == 66 or sd == 65):
                o_p[o] = p
                o_s[o] = s
                o_side[o] = sd
                if sd == 66:
                    bid_lvl[p] += s
                    if p > best_b:
                        best_b = p
                else:
                    ask_lvl[p] += s
                    if p < best_a:
                        best_a = p
        elif a == 67:  # cancel (partial or full)
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
                        best_b = _scan_down(bid_lvl, p)
                else:
                    ask_lvl[p] -= c
                    if p == best_a and ask_lvl[p] <= 0:
                        best_a = _scan_up(ask_lvl, p)
                if o_s[o] <= 0:
                    o_p[o] = -1
                    o_s[o] = 0
        elif a == 82:  # clear book
            bid_lvl[:] = 0
            ask_lvl[:] = 0
            o_p[:] = -1
            o_s[:] = 0
            best_b = -1
            best_a = n_prices
        elif a == 84 or a == 70:  # trade / fill: book changes arrive as cancels
            last_trade_ts = ts[i]
        if use_last and (flags[i] & 128) == 0:
            continue
        bs = bid_lvl[best_b] if best_b >= 0 else 0
        as_ = ask_lvl[best_a] if best_a < n_prices else 0
        if best_b != lb or best_a != la or bs != lbs or as_ != las:
            out_row[k] = i
            out_b[k] = best_b
            out_a[k] = best_a
            out_bs[k] = bs
            out_as[k] = as_
            out_tr[k] = 1 if last_trade_ts == ts[i] else 0
            k += 1
            lb, la, lbs, las = best_b, best_a, bs, as_
    return out_row[:k], out_b[:k], out_a[:k], out_bs[:k], out_as[:k], out_tr[:k]


def build_bbo(rec: np.ndarray) -> pd.DataFrame:
    """Full-depth book replay -> top-of-book change log."""
    act = rec["action"]
    px = rec["price"]
    valid = ((act == A_ADD) | (act == A_MODIFY)) & (px != UNDEF_PRICE)
    uniq = np.unique(px[valid])
    pidx = np.full(len(rec), -1, np.int64)
    pidx[valid] = np.searchsorted(uniq, px[valid])
    _, oidx = np.unique(rec["order_id"], return_inverse=True)
    use_last = bool((rec["flags"] & F_LAST).any())
    rows, b, a, bs, as_, tr = _book_engine(
        act, rec["side"], pidx, rec["size"].astype(np.int64), oidx.astype(np.int64).ravel(),
        rec["flags"], rec["ts_event"].astype(np.int64), len(uniq), int(oidx.max()) + 1 if len(rec) else 0, use_last)
    upx = np.concatenate([uniq, [0]]).astype(np.float64) / 1e9
    bid = np.where(b >= 0, upx[np.clip(b, 0, None)], np.nan)
    ask = np.where(a < len(uniq), upx[np.minimum(a, len(uniq))], np.nan)
    return pd.DataFrame({
        "ts_event": rec["ts_event"][rows], "ts_recv": rec["ts_recv"][rows],
        "bid": bid, "ask": ask, "bid_sz": bs, "ask_sz": as_, "had_trade": tr,
    })


# --------------------------------------------------------------------------- series
@dataclass
class Series:
    sym: str
    t: np.ndarray      # quote-change times (unique, sorted, ns)
    bid: np.ndarray
    ask: np.ndarray
    trade: np.ndarray  # 1 if the event at t contained an execution
    x: np.ndarray      # log price (mid or microprice) after the event at t
    tm: np.ndarray     # times where x changed
    xm: np.ndarray     # x after the change
    dxm: np.ndarray    # size of the change


def make_series(bbo: pd.DataFrame, sym, clock, price, lo, hi) -> Series:
    t = bbo[f"ts_{clock}"].to_numpy(np.int64)
    o = np.argsort(t, kind="stable")
    d = bbo.iloc[o]
    t = t[o]
    bid, ask = d["bid"].to_numpy(), d["ask"].to_numpy()
    bs, as_ = d["bid_sz"].to_numpy(np.float64), d["ask_sz"].to_numpy(np.float64)
    tr = d["had_trade"].to_numpy(np.uint8)
    ok = np.isfinite(bid) & np.isfinite(ask) & (bid > 0) & (ask > bid) & (t >= lo) & (t <= hi)
    t, bid, ask, bs, as_, tr = t[ok], bid[ok], ask[ok], bs[ok], as_[ok], tr[ok]
    if len(t) == 0:
        raise SystemExit(f"{sym}: no valid two-sided quotes in the window")
    # one state per timestamp (last wins); trade flag = any row at that timestamp
    first = np.r_[True, t[1:] != t[:-1]]
    starts = np.flatnonzero(first)
    tr = np.maximum.reduceat(tr, starts)
    last = np.r_[t[1:] != t[:-1], True]
    t, bid, ask, bs, as_ = t[last], bid[last], ask[last], bs[last], as_[last]
    p = (bid * as_ + ask * bs) / (bs + as_) if price == "micro" else 0.5 * (bid + ask)
    x = np.log(p)
    chg = np.flatnonzero(np.r_[False, x[1:] != x[:-1]])
    return Series(sym, t, bid, ask, tr, x, t[chg], x[chg], x[chg] - x[chg - 1])


def asof_idx(t, q, strict=False):
    """Index of the last element of t at (<=) q, or strictly before (<) q if strict."""
    return np.searchsorted(t, q, side="left" if strict else "right") - 1


# --------------------------------------------------------------------------- A. signed cross-correlogram
def signed_xcorr(ta, sa, tb, sb, edges):
    ub = np.concatenate([[0], np.cumsum(sb.astype(np.int64))])
    n = len(ta)
    rows = []

    def add(lo_ns, hi_ns, lo, hi):
        cnt = hi - lo
        S = float(np.dot(sa, ub[hi] - ub[lo]))
        N = float(cnt.sum())
        rows.append((lo_ns, hi_ns, S, N, S / N if N else np.nan, S / np.sqrt(N) if N else np.nan, S / n))

    pairs = list(zip(edges[:-1], edges[1:]))
    for a, b in reversed(pairs):  # LETF move BEFORE TSLA move: [t-b, t-a)
        add(-b, -a, np.searchsorted(tb, ta - b, "left"), np.searchsorted(tb, ta - a, "left"))
    add(0, 0, np.searchsorted(tb, ta, "left"), np.searchsorted(tb, ta, "right"))
    for a, b in pairs:            # LETF move AFTER TSLA move: (t+a, t+b]
        add(a, b, np.searchsorted(tb, ta + a, "right"), np.searchsorted(tb, ta + b, "right"))
    return pd.DataFrame(rows, columns=["lag_lo_ns", "lag_hi_ns", "signed_pairs", "pairs",
                                       "concordance", "z", "signed_per_trigger"])


# --------------------------------------------------------------------------- B. reaction latency
def _since_last_same_dir(St, Ss, q, qs):
    """For query times q with direction qs: ns since the most recent TSLA move (<= q) in the same direction."""
    out = np.full(len(q), -1, np.int64)
    for d in (1, -1):
        tt = St[Ss == d]
        m = qs == d
        i = asof_idx(tt, q[m])
        ok = i >= 0
        r = np.full(m.sum(), -1, np.int64)
        r[ok] = q[m][ok] - tt[i[ok]]
        out[m] = r
    return out


def reaction_latency(St, Ss, Lt, Ls, rth, rng, edges, n_null=5):
    obs = _since_last_same_dir(St, Ss, Lt, Ls)
    nulls = []
    for _ in range(n_null):  # LETF move times shifted 1-5 s either way: same time-of-day activity, no causality
        off = rng.integers(1_000_000_000, 5_000_000_000, len(Lt)) * rng.choice([-1, 1], len(Lt))
        q = Lt + off
        k = (q >= rth[0]) & (q < rth[1])
        nulls.append(_since_last_same_dir(St, Ss, q[k], Ls[k]))
    null = np.concatenate(nulls)
    obs, null = obs[obs >= 0], null[null >= 0]
    rows = [(0, 0, np.mean(obs == 0), np.mean(null == 0))]
    ho, _ = np.histogram(obs[obs > 0], bins=edges)
    hn, _ = np.histogram(null[null > 0], bins=edges)
    for a, b, co, cn in zip(edges[:-1], edges[1:], ho, hn):
        rows.append((int(a), int(b), co / len(obs), cn / len(null)))
    df = pd.DataFrame(rows, columns=["lat_lo_ns", "lat_hi_ns", "frac_obs", "frac_null"])
    df["excess"] = df.frac_obs - df.frac_null
    return df, obs, null


# --------------------------------------------------------------------------- C. impulse response
def impulse(trig_t, trig_dx, resp: Series, own: Series, taus, scale):
    """
    tau >= 0: beta = sum s*dResp(t-, t+tau) / (scale * sum s*dOwn(t-, t+tau))   -> fraction priced in
    tau <  0: beta = sum s*dResp(t+tau, t-) / (scale * sum s*dx_trigger)         -> move made BEFORE the trigger
    """
    s = np.sign(trig_dx)
    ir0 = asof_idx(resp.t, trig_t, strict=True)
    io0 = asof_idx(own.t, trig_t, strict=True)
    ok = (ir0 >= 0) & (io0 >= 0)
    t_, s_, dx_ = trig_t[ok], s[ok], trig_dx[ok]
    br, bo = resp.x[ir0[ok]], own.x[io0[ok]]
    rows = []
    for tau in taus:
        r_now = resp.x[asof_idx(resp.t, t_ + tau)]
        if tau >= 0:
            num_i = s_ * (r_now - br)
            den_i = scale * s_ * (own.x[asof_idx(own.t, t_ + tau)] - bo)
        else:
            num_i = s_ * (br - r_now)
            den_i = scale * s_ * dx_
        num, den = num_i.sum(), den_i.sum()
        beta = num / den if den else np.nan
        se = np.sqrt(np.sum((num_i - beta * den_i) ** 2)) / abs(den) if den else np.nan
        rows.append((int(tau), beta, se))
    return pd.DataFrame(rows, columns=["tau_ns", "beta", "se"])


def time_to_fraction(imp: pd.DataFrame, frac):
    """First tau where beta reaches frac * beta(longest tau), log-interpolated between grid points."""
    pos = imp[imp.tau_ns >= 0].reset_index(drop=True)
    ref = float(pos.beta.iloc[-1])
    tgt = frac * ref
    hit = np.flatnonzero(pos.beta.to_numpy() >= tgt)
    if not len(hit):
        return None, ref
    k = hit[0]
    if k == 0 or pos.tau_ns[k - 1] <= 0:
        return int(pos.tau_ns[k]), ref
    t0, t1, b0, b1 = pos.tau_ns[k - 1], pos.tau_ns[k], pos.beta[k - 1], pos.beta[k]
    w = (tgt - b0) / (b1 - b0) if b1 != b0 else 1.0
    return int(round(np.exp(np.log(t0) + w * (np.log(t1) - np.log(t0))))), ref


# --------------------------------------------------------------------------- D. fair-value breach response
def fair_raw(S, fund, pc_s, pc_l):
    """User's daily LETF fair-value formula (simplified form), applied intraday."""
    L, E = fund["L"], fund["E"]
    dS = S / pc_s - 1.0
    ret = (dS - (RISK_FREE + LEND_PREMIUM - DIV_YIELD) / 252.0) * L \
        - (E + W_EQUITY * DIV_YIELD + W_TREASURY * RISK_FREE) / 252.0
    return pc_l * (1.0 + ret)


@njit(cache=_CACHE)
def _first_move(tq, bid, ask, T, ref, direction, j0, horizon):
    n = T.shape[0]
    m = tq.shape[0]
    delay = np.full(n, -1, np.int64)
    jout = np.full(n, -1, np.int64)
    for i in range(n):
        j = j0[i]
        lim = T[i] + horizon
        while j < m and tq[j] <= lim:
            if direction[i] > 0:
                if ask[j] > ref[i]:
                    break
            else:
                if bid[j] < ref[i]:
                    break
            j += 1
        if j < m and tq[j] <= lim:
            delay[i] = tq[j] - T[i]
            jout[i] = j
    return delay, jout


def breach_response(S: Series, Lq: Series, fund, pc_s, pc_l, rth, margin_ticks, horizon, rng):
    # 1. calibrate the fair-value level (removes premium/discount and any PREV_CLOSE/fee error)
    grid = np.arange(rth[0], rth[1], 1_000_000_000, dtype=np.int64)
    gs, gl = asof_idx(S.t, grid), asof_idx(Lq.t, grid)
    g_ok = (gs >= 0) & (gl >= 0)
    basis = np.full(len(grid), np.nan)
    basis[g_ok] = Lq.x[gl[g_ok]] - np.log(fair_raw(np.exp(S.x[gs[g_ok]]), fund, pc_s, pc_l))
    basis_s = pd.Series(basis).rolling(301, center=True, min_periods=30).median().ffill().bfill().to_numpy()
    raw_premium_bp = float(np.nanmedian(basis) * 1e4)

    # 2. triggers: TSLA mid move that pushes calibrated fair through the LETF's pre-existing quote
    w = (S.tm >= rth[0]) & (S.tm < rth[1])
    T, xm, dx = S.tm[w], S.xm[w], S.dxm[w]
    adj = np.exp(np.interp(T, grid, basis_s))
    f_after = fair_raw(np.exp(xm), fund, pc_s, pc_l) * adj
    f_before = fair_raw(np.exp(xm - dx), fund, pc_s, pc_l) * adj
    jp = asof_idx(Lq.t, T, strict=True)
    ok = jp >= 0
    bid0 = np.where(ok, Lq.bid[np.maximum(jp, 0)], np.nan)
    ask0 = np.where(ok, Lq.ask[np.maximum(jp, 0)], np.nan)
    m = margin_ticks * TICK
    up = ok & (f_after > ask0 + m) & (f_before <= ask0 + m)
    dn = ok & (f_after < bid0 - m) & (f_before >= bid0 - m)
    trig = up | dn
    Tt = T[trig]
    dirn = np.where(up[trig], 1, -1).astype(np.int64)
    ref = np.where(up[trig], ask0[trig], bid0[trig])
    delay, j = _first_move(Lq.t, Lq.bid, Lq.ask, Tt, ref, dirn, jp[trig] + 1, np.int64(horizon))
    via_trade = np.where(j >= 0, Lq.trade[np.maximum(j, 0)], 0)

    # 3. placebo: same direction test from a random time 1-5 s later (same time-of-day activity)
    Tp = Tt + rng.integers(1_000_000_000, 5_000_000_000, len(Tt))
    keep = Tp < rth[1]
    Tp, dp = Tp[keep], dirn[keep]
    jpp = asof_idx(Lq.t, Tp, strict=True)
    refp = np.where(dp > 0, Lq.ask[jpp], Lq.bid[jpp])
    delay_p, _ = _first_move(Lq.t, Lq.bid, Lq.ask, Tp, refp, dp, jpp + 1, np.int64(horizon))

    ev = pd.DataFrame({"t_trigger": Tt, "direction": dirn, "stale_quote": ref,
                       "delay_ns": delay, "via_trade": via_trade})
    return ev, delay_p, raw_premium_bp


def delay_stats(d, via=None):
    d = np.asarray(d)
    hit = d[d >= 0]
    out = {"n": int(len(d)), "responded_frac": float(len(hit) / len(d)) if len(d) else np.nan}
    if len(hit):
        for p in (10, 25, 50, 75, 90):
            out[f"p{p}_us"] = float(np.percentile(hit, p) / 1e3)
        for lab, lim in (("eq0ns", 0), ("le1us", 1_000), ("le10us", 10_000), ("le100us", 100_000), ("le1ms", 1_000_000)):
            out[f"frac_{lab}"] = float(np.mean(d <= lim) if lim == 0 else np.mean((d >= 0) & (d <= lim)))
        out["frac_eq0ns"] = float(np.mean(d == 0))
    if via is not None and len(hit):
        v = np.asarray(via)[d >= 0]
        out["via_trade_frac"] = float(v.mean())
        if v.any():
            out["median_us_trade"] = float(np.median(hit[v == 1]) / 1e3)
        if (v == 0).any():
            out["median_us_cancel"] = float(np.median(hit[v == 0]) / 1e3)
    return out


# --------------------------------------------------------------------------- plotting
def _fmt_ns(ns):
    ns = abs(int(ns))
    if ns == 0:
        return "0"
    for unit, k in (("s", 1e9), ("ms", 1e6), ("µs", 1e3)):
        if ns >= k:
            return f"{ns / k:.3g}{unit}"
    return f"{ns}ns"


def plot_pair(sym, xc, lat, imp, imp_rev, ev, delay_p, path, clock):
    import matplotlib
    if not IN_NOTEBOOK:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(2, 2, figsize=(15, 9.5))
    fig.suptitle(f"{UNDERLYING} → {sym} lead/lag  (clock: ts_{clock}; positive lag = {UNDERLYING} first)", fontsize=13)

    # A
    a = ax[0, 0]
    sel = xc[(xc.lag_hi_ns.abs() <= 10_000_000) & (xc.lag_lo_ns.abs() <= 10_000_000)]
    labels = ["0" if r.lag_lo_ns == r.lag_hi_ns == 0 else
              (f"−{_fmt_ns(r.lag_lo_ns)}" if r.lag_hi_ns <= 0 else f"+{_fmt_ns(r.lag_hi_ns)}") for r in sel.itertuples()]
    col = ["#c0392b" if abs(z) > 3 else "#95a5a6" for z in sel.z.fillna(0)]
    a.bar(range(len(sel)), sel.signed_per_trigger * 100, color=col)
    a.set_xticks(range(len(sel)))
    a.set_xticklabels(labels, rotation=90, fontsize=7)
    a.axhline(0, color="k", lw=0.6)
    a.set_ylabel(f"net same-direction {sym} moves\nper 100 {UNDERLYING} moves")
    a.set_title("A. Signed cross-correlogram (bin upper edge; red = |z|>3)")

    # B
    b = ax[0, 1]
    h = lat[lat.lat_hi_ns > 0]
    xm = np.sqrt(h.lat_lo_ns * h.lat_hi_ns) / 1e3
    b.plot(xm, h.frac_obs * 100, "-", label=f"{sym} moves (observed)")
    b.plot(xm, h.frac_null * 100, "-", color="grey", label="null: same moves shifted 1–5 s")
    b.fill_between(xm, h.frac_null * 100, h.frac_obs * 100, where=h.frac_obs > h.frac_null, color="C3", alpha=0.25, label="excess")
    k = h.excess.idxmax()
    b.axvline(np.sqrt(h.lat_lo_ns[k] * h.lat_hi_ns[k]) / 1e3, color="C3", ls="--", lw=0.8,
              label=f"peak excess ≈ {_fmt_ns(np.sqrt(h.lat_lo_ns[k] * h.lat_hi_ns[k]))}")
    z = lat[lat.lat_hi_ns == 0].iloc[0]
    b.set_xscale("log")
    b.set_xlabel(f"time since last same-direction {UNDERLYING} move (µs)")
    b.set_ylabel("% of moves per bin (10 bins/decade)")
    b.legend(fontsize=8, title=f"same-ns: obs {z.frac_obs:.1%}, null {z.frac_null:.1%}", title_fontsize=8)
    b.set_title(f"B. Reaction latency of {sym} quote moves")

    # C
    c = ax[1, 0]
    for df, lab, sty in ((imp, f"{UNDERLYING} move → {sym} (÷L)", "-"), (imp_rev, f"{sym} move → {UNDERLYING} (×L)", "--")):
        x = df.tau_ns.to_numpy() / 1e3
        c.plot(x, df.beta, sty, marker="o", ms=3, label=lab)
        c.fill_between(x, df.beta - 2 * df.se, df.beta + 2 * df.se, alpha=0.2)
    c.axhline(1, color="k", lw=0.6)
    c.axhline(0, color="k", lw=0.6)
    c.axvline(0, color="k", lw=0.6)
    c.set_xscale("symlog", linthresh=0.01)
    c.set_xlabel("τ (µs)  [<0: move made before trigger; ≥0: cumulative response]")
    c.set_ylabel("fraction of move reflected")
    c.legend()
    c.set_title("C. Impulse response")

    # D
    d = ax[1, 1]
    for arr, lab in ((ev.delay_ns.to_numpy(), "after fair-value breach"), (delay_p, "placebo (random time)")):
        hit = np.sort(arr[arr >= 0]).astype(float)
        if len(hit):
            d.step(np.maximum(hit, 0.5) / 1e3, np.arange(1, len(hit) + 1) / len(arr), where="post", label=f"{lab} (n={len(arr)})")
    if len(ev):
        tr = ev[(ev.delay_ns >= 0) & (ev.via_trade == 1)].delay_ns.to_numpy()
        if len(tr):
            hit = np.sort(tr).astype(float)
            d.step(np.maximum(hit, 0.5) / 1e3, np.arange(1, len(hit) + 1) / len(ev), where="post", ls=":", label="…of which removed by a trade")
    if not len(ev):
        d.text(0.5, 0.5, "no fair-value breaches", ha="center", transform=d.transAxes)
    d.set_xscale("log")
    d.set_xlabel("time until stale quote moves (µs); 0 ns plotted at 0.5 ns")
    d.set_ylabel("cumulative fraction")
    if d.get_legend_handles_labels()[0]:
        d.legend(loc="upper left")
    d.set_title("D. Stale-quote response time")
    for axx in ax.ravel():
        axx.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    if IN_NOTEBOOK:
        from IPython.display import display
        display(fig)
    plt.close(fig)


# --------------------------------------------------------------------------- main
def main(
    dbn: str = DBN_PATH,          # path to the .dbn.zst
    cache: str | None = None,     # filtered-record cache (.npz); default: next to the DBN file
    no_cache: bool = False,       # True = re-decode the DBN even if a cache exists
    ids: str | None = None,       # manual instrument ids, e.g. "TSLA=1,TSLL=2,TSLR=3,TSLG=4"
    clock: str = "event",         # "event" = Nasdaq matching-engine time, "recv" = Databento capture time
    price: str = "mid",           # "mid" or "micro" (size-weighted microprice)
    breach_ticks: float = 0.0,    # test D: fair must clear the stale quote by this many ticks
    horizon_ms: float = 1000.0,   # test D: max wait for a stale quote to move
    rth: str = f"{RTH[0]}-{RTH[1]}",  # session window, America/New_York
    out: str = "leadlag_out",     # output folder
):
    """Run the full analysis. Call main() for defaults, or override by name, e.g.
    main(clock="recv", price="micro")."""
    if clock not in ("event", "recv"):
        raise ValueError("clock must be 'event' or 'recv'")
    if price not in ("mid", "micro"):
        raise ValueError("price must be 'mid' or 'micro'")
    args = argparse.Namespace(dbn=dbn, cache=cache, no_cache=no_cache, ids=ids, clock=clock, price=price,
                              breach_ticks=breach_ticks, horizon_ms=horizon_ms, rth=rth, out=out)

    if not HAVE_NUMBA:
        print("[warn] numba not installed: book replay will be very slow on a full day (pip install numba)")
    symbols = [UNDERLYING] + list(FUNDS)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    recs, day = get_records(args, symbols)
    s0, s1 = args.rth.split("-")
    rth = tuple(int(pd.Timestamp(f"{day} {h}", tz="America/New_York").tz_convert("UTC").value) for h in (s0, s1))
    lo, hi = rth[0] - 5 * 60 * 10**9, rth[1] + 60 * 10**9  # keep 5 min before open for as-of lookups

    series = {}
    print("\nRebuilding books")
    for s in symbols:
        t1 = time.time()
        bbo = build_bbo(recs[s])
        series[s] = make_series(bbo, s, args.clock, args.price, lo, hi)
        q = series[s]
        nm = int(((q.tm >= rth[0]) & (q.tm < rth[1])).sum())
        print(f"  {s}: {len(recs[s]):>12,} MBO msgs -> {len(bbo):>10,} BBO changes -> {nm:>9,} RTH {args.price} moves ({time.time() - t1:,.1f}s)")

    S = series[UNDERLYING]
    w = (S.tm >= rth[0]) & (S.tm < rth[1])
    St, Sx, Sdx = S.tm[w], S.xm[w], S.dxm[w]
    rng = np.random.default_rng(0)
    summary = {"date": str(day), "clock": args.clock, "price": args.price, "pairs": {}}

    for sym, fund in FUNDS.items():
        print(f"\n=== {UNDERLYING} vs {sym} ===")
        Lq = series[sym]
        wl = (Lq.tm >= rth[0]) & (Lq.tm < rth[1])
        Lt, Lx, Ldx = Lq.tm[wl], Lq.xm[wl], Lq.dxm[wl]
        res = {"tsla_moves": int(len(St)), "letf_moves": int(len(Lt))}

        xc = signed_xcorr(St, np.sign(Sdx).astype(np.int64), Lt, np.sign(Ldx).astype(np.int64), XCORR_EDGES)
        win = 1_000_000
        pos = xc[(xc.lag_lo_ns >= 0) & (xc.lag_hi_ns <= win) & (xc.lag_hi_ns > 0)]
        neg = xc[(xc.lag_hi_ns <= 0) & (xc.lag_lo_ns >= -win) & (xc.lag_lo_ns < 0)]
        zero = xc[(xc.lag_lo_ns == 0) & (xc.lag_hi_ns == 0)].iloc[0]
        best = xc.loc[xc.signed_pairs.idxmax()]
        res["xcorr"] = {
            "net_signed_after_le1ms": float(pos.signed_pairs.sum()),
            "net_signed_before_le1ms": float(neg.signed_pairs.sum()),
            "net_signed_same_ns": float(zero.signed_pairs),
            "peak_bin_ns": [int(best.lag_lo_ns), int(best.lag_hi_ns)],
        }
        print(f"  A  net same-direction {sym} moves within 1ms AFTER {UNDERLYING}: {pos.signed_pairs.sum():,.0f}  "
              f"BEFORE: {neg.signed_pairs.sum():,.0f}  same-ns: {zero.signed_pairs:,.0f}  "
              f"peak bin {_fmt_ns(best.lag_lo_ns)}..{_fmt_ns(best.lag_hi_ns)}")

        lat, lat_obs, lat_null = reaction_latency(St, np.sign(Sdx).astype(np.int64), Lt, np.sign(Ldx).astype(np.int64),
                                                  rth, rng, LAT_EDGES)
        h = lat[lat.lat_hi_ns > 0]
        k = h.excess.idxmax()
        peak = float(np.sqrt(h.lat_lo_ns[k] * h.lat_hi_ns[k]))
        z = lat[lat.lat_hi_ns == 0].iloc[0]
        ex1ms = float(lat[(lat.lat_hi_ns <= 1_000_000)].excess.clip(lower=0).sum())
        res["latency"] = {"peak_excess_ns": peak, "median_obs_ns": float(np.median(lat_obs)),
                          "median_null_ns": float(np.median(lat_null)), "same_ns_obs": float(z.frac_obs),
                          "same_ns_null": float(z.frac_null), "excess_mass_le1ms": ex1ms}
        print(f"  B  {sym} moves: peak excess latency ≈ {_fmt_ns(peak)} after a same-direction {UNDERLYING} move; "
              f"median {_fmt_ns(np.median(lat_obs))} vs null {_fmt_ns(np.median(lat_null))}; "
              f"same-ns {z.frac_obs:.1%} (null {z.frac_null:.1%}); excess mass ≤1ms {ex1ms:.0%}")

        imp = impulse(St, Sdx, Lq, S, IMPULSE_TAUS, fund["L"])
        imp_rev = impulse(Lt, Ldx, S, Lq, IMPULSE_TAUS, 1.0 / fund["L"])
        t50, ref = time_to_fraction(imp, 0.5)
        t90, _ = time_to_fraction(imp, 0.9)
        pre = imp[imp.tau_ns == IMPULSE_TAUS[0]].beta.iloc[0]
        res["impulse"] = {"beta_at_1s": ref, "t50_ns": t50, "t90_ns": t90,
                          "beta_at_0": float(imp[imp.tau_ns == 0].beta.iloc[0]), "pre_move_1ms": float(pre)}
        print(f"  C  {sym} reflects {ref:.2f} of L×{UNDERLYING} move by 1s; 50% by {_fmt_ns(t50) if t50 is not None else 'n/a'}, "
              f"90% by {_fmt_ns(t90) if t90 is not None else 'n/a'}; same-ns {res['impulse']['beta_at_0']:.3f}; "
              f"pre-move in prior 1ms {pre:.3f}")

        ev, delay_p, prem = breach_response(S, Lq, fund, PREV_CLOSE[UNDERLYING], PREV_CLOSE[sym], rth,
                                            args.breach_ticks, int(args.horizon_ms * 1e6), rng)
        ds, dp = delay_stats(ev.delay_ns, ev.via_trade), delay_stats(delay_p)
        res["breach"] = {"median_premium_vs_formula_bp": prem, "events": ds, "placebo": dp}
        print(f"  D  median {sym} premium vs formula fair value: {prem:+.1f} bp (level only; calibrated out)")
        if ds["n"]:
            print(f"     {ds['n']:,} breaches: median response {ds.get('p50_us', np.nan):,.2f} µs "
                  f"(p10 {ds.get('p10_us', np.nan):,.2f}, p90 {ds.get('p90_us', np.nan):,.2f}); "
                  f"≤10µs {ds.get('frac_le10us', 0):.0%}, same-ns {ds.get('frac_eq0ns', 0):.1%}; "
                  f"removed by trade {ds.get('via_trade_frac', 0):.0%} "
                  f"(median {ds.get('median_us_trade', np.nan):,.2f} µs via trade vs {ds.get('median_us_cancel', np.nan):,.2f} µs via cancel/requote)")
            print(f"     placebo: median {dp.get('p50_us', np.nan):,.1f} µs, ≤10µs {dp.get('frac_le10us', 0):.1%}")
        else:
            print("     no fair-value breaches found (try --breach-ticks 0)")

        xc.to_csv(out / f"{sym}_A_xcorr.csv", index=False)
        lat.to_csv(out / f"{sym}_B_latency.csv", index=False)
        imp.assign(direction=f"{UNDERLYING}->{sym}").pipe(
            lambda d: pd.concat([d, imp_rev.assign(direction=f"{sym}->{UNDERLYING}")])).to_csv(out / f"{sym}_C_impulse.csv", index=False)
        ev.to_csv(out / f"{sym}_D_breaches.csv", index=False)
        plot_pair(sym, xc, lat, imp, imp_rev, ev, delay_p, out / f"{sym}_leadlag.png", args.clock)
        summary["pairs"][sym] = res

    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=float))
    print(f"\nDone in {time.time() - t0:,.1f}s. Outputs in {out.resolve()}")


if __name__ == "__main__":
    main()