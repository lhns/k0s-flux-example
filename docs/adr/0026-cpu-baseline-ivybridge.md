# 0026 — The CPU baseline is IvyBridge: x86-64-v2 with AVX

- Status: Accepted
- Date: 2026-07-26, amended 2026-09-25
- Current answer: worker VMs use the Proxmox CPU type `IvyBridge`, which every host can run and
  live-migrate between (the oldest host has AVX but not AVX2). Software that needs AVX runs;
  software built for x86-64-v3 does not. When an image needs v3, the fix is a v2 build of that
  image, and when an image fails for lack of AVX, the fix is the VM CPU type, not an older image.

## Context

- MongoDB 5+ requires AVX, which the workers' first CPU type did not expose; `IvyBridge`
  provides it and stays migration-safe across all hosts.
- ceph-csi's upstream image since 3.18 is built on EL10, which requires x86-64-v3 and does not
  start on IvyBridge (PR #408).

## Decision

- Keep the hardware baseline; adapt the software.
- ceph-csi runs an unmodified upstream build on an EL9 base (`lhns/ceph-csi-x86-64-v2`, tested
  under `qemu -cpu IvyBridge`). Renovate reads chart and image from that image's tags, so a
  chart can only move once our build of the release exists (PR #419). The builder is not a fork:
  a daily job builds each upstream release it does not have yet.

## Consequences

- Every upstream move to a v3 baseline is a new build to maintain, as ceph-csi is now.
- The constraint is written where it bites (notesnook's MongoDB, the ceph-csi README), so nobody
  "fixes" a crash by downgrading.
- The baseline is not why notesnook stays on MongoDB 8; that is a driver constraint,
  [0051](0051-notesnook-mongodb-held-at-8.md).

## References

- `9c36023e`, `f289a095`, PR #408, PR #419.
- Conversation 2026-07-26 (no downgrade), 2026-09-24 to 2026-10-02 (the builder).
- [infra/ceph-csi-cephfs, Image](../../kube-cluster/infra/ceph-csi-cephfs/README.md#image-x86-64-v2-build).
