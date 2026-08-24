# ceph-csi-rbd

Ceph **RBD** block storage — the `ceph-rbd` StorageClass (`rbd.csi.ceph.com`).
Each PVC becomes an rbd image in the `kube` rados namespace of pool **`rbd.kube`**,
on the same Ceph cluster as cephfs (clusterID `fastappdata`, monitors
`10.20.2.101-103`).

Deployed in its own **`ceph-csi-rbd`** namespace (not co-located with the cephfs
driver in `ceph-csi-cephfs`) because both ceph-csi charts hardcode the same three
ConfigMap names and would collide in one namespace.

- `release.yaml` — HelmRepository `ceph-csi` + HelmRelease `ceph-csi-rbd`
  (`rbd.radosNamespace: kube` in `csiConfig`).
- `storageclass.yaml` — `ceph-rbd` (Retain, expandable). Uses the **shared**
  `csi-cephfs-secret` (client.kube creds) from the `ceph-csi-cephfs` namespace via
  the StorageClass's namespace-qualified secret refs — no separate secret here.

The `client.kube` cephx user needs RBD caps
(`osd 'profile rbd pool=rbd.kube namespace=kube'`, `mon/mgr 'profile rbd'`) in
addition to its cephfs caps.
