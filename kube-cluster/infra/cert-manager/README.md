# cert-manager

[cert-manager](https://cert-manager.io) + the IONOS DNS-01 webhook, for automatic
Let's Encrypt certificates (e.g. traefik's wildcard `*.kube.example.com`).

- `release.yaml` — HelmRepository `jetstack` (OCI) + HelmRelease `cert-manager`
  `v1.20.3` (CRDs enabled).
- `ionos-webhook.yaml` — the `cert-manager-webhook-ionos` DNS-01 solver.
- `ionos-secret.yaml` — SOPS-encrypted IONOS API creds (`ionos-secret`).
- `issuers.yaml` — two `ClusterIssuer`s: `letsencrypt-ionos-staging` and
  `letsencrypt-ionos-prod` (DNS-01 via the IONOS webhook, `acme.fabmade.de`).

The admission + solver webhooks are apiserver-dialed → auto-allowed by kube-vnet
(`ext.apiserver`); no custom vnet.
