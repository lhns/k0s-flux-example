# flux-webhook

Push-based reconciliation: a GitHub webhook pokes Flux the moment you push, so
changes apply in seconds instead of waiting on the poll interval.

- `receiver.yaml` — notification-controller `Receiver` (`github`, push/ping) that
  reconciles the `flux-system` GitRepository; exposes `/hook/<hmac>`.
- `secret.yaml` — SOPS-encrypted `webhook-token` (the HMAC shared with the GitHub
  repo webhook).
- `ingress.yaml` — `flux-webhook.kube.example.com/hook` → the `webhook-receiver`
  service (traefik, wildcard TLS).
- `vnet-binding.yaml` — joins `notification-controller` to the `traefik` vnet
  (ingress) so traefik can reach it.
