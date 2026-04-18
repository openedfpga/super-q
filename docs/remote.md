# Remote workers

super-q can build cores anywhere — your laptop, a home-lab Linux box,
Modal, Fly.io, a spare GitHub Actions runner. The recipes below are
ordered by "how fast can I go from zero to remote compiles."

## 1. Modal (recommended)

Why: Python-native. Scale from 0 → 64 workers instantly. Per-second
billing. No infrastructure to manage.

```bash
pip install 'super-q[modal]'
modal token new                           # one-time
modal secret create altera-eula           # paste the accepted EULA text

# Bootstrap the Quartus install into a persistent Modal volume.
# Download Quartus-lite-24.1std.0.917-linux.tar yourself first.
modal run super_q.modal_app::install_quartus --tarball=./Quartus-lite-24.1std.0.917-linux.tar

# Deploy the build function.
modal deploy super_q.modal_app

# Point super-q at it.
cat >> ~/.superq/config.toml <<'EOF'
[pool.modal]
kind = "modal"
app  = "super-q"
max_parallel = 32

[default]
pool = "modal"
EOF

superq sweep ./my-core --parallel=16     # now runs on Modal
```

Typical cost: ~$0.10 for a 16-seed sweep at 8 parallel 8-vCPU workers.

## 2. Fly.io Machines

Why: true VM control, global regions, SSH for debugging. Good when you
want physical-ish isolation or to run in a specific region.

```bash
pip install 'super-q[fly]'
flyctl auth login
flyctl apps create super-q-build

# Build and push a Quartus image:
#   docker build -f docker/Dockerfile --secret id=altera_eula,src=./EULA.txt -t registry.fly.io/super-q-build:24.1 .
#   flyctl auth docker && docker push registry.fly.io/super-q-build:24.1

# Pick an S3-compatible bucket (Tigris/R2 both work):
#   flyctl storage create -a super-q-build      (Tigris is built-in)

export FLY_API_TOKEN=$(flyctl auth token)
export AWS_ACCESS_KEY_ID=…  AWS_SECRET_ACCESS_KEY=…

cat >> ~/.superq/config.toml <<'EOF'
[pool.fly]
kind = "fly"
app  = "super-q-build"
image = "registry.fly.io/super-q-build:24.1"
region = "iad"
size = "performance-8x"
max_parallel = 8
artifact_bucket = "your-tigris-bucket"
artifact_endpoint = "https://fly.storage.tigris.dev"
EOF

superq sweep ./my-core --pool=fly --parallel=8
```

## 3. SSH pool — bring your own Linux boxes

Why: simplest mental model, zero SaaS. A $30/mo Hetzner AX-class box
runs Quartus very happily, or repurpose idle machines around the house.

```bash
cat >> ~/.superq/config.toml <<'EOF'
[pool.homelab]
kind = "ssh"
user = "superq"
slots_per_host = 4
hosts = ["build1.home.arpa", "build2.home.arpa"]
quartus_root = "/opt/intelFPGA_lite/24.1/quartus"
EOF

# One-time per host:
#   - install Quartus Lite
#   - `pip install super-q`
#   - drop your public ssh key into ~/.ssh/authorized_keys

superq sweep ./my-core --pool=homelab --parallel=6
```

## 4. GitHub Actions as compute

Why: essentially free for public repos (2000 min/month on free tier,
unlimited for public), already your CI, artifacts come back through
the normal Actions UI.

```bash
pip install 'super-q[gha]'
# Copy .github/workflows/build-core.yml from this repo into your core's repo.
# Point super-q at it:

cat >> ~/.superq/config.toml <<'EOF'
[pool.gha]
kind = "gha"
repo = "you/your-pocket-core"
workflow = "build-core.yml"
branch = "main"
artifact_bucket = "your-tigris-bucket"   # runner pulls sandbox from here
artifact_endpoint = "https://fly.storage.tigris.dev"
max_parallel = 10
EOF

export GH_TOKEN=$(gh auth token)
superq sweep . --pool=gha --parallel=10
```

Slower startup (~30 s per seed for the runner to boot) but the
per-seed compute is free-ish and the results auto-archive in your
Actions run history — convenient for audits.

## 5. Local workstation

Still supported and often the best choice for a single-core flow on a
well-specced machine. If you don't want to manage remote infra:

```bash
superq sweep ./my-core --parallel=$(($(nproc)/4))
```

## Which should I use?

| you want…                                   | pick           |
|---------------------------------------------|----------------|
| Fastest path from zero to 32 parallel seeds | **Modal**      |
| A machine I can SSH into, specific region   | **Fly**        |
| No SaaS at all, already have Linux boxes    | **SSH pool**   |
| Free compute, public repo, CI-native        | **GHA**        |
| Quick local iteration                       | **local**      |

You can mix: e.g. `watch-build` on local (incremental, warm shell) and
`sweep --pool=modal` when you want a wider search.

## AWS?

Supported but de-emphasized. The `aws` backend and
`cloud/terraform/main.tf` remain for users who already live in AWS-land.
Everyone else should start with Modal or Fly.
