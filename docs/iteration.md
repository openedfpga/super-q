# Iterative workflows

super-q has two iteration features that should feel familiar to anyone
who's used `cargo watch` or `pytest -f`:

1. **`superq watch-build`** — debounced filesystem watcher that kicks
   off an incremental rebuild on every source change.
2. **`superq daemon`** — long-running local dispatcher that keeps
   Python + Quartus warm so requests return in <1 s of overhead.

And three build modes with different speed/power trade-offs:

| mode         | parallel?  | synth per seed | wall-clock (typical) | when                     |
|--------------|:----------:|:--------------:|:---------------------|:-------------------------|
| `full`       | yes        | yes            | 4–8 min / seed       | default, safe            |
| `split-fit`  | yes        | no (shared)    | 1–3 min / seed       | seed sweeps              |
| `incremental`| **no**     | skipped where possible | 20 s – 2 min | edit loops               |

## Quick recipes

### Edit → rebuild loop

```bash
superq watch-build ./my-core --seed=7
```

Every save rebuilds incrementally, reusing the warm `quartus_sh` shell.
Results go to `.superq/artifacts/watch-*/seed-0007/`.

### "Is this change still timing-clean?"

```bash
superq daemon start --pool=local --fg &   # pays startup cost once
for f in src/*.sv; do
    nvim "$f"                              # … edit …
done
superq build . --seed=7 --incremental --json   # sub-second local dispatch
```

Subsequent `build` calls route through the daemon, so each one is just
the incremental Quartus work — no Python import, no project-open cost.

### "Find a passing seed, I don't care how long it takes"

```bash
superq explore . --budget=30m --parallel=8
```

Escalates through rungs (quick range → wider → random → high-effort →
retime) until it passes or runs out of budget.

### "Run an exploration remotely while I keep working"

```bash
superq daemon start --pool=modal
superq explore . --budget=2h --parallel=32 --pool=modal &
# keep editing… watch-build uses local + warm shell unaffected
```

## Warm shell details

When `watch-build --warm` is on (the default), super-q spawns a
`quartus_sh -t warm_shell.tcl` once and feeds it requests over
stdin/stdout. This skips:

  * the 3–5 s Quartus JVM startup
  * the 2–4 s `project_open` cost
  * the Python `import super_q.*` (~300 ms)

Net: a small-edit incremental that would take ~60 s cold drops to
~25 s warm. Multiple such rebuilds compound the win; a 10-edit
iteration session saves ~6 minutes of wall time.

## When to use the daemon vs the CLI directly

You probably want the daemon when:

  * You fire multiple builds per minute.
  * You're on an underpowered laptop and dispatch everything to remote
    (the daemon keeps one open connection vs. handshaking each time).
  * Multiple shell sessions/agents coordinate on one project.

You probably don't need it when:

  * You run one sweep per hour.
  * You're on a beefy workstation and the 2 s of Python startup is
    dwarfed by the 3 min compile.

The daemon and direct CLI both hit the same SQLite state DB, so you
can start using it mid-session without losing history.
