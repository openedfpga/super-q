from pathlib import Path

from super_q.db import Store


def test_job_and_task_lifecycle(tmp_path: Path) -> None:
    store = Store(tmp_path / "s.db")
    jid = store.create_job(
        core_path="/tmp/core", core_name="a.b", kind="sweep",
        spec={"plan": {"seeds": [1, 2]}},
    )
    store.start_job(jid)
    t1 = store.create_task(job_id=jid, seed=1, backend="local")
    t2 = store.create_task(job_id=jid, seed=2, backend="local")

    assert store.claim_task(t1, "w1")
    assert not store.claim_task(t1, "w1")  # second claim must fail
    store.start_task(t1)
    store.finish_task(t1, status="passed", slack_ns=0.1, fmax_mhz=80.0,
                      timing={"passed": True}, rbf_path="/tmp/x.rbf_r",
                      log_path="/tmp/x.log")

    store.claim_task(t2, "w1")
    store.finish_task(t2, status="failed", error="boom")

    tasks = store.list_tasks(jid)
    assert len(tasks) == 2
    assert {t["status"] for t in tasks} == {"passed", "failed"}

    store.finish_job(jid, status="passed", best_seed=1, best_slack_ns=0.1,
                    best_fmax_mhz=80.0, artifact_path="/tmp/x.rbf_r",
                    message="ok")
    job = store.get_job(jid)
    assert job and job["status"] == "passed" and job["best_seed"] == 1


def test_worker_heartbeat(tmp_path: Path) -> None:
    store = Store(tmp_path / "s.db")
    store.register_worker("w1", host="h", backend="local", slots=4, info={"ok": 1})
    store.heartbeat("w1")
    assert len(store.live_workers()) == 1


def test_events(tmp_path: Path) -> None:
    store = Store(tmp_path / "s.db")
    jid = store.create_job(core_path=".", core_name="x", kind="build", spec={})
    store.record_event(jid, None, "note", {"a": 1})
    evts = store.tail_events()
    assert any(e["kind"] == "note" for e in evts)
