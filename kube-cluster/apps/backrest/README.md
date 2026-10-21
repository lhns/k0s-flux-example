# backrest

Restic backup orchestration UI (`ghcr.io/garethgeorge/backrest`) — migrated from Docker Swarm
(`10.20.176.187`). Served on `:9898` behind Traefik at `backrest.example.com`, gated by Authelia (the
`*.example.com` → `group:admin` / `two_factor` catch-all covers it) plus backrest's own login.

## Storage
- **`backrest-state` (ceph-rbd, 2Gi)** — one PVC, three `subPath` mounts:
  - `data` → `/data` (SQLite `oplog.sqlite` + `kvdb.sqlite` + `jwt-secret` — operation history).
  - `config` → `/config` (`config.json`: restic repo password + backrest's own login; backrest **rewrites**
    it from the UI, so it lives here, not in a k8s Secret).
  - `rclone` → `/root/.config/rclone` (rclone remotes; empty today).

  Copied once from the Swarm volume; **secrets never enter git**. Block storage for the SQLite state,
  single-writer → `Recreate`.
- **`backrest-cache` (cephfs, 20Gi)** at `/cache` — restic's pack/index cache. Labeled
  `velero.io/exclude-from-backup: "true"`: it **persists across restarts** (no re-download storm) but is
  **skipped by Velero's daily 30-day snapshot** (fully regenerable). `/tmp` is an `emptyDir` (transient).
- **Static CephFS PVs (mounted in place, RW, not copied)** — the filesystems backrest backs up:
  `appdata` → `/mnt/appdata`, `fastdata` → `/mnt/fastdata`, `userdata` → `/mnt/userdata`, and the
  (empty) local restic repo dir `appdata:/docker/backrest/repos` → `/repos`. The configured repo
  (`restic-demo.offsite.example.net`) is **remote**, so there's no large local backup storage.

Runs as **root** (reads/writes the root-owned mounts), no fsGroup. `enableServiceLinks: false` +
`BACKREST_PORT=0.0.0.0:9898` (the service-link `BACKREST_PORT` would otherwise crash the bind).

## Backups (Velero)
The `backrest-state` PVC is included in the cluster's Velero `velero-daily` schedule (03:00, CSI snapshot,
30-day retention) — so backrest's config + oplog are themselves backed up. The cache is excluded (above).

## Verify
- `kubectl -n backrest get pods` → `1/1`; no bind/port error in the log.
- `backrest.example.com` → Authelia → backrest; the repo + 4 plans and the operation history are present.
- backrest can reach the remote repo (list snapshots / index succeeds); a manual backup runs.
