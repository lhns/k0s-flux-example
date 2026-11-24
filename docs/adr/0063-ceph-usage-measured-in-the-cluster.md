# 0063 — Ceph usage per owner is measured in the cluster, by a script on the Ceph image

- Status: Accepted
- Date: 2026-10-23
- Current answer: what each app, Swarm stack, VM and bucket stores in Ceph comes from
  `apps/storage-usage`, a Deployment that reads Ceph as a read-only client and the
  PersistentVolumes through the Kubernetes API. It is a Python script in a ConfigMap on the pinned
  upstream `quay.io/ceph/ceph` image. Ceph-side collection that needs nothing from the cluster
  stays under cephadm ([0056](0056-external-group-for-systems-outside-the-cluster.md)).

## Context

The mgr exports pool totals only: nothing per CephFS directory, RBD image or bucket. Who owns a
volume is recorded in its PersistentVolume, and ceph-csi's own metadata does not cover static
PVs. Usage per owner only matters while the cluster runs.

## Decision

- **In the cluster, not under cephadm.** The exporter joins Ceph's view with the PVs; on a Ceph
  host it would need a map generated from the cluster, which drifts, and a hand-applied spec.
  This is an exception to 0056's rule that a system able to run its collector runs it there:
  that rule holds for collection that needs only the system itself.
- **A script on the Ceph image, not an image of its own.** The Python bindings for librados,
  librbd and libcephfs ship with Ceph and follow its release; the script needs nothing beyond
  them and the standard library. An exception to [0009](0009-own-code-in-own-repos.md): a script
  that only reads this cluster, with its config beside it. It moves to its own repo once it needs
  a dependency or a build.
- **Read-only credentials, created on Ceph by hand**: cephx `client.storage-usage` (`r` on mon,
  mgr and mds; `rbd-read-only` on the RBD namespaces it measures) and an RGW user with read caps.
  Their keys are a SOPS Secret. Wider caps are a Ceph change, never made by the exporter's owner
  in passing.
- **Work on a schedule, nothing on scrape**; object scans run once a day, off the backup hours.

## Consequences

- No per-owner numbers while the cluster is down; Ceph's own metrics are unaffected.
- The pod reaches the monitors and OSDs because pod egress is open
  ([0031](0031-beyond-ingress-isolation.md)); an egress policy on it must allow the Ceph public
  network.
- The image follows the Ceph release; a bump that changes the bindings breaks the script.
- Its treemaps need the Business Charts panel, installed through `GF_INSTALL_PLUGINS` like the
  existing Grafana plugins, which keeps that exception to
  [0032](0032-files-come-from-images-never-fetched.md).

## References

- [apps/storage-usage](../../kube-cluster/apps/storage-usage/README.md).
