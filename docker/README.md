# Docker backend

Quartus runs inside a reproducible Ubuntu 22.04 container. This is the
recommended path on macOS and for CI.

## Building the image

Intel's redistribution terms prevent us from shipping Quartus inside
a public image. You need to accept the EULA and build the image once:

```bash
# After accepting Intel's EULA at
#   https://www.intel.com/content/www/us/en/legal/end-user-license-agreement.html
# save the exact text locally:
cat > EULA.txt <<'EOF'
I have read and accept the Intel Simplified Software License Agreement.
EOF

docker build \
    --secret id=altera_eula,src=./EULA.txt \
    --build-arg QUARTUS_VERSION=24.1 \
    -f docker/Dockerfile \
    -t superq/quartus:24.1 .
```

The first build is slow (Quartus is ~8 GB to download) but gets cached.

## Using the image with super-q

```bash
superq sweep ./my-core --backend=docker --parallel=8
```

The CLI passes `--parallel=8` through to the backend, which fans out
8 containers on the local Docker daemon. Each container bind-mounts
your core folder read-only and writes artifacts back to
`<core>/.superq/artifacts/`.

## Using the image directly

```bash
docker run --rm \
    -e SUPER_Q_SEED=7 \
    -v "$PWD:/work/core:ro" \
    -v "$PWD/.superq-out:/work/out:rw" \
    superq/quartus:24.1 full src/fpga myproject
```

## Docker Swarm (many machines)

For larger seed sweeps, run the image as a Swarm service and have each
worker connect to a shared SQLite on NFS (or swap in a PostgreSQL URL
via `SUPERQ_DB_URL` — see `TODO(cloud-db)` tag in `db.py`).

```bash
docker service create \
  --name superq-workers \
  --replicas 16 \
  --mount type=bind,src=/mnt/superq-state,dst=/state \
  -e SUPERQ_HOME=/state \
  superq/quartus:24.1 \
  super-q-worker daemon --slots=2
```
