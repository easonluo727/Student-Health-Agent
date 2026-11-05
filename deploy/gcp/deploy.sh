#!/usr/bin/env bash
# Deploy the product to one Google Compute Engine VM (deploy/gcp/README.md). Run from anywhere, in this order the
# first time; afterwards only `deploy`.
#
#   deploy/gcp/deploy.sh setup    # firewall, static IP, data disk, VM (Docker installed by the startup script)
#   deploy/gcp/deploy.sh data     # rates + providers from this machine, the app's own data from Railway, onto /data
#   deploy/gcp/deploy.sh env      # the app's environment (LLM_*, GOOGLE_CLIENT_ID, BILL_*) from Railway
#   deploy/gcp/deploy.sh deploy   # the working tree (tracked and new files), built and started on the VM
#
# Needs gcloud, logged in, with a project set (gcloud config set project ...); `data` and `env` need the railway CLI
# linked to the agent-api service.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ZONE="${ZONE:-us-west2-a}"            # Los Angeles, where the users and the plans are
REGION="${ZONE%-*}"
VM="${VM:-tally}"
DISK="${DISK:-tally-data}"
IP_NAME="${IP_NAME:-tally-ip}"
MACHINE="${MACHINE:-e2-standard-2}"   # 2 vCPU, 8 GB; the app peaks at about 4 GB
DISK_GB="${DISK_GB:-30}"              # rates.sqlite is 3.1 GB

ssh_vm() { gcloud compute ssh "$VM" --zone "$ZONE" --command "$1"; }
ip() { gcloud compute addresses describe "$IP_NAME" --region "$REGION" --format 'value(address)'; }
site() { echo "${SITE_DOMAIN:-$(ip | tr . -).sslip.io}"; }  # sslip.io resolves 1-2-3-4.sslip.io to 1.2.3.4

setup() {
  gcloud services enable compute.googleapis.com
  gcloud compute firewall-rules describe tally-web >/dev/null 2>&1 ||
    gcloud compute firewall-rules create tally-web --allow tcp:80,tcp:443 --target-tags tally-web
  gcloud compute addresses describe "$IP_NAME" --region "$REGION" >/dev/null 2>&1 ||
    gcloud compute addresses create "$IP_NAME" --region "$REGION"
  gcloud compute disks describe "$DISK" --zone "$ZONE" >/dev/null 2>&1 ||
    gcloud compute disks create "$DISK" --zone "$ZONE" --size "${DISK_GB}GB" --type pd-balanced
  gcloud compute instances describe "$VM" --zone "$ZONE" >/dev/null 2>&1 ||
    gcloud compute instances create "$VM" --zone "$ZONE" --machine-type "$MACHINE" \
      --image-family debian-12 --image-project debian-cloud --boot-disk-size 30GB \
      --disk "name=$DISK,device-name=$DISK,auto-delete=no" \
      --address "$(ip)" --tags tally-web \
      --metadata-from-file startup-script="$ROOT/deploy/gcp/startup.sh"
  echo "VM $VM at $(ip); the site will be https://$(site)"
  echo "Waiting for Docker on the VM..."
  until ssh_vm 'command -v docker >/dev/null && mountpoint -q /data' 2>/dev/null; do sleep 10; done
  echo "Ready."
}

data() {
  local tmp; tmp="$(mktemp -d)"
  # The app's own data, as consistent SQLite snapshots (the databases are live), plus uploaded files.
  railway ssh -- 'rm -rf /tmp/export && mkdir /tmp/export && cd /data &&
    for db in app.sqlite policy.sqlite sessions.sqlite users.sqlite; do
      [ -f $db ] && python -c "import sqlite3,sys; sqlite3.connect(sys.argv[1]).backup(sqlite3.connect(sys.argv[2]))" $db /tmp/export/$db;
    done; cp -r uploads /tmp/export/ 2>/dev/null; tar czf - -C /tmp/export . | base64 -w0; rm -rf /tmp/export' \
    | tr -d '\r' | base64 -d > "$tmp/appdata.tgz"
  tar tzf "$tmp/appdata.tgz" >/dev/null  # fails here if the transfer was cut
  echo "App data from Railway: $(du -h "$tmp/appdata.tgz" | cut -f1)"
  echo "Compressing rates.sqlite (3.1 GB)..."
  xz -T0 -3 -c "$ROOT/data/mrf/rates.sqlite" > "$tmp/rates.sqlite.xz"
  gcloud compute scp --zone "$ZONE" "$tmp/appdata.tgz" "$tmp/rates.sqlite.xz" \
    "$ROOT/data/providers/providers.sqlite" "$VM:/tmp/"
  ssh_vm 'set -e; cd /tmp; sudo tar xzf appdata.tgz -C /data; sudo mv providers.sqlite /data/;
    sudo sh -c "xz -dc rates.sqlite.xz > /data/rates.sqlite"; rm -f appdata.tgz rates.sqlite.xz; ls -la /data'
  rm -rf "$tmp"
}

env() {
  # Straight from Railway to the VM: the values are never printed or written on this machine.
  railway variables --kv | grep -E '^(LLM_|GOOGLE_CLIENT_ID=|BILL_)' |
    gcloud compute ssh "$VM" --zone "$ZONE" --command \
      'sudo mkdir -p /opt/tally && sudo tee /opt/tally/app.env >/dev/null && sudo chmod 600 /opt/tally/app.env &&
       sudo cut -d= -f1 /opt/tally/app.env'
}

deploy() {
  local tgz; tgz="$(mktemp).tgz"
  # The working tree as git sees it: tracked and new files, nothing gitignored (data, .env, node_modules).
  (cd "$ROOT" && git ls-files -z --cached --others --exclude-standard |
    COPYFILE_DISABLE=1 tar --null --no-xattrs -czf "$tgz" -T -)
  gcloud compute scp --zone "$ZONE" "$tgz" "$VM:/tmp/src.tgz"
  rm -f "$tgz"
  ssh_vm "set -e; sudo rm -rf /opt/tally/src && sudo mkdir -p /opt/tally/src &&
    sudo tar xzf /tmp/src.tgz -C /opt/tally/src && rm /tmp/src.tgz &&
    cd /opt/tally/src/deploy/gcp && echo SITE_DOMAIN=$(site) | sudo tee .env >/dev/null &&
    sudo docker compose up -d --build --remove-orphans &&
    sudo docker image prune -f >/dev/null"
  echo "Deployed: https://$(site)"
}

"${1:?usage: deploy.sh setup|data|env|deploy}"
