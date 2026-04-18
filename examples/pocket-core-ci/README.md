# Drop-in CI for an Analogue Pocket core repo

Copy the two files under `.github/workflows/` here into the root of
your Pocket core repo. That's it — no other setup:

```
your-core/
├── .github/
│   └── workflows/
│       ├── build.yml     # copy from ./.github/workflows/build.yml
│       └── release.yml   # copy from ./.github/workflows/release.yml
├── dist/
│   ├── Cores/…
│   └── Platforms/…
├── src/fpga/             # Quartus project lives here
│   └── pocket.qpf
└── README.md
```

What you get:

- **Every push/PR** → seed sweep (1–8, 4 parallel). Fails the check if
  no seed met timing. `.rbf_r` uploaded as a workflow artifact.
- **Every tag `vX.Y.Z`** → full 32-seed sweep + canonical Pocket zip
  (`Author.Core_X.Y.Z.zip`) attached to the corresponding GitHub Release.
  Drop that zip onto a Pocket SD card and the core appears next boot.

## First run

The initial workflow is slow (~20 min) because it downloads and caches
Quartus Lite 24.1 (~8 GB). Subsequent runs hit the cache and take 8–12
minutes for a typical sweep.

## Private Quartus mirror (optional but recommended)

Intel's CDN sometimes rate-limits shared CI runners. Set a repository
secret `QUARTUS_URL` pointing at your own mirror:

```
Settings → Secrets and variables → Actions → New repository secret
Name:  QUARTUS_URL
Value: https://my-mirror.example.com/Quartus-lite-24.1std.0.917-linux.tar
```

Un-comment the `secrets:` block in both workflow files and the installer
will use your mirror automatically.

## Adjusting parameters

The common knobs both workflows take:

| input         | default      | meaning                                       |
|---------------|--------------|-----------------------------------------------|
| `core-path`   | `.`          | Repo-relative path to the core root           |
| `seeds`       | `1-8`/`1-32` | Seed range or single seed                      |
| `parallel`    | `4`          | Concurrent seeds on the runner                 |
| `mode`        | `split-fit`  | `full` or `split-fit` (share synth)            |
| `target-slack-ns` | `0`      | Minimum slack to count as "passed"             |

See [docs/github-actions.md](../../docs/github-actions.md) for the full
reference.
