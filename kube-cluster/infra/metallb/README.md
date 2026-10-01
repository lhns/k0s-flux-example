# metallb

MetalLB, operator and address configuration both.

- `release.yaml` — HelmRepository + HelmRelease `metallb`, adopted in place from
  the release k0s installed, plus a `postRenderers` liveness-probe patch.
- `pool.yaml` — every `IPAddressPool` and its `L2Advertisement`. Each pool's
  comment says which app owns the VIP and why it needs one.
