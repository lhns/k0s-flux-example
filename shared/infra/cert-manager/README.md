# cert-manager

[cert-manager](https://cert-manager.io), with its CRDs.

A shared base: each cluster deploys it from `<cluster>/infra/cert-manager/`, which holds the
chart version (`version.yaml`, patched onto the base) and that cluster's issuers.

- `release.yaml` — HelmRepository `jetstack` (OCI) + HelmRelease `cert-manager` without a
  version. The webhook runs two replicas spread across nodes, since it gates every
  Certificate/Issuer apply.

The admission webhook is apiserver-dialed → auto-allowed by kube-vnet (`ext.apiserver`); no
custom vnet.
