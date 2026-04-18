"""Regression: Store.tx() must be safe for concurrent writers.

Production-path race: the scheduler fires N worker threads that all call
`claim_task`/`start_task`/`finish_task` in parallel. Before this test
landed, two threads would race on `BEGIN IMMEDIATE` and SQLite raised
`OperationalError: cannot start a transaction within a transaction`.

We reproduce by pounding the DB from many threads and assert every call
succeeds. Also exercises the RLock's same-thread re-entry path.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from super_q.db import Store


def test_many_threads_can_commit_concurrently(tmp_path: Path) -> None:
    store = Store(tmp_path / "s.db")
    jid = store.create_job(core_path=".", core_name="a.b", kind="sweep", spec={})
    store.start_job(jid)

    # 8 workers × 4 seeds each — previously failed within the first few ops.
    N_WORKERS = 8
    N_TASKS_PER_WORKER = 4

    task_ids: list[str] = []
    for _ in range(N_WORKERS * N_TASKS_PER_WORKER):
        task_ids.append(store.create_task(
            job_id=jid, seed=len(task_ids), backend="local",
        ))

    errors: list[BaseException] = []

    def worker(chunk: list[str]) -> None:
        try:
            for tid in chunk:
                store.claim_task(tid, f"w-{threading.get_ident():x}")
                store.start_task(tid)
                store.finish_task(tid, status="passed", slack_ns=0.1, fmax_mhz=75.0,
                                 timing={"passed": True}, rbf_path="/tmp/x", log_path=None)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    chunks = [
        task_ids[i * N_TASKS_PER_WORKER:(i + 1) * N_TASKS_PER_WORKER]
        for i in range(N_WORKERS)
    ]
    with ThreadPoolExecutor(max_workers=N_WORKERS) as ex:
        list(ex.map(worker, chunks))

    assert not errors, f"concurrent writers hit errors: {errors[:3]}"

    tasks = store.list_tasks(jid)
    assert len(tasks) == N_WORKERS * N_TASKS_PER_WORKER
    assert all(t["status"] == "passed" for t in tasks)


def test_nested_tx_same_thread_does_not_blow_up(tmp_path: Path) -> None:
    """RLock + in_transaction short-circuit means nested tx() on the same
    thread is safe (no more 'transaction within a transaction')."""
    store = Store(tmp_path / "s.db")
    with store.tx() as c:
        # Simulate a nested tx from within the same thread (pre-fix crash).
        with store.tx() as c2:
            c2.execute(
                "INSERT INTO jobs(id,core_path,core_name,kind,status,created_at,spec_json)"
                " VALUES('nested','.','a','build','queued',0,'{}')"
            )
    assert store.get_job("nested") is not None


def test_event_recording_during_finish_does_not_deadlock(tmp_path: Path) -> None:
    """finish_job internally calls record_event — both use tx(). Must not
    deadlock or error even under thread contention."""
    store = Store(tmp_path / "s.db")
    jid = store.create_job(core_path=".", core_name="a.b", kind="sweep", spec={})

    def cycle() -> None:
        for i in range(20):
            store.start_job(jid)
            store.finish_job(jid, status="passed", best_seed=i, best_slack_ns=0.1,
                             best_fmax_mhz=75.0, artifact_path=None, message="ok")
            store.record_event(jid, None, "ping", {"i": i})

    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(cycle) for _ in range(4)]
        for f in futs:
            f.result(timeout=10)

    assert store.get_job(jid)["status"] == "passed"
