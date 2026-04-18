"""`superq init` — scaffold a new Analogue Pocket openFPGA core repo.

Goal: a fresh `author.name` directory that's immediately committable to
GitHub and will start building on every push. Opinionated on structure
(canonical openFPGA layout), unopinionated on RTL (user brings that).

What we generate:

    <author>.<name>/
        .github/workflows/build.yml      # per-PR seed sweep
        .github/workflows/release.yml    # tag → Pocket zip
        .gitignore
        .dockerignore
        README.md                         # TODO-style starter
        dist/
            Cores/<author>.<name>/
                core.json
                audio.json video.json input.json
                interact.json data.json variants.json
            Platforms/
                <platform_id>.json
                _images/.gitkeep
        src/fpga/
            README.md                     # placeholder until RTL arrives

Intentionally not scaffolded:
    * Verilog/SystemVerilog top-level — too design-specific; point at
      analogue/openFPGA_Pocket_Framework or an existing core to fork.
    * core icons/platform images — those are art assets the user supplies.

With `--ci-only`, we only write the `.github/` workflows, leaving the
rest of a pre-existing repo untouched. That's the path for "I already
have an openFPGA core, just add super-q CI."
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path


class InitError(Exception):
    pass


@dataclass
class InitOptions:
    target: Path
    author: str
    name: str
    description: str = ""
    version: str = "0.1.0"
    super_q_ref: str = "main"
    super_q_repo: str = "openedfpga/super-q"   # <owner>/<repo> for the reusable workflow `uses:`
    ci_only: bool = False
    force: bool = False                    # overwrite existing files
    git_init: bool = True                   # run `git init` after scaffold
    platform_ids: list[str] = field(default_factory=list)
    default_seeds_build: str = "1-8"
    default_seeds_release: str = "1-32"
    inline: bool = False                    # embed workflow steps inline
                                            #   (no external `uses:` reference)
    super_q_pip: str = ""                   # pip install target for inline mode
                                            #   (empty → git+https://… at super_q_ref)

    @property
    def full_name(self) -> str:
        return f"{self.author}.{self.name}"

    @property
    def shortname(self) -> str:
        """Identifier form for core.json (lowercase, underscores only)."""
        return re.sub(r"[^a-z0-9]+", "_", self.name.lower()).strip("_") or "core"

    @property
    def primary_platform(self) -> str:
        return self.platform_ids[0] if self.platform_ids else self.shortname


@dataclass
class InitResult:
    target: Path
    created: list[Path]
    skipped: list[Path]

    def as_dict(self) -> dict:
        return {
            "target": str(self.target),
            "created": [str(p) for p in self.created],
            "skipped": [str(p) for p in self.skipped],
        }


# ---------------------------------------------------------------------------
# scaffolder
# ---------------------------------------------------------------------------

_AUTHOR_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def validate_identifier(name: str, kind: str) -> None:
    if not _AUTHOR_NAME_RE.match(name):
        raise InitError(
            f"invalid {kind}: {name!r} — use letters, digits, hyphen, underscore only"
        )


def scaffold(opts: InitOptions) -> InitResult:
    validate_identifier(opts.author, "author")
    validate_identifier(opts.name, "name")
    if not opts.platform_ids:
        opts.platform_ids = [opts.primary_platform]

    opts.target.mkdir(parents=True, exist_ok=True)

    files = _ci_files(opts) if opts.ci_only else _full_files(opts)
    created: list[Path] = []
    skipped: list[Path] = []

    for rel, content in files.items():
        dst = opts.target / rel
        if dst.exists() and not opts.force:
            skipped.append(dst)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(content)
        created.append(dst)

    if opts.git_init and not (opts.target / ".git").exists():
        try:
            import subprocess
            subprocess.run(["git", "init", "-q"], cwd=opts.target, check=False)
        except FileNotFoundError:
            pass

    return InitResult(target=opts.target, created=created, skipped=skipped)


# ---------------------------------------------------------------------------
# file-set builders
# ---------------------------------------------------------------------------


def _full_files(opts: InitOptions) -> dict[str, str]:
    files: dict[str, str] = {}
    files.update(_ci_files(opts))
    files[".gitignore"]     = _TEMPLATE_GITIGNORE
    files[".dockerignore"]  = _TEMPLATE_DOCKERIGNORE
    files["README.md"]      = _TEMPLATE_README.format(**_vars(opts))

    core_dir = f"dist/Cores/{opts.full_name}"
    files[f"{core_dir}/core.json"]     = _TEMPLATE_CORE_JSON.format(**_vars(opts))
    files[f"{core_dir}/audio.json"]    = _TEMPLATE_AUDIO_JSON
    files[f"{core_dir}/video.json"]    = _TEMPLATE_VIDEO_JSON
    files[f"{core_dir}/input.json"]    = _TEMPLATE_INPUT_JSON
    files[f"{core_dir}/interact.json"] = _TEMPLATE_INTERACT_JSON
    files[f"{core_dir}/data.json"]     = _TEMPLATE_DATA_JSON
    files[f"{core_dir}/variants.json"] = _TEMPLATE_VARIANTS_JSON

    for pid in opts.platform_ids:
        files[f"dist/Platforms/{pid}.json"] = _TEMPLATE_PLATFORM_JSON.format(
            platform_id=pid, core_name=opts.name,
        )
    files["dist/Platforms/_images/.gitkeep"] = ""

    files["src/fpga/README.md"] = _TEMPLATE_FPGA_README.format(**_vars(opts))

    return files


def _ci_files(opts: InitOptions) -> dict[str, str]:
    v = _vars(opts)
    if opts.inline:
        return {
            ".github/workflows/build.yml":   _TEMPLATE_WORKFLOW_BUILD_INLINE.format(**v),
            ".github/workflows/release.yml": _TEMPLATE_WORKFLOW_RELEASE_INLINE.format(**v),
        }
    return {
        ".github/workflows/build.yml":   _TEMPLATE_WORKFLOW_BUILD.format(**v),
        ".github/workflows/release.yml": _TEMPLATE_WORKFLOW_RELEASE.format(**v),
    }


def _vars(opts: InitOptions) -> dict[str, str]:
    # pip target for inline mode — users can override to point at a forked
    # super-q, a private GHCR wheel, or a pinned PyPI release.
    pip_target = opts.super_q_pip or (
        f"super-q @ git+https://github.com/{opts.super_q_repo}@{opts.super_q_ref}"
    )
    return {
        "author":          opts.author,
        "name":            opts.name,
        "full_name":       opts.full_name,
        "shortname":       opts.shortname,
        "description":     opts.description or f"{opts.name} for Analogue Pocket",
        "version":         opts.version,
        "date":            datetime.now(UTC).strftime("%Y-%m-%d"),
        "super_q_ref":     opts.super_q_ref,
        "super_q_repo":    opts.super_q_repo,
        "super_q_pip":     pip_target,
        "primary_platform": opts.primary_platform,
        "platform_ids_json": ", ".join(f"\"{p}\"" for p in opts.platform_ids),
        "seeds_build":     opts.default_seeds_build,
        "seeds_release":   opts.default_seeds_release,
    }


# ---------------------------------------------------------------------------
# templates — kept inline so a pip-installed super-q needs no extra data files
# ---------------------------------------------------------------------------

_TEMPLATE_WORKFLOW_BUILD = """# Generated by `superq init` — re-run with `--force` to refresh.
name: build
on:
  push:
    branches: [main]
  pull_request:
  workflow_dispatch:

jobs:
  build:
    uses: {super_q_repo}/.github/workflows/reusable-build.yml@{super_q_ref}
    with:
      core-path: .
      seeds: "{seeds_build}"
      parallel: 4
      mode: split-fit
    # secrets:
    #   QUARTUS_URL: ${{{{ secrets.QUARTUS_URL }}}}     # private Quartus mirror
"""

_TEMPLATE_WORKFLOW_RELEASE = """# Generated by `superq init` — re-run with `--force` to refresh.
name: release
on:
  push:
    tags: ['v*']
  workflow_dispatch:
    inputs:
      version:
        description: Explicit version (otherwise inferred from the tag)
        required: false

jobs:
  release:
    uses: {super_q_repo}/.github/workflows/reusable-release.yml@{super_q_ref}
    with:
      core-path: .
      seeds: "{seeds_release}"
      parallel: 4
      mode: split-fit
      version: ${{{{ inputs.version || '' }}}}
      # draft: true       # un-comment to gate publish behind manual review
    permissions:
      contents: write
"""

_TEMPLATE_GITIGNORE = """# super-q
.superq/
*.rbf
*.rbf_r
*.sof
*.pof
*.jdi

# Quartus
output_files/
db/
incremental_db/
qdb/
greybox_tmp/
simulation/
tmp-clearbox/
*.qws
*.qws.bak
*.bak

# Release artifacts
release/
*.zip

# OS / editor
.DS_Store
Thumbs.db
.vscode/
.idea/
"""

_TEMPLATE_DOCKERIGNORE = """.git
.github
.superq
output_files/
db/
incremental_db/
qdb/
__pycache__/
*.pyc
.DS_Store
release/
"""

_TEMPLATE_README = """# {full_name}

{description}

This is an Analogue Pocket openFPGA core. CI builds via
[super-q](https://github.com/openedfpga/super-q) — every push runs a
seed sweep, every `v*` tag produces a release.

## Layout

```
{full_name}/
├── .github/workflows/        # super-q GHA wiring
├── dist/                     # Pocket-facing assets (shipped in the release zip)
│   ├── Cores/{full_name}/    #   core.json, audio.json, video.json, …
│   └── Platforms/            #   platform metadata + _images/
├── src/fpga/                 # Quartus project goes here (see README inside)
└── README.md                 # (this file)
```

## Next steps

1. Add your Quartus project under `src/fpga/`. The super-q CI expects a
   `.qpf` there — see [`src/fpga/README.md`](src/fpga/README.md).
2. Edit `dist/Cores/{full_name}/core.json` and the related JSONs with
   your real metadata and video/audio/input/interact specs.
3. Add platform imagery at `dist/Platforms/_images/<platform>.bin`.
4. Commit and push to GitHub — the `build` workflow runs automatically.
5. When ready to release:
   ```bash
   git tag v{version}
   git push --tags
   ```
   The `release` workflow packages `dist/` + the winning bitstream into
   `{full_name}_{version}.zip` and attaches it to a GitHub Release.

## Local iteration

With [super-q](https://github.com/openedfpga/super-q) installed:

```bash
superq verify .                 # sanity-check the layout
superq watch-build .            # rebuild on every source save
superq sweep . --min=1 --max=16 # parallel seed sweep
superq explore . --budget=30m   # adaptive seed exploration
```

## References

- [Analogue openFPGA Developer Docs](https://www.analogue.co/developer/docs/overview)
- [super-q agent guide](https://github.com/openedfpga/super-q/blob/main/AGENTS.md)
"""

_TEMPLATE_FPGA_README = """# Quartus project

This directory is where your Pocket core's Quartus Lite 24.1 project
lives. super-q auto-detects the `.qpf` here.

## Getting started

Fork one of these starter repos and copy `src/fpga/` across, then
replace the top-level logic with yours:

- [openfpga-template](https://github.com/openfpga/openfpga-template)
- [Analogue openFPGA framework](https://github.com/Analogue/openFPGA_Pocket_Framework)

Target device: **5CEBA4F23C8** (Cyclone V, the Pocket FPGA).

## Expected files after setup

```
src/fpga/
├── {shortname}.qpf            # Quartus project file
├── {shortname}.qsf            # Quartus settings (device, top-level, IP)
├── {shortname}.sdc            # Timing constraints (Pocket clocks)
└── rtl/                       # your SystemVerilog/Verilog
```

Once these exist, `superq verify .` from the repo root will confirm
detection works.
"""

_TEMPLATE_CORE_JSON = """{{
  "core": {{
    "magic": "APF_VER_1",
    "metadata": {{
      "platform_ids": [{platform_ids_json}],
      "shortname": "{shortname}",
      "description": "{description}",
      "author": "{author}",
      "url": "",
      "version": "{version}",
      "date_release": "{date}"
    }},
    "framework": {{
      "target_product": "Analogue Pocket",
      "version_required": "1.1",
      "sleep_supported": false,
      "dock": {{ "supported": true, "analog_output": false }},
      "hardware": {{ "link_port": false, "cartridge_adapter": -1 }}
    }},
    "cores": [
      {{ "name": "default", "id": 0, "filename": "bitstream.rbf_r" }}
    ]
  }}
}}
"""

_TEMPLATE_AUDIO_JSON = """{
  "audio": {
    "magic": "APF_VER_1"
  }
}
"""

_TEMPLATE_VIDEO_JSON = """{
  "video": {
    "magic": "APF_VER_1",
    "scaler_modes": [
      {
        "width": 320,
        "height": 240,
        "aspect_w": 4,
        "aspect_h": 3,
        "rotation": 0,
        "mirror": 0
      }
    ]
  }
}
"""

_TEMPLATE_INPUT_JSON = """{
  "input": {
    "magic": "APF_VER_1",
    "controllers": [
      {
        "type": "default",
        "mappings": [
          { "id": 1, "name": "A", "key": "face_a" },
          { "id": 2, "name": "B", "key": "face_b" },
          { "id": 4, "name": "X", "key": "face_x" },
          { "id": 8, "name": "Y", "key": "face_y" }
        ]
      }
    ]
  }
}
"""

_TEMPLATE_INTERACT_JSON = """{
  "interact": {
    "magic": "APF_VER_1",
    "variables": [],
    "messages": []
  }
}
"""

_TEMPLATE_DATA_JSON = """{
  "data": {
    "magic": "APF_VER_1",
    "data_slots": []
  }
}
"""

_TEMPLATE_VARIANTS_JSON = """{
  "variants": {
    "magic": "APF_VER_1",
    "variant_list": []
  }
}
"""

_TEMPLATE_PLATFORM_JSON = """{{
  "platform": {{
    "category": "Other",
    "name": "{core_name}",
    "year": 2026,
    "manufacturer": ""
  }}
}}
"""


# ---------------------------------------------------------------------------
# inline workflows — zero external dependencies. Good for private cores
# where you can't (or don't want to) reference a reusable workflow from a
# separate repo. Tradeoff: you'll have to copy super-q updates into your
# repo manually rather than floating on `@main`.
# ---------------------------------------------------------------------------

_TEMPLATE_WORKFLOW_BUILD_INLINE = """# Generated by `superq init --inline` — re-run with `--force` to refresh.
# Self-contained: does NOT reference super-q as a reusable workflow, so
# it works in private repos without any cross-repo access grants.
name: build
on:
  push:
    branches: [main]
  pull_request:
  workflow_dispatch:

jobs:
  build:
    runs-on: ubuntu-22.04
    timeout-minutes: 120
    steps:
      - uses: actions/checkout@v4

      - name: Install apt runtime deps
        run: |
          sudo dpkg --add-architecture i386
          sudo apt-get update
          sudo apt-get install -y --no-install-recommends \\
              libc6-i386 libncurses5 libncurses6 libtinfo5 libxft2 \\
              libxrender1 libxtst6 libxi6 libfreetype6 libpng16-16 \\
              libjpeg62-turbo rsync zip

      - uses: actions/setup-python@v5
        with: {{ python-version: "3.11" }}

      - name: Install super-q
        run: pip install "{super_q_pip}"

      - name: Restore Quartus cache
        id: cache
        uses: actions/cache@v4
        with:
          path: /opt/intelFPGA_lite/24.1
          key: quartus-24.1-v2
          restore-keys: |
            quartus-24.1-

      - name: Install Quartus (on cache miss)
        if: steps.cache.outputs.cache-hit != 'true'
        env:
          QUARTUS_URL: ${{{{ secrets.QUARTUS_URL }}}}
        run: |
          sudo mkdir -p /opt/intelFPGA_lite
          sudo chown "$USER" /opt/intelFPGA_lite
          superq install-quartus --accept-eula --version=24.1

      - name: Export Quartus env
        run: |
          echo "QUARTUS_ROOTDIR=/opt/intelFPGA_lite/24.1/quartus" >> "$GITHUB_ENV"
          echo "/opt/intelFPGA_lite/24.1/quartus/bin" >> "$GITHUB_PATH"

      - name: Seed sweep
        run: |
          superq ci build . \\
            --min="$(echo '{seeds_build}' | cut -d- -f1)" \\
            --max="$(echo '{seeds_build}' | cut -d- -f2)" \\
            --parallel=4 --mode=split-fit

      - name: Upload bitstream
        if: success()
        uses: actions/upload-artifact@v4
        with:
          name: superq-${{{{ github.run_id }}}}
          path: |
            .superq/artifacts/**/seed-*/bitstream.rbf_r
            .superq/artifacts/**/seed-*/timing.json
          if-no-files-found: error
          retention-days: 30
"""

_TEMPLATE_WORKFLOW_RELEASE_INLINE = """# Generated by `superq init --inline` — re-run with `--force` to refresh.
# Self-contained release pipeline; no cross-repo references.
name: release
on:
  push:
    tags: ['v*']
  workflow_dispatch:
    inputs:
      version:
        description: Explicit version (otherwise inferred from the tag)
        required: false

jobs:
  release:
    runs-on: ubuntu-22.04
    timeout-minutes: 180
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v4

      - name: Install apt runtime deps
        run: |
          sudo dpkg --add-architecture i386
          sudo apt-get update
          sudo apt-get install -y --no-install-recommends \\
              libc6-i386 libncurses5 libncurses6 libtinfo5 libxft2 \\
              libxrender1 libxtst6 libxi6 libfreetype6 libpng16-16 \\
              libjpeg62-turbo rsync zip

      - uses: actions/setup-python@v5
        with: {{ python-version: "3.11" }}

      - name: Install super-q
        run: pip install "{super_q_pip}"

      - name: Restore Quartus cache
        id: cache
        uses: actions/cache@v4
        with:
          path: /opt/intelFPGA_lite/24.1
          key: quartus-24.1-v2
          restore-keys: |
            quartus-24.1-

      - name: Install Quartus (on cache miss)
        if: steps.cache.outputs.cache-hit != 'true'
        env:
          QUARTUS_URL: ${{{{ secrets.QUARTUS_URL }}}}
        run: |
          sudo mkdir -p /opt/intelFPGA_lite
          sudo chown "$USER" /opt/intelFPGA_lite
          superq install-quartus --accept-eula --version=24.1

      - name: Export Quartus env
        run: |
          echo "QUARTUS_ROOTDIR=/opt/intelFPGA_lite/24.1/quartus" >> "$GITHUB_ENV"
          echo "/opt/intelFPGA_lite/24.1/quartus/bin" >> "$GITHUB_PATH"

      - name: Resolve version
        id: ver
        run: |
          V="${{{{ inputs.version }}}}"
          if [ -z "$V" ]; then V="${{{{ github.ref_name }}}}"; V="${{V#v}}"; fi
          echo "version=$V" >> "$GITHUB_OUTPUT"

      - name: Seed sweep
        run: |
          superq ci build . \\
            --min="$(echo '{seeds_release}' | cut -d- -f1)" \\
            --max="$(echo '{seeds_release}' | cut -d- -f2)" \\
            --parallel=4 --mode=split-fit

      - name: Locate bitstream
        id: bits
        run: |
          RBF=$(find .superq/artifacts -name bitstream.rbf_r | head -1)
          echo "rbf_r=$RBF" >> "$GITHUB_OUTPUT"

      - name: Package Pocket zip
        id: pack
        run: |
          superq release pack \\
            --core-path=. \\
            --rbf-r="${{{{ steps.bits.outputs.rbf_r }}}}" \\
            --version="${{{{ steps.ver.outputs.version }}}}" \\
            --out-dir=release --json | tee pack.json
          ZIP=$(python -c 'import json;print(json.load(open("pack.json"))["zip_path"])')
          echo "zip=$ZIP" >> "$GITHUB_OUTPUT"

      - name: Create Release
        uses: softprops/action-gh-release@v2
        with:
          generate_release_notes: true
          files: |
            ${{{{ steps.pack.outputs.zip }}}}
            ${{{{ steps.bits.outputs.rbf_r }}}}
          body: |
            Built with [super-q](https://github.com/openedfpga/super-q).
            version: `${{{{ steps.ver.outputs.version }}}}`

            Unpack the zip at the root of your Pocket's SD card.
"""
