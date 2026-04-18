# super-q for agents

This document is for automated agents (Claude Code, Cursor, etc.) that
want to build Analogue Pocket cores without human hand-holding. If
you're a human, start with [README.md](README.md).

## TL;DR for a new agent session

```bash
superq info --json                     # check environment + see available pools
superq verify ./my-core --json         # sanity-check a core
superq remote show --json              # what remote pools are configured?
superq sweep  ./my-core --json \
              --pool=modal --min=1 --max=16 --parallel=8 --stop-on-pass
```

Exit codes: `0` success, `1` user error, `2` toolchain/pool problem,
`3` timing failure, `4` cancelled or budget exhausted.

If the host can't run Quartus locally, pass `--pool=modal` (or `fly`,
`homelab`, `gha`) on every command. With a `default.pool` set in
`~/.superq/config.toml`, you can omit it. Agents should prefer remote
pools by default — local is only the right answer on a workstation
with Quartus installed.

## The one-paragraph mental model

You point super-q at a folder containing a Pocket core. It auto-detects
the Quartus project, copies the source tree into per-seed sandboxes,
compiles them in parallel (optionally sharing synthesis via
`--mode=split-fit`), parses the resulting STA reports, and saves the
best passing bitstream (byte-reversed for APF) at
`<core>/.superq/artifacts/latest/bitstream.rbf_r`. The whole run shows
up in a SQLite DB so you can query progress or kill it.

## Commands you will actually use

### `superq info --json`

Returns host + Quartus metadata. Call this first; you cannot build
without Quartus, and this will tell you if it's missing.

```json
{
  "version": "super-q · Quartus 24.1 · 16 cpus · 64GB",
  "quartus": { "installed": true, "version": "24.1", "root": "/opt/intelFPGA_lite/24.1/quartus" },
  "host": { "cpus": 16, "mem_gb": 64.0, "suggested_parallel": 4 },
  "issues": []
}
```

### `superq verify <path> --json`

Validates a folder looks like a Pocket core and prints warnings
about missing constraints or unexpected devices.

### `superq sweep <path> [options] --json`

The heart of the system. Every `--json` invocation emits
**newline-delimited events** during the run and a **final summary**
at the end. Events have shape `{"kind": "seed.started", ...}` etc.

Minimum arg: a path. Reasonable defaults: `--min=1 --max=16 --parallel=4
--stop-on-pass --mode=full --backend=local`.

Return value (last line) is a `SweepOutcome`:

```json
{
  "job_id": "abc123",
  "core": { "root": "/…/my-core", "project_name": "pocket" },
  "plan": { "seeds": [1,2,3,…,16], "stop_on_first_pass": true, "max_parallel": 4 },
  "results": [ { "seed": 1, "passed": false, "slack_ns": -0.123 }, … ],
  "summary": { "ran": 5, "passed": 1, "best_seed": 5 },
  "best":   { "seed": 5, "passed": true, "rbf_r_path": "…/bitstream.rbf_r" }
}
```

### `superq batch <paths…> --json`

Sweep many cores. Accepts core roots or a parent directory; anything
with a `.qpf` under a conventional layout is found.

```bash
superq batch ~/cores --parallel-cores=2 --parallel=4 --json > result.json
```

### `superq status [job-id] --json`

Without args, lists recent jobs. With a job id, returns the full task
breakdown (one row per seed).

### `superq watch <job-id>`

Blocking live dashboard. Good for a human watching; for agents,
poll `status` on an interval instead.

### `superq inspect <path> --json`

No rebuild. Re-parses the last compile's STA report and returns the
same timing shape as a sweep result. Useful after a teammate's manual
Quartus GUI run.

### `superq cancel <job-id>`

Cancels a running job. Returns number of tasks cancelled.

## MCP tool surface

When launched via `super-q-mcp`, the same capabilities appear as
typed tool calls. Schemas (abbreviated):

| tool                 | inputs                                             | returns                      |
|----------------------|----------------------------------------------------|------------------------------|
| `info`               | —                                                  | host + Quartus info          |
| `verify_core`        | `path`                                             | core metadata + warnings     |
| `find_cores`         | `paths[]`                                          | list of detected cores       |
| `build_core`         | `path`, `seed?`, `backend?`                        | `SweepOutcome` for one seed  |
| `sweep_seeds`        | `path`, `min_seed?`, `max_seed?`, `strategy?`, `parallel?`, `mode?`, `pool?`, `backend?` | full `SweepOutcome` |
| `batch_sweep`        | `paths[]`, `min_seed?`, `max_seed?`, `parallel?`   | list of outcomes             |
| `incremental_build`  | `path`, `seed?`, `use_warm_shell?`                 | fast rebuild result          |
| `explore`            | `path`, `budget_s?`, `parallel?`, `pool?`          | `ExploreOutcome` (rungs)     |
| `list_jobs`          | `status?`, `limit?`                                | job rows                     |
| `job_status`         | `job_id`                                           | job + all task rows          |
| `cancel_job`         | `job_id`                                           | `{cancelled: N}`             |
| `inspect_timing`     | `path`                                             | timing report                |
| `list_pools`         | —                                                  | named pools + default        |
| `daemon_ping`        | —                                                  | `{alive, uptime_s, backend}` |

All tools return JSON text content so you can re-parse on the agent
side without any special handling.

## Picking compute

Before picking any tool, decide where builds should run. Agents running
on laptops without Quartus installed should always use a remote pool.

```
list_pools()
  → if .pools is empty:
      ask the user to run `superq remote init` and configure Modal/Fly/SSH
      (or fall back to --pool=gha if they have a public repo on GitHub)
  → if default_pool is set:
      use it (no need to pass pool= on every call)
  → else:
      pass pool="modal" (or the best available) on every build
```

Modal is the most forgiving default — no cluster to provision, scales
to 0, per-second billing. Fly.io gives you a real VM. SSH is best if
you already own hardware. GHA is free-ish for public repos.

## Iteration loop (warm shell + incremental)

For edit-compile-test loops, skip seed sweeps entirely:

```
incremental_build(path=".", seed=7, use_warm_shell=True)
  → ~20 s–2 min per call; serial (holds a core-wide lock)
  → reuses Quartus partition cache in db/ and incremental_db/
```

Or run the adaptive budget explorer if you don't care how:

```
explore(path=".", budget_s=1800, parallel=8, pool="modal")
  → rungs: quick-range → wider-range → random → high-effort → retime
  → returns the first passing seed, or an audit trail if the budget ran out
```

## Workflow recipes

### "Build this core"

```
sweep_seeds(path=".", max_seed=8, parallel=4)
  → if result.best: ship result.best.rbf_r_path
  → else: sweep_seeds(path=".", strategy="random", count=16, rng_seed=42)
```

### "Get me the fastest build possible"

```
sweep_seeds(path=".", mode="split-fit", strategy="spaced", count=32, parallel=16,
            stop_on_pass=false)        # explore full space
  → pick result.summary.best_seed
  → report result.best.fmax_mhz and best_slack_ns
```

### "Check CI for every core"

```
batch_sweep(paths=["./cores"], min_seed=1, max_seed=4,
            parallel=4, parallel_cores=2)
  → for each outcome with best is None: open an issue
```

### "Investigate a timing failure"

```
inspect_timing(path=".")
  → look at clocks[].setup_slack_ns
  → look for the worst clock name — that's where you add constraints
```

## Things agents often get wrong

- **Don't set `--parallel` higher than CPUs/4.** Quartus fitter
  peaks at ~4–8 GB; over-parallelism swaps and gets slower.
- **Don't pass `--stop-on-pass` when you want to find the *best* seed.**
  It exits on the first passing seed, which is often not the fastest.
  Use `--no-stop-on-pass` for exploration.
- **The `.rbf_r` is the shippable artifact, not the `.rbf`.** Look at
  `result.best.rbf_r_path`, not `rbf_path`.
- **Check `--json` output line-by-line during a sweep.** Intermediate
  lines are event objects, not the final result. Final `SweepOutcome`
  is the last line.
- **`--mode=split-fit` requires Quartus 21.1+.** The CLI silently
  falls back to `full` if the checkpoint export fails, so don't assume
  every seed is checkpoint-reused; check `event.kind=="synth.failed"`.

## Where the bitstream ends up

After a successful sweep, the winning `.rbf_r` is copied to:

```
<core>/.superq/artifacts/<job-id>/seed-<NNNN>/bitstream.rbf_r
<core>/.superq/artifacts/latest               (symlink → most recent)
```

If the core has a canonical `dist/Cores/<Author>.<Name>/` directory,
super-q *also* copies the `.rbf_r` there as `bitstream.rbf_r` so
distribution scripts don't need to re-encode.

## Typical session durations

| workload                  | local workstation (16 cpu)     | AWS c7i.4xlarge spot          |
|---------------------------|---------------------------------|-------------------------------|
| single seed, small core   | 2–4 min                         | 3–5 min                       |
| 16-seed sweep, full       | 8–14 min                        | 8–14 min                      |
| 16-seed sweep, split-fit  | 4–8 min                         | 4–8 min                       |
| batch of 8 cores × 8 seeds| 25–45 min                       | 12–20 min (8 instances)       |

Use these to decide whether to let the user wait synchronously or
kick off a background job and poll.
