# metallb

[MetalLB](https://metallb.io), the operator only; each cluster's address pools live beside it.

A shared base: each cluster deploys it from `<cluster>/infra/metallb/`, which holds the chart
version (`version.yaml`, patched onto the base).

- `release.yaml` — HelmRepository + HelmRelease `metallb` without a version: chart defaults plus
  a `postRenderers` patch raising the frr-k8s probe timeouts to 5s.
