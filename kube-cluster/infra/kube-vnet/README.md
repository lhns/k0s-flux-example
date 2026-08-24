# kube-vnet

Deploys [kube-vnet](https://github.com/lhns/kube-vnet) — cluster-wide ingress
isolation implemented as plain `networking.k8s.io/v1` NetworkPolicies — from its
published OCI chart (`oci://ghcr.io/lhns/charts/kube-vnet`).

Migrated off the k0s helm extension into Flux (adopt-in-place). The operator
generates the NetworkPolicies at runtime from `VirtualNetwork`/`VirtualNetworkBinding`
CRs + the cluster baseline.

- `release.yaml` — `OCIRepository` (pinned tag) + `HelmRelease`.

Config (`release.yaml` values):
- `clusterBaseline.ingressIsolationLevel: pod` — default-deny ingress everywhere
  (even same-namespace); ingress only via an explicit vnet membership/binding plus
  the auto-allows (LoadBalancer/NodePort Services, hostPorts, apiserver-dialed
  webhooks).
- `disabledNamespaces: []` — manage every namespace, including kube-system. The
  operator always self-excludes its own namespace (kube-vnet-system), so `[]` is
  safe. (As of chart 0.5.0 an empty list means "disable nothing"; older charts fell
  back to the system-namespace default on `[]`.)

Cluster DNS survives pod-level isolation via the chart's own `dnsCarveout`
NetworkPolicy (`kube-vnet-coredns-allow`, opens `:53` from `0.0.0.0/0`), rendered
automatically whenever kube-system is managed — no hand-rolled binding needed.

The chart also publishes JSON schemas for its CRDs (0.5.1+), so `scripts/validate.sh`
schema-checks the `VirtualNetwork*` resources instead of skipping them.

Roll a new version by bumping `ref.tag` (the linter derives the matching schema
version from it).
