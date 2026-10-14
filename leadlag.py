"""
Build top-of-book (BBO) from Databento XNAS.ITCH MBO data, compute mid and
weighted mid for TSLA and its 2x LETFs, and measure how fast the LETFs reprice
relative to TSLA.

Install (Mac):  pip3 install databento sortedcontainers matplotlib
Run:            python3 letf_lag.py
"""

import numpy as np
import pandas as pd
import databento as db
from sortedcontainers import SortedDict

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
DBN_PATH = "XNAS_MBO/xnas-itch-20261009.mbo.dbn.zst"
DATE = "2026-10-09"
TZ = "America/New_York"

UNDERLYING = "TSLA"

# All three are 2x daily long Tesla funds. Expense ratios are approximate --
# check each fund's site. They only shift fair value by well under 1 bp/day.
LETFS = {
    "TSLL": {"L": 2.0, "E": 0.0083},   # Direxion Daily TSLA Bull 2X, 0.83
    "TSLR": {"L": 2.0, "E": 0.0095},   # GraniteShares 2x Long TSLA Daily, 0.95
    "TSLG": {"L": 2.0, "E": 0.0075},   # Leverage Shares 2X Long TSLA Daily, 0.75
}

# Financing / income terms from your daily formula (annual decimals).
COSTS = {
    "R": 0.0388,     # PLACEHOLDER: set to the current effective fed funds rate
    "P": 0.002,    # lending premium (~50 bp)
    "D_S": 0.0,    # TSLA dividend yield
    "W_E": 0.0,    # fund weight in direct equity holdings
    "W_T": 0.0,    # fund weight in treasuries
}

# Official closes on the PREVIOUS trading day (2026-10-08). LETFs reset to the
# underlying's return vs. this close, so these anchor the fair value.
# Leave as None to estimate them from the day's data instead (see fair_value()).
PREV_CLOSE = {"TSLA": 375.0, "TSLL": 10.01, "TSLR": 17.39, "TSLG": 5.0}

PRICE_COL = "wmid"                       # "mid" or "wmid" for the analysis
RTH_START, RTH_END = "09:35", "15:55"    # skip the opening/closing auctions
GRID_MS = 50                             # sampling grid for lead-lag
MAX_LAG_MS = 2000                        # lead-lag search window (+/-)

F_LAST = 128
PX_SCALE = 1e-9

# ----------------------------------------------------------------------------
# 1. Order book -> BBO
# ----------------------------------------------------------------------------
ACT_ADD, ACT_CANCEL, ACT_MODIFY, ACT_CLEAR = (ord(c) for c in "ACMR")
SIDE_BID, SIDE_ASK = ord("B"), ord("A")


class Book:
    """Per-instrument L3 book that keeps aggregated size per price level."""

    __slots__ = ("orders", "bids", "asks")

    def __init__(self):
        self.clear()

    def clear(self):
        self.orders = {}            # order_id -> [side, price, size]
        self.bids = SortedDict()    # price -> total size
        self.asks = SortedDict()

    def _level(self, side, px, dq):
        lv = self.bids if side == SIDE_BID else self.asks
        q = lv.get(px, 0) + dq
        if q > 0:
            lv[px] = q
        else:
            lv.pop(px, None)

    def add(self, oid, side, px, sz):
        if side != SIDE_BID and side != SIDE_ASK:
            return
        old = self.orders.get(oid)
        if old is not None:                       # defensive: duplicate add
            self._level(old[0], old[1], -old[2])
        self.orders[oid] = [side, px, sz]
        self._level(side, px, sz)

    def cancel(self, oid, sz):
        """Partial or full cancel. ITCH executions arrive as T, F, then C:
        the C removes the filled shares, so F is not applied separately."""
        o = self.orders.get(oid)
        if o is None:
            return False
        q = sz if sz < o[2] else o[2]
        self._level(o[0], o[1], -q)
        o[2] -= q
        if o[2] <= 0:
            del self.orders[oid]
        return True

    def modify(self, oid, side, px, sz):
        """Not emitted for XNAS.ITCH (replace = C + A), kept for other venues."""
        o = self.orders.pop(oid, None)
        if o is not None:
            self._level(o[0], o[1], -o[2])
        self.add(oid, side, px, sz)

    def top(self):
        bp, bs = self.bids.peekitem(-1) if self.bids else (-1, 0)
        ap, as_ = self.asks.peekitem(0) if self.asks else (-1, 0)
        return bp, bs, ap, as_


def load_mbo(path):
    store = db.DBNStore.from_file(path)
    df = store.to_df(price_type="fixed", pretty_ts=False, map_symbols=True)
    return df.reset_index()      # ts_recv becomes a column


def build_bbo(df):
    """Replay MBO records in file order. Emit a row whenever an instrument's
    top of book changes, sampled only at event boundaries (F_LAST)."""
    act = df["action"].to_numpy().astype("S1").view(np.uint8).tolist()
    side = df["side"].to_numpy().astype("S1").view(np.uint8).tolist()
    inst = df["instrument_id"].to_numpy().tolist()
    oid = df["order_id"].to_numpy().tolist()
    px = df["price"].to_numpy(np.int64).tolist()
    sz = df["size"].to_numpy().tolist()
    flg = df["flags"].to_numpy().tolist()
    ts_ev = df["ts_event"].to_numpy(np.int64)
    ts_rc = df["ts_recv"].to_numpy(np.int64)

    books, last_top, rows = {}, {}, []
    missing = 0

    for i in range(len(act)):
        iid = inst[i]
        bk = books.get(iid)
        if bk is None:
            bk = books[iid] = Book()

        a = act[i]
        if a == ACT_ADD:
            bk.add(oid[i], side[i], px[i], sz[i])
        elif a == ACT_CANCEL:
            if not bk.cancel(oid[i], sz[i]):
                missing += 1
        elif a == ACT_MODIFY:
            bk.modify(oid[i], side[i], px[i], sz[i])
        elif a == ACT_CLEAR:
            bk.clear()
        # T (trade), F (fill), N (none): no book change

        if flg[i] & F_LAST:
            top = bk.top()
            if top != last_top.get(iid):
                last_top[iid] = top
                rows.append((ts_ev[i], ts_rc[i], iid) + top)

    if missing:
        print(f"warning: {missing:,} cancels referenced unknown order_ids")

    bbo = pd.DataFrame(rows, columns=["ts_event", "ts_recv", "instrument_id",
                                      "bid_px", "bid_sz", "ask_px", "ask_sz"])
    sym = df.groupby("instrument_id")["symbol"].first()
    bbo["symbol"] = bbo["instrument_id"].map(sym)
    return add_prices(bbo)


def add_prices(bbo):
    for c in ("bid_px", "ask_px"):
        bbo[c] = bbo[c].where(bbo[c] >= 0).astype(float) * PX_SCALE
    two_sided = bbo["bid_px"].notna() & bbo["ask_px"].notna()
    bbo["mid"] = ((bbo["bid_px"] + bbo["ask_px"]) / 2).where(two_sided)
    # Weighted mid (microprice): leans toward the side with LESS size,
    # since the thinner side is more likely to be taken out next.
    tot = bbo["bid_sz"] + bbo["ask_sz"]
    bbo["wmid"] = ((bbo["bid_px"] * bbo["ask_sz"] + bbo["ask_px"] * bbo["bid_sz"])
                   / tot).where(two_sided & (tot > 0))
    bbo["spread"] = bbo["ask_px"] - bbo["bid_px"]
    bbo["crossed"] = two_sided & (bbo["spread"] <= 0)
    bbo["time"] = pd.to_datetime(bbo["ts_event"], utc=True).dt.tz_convert(TZ)
    return bbo


# ----------------------------------------------------------------------------
# 2. Sampling helpers
# ----------------------------------------------------------------------------
def rth_bounds_ns():
    s = pd.Timestamp(f"{DATE} {RTH_START}", tz=TZ).value
    e = pd.Timestamp(f"{DATE} {RTH_END}", tz=TZ).value
    return s, e


def sample_on_grid(bbo, symbol, col, grid):
    """Last known value of `col` at each grid time (previous-tick sampling)."""
    d = bbo[(bbo["symbol"] == symbol) & bbo[col].notna()]
    ts = d["ts_event"].to_numpy(np.int64)
    v = d[col].to_numpy(float)
    idx = np.searchsorted(ts, grid, side="right") - 1
    out = np.full(len(grid), np.nan)
    ok = idx >= 0
    out[ok] = v[idx[ok]]
    return out


# ----------------------------------------------------------------------------
# 3. Fair value of each LETF from the underlying
# ----------------------------------------------------------------------------
def daily_cost(L, E, R, P, D_S, W_E, W_T):
    """Non-price part of your daily return formula (top line):
    -L(R + P - D_S)/252 - E/252 + W_E*D_S/252 + W_T*R/252"""
    return (-L * (R + P - D_S) - E + W_E * D_S + W_T * R) / 252


def fair_value(s_px, letf_px, sym):
    """Fair LETF price from the underlying price path.

    Intraday the fund holds a fixed exposure set at the prior close, so
        fair_t = C_L * (1 + c + L * (S_t / C_S - 1))
    which is affine in S_t: fair_t = a + b * S_t.
    With PREV_CLOSE given, a and b come from the closes; otherwise they are
    fitted by OLS (premium is then relative: mean ~0 by construction)."""
    p = LETFS[sym]
    c = daily_cost(p["L"], p["E"], **COSTS)
    C_S, C_L = PREV_CLOSE.get(UNDERLYING), PREV_CLOSE.get(sym)

    if C_S and C_L:
        a = C_L * (1 + c - p["L"])
        b = C_L * p["L"] / C_S
        mode = "prev-close anchored"
    else:
        m = np.isfinite(s_px) & np.isfinite(letf_px)
        b, a = np.polyfit(s_px[m], letf_px[m], 1)
        C_L = a / (1 + c - p["L"])
        C_S = C_L * p["L"] / b
        mode = (f"OLS-fitted (implied prev closes: {UNDERLYING} {C_S:.2f}, "
                f"{sym} {C_L:.4f})")
    return a + b * s_px, mode


# ----------------------------------------------------------------------------
# 4. Lead-lag via cross-correlation of returns
# ----------------------------------------------------------------------------
def xcorr(x, y, max_lag):
    """corr(x_t, y_{t+k}) for k in [-max_lag, max_lag].
    Peak at k > 0 means y LAGS x by k grid steps."""
    lags = np.arange(-max_lag, max_lag + 1)
    out = np.empty(len(lags))
    n = len(x)
    for j, k in enumerate(lags):
        a, b = (x[:n - k], y[k:]) if k >= 0 else (x[-k:], y[:n + k])
        m = np.isfinite(a) & np.isfinite(b)
        out[j] = np.corrcoef(a[m], b[m])[0, 1]
    return lags, out


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    print("loading ...")
    df = load_mbo(DBN_PATH)
    print(f"{len(df):,} MBO records, symbols: {sorted(df['symbol'].unique())}")

    print("building books ...")
    bbo = build_bbo(df)
    del df
    print(f"{len(bbo):,} top-of-book updates")
    print(bbo.groupby("symbol").agg(updates=("mid", "size"),
                                    crossed=("crossed", "sum"),
                                    med_spread=("spread", "median")))
    try:
        1/0; bbo.to_parquet("bbo_20261009.parquet")
    except Exception:
        bbo.to_csv("bbo_20261009.csv.gz", index=False)

    # Regular-hours grid
    s, e = rth_bounds_ns()
    step = GRID_MS * 1_000_000
    grid = np.arange(s, e, step, dtype=np.int64)
    max_lag = MAX_LAG_MS // GRID_MS

    S = sample_on_grid(bbo, UNDERLYING, PRICE_COL, grid)
    r_S = np.diff(np.log(S))

    results, premia, curves = [], {}, {}
    for sym in LETFS:
        X = sample_on_grid(bbo, sym, PRICE_COL, grid)
        fair, mode = fair_value(S, X, sym)
        prem = (X / fair - 1) * 1e4
        premia[sym] = prem

        lags, cc = xcorr(r_S, np.diff(np.log(X)), max_lag)
        curves[sym] = (lags * GRID_MS, cc)
        k = lags[np.nanargmax(cc)]
        results.append({
            "symbol": sym,
            "fair_mode": mode,
            "premium_mean_bp": np.nanmean(prem),
            "premium_std_bp": np.nanstd(prem),
            "peak_lag_ms": k * GRID_MS,
            "peak_corr": np.nanmax(cc),
            "corr_at_0": cc[lags == 0][0],
        })

    res = pd.DataFrame(results).set_index("symbol")
    pd.set_option("display.width", 200, "display.max_colwidth", 80)
    print(res)

    plot(grid, S, bbo, premia, curves)
    return res


def plot(grid, S, bbo, premia, curves):
    import matplotlib.pyplot as plt

    t = pd.to_datetime(grid, utc=True).tz_convert(TZ)

    fig, ax = plt.subplots(figsize=(12, 4))
    ax.plot(t, S / S[np.isfinite(S)][0] - 1, label=UNDERLYING, lw=0.8)
    for sym in LETFS:
        X = sample_on_grid(bbo, sym, PRICE_COL, grid)
        ax.plot(t, (X / X[np.isfinite(X)][0] - 1) / LETFS[sym]["L"],
                label=f"{sym} / L", lw=0.8)
    ax.set_title(f"{PRICE_COL} return since {RTH_START} (LETFs de-levered)")
    ax.legend()
    fig.tight_layout()
    # fig.savefig("returns.png", dpi=130)

    fig, ax = plt.subplots(figsize=(12, 4))
    for sym, p in premia.items():
        ax.plot(t, p, label=sym, lw=0.6)
    ax.axhline(0, color="k", lw=0.5)
    ax.set_title("LETF premium vs. fair value (bp)")
    ax.legend()
    fig.tight_layout()
    # fig.savefig("premium.png", dpi=130)

    fig, ax = plt.subplots(figsize=(8, 4))
    for sym, (lag_ms, cc) in curves.items():
        ax.plot(lag_ms, cc, marker=".", label=sym)
    ax.axvline(0, color="k", lw=0.5)
    ax.set_xlabel(f"lag (ms); > 0 means LETF lags {UNDERLYING}")
    ax.set_ylabel("return correlation")
    ax.set_title(f"Lead-lag, {GRID_MS} ms grid")
    ax.legend()
    fig.tight_layout()
    # fig.savefig("leadlag.png", dpi=130)
    print("saved returns.png, premium.png, leadlag.png")


if __name__ == "__main__":
    main()