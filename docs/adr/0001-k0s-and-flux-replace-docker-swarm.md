# 0001 — k0s and Flux replace Docker Swarm

- Status: Accepted
- Date: 2026-07-03, amended 2026-07-08, 2026-07-25 and 2026-07-27
- Current answer: the services that ran on a Docker Swarm move into a k0s cluster reconciled
  by Flux from this repository. `k0sctl` creates the cluster and is run by hand; it installs
  k0s and the CNI and nothing else. Everything inside the cluster is Flux. The Swarm stays
  untouched and running as each service's rollback until its cutover is verified.

## Context

The services lived in Swarm stacks on hosts backed by an external Ceph cluster. Swarm offers no
privileged containers, no ingress policy model, and no declarative reconciliation of the whole
estate. k0s was chosen because it is simple to install, largely declarative, and balances being
opinionated with being open.

Cluster add-ons installed as k0s Helm extensions from `k0sctl.yaml` would leave two tools owning
what runs in the cluster.

## Decision

- **Flux reconciles everything in the cluster** from this repo.
- **k0sctl owns the cluster only** (k0s, CNI, control plane, node files). Helm extensions
  (Traefik, MetalLB, cert-manager, kube-vnet) are adopted in place by Flux HelmReleases with the
  same release name and namespace (a no-op upgrade), then removed from `k0sctl.yaml`, which has
  no `helm` block. `k0sctl.yaml` lives in the cluster's directory.
- **Node settings are k0sctl files** (2026-07-27): sysctls, containerd drop-ins and the syslog
  forwarder are files in `k0s-files/` uploaded by `k0sctl.yaml` with an apply hook, never an ad
  hoc change over SSH, so a rebuilt node gets them back.
- **Migration is per service and read-only on the Swarm side**: scaffold the component at
  `replicas: 0`, restore databases and copy or mount data, scale to 1, verify, then the
  owner stops the Swarm service. Cleaning up migration leftovers touches only the cluster, never
  the Swarm or its folders (2026-07-25). The runbook is
  [docs/migrating-swarm-to-kubernetes.md](../migrating-swarm-to-kubernetes.md).

## Consequences

- A cluster change outside Flux (`k0sctl.yaml`, node files under `k0s-files/`) needs a manual
  `k0sctl apply`, and for containerd daemon config a worker restart too.
- k0s versions still move through Flux, by Autopilot: [0018](0018-k0s-upgrades-through-autopilot.md).
- Large Swarm data was not copied but mounted where it lay:
  [0020](0020-swarm-data-mounted-in-place.md).
- etcd's disk is VM-level and outside both tools: [0023](0023-etcd-on-local-disk.md).

## References

- `2e26cff8`, `f4cced8b` (Flux manifests), `2c9ba795`/`d43ad066` (Traefik adopted, extension
  removed), `3f66ae3d`, `e6c9f7ac`, `a2eaff60`, `38778e17` (helm extensions block removed),
  `89f0fda0` (k0sctl.yaml into the cluster directory), `174f120a` (migration runbook),
  `5f6ee6e7`, `5144122c` (node files).
- Conversation 2026-07-25 (Swarm untouched), 2026-10-05 (why k0s).
