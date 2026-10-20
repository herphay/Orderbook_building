#!/usr/bin/env python3
"""Summarize MBO order activity into behavioral role proxies for one session.

These are observable order behaviors, not identities or verified participant types.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pandas as pd


ACTION = {"A": 65, "C": 67, "M": 77, "R": 82, "T": 84, "F": 70}
SIDE = {"B": 66, "A": 65}
NS_PER_MS = 1_000_000


def markdown_table(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "(no rows)"
    cols = [str(c) for c in frame.columns]
    vals = [[str(v) for v in row] for row in frame.itertuples(index=False, name=None)]
    widths = [max(len(cols[i]), *(len(row[i]) for row in vals)) for i in range(len(cols))]
    line = "| " + " | ".join(cols[i].ljust(widths[i]) for i in range(len(cols))) + " |"
    sep = "| " + " | ".join("-" * widths[i] for i in range(len(cols))) + " |"
    body = ["| " + " | ".join(row[i].ljust(widths[i]) for i in range(len(cols))) + " |" for row in vals]
    return "\n".join([line, sep, *body])


def analyze(cache: Path, out: Path, fleeting_ms: float, replenish_ms: float) -> dict:
    z = np.load(cache, allow_pickle=False)
    day = str(z["day"])
    out.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    symbol_events: dict[str, Counter] = {}
    trade_flow: list[dict] = []

    # Use (symbol, order_id), since the cache does not retain channel_id.
    states: dict[tuple[str, int], dict] = {}
    # Track the last fill event at each displayed price to identify replenishment behavior.
    last_fill: dict[tuple[str, int, int], int] = {}
    replenish_gap_ns = int(replenish_ms * NS_PER_MS)

    for symbol in ("TSLA", "TSLL", "TSLR", "TSLG"):
        rec = z[symbol]
        counts = Counter()
        for e in rec:
            action = int(e["action"])
            counts[chr(action) if action else "?"] += 1
            ts = int(e["ts_event"])
            oid = int(e["order_id"])
            px = int(e["price"])
            size = int(e["size"])
            side_code = int(e["side"])
            side = chr(side_code) if side_code in (65, 66) else "?"
            key = (symbol, oid)

            if action == ACTION["T"]:
                trade_flow.append({
                    "symbol": symbol, "ts_event": ts, "price_raw": px,
                    "size": size, "side": side,
                    "side_observed": side != "?",
                })
                continue
            if action == ACTION["A"] and oid:
                px_key = (symbol, side_code, px)
                prior_fill = last_fill.get(px_key)
                replenishes = prior_fill is not None and 0 <= ts - prior_fill <= replenish_gap_ns
                states[key] = {
                    "symbol": symbol, "order_id": oid, "side": side,
                    "add_ts": ts, "last_ts": ts, "price_raw": px,
                    "initial_size": size, "fill_size": 0, "cancel_size": 0,
                    "modify_count": 0, "replenish_after_fill": replenishes,
                }
                continue

            st = states.get(key)
            if st is None:
                continue
            st["last_ts"] = ts
            if action == ACTION["M"]:
                st["modify_count"] += 1
                if px > 0 and px != np.iinfo(np.int64).max:
                    st["price_raw"] = px
            elif action == ACTION["F"]:
                st["fill_size"] += size
                last_fill[(symbol, side_code, int(st["price_raw"]))] = ts
                if st["fill_size"] + st["cancel_size"] >= st["initial_size"]:
                    st["terminal"] = "filled"
                    rows.append(st.copy())
                    del states[key]
            elif action == ACTION["C"]:
                st["cancel_size"] += size
                # Nasdaq cancel messages can be partial. Close only when the recorded
                # canceled plus executed amount reaches the original displayed amount.
                if st["cancel_size"] + st["fill_size"] >= st["initial_size"]:
                    st["terminal"] = "cancel"
                    rows.append(st.copy())
                    del states[key]
            elif action == ACTION["R"]:
                # Clear records have no reliable per-order lifecycle attribution here.
                continue

        symbol_events[symbol] = counts

    # Remaining orders were still present at the end of the captured session/window.
    for st in states.values():
        st = st.copy()
        st["terminal"] = "open_at_end"
        rows.append(st)

    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError("No order lifecycles could be reconstructed from this cache")
    df["lifetime_us"] = (df["last_ts"] - df["add_ts"]) / 1_000
    df["filled"] = df["fill_size"] > 0
    df["cancelled"] = df["cancel_size"] > 0
    df["filled_fraction"] = np.minimum(df["fill_size"] / df["initial_size"].clip(lower=1), 1.0)
    df["behavior_group"] = np.select(
        [df["filled"], (~df["filled"]) & df["cancelled"] & (df["lifetime_us"] <= fleeting_ms * 1_000)],
        ["execution_touched displayed liquidity", "rapid cancel / fleeting quote"],
        default="persistent or unfilled displayed quote",
    )
    df["replenishment_marker"] = df["replenish_after_fill"].astype(bool)
    df.to_csv(out / "orders.csv", index=False)

    trades = pd.DataFrame(trade_flow)
    if trades.empty:
        trades = pd.DataFrame(columns=["symbol", "ts_event", "price_raw", "size", "side", "side_observed"])
    trades.to_csv(out / "trade_prints.csv", index=False)

    summaries = []
    for (symbol, group), g in df.groupby(["symbol", "behavior_group"], sort=False):
        summaries.append({
            "symbol": symbol, "behavior_group": group,
            "orders": int(len(g)), "initial_shares": int(g.initial_size.sum()),
            "filled_shares": int(g.fill_size.sum()), "cancel_event_shares_raw": int(g.cancel_size.sum()),
            "median_lifetime_us": float(g.lifetime_us.median()),
            "orders_with_modifications": int((g.modify_count > 0).sum()),
            "replenishment_marked_orders": int(g.replenishment_marker.sum()),
        })
    summary_df = pd.DataFrame(summaries)
    summary_df.to_csv(out / "group_summary.csv", index=False)

    event_rows = []
    for sym, cnt in symbol_events.items():
        for code, n in cnt.items():
            event_rows.append({"symbol": sym, "action": code, "events": n})
    pd.DataFrame(event_rows).to_csv(out / "event_counts.csv", index=False)

    trade_summary = []
    if len(trades):
        for sym, g in trades.groupby("symbol"):
            trade_summary.append({
                "symbol": sym, "trade_prints": len(g), "shares": int(g["size"].sum()),
                "side_known_prints": int(g.side_observed.sum()),
                "side_unknown_prints": int((~g.side_observed).sum()),
            })

    lines = [
        f"# MBO behavioral activity groups — {day}", "",
        f"Source cache: `{cache}`", "",
        "These labels summarize order behavior in the feed. They do not identify a person, firm, account, or stable actor. The cache omits MPID and channel_id; order IDs identify displayed orders, not participants.", "",
        f"Exclusive order labels use a {fleeting_ms:g} ms cutoff for unfilled orders canceled quickly. Replenishment is a separate marker: a new order at the same symbol, side, and price within {replenish_ms:g} ms after a recorded fill at that price.", "",
        "## Order behavior by symbol", "",
        markdown_table(summary_df) if len(summary_df) else "No order groups.", "",
        "## Trade-print flow", "",
        "Trade (`T`) messages are summarized separately because they do not provide a stable aggressor identity; side may also be absent.", "",
        markdown_table(pd.DataFrame(trade_summary)) if trade_summary else "No trade prints found.", "",
        "## Feed event counts", "",
        markdown_table(pd.DataFrame(event_rows)), "",
        "## Interpretation limits", "",
        "- Execution-touched displayed liquidity means the tracked resting order received at least one fill; it does not reveal who submitted it.",
        "- Rapid cancel / fleeting quote describes unfilled orders canceled within the selected lifetime cutoff. It is not evidence of spoofing or intent.",
        "- Persistent or unfilled displayed quote means the order remained longer or was present at the end of the captured stream; this does not prove market making.",
        "- Replenishment markers are price-level patterns, not proof the same participant replenished. Another participant can post at the same price.",
        "- `cancel_event_shares_raw` sums the raw size on Cancel records. Nasdaq execution messages normalize to Trade, Fill, then Cancel, so this value can overlap with filled shares and is not standalone cancellation volume.",
        "- Orders with lifecycle events missing from the filtered day, replace semantics, or cache boundaries can be misclassified. Results are exploratory proxies.", "",
        "Files: `group_summary.csv`, `orders.csv`, `trade_prints.csv`, and `event_counts.csv`.",
    ]
    (out / "report.md").write_text("\n".join(lines) + "\n")
    return {"day": day, "orders": len(df), "out": str(out), "summary": summary_df, "trade_summary": trade_summary}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--day", help="YYYYMMDD; defaults to latest cached session")
    ap.add_argument("--data-dir", type=Path, default=Path("XNAS_MBO"))
    ap.add_argument("--out", type=Path)
    ap.add_argument("--fleeting-ms", type=float, default=1.0)
    ap.add_argument("--replenish-ms", type=float, default=1.0)
    a = ap.parse_args()
    pattern = "xnas-itch-*.mbo.dbn.zst.TSLA-TSLL-TSLR-TSLG.npz"
    caches = sorted(a.data_dir.glob(pattern))
    if a.day:
        target = a.data_dir / f"xnas-itch-{a.day}.mbo.dbn.zst.TSLA-TSLL-TSLR-TSLG.npz"
    elif caches:
        target = caches[-1]
    else:
        ap.error(f"No caches found in {a.data_dir}")
    if not target.exists():
        ap.error(f"Cache not found: {target}")
    out = a.out or Path("analysis") / f"actor_groups_{target.name.split('xnas-itch-')[1][:8]}"
    result = analyze(target, out, a.fleeting_ms, a.replenish_ms)
    print(f"Analyzed {result['orders']:,} reconstructed order lifecycles for {result['day']}")
    print(f"Report: {out / 'report.md'}")
    print(result["summary"].to_string(index=False))


if __name__ == "__main__":
    main()
