# Pocket cores on GitHub Actions

Zero-infra CI and tagged-release automation for Analogue Pocket cores.
Copy two workflow files into your core repo and every push, PR, and tag
gets a fresh Quartus build without you running anything locally.

## What's in the box

super-q ships three reusable workflows and one optional image:

| path                                               | what it does                                         |
|----------------------------------------------------|------------------------------------------------------|
| `.github/workflows/reusable-build.yml`             | build + seed sweep, upload `.rbf_r` as artifact       |
| `.github/workflows/reusable-release.yml`           | same + Pocket dist zip + GitHub Release               |
| `.github/workflows/docker-publish.yml`             | builds the super-q Docker image and pushes to GHCR    |
| `docker/Dockerfile`                                | lightweight (~250 MB) image — runtime deps + super-q  |

## Quick start (in your Pocket core repo)

1. Copy [build.yml](../examples/pocket-core-ci/.github/workflows/build.yml)
   and [release.yml](../examples/pocket-core-ci/.github/workflows/release.yml)
   into `.github/workflows/` in your core's repo.
2. Commit and push. First run on `main` takes ~20 min (downloads
   Quartus). Subsequent builds use the cache.
3. When you're ready to publish a release:
   ```bash
   git tag v0.3.0
   git push --tags
   ```
   A GitHub Release appears shortly after, with the canonical
   `<Author>.<Core>_0.3.0.zip` attached.

## How the Quartus cache works

The Quartus Lite installer is ~8 GB compressed and ~15 GB installed.
To fit into GHA's free 10 GB cache, `docker/install-quartus.sh` trims
device support down to Cyclone V only (what the Pocket uses), dropping
the total to ~5 GB. Cache key is `quartus-<version>-v2`, so bumping
the version or the script's `-v2` suffix forces a re-download.

If Intel rate-limits your runner (rare but happens), set a repo secret
`QUARTUS_URL` pointing at a mirror. The installer honors it.

## How the release workflow works

```
tag push → reusable-release
              ├── reusable-build (seed sweep)
              │     └── upload .rbf_r as artifact
              └── publish job
                    ├── download artifact
                    ├── superq release pack --rbf-r=… --version=<tag>
                    │     └── stamps core.json, zips dist/
                    └── softprops/action-gh-release → attach zip + rbf_r
```

The pack step:
  * writes the winning `.rbf_r` into `dist/Cores/<Author>.<Core>/`
  * stamps your tag as `core.metadata.version` in `core.json`
  * updates `core.metadata.date_release` to today
  * zips the whole `dist/` tree into `<Author>.<Core>_<ver>.zip`

The zip is drop-in: unpack it at the root of a Pocket SD card and the
core shows up under the Author's menu on next boot.

## Using the pre-built Docker image

If you'd rather not let GHA install Quartus on every new cache miss,
build the image once from this repo (via `docker-publish.yml`) and
reference it in your own workflows:

```yaml
jobs:
  build:
    runs-on: ubuntu-22.04
    container:
      image: ghcr.io/<you>/super-q:24.1
    steps:
      - uses: actions/checkout@v4
      - uses: actions/cache@v4
        with: { path: /opt/intelFPGA_lite/24.1, key: quartus-24.1-v2 }
      - run: superq-install-quartus 24.1       # cache-hit path is a no-op
        env: { SUPERQ_ACCEPT_EULA: "1" }
      - run: superq ci build . --min=1 --max=8 --parallel=4
```

The image itself does **not** contain Quartus — Intel's redistribution
rules forbid that in a public image. The cache supplies it.

## Per-PR behavior

The default `build.yml` uses `seeds: 1-8, parallel: 4`. That's tuned
for PR feedback: ~8–12 min wall clock, catches timing regressions
without burning through a 45-minute matrix every PR.

If a PR fails timing, the annotations point at `src/fpga/pocket.qpf`
so reviewers see it inline. The run's Summary page has the full seed
table.

## Per-tag behavior

`release.yml` defaults to `seeds: 1-32` so you have a better chance of
finding a clean bitstream for the shipped release. Add
`target-slack-ns: 0.1` if you want extra margin.

Use `draft: true` if you want to review the release before publishing:

```yaml
with:
  draft: true
```

A draft release is created with the zip attached; open it in the
Releases UI and click Publish to flip it live.

## Pinning super-q

By default workflows pull super-q from `main`. To pin:

```yaml
jobs:
  build:
    uses: super-q/super-q/.github/workflows/reusable-build.yml@v0.1.0
    with:
      super-q-ref: v0.1.0          # also install this ref into the runner
```

## Troubleshooting

**Cache keeps missing even when key matches.** GHA caches are
branch-scoped: the first build on a feature branch doesn't see the
cache from `main`. The workflows include `restore-keys` so old caches
partially restore. For immediate rebuilds, push the workflow change to
`main` first.

**Release didn't attach the zip.** Check the `publish` job's
Permissions — the job needs `contents: write`. The example
`release.yml` sets this already.

**Seed sweep finds nothing.** Bump `seeds: 1-32` and switch to
`mode: full` (not `split-fit`) to try harder. For stubborn cores, use
`superq explore` locally with `--budget=1h` and feed the winning seed
into a build run.

**Docker image too big.** `docker/Dockerfile` produces a ~250 MB image
— but only because Quartus isn't in it. If yours is much larger, check
you didn't `COPY` a `db/` or `incremental_db/` into the context; add
them to `.dockerignore`.

## Going further

- [docs/remote.md](remote.md) — swap the runner out for Modal/Fly/SSH
- [docs/iteration.md](iteration.md) — local watch-build, daemon, incremental
- [docs/ci.md](ci.md) — how super-q emits CI-native annotations
