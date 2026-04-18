"""Adaptive seed explorer with a time budget.

Agents often don't care *how* super-q finds a passing seed — they just
want "try everything reasonable until it works or the clock runs out."
`explore(core, budget=30m)` runs an escalation ladder:

  Rung 1: quick range sweep (seeds 1–4)
  Rung 2: wider range (5–16)
  Rung 3: random seeds (sampled across the space)
  Rung 4: high-effort global optimization (PERF_OPTIMIZATION_TECHNIQUE)
  Rung 5: relaxed placement + fitter retry

Each rung is skipped if the previous one passed, and the ladder bails
out early when either:
  * a passing seed is found, OR
  * the wall-clock budget is exhausted.

The function returns a consolidated `ExploreOutcome` with the full
audit trail (which rungs ran, how long they took, why they stopped).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from super_q.backends.base import Backend
from super_q.db import Store
from super_q.project import PocketCore
from super_q.scheduler import Scheduler, SweepOutcome
from super_q.seeds import SeedPlan, SeedResult

log = logging.getLogger("superq.explore")


@dataclass
class Rung:
    name: str
    plan_factory: Any               # Callable[[int], SeedPlan]
    mode: str = "full"
    extra_assignments: dict[str, str] = field(default_factory=dict)
    description: str = ""


@dataclass
class RungResult:
    name: str
    started_at: float
    duration_s: float
    outcome: SweepOutcome
    stopped_reason: str


@dataclass
class ExploreOutcome:
    core: PocketCore
    budget_s: int
    rungs: list[RungResult]
    best: SeedResult | None
    total_duration_s: float
    timed_out: bool

    def as_dict(self) -> dict:
        return {
            "core": self.core.as_dict(),
            "budget_s": self.budget_s,
            "rungs": [
                {
                    "name": r.name,
                    "duration_s": r.duration_s,
                    "stopped_reason": r.stopped_reason,
                    "outcome": r.outcome.as_dict(),
                }
                for r in self.rungs
            ],
            "best": self.best.as_dict() if self.best else None,
            "total_duration_s": self.total_duration_s,
            "timed_out": self.timed_out,
        }


def default_ladder(parallel: int) -> list[Rung]:
    """The escalation ladder we use when the caller doesn't override it."""
    return [
        Rung(
            name="quick-range",
            plan_factory=lambda p=parallel: SeedPlan.range(1, 4, max_parallel=p),
            description="4 seeds, full compile, fast confirmation pass",
        ),
        Rung(
            name="wider-range",
            plan_factory=lambda p=parallel: SeedPlan.range(5, 16, max_parallel=p),
            mode="split-fit",
            description="12 seeds, synth shared across fitter runs",
        ),
        Rung(
            name="random-sample",
            plan_factory=lambda p=parallel: SeedPlan.random(count=16, rng_seed=1, max_parallel=p),
            mode="split-fit",
            description="16 random seeds across full 16-bit space",
        ),
        Rung(
            name="high-effort",
            plan_factory=lambda p=parallel: SeedPlan.spaced(count=8, max_parallel=p),
            mode="full",
            extra_assignments={
                "PHYSICAL_SYNTHESIS_EFFORT": "EXTRA",
                "PLACEMENT_EFFORT_MULTIPLIER": "2.0",
                "ROUTER_EFFORT_MULTIPLIER": "4.0",
                "OPTIMIZATION_TECHNIQUE": "SPEED",
            },
            description="8 seeds w/ maxed fitter/router effort",
        ),
        Rung(
            name="last-chance",
            plan_factory=lambda p=parallel: SeedPlan.random(count=32, rng_seed=42, max_parallel=p),
            mode="full",
            extra_assignments={
                "ALLOW_REGISTER_RETIMING": "ON",
                "PHYSICAL_SYNTHESIS_REGISTER_RETIMING": "ON",
                "FINAL_PLACEMENT_OPTIMIZATION": "ALWAYS",
            },
            description="32 random seeds + retiming — everything we've got",
        ),
    ]


def explore(
    store: Store,
    backend: Backend,
    core: PocketCore,
    *,
    budget_s: int = 30 * 60,
    parallel: int = 4,
    threads_per_task: int = 2,
    ladder: list[Rung] | None = None,
    on_event: Any = None,
) -> ExploreOutcome:
    """Run the adaptive ladder until pass or budget exhausted."""
    ladder = ladder or default_ladder(parallel)
    sched = Scheduler(store, backend, on_event=on_event)

    rungs: list[RungResult] = []
    best: SeedResult | None = None
    deadline = time.time() + budget_s
    started = time.time()

    for rung in ladder:
        if best is not None:
            break
        remaining = deadline - time.time()
        if remaining <= 0:
            log.info("budget exhausted before %s", rung.name)
            break

        log.info("rung %s (%s) — %.0fs budget left", rung.name, rung.description, remaining)
        if on_event:
            on_event("explore.rung_started", {
                "rung": rung.name,
                "description": rung.description,
                "remaining_s": remaining,
            })

        plan = rung.plan_factory()
        # Force stop_on_pass for each rung — adaptive by definition.
        plan.stop_on_first_pass = True

        started_at = time.time()
        per_rung_timeout = min(remaining, 60 * 60)
        outcome = sched.run_sweep(
            core, plan,
            mode=rung.mode,
            threads_per_task=threads_per_task,
            timeout_s=int(per_rung_timeout),
            extra_assignments=rung.extra_assignments or None,
        )

        duration = time.time() - started_at
        stopped = "passed" if outcome.best else (
            "budget" if time.time() >= deadline else "failed"
        )
        rungs.append(RungResult(
            name=rung.name,
            started_at=started_at,
            duration_s=duration,
            outcome=outcome,
            stopped_reason=stopped,
        ))

        if on_event:
            on_event("explore.rung_finished", {
                "rung": rung.name,
                "duration_s": duration,
                "passed": outcome.best is not None,
                "best_seed": outcome.best.seed if outcome.best else None,
            })

        if outcome.best is not None:
            best = outcome.best
            break

    total = time.time() - started
    return ExploreOutcome(
        core=core,
        budget_s=budget_s,
        rungs=rungs,
        best=best,
        total_duration_s=total,
        timed_out=(best is None and time.time() >= deadline),
    )
