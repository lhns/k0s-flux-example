# duckdb

The [DuckDB UI](https://duckdb.org/docs/extensions/ui) (`duckdb/duckdb`), ported
from a Docker Swarm stack. Exposed at **`duckdb.example.com`** via traefik, behind
**Authelia SSO**.

- `resources.yaml` — a `ceph-rbd` PVC for the workspace,
  the StatefulSet (`duckdb` + a `socat` sidecar), Service, the `duckdb-headers`
  Middleware, and the Ingress.

## How it runs

The `duckdb/duckdb` image is distroless, so instead of the Swarm
`sh -c "socat & duckdb"` we run the binary directly and put **socat in a sidecar**:

- **duckdb** runs `duckdb -cmd "… CALL start_ui_server();" database.db` in
  `/workspace`. The UI binds `[::1]:4213` (IPv6 localhost). `tty`+`stdin` keep the
  interactive CLI alive; `HOME=/workspace` caches the `ui` extension on the volume.
- **socat** sidecar forwards `:4214 → [::1]:4213` so the Service can reach the UI.

## Auth + headers

The Ingress chains two middlewares (order matters):

1. `duckdb-authelia@kubernetescrd` — the reflected Authelia SSO middleware (sees the real
   host for access control).
2. `duckdb-duckdb-headers@kubernetescrd` — rewrites `Host`/`Origin`/`Referer` to
   `localhost:4213` so the UI accepts the proxied request.

## Storage

`database.db` lives on a fresh `ceph-rbd` (block) volume — appropriate for a
single-writer DB. To bring over an existing workspace, copy `database.db` into the
PVC (e.g. `kubectl cp ./database.db duckdb/<pod>:/workspace/database.db`).

## Decisions

- **A one-replica StatefulSet, not a Deployment**, per ADR
  [0007](../../../docs/adr/0007-ceph-csi-storage-classes-by-access-pattern.md). It mounts the
  existing `duckdb-workspace` PVC by `claimName`; a `volumeClaimTemplate` would provision a new,
  empty volume. After a node loss the pod waits for the owner:
  [operations](../../../docs/operations.md#a-node-is-lost-statefulset-pods-stay-terminating).
