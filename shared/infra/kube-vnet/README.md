# kube-vnet

Deploys [kube-vnet](https://github.com/lhns/kube-vnet) — cluster-wide ingress
isolation implemented as plain `networking.k8s.io/v1` NetworkPolicies — from its
published OCI chart (`oci://ghcr.io/lhns/charts/kube-vnet`).

The operator generates the NetworkPolicies at runtime from
`VirtualNetwork`/`VirtualNetworkBinding` CRs + the cluster baseline.

A shared base: each cluster deploys it from `<cluster>/infra/kube-vnet/`, which holds the
chart version (`version.yaml`, the `OCIRepository` tag patched onto the base) and, where Flux
manages it, the Namespace (`namespaces/`).

- `release.yaml` — `OCIRepository` without a tag + `HelmRelease`.

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

Roll a new version by bumping `ref.tag` in each cluster's `version.yaml` (the linter
derives the matching schema version from kube-cluster's).

Use the pod annotation `kube-vnet/network-max-wait` only for an app that stays running but
broken when its network is not ready at start, or whose crash on start is user-visible (Authelia,
the login path). An app that exits and quietly recovers on restart does not need it. The wait
ends as soon as every node has programmed the pod; the value only bounds a dead node's beacon.
