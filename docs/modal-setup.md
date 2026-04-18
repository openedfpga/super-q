# Modal setup — step by step

End-to-end setup takes ~15 minutes of your time plus ~20 minutes of
unattended Quartus install. After that, every build request is a
one-line call.

Prereqs: a Modal account (https://modal.com, free tier is fine) and an
accepted copy of Intel's Quartus Lite EULA.

## 1. Install the SDK

```bash
pip install 'super-q[modal]'
superq modal check          # tells you exactly what's missing
```

Expected early state:

```
Modal backend readiness
  ✓ SDK installed
  ✗ API token
  ✓ super_q.modal_app importable
  ✗ App `super-q` deployed

next steps
  $ modal token new
```

## 2. Get a Modal token

```bash
modal token new             # opens a browser; pastes creds into ~/.modal.toml
```

Re-run `superq modal check` — token should flip to ✓.

## 3. Stash the Intel EULA as a Modal Secret

Read the EULA at
https://www.intel.com/content/www/us/en/legal/end-user-license-agreement.html
and then:

```bash
modal secret create altera-eula \
  EULA='I have read and accept the Intel Simplified Software License Agreement.'
```

## 4. Deploy the super-q App

```bash
superq modal deploy         # thin wrapper around `modal deploy super_q.modal_app`
```

First deploy takes 3–5 minutes to build the image (pulling Ubuntu,
apt-get, pip install). Subsequent deploys reuse the layer cache and
take seconds.

## 5. Smoke-test without Quartus

```bash
superq modal smoke
```

This calls a tiny no-op function on Modal to verify:
  * the image built
  * super-q imports inside the container
  * the TCL wrappers are in place

Expected output:

```
✓ smoke test passed (8.3s round-trip)

  host     : modal-prod-sfo-12
  cpu      : AMD EPYC 7B13
  vcpus    : 2
  python   : 3.11.11
  super-q  : 0.1.0
  quartus  : not installed in Volume yet
  TCL      : 5 wrappers baked in

next: modal run super_q.modal_app::install_quartus --tarball=…
```

Stop here if the smoke test fails — no point installing Quartus if the
image or RPC is broken.

## 6. Install Quartus into the persistent Volume

Download
[Quartus-lite-24.1std.0.917-linux.tar](https://www.altera.com/downloads/fpga-development-tools/quartus-prime-lite-edition-design-software-version-24-1-linux)
(~8 GB) from Intel. Then:

```bash
superq modal install-quartus --tarball=./Quartus-lite-24.1std.0.917-linux.tar
```

This uploads the tar to Modal and unpacks it into the `superq-quartus`
Volume. ~15–30 minutes the first time; you only do it once per Quartus
release.

## 7. End-to-end bench: build one real seed

```bash
superq modal bench ./my-core --seed=1
```

You should see a normal `SweepOutcome` with timing + an `.rbf_r` at
`./my-core/.superq/artifacts/…/bitstream.rbf_r`.

## 8. Use it as your default pool

```bash
cat >> ~/.superq/config.toml <<'EOF'
[pool.modal]
kind = "modal"
app = "super-q"
max_parallel = 32
cpu = 8
memory_gb = 16

[default]
pool = "modal"
EOF

superq sweep ./my-core --parallel=16         # now runs on Modal
superq explore ./my-core --budget=30m        # so does this
```

## Troubleshooting

**`superq modal check` shows `app_deployed: false` after deploy**

Modal caches function lookups; occasionally you need to wait 10–20 s
for the deploy to propagate. Try again.

**`install_quartus` fails with "EULA secret missing"**

Double-check the secret name is `altera-eula` (lowercase, dash):

```bash
modal secret list | grep altera
```

**Smoke test hangs on first run**

First call builds the image, which downloads ~500 MB of apt + pip. Use
`--verbose` on the CLI to see Modal's image-build log.

**Quartus install times out**

The install function has a 1-hour timeout. If your upload is slow,
bump it: edit `super_q/modal_app.py`, change `timeout=60 * 60` on
`install_quartus` to `60 * 60 * 2`, and `superq modal deploy` again.

**Cost went higher than expected**

Each `run_seed` invocation is a separate 8-vCPU container. Check:

```bash
modal app stats super-q
```

Cap concurrency by lowering `max_parallel` in your pool config.
