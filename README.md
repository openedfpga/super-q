# super-q

**Ultra-fast distributed Quartus build system for Analogue Pocket cores.**
Seed sweeps, incremental rebuilds, warm Quartus shells, remote fan-out
to Modal / Fly / SSH / GitHub Actions, and end-to-end CI.

```
super-q · Quartus 24.1 · 16 cpus · 64GB

$ superq sweep ./openfpga-GBC --parallel=16 --pool=modal
seed sweep — elapsed 2m14s
┏━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃   seed ┃ status  ┃    slack (ns) ┃ Fmax (MHz) ┃     time ┃ detail                       ┃
┡━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│      3 │ passed  │       +0.082 │      74.51 │    3m56s │                              │
│      1 │ failed  │       -0.123 │      73.50 │    3m44s │ timing not met               │
│      2 │ running │            — │          — │        — │                              │
└────────┴─────────┴──────────────┴────────────┴──────────┴──────────────────────────────┘
✓ passed seed=3 slack=+0.082ns Fmax=74.51
```

## What you get

- **Local speed**: warm `quartus_sh` shell + incremental compile cuts
  per-iteration cost to ~20 s.
- **Remote fan-out** without the AWS tax: Modal, Fly.io Machines, an SSH
  pool of your own boxes, or GitHub Actions as free compute.
- **Iterative**: `watch-build` rebuilds on every save; `explore` runs an
  adaptive ladder until it passes or the budget runs out; `daemon`
  keeps Quartus warm across dozens of requests.
- **CI-native**: `superq ci build` auto-detects GHA/GitLab/Circle,
  emits annotations + outputs, uploads the winning `.rbf_r`.
- **Agent-native**: every command speaks JSON; an MCP server exposes
  the whole toolkit.

## Install

```bash
pip install 'super-q[all]'        # or pick extras: modal, fly, gha, watch, mcp
```

Then choose your compute path:

```bash
# Local only (needs Quartus Lite 24.1 installed):
bash scripts/install-quartus.sh --version=24.1 --accept-eula

# Modal (serverless, recommended):
pip install 'super-q[modal]'
modal deploy super_q.modal_app    # see docs/remote.md

# Fly.io / SSH pool / GitHub Actions: see docs/remote.md
```

## Tour

```bash
superq info                          # environment sanity-check
superq verify ./my-core              # does this folder look buildable?

# one-shot build
superq build ./my-core --seed=7

# parallel seed sweep (first-pass exit)
superq sweep ./my-core --min=1 --max=32 --parallel=8

# adaptive budget-bounded exploration
superq explore ./my-core --budget=30m --parallel=8

# rebuild on every source edit, warm shell
superq watch-build ./my-core --seed=7

# start a warm daemon; every subsequent call is fast
superq daemon start --pool=modal
superq build . --seed=7 --incremental --json

# batch across dozens of cores
superq batch ./cores --parallel=4 --parallel-cores=2

# CI
superq ci build ./my-core --pool=modal --max=16
```

All commands take `--json` for agent-friendly output.

## Remote worker pools

super-q treats compute as pluggable. Define pools once in
`~/.superq/config.toml`, then refer to them by name:

```toml
[pool.modal]
kind = "modal"
app  = "super-q"
max_parallel = 32

[pool.homelab]
kind = "ssh"
hosts = ["build1.local", "build2.local"]
user = "superq"
slots_per_host = 4

[default]
pool = "modal"
```

```bash
superq sweep ./core --pool=modal           # serverless, scale to any N
superq sweep ./core --pool=homelab         # your Linux boxes
superq sweep ./core --pool=gha             # CI minutes as compute
superq sweep ./core --pool=local           # beefy workstation
```

See [docs/remote.md](docs/remote.md) for the full recipes.

## Iteration features

| need                                   | command                                 |
|----------------------------------------|-----------------------------------------|
| Rebuild on save                        | `superq watch-build .`                  |
| Sub-second request latency             | `superq daemon start` + `build --incremental` |
| "Just make it pass, I'll wait"         | `superq explore . --budget=30m`         |
| Keep Quartus warm between invocations  | `--warm` (on by default)                |
| Skip work for unchanged partitions     | incremental mode (watch/build)          |

See [docs/iteration.md](docs/iteration.md).

## CI

```yaml
# .github/workflows/ci.yml
jobs:
  build:
    runs-on: ubuntu-22.04
    steps:
      - uses: actions/checkout@v4
      - uses: super-q/action@v1
        id: build
        with:
          core-path: .
          seeds: "1-8"
          parallel: "4"
          pool: modal        # or local to use the GH runner
      - run: echo "won with seed ${{ steps.build.outputs.best-seed }}"
```

`superq ci build` auto-detects GitHub/GitLab/Circle/Buildkite and
emits native annotations + outputs. See [docs/ci.md](docs/ci.md).

## Speed techniques

| technique                              | where                                        |
|----------------------------------------|----------------------------------------------|
| parallel seed sweep, first-pass exit   | [scheduler.py](src/super_q/scheduler.py)     |
| split-fit (shared `.qdb` checkpoint)   | [quartus.py](src/super_q/quartus.py)         |
| warm `quartus_sh` persistent TCL REPL  | [warm_shell.py](src/super_q/warm_shell.py) + [tcl/warm_shell.tcl](tcl/warm_shell.tcl) |
| incremental partition reuse            | [incremental.py](src/super_q/incremental.py) + [tcl/incremental_build.tcl](tcl/incremental_build.tcl) |
| adaptive explore ladder                | [explore.py](src/super_q/explore.py)         |
| per-task sandbox (no seed collision)   | [quartus.py:prepare_work_dir](src/super_q/quartus.py) |
| HIGH PERF EFFORT + router MAX + retime | [tcl/build_seed.tcl](tcl/build_seed.tcl)     |
| structured timing JSON from TCL        | [tcl/common.tcl](tcl/common.tcl)             |

## Where artifacts land

```
<core>/.superq/artifacts/<job-id>/seed-<NNNN>/
    bitstream.rbf            (raw Quartus output)
    bitstream.rbf_r          (byte-reversed — the shippable file)
    bitstream.sha256
    timing.json              (structured STA)
    <project>.sta.rpt        (raw STA text)
    build.log

<core>/.superq/artifacts/latest   → symlink to the most recent passing seed
```

If the repo has a canonical `dist/Cores/<Author>.<Name>/` folder, the
winning `.rbf_r` is copied there automatically.

## Agent integration

```json
{ "mcpServers": { "super-q": { "command": "super-q-mcp" } } }
```

Tools exposed: `info`, `verify_core`, `find_cores`, `build_core`,
`sweep_seeds`, `batch_sweep`, `incremental_build`, `explore`,
`list_jobs`, `job_status`, `cancel_job`, `inspect_timing`,
`list_pools`, `daemon_ping`.

See [AGENTS.md](AGENTS.md) for a step-by-step agent playbook.

## Docs

- [docs/remote.md](docs/remote.md) — Modal / Fly / SSH / GHA recipes
- [docs/iteration.md](docs/iteration.md) — watch / daemon / incremental
- [docs/ci.md](docs/ci.md) — GHA Action + GitLab pipeline
- [AGENTS.md](AGENTS.md) — tool catalog + common workflows
- [examples/agent_example.md](examples/agent_example.md) — sample session

## License

MIT. Quartus Prime Lite ships under Intel's license — super-q locates
it, never redistributes.
