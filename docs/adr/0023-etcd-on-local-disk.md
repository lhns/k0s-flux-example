# 0023 — etcd lives on a local disk, never on network storage

- Status: Accepted
- Date: 2026-07-20
- Current answer: each controller VM keeps `/var/lib/k0s/etcd` on a second disk that is local to
  its host, formatted ext4, not on the Ceph-backed disk the VM boots from. This is VM-level setup,
  done by hand; nothing in this repo describes or enforces it.

## Context

etcd needs low fsync latency. On Ceph-backed VM storage it is far above what etcd tolerates,
which shows as API timeouts, leader-election churn in every controller and restarting pods.

## Decision

- **A genuinely local disk per controller**, migrated one member at a time, followers first, so
  quorum holds, after a manual `k0s backup`.
- **ext4**: a journalling filesystem for safety; ZFS is rejected for etcd, since copy-on-write
  hurts its fsync latency.
- Probe timeouts are raised as hardening.

## Consequences

- A rebuilt controller needs its local etcd disk set up by hand before it joins.
- A lost member re-syncs from the other two; only losing several needs a backup. A periodic
  `k0s backup` was recommended, because VM backups may not include a local disk, and is not set
  up.

## References

- `2e2c132f` (MetalLB probe timeouts).
- Conversation 2026-07-20.
