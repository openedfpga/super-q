"""Seed exploration plan + results.

"Seed sweep" is the Quartus vernacular for running the fitter N times with
different RNG seeds to try to find a placement that meets timing. It's
embarrassingly parallel; this module describes the plan (which seeds, how
to stop early) independently of how we execute it.
"""
from __future__ import annotations

import random
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field


@dataclass
class SeedPlan:
    """Describes a sweep — enough info to replay it deterministically."""

    seeds: list[int]
    stop_on_first_pass: bool = True
    target_slack_ns: float = 0.0
    target_fmax_mhz: float | None = None
    max_parallel: int = 4

    @classmethod
    def range(cls, start: int = 1, end: int = 16, **kwargs) -> "SeedPlan":
        return cls(seeds=list(range(start, end + 1)), **kwargs)

    @classmethod
    def random(cls, count: int = 16, *, rng_seed: int = 0, **kwargs) -> "SeedPlan":
        """Draw `count` seeds from the full 16-bit range, stable per rng_seed."""
        rng = random.Random(rng_seed)
        # Quartus accepts any 32-bit int but historically seeds are kept small.
        seeds = rng.sample(range(1, 65535), count)
        return cls(seeds=seeds, **kwargs)

    @classmethod
    def spaced(cls, count: int = 16, **kwargs) -> "SeedPlan":
        """Uniformly-spaced seeds — covers the space without duplicates."""
        if count < 1:
            count = 1
        step = max(1, 65530 // count)
        seeds = [1 + i * step for i in range(count)]
        return cls(seeds=seeds, **kwargs)

    def as_dict(self) -> dict:
        return {
            "seeds": list(self.seeds),
            "stop_on_first_pass": self.stop_on_first_pass,
            "target_slack_ns": self.target_slack_ns,
            "target_fmax_mhz": self.target_fmax_mhz,
            "max_parallel": self.max_parallel,
        }


@dataclass
class SeedResult:
    """Outcome of a single seed attempt within a sweep."""

    seed: int
    passed: bool
    slack_ns: float | None
    fmax_mhz: float | None
    duration_s: float
    rbf_r_path: str | None = None
    error: str | None = None
    timing: dict | None = field(default=None)

    @property
    def score(self) -> float:
        """Higher is better. Used to pick the best seed once a sweep ends.

        We reward positive slack linearly and penalize duration lightly so
        two equally-timing-clean seeds prefer the faster one. Failed runs
        are worst.
        """
        if not self.passed or self.slack_ns is None:
            return float("-inf")
        return self.slack_ns - (self.duration_s / 3600.0)

    def as_dict(self) -> dict:
        return {
            "seed": self.seed,
            "passed": self.passed,
            "slack_ns": self.slack_ns,
            "fmax_mhz": self.fmax_mhz,
            "duration_s": self.duration_s,
            "rbf_r_path": self.rbf_r_path,
            "error": self.error,
        }


def rank(results: Iterable[SeedResult]) -> list[SeedResult]:
    return sorted(results, key=lambda r: r.score, reverse=True)


def chunk_plan(plan: SeedPlan, batch_size: int) -> Iterator[list[int]]:
    """Yield successive `batch_size` chunks of the plan's seeds."""
    xs = list(plan.seeds)
    for i in range(0, len(xs), batch_size):
        yield xs[i : i + batch_size]


def summarize(results: list[SeedResult], *, plan: SeedPlan) -> dict:
    """A compact summary object suitable for CLI output or MCP responses."""
    ranked = rank(results)
    passed = [r for r in results if r.passed]
    best = ranked[0] if ranked and ranked[0].score != float("-inf") else None
    return {
        "total": len(plan.seeds),
        "ran": len(results),
        "passed": len(passed),
        "failed": len(results) - len(passed),
        "best_seed": best.seed if best else None,
        "best_slack_ns": best.slack_ns if best else None,
        "best_fmax_mhz": best.fmax_mhz if best else None,
        "best_rbf_r": best.rbf_r_path if best else None,
        "early_exit": plan.stop_on_first_pass and bool(passed),
    }
