from super_q.seeds import SeedPlan, SeedResult, chunk_plan, rank, summarize


def test_range_plan():
    p = SeedPlan.range(start=1, end=4)
    assert p.seeds == [1, 2, 3, 4]
    assert p.stop_on_first_pass is True


def test_spaced_plan_is_unique():
    p = SeedPlan.spaced(count=8)
    assert len(p.seeds) == 8
    assert len(set(p.seeds)) == 8


def test_random_plan_is_reproducible():
    a = SeedPlan.random(count=4, rng_seed=123)
    b = SeedPlan.random(count=4, rng_seed=123)
    assert a.seeds == b.seeds


def test_chunk_plan():
    p = SeedPlan(seeds=list(range(10)))
    chunks = list(chunk_plan(p, 3))
    assert chunks == [[0, 1, 2], [3, 4, 5], [6, 7, 8], [9]]


def test_rank_and_summary():
    r1 = SeedResult(seed=1, passed=True, slack_ns=0.5, fmax_mhz=75.0, duration_s=200)
    r2 = SeedResult(seed=2, passed=True, slack_ns=0.1, fmax_mhz=74.0, duration_s=180)
    r3 = SeedResult(seed=3, passed=False, slack_ns=-0.1, fmax_mhz=None, duration_s=220)
    ranked = rank([r1, r2, r3])
    assert ranked[0].seed == 1  # best slack wins
    s = summarize([r1, r2, r3], plan=SeedPlan(seeds=[1, 2, 3], stop_on_first_pass=False))
    assert s["passed"] == 2
    assert s["failed"] == 1
    assert s["best_seed"] == 1
