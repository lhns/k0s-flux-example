# kube-test-cluster

A second cluster, scaffolded but not yet bootstrapped. Same shape as `kube-cluster/`, rendered
by its own root release; see [Multiple clusters](../README.md#multiple-clusters) for how the two
relate and for the bootstrap procedure.

## Placeholders to fill before bootstrapping

| what | where |
| --- | --- |
| node addresses (`192.0.2.x`, TEST-NET-1) | `k0sctl.yaml` |
| the traefik VIP `192.0.2.100` | `infra/metallb-pool/pool.yaml`, `infra/traefik/release.yaml` |
| the DNS zone `*.test.kube.example.com` | `infra/traefik-config/tls.yaml`, `apps/nginx-test/resources.yaml` |
| storage: prod's StorageClass names on node-local disk | `infra/local-storage/` |
| a GitHub push webhook: none, so Flux polls git hourly | add an `infra/flux-webhook` like prod's |

## What runs here

- `infra/reflector`: the shared base `shared/infra/reflector`; copies `sops-age` into every
  control namespace.
- `infra/reloader`: the shared base `shared/infra/reloader` with this cluster's version.
- `infra/kube-vnet`: the shared base `shared/infra/kube-vnet`; pod-level ingress isolation, as
  in prod.
- `infra/cert-manager` (the shared base `shared/infra/cert-manager`) and
  `infra/cert-manager-issuers`: a self-signed CA, `ClusterIssuer test-ca`. No ACME and no DNS
  credentials.
- `infra/metallb` (the shared base `shared/infra/metallb`), `infra/metallb-pool`, `infra/traefik`,
  `infra/traefik-config`: one VIP, the `web` and `websecure` entrypoints, a default cert for
  `*.test.kube.example.com`.
- `infra/local-storage`: local-path-provisioner. No Ceph, no snapshots, no pv-reaper.
- `apps/nginx-test`: the sample app, exercising `namespaces/`, `substitution-secrets/`,
  `as-secret`, a PVC, an IngressRoute and `dependsOn`.

Operators split from their custom resources (`cert-manager-issuers`, `metallb-pool`,
`traefik-config`) because a fresh cluster has no CRDs yet: one Kustomization holding a
HelmRelease and its CRs fails its dry-run before the HelmRelease exists. Prod never hit this,
since k0s installed those operators first.

Secrets here are encrypted to this cluster's own age key (`.sops.yaml`). The one in
`apps/nginx-test` is a dummy value.
