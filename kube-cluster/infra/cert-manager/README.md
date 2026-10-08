# cert-manager (kube-cluster)

The shared base [shared/infra/cert-manager](../../../shared/infra/cert-manager/README.md) with
this cluster's version, plus the IONOS DNS-01 webhook for Let's Encrypt certificates (e.g.
traefik's wildcard `*.kube.example.com`). The k0s helm extension installed the release; Flux adopted
it in place.

- `ionos-webhook.yaml` — the `cert-manager-webhook-ionos` DNS-01 solver (apiserver-dialed, so
  auto-allowed by kube-vnet).
- `ionos-secret.yaml` — SOPS-encrypted IONOS API creds (`ionos-secret`).
- `issuers.yaml` — two `ClusterIssuer`s: `letsencrypt-ionos-staging` and
  `letsencrypt-ionos-prod` (DNS-01 via the IONOS webhook, `acme.fabmade.de`).
