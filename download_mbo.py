#!/usr/bin/env python3
"""Download daily XNAS.ITCH MBO files and build project caches."""
from __future__ import annotations

import argparse
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

import databento as db
import numpy as np

from utils import leadlag


DATASET = "XNAS.ITCH"
SYMBOLS = ["TSLA", "TSLL", "TSLR", "TSLG"]
CACHE_SUFFIX = ".TSLA-TSLL-TSLR-TSLG.npz"


def load_local_env() -> None:
    """Load DATABENTO_API_KEY from an ignored local .env if not already set."""
    env_file = Path(".env")
    if os.environ.get("DATABENTO_API_KEY") or not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() == "DATABENTO_API_KEY":
            os.environ["DATABENTO_API_KEY"] = value.strip().strip("\"'")
            return


def list_days(start: date, end: date, skip_dates: set[date]) -> list[date]:
    """Return weekdays in the half-open [start, end) window, minus skips."""
    days = []
    day = start
    while day < end:
        if day.weekday() < 5 and day not in skip_dates:
            days.append(day)
        day += timedelta(days=1)
    return days


def download_day(day: date, data_dir: Path) -> str:
    raw = data_dir / f"xnas-itch-{day:%Y%m%d}.mbo.dbn.zst"
    cache = Path(str(raw) + CACHE_SUFFIX)
    if raw.exists() and raw.stat().st_size and cache.exists() and cache.stat().st_size:
        return f"Already downloaded and cached {day}"

    if not raw.exists() or not raw.stat().st_size:
        # Keep a recognizable .zst suffix for the Databento encoder and only
        # publish the final filename when the whole response has arrived.
        partial = raw.with_name(raw.name + ".part.zst")
        last_error = None
        for attempt in range(1, 5):
            try:
                if partial.exists():
                    partial.unlink()
                client = db.Historical()  # separate client per worker
                print(f"Requesting {day} (attempt {attempt}/4)", flush=True)
                store = client.timeseries.get_range(
                    dataset=DATASET, schema="mbo", stype_in="raw_symbol",
                    symbols=SYMBOLS, start=day.isoformat(),
                    end=(day + timedelta(days=1)).isoformat(),
                )
                store.to_file(str(partial))
                # Databento may return a tiny metadata-only DBN file for a
                # weekday when the exchange is closed (for example, July 3,
                # 2026). Do not publish it as a usable session or cache it.
                if partial.stat().st_size < 1024:
                    partial.unlink()
                    return f"No market data returned for {day}; skipped (likely exchange holiday)"
                partial.replace(raw)
                last_error = None
                break
            except Exception as exc:  # retry transient gateway/network failures
                last_error = exc
                print(f"Request failed for {day}: {exc}", flush=True)
                if attempt < 4:
                    time.sleep(5 * attempt)
        if last_error is not None:
            raise RuntimeError(f"Failed to download {day} after 4 attempts: {last_error}")
        print(f"Downloaded {raw} ({raw.stat().st_size:,} bytes)", flush=True)

    if not cache.exists() or not cache.stat().st_size:
        records, data_day = leadlag.load_mbo(raw, SYMBOLS)
        cache_part = cache.with_name(cache.name + ".part")
        with cache_part.open("wb") as fh:
            np.savez(fh, day=str(data_day), **records)
        cache_part.replace(cache)
        print(f"Cached filtered records: {cache}", flush=True)

    return f"Completed {day}"


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start-day", default="2026-08-01",
                    help="Inclusive ISO date (default: 2026-08-01)")
    ap.add_argument("--end-day", default="2026-09-01",
                    help="Exclusive ISO date (default: 2026-09-01)")
    ap.add_argument("--workers", type=int, default=2,
                    help="Concurrent daily requests (default: 2)")
    ap.add_argument("--data-dir", type=Path, default=Path("XNAS_MBO"))
    ap.add_argument("--skip-date", action="append", default=[], metavar="YYYY-MM-DD",
                    help="Skip an exchange holiday; may be repeated")
    ap.add_argument("--estimate-only", action="store_true",
                    help="Print the Databento estimate without downloading")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    if args.workers < 1 or args.workers > 8:
        raise SystemExit("--workers must be between 1 and 8")
    try:
        start = date.fromisoformat(args.start_day)
        end = date.fromisoformat(args.end_day)
        skip_dates = {date.fromisoformat(x) for x in args.skip_date}
    except ValueError as exc:
        raise SystemExit(f"Dates must use YYYY-MM-DD: {exc}") from exc
    if end <= start:
        raise SystemExit("--end-day must be later than --start-day")

    load_local_env()
    if not os.environ.get("DATABENTO_API_KEY"):
        raise SystemExit("Set DATABENTO_API_KEY in the environment or ignored local .env")

    client = db.Historical()
    estimate = client.metadata.get_cost(
        dataset=DATASET, symbols=SYMBOLS, schema="mbo", stype_in="raw_symbol",
        start=start.isoformat(), end=end.isoformat(),
    )
    print(f"Databento estimate ({start} inclusive to {end} exclusive): ${estimate:.2f}", flush=True)
    if args.estimate_only:
        return

    days = list_days(start, end, skip_dates)
    if not days:
        raise SystemExit("No weekdays remain in the requested period")
    args.data_dir.mkdir(parents=True, exist_ok=True)
    print(f"Processing {len(days)} weekdays with {args.workers} worker(s)", flush=True)

    errors = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(download_day, day, args.data_dir): day for day in days}
        for future in as_completed(futures):
            day = futures[future]
            try:
                print(future.result(), flush=True)
            except Exception as exc:  # finish other independent dates, report failures together
                errors.append((day, exc))
                print(f"FAILED {day}: {exc}", flush=True)

    if errors:
        detail = "\n".join(f"  {day}: {exc}" for day, exc in errors)
        raise SystemExit(f"{len(errors)} day(s) failed; rerun the same command to resume:\n{detail}")
    print(f"Completed requested period under {args.data_dir}")


if __name__ == "__main__":
    main()
