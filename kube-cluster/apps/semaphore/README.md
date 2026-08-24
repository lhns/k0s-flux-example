# semaphore

Ansible / automation web UI (`semaphoreui/semaphore`) — migrated from Docker Swarm (`10.20.2.10`).

## Shape
Single self-contained service: an **embedded BoltDB** (`SEMAPHORE_DB_DIALECT: bolt`, no external
database), one HTTP port `:3000` behind Traefik (`semaphore.example.com`), its own login (no OIDC / no
forwardAuth middleware). Ansible runs, SSH to managed hosts, and git clones are all external egress.

## Storage
- **`semaphore-data` (ceph-rbd, 2Gi)** — BoltDB is mmap-based + single-writer → block storage. One PVC,
  two `subPath` mounts:
  - `config` → `/etc/semaphore` (holds `config.json`, incl. `access_key_encryption`).
  - `data` → `/var/lib/semaphore` (holds `database.boltdb`).

Runs as **root** (source files are `ansible`-owned; root reads/writes them and runs ansible), no fsGroup.
Single-writer → `Recreate`, `replicas: 1`.

> **config.json is not optional.** It holds `access_key_encryption`, the key that decrypts the SSH
> keys/secrets stored in the BoltDB keystore. It is copied verbatim — regenerating it makes every stored
> key undecryptable.

## Cutover
Ships `replicas: 0`. To go live:

1. Confirm applied (ns + `semaphore-data` PVC bound).
2. **Stop the Swarm semaphore stack** (frees the BoltDB for a consistent copy).
3. Copy `config/` + `data/` (~17M) from `10.20.2.10:/mnt/fastappdata/docker/semaphore` into the PVC — a
   temp pod mounting `semaphore-data`, tar-pipe over ssh, landing at `<vol>/config` and `<vol>/data`;
   `chown -R 0:0`.
4. Scale up:
   ```
   kubectl -n semaphore scale deploy/semaphore --replicas=1
   ```
   (or set `replicas: 1` in `resources.yaml`). Semaphore opens the copied BoltDB, decrypts the keystore
   with the copied `config.json`, reconciles the admin.
5. **DNS**: point `semaphore.example.com` (+ `.kube`) at the Traefik VIP `10.20.2.15`.

## Verify
- `kubectl -n semaphore get pods` → `1/1`; no keystore-decrypt errors in the log.
- `https://semaphore.example.com` → login as `admin`; existing projects, inventories, keys, and task history
  present.
