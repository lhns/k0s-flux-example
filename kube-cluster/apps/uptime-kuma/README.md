# uptime-kuma

Uptime Kuma (`louislam/uptime-kuma`, status/uptime monitor). Public at
`uptimekuma.example.com` (gated by **Authelia forwardAuth**); `uptimekuma.kube.example.com` redirects
there.

## Shape
- **No external database** — Uptime Kuma keeps everything (the embedded **SQLite** `kuma.db`, config,
  uploads) in `/app/data`. All of it is on a dedicated Ceph RBD volume
  (`uptime-kuma-data`, RWO). No CNPG, no SOPS secrets.
- **Auth**: Uptime Kuma's own login is **disabled** (`disableAuth=true` in its DB), so the external
  **Authelia forwardAuth** (the `authelia` middleware, reflected into every namespace by
  traefik-reflector) is the sole gate. No OIDC client.
- **Version**: on v2. Its schema upgrade was one-way, so a rollback to v1 needs the DB restored from
  backup, not just the tag reverted.
- **kube-vnet**: only `net.traefik.traefik: ingress`. Monitor probes reach their targets over
  unrestricted external egress; a monitor pointing at an in-cluster service behind a kube-vnet would
  need that vnet opened separately.
- Single replica, as a StatefulSet (RWO SQLite volume; see Decisions).
- The Swarm `prometheus.*` scrape labels were dropped — there is no in-cluster Prometheus.

## Decisions
- **A one-replica StatefulSet, not a Deployment**, per ADR
  [0007](../../../docs/adr/0007-ceph-csi-storage-classes-by-access-pattern.md). It mounts the
  existing `uptime-kuma-data` PVC by `claimName`; a `volumeClaimTemplate` would provision a new,
  empty volume. After a node loss the pod waits for the owner:
  [operations](../../../docs/operations.md#a-node-is-lost-statefulset-pods-stay-terminating).
