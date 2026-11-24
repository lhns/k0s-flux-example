# 0068 — Workers allow 250 pods each

- Status: Accepted
- Date: 2026-10-28
- Current answer: the default workerProfile sets `maxPods: 250` (kubelet default 110). 250 is the
  most a worker's pod CIDR can hold without re-addressing the nodes.

## Context

The cluster has three workers, so the per-node limit is also the cluster's total pod capacity
divided by three. After drains the pods pile onto the remaining nodes and 110 is reached.

## Decision

- **`maxPods: 250`** in `workerProfiles[default]` in `kube-cluster/k0sctl.yaml`.
- The ceiling is the per-node pod CIDR: the controller manager assigns a /24 by default
  (`node-cidr-mask-size`), 254 usable addresses. Going higher needs a larger mask, which cannot be
  changed on nodes that already have a CIDR.

## Consequences

- A worker reads its profile only at k0s start; a profile-only change needs `k0sworker` restarted
  by hand (see the comment in `k0sctl.yaml`).
- Pod-to-node spread is still not rebalanced after a drain (`kube-cluster/TODO.md`).
- Per-node load, not the pod count, is the practical limit; 250 pods on one node still compete for
  CPU, memory and the container runtime.
