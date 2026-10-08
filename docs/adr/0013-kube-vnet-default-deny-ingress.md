# 0013 — kube-vnet: default-deny ingress, pod by pod

- Status: Accepted
- Date: 2026-07-08, amended 2026-07-09, 2026-07-26 and 2026-10-08
- Current answer: [kube-vnet](https://github.com/lhns/kube-vnet) (the owner's own operator,
  [0009](0009-own-code-in-own-repos.md)) turns `VirtualNetwork`/`VirtualNetworkBinding` CRs into
  plain NetworkPolicies. Ingress is denied everywhere, even within a namespace, including
  `kube-system`; a pod receives traffic only through an explicit vnet membership or an auto-allow
  (LoadBalancer/NodePort Services, hostPorts, apiserver-dialed webhooks). Egress is not
  restricted by kube-vnet.

## Context

Swarm overlay networks scoped who could reach whom. On Kubernetes the default is that every pod
reaches every pod.

## Decision

- **Pod-level isolation** (2026-07-08): `ingressIsolationLevel: pod` with every namespace managed
  (`disabledNamespaces: []`), after a CoreDNS carve-out.
- Membership is by pod label; each component declares its vnets in its own directory
  (`vnet.yaml`), next to the workloads.
- **Narrow vnets** (2026-07-09): a dedicated `VirtualNetwork` with selector-scoped bindings, not a
  namespace-wide baseline. A baseline only where the namespace is its own sole tenant and genuinely
  needs a mesh.
- **`allowedNamespaces` lists only other namespaces** (2026-07-26): same-namespace
  members are allowed anyway, so a vnet never lists its own namespace.
- From chart 0.5.0 (2026-07-09) the chart ships the DNS carve-out and publishes CRD schemas, so
  `validate.sh` checks the CRs.
- **Network wait is per-pod opt-in** (2026-10-08): it holds an opted-in pod's start until
  kube-router has programmed its rules, and fails open at `kube-vnet/network-max-wait`. Authelia
  is the first opt-in (60 s) because its startup checks are fatal.

## Consequences

- A new client of a service needs its label on the client and the client's namespace allowed on
  the vnet; a missing one looks like a timeout, not a refusal.
- Two instances of one app can share a namespace: a separate namespace adds no isolation that
  kube-vnet does not already give (2026-07-27).
- kube-vnet does not see secondary interfaces or egress; both need other mechanisms
  ([0031](0031-beyond-ingress-isolation.md)).
- Since 2026-10-05 it is a shared base deployed by every cluster
  ([0048](0048-several-clusters-one-repo.md)).
- A new pod's traffic is dropped for a few seconds until kube-router has programmed it; a pod
  that fails fatally on early network errors needs the network wait.

## References

- `cb7bf570`, `702c24ba`, `f2ce7994`, `238e67f8`, `d4c13059`, `bb8608a1` (network wait).
- Conversation 2026-07-08 (pod level), 2026-07-09 (narrow vnets), 2026-07-26
  (`allowedNamespaces`), 2026-07-27 (shared namespace).
- [shared/infra/kube-vnet](../../shared/infra/kube-vnet/README.md),
  [docs/migrating-swarm-to-kubernetes.md, 6.1](../migrating-swarm-to-kubernetes.md#61-kube-vnet-default-deny-ingress).
