# super-q in CI

super-q is CI-native: every command emits structured output, detects
the runner automatically, and posts annotations/outputs that PR
reviewers and downstream jobs can consume.

## GitHub Actions

### Simplest — as a composite action

```yaml
# .github/workflows/ci.yml
on: [push, pull_request]
jobs:
  build:
    runs-on: ubuntu-22.04
    steps:
      - uses: actions/checkout@v4
      - uses: super-q/action@v1          # (or ./.github/actions/super-q if vendored)
        id: build
        with:
          core-path: .
          seeds: "1-8"
          parallel: "4"
          mode: split-fit
          target-slack-ns: "0.0"
      - run: echo "seed=${{ steps.build.outputs.best-seed }}"
```

The action installs Quartus (cached), runs `superq ci build`, emits:

  * **annotations** attached to your `.qpf` on failure
  * **step outputs**: `passed`, `best-seed`, `best-slack-ns`, `rbf-path`
  * **job summary markdown** with the full seed table
  * **artifact** `superq-<run-id>-<seed>/bitstream.rbf_r`

### Using remote compute in CI

If builds take too long on GHA runners, dispatch to your remote pool
from inside the action (GHA runner acts as coordinator only):

```yaml
- run: |
    pip install 'super-q[modal]'
    superq ci build . --pool=modal --parallel=16
  env:
    MODAL_TOKEN_ID: ${{ secrets.MODAL_TOKEN_ID }}
    MODAL_TOKEN_SECRET: ${{ secrets.MODAL_TOKEN_SECRET }}
```

### Matrix over many cores

```yaml
strategy:
  matrix:
    core:
      - cores/author.core_a
      - cores/author.core_b
steps:
  - uses: actions/checkout@v4
  - uses: super-q/action@v1
    with: { core-path: ${{ matrix.core }} }
```

## GitLab CI

Copy `examples/ci/.gitlab-ci.yml` into your repo root. It runs
`superq ci build .` for MRs and `superq ci explore .` for main.

## CircleCI / Buildkite

super-q auto-detects both. Use `superq ci build <path>` — annotations
fall back to log lines, outputs still print.

## What "CI-tuned" means

`superq ci build` differs from `superq sweep`:

| behavior                        | `sweep`              | `ci build`              |
|---------------------------------|----------------------|-------------------------|
| progress output                 | rich table           | suppressed              |
| JSON final result               | only with `--json`   | always on stdout        |
| annotations                     | none                 | GHA/GitLab native       |
| runner outputs (GITHUB_OUTPUT)  | none                 | `passed`, `best_seed`…  |
| job summary                     | none                 | markdown table posted   |
| defaults                        | `--max=16`           | `--max=8` (faster PRs)  |
| exit code 0                     | passed               | passed                  |
| exit code 3                     | timing failed        | timing failed           |

## Artifact upload conventions

super-q writes everything agents might want under
`.superq/artifacts/<job>/seed-<NNNN>/`. The GH Action uploads only the
passing seed; the GitLab example uploads everything in the directory.

If you want to publish beyond CI (e.g. push the `.rbf_r` to a CDN),
chain another step:

```yaml
- run: aws s3 cp "${{ steps.build.outputs.rbf-path }}" s3://my-cdn/cores/
```
