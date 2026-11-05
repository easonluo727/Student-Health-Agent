#!/bin/bash
# The VM's startup script (runs as root at every boot): mount the data disk at /data, formatting it only the first
# time, and install Docker once.
set -e
DEV=/dev/disk/by-id/google-tally-data
if ! blkid "$DEV" >/dev/null 2>&1; then mkfs.ext4 -F "$DEV"; fi
mkdir -p /data
grep -q " /data " /etc/fstab || echo "$DEV /data ext4 defaults,nofail 0 2" >> /etc/fstab
mountpoint -q /data || mount /data
command -v docker >/dev/null || (curl -fsSL https://get.docker.com | sh)
systemctl enable --now docker
