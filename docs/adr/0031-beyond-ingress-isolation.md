# 0031 — Beyond ingress isolation: egress policies, Multus macvlan, a VPN gateway

- Status: Accepted
- Date: 2026-07-31, amended 2026-08-06, 2026-08-30 and 2026-09-24
- Current answer: kube-vnet stays ingress-only ([0013](0013-kube-vnet-default-deny-ingress.md)).
  Three additive mechanisms cover what it cannot: an `Egress` NetworkPolicy on workloads that run
  untrusted jobs (DNS and the public internet only); a Multus `macvlan` interface for the few pods
  that need the LAN itself (multicast discovery, IPv6-only Matter); and pod-gateway for namespaces
  whose egress must leave only through a VPN. `hostNetwork` is not used for apps.

## Context

- Pods are IPv4 single-stack behind kube-router and reachable only as unicast through MetalLB
  VIPs, so mDNS/SSDP never arrive and an IPv6-only protocol cannot run at all.
- A CI runner's job containers are SNAT'd to the runner pod, so they inherit its reach: the Ceph
  monitors, the API server, every LAN host and every pod.
- The Swarm's VPN-only egress was a Docker-socket trick with no Kubernetes equivalent.

## Decision

- **Multus with macvlan** (2026-07-31): vendored from upstream (no official chart), image pinned,
  cleanup on exit so removal is safe. That cleanup is why a CNI add-on may live in Flux at all: a
  revert deletes the DaemonSet and each node reverts itself, which is rehearsed before anything
  depends on it. `hostNetwork` is ruled out, and the Docker-era multicast relays cannot cross
  nodes on routed subnets. A pod with a LAN interface must not bind its service
  there: kube-vnet does not police secondary interfaces.
- **Egress NetworkPolicies** (2026-08-06): allow DNS, what the job legitimately needs, and the
  internet minus private ranges. The exclusion of `172.16/12` covers the pod and service networks.
  Targets are allowed by selector and by IP, because a VIP is DNAT'd before the policy sees it.
  Purely additive: kube-vnet writes no egress rules to clash with.
- **pod-gateway** (2026-08-30): a webhook rewrites pods in labelled namespaces to route through
  a gateway pod running gluetun, gated by an init container so the app never starts with its
  default route. Routed namespaces own themselves and receive the settings by Reflector.
- **Untrusted jobs need a pod of their own** (2026-09-24): a NetworkPolicy cannot tell apart
  containers SNAT'd to one shared pod, so confined CI gets its own remote-docker workspace
  ([0043](0043-ci-on-self-hosted-runners.md)).

## Consequences

- Egress is open by default for everything else; confining it is a per-workload decision.
- pod-gateway deploys two of the four layers its design brief calls for; its README says what a
  routed pod can still leak.

## References

- `5e920115`, `b678e30a`, `23895e6c`, `0e7e5cf3`, `3ac61d92`, `716b093f`, PR #415.
- Conversation 2026-07-30 (Multus under Flux, no `hostNetwork`).
- [infra/multus](../../kube-cluster/infra/multus/README.md),
  [infra/pod-gateway](../../kube-cluster/infra/pod-gateway/README.md).
