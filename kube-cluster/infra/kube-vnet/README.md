# kube-vnet (kube-cluster)

The shared base [shared/infra/kube-vnet](../../../shared/infra/kube-vnet/README.md) with this
cluster's version.

The k0s helm extension installed this release; Flux adopted it in place. Its namespace
`kube-vnet-system` predates Flux, so this component has no `namespaces/`.
