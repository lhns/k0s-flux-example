# barman-cloud-plugin

The [CloudNativePG Barman Cloud Plugin](https://github.com/cloudnative-pg/plugin-barman-cloud)
(CNPG-I) — object-store backups + WAL archiving for CloudNativePG, and the go-forward
replacement for the deprecated in-tree `spec.backup.barmanObjectStore`. Runs in
`cnpg-system` (the operator's namespace) and uses cert-manager for its gRPC TLS to the
operator.

- `manifest.yaml` — the pinned upstream release (`v0.13.0`). Provides the `ObjectStore`
  CRD (`barmancloud.cnpg.io`) + the plugin Deployment/RBAC/certs. There is no upstream
  Helm chart, so it's vendored and bumped by hand.

Used by `infra/postgres`: its `ObjectStore` points at the Ceph RGW bucket, and the Cluster
references the plugin as its WAL archiver. Egress-only → no kube-vnet rule.
