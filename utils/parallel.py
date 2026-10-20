"""Small process-pool helper for independent daily backtest sessions."""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Callable, Iterable, TypeVar

T = TypeVar("T")
R = TypeVar("R")


def run_sessions(worker: Callable[[T], R], tasks: Iterable[T], workers: int = 1):
    """Yield one worker result per session, as soon as that session completes.

    A single worker runs inline, which keeps small/debug runs simple. Worker
    functions must be module-level and receive picklable task values.
    """
    if workers < 1:
        raise ValueError("workers must be at least 1")
    task_list = list(tasks)
    if workers == 1:
        for task in task_list:
            yield worker(task)
        return
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(worker, task) for task in task_list]
        for future in as_completed(futures):
            yield future.result()
