# Building the super-q AWS AMI

The AWS backend spawns spot instances from a pre-baked AMI containing
Quartus Lite + super-q. Bake it once per Quartus release, then reuse.

## One-time setup

1. Launch an `ubuntu-22.04-amd64` instance (t3.large is fine for baking).
2. Accept Intel's EULA on that instance.
3. Run:

   ```bash
   # On the instance
   sudo apt-get update && sudo apt-get install -y python3-pip git rsync
   git clone https://github.com/you/super-q.git
   cd super-q
   bash scripts/install-quartus.sh --version=24.1 --accept-eula
   pip3 install -e '.[aws]'
   ```

4. Verify:

   ```bash
   superq info   # should show quartus 24.1
   ```

5. Add a systemd unit `/etc/systemd/system/superq-worker.service`:

   ```ini
   [Unit]
   Description=super-q worker
   After=network-online.target

   [Service]
   Type=simple
   ExecStart=/usr/local/bin/super-q-worker daemon --slots=4 --idle-quit=600
   Restart=no
   User=ubuntu
   Environment=QUARTUS_ROOTDIR=/home/ubuntu/intelFPGA_lite/24.1/quartus
   Environment=PATH=/home/ubuntu/intelFPGA_lite/24.1/quartus/bin:/usr/local/bin:/usr/bin:/bin

   [Install]
   WantedBy=multi-user.target
   ```

6. `sudo systemctl enable superq-worker` so the AMI boots ready to serve.
7. Snapshot the instance:

   ```bash
   aws ec2 create-image --instance-id i-xxx \
       --name "superq-quartus-24.1-$(date +%Y%m%d)" \
       --no-reboot
   ```

8. Use the resulting `ami-…` id as `var.ami_id` in `cloud/terraform/main.tf`.

## Why pre-bake instead of cloud-init?

Quartus is ~15 GB unpacked. Downloading it at instance launch burns
5+ minutes on every spot instance. Pre-baking gets you a clean 20-second
cold start.

## Cost notes

`c7i.4xlarge` spot in `us-east-2` runs ~$0.24/hr as of 2026-Q2.
A typical Pocket core full compile is 3–8 minutes, so a 16-seed sweep
using 8 parallel workers finishes in ~10 min wall clock for ~$0.32.
