# syncthing

File-sync (`syncthing/syncthing`) — migrated from Docker Swarm (`docker-node-03`).

## Shape
- **Web UI `:8384`** via Traefik at `syncthing.example.com` (+ `syncthing.example.net` redirect, `.kube`
  redirect). Syncthing keeps its **own GUI login** — no Authelia (matches the compose).
- **Raw sync `:22000` (TCP + UDP)** on a **dedicated MetalLB VIP `10.20.2.18`** (`syncthing-lb`). Traefik
  here is TCP/HTTP-only (no UDP entrypoint), so the sync port gets its own LoadBalancer — same pattern as
  sftpgo/node-red. The pool is in `infra/metallb/pool.yaml` (`syncthing-vip`).

## Storage — mounted in place, nothing copied
The entire home `/var/syncthing` (device identity `cert.pem`/`key.pem`, `config.xml`, the live `index-v2`
SQLite DB, **and 249G of synced folders**) is one **static CephFS PV** on the `appdata` fs
(`rootPath /docker/syncthing`), mounted RW. Mounting in place preserves the **Device ID** (so peers don't
need re-approval) and the index (no full re-hash).

The **SQLite index (`index-v2`) is on a separate `ceph-rbd` block volume** (`syncthing-index`), overlaid
at `/var/syncthing/config/index-v2`. SQLite's fsync-heavy access — especially the one-time v2.0→v2.1
index migration — is *pathologically* slow on CephFS (~1 KB/s), so it lives on block storage (this mirrors
how the index was kept on fast local disk on Swarm). It was seeded once by copying the existing `index-v2`
off the CephFS home into the RBD volume (subPath `idx`).

Notes:
- The source is already on **syncthing v2.x**, so the old `index-v0.14.0.db` LevelDB bind-mount is
  vestigial and **dropped** — v2 uses `index-v2` in the config dir. (A stale `index-v0.14.0.db` dir still
  sits in the CephFS config from the old setup; syncthing logs an advisory to remove it — harmless.)
- Runs as **uid/gid 1000** (the data's owner). Being non-root makes the image entrypoint skip its
  root-only chown of `$HOME`, so there's **no walk of the 249G tree**. No fsGroup.

## Cutover
Swarm syncthing was stopped, so this shipped **live** (`replicas: 1`) — no copy step. On startup syncthing
opens the same identity + config + index off appdata.

**Your side:**
- Point `syncthing.example.com` (+ `.lolhens`/`.kube`) DNS at the Traefik VIP `10.20.2.15`.
- **Repoint the router's `22000` TCP+UDP port-forward to `10.20.2.18`** (was docker-node-03). Until then,
  relay/outbound sync works but direct inbound won't.

## Verify
- `kubectl -n syncthing get pods,svc` → pod `1/1`, `syncthing-lb` EXTERNAL-IP `10.20.2.18`.
- Log shows `My ID: <same device ID as before>` + folders loaded off `index-v2` (no re-hash).
- `https://syncthing.example.com` → GUI login; devices + folders present, folder states green.
- `22000` reachable tcp+udp on `10.20.2.18`; a remote device reconnects.
