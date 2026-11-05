# Google Cloud deployment

The product runs on one Compute Engine VM, `tally` (project `peak-seat-513523-h8`, zone `us-west2-a`, e2-standard-2),
at https://34-20-165-37.sslip.io (static IP `tally-ip`, 34.20.165.37; sslip.io resolves the name to that IP).

- `compose.yml`: the app image from the root `Dockerfile`, and Caddy in front of it for HTTPS (Let's Encrypt). The chat
  stream (`/api/turn`) is not compressed or buffered.
- `/data` on the VM is the persistent disk `tally-data` (kept when the VM is deleted): `rates.sqlite`,
  `providers.sqlite`, `policy.sqlite`, `app.sqlite` and `uploads/`, the same layout as the old Railway volume.
- `/opt/tally/app.env` on the VM holds the app's environment (`LLM_*`, `GOOGLE_CLIENT_ID`, `BILL_*`), mode 600.
- `startup.sh` is the VM's startup script: it mounts the disk and installs Docker.

To deploy the working tree (tracked and new files, nothing gitignored):

```bash
deploy/gcp/deploy.sh deploy
```

`deploy.sh setup`, `data` and `env` were run once to create the VM, copy the data and copy the environment from
Railway; rerunning `data` overwrites the app data on the VM with Railway's. To change a variable, edit
`/opt/tally/app.env` on the VM (`gcloud compute ssh tally --zone us-west2-a`) and run
`cd /opt/tally/src/deploy/gcp && sudo docker compose up -d`. Logs: `sudo docker compose logs -f app` there.

Google sign-in works only from origins listed on the OAuth client (APIs & Services → Credentials in the same project),
so a new domain must be added there. With your own domain, point its A record at 34.20.165.37 and deploy with
`SITE_DOMAIN=your.domain deploy/gcp/deploy.sh deploy`.
