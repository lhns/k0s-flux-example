# cloudnative-pg

The [CloudNativePG](https://cloudnative-pg.io) operator — manages PostgreSQL
`Cluster` resources (HA via Postgres streaming replication, backups, failover).

- `release.yaml` — HelmRepository `cnpg` + HelmRelease `cloudnative-pg` (installs
  the operator into `cnpg-system` and the `postgresql.cnpg.io` CRDs).

Databases live elsewhere (e.g. `infra/postgres/`) as `Cluster` CRs that depend on
this component (via the `dependsOn` annotation) so the CRDs exist first. Storage
is per-`Cluster` (`spec.storage.storageClass`).
