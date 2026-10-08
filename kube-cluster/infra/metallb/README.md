# metallb (kube-cluster)

The shared base [shared/infra/metallb](../../../shared/infra/metallb/README.md) with this
cluster's version, adopted in place from the release k0s installed.

- `pool.yaml` — every `IPAddressPool` and its `L2Advertisement`. Each pool's comment says which
  app owns the VIP and why it needs one.
