# sftpgo

SFTP / FTP / WebDAV file server with OIDC login — migrated from Docker Swarm (`10.20.2.10`).

## Shape
sftpgo is dual-natured, so it uses two ingress paths:

- **HTTP (via Traefik)** — web UI on `:8080` (`sftpgo.example.com`) and WebDAV on `:10080`
  (`webdav.example.com`), both under the `*.example.com` wildcard cert. sftpgo does its **own** OIDC login
  against Authelia (client `sftpgo` already registered), so there is **no forwardAuth middleware**.
- **Raw TCP (via a dedicated MetalLB VIP `10.20.2.17`)** — SFTP `:2022`, FTP `:2121`, and FTP passive
  `:50000-50010`. Traefik can't carry these (SFTP is SSH; no SNI), and its VIP can't be shared
  (`externalTrafficPolicy: Local`), so `sftpgo-lb` is its own `LoadBalancer` Service. The pool is in
  `infra/metallb/pool.yaml` (`sftpgo-vip`). `FORCE_PASSIVE_IP` is set to this VIP.

## Storage
- **`sftpgo-config` (ceph-rbd, 2Gi)** at `/var/lib/sftpgo` — the SQLite data-provider db (`sftpgo.db`)
  and the SFTP host keys. On block storage for proper SQLite locking. Copied in once during the Swarm
  migration from `fastappdata:/docker/sftpgo/config` (preserving the host keys so clients don't hit
  "host key changed"); the live db lives here and is backed up by Velero.
- **Static CephFS PVs (mounted in place, RW, not copied)** — the filesystems sftpgo serves:
  - `sftpgo-appdata` (fs `appdata`) → `/appdata`, `/mnt/appdata`, and `/srv/sftpgo` (subPath
    `docker/sftpgo/data`, ~249G).
  - `sftpgo-fastappdata` (fs `fastappdata`) → `/mnt/fastappdata`.
  - `sftpgo-userdata` (fs `userdata`) → `/userdata`, `/mnt/userdata`.
  - `sftpgo-archive` (fs `archive`, rootPath `/media`) → `/media`, `/mnt/archive/media`.

Runs as **root** (compose parity — the served library is root-owned), **no fsGroup** (avoids a recursive
chown of the 249G library). Single-writer → `Recreate`, `replicas: 1` (never two).

## Cutover
The Deployment ships with `replicas: 0`. To go live:

1. Confirm the manifests are applied (ns, PVCs bound, `sftpgo-lb` has `10.20.2.17`).
2. **Stop the Swarm sftpgo stack** — frees `sftpgo.db` and the served dirs so the final db can be copied.
3. Copy the db + host keys from `fastappdata:/docker/sftpgo/config` into the `sftpgo-config` RBD volume
   (one-time; done at migration). Then scale up:
   ```
   kubectl -n sftpgo scale deploy/sftpgo --replicas=1
   ```
   (or set `replicas: 1` in `resources.yaml` and let Flux apply it).
4. **DNS**: point `sftpgo.example.com` + `webdav.example.com` (+ `.kube`) at the Traefik VIP `10.20.2.15`; point
   SFTP/FTP clients at `10.20.2.17`.

## Verify
- `kubectl -n sftpgo get pods,svc` — sftpgo `1/1`, `sftpgo-lb` EXTERNAL-IP `10.20.2.17`.
- `https://sftpgo.example.com` → login; OIDC round-trips to auth.example.com; users/folders present.
- `ssh-keyscan -p 2022 10.20.2.17` → fingerprint matches the old server.
- `sftp -P 2022 <user>@10.20.2.17` lists a folder; passive FTP transfer works.
