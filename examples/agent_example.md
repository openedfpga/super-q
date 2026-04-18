# Example agent session

A transcript-style walkthrough of a Claude agent using super-q to ship a
new Pocket core. Each step shows the tool call and the relevant part of
the response.

## 1. Sanity-check the environment

```
# tool: info
{
  "version": "super-q · Quartus 24.1 · 16 cpus · 64GB",
  "quartus": { "installed": true, "version": "24.1" },
  "issues": []
}
```

Good — Quartus is here.

## 2. Verify the core

```
# tool: verify_core
# input: {"path": "/Users/dev/cores/super-tile-matcher"}
{
  "ok": true,
  "core": {
    "project_name": "pocket",
    "qpf": "/Users/dev/cores/super-tile-matcher/src/fpga/pocket.qpf",
    "device": "5CEBA4F23C8",
    "author": "dev",
    "core_name": "super-tile-matcher"
  }
}
```

## 3. Run a seed sweep

```
# tool: sweep_seeds
# input:
{
  "path": "/Users/dev/cores/super-tile-matcher",
  "min_seed": 1,
  "max_seed": 16,
  "parallel": 8,
  "mode": "split-fit"
}
```

Stream of events during the run:

```
{"kind": "job.started", "job_id": "a1b2c3", "core": "dev.super-tile-matcher"}
{"kind": "synth.started", "job_id": "a1b2c3"}
{"kind": "synth.finished", "job_id": "a1b2c3", "qdb": "/.../pocket.qdb"}
{"kind": "seed.started",  "seed": 1}
{"kind": "seed.started",  "seed": 2}
{"kind": "seed.finished", "seed": 3, "ok": true, "slack": 0.082, "fmax": 74.60}
{"kind": "job.finished",  "status": "passed", "summary": {"best_seed": 3, ...}}
```

Final result:

```json
{
  "best": {
    "seed": 3,
    "passed": true,
    "slack_ns": 0.082,
    "fmax_mhz": 74.60,
    "rbf_r_path": "/Users/dev/cores/super-tile-matcher/.superq/artifacts/a1b2c3/seed-0003/bitstream.rbf_r"
  }
}
```

## 4. Ship the bitstream

```bash
cp /Users/dev/cores/super-tile-matcher/.superq/artifacts/latest/bitstream.rbf_r \
   /Users/dev/cores/super-tile-matcher/dist/Cores/dev.super-tile-matcher/bitstream.rbf_r
```

(super-q does this automatically when a canonical `dist/` layout exists.)

## 5. Look at timing after the fact

```
# tool: inspect_timing
# input: {"path": "/Users/dev/cores/super-tile-matcher"}
{
  "timing": {
    "passed": true,
    "worst_setup_slack_ns": 0.082,
    "clocks": [
      {"name": "pocket_cart_clk_74_25",  "fmax_mhz": 74.60, "setup_slack_ns": 0.082},
      {"name": "pocket_bridge_spi_sclk", "fmax_mhz": 123.4, "setup_slack_ns": 1.740}
    ]
  }
}
```

## 6. Batch across a folder of cores

```
# tool: batch_sweep
# input: {"paths": ["/Users/dev/cores"], "min_seed": 1, "max_seed": 8, "parallel": 4, "parallel_cores": 2}
```

Returns `{"cores": [...outcome per core...]}` — iterate and flag any
where `best === null`.
