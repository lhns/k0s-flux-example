# metallb

MetalLB **address configuration** (not the operator). MetalLB is installed by
the k0s helm extension in `k0sctl.yaml`; only its `IPAddressPool` /
`L2Advertisement` CRs live here — the same split used for cert-manager and
traefik (operator in k0s, config in Flux).

- `pool.yaml` — `traefik-vip` pool (`10.20.2.15/32`) + its L2 advertisement.

This adopts CRs that were previously applied by hand into GitOps; the content is
identical to the live objects, so bringing it under Flux is a no-op change of
ownership (traefik keeps `10.20.2.15`).
