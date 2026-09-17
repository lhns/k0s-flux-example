# barman-cloud-plugin

The [CloudNativePG Barman Cloud Plugin](https://github.com/cloudnative-pg/plugin-barman-cloud)
(CNPG-I) — object-store backups + WAL archiving for CloudNativePG, and the go-forward
replacement for the deprecated in-tree `spec.backup.barmanObjectStore`. Runs in
`cnpg-system` (the operator's namespace) and uses cert-manager for its gRPC TLS to the
operator.

- `manifest.yaml` — the pinned upstream release (`v0.15.0`). Provides the `ObjectStore`
  CRD (`barmancloud.cnpg.io`) + the plugin Deployment/RBAC/certs. There is no upstream
  Helm chart, so it's vendored and bumped by hand.

  Bump it by replacing the whole file with the upstream release manifest, never by editing
  the image tag alone: the Deployment's image and the `SIDECAR_IMAGE` in the generated
  Secret are two separate versions, and the Secret's name carries a content hash. Editing
  one line leaves the controller and the injected sidecar on different releases.

Used by `infra/postgres`: its `ObjectStore` points at the Ceph RGW bucket, and the Cluster
references the plugin as its WAL archiver. Egress-only → no kube-vnet rule.
