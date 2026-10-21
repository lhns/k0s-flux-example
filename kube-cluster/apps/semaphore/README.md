# semaphore

Ansible / automation web UI (`semaphoreui/semaphore`).

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

## Verify
- `kubectl -n semaphore get pods` → `1/1`; no keystore-decrypt errors in the log.
- `https://semaphore.example.com` → login as `admin`; existing projects, inventories, keys, and task history
  present.
